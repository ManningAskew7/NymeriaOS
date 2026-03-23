"""Pydantic schemas for custom tool definitions.

Supports two implementation types:
- HTTP: REST API calls with URL/body templating
- MCP: Model Context Protocol servers with JSON-RPC over stdio

Based on MCP SDK best practices 2025-2026:
- Use Pydantic for validation and configuration
- Support structured output schemas
- Environment variable interpolation for secrets
"""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class MCPDiscoveredTool(BaseModel):
    """A tool discovered from an MCP server via tools/list."""

    name: str
    description: str = ""
    input_schema: Dict[str, Any] = {}


class MCPServerDefinition(BaseModel):
    """Definition of an MCP server and its discovered tools."""

    id: str
    name: str
    description: str = ""
    server_command: str
    server_args: List[str] = []
    env_vars: Dict[str, str] = {}
    working_directory: Optional[str] = None
    idle_timeout_seconds: int = 300
    startup_timeout_seconds: int = 30
    enabled: bool = True
    discovered_tools: List[MCPDiscoveredTool] = []
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ToolParameter(BaseModel):
    """Definition of a single tool parameter."""

    type: Literal["string", "integer", "number", "boolean", "array", "object"] = Field(
        default="string",
        description="JSON Schema type of the parameter",
    )
    description: str = Field(
        default="",
        description="Human-readable description of the parameter",
    )
    required: bool = Field(
        default=False,
        description="Whether this parameter is required",
    )
    default: Optional[Any] = Field(
        default=None,
        description="Default value if not provided",
    )
    enum: Optional[List[str]] = Field(
        default=None,
        description="Allowed values (for string type)",
    )


class HTTPToolConfig(BaseModel):
    """Configuration for HTTP-based custom tools.

    Supports URL and body templating with ${param} syntax.
    Secrets can be referenced via ${env:VAR_NAME} syntax.

    Example:
        url: "https://api.example.com/users/${user_id}"
        headers: {"Authorization": "Bearer ${env:API_KEY}"}
        body_template: '{"query": "${query}", "limit": ${limit}}'
    """

    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH"] = Field(
        default="GET",
        description="HTTP method",
    )
    url: str = Field(
        ...,
        description="URL template with ${param} placeholders",
    )
    headers: Dict[str, str] = Field(
        default_factory=dict,
        description="HTTP headers (supports ${env:VAR} for secrets)",
    )
    body_template: Optional[str] = Field(
        default=None,
        description="JSON body template with ${param} placeholders",
    )
    query_params: Dict[str, str] = Field(
        default_factory=dict,
        description="Query parameters template",
    )
    timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="Request timeout in seconds",
    )
    response_path: Optional[str] = Field(
        default=None,
        description="JSONPath to extract result (e.g., '$.data.items')",
    )
    response_format: Literal["json", "text", "auto"] = Field(
        default="auto",
        description="How to parse the response",
    )

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        """Validate URL format."""
        if not v.startswith(("http://", "https://", "${env:")):
            raise ValueError("URL must start with http://, https://, or ${env:}")
        return v


class MCPToolConfig(BaseModel):
    """Configuration for MCP (Model Context Protocol) server tools.

    MCP servers communicate via JSON-RPC over stdio. This config defines
    how to start the server and which tool to invoke.

    Best practices (MCP SDK 2025):
    - Never write to stdout in STDIO servers (use stderr for logging)
    - Use SDK version 1.2.0+ for stability
    - Set idle timeout for resource management

    Example:
        server_command: "npx"
        server_args: ["-y", "@anthropic/mcp-server-filesystem", "/allowed/path"]
        tool_name: "read_file"
    """

    server_command: str = Field(
        ...,
        description="Command to start the MCP server (e.g., 'npx', 'python', 'node')",
    )
    server_args: List[str] = Field(
        default_factory=list,
        description="Command line arguments for the server",
    )
    tool_name: str = Field(
        ...,
        description="Name of the tool exposed by the MCP server",
    )
    env_vars: Dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables for the server process (supports ${env:VAR})",
    )
    working_directory: Optional[str] = Field(
        default=None,
        description="Working directory for the server process",
    )
    idle_timeout_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Seconds of idle time before shutting down server",
    )
    startup_timeout_seconds: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Seconds to wait for server initialization",
    )


class CustomToolDefinition(BaseModel):
    """Complete definition of a custom tool.

    Custom tools can be either HTTP-based (REST API calls) or
    MCP-based (Model Context Protocol servers).

    The tool is converted to a LangChain @tool function at runtime,
    with parameters extracted from the definition.
    """

    id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$",
        description="Unique identifier (alphanumeric, underscore, hyphen)",
    )
    name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Human-readable name for the tool",
    )
    description: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Description shown to the LLM for tool selection",
    )
    parameters: Dict[str, ToolParameter] = Field(
        default_factory=dict,
        description="Tool parameters with their schemas",
    )
    implementation_type: Literal["http", "mcp"] = Field(
        ...,
        description="Type of tool implementation",
    )
    http_config: Optional[HTTPToolConfig] = Field(
        default=None,
        description="HTTP tool configuration (required if type is 'http')",
    )
    mcp_config: Optional[MCPToolConfig] = Field(
        default=None,
        description="MCP tool configuration (required if type is 'mcp')",
    )
    enabled: bool = Field(
        default=True,
        description="Whether the tool is enabled",
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Tags for categorization",
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Creation timestamp",
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        description="Last update timestamp",
    )

    @field_validator("http_config", "mcp_config")
    @classmethod
    def validate_config_matches_type(cls, v, info):
        """Config will be validated in model_validator."""
        return v

    def model_post_init(self, __context: Any) -> None:
        """Validate that the config matches the implementation type."""
        if self.implementation_type == "http" and self.http_config is None:
            raise ValueError("http_config is required when implementation_type is 'http'")
        if self.implementation_type == "mcp" and self.mcp_config is None:
            raise ValueError("mcp_config is required when implementation_type is 'mcp'")

    def to_json_schema(self) -> Dict[str, Any]:
        """Convert parameters to JSON Schema format for LangChain."""
        properties = {}
        required = []

        for name, param in self.parameters.items():
            prop = {
                "type": param.type,
                "description": param.description,
            }
            if param.default is not None:
                prop["default"] = param.default
            if param.enum:
                prop["enum"] = param.enum
            properties[name] = prop

            if param.required:
                required.append(name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }
