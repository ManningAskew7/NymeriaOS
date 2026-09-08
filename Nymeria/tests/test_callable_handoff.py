"""The callable tool's schema and the request path's scheduled and busy shapes.

Every callable-thread call is a request (backlog #357): the tool exposes
``task`` / ``wait_seconds`` / ``scheduled_for`` / ``if_busy`` and no ``mode``;
a scheduled request lands as a TODO on the target carrying the request block;
``if_busy="error"`` on a busy target neither schedules nor starts work.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from nymeria.agents.tool_factory import create_callable_thread_tool
from nymeria.core import thread_agent_executor as ex
from nymeria.core import thread_requests as tr
from nymeria.core.agent import set_current_agent
from nymeria.core.thread_agent_executor import handoff, request
from nymeria.core.thread_config import ThreadConfig
from nymeria.core.todo_manager import TodoManager
from nymeria.core.todo_schedule_db import TodoScheduleDB


class FakeThreadConfigManager:
    def __init__(self, target_thread_id: str):
        self.target_thread_id = target_thread_id

    def get_config(self, thread_id: str):
        if thread_id != self.target_thread_id:
            return None
        return ThreadConfig(
            thread_id=thread_id,
            callable=True,
            callable_name="UserAgent",
        )


class FakeThreadLocks:
    def __init__(self, busy: bool = False):
        self.busy = busy

    def is_thread_busy(self, thread_id: str) -> bool:
        return self.busy


class FakeAgent:
    def __init__(self, data_dir: Path, target_thread_id: str, busy: bool = False):
        self.thread_config_manager = FakeThreadConfigManager(target_thread_id)
        self.todo_manager = TodoManager(data_dir)
        self._schedule_db = TodoScheduleDB(data_dir / "todo_schedule.db")
        self._thread_locks = FakeThreadLocks(busy=busy)


@pytest.fixture(autouse=True)
def _fresh_ledger():
    tr.reset_for_tests()
    yield
    tr.reset_for_tests()
    set_current_agent(None)


def test_callable_tool_schema_is_request_shaped():
    tool = create_callable_thread_tool(
        ThreadConfig(
            thread_id="user-agent-thread",
            callable=True,
            callable_name="UserAgent",
        )
    )

    assert tool.name == "UserAgent"
    assert sorted(tool.args.keys()) == ["if_busy", "scheduled_for", "task", "wait_seconds"]
    assert "mode" not in tool.args


def test_scheduled_request_creates_a_todo_on_the_target_carrying_the_request_block(tmp_path: Path):
    agent = FakeAgent(tmp_path, target_thread_id="user-agent-thread")
    set_current_agent(agent)
    receipt, req = request(
        "user-agent-thread",
        "Tell the user an important email arrived.",
        "owner",
        "UserAgent",
        caller_thread_id="outlook-agent-thread",
        caller_name="OutlookAgent",
        scheduled_for="30s",
    )

    assert receipt.startswith("[Requested]:")
    assert "todo_id=" in receipt and "request_id=req-" in receipt
    assert req is None  # nothing dispatched now; the TODO fires it

    todos = agent.todo_manager.get_todos("owner").items
    assert len(todos) == 1
    assert todos[0].thread_id == "user-agent-thread"
    assert "Tell the user an important email arrived." in todos[0].task
    # The note carries the metadata block ALONE (the ticker prompts with the
    # TODO's task text plus the note), intact within the note's 1000-char cap.
    assert todos[0].notes is not None
    assert todos[0].notes.startswith("[Request Metadata]\n")
    assert todos[0].notes.rstrip().endswith("[/Request Metadata]")
    assert "Tell the user an important email arrived." not in todos[0].notes
    assert "source_thread_id: outlook-agent-thread" in todos[0].notes
    assert "source_thread_name: OutlookAgent" in todos[0].notes
    assert "reply_to_thread" in todos[0].notes

    scheduled = agent._schedule_db.get_for_user("owner")
    assert len(scheduled) == 1
    assert scheduled[0].todo_id == todos[0].id
    assert scheduled[0].thread_id == "user-agent-thread"

    # The ledger holds the request, clocked from the scheduled time: nothing
    # is owed by the target before the TODO is due.
    awaited = tr.requests_awaited_by("outlook-agent-thread")
    assert len(awaited) == 1 and awaited[0].id in receipt
    assert awaited[0].scheduled is True
    assert awaited[0].opened_at > time.time() + 20
    assert tr.requests_owed_by("user-agent-thread") == []
    assert tr.unreplied_request_reminder("user-agent-thread") is None
    assert tr.requests_owed_by("user-agent-thread", now=awaited[0].opened_at + 1) == [awaited[0]]


def test_a_handoff_with_no_calling_thread_dispatches_the_bare_task_without_a_record(tmp_path: Path, monkeypatch):
    """A headless workflow's ``nym.thread`` handoff has no thread a reply could
    reach: the callee gets the plain task, the caller a [Dispatched] receipt,
    and the ledger records nothing (nothing to remind, nudge, or expire)."""
    agent = FakeAgent(tmp_path, target_thread_id="user-agent-thread")
    set_current_agent(agent)
    calls: list[dict] = []
    monkeypatch.setattr(ex, "_run_callable_stream", lambda **kwargs: calls.append(kwargs) or "")

    result = handoff(
        "user-agent-thread",
        "Tell the user an important email arrived.",
        "owner",
        "UserAgent",
        caller_thread_id=None,
    )

    assert result.startswith("[Dispatched]:") and "no calling thread" in result
    deadline = time.time() + 5
    while time.time() < deadline and not calls:
        time.sleep(0.02)
    assert len(calls) == 1
    assert calls[0]["task"] == "Tell the user an important email arrived."
    assert "[Request Metadata]" not in calls[0]["task"]
    assert calls[0]["event_metadata"] == {"source": "handoff"}
    assert tr.open_requests() == []


def test_busy_target_with_if_busy_error_neither_schedules_nor_starts_work(tmp_path: Path):
    agent = FakeAgent(tmp_path, target_thread_id="user-agent-thread", busy=True)
    set_current_agent(agent)
    result = handoff(
        "user-agent-thread",
        "Tell the user an important email arrived.",
        "owner",
        "UserAgent",
        caller_thread_id="outlook-agent-thread",
        caller_name="OutlookAgent",
        if_busy="error",
    )

    assert result.startswith("[Busy]:")
    assert agent.todo_manager.get_todos("owner").items == []
    assert agent._schedule_db.get_for_user("owner") == []
    assert tr.open_requests() == []
