"""Compatibility imports for tool definition schemas.

New code should import from ``custom_tool_schema`` or ``mcp_schema`` directly.
This module remains for existing persisted imports and external callers.
"""

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
