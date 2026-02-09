"""TODO management for Nymeria autonomous operation."""

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

# Thread-safe locks for todo operations (keyed by user_id)
_todo_locks: Dict[str, threading.RLock] = {}
_locks_lock = threading.Lock()


class TodoStatus(str, Enum):
    """Status of a TODO item."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


class TodoPriority(str, Enum):
    """Priority level for a TODO item."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TodoItem(BaseModel):
    """A single TODO item."""

    id: str = Field(..., description="8-character unique identifier")
    task: str = Field(..., max_length=500, description="Task description")
    status: TodoStatus = Field(default=TodoStatus.PENDING)
    priority: Optional[TodoPriority] = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    deadline: Optional[datetime] = Field(default=None)
    notes: Optional[str] = Field(default=None, max_length=1000)
    blocked_reason: Optional[str] = Field(default=None, max_length=500)

    # Scheduling fields - when set, Nymeria wakes up to work on this TODO
    scheduled_for: Optional[datetime] = Field(default=None, description="When to wake up and work on this TODO")
    thread_id: str = Field(..., description="Thread this TODO belongs to")
    last_execution: Optional[datetime] = Field(default=None, description="Last scheduled execution time")

    @model_validator(mode='before')
    @classmethod
    def _migrate_thread_id(cls, data: dict) -> dict:
        """Backfill thread_id='legacy' for old TODOs that lack one."""
        if isinstance(data, dict) and not data.get('thread_id'):
            data['thread_id'] = 'legacy'
        return data

    # User management & recurrence fields
    created_by: str = Field(default="agent", description="Who created this TODO: 'agent' or 'user'")
    recurrence: Optional[str] = Field(default=None, description="Recurrence pattern: 'hourly', 'daily', 'weekly', 'monthly'")
    permanent: bool = Field(default=False, description="Permanent recurring TODO - cannot be completed, only deleted")

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
        threshold = datetime.utcnow() - timedelta(hours=staleness_hours)
        return self.updated_at < threshold

    def hours_since_update(self) -> float:
        """Get hours since last update."""
        delta = datetime.utcnow() - self.updated_at
        return delta.total_seconds() / 3600


