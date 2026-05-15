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
from typing import Annotated, Dict, Optional, Union

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, InjectedToolCallId, tool
from langgraph.types import Command

from ..core.tool_reload import tool_reload_command
from .tool_search import DEFAULT_TTL, bind_tools_for_thread
from .utils import get_user_id

logger = logging.getLogger(__name__)


def _agent():
    """Lazy import to avoid a tool->agent circular import at module load."""
    from ..core.agent import get_current_agent
    return get_current_agent()


def _json_result(**payload) -> str:
    return json.dumps(payload, indent=2, default=str)


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
                "discovered_tools": [
                    f"mcp__{server.id}__{tool_def.name}"
                    for tool_def in server.discovered_tools
                ],
                "missing_config": server.missing_config,
            }
        )
    return _json_result(count=len(payload), servers=payload)


def _preview_mcp_source(source: str, name: Optional[str] = None) -> str:
    from ..core.mcp_installer import MCPInstallError
    from ..core.mcp_runtime import plan_text_source

    if not source:
        return _json_result(error="source is required for preview")
    try:
        defn, plan = plan_text_source(source, name=name)
    except MCPInstallError as e:
        return _json_result(error=f"could not parse source: {e}")
    except Exception as e:
        logger.exception("manage_mcp preview failed")
        return _json_result(error=f"{type(e).__name__}: {e}")

    return _json_result(
        server={
            "id": defn.id,
            "name": defn.name,
            "description": defn.description,
            "transport": defn.transport,
        },
        plan=plan.to_dict(),
    )


def _command_or_text(text: str, queued_reload: bool, tool_call_id: Optional[str]) -> Union[str, Command]:
    if queued_reload and tool_call_id:
        return tool_reload_command(text, tool_call_id)
    return text


def _install_mcp_server_impl(
    source: str,
    name: Optional[str] = None,
    auto_enable: bool = True,
    confirmed: bool = False,
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
        confirmed: Required for Git, bundle, local-path, or unknown command
            installs. When False, the tool returns the install plan without
            running it.
        config_values: Optional required config values. Sensitive values are
            encrypted with NYMERIA_SECRETS_KEY before saving.

    Returns:
        Human-readable summary including the server id, the discovered tool
        names, and the parsed command (for a quick visual sanity check before
        the next turn runs it).
    """
    from ..core.mcp_installer import MCPInstallError
    from ..core.mcp_auth_bridge import apply_mcp_auth_presets
    from ..core.mcp_runtime import make_failed_draft, plan_text_source, prepare_runtime
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
        defn, plan = plan_text_source(source, name=name)
    except MCPInstallError as e:
        return f"[error] could not parse source: {e}"
    except Exception as e:
        logger.exception("install_mcp_server parse failed")
        return f"[error] {type(e).__name__}: {e}"

    if plan.confirmation_required and not confirmed:
        return json.dumps(
            {
                "requires_confirmation": True,
                "message": "This MCP install needs admin confirmation before Nymeria runs it.",
                "server_id": defn.id,
                "plan": plan.to_dict(),
            },
            indent=2,
        )

    registry = get_mcp_server_registry()
    if registry.get_server(defn.id):
        return f"[error] MCP server '{defn.id}' already exists"

    logs = []
    try:
        defn, logs = prepare_runtime(defn, plan, config_values=config_values or {}, log_sink=logs)
        defn = apply_mcp_auth_presets(defn, user_id=user_id, log_sink=logs)
    except Exception as e:
        failed = make_failed_draft(defn, plan, str(e), logs)
        registry.save_server(failed)
        logger.warning("install_mcp_server setup failed for %s: %s", defn.id, e)
        return (
            f"[draft] MCP server id={defn.id} was saved disabled because setup failed.\n"
            f"Parsed as: {plan.parsed_summary}\n"
            f"Cause: {e}"
        )

    defn.enabled = bool(auto_enable)
    defn.install_status = "ready"
    defn.last_error = None
    defn.install_logs = logs
    defn.install_plan = plan.to_dict()
    defn.missing_config = []
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

    agent = _agent()
    if agent is not None and hasattr(agent, "reload_mcp_server_tools"):
        try:
            agent.reload_mcp_server_tools()
        except Exception as e:
            logger.warning("install_mcp_server: reload_mcp_server_tools failed: %s", e)

    tool_names = [f"mcp__{defn.id}__{t.name}" for t in discovered]
    install_text = (
        f"Installed MCP server id={defn.id}\n"
        f"Parsed as: {plan.parsed_summary}\n"
        f"Discovered {len(discovered)} tool(s): {', '.join(tool_names) or '(none)'}\n"
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
    return _command_or_text(
        result_text,
        bool(binding.reload_tools and not binding.cap_hit),
        tool_call_id,
    )


@tool
def install_mcp_server(
    source: str,
    name: Optional[str] = None,
    auto_enable: bool = True,
    confirmed: bool = False,
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
        auto_enable=auto_enable,
        confirmed=confirmed,
        config_values=config_values,
        ttl=ttl,
        auto_enable_thread=True,
        tool_call_id=tool_call_id,
        config=config,
    )


@tool("manage_mcp")
def mcp_manage(
    action: str,
    query: str = "",
    source: str = "",
    name: str = "",
    confirmed: bool = False,
    config_values: Optional[Dict[str, str]] = None,
    ttl: str = DEFAULT_TTL,
    auto_enable_thread: bool = True,
    *,
    tool_call_id: Annotated[str, InjectedToolCallId],
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> Union[str, Command]:
    """Search, preview, install, and inspect MCP servers.

    Args:
        action: One of: search, preview, install, inspect.
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
        return search_mcp.func(query=query or source or name, top_k=10)
    if action_key == "preview":
        return _preview_mcp_source(source=source, name=name or None)
    if action_key == "install":
        return _install_mcp_server_impl(
            source=source or query,
            name=name or None,
            auto_enable=True,
            confirmed=confirmed,
            config_values=config_values,
            ttl=ttl,
            auto_enable_thread=auto_enable_thread,
            tool_call_id=tool_call_id,
            config=config,
        )
    if action_key in {"inspect", "status", "list"}:
        return _inspect_mcp(name=name)
    return _json_result(
        error="action must be one of: search, preview, install, inspect"
    )


SEARCH_MCP_TOOLS = [mcp_manage, search_mcp, install_mcp_server]

__all__ = ["mcp_manage", "search_mcp", "install_mcp_server", "SEARCH_MCP_TOOLS"]
