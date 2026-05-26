"""Custom tool API schemas and conversion helpers."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from ...tools.definitions.custom_tool_schema import (
    CustomToolDefinition,
    HTTPToolConfig,
    PythonToolConfig,
    ToolParameter,
)
from ...tools.definitions.mcp_schema import MCPToolConfig


ParameterType = Literal["string", "integer", "number", "boolean", "array", "object"]
HTTPMethod = Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
ResponseFormat = Literal["json", "text", "auto"]
ImplementationType = Literal["http", "mcp", "python"]


class ToolParameterModel(BaseModel):
    """API model for tool parameters."""

    type: ParameterType = "string"
    description: str = ""
    required: bool = False
    default: str | None = None
    enum: list[str] | None = None


class HTTPToolConfigModel(BaseModel):
    """API model for HTTP tool configuration."""

    method: HTTPMethod = Field(default="GET", description="HTTP method")  # type: ignore[assignment]
    url: str = Field(
        ...,
        description=(
            "URL template with ${param} placeholders. Use "
            "${credential:cred_id.field} for secret values."
        ),
    )
    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "HTTP headers. Sensitive headers must use "
            "${credential:cred_id.field}; raw secrets are rejected."
        ),
    )
    body_template: str | None = Field(
        default=None,
        description=(
            "JSON/text body template with ${param} placeholders. Use "
            "${credential:cred_id.field} for secret values."
        ),
    )
    query_params: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Query parameter templates. Use ${credential:cred_id.field} for "
            "secret values."
        ),
    )
    timeout_seconds: int = Field(default=30, description="Request timeout in seconds")
    response_path: str | None = Field(
        default=None,
        description="Optional JSON path to extract from a JSON response",
    )
    response_format: ResponseFormat = Field(
        default="auto",
        description="Response parsing mode",
    )


class MCPToolConfigModel(BaseModel):
    """API model for MCP tool configuration."""

    server_command: str
    server_args: list[str] = []
    tool_name: str
    env_vars: dict[str, str] = {}
    working_directory: str | None = None
    idle_timeout_seconds: int = 300
    startup_timeout_seconds: int = 30


class PythonToolConfigModel(BaseModel):
    """API model for subprocess-backed Python custom tools."""

    source_code: str
    entrypoint: str = "run"
    runtime: Literal["subprocess"] = "subprocess"


class CustomToolResponse(BaseModel):
    """Response model for a custom tool."""

    id: str
    name: str
    description: str
    parameters: dict[str, ToolParameterModel]
    implementation_type: ImplementationType
    http_config: HTTPToolConfigModel | None = None
    mcp_config: MCPToolConfigModel | None = None
    python_config: PythonToolConfigModel | None = None
    enabled: bool
    tags: list[str] = []
    created_at: datetime
    updated_at: datetime


class CustomToolCreateRequest(BaseModel):
    """Request model for creating a custom tool."""

    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1, max_length=1000)
    parameters: dict[str, ToolParameterModel] = {}
    implementation_type: ImplementationType
    http_config: HTTPToolConfigModel | None = None
    mcp_config: MCPToolConfigModel | None = None
    python_config: PythonToolConfigModel | None = None
    enabled: bool = True
    tags: list[str] = []


class CustomToolUpdateRequest(BaseModel):
    """Request model for updating a custom tool."""

    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    parameters: dict[str, ToolParameterModel] | None = None
    http_config: HTTPToolConfigModel | None = None
    mcp_config: MCPToolConfigModel | None = None
    python_config: PythonToolConfigModel | None = None
    enabled: bool | None = None
    tags: list[str] | None = None


class CustomToolTestRequest(BaseModel):
    """Request model for testing a custom tool."""

    params: dict[str, Any] = {}


class CustomToolListResponse(BaseModel):
    """Response model for custom tools list."""

    tools: list[CustomToolResponse]
    total: int


def tool_parameters_to_core(
    parameters: dict[str, ToolParameterModel],
) -> dict[str, ToolParameter]:
    """Convert API parameter models to core custom-tool parameters."""
    return {
        name: ToolParameter(
            type=param.type,
            description=param.description,
            required=param.required,
            default=param.default,
            enum=param.enum,
        )
        for name, param in parameters.items()
    }


def http_config_to_core(config: HTTPToolConfigModel) -> HTTPToolConfig:
    """Convert an API HTTP config model to the core config model."""
    return HTTPToolConfig(
        method=config.method,
        url=config.url,
        headers=config.headers,
        body_template=config.body_template,
        query_params=config.query_params,
        timeout_seconds=config.timeout_seconds,
        response_path=config.response_path,
        response_format=config.response_format,
    )


def mcp_config_to_core(config: MCPToolConfigModel) -> MCPToolConfig:
    """Convert an API MCP config model to the core config model."""
    return MCPToolConfig(
        server_command=config.server_command,
        server_args=config.server_args,
        tool_name=config.tool_name,
        env_vars=config.env_vars,
        working_directory=config.working_directory,
        idle_timeout_seconds=config.idle_timeout_seconds,
        startup_timeout_seconds=config.startup_timeout_seconds,
    )


def python_config_to_core(config: PythonToolConfigModel) -> PythonToolConfig:
    """Convert an API Python config model to the core config model."""
    return PythonToolConfig(
        source_code=config.source_code,
        entrypoint=config.entrypoint,
        runtime=config.runtime,
    )


def custom_tool_definition_to_response(defn: CustomToolDefinition) -> CustomToolResponse:
    """Convert a CustomToolDefinition to API response format."""
    return CustomToolResponse(
        id=defn.id,
        name=defn.name,
        description=defn.description,
        parameters={
            name: ToolParameterModel(
                type=param.type,
                description=param.description,
                required=param.required,
                default=str(param.default) if param.default is not None else None,
                enum=param.enum,
            )
            for name, param in defn.parameters.items()
        },
        implementation_type=defn.implementation_type,
        http_config=HTTPToolConfigModel(
            method=defn.http_config.method,
            url=defn.http_config.url,
            headers=defn.http_config.headers,
            body_template=defn.http_config.body_template,
            query_params=defn.http_config.query_params,
            timeout_seconds=defn.http_config.timeout_seconds,
            response_path=defn.http_config.response_path,
            response_format=defn.http_config.response_format,
        ) if defn.http_config else None,
        mcp_config=MCPToolConfigModel(
            server_command=defn.mcp_config.server_command,
            server_args=defn.mcp_config.server_args,
            tool_name=defn.mcp_config.tool_name,
            env_vars=defn.mcp_config.env_vars,
            working_directory=defn.mcp_config.working_directory,
            idle_timeout_seconds=defn.mcp_config.idle_timeout_seconds,
            startup_timeout_seconds=defn.mcp_config.startup_timeout_seconds,
        ) if defn.mcp_config else None,
        python_config=PythonToolConfigModel(
            source_code=defn.python_config.source_code,
            entrypoint=defn.python_config.entrypoint,
            runtime=defn.python_config.runtime,
        ) if defn.python_config else None,
        enabled=defn.enabled,
        tags=defn.tags,
        created_at=defn.created_at,
        updated_at=defn.updated_at,
    )
