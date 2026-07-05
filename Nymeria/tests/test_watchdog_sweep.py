"""Unit tests for the WatchdogSweep ticker sub-loop (the folded watchdog).

Ports the standalone WatchdogWorker's behavior pins onto the sweep and adds
the fold-specific seams:

- Nudge turns fire through the TurnExecutor seam with the same identity the
  standalone watchdog used (is_self_invoke, trigger_override="watchdog").
- External alerts dispatch in-process via
  notification_dispatch.send_external_notifications (both runtimes hold the
  master secrets key), never over HTTP.
- A nudge turn that fails outright (transport error, or an in-band error
  with no response) leaves the TODOs unmarked so the next cycle retries,
  capped at MAX_NUDGE_FAILURES.
- Nudge/timestamp dedupe state is pruned when TODOs disappear, so the
  long-lived process does not leak keys.
- The sweep publishes its own autonomous bookends (task_started deferred
  past queue-meta chunks, task_completed with the response content).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List

import httpx

from nymeria.core import watchdog_sweep as sweep_module
from nymeria.core.todo_manager import TodoItem, TodoStatus
from nymeria.core.watchdog_sweep import MAX_NUDGE_FAILURES, WatchdogSweep


def _settings(tmp_path):
    return SimpleNamespace(
        watchdog_enabled=True,
        watchdog_interval_minutes=5,
        todo_staleness_minutes=20,
        data_dir=tmp_path,
    )


def _stale_todo(todo_id="t1", thread_id="th1", minutes_old=60, **overrides) -> TodoItem:
    updated = datetime.now(timezone.utc) - timedelta(minutes=minutes_old)
    fields: Dict[str, Any] = {
        "id": todo_id,
        "thread_id": thread_id,
        "status": TodoStatus.PENDING,
        "task": "do the thing",
        "updated_at": updated,
    }
    fields.update(overrides)
    return TodoItem(**fields)


class FakeExecutor:
    """Duck-typed TurnExecutor covering what the sweep calls."""

    is_remote = False

    def __init__(self, *, chunks: List[Dict[str, Any]] | None = None):
        self.chunks = (
            chunks
            if chunks is not None
            else [{"type": "response", "content": "ok"}]
        )
        self.calls: List[Dict[str, Any]] = []

    async def astream(self, **kwargs):
        self.calls.append(kwargs)
        for chunk in self.chunks:
            yield chunk


class TransportFailureExecutor(FakeExecutor):
    async def astream(self, **kwargs):
        self.calls.append(kwargs)
        raise httpx.ConnectError("api unreachable")
        yield {}  # pragma: no cover - makes this an async generator


class MidStreamFailureExecutor(FakeExecutor):
    """Yields a response, then dies with a transport error (no error chunk)."""

    async def astream(self, **kwargs):
        self.calls.append(kwargs)
        yield {"type": "response", "content": "partial answer"}
        raise httpx.ReadError("connection dropped mid-stream")


class FakeTodoManager:
    """Just the two readers the sweep uses."""

    def __init__(self, todos_by_user: Dict[str, List[TodoItem]] | None = None):
        self.todos_by_user = todos_by_user or {}

    def get_all_users_with_todos(self) -> List[str]:
        return list(self.todos_by_user)

    def get_todos(self, user_id: str):
        return SimpleNamespace(items=list(self.todos_by_user.get(user_id, [])))


def _sweep(tmp_path, executor, *, todo_manager=None) -> WatchdogSweep:
    return WatchdogSweep(
        executor=executor,  # type: ignore[arg-type]
        settings=_settings(tmp_path),  # type: ignore[arg-type]
        todo_manager=todo_manager or FakeTodoManager(),  # type: ignore[arg-type]
        thread_config_manager=SimpleNamespace(),  # type: ignore[arg-type]
    )


def _quiet_publishing(monkeypatch, notify_results: List[str] | None = None,
                      notify_error: Exception | None = None):
    """Stub the sweep's module-level publish/notify seams; return recorders."""
    events: List[Dict[str, Any]] = []
    chunks: List[Dict[str, Any]] = []
    notifications: List[Dict[str, Any]] = []
    autonomous: List[Dict[str, Any]] = []

    def _publish_event(**kwargs):
        events.append(kwargs)

    def _publish_chunk(chunk, **kwargs):
        chunks.append({"chunk": chunk, **kwargs})

    def _send_external(message, settings, *, user_id="default", thread_id=""):
        if notify_error is not None:
            raise notify_error
        notifications.append(
            {"user_id": user_id, "message": message, "thread_id": thread_id}
        )
        return list(notify_results if notify_results is not None else ["tg-main"])

    def _create_autonomous(**kwargs):
        autonomous.append(kwargs)

    monkeypatch.setattr(sweep_module, "publish_autonomous_event", _publish_event)
    monkeypatch.setattr(sweep_module, "publish_agent_stream_chunk", _publish_chunk)
    monkeypatch.setattr(sweep_module, "send_external_notifications", _send_external)
    monkeypatch.setattr(sweep_module, "create_autonomous_notification", _create_autonomous)
    monkeypatch.setattr(sweep_module, "should_notify_autonomous", lambda *a, **k: False)
    return SimpleNamespace(
        events=events,
        chunks=chunks,
        notifications=notifications,
        autonomous=autonomous,
    )


