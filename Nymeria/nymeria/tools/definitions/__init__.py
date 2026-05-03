"""Tool definition schemas for custom tools and MCP servers."""

from .custom_tool_schema import CustomToolDefinition, HTTPToolConfig, ToolParameter
from .mcp_schema import (
    MCPDiscoveredTool,
    MCPInstallStatus,
    MCPServerDefinition,
    MCPToolConfig,
    MCPTransport,
)

__all__ = [
    "CustomToolDefinition",
    "HTTPToolConfig",
    "MCPDiscoveredTool",
    "MCPInstallStatus",
    "MCPServerDefinition",
    "MCPToolConfig",
    "MCPTransport",
    "ToolParameter",
]
