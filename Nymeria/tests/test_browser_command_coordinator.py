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


def test_register_stamps_the_turn_dispatch_ledger() -> None:
    """Registering a command IS the "this turn drove the browser" fact (#191):
    the pop answers True exactly once, then False until the next dispatch."""
    async def run() -> None:
        coord = _make_coord()
        assert coord.pop_turn_dispatched("u1", "t1") is False
        coord.register(
            command_id="bcmd_led_1",
            user_id="u1",
            thread_id="t1",
            command_type="act",
        )
        assert coord.pop_turn_dispatched("u1", "t1") is True
        assert coord.pop_turn_dispatched("u1", "t1") is False

    asyncio.run(run())


def test_release_publishes_only_for_a_turn_that_drove_the_browser(monkeypatch) -> None:
    """The turn-end release (#191) puts nothing on the wire for ordinary
    turns, and the event it does publish is scoped to the dispatching
    account and thread."""
    import nymeria.core.browser_command_coordinator as mod
    import nymeria.core.event_bus as event_bus

    published: list[dict] = []

    def fake_publish(**kwargs):
        published.append(kwargs)

    monkeypatch.setattr(event_bus, "publish_autonomous_event", fake_publish)

    async def run() -> None:
        coord = mod.get_browser_command_coordinator()
        # No dispatch this turn: nothing published.
        assert mod.release_browser_session("u1", "t1") is False
        assert published == []

        coord.register(
            command_id="bcmd_rel_1",
            user_id="u1",
            thread_id="t1",
            command_type="act",
        )
        assert mod.release_browser_session("u1", "t1") is True
        assert len(published) == 1
        evt = published[0]
        assert evt["event_type"] == "browser_session_release"
        assert evt["user_id"] == "u1"
        assert evt["thread_id"] == "t1"
        # Popped: the same turn end never publishes twice.
        assert mod.release_browser_session("u1", "t1") is False
        assert len(published) == 1
        # And another account's dispatch never releases for this one.
        coord.register(
            command_id="bcmd_rel_2",
            user_id="u2",
            thread_id="t9",
            command_type="act",
        )
        assert mod.release_browser_session("u1", "t1") is False
        assert len(published) == 1

    asyncio.run(run())


def test_release_swallows_a_publish_failure(monkeypatch) -> None:
    """A release must never raise into the turn tail: a failed publish is a
    False, and the extension's own safety-net linger covers the miss."""
    import nymeria.core.browser_command_coordinator as mod
    import nymeria.core.event_bus as event_bus

    def boom(**kwargs):
        raise RuntimeError("bus down")

    monkeypatch.setattr(event_bus, "publish_autonomous_event", boom)

    async def run() -> None:
        coord = mod.get_browser_command_coordinator()
        coord.register(
            command_id="bcmd_rel_3",
            user_id="u1",
            thread_id="t1",
            command_type="act",
        )
        assert mod.release_browser_session("u1", "t1") is False

    asyncio.run(run())


def test_agent_release_seam_reaches_the_coordinator(monkeypatch) -> None:
    """The seam's body actually calls the coordinator's release (the stub
    test below proves the fire points call the seam; this closes the chain)
    and swallows a release that raises, since the seam runs in the turn
    tail."""
    import nymeria.core.browser_command_coordinator as mod
    from nymeria.core.agent import NymeriaAgent

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mod, "release_browser_session", lambda user_id, thread_id: calls.append((user_id, thread_id))
    )
    NymeriaAgent._release_browser_session("u1", "t1")
    assert calls == [("u1", "t1")]

    def boom(user_id, thread_id):
        raise RuntimeError("release exploded")

    monkeypatch.setattr(mod, "release_browser_session", boom)
    NymeriaAgent._release_browser_session("u1", "t1")  # must not raise


def test_done_observe_fires_the_browser_release_on_both_paths() -> None:
    """Both DONE observe fire points (the exactly-once turn-end funnel) call
    the release seam, so a turn end releases the browser hold on the sync
    chat path and the async streaming path alike."""
    import asyncio as aio
    from types import SimpleNamespace

    from nymeria.core.agent import NymeriaAgent

    calls: list[tuple[str, str]] = []
    stub = SimpleNamespace(
        _release_browser_session=lambda user_id, thread_id: calls.append((user_id, thread_id)),
        # The hooks half is not under test; failing it proves the release
        # happens regardless (it sits outside that try).
        _hook_registry_for_turn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no hooks")),
    )

    NymeriaAgent._fire_done_observe_sync(
        stub,
        thread_id="t1",
        user_id="u1",
        is_autonomous=False,
        holder_kind=None,
        completed_normally=True,
        final_text="",
    )
    aio.run(
        NymeriaAgent._fire_done_observe(
            stub,
            thread_id="t2",
            user_id="u2",
            is_autonomous=True,
            holder_kind="ticker",
            completed_normally=False,
            final_text="",
        )
    )
    assert calls == [("u1", "t1"), ("u2", "t2")]


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
