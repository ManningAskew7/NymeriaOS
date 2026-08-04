"""TODO management for Nymeria autonomous operation."""

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .keyed_locks import KeyedRLockMap
from .storage_paths import safe_path_segment, write_text_atomic
from .time_utils import ensure_aware_utc, utc_now

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .todo_schedule_db import TodoScheduleDB

# Thread-safe locks for todo operations (keyed by user_id)
_todo_locks = KeyedRLockMap()


class TodoStatus(str, Enum):
    """Status of a TODO item."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class TodoItem(BaseModel):
    """A single TODO item."""

    id: str = Field(..., description="8-character unique identifier")
    task: str = Field(..., max_length=500, description="Task description")
    status: TodoStatus = Field(default=TodoStatus.PENDING)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    notes: Optional[str] = Field(default=None, max_length=1000)

    # Scheduling fields - when set, Nymeria wakes up to work on this TODO
    scheduled_for: Optional[datetime] = Field(default=None, description="When to wake up and work on this TODO")
    thread_id: str = Field(..., description="Thread this TODO belongs to")
    last_execution: Optional[datetime] = Field(default=None, description="Last scheduled execution time")

    @model_validator(mode='before')
    @classmethod
    def _migrate_legacy_fields(cls, data: dict) -> dict:
        """Migrate old TODO data: backfill thread_id, strip removed fields, convert blocked→pending."""
        if isinstance(data, dict):
            # Backfill thread_id='legacy' for old TODOs that lack one
            if not data.get('thread_id'):
                data['thread_id'] = 'legacy'
            # Migrate blocked → pending
            if data.get('status') == 'blocked':
                data['status'] = 'pending'
                if data.get('blocked_reason'):
                    existing_notes = data.get('notes') or ''
                    migrated = f"[was blocked: {data['blocked_reason']}] {existing_notes}".strip()
                    data['notes'] = migrated[:1000]  # Respect max_length
            # Strip removed fields (Pydantic would reject unknown fields)
            for field in ('priority', 'deadline', 'blocked_reason', 'permanent'):
                data.pop(field, None)
        return data

    # User management & recurrence fields
    created_by: str = Field(default="agent", description="Who created this TODO: 'agent' or 'user'")
    recurrence: Optional[str] = Field(default=None, description="Recurrence interval as a duration string (e.g. '5m', '2h', '1d', '1w', '1mo'). Calendar months (Nmo) use calendar arithmetic; everything else is a fixed duration. Legacy preset names (hourly/daily/weekly/monthly, 5min/10min/15min/30min) are still accepted on input and resolved by todo_constants.")
    recurrence_anchor: Optional[datetime] = Field(default=None, description="Stable origin fire time for calendar-month (Nmo) recurrence. Next slots are derived as origin + N months so a month-end day (29-31) clamps to short months without drifting downward. Adopted lazily from the first fired slot and reset when the recurrence changes; unused for fixed-duration intervals.")

    # /goal integration: when set, this TODO is part of a supervised goal and
    # cannot be transitioned to `done` by anyone but the goal's supervisor
    # thread. Worker-side `nym_todo(status="done", ...)` calls are refused;
    # only `mark_task_done` (in goal_tools.py) and its authority check can
    # flip a goal-locked TODO to done.
    goal_id: Optional[str] = Field(default=None, description="Parent goal_id when this TODO is goal-locked")

    # Scheduled-workflow integration: when workflow_id is set, the ticker
    # runs that published workflow tool headlessly (no agent turn) instead
    # of prompting the agent with the task text. Recurrence, retries, and
    # the missed-work policy apply unchanged.
    workflow_id: Optional[str] = Field(
        default=None,
        description="Published workflow tool to run instead of an agent turn",
    )
    workflow_params: Optional[dict] = Field(
        default=None, description="Parameters bound to the scheduled workflow run"
    )

    @field_validator("created_at", "updated_at", "scheduled_for", "last_execution", "recurrence_anchor")
    @classmethod
    def _datetimes_as_utc(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        return ensure_aware_utc(value)

    def is_active(self) -> bool:
        """Check if this TODO is active (not done)."""
        return self.status != TodoStatus.DONE

    def is_scheduled(self) -> bool:
        """Check if this TODO has a pending scheduled execution."""
        return self.scheduled_for is not None and self.is_active()

    def is_stale(self, staleness_hours: int) -> bool:
        """Check if this TODO hasn't been updated in staleness_hours."""
        if not self.is_active():
            return False
        threshold = utc_now() - timedelta(hours=staleness_hours)
        return ensure_aware_utc(self.updated_at) < threshold

    def hours_since_update(self) -> float:
        """Get hours since last update."""
        delta = utc_now() - ensure_aware_utc(self.updated_at)
        return delta.total_seconds() / 3600


