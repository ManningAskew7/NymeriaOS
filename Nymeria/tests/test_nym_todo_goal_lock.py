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


def test_todo_atomic_update_raises_on_save_failure(tmp_path: Path):
    """F11: a failed save inside ``TodoManager.atomic_update`` raises instead of
    silently dropping the mutation."""
    tm = TodoManager(tmp_path)
    tm.save_todos = lambda todo_list: False  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="Failed to persist TODOs"):
        with tm.atomic_update("u1") as todo_list:
            todo_list.add_item("task", created_by="user", thread_id="t1")


def test_todo_atomic_update_does_not_mask_body_exception(tmp_path: Path):
    """F11: an exception raised inside the block propagates (not masked by the
    save-failure check), and the save is still attempted in ``finally``."""
    tm = TodoManager(tmp_path)
    saves = {"n": 0}
    real_save = tm.save_todos

    def _counting_save(todo_list):
        saves["n"] += 1
        return real_save(todo_list)

    tm.save_todos = _counting_save  # type: ignore[method-assign]
    with pytest.raises(ValueError, match="boom"):
        with tm.atomic_update("u1"):
            raise ValueError("boom")
    # The save still ran in ``finally`` despite the body exception.
    assert saves["n"] == 1
