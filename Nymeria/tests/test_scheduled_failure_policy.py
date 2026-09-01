"""#154 recurring-failure policy: count, alert, auto-pause, reset.

A failed recurring OCCURRENCE (the retry cap, not each intra-occurrence
retry) increments a durable per-TODO streak; the owner is alerted once at
``scheduler_failure_alert_after``, the schedule auto-pauses (recurrence
kept, resumable marker) at ``scheduler_failure_pause_after``, and any
success resets the episode. Trigger ACTION failures feed the triggers'
existing consecutive_errors/health machinery with one owner alert on the
transition into "failing". The re-arm-on-failure precedent (2026-07-09) is
preserved: the policy sits on top of it.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nymeria.core.ticker import Ticker
from nymeria.core.todo_manager import TodoManager
from nymeria.core.todo_schedule_db import ScheduledTodoEntry, TodoScheduleDB
from nymeria.core.trigger_manager import (
    TriggerAction,
    TriggerDefinition,
    TriggerManager,
)
from nymeria.core.time_utils import utc_now
from nymeria.core.turn_executor import LocalAgentExecutor
from nymeria.core.user_profile import UserProfileManager
from nymeria.core import notification_dispatch as dispatch_module
from nymeria.core import ticker as ticker_module


# ------------------------------------------------------------------
# Ticker-side fixtures (pattern: test_ticker_extraction.py)
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
    scheduler_failure_alert_after = 2
    scheduler_failure_pause_after = 5
    trigger_failure_alert_after = 2
    trigger_failure_pause_after = 5
    trigger_failure_alert_cooldown_minutes = 0


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.todo_manager = TodoManager(data_dir)
        self.thread_config_manager = FakeThreadConfigManager()
        self.profile_manager = UserProfileManager(data_dir)
        self.settings = FakeSettings()
        self.settings.data_dir = data_dir
        self._schedule_db = TodoScheduleDB(data_dir / "todo_schedule.db")

    def sync_agent_tools(self):
        pass


def _make_ticker(tmp_path: Path, *, alert_after: int = 2, pause_after: int = 5):
    agent = FakeAgent(tmp_path)
    agent.settings.scheduler_failure_alert_after = alert_after
    agent.settings.scheduler_failure_pause_after = pause_after
    ticker = Ticker(
        executor=LocalAgentExecutor(agent),
        settings=agent.settings,
        schedule_db=agent._schedule_db,
        todo_manager=agent.todo_manager,
        thread_config_manager=agent.thread_config_manager,
        profile_manager=agent.profile_manager,
        busy_agent=agent,
        spawn_sweeper=None,
    )
    return ticker, agent


def _add_recurring_todo(agent: FakeAgent, user_id: str = "owner"):
    with agent.todo_manager.atomic_update(user_id) as todo_list:
        todo = todo_list.add_item(
            "Morning briefing",
            scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
            thread_id="thread-1",
            created_by="user",
            recurrence="1d",
        )
    assert todo is not None
    agent.todo_manager.sync_schedule_to_db(user_id, todo.id, agent._schedule_db)
    return todo


def _get_todo(agent: FakeAgent, todo_id: str, user_id: str = "owner"):
    current = agent.todo_manager.get_todo_by_id(user_id, todo_id)
    assert current is not None
    return current


def _get_trigger(manager: TriggerManager, trigger_id: str, user_id: str = "owner"):
    stored = manager.get_trigger(user_id, trigger_id)
    assert stored is not None
    return stored


def _entry_for(agent, todo, user_id: str = "owner") -> ScheduledTodoEntry:
    # Entry slot mirrors the item's CURRENT scheduled_for, re-read from the
    # store, as at a real fire (the schedule row is always derived from the
    # item). Previously an approximate time.time() - 60 that matched the
    # item only within sub-second luck, and a stale snapshot would diverge
    # after the first occurrence re-arms; a divergent slot reads as a
    # mid-run schedule rewrite and skips the re-arm under test.
    current = agent.todo_manager.get_todo_by_id(user_id, todo.id)
    assert current is not None and current.scheduled_for is not None
    return ScheduledTodoEntry(
        todo_id=todo.id,
        user_id=user_id,
        thread_id="thread-1",
        scheduled_for=current.scheduled_for.timestamp(),
        task_preview=todo.task,
        created_at=time.time(),
    )


@pytest.fixture
def quiet_ticker_module(monkeypatch):
    """Silence the transcript/notification plane; capture owner alerts."""
    alerts: list[dict] = []
    monkeypatch.setattr(
        ticker_module,
        "publish_autonomous_event",
        lambda event_type, **kw: None,
    )
    monkeypatch.setattr(
        ticker_module, "publish_agent_stream_chunk", lambda *a, **kw: None
    )
    monkeypatch.setattr(ticker_module, "log_activity", lambda *a, **kw: None)
    monkeypatch.setattr(
        ticker_module, "create_autonomous_notification", lambda **kw: None
    )
    monkeypatch.setattr(
        ticker_module,
        "send_owner_alert",
        lambda message, settings, **kw: alerts.append(
            {"message": message, **kw}
        ),
    )
    return alerts


def _failing_astream(error_message: str = "provider exploded"):
    async def fake_astream(**kwargs):
        raise RuntimeError(error_message)
        yield  # pragma: no cover - makes this an async generator

    return fake_astream


def _succeeding_astream():
    async def fake_astream(**kwargs):
        yield {"type": "response", "content": "done"}

    return fake_astream


def _run_one_occurrence(ticker: Ticker, entry: ScheduledTodoEntry) -> None:
    """Drive one full failed occurrence: the retry cap's worth of attempts."""
    for _ in range(Ticker.MAX_RETRIES):
        ticker._execute_scheduled_todo(entry)


# ------------------------------------------------------------------
# Behaviors 1-2: streak counting and reset
# ------------------------------------------------------------------


def test_failed_occurrence_increments_streak_once_and_rearms(
    tmp_path, quiet_ticker_module
):
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_recurring_todo(agent)
    agent.astream = _failing_astream()

    _run_one_occurrence(ticker, _entry_for(agent, todo))

    current = _get_todo(agent, todo.id)
    # ONE increment per occurrence, not one per retry attempt.
    assert current.consecutive_failures == 1
    assert "provider exploded" in (current.last_failure or "")
    assert current.last_failure_at is not None
    # The re-arm precedent holds: next slot scheduled, schedule row present.
    assert current.scheduled_for is not None
    assert agent._schedule_db.get_entry(todo.id) is not None
    assert current.schedule_paused_at is None


