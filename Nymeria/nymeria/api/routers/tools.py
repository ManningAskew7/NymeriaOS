"""Classic tool discovery and default-tool routes."""

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from ...core.accounts import AuthenticatedUser
from ..schemas.tools import DefaultToolsUpdateRequest

logger = logging.getLogger(__name__)


def _apply_auth_status_fields(tools_out: list, user_id: str) -> None:
    """Stamp the credential axis onto serialized tool dicts, in place.

    ``auth_status``/``auth_provider`` for tools that map to a provider in the
    credential-spec registry (dev-todo #7); everything else gets None (= no
    credential required). One batched vault metadata read for the whole list.
    Best-effort and understating: a registry failure leaves the fields None,
    while a vault read failure inside the batch helper reads as "no records"
    (statuses degrade to needs_setup/optional, never a false "connected").
    """
    for item in tools_out:
        item.setdefault("auth_status", None)
        item.setdefault("auth_provider", None)
    try:
        from ...tools.credential_registry import auth_status_for_tools

        pairs = auth_status_for_tools((item["name"] for item in tools_out), user_id)
        for item in tools_out:
            pair = pairs.get(item["name"])
            if pair is not None:
                item["auth_provider"], item["auth_status"] = pair
    except Exception:
        logger.debug("auth-status stamping failed for tool list", exc_info=True)


