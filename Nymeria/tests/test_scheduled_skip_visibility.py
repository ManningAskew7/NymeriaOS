"""#262: a scheduled occurrence that never runs must say so.

Two silent-skip holes, both measured in the Effexor-miss diagnosis
(tmp/keep/effexor-miss-diagnosis.md): the ticker's re-arm skip-forward
collapses every slot that already passed (downtime, a run longer than its
interval, a late release) with no signal, and a leftover execution marker
refuses every poll of a due TODO until the stale sweep reclaims it, with one
INFO line per poll. Both now write a ``task_skipped`` activity row and alert
the owner once per episode; a failed marker release is retried by the poll
loop instead of holding the TODO for the stale window.
"""

from __future__ import annotations

import sqlite3
import time
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Callable, Optional, cast

import pytest

from nymeria.core import ticker as ticker_module
from nymeria.core import time_utils as time_utils_module
from nymeria.core import todo_constants as todo_constants_module
from nymeria.core.activity_log import ActivityType
from nymeria.core.ticker import Ticker
from nymeria.core.todo_constants import count_skipped_occurrences
from nymeria.core.todo_manager import TodoManager
from nymeria.tools import todo as todo_tools
from nymeria.core.todo_schedule_db import ScheduledTodoEntry, TodoScheduleDB
from nymeria.core.turn_executor import LocalAgentExecutor
from nymeria.core.user_profile import UserProfileManager

USER = "owner"


# ------------------------------------------------------------------
# Pure helper: count_skipped_occurrences
# ------------------------------------------------------------------


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_fixed_interval_counts_the_slots_strictly_between():
    fired = _utc(2026, 9, 1, 3, 0)
    assert count_skipped_occurrences("1h", fired, fired + timedelta(hours=1)) == 0
    assert count_skipped_occurrences("1h", fired, fired + timedelta(hours=4)) == 3
    assert count_skipped_occurrences("1d", fired, fired + timedelta(days=2)) == 1
    # Legacy alias resolves like its duration.
    assert count_skipped_occurrences("daily", fired, fired + timedelta(days=3)) == 2
    # An off-cadence upper bound (the ticker caps at the armed slot): the
    # slots strictly before it count, a slot equal to it does not.
    assert count_skipped_occurrences("1h", fired, fired + timedelta(hours=2, minutes=30)) == 2
    assert count_skipped_occurrences("1h", fired, fired + timedelta(minutes=35)) == 0
    assert count_skipped_occurrences("5m", fired, fired + timedelta(minutes=10, seconds=10)) == 2


def test_no_skip_for_a_non_advancing_or_unparseable_recurrence():
    fired = _utc(2026, 9, 1, 3, 0)
    assert count_skipped_occurrences("1h", fired, fired) == 0
    assert count_skipped_occurrences("1h", fired, fired - timedelta(hours=2)) == 0
    assert count_skipped_occurrences("sometimes", fired, fired + timedelta(days=9)) == 0


def test_calendar_months_follow_the_origin_clamp():
    # Month-end series: Jan 31 -> Feb 28 -> Mar 31 -> Apr 30. A re-arm that
    # lands Apr 30 from the Jan 31 run jumped over Feb 28 and Mar 31.
    origin = _utc(2026, 1, 31, 9, 0)
    assert (
        count_skipped_occurrences(
            "1mo", origin, _utc(2026, 4, 30, 9, 0), origin=origin
        )
        == 2
    )
    # No stored origin: the fired slot is adopted, as the re-arm does.
    assert count_skipped_occurrences("1mo", origin, _utc(2026, 2, 28, 9, 0)) == 0
    # A CLAMPED fired slot must step from the stored origin, not from itself:
    # the Feb 28 run re-armed to May 31 skipped Mar 31 and Apr 30. Stepping
    # from Feb 28 would count Mar 28, Apr 28 and May 28 instead.
    assert (
        count_skipped_occurrences(
            "1mo", _utc(2026, 2, 28, 9, 0), _utc(2026, 5, 31, 9, 0), origin=origin
        )
        == 2
    )


# ------------------------------------------------------------------
# Ticker harness (pattern: test_scheduled_failure_policy.py)
# ------------------------------------------------------------------


class FakeThreadConfigManager:
    def get_config(self, thread_id: str):
        return None


class FakeSettings:
    data_dir = Path("/tmp")
    context_management = "none"
    sliding_window_cycles = 5
    max_results = 10
    notification_level = "none"
    # #154 stages off: these tests isolate the skip signals.
    scheduler_failure_alert_after = 0
    scheduler_failure_pause_after = 0
    scheduler_active_execution_stale_minutes = 1440
    scheduler_skip_alert_after = 1
    scheduler_skip_alert_cooldown_minutes = 1440


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.todo_manager = TodoManager(data_dir)
        self.thread_config_manager = FakeThreadConfigManager()
        self.profile_manager = UserProfileManager(data_dir)
        self.settings = FakeSettings()
        self.settings.data_dir = data_dir
        self._schedule_db = TodoScheduleDB(data_dir / "todo_schedule.db")
        self.turns = 0
        self.fail = False
        self.on_turn: Optional[Callable[[], None]] = None

    async def astream(self, **kwargs):
        self.turns += 1
        if self.on_turn is not None:
            self.on_turn()
        if self.fail:
            raise RuntimeError("provider exploded")
        yield {"type": "response", "content": "done"}

    def sync_agent_tools(self):
        pass


@pytest.fixture
def signals(monkeypatch):
    """Capture the ticker's activity rows, owner alerts, and event order."""
    captured: dict[str, list] = {"rows": [], "alerts": [], "order": []}
    monkeypatch.setattr(
        ticker_module,
        "publish_autonomous_event",
        lambda event_type, **kw: captured["order"].append(("event", event_type)),
    )
    monkeypatch.setattr(
        ticker_module, "publish_agent_stream_chunk", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        ticker_module, "create_autonomous_notification", lambda **kw: None
    )
    monkeypatch.setattr(
        ticker_module,
        "log_activity",
        lambda activity_type, message, **kw: captured["rows"].append(
            {"type": activity_type, "message": message, **kw}
        ),
    )
    monkeypatch.setattr(
        ticker_module,
        "send_owner_alert",
        lambda message, settings, **kw: (
            captured["alerts"].append({"message": message, **kw}),
            captured["order"].append(("alert", message.split("]")[0] + "]")),
        ),
    )
    return captured