def test_successful_occurrence_resets_streak(tmp_path, quiet_ticker_module):
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_recurring_todo(agent)
    agent.astream = _failing_astream()
    _run_one_occurrence(ticker, _entry_for(agent, todo))
    assert (
        _get_todo(agent, todo.id).consecutive_failures
        == 1
    )

    agent.astream = _succeeding_astream()
    ticker._execute_scheduled_todo(_entry_for(agent, todo))

    current = _get_todo(agent, todo.id)
    assert current.consecutive_failures == 0
    assert current.last_failure is None
    assert current.last_failure_at is None


# ------------------------------------------------------------------
# Behavior 3: one alert per episode at the threshold
# ------------------------------------------------------------------


def test_alert_fires_exactly_once_at_threshold(tmp_path, quiet_ticker_module):
    alerts = quiet_ticker_module
    ticker, agent = _make_ticker(tmp_path, alert_after=2, pause_after=0)
    todo = _add_recurring_todo(agent)
    agent.astream = _failing_astream("no such model")

    _run_one_occurrence(ticker, _entry_for(agent, todo))
    assert alerts == []  # first failure: could be a one-off

    _run_one_occurrence(ticker, _entry_for(agent, todo))
    assert len(alerts) == 1
    message = alerts[0]["message"]
    assert "Morning briefing" in message
    assert "2 consecutive" in message
    assert "no such model" in message
    assert alerts[0]["user_id"] == "owner"

    _run_one_occurrence(ticker, _entry_for(agent, todo))
    _run_one_occurrence(ticker, _entry_for(agent, todo))
    assert len(alerts) == 1  # still exactly one for the episode


# ------------------------------------------------------------------
# Behavior 4: auto-pause at the pause threshold
# ------------------------------------------------------------------


def test_pause_at_threshold_stops_the_schedule(tmp_path, quiet_ticker_module):
    alerts = quiet_ticker_module
    ticker, agent = _make_ticker(tmp_path, alert_after=0, pause_after=2)
    todo = _add_recurring_todo(agent)
    agent.astream = _failing_astream()

    _run_one_occurrence(ticker, _entry_for(agent, todo))
    _run_one_occurrence(ticker, _entry_for(agent, todo))

    current = _get_todo(agent, todo.id)
    assert current.schedule_paused_at is not None
    assert current.scheduled_for is None
    assert current.recurrence == "1d"  # kept: pause is resumable
    assert "[auto-paused after 2 consecutive" in (current.notes or "")
    assert agent._schedule_db.get_entry(todo.id) is None
    pause_alerts = [a for a in alerts if "PAUSED" in a["message"]]
    assert len(pause_alerts) == 1
    assert "reschedule" in pause_alerts[0]["message"]

    # Startup recovery must not resurrect a paused schedule, and no poll
    # can ever see it as due again.
    agent._schedule_db.rebuild_from_todos(agent.todo_manager)
    assert agent._schedule_db.get_entry(todo.id) is None
    assert todo.id not in [e.todo_id for e in agent._schedule_db.get_due()]


# ------------------------------------------------------------------
# Behavior 5 + the re-arm keying: resume clears, re-arm preserves
# ------------------------------------------------------------------


