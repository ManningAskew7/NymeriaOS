"""MCP server management and install routes."""

import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from ...core.accounts import AuthenticatedUser
from ...core.mcp_tool_names import (
    display_mcp_tool_names,
    format_mcp_tool_name,
    registered_mcp_tool_names,
)
from ...core.mcp_servers import get_mcp_server_registry
from ...tools.definitions.mcp_schema import MCPServerDefinition
from ..schemas.mcp_servers import (
    MCPServerCreateRequest,
    MCPServerInstallPreviewRequest,
    MCPServerInstallRequest,
    MCPServerInstallRetryRequest,
    MCPServerUpdateRequest,
)

logger = logging.getLogger(__name__)


def _mcp_install_response(
    *,
    registry,
    defn,
    parsed_summary: str,
    discovered,
    thread_id: Optional[str],
    status: str = "ok",
    discovery_error: Optional[str] = None,
):
    tool_names = registered_mcp_tool_names(defn, discovered)
    return {
        "status": status,
        "server": registry.get_server(defn.id).model_dump(mode="json"),
        "parsed_summary": parsed_summary,
        "discovered_tools": len(discovered),
        "tool_names": tool_names,
        "tool_display_names": display_mcp_tool_names(defn, discovered),
        "thread_id": thread_id,
        "discovery_error": discovery_error,
        "install_logs": defn.install_logs,
        "missing_config": defn.missing_config,
        "credential_requirements": defn.credential_requirements,
        "registered_tool_names": defn.registered_tool_names,
        "requires_confirmation": bool(defn.confirmation_required and status != "ok"),
    }


def _enable_mcp_tools_for_user_defaults(
    *,
    get_agent_fn: Callable[[], Any],
    user_id: str,
    tool_names: List[str],
) -> None:
    if not tool_names:
        return
    from ...tools import SEED_TOOLS

    agent = get_agent_fn()
    with agent.profile_manager.atomic_update(user_id) as profile:
        dtt = profile.tool_preferences.default_thread_tools
        if dtt is None:
            dtt = [t.name for t in SEED_TOOLS]
        for name in tool_names:
            if name not in dtt:
                dtt.append(name)
        profile.tool_preferences.default_thread_tools = dtt
    agent._rebuild_default_graphs()


def _attach_mcp_tools_to_thread(
    *,
    get_agent_fn: Callable[[], Any],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
    user: AuthenticatedUser,
    thread_id: str,
    tool_names: List[str],
) -> None:
    if not thread_id or not tool_names:
        return
    agent = get_agent_fn()
    require_thread_access_fn(user, thread_id)
    from ...core.thread_config import ThreadConfig

    tc = agent.thread_config_manager.get_config(thread_id)
    enabled_tools = list(tc.enabled_tools) if tc and tc.enabled_tools else []
    for tool_name in tool_names:
        if tool_name not in enabled_tools:
            enabled_tools.append(tool_name)
    if tc is None:
        tc = ThreadConfig(thread_id=thread_id)
    tc.enabled_tools = enabled_tools
    agent.thread_config_manager.save_config(tc)
    agent.invalidate_thread_config_cache(thread_id)


