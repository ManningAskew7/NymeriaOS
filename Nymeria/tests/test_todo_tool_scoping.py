from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from nymeria.core.time_utils import utc_now
from nymeria.core.todo_manager import TodoManager, TodoStatus
from nymeria.tools import todo as todo_tools


def _config(thread_id: str) -> dict:
    return {"configurable": {"user_id": "owner", "thread_id": thread_id}}


def test_nym_todo_list_is_scoped_to_current_thread(
    tmp_path: Path,
    monkeypatch,
):
    manager = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", manager)

    with manager.atomic_update("owner") as todo_list:
        current_active = todo_list.add_item("Current thread active", thread_id="thread-a")
        current_done = todo_list.add_item("Current thread done", thread_id="thread-a")
        other_active = todo_list.add_item("Other thread active", thread_id="thread-b")
        other_done = todo_list.add_item("Other thread done", thread_id="thread-b")

        assert current_active is not None
        assert current_done is not None
        assert other_active is not None
        assert other_done is not None

        todo_list.update_item(current_done.id, status=TodoStatus.DONE)
        todo_list.update_item(other_done.id, status=TodoStatus.DONE)

    default_result = todo_tools.nym_todo_list.func(config=_config("thread-a"))
    assert "Current thread active" in default_result
    assert "Current thread done" not in default_result
    assert "Other thread active" not in default_result
    assert "Other thread done" not in default_result

    all_result = todo_tools.nym_todo_list.func(
        filter_status="all",
        config=_config("thread-a"),
    )
    assert "Current thread active" in all_result
    assert "Current thread done" in all_result
    assert "Other thread active" not in all_result
    assert "Other thread done" not in all_result

    done_result = todo_tools.nym_todo_list.func(
        filter_status="done",
        config=_config("thread-a"),
    )
    assert "Current thread active" not in done_result
    assert "Current thread done" in done_result
    assert "Other thread done" not in done_result


def test_nym_todo_list_empty_message_mentions_current_thread(
    tmp_path: Path,
    monkeypatch,
):
    manager = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", manager)

    with manager.atomic_update("owner") as todo_list:
        assert todo_list.add_item("Other thread active", thread_id="thread-b") is not None

    result = todo_tools.nym_todo_list.func(config=_config("thread-a"))

    assert result == "[Info]: No active TODOs for this thread. Use nym_todo to create tasks."


def test_nym_todo_update_cannot_modify_another_thread(
    tmp_path: Path,
    monkeypatch,
):
    manager = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", manager)

    with manager.atomic_update("owner") as todo_list:
        current = todo_list.add_item("Current thread task", thread_id="thread-a")
        other = todo_list.add_item("Other thread task", thread_id="thread-b")
        assert current is not None
        assert other is not None

    wrong_thread_result = todo_tools.nym_todo.func(
        todo_id=other.id,
        status="done",
        config=_config("thread-a"),
    )
    assert wrong_thread_result == (
        f"[Error]: TODO '{other.id}' not found for this thread. "
        "Use nym_todo_list to see available TODOs."
    )

    todo_list = manager.get_todos("owner")
    assert todo_list.get_item(other.id).status == TodoStatus.PENDING

    current_result = todo_tools.nym_todo.func(
        todo_id=current.id,
        status="done",
        config=_config("thread-a"),
    )
    assert current_result.startswith(f"[Updated]: TODO {current.id}")
    assert manager.get_todos("owner").get_item(current.id).status == TodoStatus.DONE


def test_nym_todo_recurring_done_preserves_scheduled_anchor(
    tmp_path: Path,
    monkeypatch,
):
    manager = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", manager)
    scheduled_anchor = utc_now() - timedelta(minutes=3)

    with manager.atomic_update("owner") as todo_list:
        recurring = todo_list.add_item(
            "Hourly recurring task",
            scheduled_for=scheduled_anchor,
            thread_id="thread-a",
            recurrence="hourly",
        )
        assert recurring is not None

    result = todo_tools.nym_todo.func(
        todo_id=recurring.id,
        status="done",
        config=_config("thread-a"),
    )

    updated = manager.get_todos("owner").get_item(recurring.id)
    assert result.startswith(f"[Updated]: TODO {recurring.id}")
    assert updated.status == TodoStatus.PENDING
    assert updated.scheduled_for == scheduled_anchor + timedelta(hours=1)
    assert updated.last_execution == scheduled_anchor


def test_nym_todo_delete_cannot_remove_another_thread(
    tmp_path: Path,
    monkeypatch,
):
    manager = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", manager)

    with manager.atomic_update("owner") as todo_list:
        current = todo_list.add_item("Current thread task", thread_id="thread-a")
        other = todo_list.add_item("Other thread task", thread_id="thread-b")
        assert current is not None
        assert other is not None

    wrong_thread_result = todo_tools.nym_todo_delete.func(
        todo_id=other.id,
        config=_config("thread-a"),
    )
    assert wrong_thread_result == (
        f"[Error]: TODO '{other.id}' not found for this thread. "
        "Use nym_todo_list to see available TODOs."
    )

    todo_list = manager.get_todos("owner")
    assert todo_list.get_item(current.id) is not None
    assert todo_list.get_item(other.id) is not None

    current_result = todo_tools.nym_todo_delete.func(
        todo_id=current.id,
        config=_config("thread-a"),
    )
    assert current_result == "[Deleted]: Current thread task"

    todo_list = manager.get_todos("owner")
    assert todo_list.get_item(current.id) is None
    assert todo_list.get_item(other.id) is not None
