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
