"""Unit tests for UiPromptCoordinator: register / resolve / discard /
abort_thread / sweep semantics (mirrors test_browser_command_coordinator.py)."""

from __future__ import annotations

import asyncio

import pytest

from nymeria.core.ui_prompt_coordinator import (
    UiPromptCoordinator,
    new_prompt_id,
)


@pytest.fixture(autouse=True)
def reset_coordinator(monkeypatch):
    """Force a fresh coordinator per test so leftover state doesn't bleed."""
    import nymeria.core.ui_prompt_coordinator as mod
    monkeypatch.setattr(mod, "_coordinator", None)
    yield


def _make_coord() -> UiPromptCoordinator:
    return UiPromptCoordinator()


def test_new_prompt_id_format() -> None:
    pid = new_prompt_id()
    assert pid.startswith("uip_")
    assert len(pid) > len("uip_") + 10


def test_register_returns_pending_future() -> None:
    async def run() -> None:
        coord = _make_coord()
        future = coord.register(
            prompt_id="uip_test_1",
            user_id="u1",
            thread_id="t1",
            title="Pick options",
        )
        assert not future.done()
        record = coord.get("uip_test_1")
        assert record is not None
        assert record.user_id == "u1"
        assert record.thread_id == "t1"
        assert record.title == "Pick options"

    asyncio.run(run())


def test_resolve_wakes_future_and_removes_record() -> None:
    async def run() -> None:
        coord = _make_coord()
        future = coord.register(
            prompt_id="uip_test_2",
            user_id="u1",
            thread_id="t1",
        )

        async def waiter() -> dict:
            return await asyncio.wait_for(future, timeout=2)

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0)
        assert coord.resolve(
            "uip_test_2",
            {"ok": True, "status": "submitted", "values": {"color": "red"}},
        ) is True
        result = await task
        assert result["ok"] is True
        assert result["status"] == "submitted"
        assert result["values"] == {"color": "red"}
        assert coord.get("uip_test_2") is None

    asyncio.run(run())


def test_resolve_unknown_returns_false() -> None:
    coord = _make_coord()
    assert coord.resolve("uip_does_not_exist", {"ok": True, "status": "submitted"}) is False


def test_discard_removes_without_resolving() -> None:
    async def run() -> None:
        coord = _make_coord()
        future = coord.register(
            prompt_id="uip_test_3",
            user_id="u1",
            thread_id="t1",
        )
        coord.discard("uip_test_3")
        assert coord.get("uip_test_3") is None
        assert not future.done()  # discard does NOT resolve

    asyncio.run(run())


def test_abort_thread_resolves_only_matching() -> None:
    async def run() -> None:
        coord = _make_coord()
        future_t1 = coord.register(
            prompt_id="uip_a",
            user_id="u1",
            thread_id="thread_to_abort",
        )
        future_t2 = coord.register(
            prompt_id="uip_b",
            user_id="u1",
            thread_id="other_thread",
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
        # The other thread's prompt is still pending.
        assert not future_t2.done()
        assert coord.get("uip_b") is not None

    asyncio.run(run())


def test_abort_thread_with_no_prompts_returns_zero() -> None:
    coord = _make_coord()
    assert coord.abort_thread("unknown_thread") == 0


def test_pending_count_tracks_register_and_resolve() -> None:
    async def run() -> None:
        coord = _make_coord()
        assert coord.pending_count() == 0
        coord.register(prompt_id="uip_p1", user_id="u1", thread_id="t1")
        coord.register(prompt_id="uip_p2", user_id="u1", thread_id="t1")
        assert coord.pending_count() == 2
        coord.resolve("uip_p1", {"ok": True, "status": "submitted"})
        assert coord.pending_count() == 1
        coord.discard("uip_p2")
        assert coord.pending_count() == 0

    asyncio.run(run())


def test_sweep_resolves_orphaned_futures(monkeypatch) -> None:
    """Force the orphan TTL very short and verify the sweep loop fires."""
    import nymeria.core.ui_prompt_coordinator as mod
    monkeypatch.setattr(mod, "_ORPHAN_TTL_SECONDS", 0.05)
    monkeypatch.setattr(mod, "_SWEEP_INTERVAL_SECONDS", 0.02)

    async def run() -> None:
        coord = _make_coord()
        future = coord.register(
            prompt_id="uip_orphan",
            user_id="u1",
            thread_id="t1",
        )
        try:
            result = await asyncio.wait_for(future, timeout=2)
        except asyncio.TimeoutError:
            pytest.fail("sweep should have resolved orphaned future")
        assert result["ok"] is False
        assert result["status"] == "swept"

    asyncio.run(run())
