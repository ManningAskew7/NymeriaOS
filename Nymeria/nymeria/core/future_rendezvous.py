"""Generic in-process future rendezvous.

A small registry of pending records, each holding an ``asyncio.Future`` and a
``created_at`` monotonic timestamp, plus a background sweep that resolves
orphaned records once they pass a TTL. This is the shared machinery behind
:class:`~nymeria.core.auth_prompt_coordinator.AuthPromptCoordinator` (credential
prompts) and :class:`~nymeria.core.browser_command_coordinator.BrowserCommandCoordinator`
(chrome tool commands): both keep a ``dict[str, record]`` under a lock, register
futures, resolve/discard by id, and run an identical sweep loop. Only the record
type, the TTL/interval, the log wording, and the resolve/swept payloads differ,
so subclasses supply those via hooks while the registry and sweep live here once.

Single-process only: futures live in this process. If the API is ever scaled to
multiple workers, the resolver path needs Redis pub/sub to fan out to whichever
worker owns the pending record.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable, Generic, Optional, Protocol, TypeVar

logger = logging.getLogger(__name__)


def safe_set_result(future: asyncio.Future, value: Any) -> None:
    """Set ``future``'s result unless it is already done.

    Always call this via ``loop.call_soon_threadsafe`` so the future is touched
    on its owning loop, since resolvers may run on a different thread (HTTP
    endpoint handlers) than the awaiting tool.
    """
    if not future.done():
        future.set_result(value)


class RendezvousRecord(Protocol):
    """Structural contract every registered record must satisfy."""

    future: asyncio.Future
    created_at: float


TRecord = TypeVar("TRecord", bound=RendezvousRecord)


class FutureRendezvous(Generic[TRecord]):
    """Lock-guarded registry of pending futures with an orphan sweep.

    Subclasses build their own domain records and expose a domain-named
    ``register``/``resolve`` surface, delegating storage to :meth:`_add`,
    :meth:`_resolve`, and :meth:`_drain_matching`, and supplying the
    sweep payload via :meth:`_swept_result` (and optional logging via
    :meth:`_on_orphan_swept`).
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        sweep_interval_seconds: float,
        log_label: str,
    ) -> None:
        self._items: dict[str, TRecord] = {}
        self._lock = threading.Lock()
        self._sweep_task: Optional[asyncio.Task] = None
        self._sweep_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ttl_seconds = ttl_seconds
        self._sweep_interval_seconds = sweep_interval_seconds
        self._log_label = log_label

    # Registry

    def _add(self, key: str, record: TRecord) -> None:
        """Store ``record`` under ``key`` and ensure the sweep loop is running."""
        with self._lock:
            self._items[key] = record
        self._ensure_sweep()

    def get(self, key: str) -> Optional[TRecord]:
        with self._lock:
            return self._items.get(key)

    def discard(self, key: str) -> None:
        """Remove a record without resolving its future. Use only when the
        owning tool has already returned (e.g. cleanup after a tool-side
        timeout)."""
        with self._lock:
            self._items.pop(key, None)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._items)

    def _resolve(
        self,
        key: str,
        build_result: Callable[[TRecord], dict[str, Any]],
    ) -> bool:
        """Pop ``key`` and wake its future with ``build_result(record)``.

        Returns False if there is nothing to wake (already resolved, swept, or
        never registered). ``build_result`` is invoked only after the record is
        confirmed present and its future not yet done, so subclasses can read
        record state (e.g. attempt counters) into the payload.
        """
        with self._lock:
            record = self._items.pop(key, None)
        if record is None:
            return False
        future = record.future
        if future.done():
            return False
        return self._wake(future, build_result(record))

    def _drain_matching(self, predicate: Callable[[TRecord], bool]) -> list[TRecord]:
        """Pop and return every record matching ``predicate`` (under the lock).

        The caller is responsible for resolving the returned futures; this only
        removes them from the registry so they can't be double-resolved.
        """
        with self._lock:
            matched = [(key, record) for key, record in list(self._items.items()) if predicate(record)]
            for key, _ in matched:
                self._items.pop(key, None)
        return [record for _, record in matched]

    # Sweep loop

    def _ensure_sweep(self) -> None:
        """Arm the orphan sweep on the current loop, re-arming after loop death.

        Coordinators are process-wide singletons but registrations can arrive
        from short-lived loops (e.g. a sync dispatch bridge's ``asyncio.run``).
        A plain started-once flag would latch onto whichever loop registered
        first and leave the coordinator sweep-less forever once that loop
        closed; instead the owning loop is tracked and the sweep lazily
        re-arms on the next registration whenever that loop is gone. Lazy
        re-arm is sufficient: registration precedes every park, and the sweep
        is orphan hygiene, not a liveness dependency (waiters carry their own
        timeouts).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        with self._lock:
            task, owner = self._sweep_task, self._sweep_loop
            if task is not None and owner is not None:
                if owner is loop and not task.done():
                    return
                if owner is not loop and not owner.is_closed() and not task.done():
                    return  # alive on another still-open loop
            # Create first, assign both after: a failed create_task must not
            # leave _sweep_loop pointing at a loop whose task was never made.
            new_task = loop.create_task(self._sweep_forever())
            self._sweep_loop = loop
            self._sweep_task = new_task

    def _wake(self, future: asyncio.Future, payload: dict[str, Any]) -> bool:
        """Schedule ``payload`` onto ``future`` on its owning loop.

        Returns False without raising when there is nothing to wake: the
        future is already done, or its owning loop has closed (the waiter
        died with its loop), so bulk paths (sweep, thread aborts) can keep
        going past a dead record instead of aborting mid-batch.
        """
        if future.done():
            return False
        try:
            future.get_loop().call_soon_threadsafe(safe_set_result, future, payload)
        except RuntimeError:
            logger.warning("%s could not wake a waiter (loop closed)", self._log_label)
            return False
        return True

    async def _sweep_forever(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._sweep_interval_seconds)
                self._sweep_once()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("%s sweep failed", self._log_label)

    def _sweep_once(self) -> None:
        cutoff = time.monotonic() - self._ttl_seconds
        with self._lock:
            orphan_keys = [key for key, record in self._items.items() if record.created_at < cutoff]
            orphans = [self._items.pop(key) for key in orphan_keys]
        for orphan in orphans:
            if orphan.future.done():
                continue
            self._on_orphan_swept(orphan)
            self._wake(orphan.future, self._swept_result(orphan))

    # Subclass hooks

    def _swept_result(self, record: TRecord) -> dict[str, Any]:
        """Result payload set on an orphaned record's future. Override."""
        return {"ok": False, "status": "swept"}

    def _on_orphan_swept(self, record: TRecord) -> None:
        """Optional side effect (logging) before an orphan is resolved."""
        return None


__all__ = [
    "FutureRendezvous",
    "RendezvousRecord",
    "safe_set_result",
]