def test_explicit_reschedule_resumes_a_paused_todo(
    tmp_path, quiet_ticker_module
):
    ticker, agent = _make_ticker(tmp_path, alert_after=0, pause_after=1)
    todo = _add_recurring_todo(agent)
    agent.astream = _failing_astream()
    _run_one_occurrence(ticker, _entry_for(agent, todo))
    assert (
        _get_todo(agent, todo.id).schedule_paused_at
        is not None
    )

    with agent.todo_manager.atomic_update("owner") as todo_list:
        todo_list.update_item(
            todo.id,
            scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    current = _get_todo(agent, todo.id)
    assert current.schedule_paused_at is None
    assert current.consecutive_failures == 0
    assert current.last_failure is None
    assert current.last_failure_at is None
    assert current.scheduled_for is not None


def test_ticker_rearm_preserves_streak_on_unpaused_todo(
    tmp_path, quiet_ticker_module
):
    """The resume-clear is keyed on the pause marker: the recurrence re-arm
    (and any reschedule of an unpaused TODO) must NOT zero the streak, or
    every failed occurrence would reset its own count."""
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_recurring_todo(agent)
    agent.astream = _failing_astream()
    _run_one_occurrence(ticker, _entry_for(agent, todo))

    current = _get_todo(agent, todo.id)
    # The occurrence ended with a re-arm (scheduled_for set by update_item);
    # the streak survived it.
    assert current.scheduled_for is not None
    assert current.consecutive_failures == 1

    with agent.todo_manager.atomic_update("owner") as todo_list:
        todo_list.update_item(
            todo.id,
            scheduled_for=datetime.now(timezone.utc) + timedelta(hours=2),
        )
    assert (
        _get_todo(agent, todo.id).consecutive_failures
        == 1
    )


def test_pause_preserves_and_resume_restores_user_notes(
    tmp_path, quiet_ticker_module
):
    """Notes are prompt input and user instructions (review MEDIUM): the
    pause PREPENDS its reason and resume strips it, so a resumed briefing
    still carries its brief."""
    ticker, agent = _make_ticker(tmp_path, alert_after=0, pause_after=1)
    todo = _add_recurring_todo(agent)
    with agent.todo_manager.atomic_update("owner") as todo_list:
        todo_list.update_item(todo.id, notes="Include weather; skip weekends")
    agent.astream = _failing_astream()

    _run_one_occurrence(ticker, _entry_for(agent, todo))

    current = _get_todo(agent, todo.id)
    assert (current.notes or "").startswith("[auto-paused after 1 consecutive")
    assert "Include weather; skip weekends" in (current.notes or "")

    with agent.todo_manager.atomic_update("owner") as todo_list:
        todo_list.update_item(
            todo.id,
            scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    assert _get_todo(agent, todo.id).notes == "Include weather; skip weekends"


# ------------------------------------------------------------------
# Behavior 6: 0 disables a stage
# ------------------------------------------------------------------


def test_zero_thresholds_disable_alert_and_pause(
    tmp_path, quiet_ticker_module
):
    alerts = quiet_ticker_module
    ticker, agent = _make_ticker(tmp_path, alert_after=0, pause_after=0)
    todo = _add_recurring_todo(agent)
    agent.astream = _failing_astream()

    for _ in range(4):
        _run_one_occurrence(ticker, _entry_for(agent, todo))

    current = _get_todo(agent, todo.id)
    assert current.consecutive_failures == 4  # counting still happens
    assert current.schedule_paused_at is None
    assert current.scheduled_for is not None  # still re-arming
    assert alerts == []


# ------------------------------------------------------------------
# Behavior 7: non-recurring failures unchanged
# ------------------------------------------------------------------


def test_non_recurring_failure_keeps_todays_behavior(
    tmp_path, quiet_ticker_module
):
    alerts = quiet_ticker_module
    ticker, agent = _make_ticker(tmp_path)
    with agent.todo_manager.atomic_update("owner") as todo_list:
        todo = todo_list.add_item(
            "One-shot task",
            scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
            thread_id="thread-1",
            created_by="user",
        )
    agent.todo_manager.sync_schedule_to_db("owner", todo.id, agent._schedule_db)
    agent.astream = _failing_astream()

    _run_one_occurrence(ticker, _entry_for(agent, todo))

    current = _get_todo(agent, todo.id)
    assert current.consecutive_failures == 0
    assert current.last_failure is None
    assert current.schedule_paused_at is None
    assert current.scheduled_for is None  # schedule cleared, as before
    assert "failed after" in (current.notes or "")
    assert alerts == []


def test_workflow_todo_success_resets_streak(tmp_path, quiet_ticker_module):
    """The workflow success site runs the same reset as the agent site."""

    class _FlippableWorkflowExecutor:
        def __init__(self):
            self.envelope = {
                "ok": False,
                "status": "error",
                "error": {"kind": "runtime", "message": "script crashed"},
            }

        async def run_workflow(self, workflow_id, params=None, **kw):
            return dict(self.envelope)

    ticker, agent = _make_ticker(tmp_path)
    with agent.todo_manager.atomic_update("owner") as todo_list:
        todo = todo_list.add_item(
            "Nightly report",
            scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
            thread_id="thread-1",
            created_by="user",
            recurrence="1d",
            workflow_id="wf_report",
        )
    assert todo is not None
    agent.todo_manager.sync_schedule_to_db("owner", todo.id, agent._schedule_db)
    executor = _FlippableWorkflowExecutor()
    ticker._turn_executor = executor

    _run_one_occurrence(ticker, _entry_for(agent, todo))
    assert _get_todo(agent, todo.id).consecutive_failures == 1

    executor.envelope = {"ok": True, "status": "ok"}
    ticker._execute_scheduled_todo(_entry_for(agent, todo))

    current = _get_todo(agent, todo.id)
    assert current.consecutive_failures == 0
    assert current.last_failure is None


# ------------------------------------------------------------------
# Behavior 9: fault isolation and the alert helper contract
# ------------------------------------------------------------------


def test_ticker_survives_a_raising_alert_dispatch(tmp_path, monkeypatch):
    """A raising alert cannot break the occurrence: the streak records and
    the schedule still re-arms (the failure handler's caller catches, but
    the policy writes happen before the alert fires)."""
    monkeypatch.setattr(
        ticker_module, "publish_autonomous_event", lambda event_type, **kw: None
    )
    monkeypatch.setattr(
        ticker_module, "publish_agent_stream_chunk", lambda *a, **kw: None
    )
    monkeypatch.setattr(ticker_module, "log_activity", lambda *a, **kw: None)
    monkeypatch.setattr(
        ticker_module, "create_autonomous_notification", lambda **kw: None
    )

    def boom(*a, **kw):
        raise RuntimeError("alert path down")

    monkeypatch.setattr(ticker_module, "send_owner_alert", boom)

    ticker, agent = _make_ticker(tmp_path, alert_after=1, pause_after=0)
    todo = _add_recurring_todo(agent)
    agent.astream = _failing_astream()

    _run_one_occurrence(ticker, _entry_for(agent, todo))  # must not raise

    current = _get_todo(agent, todo.id)
    assert current.consecutive_failures == 1
    assert current.scheduled_for is not None  # re-armed despite the raise


def test_send_owner_alert_rides_the_profile_composite_ungated(
    monkeypatch, tmp_path
):
    """The alert is ONE send_via_profile call (external destinations plus
    the in-app row with delivery badges and the live event), with NO
    in_app_level passed, so the in-app half can never be silenced by the
    autonomous-notification gating."""
    calls: list[dict] = []

    monkeypatch.setattr(
        dispatch_module,
        "send_via_profile",
        lambda **kw: calls.append(kw),
    )

    settings = FakeSettings()
    settings.data_dir = tmp_path
    dispatch_module.send_owner_alert(
        "task failing", settings, user_id="owner", thread_id="t", task_id="x"
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["message"] == "task failing"
    assert call["user_id"] == "owner"
    assert call["thread_id"] == "t"
    assert call["task_id"] == "x"
    # The ungated property: no in_app_level rides the call, so
    # send_via_profile's in-app half always lands.
    assert "in_app_level" not in call


def test_send_owner_alert_never_raises(monkeypatch, tmp_path):
    def boom(**kw):
        raise RuntimeError("delivery infrastructure down")

    monkeypatch.setattr(dispatch_module, "send_via_profile", boom)

    settings = FakeSettings()
    settings.data_dir = tmp_path
    # The contract the ticker and trigger call sites rely on.
    dispatch_module.send_owner_alert(
        "alert", settings, user_id="owner", thread_id="t", task_id="x"
    )


# ------------------------------------------------------------------
# Behavior 10: workflow TODOs ride the same policy
# ------------------------------------------------------------------


def test_workflow_todo_failure_rides_the_policy(tmp_path, quiet_ticker_module):
    class _FailingWorkflowExecutor:
        async def run_workflow(self, workflow_id, params=None, **kw):
            return {
                "ok": False,
                "status": "error",
                "error": {"kind": "runtime", "message": "script crashed"},
            }

    ticker, agent = _make_ticker(tmp_path)
    with agent.todo_manager.atomic_update("owner") as todo_list:
        todo = todo_list.add_item(
            "Nightly report",
            scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
            thread_id="thread-1",
            created_by="user",
            recurrence="1d",
            workflow_id="wf_report",
        )
    agent.todo_manager.sync_schedule_to_db("owner", todo.id, agent._schedule_db)
    ticker._turn_executor = _FailingWorkflowExecutor()

    _run_one_occurrence(ticker, _entry_for(agent, todo))

    current = _get_todo(agent, todo.id)
    assert current.consecutive_failures == 1
    assert "script crashed" in (current.last_failure or "")
    assert current.scheduled_for is not None  # re-armed


# ------------------------------------------------------------------
# Behavior 8: trigger action failures feed the health machinery
# ------------------------------------------------------------------


class _WorkflowExecutorStub:
    is_remote = False  # wrap_for_stream duck-check: pass through unwrapped

    def __init__(self, envelope):
        self.envelope = envelope

    async def run_workflow(self, workflow_id, params=None, **kw):
        return dict(self.envelope)


def _add_action_trigger(manager: TriggerManager, user_id: str = "owner"):
    # Direct store append: add_trigger validates source registration, which
    # this bare test environment does not provide, and the policy under test
    # only needs the trigger to EXIST in the store.
    trigger = TriggerDefinition(
        id="trig-1",
        name="Mail sweep",
        source_type="webhook",
        source_config={},
        action=TriggerAction(
            type="run_workflow", config={"workflow_id": "wf_demo"}
        ),
        thread_id="trig-thread",
        enabled=True,
    )
    with manager.atomic_update(user_id) as store:
        store.triggers.append(trigger)
    return trigger


@pytest.fixture
def trigger_alerts(monkeypatch):
    alerts: list[dict] = []
    monkeypatch.setattr(
        dispatch_module,
        "send_owner_alert",
        lambda message, settings, **kw: alerts.append(
            {"message": message, **kw}
        ),
    )
    import nymeria.config as config_pkg

    monkeypatch.setattr(config_pkg, "get_settings", lambda: FakeSettings())
    return alerts


def _fire_failing(manager, trigger, monkeypatch):
    from nymeria.core.workflows import tool_runtime as tool_runtime_module

    monkeypatch.setattr(
        tool_runtime_module, "workflow_declares_event", lambda _wf: False
    )
    executor = _WorkflowExecutorStub(
        {"ok": False, "status": "error", "error": {"kind": "x", "message": "kaput"}}
    )
    manager.fire_action(trigger, {}, executor, "owner")


def _fire_succeeding(manager, trigger, monkeypatch):
    from nymeria.core.workflows import tool_runtime as tool_runtime_module

    monkeypatch.setattr(
        tool_runtime_module, "workflow_declares_event", lambda _wf: False
    )
    executor = _WorkflowExecutorStub({"ok": True, "status": "ok"})
    manager.fire_action(trigger, {}, executor, "owner")


def test_trigger_action_failures_alert_then_pause(
    tmp_path, monkeypatch, trigger_alerts
):
    """#264 behaviors 1 + 2: alert once at the alert threshold, auto-pause
    at the pause threshold.

    Supersedes the pre-#264 pin (one alert on the health transition into
    "failing", nothing else ever). The action plane no longer sends that
    transition alert: it would be the second of three notifications for one
    broken trigger. The health LABELS still move exactly as before.
    """
    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)

    _fire_failing(manager, trigger, monkeypatch)
    assert trigger_alerts == []  # one failure is not an episode

    _fire_failing(manager, trigger, monkeypatch)
    stored = _get_trigger(manager, trigger.id)
    assert stored.action_failures == 2
    assert stored.consecutive_errors == 2
    assert stored.health_status == "degraded"
    assert "kaput" in (stored.last_error or "")
    assert stored.auto_paused_at is None  # alerting is not stopping
    assert len(trigger_alerts) == 1
    assert "[TRIGGER ALERT]" in trigger_alerts[0]["message"]
    assert "Mail sweep" in trigger_alerts[0]["message"]

    # Failures 3 and 4 are inside the same episode: no further alerts.
    for _ in range(2):
        _fire_failing(manager, trigger, monkeypatch)
    assert len(trigger_alerts) == 1

    # The fifth crosses the pause threshold.
    _fire_failing(manager, trigger, monkeypatch)
    stored = _get_trigger(manager, trigger.id)
    assert stored.action_failures == 5
    assert stored.health_status == "failing"
    assert stored.auto_paused_at is not None
    assert stored.enabled is True  # the policy never touches the user's toggle
    assert len(trigger_alerts) == 2
    assert "[TRIGGER PAUSED]" in trigger_alerts[1]["message"]
    assert "resume" in trigger_alerts[1]["message"].lower()


def test_paused_trigger_does_not_stack_a_second_pause_or_alert(
    tmp_path, monkeypatch, trigger_alerts
):
    """A fire already in flight when the pause landed must not re-pause.

    The poll loop and the webhook route both refuse a paused trigger, so
    the only way to reach _record_action_health while paused is that race.
    """
    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)
    for _ in range(5):
        _fire_failing(manager, trigger, monkeypatch)
    paused_at = _get_trigger(manager, trigger.id).auto_paused_at
    assert paused_at is not None
    assert len(trigger_alerts) == 2

    _fire_failing(manager, trigger, monkeypatch)
    stored = _get_trigger(manager, trigger.id)
    assert stored.auto_paused_at == paused_at  # not re-stamped
    assert len(trigger_alerts) == 2  # and not re-alerted


def test_trigger_action_success_ends_the_episode(
    tmp_path, monkeypatch, trigger_alerts
):
    """#264 behavior 6: a success resets the streak, so the next failure
    starts counting from one rather than resuming mid-episode."""
    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)

    for _ in range(4):
        _fire_failing(manager, trigger, monkeypatch)
    assert _get_trigger(manager, trigger.id).action_failures == 4
    assert len(trigger_alerts) == 1

    _fire_succeeding(manager, trigger, monkeypatch)
    stored = _get_trigger(manager, trigger.id)
    assert stored.action_failures == 0
    assert stored.consecutive_errors == 0
    assert stored.health_status == "healthy"

    # Four more failures would have paused a trigger mid-episode; from a
    # clean streak they only re-alert.
    for _ in range(4):
        _fire_failing(manager, trigger, monkeypatch)
    stored = _get_trigger(manager, trigger.id)
    assert stored.auto_paused_at is None
    assert len(trigger_alerts) == 2


