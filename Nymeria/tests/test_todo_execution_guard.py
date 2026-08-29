"""Regression tests for active scheduled TODO mutation guards."""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.core.ticker import Ticker
from nymeria.core.todo_manager import TodoManager, TodoStatus
from nymeria.core.todo_schedule_db import ScheduledTodoEntry, TodoScheduleDB
from nymeria.core.trigger_manager import TriggerManager
from nymeria.core.turn_executor import LocalAgentExecutor
from nymeria.core.user_profile import UserProfileManager
from nymeria.core import ticker as ticker_module
from nymeria.api.schemas.settings import ServerSettingsResponse, ServerSettingsUpdate


class FakeThreadConfigManager:
    def get_config(self, thread_id: str):
        return None


class FakeAgent:
    def __init__(self, settings):
        data_dir = settings.data_dir
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.todo_manager = TodoManager(data_dir)
        self._schedule_db = TodoScheduleDB(data_dir / "todo_schedule.db")
        self.trigger_manager = TriggerManager(data_dir)
        self.thread_config_manager = FakeThreadConfigManager()
        self.profile_manager = UserProfileManager(data_dir)
        self.settings = settings

    def sync_agent_tools(self):
        pass

    async def astream(self, *args, **kwargs):
        raise RuntimeError("boom")


def _make_ticker(
    agent: "FakeAgent",
    schedule_db=None,
    *,
    poll_interval: int = Ticker.DEFAULT_POLL_INTERVAL,
) -> Ticker:
    """Build the new-form Ticker from a FakeAgent for these tests.

    Wraps the agent in a LocalAgentExecutor so existing ``agent.astream``
    monkey-patches still drive the ticker. ``busy_agent=agent`` preserves
    the slim-mode behavior these tests originally exercised (the trigger
    busy check and the post-turn sliding-window trim path).
    """
    return Ticker(
        executor=LocalAgentExecutor(agent),
        settings=agent.settings,
        schedule_db=schedule_db if schedule_db is not None else agent._schedule_db,
        todo_manager=agent.todo_manager,
        thread_config_manager=agent.thread_config_manager,
        profile_manager=agent.profile_manager,
        poll_interval=poll_interval,
        busy_agent=agent,
        spawn_sweeper=None,
    )


def _agent(tmp_path: Path, api_client_builder) -> FakeAgent:
    return FakeAgent(api_client_builder.settings(tmp_path))


def _client(tmp_path: Path, api_client_builder) -> tuple[object, FakeAgent, str]:
    settings = api_client_builder.settings(tmp_path)
    api_client_builder.create_checkpoint_table(settings)
    agent = FakeAgent(settings)
    client, token = api_client_builder.authenticated_client(
        agent,
        settings,
        user_id="owner",
        email="owner@example.com",
        display_name="Owner",
    )
    return client, agent, token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _add_todo(agent: FakeAgent, user_id: str = "owner"):
    with agent.todo_manager.atomic_update(user_id) as todo_list:
        return todo_list.add_item(
            "Check the inbox",
            scheduled_for=datetime.now(timezone.utc) + timedelta(minutes=5),
            thread_id="thread-1",
            created_by="user",
        )


def _add_scheduled_todo(
    agent: FakeAgent,
    *,
    task: str,
    scheduled_for: datetime,
    user_id: str = "owner",
):
    with agent.todo_manager.atomic_update(user_id) as todo_list:
        item = todo_list.add_item(
            task,
            scheduled_for=scheduled_for,
            thread_id="thread-1",
            created_by="user",
        )
        assert item is not None
        return item


def _add_recurring_todo(
    agent: FakeAgent,
    *,
    task: str,
    scheduled_for: datetime,
    recurrence: str,
    user_id: str = "owner",
):
    with agent.todo_manager.atomic_update(user_id) as todo_list:
        item = todo_list.add_item(
            task,
            scheduled_for=scheduled_for,
            thread_id="thread-1",
            created_by="user",
            recurrence=recurrence,
        )
        assert item is not None
    agent.todo_manager.sync_schedule_to_db(user_id, item.id, agent._schedule_db)
    return item


def _entry_for(todo, fire_time: datetime) -> ScheduledTodoEntry:
    return ScheduledTodoEntry(
        todo_id=todo.id,
        user_id="owner",
        thread_id=todo.thread_id,
        scheduled_for=fire_time.timestamp(),
        task_preview=todo.task,
        created_at=time.time(),
    )


def test_recurring_todo_rearms_at_next_slot_on_retry_giveup(
    tmp_path: Path, api_client_builder
):
    """A recurring TODO that exhausts its retries skips the failed occurrence
    and re-arms at the next slot instead of having its schedule cleared (which
    would leave recurrence set but scheduled_for null, killing it forever)."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    todo = _add_recurring_todo(
        agent, task="Daily digest", scheduled_for=fire_time, recurrence="1d"
    )
    entry = _entry_for(todo, fire_time)

    for _ in range(Ticker.MAX_RETRIES):
        ticker._handle_execution_failure(
            entry, todo, todo.thread_id, RuntimeError("proxy 503")
        )

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed is not None
    assert refreshed.recurrence == "1d"
    assert refreshed.scheduled_for is not None
    assert refreshed.scheduled_for > fire_time  # advanced to the next slot
    assert refreshed.status == TodoStatus.PENDING
    # Still indexed for polling: the schedule was NOT cleared.
    assert agent._schedule_db.get_entry(todo.id) is not None
    # Retry budget reset so the next occurrence starts fresh.
    assert todo.id not in ticker._retry_counts


def test_oneshot_todo_clears_schedule_on_retry_giveup(
    tmp_path: Path, api_client_builder
):
    """A non-recurring TODO that exhausts its retries clears its schedule and
    is left PENDING with a failure note (unchanged behavior)."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    todo = _add_scheduled_todo(agent, task="One-shot", scheduled_for=fire_time)
    agent.todo_manager.sync_schedule_to_db("owner", todo.id, agent._schedule_db)
    entry = _entry_for(todo, fire_time)

    for _ in range(Ticker.MAX_RETRIES):
        ticker._handle_execution_failure(
            entry, todo, todo.thread_id, RuntimeError("boom")
        )

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed is not None
    assert refreshed.scheduled_for is None
    assert refreshed.recurrence is None
    assert refreshed.status == TodoStatus.PENDING
    assert agent._schedule_db.get_entry(todo.id) is None
    assert "failed after 3 retries" in (refreshed.notes or "")
    assert todo.id not in ticker._retry_counts


