from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nymeria.core.activity_log import ActivityType
from nymeria.core.time_utils import utc_now
from nymeria.core.todo_manager import TodoManager, TodoStatus
from nymeria.core.todo_schedule_db import TodoScheduleDB
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


def _seed_rearmed_recurring(
    manager: TodoManager,
    schedule_db: TodoScheduleDB,
    *,
    slot: datetime,
    last_execution: datetime | None,
):
    """Seed one daily recurring TODO plus its schedule-index row at ``slot``."""
    with manager.atomic_update("owner") as todo_list:
        recurring = todo_list.add_item(
            "Hound Alex about his meds",
            scheduled_for=slot,
            thread_id="thread-a",
            recurrence="1d",
        )
        assert recurring is not None
        recurring.last_execution = last_execution
    schedule_db.add_scheduled(
        todo_id=recurring.id,
        user_id="owner",
        scheduled_for=slot,
        task_preview="Hound Alex about his meds",
        thread_id="thread-a",
    )
    return recurring.id


def _row_time(schedule_db: TodoScheduleDB, todo_id: str) -> datetime:
    entry = schedule_db.get_entry(todo_id)
    assert entry is not None, "TODO fell out of the schedule index"
    return datetime.fromtimestamp(entry.scheduled_for, timezone.utc)


def test_nym_todo_recurring_done_after_ticker_rearm_keeps_the_armed_slot(
    tmp_path: Path,
    monkeypatch,
):
    """A late "done" must not advance a schedule the ticker already advanced.

    The ticker re-arms on every successful run (``Ticker._handle_recurrence``),
    so an agent that marks the occurrence done AFTER the turn finalized is a
    SECOND writer for the SAME occurrence. Anchoring on the schedule row (or on
    ``item.scheduled_for``, which the re-arm moved too) advanced day N+1 to day
    N+2 and consumed day N+1 without ever dispatching it: five medication
    reminders were silently skipped in production before 2026-08-26.
    """
    manager = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", manager)
    schedule_db = TodoScheduleDB(tmp_path / "todo_schedule.db")
    monkeypatch.setattr(todo_tools, "_get_schedule_db", lambda: schedule_db)

    day_n = utc_now().replace(microsecond=0) - timedelta(hours=3)
    day_n_plus_1 = day_n + timedelta(days=1)
    todo_id = _seed_rearmed_recurring(
        manager, schedule_db, slot=day_n_plus_1, last_execution=day_n
    )

    result = todo_tools.nym_todo.func(
        todo_id=todo_id,
        status="done",
        notes="confirmed taken, 50 minutes late",
        config=_config("thread-a"),
    )

    updated = manager.get_todos("owner").get_item(todo_id)
    assert result.startswith(f"[Updated]: TODO {todo_id}")
    assert updated.status == TodoStatus.PENDING
    # Day N+1 survives, in the item AND in the index the ticker actually polls.
    assert updated.scheduled_for == day_n_plus_1
    assert updated.last_execution == day_n
    assert _row_time(schedule_db, todo_id) == day_n_plus_1


def test_nym_todo_recurring_done_before_ticker_rearm_lands_the_same_slot(
    tmp_path: Path,
    monkeypatch,
):
    """The mid-turn ordering reaches the SAME slot as the late one.

    Marking done during the scheduled turn leaves ``last_execution`` at day N-1
    (the previous run stamped it) and ``scheduled_for`` at the day N slot that
    is currently firing. Advancing from day N-1 lands on day N, which is already
    past, so ``calculate_next_recurrence_time``'s skip-forward normalizes it to
    day N+1: the same answer the post-finalize ordering gives, which is what
    makes the two writers idempotent instead of additive.

    Teeth: this pins the advance HAPPENING in the mid-turn ordering (red if
    the done-path stops rescheduling). It cannot discriminate the anchor
    choice, because here both anchors land the same slot by design; the
    late-done test above and the frozen-clock origin test in
    test_api_todos_router.py carry that regression.
    """
    manager = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", manager)
    schedule_db = TodoScheduleDB(tmp_path / "todo_schedule.db")
    monkeypatch.setattr(todo_tools, "_get_schedule_db", lambda: schedule_db)

    day_n = utc_now().replace(microsecond=0) - timedelta(minutes=3)
    todo_id = _seed_rearmed_recurring(
        manager,
        schedule_db,
        slot=day_n,
        last_execution=day_n - timedelta(days=1),
    )

    todo_tools.nym_todo.func(
        todo_id=todo_id,
        status="done",
        config=_config("thread-a"),
    )

    updated = manager.get_todos("owner").get_item(todo_id)
    assert updated.status == TodoStatus.PENDING
    assert updated.scheduled_for == day_n + timedelta(days=1)
    assert updated.last_execution == day_n - timedelta(days=1)
    assert _row_time(schedule_db, todo_id) == day_n + timedelta(days=1)


