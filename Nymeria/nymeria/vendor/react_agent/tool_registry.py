"""
Tool Registry for Dynamic Tool Management

Allows frameworks to register, discover, and manage tools dynamically.
Supports multiple registration patterns for flexibility.
"""

from typing import Callable, List, Optional, Dict, Any
from langchain_core.tools import BaseTool, tool as tool_decorator


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
