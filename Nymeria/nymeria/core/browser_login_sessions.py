"""In-process registry of live browser login sessions.

A login session is the handoff that lets a HUMAN drive one tab of the
Chrome the agent normally drives, so they can type a password the agent
must never see. It exists because the server-hosted headless Chrome keeps
its cookies in a persistent profile: sign in once by hand and the agent
acts signed-in from then on.

The flow, and where each piece lives:

1. The agent's login tool registers a session here and gets an
   ``asyncio.Future``, then dispatches ``login_session_start`` to the
   extension over the ordinary ``browser_command`` channel.
2. The extension starts a JPEG ``Page.startScreencast`` on that tab and
   POSTs frames to ``POST /browser-login/{session_id}/frame``, which lands
   them in this session's :meth:`BrowserLoginSession.append_frame` buffer.
3. nymeria-desktop tails ``GET /browser-login/{session_id}/stream`` and
   renders the frames, POSTing the operator's keystrokes and clicks back
   to ``POST /browser-login/{session_id}/input``.
4. "Done", the TTL, or a thread abort calls :meth:`finish`, which wakes the
   agent's future and drains the readers.

Two properties are load-bearing and structural rather than policy:

* **Frames never reach the agent.** They are only ever readable through
  :meth:`BrowserLoginSession.stream_frames`, which only the desktop SSE
  endpoint calls. The payload this module hands back to the agent's future
  carries an outcome and nothing else, so the password on screen is out of
  the model's view by construction. Pinned by
  ``tests/test_browser_login_sessions.py``.
* **The agent cannot drive the tab mid-login.** While a session is active,
  ``chrome_browser._run`` refuses commands aimed at that tab (see
  :func:`active_session_for_tab`), so the human and the agent never fight
  over one page.

The frame buffer is the ``core/turn_stream_buffer`` pulse pattern with one
deliberate difference: a reader that falls behind silently SKIPS the
evicted frames instead of raising a gap error. Dropping stale frames is
correct for video, where the newest frame supersedes the ones it outran.

Single-process only, like the sibling coordinators: futures and buffers
live in this process, which is safe because the API process is the sole
agent runtime in both deployment shapes.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Deque, Optional, Tuple

from .future_rendezvous import FutureRendezvous

logger = logging.getLogger(__name__)

# Hard ceiling on one login session. A human signing into Google with 2FA
# on their phone fits well inside this; the cap exists so a session the
# operator walks away from cannot pin a tab (and the agent) forever. It is
# NOT extended by activity: the desktop viewer shows the deadline as a
# countdown, and a countdown that silently resets is worse than an honest
# one. Matches the credential-prompt human wait (auth_prompt_coordinator).
LOGIN_SESSION_TTL_SECONDS = 600
# Short enough that "10 minutes" does not mean "10 to 11 minutes".
_SWEEP_INTERVAL_SECONDS = 15

# Frames held per session. The buffer absorbs an extension micro-batch
# (2-4 frames) plus jitter; anything older than that is stale video the
# viewer should skip rather than replay. ~17KB/frame measured at JPEG q60,
# so the byte cap is slack that only a full-motion burst would reach.
MAX_FRAMES_BUFFERED = 8
MAX_FRAME_BYTES_BUFFERED = 2 * 1024 * 1024

# Poll ceiling for live frame readers; real wakeups come from the pulse.
_READER_WAIT_SECONDS = 15.0

STATE_ACTIVE = "active"
STATE_ENDED = "ended"

# Why a session ended. ``completed`` is the operator clicking Done; the
# rest are the ways a session ends without them.
REASON_COMPLETED = "completed"
REASON_EXPIRED = "expired"
REASON_ABORTED = "aborted"
REASON_FAILED = "failed"
REASON_CANCELLED = "cancelled"


@dataclass
class BrowserLoginSession:
    """One live human-drives-the-tab handoff, with its frame buffer."""

    session_id: str
    user_id: str
    thread_id: str
    tab_id: int
    #: The page the operator was sent to sign into. A label for the viewer
    #: and the agent's result line, never used to route anything.
    url: str
    future: asyncio.Future
    #: The loop the desktop's SSE readers run on (the API loop), captured
    #: at construction so off-loop writers can marshal reader wakeups onto
    #: it. The sibling turn-stream buffer keeps this in a module global
    #: registered at API startup; per-session capture needs no startup wiring.
    loop: Optional[asyncio.AbstractEventLoop] = None
    created_at: float = field(default_factory=time.monotonic)
    metadata: dict[str, Any] = field(default_factory=dict)

    state: str = STATE_ACTIVE
    end_reason: Optional[str] = None
    #: When the first frame landed, so the viewer can tell "starting up"
    #: from "the page is simply static" (screencast is change-driven).
    first_frame_at: Optional[float] = None
    frames_received: int = 0
    frames_dropped: int = 0

    # Frame buffer internals. ``init=False`` so they are per-instance state
    # rather than constructor arguments; the pulse event is replaced (not
    # cleared) on every wake so any number of readers wake without races.
    _entries: Deque[Tuple[int, str]] = field(
        default_factory=deque, init=False, repr=False
    )
    _next_seq: int = field(default=1, init=False, repr=False)
    _bytes: int = field(default=0, init=False, repr=False)
    _pulse_event: asyncio.Event = field(
        default_factory=asyncio.Event, init=False, repr=False
    )
    _meta_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    # -- deadline -----------------------------------------------------------

    @property
    def expires_at(self) -> float:
        """Monotonic deadline this session is swept at."""
        return self.created_at + LOGIN_SESSION_TTL_SECONDS

    @property
    def seconds_remaining(self) -> float:
        """Seconds until the hard cap, floored at zero (viewer countdown)."""
        return max(0.0, self.expires_at - time.monotonic())

    def expires_at_iso(self) -> str:
        """Wall-clock deadline for the viewer, derived from the monotonic one."""
        return (
            datetime.now(timezone.utc) + timedelta(seconds=self.seconds_remaining)
        ).isoformat(timespec="seconds")

    # -- writer side (the extension's frame POSTs) --------------------------

    def append_frame(self, frame: dict[str, Any]) -> int:
        """Stamp ``seq`` into ``frame``, buffer it, and wake the viewer.

        Returns the stamped seq. A frame that arrives after the session
        ended is dropped and returns 0: the POST endpoint reads that as
        "stop capturing" and tells the extension to shut the screencast
        down, which is how a session that ended on the backend's clock
        reaches an extension that never heard about it.
        """
        with self._meta_lock:
            if self.state != STATE_ACTIVE:
                return 0
            seq = self._next_seq
            self._next_seq += 1
        frame["seq"] = seq
        payload = json.dumps(frame)
        with self._meta_lock:
            self._entries.append((seq, payload))
            self._bytes += len(payload)
            self.frames_received += 1
            if self.first_frame_at is None:
                self.first_frame_at = time.monotonic()
            while self._entries and (
                len(self._entries) > MAX_FRAMES_BUFFERED
                or self._bytes > MAX_FRAME_BYTES_BUFFERED
            ):
                _, dropped = self._entries.popleft()
                self._bytes -= len(dropped)
                self.frames_dropped += 1
        self._pulse()
        return seq

    def mark_ended(self, reason: str) -> bool:
        """Flip to ended and wake readers so they drain and disconnect.

        Returns False when the session had already ended, so callers racing
        the sweep do not double-report. Separate from the registry's
        :meth:`BrowserLoginSessionRegistry.finish` because the registry owns
        the agent's future while the session owns its own readers.
        """
        with self._meta_lock:
            if self.state != STATE_ACTIVE:
                return False
            self.state = STATE_ENDED
            self.end_reason = reason
        self._pulse()
        return True

    def _pulse(self) -> None:
        """Wake frame readers, marshaling onto the reader loop when off-loop.

        The frame POST endpoint runs on the API loop, where a plain
        ``Event.set()`` is correct. Endings can arrive from off-loop callers
        (a thread abort cascading from a sync path), and ``asyncio.Event.set``
        is not thread-safe, so those hand the set to the captured loop.
        """
        with self._meta_lock:
            event = self._pulse_event
            self._pulse_event = asyncio.Event()
        loop = self.loop
        if loop is not None and loop.is_running():
            try:
                running: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not loop:
                try:
                    loop.call_soon_threadsafe(event.set)
                    return
                except RuntimeError:
                    pass  # loop closed between the check and the call
        event.set()

    # -- reader side (the desktop's SSE attach) -----------------------------

    def snapshot(self) -> dict[str, Any]:
        """Scalar view for REST payloads and the viewer's status line.

        Deliberately carries no frame bytes: this is the shape that may be
        shown anywhere, including to the agent, so it must stay free of
        anything the screen said.
        """
        with self._meta_lock:
            return {
                "session_id": self.session_id,
                "thread_id": self.thread_id,
                "tab_id": self.tab_id,
                "url": self.url,
                "state": self.state,
                "end_reason": self.end_reason,
                "last_seq": self._next_seq - 1,
                "frames_received": self.frames_received,
                "frames_dropped": self.frames_dropped,
                "has_frame": self.first_frame_at is not None,
                "seconds_remaining": round(self.seconds_remaining, 1),
                "expires_at": self.expires_at_iso(),
            }

    def _entries_after(self, cursor: int) -> list[Tuple[int, str]]:
        with self._meta_lock:
            if not self._entries or self._entries[-1][0] <= cursor:
                return []
            skip = max(0, cursor + 1 - self._entries[0][0])
            return list(itertools.islice(self._entries, skip, None))

    async def stream_frames(self, from_seq: int = 0) -> AsyncGenerator[str, None]:
        """Yield buffered frame payloads with seq > ``from_seq``, then tail live.

        Ends once the session leaves the active state and the buffer is
        drained. A reader slower than the writer silently skips evicted
        frames: unlike a turn transcript, a skipped video frame loses
        nothing the newer frame does not already show.
        """
        cursor = from_seq
        while True:
            # Capture the pulse BEFORE checking for data, so an append
            # racing this check cannot be missed (it pulses the captured
            # event and we re-check on wake).
            pulse = self._pulse_event
            for seq, payload in self._entries_after(cursor):
                cursor = seq
                yield payload
            if self.state != STATE_ACTIVE:
                # Drain-then-stop: one more check catches frames appended
                # between the batch snapshot and the state read.
                if not self._entries_after(cursor):
                    return
                continue
            try:
                await asyncio.wait_for(pulse.wait(), timeout=_READER_WAIT_SECONDS)
            except asyncio.TimeoutError:
                continue


class BrowserLoginSessionRegistry(FutureRendezvous[BrowserLoginSession]):
    """Tracks live login sessions keyed by ``session_id``."""

    def __init__(self) -> None:
        super().__init__(
            ttl_seconds=LOGIN_SESSION_TTL_SECONDS,
            sweep_interval_seconds=_SWEEP_INTERVAL_SECONDS,
            log_label="browser_login_sessions",
        )

    def start(
        self,
        *,
        session_id: str,
        user_id: str,
        thread_id: str,
        tab_id: int,
        url: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> tuple[BrowserLoginSession, asyncio.Future]:
        """Register a session and return it with the agent's await future.

        Must be called from a running loop (the agent's login tool runs on
        the API loop), which is also the loop the desktop's frame readers
        will run on.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        session = BrowserLoginSession(
            session_id=session_id,
            user_id=user_id,
            thread_id=thread_id,
            tab_id=tab_id,
            url=url,
            future=future,
            loop=loop,
            metadata=metadata or {},
        )
        self._add(session_id, session)
        return session, future

    def active_for_tab(self, user_id: str, tab_id: int) -> Optional[BrowserLoginSession]:
        """The live session holding ``tab_id`` for ``user_id``, if any.

        The agent-suspension gate in ``chrome_browser._run`` reads this
        before dispatching, which is why it is keyed the way a chrome
        command is addressed (user plus tab) rather than by thread: a
        second thread must not be able to drive a tab a human is signing
        into just because it did not open the session.
        """
        with self._lock:
            for session in self._items.values():
                if (
                    session.user_id == user_id
                    and session.tab_id == tab_id
                    and session.state == STATE_ACTIVE
                ):
                    return session
        return None

    def active_for_user(self, user_id: str) -> list[BrowserLoginSession]:
        """Every live session for ``user_id`` (viewer listing, diagnostics)."""
        with self._lock:
            return [
                session
                for session in self._items.values()
                if session.user_id == user_id and session.state == STATE_ACTIVE
            ]

    def finish(
        self,
        session_id: str,
        *,
        reason: str,
        detail: Optional[str] = None,
    ) -> bool:
        """End a session: drain its readers and wake the waiting agent.

        Returns False when there is nothing to end (already finished,
        swept, or never registered), so a double "Done" is a no-op rather
        than a second result.
        """
        session = self.get(session_id)
        if session is not None:
            session.mark_ended(reason)
        return self._resolve(
            session_id,
            lambda record: _outcome(record, reason=reason, detail=detail),
        )

    def abort_thread(self, thread_id: str) -> int:
        """End every session owned by ``thread_id``. Returns how many ended.

        Called from the cancellation cascade so ``POST /threads/{id}/stop``
        does not leave a human staring at a viewer whose agent has gone.
        """
        matched = self._drain_matching(lambda session: session.thread_id == thread_id)
        aborted = 0
        for session in matched:
            session.mark_ended(REASON_ABORTED)
            if self._wake(session.future, _outcome(session, reason=REASON_ABORTED)):
                aborted += 1
        if aborted:
            logger.info(
                "browser_login_sessions aborted %d session(s) for thread %s",
                aborted,
                thread_id,
            )
        return aborted

    def _swept_result(self, record: BrowserLoginSession) -> dict[str, Any]:
        # The TTL sweep IS the auto-teardown: the base class pops the record
        # past its deadline, and this is the result the agent reads.
        return _outcome(record, reason=REASON_EXPIRED)

    def _on_orphan_swept(self, record: BrowserLoginSession) -> None:
        # The base class's designated pre-resolve side-effect hook, and the
        # right place to end the session: it runs before the future is woken,
        # so the agent never learns the session expired while its readers
        # still believe it is live.
        record.mark_ended(REASON_EXPIRED)
        logger.info(
            "browser_login_sessions expired session %s (user=%s, tab=%s, "
            "frames=%d, age>%ds)",
            record.session_id,
            record.user_id,
            record.tab_id,
            record.frames_received,
            int(self._ttl_seconds),
        )


