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
    python_config: dict[str, Any] | None = None
    workflow_config: dict[str, Any] | None = None
    tags: list[str] = []
    editable: bool = False
    configurable: bool = False
    live: bool = True
    globally_disabled: bool = False
    # Two-level integration grouping (integration tools only; None otherwise).
    group: str | None = None
    group_label: str | None = None
    service: str | None = None
    service_label: str | None = None
    # Credential axis (provider-mapped tools only; None = no credential
    # required): connected / pending / needs_setup / optional.
    auth_status: str | None = None
    auth_provider: str | None = None
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

    from ...tools.metadata import integration_grouping_fields

    grouping = integration_grouping_fields(
        tool_info["name"], tool_info.get("category", "general")
    )

    return UnifiedToolResponse(
        id=tool_info["name"],
        name=tool_info["name"],
        description=effective_desc,
        default_description=default_desc,
        custom_description=custom_desc,
        category=tool_info.get("category", "general"),
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
        python_config=None,
        tags=[],
        editable=False,
        configurable=configurable,
        group=grouping["group"],
        group_label=grouping["group_label"],
        service=grouping["service"],
        service_label=grouping["service_label"],
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
    python_config = None
    workflow_config = None

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
    elif impl_type == "python" and defn.python_config is not None:
        python_config = {
            "source_code": defn.python_config.source_code,
            "entrypoint": defn.python_config.entrypoint,
            "runtime": defn.python_config.runtime,
        }
    elif impl_type == "workflow" and defn.workflow_config is not None:
        from ...core.workflows.authoring import approval_state

        workflow_config = {
            "source_code": defn.workflow_config.source_code,
            "entrypoint": defn.workflow_config.entrypoint,
            "continuations": list(defn.workflow_config.continuations),
            "wall_clock_seconds": defn.workflow_config.wall_clock_seconds,
            "max_calls": defn.workflow_config.max_calls,
            "max_ai_calls": defn.workflow_config.max_ai_calls,
            "revision_hash": defn.workflow_config.revision_hash,
            "approval": approval_state(defn.workflow_config, defn.parameters),
            "created_by": defn.workflow_config.created_by,
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
        python_config=python_config,
        workflow_config=workflow_config,
        tags=defn.tags or [],
        editable=True,
        configurable=False,
        created_at=defn.created_at,
        updated_at=defn.updated_at,
    )
