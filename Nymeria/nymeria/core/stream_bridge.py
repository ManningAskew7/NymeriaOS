"""Sync bridges for consuming NymeriaAgent async streams from worker code."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import logging
import threading
import time
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
    """
    collection = StreamCollection()

    for chunk in iter_agent_astream(agent, **dict(astream_kwargs)):
        chunk_type = chunk.get("type")
        collection.chunk_count += 1

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

        if on_chunk is not None:
            on_chunk(chunk, collection)

        if chunk_type == "error":
            if error_message_factory is not None:
                message = error_message_factory(chunk)
            else:
                error_content = chunk.get("content", "")
                error_code = chunk.get("code", "unknown")
                message = error_content or f"Agent stream error (code={error_code})"
            raise RuntimeError(message)

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
