"""Nymeria tools module.

Consolidated tool set for maximum autonomy with minimal complexity.
Callable threads replace the old sub-agent system. Any thread can become
a callable tool with its own system prompt, LLM config, and tool set.

Callable thread tools are added per-graph in _build_graph_with_prompt(), not globally.
"""

from .bash import bash_execute
from .filesystem import file_read, file_write
from .file_edit import file_edit, FILE_EDIT_TOOLS
from .web import web_search
from .think import consult, CONSULT_TOOLS
from .claude_code import claude_code
from .memory import (
    memory_add,
    memory_edit,
    memory_read,
    memory_clear_all,
    personality_set,
    rag_search,
    rag_settings,
    MEMORY_TOOLS,
)
from .todo import (
    nym_todo,
    nym_todo_delete,
    nym_todo_list,
    TODO_TOOLS,
)
from .runtime_admin import (
    reload_all,
    self_modify_rollback,
    RUNTIME_ADMIN_TOOLS,
)
from .outlook_auth import AUTH_TOOLS
from .outlook_email import EMAIL_TOOLS
from .browser import BROWSER_TOOLS
from .calendar import CALENDAR_TOOLS
from .notify import notify, NOTIFY_TOOLS
from .triggers import (
    trigger_config,
    trigger_info,
    TRIGGER_TOOLS,
)
from .hello_test import hello_test
from .sticky_note import sticky_note, STICKY_NOTE_TOOLS
from .google_docs import GOOGLE_DOCS_TOOLS
from .google_sheets import GOOGLE_SHEETS_TOOLS
from .gmail_auth import (
    gmail_auth_start,
    gmail_auth_complete,
    gmail_auth_clear,
    gmail_list_accounts,
    GMAIL_AUTH_TOOLS,
)
from ..plugins._prv_a import (
    _PRV_TOOLS_A1,
    _PRV_TOOLS_A2,
    _PRV_TOOLS_A3,
    _PRV_TOOLS_A4,
    _PRV_TOOLS_A5,
)
from .outlook_attachments import OUTLOOK_ATTACHMENT_TOOLS
from .twitch import TWITCH_TOOLS
from .slash_command import slash_command, SLASH_COMMAND_TOOLS
from .tool_search import tool_enable, tool_search, TOOL_SEARCH_TOOLS
from .http_api import http_request, api_discover, HTTP_API_TOOLS
from .tool_create import tool_create, TOOL_CREATE_TOOLS
from ._prv_b import _PRV_TOOLS_B
from .skill_config import (
    skill_config,
    skill_kit_create,
    SKILL_CONFIG_TOOLS,
    SKILL_KIT_CREATE_TOOLS,
)
from .search_skills import (
    skill_manage,
    list_installed_skills,
    search_skills,
    install_skill,
    SEARCH_SKILLS_TOOLS,
)
from .search_mcp import (
    mcp_manage,
    search_mcp,
    install_mcp_server,
    SEARCH_MCP_TOOLS,
)
from .activity_feed import activity_feed, ACTIVITY_FEED_TOOLS
from .watchdog_dispatch import watchdog_dispatch, watchdog_read_notepad, watchdog_todo_overview, WATCHDOG_DISPATCH_TOOLS
from .spawn_thread import spawn_thread, SPAWN_THREAD_TOOLS
from .image_generation import image_generate, IMAGE_GENERATION_TOOLS
from ..core.self_agent import SELF_AGENT_TOOLS

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

# Optional tools — available for per-thread enabling but NOT loaded by default.
# Maps tool name -> tool object. Users enable these via thread config UI.
OPTIONAL_TOOLS = {t.name: t for t in (
    [claude_code, sticky_note, hello_test, memory_clear_all, rag_settings]
    + FILE_EDIT_TOOLS
    + OUTLOOK_TOOLS
    + GMAIL_AUTH_TOOLS
    + OUTLOOK_ATTACHMENT_TOOLS
    + TRIGGER_TOOLS
    + BROWSER_TOOLS
    + CALENDAR_TOOLS
    + SELF_AGENT_TOOLS
    + RUNTIME_ADMIN_TOOLS
    + GOOGLE_DOCS_TOOLS
    + _PRV_TOOLS_A
    + TWITCH_TOOLS
    + SLASH_COMMAND_TOOLS
    + TOOL_SEARCH_TOOLS
    + SEARCH_SKILLS_TOOLS
    + SEARCH_MCP_TOOLS
    + HTTP_API_TOOLS
    + TOOL_CREATE_TOOLS
    + _PRV_TOOLS_B
    + SKILL_CONFIG_TOOLS
    + SKILL_KIT_CREATE_TOOLS
    + WATCHDOG_TOOLS
    + SPAWN_THREAD_TOOLS
    + IMAGE_GENERATION_TOOLS
)}

