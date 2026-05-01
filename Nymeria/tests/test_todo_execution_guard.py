"""Regression tests for active scheduled TODO mutation guards."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.core.ticker import Ticker
from nymeria.core.todo_manager import TodoManager
from nymeria.core.todo_schedule_db import ScheduledTodoEntry, TodoScheduleDB
from nymeria.core.trigger_manager import TriggerManager
from nymeria.core import ticker as ticker_module
from nymeria.triggers import api as api_module


@dataclass
class FakeSettings:
    data_dir: Path
    database_backend: str = "sqlite"
    postgres_uri: str | None = None
    redis_enabled: bool = False
    redis_url: str | None = None
    fcm_enabled: bool = False
    fcm_credentials_json: str | None = None
    context_management: str = "none"
    sliding_window_cycles: int = 20
    cors_origins_list: list[str] | None = None

    def __post_init__(self):
        if self.cors_origins_list is None:
            self.cors_origins_list = ["*"]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nymeria.db"


class FakeThreadConfigManager:
    def get_config(self, thread_id: str):
        return None


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.todo_manager = TodoManager(data_dir)
        self._schedule_db = TodoScheduleDB(data_dir / "todo_schedule.db")
        self.trigger_manager = TriggerManager(data_dir)
        self.thread_config_manager = FakeThreadConfigManager()
        self.settings = FakeSettings(data_dir)

    def sync_agent_tools(self):
        pass

    def stream(self, *args, **kwargs):
        raise AssertionError("autonomous scheduled TODOs should use astream()")

    async def astream(self, *args, **kwargs):
        raise RuntimeError("boom")


def _client(tmp_path: Path, monkeypatch) -> tuple[TestClient, FakeAgent, str]:
    settings = FakeSettings(tmp_path)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("CREATE TABLE checkpoints (thread_id TEXT)")
        conn.commit()
    agent = FakeAgent(tmp_path)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    app = api_module.create_api_app(agent)
    agent.accounts_repo.create_user("owner", "owner@example.com", "Owner")
    token = agent.accounts_repo.issue_token("owner")
    return TestClient(app), agent, token


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
    monkeypatch,
    method: str,
    path_suffix: str,
    json_body: dict[str, str] | None,
):
    client, agent, token = _client(tmp_path, monkeypatch)
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
    monkeypatch,
    method: str,
    path_suffix: str,
    json_body: dict[str, str] | None,
):
    client, agent, token = _client(tmp_path, monkeypatch)
    todo = _add_todo(agent)
    assert agent._schedule_db.mark_execution_started(todo.id, "owner", todo.thread_id)
    assert agent._schedule_db.clear_execution(todo.id, "owner")

    request = getattr(client, method)
    kwargs = {"headers": _headers(token)}
    if json_body is not None:
        kwargs["json"] = json_body
    response = request(f"/todos/{todo.id}{path_suffix}", **kwargs)

    assert response.status_code == 200


def test_ticker_clears_active_execution_marker_after_failed_run(tmp_path: Path):
    agent = FakeAgent(tmp_path)
    ticker = Ticker(agent, agent._schedule_db, agent.todo_manager)
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


def test_ticker_uses_async_stream_and_forwards_reload_events(tmp_path: Path, monkeypatch):
    agent = FakeAgent(tmp_path)
    ticker = Ticker(agent, agent._schedule_db, agent.todo_manager)
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
