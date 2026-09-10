"""Own a local turn independently of its initiating transport.

HTTP initiators observe request-scoped queue events, then read the holder's
buffer. Worker initiators can receive raw chunks through a thread-safe sink.
Only stop and process shutdown govern holder execution after admission.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import queue
import threading
import uuid
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any, Protocol

from .interactive_admission import TurnSlot
from .pending_prompt_queue import FANOUT_MAILBOX_MAXSIZE, PENDING_QUEUE_META_EVENT_TYPES
from .thread_lock_manager import get_thread_epoch, thread_admission_guard
from .turn_stream_buffer import (
    STATE_ABORTED, STATE_DONE, STATE_ERROR, STATE_LIVE,
    TurnStreamBuffer, get_turn_stream_registry,
)

logger = logging.getLogger(__name__)
HOLDER_STARTED = "holder_started"


class TurnSink(Protocol):
    wants_holder_chunks: bool

    def put(self, event: dict[str, Any]) -> None: ...
    def close(self) -> None: ...


class AsyncTurnSink:
    """Same-loop initiator mailbox; holder output lives only in its buffer."""

    wants_holder_chunks = False

    def __init__(self) -> None:
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._closed = False
        self.detached = False
        self.on_detach: Callable[[], None] | None = None
        self._dropped = 0

    def put(self, event: dict[str, Any]) -> None:
        if self._closed or self.detached:
            return
        if self._queue.qsize() >= FANOUT_MAILBOX_MAXSIZE:
            # Preserve the admission receipt and terminal/control messages.
            # Only pre-holder fanout can fill this queue: holder chunks bypass it.
            retained = []
            dropped = False
            while not self._queue.empty():
                previous = self._queue.get_nowait()
                if not dropped and previous is not None and previous.get("type") not in {
                    "queued", "prompt_queued", "prompt_absorbed", "error", HOLDER_STARTED,
                }:
                    dropped = True
                    self._dropped += 1
                else:
                    retained.append(previous)
            for previous in retained:
                self._queue.put_nowait(previous)
            if not dropped:
                # The queue cannot accumulate unlimited receipt-like fanout.
                self._dropped += 1
                if event.get("type") not in {"error", "prompt_absorbed", HOLDER_STARTED}:
                    return
        self._queue.put_nowait(event)

    async def get(self) -> dict[str, Any] | None:
        if self._dropped:
            dropped, self._dropped = self._dropped, 0
            return {"type": "fanout_dropped", "dropped_count": dropped}
        return await self._queue.get()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put_nowait(None)

    def detach(self) -> None:
        self.detached = True
        while not self._queue.empty():
            self._queue.get_nowait()
        if self.on_detach is not None:
            self.on_detach()


@dataclass
class ThreadTurnDelivery:
    """One raw chunk; the worker settles delivery after its callback returns."""

    event: dict[str, Any]
    delivered: concurrent.futures.Future[BaseException | None] = field(
        default_factory=concurrent.futures.Future,
    )

    def acknowledge(self, error: BaseException | None = None) -> None:
        try:
            self.delivered.set_result(error)
        except concurrent.futures.InvalidStateError:
            # Shutdown can cancel the producer while its worker is in a callback.
            pass


class ThreadTurnSink:
    """One-at-a-time raw delivery without blocking the producer's event loop."""

    wants_holder_chunks = True

    def __init__(self) -> None:
        self._queue: queue.Queue[ThreadTurnDelivery | None] = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._pending: ThreadTurnDelivery | None = None

    def put(self, event: dict[str, Any]) -> None:
        with self._lock:
            if not self._closed:
                self._pending = ThreadTurnDelivery(event)
                self._queue.put_nowait(self._pending)

    async def wait_for_delivery(self) -> None:
        pending = self._pending
        if pending is None:
            return
        error = await asyncio.wrap_future(pending.delivered)
        self._pending = None
        if error is not None:
            if isinstance(error, Exception) and not isinstance(error, concurrent.futures.CancelledError):
                raise error
            # Do not inject a worker's KeyboardInterrupt/SystemExit into the
            # bridge event loop. Its caller re-raises the original after drain.
            raise asyncio.CancelledError() from None

    def get(self) -> ThreadTurnDelivery | None:
        return self._queue.get()

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.put_nowait(None)