def _outcome(
    session: BrowserLoginSession,
    *,
    reason: str,
    detail: Optional[str] = None,
) -> dict[str, Any]:
    """The payload the agent's future is woken with.

    Carries the outcome and the session's own counters, never a frame and
    never anything the page or the operator typed. This function is the
    only writer of that payload, so the "frames never reach the agent"
    property is one place to read and one place to test.
    """
    payload: dict[str, Any] = {
        "ok": reason == REASON_COMPLETED,
        "status": reason,
        "session_id": session.session_id,
        "tab_id": session.tab_id,
        "frames_received": session.frames_received,
        "duration_seconds": round(time.monotonic() - session.created_at, 1),
    }
    if detail:
        payload["detail"] = detail
    return payload


_registry: Optional[BrowserLoginSessionRegistry] = None
_registry_lock = threading.Lock()


def get_browser_login_registry() -> BrowserLoginSessionRegistry:
    """Process-wide singleton (the API is the only agent runtime)."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = BrowserLoginSessionRegistry()
    return _registry


def active_session_for_tab(user_id: str, tab_id: int) -> Optional[BrowserLoginSession]:
    """Module-level shortcut for the chrome dispatch gate."""
    return get_browser_login_registry().active_for_tab(user_id, tab_id)


def new_login_session_id() -> str:
    return f"blogin_{secrets.token_urlsafe(16)}"


def reset_for_tests() -> None:
    """Test seam: drop the singleton so each test starts with no sessions."""
    global _registry
    with _registry_lock:
        _registry = None


__all__ = [
    "BrowserLoginSession",
    "BrowserLoginSessionRegistry",
    "LOGIN_SESSION_TTL_SECONDS",
    "MAX_FRAMES_BUFFERED",
    "REASON_ABORTED",
    "REASON_CANCELLED",
    "REASON_COMPLETED",
    "REASON_EXPIRED",
    "REASON_FAILED",
    "STATE_ACTIVE",
    "STATE_ENDED",
    "active_session_for_tab",
    "get_browser_login_registry",
    "new_login_session_id",
    "reset_for_tests",
]
