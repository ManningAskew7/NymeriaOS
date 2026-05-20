"""Per-thread queue for prompts that arrive while a turn is mid-flight.

When a Nymeria thread is busy (its ``threading.Lock`` is held by another
turn), incoming prompts are enqueued here instead of blocking. The
running turn cooperates: ``route_after_tools`` peeks the queue at every
sub-turn boundary; a non-empty queue causes the graph to end early
("halt"). ``astream()`` then drains the queue, builds one
``HumanMessage`` per pending prompt with correct visibility flags, and
re-drives the graph -- so the model absorbs the new context without
re-entering its entrypoint or losing tool state.

The module also defines a tiny cross-loop-safe mailbox primitive
(``FanoutMailbox``) so that the queuer's SSE consumer (running on a
different event loop in the callable-thread / ticker case) can observe
the live events of the holder's turn after its prompt is injected.

v1 ships only the in-memory backend, and that is now sufficient for
both supported shapes: in slim everything runs in one process, and in
Docker the worker no longer instantiates ``NymeriaAgent`` -- it relays
TODO and trigger turns to the API container via HTTP, so the queue is
always process-local to the one process that actually runs the agent.
The ``PendingPromptQueueBackend`` protocol stays in place so a Redis
backend could still slot in later (e.g. for multi-replica API), but
that is no longer required to close the original cross-process gap.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Optional, Protocol

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)


# Bound the per-mailbox buffer so a stalled queuer (slow SSE consumer)
# cannot pin the holder's events in memory indefinitely. On overflow we
# drop the oldest event and inject a ``fanout_dropped`` marker so the
# eventual consumer can detect the gap.
FANOUT_MAILBOX_MAXSIZE = 512
PENDING_PROMPT_QUEUE_MAXSIZE = 256


class PendingPromptQueueClosingError(RuntimeError):
    """Raised when a holder is releasing and new prompts should take the next turn."""


class FanoutMailbox:
    """Cross-loop-safe single-producer / single-consumer mailbox.

    The producer (the holder's coroutine, running on its own event
    loop) calls ``put()`` from inside ``_drive_with_fanout``. The
    consumer (the queuer's coroutine, running on its own event loop)
    awaits ``get()``.

    Internally a ``collections.deque`` + ``threading.Lock`` make
    ``put()`` safe to call from any thread/loop; an ``asyncio.Event``
    bound to the consumer's loop wakes ``get()`` and is poked via
    ``loop.call_soon_threadsafe`` so we never touch an asyncio
    primitive on the wrong loop.
    """

    def __init__(self, consumer_loop: asyncio.AbstractEventLoop) -> None:
        self._consumer_loop = consumer_loop
        # Buffer is bounded; deque(maxlen=...) silently evicts oldest --
        # we want to know about the drop, so we manage eviction ourselves.
        self._buf: Deque[Dict[str, Any]] = collections.deque()
        self._lock = threading.Lock()
        self._event = asyncio.Event()
        self._closed = False
        self._dropped_since_signal = 0

    @property
    def consumer_loop(self) -> asyncio.AbstractEventLoop:
        return self._consumer_loop

    def put(self, event: Dict[str, Any]) -> None:
        """Thread/loop-safe append of an event for the consumer."""
        with self._lock:
            if self._closed:
                return
            if len(self._buf) >= FANOUT_MAILBOX_MAXSIZE:
                # Drop oldest and remember the loss; we surface it on
                # the next get() so the consumer sees a marker.
                try:
                    self._buf.popleft()
                except IndexError:  # buffer raced empty between len() and popleft()
                    pass
                self._dropped_since_signal += 1
            self._buf.append(event)
        # Schedule the event.set() on the consumer's loop. This is the
        # only safe way to poke an asyncio.Event whose loop is not the
        # current one.
        try:
            self._consumer_loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:
            # Consumer loop is already closed (queuer died/disconnected).
            # Mark closed; any subsequent put() is a no-op.
            with self._lock:
                self._closed = True

    async def get(self) -> Dict[str, Any]:
        """Await and pop the next event. Must be called from consumer_loop."""
        while True:
            with self._lock:
                dropped = self._dropped_since_signal
                if dropped:
                    self._dropped_since_signal = 0
                    return {"type": "fanout_dropped", "dropped_count": dropped}
                if self._buf:
                    event = self._buf.popleft()
                    if not self._buf:
                        # Buffer drained; rearm the event for the next put().
                        self._event.clear()
                    return event
                if self._closed:
                    # Closed and empty: signal end-of-stream via sentinel.
                    return {"type": "prompt_absorbed_sentinel"}
                self._event.clear()
            await self._event.wait()

    def close(self) -> None:
        """Mark the mailbox closed; consumer will receive the sentinel."""
        with self._lock:
            self._closed = True
        try:
            self._consumer_loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:  # consumer loop already closed; nothing to wake
            pass


@dataclass
class PendingPrompt:
    """A prompt waiting for a busy thread to reach a sub-turn boundary.

    ``fanout_mailbox`` is set only when the queuer wants to observe the
    holder's stream after injection (interactive sources: user chat,
    callable threads, MCP). Fire-and-forget queuers (triggers, ticker,
    watchdog) leave it None.
    """

    message: str
    source: str
    source_id: Optional[str]
    source_label: str
    user_id: str
    enqueued_at: float
    is_autonomous: bool
    fanout_mailbox: Optional[FanoutMailbox]
    notify_event: threading.Event
    consumer_loop: Optional[asyncio.AbstractEventLoop]
    # Set to True by ``clear(abandoned=True)`` so a blocked queuer can
    # tell its wait was cut short by an abort vs. a normal absorb.
    abandoned: bool = field(default=False)


class PendingPromptQueueBackend(Protocol):
    """Pluggable storage protocol for the pending-prompt queue.

    v1 has one implementation (``InMemoryPendingPromptQueue``); a Redis
    backend will implement the same shape so a process-crossing setup
    can use ``create_pending_queue`` to pick at runtime.
    """

    def enqueue(self, thread_id: str, prompt: PendingPrompt) -> int: ...
    def drain(self, thread_id: str) -> List[PendingPrompt]: ...
    def peek(self, thread_id: str) -> bool: ...
    def size(self, thread_id: str) -> int: ...
    def clear(self, thread_id: str, *, abandoned: bool = True) -> int: ...
    def mark_halt_observed(self, thread_id: str, count: int) -> None: ...
    def consume_halt_observation(self, thread_id: str) -> int: ...
    def begin_release(self, thread_id: str) -> None: ...
    def end_release(self, thread_id: str) -> None: ...
    def is_releasing(self, thread_id: str) -> bool: ...


class InMemoryPendingPromptQueue:
    """Process-local backend.

    A single ``threading.Lock`` guards two dicts: per-thread FIFO of
    ``PendingPrompt`` and per-thread halt-observation counters
    (populated by ``route_after_tools`` and consumed by ``astream``
    just before draining).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: Dict[str, Deque[PendingPrompt]] = {}
        self._halt_observed: Dict[str, int] = {}
        self._releasing: set[str] = set()

    def enqueue(self, thread_id: str, prompt: PendingPrompt) -> int:
        evicted: Optional[PendingPrompt] = None
        with self._lock:
            if thread_id in self._releasing:
                raise PendingPromptQueueClosingError(
                    f"Thread {thread_id} is releasing; retry as next turn"
                )
            queue = self._queues.setdefault(thread_id, collections.deque())
            if len(queue) >= PENDING_PROMPT_QUEUE_MAXSIZE:
                evicted = queue.popleft()
            queue.append(prompt)
            position = len(queue)
        if evicted is not None:
            self._wake_prompt(
                evicted,
                abandoned=True,
                error_code="queue_overflow",
                error_content="Queued prompt was dropped because the pending queue is full.",
            )
        return position

    def drain(self, thread_id: str) -> List[PendingPrompt]:
        with self._lock:
            queue = self._queues.pop(thread_id, None)
        if not queue:
            return []
        return list(queue)

    def peek(self, thread_id: str) -> bool:
        with self._lock:
            queue = self._queues.get(thread_id)
            return bool(queue)

    def size(self, thread_id: str) -> int:
        with self._lock:
            queue = self._queues.get(thread_id)
            return len(queue) if queue else 0

    def clear(self, thread_id: str, *, abandoned: bool = True) -> int:
        """Drop all pending prompts for ``thread_id``.

        When ``abandoned`` is True (the abort path) each pending entry
        gets ``abandoned=True``, an aborted error pushed into its
        mailbox plus the sentinel, and its ``notify_event`` set so
        blocked queuers wake up rather than hang on the lock_timeout.
        """
        with self._lock:
            queue = self._queues.pop(thread_id, None)
            self._halt_observed.pop(thread_id, None)
        if not queue:
            return 0
        count = 0
        for prompt in queue:
            count += 1
            self._wake_prompt(
                prompt,
                abandoned=abandoned,
                error_code="aborted" if abandoned else None,
                error_content="Turn aborted." if abandoned else None,
            )
        return count

    def mark_halt_observed(self, thread_id: str, count: int) -> None:
        """Record that the router observed ``count`` pending prompts.

        Accumulates across multiple observations within a single turn
        (rare but possible if a halt fires, prompts re-arrive, and a
        re-driven graph halts again before drain runs).
        """
        if count <= 0:
            return
        with self._lock:
            self._halt_observed[thread_id] = (
                self._halt_observed.get(thread_id, 0) + count
            )

    def consume_halt_observation(self, thread_id: str) -> int:
        """Atomically read-and-clear the halt counter for ``thread_id``."""
        with self._lock:
            return self._halt_observed.pop(thread_id, 0)

    def begin_release(self, thread_id: str) -> None:
        """Stop accepting new queued prompts while the holder releases the lock.

        A contender that races with this phase should wait for the lock and run as
        the next turn, not enqueue behind a holder that has no more sub-turn
        boundaries to observe.
        """
        with self._lock:
            self._releasing.add(thread_id)

    def end_release(self, thread_id: str) -> None:
        """Allow queueing for the next holder after the lock has been released."""
        with self._lock:
            self._releasing.discard(thread_id)

    def is_releasing(self, thread_id: str) -> bool:
        with self._lock:
            return thread_id in self._releasing

    @staticmethod
    def _wake_prompt(
        prompt: PendingPrompt,
        *,
        abandoned: bool,
        error_code: Optional[str] = None,
        error_content: Optional[str] = None,
    ) -> None:
        if abandoned:
            prompt.abandoned = True
        if prompt.fanout_mailbox is not None:
            if error_code:
                prompt.fanout_mailbox.put({
                    "type": "error",
                    "code": error_code,
                    "content": error_content or "Queued prompt was not processed.",
                })
            prompt.fanout_mailbox.close()
        prompt.notify_event.set()


