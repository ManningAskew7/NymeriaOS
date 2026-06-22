"""Tool search, discovery, and per-thread binding for Nymeria.

``tool_search`` is intentionally search-only. ``tool_manage`` owns the
thread-binding mutations that used to live behind ``tool_search(action=...)``.

In dynamic-binding mode, enabling a tool with a TTL returns normal tool text;
the next model step resolves the updated tool set without a graph rebuild. In
legacy rebuild mode, the turn finishes the current graph invocation, rebuilds
a fresh graph, and resumes via an internal `tool_reload_resume` message. See
`core/agent.py::_do_tool_reload` for the legacy orchestration and
`docs/tools.md` for the full flow.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any, Dict, List, Optional, Tuple, Union

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, InjectedToolCallId, tool
from langgraph.types import Command

from ..core.time_utils import (
    ensure_aware_utc,
    parse_tool_ttl,
    parse_usage_timestamp,
    utc_now,
)
from ..core.tool_reload import should_emit_reload_command, tool_reload_command
from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)


DEFAULT_TTL = "2h"
PROTECTED_MANAGEMENT_TOOL_NAMES = frozenset(
    {
        "tool_search",
        "tool_manage",
        "skill_manage",
        "skill_write",
        "skill_edit",
        "manage_mcp",
        "api_discover",
        "http_request",
        "tool_create",
    }
)


@dataclass
class ToolBindingResult:
    ok: bool
    text: str
    reload_tools: List[str] = field(default_factory=list)
    ttl_key: str = DEFAULT_TTL
    ttl_seconds: Optional[int] = None
    cap_hit: bool = False
    source: str = "tool_search"
    skill_name: Optional[str] = None
    reason: Optional[str] = None


def _format_remaining(expires_at: datetime) -> str:
    delta = ensure_aware_utc(expires_at) - utc_now()
    total = int(delta.total_seconds())
    if total <= 0:
        return "expired"
    h, rem = divmod(total, 3600)
    m, _ = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m left"
    return f"{m}m left"


def _short_desc(desc: str, max_len: int = 60) -> str:
    """Truncate a description to ~max_len chars, breaking on the last word
    boundary and appending an ellipsis. Avoids cutting mid-word or leaking
    embedded newlines into the status view."""
    if not desc:
        return ""
    # Collapse whitespace/newlines so truncated descriptions stay on one line.
    cleaned = " ".join(desc.split())
    if len(cleaned) <= max_len:
        return cleaned
    cut = cleaned[: max_len - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


def _build_catalog() -> Dict[str, dict]:
    """Build a searchable catalog from ALL tools (core + optional + dynamic)."""
    from . import static_tool_catalog
    from .metadata import (
        CUSTOM_TOOL_METADATA,
        MCP_SERVER_TOOL_METADATA,
        get_all_tool_metadata,
    )

    catalog = {}

    all_tools = static_tool_catalog()

    for name, tool_obj in all_tools.items():
        meta = get_all_tool_metadata(name)
        desc = tool_obj.description or ""
        if meta and meta.description:
            desc = meta.description
        catalog[name] = {
            "name": name,
            "description": desc,
            "category": meta.category.value if meta else "unknown",
            "security_level": meta.security_level.value if meta else "moderate",
        }

    for registry in (MCP_SERVER_TOOL_METADATA, CUSTOM_TOOL_METADATA):
        for name, meta in registry.items():
            if name not in catalog:
                catalog[name] = {
                    "name": name,
                    "description": meta.description,
                    "category": meta.category.value,
                    "security_level": meta.security_level.value,
                }

    return catalog


def _get_user_role(user_id: str) -> str:
    """Best-effort role lookup for discovery and enable gates."""
    from ..core.agent import get_current_agent

    try:
        agent = get_current_agent()
        user = agent.accounts_repo.get_user_by_id(user_id) if agent and user_id else None
        return user.role if user else "user"
    except Exception:
        return "user"


def _build_discovery_catalog(user_role: str) -> Dict[str, dict]:
    """Build the catalog shown to a caller in search/category views."""
    from . import filter_discoverable_catalog_tool_names

    catalog = _build_catalog()
    visible = filter_discoverable_catalog_tool_names(catalog.keys(), user_role)
    return {name: entry for name, entry in catalog.items() if name in visible}


def _resolve_tool_object(name: str, agent) -> Optional[Any]:
    """Return the actual tool object that would be bound at graph-build time.

    Mirrors the resolution logic in agent._build_graph_with_prompt so that
    enable-time validation matches what the rebuild actually does. Returns
    None if the name is in the searchable catalog (metadata only) but has
    no live tool object (typically MCP server tools whose backing server
    is installed but disabled).
    """
    from . import static_tool_catalog

    all_tools_dict = static_tool_catalog()
    if name in all_tools_dict:
        return all_tools_dict[name]
    if agent is not None and getattr(agent, "tool_registry", None):
        return agent.tool_registry.get_tool(name)
    return None


def _format_unloadable_error(unloadable: List[str]) -> str:
    """Build a helpful error for tools that exist in the catalog but can't be bound."""
    mcp_servers = set()
    other = []
    for name in unloadable:
        if name.startswith("mcp__"):
            parts = name.split("__", 2)
            if len(parts) >= 2:
                mcp_servers.add(parts[1])
            else:
                other.append(name)
        else:
            other.append(name)
    lines = [
        f"[Error]: Cannot enable {len(unloadable)} tool(s); "
        "they appear in search results but their backing source is not active:"
    ]
    if mcp_servers:
        lines.append(
            f"  MCP server(s) installed but disabled: {', '.join(sorted(mcp_servers))}"
        )
        lines.append(
            "  Activate them via manage_mcp(action=\"install\", ...) or the "
            "Settings → MCP UI before retrying enable."
        )
    if other:
        lines.append(f"  Other unresolved: {', '.join(other)}")
    lines.append(f"  Names: {', '.join(unloadable)}")
    return "\n".join(lines)