def test_trigger_policy_thresholds_of_zero_disable_each_stage(
    tmp_path, monkeypatch, trigger_alerts
):
    """#264 behavior 8: 0 disables that stage, matching #154's knobs."""
    import nymeria.config as config_pkg

    settings = FakeSettings()
    settings.trigger_failure_alert_after = 0
    settings.trigger_failure_pause_after = 0
    monkeypatch.setattr(config_pkg, "get_settings", lambda: settings)

    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)
    for _ in range(8):
        _fire_failing(manager, trigger, monkeypatch)

    stored = _get_trigger(manager, trigger.id)
    assert stored.action_failures == 8
    assert stored.auto_paused_at is None
    assert trigger_alerts == []


def test_a_flapping_trigger_alerts_once_then_falls_under_the_cooldown(
    tmp_path, monkeypatch, trigger_alerts
):
    """A trigger that fails twice, succeeds, and repeats never reaches the
    pause threshold (each success resets the streak), so without a cooldown
    it would alert once per episode forever on external destinations that
    deliberately bypass the in-app notification level.
    """
    import nymeria.config as config_pkg

    settings = FakeSettings()
    settings.trigger_failure_alert_cooldown_minutes = 180
    monkeypatch.setattr(config_pkg, "get_settings", lambda: settings)

    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)

    for _ in range(4):  # four flap cycles
        _fire_failing(manager, trigger, monkeypatch)
        _fire_failing(manager, trigger, monkeypatch)
        _fire_succeeding(manager, trigger, monkeypatch)

    stored = _get_trigger(manager, trigger.id)
    assert stored.auto_paused_at is None  # it never reaches the pause count
    assert len(trigger_alerts) == 1  # and it only spoke once

    # The stamp is NOT cleared by the successes, which is what makes the
    # cooldown survive a flap at all.
    assert stored.last_policy_alert_at is not None

    # A resume is a fresh start: the next episode may speak again.
    manager.resume_trigger("owner", trigger.id)
    assert _get_trigger(manager, trigger.id).last_policy_alert_at is None
    _fire_failing(manager, trigger, monkeypatch)
    _fire_failing(manager, trigger, monkeypatch)
    assert len(trigger_alerts) == 2


