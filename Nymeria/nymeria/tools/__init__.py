"""Nymeria tools module.

Consolidated tool set for maximum autonomy with minimal complexity.
Callable threads replace the old sub-agent system. Any thread can become
a callable tool with its own system prompt, LLM config, and tool set.

Use get_all_tools_with_agents() for backward compatibility (returns ALL_TOOLS).
Callable thread tools are added per-graph in _build_graph_with_prompt(), not globally.
"""

from .bash import bash_execute
from .filesystem import file_read, file_write
from .web import web_search
from .think import consult, CONSULT_TOOLS
from .claude_code import claude_code
from .memory import (
    profile_save,
    profile_forget,
    profile_list,
    personality_set,
    rag_search,
    PROFILE_TOOLS,
)
from .thread_notes import (
    notepad_write,
    notepad_read,
    notepad_edit,
    notepad_clear,
    NOTEPAD_TOOLS,
)
from .todo import (
    nym_todo,
    nym_todo_delete,
    nym_todo_list,
    todo,
    todo_delete,
    todo_list,
    TODO_TOOLS,
)
from .subagent import (
    reload_all,
    self_modify_rollback,
    SUBAGENT_TOOLS,
)
from .outlook_auth import AUTH_TOOLS
from .outlook_email import EMAIL_TOOLS
from .browser import BROWSER_TOOLS
from .calendar import CALENDAR_TOOLS
from .notify import notify, NOTIFY_TOOLS
from .triggers import (
    trigger_create,
    trigger_list,
    trigger_update,
    trigger_delete,
    trigger_inspect,
    TRIGGER_TOOLS,
)
from .hello_test import hello_test
from .sticky_note import sticky_note, STICKY_NOTE_TOOLS
from .google_docs import GOOGLE_DOCS_TOOLS
from .google_sheets import GOOGLE_SHEETS_TOOLS
from ._prv_a_supplier import _PRV_TOOLS_A1
from ._prv_a_vendor import _PRV_TOOLS_A2
from ._prv_a_products import _PRV_TOOLS_A3
from ._prv_a_acme import _PRV_TOOLS_A4
from ._prv_a_acme import _PRV_TOOLS_A5
from .outlook_attachments import OUTLOOK_ATTACHMENT_TOOLS
from .twitch import TWITCH_TOOLS
from .slash_command import slash_command, SLASH_COMMAND_TOOLS
from .tool_search import tool_search, TOOL_SEARCH_TOOLS
from .search_skills import (
    list_installed_skills,
    search_skills,
    install_skill,
    SEARCH_SKILLS_TOOLS,
)
from .search_mcp import (
    mcp_search,
    mcp_install,
    SEARCH_MCP_TOOLS,
)
from .activity_feed import activity_feed, ACTIVITY_FEED_TOOLS
from .watchdog_dispatch import watchdog_dispatch, watchdog_read_notepad, watchdog_todo_overview, WATCHDOG_DISPATCH_TOOLS
from .spawn_thread import spawn_thread, SPAWN_THREAD_TOOLS

WATCHDOG_TOOLS = ACTIVITY_FEED_TOOLS + WATCHDOG_DISPATCH_TOOLS

# Combined Outlook tools list
OUTLOOK_TOOLS = AUTH_TOOLS + EMAIL_TOOLS

# Combined _PRV_A tools list (all Google Sheets-based _PRV_A tools)
_PRV_TOOLS_A = (
    GOOGLE_SHEETS_TOOLS
    + _PRV_TOOLS_A1
    + _PRV_TOOLS_A2
    + _PRV_TOOLS_A3
    + _PRV_TOOLS_A4
    + _PRV_TOOLS_A5
)

# Self-modify tools (from core/self_agent.py)
from ..core.self_agent import SELF_AGENT_TOOLS

# Optional tools — available for per-thread enabling but NOT loaded by default.
# Maps tool name -> tool object. Users enable these via thread config UI.
OPTIONAL_TOOLS = {t.name: t for t in (
    [claude_code, sticky_note, hello_test]
    + OUTLOOK_TOOLS
    + OUTLOOK_ATTACHMENT_TOOLS
    + TRIGGER_TOOLS
    + BROWSER_TOOLS
    + CALENDAR_TOOLS
    + SELF_AGENT_TOOLS
    + SUBAGENT_TOOLS
    + GOOGLE_DOCS_TOOLS
    + _PRV_TOOLS_A
    + TWITCH_TOOLS
    + SLASH_COMMAND_TOOLS
    + WATCHDOG_TOOLS
    + SPAWN_THREAD_TOOLS
)}

