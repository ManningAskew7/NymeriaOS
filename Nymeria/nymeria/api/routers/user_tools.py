"""User tool preferences endpoints."""

from collections.abc import Callable
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ...core.accounts import AuthenticatedUser
from ...core.time_utils import utc_now


class ToolConfigRequest(BaseModel):
    config: Dict[str, Any] = Field(..., description="Tool configuration")


class ToolPreferencesResponse(BaseModel):
    default_thread_tools: Optional[List[str]] = None
    tool_configs: Dict[str, Dict[str, Any]] = {}


def create_user_tools_router(
    verify_api_key: Callable[..., Any],
    get_agent_fn: Callable[..., Any],
    require_same_user_or_admin_fn: Callable[..., None],
) -> APIRouter:
    """Create the user tool preferences router with app dependencies injected."""
    router = APIRouter(tags=["User Tools"])

    @router.get("/users/{user_id}/tools")
    async def list_user_tools(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List all tools with their enabled state for a specific user."""
        require_same_user_or_admin_fn(user, user_id)
        from ...tools import (
            ALL_TOOLS,
            OPTIONAL_TOOLS,
            filter_discoverable_optional_tool_names,
        )
        from ...tools.metadata import get_tool_metadata

        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        dtt = profile.tool_preferences.default_thread_tools
        dtt_set = set(dtt) if dtt is not None else {t.name for t in ALL_TOOLS}

        tools_list = []
        for t in ALL_TOOLS:
            meta = get_tool_metadata(t.name)
            tools_list.append({
                "name": t.name,
                "description": t.description,
                "category": meta.category.value if meta else "core",
                "security_level": meta.security_level.value if meta else "safe",
                "enabled": t.name in dtt_set,
                "enabled_reason": "default_thread_tools",
                "default_enabled": True,
                "config_schema": meta.config_schema if meta else None,
                "user_config": profile.tool_preferences.get_tool_config(t.name),
                "globally_disabled": False,
            })

        visible_optional = filter_discoverable_optional_tool_names(
            OPTIONAL_TOOLS.keys(),
            user.role,
        )
        for name, t in OPTIONAL_TOOLS.items():
            if name not in visible_optional:
                continue
            meta = get_tool_metadata(name)
            tools_list.append({
                "name": name,
                "description": t.description,
                "category": meta.category.value if meta else "unknown",
                "security_level": meta.security_level.value if meta else "moderate",
                "enabled": name in dtt_set,
                "enabled_reason": "default_thread_tools",
                "default_enabled": False,
                "config_schema": meta.config_schema if meta else None,
                "user_config": profile.tool_preferences.get_tool_config(name),
                "globally_disabled": False,
            })

        by_category: Dict[str, List[dict]] = {}
        for tool in tools_list:
            category = tool.get("category", "unknown")
            if category not in by_category:
                by_category[category] = []
            by_category[category].append(tool)

        return {
            "user_id": user_id,
            "tools": tools_list,
            "by_category": by_category,
            "total": len(tools_list),
        }

    @router.get("/users/{user_id}/tools/preferences", response_model=ToolPreferencesResponse)
    async def get_tool_preferences(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Get user's tool preferences."""
        require_same_user_or_admin_fn(user, user_id)
        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)

        return ToolPreferencesResponse(
            default_thread_tools=profile.tool_preferences.default_thread_tools,
            tool_configs=profile.tool_preferences.tool_configs,
        )

    @router.put("/users/{user_id}/tools/{tool_name}/config")
    async def set_tool_config(
        user_id: str,
        tool_name: str,
        request: ToolConfigRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Set configuration for a specific tool."""
        require_same_user_or_admin_fn(user, user_id)
        from ...tools.metadata import get_tool_metadata

        agent = get_agent_fn()

        tool = agent.tool_registry.get_tool(tool_name)
        if not tool:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_name}' not found"
            )

        metadata = get_tool_metadata(tool_name)
        if metadata and metadata.config_schema:
            schema_props = metadata.config_schema.get("properties", {})
            for key in request.config:
                if key not in schema_props:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Unknown config key '{key}' for tool '{tool_name}'"
                    )

        with agent.profile_manager.atomic_update(user_id) as profile:
            profile.tool_preferences.set_tool_config(tool_name, request.config)
            profile.updated_at = utc_now()

        return {
            "status": "ok",
            "tool_name": tool_name,
            "config": request.config,
        }

    @router.post("/users/{user_id}/tools/reset")
    async def reset_tool_preferences(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Reset all tool preferences to defaults (all core tools enabled)."""
        require_same_user_or_admin_fn(user, user_id)
        from ...tools import ALL_TOOLS

        agent = get_agent_fn()

        with agent.profile_manager.atomic_update(user_id) as profile:
            profile.tool_preferences.default_thread_tools = [t.name for t in ALL_TOOLS]
            profile.tool_preferences.tool_configs.clear()
            profile.tool_preferences.custom_descriptions.clear()
            profile.updated_at = utc_now()

        agent._user_graphs.clear()
        agent._async_user_graphs.clear()
        agent._default_graph = agent._build_graph_with_prompt(agent._base_system_prompt)
        agent._default_async_graph = agent._build_async_graph_with_prompt(agent._base_system_prompt)

        return {
            "status": "ok",
            "message": "Tool preferences reset to defaults",
        }

    return router