@dataclass
class TurnSpec:
    message: str
    thread_id: str
    user_id: str
    attachments: list[dict[str, Any]] | None = None
    images: list[dict[str, Any]] | None = None
    force_unsupported_attachments: bool = False
    is_self_invoke: bool = False
    trigger_override: str | None = None
    source: str | None = None
    source_id: str | None = None
    source_label: str | None = None
    resume_halted_turn: bool = False
    turn_user_message_id: str | None = None
    thread_epoch: int | None = None
    stream_thread_id: str | None = None
    dispatch_fields: dict[str, Any] = field(default_factory=dict)
    quick_footer: str | None = None
    client_id: str = ""
    holder_kind: str | None = None
    holder_label: str | None = None
    platform_origin: dict[str, Any] | None = None
    auto_title: bool = False
    refresh_activity: bool = False
    autonomous_task_id: str | None = None
    trigger_fields: dict[str, Any] = field(default_factory=dict)
    publish_sync_event: Callable[..., Any] | None = None
    publish_autonomous_event: Callable[..., Any] | None = None
    publish_agent_stream_chunk: Callable[..., Any] | None = None
    create_autonomous_notification: Callable[..., Any] | None = None
    should_notify_autonomous: Callable[..., bool] | None = None
    settings: Any = None
    on_turn_started: Callable[[], None] | None = None
    astream_overrides: dict[str, Any] = field(default_factory=dict)
    stop_on_error: bool = False

    def __post_init__(self) -> None:
        if self.resume_halted_turn:
            self.turn_user_message_id = None
        elif not self.turn_user_message_id:
            self.turn_user_message_id = str(uuid.uuid4())
        if self.thread_epoch is None:
            self.thread_epoch = get_thread_epoch(self.thread_id)

    @classmethod
    def from_astream_kwargs(cls, kwargs: dict[str, Any]) -> TurnSpec:
        """Preserve worker inputs while extracting shared holder metadata."""
        overrides = dict(kwargs)
        message = overrides.pop("message", "")
        on_started = overrides.pop("_on_turn_started", None)
        # An omitted autonomous label must retain the agent's own fallback,
        # rather than taking the HTTP adapter's default user label.
        overrides.setdefault("source_label", None)
        return cls(
            message=message, thread_id=str(kwargs.get("thread_id", "default") or ""),
            user_id=str(kwargs.get("user_id", "default") or ""),
            is_self_invoke=bool(kwargs.get("_is_self_invoke")),
            trigger_override=kwargs.get("_trigger_override"),
            source=kwargs.get("source"), source_id=kwargs.get("source_id"),
            source_label=kwargs.get("source_label"),
            resume_halted_turn=bool(kwargs.get("_resume_halted_turn")),
            turn_user_message_id=kwargs.get("_turn_user_message_id"),
            thread_epoch=kwargs.get("_thread_epoch"),
            on_turn_started=on_started, astream_overrides=overrides, stop_on_error=True,
        )

    def wire_event(self, event: dict[str, Any]) -> dict[str, Any]:
        return {**event, "thread_id": self.stream_thread_id or self.thread_id, **self.dispatch_fields}


@dataclass
class TurnResult:
    buffer: TurnStreamBuffer | None = None
    response_parts: list[str] = field(default_factory=list)
    fanout_observed: bool = False
    error: Exception | None = None


def apply_platform_origin(agent: Any, spec: TurnSpec) -> str:
    """Apply trusted platform provenance and optional reaction guidance."""
    from .bot_reactions import clear_turn_origin, set_turn_origin

    origin = spec.platform_origin
    if origin is None:
        clear_turn_origin(spec.thread_id)
        return spec.message
    set_turn_origin(spec.thread_id, **origin)
    if origin.get("kind") == "reaction":
        try:
            from ..tools.react import reaction_guidance_block

            guidance = reaction_guidance_block(agent, spec.user_id, spec.thread_id)
            if guidance:
                return f"{spec.message}\n\n{guidance}"
        except Exception:
            logger.debug("Failed to build reaction guidance block", exc_info=True)
    return spec.message


