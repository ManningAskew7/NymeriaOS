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


def test_trigger_action_failures_degrade_health_and_alert_once(
    tmp_path, monkeypatch, trigger_alerts
):
    manager = TriggerManager(tmp_path)
    trigger = _add_action_trigger(manager)

    for _ in range(2):
        _fire_failing(manager, trigger, monkeypatch)
    stored = _get_trigger(manager, trigger.id)
    assert stored.consecutive_errors == 2
    assert stored.health_status == "degraded"
    assert "kaput" in (stored.last_error or "")
    assert trigger_alerts == []

    for _ in range(3):
        _fire_failing(manager, trigger, monkeypatch)
    stored = _get_trigger(manager, trigger.id)
    assert stored.consecutive_errors == 5
    assert stored.health_status == "failing"
    assert len(trigger_alerts) == 1
    assert "Mail sweep" in trigger_alerts[0]["message"]

    # Still failing on the sixth: no second alert for the same episode.
    _fire_failing(manager, trigger, monkeypatch)
    assert len(trigger_alerts) == 1


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
    assert len(trigger_alerts) == 1


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
