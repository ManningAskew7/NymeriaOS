"""Unified tool API schemas and conversion helpers."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel

from ...tools.definitions.custom_tool_schema import CustomToolDefinition

if TYPE_CHECKING:
    from ...core.user_profile import ToolPreferences


class UnifiedToolResponse(BaseModel):
    """Response model for a unified tool (built-in, MCP server, or custom)."""

    id: str
    name: str
    description: str
    default_description: str
    custom_description: str | None = None
    category: str
    security_level: str
    enabled: bool
    enabled_reason: str
    tool_type: Literal["builtin", "custom", "mcp_server"]
    implementation_type: str | None = None
    config_schema: dict[str, Any] | None = None
    user_config: dict[str, Any] = {}
    parameters: dict[str, Any] | None = None
    http_config: dict[str, Any] | None = None
    mcp_config: dict[str, Any] | None = None
    tags: list[str] = []
    editable: bool = False
    configurable: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class UnifiedToolListResponse(BaseModel):
    """Response model for unified tools list."""

    tools: list[UnifiedToolResponse]
    total: int
    builtin_count: int
    custom_count: int


class UnifiedToolEnableRequest(BaseModel):
    """Request model for enabling/disabling a tool."""

    enabled: bool


class UnifiedToolDescriptionRequest(BaseModel):
    """Request model for setting a custom tool description."""

    description: str | None = None


class UnifiedToolConfigRequest(BaseModel):
    """Request model for setting tool configuration."""

    config: dict[str, Any]


def builtin_tool_to_unified(
    tool_info: dict[str, Any],
    tool_preferences: "ToolPreferences | None" = None,
) -> UnifiedToolResponse:
    """Convert built-in tool info to unified response format."""
    default_desc = tool_info.get("description", "")
    custom_desc = None
    if tool_preferences:
        custom_desc = tool_preferences.get_custom_description(tool_info["name"])

    effective_desc = custom_desc if custom_desc else default_desc
    config_schema = tool_info.get("config_schema")
    configurable = config_schema is not None and len(config_schema) > 0

    return UnifiedToolResponse(
        id=tool_info["name"],
        name=tool_info["name"],
        description=effective_desc,
        default_description=default_desc,
        custom_description=custom_desc,
        category=tool_info.get("category", "core"),
        security_level=tool_info.get("security_level", "safe"),
        enabled=tool_info.get("enabled", True),
        enabled_reason=tool_info.get("enabled_reason", "default"),
        tool_type="builtin",
        implementation_type=None,
        config_schema=config_schema,
        user_config=tool_info.get("user_config", {}),
        parameters=None,
        http_config=None,
        mcp_config=None,
        tags=[],
        editable=False,
        configurable=configurable,
        created_at=None,
        updated_at=None,
    )


def custom_tool_definition_to_unified(
    defn: CustomToolDefinition,
    enabled: bool,
    enabled_reason: str,
    user_config: dict[str, Any],
    tool_preferences: "ToolPreferences | None" = None,
) -> UnifiedToolResponse:
    """Convert a custom tool definition to unified response format."""
    impl_type = defn.implementation_type
    http_config = None
    mcp_config = None

    if impl_type == "http" and defn.http_config is not None:
        http_config = {
            "method": defn.http_config.method,
            "url": defn.http_config.url,
            "headers": defn.http_config.headers,
            "body_template": defn.http_config.body_template,
            "query_params": defn.http_config.query_params,
            "timeout_seconds": defn.http_config.timeout_seconds,
            "response_path": defn.http_config.response_path,
            "response_format": defn.http_config.response_format,
        }
    elif impl_type == "mcp" and defn.mcp_config is not None:
        mcp_config = {
            "transport": defn.mcp_config.transport,
            "server_command": defn.mcp_config.server_command,
            "server_args": defn.mcp_config.server_args,
            "url": defn.mcp_config.url,
            "headers": defn.mcp_config.headers,
            "tool_name": defn.mcp_config.tool_name,
            "env_vars": defn.mcp_config.env_vars,
            "working_directory": defn.mcp_config.working_directory,
            "idle_timeout_seconds": defn.mcp_config.idle_timeout_seconds,
            "startup_timeout_seconds": defn.mcp_config.startup_timeout_seconds,
        }

    params = None
    if defn.parameters:
        params = {name: param.model_dump() for name, param in defn.parameters.items()}

    default_desc = defn.description
    custom_desc = None
    if tool_preferences:
        custom_desc = tool_preferences.get_custom_description(defn.id)
    effective_desc = custom_desc if custom_desc else default_desc

    return UnifiedToolResponse(
        id=defn.id,
        name=defn.name,
        description=effective_desc,
        default_description=default_desc,
        custom_description=custom_desc,
        category="custom",
        security_level="moderate",
        enabled=enabled,
        enabled_reason=enabled_reason,
        tool_type="custom",
        implementation_type=impl_type,
        config_schema=None,
        user_config=user_config,
        parameters=params,
        http_config=http_config,
        mcp_config=mcp_config,
        tags=defn.tags or [],
        editable=True,
        configurable=False,
        created_at=defn.created_at,
        updated_at=defn.updated_at,
    )