def _get_thread_status(thread_id: str) -> Tuple[set, dict, set]:
    """Return (enabled_permanent, temporary_tools_map, disabled) for a thread."""
    from ..core.agent import get_current_agent

    agent = get_current_agent()
    if agent is None:
        return set(), {}, set()

    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        return set(), {}, set()
    return set(tc.enabled_tools), dict(tc.temporary_tools), set(tc.disabled_tools)


def _search(
    query: str,
    category: str,
    thread_id: str,
    user_role: str = "user",
    *,
    top_k: int = 15,
    include_status: bool = True,
) -> str:
    if not query and not category:
        return "[Error]: Provide a query, a category, or both."

    from ..core.agent import get_current_agent
    from ..core.tool_search_index import search_tools

    agent = get_current_agent()
    user_id = "default"
    try:
        if agent and thread_id:
            user_id = agent.accounts_repo.get_thread_owner(thread_id) or "default"
    except Exception:
        user_id = "default"

    response = search_tools(
        query,
        category=category,
        user_id=user_id,
        user_role=user_role,
        agent=agent,
        thread_id=thread_id,
        top_k=top_k,
        include_status=include_status,
    )

    if not response.results:
        if category:
            cat_key = category.lower().strip().replace("-", "_")
            try:
                from .metadata import get_all_categories

                known_categories = set(get_all_categories()) | {"callable"}
                if cat_key not in known_categories:
                    cats = ", ".join(sorted(known_categories))
                    return f"[No results]: Unknown category '{category}'. Available: {cats}"
            except Exception:
                logger.debug("Failed to inspect tool categories", exc_info=True)
        return f"[No results]: No tools matched '{query}'." + (
            f" (category filter: {category})" if category else ""
        )

    lines = [f"[Tool Search]: {len(response.results)} result(s)" + (
        f" in category '{category}'" if category else ""
    ) + f" ({response.mode})"]
    if response.warning:
        lines.append(f"[Warning]: {response.warning}")
    for c in response.results:
        status = ""
        # disabled_tools is authoritative — check it first so a disabled
        # tool with a preserved enabled/TTL entry doesn't get labeled ENABLED.
        if include_status and c.status:
            if c.status == "disabled":
                status = " [DISABLED]"
            elif c.status == "enabled_permanent":
                status = " [ENABLED permanent]"
            elif c.status.startswith("enabled_ttl:"):
                status = f" [ENABLED {c.status.split(':', 1)[1]}]"
        lines.append(
            f"  {c.name} ({c.category}, {c.security_level}){status}"
            f"\n    {c.description}"
            f"\n    Enable hint: {c.enable_hint}"
        )

    return "\n".join(lines)


