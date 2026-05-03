"""Import-surface tests for tool definition schemas."""

from nymeria.tools import definitions
from nymeria.tools.definitions import custom_tool_schema, mcp_schema, schema


def test_custom_tool_schema_imports_are_canonical():
    assert definitions.CustomToolDefinition is custom_tool_schema.CustomToolDefinition
    assert definitions.HTTPToolConfig is custom_tool_schema.HTTPToolConfig
    assert definitions.ToolParameter is custom_tool_schema.ToolParameter


def test_mcp_schema_imports_are_canonical():
    assert definitions.MCPDiscoveredTool is mcp_schema.MCPDiscoveredTool
    assert definitions.MCPServerDefinition is mcp_schema.MCPServerDefinition
    assert definitions.MCPToolConfig is mcp_schema.MCPToolConfig


def test_legacy_schema_module_keeps_compatibility_imports():
    assert schema.CustomToolDefinition is custom_tool_schema.CustomToolDefinition
    assert schema.HTTPToolConfig is custom_tool_schema.HTTPToolConfig
    assert schema.ToolParameter is custom_tool_schema.ToolParameter
    assert schema.MCPDiscoveredTool is mcp_schema.MCPDiscoveredTool
    assert schema.MCPServerDefinition is mcp_schema.MCPServerDefinition
    assert schema.MCPToolConfig is mcp_schema.MCPToolConfig