def _skip_rows(signals, reason: str) -> list[dict]:
    return [
        row
        for row in signals["rows"]
        if row["type"] == ActivityType.TASK_SKIPPED
        and (row.get("metadata") or {}).get("reason") == reason
    ]


def _alerts(signals, prefix: str) -> list[dict]:
    return [a for a in signals["alerts"] if a["message"].startswith(prefix)]


def _make_ticker(
    tmp_path: Path,
    *,
    failing: bool = False,
    alert_after: int = 1,
    cooldown_minutes: int = 1440,
):
    agent = FakeAgent(tmp_path)
    agent.fail = failing
    agent.settings.scheduler_skip_alert_after = alert_after
    agent.settings.scheduler_skip_alert_cooldown_minutes = cooldown_minutes
    # The fakes stand in for the real collaborators (duck-typed).
    fake: Any = agent
    ticker = Ticker(
        executor=LocalAgentExecutor(fake),
        settings=cast(Any, agent.settings),
        schedule_db=agent._schedule_db,
        todo_manager=agent.todo_manager,
        thread_config_manager=cast(Any, agent.thread_config_manager),
        profile_manager=agent.profile_manager,
        busy_agent=fake,
        spawn_sweeper=None,
    )
    return ticker, agent


def _add_todo(agent: FakeAgent, *, slot: datetime, recurrence: str = "1h"):
    with agent.todo_manager.atomic_update(USER) as todo_list:
        todo = todo_list.add_item(
            "Hourly medication check",
            scheduled_for=slot,
            thread_id="thread-1",
            created_by="user",
            recurrence=recurrence,
        )
    assert todo is not None
    agent.todo_manager.sync_schedule_to_db(USER, todo.id, agent._schedule_db)
    return todo


def _move_slot(agent: FakeAgent, todo_id: str, slot: datetime) -> None:
    """Stage the next fire: put the TODO's due slot at ``slot``."""
    with agent.todo_manager.atomic_update(USER) as todo_list:
        assert todo_list.update_item(todo_id, scheduled_for=slot)
    agent.todo_manager.sync_schedule_to_db(USER, todo_id, agent._schedule_db)


def _current(agent: FakeAgent, todo_id: str):
    item = agent.todo_manager.get_todo_by_id(USER, todo_id)
    assert item is not None
    return item


def _entry_for(agent: FakeAgent, todo_id: str) -> ScheduledTodoEntry:
    # The row is always derived from the item: fire the item's CURRENT slot
    # (a divergent slot reads as a mid-run rewrite and skips the re-arm).
    current = _current(agent, todo_id)
    assert current.scheduled_for is not None
    return ScheduledTodoEntry(
        todo_id=todo_id,
        user_id=USER,
        thread_id="thread-1",
        scheduled_for=current.scheduled_for.timestamp(),
        task_preview=current.task,
        created_at=time.time(),
    )


def _set_marker_started_at(db_path: Path, todo_id: str, started_at: float) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE active_todo_executions SET started_at = ? WHERE todo_id = ?",
            (started_at, todo_id),
        )
        conn.commit()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------
# Late fires: the occurrences they let pass are reported
# ------------------------------------------------------------------


def test_late_fire_reports_the_collapsed_occurrences_once(tmp_path, signals):
    ticker, agent = _make_ticker(tmp_path)
    slot = _now() - timedelta(hours=3, minutes=10)
    todo = _add_todo(agent, slot=slot)

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    current = _current(agent, todo.id)
    # The cadence behavior is unchanged: skip forward to the next future slot.
    assert current.scheduled_for is not None
    assert abs(
        current.scheduled_for.timestamp() - (slot + timedelta(hours=4)).timestamp()
    ) < 1
    rows = _skip_rows(signals, "occurrences_collapsed")
    assert len(rows) == 1
    meta = rows[0]["metadata"]
    assert meta["skipped_occurrences"] == 3
    assert meta["todo_id"] == todo.id
    assert abs(datetime.fromisoformat(meta["fired_slot"]).timestamp() - slot.timestamp()) < 1
    assert abs(
        datetime.fromisoformat(meta["next_slot"]).timestamp()
        - current.scheduled_for.timestamp()
    ) < 1
    assert rows[0]["message"].startswith("Skipped 3 scheduled occurrences:")
    alerts = _alerts(signals, "[SCHEDULED TASK SKIPPED]")
    assert len(alerts) == 1
    assert "skipped 3 scheduled occurrences" in alerts[0]["message"]
    assert ticker_module._format_slot(current.scheduled_for) in alerts[0]["message"]
    assert alerts[0]["task_id"] == todo.id
    assert alerts[0]["user_id"] == USER
    assert current.skip_alerted_at is not None


def test_skip_alert_goes_out_after_the_runs_own_completion_event(tmp_path, signals):
    # The late run IS the reminder: its task_completed (what the bots
    # deliver) must not wait behind an alert to a slow destination.
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(hours=2, minutes=10))

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    order = signals["order"]
    completed = order.index(("event", "task_completed"))
    alerted = order.index(("alert", "[SCHEDULED TASK SKIPPED]"))
    assert completed < alerted


def test_further_skips_inside_the_cooldown_write_rows_without_realerting(
    tmp_path, signals
):
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(hours=2, minutes=10))
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    # An on-time run between does not reopen the alert window.
    _move_slot(agent, todo.id, _now() - timedelta(minutes=1))
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))
    _move_slot(agent, todo.id, _now() - timedelta(hours=1, minutes=10))
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    rows = _skip_rows(signals, "occurrences_collapsed")
    assert [r["metadata"]["skipped_occurrences"] for r in rows] == [2, 1]
    assert len(_alerts(signals, "[SCHEDULED TASK SKIPPED]")) == 1