def test_the_pause_alert_is_never_suppressed_by_the_cooldown(
    tmp_path, monkeypatch, trigger_alerts
):
    """A pause is terminal: the owner has to be told the trigger stopped,
    even moments after an episode alert."""
    import nymeria.config as config_pkg

    settings = FakeSettings()
    settings.trigger_failure_alert_cooldown_minutes = 180
    monkeypatch.setattr(config_pkg, "get_settings", lambda: settings)

    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)

    for _ in range(5):
        _fire_failing(manager, trigger, monkeypatch)

    stored = _get_trigger(manager, trigger.id)
    assert stored.auto_paused_at is not None
    assert len(trigger_alerts) == 2  # the episode alert AND the pause alert
    assert "[TRIGGER PAUSED]" in trigger_alerts[1]["message"]


def test_a_batch_stops_firing_once_it_crosses_the_pause_threshold(
    tmp_path, monkeypatch, trigger_alerts
):
    """A per-event batch must not keep acting after the policy said stop.

    Non-agent_prompt actions fire once per event, so a batch can cross the
    threshold part-way. Firing the rest would act past the pause, overshoot
    the count the pause reports, and let a late success in the same batch
    zero the streak (reporting "auto-paused after 0 failed actions").
    """
    import nymeria.config as config_pkg

    settings = FakeSettings()
    settings.trigger_failure_pause_after = 3
    monkeypatch.setattr(config_pkg, "get_settings", lambda: settings)

    manager = TriggerManager(tmp_path)
    trigger = TriggerDefinition(
        id="trig-batch",
        name="Feed fanout",
        source_type="webhook",
        source_config={},
        action=TriggerAction(
            type="notify", config={"message_template": "New: {title}"}
        ),
        thread_id="trig-thread",
        enabled=True,
    )
    with manager.atomic_update("owner") as store:
        store.triggers.append(trigger)

    fired: list[dict] = []

    def _boom(*a, **kw):
        fired.append(kw)
        raise RuntimeError("notify down")

    monkeypatch.setattr(manager, "_fire_notify", _boom)
    monkeypatch.setattr(manager, "_publish_trigger_error", lambda *a, **kw: None)

    events = [{"title": f"item {i}"} for i in range(10)]
    manager.fire_action_batch(trigger, events, object(), "owner")

    stored = _get_trigger(manager, "trig-batch")
    assert stored.auto_paused_at is not None
    # Exactly the threshold: it stopped on the fire that paused it, and the
    # reported count is the one that actually caused the pause.
    assert stored.action_failures == 3
    assert len(fired) == 3


def test_unpersisted_failure_drives_no_alert_and_no_pause(
    tmp_path, monkeypatch, trigger_alerts
):
    """#264 behavior 9: unpersisted state must not drive policy.

    The #247 delivery policy states the rule (delivery_accounting.py): a
    streak that could not be saved reports 0 and the alert/pause branches
    no-op. Here the same outcome comes from atomic_update raising on a
    failed save, which leaves _record_action_health through its except
    before any alert is sent, and discards the mutated store (the manager
    re-reads from disk, it holds no cache). Either way an owner must never
    be told a trigger was paused when the pause did not survive.
    """
    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)
    for _ in range(4):
        _fire_failing(manager, trigger, monkeypatch)
    assert len(trigger_alerts) == 1  # the episode alert landed while saves worked

    monkeypatch.setattr(manager, "_save", lambda store: False)

    # The fire that would have crossed the pause threshold.
    _fire_failing(manager, trigger, monkeypatch)

    assert len(trigger_alerts) == 1  # no pause alert for a pause that did not persist
    monkeypatch.undo()
    stored = _get_trigger(manager, trigger.id)
    assert stored.auto_paused_at is None
    assert stored.action_failures == 4  # the increment did not survive either


def test_alert_threshold_of_zero_is_silent_while_pause_still_fires(
    tmp_path, monkeypatch, trigger_alerts
):
    """#264 behavior 8, alert half. Separated from the both-zero case so the
    alert guard is pinned on its own: with both at 0 the equality test is
    unreachable anyway, so that case cannot tell a working guard from a
    missing one."""
    import nymeria.config as config_pkg

    settings = FakeSettings()
    settings.trigger_failure_alert_after = 0
    settings.trigger_failure_pause_after = 3
    monkeypatch.setattr(config_pkg, "get_settings", lambda: settings)

    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)

    for _ in range(2):
        _fire_failing(manager, trigger, monkeypatch)
    assert trigger_alerts == []  # no episode alert at any count

    _fire_failing(manager, trigger, monkeypatch)
    assert _get_trigger(manager, trigger.id).auto_paused_at is not None
    assert len(trigger_alerts) == 1  # the pause still speaks
    assert "[TRIGGER PAUSED]" in trigger_alerts[0]["message"]


