"""Sync bridges for consuming NymeriaAgent async streams from worker code."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import contextvars
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generator, Iterator, Mapping, Optional

from .turn_runner import begin_holder_turn_tee as begin_holder_turn_tee

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


def iter_agent_astream(agent: Any, **kwargs: Any) -> Generator[Dict[str, Any], None, None]:
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

    This is also the seam that isolates a NESTED turn from the turn that
    started it (``_detached_langchain_run_context``): every bridge step copies
    this worker thread's context onto the loop, so the detach must wrap the
    whole generator here, in the worker, not inside the coroutine.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        with _detached_langchain_run_context():
            yield from _iter_in_bridge_loop(agent, **kwargs)
        return

    raise RuntimeError("iter_agent_astream() is only supported from synchronous code")


@contextlib.contextmanager
def _detached_langchain_run_context() -> Iterator[None]:
    """Sever LangChain's callback and tracing inheritance for a nested turn.

    A worker spawned from inside a tool call (a callable reply's wake-up, a
    spawn, a hook-fired workflow) copies the tool call's context, and with it
    ``var_child_runnable_config``: LangChain's "you are inside this run"
    pointer, set by ``BaseTool.invoke`` for the call's duration. Its callbacks
    are the parent turn's child callback manager, whose inheritable handlers
    include the parent's ``astream_events`` streamer. A graph run started under
    it (``ensure_config`` merges the var into any config that names no
    callbacks) registers as a CHILD of the parent's tool call, and every one of
    its token events surfaces in the parent's stream as the parent's own: the
    2026-09-09 Telegram garble, where a caller's wake-up turn was token-spliced
    into the callee's final message (shipped/02). The tracer and run-collector
    vars are cleared alongside, as the per-site resets this seam replaced did
    (they only matter with LangSmith tracing on, which Nymeria does not run).
    The caller's values are restored afterwards so a tool body that streams
    inline keeps its own context once the turn is over.

    Belt and braces: ``NymeriaAgent.astream`` also passes ``callbacks=[]`` in
    its main graph config (as ``chat()`` does). No same-loop nested
    ``astream`` caller exists today; if one appears, that line isolates its
    main graph run only, and this seam is what isolates the whole turn.
    """
    from langchain_core.runnables.config import var_child_runnable_config
    from langchain_core.tracers.context import run_collector_var, tracing_v2_callback_var

    run_vars: tuple[contextvars.ContextVar[Any], ...] = (
        var_child_runnable_config,
        tracing_v2_callback_var,
        run_collector_var,
    )
    tokens = [(var, var.set(None)) for var in run_vars]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            try:
                var.reset(token)
            except ValueError:
                # Closed from a different Context (a generator finalized off
                # its thread): that context never held the caller's values.
                pass


def stream_and_collect(
    agent: Any,
    *,
    astream_kwargs: Mapping[str, Any],
    on_chunk: Optional[StreamChunkCallback] = None,
    error_message_factory: Optional[StreamErrorMessageFactory] = None,
    should_mark_iteration_limit: Optional[StreamIterationLimitPredicate] = None,
) -> StreamCollection:
    """Consume a turn on the caller's worker thread through the shared runner.

    Local execution and buffer production belong to the runner on the bridge
    loop. Callbacks, collection and caller-specific publishing remain synchronous.
    Remote executors retain their relay path; the API owns their runner.
    """
    from .turn_executor import wrap_for_stream

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Expected: a synchronous worker has no running event loop.
        pass
    else:
        raise RuntimeError("stream_and_collect() is only supported from synchronous code")

    collection = StreamCollection()
    executor = wrap_for_stream(agent)

    def consume(chunk: Dict[str, Any]) -> RuntimeError | None:
        chunk_type = chunk.get("type")
        collection.chunk_count += 1
        if chunk_type == "prompt_queued":
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
            if should_mark_iteration_limit is None or should_mark_iteration_limit(chunk):
                collection.iteration_limit_hit = True
                collection.iteration_limit_event = dict(chunk)

        if on_chunk is not None:
            # Fanout copies describe the holder; never stamp the original chunk
            # or the receipt that belongs to this queuing initiator.
            on_chunk(
                {**chunk, "fanout": True}
                if collection.fanout_observed and chunk_type != "prompt_queued" else chunk,
                collection,
            )
        if chunk_type == "error":
            message = (
                error_message_factory(chunk) if error_message_factory is not None
                else chunk.get("content") or f"Agent stream error (code={chunk.get('code', 'unknown')})"
            )
            error = RuntimeError(message)
            error.fanout_observed = collection.fanout_observed  # type: ignore[attr-defined]
            return error
        return None

    try:
        # submit copies this worker's context. Keep callbacks in the detached
        # context too, and restore the parent only after the whole turn settles.
        with _detached_langchain_run_context():
            if executor.is_remote:
                with contextlib.closing(iter_agent_astream(agent, **dict(astream_kwargs))) as stream:
                    for chunk in stream:
                        error = consume(chunk)
                        if error is not None:
                            raise error
            else:
                _collect_local_turn(executor, dict(astream_kwargs), consume)
    except Exception as exc:
        if not hasattr(exc, "fanout_observed"):
            try:
                exc.fanout_observed = collection.fanout_observed  # type: ignore[attr-defined]
            except Exception:  # slotted exceptions cannot carry the optional latch
                pass
        raise
    return collection


def _collect_local_turn(
    executor: Any, kwargs: Dict[str, Any],
    consume: Callable[[Dict[str, Any]], RuntimeError | None],
) -> None:
    from .turn_runner import ThreadTurnSink, TurnSpec, start_turn

    sink = ThreadTurnSink()
    spec = TurnSpec.from_astream_kwargs(kwargs)
    started_at = time.monotonic()
    chunk_count = 0

    runner_task: asyncio.Task | None = None
    future: concurrent.futures.Future | None = None
    bridge: _StreamBridgeLoop | None = None

    async def execute():
        nonlocal runner_task
        runner_task = start_turn(executor, spec, sink)
        return await runner_task

    async def cancel_and_drain() -> None:
        # Submitted after execute on the same loop, so its task is registered
        # before this runs. Cancel the actual task and await its cleanup, rather
        # than cancelling the cross-thread Future (which settles immediately).
        if runner_task is not None:
            runner_task.cancel()
            await asyncio.gather(runner_task, return_exceptions=True)

    coro = execute()
    try:
        bridge = _get_bridge_loop()
        future = bridge.submit(coro)
        # Also wakes the worker if execute failed before run_turn's first step.
        future.add_done_callback(lambda _: sink.close())
        if spec.is_self_invoke:
            logger.info("[STREAM_BRIDGE] start thread=%s user=%s autonomous=True", spec.thread_id, spec.user_id)
        while (delivery := sink.get()) is not None:
            chunk_count += 1
            if spec.is_self_invoke and chunk_count == 1:
                logger.info("[STREAM_BRIDGE] first_chunk thread=%s type=%s after_ms=%d",
                            spec.thread_id, delivery.event.get("type"),
                            int((time.monotonic() - started_at) * 1000))
            try:
                error = consume(delivery.event)
            except BaseException as exc:
                delivery.acknowledge(exc)
                # Closing the source can await lock/tool cleanup. Return the
                # caller's original failure only once that cleanup has settled.
                with contextlib.suppress(BaseException):
                    future.result()
                raise
            delivery.acknowledge()
            if error is not None:
                future.result()
                raise error
        result = future.result()
        if result.error is not None:
            raise result.error
    except BaseException:
        if future is None:
            coro.close()
            sink.close()
        elif bridge is not None and not future.done():
            # Includes an interruption in get(), after receipt, or while waiting
            # for final settlement. No departed worker may strand an ack wait.
            with contextlib.suppress(BaseException):
                bridge.result(cancel_and_drain())
                future.result()
        raise
    finally:
        if spec.is_self_invoke:
            logger.info("[STREAM_BRIDGE] end thread=%s chunks=%d elapsed_ms=%d",
                        spec.thread_id, chunk_count, int((time.monotonic() - started_at) * 1000))


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

            from .turn_runner import shutdown_turns

            async def drain_and_close() -> None:
                await shutdown_turns()
                await close_provider_async_http_pools_for_loop(loop)

            future = asyncio.run_coroutine_threadsafe(drain_and_close(), loop)
            future.result()
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