def test_nym_todo_done_on_paused_schedule_does_not_resume(
    tmp_path: Path,
    monkeypatch,
):
    """#154: "done" on an auto-paused recurring TODO must not re-arm it.

    The reschedule would write ``scheduled_for``, and ``update_item``'s
    resume-clear would then erase the pause marker and the failure streak:
    completion must not be a silent resume. Mirrors the guards the REST
    complete path and ``/todos complete`` already carry, which this tool
    path had drifted from.
    """
    manager = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", manager)
    schedule_db = TodoScheduleDB(tmp_path / "todo_schedule.db")
    monkeypatch.setattr(todo_tools, "_get_schedule_db", lambda: schedule_db)

    day_n = utc_now().replace(microsecond=0) - timedelta(hours=3)
    day_n_plus_1 = day_n + timedelta(days=1)
    todo_id = _seed_rearmed_recurring(
        manager, schedule_db, slot=day_n_plus_1, last_execution=day_n
    )
    paused_at = utc_now().replace(microsecond=0)
    with manager.atomic_update("owner") as todo_list:
        todo_list.get_item(todo_id).schedule_paused_at = paused_at

    result = todo_tools.nym_todo.func(
        todo_id=todo_id,
        status="done",
        config=_config("thread-a"),
    )

    updated = manager.get_todos("owner").get_item(todo_id)
    assert result.startswith(f"[Updated]: TODO {todo_id}")
    # Completed as DONE with the pause intact: no PENDING flip, no re-arm,
    # no erased pause marker. Resume stays an explicit reschedule.
    assert updated.status == TodoStatus.DONE
    assert updated.schedule_paused_at == paused_at
    assert updated.scheduled_for == day_n_plus_1
    assert updated.last_execution == day_n


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


def _write_raw_store(manager: TodoManager, user_id: str, payload: dict) -> None:
    """Write a TODO store JSON straight to disk, bypassing the models."""
    path = manager.todos_dir / f"{user_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_legacy_goal_id_todo_still_loads_lists_and_completes(
    tmp_path: Path,
    monkeypatch,
):
    """A store written before the /goal subsystem was removed carries a
    serialized ``goal_id`` on its items. That key must not strand the item:
    the whole store still loads (a validation failure would silently return
    an EMPTY list from get_todos and lose every other TODO with it), the
    item lists, and marking it done succeeds now that the supervisor lock
    is gone.
    """
    manager = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", manager)

    _write_raw_store(
        manager,
        "owner",
        {
            "user_id": "owner",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "items": [
                {
                    "id": "legacy01",
                    "task": "Legacy goal-locked work",
                    "status": "pending",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "thread_id": "thread-a",
                    "goal_id": "goal-abc123",
                },
                {
                    "id": "plain001",
                    "task": "Ordinary neighbour",
                    "status": "pending",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "thread_id": "thread-a",
                },
            ],
        },
    )

    # Loads: both items survive the round trip, so the neighbour is not
    # collateral damage of an unknown key on the first item.
    loaded = manager.get_todos("owner")
    assert [item.id for item in loaded.items] == ["legacy01", "plain001"]
    legacy = loaded.get_item("legacy01")
    assert legacy is not None
    assert legacy.task == "Legacy goal-locked work"

    # Lists: the tool surface shows it like any other pending TODO.
    listing = todo_tools.nym_todo_list.func(config=_config("thread-a"))
    assert "Legacy goal-locked work" in listing
    assert "Ordinary neighbour" in listing

    # Completes: no supervisor exists any more, so the done transition must
    # go through rather than be refused.
    result = todo_tools.nym_todo.func(
        todo_id="legacy01",
        status="done",
        config=_config("thread-a"),
    )
    assert "[Error]" not in result
    completed = manager.get_todos("owner").get_item("legacy01")
    assert completed is not None
    assert completed.status == TodoStatus.DONE


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
