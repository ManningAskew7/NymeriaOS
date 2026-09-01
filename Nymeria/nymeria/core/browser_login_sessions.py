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
from .loop_pulse import LoopPulse

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
# Wire timeout on the fire-and-forget ``login_session_stop`` push in
# ``_announce_ended``. Single source of truth: ``chrome_browser._TIMEOUTS``
# references this for the same command type.
LOGIN_SESSION_STOP_TIMEOUT_SECONDS = 10

# Frames held per session. The buffer absorbs an extension micro-batch
# (2-4 frames) plus jitter; anything older than that is stale video the
# viewer should skip rather than replay. ~17KB/frame measured at JPEG q60,
# so the byte cap is slack that only a full-motion burst would reach.
MAX_FRAMES_BUFFERED = 8
MAX_FRAME_BYTES_BUFFERED = 2 * 1024 * 1024

# Poll ceiling for live frame readers; real wakeups come from the pulse.
_READER_WAIT_SECONDS = 15.0

# How long a finished session's outcome stays readable after the registry
# pops its record (``_resolve`` pops), and how many are kept. The agent's
# ``chrome_await_login`` may arrive well after the user clicked Done, so the
# outcome must outlive the record; generously past the TTL, because a turn
# that parks the await behind other work still deserves the real answer.
_OUTCOME_RETENTION_SECONDS = 900
_MAX_RECENT_OUTCOMES = 64

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
    #: Which browser the session runs in (the persistent extension
    #: client_id resolved at start). The login-input relay stamps its
    #: events with it so keystrokes reach ONLY this browser, and the
    #: target-switch guard reads it to refuse retargeting mid-login.
    #: None on legacy callers; those degrade to unstamped (all-browser)
    #: input events, the pre-routing shape.
    client_id: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)

    state: str = STATE_ACTIVE
    end_reason: Optional[str] = None
    #: When the first frame landed, so the viewer can tell "starting up"
    #: from "the page is simply static" (screencast is change-driven).
    first_frame_at: Optional[float] = None
    frames_received: int = 0
    frames_dropped: int = 0

    # Frame buffer internals. ``init=False`` so they are per-instance state
    # rather than constructor arguments. The reader doorbell is the shared
    # ``LoopPulse`` (replace-not-clear wake semantics, off-loop marshalling);
    # the registry binds it to the reader loop at :meth:`start`.
    _entries: Deque[Tuple[int, str]] = field(
        default_factory=deque, init=False, repr=False
    )
    _next_seq: int = field(default=1, init=False, repr=False)
    _bytes: int = field(default=0, init=False, repr=False)
    _doorbell: LoopPulse = field(default_factory=LoopPulse, init=False, repr=False)
    _meta_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        """Name the loop the desktop's SSE frame readers run on (the API
        loop), so off-loop writers can marshal reader wakeups onto it. The
        sibling turn-stream buffer keeps this in a module global registered
        at API startup; per-session binding needs no startup wiring."""
        self._doorbell.bind(loop)

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
        self._announce_ended()
        return True

    def _announce_ended(self) -> None:
        """Tell the two parties an ending never reaches otherwise. Never raises.

        The desktop gets ``browser_login_ended`` on the autonomous stream:
        an attached viewer also sees the stream's own ``login_end``, but a
        viewer that never attached (or a prompt chip) has only this
        broadcast. The extension gets a fire-and-forget
        ``login_session_stop`` command envelope: its only other stop cue is
        ``session_active: false`` on a frame ack, and a static page sends
        no frames, so without this a screencast (and its debugger hold)
        could outlive the session indefinitely. The stop is idempotent
        extension-side, and the result it POSTs back resolves no future
        (the result endpoint answers ``delivered=False`` for an unknown
        command_id, by design).

        Sits on the state flip above, so every ended session announces
        exactly once no matter which path ended it (Done, cancel, TTL
        sweep, thread abort).
        """
        try:
            from .browser_command_coordinator import new_command_id
            from .event_bus import publish_autonomous_event

            publish_autonomous_event(
                event_type="browser_login_ended",
                thread_id=self.thread_id,
                user_id=self.user_id,
                task_id="",
                data=self.snapshot(),
            )
            stop_data: dict = {
                "command_id": new_command_id(),
                "command_type": "login_session_stop",
                "args": {"tab_id": self.tab_id, "session_id": self.session_id},
                "timeout_seconds": LOGIN_SESSION_STOP_TIMEOUT_SECONDS,
            }
            if self.client_id:
                # Route the stop to the one browser the session ran in
                # (underscore key: read by the per-subscriber delivery
                # filter, stripped from the wire). Unstamped it would land
                # on every connected browser; the stop is idempotent, but
                # a foreign browser must not see commands it was never
                # part of. An unpinned session (no target resolvable at
                # start) keeps the all-browsers broadcast.
                stop_data["_target_client_id"] = self.client_id
            publish_autonomous_event(
                event_type="browser_command",
                thread_id=self.thread_id,
                user_id=self.user_id,
                task_id="",
                data=stop_data,
            )
        except Exception:  # noqa: BLE001 - an ending must never fail to end
            logger.debug(
                "browser_login_sessions ended-announce failed for %s",
                self.session_id,
                exc_info=True,
            )

    def _pulse(self) -> None:
        """Wake frame readers (off-loop callers are marshalled by the pulse).

        The frame POST endpoint rings from the API loop; endings can ring
        from off-loop callers (a thread abort cascading from a sync path).
        Both are the shared :class:`LoopPulse`'s problem now.
        """
        self._doorbell.ring()

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
            pulse = self._doorbell.listen()
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