def create_pending_queue(settings: Optional["Settings"] = None) -> PendingPromptQueueBackend:
    """Factory mirror of ``create_event_bus``.

    v1 only knows how to make ``InMemoryPendingPromptQueue``. v2 will
    inspect ``settings.redis_enabled`` (or a dedicated flag) and dispatch
    to a Redis-backed implementation when configured.
    """
    # settings is accepted for forward-compat; intentionally unused now.
    _ = settings
    return InMemoryPendingPromptQueue()


_singleton: Optional[PendingPromptQueueBackend] = None
_singleton_lock = threading.Lock()


def get_pending_queue() -> PendingPromptQueueBackend:
    """Return the process-wide singleton backend, building it on first use.

    The agent's __init__ also publishes a backend via ``set_pending_queue``;
    that explicit call wins if it's executed before the first ``get``.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = create_pending_queue(None)
        return _singleton


def set_pending_queue(backend: PendingPromptQueueBackend) -> None:
    """Publish a process-wide backend instance (called by NymeriaAgent.__init__)."""
    global _singleton
    with _singleton_lock:
        _singleton = backend


def reset_pending_queue_for_tests() -> None:
    """Test helper: drop the singleton so the next access rebuilds it."""
    global _singleton
    with _singleton_lock:
        _singleton = None


def make_pending_prompt(
    *,
    message: str,
    source: str,
    source_id: Optional[str],
    source_label: str,
    user_id: str,
    is_autonomous: bool,
    fanout_mailbox: Optional[FanoutMailbox] = None,
    consumer_loop: Optional[asyncio.AbstractEventLoop] = None,
) -> PendingPrompt:
    """Build a PendingPrompt with sensible defaults for enqueued_at and notify_event."""
    return PendingPrompt(
        message=message,
        source=source,
        source_id=source_id,
        source_label=source_label,
        user_id=user_id,
        enqueued_at=time.time(),
        is_autonomous=is_autonomous,
        fanout_mailbox=fanout_mailbox,
        notify_event=threading.Event(),
        consumer_loop=consumer_loop,
    )


# Maps ``PendingPrompt.source`` to the human-readable trigger label shown in
# the drained-prompt header. Mirrors ``get_time_context`` in ``prompts.py`` so
# a drained sub-turn reads consistently with a freshly-started turn.
_SOURCE_LABELS = {
    "ticker": "Scheduled TODO",
    "user": "User Message",
    "trigger": "Event Trigger",
    "callable": "Callable Thread",
    "mcp": "MCP Client",
    "watchdog": "Watchdog",
    "credential_resolution": "Credential Prompt",
}


def queued_prompt_header(prompt: PendingPrompt) -> str:
    """Build the metadata header prefixed to each drained prompt's HumanMessage."""
    from .time_utils import format_user_time

    label = _SOURCE_LABELS.get(prompt.source, prompt.source.capitalize())
    return (
        f"[Time: {format_user_time(prompt.enqueued_at)}]\n"
        f"[Trigger: {label}]"
    )


