"""Unit tests for BrowserCommandCoordinator — register / resolve / discard /
abort_thread / sweep semantics."""

from __future__ import annotations

import asyncio

import pytest

from nymeria.core.browser_command_coordinator import (
    BrowserCommandCoordinator,
    new_command_id,
)


@pytest.fixture(autouse=True)
def reset_coordinator(monkeypatch):
    """Force a fresh coordinator per test so leftover state doesn't bleed."""
    import nymeria.core.browser_command_coordinator as mod
    monkeypatch.setattr(mod, "_coordinator", None)
    yield


def _make_coord() -> BrowserCommandCoordinator:
    return BrowserCommandCoordinator()


def test_new_command_id_format() -> None:
    cid = new_command_id()
    assert cid.startswith("bcmd_")
    assert len(cid) > len("bcmd_") + 10


def test_register_returns_pending_future() -> None:
    async def run() -> None:
        coord = _make_coord()
        future = coord.register(
            command_id="bcmd_test_1",
            user_id="u1",
            thread_id="t1",
            command_type="navigate",
            metadata={"url": "https://example.com"},
        )
        assert not future.done()
        record = coord.get("bcmd_test_1")
        assert record is not None
        assert record.user_id == "u1"
        assert record.thread_id == "t1"
        assert record.command_type == "navigate"

    asyncio.run(run())


def test_resolve_wakes_future_and_removes_record() -> None:
    async def run() -> None:
        coord = _make_coord()
        future = coord.register(
            command_id="bcmd_test_2",
            user_id="u1",
            thread_id="t1",
            command_type="snapshot",
        )

        async def waiter() -> dict:
            return await asyncio.wait_for(future, timeout=2)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        assert coord.resolve("bcmd_test_2", {"ok": True, "status": "success", "data": {}}) is True
        result = await task
        assert result["ok"] is True
        assert result["status"] == "success"
        assert coord.get("bcmd_test_2") is None

    asyncio.run(run())


def test_resolve_unknown_returns_false() -> None:
    coord = _make_coord()
    assert coord.resolve("bcmd_does_not_exist", {"ok": True, "status": "success"}) is False


def test_discard_removes_without_resolving() -> None:
    async def run() -> None:
        coord = _make_coord()
        future = coord.register(
            command_id="bcmd_test_3",
            user_id="u1",
            thread_id="t1",
            command_type="navigate",
        )
        coord.discard("bcmd_test_3")
        assert coord.get("bcmd_test_3") is None
        assert not future.done()  # discard does NOT resolve

    asyncio.run(run())


def test_abort_thread_resolves_only_matching() -> None:
    async def run() -> None:
        coord = _make_coord()
        future_t1 = coord.register(
            command_id="bcmd_a",
            user_id="u1",
            thread_id="thread_to_abort",
            command_type="navigate",
        )
        future_t2 = coord.register(
            command_id="bcmd_b",
            user_id="u1",
            thread_id="other_thread",
            command_type="snapshot",
        )

        async def wait_a() -> dict:
            return await asyncio.wait_for(future_t1, timeout=2)

        task = asyncio.create_task(wait_a())
        await asyncio.sleep(0)
        aborted = coord.abort_thread("thread_to_abort")
        assert aborted == 1
        result = await task
        assert result["ok"] is False
        assert result["status"] == "aborted"
        # The other thread's command is still pending.
        assert not future_t2.done()
        assert coord.get("bcmd_b") is not None

    asyncio.run(run())


def test_abort_thread_with_no_commands_returns_zero() -> None:
    coord = _make_coord()
    assert coord.abort_thread("unknown_thread") == 0


def test_pending_count_tracks_register_and_resolve() -> None:
    async def run() -> None:
        coord = _make_coord()
        assert coord.pending_count() == 0
        coord.register(
            command_id="bcmd_p1",
            user_id="u1",
            thread_id="t1",
            command_type="navigate",
        )
        coord.register(
            command_id="bcmd_p2",
            user_id="u1",
            thread_id="t1",
            command_type="snapshot",
        )
        assert coord.pending_count() == 2
        coord.resolve("bcmd_p1", {"ok": True, "status": "success"})
        assert coord.pending_count() == 1
        coord.discard("bcmd_p2")
        assert coord.pending_count() == 0

    asyncio.run(run())


def test_sweep_resolves_orphaned_futures(monkeypatch) -> None:
    """Force the orphan TTL very short and verify the sweep loop fires."""
    import nymeria.core.browser_command_coordinator as mod
    monkeypatch.setattr(mod, "ORPHAN_TTL_SECONDS", 0.05)
    monkeypatch.setattr(mod, "_SWEEP_INTERVAL_SECONDS", 0.02)

    async def run() -> None:
        coord = _make_coord()
        future = coord.register(
            command_id="bcmd_orphan",
            user_id="u1",
            thread_id="t1",
            command_type="navigate",
        )
        # Wait long enough for at least one sweep iteration to fire.
        try:
            result = await asyncio.wait_for(future, timeout=2)
        except asyncio.TimeoutError:
            pytest.fail("sweep should have resolved orphaned future")
        assert result["ok"] is False
        assert result["status"] == "swept"

    asyncio.run(run())
