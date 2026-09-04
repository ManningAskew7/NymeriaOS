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
    # Set to True by ``clear_with_restore`` (the user-initiated stop path)
    # so a blocked queuer can tell its prompt was handed back to the user
    # rather than dropped.
    restored: bool = field(default=False)
    # Set by ``_wake_prompt`` when the prompt was woken with an error (an
    # abort, a restore, an eviction, an inject failure) rather than absorbed,
    # so a producer without a mailbox can still read the outcome.
    error_code: Optional[str] = field(default=None)


def _wake_prompt(
    prompt: PendingPrompt,
    *,
    abandoned: bool,
    restored: bool = False,
    error_code: Optional[str] = None,
    error_content: Optional[str] = None,
) -> None:
    """Wake one prompt's waiters: optional error into the mailbox, close, set.

    ``abandoned``/``restored`` are stamped before ``notify_event`` fires so a
    woken waiter reads the final value. Shared by the backend's evict/clear/
    restore paths and the module-level batch helpers below.
    """
    if abandoned:
        prompt.abandoned = True
    if restored:
        prompt.restored = True
    if error_code:
        prompt.error_code = error_code
    if prompt.fanout_mailbox is not None:
        if error_code:
            prompt.fanout_mailbox.put({
                "type": "error",
                "code": error_code,
                "content": error_content or "Queued prompt was not processed.",
            })
        prompt.fanout_mailbox.close()
    prompt.notify_event.set()


# Wire code + content pushed to a queued waiter whose prompt was handed
# back to the user by a stop (``clear_with_restore``) instead of dropped.
# Rides the ``error`` envelope so the queued-prompt SSE generators (which
# exit on ``error``/``prompt_absorbed``) terminate cleanly on old clients.
RESTORED_ERROR_CODE = "restored"
RESTORED_ERROR_CONTENT = (
    "Turn stopped; this queued message was returned to you unprocessed."
)


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
    def clear_with_restore(
        self, thread_id: str
    ) -> tuple[List[PendingPrompt], int]: ...
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

    def clear_with_restore(
        self, thread_id: str
    ) -> tuple[List[PendingPrompt], int]:
        """Drop all pending prompts, handing user-source entries back.

        The user-initiated-stop sibling of ``clear``: pops the queue and
        the halt-observation counter atomically, then wakes each entry.
        User-source prompts (``source == "user"``, the only human source;
        callable/mcp waiters are interactive but programmatic) are stamped
        ``restored`` and woken with a ``restored`` error so their blocked
        waiters exit cleanly, and are returned in FIFO order so the stop
        caller can hand the raw texts back to the user. Everything else
        keeps ``clear``'s abandoned/aborted semantics so programmatic
        queuers (ticker, triggers, callable-ask waiters) never hang.

        Returns ``(restored_user_prompts, discarded_count)``.
        """
        with self._lock:
            queue = self._queues.pop(thread_id, None)
            self._halt_observed.pop(thread_id, None)
        if not queue:
            return [], 0
        restored: List[PendingPrompt] = []
        discarded = 0
        for prompt in queue:
            if prompt.source == "user":
                restored.append(prompt)
                self._wake_prompt(
                    prompt,
                    abandoned=False,
                    restored=True,
                    error_code=RESTORED_ERROR_CODE,
                    error_content=RESTORED_ERROR_CONTENT,
                )
            else:
                discarded += 1
                self._wake_prompt(
                    prompt,
                    abandoned=True,
                    error_code="aborted",
                    error_content="Turn aborted.",
                )
        return restored, discarded

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
        restored: bool = False,
        error_code: Optional[str] = None,
        error_content: Optional[str] = None,
    ) -> None:
        _wake_prompt(
            prompt,
            abandoned=abandoned,
            restored=restored,
            error_code=error_code,
            error_content=error_content,
        )


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


