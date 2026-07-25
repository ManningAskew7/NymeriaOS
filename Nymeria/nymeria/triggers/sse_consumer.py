"""Shared SSE event consumer for bot and CLI trigger platforms.

Centralises event-type routing, field extraction, tool-call counting,
and the "flush before status" discipline so that individual platform
handlers only implement rendering.

Usage — interactive chat stream::

    handler = MyPlatformHandler(...)
    await consume_sse_stream(api.chat_stream(...), handler)

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
    whether to swap to the fallback model). Bot-origin turns never park
    (they auto-swap), so this text mostly reaches users watching a GUI/CLI
    thread from a secondary text surface; the resolve commands still work
    from anywhere.
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
