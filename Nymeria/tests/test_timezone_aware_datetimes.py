from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nymeria.core.thread_config import TemporaryToolEntry
from nymeria.core.time_utils import ensure_aware_utc, utc_now
from nymeria.core.todo_manager import TodoList, TodoStatus
from nymeria.core.trigger_manager import TriggerAction, TriggerDefinition


def test_utc_now_returns_timezone_aware_utc_datetime():
    now = utc_now()

    assert now.tzinfo is timezone.utc
    assert now.utcoffset() == timedelta(0)


def test_ensure_aware_utc_treats_legacy_naive_values_as_utc():
    legacy = datetime(2026, 5, 3, 12, 30, 0)

    normalized = ensure_aware_utc(legacy)

    assert normalized == datetime(2026, 5, 3, 12, 30, 0, tzinfo=timezone.utc)


def test_todo_datetimes_normalize_legacy_naive_json_values():
    old_updated = (utc_now() - timedelta(hours=3)).replace(tzinfo=None)
    todo_list = TodoList.model_validate(
        {
            "user_id": "owner",
            "created_at": old_updated.isoformat(),
            "updated_at": old_updated.isoformat(),
            "items": [
                {
                    "id": "todo-1",
                    "task": "Check stale handling",
                    "status": "pending",
                    "created_at": old_updated.isoformat(),
                    "updated_at": old_updated.isoformat(),
                    "scheduled_for": old_updated.isoformat(),
                    "last_execution": old_updated.isoformat(),
                    "thread_id": "thread-1",
                }
            ],
        }
    )

    item = todo_list.items[0]
    assert item.updated_at.tzinfo is timezone.utc
    assert item.scheduled_for is not None
    assert item.scheduled_for.tzinfo is timezone.utc
    assert item.is_stale(1)
    assert item.hours_since_update() >= 3


def test_archive_completed_tolerates_in_memory_naive_datetimes():
    todo_list = TodoList(user_id="owner")
    old_done = todo_list.add_item("Old completed", thread_id="thread-1")
    recent_done = todo_list.add_item("Recent completed", thread_id="thread-1")
    active = todo_list.add_item("Still active", thread_id="thread-1")
    assert old_done is not None
    assert recent_done is not None
    assert active is not None

    old_done.status = TodoStatus.DONE
    old_done.updated_at = (utc_now() - timedelta(days=4)).replace(tzinfo=None)
    recent_done.status = TodoStatus.DONE
    recent_done.updated_at = (utc_now() - timedelta(days=2)).replace(tzinfo=None)

    archived = todo_list.archive_completed(days_old=3)

    remaining_ids = {item.id for item in todo_list.items}
    assert archived == 1
    assert old_done.id not in remaining_ids
    assert recent_done.id in remaining_ids
    assert active.id in remaining_ids


def test_other_persisted_models_normalize_naive_datetime_fields():
    expires_at = (utc_now() + timedelta(minutes=10)).replace(tzinfo=None)
    entry = TemporaryToolEntry.model_validate({"expires_at": expires_at.isoformat()})
    trigger = TriggerDefinition(
        id="trig-1",
        name="Cooldown",
        source_type="webhook",
        action=TriggerAction(type="notify", config={}),
        last_fired=expires_at,
    )

    assert entry.expires_at.tzinfo is timezone.utc
    assert trigger.last_fired is not None
    assert trigger.last_fired.tzinfo is timezone.utc