async def _run_mcp_install(
    *,
    defn,
    plan,
    registry,
    user: AuthenticatedUser,
    auto_enable: bool,
    thread_id: Optional[str],
    confirmed: bool,
    confirmed_risk_ids: List[str],
    config_values: Dict[str, str],
    credential_values: Dict[str, str],
    credential_bindings: Dict[str, Any],
    get_agent_fn: Callable[[], Any],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
):
    from ...core.mcp_runtime import make_failed_draft, prepare_runtime
    from ...core.mcp_auth_bridge import apply_mcp_auth_presets

    required_risks = {
        str(signal.get("id"))
        for signal in plan.risk_signals
        if signal.get("requires_confirmation")
    }
    confirmed_risks = set(confirmed_risk_ids or [])
    if plan.confirmation_required and not (confirmed or required_risks <= confirmed_risks):
        defn.enabled = False
        raise HTTPException(
            status_code=409,
            detail={
                "message": "This MCP install needs admin confirmation before Nymeria runs it.",
                "preview": plan.to_dict(),
                "server": defn.model_dump(mode="json"),
            },
        )

    logs: List[str] = []
    defn.install_status = "approved"
    defn.enabled = False
    defn.install_logs = ["Install approved."]
    registry.save_server(defn)
    try:
        defn.install_status = "preparing"
        defn.install_logs = logs
        registry.save_server(defn)
        defn, logs = prepare_runtime(
            defn,
            plan,
            config_values=config_values,
            credential_values=credential_values,
            credential_bindings=credential_bindings,
            user_id=user.id,
            log_sink=logs,
        )
        defn = apply_mcp_auth_presets(defn, user_id=user.id, log_sink=logs)
    except Exception as e:
        failed = make_failed_draft(defn, plan, str(e), logs)
        if failed.missing_config:
            failed.install_status = "needs_config"
        registry.save_server(failed)
        return _mcp_install_response(
            registry=registry,
            defn=failed,
            parsed_summary=plan.parsed_summary,
            discovered=[],
            thread_id=thread_id,
            status="needs_config" if failed.install_status == "needs_config" else "draft",
            discovery_error=str(e),
        )

    defn.enabled = bool(auto_enable)
    defn.install_status = "discovering"
    defn.last_error = None
    defn.install_logs = logs
    defn.install_plan = plan.to_dict()
    defn.missing_config = []
    defn.credential_requirements = plan.credential_requirements
    defn.risk_signals = plan.risk_signals
    registry.save_server(defn)

    try:
        discovered = registry.discover_tools(defn.id)
    except Exception as e:
        failed = make_failed_draft(defn, plan, str(e), logs)
        registry.save_server(failed)
        logger.warning("install_mcp_server discovery failed for %s: %s", defn.id, e)
        return _mcp_install_response(
            registry=registry,
            defn=failed,
            parsed_summary=plan.parsed_summary,
            discovered=[],
            thread_id=thread_id,
            status="draft",
            discovery_error=str(e),
        )
    defn = registry.get_server(defn.id)
    defn.install_status = "ready"
    defn.enabled = bool(auto_enable)
    defn.last_error = None
    defn.install_logs = logs
    defn.registered_tool_names = registered_mcp_tool_names(defn, discovered)
    registry.save_server(defn)

    agent = get_agent_fn()
    agent.reload_mcp_server_tools()
    tool_names = registered_mcp_tool_names(defn, discovered)
    if thread_id:
        _attach_mcp_tools_to_thread(
            get_agent_fn=get_agent_fn,
            require_thread_access_fn=require_thread_access_fn,
            user=user,
            thread_id=thread_id,
            tool_names=tool_names,
        )
    elif auto_enable:
        _enable_mcp_tools_for_user_defaults(
            get_agent_fn=get_agent_fn,
            user_id=user.id,
            tool_names=tool_names,
        )

    return _mcp_install_response(
        registry=registry,
        defn=registry.get_server(defn.id),
        parsed_summary=plan.parsed_summary,
        discovered=discovered,
        thread_id=thread_id,
        status="ok",
    )