# ---------------------------------------------------------------------------
# SSE event constants (single source of truth so registries/handlers stay
# in sync). Each value matches the documented ``type`` field on the wire.
# ---------------------------------------------------------------------------

EVENT_PROMPT_QUEUED = "prompt_queued"
EVENT_PROMPT_INJECTED = "prompt_injected"
EVENT_PROMPT_ABSORBED = "prompt_absorbed"
EVENT_TURN_HALTED = "turn_halted"
EVENT_FANOUT_DROPPED = "fanout_dropped"

# Internal sentinel pushed into a mailbox by the holder when its drive
# for that prompt's batch completes. The consumer translates this into
# a public ``prompt_absorbed`` event and exits.
_SENTINEL_PROMPT_ABSORBED = "prompt_absorbed_sentinel"


PENDING_QUEUE_EVENT_TYPES = frozenset({
    EVENT_PROMPT_QUEUED,
    EVENT_PROMPT_INJECTED,
    EVENT_PROMPT_ABSORBED,
    EVENT_TURN_HALTED,
    EVENT_FANOUT_DROPPED,
})


# Queue-meta event types that autonomous publishers (ticker, trigger
# manager) must NOT use to fire ``task_started``. These signal queue
# state transitions, not the start of actual model work for the queued
# prompt. The legacy ``queued`` alias (emitted alongside ``prompt_queued``
# for back-compat in agent.py:_yield_queued_events) is included so it
# never accidentally triggers an early ``task_started``.
PENDING_QUEUE_META_EVENT_TYPES = frozenset({
    "queued",  # legacy alias for prompt_queued
    EVENT_PROMPT_QUEUED,
    EVENT_PROMPT_INJECTED,
    EVENT_PROMPT_ABSORBED,
    EVENT_TURN_HALTED,
    EVENT_FANOUT_DROPPED,
})