def test_handle_recurrence_adopts_and_keeps_month_origin_anchor(
    tmp_path: Path, api_client_builder
):
    """A monthly TODO adopts its first slot as a stable origin and keeps it
    across cycles, so later slots derive from the origin (drift-free) rather
    than the previous clamped slot."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    origin_slot = datetime(2026, 1, 31, 11, 0, tzinfo=timezone.utc)
    todo = _add_recurring_todo(
        agent, task="Month-end report", scheduled_for=origin_slot, recurrence="1mo"
    )

    ticker._handle_recurrence(_entry_for(todo, origin_slot), todo)
    after_first = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert after_first is not None
    assert after_first.recurrence_anchor == origin_slot  # origin adopted
    assert after_first.scheduled_for is not None
    assert after_first.scheduled_for.day >= 28  # a valid month-end clamp

    # Advance again from the (clamped) new slot: the origin must NOT be
    # re-adopted to the clamped slot; it stays pinned to Jan 31.
    second_slot = after_first.scheduled_for
    ticker._handle_recurrence(_entry_for(after_first, second_slot), after_first)
    after_second = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert after_second is not None
    assert after_second.recurrence_anchor == origin_slot
    assert after_second.scheduled_for != second_slot


def test_handle_recurrence_leaves_no_origin_for_fixed_duration(
    tmp_path: Path, api_client_builder
):
    """A fixed-duration (daily) recurrence never adopts a month-origin anchor."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime(2026, 1, 31, 11, 0, tzinfo=timezone.utc)
    todo = _add_recurring_todo(
        agent, task="Daily", scheduled_for=fire_time, recurrence="1d"
    )

    ticker._handle_recurrence(_entry_for(todo, fire_time), todo)

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed is not None
    assert refreshed.recurrence_anchor is None


def test_update_item_resets_recurrence_anchor_on_change(tmp_path: Path, api_client_builder):
    """Changing or clearing the recurrence drops a stale month origin so the
    next fire re-adopts a fresh anchor."""
    agent = _agent(tmp_path, api_client_builder)
    fire_time = datetime(2026, 1, 31, 11, 0, tzinfo=timezone.utc)
    todo = _add_recurring_todo(
        agent, task="Month-end", scheduled_for=fire_time, recurrence="1mo"
    )
    with agent.todo_manager.atomic_update("owner") as todo_list:
        item = todo_list.get_item(todo.id)
        item.recurrence_anchor = fire_time

    with agent.todo_manager.atomic_update("owner") as todo_list:
        assert todo_list.update_item(todo.id, recurrence="2mo")
    assert agent.todo_manager.get_todo_by_id("owner", todo.id).recurrence_anchor is None

    # Re-stamp, then clear the recurrence entirely.
    with agent.todo_manager.atomic_update("owner") as todo_list:
        todo_list.get_item(todo.id).recurrence_anchor = fire_time
    with agent.todo_manager.atomic_update("owner") as todo_list:
        assert todo_list.update_item(todo.id, clear_recurrence=True)
    assert agent.todo_manager.get_todo_by_id("owner", todo.id).recurrence_anchor is None


