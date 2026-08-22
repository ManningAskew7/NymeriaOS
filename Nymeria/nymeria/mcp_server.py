"""
Nymeria MCP Server - thin client over the Nymeria REST/SSE API.

The MCP process intentionally owns no agent, checkpointer, TODO manager, or
profile state.  It authenticates to the running API with NYMERIA_SERVICE_TOKEN
and uses X-Nymeria-Act-As for user-scoped operations.
"""

from __future__ import annotations

import asyncio
import base64
import json
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
    normalize_transcript_verbosity,
    project_history_message_for_verbosity,
    transcript_from_events,
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


@mcp.tool()
async def nymeria_turn_status(
    thread_id: Optional[str] = None,
    user_id: str = "default",
) -> Dict[str, Any]:
    """Is a turn still alive? Answers without waiting on it.

    This is the question a handoff leaves open, and the one a lost resume token
    strands you on.

    With ``thread_id``: reads that thread's status, including ``processing``
    and a ``turn`` block whose ``state`` is live / done / error / aborted. This
    is the precise answer and it works for any identity.

    Without ``thread_id``: reads the whole-instance aggregate
    (``active_turns``, ``interactive_active``, ``background_jobs``). Its
    ``busy_threads`` detail is cross-user metadata and arrives populated only
    for an admin identity, so an empty list from a non-admin means "not visible
    to you" rather than "nothing running".
    """
    if thread_id:
        return await _json_call(
            "GET", f"/threads/{_enc(thread_id)}/status", user_id=user_id
        )
    return await _json_call("GET", "/status/turns", user_id=user_id)


# =============================================================================
# Chat
# =============================================================================


# Bounds for a blocking chat's wait budget.
#
# The default matches ``settings.tool_timeout`` (300s), so an MCP ask waits
# exactly as long as an in-process callable-thread ask does.
#
# The ceiling is generous because real browser drives run ten to twenty
# minutes, but it carries a TRAP worth stating at the site. This is an inner
# wait nested inside the MCP client's own transport timeout, and an inner
# timeout that cannot win its race is dead code: the client aborts first, so
# the caller gets a hard transport error instead of the partial transcript and
# resume token this path exists to hand back. (Measured precedent: the
# ui_prompt review fix of 2026-07-12, ``shipped/02``, where an uncapped inner
# wait always lost to SafeToolNode's per-call kill so its graceful branch
# never ran.)
#
# We cannot enforce that from here: the client's timeout is client-side config
# this process cannot read. So the rule lives in the docs instead, and the repo
# .mcp.json is set above this ceiling. A caller whose transport timeout is
# lower should pass a smaller wait_seconds; if it aborts anyway, the turn is
# still recoverable with nymeria_chat_collect against the thread.
_CHAT_WAIT_MIN_SECONDS = 1
_CHAT_WAIT_MAX_SECONDS = 3600
_CHAT_WAIT_DEFAULT_SECONDS = 300

_CHAT_MODES = ("ask", "handoff")
_IF_BUSY_CHOICES = ("queue", "error")