def bind_tools_for_thread(
    tool_names: List[str],
    category: str,
    thread_id: str,
    user_id: str,
    ttl: str = DEFAULT_TTL,
    *,
    strict: bool = False,
    source: str = "tool_search",
    skill_name: Optional[str] = None,
    reason: Optional[str] = None,
) -> ToolBindingResult:
    """Validate and bind tools for a thread.

    ``strict=True`` is used by Skill Kits: any invalid, unloadable, or
    admin-blocked dependency aborts before the thread config is mutated.
    ``strict=False`` preserves tool_search's partial-success behavior for
    invalid names mixed into an otherwise valid enable request.
    """
    from ..core.agent import get_current_agent
    from ..core.thread_config import ThreadConfig, TemporaryToolEntry
    from . import SEED_TOOLS
    from .metadata import ToolCategory, get_all_tool_metadata, SecurityLevel

    agent = get_current_agent()
    if agent is None:
        return ToolBindingResult(
            ok=False,
            text="[Error]: No active agent. Cannot modify thread config.",
            source=source,
            skill_name=skill_name,
            reason=reason,
        )

    try:
        user = agent.accounts_repo.get_user_by_id(user_id) if user_id else None
        user_role = user.role if user else "user"
    except Exception:
        user_role = "user"

    ttl_value = DEFAULT_TTL if ttl is None else ttl
    try:
        ttl_key, ttl_seconds = parse_tool_ttl(ttl_value)
    except ValueError as exc:
        return ToolBindingResult(
            ok=False,
            text=f"[Error]: {exc}",
            source=source,
            skill_name=skill_name,
            reason=reason,
        )

    if category and not tool_names:
        cat_key = category.lower().strip().replace("-", "_")
        try:
            ToolCategory(cat_key)
        except ValueError:
            from .metadata import get_all_categories
            cats = ", ".join(get_all_categories())
            return ToolBindingResult(
                ok=False,
                text=f"[Error]: Unknown category '{category}'. Available: {cats}",
                ttl_key=ttl_key,
                ttl_seconds=ttl_seconds,
                source=source,
                skill_name=skill_name,
                reason=reason,
            )

        catalog = _build_discovery_catalog(user_role)
        tool_names = [c["name"] for c in catalog.values() if c["category"] == cat_key]
        if not tool_names:
            return ToolBindingResult(
                ok=False,
                text=f"[Error]: No tools in category '{category}'.",
                ttl_key=ttl_key,
                ttl_seconds=ttl_seconds,
                source=source,
                skill_name=skill_name,
                reason=reason,
            )

    if not tool_names:
        return ToolBindingResult(
            ok=False,
            text="[Error]: Provide tool names via 'tools' or a category via 'category'.",
            ttl_key=ttl_key,
            ttl_seconds=ttl_seconds,
            source=source,
            skill_name=skill_name,
            reason=reason,
        )

    catalog = _build_catalog()
    all_known = set(catalog.keys())
    valid: List[str] = []
    invalid: List[str] = []
    unloadable: List[str] = []
    warnings: List[str] = []

    for name in tool_names:
        # Step 1 — must appear in the searchable catalog or in the live registry.
        in_catalog = name in all_known
        in_registry = bool(
            agent.tool_registry and agent.tool_registry.get_tool(name)
        )
        if not (in_catalog or in_registry):
            invalid.append(name)
            continue
        # Step 2 — must actually resolve to a live tool object. Catches the
        # MCP-tool-with-disabled-server case where metadata is registered but
        # no tool object is bound (graph-build would silently drop it).
        if _resolve_tool_object(name, agent) is None:
            unloadable.append(name)
            continue
        valid.append(name)
        meta = get_all_tool_metadata(name)
        if meta and meta.security_level == SecurityLevel.SENSITIVE:
            warnings.append(f"'{name}' is SENSITIVE")

    if strict and (invalid or unloadable):
        lines = ["[Error]: Tool dependency validation failed; no tools were bound."]
        if invalid:
            lines.append(f"[Not found]: {', '.join(invalid)}")
        if unloadable:
            lines.append(_format_unloadable_error(unloadable))
        return ToolBindingResult(
            ok=False,
            text="\n".join(lines),
            ttl_key=ttl_key,
            ttl_seconds=ttl_seconds,
            source=source,
            skill_name=skill_name,
            reason=reason,
        )

    if not valid:
        # Surface unloadable details first (more actionable for the agent).
        if unloadable:
            err = _format_unloadable_error(unloadable)
            if invalid:
                err += f"\n[Not found]: {', '.join(invalid)}"
            return ToolBindingResult(
                ok=False,
                text=err,
                ttl_key=ttl_key,
                ttl_seconds=ttl_seconds,
                source=source,
                skill_name=skill_name,
                reason=reason,
            )
        return ToolBindingResult(
            ok=False,
            text=f"[Error]: No valid tools to enable. Unknown: {', '.join(invalid)}",
            ttl_key=ttl_key,
            ttl_seconds=ttl_seconds,
            source=source,
            skill_name=skill_name,
            reason=reason,
        )

    # Admin-only gate. Mirror the REST gate at PATCH /threads/{id}/config —
    # without this, an agent could call tool_manage(action="enable",
    # tools=["reload_all"]) to escalate to admin-only tools that are
    # equivalent to authenticated RCE on the shared backend.
    from . import filter_admin_only_tools, filter_developer_only_tools
    allowed, blocked = filter_admin_only_tools(valid, user_role)
    if blocked:
        return ToolBindingResult(
            ok=False,
            text=(
                f"[Error]: Admin-only tools cannot be enabled by this user: "
                f"{sorted(blocked)}. Ask an administrator to enable them on this "
                f"thread, or pick a non-admin alternative."
            ),
            ttl_key=ttl_key,
            ttl_seconds=ttl_seconds,
            source=source,
            skill_name=skill_name,
            reason=reason,
        )
    valid = [n for n in valid if n in allowed]

    # Developer-only diagnostics are kept in CATALOG_TOOLS for admin/test
    # validation, but regular users should neither discover nor bind them.
    allowed, blocked = filter_developer_only_tools(valid, user_role)
    if blocked:
        return ToolBindingResult(
            ok=False,
            text=(
                f"[Error]: Developer-only diagnostic tools cannot be enabled "
                f"by this user: {sorted(blocked)}."
            ),
            ttl_key=ttl_key,
            ttl_seconds=ttl_seconds,
            source=source,
            skill_name=skill_name,
            reason=reason,
        )
    valid = [n for n in valid if n in allowed]

    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        tc = ThreadConfig(thread_id=thread_id)
    elif hasattr(agent, "_resolve_temporary_tools"):
        # Expired TTL entries must not make enable look like a refresh-only
        # no-op. Evict them before classifying requested names.
        agent._resolve_temporary_tools(tc)

    # Compute the thread's *actual* default-bound tool set. A user profile
    # can override SEED_TOOLS via profile.tool_preferences.default_thread_tools
    # (a curated subset), and graph-build uses that subset — not SEED_TOOLS —
    # to decide which tools to bind by default. Classifying against SEED_TOOLS
    # silently misclassifies any tool that lives in SEED_TOOLS but is absent
    # from default_thread_tools (e.g., `rag_search`): the classifier
    # thinks it's already bound, drops it into the no-op bucket, and never
    # actually adds it anywhere — the tool then vanishes. Using the same
    # source of truth as graph-build (`agent.py::_build_graph_with_prompt`)
    # keeps classification honest.
    try:
        profile = agent.profile_manager.get_profile(user_id or "default")
        default_tools_pref = profile.tool_preferences.default_thread_tools
    except Exception:
        default_tools_pref = None
    if default_tools_pref is None:
        default_bound = {t.name for t in SEED_TOOLS}
    else:
        default_bound = set(default_tools_pref)

    newly_added: List[str] = []       # new binding written to enabled_tools/temporary_tools
    refreshed: List[str] = []         # TTL'd tool whose expires_at was pushed out
    promoted: List[str] = []          # moved from temporary_tools to enabled_tools
    already_default: List[str] = []   # already in default-bound set — no write
    already_permanent: List[str] = [] # already in tc.enabled_tools — no write
    un_disabled: List[str] = []       # removed from tc.disabled_tools

    original_enabled = list(tc.enabled_tools)
    original_disabled = list(tc.disabled_tools)
    original_temporary = dict(tc.temporary_tools)

    new_enabled = set(tc.enabled_tools)
    new_temporary = dict(tc.temporary_tools)
    new_disabled = set(tc.disabled_tools)

    for name in valid:
        was_disabled = name in new_disabled
        if was_disabled:
            new_disabled.discard(name)

        in_enabled = name in new_enabled
        in_default = name in default_bound
        in_temporary = name in new_temporary

        # -- Un-disable path --
        # Un-disabling is the primary action when the tool was filtered out
        # by tc.disabled_tools. Since disable is non-destructive (it only
        # adds to disabled_tools without touching enabled_tools/temporary_tools),
        # un-disable should restore preserved state AS-IS; the requested `ttl`
        # must NOT bleed into this path and overwrite a preserved permanent or
        # TTL entry. Mixed-batch example: enable([hello_test, memory_clear_all],
        # ttl="never") where hello_test is a TTL-to-permanent promotion and
        # memory_clear_all is a disabled-with-preserved-TTL: the "never" was
        # intended for hello_test, so memory_clear_all's 18m TTL must survive intact.
        #
        # Only write fresh state when there's genuinely nothing to restore:
        # no default binding, no permanent entry, no TTL entry. Expired TTL
        # entries left in temporary_tools get cleaned up by the graph-build
        # lazy-eviction; if that leaves the tool unbound, the user can call
        # enable again with an explicit TTL.
        if was_disabled:
            un_disabled.append(name)
            has_preserved = in_enabled or in_temporary
            if not in_default and not has_preserved:
                if ttl_seconds is None:
                    new_enabled.add(name)
                else:
                    expires_at = utc_now() + timedelta(seconds=ttl_seconds)
                    new_temporary[name] = TemporaryToolEntry(expires_at=expires_at)
            continue

        # -- Not disabled: priority-ordered classification --
        # (1) explicitly persisted permanent wins — even if the tool is ALSO
        #     in default_bound. Matches what `status` shows the user.
        if in_enabled:
            already_permanent.append(name)
            continue

        # (2) part of the thread's default-bound set — no write needed,
        #     adding to enabled_tools would be redundant clutter. (This is the
        #     same default-bound set the disable guard below protects, so
        #     enable and disable agree on what "default" means for the thread.)
        if in_default:
            already_default.append(name)
            continue

        # (3) already has a TTL — refresh the expiry, or promote to permanent.
        if in_temporary:
            if ttl_seconds is None:
                del new_temporary[name]
                new_enabled.add(name)
                promoted.append(name)
            else:
                expires_at = utc_now() + timedelta(seconds=ttl_seconds)
                new_temporary[name] = TemporaryToolEntry(
                    enabled_at=new_temporary[name].enabled_at,
                    expires_at=expires_at,
                )
                refreshed.append(name)
            continue

        # (4) genuinely new.
        if ttl_seconds is None:
            new_enabled.add(name)
        else:
            expires_at = utc_now() + timedelta(seconds=ttl_seconds)
            new_temporary[name] = TemporaryToolEntry(expires_at=expires_at)
        newly_added.append(name)

    # Bound-list anchoring for the model. In dynamic-binding mode the model
    # has no "tool list changed" sentinel and can fall into a wrong inference
    # like "the tool's in my list now, so I must have had it earlier." Giving
    # it concrete before/after counts in the result text makes the state
    # change unambiguous. Computed against the same union graph-build uses:
    # (default-bound ∪ enabled ∪ live-temporary) − disabled.
    live_temp_before = {n for n in original_temporary}
    live_temp_after = {n for n in new_temporary}
    prior_bound_names = (default_bound | set(original_enabled) | live_temp_before) - set(original_disabled)
    new_bound_names = (default_bound | new_enabled | live_temp_after) - new_disabled
    prior_count = len(prior_bound_names)
    new_count = len(new_bound_names)

    tc.enabled_tools = sorted(new_enabled)
    tc.disabled_tools = sorted(new_disabled)
    tc.temporary_tools = new_temporary

    if not agent.thread_config_manager.save_config(tc):
        tc.enabled_tools = original_enabled
        tc.disabled_tools = original_disabled
        tc.temporary_tools = original_temporary
        return ToolBindingResult(
            ok=False,
            text="[Error]: Failed to save thread config.",
            ttl_key=ttl_key,
            ttl_seconds=ttl_seconds,
            source=source,
            skill_name=skill_name,
            reason=reason,
        )

    if hasattr(agent, "invalidate_thread_config_cache"):
        agent.invalidate_thread_config_cache(thread_id)

    # An in-turn reload is needed when we added a genuinely new binding —
    # that's newly_added (optional tools new to the graph) OR un_disabled
    # (tools that were previously filtered out by tc.disabled_tools, now
    # un-filtered so they'll be bound on rebuild).
    reload_tools = sorted(set(newly_added) | set(un_disabled))

    needs_legacy_reload = bool(reload_tools) and should_emit_reload_command(
        reload_tools or [],
        thread_id=thread_id,
    )
    # The reload cap only applies to the legacy rebuild/resume path. Dynamic
    # binding returns ordinary tool text and lets the next model step resolve
    # the updated tool set without touching _pending_tool_reload.
    reload_cap = getattr(agent, "MAX_TOOL_RELOADS_PER_TURN", 1)
    current_reloads = getattr(agent, "_turn_reload_count", {}).get(thread_id, 0)
    cap_hit = needs_legacy_reload and current_reloads >= reload_cap
    will_reload = needs_legacy_reload and not cap_hit
    if will_reload:
        if not hasattr(agent, "_pending_tool_reload"):
            agent._pending_tool_reload = {}
        agent._pending_tool_reload[thread_id] = {
            "new_tools": reload_tools,
            "ttl": ttl_key,
            "ttl_seconds": ttl_seconds,
            "source": source,
            "skill_name": skill_name,
            "reason": reason,
        }

    ttl_desc = "permanent" if ttl_seconds is None else ttl_key

    total_requested = len(valid)
    change_count = len(newly_added) + len(refreshed) + len(promoted) + len(un_disabled)
    noop_count = max(0, total_requested - change_count)
    header = (
        f"[Success]: {total_requested} requested. "
        f"{len(newly_added)} newly loaded, "
        f"{len(refreshed)} TTL refreshed, "
        f"{len(promoted)} promoted, "
        f"{len(un_disabled)} un-disabled, "
        f"{noop_count} no-op "
        f"(TTL for new/refreshed: {ttl_desc})"
    )
    lines = [header]
    if newly_added:
        lines.append(f"  Newly loaded: {', '.join(sorted(newly_added))}")
    if refreshed:
        lines.append(f"  TTL refreshed: {', '.join(sorted(refreshed))}")
    if promoted:
        lines.append(f"  Promoted to permanent: {', '.join(sorted(promoted))}")
    if un_disabled:
        lines.append(f"  Un-disabled (removed from disabled list): {', '.join(sorted(un_disabled))}")
    if already_default:
        lines.append(f"  Already bound (default set, no change): {', '.join(sorted(already_default))}")
    if already_permanent:
        lines.append(f"  Already permanent (no change): {', '.join(sorted(already_permanent))}")
    if warnings:
        lines.append(f"[Warning]: {'; '.join(warnings)}")
    if invalid:
        lines.append(f"[Not found]: {', '.join(invalid)}")
    if unloadable:
        lines.append(f"[Unloadable]: {_format_unloadable_error(unloadable)}")
    lines.append("")
    if will_reload:
        lines.append(
            "[Tool reload queued - STOP NOW]\n"
            "The newly-loaded tools are NOT bound to the model in this "
            "iteration. Do not write a final answer, do not explain the "
            "enablement to the user, and do not call another tool now. This "
            "graph invocation is ending after this tool result so the system "
            "can rebuild the tool list. The system will automatically prompt "
            "you again with tool_reload_resume after the tools are bound; "
            "continue the user's task and call the new tools only after that "
            "automatic resume."
        )
    elif reload_tools and not cap_hit:
        # Dynamic-binding mode: the model node will rebind on its next step,
        # and SafeToolNode can resolve post-build tools before dispatch.
        delta = new_count - prior_count
        lines.append(
            "[Tools bound; callable on the next model step]\n"
            "These tool(s) were NOT in your bound list before this call — "
            "earlier turns of this conversation did not have access to them. "
            "They become callable immediately after this tool result without "
            "a graph rebuild or resume.\n"
            f"Your bound list: {prior_count} → {new_count} tool(s)"
            + (f" (+{delta} new binding)." if delta > 0 else ".")
            + " Trust this result over any assumption about earlier-turn "
            "availability; do not second-guess this as a redundant enable."
        )
    elif reload_tools and cap_hit:
        lines.append(
            f"[Reload cap hit]: this turn has already triggered "
            f"{current_reloads}/{reload_cap} in-turn graph rebuilds. The new "
            "binding was persisted to this thread's config, but will NOT be "
            "bound to the model until the next user message. Do not attempt "
            "to call the newly-enabled tool(s) in this turn; answer only with "
            "that limitation if a response is needed."
        )
    else:
        lines.append("No binding changes; nothing to reload.")
    result_text = "\n".join(lines)

    return ToolBindingResult(
        ok=True,
        text=result_text,
        reload_tools=reload_tools,
        ttl_key=ttl_key,
        ttl_seconds=ttl_seconds,
        cap_hit=bool(cap_hit),
        source=source,
        skill_name=skill_name,
        reason=reason,
    )


