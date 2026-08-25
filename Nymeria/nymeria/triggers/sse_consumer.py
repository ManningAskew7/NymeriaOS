"""Shared SSE event consumer for bot and CLI trigger platforms.

Centralises event-type routing, field extraction, tool-call counting,
and the "flush before status" discipline so that individual platform
handlers only implement rendering.

Usage — interactive chat stream (#88: always the recovery consumer, so a
mid-turn connection drop re-attaches instead of losing the tail or letting
the caller re-POST a running turn)::

    handler = MyPlatformHandler(...)
    await consume_chat_stream_with_recovery(
        api, handler, message=..., thread_id=..., user_id=..., chat_kwargs=...,
    )

(`consume_sse_stream` is the bare dispatch loop underneath: kept for tests
and for consuming a stream that is not a recoverable chat turn; do NOT use
it for bot chat, that is the pre-#88 behavior.)

Usage — autonomous single-event dispatch::

    count = await dispatch_event(event, handler, tool_call_count)
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from typing import (
    Any,
    AsyncIterable,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)

import httpx

from ..core.agent_compaction import COMPACTING_MESSAGE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSE wire-format line parsing
# ---------------------------------------------------------------------------

_SSE_DATA_PREFIX = "data: "


def parse_sse_data_line(line: str) -> Optional[Any]:
    """Decode one raw SSE line into its JSON payload, or ``None`` to skip.

    Returns the decoded object for a ``data: <json>`` payload line. Returns
    ``None`` for anything a consumer should skip: a blank line, a non-``data:``
    line (including FastAPI's bare ``: keepalive`` comment frames), a
    ``data: :...`` comment payload, or a payload that is not valid JSON.

    This is the shared parser for the SSE line grammar that the in-slice
    streaming consumers (``api_client.chat_stream``/``autonomous_stream`` and
    the ``trigger_api`` fire peek) previously hand-rolled with an inline
    ``[6:]`` slice. The ``discord_bot``/``telegram_bot`` firehose loops also
    route through this parser via :func:`consume_autonomous_firehose`. Because
    real events are always JSON objects, a bare ``data: null`` line decodes to
    ``None`` and is therefore skipped like any other no-op line; that edge
    never occurs on the wire.
    """
    if not line or not line.startswith(_SSE_DATA_PREFIX):
        return None
    raw = line[len(_SSE_DATA_PREFIX):]
    if raw.startswith(":"):
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


_ATTACH_RE = re.compile(r"\[attach:(.+?)\]")


def parse_attach_paths(result: str) -> List[str]:
    """Extract file paths from ``[attach:/path]`` tags in tool output."""
    if not isinstance(result, str):
        return []
    return _ATTACH_RE.findall(result)


_MAX_INSTRUCTIONS_RENDER_CHARS = 800
"""Cap on instructions length in bot-rendered output. Telegram messages
hard-limit at 4096 chars; the rest of the credential prompt (links, codes,
expiry) eats a few hundred, so 800 keeps headroom even on smaller surfaces."""


def format_auth_prompt_message(event: Dict[str, Any]) -> str:
    """Render a safe default credential-setup prompt for text chat surfaces.

    Branches on the ``mode`` field set by ``request_credential``:

    - ``"oauth"`` (authorization code) renders a single sign-in link.
    - ``"oauth_device"`` (RFC 8628) renders a verification URL plus user code.
    - anything else (``api_key``, ``pat``, ``form``, missing) renders the
      one-time hosted-form link from ``connect_url``.

    If the agent passed ``instructions`` (markdown step-by-step), they are
    appended below the main body so chat-app users get the same tailored
    guidance the desktop modal shows.
    """
    body = _format_auth_prompt_body(event)
    instructions_suffix = _format_instructions_suffix(event.get("instructions"))
    if instructions_suffix:
        return f"{body}{instructions_suffix}"
    return body


def _format_auth_prompt_body(event: Dict[str, Any]) -> str:
    mode = str(event.get("mode") or "").strip()
    display_name = str(
        event.get("display_name") or event.get("provider") or "a service"
    )

    if mode == "oauth":
        return _format_oauth_auth_code(event, display_name)
    if mode == "oauth_device":
        return _format_oauth_device_code(event, display_name)

    expires_at = event.get("expires_at")
    connect_url = event.get("connect_url")
    if isinstance(connect_url, str) and connect_url.strip():
        expiry = f"\n\nLink expires: {expires_at}" if expires_at else ""
        return (
            f"Credential setup requested for {display_name}.\n\n"
            f"Open this secure one-time link: {connect_url.strip()}"
            f"{expiry}\n\n"
            "Do not paste secrets into chat."
        )
    return (
        f"Credential setup requested for {display_name}, but this chat app "
        "cannot show the secure setup form because NYMERIA_PUBLIC_URL is not "
        "configured on the server. Use the desktop app's credential prompt or "
        "ask the server admin to set NYMERIA_PUBLIC_URL. Do not paste secrets "
        "into chat."
    )


def _format_instructions_suffix(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text:
        return ""
    if len(text) > _MAX_INSTRUCTIONS_RENDER_CHARS:
        text = (
            text[:_MAX_INSTRUCTIONS_RENDER_CHARS].rstrip()
            + "\n\n(truncated; open the desktop modal for full steps)"
        )
    return f"\n\nSteps:\n{text}"


def _format_oauth_auth_code(event: Dict[str, Any], display_name: str) -> str:
    auth_url = str(event.get("auth_url") or "").strip()
    if not auth_url:
        return (
            f"Sign-in requested for {display_name}, but the server did not "
            "provide an authorization URL. Ask the server admin to check the "
            "OAuth provider configuration."
        )
    minutes = _expiry_minutes(event.get("timeout_seconds"))
    expiry = (
        f"\n\nThis link expires in {minutes} minutes. Do not share it."
        if minutes
        else "\n\nDo not share this link."
    )
    return f"Sign in to {display_name}: {auth_url}{expiry}"


def _format_oauth_device_code(event: Dict[str, Any], display_name: str) -> str:
    user_code = str(event.get("user_code") or "").strip()
    verification_uri = str(
        event.get("verification_uri_complete") or event.get("verification_uri") or ""
    ).strip()
    if not user_code or not verification_uri:
        return (
            f"Sign-in requested for {display_name}, but the server did not "
            "provide a user code or verification URL. Ask the server admin to "
            "check the OAuth provider configuration."
        )
    minutes = _expiry_minutes(event.get("expires_in"))
    expiry = (
        f"\n\nThe code expires in {minutes} minutes."
        if minutes
        else ""
    )
    return (
        f"To connect {display_name}, open {verification_uri} on any device "
        f"and enter this code:\n\n    {user_code}{expiry}"
    )


def _expiry_minutes(value: Any) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return 0
    if seconds <= 0:
        return 0
    return max(1, seconds // 60)


_MAX_ARGS_PREVIEW_RENDER_CHARS = 300
"""Cap on the tool-args preview in bot-rendered approval prompts. The full
preview (up to 2000 chars) stays available on the REST surface; chat bubbles
only need enough to recognise the call."""


def format_hook_approval_message(event: Dict[str, Any]) -> str:
    """Render a default approval-request prompt for text chat surfaces.

    Used for the ``hook_approval`` autonomous event (a ``require_approval``
    hook holding a tool call). Surfaces with native buttons (Telegram inline
    keyboard, Discord view) render their own message and skip this; everything
    else falls back to this text plus the ``/hook approve|deny`` commands.
    """
    tool_name = str(event.get("tool_name") or "a tool")
    record_id = str(event.get("record_id") or "")
    prompt = str(event.get("prompt") or "").strip()
    preview = str(event.get("tool_args_preview") or "").strip()
    if len(preview) > _MAX_ARGS_PREVIEW_RENDER_CHARS:
        preview = preview[:_MAX_ARGS_PREVIEW_RENDER_CHARS].rstrip() + "..."
    lines = [f"Approval needed: the agent wants to run {tool_name}."]
    if prompt:
        lines.append(prompt)
    if preview:
        lines.append(f"Args: {preview}")
    window = _approval_window_seconds(event)
    deadline = f" within {window} seconds" if window else ""
    lines.append(
        f"Reply /hook approve {record_id} or /hook deny {record_id}"
        f"{deadline}. No answer means the call is denied."
    )
    return "\n\n".join(lines)


def _approval_window_seconds(event: Dict[str, Any]) -> int:
    """Best-effort window length from the record's created/expires stamps."""
    from datetime import datetime

    try:
        created = datetime.fromisoformat(str(event.get("created_at")))
        expires = datetime.fromisoformat(str(event.get("expires_at")))
        seconds = int((expires - created).total_seconds())
        return seconds if seconds > 0 else 0
    except (TypeError, ValueError):
        return 0


def format_fallback_prompt_message(event: Dict[str, Any]) -> str:
    """Render a default fallback-consent prompt for text chat surfaces.

    Used for the ``fallback_prompt`` autonomous event (a parked turn asking
    whether to swap to the fallback model). Turns originating on Telegram or
    Discord park like GUI turns (those bots render inline buttons over this
    text body); other bot platforms auto-swap without parking, so on those
    surfaces this text reaches users watching a GUI/CLI thread from a
    secondary chat binding. The resolve commands work from anywhere.
    """
    record_id = str(event.get("record_id") or "")
    from_model = str(event.get("from_model") or "the primary model")
    to_model = str(event.get("to_model") or "the fallback model")
    if str(event.get("kind") or "") == "refusal":
        first = (
            f"{from_model} refused this turn (safety classifier). "
            f"Swap to {to_model} and continue?"
        )
    else:
        first = (
            f"{from_model} keeps failing "
            f"({event.get('reason') or 'provider error'}). "
            f"Swap this thread to {to_model}?"
        )
    window = _approval_window_seconds(event)
    deadline = f" within {window} seconds" if window else ""
    return (
        f"{first}\n\nReply /fallback approve {record_id} [minutes|permanent] "
        f"or /fallback deny {record_id}{deadline}. No answer swaps "
        f"automatically."
    )


def fallback_hold_phrase(hold_seconds: Any, permanent: Any) -> str:
    """Human phrase for how long a fallback hold lasts.

    The single hold-copy authority for text surfaces: the bots and the CLI
    (reducer + repl form notes) all import it, so durations can never render
    differently across surfaces."""
    if permanent:
        return "until reverted"
    try:
        seconds = int(hold_seconds or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return "for this turn"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return f"for {hours} hour" + ("s" if hours != 1 else "")
    if seconds >= 60:
        return f"for {seconds // 60} min"
    return f"for {seconds}s"


def format_provider_fallback_message(event: Dict[str, Any]) -> str:
    """Render an applied model-swap notice for text chat surfaces.

    Used for the ``provider_fallback`` event (any consent mode), by the bots
    and by the CLI reducer's transcript line. Refusal swaps get distinct
    copy: a refusal is a clean HTTP 200 whose safety classifier flagged the
    request (often a false positive), not a provider error. The ``/fallback
    revert`` line keeps the swap manageable from surfaces without a Revert
    button. ``rewound`` marks the mid-stream recovery shape, where text
    already delivered was superseded by the re-driven turn.
    """
    target = str(event.get("to_model") or "the fallback model")
    hold = fallback_hold_phrase(event.get("hold_seconds"), event.get("permanent"))
    superseded = (
        " Partial output already shown was superseded."
        if event.get("rewound")
        else ""
    )
    if str(event.get("reason") or "") == "refusal":
        source = str(event.get("from_model") or "The model")
        return (
            f"{source} refused this turn (safety classifier, not an error); "
            f"switched to {target} {hold}. Revert with /fallback revert."
            f"{superseded}"
        )
    detail = str(event.get("reason") or "provider error")
    http_status = event.get("http_status")
    if http_status:
        detail = f"{detail}, HTTP {http_status}"
    return (
        f"Switched to fallback {target} {hold} ({detail}). "
        f"Revert with /fallback revert.{superseded}"
    )


# ---------------------------------------------------------------------------
# Handler protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SSEEventHandler(Protocol):
    """Platform-specific rendering callbacks for SSE events.

    The dispatcher calls ``flush_text(final=True)`` **before** status
    events (compacting, compacted, context_attached, error, tool_reload)
    so handlers don't need to remember to flush.
    """

    async def flush_text(self, final: bool = False) -> None:
        """Flush buffered response text."""
        ...

    async def on_thinking(self) -> None: ...

    async def on_response_chunk(self, content: str) -> None: ...

    async def on_compacting(self, message: str) -> None: ...

    async def on_compacted(
        self,
        summary: str,
        messages_removed: int,
        title: str,
    ) -> None: ...

    async def on_tool_call(
        self,
        name: str,
        args: Dict[str, Any],
        call_id: str,
        count: int,
    ) -> None: ...

    async def on_tool_result(
        self,
        call_id: str,
        result: str,
        attachments: List[str],
    ) -> None: ...

    async def on_tool_reload(self, tools: List[str], ttl: str) -> None: ...

    async def on_workspace_artifact(self, path: str) -> None: ...

    async def on_error(self, content: str) -> None: ...

    async def on_iteration_limit(self, content: str) -> None: ...

    async def on_turn_rewound(self, content: str) -> None:
        """A refused turn was rewound server-side; deliver the explanation.

        The backend already removed the refused exchange from the checkpoint
        (backlog #105), so bot handlers only need to send *content* as the
        turn's reply text.
        """
        ...

    async def on_done(self, tool_call_count: int) -> None: ...

    async def on_stream_end(self, tool_call_count: int) -> None: ...


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


async def dispatch_event(
    event: Dict[str, Any],
    handler: SSEEventHandler,
    tool_call_count: int,
) -> int:
    """Route a single SSE event to the matching handler callback.

    Returns the (possibly incremented) *tool_call_count* so callers can
    thread it through a sequence of events.
    """
    etype = event.get("type", "")

    if etype == "thinking" or etype == "tool_call_delta":
        await handler.on_thinking()

    elif etype == "dispatched":
        dispatched_to = event.get("dispatched_to")
        target = dispatched_to if isinstance(dispatched_to, dict) else {}
        title = str(event.get("title") or target.get("title") or "thread")
        await handler.on_response_chunk(f"[Response from {title}]\n\n")

    elif etype == "response":
        content = event.get("content", "")
        if content:
            await handler.on_response_chunk(content)

    elif etype == "auth_prompt":
        await handler.flush_text(final=True)
        callback = getattr(handler, "on_auth_prompt", None)
        if callable(callback):
            result = callback(event)
            if inspect.isawaitable(result):
                await result
        else:
            await handler.on_response_chunk(format_auth_prompt_message(event))
            await handler.flush_text(final=True)

    elif etype == "hook_approval":
        await handler.flush_text(final=True)
        callback = getattr(handler, "on_hook_approval", None)
        if callable(callback):
            result = callback(event)
            if inspect.isawaitable(result):
                await result
        else:
            await handler.on_response_chunk(format_hook_approval_message(event))
            await handler.flush_text(final=True)

    elif etype == "hook_approval_resolved":
        # Surfaces with a live approval UI (buttons, form) drop it here; text
        # surfaces have nothing to retract, so the default is silence.
        callback = getattr(handler, "on_hook_approval_resolved", None)
        if callable(callback):
            result = callback(event)
            if inspect.isawaitable(result):
                await result

    elif etype == "fallback_prompt":
        await handler.flush_text(final=True)
        callback = getattr(handler, "on_fallback_prompt", None)
        if callable(callback):
            result = callback(event)
            if inspect.isawaitable(result):
                await result
        else:
            await handler.on_response_chunk(format_fallback_prompt_message(event))
            await handler.flush_text(final=True)

    elif etype == "fallback_prompt_resolved":
        # Surfaces with a live consent UI drop their card here; text surfaces
        # have nothing to retract, so the default is silence.
        callback = getattr(handler, "on_fallback_prompt_resolved", None)
        if callable(callback):
            result = callback(event)
            if inspect.isawaitable(result):
                await result

    elif etype == "provider_fallback":
        # An applied model swap (any consent mode). Bots with inline buttons
        # override the callback to attach a Revert button; the default is the
        # plain notice with /fallback revert as the management path.
        await handler.flush_text(final=True)
        callback = getattr(handler, "on_provider_fallback", None)
        if callable(callback):
            result = callback(event)
            if inspect.isawaitable(result):
                await result
        else:
            await handler.on_response_chunk(format_provider_fallback_message(event))
            await handler.flush_text(final=True)

    elif etype == "reply_suppressed":
        # The react tool asked to hide the turn's reply text (backlog #45).
        # No flush here: suppression means DROP the pending buffer, which the
        # handler does itself. Optional callback so non-bot consumers (CLI,
        # GUIs) that render tool calls verbatim are untouched.
        callback = getattr(handler, "on_reply_suppressed", None)
        if callable(callback):
            result = callback()
            if inspect.isawaitable(result):
                await result

    elif etype == "compacting":
        await handler.flush_text(final=True)
        await handler.on_compacting(
            event.get("message") or COMPACTING_MESSAGE,
        )

    elif etype == "compacted":
        await handler.flush_text(final=True)
        await handler.on_compacted(
            event.get("summary", ""),
            int(event.get("messages_removed") or 0),
            title="Context compacted",
        )

    elif etype == "context_attached":
        await handler.flush_text(final=True)
        await handler.on_compacted(
            event.get("summary", ""),
            0,
            title="Context summary attached",
        )

    elif etype == "tool_call":
        tool_call_count += 1
        await handler.on_tool_call(
            name=event.get("name", "?"),
            args=event.get("args", {}),
            call_id=event.get("id", ""),
            count=tool_call_count,
        )

    elif etype == "tool_result":
        result = event.get("result", "")
        await handler.on_tool_result(
            call_id=event.get("id", ""),
            result=result,
            attachments=parse_attach_paths(result),
        )

    elif etype == "tool_reload":
        await handler.flush_text(final=True)
        await handler.on_tool_reload(
            tools=event.get("tools", []),
            ttl=event.get("ttl", ""),
        )

    elif etype == "workspace_artifact":
        path = event.get("path")
        if isinstance(path, str) and path:
            await handler.on_workspace_artifact(path)

    elif etype == "error":
        await handler.flush_text(final=True)
        await handler.on_error(event.get("content", "Unknown error"))

    elif etype == "iteration_limit":
        content = event.get("content", "")
        if content:
            await handler.on_iteration_limit(content)

    elif etype == "turn_rewound":
        await handler.flush_text(final=True)
        content = event.get("content", "")
        if content:
            # getattr-guarded so an out-of-tree handler predating this event
            # still delivers the explanation (as a plain notice) instead of
            # crashing the consumer loop with an AttributeError.
            callback = getattr(handler, "on_turn_rewound", None)
            if callback is not None:
                await callback(content)
            else:
                await handler.on_iteration_limit(content)

    elif etype == "done":
        await handler.on_done(tool_call_count)

    return tool_call_count


async def consume_sse_stream(
    events: AsyncIterable[Dict[str, Any]],
    handler: SSEEventHandler,
) -> None:
    """Consume an async SSE event stream and dispatch to *handler*.

    After the stream is exhausted (or on ``done``),
    ``handler.on_stream_end()`` is called so the handler can flush any
    remaining buffered text.
    """
    tool_call_count = 0
    async for event in events:
        tool_call_count = await dispatch_event(event, handler, tool_call_count)
    await handler.on_stream_end(tool_call_count)


# ── Interactive chat stream with dropped-turn recovery (backlog #88) ────────

# Exception shapes that mean "the connection dropped", not "the request was
# rejected" (mirrors the CLI transport's CONNECTION_ERRORS).
CHAT_CONNECTION_ERRORS: tuple = (httpx.TransportError,)

# Turn-recovery backoff (same posture as the CLI/GUI clients).
CHAT_RECOVERY_BASE_DELAY_SECONDS = 2.0
CHAT_RECOVERY_MAX_DELAY_SECONDS = 15.0
CHAT_RECOVERY_MAX_ATTEMPTS = 20

TURN_LOST_MESSAGE = (
    "Lost connection while this reply was streaming and could not rejoin "
    "it. The turn may still finish on the server; its result is saved to "
    "the thread history."
)


def _exc_status_code(exc: Exception) -> Optional[int]:
    return getattr(getattr(exc, "response", None), "status_code", None)


async def _safe_stream_end(handler: SSEEventHandler, tool_call_count: int) -> None:
    """``on_stream_end`` that cannot escape to the caller.

    Used on every path where the turn HAS identity: a raise here would land
    in the caller's except and re-POST a turn whose reply already landed.
    """
    try:
        await handler.on_stream_end(tool_call_count)
    except Exception:  # noqa: BLE001
        logger.warning("Handler on_stream_end failed", exc_info=True)


async def consume_chat_stream_with_recovery(
    api: Any,
    handler: SSEEventHandler,
    *,
    message: str,
    thread_id: str,
    user_id: str,
    chat_kwargs: Optional[Dict[str, Any]] = None,
) -> str:
    """Consume ``POST /chat`` SSE with CLI-style dropped-turn recovery.

    The bot-side port of the CLI transport's recovery loop
    (``triggers/cli/transport/api.py::stream_chat``): a connection drop
    mid-turn does not end the turn server-side, so this re-attaches via
    ``GET /threads/{id}/turn/stream`` and resumes dispatching from the last
    seen ``seq`` instead of losing the tail (or worse, re-POSTing the
    prompt and running the turn twice, the pre-#88 bot behavior).

    Contract with the caller:

    - Any exception raised while the turn has no identity is re-raised:
      the caller's legacy sync-``chat`` fallback and its platform branches
      (e.g. the 429 capacity shed) keep working. That covers real pre-turn
      failures (the prompt never became a held turn, so a re-POST is not a
      duplicate) but ALSO two identity-less stream shapes where a re-POST
      WOULD duplicate, so callers must guard those themselves:
      ``is_self_invoke`` turns (the chat route withholds the wire
      ``turn_started`` for them) and in-process adapters (Teams/WhatsApp),
      which never synthesize ``turn_started`` at all.
    - After ``turn_started``, this function owns the outcome and never
      raises (besides ``CancelledError``): it recovers, finishes, or
      renders the honest turn-lost error through ``handler.on_error``,
      and a raising ``on_stream_end`` is contained rather than handed to
      the caller (which would re-POST a turn whose reply already landed).
      A handler exception mid-dispatch also lands in recovery; the cursor
      already advanced past the poison event, so replay skips it.
    - The ``consume_sse_stream`` contract is preserved: every renderable
      event goes through :func:`dispatch_event` exactly once and
      ``on_stream_end`` fires exactly once at the true end.

    Returns one of ``"completed"`` (terminal event on the primary stream),
    ``"queued"``, ``"dispatched"``, ``"recovered"`` (finished via
    re-attach), or ``"lost"``. No production caller branches on it today;
    it exists for tests and observability.
    """
    kwargs = dict(chat_kwargs or {})
    tool_call_count = 0
    turn_id: Optional[str] = None
    last_seq = 0
    terminal_seen = False
    prompt_queued_seen = False
    dispatched_seen = False
    recovery_cause: Exception
    try:
        async for event in api.chat_stream(message, thread_id, user_id, **kwargs):
            etype = event.get("type")
            if etype == "turn_started":
                new_id = event.get("turn_id")
                if isinstance(new_id, str) and new_id:
                    turn_id = new_id
            seq = event.get("seq")
            if isinstance(seq, int) and seq > last_seq:
                last_seq = seq
            if etype in ("done", "error"):
                terminal_seen = True
            elif etype == "prompt_queued":
                # ONLY prompt_queued proves the queued outcome; the legacy
                # bare "queued" event also fires on paths that still become
                # the holder (see the CLI transport's identical note).
                prompt_queued_seen = True
            elif etype == "dispatched":
                dispatched_seen = True
            tool_call_count = await dispatch_event(event, handler, tool_call_count)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - split into raise vs recover below
        if turn_id is None:
            raise
        if terminal_seen:
            # The turn already finished on the wire; a drop during the
            # server's stream close has nothing left to recover.
            await _safe_stream_end(handler, tool_call_count)
            return "completed"
        recovery_cause = exc
    else:
        if terminal_seen or prompt_queued_seen or dispatched_seen or turn_id is None:
            # Ended properly, was a queued/ack shape that never held a turn,
            # or was dispatched to another thread (whose buffer this stream
            # cannot poll; the CLI finalizes locally for the same reason).
            if turn_id is None:
                # No identity: a raise here goes to the caller's legacy
                # fallback, the pre-#88 status quo for identity-less shapes.
                await handler.on_stream_end(tool_call_count)
            else:
                await _safe_stream_end(handler, tool_call_count)
            if prompt_queued_seen:
                return "queued"
            if dispatched_seen:
                return "dispatched"
            return "completed"
        # Clean stream end WITHOUT a terminal event on a held turn: the
        # server withholds the wire terminal when its turn-end disconnect
        # probe latched, but the buffered tail (including done) is
        # replayable; drain it through the same recovery path.
        recovery_cause = RuntimeError("chat stream ended without a terminal event")

    if not (
        callable(getattr(api, "reattach_turn_stream", None))
        and callable(getattr(api, "get_thread_status", None))
    ):
        # In-process adapters (Teams/WhatsApp) and older fakes cannot
        # re-attach; degrade to the honest message, never silent truncation.
        logger.warning(
            "Chat stream for thread %s dropped mid-turn and this client "
            "cannot re-attach (%s)",
            thread_id,
            recovery_cause,
        )
        return await _finish_turn_lost(handler, tool_call_count)

    logger.info(
        "Chat stream for thread %s interrupted mid-turn (%s); recovering",
        thread_id,
        recovery_cause,
    )
    attempt = 0
    while attempt < CHAT_RECOVERY_MAX_ATTEMPTS:
        attempt += 1
        status: Optional[Dict[str, Any]] = None
        try:
            raw_status = await api.get_thread_status(thread_id, user_id=user_id)
            status = raw_status if isinstance(raw_status, dict) else None
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - backend unreachable; keep retrying
            status = None

        if status is not None:
            raw_turn = status.get("turn")
            turn = raw_turn if isinstance(raw_turn, dict) else None
            turn_matches = turn is not None and (
                not turn_id or turn.get("turn_id") == turn_id
            )
            if turn is not None and turn_matches:
                outcome: Optional[str] = None
                progressed = False
                stream = None
                try:
                    stream = api.reattach_turn_stream(
                        thread_id,
                        user_id=user_id,
                        turn_id=str(turn.get("turn_id") or "") or None,
                        from_seq=last_seq,
                    )
                    while True:
                        try:
                            # Liveness bound (same rationale as the attach
                            # class above): a holder that dies without a
                            # terminal event must not park this recovery
                            # forever on a silent stream. The client's SSE
                            # read timeout is None, so this is the only
                            # bound.
                            event = await asyncio.wait_for(
                                stream.__anext__(),
                                timeout=ATTACH_IDLE_TIMEOUT_SECONDS,
                            )
                        except StopAsyncIteration:
                            break
                        etype = event.get("type")
                        if etype in ("turn_attach", "turn_started"):
                            continue
                        if etype == "turn_replay_gap":
                            # Overflow evicted events past our cursor
                            # mid-stream; the remainder cannot be replayed
                            # faithfully and the server ends the stream.
                            outcome = "gone"
                            continue
                        seq = event.get("seq")
                        if isinstance(seq, int) and seq > last_seq:
                            last_seq = seq
                        tool_call_count = await dispatch_event(
                            event, handler, tool_call_count
                        )
                        # Only a DISPATCHED event resets the attempt budget:
                        # counting before dispatch would hand a handler that
                        # raises on every event one fresh re-attach per
                        # event (unbounded for long turns) instead of the
                        # bounded budget.
                        progressed = True
                        if etype in ("done", "error"):
                            outcome = "finished"
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    # Silent for the whole liveness bound while status still
                    # names this turn: presume the writer is dead.
                    outcome = "gone"
                except CHAT_CONNECTION_ERRORS:
                    outcome = None  # dropped again: back off and retry
                except Exception as exc:  # noqa: BLE001
                    if _exc_status_code(exc) in (404, 410):
                        # Buffer replaced/expired or replay gap: the rest of
                        # this turn cannot be recovered here.
                        outcome = "gone"
                    else:
                        outcome = None
                else:
                    if outcome != "finished":
                        # Clean stream end without a terminal event: the
                        # turn's writer died without finishing.
                        outcome = "gone"
                finally:
                    if stream is not None:
                        try:
                            await stream.aclose()
                        except Exception:  # noqa: BLE001 - best-effort close
                            pass
                if outcome == "finished":
                    await _safe_stream_end(handler, tool_call_count)
                    return "recovered"
                if outcome == "gone":
                    break
                if progressed:
                    attempt = 0
            elif not status.get("processing"):
                # Turn is gone (API restart, buffer expired, or another turn
                # already ran).
                break
            # else: thread busy with an unattachable turn; keep waiting.

        await asyncio.sleep(
            min(
                CHAT_RECOVERY_BASE_DELAY_SECONDS * attempt,
                CHAT_RECOVERY_MAX_DELAY_SECONDS,
            )
        )

    return await _finish_turn_lost(handler, tool_call_count)


async def _finish_turn_lost(handler: SSEEventHandler, tool_call_count: int) -> str:
    """Best-effort honest ending for an unrecoverable turn.

    Never raises: at this point re-raising would send the caller into its
    sync-``chat`` fallback and run the turn a second time.
    """
    try:
        await handler.flush_text(final=True)
        await handler.on_error(TURN_LOST_MESSAGE)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to render turn-lost notice", exc_info=True)
    try:
        await handler.on_stream_end(tool_call_count)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to finish handler after turn loss", exc_info=True)
    return "lost"


# ── Autonomous delivery honesty + the delivery report (backlog #247) ────────

# Default strong-reference store for fire-and-forget report tasks: the event
# loop keeps only weak references, so a bare create_task can be GC'd
# mid-flight and the report silently lost. Bots with their own background
# machinery (Telegram's _spawn_background_task) pass it as ``spawn`` instead.
_REPORT_TASKS: set = set()


def _spawn_report_task(coro: Any) -> Any:
    task = asyncio.get_running_loop().create_task(coro)
    _REPORT_TASKS.add(task)
    task.add_done_callback(_REPORT_TASKS.discard)
    return task


def finish_autonomous_delivery(
    handler: Any,
    api: Any,
    *,
    platform: str,
    label: str,
    target: str,
    thread_id: str,
    via: str,
    todo_id: Optional[str] = None,
    report: bool = True,
    spawn: Optional[Callable[[Any], Any]] = None,
    log: Optional[logging.Logger] = None,
) -> Optional[str]:
    """Honest completion line + delivery report for one autonomous turn.

    Shared by the Telegram and Discord bots (their pre-#247 completion
    lines logged "Streamed autonomous result" unconditionally, where
    "delivered" only ever meant "the stream ended"). Reads the handler's
    content-send counters (``_sends_attempted`` / ``_sends_ok`` /
    ``_first_send_error``), logs the honest outcome through the BOT's
    logger (``log``), and for TODO-driven turns fires the delivery report
    (fire-and-forget via ``spawn``; never disturbs delivery).

    A turn that errored is excluded from reporting on BOTH delivery paths:
    the firehose caller passes ``report=False`` from the completed event,
    and the attach path (which has no completed event here) is covered by
    the handler's ``_error_seen`` flag, set by its ``on_error`` callback.
    An errored occurrence already joined the ticker's #154 execution
    accounting; one bad occurrence must not feed both streaks.

    Returns the outcome string it classified (None when nothing was
    attempted), mainly for tests.
    """
    bot_log = log or logger
    attempted = int(getattr(handler, "_sends_attempted", 0))
    ok = int(getattr(handler, "_sends_ok", 0))
    failed = attempted - ok
    first_error = getattr(handler, "_first_send_error", None)
    suffix = " (turn attach)" if via == "attach" else ""
    outcome: Optional[str] = None
    if attempted == 0:
        bot_log.info(
            f"Autonomous turn for {label} had no deliverable output{suffix}"
        )
    elif failed == 0:
        outcome = "delivered"
        bot_log.info(f"Streamed autonomous result to {label}{suffix}")
    elif ok:
        outcome = "partial"
        bot_log.warning(
            f"Partially delivered autonomous result to {label}{suffix}: "
            f"{failed} of {attempted} sends failed; first error: {first_error}"
        )
    else:
        outcome = "failed"
        bot_log.error(
            f"Autonomous delivery to {label} FAILED{suffix}: all "
            f"{attempted} sends failed; first error: {first_error}"
        )
    if getattr(handler, "_error_seen", False):
        report = False
    if (
        outcome
        and report
        and todo_id
        and callable(getattr(api, "report_todo_delivery", None))
    ):
        (spawn or _spawn_report_task)(
            _report_todo_delivery(
                api,
                todo_id=str(todo_id),
                outcome=outcome,
                platform=platform,
                target=target,
                error=first_error,
                thread_id=thread_id,
                log=bot_log,
            )
        )
    return outcome


async def _report_todo_delivery(
    api: Any,
    *,
    todo_id: str,
    outcome: str,
    platform: str,
    target: str,
    error: Optional[str],
    thread_id: str,
    log: logging.Logger,
) -> None:
    """POST the delivery outcome (#247); never disturbs delivery."""
    try:
        await api.report_todo_delivery(
            todo_id,
            outcome=outcome,
            platform=platform,
            target=target,
            # The schema caps error at 500 chars; an oversized description
            # must degrade to truncation, not a 422 that drops the report.
            error=(error or None) and str(error)[:500],
            thread_id=thread_id,
        )
    except Exception as e:  # noqa: BLE001 - accounting is best-effort
        log.warning(
            "Failed to report TODO %s delivery outcome (%s): %s",
            todo_id,
            outcome,
            e,
        )


async def consume_autonomous_firehose(
    *,
    base_url: str,
    api_key: str,
    on_event: Callable[[Any], Awaitable[None]],
    log_label: str,
    logger: logging.Logger,
    should_stop: Optional[Callable[[], bool]] = None,
    initial_delay: float = 3,
    max_delay: float = 30,
) -> None:
    """Stream the autonomous-event firehose, dispatching each event to ``on_event``.

    Connects to ``GET {base_url}/autonomous/stream`` with the admin service token
    and ``X-Nymeria-Act-As: *`` (the wildcard firehose), parses each ``data:`` line
    via :func:`parse_sse_data_line`, and reconnects with a ``initial_delay`` ->
    ``max_delay`` exponential backoff. This is the shared body of the per-bot
    ``_api_sse_listener`` loops.

    Injected per caller:

    - ``on_event``: an async callback receiving each decoded event. Bots put their
      own routing here (e.g. multi-bot dispatch); returning early skips the event,
      equivalent to ``continue`` in the read loop.
    - ``should_stop``: an optional zero-arg predicate. While it returns ``True`` the
      listener exits (checked at the loop top, mid-stream before each event, and
      before each reconnect sleep). ``None`` means never stop, i.e. ``while True``
      relying on task cancellation, which is the Telegram listener's behavior.
    - ``log_label``: prefixes the connect/connected/stopped log lines (e.g. "API").
    - ``logger``: the caller's module logger, passed in so log records keep each
      bot's provenance rather than this module's name.
    """
    url = f"{base_url}/autonomous/stream"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Nymeria-Act-As": "*",
    }

    def stopped() -> bool:
        return should_stop is not None and should_stop()

    logger.info(f"{log_label} SSE listener connecting to {url}")

    reconnect_delay = initial_delay

    while not stopped():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code != 200:
                        logger.error(f"SSE connection failed: {resp.status_code}")
                        await asyncio.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, max_delay)
                        continue

                    logger.info(f"{log_label} SSE connected, listening for events")
                    reconnect_delay = initial_delay

                    async for line in resp.aiter_lines():
                        if stopped():
                            return
                        event = parse_sse_data_line(line)
                        if event is None:
                            continue
                        await on_event(event)

        except httpx.ReadTimeout:
            logger.debug("SSE read timeout, reconnecting...")
        except httpx.ConnectError:
            logger.warning(
                f"Cannot reach API at {base_url}, retrying in {reconnect_delay}s"
            )
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"SSE listener error: {e}", exc_info=True)

        if not stopped():
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_delay)

    logger.info(f"{log_label} SSE listener stopped")