# ── Nudge delivery + notification transport ───────────────────────────────


def test_successful_nudge_marks_and_notifies(tmp_path, monkeypatch):
    recorded = _quiet_publishing(monkeypatch)
    executor = FakeExecutor()
    sweep = _sweep(tmp_path, executor)
    todo = _stale_todo()

    sweep._nudge_thread("u1", "th1", [todo])

    assert ("u1", "t1") in sweep._nudged
    assert sweep._nudges_sent == 1
    # Turn identity matches the standalone watchdog's /chat call.
    call = executor.calls[0]
    assert call["_is_self_invoke"] is True
    assert call["_trigger_override"] == "watchdog"
    assert call["source"] == "watchdog"
    assert call["thread_id"] == "th1"
    assert call["user_id"] == "u1"
    # External alert dispatched in-process, scoped to the nudged user/thread.
    assert len(recorded.notifications) == 1
    sent = recorded.notifications[0]
    assert sent["user_id"] == "u1"
    assert sent["thread_id"] == "th1"
    assert "[Nymeria Watchdog]" in sent["message"]
    assert sweep._notifications_sent == 1
    assert sweep._notification_failures == 0


def test_notification_failure_is_isolated_and_counted(tmp_path, monkeypatch):
    _quiet_publishing(monkeypatch, notify_error=RuntimeError("boom"))
    sweep = _sweep(tmp_path, FakeExecutor())
    todo = _stale_todo()

    # Must not raise: the nudge reached the agent even if the alert failed.
    sweep._nudge_thread("u1", "th1", [todo])

    assert ("u1", "t1") in sweep._nudged
    assert sweep._notification_failures == 1
    assert sweep._notifications_sent == 0


# ── Autonomous event publishing (the fold's own bookends) ─────────────────


def test_nudge_publishes_bookends_with_watchdog_task_id(tmp_path, monkeypatch):
    recorded = _quiet_publishing(monkeypatch)
    executor = FakeExecutor(
        chunks=[
            {"type": "queued", "position": 1},  # queue-meta: must not start
            {"type": "response", "content": "on it"},
        ]
    )
    sweep = _sweep(tmp_path, executor)

    sweep._nudge_thread("u1", "th1", [_stale_todo()])

    started = [e for e in recorded.events if e["event_type"] == "task_started"]
    completed = [e for e in recorded.events if e["event_type"] == "task_completed"]
    assert len(started) == 1 and len(completed) == 1
    assert started[0]["task_id"] == "watchdog-th1"
    assert started[0]["data"]["source"] == "watchdog"
    assert completed[0]["task_id"] == "watchdog-th1"
    assert completed[0]["data"]["content"] == "on it"
    assert "error" not in completed[0]["data"]
    # Both chunks were mirrored to the autonomous stream.
    assert len(recorded.chunks) == 2
    assert all(c["task_id"] == "watchdog-th1" for c in recorded.chunks)


