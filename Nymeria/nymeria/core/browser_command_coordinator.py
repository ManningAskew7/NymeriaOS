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

* Commands resolve in seconds, not minutes. ``_ORPHAN_TTL_SECONDS`` is
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
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ORPHAN_TTL_SECONDS = 90
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


class BrowserCommandCoordinator:
    """Tracks in-flight browser commands keyed by ``command_id``."""

    def __init__(self) -> None:
        self._commands: dict[str, PendingCommand] = {}
        self._lock = threading.Lock()
        self._sweep_task: Optional[asyncio.Task] = None
        self._sweep_started = False

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
        with self._lock:
            self._commands[command_id] = command
        self._start_sweep_locked()
        return future

    def get(self, command_id: str) -> Optional[PendingCommand]:
        with self._lock:
            return self._commands.get(command_id)

    def resolve(self, command_id: str, result: dict[str, Any]) -> bool:
        """Wake the tool with ``result``. Returns False if there's nothing to
        wake (already resolved, swept, or never registered)."""
        with self._lock:
            command = self._commands.pop(command_id, None)
        if command is None:
            return False
        future = command.future
        if future.done():
            return False
        future.get_loop().call_soon_threadsafe(_safe_set_result, future, result)
        return True

    def discard(self, command_id: str) -> None:
        """Remove a command without resolving its future. Use after a
        tool-side timeout, when the tool has already returned the timeout
        error."""
        with self._lock:
            self._commands.pop(command_id, None)

    def abort_thread(self, thread_id: str) -> int:
        """Resolve every pending command for ``thread_id`` with
        ``status="aborted"``. Returns the number of commands aborted.

        Called by ``agent_callable_lifecycle.abort_with_cascade`` so
        ``POST /threads/{id}/stop`` snaps awaiting browser tools back
        immediately instead of waiting for their per-command timeout.
        """
        with self._lock:
            matched = [
                cmd
                for cmd_id, cmd in list(self._commands.items())
                if cmd.thread_id == thread_id
            ]
            for cmd in matched:
                self._commands.pop(cmd.command_id, None)
        aborted = 0
        for cmd in matched:
            future = cmd.future
            if future.done():
                continue
            future.get_loop().call_soon_threadsafe(
                _safe_set_result,
                future,
                {"ok": False, "status": "aborted", "error": "thread aborted"},
            )
            aborted += 1
        if aborted:
            logger.info(
                "browser_command_coordinator aborted %d pending command(s) for thread %s",
                aborted,
                thread_id,
            )
        return aborted

    def pending_count(self) -> int:
        with self._lock:
            return len(self._commands)

    def _start_sweep_locked(self) -> None:
        if self._sweep_started:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._sweep_started = True
        self._sweep_task = loop.create_task(self._sweep_forever())

    async def _sweep_forever(self) -> None:
        while True:
            try:
                await asyncio.sleep(_SWEEP_INTERVAL_SECONDS)
                self._sweep_once()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("browser_command_coordinator sweep failed")

    def _sweep_once(self) -> None:
        cutoff = time.monotonic() - _ORPHAN_TTL_SECONDS
        orphans: list[PendingCommand] = []
        with self._lock:
            for command_id in list(self._commands.keys()):
                if self._commands[command_id].created_at < cutoff:
                    orphans.append(self._commands.pop(command_id))
        for orphan in orphans:
            future = orphan.future
            if future.done():
                continue
            logger.warning(
                "browser_command_coordinator swept orphaned command %s "
                "(type=%s, user=%s, thread=%s, age>%ds)",
                orphan.command_id,
                orphan.command_type,
                orphan.user_id,
                orphan.thread_id,
                _ORPHAN_TTL_SECONDS,
            )
            future.get_loop().call_soon_threadsafe(
                _safe_set_result,
                future,
                {"ok": False, "status": "swept", "error": "command orphaned"},
            )


def _safe_set_result(future: asyncio.Future, value: Any) -> None:
    if not future.done():
        future.set_result(value)


_coordinator: Optional[BrowserCommandCoordinator] = None


def get_browser_command_coordinator() -> BrowserCommandCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = BrowserCommandCoordinator()
    return _coordinator


def new_command_id() -> str:
    return f"bcmd_{secrets.token_urlsafe(16)}"


__all__ = [
    "BrowserCommandCoordinator",
    "PendingCommand",
    "get_browser_command_coordinator",
    "new_command_id",
]
