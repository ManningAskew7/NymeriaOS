"""Pydantic schemas for user-defined custom tools."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from ...core.time_utils import utc_now
from .mcp_schema import MCPToolConfig


class ToolParameter(BaseModel):
    """Definition of a single tool parameter."""

    type: Literal["string", "integer", "number", "boolean", "array", "object"] = Field(  # type: ignore[assignment]
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

    method: Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"] = Field(  # type: ignore[assignment]
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


class PythonToolConfig(BaseModel):
    """Configuration for subprocess-backed Python custom tools."""

    source_code: str = Field(
        ...,
        min_length=1,
        description="Python source code containing the tool entrypoint",
    )
    entrypoint: str = Field(
        default="run",
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="Callable function name to invoke",
    )
    runtime: Literal["subprocess"] = Field(
        default="subprocess",
        description="Execution runtime. Python custom tools run out-of-process.",
    )


class CustomToolDefinition(BaseModel):
    """Complete definition of a custom tool.

    Custom tools can be HTTP-based, MCP-based, or subprocess-backed Python.

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
    implementation_type: Literal["http", "mcp", "python"] = Field(
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
    python_config: Optional[PythonToolConfig] = Field(
        default=None,
        description="Python tool configuration (required if type is 'python')",
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
        default_factory=utc_now,
        description="Creation timestamp",
    )
    updated_at: datetime = Field(
        default_factory=utc_now,
        description="Last update timestamp",
    )

    @field_validator("http_config", "mcp_config")
    @classmethod
    def validate_config_matches_type(cls, v, info):
        """Config will be validated in model_post_init."""
        return v

    def model_post_init(self, __context: Any) -> None:
        """Validate that the config matches the implementation type."""
        if self.implementation_type == "http" and self.http_config is None:
            raise ValueError("http_config is required when implementation_type is 'http'")
        if self.implementation_type == "mcp" and self.mcp_config is None:
            raise ValueError("mcp_config is required when implementation_type is 'mcp'")
        if self.implementation_type == "python" and self.python_config is None:
            raise ValueError("python_config is required when implementation_type is 'python'")

    def to_json_schema(self) -> Dict[str, Any]:
        """Convert parameters to JSON Schema format for LangChain."""
        properties = {}
        required = []

        for name, param in self.parameters.items():
            prop: Dict[str, Any] = {
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


__all__ = [
    "CustomToolDefinition",
    "HTTPToolConfig",
    "MCPToolConfig",
    "PythonToolConfig",
    "ToolParameter",
]