def test_queue_meta_only_stream_never_publishes_task_started(tmp_path, monkeypatch):
    recorded = _quiet_publishing(monkeypatch)
    executor = FakeExecutor(chunks=[{"type": "queued", "position": 1}])
    sweep = _sweep(tmp_path, executor)

    sweep._nudge_thread("u1", "th1", [_stale_todo()])

    assert [e["event_type"] for e in recorded.events] == []


# ── Retry semantics ────────────────────────────────────────────────────────


def test_transport_failure_leaves_todos_unmarked_for_retry(tmp_path, monkeypatch):
    recorded = _quiet_publishing(monkeypatch)
    sweep = _sweep(tmp_path, TransportFailureExecutor())
    todo = _stale_todo()

    sweep._nudge_thread("u1", "th1", [todo])

    assert sweep._nudged == {}
    assert recorded.notifications == []
    assert sweep._nudges_sent == 0
    # The in-flight guard must release so the next cycle can retry.
    assert sweep._in_flight == set()


def test_error_turn_without_response_leaves_todos_unmarked(tmp_path, monkeypatch):
    recorded = _quiet_publishing(monkeypatch)
    sweep = _sweep(
        tmp_path,
        FakeExecutor(chunks=[{"type": "error", "content": "llm exploded"}]),
    )
    todo = _stale_todo()

    sweep._nudge_thread("u1", "th1", [todo])

    assert sweep._nudged == {}
    assert recorded.notifications == []


def test_repeated_failures_hit_the_cap_and_suppress(tmp_path, monkeypatch):
    # A deterministically failing thread must not retry (and burn an
    # errored LLM turn) every cycle forever: after MAX_NUDGE_FAILURES
    # consecutive failures the TODO is marked nudged anyway, staying
    # quiet until its updated_at advances.
    _quiet_publishing(monkeypatch)
    sweep = _sweep(tmp_path, TransportFailureExecutor())
    todo = _stale_todo()

    for _ in range(MAX_NUDGE_FAILURES - 1):
        sweep._nudge_thread("u1", "th1", [todo])
        assert ("u1", "t1") not in sweep._nudged  # still retrying

    sweep._nudge_thread("u1", "th1", [todo])

    assert ("u1", "t1") in sweep._nudged
    assert sweep._nudge_failures == {}
    assert sweep._nudges_sent == 0  # no successful nudge was counted


def test_success_clears_failure_count(tmp_path, monkeypatch):
    _quiet_publishing(monkeypatch)
    sweep = _sweep(tmp_path, FakeExecutor())
    todo = _stale_todo()
    sweep._nudge_failures[("u1", "t1")] = MAX_NUDGE_FAILURES - 1

    sweep._nudge_thread("u1", "th1", [todo])

    assert ("u1", "t1") in sweep._nudged
    assert sweep._nudge_failures == {}


def test_error_after_response_still_marks_nudged(tmp_path, monkeypatch):
    # The agent saw the nudge and answered; a later in-band error must not
    # cause a duplicate nudge next cycle, and the completion keeps the
    # partial response as normal content (parity with the API's finally
    # block, which published accumulated content for in-band error chunks).
    recorded = _quiet_publishing(monkeypatch)
    sweep = _sweep(
        tmp_path,
        FakeExecutor(
            chunks=[
                {"type": "response", "content": "on it"},
                {"type": "error", "content": "post-turn hiccup"},
            ]
        ),
    )
    todo = _stale_todo()

    sweep._nudge_thread("u1", "th1", [todo])

    assert ("u1", "t1") in sweep._nudged
    completed = [
        e for e in recorded.events if e["event_type"] == "task_completed"
    ]
    assert len(completed) == 1
    assert completed[0]["data"]["content"] == "on it"
    assert "error" not in completed[0]["data"]


