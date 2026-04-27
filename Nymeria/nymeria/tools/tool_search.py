"""Tool search and discovery for Nymeria.

Allows the agent to search optional tools by keyword or category,
enable/disable them for the current thread, and check tool status.

Enabling a tool with a TTL (default 2h) triggers an in-turn auto-continue:
the turn finishes the current graph invocation, rebuilds a fresh graph with
the new tools bound, and resumes via an internal `tool_reload_resume` message
so the agent can call the newly-enabled tools without waiting for the next
user message. See `core/agent.py::_do_tool_reload` for the orchestration
and `docs/tools.md` for the full flow.
"""

import logging
from datetime import datetime, timedelta
from typing import Annotated, Any, Dict, List, Optional, Tuple, Union

from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, InjectedToolCallId, tool
from langgraph.graph import END
from langgraph.types import Command

from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)


# TTL presets. None => permanent (no expiry).
TTL_PRESETS: Dict[str, Optional[int]] = {
    "30m": 30 * 60,
    "2h": 2 * 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "permanent": None,
}
DEFAULT_TTL = "2h"


def _format_remaining(expires_at: datetime) -> str:
    delta = expires_at - datetime.utcnow()
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
    from . import ALL_TOOLS, OPTIONAL_TOOLS
    from .metadata import (
        CUSTOM_TOOL_METADATA,
        MCP_SERVER_TOOL_METADATA,
        get_all_tool_metadata,
    )

    catalog = {}

    all_tools = {t.name: t for t in ALL_TOOLS}
    all_tools.update(OPTIONAL_TOOLS)

    for name, tool_obj in all_tools.items():
        if name == "tool_search":
            continue
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


def _resolve_tool_object(name: str, agent) -> Optional[Any]:
    """Return the actual tool object that would be bound at graph-build time.

    Mirrors the resolution logic in agent._build_graph_with_prompt so that
    enable-time validation matches what the rebuild actually does. Returns
    None if the name is in the searchable catalog (metadata only) but has
    no live tool object (typically MCP server tools whose backing server
    is installed but disabled).
    """
    from . import ALL_TOOLS, OPTIONAL_TOOLS

    all_tools_dict = {t.name: t for t in ALL_TOOLS}
    all_tools_dict.update(OPTIONAL_TOOLS)
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
            "  Activate them via mcp_install (which auto-enables) or the "
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


def _search(query: str, category: str, thread_id: str) -> str:
    catalog = _build_catalog()
    enabled_perm, temp_map, disabled = _get_thread_status(thread_id)

    candidates = list(catalog.values())
    if category:
        cat_key = category.lower().strip().replace("-", "_")
        candidates = [c for c in candidates if c["category"] == cat_key]
        if not candidates:
            from .metadata import get_all_categories
            cats = ", ".join(get_all_categories())
            return f"[No results]: Unknown category '{category}'. Available: {cats}"

    if not query and not category:
        return "[Error]: Provide a query, a category, or both."

    if not query:
        results = [(0, c) for c in candidates]
    else:
        query_lower = query.lower().strip()
        terms = query_lower.split()
        results = []
        for c in candidates:
            name_lower = c["name"].lower()
            desc_lower = c["description"].lower()
            score = 0
            if query_lower == name_lower:
                score = 100
            elif query_lower == name_lower.replace("_", ""):
                score = 95
            elif query_lower in name_lower:
                score = 80
            elif all(t in name_lower for t in terms):
                score = 70
            elif all(t in desc_lower for t in terms):
                score = 50
            elif any(t in name_lower for t in terms):
                score = 40
            elif any(t in desc_lower for t in terms):
                score = 20
            if score > 0:
                results.append((score, c))

    results.sort(key=lambda x: (-x[0], x[1]["name"]))
    results = results[:15]

    if not results:
        return f"[No results]: No tools matched '{query}'." + (
            f" (category filter: {category})" if category else ""
        )

    lines = [f"[Tool Search]: {len(results)} result(s)" + (
        f" in category '{category}'" if category else ""
    )]
    for _, c in results:
        status = ""
        # disabled_tools is authoritative — check it first so a disabled
        # tool with a preserved enabled/TTL entry doesn't get labeled ENABLED.
        if c["name"] in disabled:
            status = " [DISABLED]"
        elif c["name"] in enabled_perm:
            status = " [ENABLED permanent]"
        elif c["name"] in temp_map:
            status = f" [ENABLED {_format_remaining(temp_map[c['name']].expires_at)}]"
        lines.append(
            f"  {c['name']} ({c['category']}, {c['security_level']}){status}"
            f"\n    {c['description']}"
        )

    return "\n".join(lines)