def test_scheduled_todo_for_deleted_item_is_skipped_and_cleaned(
    tmp_path: Path, api_client_builder
):
    """A due entry whose TODO was reaped (e.g. its thread was deleted between
    scheduling and firing) is skipped without an agent turn and removed from the
    schedule by the item-not-found guard, never firing into a deleted/ghost
    thread. Locks the "orphaned TODOs are cleaned, not fired" guarantee that
    makes a thread-existence fire-gate unnecessary."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    agent._schedule_db.add_scheduled(
        todo_id="gone-todo",
        user_id="owner",
        scheduled_for=datetime.now(timezone.utc),
        task_preview="orphaned",
        thread_id="deleted-thread",
    )
    entry = ScheduledTodoEntry(
        todo_id="gone-todo",
        user_id="owner",
        thread_id="deleted-thread",
        scheduled_for=time.time() - 1,
        task_preview="orphaned",
        created_at=time.time(),
    )

    astream_calls: list = []

    async def tracking_astream(**kwargs):
        astream_calls.append(kwargs)
        yield {"type": "response", "content": "should not run"}

    agent.astream = tracking_astream

    ticker._execute_scheduled_todo(entry)

    assert astream_calls == []  # no agent turn fired for the orphaned TODO
    assert agent._schedule_db.get_entry("gone-todo") is None  # schedule cleaned
    assert not agent._schedule_db.is_execution_active("gone-todo", "owner")


def test_schedule_db_active_execution_lifecycle(tmp_path: Path):
    db = TodoScheduleDB(tmp_path / "todo_schedule.db")

    assert db.mark_execution_started("todo-1", "owner", "thread-1")
    assert db.is_execution_active("todo-1", "owner")
    assert not db.mark_execution_started("todo-1", "owner", "thread-1")

    assert db.clear_execution("todo-1", "owner")
    assert not db.is_execution_active("todo-1", "owner")


def test_schedule_db_clears_stale_active_execution_markers(tmp_path: Path):
    db_path = tmp_path / "todo_schedule.db"
    db = TodoScheduleDB(db_path)
    assert db.mark_execution_started("todo-1", "owner", "thread-1")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE active_todo_executions SET started_at = ? WHERE todo_id = ?",
            (time.time() - 100, "todo-1"),
        )
        conn.commit()

    assert not db.is_execution_active("todo-1", "owner", stale_after_seconds=10)
    assert db.mark_execution_started("todo-1", "owner", "thread-1")


def test_ticker_ask_policy_holds_only_startup_missed_todos(
    tmp_path: Path,
    monkeypatch,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        scheduler_missed_work_policy="ask",
    )
    agent = FakeAgent(settings)
    past = _add_scheduled_todo(
        agent,
        task="Missed while offline",
        scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    future = _add_scheduled_todo(
        agent,
        task="Due after startup",
        scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    ticker = _make_ticker(agent)

    status = ticker.prepare_startup_recovery()

    assert status["pending_missed_todo_ids"] == [past.id]
    assert status["trigger_catchup_paused"] is True

    with sqlite3.connect(agent._schedule_db.db_path) as conn:
        conn.execute(
            "UPDATE scheduled_todos SET scheduled_for = ? WHERE todo_id = ?",
            (time.time() - 1, future.id),
        )
        conn.commit()

    executed: list[str] = []

    def capture_execute(entry):
        executed.append(entry.todo_id)
        agent._schedule_db.remove_scheduled(entry.todo_id)

    monkeypatch.setattr(ticker, "_execute_scheduled_todo", capture_execute)
    ticker._executor = ThreadPoolExecutor(max_workers=1)
    try:
        ticker._check_and_execute()
        ticker._executor.shutdown(wait=True)
        assert executed == [future.id]

        ticker._executor = ThreadPoolExecutor(max_workers=1)
        release = ticker.release_missed_work()
        assert release["released_todo_ids"] == [past.id]
        assert release["trigger_catchup_paused"] is False

        ticker._check_and_execute()
        ticker._executor.shutdown(wait=True)
        assert executed == [future.id, past.id]
    finally:
        if ticker._executor:
            ticker._executor.shutdown(wait=False, cancel_futures=True)
            ticker._executor = None


def test_ticker_ask_policy_pauses_trigger_catchup_until_release(
    tmp_path: Path,
    monkeypatch,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        scheduler_missed_work_policy="ask",
    )
    agent = FakeAgent(settings)
    _add_scheduled_todo(
        agent,
        task="Missed while offline",
        scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    ticker = _make_ticker(agent, poll_interval=1)
    ticker.prepare_startup_recovery()

    called = False

    def capture_trigger_check():
        nonlocal called
        called = True

    monkeypatch.setattr(ticker, "_check_triggers", capture_trigger_check)
    ticker._housekeeping_executor = ThreadPoolExecutor(max_workers=1)
    try:
        assert ticker._maybe_submit_trigger_poll(time.time() + 60) is False
        assert called is False

        ticker.release_missed_work()
        assert ticker._maybe_submit_trigger_poll(time.time() + 60) is True
        ticker._housekeeping_executor.shutdown(wait=True)
        assert called is True
    finally:
        if ticker._housekeeping_executor:
            ticker._housekeeping_executor.shutdown(wait=False, cancel_futures=True)
            ticker._housekeeping_executor = None


def test_ticker_prepare_clears_all_execution_markers_at_startup(
    tmp_path: Path,
    api_client_builder,
):
    """Startup clears every execution marker regardless of age.

    A single ticker owns the schedule DB, so any marker present at startup is
    orphaned by a crash or non-graceful shutdown. A marker written moments
    before a restart (well within the 24h stale window) must still be cleared,
    otherwise the TODO stays wedged as "already executing" for up to a day.
    """
    agent = FakeAgent(api_client_builder.settings(tmp_path))
    # A fresh marker (started_at = now) and an aged one: both are orphaned.
    assert agent._schedule_db.mark_execution_started("todo-fresh", "owner", "thread-1")
    assert agent._schedule_db.mark_execution_started("todo-aged", "owner", "thread-1")
    with sqlite3.connect(agent._schedule_db.db_path) as conn:
        conn.execute(
            "UPDATE active_todo_executions SET started_at = ? WHERE todo_id = ?",
            (time.time() - 120, "todo-aged"),
        )
        conn.commit()

    ticker = _make_ticker(agent)
    status = ticker.prepare_startup_recovery()

    assert status["startup_execution_markers_cleared"] == 2
    assert agent._schedule_db.count_active_executions() == 0


def test_ticker_startup_reclears_lets_interrupted_todo_fire_again(
    tmp_path: Path,
    monkeypatch,
    api_client_builder,
):
    """A TODO interrupted mid-execution by a restart re-fires on the next poll.

    Regression for the marker-drop bug: a fresh (non-stale) execution marker
    left by a restart used to block the due TODO for up to 24h.
    """
    agent = _agent(tmp_path, api_client_builder)
    todo = _add_scheduled_todo(
        agent,
        task="Interrupted by restart",
        scheduled_for=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    # Simulate the pre-restart state: the TODO was mid-execution, so a fresh
    # marker is present when the new process starts up.
    assert agent._schedule_db.mark_execution_started(todo.id, "owner", todo.thread_id)

    ticker = _make_ticker(agent)
    ticker.prepare_startup_recovery()
    assert agent._schedule_db.count_active_executions() == 0

    executed: list[str] = []

    def capture_execute(entry):
        executed.append(entry.todo_id)
        agent._schedule_db.remove_scheduled(entry.todo_id)

    monkeypatch.setattr(ticker, "_execute_scheduled_todo", capture_execute)
    ticker._executor = ThreadPoolExecutor(max_workers=1)
    try:
        ticker._check_and_execute()
        ticker._executor.shutdown(wait=True)
        assert executed == [todo.id]
    finally:
        if ticker._executor:
            ticker._executor.shutdown(wait=False, cancel_futures=True)
            ticker._executor = None


def test_scheduler_status_and_release_endpoints_require_admin_and_release_missed_work(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        scheduler_missed_work_policy="ask",
    )
    api_client_builder.create_checkpoint_table(settings)
    agent = FakeAgent(settings)
    todo = _add_scheduled_todo(
        agent,
        task="Missed while offline",
        scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    ticker = _make_ticker(agent)
    ticker.prepare_startup_recovery()
    agent._ticker = ticker

    client, admin_token = api_client_builder.authenticated_client(
        agent,
        settings,
        user_id="admin",
        role="admin",
    )
    user = agent.accounts_repo.create_user(
        "regular",
        "regular@example.com",
        "Regular",
        role="user",
    )
    assert user is not None
    user_token = agent.accounts_repo.issue_token("regular")

    forbidden = client.get("/scheduler/status", headers=_headers(user_token))
    assert forbidden.status_code == 403

    status = client.get("/scheduler/status", headers=_headers(admin_token))
    assert status.status_code == 200
    body = status.json()
    assert body["missed_work_policy"] == "ask"
    assert body["pending_missed_todo_ids"] == [todo.id]
    assert body["trigger_catchup_paused"] is True

    released = client.post(
        "/scheduler/missed-work/run",
        headers=_headers(admin_token),
    )
    assert released.status_code == 200
    release_body = released.json()
    assert release_body["released_todo_ids"] == [todo.id]
    assert release_body["pending_missed_todo_ids"] == []
    assert release_body["trigger_catchup_paused"] is False


def test_legacy_tasks_endpoint_and_rate_limit_setting_are_removed(tmp_path: Path, api_client_builder):
    client, _agent, token = _client(tmp_path, api_client_builder)

    response = client.get("/tasks", headers=_headers(token))

    assert response.status_code == 404
    assert "max_self_invokes_per_hour" not in ServerSettingsResponse.model_fields
    assert "max_self_invokes_per_hour" not in ServerSettingsUpdate.model_fields


@pytest.mark.parametrize(
    ("method", "path_suffix", "json_body"),
    [
        ("patch", "", {"task": "Updated task"}),
        ("post", "/complete", None),
        ("delete", "", None),
    ],
)
def test_todo_write_endpoints_reject_active_execution(
    tmp_path: Path,
    api_client_builder,
    method: str,
    path_suffix: str,
    json_body: dict[str, str] | None,
):
    client, agent, token = _client(tmp_path, api_client_builder)
    todo = _add_todo(agent)
    assert agent._schedule_db.mark_execution_started(todo.id, "owner", todo.thread_id)

    request = getattr(client, method)
    kwargs = {"headers": _headers(token)}
    if json_body is not None:
        kwargs["json"] = json_body
    response = request(f"/todos/{todo.id}{path_suffix}", **kwargs)

    assert response.status_code == 409
    assert "currently executing" in response.json()["detail"]


@pytest.mark.parametrize(
    ("method", "path_suffix", "json_body"),
    [
        ("patch", "", {"task": "Updated task"}),
        ("post", "/complete", None),
        ("delete", "", None),
    ],
)
def test_todo_write_endpoints_succeed_after_execution_marker_clears(
    tmp_path: Path,
    api_client_builder,
    method: str,
    path_suffix: str,
    json_body: dict[str, str] | None,
):
    client, agent, token = _client(tmp_path, api_client_builder)
    todo = _add_todo(agent)
    assert agent._schedule_db.mark_execution_started(todo.id, "owner", todo.thread_id)
    assert agent._schedule_db.clear_execution(todo.id, "owner")

    request = getattr(client, method)
    kwargs = {"headers": _headers(token)}
    if json_body is not None:
        kwargs["json"] = json_body
    response = request(f"/todos/{todo.id}{path_suffix}", **kwargs)

    assert response.status_code == 200


def test_todo_write_endpoint_allows_configured_stale_execution_marker(
    tmp_path: Path,
    api_client_builder,
):
    settings = api_client_builder.settings(
        tmp_path,
        scheduler_active_execution_stale_minutes=1,
    )
    api_client_builder.create_checkpoint_table(settings)
    agent = FakeAgent(settings)
    client, token = api_client_builder.authenticated_client(
        agent,
        settings,
        user_id="owner",
        email="owner@example.com",
        display_name="Owner",
    )
    todo = _add_todo(agent)
    assert agent._schedule_db.mark_execution_started(todo.id, "owner", todo.thread_id)
    with sqlite3.connect(agent._schedule_db.db_path) as conn:
        conn.execute(
            "UPDATE active_todo_executions SET started_at = ? WHERE todo_id = ?",
            (time.time() - 120, todo.id),
        )
        conn.commit()

    response = client.patch(
        f"/todos/{todo.id}",
        headers=_headers(token),
        json={"task": "Updated after stale marker"},
    )

    assert response.status_code == 200


def test_ticker_clears_active_execution_marker_after_failed_run(tmp_path: Path, api_client_builder):
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    todo = _add_todo(agent)
    entry = ScheduledTodoEntry(
        todo_id=todo.id,
        user_id="owner",
        thread_id=todo.thread_id,
        scheduled_for=time.time() - 1,
        task_preview=todo.task,
        created_at=time.time(),
    )

    ticker._execute_scheduled_todo(entry)

    assert not agent._schedule_db.is_execution_active(todo.id, "owner")


def test_ticker_clears_marker_even_if_failure_handler_raises(
    tmp_path: Path, api_client_builder, monkeypatch
):
    """F11 regression: if the execution-failure handler itself raises (e.g. a
    disk-save error now surfaced by ``atomic_update``), the active-execution
    marker must still be cleared by the ``finally`` so the TODO is not blocked
    until the stale sweep."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    todo = _add_todo(agent)
    entry = ScheduledTodoEntry(
        todo_id=todo.id,
        user_id="owner",
        thread_id=todo.thread_id,
        scheduled_for=time.time() - 1,
        task_preview=todo.task,
        created_at=time.time(),
    )

    def _stream_boom(*args, **kwargs):
        raise RuntimeError("stream failed")

    def _handler_boom(*args, **kwargs):
        raise RuntimeError("Failed to persist TODOs for user owner")

    monkeypatch.setattr(ticker, "_stream_todo_execution", _stream_boom)
    monkeypatch.setattr(ticker, "_handle_execution_failure", _handler_boom)

    with pytest.raises(RuntimeError, match="Failed to persist"):
        ticker._execute_scheduled_todo(entry)

    # Despite the failure handler raising, the marker was cleared in finally.
    assert not agent._schedule_db.is_execution_active(todo.id, "owner")