def test_transport_failure_after_response_publishes_error_bookend(
    tmp_path, monkeypatch
):
    # A transport failure after a response still counts as delivered (marks
    # nudged, sends the external alert), but the completion bookend carries
    # the error flag and text, matching the API's exception handler.
    recorded = _quiet_publishing(monkeypatch)
    sweep = _sweep(tmp_path, MidStreamFailureExecutor())
    todo = _stale_todo()

    sweep._nudge_thread("u1", "th1", [todo])

    assert ("u1", "t1") in sweep._nudged
    assert len(recorded.notifications) == 1
    completed = [
        e for e in recorded.events if e["event_type"] == "task_completed"
    ]
    assert len(completed) == 1
    assert completed[0]["data"]["error"] is True
    assert "connection dropped mid-stream" in completed[0]["data"]["content"]


# ── Staleness rules ────────────────────────────────────────────────────────


def test_staleness_filter_matches_standalone_rules(tmp_path, monkeypatch):
    _quiet_publishing(monkeypatch)
    sweep = _sweep(tmp_path, FakeExecutor())
    future = datetime.now(timezone.utc) + timedelta(hours=1)

    assert sweep._is_stale(_stale_todo()) is True
    assert sweep._is_stale(_stale_todo(minutes_old=5)) is False  # fresh
    assert sweep._is_stale(_stale_todo(status=TodoStatus.DONE)) is False
    assert sweep._is_stale(_stale_todo(recurrence="1h")) is False
    assert sweep._is_stale(_stale_todo(scheduled_for=future)) is False


# ── Cycle behavior: pruning, dedupe, kill switches ─────────────────────────


def test_check_user_prunes_state_for_vanished_todos(tmp_path, monkeypatch):
    _quiet_publishing(monkeypatch)
    fresh = _stale_todo(todo_id="live", minutes_old=0)  # recent => not stale
    executor = FakeExecutor()
    sweep = _sweep(
        tmp_path, executor, todo_manager=FakeTodoManager({"u1": [fresh]})
    )

    now = datetime.now(timezone.utc)
    sweep._nudged[("u1", "dead")] = now.timestamp()
    sweep._timestamps[("u1", "dead")] = now
    # Another user's state must survive a u1 listing untouched.
    sweep._nudged[("u2", "other")] = now.timestamp()

    sweep._check_user("u1", None)

    assert ("u1", "dead") not in sweep._nudged
    assert ("u1", "dead") not in sweep._timestamps
    assert ("u2", "other") in sweep._nudged
    # The fresh TODO is not stale, so no nudge fired.
    assert executor.calls == []


def test_run_cycle_nudges_once_then_dedupes(tmp_path, monkeypatch):
    _quiet_publishing(monkeypatch)
    executor = FakeExecutor()
    sweep = _sweep(
        tmp_path,
        executor,
        todo_manager=FakeTodoManager({"u1": [_stale_todo()]}),
    )

    sweep.run_cycle(None)
    assert len(executor.calls) == 1

    # Same unchanged TODO: the nudged marker suppresses a second turn.
    sweep.run_cycle(None)
    assert len(executor.calls) == 1