class HolderTurn:
    """One buffer producer, also used by model-free completion delivery."""

    def __init__(self, agent: Any, spec: TurnSpec) -> None:
        self.agent = agent
        self.spec = spec
        self.buffer: TurnStreamBuffer | None = None

    def begin(self) -> TurnStreamBuffer:
        spec = self.spec
        label = spec.holder_label or spec.source_label or spec.trigger_override or spec.source
        self.buffer = get_turn_stream_registry().begin_turn(
            spec.thread_id, spec.user_id, user_message_id=spec.turn_user_message_id,
            holder_kind=spec.holder_kind or ("autonomous" if spec.is_self_invoke else "user"),
            source_label=str(label)[:80] if spec.is_self_invoke and label else None,
            user_message_internal=spec.is_self_invoke,
            managed=True,
        )
        self.record({"type": "turn_started", "turn_id": self.buffer.turn_id})
        return self.buffer

    def record(self, chunk: dict[str, Any]) -> None:
        if self.buffer is not None:
            self.buffer.append(self.spec.wire_event(chunk))

    def done_event(self) -> dict[str, Any]:
        try:
            stats = self.agent.get_context_stats(self.spec.thread_id)
        except Exception:
            logger.warning("Context stats unavailable for %s", self.spec.thread_id, exc_info=True)
            stats = None
        try:
            cfg = self.agent._get_llm_config_for_thread(self.spec.thread_id)
            model = cfg.model or self.agent.settings.llm_model
        except Exception:
            model = None
        return {"type": "done", "context_stats": stats, "model": model}

    def finish_done(self, event: dict[str, Any] | None = None) -> None:
        if self.buffer is not None:
            self.record(event if event is not None else self.done_event())
            self.buffer.finish(STATE_DONE)

    def finish_error(self) -> None:
        if self.buffer is not None:
            self.buffer.finish(STATE_ERROR)

    def finish_error_with_event(self, message: str) -> None:
        if self.buffer is not None and self.buffer.state == STATE_LIVE:
            self.record({"type": "error", "content": message})
            self.finish_error()

    def finish_aborted_if_live(self) -> None:
        if self.buffer is not None:
            self.buffer.finish(STATE_ABORTED)


def begin_holder_turn_tee(
    agent: Any, *, thread_id: str, user_id: str, source: str,
    source_label: str | None, user_message_id: str | None,
) -> HolderTurn:
    holder = HolderTurn(agent, TurnSpec(
        message="", thread_id=thread_id, user_id=user_id, is_self_invoke=True,
        source=source, source_label=source_label, turn_user_message_id=user_message_id,
    ))
    holder.begin()
    return holder


