"""Agent-facing tools for discovering and installing Agent Skills.

These tools let Nymeria reason about skills as first-class resources:
- list_installed_skills: what's currently available on disk
- search_skills: search installed skills OR pull a fresh index from a marketplace
- install_skill: download a skill from a marketplace into user/global scope

The `Skill` meta-tool itself is *not* defined here; it's built dynamically
per-graph in skills.meta_tool.create_skill_meta_tool so its description can
carry the (name, description) index for the current thread's active skills.
"""

from __future__ import annotations

import logging
import json
from datetime import datetime, timedelta
from typing import Annotated, Any, List, Literal, Optional, Union, cast

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, InjectedToolCallId, tool
from langgraph.types import Command

from ..core.thread_config import ThreadConfig
from ..core.time_utils import ensure_aware_utc, utc_now
from ..core.tool_reload import should_emit_reload_command, tool_reload_command
from .utils import get_user_id
from .utils import get_thread_id

logger = logging.getLogger(__name__)

PROTECTED_SKILL_NAMES = frozenset({"self-improve"})


def _agent():
    """Lazy import to avoid a tool->agent circular import at module load."""
    from ..core.agent import get_current_agent
    return get_current_agent()


def _command_or_text(
    text: str,
    queued_reload: bool,
    tool_call_id: Optional[str],
    new_tool_names: Optional[list[str]] = None,
    thread_id: str = "",
) -> Union[str, Command]:
    """Emit Command(goto=END) only when the rebuild is actually required.

    In dynamic-binding mode, ``should_emit_reload_command`` skips the
    Command because the next agent step resolves tools and skill metadata from
    the live resolver. Skill-only changes pass an empty list.
    """
    if queued_reload and tool_call_id and should_emit_reload_command(
        new_tool_names or [],
        thread_id=thread_id,
    ):
        return tool_reload_command(text, tool_call_id)
    return text


def _json_result(**payload) -> str:
    return json.dumps(payload, indent=2, default=str)


def _global_enabled_skills(agent: Any, user_id: str) -> set[str]:
    try:
        profile = agent.profile_manager.get_profile(user_id or "default")
        return set(profile.enabled_global_skills or [])
    except Exception:
        return set()


def _parse_usage_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return ensure_aware_utc(datetime.fromisoformat(str(value)))
    except Exception:
        return None


def _skill_status(*, user_id: str, thread_id: str) -> str:
    agent = _agent()
    if agent is None or not hasattr(agent, "skill_manager") or agent.skill_manager is None:
        return _json_result(ok=False, error="skills subsystem not initialized")
    if not thread_id:
        return _json_result(ok=False, error="thread_id is required")

    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        tc = ThreadConfig(thread_id=thread_id)
    global_enabled = sorted(_global_enabled_skills(agent, user_id))
    active = agent.skill_manager.list_for_thread(
        user_id=user_id,
        enabled_global_skills=global_enabled,
        thread_enabled_skills=tc.enabled_skills,
        thread_disabled_skills=tc.disabled_skills,
    )
    return _json_result(
        ok=True,
        action="status",
        thread_id=thread_id,
        global_enabled_skills=global_enabled,
        thread_enabled_skills=sorted(tc.enabled_skills),
        thread_disabled_skills=sorted(tc.disabled_skills),
        active_skills=[
            {
                "name": skill.name,
                "description": skill.description,
                "scope": skill.scope,
                "required_tools": skill.required_tools,
                "tool_ttl": skill.tool_ttl,
                "is_skill_kit": skill.is_skill_kit,
            }
            for skill in active
        ],
    )