def test_ticker_archives_completed_todos_using_configured_retention(tmp_path: Path, api_client_builder):
    agent = _agent(tmp_path, api_client_builder)
    agent.settings.todo_auto_archive_days = 3
    ticker = _make_ticker(agent)

    with agent.todo_manager.atomic_update("owner") as todo_list:
        old_done = todo_list.add_item("Old completed", thread_id="thread-1")
        recent_done = todo_list.add_item("Recent completed", thread_id="thread-1")
        active = todo_list.add_item("Still active", thread_id="thread-1")
        assert old_done is not None
        assert recent_done is not None
        assert active is not None
        old_done.status = TodoStatus.DONE
        old_done.updated_at = datetime.now(timezone.utc) - timedelta(days=4)
        recent_done.status = TodoStatus.DONE
        recent_done.updated_at = datetime.now(timezone.utc) - timedelta(days=2)

    ticker._archive_completed_todos()

    remaining = agent.todo_manager.get_todos("owner").items
    remaining_ids = {todo.id for todo in remaining}
    assert old_done.id not in remaining_ids
    assert recent_done.id in remaining_ids
    assert active.id in remaining_ids


def test_ticker_uses_async_stream_and_forwards_reload_events(tmp_path: Path, monkeypatch, api_client_builder):
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    todo = _add_todo(agent)
    entry = ScheduledTodoEntry(
        todo_id=todo.id,
        user_id="owner",
        thread_id=todo.thread_id,
        scheduled_for=time.time() - 1,
        task_preview=todo.task,
        created_at=time.time(),
    )

    calls = []

    async def fake_astream(**kwargs):
        calls.append(kwargs)
        yield {
            "type": "tool_reload",
            "tools": ["tool_create"],
            "ttl": "2h",
            "ttl_seconds": 7200,
            "source": "tool_search",
        }
        yield {
            "type": "tool_call",
            "id": "call-1",
            "name": "tool_create",
            "args": {"action": "draft"},
        }
        yield {
            "type": "tool_result",
            "id": "call-1",
            "name": "tool_create",
            "result": "drafted",
        }
        yield {"type": "response", "content": "done"}

    agent.astream = fake_astream

    events = []

    def capture_autonomous_event(event_type, thread_id, user_id, task_id="", data=None):
        events.append((event_type, data or {}))

    def capture_stream_chunk(chunk, **kwargs):
        events.append((chunk["type"], {k: v for k, v in chunk.items() if k != "type"}))
        return True

    monkeypatch.setattr(ticker_module, "publish_autonomous_event", capture_autonomous_event)
    monkeypatch.setattr(ticker_module, "publish_agent_stream_chunk", capture_stream_chunk)

    ticker._execute_scheduled_todo(entry)

    assert calls
    assert calls[0]["message"].startswith(f"Work on TODO {todo.id}:")
    assert calls[0]["thread_id"] == todo.thread_id
    assert calls[0]["user_id"] == "owner"
    assert calls[0]["_is_self_invoke"] is True
    assert [event[0] for event in events] == [
        "task_started",
        "tool_reload",
        "tool_call",
        "tool_result",
        "response",
        "task_completed",
    ]
    assert events[1][1]["ttl_seconds"] == 7200


