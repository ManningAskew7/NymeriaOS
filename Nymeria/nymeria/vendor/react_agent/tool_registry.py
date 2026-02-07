"""
Tool Registry for Dynamic Tool Management

Allows frameworks to register, discover, and manage tools dynamically.
Supports multiple registration patterns for flexibility.

Supports per-user tool filtering based on:
- Tool-level enable/disable overrides
- Category-level disabling
- Sensitive tool opt-in requirements
"""

from typing import Callable, List, Optional, Dict, Any, TYPE_CHECKING
from langchain_core.tools import BaseTool, tool as tool_decorator

if TYPE_CHECKING:
    from ...core.user_profile import UserProfileManager


class ToolRegistry:
    """
    Central registry for managing agent tools.

    Usage:
        # Create registry
        registry = ToolRegistry()

        # Register tools
        registry.register(my_tool)
        registry.register_function(my_func, name="my_tool", description="Does something")

        # Bulk register
        registry.register_all([tool1, tool2, tool3])

        # Get tools for the agent
        tools = registry.get_enabled_tools()

        # Enable/disable tools
        registry.disable("dangerous_tool")
        registry.enable("dangerous_tool")
    """

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}
        self._disabled: set = set()

    def register(self, tool: BaseTool) -> "ToolRegistry":
        """
        Register a LangChain tool.

        Args:
            tool: A LangChain BaseTool instance (created with @tool decorator)

        Returns:
            Self for chaining
        """
        self._tools[tool.name] = tool
        return self

    def register_function(
        self,
        func: Callable,
        name: Optional[str] = None,
        description: Optional[str] = None,
        **kwargs
    ) -> "ToolRegistry":
        """
        Register a plain function as a tool.

        Args:
            func: The function to register
            name: Tool name (defaults to function name)
            description: Tool description (defaults to docstring)
            **kwargs: Additional arguments passed to the tool decorator

        Returns:
            Self for chaining
        """
        # Apply the tool decorator to convert function to BaseTool
        if description:
            decorated = tool_decorator(description=description, **kwargs)(func)
        else:
            decorated = tool_decorator(**kwargs)(func)

        if name:
            decorated.name = name

        return self.register(decorated)

    def register_all(self, tools: List[BaseTool]) -> "ToolRegistry":
        """Register multiple tools at once."""
        for t in tools:
            self.register(t)
        return self

    def unregister(self, name: str) -> "ToolRegistry":
        """Remove a tool from the registry."""
        self._tools.pop(name, None)
        self._disabled.discard(name)
        return self

    def disable(self, name: str) -> "ToolRegistry":
        """Disable a tool (keeps it registered but won't be used)."""
        if name in self._tools:
            self._disabled.add(name)
        return self

    def enable(self, name: str) -> "ToolRegistry":
        """Re-enable a disabled tool."""
        self._disabled.discard(name)
        return self

    def get_tool(self, name: str) -> Optional[BaseTool]:
        """Get a specific tool by name."""
        return self._tools.get(name)

    def get_all_tools(self) -> List[BaseTool]:
        """Get all registered tools (including disabled)."""
        return list(self._tools.values())

    def get_enabled_tools(self) -> List[BaseTool]:
        """Get only enabled tools (what the agent will use)."""
        return [t for name, t in self._tools.items() if name not in self._disabled]

    def get_tools_for_user(
        self,
        user_id: str,
        profile_manager: "UserProfileManager",
    ) -> List[BaseTool]:
        """
        Get tools filtered by user preferences.

        This method filters the enabled tools based on:
        1. Tool-specific overrides in enabled_overrides
        2. Category disabled_categories
        3. Default from TOOL_METADATA

        Args:
            user_id: The user identifier
            profile_manager: UserProfileManager instance to fetch user profile

        Returns:
            List of tools available for this user
        """
        from ...tools.metadata import get_tool_metadata

        profile = profile_manager.get_profile(user_id)
        tool_prefs = profile.tool_preferences

        filtered_tools = []
        for name, tool in self._tools.items():
            # Skip globally disabled tools
            if name in self._disabled:
                continue

            # Get tool metadata
            metadata = get_tool_metadata(name)

            if metadata:
                # Check if tool is enabled based on user preferences
                is_enabled = tool_prefs.is_tool_enabled(
                    tool_name=name,
                    category=metadata.category.value,
                    default_enabled=metadata.default_enabled,
                )
                if is_enabled:
                    filtered_tools.append(tool)
            else:
                # Unknown tool (custom tool) - include by default unless explicitly disabled
                is_enabled = tool_prefs.is_tool_enabled(
                    tool_name=name,
                    category=None,
                    default_enabled=True,
                )
                if is_enabled:
                    filtered_tools.append(tool)

        return filtered_tools

    def get_tools_with_user_status(
        self,
        user_id: str,
        profile_manager: "UserProfileManager",
    ) -> List[Dict[str, Any]]:
        """
        Get all tools with their enabled status for a specific user.

        Returns detailed information about each tool including:
        - Whether it's enabled for this user
        - Category and security level
        - Configuration options

        Args:
            user_id: The user identifier
            profile_manager: UserProfileManager instance

        Returns:
            List of dicts with tool info and user-specific status
        """
        from ...tools.metadata import get_tool_metadata

        profile = profile_manager.get_profile(user_id)
        tool_prefs = profile.tool_preferences

        result = []
        for name, tool in self._tools.items():
            metadata = get_tool_metadata(name)

            entry = {
                "name": name,
                "description": tool.description,
                "globally_disabled": name in self._disabled,
            }

            if metadata:
                entry.update({
                    "category": metadata.category.value,
                    "security_level": metadata.security_level.value,
                    "default_enabled": metadata.default_enabled,
                    "config_schema": metadata.config_schema,
                })

                # Determine user-specific enabled state
                if name in self._disabled:
                    entry["enabled"] = False
                    entry["enabled_reason"] = "globally_disabled"
                elif name in tool_prefs.enabled_overrides:
                    entry["enabled"] = tool_prefs.enabled_overrides[name]
                    entry["enabled_reason"] = "user_override"
                elif metadata.category.value in tool_prefs.disabled_categories:
                    entry["enabled"] = False
                    entry["enabled_reason"] = "category_disabled"
                else:
                    entry["enabled"] = metadata.default_enabled
                    entry["enabled_reason"] = "default"
            else:
                # Custom/unknown tool
                entry.update({
                    "category": "custom",
                    "security_level": "moderate",
                    "default_enabled": True,
                    "config_schema": None,
                })

                if name in self._disabled:
                    entry["enabled"] = False
                    entry["enabled_reason"] = "globally_disabled"
                elif name in tool_prefs.enabled_overrides:
                    entry["enabled"] = tool_prefs.enabled_overrides[name]
                    entry["enabled_reason"] = "user_override"
                else:
                    entry["enabled"] = True
                    entry["enabled_reason"] = "default"

            # Add user-specific config
            entry["user_config"] = tool_prefs.get_tool_config(name)

            result.append(entry)

        return result

    def list_tools(self) -> List[Dict[str, Any]]:
        """List all tools with their status."""
        return [
            {
                "name": name,
                "description": tool.description,
                "enabled": name not in self._disabled,
            }
            for name, tool in self._tools.items()
        ]

    def clear(self) -> "ToolRegistry":
        """Remove all tools from the registry."""
        self._tools.clear()
        self._disabled.clear()
        return self

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools


# Global default registry
default_registry = ToolRegistry()


def get_default_registry() -> ToolRegistry:
    """Get the default global tool registry."""
    return default_registry


def register_tool(tool: BaseTool) -> BaseTool:
    """
    Decorator/function to register a tool with the default registry.

    Usage:
        @tool
        @register_tool
        def my_tool(query: str) -> str:
            '''Tool description.'''
            return result

        # Or after creation:
        register_tool(my_existing_tool)
    """
    default_registry.register(tool)
    return tool