# ── Autonomous turn attach (backlog #91, attach-preferred delivery) ─────────

# Meta events the attach stream carries that the bots' per-thread handlers
# must not render: attach/turn bookkeeping plus queue-state transitions
# (already invisible on the firehose path via task_started gating).
ATTACH_SKIP_EVENT_TYPES = frozenset({
    "turn_attach",
    "turn_started",
    "llm_call_started",
    "queued",
    "prompt_queued",
    "prompt_injected",
    "prompt_absorbed",
    "turn_halted",
    "turn_replay_gap",
    "fanout_dropped",
})

# Liveness bound on the attach stream: if the buffer yields nothing for this
# long the holder is presumed dead without a terminal event (hard process
# kill) and the attach degrades instead of suppressing the thread forever.
# Must exceed the longest legitimately quiet span (tool calls are capped at
# 600s by the dispatch clamp).
ATTACH_IDLE_TIMEOUT_SECONDS = 900.0


class AutonomousTurnAttach:
    """Attach-preferred autonomous delivery for firehose bot consumers.

    One instance per (thread, holder turn), created by a bot when an
    unmarked ``task_started`` arrives on the firehose. :meth:`run` consumes
    ``GET /threads/{id}/turn/stream`` (byte-identical replay from seq 0 plus
    live tail) and drives the bot's existing per-thread ``SSEEventHandler``
    through :func:`dispatch_event`, so rendering is identical to the
    interactive chat stream instead of a reconstruction from converted bus
    events.

    Protocol with the owning bot:

    - While ``state`` is ``pending``/``live``/``delivered``, the bot
      suppresses firehose transcript events for the thread and routes
      ``task_completed`` to :meth:`note_task_completed` (which returns True
      when the attach owns delivery). ``fallback`` hands everything back to
      the legacy firehose path.
    - A turn that is not attachable (404 ``turn_not_found`` / 410
      ``turn_replay_gap`` before anything rendered) flips to ``fallback``;
      a ``task_completed`` stashed while pending is re-delivered through the
      bot-provided ``deliver_fallback_completed`` so the turn is never lost
      (this also keeps non-buffered turns, e.g. headless workflow TODOs,
      delivering exactly as before).
    - Mid-stream failure after rendering retries once from the last seen
      ``seq``; a second failure flushes what already rendered rather than
      risking a double delivery via the completion fallback.

    The instance owns finalization (flush + tool-call footer + completion
    log); the per-thread state entry is popped once BOTH the attach has
    delivered and the firehose ``task_completed`` has been observed,
    whichever order they arrive in.
    """

    def __init__(
        self,
        *,
        api: Any,
        thread_id: str,
        handler: Any,
        pop_state: Callable[[], Any],
        log_delivered: Callable[[], None],
        deliver_fallback_completed: Callable[[Dict[str, Any]], Awaitable[None]],
        act_as: Optional[str] = None,
        finalize: Optional[Callable[[], Awaitable[None]]] = None,
        task_id: Optional[str] = None,
    ) -> None:
        self._api = api
        self._thread_id = thread_id
        self._handler = handler
        self._pop_state = pop_state
        self._log_delivered = log_delivered
        self._deliver_fallback = deliver_fallback_completed
        self._act_as = act_as
        self._finalize_override = finalize
        self._task_id = task_id
        self._turn_id: Optional[str] = None
        self.state = "pending"
        self._completed_seen = False
        self._completed_event: Optional[Dict[str, Any]] = None

    @property
    def suppresses_firehose(self) -> bool:
        """True while the attach (not the legacy path) owns this thread."""
        return self.state != "fallback"

    def note_task_completed(self, event: Dict[str, Any]) -> bool:
        """Record a firehose ``task_completed``; True when attach owns it."""
        if self.state == "fallback":
            return False
        event_task = event.get("task_id")
        if self._task_id and event_task and event_task != self._task_id:
            # A superseded turn's completion: the two turns' lifecycle
            # events crossed a turn boundary and that turn's own (orphaned)
            # attach already owned its rendering. Delivering its aggregate
            # content here would double it; consume and drop.
            return True
        self._completed_seen = True
        self._completed_event = event
        if self.state == "delivered":
            self._pop_state()
        return True

    async def run(self) -> None:
        """Consume the attach stream until the turn ends; never raises."""
        last_seq = 0
        rendered = False
        retried = False
        while True:
            stream = None
            try:
                stream = self._api.reattach_turn_stream(
                    self._thread_id,
                    self._act_as,
                    turn_id=self._turn_id,
                    from_seq=last_seq,
                )
                while True:
                    try:
                        # Liveness bound: a holder that dies without a
                        # terminal event must not wedge this attach (and
                        # with it the thread's firehose suppression).
                        event = await asyncio.wait_for(
                            stream.__anext__(),
                            timeout=ATTACH_IDLE_TIMEOUT_SECONDS,
                        )
                    except StopAsyncIteration:
                        break
                    etype = event.get("type", "")
                    if etype == "turn_attach":
                        self.state = "live"
                        attached_turn = event.get("turn_id")
                        if attached_turn:
                            # Pin retries to this turn: a retry landing on
                            # a NEWER turn's buffer must 404 instead of
                            # silently tailing the wrong turn from a stale
                            # seq cursor.
                            self._turn_id = attached_turn
                        continue
                    seq = event.get("seq")
                    if isinstance(seq, int):
                        last_seq = seq
                    if etype in ATTACH_SKIP_EVENT_TYPES:
                        continue
                    if etype == "done":
                        break
                    rendered = True
                    self._handler._tool_count = await dispatch_event(
                        event, self._handler, self._handler._tool_count
                    )
                await self._finish_delivered()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - delivery must degrade, not die
                status = getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                if status in (404, 410) and not rendered:
                    await self._enter_fallback(exc)
                    return
                if not retried:
                    retried = True
                    await asyncio.sleep(0.5)
                    continue
                if rendered:
                    logger.warning(
                        "[TURN ATTACH] thread=%s failed mid-stream after "
                        "rendering (%s); flushing partial delivery",
                        self._thread_id,
                        exc,
                    )
                    await self._finish_delivered()
                    return
                await self._enter_fallback(exc)
                return
            finally:
                if stream is not None:
                    try:
                        await stream.aclose()
                    except Exception:  # noqa: BLE001 - best-effort close
                        pass

    async def _finish_delivered(self) -> None:
        handler = self._handler
        try:
            if self._finalize_override is not None:
                # Bot-specific footer/flush shape (e.g. Discord's subtext
                # footer and message-edit case).
                await self._finalize_override()
            elif getattr(handler, "_reply_suppressed", False):
                handler._text_buffer = ""
            else:
                tool_count = getattr(handler, "_tool_count", 0)
                if tool_count:
                    footer = f"\n\n_Tool calls: {tool_count}_"
                    handler._text_buffer = (
                        handler._text_buffer + footer
                        if handler._text_buffer
                        else footer
                    )
                await handler.flush_text(final=True)
            self._log_delivered()
        except Exception:  # noqa: BLE001 - never leave the state machine wedged
            logger.warning(
                "[TURN ATTACH] thread=%s finalize failed", self._thread_id,
                exc_info=True,
            )
        self.state = "delivered"
        if self._completed_seen:
            self._pop_state()

    async def _enter_fallback(self, exc: Exception) -> None:
        logger.info(
            "[TURN ATTACH] thread=%s not attachable (%s); using firehose "
            "fallback",
            self._thread_id,
            exc,
        )
        self.state = "fallback"
        completed = self._completed_event
        self._completed_event = None
        if completed is not None:
            try:
                await self._deliver_fallback(completed)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "[TURN ATTACH] thread=%s fallback completion delivery "
                    "failed",
                    self._thread_id,
                    exc_info=True,
                )