def _prune_thread_skills(
    *,
    user_id: str,
    thread_id: str,
    stale_after_days: int = 30,
    min_enabled_age_days: int = 7,
    dry_run: bool = False,
) -> str:
    from ..core.capability_usage import get_capability_usage_store

    agent = _agent()
    if agent is None or not hasattr(agent, "skill_manager") or agent.skill_manager is None:
        return _json_result(ok=False, error="skills subsystem not initialized")
    if not thread_id:
        return _json_result(ok=False, error="thread_id is required")

    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        return _json_result(
            ok=True,
            action="prune",
            dry_run=dry_run,
            changed=False,
            message="thread has no skill config",
        )

    stale_days = max(1, int(stale_after_days or 30))
    min_age_days = max(0, int(min_enabled_age_days or 0))
    now = utc_now()
    stale_cutoff = now - timedelta(days=stale_days)
    config_old_enough = ensure_aware_utc(tc.updated_at) <= now - timedelta(days=min_age_days)

    visible = {
        skill.name: skill
        for skill in agent.skill_manager.list_installed(user_id=user_id)
    }
    global_enabled = _global_enabled_skills(agent, user_id)
    enabled = list(tc.enabled_skills or [])
    disabled = list(tc.disabled_skills or [])
    usage_store = get_capability_usage_store()

    enabled_missing = [
        name
        for name in enabled
        if name not in PROTECTED_SKILL_NAMES and name not in visible
    ]
    disabled_missing = [
        name
        for name in disabled
        if name not in PROTECTED_SKILL_NAMES and name not in visible
    ]
    enabled_redundant_global = [
        name
        for name in enabled
        if (
            name not in PROTECTED_SKILL_NAMES
            and name in global_enabled
            and name not in disabled
            and name not in enabled_missing
        )
    ]
    disabled_redundant_noop = [
        name
        for name in disabled
        if (
            name not in PROTECTED_SKILL_NAMES
            and name not in global_enabled
            and name not in enabled
            and name not in disabled_missing
        )
    ]

    enabled_stale: list[str] = []
    if config_old_enough:
        for name in enabled:
            if (
                name in PROTECTED_SKILL_NAMES
                or name in enabled_missing
                or name in enabled_redundant_global
                or name in disabled
            ):
                continue
            usage = usage_store.get_skill(user_id=user_id, thread_id=thread_id, name=name)
            last_used = _parse_usage_timestamp(usage.last_used_at)
            if last_used is not None and last_used <= stale_cutoff:
                enabled_stale.append(name)

    remove_enabled = set(enabled_missing) | set(enabled_redundant_global) | set(enabled_stale)
    remove_disabled = set(disabled_missing) | set(disabled_redundant_noop)
    changed = bool(remove_enabled or remove_disabled)
    if changed and not dry_run:
        tc.enabled_skills = [name for name in enabled if name not in remove_enabled]
        tc.disabled_skills = [name for name in disabled if name not in remove_disabled]
        if not agent.thread_config_manager.save_config(tc):
            return _json_result(ok=False, error="failed to save thread skill config")
        if hasattr(agent, "invalidate_thread_config_cache"):
            agent.invalidate_thread_config_cache(thread_id)
        try:
            agent._rebuild_default_graphs()
        except Exception:
            logger.debug("Failed to rebuild graphs after skill prune", exc_info=True)

    return _json_result(
        ok=True,
        action="prune",
        dry_run=dry_run,
        changed=changed,
        thread_id=thread_id,
        stale_after_days=stale_days,
        min_enabled_age_days=min_age_days,
        removed={
            "enabled_missing": sorted(enabled_missing),
            "enabled_redundant_global": sorted(enabled_redundant_global),
            "enabled_stale_used_before_cutoff": sorted(enabled_stale),
            "disabled_missing": sorted(disabled_missing),
            "disabled_redundant_noop": sorted(disabled_redundant_noop),
        },
        protected=sorted(
            name
            for name in set(enabled) | set(disabled)
            if name in PROTECTED_SKILL_NAMES
        ),
        notes=[
            "Stale pruning only removes thread-enabled skills with recorded usage older than the cutoff.",
            "Skill Kit required tool TTL bindings are not removed here; use tool_manage(action='prune') for tool bindings.",
        ],
    )