def _encode_resume(
    dispatch_id: str,
    thread_id: Optional[str],
    cursor: int,
    user_id: Optional[str] = None,
    turn_id: Optional[str] = None,
) -> str:
    """Pack the state a resume needs into one opaque token.

    The token carries the THREAD, not just the dispatch, and that is the whole
    point: the in-memory dispatch is evicted ten minutes after it completes,
    but the server keeps its own copy of the turn. A token that named only the
    dispatch turned a ten-minute distraction into a lost result; one that names
    the thread can fall back to the server's replay instead.

    ``cursor`` indexes the dispatch's OWN event list, a different numbering
    from the server's ``seq``. The two are never mixed: the replay path ignores
    the cursor and re-reads the turn whole rather than mapping between them.
    """
    raw = json.dumps(
        {
            "d": dispatch_id,
            "t": thread_id,
            "i": int(cursor),
            "u": user_id,
            "n": turn_id,
        }
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_resume(token: str) -> Dict[str, Any]:
    """Unpack a resume token; tolerate a bare dispatch_id for compatibility."""
    candidate = token.strip()
    padded = candidate + "=" * (-len(candidate) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        # Key presence, not truthiness. A server-replay token legitimately
        # carries an EMPTY dispatch id (there is no in-process dispatch to
        # resume, only a thread to re-read), and gating on truthiness sent it
        # down the bare-id branch, where it decoded to the base64 blob as an id
        # and lost the thread. The recovery path could then be entered but not
        # continued.
        if isinstance(decoded, dict) and ("d" in decoded or "t" in decoded):
            return {
                "dispatch_id": str(decoded["d"]),
                "thread_id": decoded.get("t"),
                "cursor": int(decoded.get("i") or 0),
                "user_id": decoded.get("u"),
                "turn_id": decoded.get("n"),
            }
    except Exception:  # noqa: BLE001 - not a packed token, fall through
        pass
    # Callers (and the regression specs) pass raw dispatch_ids; keep them working.
    return {
        "dispatch_id": candidate,
        "thread_id": None,
        "cursor": 0,
        "user_id": None,
        "turn_id": None,
    }


_REPLAY_GAP_ERROR = (
    "[Error]: the buffered record for thread '{thread_id}' has a gap: the turn "
    "produced more events than the buffer holds, so an unknown middle section "
    "is missing. Do not treat what follows as the whole turn; use "
    "nymeria_get_thread_history for the persisted result."
)


async def _replay_turn_events(
    thread_id: str,
    user_id: str,
    budget: int,
    turn_id: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
    """Read a turn from the SERVER's buffer. Returns (events, error, state).

    Used when the in-memory dispatch is gone, and when a caller has no token at
    all and just wants to know what a thread is doing.

    Three server signals are load-bearing here, and the two in-band ones are
    easy to miss because the endpoint also has HTTP equivalents:

    - The stream OPENS with a ``turn_attach`` meta frame carrying the turn's
      state. A turn whose writer died replays with state ``aborted``; without
      reading it we would report "still running" and invite the caller to poll
      a corpse.
    - A gap discovered MID-stream arrives as an in-band ``turn_replay_gap``
      frame, after which the server ends the stream with no terminal event.
      The attach-time 410 only covers gaps that already existed. Both mean the
      record has a hole and must not read as the whole turn.
    - 404 at attach time means there is nothing to replay at all.
    """
    events: List[Dict[str, Any]] = []
    box: Dict[str, Optional[str]] = {"state": None, "gap": None}

    async def _drain() -> None:
        async for event in _get_client().stream_turn_replay(
            thread_id=thread_id, user_id=user_id, turn_id=turn_id
        ):
            etype = event.get("type")
            if etype == "turn_attach":
                state = event.get("state")
                box["state"] = str(state) if state else None
                continue
            if etype == "turn_replay_gap":
                box["gap"] = "mid_stream"
                continue
            events.append(event)
            if etype in ("done", "error"):
                break

    # #199: the thread_id-only / evicted-dispatch collect is the RECOVERY
    # path the collect docstring routes people to, so it must prove it is
    # alive the same way the dispatch wait does, or the fix's advertised
    # fallback keeps the very defect it exists to escape (review catch).
    drain = asyncio.create_task(_drain())
    try:
        if not await _wait_with_progress(drain, budget):
            drain.cancel()
            try:
                await drain
            except (asyncio.CancelledError, Exception):
                # Giving up on the wait: the cancellation (or any late drain
                # error) carries nothing the partial `events` do not.
                pass
            return events, None, box["state"]
        await drain
    except NymeriaAPIError as exc:
        if exc.status_code == 404:
            return events, (
                f"[Error]: no matching turn to replay for thread '{thread_id}'. "
                "It never ran, its buffer has expired, or the thread has since "
                "moved on to a different turn. Read the outcome with "
                "nymeria_get_thread_history instead."
            ), box["state"]
        if exc.status_code == 410:
            return events, _REPLAY_GAP_ERROR.format(thread_id=thread_id), box["state"]
        return events, f"[Error]: could not replay the turn: {exc}", box["state"]
    except Exception as exc:  # noqa: BLE001
        return events, f"[Error]: could not replay the turn: {exc}", box["state"]

    if box["gap"]:
        return events, _REPLAY_GAP_ERROR.format(thread_id=thread_id), box["state"]
    return events, None, box["state"]


async def _thread_is_busy(thread_id: str, user_id: str) -> Optional[bool]:
    """Is this specific thread already running a turn? ``None`` = could not tell.

    Reads ``GET /threads/{id}/status``, whose ``processing`` flag is the same
    in-process lock predicate the callable-thread busy guard consults
    (``agent.is_thread_busy``), and which is gated on thread ACCESS rather than
    on role. The obvious-looking alternative, ``/status/turns``, hides its
    ``busy_threads`` detail from non-admins, so a non-admin caller could never
    get a ``True`` out of it and ``if_busy="error"`` would silently degrade to
    "queue" for exactly the identity that most needs the refusal.

    ``None`` is still possible (the endpoint erred, or answered a shape we do
    not recognise) and means unknowable, not idle.
    """
    try:
        data = await _json_call(
            "GET", f"/threads/{_enc(thread_id)}/status", user_id=user_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("busy pre-check unavailable: %s", exc)
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    processing = data.get("processing")
    return bool(processing) if isinstance(processing, bool) else None


def _configured_chat_wait_seconds() -> int:
    """The operator's default wait, derived from settings rather than os.environ.

    Read through pydantic settings on purpose: it loads ``.env`` /
    ``config.env`` WITHOUT exporting to the process environment, so an
    ``os.environ`` read would silently ignore the file the docs tell a slim
    operator to edit and only work where a process manager exports it.

    Unset means derive from ``tool_timeout``, matching the
    ``nymeria_claude_code_block_seconds`` precedent, so an MCP ask really does
    wait as long as an in-process callable-thread ask rather than merely
    sharing its default number.
    """
    try:
        from nymeria.config import get_settings

        settings = get_settings()
        configured = getattr(settings, "nymeria_mcp_chat_wait_seconds", None)
        if configured is None:
            configured = getattr(settings, "tool_timeout", _CHAT_WAIT_DEFAULT_SECONDS)
        return max(_CHAT_WAIT_MIN_SECONDS, min(_CHAT_WAIT_MAX_SECONDS, int(configured)))
    except Exception:  # noqa: BLE001 - a thin client must still answer
        logger.debug("Could not resolve the configured chat wait; using default")
        return _CHAT_WAIT_DEFAULT_SECONDS


def _resolve_chat_wait_seconds(requested: Optional[int]) -> int:
    """Resolve the blocking-chat wait budget, in seconds.

    An explicit out-of-range argument RAISES, naming both numbers, because the
    caller asked for something this tool cannot honor and silently substituting
    a different budget would make the returned status a lie. A misconfigured
    ``NYMERIA_MCP_CHAT_WAIT_SECONDS`` only CLAMPS: an operator typo should not
    brick every chat call, and no caller asked for that value.
    """
    if requested is None:
        return _configured_chat_wait_seconds()

    value = int(requested)
    if not (_CHAT_WAIT_MIN_SECONDS <= value <= _CHAT_WAIT_MAX_SECONDS):
        raise ValueError(
            f"wait_seconds must be between {_CHAT_WAIT_MIN_SECONDS} and "
            f"{_CHAT_WAIT_MAX_SECONDS} (got {value}). For work expected to run "
            f"past {_CHAT_WAIT_MAX_SECONDS}s, dispatch with mode='handoff' and "
            "poll with nymeria_chat_collect instead of waiting."
        )
    return value


@mcp.tool()
async def nymeria_chat(
    message: str,
    user_id: str = "default",
    thread_id: Optional[str] = None,
    include_events: bool = False,
    verbosity: str = "verbose",
    attachments: Optional[List[Dict[str, Any]]] = None,
    force_unsupported_attachments: bool = False,
    mode: str = "ask",
    wait_seconds: Optional[int] = None,
    if_busy: str = "queue",
) -> Dict[str, Any]:
    """
    Send a message to Nymeria and return a transcript.

    mode:
    - ask (default): wait up to ``wait_seconds`` for the final answer. If the
      turn finishes in budget you get ``status="done"`` and the full
      transcript. If it outlives the budget you get ``status="running"``, a
      PARTIAL transcript of the turn so far, and a ``resume`` token. The turn
      keeps running; pass the token to ``nymeria_chat_collect`` to continue
      waiting. Nothing is lost and nothing is cancelled by the budget expiring.
    - handoff: dispatch and return a receipt immediately, without waiting. Use
      it to start long work you will check on later, or to start a second turn
      while a first is still running. Note this is a DEFERRED READ, not a
      transfer of responsibility: unlike a callable-thread handoff, the target
      is not told to report through its own channels, so you remain the one who
      collects the result.

    wait_seconds applies to mode="ask" only (1-3600, default 300, matching the
    backend's own tool_timeout). Passing it with mode="handoff" is an error
    rather than a silent no-op, because a handoff never waits. Your MCP
    client's transport timeout must EXCEED wait_seconds or the client aborts
    first and you get a transport error instead of a partial transcript. The
    wait emits MCP progress notifications (they reach you only if your client
    sent a progressToken); when in doubt, keep wait_seconds under your
    client's idle ceiling and continue the turn with nymeria_chat_collect,
    whose docstring carries the polling pattern.

    if_busy: "queue" (default) lets the prompt join a busy thread's sub-turn
    queue; "error" refuses instead, so a caller that needs a clean turn is not
    silently folded into someone else's.

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
    if mode not in _CHAT_MODES:
        return {"error": f"mode must be one of: {', '.join(_CHAT_MODES)}"}
    if if_busy not in _IF_BUSY_CHOICES:
        return {"error": f"if_busy must be one of: {', '.join(_IF_BUSY_CHOICES)}"}
    if mode == "handoff" and wait_seconds is not None:
        return {
            "error": (
                "wait_seconds is only supported when mode='ask'. A handoff "
                "returns immediately by design; to wait for an answer use "
                "mode='ask', or dispatch with mode='handoff' and then poll "
                "nymeria_chat_collect."
            )
        }
    if mode == "handoff" and not thread_id:
        return {
            "error": (
                "thread_id is required when mode='handoff', because a handoff "
                "returns before the new thread's id is known. Create the "
                "thread with mode='ask' first, then hand off to it."
            )
        }
    try:
        budget = _resolve_chat_wait_seconds(wait_seconds)
        normalize_transcript_verbosity(verbosity)
    except ValueError as exc:
        return {"error": str(exc)}

    # Pin identity synchronously, while the request context var is still set.
    user_id = effective_act_as(user_id) or user_id

    if if_busy == "error" and thread_id:
        busy = await _thread_is_busy(thread_id, user_id)
        if busy:
            return {
                "thread_id": thread_id,
                "status": "busy",
                "error": (
                    f"[Busy]: thread '{thread_id}' is already running a turn. "
                    "Wait for it to finish, or retry with if_busy='queue' to "
                    "join its queue instead."
                ),
            }

    try:
        ctx = _start_background_chat(
            message=message,
            thread_id=thread_id,
            user_id=user_id,
            attachments=attachments,
            force_unsupported_attachments=force_unsupported_attachments,
        )
    except Exception as exc:
        return _error_result(exc)

    if mode == "handoff":
        return {
            "thread_id": thread_id,
            "dispatch_id": ctx["dispatch_id"],
            "status": "dispatched",
            "resume": _encode_resume(
                ctx["dispatch_id"], thread_id, 0, user_id, ctx.get("turn_id")
            ),
            "handoff": (
                f"[HandedOff]: dispatch_id={ctx['dispatch_id']} "
                f"target_thread_id={thread_id}. The turn runs in the "
                "background; no final response will be returned here. Collect "
                "it with nymeria_chat_collect(dispatch_id="
                f"'{ctx['dispatch_id']}'), or watch the thread with "
                "nymeria_turn_status."
            ),
        }

    done = await _await_dispatch(ctx, budget)
    events = list(ctx["events"])
    # A turn that never started is a failed CALL, not a completed turn with an
    # error attached: report it the way every other tool reports a backend
    # refusal, rather than as status="done" over an empty transcript.
    if ctx.get("error_result") and not events:
        return ctx["error_result"]
    result: Dict[str, Any] = {
        **await _render_dispatch_transcript(ctx, events, verbosity=verbosity),
        "thread_id": ctx["thread_id"],
        "status": "done" if done else "running",
        "error": ctx.get("error"),
    }
    if include_events:
        result["events"] = events
    overflow = _overflow_note(ctx)
    if overflow:
        result["truncated"] = overflow
    if not done:
        result["resume"] = _encode_resume(
            ctx["dispatch_id"], ctx["thread_id"], len(events), user_id,
            ctx.get("turn_id"),
        )
        result["running"] = (
            f"[Running]: this turn outlived the {budget}s wait and is STILL "
            "RUNNING; the transcript above is partial, not final. Continue "
            "with nymeria_chat_collect(dispatch_id="
            f"'{ctx['dispatch_id']}'). Nothing was cancelled."
        )
    return result


def _overflow_note(ctx: Dict[str, Any]) -> Optional[str]:
    """Say when a dispatch stopped retaining, so a partial cannot read as whole."""
    if not ctx.get("overflowed"):
        return None
    thread_id = ctx.get("thread_id") or "the thread"
    return (
        "[Truncated]: this turn produced more than this client retains, so the "
        "transcript stops short of the end. The server keeps the full record: "
        f"read it with nymeria_chat_collect(thread_id='{thread_id}') or "
        "nymeria_get_thread_history."
    )


# How often a blocking wait proves it is alive (#199). Cheap on the wire (a
# 30-minute wait is ~180 tiny notifications) and far under the 300s idle
# ceiling the pattern was measured against.
_PROGRESS_INTERVAL_S = 10.0


async def _wait_with_progress(task: "asyncio.Task[Any]", seconds: float) -> bool:
    """Wait up to ``seconds`` for ``task``, proving the call is alive (#199).

    Every long MCP wait used to be one silent ``wait_for``, indistinguishable
    on the wire from a hung call, so a client's idle timeout (Claude Code:
    300s) aborted collects over turns that were healthy and still running.
    This waits in ``_PROGRESS_INTERVAL_S`` slices and emits an MCP progress
    notification between them. Progress only reaches clients that sent a
    ``progressToken`` (the SDK no-ops otherwise), which is why the tool
    docstrings still teach the polling shape as the fallback. The channel is
    best-effort by contract: acquisition failure downgrades to a plain wait,
    any send failure drops it for the rest of the wait, and a heartbeat is
    never attempted past the deadline (staying under the caller's budget is
    the one property this exists to protect).

    Returns True when the task finished (its result or exception is the
    CALLER's to collect), False on timeout. Deliberately does NOT cancel on
    timeout: one caller re-waits the same event, another wants the partial
    drain kept, so cancellation policy stays at the call site.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    try:
        reporter = mcp.get_context()
    except Exception:
        reporter = None
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return task.done()
        done, _ = await asyncio.wait({task}, timeout=min(_PROGRESS_INTERVAL_S, remaining))
        if done:
            return True
        if reporter is not None and deadline - loop.time() > 0:
            elapsed = seconds - max(deadline - loop.time(), 0.0)
            try:
                await reporter.report_progress(round(elapsed, 1), seconds)
            except Exception:
                reporter = None


async def _await_dispatch(ctx: Dict[str, Any], seconds: int) -> bool:
    """Wait up to ``seconds`` for a dispatch to finish. True if it did."""
    waiter = asyncio.create_task(ctx["done_event"].wait())
    try:
        finished = await _wait_with_progress(waiter, seconds)
    finally:
        waiter.cancel()
    return finished or ctx["done_event"].is_set()


# Background chat dispatch state: lets a caller fire a chat and return before
# the SSE stream finishes, so a second tool call (typically another chat
# against the same thread) can observe the first one's busy state. The state
# is process-local; that is fine for the single nymeria-mcp container.
_BACKGROUND_CHATS: Dict[str, Dict[str, Any]] = {}
_BACKGROUND_CHATS_MAX = 64
_BACKGROUND_CHATS_TTL_SECONDS = 600
# Per-dispatch retention, mirroring core/turn_stream_buffer's own caps. Every
# chat now parks its event list here for the TTL, not just explicit regression
# dispatches, so an unbounded list would let a handful of long browser drives
# hold a container's worth of memory. On overflow we stop RETAINING (rather
# than dropping the oldest, which would silently punch a hole in the middle of
# a cursor's numbering) and say so; the server keeps the authoritative copy.
_DISPATCH_MAX_EVENTS = 4000
_DISPATCH_MAX_BYTES = 2 * 1024 * 1024


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


def _start_background_chat(
    *,
    message: str,
    thread_id: Optional[str],
    user_id: str,
    attachments: Optional[List[Dict[str, Any]]] = None,
    force_unsupported_attachments: bool = False,
    source: Optional[str] = None,
    is_self_invoke: bool = False,
) -> Dict[str, Any]:
    """Dispatch a chat onto a background task; return its tracking context.

    Every MCP chat entry point routes through here, so that waiting is a
    property of the CALLER rather than of the dispatch. A blocking ask is this
    plus a bounded wait; a handoff is this and nothing else; the regression
    dispatcher is this plus its capture window. Keeping one dispatch path also
    keeps one set of stream semantics: before this was shared, the background
    pair silently lacked attachment support that the blocking tool had.

    The caller must have resolved ``user_id`` through ``effective_act_as``
    already: identity has to be pinned synchronously, while the request context
    var is still set, or the background task would capture whatever the caller
    claimed rather than what the bearer token resolved to.
    """
    _evict_old_background_chats()
    dispatch_id = uuid.uuid4().hex[:12]
    ctx: Dict[str, Any] = {
        "dispatch_id": dispatch_id,
        "thread_id": thread_id,
        "user_id": user_id,
        # Retained so a later collect can run the same persisted-steps overlay
        # the blocking chat tool does: that lookup matches the assistant message
        # by its preceding USER message, so it needs the prompt verbatim.
        "message": message,
        "started_at": time.time(),
        "completed_at": None,
        "events": [],
        "turn_id": None,
        "bytes": 0,
        "overflowed": False,
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
                attachments=attachments,
                force_unsupported_attachments=force_unsupported_attachments,
                is_self_invoke=is_self_invoke,
                source=resolved_source,
            ):
                if not ctx["overflowed"]:
                    ctx["bytes"] += len(str(evt))
                    ctx["events"].append(evt)
                    if (
                        ctx["bytes"] > _DISPATCH_MAX_BYTES
                        or len(ctx["events"]) > _DISPATCH_MAX_EVENTS
                    ):
                        ctx["overflowed"] = True
                # Pin the TURN as well as the thread. Without it a resume
                # attaches to whatever turn the thread is running NOW, which
                # after a follow-up prompt is a different turn wearing the same
                # thread id; the endpoint 404s on a turn_id mismatch instead,
                # which is the honest answer.
                if not ctx["turn_id"] and evt.get("turn_id"):
                    ctx["turn_id"] = str(evt["turn_id"])
                # A thread this call CREATED only announces its id on the wire.
                # Learning it here is what lets a resumed or timed-out call name
                # the thread it started, instead of stranding the caller with a
                # running turn they cannot address.
                if not ctx["thread_id"] and evt.get("thread_id"):
                    ctx["thread_id"] = str(evt["thread_id"])
                if evt.get("type") in ("done", "error"):
                    break
        except Exception as exc:
            ctx["error"] = f"{type(exc).__name__}: {exc}"
            # Keep the TYPED result too. Moving the stream inside this task
            # took the backend's error out of the tool's own try/except, and a
            # flattened string loses status_code and payload: the 429's
            # retry_after, the 401's auth detail. A caller needs those to know
            # whether to back off, re-auth, or give up.
            ctx["error_result"] = _error_result(exc)
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
    return ctx


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

    THIS IS A REGRESSION-TEST INSTRUMENT, not the general way to start work
    you will not wait for. It exists so a sub-turn-queue spec can start a slow
    holder, return the instant the lock is taken, and then dispatch a queuer
    that observes the busy state. The capture window is the whole point, and
    it is the only tool that offers one.

    For ordinary use, prefer ``nymeria_chat``: ``mode="handoff"`` to dispatch
    without waiting, or ``mode="ask"`` with ``wait_seconds`` to wait for a
    bounded time and get a resume token if the turn outlives it. Those accept
    attachments and return a rendered transcript; this does not.

    The stream keeps running server-side. Pass the returned ``dispatch_id``
    to ``nymeria_chat_collect`` to wait for completion and fetch the full
    event list.

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

    window_ms = max(50, min(10_000, int(capture_window_ms)))
    ctx = _start_background_chat(
        message=message,
        thread_id=thread_id,
        user_id=user_id,
        source=source,
        is_self_invoke=is_self_invoke,
    )
    dispatch_id = ctx["dispatch_id"]

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
    dispatch_id: Optional[str] = None,
    timeout_seconds: int = 90,
    verbosity: str = "verbose",
    include_events: bool = True,
    resume: Optional[str] = None,
    thread_id: Optional[str] = None,
    user_id: str = "default",
) -> Dict[str, Any]:
    """
    Continue watching a turn: wait for more of it, and return what has arrived.

    Three ways to say which turn, in order of preference:
    - ``resume``: the token returned by ``nymeria_chat`` when a turn outlived
      its wait, or by a previous collect. Continues from where that call
      stopped, so each collect returns only what is NEW.
    - ``dispatch_id``: a raw id from ``nymeria_chat_background``. Returns the
      turn from the beginning.
    - ``thread_id`` alone: attaches to that thread's current or most recent
      turn. This is the recovery path when a token was lost or a call was
      killed in transit; nothing is lost, because the server keeps its own
      copy of the turn.

    Returns the rendered transcript FLAT (``final_response``, ``steps``,
    ``full_markdown``, ``context_stats``, ...) exactly as ``nymeria_chat``
    does, plus ``status`` ("done" or "timeout"), ``thread_id`` and a fresh
    ``resume`` token when the turn is still running. On timeout the transcript
    is a PARTIAL and the turn keeps running: nothing is cancelled by giving up
    on waiting.

    POLLING IS THE EXPECTED SHAPE for long turns, not a failure mode. A
    collect emits MCP progress notifications while it waits, but they only
    reach clients that sent a ``progressToken``, and most clients abort any
    call that stays silent past their own idle ceiling (Claude Code: 300s
    unless its per-server timeout was raised). So keep ``timeout_seconds``
    comfortably under your client's ceiling and, on each "timeout" result,
    simply call collect again with the fresh ``resume`` token: the turn keeps
    running between calls and nothing is lost. A client-side abort mid-collect
    loses nothing either; re-attach with ``thread_id`` alone.

    ``verbosity`` shapes the transcript: "verbose" (tool args and results),
    "concise" (thinking and tool names/status, no payloads), or "chat" (final
    response only).

    ``events`` is the RAW SSE list and stays on by default. It is not
    redundant with the transcript: queue and lifecycle events
    (``prompt_queued``, ``turn_halted``, ``prompt_injected``,
    ``prompt_absorbed``) have no transcript representation, and the
    sub-turn-queue regression specs assert on them. Pass
    ``include_events=False`` when you only want something readable and would
    rather not spend the context.
    """
    try:
        normalize_transcript_verbosity(verbosity)
    except ValueError as exc:
        return {"error": str(exc)}
    try:
        # Same bounds as an ask's wait: collect is the tool you reach for when
        # the turn is long, so it is the likelier place to outlive the client.
        budget = _resolve_chat_wait_seconds(int(timeout_seconds))
    except ValueError as exc:
        return {"error": str(exc)}

    # Either parameter may carry either spelling. A packed token handed to
    # dispatch_id is the mistake a caller makes once and cannot diagnose (the
    # id simply "is not found"), and _decode_resume already treats an
    # unrecognised string as a bare id, so accepting both costs nothing.
    cursor = 0
    fallback_thread = thread_id
    replay_user = user_id
    replay_turn: Optional[str] = None
    token = resume or dispatch_id
    if token:
        decoded = _decode_resume(token)
        dispatch_id = decoded["dispatch_id"]
        cursor = decoded["cursor"]
        fallback_thread = decoded["thread_id"] or thread_id
        replay_user = decoded["user_id"] or user_id
        replay_turn = decoded["turn_id"]

    ctx = _BACKGROUND_CHATS.get(dispatch_id) if dispatch_id else None
    # A dispatch id alone must not read another identity's turn out of this
    # process's memory. Every chat now creates a dispatch, so the exposed set
    # is no longer just explicit regression runs. A mismatch falls through to
    # the server path, which the backend gates on thread access.
    if ctx is not None:
        caller = effective_act_as(replay_user) or replay_user
        if ctx.get("user_id") != caller:
            fallback_thread = fallback_thread or ctx.get("thread_id")
            ctx = None
    if ctx is None:
        if not fallback_thread:
            return {
                "error": (
                    f"dispatch_id {dispatch_id!r} not found (already evicted, "
                    "expired, or never created), and no thread_id was given to "
                    "recover from. Pass thread_id= to attach to that thread's "
                    "most recent turn instead."
                )
            }
        return await _collect_from_server(
            fallback_thread,
            budget,
            user_id=replay_user,
            verbosity=verbosity,
            include_events=include_events,
            turn_id=replay_turn,
        )

    timed_out = not await _await_dispatch(ctx, budget)

    all_events = list(ctx["events"])
    if ctx.get("error_result") and not all_events:
        return ctx["error_result"]
    events = all_events[cursor:] if cursor else all_events
    # The persisted-steps overlay replaces `steps` with the WHOLE assistant
    # message, so taking it while rendering a slice hands back the entire turn
    # under `continued_from` and defeats the cursor on the largest poll, the
    # last one. It only makes sense for a render that covers the whole turn.
    rendered = await _render_dispatch_transcript(
        ctx, events, verbosity=verbosity, allow_overlay=not cursor
    )
    if cursor:
        # The WORKING is incremental (the point of a cursor: a long drive
        # should not re-send its whole transcript every poll), but the ANSWER
        # is not divisible. Rendering final_response from the slice alone
        # produced fragments like " and the rest" under a field whose name
        # promises the finished answer, so the answer is always computed from
        # the whole turn; only steps and markdown are the new slice.
        whole = await _render_dispatch_transcript(ctx, all_events, verbosity="chat")
        rendered["final_response"] = whole.get("final_response", "")
    result: Dict[str, Any] = {
        **rendered,
        "dispatch_id": dispatch_id,
        "thread_id": ctx["thread_id"],
        "status": "timeout" if timed_out else "done",
        "error": (
            "timeout waiting for completion" if timed_out else ctx.get("error")
        ),
    }
    overflow = _overflow_note(ctx)
    if overflow:
        result["truncated"] = overflow
    if cursor:
        result["continued_from"] = cursor
    if timed_out:
        result["resume"] = _encode_resume(
            str(dispatch_id), ctx["thread_id"], len(all_events),
            ctx.get("user_id"), ctx.get("turn_id"),
        )
    if include_events:
        result["events"] = events
    return result


async def _collect_from_server(
    thread_id: str,
    budget: int,
    *,
    user_id: str,
    verbosity: str,
    include_events: bool,
    turn_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Recover a turn from the server's own buffer when the dispatch is gone.

    Re-reads the turn WHOLE rather than continuing from a cursor: see
    :func:`_encode_resume` for why the two numbering schemes are never mixed.
    """
    user_id = effective_act_as(user_id) or user_id
    events, error, state = await _replay_turn_events(
        thread_id, user_id, budget, turn_id
    )
    terminal = any(e.get("type") in ("done", "error") for e in events)
    # An aborted turn has no terminal event: its writer died. Treating that as
    # "still running" would hand back a resume token for a corpse.
    finished = terminal or state in ("done", "error", "aborted")
    result: Dict[str, Any] = {
        **await _render_dispatch_transcript(
            {"thread_id": thread_id, "user_id": user_id, "message": None},
            events,
            verbosity=verbosity,
        ),
        "thread_id": thread_id,
        "status": "done" if finished else "timeout",
        "recovered_from": "server_replay",
        "error": error,
    }
    if state:
        result["turn_state"] = state
    if state == "aborted" and not error:
        result["error"] = (
            f"[Error]: the turn on thread '{thread_id}' was aborted before it "
            "finished, so what follows is only what it managed to emit. "
            "Nothing further will arrive on it."
        )
    if not finished and not error:
        result["resume"] = _encode_resume("", thread_id, 0, user_id, turn_id)
    if include_events:
        result["events"] = events
    return result


async def _render_dispatch_transcript(
    ctx: Dict[str, Any],
    events: List[Dict[str, Any]],
    *,
    verbosity: str,
    allow_overlay: bool = True,
) -> Dict[str, Any]:
    """Render a dispatch's events, degrading to a note rather than raising.

    A transcript is a READING aid over events the caller may already have, so a
    rendering failure must not cost them the whole call. The failure is reported
    under ``transcript_error`` rather than ``error`` because these payloads are
    flattened into the tool result, and ``error`` there already means "the turn
    itself failed": collapsing the two would let a cosmetic rendering fault read
    as a failed turn.
    """
    try:
        return await transcript_from_events(
            _get_client(),
            events,
            thread_id=ctx.get("thread_id"),
            user_id=ctx.get("user_id"),
            message=ctx.get("message") if allow_overlay else None,
            verbosity=verbosity,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not render collected transcript: %s", exc)
        return {
            "transcript_error": (
                f"transcript rendering failed: {type(exc).__name__}: {exc}"
            )
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
    callers are formless (only the Rich CLI renders forms today; the
    dispatcher inlines the active picker's option rows for everyone else).
    Unknown values return the backend's validation error.
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
