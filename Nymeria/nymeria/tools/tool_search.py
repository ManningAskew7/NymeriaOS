"""Tool search and discovery for Nymeria.

Allows the agent to search optional tools by keyword or category,
enable/disable them for the current thread, and check tool status.
"""

import logging
from typing import Annotated, Dict, List, Optional, Tuple

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .utils import get_thread_id

logger = logging.getLogger(__name__)


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


def _get_thread_status(thread_id: str) -> Tuple[set, set]:
    """Return (enabled_tools, disabled_tools) sets for a thread."""
    from ..core.agent import get_current_agent

    agent = get_current_agent()
    if agent is None:
        return set(), set()

    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        return set(), set()
    return set(tc.enabled_tools), set(tc.disabled_tools)


def _search(query: str, category: str, thread_id: str) -> str:
    catalog = _build_catalog()
    enabled, disabled = _get_thread_status(thread_id)

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
        if c["name"] in enabled:
            status = " [ENABLED]"
        elif c["name"] in disabled:
            status = " [DISABLED]"
        lines.append(
            f"  {c['name']} ({c['category']}, {c['security_level']}){status}"
            f"\n    {c['description']}"
        )

    return "\n".join(lines)


def _enable(tool_names: List[str], category: str, thread_id: str) -> str:
    from ..core.agent import get_current_agent
    from .metadata import ToolCategory, get_all_tool_metadata, SecurityLevel

    agent = get_current_agent()
    if agent is None:
        return "[Error]: No active agent. Cannot modify thread config."

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
    valid, invalid, warnings = [], [], []

    for name in tool_names:
        if name in all_known or (agent.tool_registry and agent.tool_registry.get_tool(name)):
            valid.append(name)
            meta = get_all_tool_metadata(name)
            if meta and meta.security_level == SecurityLevel.SENSITIVE:
                warnings.append(f"'{name}' is SENSITIVE")
        else:
            invalid.append(name)

    if not valid:
        return f"[Error]: No valid tools to enable. Unknown: {', '.join(invalid)}"

    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        from ..core.thread_config import ThreadConfig
        tc = ThreadConfig(thread_id=thread_id)

    current_enabled = set(tc.enabled_tools)
    current_disabled = set(tc.disabled_tools)
    new_enabled = current_enabled | set(valid)
    new_disabled = current_disabled - set(valid)

    tc.enabled_tools = sorted(new_enabled)
    tc.disabled_tools = sorted(new_disabled)

    if not agent.thread_config_manager.save_config(tc):
        return "[Error]: Failed to save thread config."

    agent.invalidate_thread_config_cache(thread_id)

    lines = [f"[Success]: Enabled {len(valid)} tool(s): {', '.join(sorted(valid))}"]
    if warnings:
        lines.append(f"[Warning]: {'; '.join(warnings)}")
    if invalid:
        lines.append(f"[Not found]: {', '.join(invalid)}")
    lines.append("")
    lines.append("These tools will be available on your NEXT message.")
    return "\n".join(lines)


def _disable(tool_names: List[str], thread_id: str) -> str:
    from ..core.agent import get_current_agent

    agent = get_current_agent()
    if agent is None:
        return "[Error]: No active agent. Cannot modify thread config."

    if not tool_names:
        return "[Error]: Provide tool names to disable via the 'tools' parameter."

    from . import OPTIONAL_TOOLS
    catalog = _build_catalog()
    all_known = set(OPTIONAL_TOOLS.keys()) | set(catalog.keys())
    valid = [n for n in tool_names if n in all_known]
    invalid = [n for n in tool_names if n not in all_known]

    if not valid:
        return f"[Error]: No valid tools to disable. Unknown: {', '.join(invalid)}"

    tc = agent.thread_config_manager.get_config(thread_id)
    if tc is None:
        from ..core.thread_config import ThreadConfig
        tc = ThreadConfig(thread_id=thread_id)

    current_enabled = set(tc.enabled_tools)
    current_disabled = set(tc.disabled_tools)
    new_disabled = current_disabled | set(valid)
    new_enabled = current_enabled - set(valid)

    tc.enabled_tools = sorted(new_enabled)
    tc.disabled_tools = sorted(new_disabled)

    if not agent.thread_config_manager.save_config(tc):
        return "[Error]: Failed to save thread config."

    agent.invalidate_thread_config_cache(thread_id)

    lines = [f"[Success]: Disabled {len(valid)} tool(s): {', '.join(sorted(valid))}"]
    if invalid:
        lines.append(f"[Not found]: {', '.join(invalid)}")
    lines.append("")
    lines.append("Changes take effect on your NEXT message.")
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
    enabled, disabled = _get_thread_status(thread_id)
    catalog = _build_catalog()

    lines = [f"[Thread Tool Status]"]

    if enabled:
        lines.append(f"\nEnabled optional tools ({len(enabled)}):")
        for name in sorted(enabled):
            c = catalog.get(name, {})
            desc = c.get("description", "")[:60]
            cat = c.get("category", "unknown")
            lines.append(f"  {name} ({cat}) — {desc}")
    else:
        lines.append("\nNo optional tools enabled for this thread.")

    if disabled:
        lines.append(f"\nDisabled tools ({len(disabled)}):")
        for name in sorted(disabled):
            lines.append(f"  {name}")

    return "\n".join(lines)


@tool
def tool_search(
    action: str,
    query: str = "",
    category: str = "",
    tools: Optional[List[str]] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Search, enable, and disable optional tools for this thread.

    There are 100+ tools organized by category (email, browser, calendar,
    google_docs, twitch, _prv_a, trigger, self_modify, etc.). Use this tool
    to discover and activate them for the current thread.

    Enabled/disabled tools take effect on the NEXT message, not the current
    turn. Tip: after enabling tools, use nym_todo to schedule a prompt
    to yourself (~10 seconds out) describing what to do with the new tools.
    This triggers a new turn where the tools are available automatically.

    Actions:
      search          — Search tools by keyword and/or category
      enable          — Enable tools for this thread (by name or category)
      disable         — Disable tools for this thread
      list_categories — List all tool categories with counts
      status          — Show enabled/disabled tools for this thread

    Args:
        action: One of: search, enable, disable, list_categories, status
        query: Search keyword (for 'search' action)
        category: Category name to filter or enable (e.g. "email", "twitch")
        tools: List of tool names to enable or disable
    """
    action = action.strip().lower()
    thread_id = get_thread_id(config)
    logger.info(f"tool_search: action={action}, query={query!r}, category={category!r}, tools={tools}")

    if action == "search":
        return _search(query, category, thread_id)
    elif action == "enable":
        return _enable(tools or [], category, thread_id)
    elif action == "disable":
        return _disable(tools or [], thread_id)
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
