from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from nymeria.core.activity_log import ActivityType
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


@pytest.mark.parametrize(
    ("recurrence", "delta"),
    [
        ("hourly", timedelta(hours=1)),
        ("2h", timedelta(hours=2)),
    ],
)
def test_nym_todo_recurring_done_preserves_scheduled_anchor(
    tmp_path: Path,
    monkeypatch,
    recurrence: str,
    delta: timedelta,
):
    manager = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", manager)
    scheduled_anchor = utc_now() - timedelta(minutes=3)

    with manager.atomic_update("owner") as todo_list:
        recurring = todo_list.add_item(
            "Recurring task",
            scheduled_for=scheduled_anchor,
            thread_id="thread-a",
            recurrence=recurrence,
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
    assert updated.scheduled_for == scheduled_anchor + delta
    assert updated.last_execution == scheduled_anchor


def test_todo_complete_internal_reschedules_recurring(
    tmp_path: Path,
    monkeypatch,
):
    """The MCP completion path shares _advance_recurring_done with the nym_todo
    update path, so a recurring TODO completed via _todo_complete_internal is
    rescheduled to the next slot with last_execution stamped to the prior slot."""
    manager = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", manager)
    scheduled_anchor = utc_now() - timedelta(minutes=3)

    with manager.atomic_update("owner") as todo_list:
        recurring = todo_list.add_item(
            "Recurring task",
            scheduled_for=scheduled_anchor,
            thread_id="thread-a",
            recurrence="2h",
        )
        assert recurring is not None

    result = todo_tools._todo_complete_internal(recurring.id, "owner")

    updated = manager.get_todos("owner").get_item(recurring.id)
    assert result.startswith("[Completed]:")
    assert "auto-rescheduled" in result.lower()
    assert updated.status == TodoStatus.PENDING
    assert updated.scheduled_for == scheduled_anchor + timedelta(hours=2)
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


def test_todo_activity_entries_carry_thread_id(
    tmp_path: Path,
    monkeypatch,
):
    """Every todo activity event (added/updated/completed/deleted) is tagged with
    the todo's thread so the dashboard activity feed can resolve a thread badge
    and per-thread activity views include them (parity with trigger/scheduled
    runs, which already pass thread_id)."""
    manager = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", manager)

    calls: list[tuple] = []

    def _capture(activity_type, message, **kwargs):
        calls.append((activity_type, kwargs))

    monkeypatch.setattr(todo_tools, "log_activity", _capture)

    def _thread_id_for(activity_type) -> object:
        matches = [kw for at, kw in calls if at is activity_type]
        assert matches, f"no {activity_type} activity recorded"
        return matches[-1].get("thread_id")

    add_result = todo_tools.nym_todo.func(
        task="Buy milk",
        scheduled_for="1h",
        config=_config("thread-a"),
    )
    assert add_result.startswith("[Added]:")
    assert _thread_id_for(ActivityType.TODO_ADDED) == "thread-a"

    todo_id = [kw for at, kw in calls if at is ActivityType.TODO_ADDED][-1][
        "metadata"
    ]["todo_id"]

    todo_tools.nym_todo.func(
        todo_id=todo_id,
        status="in_progress",
        config=_config("thread-a"),
    )
    assert _thread_id_for(ActivityType.TODO_UPDATED) == "thread-a"

    # Completion via the MCP internal path (no thread in scope) tags from the
    # item's own thread_id.
    todo_tools._todo_complete_internal(todo_id, "owner")
    assert _thread_id_for(ActivityType.TODO_COMPLETED) == "thread-a"

    todo_tools.nym_todo_delete.func(todo_id=todo_id, config=_config("thread-a"))
    assert _thread_id_for(ActivityType.TODO_DELETED) == "thread-a"


def test_nym_todo_workflow_binding_is_create_only(tmp_path: Path, monkeypatch):
    """Phase 4: nym_todo validates workflow bindings at create time and
    refuses to change them on update."""
    manager = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", manager)

    monkeypatch.setattr(
        "nymeria.core.workflows.tool_runtime.workflow_binding_error",
        lambda wf, p, *, allow_event=False: "no published workflow tool named 'wf_x'",
    )
    rejected = todo_tools.nym_todo.func(
        task="Run report",
        scheduled_for="30m",
        workflow_id="wf_x",
        config=_config("thread-a"),
    )
    assert rejected.startswith("[Error]:")
    assert "no published workflow tool" in rejected

    monkeypatch.setattr(
        "nymeria.core.workflows.tool_runtime.workflow_binding_error",
        lambda wf, p, *, allow_event=False: None,
    )
    created = todo_tools.nym_todo.func(
        task="Run report",
        scheduled_for="30m",
        workflow_id="wf_x",
        workflow_params={"channel": "ops"},
        config=_config("thread-a"),
    )
    assert created.startswith("[Added]:")
    assert "runs workflow 'wf_x'" in created
    todo_id = created.split("TODO ")[1].split(":")[0]

    item = manager.get_todo_by_id("owner", todo_id)
    assert item.workflow_id == "wf_x"
    assert item.workflow_params == {"channel": "ops"}

    update = todo_tools.nym_todo.func(
        todo_id=todo_id,
        workflow_id="wf_other",
        config=_config("thread-a"),
    )
    assert "create-only" in update
    assert manager.get_todo_by_id("owner", todo_id).workflow_id == "wf_x"
