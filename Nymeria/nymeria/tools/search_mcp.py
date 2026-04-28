"""Agent-facing tools for discovering and installing MCP servers.

Mirrors the search_skills / install_skill pattern so the UX feels the same:
- mcp_search: find servers in the official MCP registry and Smithery
- mcp_install: preview/prepare a valid install source (Claude Desktop JSON,
  bare stdio command, HTTP URL, package page, Git URL, bundle URL, or registry
  id), save it, discover tools, and refresh the agent's tool registry.

Accept the reality of 2026 install surfaces: every assistant vendor formats
"here's how you install my MCP server" differently. The parser's job is to
smooth that over; these tools are just a thin @tool wrapper around it.
"""

from __future__ import annotations

import logging
import json
from typing import Annotated, Dict, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

logger = logging.getLogger(__name__)


def _agent():
    """Lazy import to avoid a tool->agent circular import at module load."""
    from ..core.agent import get_current_agent
    return get_current_agent()


@tool
def mcp_search(query: str, top_k: int = 10) -> str:
    """Search public MCP server registries for servers matching a query.

    Looks across the official MCP registry (registry.modelcontextprotocol.io)
    and Smithery. Results come back even if one of the two registries is down
    or rate-limiting us. Use this before mcp_install when the user says
    something like "find me an MCP server that can X" rather than handing
    over a specific install command.

    Args:
        query: Free-text search term (e.g. "filesystem", "postgres", "browser").
        top_k: Max results to return across both registries (default 10).

    Returns:
        JSON string with {count, results: [{id, name, description, source,
        install_hint}]}. The `install_hint` (when present) is exactly what
        mcp_install(source=...) accepts.
    """
    from ..core.mcp_registry_client import search_registries

    try:
        results = search_registries(query or "")
    except Exception as e:
        logger.exception("mcp_search failed")
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


@tool
def mcp_install(
    source: str,
    name: Optional[str] = None,
    auto_enable: bool = True,
    confirmed: bool = False,
    config_values: Optional[Dict[str, str]] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
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
    from ..core.mcp_runtime import make_failed_draft, plan_text_source, prepare_runtime
    from ..core.mcp_servers import get_mcp_server_registry

    # Admin-only: installing an MCP server can launch arbitrary stdio commands
    # in the agent process. Resolve the caller via the injected RunnableConfig
    # and reject non-admin users (e.g. a second user) even if they enabled this tool.
    user_id = (config or {}).get("configurable", {}).get("user_id", "default") if config else "default"
    try:
        from ..core.agent import get_current_agent
        _agent_ref = get_current_agent()
        if _agent_ref is not None:
            caller = _agent_ref.accounts_repo.get_user_by_id(user_id)
            if not caller or caller.role != "admin":
                return (
                    "[error] mcp_install requires admin role. "
                    "Ask the workspace admin to install the server via the desktop UI "
                    "(Settings → MCP) or `POST /mcp-servers/install`."
                )
    except Exception as e:
        logger.warning("mcp_install: admin check failed: %s", e)
        return f"[error] mcp_install: caller verification failed: {e}"

    try:
        defn, plan = plan_text_source(source, name=name)
    except MCPInstallError as e:
        return f"[error] could not parse source: {e}"
    except Exception as e:
        logger.exception("mcp_install parse failed")
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
    except Exception as e:
        failed = make_failed_draft(defn, plan, str(e), logs)
        registry.save_server(failed)
        logger.warning("mcp_install setup failed for %s: %s", defn.id, e)
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
        logger.warning("mcp_install discovery failed for %s: %s", defn.id, e)
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
            logger.warning("mcp_install: reload_mcp_server_tools failed: %s", e)

    tool_names = [f"mcp__{defn.id}__{t.name}" for t in discovered]
    return (
        f"Installed MCP server id={defn.id}\n"
        f"Parsed as: {plan.parsed_summary}\n"
        f"Discovered {len(discovered)} tool(s): {', '.join(tool_names) or '(none)'}\n"
        f"They will be callable on the next message."
    )


SEARCH_MCP_TOOLS = [mcp_search, mcp_install]

__all__ = ["mcp_search", "mcp_install", "SEARCH_MCP_TOOLS"]
