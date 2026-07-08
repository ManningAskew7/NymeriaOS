"""Agent-facing tools for discovering and installing MCP servers.

Mirrors the search_skills / install_skill pattern so the UX feels the same:
- search_mcp: find servers in the official MCP registry and Smithery
- install_mcp_server: preview/prepare a valid install source (Claude Desktop JSON,
  bare stdio command, HTTP URL, package page, Git URL, bundle URL, or registry
  id), save it, discover tools, and refresh the agent's tool registry.

The helper tool names intentionally avoid the `mcp_` prefix. Claude OAuth
classifies `mcp_<name>` tool names as third-party MCP app usage; dynamic MCP
server tools still use the safe `mcp__<server>__<tool>` namespace.

Accept the reality of 2026 install surfaces: every assistant vendor formats
"here's how you install my MCP server" differently. The parser's job is to
smooth that over; these tools are just a thin @tool wrapper around it.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Dict, Optional, Union, cast

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, InjectedToolCallId, tool
from langgraph.types import Command

from ..core.mcp_tool_names import (
    display_mcp_tool_names,
    format_mcp_tool_name,
    registered_mcp_tool_names,
)
from ..core.tool_reload import command_or_text
from .tool_search import DEFAULT_TTL, bind_tools_for_thread
from .utils import current_agent, get_user_id, is_admin, json_result

logger = logging.getLogger(__name__)


def _caller_is_admin(config: Optional[RunnableConfig]) -> bool:
    """Admin gate for MCP search actions; fails closed (see ``utils.is_admin``)."""
    return is_admin(get_user_id(config))


@tool
def search_mcp(query: str, top_k: int = 10) -> str:
    """Search public MCP server registries for servers matching a query.

    Looks across the official MCP registry (registry.modelcontextprotocol.io)
    and Smithery. Results come back even if one of the two registries is down
    or rate-limiting us. Use this before install_mcp_server when the user says
    something like "find me an MCP server that can X" rather than handing
    over a specific install command.

    Args:
        query: Free-text search term (e.g. "filesystem", "postgres", "browser").
        top_k: Max results to return across both registries (default 10).

    Returns:
        JSON string with {count, results: [{id, name, description, source,
        install_hint}]}. The `install_hint` (when present) is exactly what
        install_mcp_server(source=...) accepts.
    """
    from ..core.mcp_registry_client import search_registries

    try:
        results = search_registries(query or "")
    except Exception as e:
        logger.exception("search_mcp failed")
        return json.dumps({"error": f"{type(e).__name__}: {e}"})

    payload = [
        {
            "id": r.id,
            "name": r.name,
            "description": r.description,
            "source": r.source,
            "install_hint": r.install_hint,
        }
        for r in results[: max(1, top_k)]
    ]
    return json.dumps({"count": len(payload), "results": payload}, indent=2)


def _inspect_mcp(name: str = "") -> str:
    from ..core.mcp_servers import get_mcp_server_registry

    registry = get_mcp_server_registry()
    servers = registry.get_all_servers()
    if name:
        lowered = name.strip().lower()
        servers = [
            s for s in servers
            if s.id.lower() == lowered or s.name.lower() == lowered
        ]

    payload = []
    for server in sorted(servers, key=lambda item: item.id):
        payload.append(
            {
                "id": server.id,
                "name": server.name,
                "description": server.description,
                "enabled": server.enabled,
                "transport": server.transport,
                "install_status": server.install_status,
                "last_error": server.last_error,
                "risk_level": server.risk_level,
                "registered_tool_names": server.registered_tool_names,
                "tool_display_names": display_mcp_tool_names(server),
                "discovered_tool_names": [
                    tool_def.name for tool_def in server.discovered_tools
                ],
                "discovered_tools": [
                    {
                        "name": tool_def.name,
                        "display_name": f"{server.name or server.id} / {tool_def.name}",
                        "registered_name": format_mcp_tool_name(server.id, tool_def.name),
                        "description": tool_def.description,
                    }
                    for tool_def in server.discovered_tools
                ],
                "missing_config": server.missing_config,
                "credential_requirements": server.credential_requirements,
            }
        )
    return json_result(count=len(payload), servers=payload)


def _preview_mcp_source(source: str, name: Optional[str] = None) -> str:
    from ..core.mcp_installer import MCPInstallError
    from ..core.mcp_runtime import analyze_text_source, save_preview

    if not source:
        return json_result(error="source is required for preview")
    try:
        candidates = analyze_text_source(source, name=name)
        first = candidates[0]
        token = save_preview(
            first.definition,
            first.plan,
            source=source,
            candidates=candidates,
        )
    except MCPInstallError as e:
        return json_result(error=f"could not parse source: {e}")
    except Exception as e:
        logger.exception("manage_mcp preview failed")
        return json_result(error=f"{type(e).__name__}: {e}")

    return json_result(
        preview_token=token,
        selected_candidate_id=first.id if len(candidates) == 1 else None,
        server={
            "id": first.definition.id,
            "name": first.definition.name,
            "description": first.definition.description,
            "transport": first.definition.transport,
        },
        plan=first.plan.to_dict(),
        candidates=[candidate.to_dict() for candidate in candidates],
    )


def _install_mcp_server_impl(
    source: str,
    name: Optional[str] = None,
    preview_token: str = "",
    candidate_id: str = "",
    auto_enable: bool = True,
    confirmed: bool = False,
    confirmed_risk_ids: Optional[list[str]] = None,
    config_values: Optional[Dict[str, str]] = None,
    ttl: str = DEFAULT_TTL,
    auto_enable_thread: bool = True,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
    tool_call_id: Optional[str] = None,
) -> Union[str, Command]:
    """Install an MCP server from a user-provided source and wire its tools into Nymeria.

    Accepted source forms:
      - Claude Desktop JSON snippet: {"mcpServers": {"<name>": {"command": "...", "args": [...]}}}
      - Bare stdio command: `npx -y @modelcontextprotocol/server-filesystem /tmp`
      - HTTP/SSE URL: http://localhost:8811/mcp (works for Docker MCP Gateway too)
      - npm/PyPI package page, Git repository URL, or MCPB/DXT bundle URL
      - Official registry id: `io.github.modelcontextprotocol/server-filesystem`

    What this does:
      1. Parses the source into a server definition.
      2. Prepares any managed runtime needed for package/Git/bundle installs.
      3. Saves it to data/mcp_servers/<id>.json.
      4. Starts/connects, lists tools, stores the schemas.
      5. Refreshes the agent's tool registry so the new mcp__<id>__<tool>
         entries become callable on the next user turn.

    Args:
        source: Any of the accepted install forms above.
        name: Optional display name for the server. Defaults to a name derived
            from the source.
        auto_enable: If True (default), the server is saved with enabled=True
            so its tools are immediately available. Set False to save but not
            activate.
        confirmed: Retained for API compatibility. High-risk installs (Git,
            bundle, local-path, or unknown command) can NO LONGER be approved by
            the agent: whatever this is set to, the tool returns the install
            plan without running it and a human admin must confirm via the
            desktop app (Settings -> MCP) or POST /mcp-servers/install. Only
            low-risk installs run directly from the agent.
        config_values: Optional required config values. Sensitive values are
            encrypted with NYMERIA_SECRETS_KEY before saving.

    Returns:
        Human-readable summary including the server id, the discovered tool
        names, and the parsed command (for a quick visual sanity check before
        the next turn runs it).
    """
    from ..core.mcp_installer import MCPInstallError
    from ..core.mcp_auth_bridge import apply_mcp_auth_presets
    from ..core.mcp_runtime import (
        consume_preview,
        load_preview,
        make_failed_draft,
        plan_text_source,
        prepare_runtime,
    )
    from ..core.mcp_servers import get_mcp_server_registry
    from .utils import get_thread_id

    # Admin-only: installing an MCP server can launch arbitrary stdio commands
    # in the agent process. Resolve the caller via the injected RunnableConfig
    # and reject non-admin users (e.g. a second user) even if they enabled this tool.
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)
    try:
        from ..core.agent import get_current_agent
        _agent_ref = get_current_agent()
        if _agent_ref is not None:
            caller = _agent_ref.accounts_repo.get_user_by_id(user_id)
            if not caller or caller.role != "admin":
                return (
                    "[error] install_mcp_server requires admin role. "
                    "Ask the workspace admin to install the server via the desktop UI "
                    "(Settings → MCP) or `POST /mcp-servers/install`."
                )
    except Exception as e:
        logger.warning("install_mcp_server: admin check failed: %s", e)
        return f"[error] install_mcp_server: caller verification failed: {e}"

    try:
        if preview_token:
            _, defn, plan = load_preview(preview_token, candidate_id=candidate_id or None)
        else:
            defn, plan = plan_text_source(source, name=name)
    except MCPInstallError as e:
        return f"[error] could not parse source: {e}"
    except Exception as e:
        logger.exception("install_mcp_server parse failed")
        return f"[error] {type(e).__name__}: {e}"

    # A high-risk install must be confirmed by a HUMAN out of band, never by the
    # acting agent. `confirmed`/`confirmed_risk_ids` are LLM-supplied tool args,
    # so honoring them here would let a prompt-injected admin thread self-approve
    # an arbitrary stdio launch (the role gate only ever blocks a NON-admin, not
    # the acting agent). For a confirmation-required plan we therefore ALWAYS
    # return the plan without running it and route the human to the authenticated
    # REST/UI approve path (POST /mcp-servers/install or Settings -> MCP), whose
    # `confirmed` comes from an admin's own request rather than the model. This
    # holds regardless of `nymeria_allow_unsandboxed_mcp_install`. Low-risk
    # installs (confirmation_required=False) still run directly from the agent.
    if plan.confirmation_required:
        return json.dumps(
            {
                "requires_confirmation": True,
                "message": (
                    "This MCP install is high-risk and cannot be approved by the "
                    "agent itself. A human admin must review and confirm it in the "
                    "desktop app (Settings -> MCP) or via POST /mcp-servers/install."
                ),
                "server_id": defn.id,
                "plan": plan.to_dict(),
                "risk_signals": plan.risk_signals,
            },
            indent=2,
        )

    registry = get_mcp_server_registry()
    if registry.get_server(defn.id):
        return f"[error] MCP server '{defn.id}' already exists"

    logs = []
    try:
        defn.install_status = "preparing"
        registry.save_server(defn)
        defn, logs = prepare_runtime(
            defn,
            plan,
            config_values=config_values or {},
            user_id=user_id,
            log_sink=logs,
        )
        defn = apply_mcp_auth_presets(defn, user_id=user_id, log_sink=logs)
    except Exception as e:
        failed = make_failed_draft(defn, plan, str(e), logs)
        if failed.missing_config:
            failed.install_status = "needs_config"
        registry.save_server(failed)
        logger.warning("install_mcp_server setup failed for %s: %s", defn.id, e)
        return (
            f"[draft] MCP server id={defn.id} was saved disabled because setup failed.\n"
            f"Parsed as: {plan.parsed_summary}\n"
            f"Cause: {e}"
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
        return (
            f"[draft] MCP server id={defn.id} was saved disabled because discovery failed.\n"
            f"Parsed as: {plan.parsed_summary}\n"
            f"Cause: {e}"
        )

    agent = current_agent()
    if agent is not None and hasattr(agent, "reload_mcp_server_tools"):
        try:
            agent.reload_mcp_server_tools()
        except Exception as e:
            logger.warning("install_mcp_server: reload_mcp_server_tools failed: %s", e)

    defn = registry.get_server(defn.id) or defn
    defn.install_status = "ready"
    defn.last_error = None
    defn.registered_tool_names = registered_mcp_tool_names(defn, discovered)
    registry.save_server(defn)
    if preview_token:
        consume_preview(preview_token)

    tool_names = registered_mcp_tool_names(defn, discovered)
    tool_display_names = display_mcp_tool_names(defn, discovered)
    install_text = (
        f"Installed MCP server id={defn.id}\n"
        f"Parsed as: {plan.parsed_summary}\n"
        f"Discovered {len(discovered)} tool(s): {', '.join(tool_display_names) or '(none)'}\n"
    )
    if not tool_names:
        return install_text + "No tools were discovered."
    if not auto_enable_thread or not auto_enable:
        return (
            install_text
            + "Server tools are installed but not enabled on this thread."
        )

    binding = bind_tools_for_thread(
        tool_names,
        "",
        thread_id,
        user_id,
        ttl=ttl,
        strict=False,
        source="mcp_install",
        reason=f"installed MCP server {defn.id}",
    )
    result_text = (
        install_text
        + "\n\nThread binding result:\n"
        + binding.text
    )
    return command_or_text(
        result_text,
        bool(binding.reload_tools and not binding.cap_hit),
        tool_call_id,
        binding.reload_tools,
        thread_id=thread_id,
    )


@tool
def install_mcp_server(
    source: str,
    name: Optional[str] = None,
    preview_token: str = "",
    candidate_id: str = "",
    auto_enable: bool = True,
    confirmed: bool = False,
    confirmed_risk_ids: Optional[list[str]] = None,
    config_values: Optional[Dict[str, str]] = None,
    ttl: str = DEFAULT_TTL,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> Union[str, Command]:
    """Compatibility wrapper for installing one MCP server.

    Prefer ``manage_mcp(action="install", ...)`` for new agent-facing flows.

    Returns:
        Same as manage_mcp(action="install"). Human-readable summary on
        success; may trigger "[Tool reload queued - STOP NOW]" and
        graph force-end when new tools are loaded. JSON
        {requires_confirmation, plan} when unconfirmed.
        Errors: "[error] ...".
    """
    return _install_mcp_server_impl(
        source=source,
        name=name,
        preview_token=preview_token,
        candidate_id=candidate_id,
        auto_enable=auto_enable,
        confirmed=confirmed,
        confirmed_risk_ids=confirmed_risk_ids,
        config_values=config_values,
        ttl=ttl,
        auto_enable_thread=True,
        tool_call_id=tool_call_id,
        config=config,
    )


def _find_server(server_id_or_name: str):
    from ..core.mcp_servers import get_mcp_server_registry

    registry = get_mcp_server_registry()
    key = (server_id_or_name or "").strip().lower()
    if not key:
        return registry, None
    server = registry.get_server(server_id_or_name)
    if server:
        return registry, server
    for candidate in registry.get_all_servers():
        if candidate.id.lower() == key or candidate.name.lower() == key:
            return registry, candidate
    return registry, None


def _reload_mcp_agent_tools() -> None:
    agent = current_agent()
    if agent is not None and hasattr(agent, "reload_mcp_server_tools"):
        agent.reload_mcp_server_tools()


def _mcp_logs(server_id_or_name: str) -> str:
    _registry, server = _find_server(server_id_or_name)
    if server is None:
        return json_result(error="server_id or name is required/found")
    return json_result(
        id=server.id,
        install_status=server.install_status,
        last_error=server.last_error,
        missing_config=server.missing_config,
        install_logs=server.install_logs[-80:],
    )


def _mcp_test(server_id_or_name: str) -> str:
    registry, server = _find_server(server_id_or_name)
    if server is None:
        return json_result(error="server_id or name is required/found")
    return json_result(**registry.test_connection(server.id))


def _mcp_discover(server_id_or_name: str) -> str:
    registry, server = _find_server(server_id_or_name)
    if server is None:
        return json_result(error="server_id or name is required/found")
    try:
        tools = registry.discover_tools(server.id)
        server = registry.get_server(server.id) or server
        server.install_status = "ready"
        server.last_error = None
        server.registered_tool_names = registered_mcp_tool_names(server, tools)
        registry.save_server(server)
        _reload_mcp_agent_tools()
        return json_result(
            status="ok",
            server_id=server.id,
            count=len(tools),
            tool_names=server.registered_tool_names,
            tool_display_names=display_mcp_tool_names(server, tools),
        )
    except Exception as e:
        server.install_status = "failed"
        server.enabled = False
        server.last_error = str(e)
        registry.save_server(server)
        _reload_mcp_agent_tools()
        return json_result(error=f"Discovery failed: {e}", server_id=server.id)


def _mcp_retry(
    server_id_or_name: str,
    *,
    confirmed: bool,
    confirmed_risk_ids: Optional[list[str]],
    config_values: Optional[Dict[str, str]],
    user_id: str,
) -> str:
    registry, server = _find_server(server_id_or_name)
    if server is None:
        return json_result(error="server_id or name is required/found")
    if not server.install_plan:
        return json_result(error="server has no install plan to retry", server_id=server.id)
    from ..core.mcp_runtime import MCPInstallPlan, make_failed_draft, prepare_runtime
    from ..core.mcp_auth_bridge import apply_mcp_auth_presets

    plan = MCPInstallPlan.from_dict(server.install_plan)
    # Same rule as the install path: a high-risk (confirmation-required) retry
    # cannot be approved by the agent. `confirmed`/`confirmed_risk_ids` are
    # LLM-supplied, so honoring them would let a prompt-injected admin thread
    # re-fire a high-risk launch a human authored once. Route the human to the
    # authenticated REST retry (POST /mcp-servers/{id}/retry), whose `confirmed`
    # comes from an admin's own request.
    if plan.confirmation_required:
        return json_result(
            requires_confirmation=True,
            server_id=server.id,
            plan=plan.to_dict(),
            message=(
                "This retry is high-risk and cannot be approved by the agent. "
                "A human admin must confirm it in the desktop app (Settings -> "
                "MCP) or via POST /mcp-servers/{id}/retry."
            ),
        )

    logs: list[str] = []
    try:
        server.install_status = "preparing"
        registry.save_server(server)
        server, logs = prepare_runtime(
            server,
            plan,
            config_values=config_values or {},
            user_id=user_id,
            log_sink=logs,
        )
        server = apply_mcp_auth_presets(server, user_id=user_id, log_sink=logs)
        server.install_status = "discovering"
        server.enabled = True
        server.install_logs = logs
        registry.save_server(server)
        tools = registry.discover_tools(server.id)
    except Exception as e:
        failed = make_failed_draft(server, plan, str(e), logs)
        if failed.missing_config:
            failed.install_status = "needs_config"
        registry.save_server(failed)
        _reload_mcp_agent_tools()
        return json_result(status=failed.install_status, server_id=failed.id, error=str(e))

    server = registry.get_server(server.id) or server
    server.install_status = "ready"
    server.last_error = None
    server.registered_tool_names = registered_mcp_tool_names(server, tools)
    registry.save_server(server)
    _reload_mcp_agent_tools()
    return json_result(
        status="ok",
        server_id=server.id,
        tool_names=server.registered_tool_names,
        tool_display_names=display_mcp_tool_names(server, tools),
    )


def _mcp_set_enabled(server_id_or_name: str, *, enabled: bool) -> str:
    registry, server = _find_server(server_id_or_name)
    if server is None:
        return json_result(error="server_id or name is required/found")
    server.enabled = enabled
    if not enabled:
        server.install_status = "disabled"
    elif server.install_status == "disabled":
        server.install_status = "ready" if server.discovered_tools else "needs_config"
    registry.save_server(server)
    _reload_mcp_agent_tools()
    return json_result(status="ok", server_id=server.id, enabled=server.enabled)


def _mcp_delete(server_id_or_name: str) -> str:
    registry, server = _find_server(server_id_or_name)
    if server is None:
        return json_result(error="server_id or name is required/found")
    deleted = registry.delete_server(server.id)
    _reload_mcp_agent_tools()
    return json_result(status="ok" if deleted else "missing", deleted=server.id)


def _mcp_configure_credentials(
    server_id_or_name: str,
    *,
    config_values: Optional[Dict[str, str]],
    user_id: str,
) -> str:
    registry, server = _find_server(server_id_or_name)
    if server is None:
        return json_result(error="server_id or name is required/found")
    # The agent cannot receive plaintext secrets. This action creates or
    # refreshes pending vault records that the user can complete in Settings.
    from ..core.mcp_runtime import MCPInstallPlan, apply_config_values

    plan = MCPInstallPlan.from_dict(server.install_plan or {})
    _server, missing = apply_config_values(
        server,
        plan,
        config_values=config_values or {},
        user_id=user_id,
    )
    server.missing_config = missing or server.missing_config
    server.install_status = "needs_config" if server.missing_config else server.install_status
    registry.save_server(server)
    return json_result(
        status=server.install_status,
        server_id=server.id,
        missing_config=server.missing_config,
        message="Pending MCP credential records are available in Settings > Connections.",
    )


@tool("manage_mcp")
def mcp_manage(
    action: str,
    query: str = "",
    source: str = "",
    name: str = "",
    server_id: str = "",
    preview_token: str = "",
    candidate_id: str = "",
    confirmed: bool = False,
    confirmed_risk_ids: Optional[list[str]] = None,
    config_values: Optional[Dict[str, str]] = None,
    ttl: str = DEFAULT_TTL,
    auto_enable_thread: bool = True,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> Union[str, Command]:
    """Search, preview, install, and inspect MCP servers.

    Args:
        action: One of: search, preview, install, inspect, logs, test, discover,
            retry, disable, enable, delete, configure_credentials.
        query: Search query for public MCP registries.
        source: Install source or config snippet for preview/install.
        name: Optional server display name, or server id/name for inspect.
        confirmed: Required for higher-risk installs.
        config_values: Required config values for install.
        ttl: TTL for discovered tools enabled on this thread. Format:
            Nm/Nh/Nd/Nw or "never"/"permanent". Default "2h".
        auto_enable_thread: Enable discovered server tools on this thread.

    Returns:
        search: JSON {count, results: [{id, name, description, source,
        install_hint}]}.
        preview: JSON {server: {id, name, description, transport}, plan}.
        install: human-readable summary with server id, discovered tools,
        and thread binding result. When new tools are loaded, includes
        "[Tool reload queued - STOP NOW]" and the graph is force-ended
        for rebuild — do not respond after this. JSON
        {requires_confirmation, plan} when confirmed=False.
        inspect: JSON {count, servers: [{id, name, enabled, transport,
        discovered_tools, missing_config}]}.
        Errors: JSON {error: "..."}.
    """
    action_key = (action or "").strip().lower()
    if action_key == "search":
        return cast(Any, search_mcp).func(query=query or source or name, top_k=10)
    if action_key == "preview":
        return _preview_mcp_source(source=source, name=name or None)
    admin_actions = {
        "install",
        "discover",
        "rediscover",
        "retry",
        "disable",
        "enable",
        "delete",
        "configure_credentials",
        "test",
    }
    if action_key in admin_actions and not _caller_is_admin(config):
        return json_result(error="manage_mcp lifecycle actions require admin role")
    if action_key == "install":
        return _install_mcp_server_impl(
            source=source or query,
            name=name or None,
            preview_token=preview_token,
            candidate_id=candidate_id,
            auto_enable=True,
            confirmed=confirmed,
            confirmed_risk_ids=confirmed_risk_ids,
            config_values=config_values,
            ttl=ttl,
            auto_enable_thread=auto_enable_thread,
            tool_call_id=tool_call_id,
            config=config,
        )
    if action_key in {"inspect", "status", "list"}:
        return _inspect_mcp(name=server_id or name)
    if action_key == "logs":
        return _mcp_logs(server_id or name)
    if action_key == "test":
        return _mcp_test(server_id or name)
    if action_key in {"discover", "rediscover"}:
        return _mcp_discover(server_id or name)
    if action_key == "retry":
        return _mcp_retry(
            server_id or name,
            confirmed=confirmed,
            confirmed_risk_ids=confirmed_risk_ids,
            config_values=config_values,
            user_id=get_user_id(config),
        )
    if action_key in {"disable", "enable"}:
        return _mcp_set_enabled(server_id or name, enabled=action_key == "enable")
    if action_key == "delete":
        return _mcp_delete(server_id or name)
    if action_key == "configure_credentials":
        return _mcp_configure_credentials(
            server_id or name,
            config_values=config_values,
            user_id=get_user_id(config),
        )
    return json_result(
        error=(
            "action must be one of: search, preview, install, inspect, logs, "
            "test, discover, retry, disable, enable, delete, configure_credentials"
        )
    )


SEARCH_MCP_TOOLS = [mcp_manage, search_mcp, install_mcp_server]

__all__ = ["mcp_manage", "search_mcp", "install_mcp_server", "SEARCH_MCP_TOOLS"]