def queued_prompt_header(prompt: PendingPrompt) -> str:
    """Build the metadata header prefixed to each drained prompt's HumanMessage.

    The label comes from ``prompts.SOURCE_TRIGGER_LABELS``, the same table
    ``get_time_context`` resolves a fresh turn's header against, so a drained
    sub-turn reads exactly like a turn this prompt had started itself. An
    unlisted source falls back to ``capitalize()`` rather than raising: a
    novel source is still labelled honestly, just unpolished.
    """
    from .prompts import SOURCE_TRIGGER_LABELS
    from .time_utils import format_user_time

    label = SOURCE_TRIGGER_LABELS.get(prompt.source, prompt.source.capitalize())
    return (
        f"[Time: {format_user_time(prompt.enqueued_at)}]\n"
        f"[Trigger: {label}]"
    )


def restored_prompts_payload(prompts: List[PendingPrompt]) -> List[Dict[str, Any]]:
    """Wire payload for a stop response's ``restored_prompts`` field.

    Shared by every stop surface (REST route, command-service adapter,
    in-process bot adapter) so the field shape cannot drift. ``text`` is
    the RAW user text: the ``[Time:]/[Trigger:]`` header is applied only
    at message-build time, never stored on the entry.
    """
    return [
        {
            "text": p.message,
            "source_label": p.source_label,
            "user_id": p.user_id,
            "enqueued_at": p.enqueued_at,
        }
        for p in (prompts or [])
    ]


def restored_prompts_notice(prompts: List[Dict[str, Any]]) -> Optional[str]:
    """Human-readable echo block for stop responses: queued messages NOT sent.

    ``prompts`` is the ``restored_prompts`` payload from a stop response
    (dicts carrying at least ``text``). Returns None when there is nothing
    to echo. Shared by the ``/stop`` slash handler and the chat-bot stop
    confirmations so every text-only surface (no writable composer) renders
    the same quote-for-resend echo.
    """
    texts = [str(p.get("text") or "").strip() for p in (prompts or [])]
    texts = [t for t in texts if t]
    if not texts:
        return None
    if len(texts) == 1:
        header = "This queued message was NOT sent:"
    else:
        header = f"These {len(texts)} queued messages were NOT sent:"
    blocks = [header]
    for text in texts:
        blocks.append("\n".join(f"> {line}" for line in text.splitlines()))
    return "\n\n".join(blocks)


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


# ---------------------------------------------------------------------------
# Batch wake helpers, shared by the chat()/astream() drain call sites so the
# per-prompt mailbox choreography lives next to the protocol it implements.
# ---------------------------------------------------------------------------


def notify_batch_absorbed(prompts: List[PendingPrompt]) -> None:
    """Signal absorption for a drained batch.

    Pushes the absorbed sentinel into each prompt's fanout mailbox (so
    stream-observing queuers exit their fan-in loop and report
    ``prompt_absorbed``), closes the mailbox, then wakes any
    ``notify_event`` waiters. This is the success-path sibling of
    ``InMemoryPendingPromptQueue._wake_prompt`` (which handles the error
    shapes and has no sentinel).
    """
    for prompt in prompts:
        if prompt.fanout_mailbox is not None:
            prompt.fanout_mailbox.put({"type": _SENTINEL_PROMPT_ABSORBED})
            prompt.fanout_mailbox.close()
        prompt.notify_event.set()


def notify_batch_error(
    prompts: List[PendingPrompt],
    *,
    code: str,
    content: str,
    abandoned: bool = False,
) -> None:
    """Wake a drained batch with an error so blocked queuers do not hang.

    ``abandoned=True`` additionally marks each prompt abandoned (set before
    ``notify_event`` fires, so a woken waiter reads the final value); the
    cross-user rejection path uses it, the inject-failure paths do not.
    """
    for prompt in prompts:
        _wake_prompt(
            prompt,
            abandoned=abandoned,
            error_code=code,
            error_content=content,
        )