def test_paused_trigger_is_skipped_by_the_poll_loop(
    tmp_path, monkeypatch, trigger_alerts
):
    """#264 behavior 3: a paused trigger's SOURCE is never checked.

    Load-bearing beyond "it does not fire": an Outlook check tags mail read
    before its action ever runs (#265), so a paused trigger that still
    polled would keep consuming and dropping events.

    Pause threshold 2, deliberately BELOW the health machinery's own
    "failing" threshold of 5. At 2 failures the trigger is only "degraded",
    so the pre-existing failing-backoff does not skip it and the pause is
    the sole reason the poll passes it over. Pausing at 5 would leave this
    test green even with the pause check deleted, because the backoff would
    be doing the skipping (measured: it was).
    """
    import nymeria.config as config_pkg

    settings = FakeSettings()
    settings.trigger_failure_pause_after = 2
    monkeypatch.setattr(config_pkg, "get_settings", lambda: settings)

    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)
    source = _wire_source(monkeypatch, _QuietSource())

    manager.check_triggers("owner")
    assert source.checks == 1  # healthy: polled

    for _ in range(2):
        _fire_failing(manager, trigger, monkeypatch)
    stored = _get_trigger(manager, trigger.id)
    assert stored.auto_paused_at is not None
    assert stored.health_status == "degraded"  # NOT failing: no backoff here

    for _ in range(3):
        assert manager.check_triggers("owner") == []
    assert source.checks == 1  # paused: not polled again


def test_resume_clears_the_pause_and_the_whole_failure_history(
    tmp_path, monkeypatch, trigger_alerts
):
    """#264 behaviors 12 + 15: resume is the repair verb, and it leaves the
    user's own enable toggle alone."""
    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)
    source = _wire_source(monkeypatch, _QuietSource())
    for _ in range(5):
        _fire_failing(manager, trigger, monkeypatch)

    summary = manager.resume_trigger("owner", trigger.id)
    assert summary is not None
    assert summary["was_paused"] is True
    assert summary["action_failures"] == 5

    stored = _get_trigger(manager, trigger.id)
    assert stored.auto_paused_at is None
    assert stored.action_failures == 0
    assert stored.consecutive_errors == 0
    assert stored.health_status == "healthy"
    assert stored.last_error is None
    assert stored.last_error_kind is None
    assert stored.enabled is True

    manager.check_triggers("owner")
    assert source.checks == 1  # polling again


def test_resume_clears_health_on_a_failing_but_unpaused_trigger(
    tmp_path, monkeypatch, trigger_alerts
):
    """#264 behavior 13: the incident's own recovery step. A trigger whose
    cause you already fixed sat at failing/N and kept skipping 9 of 10
    polls; before resume existed, hand-editing the store was the only way
    to clear it."""
    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)
    _wire_source(monkeypatch, _BrokenSource())
    for _ in range(5):
        manager.check_triggers("owner")
    stored = _get_trigger(manager, trigger.id)
    assert stored.health_status == "failing"
    assert stored.auto_paused_at is None  # source plane never pauses

    summary = manager.resume_trigger("owner", trigger.id)
    assert summary is not None
    assert summary["was_paused"] is False
    stored = _get_trigger(manager, trigger.id)
    assert stored.consecutive_errors == 0
    assert stored.health_status == "healthy"


def test_resume_on_an_unknown_trigger_reports_nothing_cleared(tmp_path):
    """#264 behavior 14: the caller can tell 'repaired' from 'no such
    trigger' rather than getting a cheerful no-op."""
    manager = TriggerManager(tmp_path)
    assert manager.resume_trigger("owner", "nope-123") is None


def test_source_failures_never_auto_pause(
    tmp_path, monkeypatch, trigger_alerts
):
    """#264 behavior 7, the ratified plane split: a source outage backs off
    and self-heals, so a long outage must never end with a stopped trigger
    the owner has to resume by hand."""
    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)
    _wire_source(monkeypatch, _BrokenSource())

    for _ in range(40):
        manager.check_triggers("owner")

    stored = _get_trigger(manager, trigger.id)
    assert stored.health_status == "failing"
    assert stored.consecutive_errors >= 5
    assert stored.auto_paused_at is None
    assert stored.action_failures == 0  # the action plane never moved


def test_trigger_batched_agent_turn_failure_feeds_health(
    tmp_path, monkeypatch, trigger_alerts
):
    """The incident-shaped site: a batched agent_prompt fire whose turn
    raises records health exactly like a single-event action failure."""
    manager = TriggerManager(tmp_path)
    trigger = TriggerDefinition(
        id="trig-2",
        name="Inbox sweep",
        source_type="webhook",
        source_config={},
        action=TriggerAction(
            type="agent_prompt", config={"prompt_template": "Handle {body}"}
        ),
        thread_id="trig-thread",
        enabled=True,
    )
    with manager.atomic_update("owner") as store:
        store.triggers.append(trigger)

    def dead_turn(*a, **kw):
        raise RuntimeError("turn died mid-stream")

    monkeypatch.setattr(manager, "_stream_live", dead_turn)
    monkeypatch.setattr(
        manager, "_publish_trigger_error", lambda *a, **kw: None
    )

    manager.fire_action_batch(
        trigger, [{"body": "a"}, {"body": "b"}], object(), "owner"
    )

    stored = _get_trigger(manager, trigger.id)
    assert stored.consecutive_errors == 1
    # The POLICY counter, not just the health label: this batched
    # agent-turn site is the incident-shaped one.
    assert stored.action_failures == 1
    assert "turn died" in (stored.last_error or "")


def test_trigger_action_success_resets_health(
    tmp_path, monkeypatch, trigger_alerts
):
    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)

    for _ in range(3):
        _fire_failing(manager, trigger, monkeypatch)
    assert _get_trigger(manager, trigger.id).consecutive_errors == 3

    _fire_succeeding(manager, trigger, monkeypatch)
    stored = _get_trigger(manager, trigger.id)
    assert stored.consecutive_errors == 0
    assert stored.health_status == "healthy"
    assert stored.last_error is None


class _QuietSource:
    """A poll source that always succeeds with no events (the common cycle)."""

    def __init__(self):
        self.checks = 0

    def check(self, source_config, state, user_id=""):
        self.checks += 1
        return []