def _enable(
    tool_names: List[str],
    category: str,
    thread_id: str,
    user_id: str,
    ttl: str = DEFAULT_TTL,
    tool_call_id: Optional[str] = None,
    *,
    source: str = "tool_search",
    skill_name: Optional[str] = None,
    reason: Optional[str] = None,
) -> Union[str, Command]:
    """Enable tools for a thread. Returns a string for no-op / refresh-only
    cases and for dynamic-binding mode, or a Command(goto=END) in legacy
    rebuild mode when a genuinely new tool was added.
    """
    binding = bind_tools_for_thread(
        tool_names,
        category,
        thread_id,
        user_id,
        ttl=ttl,
        strict=False,
        source=source,
        skill_name=skill_name,
        reason=reason,
    )

    # Legacy rebuild mode ends the graph after this tool result so the
    # astream()/chat() reload hook can rebuild. Dynamic-binding mode skips
    # Command(goto=END); the next model step resolves the updated tools.
    if (
        binding.reload_tools
        and tool_call_id
        and not binding.cap_hit
        and should_emit_reload_command(binding.reload_tools, thread_id=thread_id)
    ):
        return tool_reload_command(binding.text, tool_call_id)
    return binding.text


def _disable(tool_names: List[str], thread_id: str, force: bool = False) -> str:
    from ..core.agent import get_current_agent
    from . import CATALOG_TOOLS, resolve_default_tool_names

    agent = get_current_agent()
    if agent is None:
        return "[Error]: No active agent. Cannot modify thread config."

    if not tool_names:
        return "[Error]: Provide tool names to disable via the 'tools' parameter."

    catalog = _build_catalog()
    all_known = set(CATALOG_TOOLS.keys()) | set(catalog.keys())
    valid = [n for n in tool_names if n in all_known]
    invalid = [n for n in tool_names if n not in all_known]

    if not valid:
        return f"[Error]: No valid tools to disable. Unknown: {', '.join(invalid)}"

    # Guard against accidental lockout from the thread's default tools.
    # Disabling bash_execute, file_read, tool_search, etc. can cripple the
    # thread. Require an explicit force=True opt-in, but still proceed with
    # the rest (partial success), so mixed batches don't have their unguarded
    # portion blocked just because a protected name snuck in. The agent can
    # retry the refused subset with force=True.
    #
    # The protected set is the thread's ACTUAL default-bound tools (the owner's
    # default_thread_tools, or the seed set when uninitialized): the same
    # source graph-build uses, not the raw SEED_TOOLS list. Using SEED_TOOLS
    # would over-protect seed tools the user demoted out of their defaults (not
    # even bound to the thread) and under-protect optional tools the user
    # promoted in and now relies on.
    try:
        owner_id = agent.accounts_repo.get_thread_owner(thread_id) or "default"
        owner_default = (
            agent.profile_manager.get_profile(owner_id).tool_preferences.default_thread_tools
        )
    except Exception:
        owner_default = None
    protected_names = set(resolve_default_tool_names(owner_default))
    protected_targets = [n for n in valid if n in protected_names]
    other_targets = [n for n in valid if n not in protected_names]

    if protected_targets and not force:
        refused_protected = protected_targets
        targets = other_targets
    else:
        refused_protected = []
        targets = valid

    # All targets were protected with no force, nothing left to do.
    if not targets:
        return (
            f"[Error]: Refusing to disable default tool(s) without force=True: "
            f"{', '.join(sorted(refused_protected))}.\n"
            "These are the thread's default tools (shell access, file I/O, "
            "memory, tool discovery, etc.) and disabling them can severely "
            "limit the agent's ability to recover.\n"
            "If you're certain, retry with force=True."
        )

    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        from ..core.thread_config import ThreadConfig
        tc = ThreadConfig(thread_id=thread_id)

    # disabled_tools is authoritative in graph-build (it filters both the
    # default set AND the extras from enabled_tools/temporary_tools). We
    # only add to disabled_tools here — NOT remove from enabled_tools or
    # temporary_tools — so that a subsequent un-disable can restore the
    # tool's original permanent/TTL state. Otherwise a disable→enable
    # round-trip silently drops the permanent flag and any TTL remaining,
    # and the tool comes back either plain-default or re-classified.
    new_disabled = set(tc.disabled_tools) | set(targets)
    tc.disabled_tools = sorted(new_disabled)
    # enabled_tools / temporary_tools intentionally preserved.

    if not agent.thread_config_manager.save_config(tc):
        return "[Error]: Failed to save thread config."

    agent.invalidate_thread_config_cache(thread_id)

    forced_protected = [n for n in targets if n in protected_names]
    lines = [f"[Success]: Disabled {len(targets)} tool(s): {', '.join(sorted(targets))}"]
    if forced_protected:
        lines.append(
            f"[Warning]: {len(forced_protected)} default tool(s) disabled (force=True): "
            f"{', '.join(sorted(forced_protected))}. "
            "Re-enable with tool_manage(action=\"enable\", ...) if you need them."
        )
    if refused_protected:
        lines.append(
            f"[Refused]: {len(refused_protected)} default tool(s) not disabled "
            f"(force=False): {', '.join(sorted(refused_protected))}. "
            "Retry just this subset with force=True if disabling them is "
            "actually what you want."
        )
    if invalid:
        lines.append(f"[Not found]: {', '.join(invalid)}")
    lines.append("")
    lines.append(
        "Disabled tools will be UNBOUND from the model starting with the next "
        "agent step (graph rebuild happens before the next turn). They remain "
        "callable for the rest of this turn's current invocation."
    )
    return "\n".join(lines)


