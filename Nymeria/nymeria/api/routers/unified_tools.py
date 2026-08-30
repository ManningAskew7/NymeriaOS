"""Unified built-in, MCP server, and custom-tool routes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...core.accounts import AuthenticatedUser
from ...core.custom_tools import get_custom_tool_loader
from ...core.time_utils import utc_now
from ..schemas.custom_tools import (
    CustomToolCreateRequest,
    CustomToolUpdateRequest,
    apply_custom_tool_update,
    build_custom_tool_definition,
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

logger = logging.getLogger(__name__)


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
            SEED_TOOLS,
            CATALOG_TOOLS,
            filter_discoverable_catalog_tool_names,
            resolve_default_tool_names,
        )
        from ...tools.metadata import MCP_SERVER_TOOL_METADATA, get_tool_metadata

        agent = get_agent_fn()
        loader = get_custom_tool_loader()

        profile = agent.profile_manager.get_profile(user_id)
        tool_prefs = profile.tool_preferences

        # None is effectively unreachable now (the lazy profile migration
        # materializes the list); the canonical helper keeps the fallback
        # correct regardless.
        dtt_set = set(resolve_default_tool_names(tool_prefs.default_thread_tools))

        unified_tools = []
        seen = set()
        visible_optional = filter_discoverable_catalog_tool_names(
            CATALOG_TOOLS.keys(),
            user.role,
        )
        for tool in SEED_TOOLS:
            meta = get_tool_metadata(tool.name)
            enabled = tool.name in dtt_set
            tool_info = {
                "name": tool.name,
                "description": tool.description,
                "category": meta.category.value if meta else "general",
                "security_level": meta.security_level.value if meta else "safe",
                "enabled": enabled,
                "enabled_reason": "default_thread_tools",
                "config_schema": meta.config_schema if meta else None,
                "user_config": tool_prefs.get_tool_config(tool.name),
                "globally_disabled": False,
            }
            unified_tools.append(builtin_tool_to_unified(tool_info, tool_prefs))
            seen.add(tool.name)

        for name, tool in CATALOG_TOOLS.items():
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

        from ...tools.metadata import mcp_tool_surface_fields

        for tool_name, meta in MCP_SERVER_TOOL_METADATA.items():
            if tool_name in seen:
                continue
            enabled = tool_name in dtt_set
            live = bool(getattr(meta, "live", True))
            provenance = mcp_tool_surface_fields(tool_name)
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
                    server_id=provenance.get("server_id"),
                    server_name=provenance.get("server_name"),
                    display_name=provenance.get("display_name"),
                    # Setup axis from install status; the credential overlay
                    # below only writes provider-mapped tools, so this survives.
                    auth_status=provenance.get("auth_status"),
                    created_at=None,
                    updated_at=None,
                )
            )
            seen.add(tool_name)

        type_order = {"builtin": 0, "mcp_server": 1, "custom": 2}
        unified_tools.sort(key=lambda tool: (type_order.get(tool.tool_type, 9), tool.name))

        # Credential axis: one batched vault read covers every provider-mapped
        # tool; tools without a provider spec stay None (no credential needed).
        try:
            from ...tools.credential_registry import auth_status_for_tools

            pairs = auth_status_for_tools((tool.name for tool in unified_tools), user_id)
            for tool in unified_tools:
                pair = pairs.get(tool.name)
                if pair is not None:
                    tool.auth_provider, tool.auth_status = pair
        except Exception:
            logger.debug("unified tools auth-status overlay failed", exc_info=True)

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
            ADMIN_ONLY_TOOL_NAMES,
            DEVELOPER_ONLY_TOOL_NAMES,
            resolve_default_tool_names,
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
            and tool_id in ADMIN_ONLY_TOOL_NAMES
            and user.role != "admin"
        ):
            raise HTTPException(
                status_code=403,
                detail=f"Tool '{tool_id}' is admin-only",
            )
        if (
            request.enabled
            and tool_id in DEVELOPER_ONLY_TOOL_NAMES
            and user.role != "admin"
        ):
            raise HTTPException(
                status_code=403,
                detail=f"Tool '{tool_id}' is developer-only",
            )

        with agent.profile_manager.atomic_update(user_id) as profile:
            # Effectively unreachable fallback since the lazy profile
            # migration materializes the list; kept as the canonical helper
            # so the pattern stays correct if that ever changes.
            dtt = resolve_default_tool_names(
                profile.tool_preferences.default_thread_tools
            )

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

        definition = build_custom_tool_definition(request, actor_user_id=user.id)

        loader.save_definition(definition)
        get_agent_fn().reload_custom_tools()

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

        apply_custom_tool_update(definition, request, actor_user_id=user.id)

        loader.save_definition(definition)
        get_agent_fn().reload_custom_tools()

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
        # Agent-level narrow reload: unregisters the deleted tool and
        # rebuilds graphs (the module-level loader reload leaves the tool
        # bound until restart).
        get_agent_fn().reload_custom_tools()

        return {
            "status": "ok",
            "deleted": tool_id,
        }

    return router