def serialize_default_tools(agent: Any, *, user_id: str, role: str) -> dict:
    """Single source of truth for the default-tools read model.

    Shared by ``GET /tools/defaults`` and ``CommandBackendClient.get_default_tools``
    so the HTTP and in-process command shapes cannot drift (the TurnExecutor
    two-shape invariant), mirroring ``serialize_server_settings`` /
    ``serialize_env_entries``. The caller passes the resolved ``agent``,
    ``user_id`` (already access-checked), and account ``role``.
    """
    from ...tools import (
        SEED_TOOLS,
        CATALOG_TOOLS,
        filter_discoverable_catalog_tool_names,
        resolve_default_tool_names,
    )
    from ...tools.metadata import (
        MCP_SERVER_TOOL_METADATA,
        get_tool_metadata,
        integration_grouping_fields,
    )

    profile = agent.profile_manager.get_profile(user_id)
    prefs = profile.tool_preferences
    default_set = set(resolve_default_tool_names(prefs.default_thread_tools))

    tools_out = []
    seen = set()
    visible_optional = filter_discoverable_catalog_tool_names(
        CATALOG_TOOLS.keys(),
        role,
    )
    for t in SEED_TOOLS:
        meta = get_tool_metadata(t.name)
        category = meta.category.value if meta else "general"
        tools_out.append({
            "name": t.name,
            "description": t.description,
            "category": category,
            "security_level": meta.security_level.value if meta else "moderate",
            "is_optional": False,
            "is_default": t.name in default_set,
            **integration_grouping_fields(t.name, category),
        })
        seen.add(t.name)
    for name, t in CATALOG_TOOLS.items():
        if name in visible_optional and name not in seen:
            meta = get_tool_metadata(name)
            category = meta.category.value if meta else "unknown"
            tools_out.append({
                "name": name,
                "description": t.description,
                "category": category,
                "security_level": (
                    meta.security_level.value if meta else "moderate"
                ),
                "is_optional": True,
                "is_default": name in default_set,
                **integration_grouping_fields(name, category),
            })
            seen.add(name)

    from ...tools.metadata import mcp_tool_surface_fields

    for name, meta in MCP_SERVER_TOOL_METADATA.items():
        if name not in seen:
            provenance = mcp_tool_surface_fields(name)
            tools_out.append({
                "name": name,
                "description": meta.description,
                "category": "mcp_server",
                "security_level": "moderate",
                "is_optional": True,
                "is_default": name in default_set,
                "server_id": provenance.get("server_id"),
                "server_name": provenance.get("server_name"),
                "display_name": provenance.get("display_name"),
                # Setup axis from install status; setdefault below preserves it.
                "auth_status": provenance.get("auth_status"),
            })
            seen.add(name)

    _apply_auth_status_fields(tools_out, user_id)

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
            SEED_TOOLS,
            CATALOG_TOOLS,
            filter_discoverable_catalog_tool_names,
            resolve_default_tool_names,
        )

        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        default_tools = profile.tool_preferences.default_thread_tools

        core_set = set(resolve_default_tool_names(default_tools))
        visible_optional = filter_discoverable_catalog_tool_names(
            CATALOG_TOOLS.keys(),
            user.role,
        )
        result = []
        seen = set()
        for t in SEED_TOOLS:
            if t.name not in core_set and t.name not in seen:
                result.append({"name": t.name, "description": t.description})
                seen.add(t.name)
        for name, tool in CATALOG_TOOLS.items():
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
        require_thread_access_fn(user, thread_id, claim=False)
        from ...tools import resolve_default_tool_names, static_tool_catalog

        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        caller_tc = agent.thread_config_manager.get_config(thread_id)
        disabled = set(caller_tc.disabled_tools if caller_tc else [])
        own_callable_name = (
            caller_tc.callable_name
            if caller_tc and caller_tc.callable and caller_tc.callable_name
            else None
        )

        all_tools_dict = static_tool_catalog()
        default_tools = profile.tool_preferences.default_thread_tools
        core_names = resolve_default_tool_names(default_tools)
        existing_names = {name for name in core_names if name in all_tools_dict}
        if default_tools is not None:
            existing_names.update(
                name
                for name in core_names
                if name.startswith("mcp__") and agent.tool_registry.get_tool(name)
            )

        team_manager = getattr(agent, "team_manager", None)
        team_names: dict[str, str] = (
            team_manager.team_names(user_id) if team_manager is not None else {}
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
            team_id = tc.callable_team_id or None
            visible.append({
                "thread_id": tc.thread_id,
                "name": tc.callable_name,
                "description": tc.callable_description,
                "team_id": tc.callable_team_id,
                # Derived from the team store; legacy config name as fallback.
                "team_name": (
                    (team_names.get(team_id) or tc.callable_team_name or team_id)
                    if team_id
                    else tc.callable_team_name
                ),
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
        return serialize_default_tools(
            get_agent_fn(), user_id=user_id, role=user.role
        )

    @router.put("/tools/defaults")
    async def set_default_tools(
        request: DefaultToolsUpdateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Set which tools new threads inherit by default."""
        from ...core.user_profile import migrate_tool_names
        from ...tools import (
            ADMIN_ONLY_TOOL_NAMES,
            SEED_TOOLS,
            DEVELOPER_ONLY_TOOL_NAMES,
            CATALOG_TOOLS,
        )
        from ...tools.metadata import MCP_SERVER_TOOL_METADATA

        tool_names = migrate_tool_names(list(request.tool_names))

        known = (
            {t.name for t in SEED_TOOLS}
            | set(CATALOG_TOOLS.keys())
            | set(MCP_SERVER_TOOL_METADATA.keys())
        )
        unknown = set(tool_names) - known
        if unknown:
            raise HTTPException(400, detail=f"Unknown tools: {sorted(unknown)}")

        if user.role != "admin":
            blocked = ADMIN_ONLY_TOOL_NAMES.intersection(tool_names)
            if blocked:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "Admin-only tools cannot be set as defaults by this "
                        f"user: {sorted(blocked)}"
                    ),
                )
            blocked = DEVELOPER_ONLY_TOOL_NAMES.intersection(tool_names)
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
        """Reset default tools to the built-in seed set (SEED_TOOLS)."""
        from ...tools import seed_tool_names

        agent = get_agent_fn()
        profile = agent.profile_manager.get_profile(user_id)
        profile.tool_preferences.default_thread_tools = seed_tool_names()
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
        from ...tools import filter_discoverable_catalog_tool_names
        from ...tools.metadata import get_category_tools_summary

        categories = get_category_tools_summary()
        visible_names = filter_discoverable_catalog_tool_names(
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
