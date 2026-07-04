"""In-process rendezvous between agent-side ``cli_statusbar_*`` tools and
connected CLI clients (backlog #53, CLI modernization Phase 4).

The flow mirrors :mod:`browser_command_coordinator` (the chrome_* bridge):

1. A ``cli_statusbar_*`` tool allocates a ``command_id`` and calls
   :meth:`CLIConfigCoordinator.register` to get an ``asyncio.Future``.
2. The tool publishes a ``cli_config`` autonomous event carrying the
   ``command_id`` and the operation payload, then awaits the future with a
   short timeout.
3. Every connected Rich REPL CLI for the user consumes the event via
   ``/autonomous/stream``, applies and persists the change locally, and
   POSTs its outcome to ``POST /cli-config/{command_id}/result``.
4. The endpoint calls :meth:`resolve`; the FIRST ack wakes the awaiting
   tool (later acks report ``delivered=False``). A timeout means no CLI
   client is connected.

Single-process only: the future lives in this API process, like the
browser and hook-approval coordinators.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .future_rendezvous import FutureRendezvous, safe_set_result

logger = logging.getLogger(__name__)

_ORPHAN_TTL_SECONDS = 90
_SWEEP_INTERVAL_SECONDS = 30


@dataclass
class PendingCLIConfigCommand:
    command_id: str
    user_id: str
    thread_id: str
    command_type: str
    future: asyncio.Future
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)


class CLIConfigCoordinator(FutureRendezvous[PendingCLIConfigCommand]):
    """Tracks in-flight CLI config commands keyed by ``command_id``."""

    def __init__(self) -> None:
        super().__init__(
            ttl_seconds=_ORPHAN_TTL_SECONDS,
            sweep_interval_seconds=_SWEEP_INTERVAL_SECONDS,
            log_label="cli_config_coordinator",
        )

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
        command = PendingCLIConfigCommand(
            command_id=command_id,
            user_id=user_id,
            thread_id=thread_id,
            command_type=command_type,
            future=future,
            metadata=metadata or {},
        )
        self._add(command_id, command)
        return future

    def resolve(self, command_id: str, result: dict[str, Any]) -> bool:
        """Wake the tool with ``result``. Returns False when there is nothing
        to wake (already resolved by an earlier ack, swept, or unknown)."""
        return self._resolve(command_id, lambda command: result)

    def abort_thread(self, thread_id: str) -> int:
        """Resolve every pending command for ``thread_id`` with
        ``status="aborted"`` so ``POST /threads/{id}/stop`` snaps the
        awaiting tool back immediately (mirrors the browser coordinator)."""
        matched = self._drain_matching(lambda cmd: cmd.thread_id == thread_id)
        aborted = 0
        for cmd in matched:
            future = cmd.future
            if future.done():
                continue
            future.get_loop().call_soon_threadsafe(
                safe_set_result,
                future,
                {"ok": False, "status": "aborted", "error": "thread aborted"},
            )
            aborted += 1
        if aborted:
            logger.info(
                "cli_config_coordinator aborted %d pending command(s) for thread %s",
                aborted,
                thread_id,
            )
        return aborted

    def _swept_result(self, record: PendingCLIConfigCommand) -> dict[str, Any]:
        return {"ok": False, "status": "swept", "error": "command orphaned"}

    def _on_orphan_swept(self, record: PendingCLIConfigCommand) -> None:
        logger.warning(
            "cli_config_coordinator swept orphaned command %s "
            "(type=%s, user=%s, thread=%s, age>%ds)",
            record.command_id,
            record.command_type,
            record.user_id,
            record.thread_id,
            self._ttl_seconds,
        )


_coordinator: Optional[CLIConfigCoordinator] = None


def get_cli_config_coordinator() -> CLIConfigCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = CLIConfigCoordinator()
    return _coordinator


def new_command_id() -> str:
    return f"clicfg_{secrets.token_urlsafe(16)}"


__all__ = [
    "CLIConfigCoordinator",
    "PendingCLIConfigCommand",
    "get_cli_config_coordinator",
    "new_command_id",
]