class _BrokenSource(_QuietSource):
    def check(self, source_config, state, user_id=""):
        self.checks += 1
        raise RuntimeError("feed down")


def _wire_source(monkeypatch, source):
    from nymeria.triggers import sources as sources_module

    monkeypatch.setattr(sources_module, "get_source", lambda name: source)
    return source


def test_action_streak_survives_successful_source_polls(
    tmp_path, monkeypatch, trigger_alerts
):
    """The production-shaped loop (review HIGH): source checks succeed on
    EVERY poll cycle, so if a source success healed an action streak the
    counter would oscillate 0-1 forever and the feature would be inert."""
    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)
    source = _wire_source(monkeypatch, _QuietSource())

    for _ in range(5):
        manager.check_triggers("owner")  # the 30s poll, source succeeds
        _fire_failing(manager, trigger, monkeypatch)

    assert source.checks == 5  # the polls really ran between failures
    stored = _get_trigger(manager, trigger.id)
    assert stored.consecutive_errors == 5
    assert stored.health_status == "failing"
    # #264 behavior 11: the ACTION streak is the policy's counter and the
    # successful source polls did not touch it either, so it reached the
    # pause threshold. Two alerts: the episode alert, then the pause.
    assert stored.action_failures == 5
    assert stored.auto_paused_at is not None
    assert len(trigger_alerts) == 2


def test_source_streak_still_heals_on_source_success(
    tmp_path, monkeypatch, trigger_alerts
):
    """The pre-existing reset is preserved for the source plane: a
    recovered source goes back to healthy on its first good check."""
    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)
    _wire_source(monkeypatch, _BrokenSource())
    for _ in range(3):
        manager.check_triggers("owner")
    stored = _get_trigger(manager, trigger.id)
    assert stored.consecutive_errors == 3
    assert stored.health_status == "degraded"

    _wire_source(monkeypatch, _QuietSource())
    manager.check_triggers("owner")
    stored = _get_trigger(manager, trigger.id)
    assert stored.consecutive_errors == 0
    assert stored.health_status == "healthy"


def test_source_crossing_into_failing_alerts_once_and_backoff_lapses(
    tmp_path, monkeypatch, trigger_alerts
):
    """A source-driven crossing alerts too (the alert is a property of the
    transition, whichever plane crosses), and the failing backoff LAPSES:
    pre-#154 the counter froze at 5-9 and skipped every future check
    forever, making "failing" permanent."""
    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)
    broken = _wire_source(monkeypatch, _BrokenSource())

    for _ in range(5):
        manager.check_triggers("owner")
    stored = _get_trigger(manager, trigger.id)
    assert stored.health_status == "failing"
    assert len(trigger_alerts) == 1
    checks_at_failing = broken.checks

    for _ in range(10):
        manager.check_triggers("owner")
    assert broken.checks > checks_at_failing  # the check ran again
    assert len(trigger_alerts) == 1  # still one alert for the episode


def test_trigger_alert_failure_does_not_break_the_fire(
    tmp_path, monkeypatch
):
    """Health bookkeeping (alert included) is fault-isolated from the fire."""

    def boom(*a, **kw):
        raise RuntimeError("alert path down")

    monkeypatch.setattr(dispatch_module, "send_owner_alert", boom)
    import nymeria.config as config_pkg

    monkeypatch.setattr(config_pkg, "get_settings", lambda: FakeSettings())

    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)
    for _ in range(5):  # crosses into failing, alert raises, fire survives
        _fire_failing(manager, trigger, monkeypatch)

    stored = _get_trigger(manager, trigger.id)
    assert stored.health_status == "failing"
    # The property that matters most under a broken alert plane: the PAUSE
    # still landed. An alert sender that raises must not cost the trigger
    # its stop, or a dead notification channel silently restores the
    # unbounded-consumption behavior #264 exists to end.
    assert stored.auto_paused_at is not None
    assert stored.action_failures == 5
    executions = manager.get_executions("owner")
    assert len(executions) == 5


def test_pause_banner_with_brackets_strips_clean_on_resume(
    tmp_path, quiet_ticker_module
):
    """A pause reason containing ']' (e.g. a KeyError repr) must not break
    the resume-clear's banner strip: the banner body is sanitized so the
    first ']' always terminates the banner, and resume leaves the user's own
    notes exactly as they were."""
    ticker, agent = _make_ticker(tmp_path, alert_after=0, pause_after=1)
    todo = _add_recurring_todo(agent)
    with agent.todo_manager.atomic_update("owner") as todo_list:
        assert todo_list.update_item(todo.id, notes="med instructions")
    agent.astream = _failing_astream("KeyError['dose'] boom")

    _run_one_occurrence(ticker, _entry_for(agent, todo))

    paused = _get_todo(agent, todo.id)
    assert paused.schedule_paused_at is not None
    assert (paused.notes or "").startswith("[auto-paused after")
    assert "med instructions" in (paused.notes or "")

    with agent.todo_manager.atomic_update("owner") as todo_list:
        assert todo_list.update_item(
            todo.id,
            scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1),
        )

    resumed = _get_todo(agent, todo.id)
    assert resumed.schedule_paused_at is None
    assert resumed.notes == "med instructions"  # no banner residue


# --- #306: a permanently dead SOURCE keeps reminding ------------------------


def _settings_with_cooldown(monkeypatch, minutes: int):
    """Install FakeSettings with a real (non-zero) alert cooldown."""
    import nymeria.config as config_pkg

    settings = FakeSettings()
    settings.trigger_failure_alert_cooldown_minutes = minutes
    monkeypatch.setattr(config_pkg, "get_settings", lambda: settings)
    return settings


def _drive_to_failing(manager, monkeypatch):
    """Take a trigger across the crossing into "failing" on the source plane."""
    trigger = _add_action_trigger(manager)
    broken = _wire_source(monkeypatch, _BrokenSource())
    for _ in range(5):
        manager.check_triggers("owner")
    assert _get_trigger(manager, trigger.id).health_status == "failing"
    return trigger, broken


def test_a_dead_source_stays_quiet_within_the_cooldown(
    tmp_path, monkeypatch, trigger_alerts
):
    """The ratified one-alert-per-episode behavior is unchanged in the short
    run: a source failing for less than one cooldown says nothing further."""
    _settings_with_cooldown(monkeypatch, 180)
    manager = TriggerManager(tmp_path)
    trigger, broken = _drive_to_failing(manager, monkeypatch)
    assert len(trigger_alerts) == 1

    checks_at_failing = broken.checks
    for _ in range(20):
        manager.check_triggers("owner")

    assert broken.checks > checks_at_failing  # the backoff still lapses
    assert len(trigger_alerts) == 1
    assert _get_trigger(manager, trigger.id).auto_paused_at is None


