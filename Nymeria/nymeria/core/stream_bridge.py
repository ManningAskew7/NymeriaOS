"""Sync bridges for consuming NymeriaAgent async streams from worker code."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Mapping, Optional

logger = logging.getLogger(__name__)


@dataclass
class StreamCollection:
    """Collected state from a consumed agent stream."""

    response_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    chunk_count: int = 0
    tool_call_count: int = 0
    iteration_limit_hit: bool = False
    iteration_limit_event: Optional[Dict[str, Any]] = None
    # True once the consumed stream yielded ``prompt_queued``: this consumer
    # became a QUEUER, and everything after is the holder turn's output
    # fanned in via the pending-prompt mailbox, not a turn this consumer
    # owns. Finalizers use it to stamp ``fanout`` on their task_completed.
    fanout_observed: bool = False

    def response_text(self, *, fallback_to_thinking: bool = True) -> str:
        """Return response text, optionally falling back to thinking chunks."""
        if self.response_parts:
            return "".join(self.response_parts)
        if fallback_to_thinking and self.thinking_parts:
            return "".join(self.thinking_parts)
        return ""


StreamChunkCallback = Callable[[Dict[str, Any], StreamCollection], None]
StreamErrorMessageFactory = Callable[[Dict[str, Any]], str]
StreamIterationLimitPredicate = Callable[[Dict[str, Any]], bool]


class _TurnBufferTee:
    """Feed the per-thread ``TurnStreamBuffer`` for a LOCAL holder turn.

    Autonomous turns historically published only to the event bus; the
    per-thread turn buffer (re-attach + live watching, backlog #87/#90) was
    fed exclusively by the ``POST /chat`` route. This tee mirrors the
    route's buffer choreography for in-process turns consumed through
    ``stream_and_collect``: begin the buffer only once the turn actually
    holds the thread lock (``_on_turn_started``), synthesize the
    ``turn_started`` wire event, append each chunk in chat-wire shape
    (``{**chunk, "thread_id": ...}``), synthesize ``done`` on clean end,
    and always leave the buffer in a terminal state.

    Remote executors (the Docker worker's ``APIClientExecutor``) must NOT
    tee: their turns run in the API process via ``POST /chat``, whose route
    already buffers them. ``stream_and_collect`` enforces that split, so a
    turn is buffered exactly once in exactly the process that serves
    ``GET /threads/{id}/turn/stream``.

    Construction mutates ``astream_kwargs``: it injects (or chains) the
    ``_on_turn_started`` holder callback and mints ``_turn_user_message_id``
    when the caller did not provide one, so the wakeup HumanMessage carries
    the anchor id live-attach viewers trim against.
    """

    def __init__(self, executor: Any, astream_kwargs: Dict[str, Any]) -> None:
        self._executor = executor
        self._thread_id = str(astream_kwargs.get("thread_id") or "")
        self._user_id = str(astream_kwargs.get("user_id") or "")
        is_self_invoke = bool(astream_kwargs.get("_is_self_invoke"))
        self._holder_kind = "autonomous" if is_self_invoke else "user"
        self._user_message_internal = is_self_invoke
        label = (
            astream_kwargs.get("source_label")
            or astream_kwargs.get("_trigger_override")
            or astream_kwargs.get("source")
        )
        # The chat route only labels self-invoke turns; a user-holder turn
        # (e.g. a non-self-invoke callable handoff) carries no source label.
        self._source_label = str(label)[:80] if (is_self_invoke and label) else None

        anchor = astream_kwargs.get("_turn_user_message_id")
        if not anchor:
            anchor = str(uuid.uuid4())
            astream_kwargs["_turn_user_message_id"] = anchor
        self._user_message_id = anchor

        self._buffer: Optional[Any] = None
        self._started_pending = False

        caller_on_turn_started = astream_kwargs.get("_on_turn_started")

        def _on_turn_started() -> None:
            self._begin()
            if caller_on_turn_started is not None:
                caller_on_turn_started()

        astream_kwargs["_on_turn_started"] = _on_turn_started

    def _begin(self) -> None:
        from .turn_stream_buffer import get_turn_stream_registry

        self._buffer = get_turn_stream_registry().begin_turn(
            self._thread_id,
            self._user_id,
            user_message_id=self._user_message_id,
            holder_kind=self._holder_kind,
            source_label=self._source_label,
            user_message_internal=self._user_message_internal,
        )
        self._started_pending = True

    def record(self, chunk: Dict[str, Any]) -> None:
        """Append one stream chunk (chat-wire shape) to the holder buffer.

        No-op until the turn holds the thread lock (queued/absorbed calls
        never create a buffer, matching the chat route). Appends fresh
        dicts, so the caller's chunk (which also feeds ``on_chunk`` and the
        event bus) is never mutated with the buffer's ``seq`` stamp.
        """
        buffer = self._buffer
        if buffer is None:
            return
        if self._started_pending:
            self._started_pending = False
            buffer.append(
                {
                    "type": "turn_started",
                    "turn_id": buffer.turn_id,
                    "thread_id": self._thread_id,
                }
            )
        buffer.append({**chunk, "thread_id": self._thread_id})

    def finish_done(self) -> None:
        """Synthesize the terminal ``done`` event and close the buffer."""
        from .turn_stream_buffer import STATE_DONE

        buffer = self._buffer
        if buffer is None:
            return
        buffer.append(
            {
                "type": "done",
                "thread_id": self._thread_id,
                "context_stats": self._context_stats(),
                "model": self._effective_model(),
            }
        )
        buffer.finish(STATE_DONE)

    def finish_error(self) -> None:
        from .turn_stream_buffer import STATE_ERROR

        if self._buffer is not None:
            self._buffer.finish(STATE_ERROR)

    def finish_error_with_event(self, message: str) -> None:
        """Buffer a synthesized ``error`` event, then close as error.

        Mirrors the chat route's raised-exception shape (an ``error`` wire
        event followed by ``STATE_ERROR``) so a watcher attached to a
        failing autonomous turn sees the failure instead of a bare abort.
        No-op when the buffer is already terminal (the yielded-error path
        records the agent's own error chunk and finishes first).
        """
        from .turn_stream_buffer import STATE_ERROR, STATE_LIVE

        buffer = self._buffer
        if buffer is None:
            return
        if buffer.state == STATE_LIVE:
            buffer.append(
                {
                    "type": "error",
                    "content": message,
                    "thread_id": self._thread_id,
                }
            )
            buffer.finish(STATE_ERROR)

    def finish_aborted_if_live(self) -> None:
        """Terminal backstop for cancel/GeneratorExit paths (finish no-ops
        when a terminal state is already set)."""
        from .turn_stream_buffer import STATE_ABORTED

        if self._buffer is not None:
            self._buffer.finish(STATE_ABORTED)

    # -- done-payload garnish (best-effort, mirrors the chat route) ---------

    def _agent(self) -> Optional[Any]:
        return getattr(self._executor, "agent", None)

    def _context_stats(self) -> Optional[Dict[str, Any]]:
        agent = self._agent()
        if agent is None:
            return None
        try:
            return agent.get_context_stats(self._thread_id)
        except Exception as exc:  # noqa: BLE001 - garnish only
            logger.debug("[STREAM_BRIDGE] context stats unavailable: %s", exc)
            return None

    def _effective_model(self) -> Optional[str]:
        """Thread override, else the global default (the chat route's rule)."""
        agent = self._agent()
        if agent is None:
            return None
        try:
            cfg = agent._get_llm_config_for_thread(self._thread_id)
            return cfg.model or agent.settings.llm_model
        except Exception as exc:  # noqa: BLE001 - garnish only
            logger.debug("[STREAM_BRIDGE] model resolution failed: %s", exc)
            return None


def iter_agent_astream(agent: Any, **kwargs: Any) -> Iterator[Dict[str, Any]]:
    """Yield ``agent.astream(...)`` chunks from synchronous worker contexts.

    Autonomous workers, callable tools, and spawned-thread dispatch are sync
    call sites, but regular chat uses ``NymeriaAgent.astream()`` so async-only
    tools can run. This bridge preserves live streaming while letting those sync
    callers use the same async agent path.

    A single process-local background event loop owns the async consumption for
    sync callers. Provider SDKs such as Anthropic and OpenAI keep async HTTP
    transports inside model instances; creating and closing a fresh loop for
    each callable invocation can leave concurrent streams trying to close a
    transport tied to a loop that has already been closed.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        yield from _iter_in_bridge_loop(agent, **kwargs)
        return

    raise RuntimeError("iter_agent_astream() is only supported from synchronous code")


def stream_and_collect(
    agent: Any,
    *,
    astream_kwargs: Mapping[str, Any],
    on_chunk: Optional[StreamChunkCallback] = None,
    error_message_factory: Optional[StreamErrorMessageFactory] = None,
    should_mark_iteration_limit: Optional[StreamIterationLimitPredicate] = None,
) -> StreamCollection:
    """Consume ``agent.astream(...)`` and collect common stream state.

    Synchronous autonomous workers still own their caller-specific behavior
    (event-bus publishing, console output, logging, completion payloads), but
    they should not each reimplement the same stream iteration, response
    collection, error propagation, and iteration-limit bookkeeping.

    Local turns are additionally teed into the per-thread turn stream
    buffer (see ``_TurnBufferTee``) so any client can attach to them via
    ``GET /threads/{id}/turn/stream``; remote (relayed) turns are buffered
    by the API-side chat route instead.
    """
    from .turn_executor import wrap_for_stream

    collection = StreamCollection()
    kwargs = dict(astream_kwargs)
    executor = wrap_for_stream(agent)
    tee = None if executor.is_remote else _TurnBufferTee(executor, kwargs)

    # Local autonomous turns never carry a chat-platform origin: clear any
    # stale entry so the react tool refuses instead of reacting to an old
    # bot message (backlog #45; remote/relayed turns are cleared by the /chat
    # route in the API process, where the registry lives).
    if not executor.is_remote:
        _thread_id = kwargs.get("thread_id")
        if _thread_id:
            from .bot_reactions import clear_turn_origin

            clear_turn_origin(str(_thread_id))

    try:
        for chunk in iter_agent_astream(agent, **kwargs):
            chunk_type = chunk.get("type")
            collection.chunk_count += 1

            if chunk_type == "prompt_queued":
                # This consumer's prompt was enqueued behind a running
                # holder turn. Everything the stream yields from here on is
                # the HOLDER's output fanned in via the pending-prompt
                # mailbox (``pending_prompt_queue.py``), so chunks handed to
                # ``on_chunk`` are marked ``fanout`` below: bus publishers
                # forward the marker, letting consumers (bots, CLI, GUIs)
                # distinguish this mirror from the holder's own stream and
                # skip it, instead of rendering the same turn twice.
                # ``prompt_queued`` itself (and ``queued`` before it) stay
                # unmarked: they describe THIS consumer, not the holder.
                # The collection keeps aggregating marked chunks, because
                # feeding the queuer's task_completed content is the reason
                # the fanout exists.
                collection.fanout_observed = True

            if chunk_type == "tool_call":
                collection.tool_call_count += 1
            elif chunk_type == "thinking":
                content = chunk.get("content", "")
                if content:
                    collection.thinking_parts.append(content)
            elif chunk_type == "response":
                content = chunk.get("content", "")
                if content:
                    collection.response_parts.append(content)
            elif chunk_type == "iteration_limit":
                should_mark = (
                    True
                    if should_mark_iteration_limit is None
                    else should_mark_iteration_limit(chunk)
                )
                if should_mark:
                    collection.iteration_limit_hit = True
                    collection.iteration_limit_event = dict(chunk)

            if tee is not None:
                # Original, unmarked chunk: a queuer never becomes holder,
                # so its tee has no buffer and record() is a no-op anyway.
                tee.record(chunk)

            if on_chunk is not None:
                if collection.fanout_observed and chunk_type != "prompt_queued":
                    # Marked copy; never mutate the shared chunk dict (the
                    # tee, the collection, and the caller all read it).
                    on_chunk({**chunk, "fanout": True}, collection)
                else:
                    on_chunk(chunk, collection)

            if chunk_type == "error":
                if error_message_factory is not None:
                    message = error_message_factory(chunk)
                else:
                    error_content = chunk.get("content", "")
                    error_code = chunk.get("code", "unknown")
                    message = error_content or f"Agent stream error (code={error_code})"
                if tee is not None:
                    tee.finish_error()
                error = RuntimeError(message)
                # The collection dies with this raise; carry the fanout
                # latch on the exception so publishers' error tails can
                # stamp their task_completed (a fanned-in holder error is
                # still a mirror; an unmarked error bookend would deliver
                # a second error line).
                error.fanout_observed = collection.fanout_observed  # type: ignore[attr-defined]
                raise error

        if tee is not None:
            tee.finish_done()
    except (asyncio.CancelledError, concurrent.futures.CancelledError):
        # Cooperative abort: fall to the aborted backstop below. Listed
        # explicitly because the bridge surfaces a cancelled future as
        # concurrent.futures.CancelledError, which is an Exception subclass
        # on some stdlib layouts.
        raise
    except Exception as exc:
        # Mirror the chat route: a raised exception (provider failure,
        # callback error) buffers a synthesized error event and closes the
        # buffer as STATE_ERROR so attached watchers see the failure.
        if tee is not None:
            tee.finish_error_with_event(str(exc))
        if not hasattr(exc, "fanout_observed"):
            try:
                exc.fanout_observed = collection.fanout_observed  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - slotted exception; best-effort
                pass
        raise
    finally:
        # Cancel/GeneratorExit must not leave a live buffer behind;
        # finish() no-ops after a terminal state.
        if tee is not None:
            tee.finish_aborted_if_live()

    return collection


def _iter_in_bridge_loop(agent: Any, **kwargs: Any) -> Iterator[Dict[str, Any]]:
    bridge = _get_bridge_loop()
    agen: Optional[Any] = None
    chunk_count = 0
    started_at = time.monotonic()
    thread_id = kwargs.get("thread_id", "")
    user_id = kwargs.get("user_id", "")
    is_autonomous = bool(kwargs.get("_is_self_invoke"))
    if is_autonomous:
        logger.info(
            "[STREAM_BRIDGE] start thread=%s user=%s autonomous=True",
            thread_id,
            user_id,
        )
    try:
        agen = bridge.result(_create_async_iterator(agent, dict(kwargs)))
        while True:
            try:
                chunk = bridge.result(_next_async_iterator(agen))
                chunk_count += 1
                if is_autonomous and chunk_count == 1:
                    logger.info(
                        "[STREAM_BRIDGE] first_chunk thread=%s type=%s after_ms=%d",
                        thread_id,
                        chunk.get("type"),
                        int((time.monotonic() - started_at) * 1000),
                    )
                yield chunk
            except StopAsyncIteration:
                break
    finally:
        if agen is not None:
            try:
                bridge.result(_close_async_iterator(agen))
            except RuntimeError:
                pass  # event loop may already be closed
        if is_autonomous:
            logger.info(
                "[STREAM_BRIDGE] end thread=%s chunks=%d elapsed_ms=%d",
                thread_id,
                chunk_count,
                int((time.monotonic() - started_at) * 1000),
            )


async def _create_async_iterator(agent: Any, kwargs: Mapping[str, Any]) -> Any:
    stream = agent.astream(**dict(kwargs))
    try:
        return stream.__aiter__()
    except AttributeError as exc:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
        raise TypeError("agent.astream() must return an async iterator") from exc


async def _next_async_iterator(agen: Any) -> Dict[str, Any]:
    return await agen.__anext__()


async def _close_async_iterator(agen: Any) -> None:
    await agen.aclose()


class _StreamBridgeLoop:
    """Dedicated asyncio loop for sync callers that consume async streams."""

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread = threading.Thread(
            target=self._run,
            name="NymeriaStreamBridgeLoop",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait(timeout=5)
        if self._loop is None:
            raise RuntimeError("Stream bridge loop failed to start")
        atexit.register(self.stop)

    @property
    def closed(self) -> bool:
        return self._loop is None or self._loop.is_closed()

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            asyncio.set_event_loop(None)

    def submit(self, coro: Any) -> concurrent.futures.Future:
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError("Stream bridge loop is closed")
        return asyncio.run_coroutine_threadsafe(coro, loop)

    def result(self, coro: Any) -> Any:
        try:
            future = self.submit(coro)
        except Exception:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            raise
        return future.result()

    def stop(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            from ..vendor.react_agent.providers import (
                close_provider_async_http_pools_for_loop,
            )

            future = asyncio.run_coroutine_threadsafe(
                close_provider_async_http_pools_for_loop(loop),
                loop,
            )
            future.result(timeout=2)
        except Exception:
            logger.debug(
                "[STREAM_BRIDGE] Failed to close loop-local provider HTTP pools",
                exc_info=True,
            )
        loop.call_soon_threadsafe(loop.stop)
        self._thread.join(timeout=2)


_bridge_loop: Optional[_StreamBridgeLoop] = None
_bridge_loop_lock = threading.Lock()


def _get_bridge_loop() -> _StreamBridgeLoop:
    global _bridge_loop
    with _bridge_loop_lock:
        if _bridge_loop is None or _bridge_loop.closed:
            _bridge_loop = _StreamBridgeLoop()
        return _bridge_loop
