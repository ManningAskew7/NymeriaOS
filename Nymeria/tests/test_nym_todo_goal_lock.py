"""Tests for the /goal structural lock on nym_todo done transitions.

A goal-locked TodoItem (one with `goal_id` set) cannot be flipped to `done`
except by the goal's supervisor thread — the worker thread is rejected at the
tool's atomic-update boundary.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableConfig

from nymeria.core.goal_manager import GoalManager, set_goal_manager
from nymeria.core.todo_manager import TodoItem, TodoManager, TodoStatus
from nymeria.tools import todo as todo_tools
from nymeria.tools.todo import nym_todo


def _config(thread_id: str, user_id: str = "u1") -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id, "user_id": user_id}}


@pytest.fixture
def setup(tmp_path: Path, monkeypatch):
    """Set up GoalManager + TodoManager + an active goal + a goal-locked todo."""
    gm = GoalManager(tmp_path)
    set_goal_manager(gm)

    tm = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", tm)

    # Create + approve a goal so we have worker + supervisor threads.
    goal = gm.create_goal("u1", "worker-X", "objective")
    gm.add_task("u1", goal.goal_id, "seed task")
    gm.approve_goal("u1", goal.goal_id, "supervisor-S")

    # Seed a goal-locked TodoItem owned by the worker thread.
    with tm.atomic_update("u1") as todo_list:
        item = TodoItem(
            id="todo0001",
            task="locked work",
            thread_id="worker-X",
            goal_id=goal.goal_id,
        )
        todo_list.items.append(item)

    yield SimpleNamespace(gm=gm, tm=tm, goal_id=goal.goal_id)
    set_goal_manager(None)


def test_worker_done_transition_refused_on_goal_locked_todo(setup):
    result = nym_todo.invoke(
        {"todo_id": "todo0001", "status": "done"},
        config=_config("worker-X"),
    )
    assert "[Error]" in result
    assert "locked under goal" in result
    item = setup.tm.get_todos("u1").get_item("todo0001")
    assert item is not None
    assert item.status == TodoStatus.PENDING


def test_worker_in_progress_transition_allowed_on_goal_locked_todo(setup):
    """The lock is specifically for `done`; other transitions still work."""
    result = nym_todo.invoke(
        {"todo_id": "todo0001", "status": "in_progress"},
        config=_config("worker-X"),
    )
    # Should succeed (no error prefix)
    assert "[Error]" not in result
    item = setup.tm.get_todos("u1").get_item("todo0001")
    assert item is not None
    assert item.status == TodoStatus.IN_PROGRESS


def test_unlocked_todo_done_transition_works(setup):
    """A regular (non-goal-locked) TODO can still be marked done normally."""
    with setup.tm.atomic_update("u1") as todo_list:
        todo_list.items.append(
            TodoItem(id="todo0002", task="unlocked", thread_id="worker-X")
        )
    result = nym_todo.invoke(
        {"todo_id": "todo0002", "status": "done"},
        config=_config("worker-X"),
    )
    assert "[Error]" not in result
    item = setup.tm.get_todos("u1").get_item("todo0002")
    assert item is not None
    assert item.status == TodoStatus.DONE


def test_goal_lock_blocks_pre_approval_too(tmp_path: Path, monkeypatch):
    """Before /goal approve, helper_thread_id is None — no thread should
    be able to mark a goal-locked TODO done (worker OR any other thread)."""
    gm = GoalManager(tmp_path)
    set_goal_manager(gm)
    try:
        tm = TodoManager(tmp_path)
        monkeypatch.setattr(todo_tools, "_todo_manager", tm)
        goal = gm.create_goal("u1", "worker-X", "obj")  # NO approve

        with tm.atomic_update("u1") as todo_list:
            todo_list.items.append(
                TodoItem(
                    id="locked-no-supervisor",
                    task="pre-approval lock",
                    thread_id="worker-X",
                    goal_id=goal.goal_id,
                )
            )

        result = nym_todo.invoke(
            {"todo_id": "locked-no-supervisor", "status": "done"},
            config=_config("worker-X"),
        )
        assert "[Error]" in result
        assert "locked under goal" in result
    finally:
        set_goal_manager(None)