def test_a_dead_source_re_alerts_once_the_cooldown_elapses(
    tmp_path, monkeypatch, trigger_alerts
):
    """#306: a source that will never recover used to alert once and then
    poll silently forever. It now repeats the reminder one cooldown apart,
    with copy that reads as a reminder rather than as fresh news, and still
    never auto-pauses (the ratified source-plane rule)."""
    _settings_with_cooldown(monkeypatch, 180)
    manager = TriggerManager(tmp_path)
    trigger, _broken = _drive_to_failing(manager, monkeypatch)
    assert len(trigger_alerts) == 1
    assert "[TRIGGER FAILING]" in trigger_alerts[0]["message"]

    # Four hours pass with the source still dead.
    manager.update_trigger(
        "owner",
        trigger.id,
        last_source_alert_at=utc_now() - timedelta(hours=4),
    )
    for _ in range(10):
        manager.check_triggers("owner")

    assert len(trigger_alerts) == 2
    repeat = trigger_alerts[1]["message"]
    assert "[TRIGGER STILL FAILING]" in repeat
    assert "feed down" in repeat
    # The repeat must not send the owner to `resume`: on a genuinely dead
    # source that just fails its way straight back to "failing".
    assert "resume" not in repeat
    assert "disable" in repeat

    # Still never paused, and the fresh stamp re-arms the next cooldown.
    # The stamp is the SOURCE plane's own; the action plane's is untouched,
    # so a source episode cannot swallow #264's action alert.
    stored = _get_trigger(manager, trigger.id)
    assert stored.auto_paused_at is None
    assert stored.last_source_alert_at is not None
    assert stored.last_policy_alert_at is None
    for _ in range(10):
        manager.check_triggers("owner")
    assert len(trigger_alerts) == 2


def test_a_zero_cooldown_restores_one_alert_per_episode(
    tmp_path, monkeypatch, trigger_alerts
):
    """Zero disables the reminder rather than making it infinitely fast:
    "re-alert every 0 minutes" has no sane reading, and zero already means
    "disable this stage" on the action plane."""
    _settings_with_cooldown(monkeypatch, 0)
    manager = TriggerManager(tmp_path)
    trigger, _broken = _drive_to_failing(manager, monkeypatch)

    manager.update_trigger(
        "owner",
        trigger.id,
        last_source_alert_at=utc_now() - timedelta(days=30),
    )
    for _ in range(20):
        manager.check_triggers("owner")

    assert len(trigger_alerts) == 1


def test_an_unregistered_source_degrades_instead_of_reading_healthy(
    tmp_path, monkeypatch, trigger_alerts
):
    """A trigger whose source plugin is gone from the build used to be
    skipped with a log line and nothing else, so it sat reading "healthy",
    fired nothing, and told nobody, forever. It is the one dead-source shape
    that cannot recover on its own, so it must be visible."""
    _settings_with_cooldown(monkeypatch, 180)
    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)
    _wire_source(monkeypatch, None)

    for _ in range(5):
        manager.check_triggers("owner")

    stored = _get_trigger(manager, trigger.id)
    assert stored.health_status == "failing"
    assert stored.last_error_kind == "source"
    assert "not registered" in (stored.last_error or "")
    assert len(trigger_alerts) == 1
    assert "[TRIGGER FAILING]" in trigger_alerts[0]["message"]


def test_a_source_alert_does_not_consume_the_action_plane_cooldown(
    tmp_path, monkeypatch, trigger_alerts
):
    """The two planes must not share an alert cooldown (#306 review finding).

    Sharing `last_policy_alert_at` looks free because a failing source means
    the action never runs. But the shared quantity is a THREE-HOUR WINDOW,
    not an instant: a source can blip, recover, and then the action starts
    failing well inside that window. #264's alert gate is
    `count == alert_after` exactly, so a suppressed alert is DROPPED, not
    delayed, and the owner loses the early warning entirely until the pause.
    """
    _settings_with_cooldown(monkeypatch, 180)
    manager = TriggerManager(tmp_path)
    trigger, _broken = _drive_to_failing(manager, monkeypatch)
    assert len(trigger_alerts) == 1  # the source spoke

    # The source recovers. Takes several cycles because a failing trigger
    # only really polls every 10th one.
    _wire_source(monkeypatch, _QuietSource())
    for _ in range(10):
        manager.check_triggers("owner")
    assert _get_trigger(manager, trigger.id).health_status == "healthy"

    # Now the ACTION fails twice, well inside the source alert's window.
    _fire_failing(manager, trigger, monkeypatch)
    _fire_failing(manager, trigger, monkeypatch)

    # #264's early-warning alert must still arrive.
    assert len(trigger_alerts) == 2
    assert "[TRIGGER ALERT]" in trigger_alerts[1]["message"]


def test_the_first_source_alert_never_says_still_failing(
    tmp_path, monkeypatch, trigger_alerts
):
    """The action plane can drive health to "failing" on its own, so
    `became_failing` alone cannot decide the wording: the first thing an
    owner hears about a source must not be the word "STILL"."""
    settings = _settings_with_cooldown(monkeypatch, 180)
    # Auto-pause off, or the fifth action failure pauses the trigger and the
    # poll loop stops visiting it at all, which is the #264 behavior and not
    # what is under test here.
    settings.trigger_failure_pause_after = 0
    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)

    # Drive health to "failing" purely through ACTION failures.
    _wire_source(monkeypatch, _QuietSource())
    for _ in range(5):
        _fire_failing(manager, trigger, monkeypatch)
    assert _get_trigger(manager, trigger.id).health_status == "failing"
    before = len(trigger_alerts)

    # The source now fails for the first time. Several cycles, because an
    # already-failing trigger polls its source only every 10th one (the
    # backoff reads the SHARED counter, which the action failures moved).
    _wire_source(monkeypatch, _BrokenSource())
    for _ in range(10):
        manager.check_triggers("owner")

    new_alerts = trigger_alerts[before:]
    assert len(new_alerts) == 1
    assert "[TRIGGER FAILING]" in new_alerts[0]["message"]
    assert "STILL" not in new_alerts[0]["message"]
