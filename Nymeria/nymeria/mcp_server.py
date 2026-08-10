"""
Nymeria MCP Server - thin client over the Nymeria REST/SSE API.

The MCP process intentionally owns no agent, checkpointer, TODO manager, or
profile state.  It authenticates to the running API with NYMERIA_SERVICE_TOKEN
and uses X-Nymeria-Act-As for user-scoped operations.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP

from .mcp_auth import MCPAuthMiddleware, effective_act_as
from .mcp_backend_client import (
    NymeriaAPIError,
    NymeriaBackendClient,
    collect_chat_transcript,
    normalize_transcript_verbosity,
    project_history_message_for_verbosity,
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
        "backend API for chat, slash commands, thread management, "
        "configuration, TODOs, triggers, memories, and RAG search."
    ),
    stateless_http=True,
)

_backend_url_override: Optional[str] = None
_service_token_override: Optional[str] = None
_client: Optional[NymeriaBackendClient] = None


def _resolve_api_url(api_url: Optional[str] = None) -> str:
    """Resolve the Nymeria API URL for local and Docker runs."""
    explicit = api_url or _backend_url_override or os.environ.get("NYMERIA_API_URL")
    if explicit:
        return explicit.rstrip("/")
    if Path("/.dockerenv").exists():
        return "http://nymeria-api:8000"
    return "http://localhost:8000"


def configure_backend(
    api_url: Optional[str] = None,
    service_token: Optional[str] = None,
) -> None:
    """Configure the API URL (and optionally service token) used by MCP tool calls.

    ``service_token`` takes precedence over ``settings.nymeria_service_token``
    when resolving the bearer used for backend requests. Slim mode passes it
    explicitly so the embedded MCP ASGI app uses the same internally
    provisioned token even when nothing has been written to environment yet.
    """
    global _backend_url_override, _service_token_override, _client
    _backend_url_override = api_url.rstrip("/") if api_url else None
    _service_token_override = service_token.strip() if service_token else None
    _client = None


def _get_client() -> NymeriaBackendClient:
    """Get a lazily-created backend client."""
    global _client
    from nymeria.config import get_settings

    if _service_token_override:
        service_token = _service_token_override
    else:
        settings = get_settings()
        # In the full Docker stack the mcp container shares the ``nymeria_data``
        # volume with the api and starts only once the api is healthy, so the
        # token the api self-minted is already on disk. Fall back to it when no
        # operator token is set (resolve_service_token: env wins, else file).
        from nymeria.core.service_bootstrap import resolve_service_token

        service_token = resolve_service_token(
            settings.nymeria_service_token, getattr(settings, "data_dir", None)
        )
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
    # In HTTP mode the inbound bearer governs identity: non-admins are pinned to
    # their own user_id and admins keep Act-As. STDIO mode is unchanged.
    act_as = effective_act_as(user_id)
    if method == "GET":
        return await _call(client.get(path, params=params, act_as=act_as))
    if method == "POST":
        return await _call(client.post(path, json_body=body, params=params, act_as=act_as))
    if method == "PATCH":
        return await _call(client.patch(path, json_body=body, params=params, act_as=act_as))
    if method == "PUT":
        return await _call(client.put(path, json_body=body, params=params, act_as=act_as))
    if method == "DELETE":
        return await _call(client.delete(path, params=params, act_as=act_as))
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
    verbosity: str = "verbose",
    attachments: Optional[List[Dict[str, Any]]] = None,
    force_unsupported_attachments: bool = False,
) -> Dict[str, Any]:
    """
    Send a message to Nymeria and return a transcript.

    verbosity:
    - verbose: full desktop-style transcript with tool args/results/artifacts,
      markdown, context stats, model metadata, and optional raw SSE events.
    - concise: thinking, preamble/final response text, tool names/status, useful
      metadata, and no tool arguments/results/artifacts.
    - chat: smallest conversational result with thread_id, final_response,
      errors, and done.
    """
    if not message or not message.strip():
        return {"error": "message is required"}
    user_id = effective_act_as(user_id) or user_id
    try:
        return await collect_chat_transcript(
            _get_client(),
            message=message,
            user_id=user_id,
            thread_id=thread_id,
            attachments=attachments,
            force_unsupported_attachments=force_unsupported_attachments,
            include_events=include_events,
            verbosity=verbosity,
        )
    except Exception as exc:
        return _error_result(exc)


# Background chat dispatch state: lets a caller fire a chat and return before
# the SSE stream finishes, so a second tool call (typically another chat
# against the same thread) can observe the first one's busy state. The state
# is process-local; that is fine for the single nymeria-mcp container.
_BACKGROUND_CHATS: Dict[str, Dict[str, Any]] = {}
_BACKGROUND_CHATS_MAX = 64
_BACKGROUND_CHATS_TTL_SECONDS = 600


def _evict_old_background_chats() -> None:
    """Drop completed dispatches older than the TTL; cap the live set."""
    now = time.time()
    stale = [
        did
        for did, ctx in _BACKGROUND_CHATS.items()
        if ctx["done_event"].is_set()
        and ctx.get("completed_at")
        and (now - ctx["completed_at"]) > _BACKGROUND_CHATS_TTL_SECONDS
    ]
    for did in stale:
        _BACKGROUND_CHATS.pop(did, None)
    if len(_BACKGROUND_CHATS) > _BACKGROUND_CHATS_MAX:
        finished = sorted(
            (
                (did, ctx)
                for did, ctx in _BACKGROUND_CHATS.items()
                if ctx["done_event"].is_set()
            ),
            key=lambda kv: kv[1].get("completed_at") or 0,
        )
        for did, _ in finished[: len(_BACKGROUND_CHATS) - _BACKGROUND_CHATS_MAX]:
            _BACKGROUND_CHATS.pop(did, None)


@mcp.tool()
async def nymeria_chat_background(
    message: str,
    thread_id: str,
    user_id: str = "default",
    capture_window_ms: int = 800,
    source: Optional[str] = None,
    is_self_invoke: bool = False,
) -> Dict[str, Any]:
    """
    Dispatch a chat in the background; return after capturing early SSE events.

    Returns as soon as EITHER:
      - ``capture_window_ms`` elapses, OR
      - the stream surfaces a queue/error/done signal
        (``prompt_queued`` / ``error`` / ``done``).

    The stream keeps running server-side. Pass the returned ``dispatch_id``
    to ``nymeria_chat_collect`` to wait for completion and fetch the full
    event list.

    Designed for sub-turn-queue regression specs: start a slow holder, return
    as soon as the lock is acquired, then dispatch a queuer that observes the
    busy state.

    Parameters mirror ``nymeria_chat`` plus:
    - ``capture_window_ms``: how long to wait for early events before
      returning (50–10000ms; defaults to 800).
    - ``source`` / ``is_self_invoke``: pass through to ChatRequest (use to
      exercise source-spoof handling at chat.py:_agent_prompt_source).
    """
    if not message or not message.strip():
        return {"error": "message is required"}
    if not thread_id:
        return {
            "error": "thread_id is required (background dispatch needs an existing thread)"
        }
    # Pin identity now (synchronously, while the request context var is set) so
    # the background task captures the resolved user, not a caller-spoofed one.
    user_id = effective_act_as(user_id) or user_id

    _evict_old_background_chats()
    window_ms = max(50, min(10_000, int(capture_window_ms)))
    dispatch_id = uuid.uuid4().hex[:12]
    started_at = time.time()
    ctx: Dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "thread_id": thread_id,
        "user_id": user_id,
        "started_at": started_at,
        "completed_at": None,
        "events": [],
        "done_event": asyncio.Event(),
        "error": None,
    }
    _BACKGROUND_CHATS[dispatch_id] = ctx

    resolved_source = source if source is not None else "mcp"

    async def _consume() -> None:
        try:
            client = _get_client()
            async for evt in client.stream_chat(
                message=message,
                user_id=user_id,
                thread_id=thread_id,
                is_self_invoke=is_self_invoke,
                source=resolved_source,
            ):
                ctx["events"].append(evt)
                if evt.get("type") in ("done", "error"):
                    break
        except Exception as exc:
            ctx["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            ctx["completed_at"] = time.time()
            ctx["done_event"].set()
            # Drop the strong reference now that the task is finished; the entry
            # itself is reaped by the TTL eviction sweep.
            ctx["task"] = None

    # Keep a strong reference on the ctx (retained by _BACKGROUND_CHATS) for the
    # task's lifetime. The event loop only holds a weak reference to bare tasks,
    # so a fire-and-forget create_task() can be garbage-collected mid-flight.
    ctx["task"] = asyncio.create_task(_consume())

    loop = asyncio.get_event_loop()
    deadline = loop.time() + window_ms / 1000.0
    while True:
        if ctx["done_event"].is_set():
            break
        if loop.time() >= deadline:
            break
        types_seen = {e.get("type") for e in ctx["events"]}
        if "prompt_queued" in types_seen or "error" in types_seen:
            break
        await asyncio.sleep(0.05)

    return {
        "dispatch_id": dispatch_id,
        "thread_id": thread_id,
        "status": "done" if ctx["done_event"].is_set() else "running",
        "early_events": list(ctx["events"]),
        "error": ctx.get("error"),
    }


@mcp.tool()
async def nymeria_chat_collect(
    dispatch_id: str,
    timeout_seconds: int = 90,
) -> Dict[str, Any]:
    """
    Wait for a ``nymeria_chat_background`` dispatch to finish; return all events.

    On success: ``{status: "done", events: [...], thread_id, dispatch_id, error}``.
    On timeout: ``{status: "timeout", events: [...partial...], error: "timeout ..."}``
    — the background task keeps running; you can call collect again.

    Use this when you need the holder's full transcript (turn_halted,
    prompt_injected, final response, done) or when you need to confirm
    the queuer's stream actually reached prompt_absorbed.
    """
    ctx = _BACKGROUND_CHATS.get(dispatch_id)
    if ctx is None:
        return {
            "error": (
                f"dispatch_id {dispatch_id!r} not found "
                "(already evicted, expired, or never created)"
            )
        }
    try:
        await asyncio.wait_for(
            ctx["done_event"].wait(),
            timeout=max(1, int(timeout_seconds)),
        )
    except asyncio.TimeoutError:
        return {
            "dispatch_id": dispatch_id,
            "thread_id": ctx["thread_id"],
            "status": "timeout",
            "events": list(ctx["events"]),
            "error": "timeout waiting for completion",
        }
    return {
        "dispatch_id": dispatch_id,
        "thread_id": ctx["thread_id"],
        "status": "done",
        "events": list(ctx["events"]),
        "error": ctx.get("error"),
    }


# =============================================================================
# Slash commands
# =============================================================================


@mcp.tool()
async def nymeria_command(
    command: str,
    user_id: str = "default",
    thread_id: Optional[str] = None,
    surface: str = "api",
) -> Dict[str, Any]:
    """
    Run a slash command as a user would, returning the markdown a user sees.

    Forwards the raw command line (leading slash optional) to the backend
    command dispatcher as a formless USER-actor caller: a missing required
    argument returns the usage error, never a generated picker form. The
    call carries user authority for the acted-as account (the connection's
    bearer identity governs who that can be), so admin and surface gates
    apply to that user, not to an agent actor; only wire this surface to a
    caller trusted with that account's authority.

    Commands that execute through the chat pipeline (/skill, /kit, /goal,
    /compact, and others) are not run here: with a thread_id the result
    carries a hint to send the same line via nymeria_chat, and without one
    the thread-required error comes back first, as on any frontend.

    surface: which frontend to emulate for discovery, per-surface blocking,
    and the per-surface output budget (long results truncate to the emulated
    surface's cap). One of: desktop, mobile, cli, discord, telegram, slack,
    whatsapp, teams, twitch, api, agent. Default api, the surface whose real
    callers are formless (the Rich CLI and desktop render forms). Unknown
    values return the backend's validation error.
    """
    if not command or not command.strip():
        return {"error": "command is required"}
    result = await _json_call(
        "POST",
        "/commands/execute",
        user_id=user_id,
        body={
            "command": command,
            "thread_id": thread_id,
            "source": "user",
            "actor": "user",
            "surface": surface,
            "supports_forms": False,
        },
    )
    # Same structural contract as the generic bot passthroughs
    # (triggers/bot_helpers.py): failed result + data.execution_kind.
    if (
        isinstance(result, dict)
        and not result.get("success")
        and isinstance(result.get("data"), dict)
        and result["data"].get("execution_kind") == "chat_stream"
    ):
        result["hint"] = (
            "This command runs through the chat pipeline, not the command "
            "dispatcher. Send the same line as a nymeria_chat message to "
            "execute it."
        )
    return result


@mcp.tool()
async def nymeria_fire_trigger(
    trigger_id: str,
    payload: Optional[Dict[str, Any]] = None,
    secret: Optional[str] = None,
    user_id: str = "default",
) -> Dict[str, Any]:
    """
    Fire a webhook trigger via ``POST /triggers/fire/{trigger_id}``.

    The MCP authenticates as the service token and acts-as ``user_id``,
    so ``secret`` is optional for in-cluster regression use. External
    callers still need the per-trigger shared secret.

    Returns ``{status, trigger_id, trigger_name, action_type}`` on success,
    or the backend's error body for HTTP 429 cooldown / 404 not-found /
    403 invalid-secret.
    """
    if not trigger_id:
        return {"error": "trigger_id is required"}
    params: Dict[str, Any] = {"user_id": user_id}
    if secret:
        params["secret"] = secret
    return await _json_call(
        "POST",
        f"/triggers/fire/{_enc(trigger_id)}",
        user_id=user_id,
        body=payload if payload is not None else {},
        params=params,
    )


# =============================================================================
# Thread Management
# =============================================================================


@mcp.tool()
async def nymeria_list_threads(
    user_id: str = "default",
    owned_only: bool = False,
) -> Dict[str, Any]:
    """
    List threads visible to a user, including metadata.

    Set ``owned_only=True`` to skip the checkpoint/metadata/resource recovery
    enrichment and return only threads recorded as owned by ``user_id``.
    Recommended for cleanup workflows and automated tests: without it, admin
    users see every orphan checkpoint thread in the database (intended for
    operator inspection, but easy to mistake for user-owned threads).
    """
    params: Dict[str, Any] = {}
    if owned_only:
        params["owned_only"] = "true"
    return await _json_call("GET", "/threads", user_id=user_id, params=params or None)


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
    verbosity: str = "verbose",
) -> Dict[str, Any]:
    """
    Get thread history, optionally adding assistant markdown/final text.

    verbosity:
    - verbose: current full history with tool args/results/artifacts.
    - concise: redacted assistant steps with thinking/response text and tool
      names/status only.
    - chat: smallest conversational history with message text only.
    """
    try:
        mode = normalize_transcript_verbosity(verbosity)
    except ValueError as exc:
        return {"error": str(exc)}

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

    result["messages"] = [
        project_history_message_for_verbosity(
            msg,
            mode,
            include_markdown=include_markdown,
        )
        if isinstance(msg, dict)
        else msg
        for msg in messages
    ]
    result["verbosity"] = mode
    return result


@mcp.tool()
async def nymeria_thread_history(
    thread_id: str,
    user_id: str = "default",
    limit: int = 20,
    include_internal: bool = False,
    verbosity: str = "verbose",
) -> Dict[str, Any]:
    """Backward-compatible alias for nymeria_get_thread_history."""
    return await nymeria_get_thread_history(
        thread_id=thread_id,
        user_id=user_id,
        include_internal=include_internal,
        limit=limit,
        include_markdown=True,
        verbosity=verbosity,
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
async def nymeria_compact_thread(
    thread_id: str, user_id: str = "default", priority: Optional[str] = None
) -> Dict[str, Any]:
    """Manually compact a thread's conversation context.

    Optional ``priority`` is a free-text focus instruction that steers what the
    summary emphasizes (it never drops other required content).
    """
    params = {"priority": priority} if priority else None
    return await _json_call(
        "POST", f"/threads/{_enc(thread_id)}/compact", user_id=user_id, params=params
    )


@mcp.tool()
async def nymeria_prune_thread(thread_id: str, user_id: str = "default") -> Dict[str, Any]:
    """Deterministically compress tool returns in a thread (no LLM).

    Rewrites each ToolMessage in the thread's active state to a short
    placeholder marker, leaving the agent's reasoning trail intact. Re-invoking
    a tool fetches the real result. Idempotent.
    """
    return await _json_call("POST", f"/threads/{_enc(thread_id)}/prune", user_id=user_id)


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
    user_id = effective_act_as(user_id) or user_id
    return await _json_call("GET", f"/users/{_enc(user_id)}/memories", user_id=user_id)


@mcp.tool()
async def nymeria_profile_save(key: str, value: str, user_id: str = "default") -> Dict[str, Any]:
    """Save or update a memory for a user."""
    if not key or not value:
        return {"error": "key and value are required"}
    user_id = effective_act_as(user_id) or user_id
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
    user_id = effective_act_as(user_id) or user_id
    return await _json_call("DELETE", f"/users/{_enc(user_id)}/memories/{_enc(key)}", user_id=user_id)


@mcp.tool()
async def nymeria_rag_search(query: str, max_results: int = 5, user_id: str = "default") -> Dict[str, Any]:
    """Search the user's RAG index through the backend API."""
    if not query:
        return {"error": "query is required"}
    user_id = effective_act_as(user_id) or user_id
    return await _json_call(
        "GET",
        f"/users/{_enc(user_id)}/rag/search",
        user_id=user_id,
        params={"q": query, "max_results": max(1, min(10, int(max_results)))},
    )


# =============================================================================
# Notifications (destinations, profiles, preferences)
# =============================================================================


@mcp.tool()
async def nymeria_notification_channel_types(user_id: str = "default") -> Dict[str, Any]:
    """List the channel TYPES available for new notification destinations.

    Use this when guiding the user through setup so you know which keys each
    type expects (e.g. webhook needs ``url``; telegram needs ``chat_id``).
    """
    return await _json_call(
        "GET", "/notifications/channel-types", user_id=user_id,
    )


@mcp.tool()
async def nymeria_notification_destination_list(user_id: str = "default") -> Dict[str, Any]:
    """List the user's notification destinations (telegram chat, email, webhook, push, etc.)."""
    return await _json_call(
        "GET", "/notifications/destinations", user_id=user_id,
    )


@mcp.tool()
async def nymeria_notification_destination_add(
    name: str,
    type: str,
    config: Optional[Dict[str, Any]] = None,
    secret_fields: Optional[Dict[str, str]] = None,
    enabled: bool = True,
    user_id: str = "default",
) -> Dict[str, Any]:
    """Create a notification destination.

    ``config`` carries non-secret keys (chat_id, webhook URL, email
    recipient). ``secret_fields`` carries secrets (bearer tokens, etc.).
    Call ``nymeria_notification_channel_types`` first to see which keys the
    chosen ``type`` expects.
    """
    if not name or not type:
        return {"error": "name and type are required"}
    body: Dict[str, Any] = {
        "name": name,
        "type": type,
        "config": config or {},
        "secret_fields": secret_fields or {},
        "enabled": bool(enabled),
    }
    return await _json_call(
        "POST", "/notifications/destinations", user_id=user_id, body=body,
    )


@mcp.tool()
async def nymeria_notification_destination_update(
    dest_id: str,
    name: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    secret_fields: Optional[Dict[str, Optional[str]]] = None,
    enabled: Optional[bool] = None,
    user_id: str = "default",
) -> Dict[str, Any]:
    """Patch a notification destination. ``secret_fields`` values of null
    delete the field; strings replace it. Omit keys to leave them unchanged.
    """
    if not dest_id:
        return {"error": "dest_id is required"}
    body: Dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if config is not None:
        body["config"] = config
    if secret_fields is not None:
        body["secret_fields"] = secret_fields
    if enabled is not None:
        body["enabled"] = bool(enabled)
    return await _json_call(
        "PATCH",
        f"/notifications/destinations/{_enc(dest_id)}",
        user_id=user_id,
        body=body,
    )


@mcp.tool()
async def nymeria_notification_destination_delete(
    dest_id: str, user_id: str = "default",
) -> Dict[str, Any]:
    """Delete a notification destination. Also removes it from every profile."""
    if not dest_id:
        return {"error": "dest_id is required"}
    return await _json_call(
        "DELETE", f"/notifications/destinations/{_enc(dest_id)}", user_id=user_id,
    )


@mcp.tool()
async def nymeria_notification_destination_test(
    dest_id: str,
    message: str = "Test notification from Nymeria",
    user_id: str = "default",
) -> Dict[str, Any]:
    """Send a test message to a single destination. Does NOT write to the
    in-app feed -- the result is returned in the response."""
    if not dest_id:
        return {"error": "dest_id is required"}
    return await _json_call(
        "POST",
        f"/notifications/destinations/{_enc(dest_id)}/test",
        user_id=user_id,
        body={"message": message},
    )


@mcp.tool()
async def nymeria_notification_profile_list(user_id: str = "default") -> Dict[str, Any]:
    """List the user's notification profiles (bundles of destinations)."""
    return await _json_call(
        "GET", "/notifications/profiles", user_id=user_id,
    )


@mcp.tool()
async def nymeria_notification_profile_add(
    name: str,
    destination_names: Optional[List[str]] = None,
    user_id: str = "default",
) -> Dict[str, Any]:
    """Create a notification profile referencing zero or more destinations
    by their user-facing names."""
    if not name:
        return {"error": "name is required"}
    return await _json_call(
        "POST",
        "/notifications/profiles",
        user_id=user_id,
        body={"name": name, "destination_names": list(destination_names or [])},
    )


@mcp.tool()
async def nymeria_notification_profile_update(
    profile_id: str,
    name: Optional[str] = None,
    destination_names: Optional[List[str]] = None,
    user_id: str = "default",
) -> Dict[str, Any]:
    """Patch a notification profile (rename it, or replace its destination list)."""
    if not profile_id:
        return {"error": "profile_id is required"}
    body: Dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if destination_names is not None:
        body["destination_names"] = list(destination_names)
    return await _json_call(
        "PATCH",
        f"/notifications/profiles/{_enc(profile_id)}",
        user_id=user_id,
        body=body,
    )


@mcp.tool()
async def nymeria_notification_profile_delete(
    profile_id: str, user_id: str = "default",
) -> Dict[str, Any]:
    """Delete a notification profile."""
    if not profile_id:
        return {"error": "profile_id is required"}
    return await _json_call(
        "DELETE", f"/notifications/profiles/{_enc(profile_id)}", user_id=user_id,
    )


@mcp.tool()
async def nymeria_notification_preferences_get(user_id: str = "default") -> Dict[str, Any]:
    """Get the user-level notification preferences (default profile name, etc.)."""
    return await _json_call(
        "GET", "/notifications/preferences", user_id=user_id,
    )


@mcp.tool()
async def nymeria_notification_preferences_set(
    default_profile: Optional[str] = None,
    user_id: str = "default",
) -> Dict[str, Any]:
    """Update user-level notification preferences. Omit a field to leave it
    unchanged."""
    body: Dict[str, Any] = {}
    if default_profile is not None:
        body["default_profile"] = default_profile
    return await _json_call(
        "PATCH", "/notifications/preferences", user_id=user_id, body=body,
    )


# =============================================================================
# Server Entry Points
# =============================================================================


def create_mcp_asgi_app(
    api_url: Optional[str] = None,
    service_token: Optional[str] = None,
):
    """Return a Streamable HTTP ASGI app suitable for mounting at ``/mcp``.

    Configures the backend client (base URL and optional service token
    override), forces the embedded streamable HTTP route to ``"/"`` so the
    public endpoint ends up at exactly ``/mcp`` (without ``/mcp/mcp``) when
    mounted, and returns the ASGI callable.
    """
    configure_backend(api_url=api_url, service_token=service_token)
    # FastMCP's streamable HTTP app serves its endpoint at this path relative
    # to wherever it is mounted. Using "/" means the mount point itself is the
    # endpoint; using "/mcp" (the default) would produce /mcp/mcp under our
    # mount.
    mcp.settings.streamable_http_path = "/"
    logger.info(
        "Creating Nymeria MCP ASGI app for embedded mount; api=%s",
        _resolve_api_url(),
    )
    # Require + resolve an inbound bearer on every request. Tools run with the
    # admin service token, so without this any reachable caller could act as
    # any user (C-4). The resolver targets the same backend the tools use.
    return MCPAuthMiddleware(mcp.streamable_http_app(), resolve_base_url=_resolve_api_url)


def run_stdio(api_url: Optional[str] = None) -> None:
    """Run the MCP server in STDIO mode."""
    configure_backend(api_url)
    logger.info("Starting Nymeria MCP server in STDIO mode; api=%s", _resolve_api_url())
    mcp.run()


def run_http(host: str = "127.0.0.1", port: int = 8001, api_url: Optional[str] = None) -> None:
    """Run the MCP server in streamable HTTP mode.

    The streamable-HTTP endpoint is gated by :class:`MCPAuthMiddleware`, which
    requires an inbound bearer resolving to a real account. This endpoint must
    still never be exposed beyond loopback / a private mesh: it holds the admin
    service token, so auth is defense-in-depth, not a license to publish it.
    """
    import uvicorn

    configure_backend(api_url)
    logger.info("Starting Nymeria MCP server in HTTP mode on %s:%s; api=%s", host, port, _resolve_api_url())
    mcp.settings.host = host
    mcp.settings.port = port
    # Standalone HTTP mode keeps the historical /mcp endpoint. create_mcp_asgi_app
    # rewrites this for embedded slim-mode mounts; reset it here so successive
    # run_http() calls in the same process behave identically.
    mcp.settings.streamable_http_path = "/mcp"
    app = MCPAuthMiddleware(mcp.streamable_http_app(), resolve_base_url=_resolve_api_url)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run_stdio()