# Tools that mutate the running codebase (read/write/delete project source,
# reload modules, roll back self-modifications, run arbitrary bash via
# claude_code). In multi-user mode these are admin-only — a non-admin
# enabling them on their own thread would effectively be authenticated
# remote code modification of the shared backend. Enforced at every API
# write boundary (thread config, default tool set, unified enable), at
# every agent-callable write site (tool_search, spawn_thread, slash
# /tools), and as defense-in-depth at graph-build time. Names — not tool
# objects — so the gate survives reload_all().
ADMIN_ONLY_OPTIONAL_TOOL_NAMES = frozenset(
    [t.name for t in (SELF_AGENT_TOOLS + SUBAGENT_TOOLS)] + [claude_code.name]
)


def filter_admin_only_tools(
    tool_names,
    user_role: str,
) -> tuple[set, set]:
    """Filter admin-only tool names out for non-admin users.

    Returns ``(allowed, blocked)``: the input names split into a set the
    caller may have, and a set the caller is not allowed to enable.
    Admins see everything allowed; for any other role, names in
    ``ADMIN_ONLY_OPTIONAL_TOOL_NAMES`` are stripped into ``blocked``.

    This is the single chokepoint shared by tool_search, spawn_thread, the
    REST gate at PATCH /threads/{id}/config, and the graph-build defense-
    in-depth filter. Keeping the logic here means a future addition to the
    admin-only set propagates everywhere.
    """
    names = set(tool_names)
    if user_role == "admin":
        return names, set()
    blocked = names & ADMIN_ONLY_OPTIONAL_TOOL_NAMES
    return names - blocked, blocked

# All available tools
ALL_TOOLS = [
    # Core system tools
    bash_execute,
    file_read,
    file_write,
    web_search,
    consult,
    # Profile tools (user memories & preferences)
    profile_save,
    profile_forget,
    profile_list,
    personality_set,
    rag_search,
    # Notepad tools (per-thread notes)
    notepad_write,
    notepad_read,
    notepad_edit,
    notepad_clear,
    # TODO tools
    nym_todo,
    nym_todo_delete,
    nym_todo_list,
    # Unified notification tool
    notify,
    # Tool discovery and management
    tool_search,
    # Skill discovery and installation
    list_installed_skills,
    search_skills,
    install_skill,
    # MCP server discovery and installation
    mcp_search,
    mcp_install,
]

__all__ = [
    "bash_execute",
    "file_read",
    "file_write",
    "web_search",
    "consult",
    "CONSULT_TOOLS",
    "claude_code",
    "profile_save",
    "profile_forget",
    "profile_list",
    "personality_set",
    "rag_search",
    "PROFILE_TOOLS",
    "notepad_write",
    "notepad_read",
    "notepad_edit",
    "notepad_clear",
    "NOTEPAD_TOOLS",
    "nym_todo",
    "nym_todo_delete",
    "nym_todo_list",
    "todo",
    "todo_delete",
    "todo_list",
    "TODO_TOOLS",
    "reload_all",
    "self_modify_rollback",
    "SUBAGENT_TOOLS",
    "notify",
    "NOTIFY_TOOLS",
    "trigger_create",
    "trigger_list",
    "trigger_update",
    "trigger_delete",
    "trigger_inspect",
    "TRIGGER_TOOLS",
    "AUTH_TOOLS",
    "EMAIL_TOOLS",
    "OUTLOOK_TOOLS",
    "BROWSER_TOOLS",
    "CALENDAR_TOOLS",
    "SELF_AGENT_TOOLS",
    "OPTIONAL_TOOLS",
    "ADMIN_ONLY_OPTIONAL_TOOL_NAMES",
    "filter_admin_only_tools",
    "ALL_TOOLS",
    "get_all_tools_with_agents",
    "hello_test",
    "sticky_note",
    "STICKY_NOTE_TOOLS",
    "GOOGLE_DOCS_TOOLS",
    "GOOGLE_SHEETS_TOOLS",
    "_PRV_TOOLS_A1",
    "_PRV_TOOLS_A2",
    "_PRV_TOOLS_A3",
    "_PRV_TOOLS_A4",
    "_PRV_TOOLS_A5",
    "_PRV_TOOLS_A",
    "TWITCH_TOOLS",
    "slash_command",
    "SLASH_COMMAND_TOOLS",
    "tool_search",
    "TOOL_SEARCH_TOOLS",
    "list_installed_skills",
    "search_skills",
    "install_skill",
    "SEARCH_SKILLS_TOOLS",
    "mcp_search",
    "mcp_install",
    "SEARCH_MCP_TOOLS",
    "activity_feed",
    "ACTIVITY_FEED_TOOLS",
    "watchdog_dispatch",
    "watchdog_read_notepad",
    "watchdog_todo_overview",
    "WATCHDOG_DISPATCH_TOOLS",
    "WATCHDOG_TOOLS",
    "spawn_thread",
    "SPAWN_THREAD_TOOLS",
]


def get_all_tools_with_agents() -> list:
    """
    Get ALL_TOOLS list.

    Kept for backward compatibility. Callable thread tools are now added
    per-graph in _build_graph_with_prompt(), not globally.

    Returns:
        List of core tools
    """
    return list(ALL_TOOLS)