class TodoList(BaseModel):
    """User's TODO list containing all items."""

    user_id: str = Field(default="default")
    items: List[TodoItem] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _datetimes_as_utc(cls, value: datetime) -> datetime:
        return ensure_aware_utc(value)

    # Limits
    MAX_TODOS: int = 50

    def get_item(self, todo_id: str) -> Optional[TodoItem]:
        """Get a TODO item by ID."""
        for item in self.items:
            if item.id == todo_id:
                return item
        return None

    def add_item(
        self,
        task: str,
        scheduled_for: Optional[datetime] = None,
        thread_id: str = "legacy",
        created_by: str = "agent",
        recurrence: Optional[str] = None,
        notes: Optional[str] = None,
        workflow_id: Optional[str] = None,
        workflow_params: Optional[dict] = None,
    ) -> Optional[TodoItem]:
        """
        Add a new TODO item.

        Args:
            task: Task description
            scheduled_for: When Nymeria should wake up to work on this
            thread_id: Thread context for scheduled execution
            created_by: Who created this TODO ('agent' or 'user')
            recurrence: Recurrence interval as a canonical duration string
                (e.g. '5m', '2h', '1d'). Callers should pass values already
                validated by todo_constants.validate_recurrence.
            notes: Additional notes
            workflow_id: Published workflow tool the ticker runs headlessly
                instead of an agent turn. Callers should pass values already
                validated by workflows.tool_runtime.workflow_binding_error.
            workflow_params: Parameters bound to the scheduled workflow run

        Returns:
            The created TodoItem, or None if at limit.
        """
        # Check limit
        active_count = len([i for i in self.items if i.is_active()])
        if active_count >= self.MAX_TODOS:
            return None

        # Truncate task if too long
        task = task[:500]

        # Generate short ID
        todo_id = str(uuid.uuid4())[:8]

        item = TodoItem(
            id=todo_id,
            task=task,
            scheduled_for=scheduled_for,
            thread_id=thread_id,
            created_by=created_by,
            recurrence=recurrence,
            notes=notes[:1000] if notes else None,
            workflow_id=workflow_id,
            workflow_params=workflow_params,
        )
        self.items.append(item)
        self.updated_at = utc_now()
        return item

    def update_item(
        self,
        todo_id: str,
        status: Optional[TodoStatus] = None,
        notes: Optional[str] = None,
        task: Optional[str] = None,
        scheduled_for: Optional[datetime] = None,
        clear_schedule: bool = False,
        thread_id: Optional[str] = None,
        recurrence: Optional[str] = None,
        clear_recurrence: bool = False,
    ) -> bool:
        """
        Update a TODO item.

        Args:
            todo_id: ID of the TODO to update
            status: New status
            notes: Add or update notes
            task: Update task description
            scheduled_for: Set/update scheduled execution time
            clear_schedule: If True, removes the schedule
            thread_id: Update thread context for scheduled execution
            recurrence: Recurrence interval as a canonical duration string
                (e.g. '5m', '2h', '1d'). Callers should pass values already
                validated by todo_constants.validate_recurrence.
            clear_recurrence: If True, removes the recurrence

        Returns:
            True if successful, False if not found.
        """
        item = self.get_item(todo_id)
        if not item:
            return False

        if status is not None:
            item.status = status

        if notes is not None:
            item.notes = notes[:1000] if notes else None

        if task is not None:
            item.task = task[:500]

        if clear_schedule:
            item.scheduled_for = None
        elif scheduled_for is not None:
            item.scheduled_for = scheduled_for
        # thread_id is independent of the schedule: an unspecified one is
        # preserved (scoping stays even when the schedule is cleared), and an
        # explicit rebind applies even in the same patch as clear_schedule
        # (pre-#143 the clear branch silently dropped it).
        if thread_id is not None:
            item.thread_id = thread_id

        if clear_recurrence:
            item.recurrence = None
            item.recurrence_anchor = None
        elif recurrence is not None:
            if recurrence != item.recurrence:
                # Recurrence changed: drop the stale month-end origin so the
                # next scheduled fire re-adopts a fresh anchor.
                item.recurrence_anchor = None
            item.recurrence = recurrence

        item.updated_at = utc_now()
        self.updated_at = utc_now()
        return True

    def complete_item(self, todo_id: str) -> bool:
        """
        Mark a TODO item as done.

        Returns:
            True if successful, False if not found.
        """
        item = self.get_item(todo_id)
        if not item:
            return False

        item.status = TodoStatus.DONE
        item.updated_at = utc_now()
        self.updated_at = utc_now()
        return True

    def delete_item(self, todo_id: str) -> Optional[TodoItem]:
        """
        Delete a TODO item.

        Returns:
            The deleted item, or None if not found.
        """
        for i, item in enumerate(self.items):
            if item.id == todo_id:
                deleted = self.items.pop(i)
                self.updated_at = utc_now()
                return deleted
        return None

    def delete_items_for_thread(self, thread_id: str) -> List[TodoItem]:
        """
        Delete all TODO items scoped to a thread.

        Returns:
            Deleted TodoItem objects.
        """
        deleted = [item for item in self.items if item.thread_id == thread_id]
        if deleted:
            self.items = [item for item in self.items if item.thread_id != thread_id]
            self.updated_at = utc_now()
        return deleted

    def get_active_todos(self) -> List[TodoItem]:
        """Get all active (non-done) TODO items."""
        return [item for item in self.items if item.is_active()]

    def get_active_todos_for_thread(self, thread_id: str) -> List[TodoItem]:
        """Get active TODO items scoped to a specific thread."""
        return [t for t in self.items if t.thread_id == thread_id and t.is_active()]

    def get_thread_task_counts(self) -> Dict[str, int]:
        """Get active task count per thread_id for badge display."""
        counts: Dict[str, int] = {}
        for item in self.items:
            if item.is_active():
                counts[item.thread_id] = counts.get(item.thread_id, 0) + 1
        return counts

    def get_stale_todos(self, staleness_hours: int) -> List[TodoItem]:
        """Get TODO items that haven't been updated in staleness_hours."""
        return [item for item in self.items if item.is_stale(staleness_hours)]

    def get_scheduled_todos(self) -> List[TodoItem]:
        """Get TODO items that have a scheduled execution time."""
        return [item for item in self.items if item.is_scheduled()]

    def archive_completed(self, days_old: int = 7) -> int:
        """
        Remove completed items older than days_old.

        Returns:
            Number of items archived.
        """
        threshold = utc_now() - timedelta(days=days_old)
        original_count = len(self.items)

        self.items = [
            item
            for item in self.items
            if item.status != TodoStatus.DONE or ensure_aware_utc(item.updated_at) > threshold
        ]

        archived = original_count - len(self.items)
        if archived > 0:
            self.updated_at = utc_now()
        return archived