def _enable(
    tool_names: List[str],
    category: str,
    thread_id: str,
    user_id: str,
    ttl: str = DEFAULT_TTL,
    tool_call_id: Optional[str] = None,
) -> Union[str, Command]:
    """Enable tools for a thread. Returns a string for no-op / refresh-only
    cases, or a Command(goto=END) when a genuinely new tool was added so the
    graph terminates immediately and the astream() reload hook can rebuild
    the tool list before the next agent step.
    """
    from ..core.agent import get_current_agent
    from ..core.thread_config import ThreadConfig, TemporaryToolEntry
    from . import ALL_TOOLS
    from .metadata import ToolCategory, get_all_tool_metadata, SecurityLevel

    agent = get_current_agent()
    if agent is None:
        return "[Error]: No active agent. Cannot modify thread config."

    ttl_key = (ttl or DEFAULT_TTL).strip().lower()
    if ttl_key not in TTL_PRESETS:
        options = ", ".join(f'"{k}"' for k in TTL_PRESETS)
        return f"[Error]: Invalid ttl '{ttl}'. Must be one of: {options}."
    ttl_seconds = TTL_PRESETS[ttl_key]

    if category and not tool_names:
        cat_key = category.lower().strip().replace("-", "_")
        try:
            ToolCategory(cat_key)
        except ValueError:
            from .metadata import get_all_categories
            cats = ", ".join(get_all_categories())
            return f"[Error]: Unknown category '{category}'. Available: {cats}"

        catalog = _build_catalog()
        tool_names = [c["name"] for c in catalog.values() if c["category"] == cat_key]
        if not tool_names:
            return f"[Error]: No tools in category '{category}'."

    if not tool_names:
        return "[Error]: Provide tool names via 'tools' or a category via 'category'."

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

    if not valid:
        # Surface unloadable details first (more actionable for the agent).
        if unloadable:
            err = _format_unloadable_error(unloadable)
            if invalid:
                err += f"\n[Not found]: {', '.join(invalid)}"
            return err
        return f"[Error]: No valid tools to enable. Unknown: {', '.join(invalid)}"

    # Admin-only gate. Mirror the REST gate at PATCH /threads/{id}/config —
    # without this, an agent could call tool_search(action="enable",
    # tools=["reload_all"]) to escalate to admin-only tools that are
    # equivalent to authenticated RCE on the shared backend.
    from . import filter_admin_only_tools
    user = agent.accounts_repo.get_user_by_id(user_id) if user_id else None
    user_role = user.role if user else "user"
    allowed, blocked = filter_admin_only_tools(valid, user_role)
    if blocked:
        return (
            f"[Error]: Admin-only tools cannot be enabled by this user: "
            f"{sorted(blocked)}. Ask an administrator to enable them on this "
            f"thread, or pick a non-admin alternative."
        )
    valid = [n for n in valid if n in allowed]

    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        tc = ThreadConfig(thread_id=thread_id)

    # Compute the thread's *actual* default-bound tool set. A user profile
    # can override ALL_TOOLS via profile.tool_preferences.default_thread_tools
    # (a curated subset), and graph-build uses that subset — not ALL_TOOLS —
    # to decide which tools to bind by default. Classifying against ALL_TOOLS
    # silently misclassifies any tool that lives in ALL_TOOLS but is absent
    # from default_thread_tools (e.g., `personality_set`): the classifier
    # thinks it's already bound, drops it into the no-op bucket, and never
    # actually adds it anywhere — the tool then vanishes. Using the same
    # source of truth as graph-build (`agent.py::_build_graph_with_prompt`)
    # keeps classification honest.
    try:
        profile = agent.profile_manager.get_profile("default")
        default_tools_pref = profile.tool_preferences.default_thread_tools
    except Exception:
        default_tools_pref = None
    if default_tools_pref is None:
        default_bound = {t.name for t in ALL_TOOLS}
    else:
        default_bound = set(default_tools_pref)

    newly_added: List[str] = []       # new binding written to enabled_tools/temporary_tools
    refreshed: List[str] = []         # TTL'd tool whose expires_at was pushed out
    promoted: List[str] = []          # moved from temporary_tools to enabled_tools
    already_default: List[str] = []   # already in default-bound set — no write
    already_permanent: List[str] = [] # already in tc.enabled_tools — no write
    un_disabled: List[str] = []       # removed from tc.disabled_tools

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
        # un-disable should restore preserved state AS-IS — the requested `ttl`
        # must NOT bleed into this path and overwrite a preserved permanent or
        # TTL entry. Mixed-batch example: enable([hello_test, sticky_note],
        # ttl="permanent") where hello_test is a TTL→promote and sticky_note is
        # a disabled-with-preserved-TTL: the "permanent" was intended for
        # hello_test, so sticky_note's 18m TTL must survive intact.
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
                    expires_at = datetime.utcnow() + timedelta(seconds=ttl_seconds)
                    new_temporary[name] = TemporaryToolEntry(expires_at=expires_at)
            continue

        # -- Not disabled: priority-ordered classification --
        # (1) explicitly persisted permanent wins — even if the tool is ALSO
        #     in default_bound. Matches what `status` shows the user.
        if in_enabled:
            already_permanent.append(name)
            continue

        # (2) part of the thread's default-bound set — no write needed,
        #     adding to enabled_tools would be redundant clutter. (Note:
        #     this bucket is NOT the same as the "core" used by the
        #     disable guard below — that protects ALL_TOOLS hardcoded
        #     essentials, a broader concept than default-bound.)
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
                expires_at = datetime.utcnow() + timedelta(seconds=ttl_seconds)
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
            expires_at = datetime.utcnow() + timedelta(seconds=ttl_seconds)
            new_temporary[name] = TemporaryToolEntry(expires_at=expires_at)
        newly_added.append(name)

    tc.enabled_tools = sorted(new_enabled)
    tc.disabled_tools = sorted(new_disabled)
    tc.temporary_tools = new_temporary

    if not agent.thread_config_manager.save_config(tc):
        return "[Error]: Failed to save thread config."

    agent.invalidate_thread_config_cache(thread_id)

    # An in-turn reload is needed when we added a genuinely new binding —
    # that's newly_added (optional tools new to the graph) OR un_disabled
    # (tools that were previously filtered out by tc.disabled_tools, now
    # un-filtered so they'll be bound on rebuild).
    reload_tools = sorted(set(newly_added) | set(un_disabled))

    # If the turn has already hit the reload cap, we still persist the
    # enablement but don't force another rebuild. Otherwise the tool would
    # return Command(goto=END), the graph would exit, and the astream/chat
    # reload loop would skip (cap exhausted) — leaving an orphan tool_result
    # with no LLM response. Instead, return a plain string so the LLM can
    # still respond in-turn; the new binding kicks in on the next user turn.
    reload_cap = getattr(agent, "MAX_TOOL_RELOADS_PER_TURN", 1)
    current_reloads = getattr(agent, "_turn_reload_count", {}).get(thread_id, 0)
    cap_hit = reload_tools and current_reloads >= reload_cap

    if reload_tools and not cap_hit:
        agent._pending_tool_reload[thread_id] = {
            "new_tools": reload_tools,
            "ttl": ttl_key,
            "ttl_seconds": ttl_seconds,
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
    if reload_tools and not cap_hit:
        lines.append(
            "Newly-loaded tools are NOT yet bound to the model in this "
            "iteration. Your current turn will end after this tool result "
            "and the system will rebuild the tool list, then resume you with "
            "the new tools available. Do not attempt to call them here."
        )
    elif reload_tools and cap_hit:
        lines.append(
            f"[Reload cap hit]: this turn has already triggered "
            f"{current_reloads}/{reload_cap} in-turn graph rebuilds. The new "
            "binding was persisted to this thread's config, but will NOT be "
            "bound to the model until the next user message. Do not attempt "
            "to call the newly-enabled tool(s) in this turn."
        )
    else:
        lines.append("No binding changes; nothing to reload.")
    result_text = "\n".join(lines)

    # When a reload is queued AND we're under the cap, force the graph to
    # END after this tool result so the astream()/chat() reload hook fires
    # immediately. Past the cap, return a plain string so the LLM can
    # respond in-turn (avoids leaving an orphan tool_result with no
    # follow-up response when the cap would otherwise eat the reload).
    if reload_tools and tool_call_id and not cap_hit:
        return Command(
            goto=END,
            update={
                "messages": [
                    ToolMessage(content=result_text, tool_call_id=tool_call_id)
                ]
            },
        )
    return result_text


def _disable(tool_names: List[str], thread_id: str, force: bool = False) -> str:
    from ..core.agent import get_current_agent
    from . import ALL_TOOLS, OPTIONAL_TOOLS

    agent = get_current_agent()
    if agent is None:
        return "[Error]: No active agent. Cannot modify thread config."

    if not tool_names:
        return "[Error]: Provide tool names to disable via the 'tools' parameter."

    catalog = _build_catalog()
    all_known = set(OPTIONAL_TOOLS.keys()) | set(catalog.keys())
    valid = [n for n in tool_names if n in all_known]
    invalid = [n for n in tool_names if n not in all_known]

    if not valid:
        return f"[Error]: No valid tools to disable. Unknown: {', '.join(invalid)}"

    # Guard against accidental core-tool lockout. Disabling bash_execute,
    # file_read, tool_search, etc. can cripple the thread. Require an
    # explicit force=True opt-in — but still proceed with the non-core
    # subset (partial success), so mixed batches like disable([core, opt])
    # don't have their non-core portion blocked just because a core name
    # snuck in. The agent can retry the refused core subset with force=True.
    core_names = {t.name for t in ALL_TOOLS}
    core_targets = [n for n in valid if n in core_names]
    non_core_targets = [n for n in valid if n not in core_names]

    if core_targets and not force:
        refused_core = core_targets
        targets = non_core_targets
    else:
        refused_core = []
        targets = valid

    # All targets were core with no force — nothing left to do.
    if not targets:
        return (
            f"[Error]: Refusing to disable core tool(s) without force=True: "
            f"{', '.join(sorted(refused_core))}.\n"
            "These tools are foundational to this thread (shell access, file "
            "I/O, memory, tool discovery, etc.) and disabling them can "
            "severely limit the agent's ability to recover.\n"
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

    forced_core = [n for n in targets if n in core_names]
    lines = [f"[Success]: Disabled {len(targets)} tool(s): {', '.join(sorted(targets))}"]
    if forced_core:
        lines.append(
            f"[Warning]: {len(forced_core)} CORE tool(s) disabled (force=True): "
            f"{', '.join(sorted(forced_core))}. "
            "Re-enable with tool_search(action=\"enable\", ...) if you need them."
        )
    if refused_core:
        lines.append(
            f"[Refused]: {len(refused_core)} core tool(s) not disabled "
            f"(force=False): {', '.join(sorted(refused_core))}. "
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


def _list_categories() -> str:
    catalog = _build_catalog()
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

    lines = [f"[Thread Tool Status]"]

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


@tool
def tool_search(
    action: str,
    query: str = "",
    category: str = "",
    tools: Optional[List[str]] = None,
    ttl: str = DEFAULT_TTL,
    force: bool = False,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> Union[str, Command]:
    """
    Search, enable, and disable optional tools for this thread.

    There are 100+ tools organized by category (email, browser, calendar,
    google_docs, twitch, _prv_a, trigger, self_modify, etc.). Use this tool
    to discover and activate them for the current thread.

    HOW ENABLE WORKS (important, read carefully):

    When you call action="enable" and at least one tool is genuinely new,
    your CURRENT turn ends immediately after the tool result. The harness
    then rebuilds the tool list with the new tools bound to the model and
    resumes you in a fresh agent step where the new tools ARE callable.

    Concretely: you do NOT call the new tool in the same iteration as the
    enable. That fails because the model is bound to the old tool list
    for the rest of the iteration. After your enable returns, the system
    will resume you automatically (no user reply needed) and THAT is where
    you invoke the newly-enabled tool. Treat the enable + use as two
    discrete steps: enable now, end of turn happens, then use after resume.

    Enabling a tool that's already in the bound set (refresh / no-op /
    promotion to permanent) does NOT trigger a turn-end; the agent just
    keeps going.

    TTL: each tool enablement expires unless marked permanent. Pick the
    shortest TTL that covers your task to keep the tool list lean:
      "30m":       one-shot operation
      "2h":        default, typical multi-step task
      "6h":        sustained workflow in a single session
      "24h":       multi-session workflow, e.g. across a workday
      "permanent": only if you're confident the user wants this as a
                   standing capability on this thread
    Calling enable again on a TTL'd tool resets its expiry. Calling it
    with ttl="permanent" promotes it out of the TTL bucket. Expired
    entries are evicted at the start of the next turn (lazy, no surprise
    ejections mid-turn).

    MCP tools: if you try to enable an mcp__<server>__<tool> whose backing
    server is installed but disabled, enable will reject it. Use
    mcp_install (which auto-enables) or activate the server in
    Settings → MCP first, then retry.

    Disabling core tools (bash_execute, file_read, tool_search itself, etc.)
    can cripple the thread, so disable refuses core names by default. Set
    force=True to override. "Core" here means a tool that's in the codebase's
    hardcoded ALL_TOOLS list (the superset of essential tools), which is
    broader than the "default-bound" set the classifier uses. A user's
    default_thread_tools preference curates a subset of ALL_TOOLS, so a
    tool can be default-bound for your thread but still protected by this
    guard. Mixed batches (core + non-core) succeed for the non-core portion
    and refuse only the core names; retry the refused subset separately with
    force=True if you actually want them disabled. There is no symmetric
    guard on the desktop Settings → Tools UI; this rule only applies to the
    agent-facing path.

    Disable now preserves the tool's prior permanent/TTL state. If you had
    a tool as permanent and force-disable it, the entry stays in
    enabled_tools but is suppressed from the bound set via disabled_tools.
    Un-disabling via enable restores the original state, so the permanent
    badge survives the round-trip.

    Actions:
      search:          Search tools by keyword and/or category
      enable:          Enable tools (ends the turn if new tools were added)
      disable:         Disable tools for this thread (next-message effect)
      list_categories: List all tool categories with counts
      status:          Show enabled/disabled tools (with TTL remaining)

    Enable response breakdown. Every input tool is classified in exactly
    one of these buckets, checked in this priority order:
      Un-disabled:             the name was in tc.disabled_tools and got
                               removed. Any preserved permanent or TTL
                               entry is restored AS-IS, so the requested
                               `ttl` does NOT apply (preventing a batch-
                               level TTL from silently promoting an
                               unrelated tool). Only when there's no
                               preserved state and no default binding does
                               a fresh entry get written using `ttl`.
      Already permanent:       already in tc.enabled_tools (persisted,
                               no expiry). TTL requests are rejected, so no
                               demotion from permanent to TTL.
      Already bound (default): part of the thread's default-bound tool set
                               (ALL_TOOLS or the user's default_thread_tools
                               override). Already callable, nothing written.
                               NOTE: "default-bound" is NOT the same as the
                               "core" protection used by disable; disable
                               protects ALL_TOOLS essentials (broader).
      TTL refreshed:           existed in tc.temporary_tools; expires_at
                               pushed out by ttl.
      Promoted to permanent:   existed in tc.temporary_tools; moved to
                               tc.enabled_tools (loses TTL, gains persistence).
      Newly loaded:            none of the above; written fresh to
                               enabled_tools (ttl=permanent) or
                               temporary_tools (ttl=30m/2h/6h/24h).

    Args:
        action: One of: search, enable, disable, list_categories, status
        query: Search keyword (for 'search' action)
        category: Category name to filter or enable (e.g. "email", "twitch")
        tools: List of tool names to enable or disable
        ttl: TTL preset for 'enable'. One of "30m", "2h" (default), "6h",
             "24h", or "permanent". Ignored for other actions.
        force: For 'disable' only. Set True to allow disabling core tools.
    """
    action = action.strip().lower()
    thread_id = get_thread_id(config)
    user_id = get_user_id(config)
    logger.info(
        f"tool_search: action={action}, query={query!r}, category={category!r}, "
        f"tools={tools}, ttl={ttl!r}"
    )

    if action == "search":
        return _search(query, category, thread_id)
    elif action == "enable":
        return _enable(tools or [], category, thread_id, user_id, ttl=ttl, tool_call_id=tool_call_id)
    elif action == "disable":
        return _disable(tools or [], thread_id, force=force)
    elif action == "list_categories":
        return _list_categories()
    elif action == "status":
        return _status(thread_id)
    else:
        return (
            f"[Error]: Unknown action '{action}'. "
            "Use: search, enable, disable, list_categories, status"
        )


TOOL_SEARCH_TOOLS = [tool_search]