def test_a_restart_inside_the_cooldown_does_not_realert(tmp_path, signals):
    # deploy-sync restarts the stack on every push: the cooldown lives on
    # the TODO, not in ticker memory.
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(hours=2, minutes=10))
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    restarted, _ = _make_ticker(tmp_path)
    _move_slot(agent, todo.id, _now() - timedelta(hours=1, minutes=10))
    restarted._execute_scheduled_todo(_entry_for(agent, todo.id))

    assert len(_skip_rows(signals, "occurrences_collapsed")) == 2
    assert len(_alerts(signals, "[SCHEDULED TASK SKIPPED]")) == 1


def test_a_skip_after_the_cooldown_alerts_again(tmp_path, signals):
    ticker, agent = _make_ticker(tmp_path, cooldown_minutes=60)
    todo = _add_todo(agent, slot=_now() - timedelta(hours=2, minutes=10))
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))
    with agent.todo_manager.atomic_update(USER) as todo_list:
        item = todo_list.get_item(todo.id)
        assert item is not None
        item.skip_alerted_at = _now() - timedelta(minutes=61)

    _move_slot(agent, todo.id, _now() - timedelta(hours=1, minutes=10))
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    assert len(_alerts(signals, "[SCHEDULED TASK SKIPPED]")) == 2


def test_zero_cooldown_alerts_on_every_late_run(tmp_path, signals):
    ticker, agent = _make_ticker(tmp_path, cooldown_minutes=0)
    todo = _add_todo(agent, slot=_now() - timedelta(hours=2, minutes=10))
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))
    _move_slot(agent, todo.id, _now() - timedelta(hours=1, minutes=10))
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    assert len(_alerts(signals, "[SCHEDULED TASK SKIPPED]")) == 2


def test_alert_threshold_and_zero_disables_but_rows_stay(tmp_path, signals):
    ticker, agent = _make_ticker(tmp_path / "a", alert_after=3)
    todo = _add_todo(agent, slot=_now() - timedelta(hours=2, minutes=10))
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))  # 2 skipped < 3
    assert not _alerts(signals, "[SCHEDULED TASK SKIPPED]")
    _move_slot(agent, todo.id, _now() - timedelta(hours=3, minutes=10))
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))  # 3 skipped
    assert len(_alerts(signals, "[SCHEDULED TASK SKIPPED]")) == 1

    off, off_agent = _make_ticker(tmp_path / "b", alert_after=0)
    off_todo = _add_todo(off_agent, slot=_now() - timedelta(hours=5, minutes=10))
    off._execute_scheduled_todo(_entry_for(off_agent, off_todo.id))

    assert len(_alerts(signals, "[SCHEDULED TASK SKIPPED]")) == 1
    assert [
        r["metadata"]["skipped_occurrences"]
        for r in _skip_rows(signals, "occurrences_collapsed")
    ] == [2, 3, 5]


def test_on_time_run_reports_no_skip(tmp_path, signals):
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(minutes=1))

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    assert agent.turns == 1
    assert not _skip_rows(signals, "occurrences_collapsed")
    assert not signals["alerts"]
    assert _current(agent, todo.id).skip_alerted_at is None


def test_retry_give_up_rearm_reports_the_collapse(tmp_path, signals):
    ticker, agent = _make_ticker(tmp_path, failing=True)
    todo = _add_todo(agent, slot=_now() - timedelta(hours=2, minutes=10))
    entry = _entry_for(agent, todo.id)

    for _ in range(Ticker.MAX_RETRIES):
        ticker._execute_scheduled_todo(entry)

    rows = _skip_rows(signals, "occurrences_collapsed")
    assert [r["metadata"]["skipped_occurrences"] for r in rows] == [2]
    assert len(_alerts(signals, "[SCHEDULED TASK SKIPPED]")) == 1


def test_a_late_workflow_todo_reports_too(tmp_path, signals):
    # A workflow TODO runs no agent turn; its run end is its own path.
    class _OkWorkflowExecutor:
        async def run_workflow(self, workflow_id, params=None, **kw):
            return {"ok": True, "status": "ok"}

    ticker, agent = _make_ticker(tmp_path)
    with agent.todo_manager.atomic_update(USER) as todo_list:
        todo = todo_list.add_item(
            "Hourly sync",
            scheduled_for=_now() - timedelta(hours=2, minutes=10),
            thread_id="thread-1",
            created_by="user",
            recurrence="1h",
            workflow_id="wf_sync",
        )
    assert todo is not None
    agent.todo_manager.sync_schedule_to_db(USER, todo.id, agent._schedule_db)
    ticker._turn_executor = cast(Any, _OkWorkflowExecutor())

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    rows = _skip_rows(signals, "occurrences_collapsed")
    assert [r["metadata"]["skipped_occurrences"] for r in rows] == [2]
    assert signals["order"].index(("event", "task_completed")) < signals[
        "order"
    ].index(("alert", "[SCHEDULED TASK SKIPPED]"))


def _wire_todo_tool(monkeypatch, agent: FakeAgent) -> None:
    monkeypatch.setattr(todo_tools, "_todo_manager", agent.todo_manager)
    monkeypatch.setattr(todo_tools, "_get_schedule_db", lambda: agent._schedule_db)


def test_late_fire_whose_turn_marks_it_done_still_reports(
    tmp_path, signals, monkeypatch
):
    # The reminder shape: the fire turn confirms and marks the recurring
    # TODO done. The done path skips forward past the missed slots itself,
    # and the ticker honors that write, so counting only in the ticker's own
    # re-arm missed the skip entirely.
    ticker, agent = _make_ticker(tmp_path)
    _wire_todo_tool(monkeypatch, agent)
    slot = _now() - timedelta(hours=3, minutes=10)
    todo = _add_todo(agent, slot=slot)

    def mark_done():
        done_tool: Any = todo_tools.nym_todo
        result = done_tool.func(
            todo_id=todo.id,
            status="done",
            config={"configurable": {"user_id": USER, "thread_id": "thread-1"}},
        )
        assert result.startswith("[Completed]")

    agent.on_turn = mark_done

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    rearmed = _current(agent, todo.id).scheduled_for
    assert rearmed is not None
    assert abs(rearmed.timestamp() - (slot + timedelta(hours=4)).timestamp()) < 1
    rows = _skip_rows(signals, "occurrences_collapsed")
    assert [r["metadata"]["skipped_occurrences"] for r in rows] == [3]
    assert len(_alerts(signals, "[SCHEDULED TASK SKIPPED]")) == 1