class TodoList(BaseModel):
    """User's TODO list containing all items."""

    user_id: str = Field(default="default")
    items: List[TodoItem] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

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
        priority: Optional[TodoPriority] = None,
        deadline: Optional[datetime] = None,
        scheduled_for: Optional[datetime] = None,
        thread_id: str = "legacy",
        created_by: str = "agent",
        recurrence: Optional[str] = None,
        notes: Optional[str] = None,
        permanent: bool = False,
    ) -> Optional[TodoItem]:
        """
        Add a new TODO item.

        Args:
            task: Task description
            priority: Priority level
            deadline: Due date
            scheduled_for: When Nymeria should wake up to work on this
            thread_id: Thread context for scheduled execution
            created_by: Who created this TODO ('agent' or 'user')
            recurrence: Recurrence pattern ('hourly', 'daily', 'weekly', 'monthly')
            notes: Additional notes
            permanent: If True (requires recurrence), task cannot be completed

        Returns:
            The created TodoItem, or None if at limit.
        """
        # Check limit
        active_count = len([i for i in self.items if i.is_active()])
        if active_count >= self.MAX_TODOS:
            return None

        # Truncate task if too long
        task = task[:500]

        # Permanent requires recurrence
        if permanent and not recurrence:
            permanent = False

        # Generate short ID
        todo_id = str(uuid.uuid4())[:8]

        item = TodoItem(
            id=todo_id,
            task=task,
            priority=priority,
            deadline=deadline,
            scheduled_for=scheduled_for,
            thread_id=thread_id,
            created_by=created_by,
            recurrence=recurrence,
            notes=notes[:1000] if notes else None,
            permanent=permanent,
        )
        self.items.append(item)
        self.updated_at = datetime.utcnow()
        return item

    def update_item(
        self,
        todo_id: str,
        status: Optional[TodoStatus] = None,
        notes: Optional[str] = None,
        blocked_reason: Optional[str] = None,
        priority: Optional[TodoPriority] = None,
        task: Optional[str] = None,
        scheduled_for: Optional[datetime] = None,
        clear_schedule: bool = False,
        thread_id: Optional[str] = None,
        recurrence: Optional[str] = None,
        clear_recurrence: bool = False,
        deadline: Optional[datetime] = None,
        clear_deadline: bool = False,
        permanent: Optional[bool] = None,
    ) -> bool:
        """
        Update a TODO item.

        Args:
            todo_id: ID of the TODO to update
            status: New status
            notes: Add or update notes
            blocked_reason: Why the task is blocked
            priority: New priority
            task: Update task description
            scheduled_for: Set/update scheduled execution time
            clear_schedule: If True, removes the schedule
            thread_id: Update thread context for scheduled execution
            recurrence: Recurrence pattern ('hourly', 'daily', 'weekly', 'monthly')
            clear_recurrence: If True, removes the recurrence
            deadline: Set/update deadline
            clear_deadline: If True, removes the deadline
            permanent: Set/clear permanent flag (requires recurrence)

        Returns:
            True if successful, False if not found or rejected.
        """
        item = self.get_item(todo_id)
        if not item:
            return False

        # Reject completion of permanent items
        if status == TodoStatus.DONE and item.permanent:
            return False

        if status is not None:
            item.status = status
            # Clear blocked_reason if not blocked
            if status != TodoStatus.BLOCKED:
                item.blocked_reason = None

        if notes is not None:
            item.notes = notes[:1000] if notes else None

        if blocked_reason is not None:
            item.blocked_reason = blocked_reason[:500] if blocked_reason else None
            if blocked_reason:
                item.status = TodoStatus.BLOCKED

        if priority is not None:
            item.priority = priority

        if task is not None:
            item.task = task[:500]

        if clear_schedule:
            item.scheduled_for = None
            # thread_id is preserved (scoping stays even when schedule is cleared)
        else:
            if scheduled_for is not None:
                item.scheduled_for = scheduled_for
            if thread_id is not None:
                item.thread_id = thread_id

        if clear_recurrence:
            item.recurrence = None
            item.permanent = False  # Auto-clear permanent when clearing recurrence
        elif recurrence is not None:
            item.recurrence = recurrence

        if clear_deadline:
            item.deadline = None
        elif deadline is not None:
            item.deadline = deadline

        # Handle permanent flag update
        if permanent is not None:
            if permanent:
                # Only allow permanent if item has recurrence
                if item.recurrence:
                    item.permanent = True
            else:
                item.permanent = False

        item.updated_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
        return True

    def complete_item(self, todo_id: str) -> bool:
        """
        Mark a TODO item as done.

        Returns:
            True if successful, False if not found or permanent.
        """
        item = self.get_item(todo_id)
        if not item:
            return False

        # Permanent items cannot be completed
        if item.permanent:
            return False

        item.status = TodoStatus.DONE
        item.updated_at = datetime.utcnow()
        self.updated_at = datetime.utcnow()
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
                self.updated_at = datetime.utcnow()
                return deleted
        return None

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
        threshold = datetime.utcnow() - timedelta(days=days_old)
        original_count = len(self.items)

        self.items = [
            item
            for item in self.items
            if item.status != TodoStatus.DONE or item.updated_at > threshold
        ]

        archived = original_count - len(self.items)
        if archived > 0:
            self.updated_at = datetime.utcnow()
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
        global _todo_locks
        with _locks_lock:
            if user_id not in _todo_locks:
                _todo_locks[user_id] = threading.RLock()
            return _todo_locks[user_id]

    @contextmanager
    def atomic_update(self, user_id: str = "default"):
        """
        Context manager for atomic TODO list updates.

        Usage:
            with manager.atomic_update("default") as todo_list:
                todo_list.add_item("New task")
            # List is automatically saved when exiting the context

        This ensures that multiple concurrent modifications don't overwrite each other.
        """
        lock = self._get_lock(user_id)
        with lock:
            todo_list = self.get_todos(user_id)
            try:
                yield todo_list
            finally:
                self.save_todos(todo_list)

    def _get_todos_path(self, user_id: str) -> Path:
        """Get the path to a user's TODO file."""
        # Sanitize user_id to prevent path traversal
        safe_user_id = "".join(c for c in user_id if c.isalnum() or c in "-_")
        if not safe_user_id:
            safe_user_id = "default"
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
            todo_list.updated_at = datetime.utcnow()

            # Write atomically (write to temp file, then rename)
            temp_path = todos_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(todo_list.model_dump(mode="json"), f, indent=2, default=str)

            temp_path.replace(todos_path)
            logger.debug(f"Saved TODO list for user: {todo_list.user_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to save TODOs for {todo_list.user_id}: {e}")
            return False

    def get_all_users_with_todos(self) -> List[str]:
        """Get all user IDs that have TODO lists."""
        users = []
        if self.todos_dir.exists():
            for path in self.todos_dir.iterdir():
                if path.is_file() and path.suffix == ".json":
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
        from .todo_schedule_db import TodoScheduleDB

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
        from .todo_schedule_db import TodoScheduleDB

        with self.atomic_update(user_id) as todo_list:
            todo = todo_list.get_item(todo_id)
            if todo:
                todo.last_execution = datetime.utcnow()
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
