"""
Nymeria MCP Server - thin client over the Nymeria REST/SSE API.

The MCP process intentionally owns no agent, checkpointer, TODO manager, or
profile state.  It authenticates to the running API with NYMERIA_SERVICE_TOKEN
and uses X-Nymeria-Act-As for user-scoped operations.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP

from .mcp_backend_client import (
    NymeriaAPIError,
    NymeriaBackendClient,
    collect_chat_transcript,
    message_steps_to_markdown,
    message_steps_to_response_text,
)

# Configure logging to stderr to avoid corrupting JSON-RPC in STDIO mode.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


mcp = FastMCP(
    "nymeria",
    instructions=(
        "Nymeria Personal AI Assistant thin client. Tools call the Nymeria "
        "backend API for chat, thread management, configuration, TODOs, "
        "triggers, memories, and RAG search."
    ),
    stateless_http=True,
)

_backend_url_override: Optional[str] = None
_client: Optional[NymeriaBackendClient] = None


def _resolve_api_url(api_url: Optional[str] = None) -> str:
    """Resolve the Nymeria API URL for local and Docker runs."""
    explicit = api_url or _backend_url_override or os.environ.get("NYMERIA_API_URL")
    if explicit:
        return explicit.rstrip("/")
    if Path("/.dockerenv").exists():
        return "http://nymeria-api:8000"
    return "http://localhost:8000"


def configure_backend(api_url: Optional[str] = None) -> None:
    """Configure the API URL used by subsequent MCP tool calls."""
    global _backend_url_override, _client
    _backend_url_override = api_url.rstrip("/") if api_url else None
    _client = None


def _get_client() -> NymeriaBackendClient:
    """Get a lazily-created backend client."""
    global _client
    from nymeria.config import get_settings

    settings = get_settings()
    service_token = settings.nymeria_service_token
    if not service_token:
        raise RuntimeError(
            "NYMERIA_SERVICE_TOKEN is required for the MCP thin client. "
            "Create an admin service account token and set it in the API/MCP environment."
        )

    base_url = _resolve_api_url()
    if _client is None or _client.base_url != base_url or _client.service_token != service_token:
        logger.info("Configuring Nymeria MCP backend client: api=%s", base_url)
        _client = NymeriaBackendClient(base_url=base_url, service_token=service_token)
    return _client


def _enc(value: str) -> str:
    return quote(str(value), safe="")


def _clean(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


def _error_result(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, NymeriaAPIError):
        return exc.as_dict()
    logger.error("Nymeria MCP tool error: %s", exc, exc_info=True)
    return {"error": str(exc), "type": type(exc).__name__}


def _collection_result(key: str, result: Any, **metadata: Any) -> Dict[str, Any]:
    """Wrap backend list responses in a stable MCP-friendly object shape."""
    if isinstance(result, dict):
        return result
    if isinstance(result, list):
        return _clean({key: result, "total": len(result), **metadata})
    return _clean({key: result, **metadata})


async def _call(awaitable) -> Any:
    try:
        return await awaitable
    except Exception as exc:
        return _error_result(exc)


async def _json_call(method: str, path: str, *, user_id: Optional[str] = None, body: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Any:
    client = _get_client()
    if method == "GET":
        return await _call(client.get(path, params=params, act_as=user_id))
    if method == "POST":
        return await _call(client.post(path, json_body=body, params=params, act_as=user_id))
    if method == "PATCH":
        return await _call(client.patch(path, json_body=body, params=params, act_as=user_id))
    if method == "PUT":
        return await _call(client.put(path, json_body=body, params=params, act_as=user_id))
    if method == "DELETE":
        return await _call(client.delete(path, params=params, act_as=user_id))
    return {"error": f"Unsupported method: {method}"}


# =============================================================================
# System / Auth
# =============================================================================


@mcp.tool()
async def nymeria_health() -> Dict[str, Any]:
    """Check backend health and report the MCP thin-client target URL."""
    result = await _json_call("GET", "/health")
    if isinstance(result, dict):
        result.setdefault("api_url", _resolve_api_url())
    return result


@mcp.tool()
async def nymeria_get_me(user_id: str = "default") -> Dict[str, Any]:
    """Return the backend identity MCP resolves to for a user via Act-As."""
    return await _json_call("GET", "/me", user_id=user_id)


# =============================================================================
# Chat
# =============================================================================


@mcp.tool()
async def nymeria_chat(
    message: str,
    user_id: str = "default",
    thread_id: Optional[str] = None,
    include_events: bool = False,
    attachments: Optional[List[Dict[str, Any]]] = None,
    force_unsupported_attachments: bool = False,
) -> Dict[str, Any]:
    """
    Send a message to Nymeria and return a full desktop-style transcript.

    The response includes ordered thinking, preamble response chunks, tool call
    arguments, tool results, final response text, markdown suitable for copying,
    context stats, and model metadata.
    """
    if not message or not message.strip():
        return {"error": "message is required"}
    try:
        return await collect_chat_transcript(
            _get_client(),
            message=message,
            user_id=user_id,
            thread_id=thread_id,
            attachments=attachments,
            force_unsupported_attachments=force_unsupported_attachments,
            include_events=include_events,
        )
    except Exception as exc:
        return _error_result(exc)


# =============================================================================
# Thread Management
# =============================================================================


@mcp.tool()
async def nymeria_list_threads(user_id: str = "default") -> Dict[str, Any]:
    """List threads visible to a user, including metadata."""
    return await _json_call("GET", "/threads", user_id=user_id)


@mcp.tool()
async def nymeria_claim_thread(thread_id: str, user_id: str = "default") -> Dict[str, Any]:
    """Claim ownership of a locally-created thread id for a user."""
    return await _json_call("POST", f"/threads/{_enc(thread_id)}/claim", user_id=user_id)


@mcp.tool()
async def nymeria_update_thread_metadata(
    thread_id: str,
    user_id: str = "default",
    title: Optional[str] = None,
    pinned: Optional[bool] = None,
) -> Dict[str, Any]:
    """Update a thread title and/or pinned state."""
    body = _clean({"title": title, "pinned": pinned})
    if not body:
        return {"error": "Provide title and/or pinned"}
    return await _json_call("PATCH", f"/threads/{_enc(thread_id)}/metadata", user_id=user_id, body=body)


@mcp.tool()
async def nymeria_get_thread_history(
    thread_id: str,
    user_id: str = "default",
    include_internal: bool = False,
    limit: Optional[int] = None,
    include_markdown: bool = True,
) -> Dict[str, Any]:
    """Get thread history, optionally adding assistant markdown/final text."""
    result = await _json_call(
        "GET",
        f"/threads/{_enc(thread_id)}/history",
        user_id=user_id,
        params={"include_internal": str(include_internal).lower()},
    )
    if not isinstance(result, dict) or "messages" not in result:
        return result

    messages = result.get("messages") or []
    if limit is not None:
        limit = max(1, min(200, int(limit)))
        messages = messages[-limit:]
        result["messages"] = messages

    if include_markdown:
        for msg in messages:
            if msg.get("role") != "assistant":
                continue
            steps = msg.get("steps") or []
            if isinstance(steps, list) and steps:
                msg["full_markdown"] = message_steps_to_markdown(steps)
                msg["final_response"] = message_steps_to_response_text(steps)
    return result


@mcp.tool()
async def nymeria_thread_history(
    thread_id: str,
    user_id: str = "default",
    limit: int = 20,
    include_internal: bool = False,
) -> Dict[str, Any]:
    """Backward-compatible alias for nymeria_get_thread_history."""
    return await nymeria_get_thread_history(
        thread_id=thread_id,
        user_id=user_id,
        include_internal=include_internal,
        limit=limit,
        include_markdown=True,
    )


@mcp.tool()
async def nymeria_get_thread_context(thread_id: str, user_id: str = "default") -> Dict[str, Any]:
    """Get context-window statistics for a thread."""
    return await _json_call("GET", f"/threads/{_enc(thread_id)}/context", user_id=user_id)


@mcp.tool()
async def nymeria_clear_thread(thread_id: str, user_id: str = "default") -> Dict[str, Any]:
    """Clear conversation history while preserving thread config and notepad."""
    return await _json_call("POST", f"/threads/{_enc(thread_id)}/clear", user_id=user_id)


@mcp.tool()
async def nymeria_delete_thread(thread_id: str, user_id: str = "default") -> Dict[str, Any]:
    """Fully delete a thread and its associated resources."""
    return await _json_call("DELETE", f"/threads/{_enc(thread_id)}", user_id=user_id)


@mcp.tool()
async def nymeria_stop_thread(thread_id: str, user_id: str = "default") -> Dict[str, Any]:
    """Request cancellation for the currently-running operation on a thread."""
    return await _json_call("POST", f"/threads/{_enc(thread_id)}/stop", user_id=user_id)


@mcp.tool()
async def nymeria_compact_thread(thread_id: str, user_id: str = "default") -> Dict[str, Any]:
    """Manually compact a thread's conversation context."""
    return await _json_call("POST", f"/threads/{_enc(thread_id)}/compact", user_id=user_id)