def test_late_fire_whose_turn_rearms_itself_still_reports(tmp_path, signals):
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(hours=3, minutes=10))
    rearm = _now() + timedelta(minutes=35)
    agent.on_turn = lambda: _move_slot(agent, todo.id, rearm)

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    # The turn's own re-arm is honored ...
    rearmed = _current(agent, todo.id).scheduled_for
    assert rearmed is not None and abs(rearmed.timestamp() - rearm.timestamp()) < 1
    # ... and the three slots the late fire let pass are still reported.
    rows = _skip_rows(signals, "occurrences_collapsed")
    assert [r["metadata"]["skipped_occurrences"] for r in rows] == [3]


def test_on_time_fire_with_a_mid_run_rearm_is_not_a_skip(tmp_path, signals):
    # The agent chose a wake six 5m slots away ("remind me in 35 min"):
    # intent, not a skip, unlike the ticker's own backoff wake.
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(seconds=20), recurrence="5m")
    rearm = _now() + timedelta(minutes=35)
    agent.on_turn = lambda: _move_slot(agent, todo.id, rearm)

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    assert not _skip_rows(signals, "occurrences_collapsed")
    assert not signals["alerts"]


def test_a_mid_run_recurrence_edit_is_not_a_skip(tmp_path, signals):
    # Counting uses the cadence the occurrence FIRED on: a turn that turns a
    # daily series into a 1m one has not skipped three daily occurrences.
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(minutes=3), recurrence="1d")

    def edit_recurrence():
        with agent.todo_manager.atomic_update(USER) as todo_list:
            assert todo_list.update_item(todo.id, recurrence="1m")

    agent.on_turn = edit_recurrence

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    assert _current(agent, todo.id).recurrence == "1m"
    assert not _skip_rows(signals, "occurrences_collapsed")
    assert not signals["alerts"]


def test_a_late_fire_that_backs_off_reports_the_slots_it_let_pass(
    tmp_path, signals
):
    # The double-iteration-limit backoff reschedules to now+10m; the next
    # fire anchors on that slot, so this run end is the only place to count.
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(hours=2, minutes=10))
    entry = _entry_for(agent, todo.id)

    ticker._backoff_after_double_limit(entry, _current(agent, todo.id), "thread-1")

    backoff = _current(agent, todo.id).scheduled_for
    assert backoff is not None
    assert abs(backoff.timestamp() - (_now() + timedelta(minutes=10)).timestamp()) < 5
    rows = _skip_rows(signals, "occurrences_collapsed")
    assert [r["metadata"]["skipped_occurrences"] for r in rows] == [2]
    assert signals["order"].index(("event", "task_completed")) < signals[
        "order"
    ].index(("alert", "[SCHEDULED TASK SKIPPED]"))


class _Clock:
    """Real time plus a jump the test controls (the recurrence math reads
    ``todo_constants.utc_now``)."""

    def __init__(self):
        self.offset = timedelta(0)

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + self.offset


def test_a_slot_armed_during_the_run_end_is_due_not_skipped(
    tmp_path, signals, monkeypatch
):
    # The report runs at the very end of the run. On a 1m series the slot
    # the re-arm just armed can come due while the run is still wrapping up
    # (indexing, the FCM push): it fires on the next poll, so it is not a
    # skip, and the row must not call it one.
    clock = _Clock()
    monkeypatch.setattr(todo_constants_module, "utc_now", clock.now)
    ticker, agent = _make_ticker(tmp_path)
    slot = _now() - timedelta(seconds=30)
    todo = _add_todo(agent, slot=slot, recurrence="1m")

    def slow_wrap_up(**kw):
        clock.offset = timedelta(seconds=45)

    monkeypatch.setattr(ticker, "_index_todo_completion", slow_wrap_up)

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    armed = _current(agent, todo.id).scheduled_for
    assert armed is not None
    assert abs(armed.timestamp() - (slot + timedelta(minutes=1)).timestamp()) < 1
    assert not _skip_rows(signals, "occurrences_collapsed")
    assert not signals["alerts"]


def test_a_turn_that_marks_done_then_keeps_working_is_not_a_skip(
    tmp_path, signals, monkeypatch
):
    clock = _Clock()
    monkeypatch.setattr(todo_constants_module, "utc_now", clock.now)
    ticker, agent = _make_ticker(tmp_path)
    _wire_todo_tool(monkeypatch, agent)
    todo = _add_todo(agent, slot=_now() - timedelta(seconds=30), recurrence="1m")

    def done_then_more_work():
        done_tool: Any = todo_tools.nym_todo
        assert done_tool.func(
            todo_id=todo.id,
            status="done",
            config={"configurable": {"user_id": USER, "thread_id": "thread-1"}},
        ).startswith("[Completed]")
        clock.offset = timedelta(seconds=45)  # the turn keeps talking

    agent.on_turn = done_then_more_work

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    assert not _skip_rows(signals, "occurrences_collapsed")
    assert not signals["alerts"]


def test_the_tickers_own_backoff_wake_counts_the_slots_it_passes_over(
    tmp_path, signals
):
    # A 5m series that double-limits on time is woken at now+10m: its +5m
    # and +10m slots never run, and the next fire anchors on the wake.
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(seconds=10), recurrence="5m")

    ticker._backoff_after_double_limit(
        _entry_for(agent, todo.id), _current(agent, todo.id), "thread-1"
    )

    rows = _skip_rows(signals, "occurrences_collapsed")
    assert [r["metadata"]["skipped_occurrences"] for r in rows] == [2]


def test_skip_copy_speaks_the_users_timezone_and_falls_back_to_utc(
    tmp_path, signals, monkeypatch
):
    sydney = ZoneInfo("Australia/Sydney")
    monkeypatch.setattr(time_utils_module, "get_user_tz", lambda: sydney)
    ticker, agent = _make_ticker(tmp_path)
    slot = _now() - timedelta(hours=2, minutes=10)
    todo = _add_todo(agent, slot=slot)

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    alert = _alerts(signals, "[SCHEDULED TASK SKIPPED]")[0]["message"]
    local = slot.astimezone(sydney).strftime("%Y-%m-%d %H:%M")
    assert f"due {local} Australia/Sydney" in alert

    def broken_zone():
        raise KeyError("Mars/Olympus")

    monkeypatch.setattr(time_utils_module, "get_user_tz", broken_zone)
    assert ticker_module._format_slot(slot) == (
        slot.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC"
    )


