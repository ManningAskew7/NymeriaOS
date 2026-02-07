"""SQLite database for durable scheduled tasks."""

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    """Status of a scheduled task."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DurableTask:
    """A durable scheduled task stored in SQLite."""

    id: str
    user_id: str
    thread_id: str
    prompt: str
    execute_at: float  # Unix timestamp
    status: TaskStatus
    created_at: float
    completed_at: Optional[float] = None
    result: Optional[str] = None
    muted: bool = False
    retry_count: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "DurableTask":
        """Create a DurableTask from a database row."""
        return cls(
            id=row["id"],
            user_id=row["user_id"],
            thread_id=row["thread_id"],
            prompt=row["prompt"],
            execute_at=row["execute_at"],
            status=TaskStatus(row["status"]),
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            result=row["result"],
            muted=bool(row["muted"]),
            retry_count=row["retry_count"],
        )


class TaskDatabase:
    """
    SQLite database for durable scheduled tasks.

    Thread-safe operations with connection pooling.
    Uses a separate database file from LangGraph checkpointer (data/tasks.db).
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS scheduled_tasks (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        prompt TEXT NOT NULL,
        execute_at REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL,
        completed_at REAL,
        result TEXT,
        muted INTEGER DEFAULT 0,
        retry_count INTEGER DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_execute
        ON scheduled_tasks(execute_at) WHERE status = 'pending';
    CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_user
        ON scheduled_tasks(user_id, status);
    CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_status
        ON scheduled_tasks(status);
    """

    def __init__(self, db_path: Path):
        """
        Initialize the task database.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._lock = threading.Lock()

        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize schema
        self._init_schema()
        logger.info(f"TaskDatabase initialized at {db_path}")

    def _get_connection(self) -> sqlite3.Connection:
        """Get a database connection."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        """Initialize database schema (idempotent)."""
        with self._lock:
            conn = self._get_connection()
            try:
                conn.executescript(self.SCHEMA)
                conn.commit()
            finally:
                conn.close()

    def create_task(self, task: DurableTask) -> bool:
        """
        Insert a new scheduled task.

        If there's already a pending task for this user, it will be cancelled
        first (enforcing one pending task per user).

        Args:
            task: The task to create

        Returns:
            True if created successfully
        """
        with self._lock:
            conn = self._get_connection()
            try:
                # Cancel existing pending task for this user
                conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET status = ?, completed_at = ?
                    WHERE user_id = ? AND status = ?
                    """,
                    (TaskStatus.CANCELLED.value, time.time(), task.user_id, TaskStatus.PENDING.value),
                )

                # Insert new task
                conn.execute(
                    """
                    INSERT INTO scheduled_tasks
                    (id, user_id, thread_id, prompt, execute_at, status, created_at,
                     completed_at, result, muted, retry_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task.id,
                        task.user_id,
                        task.thread_id,
                        task.prompt,
                        task.execute_at,
                        task.status.value,
                        task.created_at,
                        task.completed_at,
                        task.result,
                        1 if task.muted else 0,
                        task.retry_count,
                    ),
                )
                conn.commit()
                logger.debug(f"Created task {task.id} for user {task.user_id}")
                return True
            except Exception as e:
                logger.error(f"Failed to create task: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()

    def get_task(self, task_id: str) -> Optional[DurableTask]:
        """
        Get a task by ID.

        Args:
            task_id: The task ID

        Returns:
            The task if found, None otherwise
        """
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "SELECT * FROM scheduled_tasks WHERE id = ?",
                    (task_id,),
                )
                row = cursor.fetchone()
                return DurableTask.from_row(row) if row else None
            finally:
                conn.close()

    def get_pending_for_user(self, user_id: str) -> Optional[DurableTask]:
        """
        Get the pending task for a user (if any).

        Args:
            user_id: The user ID

        Returns:
            The pending task or None
        """
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    """
                    SELECT * FROM scheduled_tasks
                    WHERE user_id = ? AND status = ?
                    ORDER BY execute_at ASC
                    LIMIT 1
                    """,
                    (user_id, TaskStatus.PENDING.value),
                )
                row = cursor.fetchone()
                return DurableTask.from_row(row) if row else None
            finally:
                conn.close()

    def get_processing_for_user(self, user_id: str) -> Optional[DurableTask]:
        """
        Get the currently processing task for a user (if any).

        Args:
            user_id: The user ID

        Returns:
            The processing task or None
        """
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    """
                    SELECT * FROM scheduled_tasks
                    WHERE user_id = ? AND status = ?
                    LIMIT 1
                    """,
                    (user_id, TaskStatus.PROCESSING.value),
                )
                row = cursor.fetchone()
                return DurableTask.from_row(row) if row else None
            finally:
                conn.close()

    def get_due_tasks(self, before_timestamp: Optional[float] = None) -> List[DurableTask]:
        """
        Get all pending tasks that are due for execution.

        Args:
            before_timestamp: Get tasks due before this time (defaults to now)

        Returns:
            List of tasks ready to execute
        """
        if before_timestamp is None:
            before_timestamp = time.time()

        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    """
                    SELECT * FROM scheduled_tasks
                    WHERE status = ? AND execute_at <= ?
                    ORDER BY execute_at ASC
                    """,
                    (TaskStatus.PENDING.value, before_timestamp),
                )
                return [DurableTask.from_row(row) for row in cursor.fetchall()]
            finally:
                conn.close()

    def update_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: Optional[str] = None,
        muted: bool = False,
    ) -> bool:
        """
        Update task status atomically.

        Sets completed_at when status is COMPLETED or FAILED.

        Args:
            task_id: The task ID
            status: New status
            result: Optional result or error message
            muted: Whether output was muted

        Returns:
            True if updated successfully
        """
        completed_at = None
        if status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            completed_at = time.time()

        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET status = ?, result = ?, muted = ?, completed_at = ?
                    WHERE id = ?
                    """,
                    (status.value, result, 1 if muted else 0, completed_at, task_id),
                )
                conn.commit()
                logger.debug(f"Updated task {task_id} status to {status.value}")
                return True
            except Exception as e:
                logger.error(f"Failed to update task status: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()

    def cancel_user_tasks(self, user_id: str) -> int:
        """
        Cancel all pending tasks for a user.

        Args:
            user_id: The user ID

        Returns:
            Number of tasks cancelled
        """
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    """
                    UPDATE scheduled_tasks
                    SET status = ?, completed_at = ?
                    WHERE user_id = ? AND status = ?
                    """,
                    (TaskStatus.CANCELLED.value, time.time(), user_id, TaskStatus.PENDING.value),
                )
                count = cursor.rowcount
                conn.commit()
                if count > 0:
                    logger.debug(f"Cancelled {count} pending task(s) for user {user_id}")
                return count
            except Exception as e:
                logger.error(f"Failed to cancel tasks: {e}")
                conn.rollback()
                return 0
            finally:
                conn.close()

    def set_muted(self, task_id: str, muted: bool) -> bool:
        """
        Set the muted flag for a task.

        Args:
            task_id: The task ID
            muted: Whether to mute

        Returns:
            True if updated successfully
        """
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE scheduled_tasks SET muted = ? WHERE id = ?",
                    (1 if muted else 0, task_id),
                )
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Failed to set muted: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()

    def increment_retry(self, task_id: str) -> int:
        """
        Increment retry count and return new value.

        Used to detect and prevent infinite retry loops.

        Args:
            task_id: The task ID

        Returns:
            New retry count
        """
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE scheduled_tasks SET retry_count = retry_count + 1 WHERE id = ?",
                    (task_id,),
                )
                cursor = conn.execute(
                    "SELECT retry_count FROM scheduled_tasks WHERE id = ?",
                    (task_id,),
                )
                row = cursor.fetchone()
                conn.commit()
                return row["retry_count"] if row else 0
            except Exception as e:
                logger.error(f"Failed to increment retry: {e}")
                conn.rollback()
                return 0
            finally:
                conn.close()

    def cleanup_old_tasks(self, older_than_days: int = 7) -> int:
        """
        Remove old completed/cancelled/failed tasks.

        Args:
            older_than_days: Delete tasks older than this many days

        Returns:
            Number of tasks deleted
        """
        cutoff = time.time() - (older_than_days * 24 * 60 * 60)

        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    """
                    DELETE FROM scheduled_tasks
                    WHERE status IN (?, ?, ?) AND completed_at < ?
                    """,
                    (
                        TaskStatus.COMPLETED.value,
                        TaskStatus.CANCELLED.value,
                        TaskStatus.FAILED.value,
                        cutoff,
                    ),
                )
                count = cursor.rowcount
                conn.commit()
                if count > 0:
                    logger.info(f"Cleaned up {count} old tasks")
                return count
            except Exception as e:
                logger.error(f"Failed to cleanup tasks: {e}")
                conn.rollback()
                return 0
            finally:
                conn.close()
