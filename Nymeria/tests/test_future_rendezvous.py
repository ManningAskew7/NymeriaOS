"""Unit tests for the shared FutureRendezvous base: the registry + sweep
machinery behind AuthPromptCoordinator and BrowserCommandCoordinator."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from nymeria.core.future_rendezvous import FutureRendezvous, safe_set_result


@dataclass
class _StubRecord:
    record_id: str
    user_id: str
    future: asyncio.Future
    created_at: float = field(default_factory=time.monotonic)


class _StubRendezvous(FutureRendezvous[_StubRecord]):
    """Minimal concrete subclass for exercising the base contract."""

    def __init__(self, *, ttl_seconds: float = 600.0, sweep_interval_seconds: float = 60.0) -> None:
        super().__init__(
            ttl_seconds=ttl_seconds,
            sweep_interval_seconds=sweep_interval_seconds,
            log_label="stub_rendezvous",
        )
        self.swept_log: list[str] = []

    def register(self, record_id: str, user_id: str) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._add(record_id, _StubRecord(record_id=record_id, user_id=user_id, future=future))
        return future

    def resolve(self, record_id: str, result: dict[str, Any]) -> bool:
        return self._resolve(record_id, lambda record: result)

    def abort_user(self, user_id: str) -> int:
        matched = self._drain_matching(lambda r: r.user_id == user_id)
        count = 0
        for record in matched:
            if record.future.done():
                continue
            record.future.get_loop().call_soon_threadsafe(
                safe_set_result, record.future, {"status": "aborted"}
            )
            count += 1
        return count

    def _swept_result(self, record: _StubRecord) -> dict[str, Any]:
        return {"ok": False, "status": "swept", "id": record.record_id}

    def _on_orphan_swept(self, record: _StubRecord) -> None:
        self.swept_log.append(record.record_id)


def test_safe_set_result_is_idempotent() -> None:
    async def run() -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        safe_set_result(future, {"a": 1})
        assert future.done()
        # A second call must not raise InvalidStateError.
        safe_set_result(future, {"a": 2})
        assert future.result() == {"a": 1}

    asyncio.run(run())


def test_register_get_and_pending_count() -> None:
    async def run() -> None:
        coord = _StubRendezvous()
        assert coord.pending_count() == 0
        coord.register("r1", "u1")
        record = coord.get("r1")
        assert record is not None and record.user_id == "u1"
        assert coord.pending_count() == 1
        assert coord.get("missing") is None

    asyncio.run(run())


def test_resolve_wakes_future_and_removes_record() -> None:
    async def run() -> None:
        coord = _StubRendezvous()
        future = coord.register("r1", "u1")

        async def waiter() -> dict:
            return await asyncio.wait_for(future, timeout=2)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        assert coord.resolve("r1", {"ok": True}) is True
        assert (await task) == {"ok": True}
        assert coord.get("r1") is None


    asyncio.run(run())


def test_resolve_unknown_and_already_done_return_false() -> None:
    async def run() -> None:
        coord = _StubRendezvous()
        assert coord.resolve("nope", {"ok": True}) is False
        future = coord.register("r1", "u1")
        # Mark the future done out from under the coordinator.
        future.set_result({"done": "externally"})
        assert coord.resolve("r1", {"ok": True}) is False

    asyncio.run(run())


def test_discard_removes_without_resolving() -> None:
    async def run() -> None:
        coord = _StubRendezvous()
        future = coord.register("r1", "u1")
        coord.discard("r1")
        assert coord.get("r1") is None
        assert not future.done()

    asyncio.run(run())


def test_drain_matching_pops_only_matching() -> None:
    async def run() -> None:
        coord = _StubRendezvous()
        f1 = coord.register("r1", "victim")
        f2 = coord.register("r2", "victim")
        f3 = coord.register("r3", "bystander")
        aborted = coord.abort_user("victim")
        assert aborted == 2
        await asyncio.sleep(0)  # let the threadsafe result callbacks run
        assert f1.result()["status"] == "aborted"
        assert f2.result()["status"] == "aborted"
        assert not f3.done()
        assert coord.pending_count() == 1
        assert coord.get("r3") is not None

    asyncio.run(run())


def test_sweep_resolves_orphans_via_hooks() -> None:
    async def run() -> None:
        coord = _StubRendezvous(ttl_seconds=0.05, sweep_interval_seconds=0.02)
        future = coord.register("r1", "u1")
        try:
            result = await asyncio.wait_for(future, timeout=2)
        except asyncio.TimeoutError:
            pytest.fail("sweep should have resolved the orphaned future")
        assert result == {"ok": False, "status": "swept", "id": "r1"}
        assert coord.swept_log == ["r1"]
        assert coord.pending_count() == 0

    asyncio.run(run())