def _list_categories(user_role: str = "user") -> str:
    catalog = _build_discovery_catalog(user_role)
    cat_counts: Dict[str, List[str]] = {}
    for c in catalog.values():
        cat_counts.setdefault(c["category"], []).append(c["name"])

    if not cat_counts:
        return "[Tool Categories]: No optional tool categories found."

    lines = [f"[Tool Categories]: {len(cat_counts)} categories with optional tools"]
    for cat_name in sorted(cat_counts.keys()):
        tools = sorted(cat_counts[cat_name])
        preview = ", ".join(tools[:4])
        if len(tools) > 4:
            preview += ", ..."
        lines.append(f"  {cat_name} ({len(tools)} tools): {preview}")

    return "\n".join(lines)


def _status(thread_id: str) -> str:
    enabled_perm, temp_map, disabled = _get_thread_status(thread_id)
    catalog = _build_catalog()

    # disabled_tools is authoritative — a tool in both enabled_tools (or
    # temporary_tools) AND disabled_tools is currently unbound. Filter it
    # out of the enabled sections so the same tool doesn't appear in two
    # places, and surface the preserved state in the Disabled section
    # instead so the user still knows what will restore on un-disable.
    visible_enabled_perm = enabled_perm - disabled
    visible_temp_map = {k: v for k, v in temp_map.items() if k not in disabled}

    lines = ["[Thread Tool Status]"]

    if visible_enabled_perm:
        lines.append(f"\nEnabled (permanent) ({len(visible_enabled_perm)}):")
        for name in sorted(visible_enabled_perm):
            c = catalog.get(name, {})
            desc = _short_desc(c.get("description", ""))
            cat = c.get("category", "unknown")
            lines.append(f"  {name} ({cat}): {desc}")

    if visible_temp_map:
        lines.append(f"\nEnabled (TTL) ({len(visible_temp_map)}):")
        for name in sorted(visible_temp_map):
            entry = visible_temp_map[name]
            c = catalog.get(name, {})
            desc = _short_desc(c.get("description", ""))
            cat = c.get("category", "unknown")
            remaining = _format_remaining(entry.expires_at)
            lines.append(f"  {name} ({cat}, {remaining}): {desc}")

    if not visible_enabled_perm and not visible_temp_map:
        lines.append("\nNo optional tools enabled for this thread.")

    if disabled:
        lines.append(f"\nDisabled ({len(disabled)}):")
        for name in sorted(disabled):
            # Annotate preserved state so the user can see which disabled
            # tools will round-trip back to permanent/TTL on un-disable.
            if name in enabled_perm:
                lines.append(f"  {name} (preserved: permanent)")
            elif name in temp_map:
                remaining = _format_remaining(temp_map[name].expires_at)
                lines.append(f"  {name} (preserved: {remaining})")
            else:
                lines.append(f"  {name}")

    return "\n".join(lines)


