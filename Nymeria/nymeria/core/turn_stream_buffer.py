"""In-process per-thread buffer of holder-turn stream events.

Backs turn re-attach and live watching: when a client's ``POST /chat`` SSE
connection drops mid-turn, the backend keeps executing the turn (the
deliberate disconnect-keeps-running posture in ``api/routers/chat.py``), and
this buffer retains the turn's wire events so the client can rejoin via
``GET /threads/{thread_id}/turn/stream`` and re-render the full turn,
including the terminal ``done``/``error`` event that the original response
suppresses after a disconnect. Autonomous turns (TODOs, triggers, dreams,
callables, spawns, watchdog) feed the same buffer via the
``stream_and_collect`` tee in ``core/stream_bridge.py``, so any client can
attach to any in-flight turn regardless of who started it (backlog #90).

Design notes:

- One buffer per thread, holding the CURRENT (or most recently finished)
  lock-holder turn only. A new holder turn replaces the previous buffer.
  Only the holder turn writes here (queued-prompt requests observe the
  holder's stream via fanout mailboxes and must not double-write), which the
  chat route guarantees by creating the buffer from ``NymeriaAgent.astream``'s
  ``_on_turn_started`` callback, fired exactly once per holder turn right
  after lock acquisition.
- Entries store the exact serialized SSE payload strings the original client
  would have received (post thread-id/dispatch merge), each stamped with a
  monotonically increasing ``seq``, so replay is byte-identical and needs no
  re-serialization.
- Bounded per turn by event count and payload bytes (mirroring the desktop
  autonomous store's replay-buffer bounds). Overflow evicts from the front
  and marks the buffer truncated; re-attach then reports a replay gap and
  clients fall back to history reconciliation.
- In-process only, safe because the API process is the sole agent runtime in
  both deployment shapes. Readers run on the API event loop; writers are
  either the chat route (same loop) or sync autonomous workers on plain
  threads. Off-loop writers marshal reader wakeups onto the loop registered
  via ``TurnStreamRegistry.set_reader_loop`` (``call_soon_threadsafe``); the
  capture-event-then-check wakeup pattern in ``stream_payloads`` needs no
  locks for the async side. Scalar state shared across threads is guarded by
  a plain mutex.
- An API restart loses the registry with the in-flight turn itself; clients
  detect this via ``GET /threads/{id}/status`` and reconcile from history.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
import time
import uuid
from collections import deque
from typing import Any, AsyncGenerator, Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Per-turn bounds. Sized to comfortably hold a long interactive turn while
# capping worst-case memory per thread; values mirror the desktop autonomous
# store's per-thread replay buffer (4000 events / 2MB).
MAX_EVENTS_PER_TURN = 4000
MAX_BYTES_PER_TURN = 2 * 1024 * 1024

# How long a finished turn's buffer stays attachable. Long enough for a
# client to come back from a routine network blip after the turn ended
# server-side; short enough that idle threads do not pin memory.
FINISHED_RETENTION_SECONDS = 300.0

# Buffer terminal states.
STATE_LIVE = "live"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_ABORTED = "aborted"

# Poll ceiling for live readers; real wakeups come from the pulse event.
_READER_WAIT_SECONDS = 15.0


class TurnReplayGapError(Exception):
    """Overflow evicted events the reader had not consumed yet.

    Raised by ``stream_payloads`` when a slow (backpressured) reader falls
    behind the writer far enough that the front of the buffer was evicted
    past its cursor. Yielding the remaining entries would silently skip the
    evicted span, so the reader is told to fall back to history
    reconciliation instead (the same posture as the attach-time 410).
    """


class TurnStreamBuffer:
    """Bounded, replayable event buffer for one holder turn."""

    def __init__(
        self,
        thread_id: str,
        user_id: str,
        user_message_id: Optional[str] = None,
        holder_kind: str = "user",
        source_label: Optional[str] = None,
        user_message_internal: bool = False,
    ) -> None:
        self.thread_id = thread_id
        self.user_id = user_id
        # Graph message id of the turn's initiating HumanMessage (None for
        # message-less turns, e.g. /resume). Lets a live-attach viewer anchor
        # its hydrated history to the turn start: history entries expose the
        # same id as ``message_id``, so the viewer trims everything after the
        # anchor and rebuilds the turn from the replay without duplication.
        self.user_message_id = user_message_id
        # Who is running the turn: "user" (interactive) or "autonomous"
        # (self-invoke: TODOs, triggers, dreams, callables, spawns, watchdog).
        self.holder_kind = holder_kind
        # Short human label for the initiator (trigger name, TODO task,
        # callable name, "watchdog"); best-effort, may be None.
        self.source_label = source_label
        # True when the initiating message is an internal autonomous wakeup,
        # i.e. subject to the per-thread show_autonomous_prompts history
        # filter. Viewers hydrate with include_hidden_anchors=true so the
        # anchor resolves either way; this flag tells them why it may render
        # as a hidden stub.
        self.user_message_internal = user_message_internal
        self.turn_id = uuid.uuid4().hex
        self.started_at = time.time()
        self.state = STATE_LIVE
        self.finished_at: Optional[float] = None
        self.truncated = False
        self._entries: Deque[Tuple[int, str]] = deque()  # (seq, payload_json)
        self._next_seq = 1
        self._bytes = 0
        # Pulsed (set + replaced) on every append/finish so any number of
        # concurrent readers wake without clear() races.
        self._pulse_event: asyncio.Event = asyncio.Event()
        # Guards the scalar snapshot for off-loop REST reads.
        self._meta_lock = threading.Lock()

    # -- writer side (chat route, API event loop) ---------------------------

    def append(self, event: Dict[str, Any]) -> Tuple[int, str]:
        """Stamp ``seq`` into ``event``, serialize, store, and wake readers.

        Returns ``(seq, payload_json)``; the caller yields the payload to its
        own SSE consumer so the live wire and the replay are byte-identical.
        """
        with self._meta_lock:
            seq = self._next_seq
            self._next_seq += 1
        event["seq"] = seq
        payload = json.dumps(event)
        with self._meta_lock:
            self._entries.append((seq, payload))
            self._bytes += len(payload)
            while self._entries and (
                len(self._entries) > MAX_EVENTS_PER_TURN
                or self._bytes > MAX_BYTES_PER_TURN
            ):
                _, dropped = self._entries.popleft()
                self._bytes -= len(dropped)
                self.truncated = True
        self._pulse()
        return seq, payload

    def finish(self, state: str) -> None:
        """Mark the turn ended (``done``/``error``/``aborted``) and wake readers."""
        with self._meta_lock:
            if self.state != STATE_LIVE:
                return
            self.state = state
            self.finished_at = time.time()
        self._pulse()

    def _pulse(self) -> None:
        """Wake readers, marshaling onto the reader loop when off-loop.

        The chat route writes from the API event loop, where a plain
        ``Event.set()`` is correct. Autonomous turns write from sync worker
        threads (the ``stream_and_collect`` tee); ``asyncio.Event.set`` is
        not thread-safe, so those writers hand the set to the registered
        reader loop via ``call_soon_threadsafe``. With no registered loop
        (unit tests, single-loop harnesses) fall back to a direct set.
        """
        with self._meta_lock:
            event = self._pulse_event
            self._pulse_event = asyncio.Event()
        loop = _get_reader_loop()
        if loop is not None and loop.is_running():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not loop:
                try:
                    loop.call_soon_threadsafe(event.set)
                    return
                except RuntimeError:
                    pass  # loop closed between the check and the call
        event.set()

    # -- reader side (re-attach route, API event loop) ----------------------

    @property
    def last_seq(self) -> int:
        with self._meta_lock:
            return self._next_seq - 1

    @property
    def first_available_seq(self) -> Optional[int]:
        """Seq of the oldest retained entry, or None when empty."""
        with self._meta_lock:
            return self._entries[0][0] if self._entries else None

    def has_replay_gap(self, from_seq: int) -> bool:
        """True when events after ``from_seq`` were evicted by overflow."""
        with self._meta_lock:
            if not self._entries:
                return self.truncated
            return self._entries[0][0] > from_seq + 1

    def snapshot(self) -> Dict[str, Any]:
        """Scalar view for REST status payloads (safe off-loop)."""
        with self._meta_lock:
            return {
                "turn_id": self.turn_id,
                "state": self.state,
                "last_seq": self._next_seq - 1,
                "truncated": self.truncated,
                "user_message_id": self.user_message_id,
                "holder_kind": self.holder_kind,
                "source_label": self.source_label,
                "user_message_internal": self.user_message_internal,
            }

    def _entries_after(self, cursor: int) -> list[Tuple[int, str]]:
        with self._meta_lock:
            if not self._entries or self._entries[-1][0] <= cursor:
                return []
            skip = max(0, cursor + 1 - self._entries[0][0])
            return list(itertools.islice(self._entries, skip, None))

    async def stream_payloads(
        self,
        from_seq: int = 0,
    ) -> AsyncGenerator[str, None]:
        """Yield payload strings with seq > ``from_seq``: replay, then live tail.

        Ends after the last buffered event once the buffer leaves the live
        state. Callers own gap detection at attach time (``has_replay_gap``);
        a gap that opens MID-stream (overflow eviction outrunning a
        backpressured reader) raises ``TurnReplayGapError`` rather than
        silently skipping the evicted span.
        """
        cursor = from_seq
        while True:
            # Capture the pulse event BEFORE checking for data so an append
            # racing this check cannot be missed (it pulses the captured
            # event; we re-check on wake).
            pulse = self._pulse_event
            batch = self._entries_after(cursor)
            if batch and batch[0][0] > cursor + 1:
                raise TurnReplayGapError(
                    f"events {cursor + 1}..{batch[0][0] - 1} evicted for "
                    f"turn {self.turn_id}"
                )
            for seq, payload in batch:
                cursor = seq
                yield payload
            if self.state != STATE_LIVE:
                # Drain-then-stop: one more check catches entries appended
                # between the batch snapshot and the state read.
                if not self._entries_after(cursor):
                    return
                continue
            try:
                await asyncio.wait_for(pulse.wait(), timeout=_READER_WAIT_SECONDS)
            except asyncio.TimeoutError:
                continue


class TurnStreamRegistry:
    """Per-thread registry of the latest holder-turn buffer."""

    def __init__(self) -> None:
        self._buffers: Dict[str, TurnStreamBuffer] = {}
        self._lock = threading.Lock()

    def begin_turn(
        self,
        thread_id: str,
        user_id: str,
        user_message_id: Optional[str] = None,
        holder_kind: str = "user",
        source_label: Optional[str] = None,
        user_message_internal: bool = False,
    ) -> TurnStreamBuffer:
        """Create the buffer for a new holder turn, replacing any previous one.

        The per-thread lock serializes holder turns. A still-live previous
        buffer usually means its writer died without a terminal event, but
        there is also a benign race: the previous holder releases the thread
        lock (inside astream's finally) slightly before its chat route runs
        ``finish(STATE_DONE)``, so a fast next holder can mark a
        cleanly-completed predecessor ``aborted`` here (the late ``finish``
        then no-ops). Either way the aborted label only affects the replaced
        buffer's retention/state snapshot; clients that pin ``turn_id`` are
        unaffected, and marking it terminal gives late re-attachers an honest
        end-of-stream.
        """
        buffer = TurnStreamBuffer(
            thread_id,
            user_id,
            user_message_id=user_message_id,
            holder_kind=holder_kind,
            source_label=source_label,
            user_message_internal=user_message_internal,
        )
        with self._lock:
            previous = self._buffers.get(thread_id)
            self._buffers[thread_id] = buffer
        if previous is not None and previous.state == STATE_LIVE:
            previous.finish(STATE_ABORTED)
        return buffer

    def get(self, thread_id: str) -> Optional[TurnStreamBuffer]:
        with self._lock:
            buffer = self._buffers.get(thread_id)
        if buffer is not None and self._expired(buffer):
            self._discard(thread_id, buffer)
            return None
        return buffer

    def drop_thread(self, thread_id: str) -> None:
        """Forget a thread's buffer (thread deletion)."""
        with self._lock:
            self._buffers.pop(thread_id, None)

    def sweep_expired(self) -> int:
        """Drop finished buffers past retention. Returns the number dropped."""
        with self._lock:
            expired = [
                (thread_id, buffer)
                for thread_id, buffer in self._buffers.items()
                if self._expired(buffer)
            ]
        for thread_id, buffer in expired:
            self._discard(thread_id, buffer)
        return len(expired)

    def _discard(self, thread_id: str, buffer: TurnStreamBuffer) -> None:
        with self._lock:
            if self._buffers.get(thread_id) is buffer:
                self._buffers.pop(thread_id, None)

    @staticmethod
    def _expired(buffer: TurnStreamBuffer) -> bool:
        if buffer.state == STATE_LIVE or buffer.finished_at is None:
            return False
        return (time.time() - buffer.finished_at) > FINISHED_RETENTION_SECONDS


_registry: Optional[TurnStreamRegistry] = None
_registry_lock = threading.Lock()

# Event loop the attach-route readers run on (the API loop). Registered at
# API startup so off-loop writers (autonomous turns on sync worker threads)
# can marshal reader wakeups onto it. None until the API app starts, which
# is fine: nothing writes buffers before then.
_reader_loop: Optional[asyncio.AbstractEventLoop] = None


def set_reader_loop(loop: Optional[asyncio.AbstractEventLoop]) -> None:
    """Register the loop attach-route readers run on (API startup)."""
    global _reader_loop
    _reader_loop = loop


def _get_reader_loop() -> Optional[asyncio.AbstractEventLoop]:
    return _reader_loop


def get_turn_stream_registry() -> TurnStreamRegistry:
    """Process-wide registry singleton (the API is the only agent runtime)."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = TurnStreamRegistry()
    return _registry


def reset_turn_stream_registry() -> None:
    """Test seam: drop the singleton (and reader loop) so tests start clean."""
    global _registry
    with _registry_lock:
        _registry = None
    set_reader_loop(None)
