"""In-process rendezvous between agent-side ``chrome_*`` tools and the
extension-side result POSTs.

The flow:

1. A ``chrome_*`` tool allocates a ``command_id`` and calls
   :meth:`BrowserCommandCoordinator.register` to get an ``asyncio.Future``.
2. The tool publishes a ``browser_command`` autonomous event carrying the
   ``command_id`` and the operation payload, then awaits the future with a
   short per-command timeout.
3. The connected Chrome extension consumes the event via
   ``/autonomous/stream``, runs the action, and POSTs the result to
   ``POST /browser-commands/{command_id}/result``.
4. The endpoint calls :meth:`resolve`, which wakes the awaiting tool.

Differences from :class:`AuthPromptCoordinator`:

* Commands resolve in seconds, not minutes. ``ORPHAN_TTL_SECONDS`` is
  90s instead of 600s.
* :meth:`abort_thread` lets the cancellation cascade
  (``agent_callable_lifecycle.abort_with_cascade``) resolve every pending
  command for an aborted thread with ``status="aborted"`` so the tool
  returns immediately on ``POST /threads/{id}/stop``.

Single-process only: the future lives in this process. If the API is ever
scaled to multiple workers, the resolver path needs to use Redis pub/sub
to fan out to whichever worker owns the pending command.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .future_rendezvous import FutureRendezvous

logger = logging.getLogger(__name__)

# Public, unlike the same constant in the sibling coordinators: the chrome
# tools must bound their own waits by it (a tool that waits past the sweep is
# told its command was orphaned while the extension is still working).
ORPHAN_TTL_SECONDS = 90
_SWEEP_INTERVAL_SECONDS = 30


@dataclass
class PendingCommand:
    command_id: str
    user_id: str
    thread_id: str
    command_type: str
    future: asyncio.Future
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)
    # The browser this command was routed to (persistent extension
    # client_id). The result endpoint compares it against the POSTing
    # client's X-Nymeria-Client-Id so a stale second instance (the #282
    # shape) cannot resolve a command that was never sent to it.
    target_client_id: Optional[str] = None


class BrowserCommandCoordinator(FutureRendezvous[PendingCommand]):
    """Tracks in-flight browser commands keyed by ``command_id``."""

    def __init__(self) -> None:
        super().__init__(
            ttl_seconds=ORPHAN_TTL_SECONDS,
            sweep_interval_seconds=_SWEEP_INTERVAL_SECONDS,
            log_label="browser_command_coordinator",
        )
        # Which (user, thread) pairs have dispatched a browser command since
        # their last turn end (#191), and WHICH browsers (target client_ids)
        # they drove: the turn-end session release publishes only for a turn
        # that actually drove a browser, and only toward the browsers it
        # drove. Lives HERE, not in the tools module, so a
        # tools hot-reload cannot lose a pending release mid-turn. Entries
        # are popped at every turn end of their thread; one stranded by a
        # crashed turn is popped (and harmlessly released) by that thread's
        # next turn, so the map is bounded by live threads. Guarded by the
        # class's own lock like the rest of its shared state: register runs
        # on the event loop while the sync chat path pops from off-loop.
        # The empty-string member means "dispatched with no resolved target"
        # (pre-routing callers, tests): its release publishes unstamped, the
        # legacy all-browsers shape.
        self._turn_dispatches: dict[tuple[str, str], set[str]] = {}
        # Last browser each (user, thread) dispatched to (read back by the
        # login-session pin and the reload note), and the pending one-shot
        # switch markers: a dispatch that CHANGES the thread's browser arms
        # a marker naming the previous one, consumed by the next
        # tab-addressed dispatch (the stale-tab refusal). Tab ids are
        # learned from tab-less dispatches too (a tab listing's result),
        # so the switch is detected on every dispatch while only
        # tab-addressed ones may consume the marker. Live HERE, not in the
        # tools module, so a tools hot-reload cannot forget a switch
        # mid-conversation. Bounded by (user, thread) pairs that ever
        # drove a browser this process, one client_id string each.
        self._thread_targets: dict[tuple[str, str], str] = {}
        self._pending_switches: dict[tuple[str, str], str] = {}

    @property
    def _commands(self) -> dict[str, PendingCommand]:
        """Read alias for the shared registry (kept for readability/tests)."""
        return self._items

    def register(
        self,
        *,
        command_id: str,
        user_id: str,
        thread_id: str,
        command_type: str,
        metadata: Optional[dict[str, Any]] = None,
        target_client_id: Optional[str] = None,
    ) -> asyncio.Future:
        """Create the in-process future resolved by the result endpoint."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        command = PendingCommand(
            command_id=command_id,
            user_id=user_id,
            thread_id=thread_id,
            command_type=command_type,
            future=future,
            metadata=metadata or {},
            target_client_id=target_client_id,
        )
        self._add(command_id, command)
        # The turn-end release ledger (#191): registering a command IS the
        # "this turn drove the browser" fact, so it is stamped here rather
        # than trusting each tool call site to remember.
        if user_id and thread_id:
            with self._lock:
                self._turn_dispatches.setdefault((user_id, thread_id), set()).add(
                    target_client_id or ""
                )
        return future

    def note_dispatch_target(
        self, user_id: str, thread_id: str, target: str
    ) -> None:
        """Record ``target`` as the thread's current dispatch browser.

        Read back by :meth:`last_dispatch_target` (the login-session pin
        and the reload note), so those callers name the browser a dispatch
        ACTUALLY went to instead of re-running the resolution ladder. A
        call that CHANGES the thread's browser arms the one-shot switch
        marker with the previous one (:meth:`pop_switch_marker`).
        """
        if not user_id or not thread_id or not target:
            return
        key = (user_id, thread_id)
        with self._lock:
            previous = self._thread_targets.get(key)
            self._thread_targets[key] = target
            if previous is not None and previous != target:
                self._pending_switches[key] = previous

    def pop_switch_marker(self, user_id: str, thread_id: str) -> Optional[str]:
        """The browser the thread last switched AWAY from, popped, or None.

        Consumed only by tab-addressed dispatches (the callers that can be
        aimed with a stale tab id), so a tab-less command between the
        switch and the next tab-addressed one cannot eat the refusal the
        marker exists to produce. The caller compares the popped value
        against its current target: equal means the thread switched away
        and back, and those tab ids are live again.
        """
        with self._lock:
            return self._pending_switches.pop((user_id, thread_id), None)

    def last_dispatch_target(self, user_id: str, thread_id: str) -> Optional[str]:
        """The browser this (user, thread) most recently dispatched to."""
        with self._lock:
            return self._thread_targets.get((user_id, thread_id))

    def pop_turn_targets(self, user_id: str, thread_id: str) -> set[str]:
        """The browsers this (user, thread) drove since the last pop, popped.

        Empty when the turn dispatched nothing. The turn-end seam calls
        this to decide whether (and toward which browsers) a session
        release is worth publishing. An empty-string member means a
        dispatch with no resolved target (legacy callers): its release
        publishes unstamped."""
        with self._lock:
            return self._turn_dispatches.pop((user_id, thread_id), set())

    def resolve(self, command_id: str, result: dict[str, Any]) -> bool:
        """Wake the tool with ``result``. Returns False if there's nothing to
        wake (already resolved, swept, or never registered)."""
        return self._resolve(command_id, lambda command: result)

    def abort_thread(self, thread_id: str) -> int:
        """Resolve every pending command for ``thread_id`` with
        ``status="aborted"``. Returns the number of commands aborted.

        Called by ``agent_callable_lifecycle.abort_with_cascade`` so
        ``POST /threads/{id}/stop`` snaps awaiting browser tools back
        immediately instead of waiting for their per-command timeout.
        """
        matched = self._drain_matching(lambda cmd: cmd.thread_id == thread_id)
        aborted = 0
        for cmd in matched:
            if self._wake(
                cmd.future,
                {"ok": False, "status": "aborted", "error": "thread aborted"},
            ):
                aborted += 1
        if aborted:
            logger.info(
                "browser_command_coordinator aborted %d pending command(s) for thread %s",
                aborted,
                thread_id,
            )
        return aborted

    def _swept_result(self, record: PendingCommand) -> dict[str, Any]:
        return {"ok": False, "status": "swept", "error": "command orphaned"}

    def _on_orphan_swept(self, record: PendingCommand) -> None:
        logger.warning(
            "browser_command_coordinator swept orphaned command %s "
            "(type=%s, user=%s, thread=%s, age>%ds)",
            record.command_id,
            record.command_type,
            record.user_id,
            record.thread_id,
            self._ttl_seconds,
        )