def _default_bound_tools(agent: Any, user_id: str) -> set[str]:
    from . import SEED_TOOLS, filter_admin_only_tools, filter_developer_only_tools

    try:
        profile = agent.profile_manager.get_profile(user_id or "default")
        default_tools_pref = profile.tool_preferences.default_thread_tools
    except Exception:
        default_tools_pref = None

    names = {t.name for t in SEED_TOOLS} if default_tools_pref is None else set(default_tools_pref)
    try:
        user = agent.accounts_repo.get_user_by_id(user_id) if user_id else None
        role = user.role if user else "user"
        names, _ = filter_admin_only_tools(names, role)
        names, _ = filter_developer_only_tools(names, role)
    except Exception:
        logger.debug("Failed to filter default tools for user role", exc_info=True)
    return set(names)


def _tool_is_known(name: str, catalog: dict[str, Any], agent: Any) -> bool:
    if name in catalog:
        return True
    registry = getattr(agent, "tool_registry", None)
    return bool(registry and registry.get_tool(name))


def _prune_tools(
    *,
    thread_id: str,
    user_id: str,
    stale_after_days: int = 30,
    min_enabled_age_days: int = 7,
    dry_run: bool = False,
) -> str:
    from ..core.agent import get_current_agent
    from ..core.capability_usage import get_capability_usage_store

    agent = get_current_agent()
    if agent is None:
        return json.dumps({"ok": False, "error": "No active agent. Cannot modify thread config."}, indent=2)
    if not thread_id:
        return json.dumps({"ok": False, "error": "thread_id is required"}, indent=2)

    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        return json.dumps(
            {
                "ok": True,
                "action": "prune",
                "dry_run": dry_run,
                "changed": False,
                "message": "thread has no tool config",
            },
            indent=2,
        )

    stale_days = max(1, int(stale_after_days or 30))
    min_age_days = max(0, int(min_enabled_age_days or 0))
    now = utc_now()
    stale_cutoff = now - timedelta(days=stale_days)
    config_old_enough = ensure_aware_utc(tc.updated_at) <= now - timedelta(days=min_age_days)

    catalog = _build_catalog()
    default_bound = _default_bound_tools(agent, user_id)
    usage_store = get_capability_usage_store()

    enabled = list(tc.enabled_tools or [])
    disabled = list(tc.disabled_tools or [])
    temporary = dict(tc.temporary_tools or {})

    expired_temporary = [
        name
        for name, entry in temporary.items()
        if ensure_aware_utc(entry.expires_at) <= now
    ]
    unavailable_enabled = [
        name
        for name in enabled
        if name not in PROTECTED_MANAGEMENT_TOOL_NAMES and not _tool_is_known(name, catalog, agent)
    ]
    unavailable_disabled = [
        name
        for name in disabled
        if name not in PROTECTED_MANAGEMENT_TOOL_NAMES and not _tool_is_known(name, catalog, agent)
    ]
    unavailable_temporary = [
        name
        for name in temporary
        if name not in PROTECTED_MANAGEMENT_TOOL_NAMES and not _tool_is_known(name, catalog, agent)
    ]
    redundant_enabled = [
        name
        for name in enabled
        if (
            name not in PROTECTED_MANAGEMENT_TOOL_NAMES
            and name in default_bound
            and name not in disabled
            and name not in unavailable_enabled
        )
    ]

    stale_enabled: list[str] = []
    if config_old_enough:
        for name in enabled:
            if (
                name in PROTECTED_MANAGEMENT_TOOL_NAMES
                or name in default_bound
                or name in disabled
                or name in unavailable_enabled
                or name in redundant_enabled
            ):
                continue
            usage = usage_store.get_tool(user_id=user_id, thread_id=thread_id, name=name)
            last_used = parse_usage_timestamp(usage.last_used_at)
            if last_used is not None and last_used <= stale_cutoff:
                stale_enabled.append(name)

    remove_enabled = set(unavailable_enabled) | set(redundant_enabled) | set(stale_enabled)
    remove_disabled = set(unavailable_disabled)
    remove_temporary = set(expired_temporary) | set(unavailable_temporary)

    changed = bool(remove_enabled or remove_disabled or remove_temporary)
    if changed and not dry_run:
        tc.enabled_tools = [name for name in enabled if name not in remove_enabled]
        tc.disabled_tools = [name for name in disabled if name not in remove_disabled]
        tc.temporary_tools = {
            name: entry
            for name, entry in temporary.items()
            if name not in remove_temporary
        }
        if not agent.thread_config_manager.save_config(tc):
            return json.dumps({"ok": False, "error": "failed to save thread tool config"}, indent=2)
        if hasattr(agent, "invalidate_thread_config_cache"):
            agent.invalidate_thread_config_cache(thread_id)

    return json.dumps(
        {
            "ok": True,
            "action": "prune",
            "dry_run": dry_run,
            "changed": changed,
            "thread_id": thread_id,
            "stale_after_days": stale_days,
            "min_enabled_age_days": min_age_days,
            "removed": {
                "enabled_unavailable": sorted(unavailable_enabled),
                "enabled_redundant_default": sorted(redundant_enabled),
                "enabled_stale_used_before_cutoff": sorted(stale_enabled),
                "disabled_unavailable": sorted(unavailable_disabled),
                "temporary_expired": sorted(expired_temporary),
                "temporary_unavailable": sorted(unavailable_temporary),
            },
            "protected": sorted(
                name
                for name in set(enabled) | set(disabled) | set(temporary)
                if name in PROTECTED_MANAGEMENT_TOOL_NAMES
            ),
            "notes": [
                "Stale pruning only removes explicit permanent enabled_tools with recorded usage older than the cutoff.",
                "Bindings with no recorded usage are left intact.",
            ],
        },
        indent=2,
        default=str,
    )