class TodoManager:
    """Manages TODO lists on disk with thread-safe operations."""

    def __init__(self, data_dir: Path):
        """
        Initialize the TODO manager.

        Args:
            data_dir: Base data directory (TODOs stored in data_dir/todos/)
        """
        self.todos_dir = data_dir / "todos"
        self.todos_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"TodoManager initialized with directory: {self.todos_dir}")

    def _get_lock(self, user_id: str) -> threading.RLock:
        """Get or create a lock for a specific user."""
        return _todo_locks.get(user_id)

    @contextmanager
    def atomic_update(self, user_id: str = "default"):
        """
        Context manager for atomic TODO list updates.

        Usage:
            with manager.atomic_update("default") as todo_list:
                todo_list.add_item("New task")
            # List is automatically saved when exiting the context

        This ensures that multiple concurrent modifications don't overwrite each other.

        Raises ``RuntimeError`` if the save fails, so a disk error is surfaced
        rather than silently dropping the mutation (matching the
        ``delete_todos_for_thread`` contract). The save still runs in
        ``finally``, but its result is only raised on the success path, so an
        exception from inside the block propagates first and is never masked.
        """
        lock = self._get_lock(user_id)
        with lock:
            todo_list = self.get_todos(user_id)
            try:
                yield todo_list
            finally:
                saved_ok = self.save_todos(todo_list)
            if not saved_ok:
                raise RuntimeError(f"Failed to persist TODOs for user {user_id}")

    def _get_todos_path(self, user_id: str) -> Path:
        """Get the path to a user's TODO file."""
        safe_user_id = safe_path_segment(user_id)
        return self.todos_dir / f"{safe_user_id}.json"

    def get_todos(self, user_id: str = "default") -> TodoList:
        """
        Load a user's TODO list from disk, or create a new one if it doesn't exist.

        Args:
            user_id: User identifier (defaults to "default")

        Returns:
            TodoList instance
        """
        todos_path = self._get_todos_path(user_id)

        if todos_path.exists():
            try:
                with open(todos_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                todo_list = TodoList.model_validate(data)
                logger.debug(f"Loaded TODO list for user: {user_id}")
                return todo_list
            except Exception as e:
                logger.error(f"Failed to load TODOs for {user_id}: {e}")
                # Return new list on error
                return TodoList(user_id=user_id)
        else:
            logger.debug(f"Creating new TODO list for user: {user_id}")
            return TodoList(user_id=user_id)

    def save_todos(self, todo_list: TodoList) -> bool:
        """
        Save a user's TODO list to disk.

        Args:
            todo_list: TodoList to save

        Returns:
            True if successful
        """
        todos_path = self._get_todos_path(todo_list.user_id)

        try:
            # Ensure directory exists
            todos_path.parent.mkdir(parents=True, exist_ok=True)

            # Update timestamp
            todo_list.updated_at = utc_now()

            write_text_atomic(
                todos_path,
                json.dumps(todo_list.model_dump(mode="json"), indent=2, default=str),
            )
            logger.debug(f"Saved TODO list for user: {todo_list.user_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to save TODOs for {todo_list.user_id}: {e}")
            return False

    def get_all_users_with_todos(self) -> List[str]:
        """Get all user IDs that have at least one TODO item."""
        users = []
        if self.todos_dir.exists():
            for path in self.todos_dir.iterdir():
                if path.is_file() and path.suffix == ".json":
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                    except Exception as e:
                        logger.warning("Skipping unreadable TODO list %s: %s", path, e)
                        continue

                    if data.get("items"):
                        users.append(path.stem)
        return sorted(users)

    def delete_todos(self, user_id: str) -> bool:
        """
        Delete a user's TODO list.

        Args:
            user_id: User identifier

        Returns:
            True if deleted, False if not found
        """
        todos_path = self._get_todos_path(user_id)

        if todos_path.exists():
            try:
                todos_path.unlink()
                logger.info(f"Deleted TODO list for user: {user_id}")
                return True
            except Exception as e:
                logger.error(f"Failed to delete TODOs for {user_id}: {e}")
                return False
        return False

    def delete_todos_for_thread(self, user_id: str, thread_id: str) -> List[TodoItem]:
        """
        Delete every TODO for ``user_id`` that belongs to ``thread_id``.

        Returns:
            Deleted TODO items.
        """
        lock = self._get_lock(user_id)
        with lock:
            todo_list = self.get_todos(user_id)
            deleted = todo_list.delete_items_for_thread(thread_id)
            if deleted:
                if not self.save_todos(todo_list):
                    raise RuntimeError(
                        f"Failed to save TODO cleanup for user {user_id}"
                    )
            return deleted

    # =========================================================================
    # Schedule synchronization methods
    # =========================================================================

    def sync_schedule_to_db(
        self,
        user_id: str,
        todo_id: str,
        schedule_db: "TodoScheduleDB",
    ) -> None:
        """
        Sync a TODO's schedule to the schedule database.

        Called after adding/updating a TODO with scheduled_for.

        Args:
            user_id: User ID
            todo_id: TODO ID to sync
            schedule_db: TodoScheduleDB instance
        """

        todo_list = self.get_todos(user_id)
        todo = todo_list.get_item(todo_id)

        if todo and todo.scheduled_for and todo.is_active():
            schedule_db.add_scheduled(
                todo_id=todo.id,
                user_id=user_id,
                scheduled_for=todo.scheduled_for,
                task_preview=todo.task[:100],
                thread_id=todo.thread_id,
            )
        else:
            # No schedule or not active - remove from index
            schedule_db.remove_scheduled(todo_id)

    def clear_todo_schedule(
        self,
        user_id: str,
        todo_id: str,
        schedule_db: "TodoScheduleDB",
    ) -> bool:
        """
        Clear a TODO's schedule after execution.

        Sets last_execution to now, clears scheduled_for.

        Args:
            user_id: User ID
            todo_id: TODO ID
            schedule_db: TodoScheduleDB instance

        Returns:
            True if successful
        """

        with self.atomic_update(user_id) as todo_list:
            todo = todo_list.get_item(todo_id)
            if todo:
                todo.last_execution = utc_now()
                todo.scheduled_for = None
                schedule_db.remove_scheduled(todo_id)
                return True
        return False

    def migrate_unscoped_todos(self, user_id: str, default_thread_id: str = "legacy") -> int:
        """
        Migrate TODOs that have no thread_id to the given default.

        This is idempotent — the model_validator already backfills 'legacy',
        but calling this ensures the file on disk is updated too.

        Returns:
            Number of items migrated.
        """
        migrated = 0
        with self.atomic_update(user_id) as todo_list:
            for item in todo_list.items:
                if not item.thread_id or item.thread_id == "legacy":
                    if item.thread_id != default_thread_id:
                        item.thread_id = default_thread_id
                        migrated += 1
        if migrated:
            logger.info(f"Migrated {migrated} unscoped TODO(s) for user {user_id} -> thread '{default_thread_id}'")
        return migrated

    def get_todo_by_id(self, user_id: str, todo_id: str) -> Optional[TodoItem]:
        """
        Get a specific TODO item by ID.

        Args:
            user_id: User ID
            todo_id: TODO ID

        Returns:
            TodoItem or None if not found
        """
        todo_list = self.get_todos(user_id)
        return todo_list.get_item(todo_id)
