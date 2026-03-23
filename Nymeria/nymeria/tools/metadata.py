"""Tool metadata and categorization for per-user tool customization.

This module defines tool categories, security levels, and metadata registry
to support per-user tool enabling/disabling and sensitive tool opt-in.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set


class ToolCategory(str, Enum):
    """Categories for grouping related tools."""

    CORE = "core"           # bash_execute, file_read, file_write, web_search, claude_code
    PROFILE = "profile"     # profile_save, profile_forget, profile_list, personality_set, rag_search
    NOTEPAD = "notepad"     # notepad_write, notepad_read, notepad_clear
    SELF_MODIFY = "self_modify"  # self_modify, self_modify_rollback
    TODO = "todo"           # todo, todo_delete, todo_list
    SUBAGENT = "subagent"   # reload_all, self_modify_rollback (optional)
    TRIGGER = "trigger"     # trigger_create, trigger_list, trigger_update, trigger_delete
    EMAIL = "email"         # Outlook auth + email tools (optional)
    BROWSER = "browser"     # Playwright browser automation tools (optional)
    CALENDAR = "calendar"   # Google Calendar auth + API tools (optional)
    GOOGLE_DOCS = "google_docs"  # Google Docs tools (optional)
    CUSTOM = "custom"       # User-created custom tools (HTTP, MCP, etc.)
    MCP_SERVER = "mcp_server"  # Tools auto-discovered from MCP servers


class SecurityLevel(str, Enum):
    """Security level determines default enabled state and opt-in requirements."""

    SAFE = "safe"           # Enabled by default, always available
    MODERATE = "moderate"   # Enabled by default, can be disabled by user
    SENSITIVE = "sensitive" # Disabled by default, requires explicit opt-in


@dataclass
class ToolMetadata:
    """Metadata for a single tool."""

    name: str
    category: ToolCategory
    security_level: SecurityLevel
    description: str
    default_enabled: bool = True
    config_schema: Optional[Dict] = None  # JSON schema for tool-specific config

    def __post_init__(self):
        # Sensitive tools are disabled by default
        if self.security_level == SecurityLevel.SENSITIVE:
            self.default_enabled = False


# Tool metadata registry
# Maps tool names to their metadata
TOOL_METADATA: Dict[str, ToolMetadata] = {
    # Core tools - essential functionality
    "bash_execute": ToolMetadata(
        name="bash_execute",
        category=ToolCategory.CORE,
        security_level=SecurityLevel.MODERATE,
        description="Execute shell commands",
        config_schema={
            "type": "object",
            "properties": {
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Command timeout in seconds",
                    "default": 120,
                    "minimum": 1,
                    "maximum": 600,
                }
            }
        }
    ),
    "file_read": ToolMetadata(
        name="file_read",
        category=ToolCategory.CORE,
        security_level=SecurityLevel.SAFE,
        description="Read file contents",
    ),
    "file_write": ToolMetadata(
        name="file_write",
        category=ToolCategory.CORE,
        security_level=SecurityLevel.MODERATE,
        description="Write content to files",
    ),
    # file_list: Removed — redundant with bash_execute
    "web_search": ToolMetadata(
        name="web_search",
        category=ToolCategory.CORE,
        security_level=SecurityLevel.SAFE,
        description="Search the web",
    ),
    "consult": ToolMetadata(
        name="consult",
        category=ToolCategory.CORE,
        security_level=SecurityLevel.SAFE,
        description="Ask Gemini for a second opinion (uses OpenRouter credits)",
    ),
    "claude_code": ToolMetadata(
        name="claude_code",
        category=ToolCategory.CORE,
        security_level=SecurityLevel.MODERATE,
        description="Invoke Claude Code for complex coding tasks",
    ),
    "notify": ToolMetadata(
        name="notify",
        category=ToolCategory.CORE,
        security_level=SecurityLevel.MODERATE,
        description="Send notifications via Telegram, Discord, or Slack",
    ),

    # Profile tools - user data management
    "profile_save": ToolMetadata(
        name="profile_save",
        category=ToolCategory.PROFILE,
        security_level=SecurityLevel.SAFE,
        description="Save information about the user",
    ),
    "profile_forget": ToolMetadata(
        name="profile_forget",
        category=ToolCategory.PROFILE,
        security_level=SecurityLevel.SAFE,
        description="Remove saved information",
    ),
    "profile_list": ToolMetadata(
        name="profile_list",
        category=ToolCategory.PROFILE,
        security_level=SecurityLevel.SAFE,
        description="List all saved memories and personality preferences",
    ),
    "personality_set": ToolMetadata(
        name="personality_set",
        category=ToolCategory.PROFILE,
        security_level=SecurityLevel.SAFE,
        description="Set personality preferences",
    ),
    "rag_search": ToolMetadata(
        name="rag_search",
        category=ToolCategory.PROFILE,
        security_level=SecurityLevel.SAFE,
        description="Search conversation history",
    ),

    # Notepad tools - per-thread persistent notes
    "notepad_write": ToolMetadata(
        name="notepad_write",
        category=ToolCategory.NOTEPAD,
        security_level=SecurityLevel.SAFE,
        description="Write to thread's persistent notepad",
    ),
    "notepad_read": ToolMetadata(
        name="notepad_read",
        category=ToolCategory.NOTEPAD,
        security_level=SecurityLevel.SAFE,
        description="Read thread's notepad content",
    ),
    "notepad_clear": ToolMetadata(
        name="notepad_clear",
        category=ToolCategory.NOTEPAD,
        security_level=SecurityLevel.SAFE,
        description="Clear thread's notepad",
    ),

    # Self-modification tools — SENSITIVE (can write arbitrary Python code)
    "self_modify_instructions": ToolMetadata(
        name="self_modify_instructions",
        category=ToolCategory.SELF_MODIFY,
        security_level=SecurityLevel.SENSITIVE,
        description="Get the self-modification workflow guide and code templates",
    ),
    "self_file_read": ToolMetadata(
        name="self_file_read",
        category=ToolCategory.SELF_MODIFY,
        security_level=SecurityLevel.SENSITIVE,
        description="Read a file from the Nymeria codebase",
    ),
    "self_file_write": ToolMetadata(
        name="self_file_write",
        category=ToolCategory.SELF_MODIFY,
        security_level=SecurityLevel.SENSITIVE,
        description="Write content to a file in the tools or agents directory",
    ),
    "self_file_list": ToolMetadata(
        name="self_file_list",
        category=ToolCategory.SELF_MODIFY,
        security_level=SecurityLevel.SENSITIVE,
        description="List files in a directory",
    ),
    "self_file_delete": ToolMetadata(
        name="self_file_delete",
        category=ToolCategory.SELF_MODIFY,
        security_level=SecurityLevel.SENSITIVE,
        description="Delete a file from the tools or agents directory",
    ),
    "self_test_import": ToolMetadata(
        name="self_test_import",
        category=ToolCategory.SELF_MODIFY,
        security_level=SecurityLevel.SENSITIVE,
        description="Test that all tools can be imported successfully",
    ),
    "self_reload": ToolMetadata(
        name="self_reload",
        category=ToolCategory.SELF_MODIFY,
        security_level=SecurityLevel.SENSITIVE,
        description="Reload all tools after making code changes",
    ),
    "self_invoke_tool": ToolMetadata(
        name="self_invoke_tool",
        category=ToolCategory.SELF_MODIFY,
        security_level=SecurityLevel.SENSITIVE,
        description="Test a tool by invoking it with given arguments",
    ),
    "self_modify_rollback": ToolMetadata(
        name="self_modify_rollback",
        category=ToolCategory.SELF_MODIFY,
        security_level=SecurityLevel.SENSITIVE,
        description="Rollback code modifications to previous backup",
    ),

    # TODO tools - task management
    "todo": ToolMetadata(
        name="todo",
        category=ToolCategory.TODO,
        security_level=SecurityLevel.SAFE,
        description="Create or update a TODO item",
    ),
    "todo_delete": ToolMetadata(
        name="todo_delete",
        category=ToolCategory.TODO,
        security_level=SecurityLevel.SAFE,
        description="Delete a TODO item",
    ),
    "todo_list": ToolMetadata(
        name="todo_list",
        category=ToolCategory.TODO,
        security_level=SecurityLevel.SAFE,
        description="List TODO items",
    ),

    "reload_all": ToolMetadata(
        name="reload_all",
        category=ToolCategory.SELF_MODIFY,
        security_level=SecurityLevel.SENSITIVE,
        description="Reload all tools and trigger sources after code changes",
    ),

    # Trigger tools - event-driven automation (optional, not in ALL_TOOLS by default)
    "trigger_create": ToolMetadata(
        name="trigger_create",
        category=ToolCategory.TRIGGER,
        security_level=SecurityLevel.MODERATE,
        description="Create an event-driven trigger",
    ),
    "trigger_list": ToolMetadata(
        name="trigger_list",
        category=ToolCategory.TRIGGER,
        security_level=SecurityLevel.SAFE,
        description="List event triggers",
    ),
    "trigger_update": ToolMetadata(
        name="trigger_update",
        category=ToolCategory.TRIGGER,
        security_level=SecurityLevel.MODERATE,
        description="Update a trigger",
    ),
    "trigger_delete": ToolMetadata(
        name="trigger_delete",
        category=ToolCategory.TRIGGER,
        security_level=SecurityLevel.MODERATE,
        description="Delete a trigger",
    ),

    # Test tool
    "hello_test": ToolMetadata(
        name="hello_test",
        category=ToolCategory.CORE,
        security_level=SecurityLevel.SAFE,
        description="Test tool for verifying agent functionality",
    ),

    # Outlook authentication tools (optional)
    "outlook_auth_start": ToolMetadata(
        name="outlook_auth_start",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.MODERATE,
        description="Start Microsoft account authentication using device code flow",
        default_enabled=False,
    ),
    "outlook_auth_complete": ToolMetadata(
        name="outlook_auth_complete",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.MODERATE,
        description="Complete Microsoft authentication after user has signed in",
        default_enabled=False,
    ),
    "outlook_list_authenticated_accounts": ToolMetadata(
        name="outlook_list_authenticated_accounts",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.SAFE,
        description="List all authenticated Microsoft accounts",
        default_enabled=False,
    ),

    # Outlook email tools (optional)
    "outlook_list_emails": ToolMetadata(
        name="outlook_list_emails",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.SAFE,
        description="List recent emails from Outlook",
        default_enabled=False,
    ),
    "outlook_get_email": ToolMetadata(
        name="outlook_get_email",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.SAFE,
        description="Get full details of a specific email",
        default_enabled=False,
    ),
    "outlook_search_emails": ToolMetadata(
        name="outlook_search_emails",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.SAFE,
        description="Search emails by keywords",
        default_enabled=False,
    ),
    "outlook_send_email": ToolMetadata(
        name="outlook_send_email",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.MODERATE,
        description="Send a new email",
        default_enabled=False,
    ),
    "outlook_reply_email": ToolMetadata(
        name="outlook_reply_email",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.MODERATE,
        description="Reply to an email",
        default_enabled=False,
    ),
    "outlook_create_draft": ToolMetadata(
        name="outlook_create_draft",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.MODERATE,
        description="Create an email draft",
        default_enabled=False,
    ),
    "outlook_delete_email": ToolMetadata(
        name="outlook_delete_email",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.MODERATE,
        description="Delete an email",
        default_enabled=False,
    ),
    "outlook_mark_email": ToolMetadata(
        name="outlook_mark_email",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.SAFE,
        description="Mark email as read or unread",
        default_enabled=False,
    ),
    "outlook_move_email": ToolMetadata(
        name="outlook_move_email",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.MODERATE,
        description="Move email to a different folder",
        default_enabled=False,
    ),
    "outlook_forward_email": ToolMetadata(
        name="outlook_forward_email",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.MODERATE,
        description="Forward an email to another recipient",
        default_enabled=False,
    ),

    # Browser automation tools (optional)
    "browser_navigate": ToolMetadata(
        name="browser_navigate",
        category=ToolCategory.BROWSER,
        security_level=SecurityLevel.MODERATE,
        description="Navigate browser to a URL",
        default_enabled=False,
    ),
    "browser_click": ToolMetadata(
        name="browser_click",
        category=ToolCategory.BROWSER,
        security_level=SecurityLevel.MODERATE,
        description="Click an element on the page",
        default_enabled=False,
    ),
    "browser_type": ToolMetadata(
        name="browser_type",
        category=ToolCategory.BROWSER,
        security_level=SecurityLevel.MODERATE,
        description="Type text into an input field",
        default_enabled=False,
    ),
    "browser_get_content": ToolMetadata(
        name="browser_get_content",
        category=ToolCategory.BROWSER,
        security_level=SecurityLevel.SAFE,
        description="Get text content of current page",
        default_enabled=False,
    ),
    "browser_screenshot": ToolMetadata(
        name="browser_screenshot",
        category=ToolCategory.BROWSER,
        security_level=SecurityLevel.SAFE,
        description="Take screenshot of current page",
        default_enabled=False,
    ),
    "browser_scroll": ToolMetadata(
        name="browser_scroll",
        category=ToolCategory.BROWSER,
        security_level=SecurityLevel.SAFE,
        description="Scroll page up or down",
        default_enabled=False,
    ),
    "browser_close": ToolMetadata(
        name="browser_close",
        category=ToolCategory.BROWSER,
        security_level=SecurityLevel.SAFE,
        description="Close the browser",
        default_enabled=False,
    ),
    "browser_press_key": ToolMetadata(
        name="browser_press_key",
        category=ToolCategory.BROWSER,
        security_level=SecurityLevel.MODERATE,
        description="Press a keyboard key in the browser",
        default_enabled=False,
    ),
    "browser_status": ToolMetadata(
        name="browser_status",
        category=ToolCategory.BROWSER,
        security_level=SecurityLevel.SAFE,
        description="Check browser status and Playwright availability",
        default_enabled=False,
    ),

    # Google Calendar authentication tools (optional)
    "calendar_auth_start": ToolMetadata(
        name="calendar_auth_start",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.MODERATE,
        description="Start Google Calendar OAuth authentication",
        default_enabled=False,
    ),
    "calendar_auth_complete": ToolMetadata(
        name="calendar_auth_complete",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.MODERATE,
        description="Complete Google Calendar authentication",
        default_enabled=False,
    ),
    "calendar_list_authenticated_accounts": ToolMetadata(
        name="calendar_list_authenticated_accounts",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.SAFE,
        description="List authenticated Google accounts",
        default_enabled=False,
    ),

    # Google Calendar API tools (optional)
    "calendar_list_calendars": ToolMetadata(
        name="calendar_list_calendars",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.SAFE,
        description="List all available Google calendars",
        default_enabled=False,
    ),
    "calendar_list_events": ToolMetadata(
        name="calendar_list_events",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.SAFE,
        description="List events from a calendar",
        default_enabled=False,
    ),
    "calendar_get_event": ToolMetadata(
        name="calendar_get_event",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.SAFE,
        description="Get detailed information about a calendar event",
        default_enabled=False,
    ),
    "calendar_search_events": ToolMetadata(
        name="calendar_search_events",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.SAFE,
        description="Search events by text query",
        default_enabled=False,
    ),
    "calendar_create_event": ToolMetadata(
        name="calendar_create_event",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.MODERATE,
        description="Create a new calendar event",
        default_enabled=False,
    ),
    "calendar_update_event": ToolMetadata(
        name="calendar_update_event",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.MODERATE,
        description="Update an existing calendar event",
        default_enabled=False,
    ),
    "calendar_delete_event": ToolMetadata(
        name="calendar_delete_event",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.MODERATE,
        description="Delete a calendar event",
        default_enabled=False,
    ),
    "calendar_respond_to_event": ToolMetadata(
        name="calendar_respond_to_event",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.MODERATE,
        description="Respond to a calendar event invitation",
        default_enabled=False,
    ),
    "calendar_get_freebusy": ToolMetadata(
        name="calendar_get_freebusy",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.SAFE,
        description="Get free/busy information for calendars",
        default_enabled=False,
    ),
    "calendar_get_current_time": ToolMetadata(
        name="calendar_get_current_time",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.SAFE,
        description="Get current time in ISO 8601 format",
        default_enabled=False,
    ),
    "calendar_list_colors": ToolMetadata(
        name="calendar_list_colors",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.SAFE,
        description="List available calendar and event colors",
        default_enabled=False,
    ),

    # Google Docs authentication tools (optional)
    "google_docs_auth_start": ToolMetadata(
        name="google_docs_auth_start",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Start Google Docs OAuth authentication",
        default_enabled=False,
    ),
    "google_docs_auth_complete": ToolMetadata(
        name="google_docs_auth_complete",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Complete Google Docs authentication",
        default_enabled=False,
    ),
    "google_docs_list_accounts": ToolMetadata(
        name="google_docs_list_accounts",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.SAFE,
        description="List authenticated Google accounts for Docs",
        default_enabled=False,
    ),

    # Google Docs API tools (optional)
    "google_docs_read": ToolMetadata(
        name="google_docs_read",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.SAFE,
        description="Read a Google Docs document",
        default_enabled=False,
    ),
    "google_docs_append_text": ToolMetadata(
        name="google_docs_append_text",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Append text to end of a document",
        default_enabled=False,
    ),
    "google_docs_insert_text": ToolMetadata(
        name="google_docs_insert_text",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Insert text at a specific position",
        default_enabled=False,
    ),
    "google_docs_delete_range": ToolMetadata(
        name="google_docs_delete_range",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Delete content by index range",
        default_enabled=False,
    ),
    "google_docs_apply_text_style": ToolMetadata(
        name="google_docs_apply_text_style",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Apply styling (bold, italic, color, links) to text",
        default_enabled=False,
    ),
    "google_docs_insert_table": ToolMetadata(
        name="google_docs_insert_table",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Insert a table into a document",
        default_enabled=False,
    ),
    "google_docs_insert_page_break": ToolMetadata(
        name="google_docs_insert_page_break",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Insert a page break",
        default_enabled=False,
    ),
    "google_docs_replace_text": ToolMetadata(
        name="google_docs_replace_text",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Find and replace text in a document",
        default_enabled=False,
    ),

    # Google Docs new tools (Phase 2-4)
    "google_docs_create": ToolMetadata(
        name="google_docs_create",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Create a new Google Docs document",
        default_enabled=False,
    ),
    "google_docs_delete": ToolMetadata(
        name="google_docs_delete",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Delete (trash) a Google Docs document",
        default_enabled=False,
    ),
    "google_docs_list": ToolMetadata(
        name="google_docs_list",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.SAFE,
        description="List or search Google Docs documents in Drive",
        default_enabled=False,
    ),
    "google_docs_write": ToolMetadata(
        name="google_docs_write",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Write markdown-formatted content to a Google Doc",
        default_enabled=False,
    ),
    "google_docs_update_paragraph_style": ToolMetadata(
        name="google_docs_update_paragraph_style",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Update paragraph styling (headings, alignment)",
        default_enabled=False,
    ),

}


def get_tool_metadata(tool_name: str) -> Optional[ToolMetadata]:
    """Get metadata for a tool by name."""
    return TOOL_METADATA.get(tool_name)


def get_tools_by_category(category: ToolCategory) -> List[str]:
    """Get all tool names in a category."""
    return [
        name for name, meta in TOOL_METADATA.items()
        if meta.category == category
    ]


def get_sensitive_tools() -> List[str]:
    """Get all tools that require explicit opt-in."""
    return [
        name for name, meta in TOOL_METADATA.items()
        if meta.security_level == SecurityLevel.SENSITIVE
    ]


def get_default_enabled_tools() -> Set[str]:
    """Get all tools that are enabled by default."""
    return {
        name for name, meta in TOOL_METADATA.items()
        if meta.default_enabled
    }


def get_all_categories() -> List[str]:
    """Get all category names."""
    return [c.value for c in ToolCategory]


def get_category_tools_summary() -> Dict[str, List[str]]:
    """Get a summary of tools by category."""
    summary = {}
    for category in ToolCategory:
        tools = get_tools_by_category(category)
        if tools:
            summary[category.value] = tools
    return summary


# MCP server tool metadata registry
MCP_SERVER_TOOL_METADATA: Dict[str, ToolMetadata] = {}


def register_mcp_server_tool_metadata(
    tool_name: str,
    description: str,
) -> ToolMetadata:
    """Register metadata for an MCP server tool (default_enabled=False)."""
    metadata = ToolMetadata(
        name=tool_name,
        category=ToolCategory.MCP_SERVER,
        security_level=SecurityLevel.MODERATE,
        description=description,
        default_enabled=False,
    )
    MCP_SERVER_TOOL_METADATA[tool_name] = metadata
    return metadata


def unregister_mcp_server_tool_metadata(tool_name: str) -> bool:
    """Unregister metadata for an MCP server tool."""
    if tool_name in MCP_SERVER_TOOL_METADATA:
        del MCP_SERVER_TOOL_METADATA[tool_name]
        return True
    return False


def clear_mcp_server_tool_metadata() -> None:
    """Clear all MCP server tool metadata (for reload)."""
    MCP_SERVER_TOOL_METADATA.clear()


# Custom tool metadata registry (separate from built-in)
CUSTOM_TOOL_METADATA: Dict[str, ToolMetadata] = {}


def register_custom_tool_metadata(
    tool_id: str,
    description: str,
    security_level: SecurityLevel = SecurityLevel.MODERATE,
) -> ToolMetadata:
    """
    Register metadata for a custom tool.

    Args:
        tool_id: Unique identifier for the custom tool
        description: Tool description
        security_level: Security level for the tool

    Returns:
        The created ToolMetadata
    """
    metadata = ToolMetadata(
        name=tool_id,
        category=ToolCategory.CUSTOM,
        security_level=security_level,
        description=description,
    )
    CUSTOM_TOOL_METADATA[tool_id] = metadata
    return metadata


def unregister_custom_tool_metadata(tool_id: str) -> bool:
    """
    Unregister metadata for a custom tool.

    Args:
        tool_id: The tool ID to unregister

    Returns:
        True if tool was unregistered, False if not found
    """
    if tool_id in CUSTOM_TOOL_METADATA:
        del CUSTOM_TOOL_METADATA[tool_id]
        return True
    return False


def get_all_tool_metadata(tool_name: str) -> Optional[ToolMetadata]:
    """
    Get metadata for any tool (built-in, MCP server, or custom).

    Args:
        tool_name: Name/ID of the tool

    Returns:
        ToolMetadata if found, None otherwise
    """
    # Check built-in first
    if tool_name in TOOL_METADATA:
        return TOOL_METADATA[tool_name]
    # Then MCP server tools
    if tool_name in MCP_SERVER_TOOL_METADATA:
        return MCP_SERVER_TOOL_METADATA[tool_name]
    # Then custom
    return CUSTOM_TOOL_METADATA.get(tool_name)