def test_run_cycle_submits_nudges_to_worker_pool(tmp_path, monkeypatch):
    # The production path: detection submits each nudge to the autonomous
    # worker pool, so nudge state is mutated from pool threads (guarded by
    # the sweep's state lock).
    _quiet_publishing(monkeypatch)
    executor = FakeExecutor()
    sweep = _sweep(
        tmp_path,
        executor,
        todo_manager=FakeTodoManager(
            {"u1": [_stale_todo()], "u2": [_stale_todo(todo_id="t2", thread_id="th2")]}
        ),
    )

    pool = ThreadPoolExecutor(max_workers=2)
    try:
        sweep.run_cycle(pool)
    finally:
        pool.shutdown(wait=True)

    assert len(executor.calls) == 2
    assert ("u1", "t1") in sweep._nudged
    assert ("u2", "t2") in sweep._nudged
    assert sweep._in_flight == set()


def test_queued_duplicate_nudge_skips_already_marked_todos(tmp_path, monkeypatch):
    # If the pool stays saturated past one interval, detection can queue a
    # second nudge for the same TODOs before the first executes; the nudge
    # re-checks the nudged markers at execution time and skips.
    _quiet_publishing(monkeypatch)
    executor = FakeExecutor()
    sweep = _sweep(tmp_path, executor)
    todo = _stale_todo()

    sweep._nudge_thread("u1", "th1", [todo])
    assert len(executor.calls) == 1

    sweep._nudge_thread("u1", "th1", [todo])  # the stale queued duplicate
    assert len(executor.calls) == 1


def test_updated_at_advance_rearms_the_nudge(tmp_path, monkeypatch):
    _quiet_publishing(monkeypatch)
    executor = FakeExecutor()
    manager = FakeTodoManager({"u1": [_stale_todo(minutes_old=60)]})
    sweep = _sweep(tmp_path, executor, todo_manager=manager)

    sweep.run_cycle(None)
    assert len(executor.calls) == 1

    # Activity on the TODO (still stale, but newer updated_at) re-arms it.
    manager.todos_by_user["u1"] = [_stale_todo(minutes_old=30)]
    sweep.run_cycle(None)
    assert len(executor.calls) == 2


def test_env_kill_switch_skips_cycle(tmp_path, monkeypatch):
    _quiet_publishing(monkeypatch)
    executor = FakeExecutor()
    sweep = _sweep(
        tmp_path,
        executor,
        todo_manager=FakeTodoManager({"u1": [_stale_todo()]}),
    )
    monkeypatch.setenv("NYMERIA_WATCHDOG_DISABLED", "1")

    sweep.run_cycle(None)

    assert executor.calls == []


def test_flag_file_kill_switch_skips_cycle(tmp_path, monkeypatch):
    _quiet_publishing(monkeypatch)
    monkeypatch.delenv("NYMERIA_WATCHDOG_DISABLED", raising=False)
    executor = FakeExecutor()
    sweep = _sweep(
        tmp_path,
        executor,
        todo_manager=FakeTodoManager({"u1": [_stale_todo()]}),
    )
    flags = tmp_path / "flags"
    flags.mkdir()
    (flags / "watchdog-off").touch()

    sweep.run_cycle(None)

    assert executor.calls == []


# ── Heartbeat stats ────────────────────────────────────────────────────────


def test_stats_surface_delivery_counters(tmp_path, monkeypatch):
    _quiet_publishing(monkeypatch)
    monkeypatch.delenv("NYMERIA_WATCHDOG_DISABLED", raising=False)
    sweep = _sweep(tmp_path, FakeExecutor())

    sweep._nudge_thread("u1", "th1", [_stale_todo()])

    stats = sweep.stats()
    assert stats["enabled"] is True
    assert stats["kill_switch_active"] is False
    assert stats["nudges_sent"] == 1
    assert stats["notifications_sent"] == 1
    assert stats["notification_failures"] == 0
    assert stats["interval_minutes"] == 5
    assert stats["staleness_minutes"] == 20

    # A live kill switch surfaces without flipping the enabled flag.
    monkeypatch.setenv("NYMERIA_WATCHDOG_DISABLED", "1")
    stats = sweep.stats()
    assert stats["enabled"] is True
    assert stats["kill_switch_active"] is True