class LoginSessionConflictError(RuntimeError):
    """The user already has a live login session (one human, one screen).

    Raised by :meth:`BrowserLoginSessionRegistry.start`, which is the
    ATOMIC enforcement of one-session-per-user: callers may pre-check
    ``active_for_user`` for a friendly early answer, but that check races
    their own awaits (tab creation is a network round trip), so only the
    registry's claim-under-lock decides. Carries the winning session so
    the loser can name it.
    """

    def __init__(self, existing: BrowserLoginSession) -> None:
        super().__init__(
            f"login session {existing.session_id} already active for "
            f"user {existing.user_id}"
        )
        self.existing = existing


class BrowserLoginSessionRegistry(FutureRendezvous[BrowserLoginSession]):
    """Tracks live login sessions keyed by ``session_id``."""

    def __init__(self) -> None:
        super().__init__(
            ttl_seconds=LOGIN_SESSION_TTL_SECONDS,
            sweep_interval_seconds=_SWEEP_INTERVAL_SECONDS,
            log_label="browser_login_sessions",
        )
        # Outcomes of finished sessions, keyed by session_id:
        # ``(recorded_at, user_id, payload)``. Needed because ``_resolve``
        # POPS the record: the agent's await may arrive after the user
        # finished, and "no such session" would be a lie about a login that
        # succeeded. Own lock: builders run inside base-class paths whose
        # lock discipline this store must not depend on.
        self._recent_outcomes: dict[str, tuple[float, str, dict[str, Any]]] = {}
        self._recent_lock = threading.Lock()

    def _remember_outcome(
        self, session: BrowserLoginSession, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Record a finished session's agent payload, pruned by age and count."""
        now = time.monotonic()
        with self._recent_lock:
            self._recent_outcomes[session.session_id] = (
                now,
                session.user_id,
                dict(payload),
            )
            expired = [
                key
                for key, (at, _, _) in self._recent_outcomes.items()
                if now - at > _OUTCOME_RETENTION_SECONDS
            ]
            for key in expired:
                del self._recent_outcomes[key]
            while len(self._recent_outcomes) > _MAX_RECENT_OUTCOMES:
                oldest = min(self._recent_outcomes, key=lambda k: self._recent_outcomes[k][0])
                del self._recent_outcomes[oldest]
        return payload

    def outcome_for(self, session_id: str, *, user_id: str) -> Optional[dict[str, Any]]:
        """A finished session's outcome, or None. Scoped to its owner:
        another user's session_id reads the same as one that never existed."""
        with self._recent_lock:
            entry = self._recent_outcomes.get(session_id)
        if entry is None:
            return None
        recorded_at, owner, payload = entry
        if owner != user_id:
            return None
        if time.monotonic() - recorded_at > _OUTCOME_RETENTION_SECONDS:
            return None
        return dict(payload)

    async def await_outcome(
        self, session_id: str, *, user_id: str, timeout_seconds: float
    ) -> tuple[str, Optional[dict[str, Any]]]:
        """Wait (bounded) for a session to end; never ends it.

        Returns ``("ended", outcome)`` once the session has finished (now
        or already), ``("active", snapshot)`` when the wait ran out with
        the session still live, or ``("unknown", None)`` for an id that is
        not this user's or is gone past retention. The wait shields the
        session's future: this method's own timeout, or the caller being
        cancelled (a tool timeout), must never cancel the login itself.
        """
        session = self.get(session_id)
        if session is not None and session.user_id == user_id:
            try:
                outcome = await asyncio.wait_for(
                    asyncio.shield(session.future), timeout=max(0.0, timeout_seconds)
                )
                return "ended", outcome
            except asyncio.TimeoutError:
                if session.state == STATE_ACTIVE:
                    return "active", session.snapshot()
                # Ended in the same instant the wait gave up. Every ending
                # path wakes the future right after the state flip, so a
                # short grace wait reads the real outcome instead of racing
                # the resolver to the remembered-outcome store.
                try:
                    outcome = await asyncio.wait_for(
                        asyncio.shield(session.future), timeout=1.0
                    )
                    return "ended", outcome
                except asyncio.TimeoutError:
                    # A pathological path ended the session without waking
                    # the future; fall through to the remembered-outcome
                    # store, whose miss answers "unknown" honestly.
                    pass
        recent = self.outcome_for(session_id, user_id=user_id)
        if recent is not None:
            return "ended", recent
        return "unknown", None

    def start(
        self,
        *,
        session_id: str,
        user_id: str,
        thread_id: str,
        tab_id: int,
        url: str,
        client_id: Optional[str] = None,
    ) -> tuple[BrowserLoginSession, asyncio.Future]:
        """Register a session and return it with the agent's await future.

        Must be called from a running loop (the agent's login tool runs on
        the API loop), which is also the loop the desktop's frame readers
        will run on.

        Raises :class:`LoginSessionConflictError` when the user already
        has a live session. The check and the insert share ONE lock
        acquisition: a caller-side pre-check races the caller's own awaits
        (two concurrent starts both read "none live" while one is still
        creating its tab), so this is where one-per-user is actually
        enforced.
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
            client_id=client_id,
        )
        session.bind_loop(loop)
        with self._lock:
            for other in self._items.values():
                if other.user_id == user_id and other.state == STATE_ACTIVE:
                    raise LoginSessionConflictError(other)
            self._items[session_id] = session
        self._ensure_sweep()
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
            lambda record: self._remember_outcome(
                record, _outcome(record, reason=reason, detail=detail)
            ),
        )

    def abort_thread(self, thread_id: str) -> int:
        """End every session owned by ``thread_id``. Returns how many ended.

        Called from the cancellation cascade so ``POST /threads/{id}/stop``
        does not leave a human staring at a viewer whose agent has gone.
        """
        matched = self._drain_matching(lambda session: session.thread_id == thread_id)
        aborted = 0
        for session in matched:
            # Remember BEFORE announcing: the drain above already popped the
            # record, so until the outcome lands in the retention store a
            # concurrent chrome_await_login reads "unknown" about a login
            # that just ended, and mark_ended's announce publishes
            # synchronously (seconds, on a stalled Redis).
            payload = self._remember_outcome(
                session, _outcome(session, reason=REASON_ABORTED)
            )
            session.mark_ended(REASON_ABORTED)
            if self._wake(session.future, payload):
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
        # past its deadline, and this is the result the agent reads. The
        # outcome was already remembered in _on_orphan_swept (before the
        # announce, closing the pop-to-remember "unknown" window); this
        # second remember overwrites the same key, harmlessly.
        return self._remember_outcome(record, _outcome(record, reason=REASON_EXPIRED))

    def _on_orphan_swept(self, record: BrowserLoginSession) -> None:
        # The base class's designated pre-resolve side-effect hook, and the
        # right place to end the session: it runs before the future is woken,
        # so the agent never learns the session expired while its readers
        # still believe it is live. Remember the outcome FIRST: the base
        # already popped the record, and mark_ended's announce publishes
        # synchronously, so until the retention store has the outcome a
        # concurrent await reads "unknown" about a login that just expired.
        self._remember_outcome(record, _outcome(record, reason=REASON_EXPIRED))
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

    ``login_completed`` is whether the LOGIN itself finished (``reason ==
    REASON_COMPLETED``), never whether a tool call succeeded: this function
    has no "tool call" to speak of (it also feeds the TTL sweep and thread
    abort, neither of which is one), so there is no ``ok`` field here.
    ``tools/chrome_browser.py::_login_json`` is the layer that adds a
    tool-call-success ``ok`` on top, uniformly, when it composes this into a
    tool's JSON return (#291).
    """
    payload: dict[str, Any] = {
        "login_completed": reason == REASON_COMPLETED,
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
    "LoginSessionConflictError",
    "LOGIN_SESSION_STOP_TIMEOUT_SECONDS",
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