# ---- AGENT-010: Poll-loop isolation tests ----


def test_trigger_poll_does_not_occupy_autonomous_worker_pool(tmp_path: Path, monkeypatch, api_client_builder):
    """A slow trigger source poll cannot starve a due scheduled TODO."""
    agent = _agent(tmp_path, api_client_builder)
    entry = ScheduledTodoEntry(
        todo_id="todo-1",
        user_id="owner",
        thread_id="thread-1",
        scheduled_for=time.time() - 1,
        task_preview="Check the inbox",
        created_at=time.time(),
    )

    class DueSchedule:
        def get_due(self, before: float):
            return [entry]

    ticker = _make_ticker(agent, schedule_db=DueSchedule(), poll_interval=1)
    ticker._executor = ThreadPoolExecutor(max_workers=1)
    ticker._housekeeping_executor = ThreadPoolExecutor(max_workers=1)

    trigger_started = threading.Event()
    trigger_release = threading.Event()
    todo_started = threading.Event()

    def slow_check_triggers():
        trigger_started.set()
        trigger_release.wait(timeout=5)

    def fake_execute(entry_arg):
        if entry_arg.todo_id == entry.todo_id:
            todo_started.set()

    monkeypatch.setattr(ticker, "_check_triggers", slow_check_triggers)
    monkeypatch.setattr(ticker, "_execute_scheduled_todo", fake_execute)

    try:
        assert ticker._maybe_submit_trigger_poll(time.time()) is True
        assert trigger_started.wait(timeout=2)

        ticker._check_and_execute()

        assert todo_started.wait(timeout=1)
        assert not trigger_release.is_set()
    finally:
        trigger_release.set()
        ticker._housekeeping_executor.shutdown(wait=True)
        ticker._executor.shutdown(wait=True)


