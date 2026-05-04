"""Custom tool CRUD, import/export, and test routes."""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ...core.accounts import AuthenticatedUser
from ...core.custom_tools import execute_http_tool, get_custom_tool_loader
from ...tools.definitions.custom_tool_schema import CustomToolDefinition
from ..schemas.custom_tools import (
    CustomToolCreateRequest,
    CustomToolListResponse,
    CustomToolResponse,
    CustomToolTestRequest,
    CustomToolUpdateRequest,
    custom_tool_definition_to_response,
    http_config_to_core,
    mcp_config_to_core,
    tool_parameters_to_core,
)


def create_custom_tools_router(
    require_admin_user: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
) -> APIRouter:
    """Create the custom tools router with app dependencies injected."""
    router = APIRouter(tags=["Custom Tools"])

    @router.get("/tools/custom", response_model=CustomToolListResponse)
    async def list_custom_tools(
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """List all custom tools."""
        loader = get_custom_tool_loader()
        definitions = loader.get_all_definitions()

        return CustomToolListResponse(
            tools=[custom_tool_definition_to_response(d) for d in definitions],
            total=len(definitions),
        )

    @router.post("/tools/custom", response_model=CustomToolResponse)
    async def create_custom_tool(
        request: CustomToolCreateRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Create a new custom tool."""
        loader = get_custom_tool_loader()

        existing = loader.get_definition(request.id)
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Tool with ID '{request.id}' already exists",
            )

        try:
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
                parameters=tool_parameters_to_core(request.parameters),
                implementation_type=request.implementation_type,
                http_config=http_config,
                mcp_config=mcp_config,
                enabled=request.enabled,
                tags=request.tags,
            )

            loader.save_definition(definition)
            get_agent_fn().reload_tools()

            return custom_tool_definition_to_response(definition)

        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.get("/tools/custom/export")
    async def export_custom_tools(
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Export all custom tools as JSON."""
        loader = get_custom_tool_loader()
        definitions = loader.get_all_definitions()

        return {
            "tools": [d.model_dump() for d in definitions],
            "total": len(definitions),
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }

    @router.post("/tools/custom/import")
    async def import_custom_tools(
        request: Request,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Import custom tools from JSON."""
        loader = get_custom_tool_loader()
        body = await request.json()

        tools_data = body.get("tools", [])
        imported = 0
        errors = []

        for tool_data in tools_data:
            try:
                definition = CustomToolDefinition(**tool_data)
                loader.save_definition(definition)
                imported += 1
            except Exception as e:
                errors.append(f"{tool_data.get('id', 'unknown')}: {str(e)}")

        if imported > 0:
            get_agent_fn().reload_tools()

        return {
            "status": "ok",
            "imported": imported,
            "errors": errors,
        }

    @router.get("/tools/custom/{tool_id}", response_model=CustomToolResponse)
    async def get_custom_tool(
        tool_id: str,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Get a custom tool by ID."""
        loader = get_custom_tool_loader()
        definition = loader.get_definition(tool_id)

        if not definition:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        return custom_tool_definition_to_response(definition)

    @router.put("/tools/custom/{tool_id}", response_model=CustomToolResponse)
    async def update_custom_tool(
        tool_id: str,
        request: CustomToolUpdateRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Update an existing custom tool."""
        loader = get_custom_tool_loader()
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
        if request.enabled is not None:
            definition.enabled = request.enabled
        if request.tags is not None:
            definition.tags = request.tags

        if request.http_config is not None and definition.implementation_type == "http":
            definition.http_config = http_config_to_core(request.http_config)
        if request.mcp_config is not None and definition.implementation_type == "mcp":
            definition.mcp_config = mcp_config_to_core(request.mcp_config)

        loader.save_definition(definition)
        get_agent_fn().reload_tools()

        return custom_tool_definition_to_response(definition)

    @router.delete("/tools/custom/{tool_id}")
    async def delete_custom_tool(
        tool_id: str,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Delete a custom tool."""
        loader = get_custom_tool_loader()

        if not loader.delete_definition(tool_id):
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        get_agent_fn().reload_tools()

        return {"status": "ok", "deleted_id": tool_id}

    @router.post("/tools/custom/{tool_id}/test")
    async def test_custom_tool(
        tool_id: str,
        request: CustomToolTestRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Test a custom tool with sample parameters."""
        loader = get_custom_tool_loader()
        definition = loader.get_definition(tool_id)

        if not definition:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        try:
            if definition.implementation_type == "http":
                result = await execute_http_tool(definition.http_config, request.params)
            elif definition.implementation_type == "mcp":
                result = await loader.mcp_manager.call_tool(
                    definition.mcp_config,
                    request.params,
                )
            else:
                result = f"[Error]: Unknown implementation type: {definition.implementation_type}"

            return {
                "status": "ok",
                "tool_id": tool_id,
                "result": result,
            }
        except Exception as e:
            return {
                "status": "error",
                "tool_id": tool_id,
                "error": str(e),
            }

    return router