# =============================================================================
# Thread Configuration
# =============================================================================


@mcp.tool()
async def nymeria_get_thread_config(thread_id: str, user_id: str = "default") -> Dict[str, Any]:
    """Get per-thread config: instructions, tools, skills, LLM overrides, and delivery settings."""
    return await _json_call("GET", f"/threads/{_enc(thread_id)}/config", user_id=user_id)


@mcp.tool()
async def nymeria_update_thread_config(
    thread_id: str,
    updates: Dict[str, Any],
    user_id: str = "default",
) -> Dict[str, Any]:
    """
    Patch per-thread config.

    Pass backend-shaped fields such as instructions, enabled_tools,
    disabled_tools, llm_config, system_prompt, callable, enabled_skills,
    inject_todos_in_prompt, and clear_* flags.
    """
    if not updates:
        return {"error": "updates is required"}
    return await _json_call("PATCH", f"/threads/{_enc(thread_id)}/config", user_id=user_id, body=updates)


@mcp.tool()
async def nymeria_reset_thread_config(thread_id: str, user_id: str = "default") -> Dict[str, Any]:
    """Delete per-thread config and return the thread to global defaults."""
    return await _json_call("DELETE", f"/threads/{_enc(thread_id)}/config", user_id=user_id)


# =============================================================================
# Global Settings
# =============================================================================


