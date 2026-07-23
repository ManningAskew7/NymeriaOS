"""Graph build, tool selection, and graph cache for ``NymeriaAgent``.

This module owns the "given ``(user_id, thread_id)``, pick the LLM's tools and
return a runnable LangGraph" responsibility — the slice that used to live as a
~620-line cluster inside ``NymeriaAgent`` (formerly ``agent.py`` lines
1888–2524, extracted as a continuation of AGENT-001).

All functions take ``agent`` as the first argument and read/write state on
``agent`` directly. ``NymeriaAgent`` keeps thin facade methods (e.g.
``agent._select_tools_for_graph``) that delegate here, which preserves every
external caller and existing test patch site.

Graph cache state itself (``agent._user_graphs``, ``agent._async_user_graphs``,
``agent._graph_cache_lock``, ``agent._current_tool_superset_names``) stays on
the agent because external modules — routers, ``thread_deletion``,
``tool_reload``, ``search_skills`` — read and clear those attributes directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List

from langchain_core.tools import BaseTool

from ..skills.meta_tool import create_skill_meta_tool
from ..vendor.react_agent import AgentConfig, create_graph
from .time_utils import ensure_aware_utc, utc_now

if TYPE_CHECKING:
    from .agent import NymeriaAgent

logger = logging.getLogger(__name__)


def _resolve_owner_role(agent: "NymeriaAgent", account_id: str) -> str:
    """Resolve an account's role for tool gating, defaulting to ``"user"``.

    ``account_id`` is whichever id owns the tools being gated: the caller's
    ``user_id`` for the default/dream/enabled paths, or the thread *owner's*
    id for callable-thread tools. Returns ``"user"`` when the id is empty or
    the account is unknown, the conservative least-privilege default used at
    every graph-build role gate.
    """
    owner = agent.accounts_repo.get_user_by_id(account_id) if account_id else None
    return owner.role if owner else "user"


def _apply_role_gates(tool_names, owner_role: str) -> tuple[set, set]:
    """Strip admin-only then developer-only tools for ``owner_role``.

    Returns ``(allowed, blocked)`` where ``blocked`` is the union of the
    admin-gated and developer-gated names. This is the defense-in-depth
    graph-build filter; the authoritative gate runs at tool-enable time.
    Callers keep their own context-specific warning log for ``blocked`` so the
    four gating sites preserve their distinct thread/owner/scope messages.
    """
    from ..tools import filter_admin_only_tools, filter_developer_only_tools

    allowed, blocked_admin = filter_admin_only_tools(tool_names, owner_role)
    allowed, blocked_dev = filter_developer_only_tools(allowed, owner_role)
    return allowed, blocked_admin | blocked_dev


def get_team_scoped_callable_threads(
    agent: "NymeriaAgent",
    *,
    user_id: str,
    caller_thread_id: str,
) -> List:
    """Return the caller's owned callable threads, scoped by team.

    Teams are fully isolated in both directions (backlog #97, 2026-07-23): a
    thread with ``callable_team_id`` sees only callable threads in the same
    team, and an unteamed thread sees only unteamed callables. The former
    legacy behavior (unteamed threads keep the owner-wide list) is gone.
    """
    owned = set(agent.accounts_repo.list_threads_for_user(user_id))
    owned_callables = agent.thread_config_manager.list_callable_threads(
        owned_thread_ids=owned
    )

    caller_tc = (
        agent.thread_config_manager.get_config(caller_thread_id)
        if caller_thread_id
        else None
    )
    team_id = getattr(caller_tc, "callable_team_id", None) if caller_tc else None
    return [
        callable_tc
        for callable_tc in owned_callables
        if (getattr(callable_tc, "callable_team_id", None) or None) == (team_id or None)
    ]


def is_callable_visible_to_thread(
    agent: "NymeriaAgent", caller_thread_id: str, target_thread_id: str
) -> bool:
    """Runtime defense for team-scoped callable tool visibility.

    A stale graph may still contain a callable tool after team membership
    changes. Teams are isolated in both directions (backlog #97): the caller
    and target must be in the SAME team, where "no team" is itself a bubble
    (an unteamed caller can invoke only unteamed callables). A call with no
    caller thread context is not team-scoped and stays allowed.
    """
    if not caller_thread_id:
        return True
    caller_tc = agent.thread_config_manager.get_config(caller_thread_id)
    caller_team_id = (
        getattr(caller_tc, "callable_team_id", None) if caller_tc else None
    ) or None
    target_tc = agent.thread_config_manager.get_config(target_thread_id)
    target_team_id = (
        getattr(target_tc, "callable_team_id", None) if target_tc else None
    ) or None
    return caller_team_id == target_team_id


def get_callable_thread_tools(agent: "NymeriaAgent", tc) -> List[BaseTool]:
    """Get tools for a callable thread.

    Gives the standard tool set plus the callable thread's *owner's* other
    callable thread tools, excluding only this thread's own callable tool
    to prevent self-invocation loops. Cross-user callables are excluded
    so the second user's "Helper" doesn't appear in Owner's callable thread, even when
    the callable thread itself runs as a sub-agent.
    """
    from ..tools import (
        resolve_default_tool_names,
        static_tool_catalog,
    )

    own_callable_name = tc.callable_name
    owner_id = agent.accounts_repo.get_thread_owner(tc.thread_id) or "default"

    profile = agent.profile_manager.get_profile(owner_id)
    default_tools = profile.tool_preferences.default_thread_tools

    all_tools_dict = static_tool_catalog()

    core_names = resolve_default_tool_names(default_tools)
    if default_tools is not None:
        owner_role = _resolve_owner_role(agent, owner_id)
        allowed_core, blocked_core = _apply_role_gates(core_names, owner_role)
        if blocked_core:
            logger.warning(
                "Callable graph build for thread=%s owner=%s: stripped "
                "role-gated default tools %s",
                tc.thread_id, owner_id, sorted(blocked_core),
            )
        core_names = [name for name in core_names if name in allowed_core]
    tools = [
        all_tools_dict[name] for name in core_names
        if name in all_tools_dict
    ]

    # Include the owner's other callable thread tools (excluding self).
    existing_names = {t.name for t in tools}
    owned_callables = agent._get_team_scoped_callable_threads(
        user_id=owner_id,
        caller_thread_id=tc.thread_id,
    )
    from ..agents.tool_factory import create_callable_thread_tool
    for callable_tc in owned_callables:
        if (
            not callable_tc.callable_name
            or callable_tc.callable_name == own_callable_name
            or callable_tc.callable_name in existing_names
        ):
            continue
        try:
            tools.append(create_callable_thread_tool(callable_tc))
            existing_names.add(callable_tc.callable_name)
        except Exception as e:
            logger.warning(
                f"Failed to build sibling callable tool for {callable_tc.thread_id}: {e}"
            )

    return tools


def _active_skills_for_thread(agent: "NymeriaAgent", user_id: str, tc) -> List:
    """Resolve the thread's active skill set.

    Combines the user's enabled_global_skills with ThreadConfig overrides
    (enabled_skills ∪ disabled_skills). Shared by the Skill meta-tool build
    and the kit-declared template tool build so the two cannot disagree.
    """
    if agent.skill_manager is None:
        return []
    try:
        profile = agent.profile_manager.get_profile(user_id)
        enabled_global = list(getattr(profile, "enabled_global_skills", []) or [])
    except Exception:
        enabled_global = []

    enabled_thread = list(tc.enabled_skills) if tc and tc.enabled_skills else []
    disabled_thread = list(tc.disabled_skills) if tc and tc.disabled_skills else []

    return agent.skill_manager.list_for_thread(
        user_id=user_id,
        enabled_global_skills=enabled_global,
        thread_enabled_skills=enabled_thread,
        thread_disabled_skills=disabled_thread,
    )


def build_skill_meta_tool(
    agent: "NymeriaAgent", user_id: str, tc, thread_tools: List[BaseTool]
):
    """Return the Skill meta-tool for this thread, or None if no skills are active.

    Returns None when the active set is empty so we don't pay tool-schema
    overhead needlessly.
    """
    if agent.skill_manager is None:
        return None
    active = _active_skills_for_thread(agent, user_id, tc)
    if not active:
        return None

    return create_skill_meta_tool(
        active_skills=active,
        skill_manager=agent.skill_manager,
        user_id=user_id,
        thread_tool_names=[t.name for t in thread_tools],
    )


def build_template_thread_tools(
    agent: "NymeriaAgent", user_id: str, tc, existing_names
) -> List[BaseTool]:
    """Synthesize kit-declared thread-template tools for the ACTIVE skills.

    Derived state (backlog #26): the kit being active on the thread IS the
    registration, so activation is idempotent and deactivation removes the
    tools at the next build. Names already bound win (a materialized thread's
    ordinary callable tool shadows its template tool), and the thread's
    authoritative ``disabled_tools`` applies by name.
    """
    if getattr(agent, "skill_manager", None) is None:
        return []
    from ..agents.tool_factory import create_template_thread_tool

    try:
        active = _active_skills_for_thread(agent, user_id, tc)
    except Exception:  # noqa: BLE001 - template surfacing must not break a build
        logger.warning(
            "Active-skill resolution failed during template tool build",
            exc_info=True,
        )
        return []

    disabled = set(tc.disabled_tools or []) if tc else set()
    own_callable_name = (
        tc.callable_name if tc is not None and getattr(tc, "callable", False) else None
    )
    seen = set(existing_names)
    tools: List[BaseTool] = []
    for skill in active:
        for template in skill.thread_templates:
            if own_callable_name and template.name == own_callable_name:
                # Mirror get_callable_thread_tools' self-exclusion: never
                # hand a callable thread a tool that invokes itself (an ask
                # would block on the thread's own lock until tool timeout).
                continue
            if template.name in disabled:
                continue
            if template.name in seen:
                # A bound tool (or an earlier kit's template) owns the name;
                # post-materialization this is the normal shadowing path.
                logger.debug(
                    "thread template %r from kit %r shadowed by an existing "
                    "tool; skipping", template.name, skill.name,
                )
                continue
            try:
                tools.append(create_template_thread_tool(skill.name, template))
                seen.add(template.name)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to build template tool %r from kit %r: %s",
                    template.name, skill.name, exc,
                )
    return tools


def _apply_execution_environment_descriptions(
    agent: "NymeriaAgent",
    tools: List[BaseTool],
) -> None:
    try:
        from ..tools.execution_environment import configure_environment_aware_tool_descriptions

        configure_environment_aware_tool_descriptions(
            tools,
            getattr(agent, "execution_environment", None),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Execution-aware tool description update failed: %s", exc)


def _select_dream_tools_for_graph(
    agent: "NymeriaAgent",
    user_id: str,
    thread_id: str,
    tc,
) -> List[BaseTool]:
    """Return the strict tool allowlist for a dream shadow thread."""
    from .dreaming import (
        DEFAULT_DREAM_ENABLED_CORE_TOOLS,
        DEFAULT_DREAM_ENABLED_OPTIONAL_TOOLS,
    )
    from ..tools import (
        CATALOG_TOOLS,
        static_tool_catalog,
    )

    all_tools_dict = static_tool_catalog()

    disabled = set(tc.disabled_tools or [])
    requested_extras = set(tc.enabled_tools or []) - disabled
    policy_extras = set(DEFAULT_DREAM_ENABLED_OPTIONAL_TOOLS)
    extra_names = {
        name
        for name in requested_extras
        if name in CATALOG_TOOLS and name in policy_extras
    }
    ignored_outside_policy = requested_extras - extra_names
    if ignored_outside_policy:
        logger.warning(
            "Dream graph build for thread=%s: ignored enabled tool(s) "
            "outside dream policy: %s",
            thread_id,
            sorted(ignored_outside_policy),
        )

    owner_role = _resolve_owner_role(agent, user_id)
    allowed_extras, blocked_extras = _apply_role_gates(extra_names, owner_role)
    if blocked_extras:
        logger.warning(
            "Dream graph build for thread=%s user=%s: stripped role-gated "
            "tools %s from enabled_tools",
            thread_id,
            user_id,
            sorted(blocked_extras),
        )

    ordered_names: list[str] = [
        name
        for name in DEFAULT_DREAM_ENABLED_CORE_TOOLS
        if name not in disabled
    ]
    ordered_names.extend(
        name for name in sorted(allowed_extras) if name not in ordered_names
    )

    missing = [name for name in ordered_names if name not in all_tools_dict]
    if missing:
        logger.warning(
            "Dream graph build for thread=%s: configured tool(s) not found: %s",
            thread_id,
            missing,
        )

    tools = [all_tools_dict[name] for name in ordered_names if name in all_tools_dict]
    _apply_execution_environment_descriptions(agent, tools)
    return tools


def _nested_deps_digest(agent: "NymeriaAgent", skill, user_id: str) -> str:
    """One-level nested-dependency digest for an active skill.

    Folds each ``required_skills`` entry's resolved frontmatter (name, scope,
    required tools, TTL) so a defer=false activation's expanded bind set is
    part of the graph-cache key; an unresolvable name folds as missing.
    """
    if not skill.required_skills:
        return ""
    from ..skills import resolve_nested_skills

    nested, missing = resolve_nested_skills(skill, agent.skill_manager, user_id)
    parts = [
        f"{n.name}:{n.scope}:{sorted(n.required_tools)}:{n.tool_ttl}"
        for n in nested
    ]
    parts.extend(f"missing:{name}" for name in missing)
    return ";".join(parts)


def skills_fingerprint(agent: "NymeriaAgent", user_id: str, thread_id: str) -> str:
    """Hash inputs that affect the Skill meta-tool's description.

    Included so the per-(user, thread) graph cache invalidates when:
    - the user toggles a skill in enabled_global_skills
    - the thread flips enabled_skills / disabled_skills
    - an active skill's frontmatter (name, description, allowed_tools,
      required tools/skills or their one-level nested resolution) changes on disk
    """
    if agent.skill_manager is None:
        return "nosm"
    try:
        profile = agent.profile_manager.get_profile(user_id)
        enabled_global = list(getattr(profile, "enabled_global_skills", []) or [])
    except Exception:
        enabled_global = []
    tc = agent.thread_config_manager.get_config(thread_id) if thread_id else None
    enabled_thread = list(tc.enabled_skills) if tc and tc.enabled_skills else []
    disabled_thread = list(tc.disabled_skills) if tc and tc.disabled_skills else []
    active = agent.skill_manager.list_for_thread(
        user_id=user_id,
        enabled_global_skills=enabled_global,
        thread_enabled_skills=enabled_thread,
        thread_disabled_skills=disabled_thread,
    )
    parts = [
        f"{s.name}:{s.scope}:{hash(s.description)}:{sorted(s.allowed_tools)}:"
        f"{sorted(s.required_tools)}:{s.tool_ttl}:"
        f"{sorted(s.required_skills)}:{_nested_deps_digest(agent, s, user_id)}:"
        f"{_templates_digest(s)}"
        for s in active
    ]
    return f"sk:{hash('|'.join(parts))}"


def _templates_digest(skill) -> str:
    """Normalized digest of a skill's thread templates (order-sensitive)."""
    templates = skill.thread_templates
    if not templates:
        return ""
    try:
        return json.dumps(
            [t.model_dump() for t in templates], sort_keys=True, default=str
        )
    except Exception:  # noqa: BLE001 - a digest failure must not break a build
        logger.debug("template digest failed for %r", skill.name, exc_info=True)
        return "digest-error"


def select_tools_for_graph(agent: "NymeriaAgent", user_id: str, thread_id: str):
    """Select and filter the tool list for a graph build.

    Handles core tool selection, per-user callable threads, per-thread
    filtering (enabled/disabled/temporary), admin-only gating, MCP tools,
    and Skill meta-tool injection. Shared by both sync and async graph
    build paths.

    Returns:
        (tools, tc) where tc is the thread config (or None).
    """
    tc = agent.thread_config_manager.get_config(thread_id) if thread_id else None
    if tc and tc.shadow_parent_id:
        return _select_dream_tools_for_graph(agent, user_id, thread_id, tc), tc

    # Resolved lazily (and at most once) from user_id by the core and extras
    # role gates below; the callable path leaves it None so the extras gate
    # resolves it there.
    owner_role: str | None = None

    if tc and tc.callable and tc.callable_name:
        tools = agent._get_callable_thread_tools(tc)
    else:
        profile = agent.profile_manager.get_profile(user_id)
        default_tools = profile.tool_preferences.default_thread_tools

        from ..tools import (
            resolve_default_tool_names,
            static_tool_catalog,
        )
        all_tools_dict = static_tool_catalog()

        core_names = resolve_default_tool_names(default_tools)
        if default_tools is not None:
            owner_role = _resolve_owner_role(agent, user_id)
            allowed_core, blocked_core = _apply_role_gates(core_names, owner_role)
            if blocked_core:
                logger.warning(
                    "Graph build for thread=%s user=%s: stripped "
                    "role-gated default tools %s",
                    thread_id, user_id, sorted(blocked_core),
                )
            core_names = [name for name in core_names if name in allowed_core]
        tools = [all_tools_dict[name] for name in core_names if name in all_tools_dict]

        # Per-user callable thread tools. Built fresh from the caller's
        # owned callable threads (NOT from agent.tool_registry) so that:
        #   1. Owner doesn't see the second user's callable names/descriptions in their
        #      tool list — descriptions are part of the system prompt.
        #   2. Two users can each name a callable "Helper" without the
        #      global registry's last-write-wins collision rewriting one
        #      of them — each user's graph binds their own version.
        # The runtime ownership gate in create_callable_thread_tool is the
        # second line of defense; this filter is the first.
        existing_names = {t.name for t in tools}
        owned_callables = agent._get_team_scoped_callable_threads(
            user_id=user_id,
            caller_thread_id=thread_id,
        )
        from ..agents.tool_factory import create_callable_thread_tool
        for callable_tc in owned_callables:
            if not callable_tc.callable_name or callable_tc.callable_name in existing_names:
                continue
            try:
                tools.append(create_callable_thread_tool(callable_tc))
                existing_names.add(callable_tc.callable_name)
            except Exception as e:
                logger.warning(
                    f"Failed to build callable tool for {callable_tc.thread_id}: {e}"
                )

        if default_tools is not None:
            existing_names = {t.name for t in tools}
            for name in core_names:
                if name.startswith("mcp__") and name not in existing_names:
                    reg_tool = agent.tool_registry.get_tool(name)
                    if reg_tool:
                        tools.append(reg_tool)

    # Apply per-thread tool filtering. disabled_tools is AUTHORITATIVE —
    # it filters both the default-bound set AND the extras (enabled_tools
    # ∪ live_temp). Without this, `tool_manage(action="disable", ...)`
    # would have to destructively remove from enabled_tools/temporary_tools
    # to actually disable a tool that's in both lists, which means a
    # subsequent un-disable couldn't restore the original state. By
    # making disabled authoritative we let _disable just add to
    # disabled_tools and keep the original enabled_tools/temporary_tools
    # entries intact, so un-disable is a true restore.
    if tc:
        disabled = set(tc.disabled_tools) if tc.disabled_tools else set()
        if disabled:
            tools = [t for t in tools if t.name not in disabled]
        live_temp = agent._resolve_temporary_tools(tc)
        extra_names = (set(tc.enabled_tools) | live_temp) - disabled
        if extra_names:
            if owner_role is None:
                owner_role = _resolve_owner_role(agent, user_id)
            allowed_extras, blocked_extras = _apply_role_gates(extra_names, owner_role)
            if blocked_extras:
                logger.warning(
                    "Graph build for thread=%s user=%s: stripped role-gated "
                    "tools %s from enabled_tools (non-admin owner)",
                    thread_id, user_id, sorted(blocked_extras),
                )
            extra_names = allowed_extras
        if extra_names:
            from ..tools import static_tool_catalog
            all_tools_dict = static_tool_catalog()
            callable_names = set((agent._callable_tool_thread_map or {}).keys())
            existing = {t.name for t in tools}
            for name in extra_names:
                if name in existing:
                    continue
                if name in all_tools_dict:
                    tools.append(all_tools_dict[name])
                    continue
                if name in callable_names:
                    continue
                reg_tool = agent.tool_registry.get_tool(name)
                if reg_tool:
                    tools.append(reg_tool)

    # Kit-declared thread templates (backlog #26): derived from the thread's
    # ACTIVE skill set, after the disabled/extras passes so bound tools (and a
    # materialized thread's ordinary callable tool) win name collisions and
    # disabled_tools stays authoritative.
    try:
        tools.extend(
            agent._build_template_thread_tools(
                user_id, tc, {t.name for t in tools}
            )
        )
    except Exception as exc:  # noqa: BLE001 - template surfacing is best-effort
        logger.warning("Template thread tool injection failed: %s", exc)

    # Token optimization: when unbound direct calls are allowed (dynamic binding
    # only), the resident tool_invoke tool is redundant because the model can
    # call unbound tools directly, so drop it from the BOUND schema to save its
    # tokens every request. It stays in the dispatch superset, so an explicit
    # tool_invoke call is still gated (excluded) rather than silently running.
    _settings = getattr(agent, "settings", None)
    if bool(getattr(_settings, "allow_unbound_tool_calls", False)) and bool(
        getattr(_settings, "dynamic_tool_binding", False)
    ):
        tools = [t for t in tools if getattr(t, "name", None) != "tool_invoke"]

    skill_tool = agent._build_skill_meta_tool(user_id, tc, tools)
    if skill_tool is not None:
        tools.append(skill_tool)

    _apply_execution_environment_descriptions(agent, tools)

    return tools, tc


def tool_config_hash(agent: "NymeriaAgent", user_id: str, thread_id: str, tc) -> str:
    """Stable hash over the tool-relevant slice of ThreadConfig.

    Used by the dynamic-binding resolver to skip re-binding the LLM when
    consecutive agent steps see no change to the resolved tool set. Keeps
    Anthropic prompt cache hit-rate high: if the hash is unchanged, the
    previously bound LLM (and thus the tools block in the request prefix)
    is reused verbatim.

    Includes enabled/disabled/temporary tools plus enabled/disabled skills
    because skills affect which tools the meta-tool exposes. Temporary
    tools are reduced to (name, expires_at) so a passive tick doesn't
    invalidate the hash; only actual mutations do.
    """
    if tc is None:
        parts: List[Any] = [[], [], [], [], []]
    else:
        now = utc_now()
        live_temp = [
            (name, ensure_aware_utc(entry.expires_at).isoformat())
            for name, entry in (tc.temporary_tools or {}).items()
            if ensure_aware_utc(entry.expires_at) > now
        ]
        parts = [
            sorted(tc.enabled_tools or []),
            sorted(tc.disabled_tools or []),
            sorted(live_temp),
            sorted(tc.enabled_skills or []),
            sorted(tc.disabled_skills or []),
        ]
    # Include user_id so a thread reassigned across users (rare, but
    # possible via tooling) gets a fresh resolution.
    blob = json.dumps([user_id, thread_id, parts], default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def make_dynamic_tool_resolver(agent: "NymeriaAgent", user_id: str, thread_id: str):
    """Return a () -> (tools, cache_key_hash) callable for the dynamic node.

    The closure has no internal cache of its own — it always reads fresh
    ThreadConfig and resolves through select_tools_for_graph. Bound-LLM
    caching lives in the model node (keyed on cache_key_hash) so that
    per-step state stays in the node, not in the resolver.
    """
    def resolve():
        tools, tc = agent._select_tools_for_graph(user_id, thread_id)
        cache_key = agent._tool_config_hash(user_id, thread_id, tc)
        return tools, cache_key

    return resolve


def compute_tool_superset(agent: "NymeriaAgent", user_id: str, thread_id: str):
    """Compute the full set of tools the dynamic ToolNode must dispatch.

    Returns (tools_list, names_set). The ToolNode in dynamic mode is
    constructed with the superset so that any tool the model binds at
    any step (which may be a subset varying step-to-step) can still be
    executed. A tool created after graph construction can still be
    dispatched because SafeToolNode refreshes missing tool names from the
    live dynamic resolver before rejecting a call.

    Sources merged (deduped by name):
      - SEED_TOOLS (core)
      - CATALOG_TOOLS (all optional, gated downstream by role/disable)
      - tool_registry.all_tools() (MCP + dynamically registered tools)
      - owned callable threads (team-scoped, per user)
      - skill meta-tool (if the user has any active skills)

    Admin/dev-only gating is NOT applied here — the model node's bound
    list (from select_tools_for_graph) handles visibility. The
    superset is purely for execution dispatch.
    """
    tc = agent.thread_config_manager.get_config(thread_id) if thread_id else None
    if tc and tc.shadow_parent_id:
        tools = _select_dream_tools_for_graph(agent, user_id, thread_id, tc)
        return tools, {tool.name for tool in tools}

    from ..tools import SEED_TOOLS, CATALOG_TOOLS

    merged: Dict[str, BaseTool] = {t.name: t for t in SEED_TOOLS}
    for name, tool in CATALOG_TOOLS.items():
        merged.setdefault(name, tool)

    if getattr(agent, "tool_registry", None):
        try:
            for tool in agent.tool_registry.get_all_tools():
                if tool.name not in merged:
                    merged[tool.name] = tool
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "tool_registry.get_all_tools() failed during superset build: %s", exc
            )

    try:
        owned_callables = agent._get_team_scoped_callable_threads(
            user_id=user_id,
            caller_thread_id=thread_id,
        )
        from ..agents.tool_factory import create_callable_thread_tool
        for callable_tc in owned_callables:
            if not callable_tc.callable_name or callable_tc.callable_name in merged:
                continue
            try:
                merged[callable_tc.callable_name] = create_callable_thread_tool(callable_tc)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to build callable tool for %s during superset build: %s",
                    callable_tc.thread_id, exc,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Owned-callable superset enumeration failed: %s", exc)

    # Kit-declared thread templates: the SUPERSET includes templates from ALL
    # skills installed and visible to the user (not just active ones), so the
    # deferred path (tool_invoke) can execute a template without the kit being
    # enabled, mirroring the "visibility is not reachability" loadability
    # rule. Bound graphs stay active-only via build_template_thread_tools.
    try:
        skill_manager = getattr(agent, "skill_manager", None)
        if skill_manager is not None:
            from ..agents.tool_factory import create_template_thread_tool

            for skill in skill_manager.list_installed(user_id=user_id):
                for template in skill.thread_templates:
                    if template.name in merged:
                        continue
                    merged[template.name] = create_template_thread_tool(
                        skill.name, template
                    )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Template tool superset enumeration failed: %s", exc)

    # Skill meta-tool: only present when the user actually has active
    # skills for this thread. We approximate by reading thread tools and
    # calling build_skill_meta_tool with the merged superset so its
    # description sees every potentially-callable tool name.
    try:
        skill_tool = agent._build_skill_meta_tool(user_id, tc, list(merged.values()))
        if skill_tool is not None and skill_tool.name not in merged:
            merged[skill_tool.name] = skill_tool
    except Exception as exc:  # noqa: BLE001
        logger.warning("Skill meta-tool superset injection failed: %s", exc)

    return list(merged.values()), set(merged.keys())


def build_agent_config(
    agent: "NymeriaAgent",
    system_prompt: str,
    checkpointer_config,
    thread_id: str,
    tc,
    acting_user_id: str | None = None,
):
    """Build an AgentConfig with the given checkpointer config.

    ``acting_user_id`` (the graph's user) is the credential-owner fallback
    for threads without a ``thread_owners`` row, so autonomous turns on
    synthetic thread ids resolve user-owned vault LLM credentials like
    interactive turns do (dev-todo #76). The one-arg accessor call shape is
    preserved when absent so single-parameter monkeypatch stubs keep working.
    """
    if acting_user_id:
        llm_config = agent._get_llm_config_for_thread(thread_id, acting_user_id)
    else:
        llm_config = agent._get_llm_config_for_thread(thread_id)

    if tc and tc.callable and tc.callable_name:
        max_iters = tc.callable_max_iterations or agent.CALLABLE_DEFAULT_MAX_ITERATIONS
    else:
        from .agent_safety import main_iterations_cap

        max_iters = main_iterations_cap(agent)

    return AgentConfig(
        llm=llm_config,
        checkpointer=checkpointer_config,
        system_prompt=system_prompt,
        max_iterations=max_iters,
        repeated_tool_result_limit=agent.TURN_SAME_TOOL_RESULT_LIMIT,
        tool_timeout=agent.settings.tool_timeout,
        tool_output_max_chars=agent.settings.tool_output_max_chars,
        verbose=agent.settings.log_level == "DEBUG",
        on_timeout=agent._on_tool_timeout,
    )


def is_dynamic_tool_binding(agent: "NymeriaAgent") -> bool:
    """Return True when dynamic-binding mode is on.

    Reads ``agent.settings.dynamic_tool_binding`` live (not a cached
    attribute) so a PATCH /settings call refreshing ``agent.settings``
    flips the next graph build to/from dynamic mode without waiting
    for a full process restart.
    """
    return bool(getattr(agent.settings, "dynamic_tool_binding", False))


def build_graph_with_prompt(
    agent: "NymeriaAgent",
    system_prompt: str,
    user_id: str = "default",
    thread_id: str = "",
):
    """Build a sync LangGraph execution graph with a specific system prompt."""
    if agent._is_dynamic_tool_binding():
        return agent._build_dynamic_graph_with_prompt(
            system_prompt, user_id, thread_id, agent._checkpointer_config
        )
    tools, tc = agent._select_tools_for_graph(user_id, thread_id)
    config = agent._build_agent_config(
        system_prompt, agent._checkpointer_config, thread_id, tc, acting_user_id=user_id
    )
    return create_graph(config=config, tools=tools)


def build_async_graph_with_prompt(
    agent: "NymeriaAgent",
    system_prompt: str,
    user_id: str = "default",
    thread_id: str = "",
):
    """Build an async LangGraph execution graph with a specific system prompt."""
    if agent._is_dynamic_tool_binding():
        return agent._build_dynamic_graph_with_prompt(
            system_prompt, user_id, thread_id, agent._async_checkpointer_config
        )
    tools, tc = agent._select_tools_for_graph(user_id, thread_id)
    config = agent._build_agent_config(
        system_prompt,
        agent._async_checkpointer_config,
        thread_id,
        tc,
        acting_user_id=user_id,
    )
    return create_graph(config=config, tools=tools)


def build_dynamic_graph_with_prompt(
    agent: "NymeriaAgent",
    system_prompt: str,
    user_id: str,
    thread_id: str,
    checkpointer_config,
):
    """Build a graph wired for per-step dynamic tool resolution.

    The agent node receives a resolver closure (recomputes tools per call
    from ThreadConfig); the ToolNode is constructed with the superset so
    any tool the resolver may return is executable. ``tool_search`` and
    the tools node can dynamically resolve post-build tools from the same
    resolver before dispatch.
    """
    # Compute the initial executor superset for existing tools. Post-build
    # tools are resolved dynamically by SafeToolNode from the same resolver.
    superset_tools, superset_names = agent._compute_tool_superset(user_id, thread_id)
    agent._current_tool_superset_names = superset_names

    tc = agent.thread_config_manager.get_config(thread_id) if thread_id else None
    config = agent._build_agent_config(
        system_prompt, checkpointer_config, thread_id, tc, acting_user_id=user_id
    )
    resolver = agent._make_dynamic_tool_resolver(user_id, thread_id)
    return create_graph(
        config=config,
        tools=superset_tools,
        dynamic_tool_resolver=resolver,
        superset_tools=superset_tools,
    )


def get_cached_graph_entry(
    agent: "NymeriaAgent",
    cache: Dict[tuple, tuple],
    cache_key: tuple,
    memory_hash: str,
):
    with agent._graph_cache_lock:
        if cache_key not in cache:
            return None

        cached_hash, cached_graph = cache[cache_key]
        if cached_hash != memory_hash:
            cache.pop(cache_key, None)
            return None

        cache.pop(cache_key)
        cache[cache_key] = (cached_hash, cached_graph)
        return cached_graph


def store_cached_graph_entry(
    agent: "NymeriaAgent",
    cache: Dict[tuple, tuple],
    cache_key: tuple,
    memory_hash: str,
    graph,
) -> None:
    with agent._graph_cache_lock:
        if cache_key in cache:
            cache.pop(cache_key, None)
        elif len(cache) >= agent._GRAPH_CACHE_MAX:
            oldest_key = next(iter(cache))
            del cache[oldest_key]
        cache[cache_key] = (memory_hash, graph)


def get_graph_for_user_impl(
    agent: "NymeriaAgent",
    user_id: str,
    thread_id: str,
    cache: Dict[tuple, tuple],
    build_fn,
    cache_key_fn=None,
):
    """Shared implementation for sync/async graph-for-user lookup.

    Handles caching and LRU eviction. ``build_fn`` is either
    ``agent._build_graph_with_prompt`` or
    ``agent._build_async_graph_with_prompt``.

    The system prompt is source-invariant, so autonomous and interactive
    turns share the same cached graph for a given (user_id, thread_id).
    """
    # Hot-load chokepoint: pick up raw on-disk edits to the custom-tool /
    # MCP stores before any cache-freshness decision, so an external edit
    # reaches this very build (debounced stat scans; usually a no-op).
    try:
        agent._sync_external_resource_edits()
    except Exception as exc:  # noqa: BLE001 - freshness must never break a turn
        logger.warning("External resource-edit sync failed: %s", exc)

    if thread_id:
        try:
            agent._clear_expired_llm_fallback_if_idle(thread_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to clear expired LLM fallback before graph lookup for %s: %s",
                thread_id,
                exc,
            )

    memory_hash = agent._get_memory_hash(user_id, thread_id)
    build_cache_key = cache_key_fn or (lambda u, t: (u, t))
    cache_key = build_cache_key(user_id, thread_id)

    cached_graph = agent._get_cached_graph_entry(cache, cache_key, memory_hash)
    if cached_graph is not None:
        return cached_graph

    profile = agent.profile_manager.get_profile(user_id)
    todo_list = agent.todo_manager.get_todos(user_id)
    has_memories = profile.memories or profile.personality_overrides
    has_todos = bool(
        todo_list.get_active_todos_for_thread(thread_id) if thread_id
        else todo_list.get_active_todos()
    )
    has_tool_prefs = profile.tool_preferences.default_thread_tools is not None
    has_thread_config = bool(
        thread_id and agent.thread_config_manager.get_config(thread_id)
    )

    if not has_memories and not has_todos and not has_tool_prefs and not has_thread_config:
        # No-customization path: cannot reuse the default graph because
        # it was built without a user_id at startup, so its callable tool
        # list contains every user's callables (cross-user leak). Build a
        # per-user graph and cache under the sentinel thread_id "".
        no_cust_key = build_cache_key(user_id, "")
        cached_graph = agent._get_cached_graph_entry(
            cache, no_cust_key, memory_hash
        )
        if cached_graph is not None:
            return cached_graph
        graph = build_fn(agent._base_system_prompt, user_id=user_id)
        agent._store_cached_graph_entry(cache, no_cust_key, memory_hash, graph)
        return graph

    logger.debug(f"Building new graph for user {user_id}, thread {thread_id} (context or tools changed)")
    full_prompt = agent._build_full_system_prompt(user_id, thread_id=thread_id)
    graph = build_fn(full_prompt, user_id=user_id, thread_id=thread_id)

    agent._store_cached_graph_entry(cache, cache_key, memory_hash, graph)
    return graph


def get_graph_for_user(
    agent: "NymeriaAgent",
    user_id: str,
    thread_id: str = "",
):
    """Get the appropriate sync graph for a user+thread."""
    return agent._get_graph_for_user_impl(
        user_id, thread_id,
        agent._user_graphs, agent._build_graph_with_prompt,
    )


def get_async_graph_for_user(
    agent: "NymeriaAgent",
    user_id: str,
    thread_id: str = "",
):
    """Get the appropriate async graph for a user+thread."""
    return agent._get_graph_for_user_impl(
        user_id, thread_id,
        agent._async_user_graphs, agent._build_async_graph_with_prompt,
        agent._async_graph_cache_key,
    )


def async_graph_cache_key(agent: "NymeriaAgent", user_id: str, thread_id: str) -> tuple:
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = None
    return (loop_id, user_id, thread_id)
