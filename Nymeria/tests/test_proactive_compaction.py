"""Tests for opt-in proactive idle compaction (backlog #28 slice B).

Covers the CompactionManager pieces (turn-end stamps, per-thread config
resolution, occupancy floor, the sweep decision matrix) and the API-side
lifecycle registration. The sweep is deliberately conservative: every drop
path leaves the thread to a later turn end, which re-stamps it.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from nymeria.core import agent_compaction as ac
from nymeria.core.agent_compaction import CompactionManager
from nymeria.core.thread_config import ThreadLLMConfig
from nymeria.core.thread_lock_manager import ThreadLockManager


def _agent(
    *,
    context_management: str = "auto_compact",
    enabled: bool = True,
    idle_seconds: int = 60,
    min_pct: int = 85,
    thread_llm=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            context_management=context_management,
            compact_proactive_enabled=enabled,
            compact_proactive_idle_seconds=idle_seconds,
            compact_proactive_min_pct=min_pct,
        ),
        thread_config_manager=SimpleNamespace(
            get_config=lambda tid: (
                SimpleNamespace(llm_config=thread_llm)
                if thread_llm is not None
                else None
            )
        ),
        _thread_locks=ThreadLockManager(),
    )


def _mgr(**kwargs) -> CompactionManager:
    return CompactionManager(_agent(**kwargs))


def _stamp_idle(mgr: CompactionManager, thread_id: str = "t1", user_id: str = "u1"):
    """Plant a stamp old enough to pass any idle threshold in these tests."""
    mgr._turn_end_stamps[thread_id] = (user_id, time.monotonic() - 100_000)


def _occupancy(mgr: CompactionManager, ready: bool | list[bool]):
    """Script the occupancy probe (a list is consumed call by call)."""
    seq = list(ready) if isinstance(ready, list) else None

    def fake(thread_id, min_pct):
        if seq is not None:
            return seq.pop(0)
        return ready

    mgr._proactive_occupancy_ready = fake


def _capture_compact(mgr: CompactionManager, *, success: bool = True):
    calls: list[tuple[str, str]] = []

    async def fake_compact_now(thread_id, user_id):
        calls.append((thread_id, user_id))
        return {"success": success, "messages_removed": 12, "reason": "too few"}

    mgr.compact_now = fake_compact_now
    return calls


def _capture_publish(mgr: CompactionManager):
    published: list[tuple[str, str, dict]] = []
    mgr._publish_proactive_compacted = (
        lambda tid, uid, result: published.append((tid, uid, result))
    )
    return published


# --- turn-end stamps --------------------------------------------------------


def test_note_turn_end_stamps_and_replaces():
    mgr = _mgr()
    mgr.note_turn_end("t1", "u1")
    first = mgr._turn_end_stamps["t1"]
    assert first[0] == "u1"
    mgr.note_turn_end("t1", "u2")
    second = mgr._turn_end_stamps["t1"]
    assert second[0] == "u2"
    assert second[1] >= first[1]


def test_note_turn_end_ignores_empty_thread_id():
    mgr = _mgr()
    mgr.note_turn_end("", "u1")
    assert mgr._turn_end_stamps == {}


def test_note_turn_end_defaults_blank_user():
    mgr = _mgr()
    mgr.note_turn_end("t1", "")
    assert mgr._turn_end_stamps["t1"][0] == "default"


def test_clear_stamp_only_when_unchanged():
    mgr = _mgr()
    mgr.note_turn_end("t1", "u1")
    _, ended_at = mgr._turn_end_stamps["t1"]
    # A newer turn end replaces the stamp: the old handle must not clear it.
    mgr._turn_end_stamps["t1"] = ("u1", ended_at + 1.0)
    assert mgr._clear_stamp("t1", ended_at) is False
    assert "t1" in mgr._turn_end_stamps
    assert mgr._clear_stamp("t1", ended_at + 1.0) is True
    assert "t1" not in mgr._turn_end_stamps


# --- per-thread config resolution -------------------------------------------


def test_resolve_proactive_config_global_defaults():
    mgr = _mgr(enabled=True, idle_seconds=300, min_pct=70)
    assert mgr._resolve_proactive_config("t1") == (True, 300, 70)


def test_resolve_proactive_config_thread_overrides_win():
    llm = SimpleNamespace(
        compact_proactive_enabled=False,
        compact_proactive_idle_seconds=45,
        compact_proactive_min_pct=95,
    )
    mgr = _mgr(enabled=True, idle_seconds=300, min_pct=70, thread_llm=llm)
    assert mgr._resolve_proactive_config("t1") == (False, 45, 95)


def test_resolve_proactive_config_none_inherits_per_field():
    llm = SimpleNamespace(
        compact_proactive_enabled=True,
        compact_proactive_idle_seconds=None,
        compact_proactive_min_pct=None,
    )
    mgr = _mgr(enabled=False, idle_seconds=300, min_pct=70, thread_llm=llm)
    assert mgr._resolve_proactive_config("t1") == (True, 300, 70)


def test_thread_llm_config_proactive_bounds():
    ThreadLLMConfig(compact_proactive_idle_seconds=30, compact_proactive_min_pct=10)
    ThreadLLMConfig(compact_proactive_idle_seconds=3600, compact_proactive_min_pct=100)
    with pytest.raises(ValidationError):
        ThreadLLMConfig(compact_proactive_idle_seconds=10)
    with pytest.raises(ValidationError):
        ThreadLLMConfig(compact_proactive_min_pct=5)


# --- occupancy floor ---------------------------------------------------------


def test_occupancy_ready_at_and_above_floor(monkeypatch):
    mgr = _mgr()
    stats = {"context_tokens": 85_000, "compact_trigger_tokens": 100_000}
    monkeypatch.setattr(ac, "hook_context_stats", lambda agent, tid: stats)
    assert mgr._proactive_occupancy_ready("t1", 85) is True
    assert mgr._proactive_occupancy_ready("t1", 86) is False


def test_occupancy_not_ready_when_stats_unknown(monkeypatch):
    mgr = _mgr()
    monkeypatch.setattr(
        ac,
        "hook_context_stats",
        lambda agent, tid: {"context_tokens": None, "compact_trigger_tokens": None},
    )
    assert mgr._proactive_occupancy_ready("t1", 50) is False


# --- sweep decision matrix ---------------------------------------------------


def test_sweep_noop_when_not_auto_compact():
    mgr = _mgr(context_management="sliding_window")
    _stamp_idle(mgr)
    calls = _capture_compact(mgr)
    assert asyncio.run(mgr.run_proactive_sweep()) == 0
    assert calls == []
    assert "t1" in mgr._turn_end_stamps  # untouched, mode may change later


def test_sweep_disabled_clears_stamp_without_compacting():
    mgr = _mgr(enabled=False)
    _stamp_idle(mgr)
    calls = _capture_compact(mgr)
    assert asyncio.run(mgr.run_proactive_sweep()) == 0
    assert calls == []
    assert "t1" not in mgr._turn_end_stamps


def test_sweep_keeps_stamp_until_idle_elapsed():
    mgr = _mgr(idle_seconds=3600)
    mgr.note_turn_end("t1", "u1")  # fresh stamp: not idle long enough
    calls = _capture_compact(mgr)
    assert asyncio.run(mgr.run_proactive_sweep()) == 0
    assert calls == []
    assert "t1" in mgr._turn_end_stamps


def test_sweep_skips_busy_thread():
    mgr = _mgr()
    _stamp_idle(mgr)
    calls = _capture_compact(mgr)
    lock = mgr._agent._thread_locks.get_lock("t1")
    lock.acquire()
    try:
        assert asyncio.run(mgr.run_proactive_sweep()) == 0
    finally:
        lock.release()
    assert calls == []
    # The stamp was consumed: the busy turn re-stamps at its own end.
    assert "t1" not in mgr._turn_end_stamps


def test_sweep_skips_below_occupancy_floor():
    mgr = _mgr()
    _stamp_idle(mgr)
    calls = _capture_compact(mgr)
    _occupancy(mgr, False)
    assert asyncio.run(mgr.run_proactive_sweep()) == 0
    assert calls == []
    assert "t1" not in mgr._turn_end_stamps


def test_sweep_defers_on_lock_race():
    """Busy probe says idle but the lock is taken before we acquire it."""
    mgr = _mgr()
    _stamp_idle(mgr)
    calls = _capture_compact(mgr)
    _occupancy(mgr, True)
    locks = mgr._agent._thread_locks
    locks.get_lock("t1").acquire()  # a turn holds the lock...
    locks.is_thread_busy = lambda tid: False  # ...but the probe raced ahead
    try:
        assert asyncio.run(mgr.run_proactive_sweep()) == 0
    finally:
        locks.get_lock("t1").release()
    assert calls == []


def test_sweep_compacts_idle_thread():
    mgr = _mgr()
    _stamp_idle(mgr, "t1", "u1")
    calls = _capture_compact(mgr)
    published = _capture_publish(mgr)
    _occupancy(mgr, True)

    assert asyncio.run(mgr.run_proactive_sweep()) == 1

    assert calls == [("t1", "u1")]
    assert published and published[0][0] == "t1" and published[0][1] == "u1"
    assert "t1" not in mgr._turn_end_stamps
    # Lock released and holder metadata cleared after the pass.
    locks = mgr._agent._thread_locks
    assert locks.is_thread_busy("t1") is False
    assert locks.get_lock_info("t1") is None


def test_sweep_rechecks_occupancy_under_lock():
    """Ready before the lock, no longer ready under it: do not compact."""
    mgr = _mgr()
    _stamp_idle(mgr)
    calls = _capture_compact(mgr)
    _occupancy(mgr, [True, False])
    assert asyncio.run(mgr.run_proactive_sweep()) == 0
    assert calls == []
    assert mgr._agent._thread_locks.is_thread_busy("t1") is False


def test_sweep_unsuccessful_compaction_not_published():
    mgr = _mgr()
    _stamp_idle(mgr)
    _capture_compact(mgr, success=False)
    published = _capture_publish(mgr)
    _occupancy(mgr, True)
    assert asyncio.run(mgr.run_proactive_sweep()) == 0
    assert published == []


def test_sweep_one_failure_does_not_kill_the_pass():
    mgr = _mgr()
    _stamp_idle(mgr, "t-bad", "u1")
    _stamp_idle(mgr, "t-good", "u1")
    _occupancy(mgr, True)
    _capture_publish(mgr)
    compacted: list[str] = []

    async def fake_compact_now(thread_id, user_id):
        if thread_id == "t-bad":
            raise RuntimeError("boom")
        compacted.append(thread_id)
        return {"success": True, "messages_removed": 3}

    mgr.compact_now = fake_compact_now

    assert asyncio.run(mgr.run_proactive_sweep()) == 1
    assert compacted == ["t-good"]
    locks = mgr._agent._thread_locks
    assert locks.is_thread_busy("t-bad") is False  # lock released on failure
    assert locks.is_thread_busy("t-good") is False


def test_sweep_broken_thread_config_skips_candidate():
    mgr = _mgr()
    _stamp_idle(mgr)
    calls = _capture_compact(mgr)

    def broken(tid):
        raise RuntimeError("corrupt config")

    mgr._agent.thread_config_manager = SimpleNamespace(get_config=broken)
    assert asyncio.run(mgr.run_proactive_sweep()) == 0
    assert calls == []


# --- event publish -----------------------------------------------------------


def test_publish_proactive_compacted_payload(monkeypatch):
    seen = {}

    def fake_publish(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(
        "nymeria.core.event_bus.publish_autonomous_event", fake_publish
    )
    CompactionManager._publish_proactive_compacted(
        "t1", "u1", {"success": True, "messages_removed": 7}
    )
    assert seen["event_type"] == "compacted"
    assert seen["thread_id"] == "t1"
    assert seen["user_id"] == "u1"
    assert seen["data"] == {
        "proactive": True,
        "auto_resumed": False,
        "messages_removed": 7,
    }


def test_publish_proactive_compacted_swallows_errors(monkeypatch):
    def exploding_publish(**kwargs):
        raise RuntimeError("bus down")

    monkeypatch.setattr(
        "nymeria.core.event_bus.publish_autonomous_event", exploding_publish
    )
    # Best-effort: a publish failure must never propagate to the sweep.
    CompactionManager._publish_proactive_compacted("t1", "u1", {"success": True})


# --- API lifecycle registration ----------------------------------------------


def test_register_proactive_lifecycle_wires_and_stops_cleanly():
    from fastapi import FastAPI

    from nymeria.triggers.api import _register_proactive_compaction_lifecycle

    app = FastAPI()
    _register_proactive_compaction_lifecycle(
        app, agent_getter=lambda: SimpleNamespace()
    )

    startup = [h for h in app.router.on_startup if asyncio.iscoroutinefunction(h)]
    shutdown = [h for h in app.router.on_shutdown if asyncio.iscoroutinefunction(h)]
    assert startup and shutdown

    async def run() -> None:
        await startup[0]()
        assert hasattr(app.state, "proactive_compaction_task")
        assert hasattr(app.state, "proactive_compaction_stop")
        app.state.proactive_compaction_stop.set()
        await shutdown[0]()

    asyncio.run(run())
