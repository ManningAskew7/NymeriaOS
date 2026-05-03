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
    PROFILE = "profile"     # memory_add, memory_edit, memory_read, memory_clear_all, personality_set, rag_search, rag_settings
    NOTEPAD = "notepad"     # legacy alias; per-thread notepad now reached via memory_* with scope="thread"
    SELF_MODIFY = "self_modify"  # self_modify, self_modify_rollback
    TODO = "todo"           # nym_todo, nym_todo_delete, nym_todo_list
    AUTONOMY = "autonomy"   # activity feed and watchdog dispatch helpers
    SUBAGENT = "subagent"   # reload_all, self_modify_rollback (optional)
    TRIGGER = "trigger"     # trigger_config, trigger_info
    EMAIL = "email"         # Outlook auth + email tools (optional)
    BROWSER = "browser"     # Playwright browser automation tools (optional)
    CALENDAR = "calendar"   # Google Calendar auth + API tools (optional)
    GOOGLE_DOCS = "google_docs"  # Google Docs tools (optional)
    TWITCH = "twitch"       # Twitch chat, moderation, and channel tools (optional)
    _PRV_A = "_prv_a"           # Acme Hardware supplier/vendor/product lookup tools (optional)
    SKILLS = "skills"       # Agent Skills discovery/install (list_installed_skills, search_skills, install_skill)
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
    "file_edit": ToolMetadata(
        name="file_edit",
        category=ToolCategory.CORE,
        security_level=SecurityLevel.MODERATE,
        description="Precisely edit existing text files with exact, all-or-nothing operations",
        default_enabled=False,
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

    # Memory tools - unified profile + thread-notepad CRUD
    "memory_add": ToolMetadata(
        name="memory_add",
        category=ToolCategory.PROFILE,
        security_level=SecurityLevel.SAFE,
        description="Save a memory. scope='global' (user profile, requires key) or scope='thread' (per-thread notepad). Empty content deletes.",
    ),
    "memory_edit": ToolMetadata(
        name="memory_edit",
        category=ToolCategory.PROFILE,
        security_level=SecurityLevel.SAFE,
        description="Find/replace within a memory's content. scope='global' edits a single keyed memory; scope='thread' edits the notepad. Empty replace deletes the matched text.",
    ),
    "memory_read": ToolMetadata(
        name="memory_read",
        category=ToolCategory.PROFILE,
        security_level=SecurityLevel.SAFE,
        description="Read memory. Get a single keyed entry, list all, or substring-filter via query.",
    ),
    "memory_clear_all": ToolMetadata(
        name="memory_clear_all",
        category=ToolCategory.PROFILE,
        security_level=SecurityLevel.SAFE,
        description="Wipe all global memories and personality preferences for this user. Irreversible. Does not touch thread notepads.",
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
        description="Semantic search across past conversations and memories",
    ),
    "rag_settings": ToolMetadata(
        name="rag_settings",
        category=ToolCategory.PROFILE,
        security_level=SecurityLevel.SAFE,
        description="Configure RAG (semantic search) settings",
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

    # TODO tools - autonomous task scheduling
    "nym_todo": ToolMetadata(
        name="nym_todo",
        category=ToolCategory.TODO,
        security_level=SecurityLevel.SAFE,
        description="Create or update a TODO. Scheduled TODOs auto-wake the agent",
    ),
    "nym_todo_delete": ToolMetadata(
        name="nym_todo_delete",
        category=ToolCategory.TODO,
        security_level=SecurityLevel.SAFE,
        description="Delete a TODO item",
    ),
    "nym_todo_list": ToolMetadata(
        name="nym_todo_list",
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

    # Thread spawning — create new sidebar threads with scoped config (optional)
    "spawn_thread": ToolMetadata(
        name="spawn_thread",
        category=ToolCategory.SUBAGENT,
        security_level=SecurityLevel.MODERATE,
        description=(
            "Create a new conversation thread with custom instructions, tool "
            "selection, and optional LLM overrides. Optionally dispatches an "
            "initial message and blocks until the child responds. Spawned "
            "threads appear in a 'Spawned by Nymeria' folder in the sidebar."
        ),
    ),

    # Trigger tools - event-driven automation (optional, not in ALL_TOOLS by default)
    "trigger_config": ToolMetadata(
        name="trigger_config",
        category=ToolCategory.TRIGGER,
        security_level=SecurityLevel.MODERATE,
        description="Create, update, enable/disable, or delete event triggers",
    ),
    "trigger_info": ToolMetadata(
        name="trigger_info",
        category=ToolCategory.TRIGGER,
        security_level=SecurityLevel.SAFE,
        description="List triggers, inspect/test/history for one trigger, or show source schemas",
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
    "outlook_auth_clear": ToolMetadata(
        name="outlook_auth_clear",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.MODERATE,
        description="Clear saved Microsoft authentication or pending device-code flow",
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
    "calendar_auth_clear": ToolMetadata(
        name="calendar_auth_clear",
        category=ToolCategory.CALENDAR,
        security_level=SecurityLevel.MODERATE,
        description="Clear saved Google Calendar authentication",
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
    "google_docs_auth_clear": ToolMetadata(
        name="google_docs_auth_clear",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Clear saved Google Docs/Drive/Sheets authentication",
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

    # Sticky note
    "sticky_note": ToolMetadata(
        name="sticky_note",
        category=ToolCategory.CORE,
        security_level=SecurityLevel.SAFE,
        description="Manage the desktop sticky note checklist",
        default_enabled=False,
    ),

    # Slash command
    "slash_command": ToolMetadata(
        name="slash_command",
        category=ToolCategory.CORE,
        security_level=SecurityLevel.MODERATE,
        description="Invoke a Nymeria slash command on your own thread",
        default_enabled=False,
    ),
    "http_request": ToolMetadata(
        name="http_request",
        category=ToolCategory.CORE,
        security_level=SecurityLevel.MODERATE,
        description="Make a one-off HTTP request to a documented API endpoint",
        default_enabled=False,
    ),
    "api_discover": ToolMetadata(
        name="api_discover",
        category=ToolCategory.CORE,
        security_level=SecurityLevel.MODERATE,
        description="Discover OpenAPI/Swagger metadata for an API base URL",
        default_enabled=False,
    ),
    "tool_create": ToolMetadata(
        name="tool_create",
        category=ToolCategory.CUSTOM,
        security_level=SecurityLevel.MODERATE,
        description="Draft, test, and publish reusable HTTP tools into the global custom-tool registry",
        default_enabled=False,
    ),
    "skill_config": ToolMetadata(
        name="skill_config",
        category=ToolCategory.CUSTOM,
        security_level=SecurityLevel.MODERATE,
        description="Draft, validate, publish, list, and delete user-scoped Nymeria Skills and Skill Kits",
        default_enabled=False,
    ),

    # Outlook additional tools
    "outlook_get_attachments": ToolMetadata(
        name="outlook_get_attachments",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.SAFE,
        description="Download and extract text content from email attachments",
        default_enabled=False,
    ),
    "outlook_draft_reply": ToolMetadata(
        name="outlook_draft_reply",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.MODERATE,
        description="Create a draft reply to an email without sending",
        default_enabled=False,
    ),
    "outlook_edit_draft": ToolMetadata(
        name="outlook_edit_draft",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.MODERATE,
        description="Edit an existing email draft before sending",
        default_enabled=False,
    ),
    "outlook_set_category": ToolMetadata(
        name="outlook_set_category",
        category=ToolCategory.EMAIL,
        security_level=SecurityLevel.SAFE,
        description="Add or remove a category tag on an email",
        default_enabled=False,
    ),

    # Google Docs additional tools
    "google_docs_find_index": ToolMetadata(
        name="google_docs_find_index",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.SAFE,
        description="Find the document indices of a text string",
        default_enabled=False,
    ),
    "google_docs_write_table": ToolMetadata(
        name="google_docs_write_table",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Create a populated table in a Google Doc",
        default_enabled=False,
    ),
    "google_docs_table_update_cell": ToolMetadata(
        name="google_docs_table_update_cell",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Update a specific cell in an existing table",
        default_enabled=False,
    ),
    "google_docs_table_append_row": ToolMetadata(
        name="google_docs_table_append_row",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Append a new row to an existing table",
        default_enabled=False,
    ),

    # Google Sheets tools
    "google_sheets_search": ToolMetadata(
        name="google_sheets_search",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.SAFE,
        description="Search a Google Sheet for rows matching a query",
        default_enabled=False,
    ),
    "google_sheets_append": ToolMetadata(
        name="google_sheets_append",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Append one or more rows to a Google Sheet",
        default_enabled=False,
    ),
    "google_sheets_update": ToolMetadata(
        name="google_sheets_update",
        category=ToolCategory.GOOGLE_DOCS,
        security_level=SecurityLevel.MODERATE,
        description="Update cells in an existing Google Sheet row",
        default_enabled=False,
    ),

    # _PRV_A tools
    "_prv_a_supplier_lookup": ToolMetadata(
        name="_prv_a_supplier_lookup",
        category=ToolCategory._PRV_A,
        security_level=SecurityLevel.SAFE,
        description="Find overseas suppliers that stock a specific brand or product type",
        default_enabled=False,
    ),
    "_prv_a_vendor_info": ToolMetadata(
        name="_prv_a_vendor_info",
        category=ToolCategory._PRV_A,
        security_level=SecurityLevel.SAFE,
        description="Look up vendor information including quality ratings and notes",
        default_enabled=False,
    ),
    "_prv_a_product_search": ToolMetadata(
        name="_prv_a_product_search",
        category=ToolCategory._PRV_A,
        security_level=SecurityLevel.SAFE,
        description="Search the Acme Hardware product catalog for parts",
        default_enabled=False,
    ),
    "_prv_a_acme_lifecycle": ToolMetadata(
        name="_prv_a_acme_lifecycle",
        category=ToolCategory._PRV_A,
        security_level=SecurityLevel.SAFE,
        description="Check lifecycle status of Acme/Allen-Bradley part numbers",
        default_enabled=False,
    ),
    "_prv_a_acme_pricelist": ToolMetadata(
        name="_prv_a_acme_pricelist",
        category=ToolCategory._PRV_A,
        security_level=SecurityLevel.SAFE,
        description="Look up Acme Electric part details and list pricing",
        default_enabled=False,
    ),

    # Twitch tools
    "twitch_read_chat": ToolMetadata(
        name="twitch_read_chat",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.SAFE,
        description="Read recent messages from the chat buffer",
        default_enabled=False,
    ),
    "twitch_send": ToolMetadata(
        name="twitch_send",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.MODERATE,
        description="Send a message to the Twitch channel chat",
        default_enabled=False,
    ),
    "twitch_announce": ToolMetadata(
        name="twitch_announce",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.MODERATE,
        description="Send a highlighted announcement to Twitch chat",
        default_enabled=False,
    ),
    "twitch_delete_message": ToolMetadata(
        name="twitch_delete_message",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.MODERATE,
        description="Delete a chat message or clear all chat",
        default_enabled=False,
    ),
    "twitch_timeout": ToolMetadata(
        name="twitch_timeout",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.MODERATE,
        description="Timeout a user in Twitch chat",
        default_enabled=False,
    ),
    "twitch_ban": ToolMetadata(
        name="twitch_ban",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.SENSITIVE,
        description="Permanently ban a user from Twitch chat",
        default_enabled=False,
    ),
    "twitch_unban": ToolMetadata(
        name="twitch_unban",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.MODERATE,
        description="Unban or untimeout a user in Twitch chat",
        default_enabled=False,
    ),
    "twitch_warn": ToolMetadata(
        name="twitch_warn",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.MODERATE,
        description="Issue an official warning to a chat user",
        default_enabled=False,
    ),
    "twitch_automod_review": ToolMetadata(
        name="twitch_automod_review",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.MODERATE,
        description="Approve or deny a message held by AutoMod",
        default_enabled=False,
    ),
    "twitch_shoutout": ToolMetadata(
        name="twitch_shoutout",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.MODERATE,
        description="Give a shoutout to another channel",
        default_enabled=False,
    ),
    "twitch_get_stream": ToolMetadata(
        name="twitch_get_stream",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.SAFE,
        description="Get current live stream status, viewers, game, and uptime",
        default_enabled=False,
    ),
    "twitch_get_channel": ToolMetadata(
        name="twitch_get_channel",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.SAFE,
        description="Get channel info: title, game, tags, language",
        default_enabled=False,
    ),
    "twitch_get_chatters": ToolMetadata(
        name="twitch_get_chatters",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.SAFE,
        description="Get list of users currently in chat",
        default_enabled=False,
    ),
    "twitch_get_banned": ToolMetadata(
        name="twitch_get_banned",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.SAFE,
        description="Get list of banned users with reasons",
        default_enabled=False,
    ),
    "twitch_get_schedule": ToolMetadata(
        name="twitch_get_schedule",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.SAFE,
        description="Get the channel's upcoming stream schedule",
        default_enabled=False,
    ),
    "twitch_clip": ToolMetadata(
        name="twitch_clip",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.MODERATE,
        description="Create a clip of the last ~30 seconds of the live stream",
        default_enabled=False,
    ),
    "twitch_create_poll": ToolMetadata(
        name="twitch_create_poll",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.MODERATE,
        description="Create a poll in the channel",
        default_enabled=False,
    ),
    "twitch_end_poll": ToolMetadata(
        name="twitch_end_poll",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.MODERATE,
        description="End an active poll",
        default_enabled=False,
    ),
    "twitch_create_prediction": ToolMetadata(
        name="twitch_create_prediction",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.MODERATE,
        description="Create a channel points prediction",
        default_enabled=False,
    ),
    "twitch_resolve_prediction": ToolMetadata(
        name="twitch_resolve_prediction",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.MODERATE,
        description="Resolve, cancel, or lock a prediction",
        default_enabled=False,
    ),
    "twitch_set_channel_info": ToolMetadata(
        name="twitch_set_channel_info",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.MODERATE,
        description="Update channel title, game/category, and tags",
        default_enabled=False,
    ),
    "twitch_get_subs": ToolMetadata(
        name="twitch_get_subs",
        category=ToolCategory.TWITCH,
        security_level=SecurityLevel.SAFE,
        description="Check subscriber count or if a user is subscribed",
        default_enabled=False,
    ),

    # Tool search (core, always available)
    "tool_search": ToolMetadata(
        name="tool_search",
        category=ToolCategory.CORE,
        security_level=SecurityLevel.SAFE,
        description=(
            "Search, enable, and disable optional tools for the current thread. "
            "Enabling auto-continues the turn with the new tools bound (no need "
            "to wait for the next user message). Enablements have a TTL "
            "(default 2h); pick shortest needed or use 'permanent'."
        ),
    ),

    # Agent Skills — discovery, install, inspection
    "list_installed_skills": ToolMetadata(
        name="list_installed_skills",
        category=ToolCategory.SKILLS,
        security_level=SecurityLevel.SAFE,
        description="List all Agent Skills installed on disk (across user/global/bundled scopes)",
    ),
    "search_skills": ToolMetadata(
        name="search_skills",
        category=ToolCategory.SKILLS,
        security_level=SecurityLevel.SAFE,
        description="Search installed skills or the Anthropic marketplace (github.com/anthropics/skills)",
    ),
    "install_skill": ToolMetadata(
        name="install_skill",
        category=ToolCategory.SKILLS,
        security_level=SecurityLevel.MODERATE,
        description="Install a skill from the marketplace into user or global scope",
    ),
    "mcp_search": ToolMetadata(
        name="mcp_search",
        category=ToolCategory.MCP_SERVER,
        security_level=SecurityLevel.SAFE,
        description="Search public MCP server registries (official + Smithery) for installable servers",
    ),
    "mcp_install": ToolMetadata(
        name="mcp_install",
        category=ToolCategory.MCP_SERVER,
        security_level=SecurityLevel.MODERATE,
        description="Install an MCP server from a paste (Claude Desktop JSON, stdio command, HTTP URL, or registry id)",
    ),
    "activity_feed": ToolMetadata(
        name="activity_feed",
        category=ToolCategory.AUTONOMY,
        security_level=SecurityLevel.SAFE,
        description="Read recent autonomous activity and event history",
        default_enabled=False,
    ),
    "watchdog_dispatch": ToolMetadata(
        name="watchdog_dispatch",
        category=ToolCategory.AUTONOMY,
        security_level=SecurityLevel.MODERATE,
        description="Dispatch watchdog nudges for stale TODOs",
        default_enabled=False,
    ),
    "watchdog_read_notepad": ToolMetadata(
        name="watchdog_read_notepad",
        category=ToolCategory.AUTONOMY,
        security_level=SecurityLevel.SAFE,
        description="Read watchdog-specific notepad context",
        default_enabled=False,
    ),
    "watchdog_todo_overview": ToolMetadata(
        name="watchdog_todo_overview",
        category=ToolCategory.AUTONOMY,
        security_level=SecurityLevel.SAFE,
        description="Summarize TODO state for watchdog checks",
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


def clear_custom_tool_metadata() -> None:
    """Clear all custom tool metadata (for reload)."""
    CUSTOM_TOOL_METADATA.clear()


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