@tool
def tool_search(
    query: str = "",
    category: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
    top_k: int = 15,
    include_status: bool = True,
) -> str:
    """
    Search available tools by keyword/category; enable results with tool_manage.

    Args:
        query: Search keyword.
        category: Optional category filter (e.g. "email", "twitch").
        top_k: Max results to return, capped at 50.
        include_status: Include current-thread enabled/disabled annotations.

    Returns:
        "[Tool Search]: N result(s)" header + per result: name
        (category, security_level) [ENABLED/DISABLED], description,
        and enable hint. "[No results]: ..." when empty.
    """
    thread_id = get_thread_id(config)
    user_id = get_user_id(config)
    user_role = _get_user_role(user_id)
    logger.info(
        f"tool_search: query={query!r}, category={category!r}, "
        f"top_k={top_k!r}, include_status={include_status!r}"
    )
    return _search(
        query,
        category,
        thread_id,
        user_role=user_role,
        top_k=top_k,
        include_status=include_status,
    )


def _tool_manage_impl(
    action: str,
    tools: Optional[List[str]] = None,
    category: str = "",
    ttl: Optional[str] = None,
    force: bool = False,
    stale_after_days: int = 30,
    min_enabled_age_days: int = 7,
    dry_run: bool = False,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg],
    source: str = "tool_manage",
) -> Union[str, Command]:
    action = action.strip().lower()
    thread_id = get_thread_id(config)
    user_id = get_user_id(config)
    user_role = _get_user_role(user_id)
    logger.info(
        f"{source}: action={action}, category={category!r}, "
        f"tools={tools}, ttl={ttl!r}"
    )

    if action == "enable":
        if ttl is None:
            return (
                "[Error]: ttl is required for enable. "
                "Format: Nm, Nh, Nd, Nw, or 'never'."
            )
        return _enable(
            tools or [],
            category,
            thread_id,
            user_id,
            ttl=ttl,
            tool_call_id=tool_call_id,
            source=source,
        )
    if action == "disable":
        return _disable(tools or [], thread_id, force=force)
    if action == "prune":
        return _prune_tools(
            thread_id=thread_id,
            user_id=user_id,
            stale_after_days=stale_after_days,
            min_enabled_age_days=min_enabled_age_days,
            dry_run=dry_run,
        )
    if action == "list_categories":
        return _list_categories(user_role=user_role)
    if action in {"status", "inspect"}:
        return _status(thread_id)
    return (
        f"[Error]: Unknown action '{action}'. "
        "Use: enable, disable, prune, list_categories, status"
    )