def test_give_up_counts_on_the_cadence_its_last_attempt_fired_on(
    tmp_path, signals
):
    # The final failing attempt turns a daily series into a 1m one before
    # failing: the give-up must count on the daily cadence it fired on.
    ticker, agent = _make_ticker(tmp_path, failing=True)
    todo = _add_todo(agent, slot=_now() - timedelta(minutes=3), recurrence="1d")

    def edit_on_last_attempt():
        if agent.turns == Ticker.MAX_RETRIES:
            with agent.todo_manager.atomic_update(USER) as todo_list:
                assert todo_list.update_item(todo.id, recurrence="1m")

    agent.on_turn = edit_on_last_attempt
    entry = _entry_for(agent, todo.id)
    for _ in range(Ticker.MAX_RETRIES):
        ticker._execute_scheduled_todo(entry)

    assert _current(agent, todo.id).recurrence == "1m"
    assert not _skip_rows(signals, "occurrences_collapsed")


def test_a_failing_skip_row_never_breaks_the_run_end_or_drops_the_alert(
    tmp_path, signals, monkeypatch
):
    ticker, agent = _make_ticker(tmp_path)
    slot = _now() - timedelta(hours=2, minutes=10)
    todo = _add_todo(agent, slot=slot)

    def exploding_log(activity_type, message, **kw):
        if activity_type == ActivityType.TASK_SKIPPED:
            raise OSError("activity log unwritable")
        signals["rows"].append({"type": activity_type, "message": message, **kw})

    monkeypatch.setattr(ticker_module, "log_activity", exploding_log)

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    current = _current(agent, todo.id)
    assert current.scheduled_for is not None
    assert abs(
        current.scheduled_for.timestamp() - (slot + timedelta(hours=3)).timestamp()
    ) < 1
    assert agent._schedule_db.get_execution_started_at(todo.id) is None
    assert len(_alerts(signals, "[SCHEDULED TASK SKIPPED]")) == 1


def test_no_skip_alert_when_its_cooldown_stamp_cannot_be_saved(
    tmp_path, signals, monkeypatch
):
    # An alert must never outrun the state that suppresses its repeats.
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(hours=2, minutes=10))
    real_save = agent.todo_manager.save_todos

    def refuse_stamped_saves(todo_list):
        if any(item.skip_alerted_at is not None for item in todo_list.items):
            return False
        return real_save(todo_list)

    monkeypatch.setattr(agent.todo_manager, "save_todos", refuse_stamped_saves)

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    assert len(_skip_rows(signals, "occurrences_collapsed")) == 1
    assert not _alerts(signals, "[SCHEDULED TASK SKIPPED]")
    assert _current(agent, todo.id).skip_alerted_at is None


def test_resuming_a_paused_series_clears_the_skip_alert_stamp(tmp_path):
    manager = TodoManager(tmp_path)
    with manager.atomic_update(USER) as todo_list:
        todo = todo_list.add_item(
            "Paused reminder", thread_id="thread-1", recurrence="1d"
        )
        assert todo is not None
        item = todo_list.get_item(todo.id)
        assert item is not None
        item.skip_alerted_at = _now()
        item.schedule_paused_at = _now()

    with manager.atomic_update(USER) as todo_list:
        assert todo_list.update_item(
            todo.id, scheduled_for=_now() + timedelta(hours=1)
        )

    resumed = manager.get_todo_by_id(USER, todo.id)
    assert resumed is not None
    assert resumed.schedule_paused_at is None
    assert resumed.skip_alerted_at is None


# ------------------------------------------------------------------
# Blocked by a leftover execution marker
# ------------------------------------------------------------------


def _leave_marker(agent: FakeAgent, todo_id: str, started_at: float) -> None:
    if agent._schedule_db.get_execution_started_at(todo_id) is None:
        assert agent._schedule_db.mark_execution_started(todo_id, USER, "thread-1")
    _set_marker_started_at(
        agent.settings.data_dir / "todo_schedule.db", todo_id, started_at
    )


def test_marker_from_an_earlier_run_blocks_visibly_once(tmp_path, signals):
    ticker, agent = _make_ticker(tmp_path)
    slot = _now() - timedelta(minutes=1)
    todo = _add_todo(agent, slot=slot)
    started = (slot - timedelta(hours=1)).timestamp()
    _leave_marker(agent, todo.id, started)
    entry = _entry_for(agent, todo.id)

    for _ in range(3):  # the refusal repeats on every poll
        ticker._execute_scheduled_todo(entry)

    assert agent.turns == 0
    rows = _skip_rows(signals, "execution_marker_held")
    assert len(rows) == 1
    assert rows[0]["metadata"]["todo_id"] == todo.id
    assert rows[0]["metadata"]["release_retry_pending"] is False
    assert rows[0]["message"].startswith("Blocked from starting:")
    reclaim = datetime.fromisoformat(rows[0]["metadata"]["reclaim_at"])
    assert abs(reclaim.timestamp() - (started + 24 * 3600)) < 1
    alerts = _alerts(signals, "[SCHEDULED TASK BLOCKED]")
    assert len(alerts) == 1
    # Actionable: says when the stale sweep frees it, in the user's zone.
    assert ticker_module._format_slot(started + 24 * 3600) in alerts[0]["message"]
    assert "restarting the scheduler clears it now" in alerts[0]["message"]
    # Still due and untouched: nothing consumed the occurrence.
    still_due = _current(agent, todo.id).scheduled_for
    assert still_due is not None and abs(still_due.timestamp() - slot.timestamp()) < 1


