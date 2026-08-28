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


class BrowserCommandCoordinator(FutureRendezvous[PendingCommand]):
    """Tracks in-flight browser commands keyed by ``command_id``."""

    def __init__(self) -> None:
        super().__init__(
            ttl_seconds=ORPHAN_TTL_SECONDS,
            sweep_interval_seconds=_SWEEP_INTERVAL_SECONDS,
            log_label="browser_command_coordinator",
        )
        # Which (user, thread) pairs have dispatched a browser command since
        # their last turn end (#191): the turn-end session release publishes
        # only for a turn that actually drove the browser, so ordinary turns
        # put nothing on the wire. Lives HERE, not in the tools module, so a
        # tools hot-reload cannot lose a pending release mid-turn. Entries
        # are popped at every turn end of their thread; one stranded by a
        # crashed turn is popped (and harmlessly released) by that thread's
        # next turn, so the set is bounded by live threads. Guarded by the
        # class's own lock like the rest of its shared state: register runs
        # on the event loop while the sync chat path pops from off-loop.
        self._turn_dispatches: set[tuple[str, str]] = set()

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
        )
        self._add(command_id, command)
        # The turn-end release ledger (#191): registering a command IS the
        # "this turn drove the browser" fact, so it is stamped here rather
        # than trusting each tool call site to remember.
        if user_id and thread_id:
            with self._lock:
                self._turn_dispatches.add((user_id, thread_id))
        return future

    def pop_turn_dispatched(self, user_id: str, thread_id: str) -> bool:
        """True (once) when this (user, thread) dispatched a browser command
        since the last pop. The turn-end seam calls this to decide whether a
        session release is worth publishing."""
        with self._lock:
            try:
                self._turn_dispatches.remove((user_id, thread_id))
            except KeyError:
                return False
            return True

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
    """Publish the turn-end ``browser_session_release`` event (#191).

    Called from the agent's DONE seam (``core/agent.py``, both the async and
    sync observe fire points, which every turn end funnels through exactly
    once). Publishes only when this (user, thread) dispatched a browser
    command since its last turn end, so ordinary turns put nothing on the
    wire. The extension drops its idle debugger holds on receipt, which is
    what lets the "being debugged" banner fall the moment the agent answers;
    a lost event degrades to the extension's own safety-net linger, so this
    must never raise into the turn tail.
    """
    if not user_id or not thread_id:
        return False
    coord = get_browser_command_coordinator()
    if not coord.pop_turn_dispatched(user_id, thread_id):
        return False
    try:
        from .event_bus import publish_autonomous_event

        publish_autonomous_event(
            event_type="browser_session_release",
            thread_id=thread_id,
            user_id=user_id,
            task_id="",
            data={},
        )
    except Exception:  # noqa: BLE001 - a release must never break a turn end
        logger.debug("browser_session_release publish failed", exc_info=True)
        return False
    return True


__all__ = [
    "BrowserCommandCoordinator",
    "PendingCommand",
    "get_browser_command_coordinator",
    "new_command_id",
    "release_browser_session",
]
