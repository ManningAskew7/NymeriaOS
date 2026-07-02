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
    apply_custom_tool_update,
    build_custom_tool_definition,
    custom_tool_definition_to_response,
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
            definition = build_custom_tool_definition(request, actor_user_id=user.id)
            loader.save_definition(definition)
            if definition.implementation_type == "workflow" and definition.workflow_config:
                from ...core.workflows.authoring import retain_source_revision

                retain_source_revision(
                    definition.id,
                    definition.workflow_config.revision_hash,
                    definition.workflow_config.source_code,
                )
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

        apply_custom_tool_update(definition, request, actor_user_id=user.id)

        loader.save_definition(definition)
        if definition.implementation_type == "workflow" and definition.workflow_config:
            from ...core.workflows.authoring import retain_source_revision

            retain_source_revision(
                definition.id,
                definition.workflow_config.revision_hash,
                definition.workflow_config.source_code,
            )
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
                if definition.http_config is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Tool '{tool_id}' is type 'http' but has no http_config",
                    )
                result = await execute_http_tool(
                    definition.http_config,
                    request.params,
                    target_type="custom_tool",
                    target_id=definition.id,
                    actor_user_id=user.id,
                )
            elif definition.implementation_type == "mcp":
                if definition.mcp_config is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Tool '{tool_id}' is type 'mcp' but has no mcp_config",
                    )
                result = await loader.mcp_manager.call_tool(
                    definition.mcp_config,
                    request.params,
                )
            elif definition.implementation_type == "python":
                if definition.python_config is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Tool '{tool_id}' is type 'python' but has no python_config",
                    )
                from ...core.python_custom_tools import execute_python_tool

                result = await execute_python_tool(
                    definition.python_config,
                    request.params,
                    target_id=definition.id,
                )
            elif definition.implementation_type == "workflow":
                if definition.workflow_config is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Tool '{tool_id}' is type 'workflow' but has no workflow_config",
                    )
                from ...core.workflows.tool_runtime import (
                    format_envelope_for_agent,
                    run_workflow_by_id,
                )

                refusal, run = await run_workflow_by_id(
                    loader,
                    definition.id,
                    request.params,
                    user_id=user.id,
                    thread_id="",
                )
                if refusal is not None:
                    result = f"[Error]: {refusal}"
                else:
                    assert run is not None  # one of (refusal, run) is None
                    result = format_envelope_for_agent(run.envelope)
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