_coordinator: Optional[BrowserCommandCoordinator] = None


def get_browser_command_coordinator() -> BrowserCommandCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = BrowserCommandCoordinator()
    return _coordinator


def new_command_id() -> str:
    return f"bcmd_{secrets.token_urlsafe(16)}"


def release_browser_session(user_id: str, thread_id: str) -> bool:
    """Publish the turn-end ``browser_session_release`` event(s) (#191).

    Called from the agent's DONE seam (``core/agent.py``, both the async and
    sync observe fire points, which every turn end funnels through exactly
    once). Publishes only when this (user, thread) dispatched a browser
    command since its last turn end, so ordinary turns put nothing on the
    wire, and only toward the browsers the turn actually drove (each event
    is stamped ``_target_client_id`` so the delivery filter hands it to
    that browser alone). The extension drops its idle debugger holds on
    receipt, which is what lets the "being debugged" banner fall the moment
    the agent answers; a lost event degrades to the extension's own
    safety-net linger, so this must never raise into the turn tail.

    The thread's per-tab drive leases are released first, uncondition-
    ally. The release EVENT for a browser is then suppressed while another
    thread still holds live leases there: the extension drops idle holds
    per-browser, not per-tab, so publishing would release the other
    thread's tabs mid-task (the cross-thread interference measured in the
    #282 family). The suppressed browser falls back to the extension's own
    idle linger.
    """
    if not user_id or not thread_id:
        return False
    coord = get_browser_command_coordinator()
    targets = coord.pop_turn_targets(user_id, thread_id)
    try:
        from .browser_drive_leases import get_browser_drive_leases

        leases = get_browser_drive_leases()
        # A browser this thread still held tab leases on was driven by it,
        # whether or not the popped ledger names it (a crashed earlier turn
        # can pop the ledger while its leases live on), so the released
        # browsers join the publish set.
        targets |= leases.release_thread(user_id, thread_id)
    except Exception:  # noqa: BLE001 - lease cleanup must never break a turn end
        logger.debug("browser lease release failed", exc_info=True)
        leases = None
    if not targets:
        return False
    published = False
    try:
        from .event_bus import publish_autonomous_event

        for target in sorted(targets):
            if target and leases is not None and leases.other_thread_holds(
                user_id=user_id, client_id=target, thread_id=thread_id
            ):
                logger.debug(
                    "browser_session_release suppressed for %s: another "
                    "thread holds live leases",
                    target[:24],
                )
                continue
            data: dict[str, Any] = {}
            if target:
                data["_target_client_id"] = target
            publish_autonomous_event(
                event_type="browser_session_release",
                thread_id=thread_id,
                user_id=user_id,
                task_id="",
                data=data,
            )
            published = True
    except Exception:  # noqa: BLE001 - a release must never break a turn end
        logger.debug("browser_session_release publish failed", exc_info=True)
        return published
    return published


__all__ = [
    "BrowserCommandCoordinator",
    "PendingCommand",
    "get_browser_command_coordinator",
    "new_command_id",
    "release_browser_session",
]
