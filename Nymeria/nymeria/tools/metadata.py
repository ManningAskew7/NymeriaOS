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
    MEMORY = "memory"       # memory_save, memory_forget, memory_list, personality_set, rag_search
    SELF_MODIFY = "self_modify"  # self_modify, self_modify_rollback
    TODO = "todo"           # todo, todo_delete, todo_list
    SUBAGENT = "subagent"   # reload_all, self_modify_rollback (optional)
    TRIGGER = "trigger"     # trigger_create, trigger_list, trigger_update, trigger_delete
    CUSTOM = "custom"       # User-created custom tools (HTTP, MCP, etc.)


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

    # Memory tools - user data management
    "memory_save": ToolMetadata(
        name="memory_save",
        category=ToolCategory.MEMORY,
        security_level=SecurityLevel.SAFE,
        description="Save information about the user",
    ),
    "memory_forget": ToolMetadata(
        name="memory_forget",
        category=ToolCategory.MEMORY,
        security_level=SecurityLevel.SAFE,
        description="Remove saved information",
    ),
    "memory_list": ToolMetadata(
        name="memory_list",
        category=ToolCategory.MEMORY,
        security_level=SecurityLevel.SAFE,
        description="List all saved memories and personality preferences",
    ),
    # memory_clear_all: Removed — dangerous, cheap models could hallucinate and wipe all memories
    "personality_set": ToolMetadata(
        name="personality_set",
        category=ToolCategory.MEMORY,
        security_level=SecurityLevel.SAFE,
        description="Set personality preferences",
    ),
    "rag_search": ToolMetadata(
        name="rag_search",
        category=ToolCategory.MEMORY,
        security_level=SecurityLevel.SAFE,
        description="Search conversation history",
    ),
    # rag_settings removed - configure via UI settings

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
    Get metadata for any tool (built-in or custom).

    Args:
        tool_name: Name/ID of the tool

    Returns:
        ToolMetadata if found, None otherwise
    """
    # Check built-in first
    if tool_name in TOOL_METADATA:
        return TOOL_METADATA[tool_name]
    # Then check custom
    return CUSTOM_TOOL_METADATA.get(tool_name)