def test_trigger_poll_exception_clears_running_flag(tmp_path: Path, monkeypatch, api_client_builder):
    """An exception in _check_triggers resets the guard flag."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)

    def exploding_triggers():
        raise RuntimeError("source network timeout")

    monkeypatch.setattr(ticker, "_check_triggers", exploding_triggers)

    ticker._trigger_poll_running = True
    ticker._run_trigger_poll()

    assert not ticker._trigger_poll_running


def test_trigger_poll_skipped_when_already_running(tmp_path: Path, monkeypatch, api_client_builder):
    """A second trigger poll is not submitted while one is in progress."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent, poll_interval=1)
    ticker._housekeeping_executor = ThreadPoolExecutor(max_workers=1)

    call_count = 0
    started = threading.Event()
    release = threading.Event()

    def slow_check():
        nonlocal call_count
        call_count += 1
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(ticker, "_check_triggers", slow_check)

    try:
        assert ticker._maybe_submit_trigger_poll(time.time()) is True
        assert started.wait(timeout=2)
        assert ticker._maybe_submit_trigger_poll(time.time() + 60) is False
    finally:
        release.set()
        ticker._housekeeping_executor.shutdown(wait=True)

    assert call_count == 1


def test_trigger_poll_failed_submit_clears_running_flag(tmp_path: Path, api_client_builder):
    """A shutdown-time submit failure does not leave polling permanently stuck."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent, poll_interval=1)
    ticker._housekeeping_executor = ThreadPoolExecutor(max_workers=1)
    ticker._housekeeping_executor.shutdown(wait=True)

    assert ticker._maybe_submit_trigger_poll(time.time()) is False
    assert not ticker._trigger_poll_running


def test_archive_runs_off_main_loop(tmp_path: Path, monkeypatch, api_client_builder):
    """_archive_completed_todos runs in housekeeping, not inline."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent, poll_interval=1)
    ticker._housekeeping_executor = ThreadPoolExecutor(max_workers=1)

    archive_thread_ids: list[int] = []
    main_thread_id = threading.get_ident()

    original_archive = ticker._archive_completed_todos

    def tracking_archive():
        archive_thread_ids.append(threading.get_ident())
        original_archive()

    monkeypatch.setattr(ticker, "_archive_completed_todos", tracking_archive)

    assert ticker._maybe_submit_archive(time.time()) is True
    ticker._housekeeping_executor.shutdown(wait=True)

    assert len(archive_thread_ids) == 1
    assert archive_thread_ids[0] != main_thread_id


# ── Mid-run schedule edits win over the post-run automation ──────────────
# A schedule write made DURING a TODO's own fire turn (agent tool, REST/GUI,
# or the done-path re-arm) must survive the ticker's post-run re-arm/clear.
# Before 2026-08-29 the finalize path rewrote the schedule unconditionally
# from its pre-run snapshot, silently destroying mid-run re-arms (two lost
# medication-reminder wakes in production) and clearing the documented
# self-rescheduling-watcher pattern for non-recurring TODOs.


def _rewrite_mid_run(
    agent: FakeAgent,
    todo_id: str,
    *,
    scheduled_for: datetime | None = None,
    clear_schedule: bool = False,
    status: TodoStatus | None = None,
    user_id: str = "owner",
) -> None:
    """Simulate the fire turn editing its own TODO (tool/REST path shape)."""
    with agent.todo_manager.atomic_update(user_id) as todo_list:
        assert todo_list.update_item(
            todo_id,
            scheduled_for=scheduled_for,
            clear_schedule=clear_schedule,
            status=status,
        )
    agent.todo_manager.sync_schedule_to_db(user_id, todo_id, agent._schedule_db)


def test_mid_run_reschedule_survives_recurring_rearm(
    tmp_path: Path, api_client_builder
):
    """A recurring TODO that re-arms itself during its own fire turn keeps the
    mid-run wake: the post-run re-arm must not overwrite it with
    fired-slot + interval, and last_execution records the slot that ran."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    todo = _add_recurring_todo(
        agent, task="Medication hound", scheduled_for=fire_time, recurrence="1d"
    )
    entry = _entry_for(todo, fire_time)
    rearm = fire_time + timedelta(minutes=38)
    _rewrite_mid_run(
        agent, todo.id, scheduled_for=rearm, status=TodoStatus.IN_PROGRESS
    )

    ticker._handle_recurrence(entry, todo)

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed is not None
    assert refreshed.scheduled_for == rearm
    # The IN_PROGRESS at fire start is the ticker's own lifecycle write, so
    # the end-of-run transition back to PENDING still applies on the honored
    # branch (a DONE set by the run is preserved); only the SCHEDULE write is
    # what the guard protects.
    assert refreshed.status == TodoStatus.PENDING
    assert refreshed.last_execution is not None
    assert abs(refreshed.last_execution.timestamp() - fire_time.timestamp()) < 1
    row = agent._schedule_db.get_entry(todo.id)
    assert row is not None
    assert abs(row.scheduled_for - rearm.timestamp()) < 1


def test_mid_run_reschedule_survives_oneshot_clear(
    tmp_path: Path, api_client_builder
):
    """A non-recurring TODO that re-arms itself mid-run (the documented
    self-rescheduling-watcher pattern) must not have its schedule cleared by
    the post-run finalize."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    todo = _add_scheduled_todo(agent, task="Watcher", scheduled_for=fire_time)
    agent.todo_manager.sync_schedule_to_db("owner", todo.id, agent._schedule_db)
    entry = _entry_for(todo, fire_time)
    rearm = fire_time + timedelta(minutes=35)
    _rewrite_mid_run(agent, todo.id, scheduled_for=rearm)

    ticker._handle_recurrence(entry, todo)

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed is not None
    assert refreshed.scheduled_for == rearm
    assert refreshed.last_execution is not None
    assert abs(refreshed.last_execution.timestamp() - fire_time.timestamp()) < 1
    row = agent._schedule_db.get_entry(todo.id)
    assert row is not None
    assert abs(row.scheduled_for - rearm.timestamp()) < 1


