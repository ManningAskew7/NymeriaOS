from __future__ import annotations

from pathlib import Path

from nymeria.agents.tool_factory import create_callable_thread_tool
from nymeria.core.agent import set_current_agent
from nymeria.core.thread_agent_executor import handoff
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


def test_callable_tool_schema_exposes_handoff_arguments():
    tool = create_callable_thread_tool(
        ThreadConfig(
            thread_id="user-agent-thread",
            callable=True,
            callable_name="UserAgent",
        )
    )

    assert tool.name == "UserAgent"
    assert sorted(tool.args.keys()) == ["if_busy", "mode", "scheduled_for", "task"]


def test_delayed_handoff_creates_scheduled_todo_on_target_thread(tmp_path: Path):
    agent = FakeAgent(tmp_path, target_thread_id="user-agent-thread")
    set_current_agent(agent)
    try:
        result = handoff(
            "user-agent-thread",
            "Tell the user an important email arrived.",
            "owner",
            "UserAgent",
            caller_thread_id="outlook-agent-thread",
            caller_name="OutlookAgent",
            scheduled_for="30s",
        )
    finally:
        set_current_agent(None)

    assert result.startswith("[HandedOff]:")
    assert "todo_id=" in result

    todos = agent.todo_manager.get_todos("owner").items
    assert len(todos) == 1
    assert todos[0].thread_id == "user-agent-thread"
    assert "Tell the user an important email arrived." in todos[0].task
    assert todos[0].notes is not None
    assert "source_thread_id: outlook-agent-thread" in todos[0].notes
    assert "source_thread_name: OutlookAgent" in todos[0].notes

    scheduled = agent._schedule_db.get_for_user("owner")
    assert len(scheduled) == 1
    assert scheduled[0].todo_id == todos[0].id
    assert scheduled[0].thread_id == "user-agent-thread"


def test_handoff_busy_error_does_not_schedule_or_start_background_work(tmp_path: Path):
    agent = FakeAgent(tmp_path, target_thread_id="user-agent-thread", busy=True)
    set_current_agent(agent)
    try:
        result = handoff(
            "user-agent-thread",
            "Tell the user an important email arrived.",
            "owner",
            "UserAgent",
            caller_thread_id="outlook-agent-thread",
            caller_name="OutlookAgent",
            if_busy="error",
        )
    finally:
        set_current_agent(None)

    assert result.startswith("[Busy]:")
    assert agent.todo_manager.get_todos("owner").items == []
    assert agent._schedule_db.get_for_user("owner") == []