def test_a_different_leftover_marker_reports_without_a_claim_between(
    tmp_path, signals
):
    ticker, agent = _make_ticker(tmp_path)
    slot = _now() - timedelta(minutes=1)
    todo = _add_todo(agent, slot=slot)
    _leave_marker(agent, todo.id, (slot - timedelta(hours=2)).timestamp())
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    # The stale marker is swept and another run's leftover takes its place.
    _leave_marker(agent, todo.id, (slot - timedelta(minutes=30)).timestamp())
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    assert agent.turns == 0
    assert len(_alerts(signals, "[SCHEDULED TASK BLOCKED]")) == 2


def test_the_same_marker_after_a_clean_claim_reports_again(tmp_path, signals):
    # The per-marker memory ends at the next clean claim, so a later
    # leftover is always a new report (and the memory stays bounded).
    ticker, agent = _make_ticker(tmp_path)
    slot = _now() - timedelta(hours=2)
    todo = _add_todo(agent, slot=slot)
    started = (slot - timedelta(hours=1)).timestamp()
    _leave_marker(agent, todo.id, started)
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    assert agent._schedule_db.clear_execution(todo.id, USER)
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))
    assert agent.turns == 1

    _move_slot(agent, todo.id, _now() - timedelta(minutes=1))
    _leave_marker(agent, todo.id, started)
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    assert len(_alerts(signals, "[SCHEDULED TASK BLOCKED]")) == 2


def test_marker_for_this_same_occurrence_is_silent(tmp_path, signals):
    # Claimed at or after the due slot: this occurrence is running in some
    # other scheduler process. Not a skip. Both sides of the boundary.
    ticker, agent = _make_ticker(tmp_path)
    slot = _now() - timedelta(minutes=1)
    todo = _add_todo(agent, slot=slot)
    entry = _entry_for(agent, todo.id)
    for started in (entry.scheduled_for + 5, entry.scheduled_for):
        _leave_marker(agent, todo.id, started)
        ticker._execute_scheduled_todo(entry)

    assert agent.turns == 0
    assert not signals["rows"]
    assert not signals["alerts"]


def test_refused_claim_without_a_marker_is_silent(tmp_path, signals, monkeypatch):
    # A claim that fails on a database error leaves no marker; the next poll
    # retries, so there is nothing to report.
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(minutes=1))
    monkeypatch.setattr(
        agent._schedule_db, "mark_execution_started", lambda *a, **kw: False
    )

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    assert agent.turns == 0
    assert not signals["rows"]
    assert not signals["alerts"]


def test_a_failing_refusal_report_never_escapes_the_run(
    tmp_path, signals, monkeypatch
):
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(minutes=1))
    monkeypatch.setattr(
        agent._schedule_db, "mark_execution_started", lambda *a, **kw: False
    )

    def broken_read(todo_id):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(agent._schedule_db, "get_execution_started_at", broken_read)

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))  # must not raise

    assert agent.turns == 0


# ------------------------------------------------------------------
# A failed marker release is retried, not left for the stale window
# ------------------------------------------------------------------


def _fail_releases(monkeypatch, db: TodoScheduleDB, failures: int = 1) -> None:
    real_clear = db.clear_execution
    calls = {"n": 0}

    def flaky_clear(todo_id, user_id=None):
        calls["n"] += 1
        if calls["n"] <= failures:
            return False  # sqlite error swallowed inside clear_execution
        return real_clear(todo_id, user_id)

    monkeypatch.setattr(db, "clear_execution", flaky_clear)


class _InlineExecutor:
    """Runs each submission immediately, as a real pool eventually would."""

    def submit(self, fn, *args):
        future: Future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as exc:  # pragma: no cover - surfaced via the future
            future.set_exception(exc)
        return future


def test_failed_release_is_retried_by_the_next_poll(tmp_path, signals, monkeypatch):
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(minutes=1))
    _fail_releases(monkeypatch, agent._schedule_db)

    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))
    assert agent.turns == 1
    assert agent._schedule_db.get_execution_started_at(todo.id) is not None

    ticker._check_and_execute()  # one poll (no pool: nothing is due anyway)

    assert agent._schedule_db.get_execution_started_at(todo.id) is None
    # The TODO's next occurrence is not held hostage by the leaked marker.
    _move_slot(agent, todo.id, _now() - timedelta(seconds=30))
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))
    assert agent.turns == 2


def test_release_retry_runs_before_the_polls_new_submissions(
    tmp_path, signals, monkeypatch
):
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(minutes=1))
    _fail_releases(monkeypatch, agent._schedule_db)
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))
    assert agent.turns == 1

    # The next occurrence is due on the very poll that retries the release.
    _move_slot(agent, todo.id, _now())
    ticker._executor = cast(Any, _InlineExecutor())
    ticker._check_and_execute()

    assert agent.turns == 2


def test_release_retry_waits_while_a_run_of_that_todo_is_registered(
    tmp_path, signals, monkeypatch
):
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(minutes=1))
    _fail_releases(monkeypatch, agent._schedule_db)
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    # A run of the same TODO is registered in the pool and holds a marker:
    # releasing now would delete a live run's lock.
    live_run: Future = Future()
    ticker._active_futures[todo.id] = live_run
    ticker._check_and_execute()
    assert agent._schedule_db.get_execution_started_at(todo.id) is not None

    live_run.set_result(None)
    ticker._check_and_execute()  # reaps the finished run, then retries
    assert agent._schedule_db.get_execution_started_at(todo.id) is None


def test_blocked_by_its_own_failed_release_says_the_retry_is_pending(
    tmp_path, signals, monkeypatch
):
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(minutes=1))
    _fail_releases(monkeypatch, agent._schedule_db, failures=10)
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))
    ticker._check_and_execute()  # the retry fails too
    assert agent._schedule_db.get_execution_started_at(todo.id) is not None

    _move_slot(agent, todo.id, _now())
    ticker._execute_scheduled_todo(_entry_for(agent, todo.id))

    rows = _skip_rows(signals, "execution_marker_held")
    assert len(rows) == 1
    assert rows[0]["metadata"]["release_retry_pending"] is True
    alert = _alerts(signals, "[SCHEDULED TASK BLOCKED]")[0]["message"]
    assert "retries every poll" in alert
    assert "restarting the scheduler" not in alert