def test_mid_run_clear_schedule_is_not_resurrected(
    tmp_path: Path, api_client_builder
):
    """An explicit clear_schedule during the fire turn is honored: the
    post-run re-arm must not resurrect the next recurrence slot."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    todo = _add_recurring_todo(
        agent, task="Stopped hound", scheduled_for=fire_time, recurrence="1d"
    )
    entry = _entry_for(todo, fire_time)
    _rewrite_mid_run(agent, todo.id, clear_schedule=True)

    ticker._handle_recurrence(entry, todo)

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed is not None
    assert refreshed.scheduled_for is None
    assert refreshed.recurrence == "1d"  # recurrence untouched, schedule off
    assert refreshed.last_execution is not None
    assert abs(refreshed.last_execution.timestamp() - fire_time.timestamp()) < 1
    assert agent._schedule_db.get_entry(todo.id) is None


def test_mid_run_rearm_to_now_past_time_stays_due(
    tmp_path: Path, api_client_builder
):
    """A mid-run re-arm to a time that has already passed by finalize keeps
    its schedule row and shows up as due, so it fires on the next poll."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime.now(timezone.utc) - timedelta(minutes=5)
    todo = _add_recurring_todo(
        agent, task="Quick re-ping", scheduled_for=fire_time, recurrence="1d"
    )
    entry = _entry_for(todo, fire_time)
    rearm = fire_time + timedelta(minutes=1)  # already 4 minutes in the past
    _rewrite_mid_run(agent, todo.id, scheduled_for=rearm)

    ticker._handle_recurrence(entry, todo)

    due_ids = [e.todo_id for e in agent._schedule_db.get_due(before=time.time())]
    assert todo.id in due_ids


def test_mid_run_done_advance_is_left_alone_by_finalize(
    tmp_path: Path, api_client_builder
):
    """A recurring TODO marked done during its own fire turn was already
    advanced to the next slot by the done-path; finalize must leave that armed
    slot alone and stamp last_execution with the slot that ran.

    Teeth note: on the pre-guard code this test passed by idempotence (the
    re-arm recomputed the same armed slot and stamped the same value), so its
    teeth are the last_execution/slot semantics, proven red by perturbing the
    honored branch's stamp; it guards the done-anchor invariant, not the
    guard's existence."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    todo = _add_recurring_todo(
        agent, task="Daily check", scheduled_for=fire_time, recurrence="1d"
    )
    entry = _entry_for(todo, fire_time)
    armed = fire_time + timedelta(days=1)  # what the done-path arms
    _rewrite_mid_run(
        agent, todo.id, scheduled_for=armed, status=TodoStatus.PENDING
    )

    ticker._handle_recurrence(entry, todo)

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed is not None
    assert refreshed.scheduled_for == armed
    assert refreshed.last_execution is not None
    assert abs(refreshed.last_execution.timestamp() - fire_time.timestamp()) < 1


def test_retry_giveup_honors_mid_run_reschedule(
    tmp_path: Path, api_client_builder
):
    """The recurring retry-give-up re-arm routes through the same guard: a
    wake the failing run set for itself survives instead of being replaced by
    the next recurrence slot."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    todo = _add_recurring_todo(
        agent, task="Flaky feed", scheduled_for=fire_time, recurrence="1d"
    )
    entry = _entry_for(todo, fire_time)
    rearm = fire_time + timedelta(minutes=38)
    _rewrite_mid_run(agent, todo.id, scheduled_for=rearm)

    for _ in range(Ticker.MAX_RETRIES):
        ticker._handle_execution_failure(
            entry, todo, todo.thread_id, RuntimeError("proxy 503")
        )

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed is not None
    assert refreshed.scheduled_for == rearm
    assert refreshed.consecutive_failures == 1  # streak still recorded
    row = agent._schedule_db.get_entry(todo.id)
    assert row is not None
    assert abs(row.scheduled_for - rearm.timestamp()) < 1


def test_workflow_todo_mid_run_rearm_skips_auto_done_close(
    tmp_path: Path, api_client_builder
):
    """A non-recurring workflow TODO whose schedule was rewritten during the
    run keeps the new wake: the post-run auto-DONE close would park a live
    wake behind a done status, so it must be skipped."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    with agent.todo_manager.atomic_update("owner") as todo_list:
        todo = todo_list.add_item(
            "Scheduled workflow",
            scheduled_for=fire_time,
            thread_id="thread-1",
            workflow_id="wf-test",
        )
        assert todo is not None
    agent.todo_manager.sync_schedule_to_db("owner", todo.id, agent._schedule_db)
    entry = _entry_for(todo, fire_time)
    rearm = fire_time + timedelta(minutes=20)

    async def fake_run_workflow(workflow_id, params, *, user_id, thread_id):
        _rewrite_mid_run(agent, todo.id, scheduled_for=rearm)
        return {"ok": True, "status": "completed"}

    ticker._turn_executor.run_workflow = fake_run_workflow

    ticker._execute_workflow_todo(entry, todo, "thread-1")

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed is not None
    assert refreshed.status != TodoStatus.DONE
    assert refreshed.scheduled_for == rearm
    row = agent._schedule_db.get_entry(todo.id)
    assert row is not None
    assert abs(row.scheduled_for - rearm.timestamp()) < 1


def test_backoff_honors_mid_run_reschedule(tmp_path: Path, api_client_builder):
    """The double-iteration-limit backoff must not overwrite a wake (or the
    notes) the run set for itself."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    todo = _add_scheduled_todo(agent, task="Long task", scheduled_for=fire_time)
    agent.todo_manager.sync_schedule_to_db("owner", todo.id, agent._schedule_db)
    with agent.todo_manager.atomic_update("owner") as todo_list:
        assert todo_list.update_item(todo.id, notes="user instructions here")
    entry = _entry_for(todo, fire_time)
    rearm = fire_time + timedelta(minutes=45)
    _rewrite_mid_run(agent, todo.id, scheduled_for=rearm)

    ticker._backoff_after_double_limit(entry, todo, "thread-1")

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed is not None
    assert refreshed.scheduled_for == rearm
    assert refreshed.notes == "user instructions here"