async def run_turn(agent: Any, spec: TurnSpec, sink: TurnSink | None = None,
                   slot: TurnSlot | None = None) -> TurnResult:
    """Run to completion; consumers must never cancel a holder task."""
    stream_executor = agent
    agent = getattr(stream_executor, "agent", None) or stream_executor
    holder = HolderTurn(agent, spec)
    result = TurnResult()
    autonomous_started = False
    saw_error = False
    task = asyncio.current_task()

    def detach_queuer() -> None:
        if result.fanout_observed and holder.buffer is None and task is not None:
            task.cancel()

    if isinstance(sink, AsyncTurnSink):
        sink.on_detach = detach_queuer

    def started() -> None:
        result.buffer = holder.begin()
        if sink is not None and not sink.wants_holder_chunks:
            sink.put({"type": HOLDER_STARTED, "turn_id": result.buffer.turn_id, "buffer": result.buffer})
        if spec.on_turn_started is not None:
            spec.on_turn_started()

    def emit(event: dict[str, Any], *, raw: bool = False) -> None:
        holder.record(event)
        if sink is not None:
            if raw and sink.wants_holder_chunks:
                sink.put(event)
            elif holder.buffer is None and not sink.wants_holder_chunks:
                sink.put(spec.wire_event(event))

    def publish(event_type: str, data: dict[str, Any]) -> None:
        if spec.autonomous_task_id and spec.publish_autonomous_event:
            spec.publish_autonomous_event(
                event_type=event_type, thread_id=spec.thread_id, user_id=spec.user_id,
                task_id=spec.autonomous_task_id, data={
                    **data, "source": spec.trigger_override or "autonomous", **spec.trigger_fields,
                    **({"fanout": True} if result.fanout_observed else {}),
                },
            )

    try:
        message = apply_platform_origin(agent, spec)
        if spec.refresh_activity and not spec.is_self_invoke and spec.thread_id.startswith("spawned-"):
            from ..tools.spawn_thread import refresh_thread_activity

            refresh_thread_activity(agent, spec.user_id, spec.thread_id)
        kwargs = {
            "thread_id": spec.thread_id, "user_id": spec.user_id,
            "attachments": spec.attachments, "images": spec.images,
            "force_unsupported_attachments": spec.force_unsupported_attachments,
            "_is_self_invoke": spec.is_self_invoke, "_trigger_override": spec.trigger_override,
            "source": spec.source, "source_id": spec.source_id,
            "source_label": spec.source_label or spec.user_id,
            "_resume_halted_turn": spec.resume_halted_turn,
            "_thread_epoch": spec.thread_epoch,
            **spec.astream_overrides,
            "_on_turn_started": started, "_turn_user_message_id": spec.turn_user_message_id,
        }
        async with aclosing(stream_executor.astream(message=message, **kwargs)) as stream:
            async for chunk in stream:
                kind = chunk.get("type")
                if kind == "prompt_queued":
                    result.fanout_observed = True
                    if slot is not None:
                        slot.release()
                emit(chunk, raw=True)
                if isinstance(sink, ThreadTurnSink):
                    await sink.wait_for_delivery()
                if kind == "error":
                    saw_error = True
                if kind == "response":
                    result.response_parts.append(chunk.get("content", ""))
                if spec.autonomous_task_id:
                    if not autonomous_started and kind not in PENDING_QUEUE_META_EVENT_TYPES:
                        publish("task_started", {"prompt": message})
                        autonomous_started = True
                    if spec.publish_agent_stream_chunk:
                        spec.publish_agent_stream_chunk(
                            {**chunk, "fanout": True} if result.fanout_observed and kind != "prompt_queued" else chunk,
                            thread_id=spec.thread_id, user_id=spec.user_id, task_id=spec.autonomous_task_id,
                        )
                if result.fanout_observed and isinstance(sink, AsyncTurnSink) and sink.detached:
                    # Only prompt_queued proves this request can never promote.
                    return result
                if kind == "error" and spec.stop_on_error:
                    holder.finish_error()
                    return result
        if spec.quick_footer:
            emit({"type": "response", "content": spec.quick_footer})
        done = holder.done_event()
        if spec.platform_origin:
            from .bot_reactions import reply_suppressed

            if reply_suppressed(spec.thread_id):
                done["suppress_reply"] = True
        if spec.auto_title and not spec.resume_halted_turn and not result.fanout_observed and not saw_error:
            try:
                with thread_admission_guard(spec.thread_id) as epoch:
                    if epoch >= 0 and epoch == spec.thread_epoch:
                        title = agent.thread_metadata_manager.auto_title(spec.user_id, spec.thread_id, message)
                        if title:
                            done.update(title=title, title_source="auto")
                            if spec.publish_sync_event:
                                spec.publish_sync_event(
                                    event_type="thread_updated", thread_id=spec.thread_id, user_id=spec.user_id,
                                    data={"title": title, "title_source": "auto"}, origin_client_id=spec.client_id,
                                )
            except Exception:
                logger.warning("Failed to auto-title thread %s", spec.thread_id, exc_info=True)
        if holder.buffer is not None:
            holder.finish_done(done)
        else:
            emit(done)
    except concurrent.futures.CancelledError:
        raise asyncio.CancelledError() from None
    except Exception as exc:
        result.error = exc
        logger.error("Turn failed for thread %s", spec.thread_id, exc_info=True)
        emit({"type": "error", "content": str(exc)})
        holder.finish_error()
    finally:
        holder.finish_aborted_if_live()
        try:
            if spec.autonomous_task_id:
                content = str(result.error) if result.error else "".join(result.response_parts)
                if not result.fanout_observed and spec.create_autonomous_notification:
                    spec.create_autonomous_notification(
                        user_id=spec.user_id, thread_id=spec.thread_id, task_id=spec.autonomous_task_id,
                        summary=(content or "Autonomous task completed")[:200], settings=spec.settings,
                        thread_config_manager=agent.thread_config_manager,
                    )
                publish("task_completed", {
                    "content": content,
                    "notify": spec.should_notify_autonomous(spec.thread_id, agent.thread_config_manager)
                    if spec.should_notify_autonomous else False,
                    **({"error": True} if result.error else {}),
                })
        finally:
            try:
                if slot is not None:
                    slot.release()
            finally:
                try:
                    if sink is not None:
                        sink.close()
                finally:
                    _discard_turn(spec.thread_id, task)
    return result


