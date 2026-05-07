"""Classic tool discovery and default-tool routes."""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...core.accounts import AuthenticatedUser
from ..schemas.tools import DefaultToolsUpdateRequest


def create_tools_router(
    verify_api_key: Callable[..., Any],
    authed_user_id: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    require_thread_access_fn: Callable[..., None],
) -> APIRouter:
    """Create the classic Tools router with app dependencies injected."""
    router = APIRouter(tags=["Tools"])

    @router.get("/tools")
    async def list_tools(user: AuthenticatedUser = Depends(verify_api_key)):
        """List all available tools and their descriptions.

        Core and MCP tools are visible to every user. Callable-thread tools
        come from the caller's owned ThreadConfig rows instead of the global
        name-keyed registry, where same-name callables can collide.
        """
        agent = get_agent_fn()
        registry_tools = agent.tool_registry.list_tools()
        callable_map = agent._callable_tool_thread_map or {}
        callable_names_in_registry = set(callable_map.keys())

        is_admin = user.role == "admin"
        result = [
            t for t in registry_tools if t["name"] not in callable_names_in_registry
        ]

        owned = set(agent.accounts_repo.list_threads_for_user(user.id))
        for tc in agent.thread_config_manager.list_callable_threads(
            owned_thread_ids=owned,
        ):
            if not tc.callable_name:
                continue
            result.append({
                "name": tc.callable_name,
                "description": (
                    tc.callable_description
                    or f"Invoke the {tc.callable_name} thread"
                ),
                "enabled": True,
            })

        if is_admin:
            seen_names = {t["name"] for t in result}
            for tc in agent.thread_config_manager.list_callable_threads():
                if not tc.callable_name or tc.callable_name in seen_names:
                    continue
                if agent.accounts_repo.get_thread_owner(tc.thread_id) is None:
                    result.append({
                        "name": tc.callable_name,
                        "description": (
                            tc.callable_description
                            or f"Invoke the {tc.callable_name} thread"
                        ),
                        "enabled": True,
                    })

        return {"tools": result}

    @router.get("/tools/optional")
    async def list_optional_tools(
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List tools available for per-thread enabling."""
        from ...tools import (
            ALL_TOOLS,
            OPTIONAL_TOOLS,
            filter_discoverable_optional_tool_names,
        )

        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        default_tools = profile.tool_preferences.default_thread_tools

        core_set = (
            set(default_tools)
            if default_tools is not None
            else {t.name for t in ALL_TOOLS}
        )
        visible_optional = filter_discoverable_optional_tool_names(
            OPTIONAL_TOOLS.keys(),
            user.role,
        )
        result = []
        seen = set()
        for t in ALL_TOOLS:
            if t.name not in core_set and t.name not in seen:
                result.append({"name": t.name, "description": t.description})
                seen.add(t.name)
        for name, tool in OPTIONAL_TOOLS.items():
            if name in visible_optional and name not in core_set and name not in seen:
                result.append({"name": name, "description": tool.description})
                seen.add(name)
        return {"tools": result}

    @router.get("/threads/{thread_id}/callable-tools")
    async def get_thread_callable_tools(
        thread_id: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Return callable threads actually available to a caller thread."""
        require_thread_access_fn(user, thread_id)
        from ...tools import ALL_TOOLS, OPTIONAL_TOOLS

        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        caller_tc = agent.thread_config_manager.get_config(thread_id)
        disabled = set(caller_tc.disabled_tools if caller_tc else [])
        own_callable_name = (
            caller_tc.callable_name
            if caller_tc and caller_tc.callable and caller_tc.callable_name
            else None
        )

        all_tools_dict = {t.name: t for t in ALL_TOOLS}
        all_tools_dict.update(OPTIONAL_TOOLS)
        default_tools = profile.tool_preferences.default_thread_tools
        core_names = (
            default_tools
            if default_tools is not None
            else [t.name for t in ALL_TOOLS]
        )
        existing_names = {name for name in core_names if name in all_tools_dict}
        if default_tools is not None:
            existing_names.update(
                name
                for name in core_names
                if name.startswith("mcp__") and agent.tool_registry.get_tool(name)
            )

        visible = []
        seen_names: set[str] = set(existing_names)
        for tc in agent._get_team_scoped_callable_threads(
            user_id=user_id,
            caller_thread_id=thread_id,
        ):
            if tc.thread_id == thread_id or not tc.callable_name:
                continue
            if own_callable_name and tc.callable_name == own_callable_name:
                continue
            if tc.callable_name in disabled or tc.callable_name in seen_names:
                continue
            seen_names.add(tc.callable_name)
            visible.append({
                "thread_id": tc.thread_id,
                "name": tc.callable_name,
                "description": tc.callable_description,
                "team_id": tc.callable_team_id,
                "team_name": tc.callable_team_name,
            })

        return {
            "thread_id": thread_id,
            "callable_thread_count": len(visible),
            "callable_threads": visible,
        }

    @router.get("/tools/defaults")
    async def get_default_tools(
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Get the default tool set for new threads."""
        from ...tools import (
            ALL_TOOLS,
            OPTIONAL_TOOLS,
            filter_discoverable_optional_tool_names,
        )
        from ...tools.metadata import MCP_SERVER_TOOL_METADATA, get_tool_metadata

        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        prefs = profile.tool_preferences

        if prefs.default_thread_tools is not None:
            default_set = set(prefs.default_thread_tools)
        else:
            default_set = {t.name for t in ALL_TOOLS}

        tools_out = []
        seen = set()
        visible_optional = filter_discoverable_optional_tool_names(
            OPTIONAL_TOOLS.keys(),
            user.role,
        )
        for t in ALL_TOOLS:
            meta = get_tool_metadata(t.name)
            tools_out.append({
                "name": t.name,
                "description": t.description,
                "category": meta.category.value if meta else "core",
                "security_level": meta.security_level.value if meta else "moderate",
                "is_optional": False,
                "is_default": t.name in default_set,
            })
            seen.add(t.name)
        for name, t in OPTIONAL_TOOLS.items():
            if name in visible_optional and name not in seen:
                meta = get_tool_metadata(name)
                tools_out.append({
                    "name": name,
                    "description": t.description,
                    "category": meta.category.value if meta else "unknown",
                    "security_level": (
                        meta.security_level.value if meta else "moderate"
                    ),
                    "is_optional": True,
                    "is_default": name in default_set,
                })
                seen.add(name)

        for name, meta in MCP_SERVER_TOOL_METADATA.items():
            if name not in seen:
                tools_out.append({
                    "name": name,
                    "description": meta.description,
                    "category": "mcp_server",
                    "security_level": "moderate",
                    "is_optional": True,
                    "is_default": name in default_set,
                })
                seen.add(name)

        owned = set(agent.accounts_repo.list_threads_for_user(user_id))
        callable_count = len(
            agent.thread_config_manager.list_callable_threads(
                owned_thread_ids=owned,
            )
        )

        return {
            "mode": "custom",
            "default_tools": sorted(default_set),
            "available_tools": tools_out,
            "callable_thread_count": callable_count,
        }

    @router.put("/tools/defaults")
    async def set_default_tools(
        request: DefaultToolsUpdateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Set which tools new threads inherit by default."""
        from ...core.user_profile import migrate_tool_names
        from ...tools import (
            ADMIN_ONLY_OPTIONAL_TOOL_NAMES,
            ALL_TOOLS,
            DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES,
            LOCAL_SYSTEM_ACCESS_TOOL_NAMES,
            OPTIONAL_TOOLS,
        )
        from ...tools.metadata import MCP_SERVER_TOOL_METADATA

        tool_names = migrate_tool_names(list(request.tool_names))

        known = (
            {t.name for t in ALL_TOOLS}
            | set(OPTIONAL_TOOLS.keys())
            | set(MCP_SERVER_TOOL_METADATA.keys())
        )
        unknown = set(tool_names) - known
        if unknown:
            raise HTTPException(400, detail=f"Unknown tools: {sorted(unknown)}")

        blocked_defaults = LOCAL_SYSTEM_ACCESS_TOOL_NAMES.intersection(tool_names)
        if blocked_defaults:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Local shell/file tools must be enabled per-thread by an "
                    f"admin, not set as user defaults: {sorted(blocked_defaults)}"
                ),
            )

        if user.role != "admin":
            blocked = ADMIN_ONLY_OPTIONAL_TOOL_NAMES.intersection(tool_names)
            if blocked:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Admin-only tools cannot be set as defaults by this "
                        f"user: {sorted(blocked)}"
                    ),
                )
            blocked = DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES.intersection(tool_names)
            if blocked:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Developer-only diagnostic tools cannot be set as "
                        f"defaults by this user: {sorted(blocked)}"
                    ),
                )

        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        profile.tool_preferences.default_thread_tools = tool_names
        agent.profile_manager.save_profile(profile)

        agent._rebuild_default_graphs()

        return {
            "status": "ok",
            "default_tools": sorted(tool_names),
            "count": len(tool_names),
        }

    @router.delete("/tools/defaults")
    async def reset_default_tools(
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Reset default tools to all core tools."""
        from ...tools import ALL_TOOLS

        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        profile.tool_preferences.default_thread_tools = [t.name for t in ALL_TOOLS]
        agent.profile_manager.save_profile(profile)

        agent._rebuild_default_graphs()

        return {
            "status": "ok",
            "mode": "custom",
            "default_tools": sorted(profile.tool_preferences.default_thread_tools),
        }

    @router.get("/tools/categories")
    async def list_tool_categories(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List all tool categories with their tools."""
        from ...tools import filter_discoverable_optional_tool_names
        from ...tools.metadata import get_category_tools_summary

        categories = get_category_tools_summary()
        visible_names = filter_discoverable_optional_tool_names(
            {name for names in categories.values() for name in names},
            user.role,
        )
        return {
            "categories": {
                category: [name for name in names if name in visible_names]
                for category, names in categories.items()
            },
        }

    return router