@tool
def tool_manage(
    action: str,
    tools: Optional[List[str]] = None,
    category: str = "",
    ttl: Optional[str] = None,
    force: bool = False,
    stale_after_days: int = 30,
    min_enabled_age_days: int = 7,
    dry_run: bool = False,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> Union[str, Command]:
    """
    Manage current-thread tool bindings: enable, disable, prune, list_categories, status.

    Args:
        action: One of: enable, disable, prune, list_categories, status.
        tools: Tool names to enable or disable.
        category: Category name to enable or list/filter.
        ttl: Required for enable action. Duration format: Nm (minutes),
            Nh (hours), Nd (days), Nw (weeks), or "never" for permanent.
        force: For disable only. Set True to allow disabling core tools.
        stale_after_days: For prune, remove recorded-stale permanent bindings
            whose last use is older than this many days.
        min_enabled_age_days: For prune, require the thread config to be at
            least this old before stale-age pruning.
        dry_run: For prune, report changes without saving them.
    """
    return _tool_manage_impl(
        action,
        tools,
        category,
        ttl,
        force,
        stale_after_days,
        min_enabled_age_days,
        dry_run,
        tool_call_id=tool_call_id,
        config=config,
        source="tool_manage",
    )


TOOL_SEARCH_TOOLS = [tool_search, tool_manage]