# ------------------------------------------------------------------
# #395: a run that never returns holds its TODO, and is now reported
# ------------------------------------------------------------------


class _MonotonicClock:
    """The ticker's monotonic clock seam, advanced by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += minutes * 60


class _NeverEndingPool:
    """A pool whose runs START (the stamp is taken) and never finish until
    the test resolves them. ``start=False`` models a full pool: the run is
    queued and never starts."""

    def __init__(self, *, start: bool = True):
        self.start = start
        self.submitted: list[Future] = []

    def submit(self, fn, *args):
        future: Future = Future()
        self.submitted.append(future)
        if self.start:
            fn(*args)
        return future


class _RecordingPool:
    """Housekeeping pool that records submissions instead of running them."""

    def __init__(self):
        self.calls: list[tuple] = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))
        future: Future = Future()
        future.set_result(None)
        return future


def _stuck_ticker(tmp_path, *, alert_minutes: int = 60, start: bool = True):
    ticker, agent = _make_ticker(tmp_path)
    agent.settings.scheduler_run_stuck_alert_minutes = alert_minutes  # type: ignore[attr-defined]
    ticker._stuck_run_alert_seconds = alert_minutes * 60
    ticker._pool_size = 5
    clock = _MonotonicClock()
    ticker._monotonic = clock
    # The run itself never returns: its body is a no-op and the pool never
    # resolves its future.
    ticker._execute_scheduled_todo = lambda entry: None  # type: ignore[method-assign]
    pool = _NeverEndingPool(start=start)
    ticker._executor = cast(Any, pool)
    return ticker, agent, pool, clock


def _one_shot(agent, task: str = "Hourly medication check"):
    with agent.todo_manager.atomic_update(USER) as todo_list:
        todo = todo_list.add_item(
            task,
            scheduled_for=_now() - timedelta(minutes=1),
            thread_id="thread-1",
            created_by="user",
        )
    assert todo is not None
    agent.todo_manager.sync_schedule_to_db(USER, todo.id, agent._schedule_db)
    return todo


def _long_run_alerts(signals) -> list[dict]:
    return _alerts(signals, "[SCHEDULED TASK STILL RUNNING]")


@pytest.mark.parametrize("recurrence", [None, "1h"], ids=["one-shot", "recurring"])
def test_a_run_that_never_ends_is_reported_once(tmp_path, signals, caplog, recurrence):
    """B1 B3: one WARNING, one row, one alert, however many polls pass, and a
    one-shot is reported like a recurring TODO (no next occurrence needed)."""
    ticker, agent, pool, clock = _stuck_ticker(tmp_path)
    todo = _add_todo(agent, slot=_now() - timedelta(minutes=1), recurrence=recurrence)

    ticker._check_and_execute()
    assert len(pool.submitted) == 1
    clock.advance(59)
    ticker._check_and_execute()  # B2: under the threshold, nothing
    assert _long_run_alerts(signals) == []

    clock.advance(2)
    with caplog.at_level("WARNING", logger="nymeria.core.ticker"):
        for _ in range(3):
            ticker._check_and_execute()

    assert len(pool.submitted) == 1  # it still holds its TODO
    assert len(_long_run_alerts(signals)) == 1
    rows = _skip_rows(signals, "run_still_running")
    assert len(rows) == 1
    meta = rows[0]["metadata"]
    assert meta["todo_id"] == todo.id
    assert meta["running_minutes"] == 61
    assert meta["scheduled_runs_running"] == 1
    assert meta["pool_size"] == 5
    started = datetime.fromisoformat(meta["run_started_at"])
    assert abs((_now() - started).total_seconds()) < 60  # wall time of the start
    assert rows[0]["user_id"] == USER
    assert rows[0]["thread_id"] == "thread-1"
    warnings = [r for r in caplog.records if "has been running for" in r.getMessage()]
    assert len(warnings) == 1


def test_a_run_still_waiting_for_a_free_slot_is_never_reported_as_running(
    tmp_path, signals, caplog
):
    """M1: a full pool queues the run; it has not started, so /stop in its
    thread could not help and the alert would name the victim, not the cause."""
    ticker, agent, pool, clock = _stuck_ticker(tmp_path, start=False)
    _one_shot(agent)
    ticker._check_and_execute()
    clock.advance(600)

    with caplog.at_level("INFO", logger="nymeria.core.ticker"):
        ticker._check_and_execute()

    assert len(pool.submitted) == 1
    assert _long_run_alerts(signals) == []
    assert _skip_rows(signals, "run_still_running") == []
    assert any("waiting for a free pool slot" in r.getMessage() for r in caplog.records)


def test_the_long_run_alert_leads_with_the_verdict_and_the_remedy(tmp_path, signals):
    """B8, with a task long enough that a copy leading with it would push the
    remedy past the in-app row's 200 characters (#406)."""
    ticker, agent, _pool, clock = _stuck_ticker(tmp_path)
    long_task = (
        "Check the overnight medication log and message the carer if any "
        "evening dose was missed or doubled"
    )
    todo = _one_shot(agent, long_task)
    ticker._check_and_execute()
    clock.advance(95)

    ticker._check_and_execute()

    alert = _long_run_alerts(signals)[0]
    message = alert["message"]
    headline = message[:200]
    assert f"TODO [{todo.id}]" in headline
    assert "has been running 95 min" in headline
    assert "will not run again until that run ends" in headline
    assert "/stop in thread thread-1 may free it" in headline
    assert long_task[:80] in message
    assert "a due one-shot then runs again" in message
    assert alert["user_id"] == USER
    assert alert["thread_id"] == "thread-1"
    assert alert["task_id"] == todo.id


def test_the_report_is_sent_from_the_housekeeping_pool_not_the_poll(tmp_path, signals):
    """M2: a notification send can take a timeout per destination; the poll
    thread only does the bookkeeping."""
    ticker, agent, _pool, clock = _stuck_ticker(tmp_path)
    housekeeping = _RecordingPool()
    ticker._housekeeping_executor = cast(Any, housekeeping)
    _one_shot(agent)
    ticker._check_and_execute()
    clock.advance(61)

    ticker._check_and_execute()
    ticker._check_and_execute()

    assert _long_run_alerts(signals) == []  # nothing sent inline
    reports = [c for c in housekeeping.calls if c[0] == ticker._report_stuck_run]
    assert len(reports) == 1  # and handed over once
    fn, args = reports[0]
    fn(*args)
    assert len(_long_run_alerts(signals)) == 1