@tool
def list_installed_skills(
    scope: Literal["all", "user", "global", "bundled"] = "all",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """List all Agent Skills currently installed and visible to the user.

    A skill is a bundle of procedural knowledge (SKILL.md + optional scripts/
    references/assets). Installed skills are available to be enabled on
    threads; enabled skills show up in the `Skill` meta-tool's index.

    Args:
        scope: Filter by install location. "all" (default) returns every
            skill; "user" is skills installed privately for the current user;
            "global" is admin-installed skills visible to everyone;
            "bundled" is repo-shipped skills.

    Returns:
        JSON string with a list of {name, description, scope, allowed_tools,
        required_tools, tool_ttl, is_skill_kit, default_active, has_scripts,
        has_references} entries.
    """
    agent = _agent()
    if agent is None or not hasattr(agent, "skill_manager") or agent.skill_manager is None:
        return json.dumps({"error": "skills subsystem not initialized"})

    user_id = get_user_id(config)
    skills = agent.skill_manager.list_installed(user_id=user_id)
    if scope != "all":
        skills = [s for s in skills if s.scope == scope]

    payload = [
        {
            "name": s.name,
            "description": s.description,
            "scope": s.scope,
            "allowed_tools": s.allowed_tools,
            "required_tools": s.required_tools,
            "tool_ttl": s.tool_ttl,
            "is_skill_kit": s.is_skill_kit,
            "default_active": False,
            "has_scripts": s.has_scripts,
            "has_references": s.has_references,
        }
        for s in skills
    ]
    return json.dumps({"count": len(payload), "skills": payload}, indent=2)


_MARKETPLACE_INDEX_TTL_SECONDS = 15 * 60
_marketplace_indexed_at: dict[str, float] = {}


def _ensure_marketplace_indexed(agent, source: str) -> tuple[str, str | None]:
    """Refresh the marketplace namespace in the embedding index if stale.

    Returns (namespace, warning_or_none). On fetcher errors the warning is
    surfaced but we still return the namespace so any previously-indexed
    data can be searched.
    """
    import time as _time
    namespace = f"marketplace:{source}"
    index = getattr(agent.skill_manager, "embedding_index", None) if agent and agent.skill_manager else None
    if index is None:
        return namespace, "skills embedding index unavailable"

    now = _time.time()
    last = _marketplace_indexed_at.get(source, 0.0)
    if now - last < _MARKETPLACE_INDEX_TTL_SECONDS:
        return namespace, None

    from ..skills.marketplace import get_fetcher, MarketplaceError

    try:
        fetcher = get_fetcher(source)
        entries = fetcher.list(query=None)
    except NotImplementedError as e:
        return namespace, f"marketplace {source!r} not available: {e}"
    except MarketplaceError as e:
        return namespace, f"marketplace fetch failed: {e}"
    except Exception as e:
        logger.exception("marketplace list failed for %s", source)
        return namespace, f"marketplace fetch error: {type(e).__name__}: {e}"

    try:
        index.rebuild(namespace=namespace, items=entries)
        _marketplace_indexed_at[source] = now
    except Exception as e:
        logger.exception("marketplace index rebuild failed for %s", source)
        return namespace, f"index rebuild failed: {e}"
    return namespace, None


@tool
def search_skills(
    query: str,
    source: Literal["installed", "anthropic"] = "installed",
    top_k: int = 8,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Search for Agent Skills by natural-language query.

    Uses semantic similarity (OpenAI embeddings) when available, falls back
    to keyword BM25 search, then substring matching. This means you can
    search by intent like "extract text from images" and find skills named
    `ocr-tool` even though your query doesn't contain "ocr".

    Args:
        query: Free-text search term. Names, descriptions, and intent all match.
        source: Where to search. "installed" (default) = the local disk pool;
            "anthropic" = github.com/anthropics/skills (for discovering skills
            you don't yet have installed).
        top_k: Max number of results to return (default 8).

    Returns:
        JSON string with ``{count, mode, results, warning?}``:
            - ``mode`` is "semantic" (best), "bm25" (keyword fallback), or
              "substring" (final safety net)
            - ``warning`` is only present when search is running in degraded
              mode. Surface its message to the user so they can set up a
              better configuration (typically set EMBEDDING_API_KEY).
            - each result is ``{name, description, score, ...}``
    """
    agent = _agent()
    if agent is None or not hasattr(agent, "skill_manager") or agent.skill_manager is None:
        return json.dumps({"error": "skills subsystem not initialized"})

    index = getattr(agent.skill_manager, "embedding_index", None)
    q = query or ""

    if source == "installed":
        if index is None:
            # Ultimate substring fallback — no index at all.
            user_id = get_user_id(config)
            q_lower = q.strip().lower()
            matches = []
            for s in agent.skill_manager.list_installed(user_id=user_id):
                if not q_lower or q_lower in s.name.lower() or q_lower in s.description.lower():
                    matches.append({
                        "name": s.name,
                        "description": s.description,
                        "scope": s.scope,
                        "score": 1.0,
                    })
            return json.dumps({
                "count": len(matches),
                "mode": "substring",
                "warning": "embedding index unavailable; using substring match",
                "results": matches[:top_k],
            }, indent=2)

        response = index.search(q, namespace="installed", top_k=top_k)
        out = response.to_json()
        # Annotate that results are drawn from the installed pool.
        for r in out["results"]:
            r.setdefault("source", "installed")
        return json.dumps(out, indent=2)

    if source == "anthropic":
        namespace, mp_warning = _ensure_marketplace_indexed(agent, "anthropic")
        if index is None:
            # Index unavailable — fall through to direct substring match against
            # the marketplace list.
            from ..skills.marketplace import get_fetcher, MarketplaceError
            try:
                fetcher = get_fetcher("anthropic")
                entries = fetcher.list(query=q or None)
            except (MarketplaceError, NotImplementedError) as e:
                return json.dumps({"error": str(e)})
            matches = [
                {"name": e.name, "description": e.description, "source": e.source, "score": 1.0}
                for e in entries
            ][:top_k]
            return json.dumps({
                "count": len(matches),
                "mode": "substring",
                "warning": "embedding index unavailable; using keyword match on marketplace list",
                "results": matches,
            }, indent=2)

        response = index.search(q, namespace=namespace, top_k=top_k)
        out = response.to_json()
        if mp_warning:
            # Preserve any earlier warning (e.g. from degraded semantic) by joining.
            existing = out.get("warning")
            out["warning"] = f"{existing}; {mp_warning}" if existing else mp_warning
        for r in out["results"]:
            r.setdefault("source", "anthropic")
        return json.dumps(out, indent=2)

    return json.dumps({"error": f"unknown source: {source!r}"})


@tool
def install_skill(
    name: str,
    source: Literal["anthropic"] = "anthropic",
    scope: Literal["user", "global"] = "user",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """Install an Agent Skill from a marketplace onto disk.

    Installed skills are not automatically enabled on any thread. Use the
    desktop Settings → Skills tab, or the skill-creator skill's enable
    workflow, to turn them on.

    Args:
        name: The skill's kebab-case name (as returned by search_skills).
        source: Which marketplace to pull from. Only "anthropic" (the
            official anthropics/skills GitHub repo) is supported in Phase 1.
        scope: Where to store it on disk. "user" (default) installs privately
            for the current user; "global" makes it visible to all users.

    Returns:
        Human-readable summary of the install. On failure, returns an error
        message starting with "[error]".
    """
    agent = _agent()
    if agent is None or not hasattr(agent, "skill_manager") or agent.skill_manager is None:
        return "[error] skills subsystem not initialized"

    user_id = get_user_id(config)

    # ``scope=user`` is per-user (every caller can install for themselves);
    # ``scope=global`` writes into the shared skills directory visible to all
    # users. Skill bundles can ship scripts the agent process can execute, so
    # global install is admin-only — same gate as POST /skills/install.
    if scope == "global":
        try:
            caller = agent.accounts_repo.get_user_by_id(user_id)
            if not caller or caller.role != "admin":
                return (
                    "[error] install_skill scope=global requires admin role. "
                    "Install with scope='user' (default) or ask the workspace "
                    "admin to install it globally."
                )
        except Exception as e:
            logger.warning("install_skill: admin check failed: %s", e)
            return f"[error] install_skill: caller verification failed: {e}"

    from ..skills.marketplace import get_fetcher, MarketplaceError
    try:
        fetcher = get_fetcher(source)
    except MarketplaceError as e:
        return f"[error] {e}"
    except NotImplementedError as e:
        return f"[error] {e}"

    target_dir = agent.skill_manager.target_dir(
        scope, user_id=user_id if scope == "user" else None
    )
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        skill = fetcher.fetch(name, target_dir)
    except MarketplaceError as e:
        return f"[error] {e}"
    except Exception as e:
        logger.exception("install_skill failed")
        return f"[error] {type(e).__name__}: {e}"

    agent.skill_manager.reload()
    try:
        agent._rebuild_default_graphs()
    except Exception:
        logger.debug("Failed to rebuild graphs after skill install", exc_info=True)

    details: List[str] = [
        f"Installed skill {skill.name!r} from {source} into {scope} scope.",
        f"Location: {skill.path}",
        f"Description: {skill.description}",
    ]
    if skill.allowed_tools:
        details.append("Declared allowed-tools: " + ", ".join(skill.allowed_tools))
    if skill.has_scripts:
        details.append(f"Ships {len(skill.list_scripts())} scripts.")
    if skill.has_references:
        details.append(f"Ships {len(skill.list_references())} reference files.")
    details.append(
        "To make this skill available on a thread, enable it via the Skills "
        "settings (or add its name to ThreadConfig.enabled_skills)."
    )
    return "\n".join(details)


def _set_thread_skill_enabled(
    *,
    skill_name: str,
    enabled: bool,
    user_id: str,
    thread_id: str,
    tool_call_id: Optional[str],
) -> Union[str, Command]:
    agent = _agent()
    if agent is None or not hasattr(agent, "skill_manager") or agent.skill_manager is None:
        return _json_result(ok=False, error="skills subsystem not initialized")
    if not thread_id:
        return _json_result(ok=False, error="thread_id is required")

    target = (skill_name or "").strip()
    if not target:
        return _json_result(ok=False, error="name is required")
    skill = agent.skill_manager.get(target, user_id=user_id)
    if skill is None:
        return _json_result(ok=False, error=f"skill not installed or not visible: {target}")

    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        tc = ThreadConfig(thread_id=thread_id)

    changed = False
    if enabled:
        if target not in tc.enabled_skills:
            tc.enabled_skills = [*tc.enabled_skills, target]
            changed = True
        if target in tc.disabled_skills:
            tc.disabled_skills = [name for name in tc.disabled_skills if name != target]
            changed = True
    else:
        if target not in tc.disabled_skills:
            tc.disabled_skills = [*tc.disabled_skills, target]
            changed = True
        if target in tc.enabled_skills:
            tc.enabled_skills = [name for name in tc.enabled_skills if name != target]
            changed = True

    if changed:
        if not agent.thread_config_manager.save_config(tc):
            return _json_result(ok=False, error="failed to save thread skill config")
        if hasattr(agent, "invalidate_thread_config_cache"):
            agent.invalidate_thread_config_cache(thread_id)

    queued_reload = False
    cap_hit = False
    if enabled and changed:
        from .skill_config import _queue_skill_reload

        queued_reload, cap_hit = _queue_skill_reload(
            agent,
            thread_id,
            target,
            source="skill_install",
            reason="skill_enabled_on_thread",
        )
    elif changed:
        try:
            agent._rebuild_default_graphs()
        except Exception:
            logger.debug("Failed to rebuild graphs after skill disable", exc_info=True)

    payload = _json_result(
        ok=True,
        action="enable" if enabled else "disable",
        changed=changed,
        skill={
            "name": skill.name,
            "description": skill.description,
            "scope": skill.scope,
            "required_tools": skill.required_tools,
            "tool_ttl": skill.tool_ttl,
        },
        active_on_current_thread=enabled,
        reload_queued=queued_reload,
        reload_cap_hit=cap_hit,
    )
    if queued_reload:
        payload += (
            "\n\n[Skill reload queued - STOP NOW]\n"
            "The skill list changed for this thread, but the current graph "
            "invocation cannot see the updated Skill meta-tool index. Do not "
            "write a final answer or call another tool now. The system will "
            "automatically resume you after rebuilding."
        )
    elif cap_hit:
        payload += (
            "\n\n[Reload cap hit]: the skill was enabled on this thread, but "
            "it will not be visible to the model until the next user message."
        )
    return _command_or_text(payload, queued_reload, tool_call_id, thread_id=thread_id)


@tool
def skill_manage(
    action: str,
    query: str = "",
    name: str = "",
    source: Literal["installed", "anthropic"] = "installed",
    scope: Literal["all", "user", "global", "bundled"] = "user",
    activate_current_thread: bool = False,
    stale_after_days: int = 30,
    min_enabled_age_days: int = 7,
    dry_run: bool = False,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> Union[str, Command]:
    """List, search, install, enable, disable, inspect, status, or prune Agent Skills.

    Args:
        action: One of: list, search, install, enable, disable, inspect,
            status, prune.
        query: Search query.
        name: Skill name for install/enable/disable/inspect.
        source: "installed" or marketplace source "anthropic".
        scope: Install/list scope. Global install requires admin.
        activate_current_thread: After install, enable this skill on the thread.
        stale_after_days: For prune, remove recorded-stale thread-enabled
            skills whose last use is older than this many days.
        min_enabled_age_days: For prune, require the thread config to be at
            least this old before stale-age pruning.
        dry_run: For prune, report changes without saving them.

    Returns:
        list/inspect: JSON {ok, skill: {name, description, scope, ...}}
        or {count, skills: [...]}.
        search: JSON {count, mode, results: [{name, description, score}]}.
        install: human-readable summary or "[error] ...".
        enable: JSON {ok, action, changed, skill, reload_queued, ...}.
        When reload_queued=true, includes "[Skill reload queued - STOP
        NOW]" and the graph is force-ended for rebuild — do not respond
        after this directive. disable: JSON {ok, action, changed, ...}.
        Errors: JSON {ok: false, error: "..."}.
    """
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)
    action_key = (action or "").strip().lower()

    if action_key == "status":
        return _skill_status(user_id=user_id, thread_id=thread_id)

    if action_key == "prune":
        return _prune_thread_skills(
            user_id=user_id,
            thread_id=thread_id,
            stale_after_days=stale_after_days,
            min_enabled_age_days=min_enabled_age_days,
            dry_run=dry_run,
        )

    if action_key in {"list", "inspect"}:
        if name:
            agent = _agent()
            skill_manager = getattr(agent, "skill_manager", None) if agent is not None else None
            skill = (
                skill_manager.get(name, user_id=user_id)
                if skill_manager is not None
                else None
            )
            if skill is None:
                return _json_result(ok=False, error=f"skill not installed or not visible: {name}")
            return _json_result(
                ok=True,
                skill={
                    "name": skill.name,
                    "description": skill.description,
                    "scope": skill.scope,
                    "required_tools": skill.required_tools,
                    "tool_ttl": skill.tool_ttl,
                    "is_skill_kit": skill.is_skill_kit,
                    "has_scripts": skill.has_scripts,
                    "has_references": skill.has_references,
                },
            )
        list_scope = scope if scope in ("all", "user", "global", "bundled") else "all"
        return cast(Any, list_installed_skills).func(scope=list_scope, config=config)

    if action_key == "search":
        search_source = source if source in ("installed", "anthropic") else "installed"
        return cast(Any, search_skills).func(
            query=query or name,
            source=search_source,
            top_k=8,
            config=config,
        )

    if action_key == "install":
        install_source = source if source != "installed" else "anthropic"
        install_scope = "global" if scope == "global" else "user"
        result = cast(Any, install_skill).func(
            name=name or query,
            source=install_source,
            scope=install_scope,
            config=config,
        )
        if str(result).startswith("[error]") or not activate_current_thread:
            return result
        enabled_result = _set_thread_skill_enabled(
            skill_name=name or query,
            enabled=True,
            user_id=user_id,
            thread_id=thread_id,
            tool_call_id=tool_call_id,
        )
        if isinstance(enabled_result, Command):
            try:
                update = enabled_result.update or {}
                messages = update.get("messages", [])
                content = str(messages[0].content) if messages else ""
            except Exception:
                content = ""
            return tool_reload_command(
                f"{result}\n\nThread activation result:\n{content}".strip(),
                tool_call_id,
            )
        return f"{result}\n\nThread activation result:\n{enabled_result}".strip()

    if action_key == "enable":
        return _set_thread_skill_enabled(
            skill_name=name or query,
            enabled=True,
            user_id=user_id,
            thread_id=thread_id,
            tool_call_id=tool_call_id,
        )

    if action_key == "disable":
        return _set_thread_skill_enabled(
            skill_name=name or query,
            enabled=False,
            user_id=user_id,
            thread_id=thread_id,
            tool_call_id=tool_call_id,
        )

    return _json_result(
        ok=False,
        error="action must be one of: list, search, install, enable, disable, inspect, status, prune",
    )


SEARCH_SKILLS_TOOLS = [skill_manage, list_installed_skills, search_skills, install_skill]

__all__ = [
    "skill_manage",
    "list_installed_skills",
    "search_skills",
    "install_skill",
    "SEARCH_SKILLS_TOOLS",
]