# Capability expansion tools are deliberately opt-in through the bundled
# self-improve Skill Kit. They remain valid explicit per-thread enablements for
# compatibility, but should not live in profile default_thread_tools.
CAPABILITY_EXPANSION_TOOL_NAMES = frozenset(
    t.name
    for t in (
        TOOL_SEARCH_TOOLS
        + SEARCH_MCP_TOOLS
        + SEARCH_SKILLS_TOOLS
        + HTTP_API_TOOLS
        + TOOL_CREATE_TOOLS
        + SKILL_CONFIG_TOOLS
        + SKILL_KIT_CREATE_TOOLS
    )
)

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
    [t.name for t in (SELF_AGENT_TOOLS + RUNTIME_ADMIN_TOOLS)] + [claude_code.name]
)

# Optional tools that exist for development/regression validation rather than
# production use. Admins can still discover and bind them when deliberately
# testing dynamic tool loading; regular users should not see or enable them.
DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES = frozenset([hello_test.name])


def filter_developer_only_tools(
    tool_names,
    user_role: str,
) -> tuple[set, set]:
    """Filter developer-only diagnostic tool names out for non-admin users."""
    names = set(tool_names)
    if user_role == "admin":
        return names, set()
    blocked = names & DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES
    return names - blocked, blocked


def filter_discoverable_optional_tool_names(
    tool_names,
    user_role: str,
) -> set:
    """Return optional tool names that should be shown in discovery surfaces."""
    allowed, _ = filter_developer_only_tools(tool_names, user_role)
    return allowed


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
    # Memory tools (unified profile + thread-notepad CRUD)
    memory_add,
    memory_edit,
    memory_read,
    personality_set,
    rag_search,
    # TODO tools
    nym_todo,
    nym_todo_delete,
    nym_todo_list,
    # Unified notification tool
    notify,
]

__all__ = [
    "bash_execute",
    "file_read",
    "file_write",
    "file_edit",
    "FILE_EDIT_TOOLS",
    "image_generate",
    "IMAGE_GENERATION_TOOLS",
    "web_search",
    "consult",
    "CONSULT_TOOLS",
    "claude_code",
    "memory_add",
    "memory_edit",
    "memory_read",
    "memory_clear_all",
    "personality_set",
    "rag_search",
    "rag_settings",
    "MEMORY_TOOLS",
    "nym_todo",
    "nym_todo_delete",
    "nym_todo_list",
    "TODO_TOOLS",
    "reload_all",
    "self_modify_rollback",
    "RUNTIME_ADMIN_TOOLS",
    "notify",
    "NOTIFY_TOOLS",
    "trigger_config",
    "trigger_info",
    "TRIGGER_TOOLS",
    "AUTH_TOOLS",
    "EMAIL_TOOLS",
    "OUTLOOK_TOOLS",
    "BROWSER_TOOLS",
    "CALENDAR_TOOLS",
    "SELF_AGENT_TOOLS",
    "OPTIONAL_TOOLS",
    "ADMIN_ONLY_OPTIONAL_TOOL_NAMES",
    "DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES",
    "CAPABILITY_EXPANSION_TOOL_NAMES",
    "filter_admin_only_tools",
    "filter_developer_only_tools",
    "filter_discoverable_optional_tool_names",
    "ALL_TOOLS",
    "hello_test",
    "sticky_note",
    "STICKY_NOTE_TOOLS",
    "GOOGLE_DOCS_TOOLS",
    "GOOGLE_SHEETS_TOOLS",
    "gmail_auth_start",
    "gmail_auth_complete",
    "gmail_auth_clear",
    "gmail_list_accounts",
    "GMAIL_AUTH_TOOLS",
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
    "tool_enable",
    "TOOL_SEARCH_TOOLS",
    "http_request",
    "api_discover",
    "HTTP_API_TOOLS",
    "tool_create",
    "TOOL_CREATE_TOOLS",
    "_PRV_TOOLS_B",
    "skill_config",
    "skill_kit_create",
    "SKILL_CONFIG_TOOLS",
    "SKILL_KIT_CREATE_TOOLS",
    "skill_manage",
    "list_installed_skills",
    "search_skills",
    "install_skill",
    "SEARCH_SKILLS_TOOLS",
    "mcp_manage",
    "search_mcp",
    "install_mcp_server",
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