def create_mcp_servers_router(
    require_admin_user: Callable[..., Any],
    require_admin_caller: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Any],
    require_thread_access_fn: Callable[[AuthenticatedUser, str], None],
) -> APIRouter:
    """Create the MCP Servers router with app dependencies injected."""
    router = APIRouter(tags=["MCP Servers"])

    @router.get("/mcp-servers")
    async def list_mcp_servers(user: AuthenticatedUser = Depends(require_admin_user)):
        """List all MCP server definitions with discovered tools.

        Admin-only — server definitions include ``env_vars``, ``headers``,
        ``server_command``, and ``working_directory``, which can hold
        credentials and host paths. Per-thread MCP enablement uses the
        per-tool unified API; non-admin discovery has no need for raw
        configs.
        """
        registry = get_mcp_server_registry()
        servers = registry.get_all_servers()
        return {
            "servers": [s.model_dump(mode="json") for s in servers],
            "total": len(servers),
        }

    @router.post("/mcp-servers")
    async def create_mcp_server(
        request: MCPServerCreateRequest,
        thread_id: Optional[str] = Query(None, description="Auto-enable tools for this thread"),
        user: AuthenticatedUser = Depends(require_admin_caller),
    ):
        """Add a new MCP server. Admin-only — saves config and triggers tool
        discovery. MCP server install runs local stdio commands; allowing every
        authenticated user to register one would let non-admin users execute
        arbitrary commands via the agent process.

        Uses :func:`require_admin_caller` so the admin can use
        ``X-Nymeria-Act-As`` to bind tools to a specific user's thread —
        ``_require_thread_access`` below then runs ownership against that
        target (admin direct still bypasses because admin role survives
        act-as for admin-as-admin).
        """
        registry = get_mcp_server_registry()

        # Check for duplicate ID
        if registry.get_server(request.id):
            raise HTTPException(400, detail=f"MCP server '{request.id}' already exists")

        defn = MCPServerDefinition(
            id=request.id,
            name=request.name,
            description=request.description,
            server_command=request.server_command,
            server_args=request.server_args,
            env_vars=request.env_vars,
            working_directory=request.working_directory,
            idle_timeout_seconds=request.idle_timeout_seconds,
            startup_timeout_seconds=request.startup_timeout_seconds,
            enabled=request.enabled,
        )
        install_logs: List[str] = []
        from ...core.mcp_auth_bridge import apply_mcp_auth_presets

        defn = apply_mcp_auth_presets(defn, user_id=user.id, log_sink=install_logs)
        defn.install_logs = install_logs
        registry.save_server(defn)

        # Discover tools
        discovered = []
        discovery_error = None
        try:
            discovered = registry.discover_tools(request.id)
        except Exception as e:
            discovery_error = str(e)
            defn.install_status = "failed"
            defn.enabled = False
            defn.last_error = discovery_error
            registry.save_server(defn)
            logger.warning("Tool discovery failed for MCP server '%s': %s", request.id, e)

        # Reload agent tools so new MCP tools are available
        agent = get_agent_fn()
        agent.reload_mcp_server_tools()

        # If thread_id provided, auto-enable all discovered tools for that thread.
        # Admin-only endpoint already, but still gate the thread mutation: an
        # admin acting on behalf of a user (or just typo'ing a thread ID) shouldn't
        # be able to mutate a thread the caller doesn't own.
        if thread_id and discovered:
            _attach_mcp_tools_to_thread(
                get_agent_fn=get_agent_fn,
                require_thread_access_fn=require_thread_access_fn,
                user=user,
                thread_id=thread_id,
                tool_names=[
                    format_mcp_tool_name(request.id, dt.name) for dt in discovered
                ],
            )

        saved_server = registry.get_server(request.id)
        if saved_server is None:
            raise HTTPException(404, detail=f"MCP server '{request.id}' not found after save")
        result = {
            "status": "ok",
            "server": saved_server.model_dump(mode="json"),
            "discovered_tools": len(discovered),
            "tool_names": registered_mcp_tool_names(saved_server, discovered),
            "tool_display_names": display_mcp_tool_names(saved_server, discovered),
        }
        if discovery_error:
            result["discovery_error"] = discovery_error
        if thread_id:
            result["thread_id"] = thread_id

        return result

    @router.get("/mcp-servers/{server_id}")
    async def get_mcp_server(
        server_id: str,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Get a specific MCP server definition. Admin-only — same secret
        leakage concerns as the list endpoint."""
        registry = get_mcp_server_registry()
        defn = registry.get_server(server_id)
        if not defn:
            raise HTTPException(404, detail=f"MCP server '{server_id}' not found")
        return defn.model_dump(mode="json")

    @router.put("/mcp-servers/{server_id}")
    async def update_mcp_server(
        server_id: str,
        request: MCPServerUpdateRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Update an MCP server config. Admin-only — re-discovers tools after update."""
        registry = get_mcp_server_registry()
        defn = registry.get_server(server_id)
        if not defn:
            raise HTTPException(404, detail=f"MCP server '{server_id}' not found")

        # Apply updates
        update_data = request.model_dump(exclude_none=True)
        for key, value in update_data.items():
            setattr(defn, key, value)
        if "enabled" in update_data:
            if not defn.enabled:
                defn.install_status = "disabled"
            elif defn.install_status == "disabled":
                defn.install_status = "ready" if defn.discovered_tools else "needs_config"

        install_logs = list(defn.install_logs or [])
        from ...core.mcp_auth_bridge import apply_mcp_auth_presets

        defn = apply_mcp_auth_presets(defn, user_id=user.id, log_sink=install_logs)
        defn.install_logs = install_logs
        registry.save_server(defn)

        # Re-discover tools
        discovered = []
        discovery_error = None
        try:
            discovered = registry.discover_tools(server_id)
        except Exception as e:
            discovery_error = str(e)
            defn.install_status = "failed"
            defn.enabled = False
            defn.last_error = discovery_error
            registry.save_server(defn)

        # Reload agent tools
        agent = get_agent_fn()
        agent.reload_mcp_server_tools()

        updated_server = registry.get_server(server_id)
        if updated_server is None:
            raise HTTPException(404, detail=f"MCP server '{server_id}' not found after update")
        result = {
            "status": "ok",
            "server": updated_server.model_dump(mode="json"),
            "discovered_tools": len(discovered),
            "tool_names": registered_mcp_tool_names(updated_server, discovered),
            "tool_display_names": display_mcp_tool_names(updated_server, discovered),
        }
        if discovery_error:
            result["discovery_error"] = discovery_error
        return result

    @router.delete("/mcp-servers/{server_id}")
    async def delete_mcp_server(
        server_id: str,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Remove an MCP server and all its tools. Admin-only."""
        registry = get_mcp_server_registry()
        if not registry.delete_server(server_id):
            raise HTTPException(404, detail=f"MCP server '{server_id}' not found")

        # Reload agent tools to remove deleted MCP tools
        agent = get_agent_fn()
        agent.reload_mcp_server_tools()

        return {"status": "ok", "deleted": server_id}

    @router.post("/mcp-servers/{server_id}/discover")
    async def discover_mcp_server_tools(
        server_id: str,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Force re-discover tools from an MCP server. Admin-only — discovery starts the server process."""
        registry = get_mcp_server_registry()
        if not registry.get_server(server_id):
            raise HTTPException(404, detail=f"MCP server '{server_id}' not found")

        try:
            discovered = registry.discover_tools(server_id)
        except Exception as e:
            defn = registry.get_server(server_id)
            if defn:
                defn.install_status = "failed"
                defn.enabled = False
                defn.last_error = str(e)
                registry.save_server(defn)
            agent = get_agent_fn()
            agent.reload_mcp_server_tools()
            raise HTTPException(500, detail=f"Discovery failed: {str(e)}")

        # Reload agent tools
        agent = get_agent_fn()
        agent.reload_mcp_server_tools()
        updated_server = registry.get_server(server_id) or {"id": server_id, "name": server_id}

        return {
            "status": "ok",
            "server_id": server_id,
            "discovered_tools": [dt.model_dump() for dt in discovered],
            "tool_names": registered_mcp_tool_names(updated_server, discovered),
            "tool_display_names": display_mcp_tool_names(updated_server, discovered),
            "count": len(discovered),
        }

    @router.post("/mcp-servers/{server_id}/test")
    async def test_mcp_server(
        server_id: str,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Test connectivity to an MCP server. Admin-only — connects/launches the server."""
        registry = get_mcp_server_registry()
        if not registry.get_server(server_id):
            raise HTTPException(404, detail=f"MCP server '{server_id}' not found")

        result = registry.test_connection(server_id)
        return result

    @router.post("/mcp-servers/install/preview")
    async def preview_mcp_server_install(
        request: MCPServerInstallPreviewRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Parse an MCP install source and return a non-executing install plan."""
        from ...core.mcp_installer import MCPInstallError
        from ...core.mcp_runtime import analyze_text_source, save_preview

        try:
            candidates = analyze_text_source(request.source, name=request.name)
            first = candidates[0]
            token = save_preview(
                first.definition,
                first.plan,
                source=request.source,
                candidates=candidates,
            )
        except MCPInstallError as e:
            raise HTTPException(400, detail=str(e))
        except Exception as e:
            logger.exception("preview_mcp_server_install failed")
            raise HTTPException(400, detail=f"{type(e).__name__}: {e}")

        return {
            "preview_token": token,
            "selected_candidate_id": first.id if len(candidates) == 1 else None,
            "candidate_id": first.id if len(candidates) == 1 else None,
            "server": first.definition.model_dump(mode="json"),
            "plan": first.plan.to_dict(),
            "candidates": [candidate.to_dict() for candidate in candidates],
            "credential_requirements": first.plan.credential_requirements,
            "risk_signals": first.plan.risk_signals,
            "install_steps": first.plan.install_steps,
            "can_install": first.plan.can_install,
        }

    @router.post("/mcp-servers/install/preview-upload")
    async def preview_mcp_server_bundle_upload(
        file: UploadFile = File(...),
        name: Optional[str] = Form(None),
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Preview an uploaded .mcpb/.dxt/.zip bundle without running it."""
        from ...core.mcp_installer import MCPInstallError
        from ...core.mcp_runtime import plan_bundle_file, save_preview

        filename = file.filename or "bundle.mcpb"
        if not filename.lower().endswith((".mcpb", ".dxt", ".zip")):
            raise HTTPException(400, detail="upload must be a .mcpb, .dxt, or .zip file")

        upload_dir = get_settings_fn().data_dir / "mcp_install_previews" / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload_path = upload_dir / f"{uuid.uuid4().hex}-{Path(filename).name}"
        try:
            content = await file.read()
            upload_path.write_bytes(content)
            defn, plan = plan_bundle_file(upload_path, original_name=filename, name=name)
            token = save_preview(defn, plan, source=filename)
        except MCPInstallError as e:
            upload_path.unlink(missing_ok=True)
            raise HTTPException(400, detail=str(e))
        except Exception as e:
            upload_path.unlink(missing_ok=True)
            logger.exception("preview_mcp_server_bundle_upload failed")
            raise HTTPException(400, detail=f"{type(e).__name__}: {e}")

        return {
            "preview_token": token,
            "selected_candidate_id": "candidate-1",
            "candidate_id": "candidate-1",
            "server": defn.model_dump(mode="json"),
            "plan": plan.to_dict(),
            "candidates": [
                {
                    "id": "candidate-1",
                    "title": defn.name,
                    "source": filename,
                    "server": defn.model_dump(mode="json"),
                    "plan": plan.to_dict(),
                    "credential_requirements": plan.credential_requirements,
                    "risk_signals": plan.risk_signals,
                    "install_steps": plan.install_steps,
                    "can_install": plan.can_install,
                }
            ],
            "credential_requirements": plan.credential_requirements,
            "risk_signals": plan.risk_signals,
            "install_steps": plan.install_steps,
            "can_install": plan.can_install,
        }

    @router.post("/mcp-servers/install")
    async def install_mcp_server(
        request: MCPServerInstallRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Install an MCP server from a user-pasted source string. Admin-only.

        Parses `source` or a saved preview into a server definition, prepares
        any managed runtime needed, discovers tools, and wires them into the
        agent. Failed setup/discovery is saved as a disabled draft so admins
        can inspect logs and retry.

        Admin-only because install can launch arbitrary stdio commands inside
        the agent process.
        """
        from ...core.mcp_installer import MCPInstallError
        from ...core.mcp_runtime import (
            consume_preview,
            load_preview,
        )

        try:
            if request.preview_token:
                _, defn, plan = load_preview(
                    request.preview_token,
                    candidate_id=request.candidate_id,
                )
            else:
                raise MCPInstallError("preview_token is required; preview the MCP source before installing")
        except MCPInstallError as e:
            raise HTTPException(400, detail=str(e))
        except Exception as e:
            logger.exception("install_mcp_server parse failed")
            raise HTTPException(400, detail=f"{type(e).__name__}: {e}")

        registry = get_mcp_server_registry()
        if registry.get_server(defn.id):
            # Very unlikely (ids include a random suffix) but handle it.
            raise HTTPException(409, detail=f"MCP server '{defn.id}' already exists")

        result = await _run_mcp_install(
            defn=defn,
            plan=plan,
            registry=registry,
            user=user,
            auto_enable=request.auto_enable,
            thread_id=request.thread_id,
            confirmed=request.confirmed,
            confirmed_risk_ids=request.confirmed_risk_ids,
            config_values=request.config_values,
            credential_values=request.credential_values,
            credential_bindings=request.credential_bindings,
            get_agent_fn=get_agent_fn,
            require_thread_access_fn=require_thread_access_fn,
        )
        if request.preview_token:
            consume_preview(request.preview_token)
        return result

    @router.post("/mcp-servers/{server_id}/retry")
    async def retry_mcp_server_install(
        server_id: str,
        request: MCPServerInstallRetryRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Retry setup/discovery for a disabled draft or failed MCP server."""
        from ...core.mcp_runtime import MCPInstallPlan

        registry = get_mcp_server_registry()
        defn = registry.get_server(server_id)
        if not defn:
            raise HTTPException(404, detail=f"MCP server '{server_id}' not found")
        if not defn.install_plan:
            raise HTTPException(400, detail="MCP server has no install plan to retry")

        plan = MCPInstallPlan.from_dict(defn.install_plan)
        return await _run_mcp_install(
            defn=defn,
            plan=plan,
            registry=registry,
            user=user,
            auto_enable=True,
            thread_id=None,
            confirmed=request.confirmed,
            confirmed_risk_ids=request.confirmed_risk_ids,
            config_values=request.config_values,
            credential_values=request.credential_values,
            credential_bindings=request.credential_bindings,
            get_agent_fn=get_agent_fn,
            require_thread_access_fn=require_thread_access_fn,
        )

    return router