@mcp.tool()
async def nymeria_get_settings() -> Dict[str, Any]:
    """Get global backend settings."""
    return await _json_call("GET", "/settings")


@mcp.tool()
async def nymeria_update_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    """
    Patch global backend settings.

    This is admin/global scope and uses the MCP service token directly.
    """
    if not updates:
        return {"error": "updates is required"}
    return await _json_call("PATCH", "/settings", body=updates)


@mcp.tool()
async def nymeria_get_llm_runtime() -> Dict[str, Any]:
    """Get runtime LLM diagnostics for the effective global model/provider."""
    return await _json_call("GET", "/settings/llm/runtime")


# =============================================================================
# TODOs
# =============================================================================


@mcp.tool()
async def nymeria_todo_list(
    user_id: str = "default",
    status: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    """List TODOs for a user, optionally filtered by status and thread."""
    return await _json_call(
        "GET",
        "/todos",
        user_id=user_id,
        params={"filter_status": status, "thread_id": thread_id},
    )


@mcp.tool()
async def nymeria_todo_add(
    task: str,
    user_id: str = "default",
    notes: Optional[str] = None,
    scheduled_for: Optional[str] = None,
    recurrence: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a TODO, optionally scheduled/recurring and bound to a thread."""
    if not task:
        return {"error": "task is required"}
    body = _clean(
        {
            "task": task,
            "notes": notes,
            "scheduled_for": scheduled_for,
            "recurrence": recurrence,
            "thread_id": thread_id,
        }
    )
    return await _json_call("POST", "/todos", user_id=user_id, body=body)


@mcp.tool()
async def nymeria_todo_update(
    todo_id: str,
    user_id: str = "default",
    task: Optional[str] = None,
    status: Optional[str] = None,
    notes: Optional[str] = None,
    scheduled_for: Optional[str] = None,
    recurrence: Optional[str] = None,
    thread_id: Optional[str] = None,
    clear_schedule: bool = False,
    clear_recurrence: bool = False,
) -> Dict[str, Any]:
    """Patch TODO fields."""
    body = _clean(
        {
            "task": task,
            "status": status,
            "notes": notes,
            "scheduled_for": scheduled_for,
            "recurrence": recurrence,
            "thread_id": thread_id,
            "clear_schedule": clear_schedule,
            "clear_recurrence": clear_recurrence,
        }
    )
    return await _json_call("PATCH", f"/todos/{_enc(todo_id)}", user_id=user_id, body=body)


@mcp.tool()
async def nymeria_todo_complete(todo_id: str, user_id: str = "default") -> Dict[str, Any]:
    """Mark a TODO complete."""
    return await _json_call("POST", f"/todos/{_enc(todo_id)}/complete", user_id=user_id)


@mcp.tool()
async def nymeria_todo_delete(todo_id: str, user_id: str = "default") -> Dict[str, Any]:
    """Delete a TODO permanently."""
    return await _json_call("DELETE", f"/todos/{_enc(todo_id)}", user_id=user_id)


# =============================================================================
# Triggers
# =============================================================================


@mcp.tool()
async def nymeria_list_trigger_sources(user_id: str = "default") -> Dict[str, Any]:
    """List available trigger source plugins and their schemas."""
    return await _json_call("GET", "/triggers/sources/list", user_id=user_id)


@mcp.tool()
async def nymeria_list_triggers(
    user_id: str = "default",
    enabled_only: bool = False,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    """List triggers for a user."""
    result = await _json_call(
        "GET",
        "/triggers",
        user_id=user_id,
        params={"enabled_only": str(enabled_only).lower(), "thread_id": thread_id},
    )
    return _collection_result(
        "triggers",
        result,
        enabled_only=enabled_only,
        thread_id=thread_id,
    )


@mcp.tool()
async def nymeria_create_trigger(
    name: str,
    source_type: str,
    action_type: str,
    user_id: str = "default",
    source_config: Optional[Dict[str, Any]] = None,
    action_config: Optional[Dict[str, Any]] = None,
    conditions: Optional[List[Dict[str, Any]]] = None,
    cooldown_seconds: int = 0,
    enabled: bool = True,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a trigger."""
    body = {
        "name": name,
        "source_type": source_type,
        "source_config": source_config or {},
        "action_type": action_type,
        "action_config": action_config or {},
        "conditions": conditions or [],
        "cooldown_seconds": cooldown_seconds,
        "enabled": enabled,
        "thread_id": thread_id,
    }
    return await _json_call("POST", "/triggers", user_id=user_id, body=_clean(body))


@mcp.tool()
async def nymeria_update_trigger(
    trigger_id: str,
    updates: Dict[str, Any],
    user_id: str = "default",
) -> Dict[str, Any]:
    """Patch a trigger. Pass backend-shaped trigger fields in updates."""
    if not updates:
        return {"error": "updates is required"}
    return await _json_call("PATCH", f"/triggers/{_enc(trigger_id)}", user_id=user_id, body=updates)


@mcp.tool()
async def nymeria_delete_trigger(trigger_id: str, user_id: str = "default") -> Dict[str, Any]:
    """Delete a trigger."""
    return await _json_call("DELETE", f"/triggers/{_enc(trigger_id)}", user_id=user_id)


@mcp.tool()
async def nymeria_test_trigger(trigger_id: str, user_id: str = "default") -> Dict[str, Any]:
    """Dry-run a trigger against sample source data without firing it."""
    return await _json_call("POST", f"/triggers/{_enc(trigger_id)}/test", user_id=user_id)


@mcp.tool()
async def nymeria_get_trigger_executions(
    trigger_id: str,
    user_id: str = "default",
    limit: int = 50,
) -> Dict[str, Any]:
    """Get execution history for a trigger."""
    result = await _json_call(
        "GET",
        f"/triggers/{_enc(trigger_id)}/executions",
        user_id=user_id,
        params={"limit": max(1, min(200, int(limit)))},
    )
    return _collection_result("executions", result, trigger_id=trigger_id)


@mcp.tool()
async def nymeria_get_recent_trigger_executions(user_id: str = "default", limit: int = 50) -> Dict[str, Any]:
    """Get recent trigger executions across all triggers."""
    result = await _json_call(
        "GET",
        "/triggers/executions/recent",
        user_id=user_id,
        params={"limit": max(1, min(200, int(limit)))},
    )
    return _collection_result("executions", result)


# =============================================================================
# Profile / Memory / RAG
# =============================================================================


@mcp.tool()
async def nymeria_profile_list(user_id: str = "default") -> Dict[str, Any]:
    """List memories stored for a user."""
    return await _json_call("GET", f"/users/{_enc(user_id)}/memories", user_id=user_id)


@mcp.tool()
async def nymeria_profile_save(key: str, value: str, user_id: str = "default") -> Dict[str, Any]:
    """Save or update a memory for a user."""
    if not key or not value:
        return {"error": "key and value are required"}
    return await _json_call(
        "POST",
        f"/users/{_enc(user_id)}/memories",
        user_id=user_id,
        body={"key": key, "value": value},
    )


@mcp.tool()
async def nymeria_profile_forget(key: str, user_id: str = "default") -> Dict[str, Any]:
    """Remove a memory from a user profile."""
    if not key:
        return {"error": "key is required"}
    return await _json_call("DELETE", f"/users/{_enc(user_id)}/memories/{_enc(key)}", user_id=user_id)


@mcp.tool()
async def nymeria_rag_search(query: str, max_results: int = 5, user_id: str = "default") -> Dict[str, Any]:
    """Search the user's RAG index through the backend API."""
    if not query:
        return {"error": "query is required"}
    return await _json_call(
        "GET",
        f"/users/{_enc(user_id)}/rag/search",
        user_id=user_id,
        params={"q": query, "max_results": max(1, min(10, int(max_results)))},
    )


# =============================================================================
# Server Entry Points
# =============================================================================


def run_stdio(api_url: Optional[str] = None) -> None:
    """Run the MCP server in STDIO mode."""
    configure_backend(api_url)
    logger.info("Starting Nymeria MCP server in STDIO mode; api=%s", _resolve_api_url())
    mcp.run()


def run_http(host: str = "127.0.0.1", port: int = 8001, api_url: Optional[str] = None) -> None:
    """Run the MCP server in streamable HTTP mode."""
    configure_backend(api_url)
    logger.info("Starting Nymeria MCP server in HTTP mode on %s:%s; api=%s", host, port, _resolve_api_url())
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    run_stdio()