def test_backoff_prepends_banner_and_replaces_it_on_repeat(
    tmp_path: Path, api_client_builder
):
    """Without a mid-run edit the backoff reschedules +10min, but its note is
    PREPENDED to the existing notes (notes are prompt input and user
    instructions), and a second backoff replaces its banner instead of
    stacking."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    todo = _add_scheduled_todo(agent, task="Long task", scheduled_for=fire_time)
    agent.todo_manager.sync_schedule_to_db("owner", todo.id, agent._schedule_db)
    with agent.todo_manager.atomic_update("owner") as todo_list:
        assert todo_list.update_item(todo.id, notes="user instructions here")
    entry = _entry_for(todo, fire_time)

    ticker._backoff_after_double_limit(entry, todo, "thread-1")

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed is not None
    assert refreshed.scheduled_for is not None
    assert refreshed.scheduled_for > datetime.now(timezone.utc)
    assert "user instructions here" in (refreshed.notes or "")
    assert (refreshed.notes or "").index("user instructions here") > 0

    # Second backoff (fired at the slot the first one armed): one banner only.
    second_entry = _entry_for(refreshed, refreshed.scheduled_for)
    ticker._backoff_after_double_limit(second_entry, refreshed, "thread-1")
    again = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert again is not None
    banner_prefix = ticker_module.BACKOFF_NOTE_PREFIX
    assert (again.notes or "").count(banner_prefix) == 1
    assert "user instructions here" in (again.notes or "")


def test_retry_giveup_prepends_failure_note(tmp_path: Path, api_client_builder):
    """The non-recurring give-up failure note is prepended, preserving the
    TODO's existing notes instead of overwriting them."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    todo = _add_scheduled_todo(agent, task="One-shot", scheduled_for=fire_time)
    agent.todo_manager.sync_schedule_to_db("owner", todo.id, agent._schedule_db)
    with agent.todo_manager.atomic_update("owner") as todo_list:
        assert todo_list.update_item(todo.id, notes="user instructions here")
    entry = _entry_for(todo, fire_time)

    for _ in range(Ticker.MAX_RETRIES):
        ticker._handle_execution_failure(
            entry, todo, todo.thread_id, RuntimeError("boom")
        )

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed is not None
    assert refreshed.scheduled_for is None
    assert "failed after 3 retries" in (refreshed.notes or "")
    assert "user instructions here" in (refreshed.notes or "")


def test_mid_run_delivery_pause_survives_success_finalize(
    tmp_path: Path, api_client_builder
):
    """A #247 delivery-pause that lands while the TODO's own turn is running
    must survive the success finalize wholesale: schedule stays cleared (not
    resurrected), and the pause marker, delivery streak, and pause banner are
    not half-undone by the post-run failure-streak reset (which would leave
    the marker-less dead-schedule shape #154 exists to prevent)."""
    from nymeria.core import delivery_accounting

    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    todo = _add_recurring_todo(
        agent, task="Daily digest", scheduled_for=fire_time, recurrence="1d"
    )
    with agent.todo_manager.atomic_update("owner") as todo_list:
        assert todo_list.update_item(todo.id, notes="user instructions here")
    entry = _entry_for(todo, fire_time)

    async def pausing_astream(**kwargs):
        # The real #247 pause writer fires mid-turn (a delivery report for a
        # prior occurrence crossing the pause threshold).
        assert delivery_accounting._pause_undelivered_schedule(
            agent.todo_manager,
            agent._schedule_db,
            "owner",
            todo.id,
            5,
            "chat delivery failed",
        )
        with agent.todo_manager.atomic_update("owner") as todo_list:
            item = todo_list.get_item(todo.id)
            item.delivery_failures = 5
        yield {"type": "response", "content": "did the thing"}

    agent.astream = pausing_astream

    ticker._execute_scheduled_todo(entry)

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed is not None
    assert refreshed.scheduled_for is None  # pause honored, not resurrected
    assert refreshed.schedule_paused_at is not None  # marker survives reset
    assert refreshed.delivery_failures == 5  # streak survives
    assert (refreshed.notes or "").startswith("[auto-paused after")
    assert "user instructions here" in (refreshed.notes or "")
    assert agent._schedule_db.get_entry(todo.id) is None


def test_transient_banners_replace_across_kinds(
    tmp_path: Path, api_client_builder
):
    """A give-up banner replaces a leading backoff banner (and vice versa):
    at most one transient banner leads the notes, so stale banners never
    accumulate in front of the user's instructions."""
    agent = _agent(tmp_path, api_client_builder)
    ticker = _make_ticker(agent)
    fire_time = datetime.now(timezone.utc) - timedelta(seconds=1)
    todo = _add_scheduled_todo(agent, task="One-shot", scheduled_for=fire_time)
    agent.todo_manager.sync_schedule_to_db("owner", todo.id, agent._schedule_db)
    with agent.todo_manager.atomic_update("owner") as todo_list:
        assert todo_list.update_item(todo.id, notes="user instructions here")

    ticker._backoff_after_double_limit(
        _entry_for(todo, fire_time), todo, "thread-1"
    )
    backed_off = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert backed_off is not None and backed_off.scheduled_for is not None

    second_entry = _entry_for(backed_off, backed_off.scheduled_for)
    for _ in range(Ticker.MAX_RETRIES):
        ticker._handle_execution_failure(
            second_entry, backed_off, "thread-1", RuntimeError("boom")
        )

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed is not None
    notes = refreshed.notes or ""
    assert "failed after 3 retries" in notes
    assert ticker_module.BACKOFF_NOTE_PREFIX not in notes  # replaced, not buried
    assert "user instructions here" in notes