def test_a_new_run_that_sticks_is_reported_again(tmp_path, signals, caplog):
    """B4 + B6: per run; a finished run's bookkeeping goes with its future,
    its end is logged, and a later run of the same TODO re-arms the report."""
    ticker, agent, pool, clock = _stuck_ticker(tmp_path)
    todo = _one_shot(agent)
    ticker._check_and_execute()
    clock.advance(61)
    ticker._check_and_execute()
    assert len(_long_run_alerts(signals)) == 1

    clock.advance(9)
    pool.submitted[0].set_result(None)  # the long run finally ends
    with caplog.at_level("INFO", logger="nymeria.core.ticker"):
        ticker._check_and_execute()  # reaps it; the row is still due: a new run

    assert any(
        "finished after 70 min" in r.getMessage() for r in caplog.records
    )
    assert len(pool.submitted) == 2
    assert todo.id not in ticker._reported_stuck_runs
    clock.advance(61)
    ticker._check_and_execute()
    assert len(_long_run_alerts(signals)) == 2


def test_several_long_runs_are_each_reported(tmp_path, signals):
    ticker, agent, pool, clock = _stuck_ticker(tmp_path)
    first = _one_shot(agent, "First reminder")
    second = _one_shot(agent, "Second reminder")
    ticker._check_and_execute()
    clock.advance(61)

    ticker._check_and_execute()

    alerted = sorted(a["task_id"] for a in _long_run_alerts(signals))
    assert alerted == sorted([first.id, second.id])
    rows = _skip_rows(signals, "run_still_running")
    assert all(r["metadata"]["scheduled_runs_running"] == 2 for r in rows)


def test_a_run_that_ended_is_not_reported(tmp_path, signals):
    """B6: the finished run is reaped before the check, however old it was."""
    ticker, agent, pool, clock = _stuck_ticker(tmp_path)
    _one_shot(agent)
    ticker._check_and_execute()
    clock.advance(300)
    pool.submitted[0].set_result(None)

    ticker._check_and_execute()

    assert _long_run_alerts(signals) == []
    assert _skip_rows(signals, "run_still_running") == []


def test_threshold_zero_disables_the_report(tmp_path, signals):
    """B5."""
    ticker, agent, _pool, clock = _stuck_ticker(tmp_path, alert_minutes=0)
    _one_shot(agent)
    ticker._check_and_execute()
    clock.advance(10_000)

    ticker._check_and_execute()

    assert _long_run_alerts(signals) == []
    assert _skip_rows(signals, "run_still_running") == []


def test_the_threshold_comes_from_settings(tmp_path):
    agent = FakeAgent(tmp_path)
    agent.settings.scheduler_run_stuck_alert_minutes = 7  # type: ignore[attr-defined]
    fake: Any = agent
    ticker = Ticker(
        executor=LocalAgentExecutor(fake),
        settings=cast(Any, agent.settings),
        schedule_db=agent._schedule_db,
        todo_manager=agent.todo_manager,
        thread_config_manager=cast(Any, agent.thread_config_manager),
        profile_manager=agent.profile_manager,
        busy_agent=fake,
        spawn_sweeper=None,
    )
    assert ticker._stuck_run_alert_seconds == 7 * 60


def test_a_held_todo_logs_once_per_run_not_every_poll(tmp_path, signals, caplog):
    """B7: the row stays due while its run goes, so this branch runs every
    poll; it used to be silent (and "Found N due" repeated every 5 s)."""
    ticker, agent, _pool, _clock = _stuck_ticker(tmp_path)
    todo = _one_shot(agent)

    with caplog.at_level("INFO", logger="nymeria.core.ticker"):
        ticker._check_and_execute()  # starts the run: its Due line is right
        caplog.clear()
        for _ in range(4):
            ticker._check_and_execute()

    messages = [r.getMessage() for r in caplog.records]
    assert sum("is due but its run started" in m for m in messages) == 1
    assert not any(f"Due: todo_id={todo.id}" in m for m in messages)


def test_a_failing_row_write_keeps_the_alert(tmp_path, signals, monkeypatch):
    ticker, agent, _pool, clock = _stuck_ticker(tmp_path)
    _one_shot(agent)
    ticker._check_and_execute()
    clock.advance(61)

    def broken_row(*args, **kwargs):
        raise RuntimeError("activity log down")

    monkeypatch.setattr(ticker_module, "log_activity", broken_row)
    ticker._check_and_execute()

    assert len(_long_run_alerts(signals)) == 1


def test_a_report_that_raises_does_not_drop_the_others(tmp_path, signals, monkeypatch):
    ticker, agent, _pool, clock = _stuck_ticker(tmp_path)
    first = _one_shot(agent, "First reminder")
    second = _one_shot(agent, "Second reminder")
    ticker._check_and_execute()
    clock.advance(61)
    real_report = ticker._report_stuck_run

    def flaky_report(run, now_mono, running):
        if run.entry.todo_id == first.id:
            raise RuntimeError("report exploded")
        real_report(run, now_mono, running)

    monkeypatch.setattr(ticker, "_report_stuck_run", flaky_report)
    ticker._check_and_execute()

    assert [a["task_id"] for a in _long_run_alerts(signals)] == [second.id]


def test_a_failing_report_never_breaks_the_poll(tmp_path, signals, monkeypatch):
    """B9: every alert path fails; another due TODO still starts."""
    ticker, agent, pool, clock = _stuck_ticker(tmp_path)
    _one_shot(agent)
    ticker._check_and_execute()
    clock.advance(61)

    def boom(*args, **kwargs):
        raise RuntimeError("alert plane down")

    monkeypatch.setattr(ticker_module, "send_owner_alert", boom)
    monkeypatch.setattr(ticker_module, "log_activity", boom)
    other = _one_shot(agent, "Another reminder")

    ticker._check_and_execute()

    assert len(pool.submitted) == 2
    assert other.id in ticker._active_futures