_LIVE: dict[str, set[asyncio.Task[TurnResult]]] = {}
_LIVE_LOCK = threading.Lock()
_ACCEPTING = True


def _discard_turn(thread_id: str, task: asyncio.Task[Any] | None) -> None:
    with _LIVE_LOCK:
        tasks = _LIVE.get(thread_id)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                _LIVE.pop(thread_id, None)


def start_turn(agent: Any, spec: TurnSpec, sink: TurnSink | None = None,
               slot: TurnSlot | None = None) -> asyncio.Task[TurnResult]:
    """Transfer ownership before returning an HTTP response or worker handle."""
    with _LIVE_LOCK:
        if not _ACCEPTING:
            raise RuntimeError("The server is shutting down. Retry in a moment.")
        task = asyncio.create_task(run_turn(agent, spec, sink, slot), name=f"turn:{spec.thread_id}")
        _LIVE.setdefault(spec.thread_id, set()).add(task)

    def finished(done: asyncio.Task[TurnResult]) -> None:
        # Also runs if cancelled before run_turn's first step, when no finally ran.
        try:
            if slot is not None:
                slot.release()
            if sink is not None:
                sink.close()
            if not done.cancelled() and (error := done.exception()) is not None:
                logger.error("Turn finalization failed for %s", spec.thread_id,
                             exc_info=(type(error), error, error.__traceback__))
        finally:
            _discard_turn(spec.thread_id, done)

    task.add_done_callback(finished)
    return task


def active_turn_task_count() -> int:
    with _LIVE_LOCK:
        return sum(len(tasks) for tasks in _LIVE.values())


def open_turn_runner() -> None:
    global _ACCEPTING
    with _LIVE_LOCK:
        _ACCEPTING = True


async def shutdown_turns() -> None:
    """Cancel and await all owned turns on their respective event loops."""
    global _ACCEPTING
    current = asyncio.get_running_loop()
    with _LIVE_LOCK:
        _ACCEPTING = False
        tasks = [task for group in _LIVE.values() for task in group]
    by_loop: dict[asyncio.AbstractEventLoop, list[asyncio.Task[TurnResult]]] = {}
    for task in tasks:
        by_loop.setdefault(task.get_loop(), []).append(task)

    async def cancel_and_wait(owned: list[asyncio.Task[TurnResult]]) -> None:
        for task in owned:
            task.cancel()
        await asyncio.gather(*owned, return_exceptions=True)

    waits = []
    for loop, owned in by_loop.items():
        if loop is current:
            waits.append(cancel_and_wait(owned))
        elif loop.is_running():
            future = asyncio.run_coroutine_threadsafe(cancel_and_wait(owned), loop)
            waits.append(asyncio.wrap_future(future))
    if waits:
        await asyncio.gather(*waits)
