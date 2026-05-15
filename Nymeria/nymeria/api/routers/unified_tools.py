"""Unified built-in, MCP server, and custom-tool routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...core.accounts import AuthenticatedUser
from ...core.custom_tools import get_custom_tool_loader, reload_custom_tools
from ...core.time_utils import utc_now
from ...tools.definitions.custom_tool_schema import CustomToolDefinition
from ..schemas.custom_tools import (
    CustomToolCreateRequest,
    CustomToolUpdateRequest,
    http_config_to_core,
    mcp_config_to_core,
    tool_parameters_to_core,
)
from ..schemas.unified_tools import (
    UnifiedToolConfigRequest,
    UnifiedToolDescriptionRequest,
    UnifiedToolEnableRequest,
    UnifiedToolListResponse,
    UnifiedToolResponse,
    builtin_tool_to_unified,
    custom_tool_definition_to_unified,
)


def create_unified_tools_router(
    verify_api_key: Callable[..., Any],
    require_admin_user: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    require_same_user_or_admin_fn: Callable[[AuthenticatedUser, str], None],
) -> APIRouter:
    """Create the unified Tools router with app dependencies injected."""
    router = APIRouter(tags=["Unified Tools"])

    @router.get("/users/{user_id}/tools/unified", response_model=UnifiedToolListResponse)
    async def list_unified_tools(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        List all tools (built-in and custom) in a unified format.

        Returns tools with consistent structure regardless of type, including
        enable status per user derived from default_thread_tools.
        """
        require_same_user_or_admin_fn(user, user_id)
        from ...tools import (
            ALL_TOOLS,
            OPTIONAL_TOOLS,
            filter_discoverable_optional_tool_names,
        )
        from ...tools.metadata import MCP_SERVER_TOOL_METADATA, get_tool_metadata

        agent = get_agent_fn()
        loader = get_custom_tool_loader()

        profile = agent.profile_manager.get_profile(user_id)
        tool_prefs = profile.tool_preferences

        dtt = tool_prefs.default_thread_tools
        if dtt is None:
            dtt_set = {tool.name for tool in ALL_TOOLS}
        else:
            dtt_set = set(dtt)

        unified_tools = []
        seen = set()
        visible_optional = filter_discoverable_optional_tool_names(
            OPTIONAL_TOOLS.keys(),
            user.role,
        )
        for tool in ALL_TOOLS:
            meta = get_tool_metadata(tool.name)
            enabled = tool.name in dtt_set
            tool_info = {
                "name": tool.name,
                "description": tool.description,
                "category": meta.category.value if meta else "core",
                "security_level": meta.security_level.value if meta else "safe",
                "enabled": enabled,
                "enabled_reason": "default_thread_tools",
                "config_schema": meta.config_schema if meta else None,
                "user_config": tool_prefs.get_tool_config(tool.name),
                "globally_disabled": False,
                "default_enabled": True,
            }
            unified_tools.append(builtin_tool_to_unified(tool_info, tool_prefs))
            seen.add(tool.name)

        for name, tool in OPTIONAL_TOOLS.items():
            if name in seen or name not in visible_optional:
                continue
            meta = get_tool_metadata(name)
            enabled = name in dtt_set
            tool_info = {
                "name": name,
                "description": tool.description,
                "category": meta.category.value if meta else "unknown",
                "security_level": meta.security_level.value if meta else "moderate",
                "enabled": enabled,
                "enabled_reason": "default_thread_tools",
                "config_schema": meta.config_schema if meta else None,
                "user_config": tool_prefs.get_tool_config(name),
                "globally_disabled": False,
                "default_enabled": False,
            }
            unified_tools.append(builtin_tool_to_unified(tool_info, tool_prefs))
            seen.add(name)

        # Custom definitions include raw HTTP/MCP config, so only admins see
        # them in the unified discovery surface.
        if user.role == "admin":
            custom_definitions = loader.get_all_definitions()
            for defn in custom_definitions:
                user_config = tool_prefs.get_tool_config(defn.id)
                unified_tools.append(
                    custom_tool_definition_to_unified(
                        defn,
                        True,
                        "default",
                        user_config,
                        tool_prefs,
                    )
                )

        for tool_name, meta in MCP_SERVER_TOOL_METADATA.items():
            if tool_name in seen:
                continue
            enabled = tool_name in dtt_set
            live = bool(getattr(meta, "live", True))
            unified_tools.append(
                UnifiedToolResponse(
                    id=tool_name,
                    name=tool_name,
                    description=meta.description,
                    default_description=meta.description,
                    custom_description=None,
                    category="mcp_server",
                    security_level="moderate",
                    enabled=enabled and live,
                    enabled_reason="default_thread_tools",
                    tool_type="mcp_server",
                    implementation_type="mcp",
                    config_schema=None,
                    user_config={},
                    parameters=None,
                    http_config=None,
                    mcp_config=None,
                    tags=[],
                    editable=False,
                    configurable=False,
                    live=live,
                    globally_disabled=not live,
                    created_at=None,
                    updated_at=None,
                )
            )
            seen.add(tool_name)

        type_order = {"builtin": 0, "mcp_server": 1, "custom": 2}
        unified_tools.sort(key=lambda tool: (type_order.get(tool.tool_type, 9), tool.name))

        builtin_count = sum(1 for tool in unified_tools if tool.tool_type == "builtin")
        custom_count = sum(1 for tool in unified_tools if tool.tool_type == "custom")

        return UnifiedToolListResponse(
            tools=unified_tools,
            total=len(unified_tools),
            builtin_count=builtin_count,
            custom_count=custom_count,
        )

    @router.put("/users/{user_id}/tools/unified/{tool_id}/enable")
    async def set_unified_tool_enabled(
        user_id: str,
        tool_id: str,
        request: UnifiedToolEnableRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Enable or disable a built-in or MCP server tool for a user.

        This mutates default_thread_tools. Custom tools are managed per-thread
        through enabled_tools.
        """
        require_same_user_or_admin_fn(user, user_id)
        from ...tools import (
            ADMIN_ONLY_OPTIONAL_TOOL_NAMES,
            ALL_TOOLS,
            DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES,
        )
        from ...tools.metadata import get_all_tool_metadata

        agent = get_agent_fn()
        tool_meta = get_all_tool_metadata(tool_id)
        if not tool_meta:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )
        if request.enabled and getattr(tool_meta, "category", None) and tool_meta.category.value == "mcp_server":
            if not getattr(tool_meta, "live", True):
                raise HTTPException(
                    status_code=409,
                    detail=f"MCP tool '{tool_id}' is known but not currently available",
                )

        if (
            request.enabled
            and tool_id in ADMIN_ONLY_OPTIONAL_TOOL_NAMES
            and user.role != "admin"
        ):
            raise HTTPException(
                status_code=403,
                detail=f"Tool '{tool_id}' is admin-only",
            )
        if (
            request.enabled
            and tool_id in DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES
            and user.role != "admin"
        ):
            raise HTTPException(
                status_code=403,
                detail=f"Tool '{tool_id}' is developer-only",
            )

        with agent.profile_manager.atomic_update(user_id) as profile:
            dtt = profile.tool_preferences.default_thread_tools
            if dtt is None:
                dtt = [tool.name for tool in ALL_TOOLS]

            if request.enabled:
                if tool_id not in dtt:
                    dtt.append(tool_id)
            else:
                if tool_id in dtt:
                    dtt.remove(tool_id)

            profile.tool_preferences.default_thread_tools = dtt
            profile.updated_at = utc_now()

        agent._rebuild_default_graphs()

        return {
            "status": "ok",
            "tool_id": tool_id,
            "enabled": request.enabled,
            "tool_type": (
                tool_meta.category.value
                if tool_meta.category.value == "mcp_server"
                else "builtin"
            ),
        }

    @router.put("/users/{user_id}/tools/unified/{tool_id}/description")
    async def set_unified_tool_description(
        user_id: str,
        tool_id: str,
        request: UnifiedToolDescriptionRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Set a custom description for a tool.

        Works for both built-in and custom tools. Pass null to clear the
        custom description and revert to default.
        """
        require_same_user_or_admin_fn(user, user_id)
        from ...tools.metadata import get_tool_metadata

        agent = get_agent_fn()
        loader = get_custom_tool_loader()

        builtin_meta = get_tool_metadata(tool_id)
        custom_defn = loader.get_definition(tool_id)
        if not builtin_meta and not custom_defn:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        with agent.profile_manager.atomic_update(user_id) as profile:
            if request.description is None:
                cleared = profile.tool_preferences.clear_custom_description(tool_id)
                return {
                    "status": "ok",
                    "tool_id": tool_id,
                    "action": "cleared" if cleared else "no_change",
                    "description": None,
                }

            profile.tool_preferences.set_custom_description(tool_id, request.description)
            return {
                "status": "ok",
                "tool_id": tool_id,
                "action": "set",
                "description": request.description,
            }

    @router.put("/users/{user_id}/tools/unified/{tool_id}/config")
    async def set_unified_tool_config(
        user_id: str,
        tool_id: str,
        request: UnifiedToolConfigRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Set configuration for a tool.

        Works for both built-in and custom tools. The config is merged with
        existing config; pass an empty dict to clear.
        """
        require_same_user_or_admin_fn(user, user_id)
        from ...tools.metadata import get_tool_metadata

        agent = get_agent_fn()
        loader = get_custom_tool_loader()

        builtin_meta = get_tool_metadata(tool_id)
        custom_defn = loader.get_definition(tool_id)
        if not builtin_meta and not custom_defn:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        with agent.profile_manager.atomic_update(user_id) as profile:
            if not request.config:
                profile.tool_preferences.tool_configs.pop(tool_id, None)
                return {
                    "status": "ok",
                    "tool_id": tool_id,
                    "action": "cleared",
                    "config": {},
                }

            profile.tool_preferences.set_tool_config(tool_id, request.config)
            return {
                "status": "ok",
                "tool_id": tool_id,
                "action": "set",
                "config": request.config,
            }

    @router.post("/tools/unified", response_model=UnifiedToolResponse)
    async def create_unified_tool(
        request: CustomToolCreateRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Create a new custom tool via the unified API."""
        loader = get_custom_tool_loader()

        if loader.get_definition(request.id):
            raise HTTPException(
                status_code=400,
                detail=f"Tool '{request.id}' already exists",
            )

        http_config = None
        mcp_config = None
        if request.implementation_type == "http":
            if not request.http_config:
                raise HTTPException(
                    status_code=400,
                    detail="http_config is required for HTTP tools",
                )
            http_config = http_config_to_core(request.http_config)
        elif request.implementation_type == "mcp":
            if not request.mcp_config:
                raise HTTPException(
                    status_code=400,
                    detail="mcp_config is required for MCP tools",
                )
            mcp_config = mcp_config_to_core(request.mcp_config)

        definition = CustomToolDefinition(
            id=request.id,
            name=request.name,
            description=request.description,
            parameters=tool_parameters_to_core(request.parameters or {}),
            implementation_type=request.implementation_type,
            http_config=http_config,
            mcp_config=mcp_config,
            enabled=request.enabled,
            tags=request.tags,
        )

        loader.save_definition(definition)
        get_agent_fn().reload_tools()

        return custom_tool_definition_to_unified(definition, True, "default", {})

    @router.put("/tools/unified/{tool_id}", response_model=UnifiedToolResponse)
    async def update_unified_tool(
        tool_id: str,
        request: CustomToolUpdateRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Update a custom tool via the unified API."""
        from ...tools.metadata import get_tool_metadata

        loader = get_custom_tool_loader()
        if get_tool_metadata(tool_id):
            raise HTTPException(
                status_code=400,
                detail="Cannot edit built-in tools. Use enable/disable or configure instead.",
            )

        definition = loader.get_definition(tool_id)
        if not definition:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        if request.name is not None:
            definition.name = request.name
        if request.description is not None:
            definition.description = request.description
        if request.parameters is not None:
            definition.parameters = tool_parameters_to_core(request.parameters)
        if request.http_config is not None and definition.implementation_type == "http":
            definition.http_config = http_config_to_core(request.http_config)
        if request.mcp_config is not None and definition.implementation_type == "mcp":
            definition.mcp_config = mcp_config_to_core(request.mcp_config)
        if request.enabled is not None:
            definition.enabled = request.enabled
        if request.tags is not None:
            definition.tags = request.tags

        loader.save_definition(definition)
        get_agent_fn().reload_tools()

        return custom_tool_definition_to_unified(definition, True, "default", {})

    @router.delete("/tools/unified/{tool_id}")
    async def delete_unified_tool(
        tool_id: str,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Delete a custom tool via the unified API."""
        from ...tools.metadata import get_tool_metadata

        loader = get_custom_tool_loader()
        if get_tool_metadata(tool_id):
            raise HTTPException(
                status_code=400,
                detail="Cannot delete built-in tools",
            )

        if not loader.get_definition(tool_id):
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        loader.delete_definition(tool_id)
        reload_custom_tools()

        return {
            "status": "ok",
            "deleted": tool_id,
        }

    return router
