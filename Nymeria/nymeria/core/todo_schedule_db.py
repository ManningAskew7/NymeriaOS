# TODO: Consider removing this SQLite index entirely. With a single user and
# ≤50 TODOs, scanning JSON files directly on the 5-second ticker poll would be
# trivial and would eliminate all dual-storage sync complexity.
"""SQLite index for scheduled TODOs - enables efficient polling."""

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

ACTIVE_EXECUTION_STALE_SECONDS = 24 * 60 * 60


def _datetime_to_timestamp(dt: datetime) -> float:
    """
    Convert a datetime to a Unix timestamp, handling both naive and timezone-aware.

    For naive datetimes (no timezone info), we assume they represent UTC time.
    This is important because Python's timestamp() method interprets naive
    datetimes as LOCAL time, which causes bugs in different timezones.

    Args:
        dt: datetime object (naive or timezone-aware)

    Returns:
        Unix timestamp in seconds
    """
    original_dt = dt
    if dt.tzinfo is None:
        # Naive datetime - assume it's UTC and add timezone info
        dt = dt.replace(tzinfo=timezone.utc)
        logger.info(f"[TIMESTAMP] Naive datetime {original_dt} -> treated as UTC -> timestamp {dt.timestamp()}")
    else:
        logger.info(f"[TIMESTAMP] Timezone-aware datetime {original_dt} (tzinfo={dt.tzinfo}) -> timestamp {dt.timestamp()}")
    return dt.timestamp()


@dataclass
class ScheduledTodoEntry:
    """An entry in the scheduled TODOs index."""

    todo_id: str
    user_id: str
    thread_id: Optional[str]
    scheduled_for: float  # Unix timestamp
    task_preview: str  # First 100 chars of task
    created_at: float

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ScheduledTodoEntry":
        """Create from a database row."""
        return cls(
            todo_id=row["todo_id"],
            user_id=row["user_id"],
            thread_id=row["thread_id"],
            scheduled_for=row["scheduled_for"],
            task_preview=row["task_preview"],
            created_at=row["created_at"],
        )


class TodoScheduleDB:
    """
    SQLite index for efficient scheduled TODO polling.

    This is a lightweight index that tracks which TODOs have scheduled_for set.
    The actual TODO data remains in the JSON files - this just enables
    efficient time-based polling without scanning all files.

    Features:
    - Indexed by scheduled_for for fast due item lookup
    - Indexed by user_id for user-specific queries
    - Synced with TodoManager operations
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS scheduled_todos (
        todo_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        thread_id TEXT,
        scheduled_for REAL NOT NULL,
        task_preview TEXT,
        created_at REAL NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_scheduled_due
        ON scheduled_todos(scheduled_for);
    CREATE INDEX IF NOT EXISTS idx_scheduled_user
        ON scheduled_todos(user_id);

    CREATE TABLE IF NOT EXISTS active_todo_executions (
        todo_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        thread_id TEXT,
        started_at REAL NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_active_todo_executions_user
        ON active_todo_executions(user_id);
    CREATE INDEX IF NOT EXISTS idx_active_todo_executions_started
        ON active_todo_executions(started_at);
    """

    def __init__(self, db_path: Path):
        """
        Initialize the schedule index database.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._lock = threading.Lock()

        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialize schema
        self._init_schema()
        logger.info(f"TodoScheduleDB initialized at {db_path}")

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

    def _delete_stale_executions(
        self,
        conn: sqlite3.Connection,
        *,
        now: float,
        stale_after_seconds: int,
    ) -> int:
        """Delete crashed-worker execution markers older than the stale cutoff."""
        cutoff = now - stale_after_seconds
        cursor = conn.execute(
            "DELETE FROM active_todo_executions WHERE started_at < ?",
            (cutoff,),
        )
        deleted = cursor.rowcount if cursor.rowcount is not None else 0
        if deleted:
            logger.warning(
                "Removed %s stale active TODO execution marker(s) older than %ss",
                deleted,
                stale_after_seconds,
            )
        return deleted

    def mark_execution_started(
        self,
        todo_id: str,
        user_id: str,
        thread_id: Optional[str] = None,
        *,
        stale_after_seconds: int = ACTIVE_EXECUTION_STALE_SECONDS,
    ) -> bool:
        """
        Mark a scheduled TODO as actively executing.

        Returns False when another live worker already owns the TODO.
        Stale markers are removed first so a crashed worker cannot lock a TODO
        forever.
        """
        with self._lock:
            conn = self._get_connection()
            try:
                now = time.time()
                self._delete_stale_executions(
                    conn,
                    now=now,
                    stale_after_seconds=stale_after_seconds,
                )
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO active_todo_executions
                    (todo_id, user_id, thread_id, started_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (todo_id, user_id, thread_id, now),
                )
                conn.commit()
                if cursor.rowcount == 1:
                    logger.info("Marked TODO %s as actively executing", todo_id)
                    return True
                logger.info("TODO %s is already actively executing", todo_id)
                return False
            except Exception as e:
                logger.error(f"Failed to mark active TODO execution: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()

    def clear_execution(self, todo_id: str, user_id: Optional[str] = None) -> bool:
        """
        Clear an active scheduled TODO execution marker.

        Returns True if the marker was removed or was already absent.
        """
        with self._lock:
            conn = self._get_connection()
            try:
                if user_id is None:
                    conn.execute(
                        "DELETE FROM active_todo_executions WHERE todo_id = ?",
                        (todo_id,),
                    )
                else:
                    conn.execute(
                        """
                        DELETE FROM active_todo_executions
                        WHERE todo_id = ? AND user_id = ?
                        """,
                        (todo_id, user_id),
                    )
                conn.commit()
                logger.debug("Cleared active execution marker for TODO %s", todo_id)
                return True
            except Exception as e:
                logger.error(f"Failed to clear active TODO execution marker: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()

    def is_execution_active(
        self,
        todo_id: str,
        user_id: Optional[str] = None,
        *,
        stale_after_seconds: int = ACTIVE_EXECUTION_STALE_SECONDS,
    ) -> bool:
        """Return whether a scheduled TODO is actively executing."""
        with self._lock:
            conn = self._get_connection()
            try:
                now = time.time()
                self._delete_stale_executions(
                    conn,
                    now=now,
                    stale_after_seconds=stale_after_seconds,
                )
                if user_id is None:
                    cursor = conn.execute(
                        "SELECT 1 FROM active_todo_executions WHERE todo_id = ?",
                        (todo_id,),
                    )
                else:
                    cursor = conn.execute(
                        """
                        SELECT 1 FROM active_todo_executions
                        WHERE todo_id = ? AND user_id = ?
                        """,
                        (todo_id, user_id),
                    )
                active = cursor.fetchone() is not None
                conn.commit()
                return active
            except Exception as e:
                logger.error(f"Failed to check active TODO execution marker: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()

    def add_scheduled(
        self,
        todo_id: str,
        user_id: str,
        scheduled_for: datetime,
        task_preview: str,
        thread_id: Optional[str] = None,
    ) -> bool:
        """
        Add or update a scheduled TODO entry.

        Args:
            todo_id: The TODO ID
            user_id: User ID
            scheduled_for: When to execute
            task_preview: First 100 chars of task
            thread_id: Thread context for execution

        Returns:
            True if successful
        """
        with self._lock:
            conn = self._get_connection()
            try:
                timestamp = _datetime_to_timestamp(scheduled_for)
                logger.info(f"[SCHEDULE DB] Adding TODO {todo_id}: scheduled_for={scheduled_for} (tzinfo={scheduled_for.tzinfo}), timestamp={timestamp}, current_time={time.time()}")
                conn.execute(
                    """
                    INSERT OR REPLACE INTO scheduled_todos
                    (todo_id, user_id, thread_id, scheduled_for, task_preview, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        todo_id,
                        user_id,
                        thread_id,
                        timestamp,
                        task_preview[:100],
                        time.time(),
                    ),
                )
                conn.commit()
                logger.info(f"[SCHEDULE DB] Successfully added TODO {todo_id} for timestamp {timestamp}")
                return True
            except Exception as e:
                logger.error(f"Failed to add scheduled TODO: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()

    def remove_scheduled(self, todo_id: str) -> bool:
        """
        Remove a TODO from the schedule index.

        Called when:
        - scheduled_for is cleared
        - TODO is completed/deleted
        - Schedule is executed

        Args:
            todo_id: The TODO ID to remove

        Returns:
            True if removed (or didn't exist)
        """
        with self._lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "DELETE FROM scheduled_todos WHERE todo_id = ?",
                    (todo_id,),
                )
                conn.commit()
                logger.debug(f"Removed scheduled TODO {todo_id}")
                return True
            except Exception as e:
                logger.error(f"Failed to remove scheduled TODO: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()

    def remove_for_thread(self, thread_id: str, todo_ids: Optional[List[str]] = None) -> int:
        """
        Remove scheduled entries tied to a thread.

        ``todo_ids`` are included as a defensive fallback for old or corrupt
        schedule rows whose ``thread_id`` was missing but whose TODO item was
        still thread-scoped.

        Returns:
            Number of schedule rows removed.
        """
        todo_ids = list(dict.fromkeys(todo_ids or []))
        with self._lock:
            conn = self._get_connection()
            try:
                params: List[str] = [thread_id]
                where = "thread_id = ?"
                if todo_ids:
                    placeholders = ", ".join("?" for _ in todo_ids)
                    where = f"({where} OR todo_id IN ({placeholders}))"
                    params.extend(todo_ids)
                cursor = conn.execute(
                    f"DELETE FROM scheduled_todos WHERE {where}",
                    params,
                )
                conn.commit()
                count = cursor.rowcount or 0
                logger.info(
                    "Removed %s scheduled TODO row(s) for thread %s",
                    count,
                    thread_id,
                )
                return count
            except Exception as e:
                logger.error(f"Failed to remove scheduled TODOs for thread {thread_id}: {e}")
                conn.rollback()
                raise
            finally:
                conn.close()

    def get_due(self, before: Optional[float] = None) -> List[ScheduledTodoEntry]:
        """
        Get all scheduled TODOs that are due for execution.

        Args:
            before: Unix timestamp cutoff (defaults to now)

        Returns:
            List of due entries, sorted by scheduled_for ascending
        """
        if before is None:
            before = time.time()

        with self._lock:
            conn = self._get_connection()
            try:
                # Log all entries in the database for debugging (only log every 30 seconds to reduce spam)
                all_cursor = conn.execute("SELECT todo_id, scheduled_for FROM scheduled_todos")
                all_entries = all_cursor.fetchall()
                if all_entries and int(before) % 30 < 5:
                    logger.info(f"[SCHEDULE DB] All entries in DB (NOW={before}):")
                    for row in all_entries:
                        delta = row['scheduled_for'] - before
                        is_due = delta <= 0
                        logger.info(f"[SCHEDULE DB]   todo_id={row['todo_id']}, scheduled_for={row['scheduled_for']}, delta={delta:.1f}s, due={is_due}")

                cursor = conn.execute(
                    """
                    SELECT * FROM scheduled_todos
                    WHERE scheduled_for <= ?
                    ORDER BY scheduled_for ASC
                    """,
                    (before,),
                )
                results = [ScheduledTodoEntry.from_row(row) for row in cursor.fetchall()]
                if results:
                    logger.info(f"[SCHEDULE DB] Found {len(results)} due entries")
                return results
            finally:
                conn.close()

    def get_for_user(self, user_id: str) -> List[ScheduledTodoEntry]:
        """
        Get all scheduled TODOs for a user.

        Args:
            user_id: User ID

        Returns:
            List of scheduled entries for the user
        """
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    """
                    SELECT * FROM scheduled_todos
                    WHERE user_id = ?
                    ORDER BY scheduled_for ASC
                    """,
                    (user_id,),
                )
                return [ScheduledTodoEntry.from_row(row) for row in cursor.fetchall()]
            finally:
                conn.close()

    def get_next_for_user(self, user_id: str) -> Optional[ScheduledTodoEntry]:
        """
        Get the next scheduled TODO for a user.

        Args:
            user_id: User ID

        Returns:
            Next scheduled entry or None
        """
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    """
                    SELECT * FROM scheduled_todos
                    WHERE user_id = ?
                    ORDER BY scheduled_for ASC
                    LIMIT 1
                    """,
                    (user_id,),
                )
                row = cursor.fetchone()
                return ScheduledTodoEntry.from_row(row) if row else None
            finally:
                conn.close()

    def get_entry(self, todo_id: str) -> Optional[ScheduledTodoEntry]:
        """
        Get a specific scheduled TODO entry.

        Args:
            todo_id: The TODO ID

        Returns:
            Entry or None if not scheduled
        """
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "SELECT * FROM scheduled_todos WHERE todo_id = ?",
                    (todo_id,),
                )
                row = cursor.fetchone()
                return ScheduledTodoEntry.from_row(row) if row else None
            finally:
                conn.close()

    def count_for_user(self, user_id: str) -> int:
        """
        Count scheduled TODOs for a user.

        Args:
            user_id: User ID

        Returns:
            Number of scheduled TODOs
        """
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "SELECT COUNT(*) as count FROM scheduled_todos WHERE user_id = ?",
                    (user_id,),
                )
                row = cursor.fetchone()
                return row["count"] if row else 0
            finally:
                conn.close()

    def clear_user_schedules(self, user_id: str) -> int:
        """
        Clear all scheduled TODOs for a user.

        Args:
            user_id: User ID

        Returns:
            Number of entries cleared
        """
        with self._lock:
            conn = self._get_connection()
            try:
                cursor = conn.execute(
                    "DELETE FROM scheduled_todos WHERE user_id = ?",
                    (user_id,),
                )
                count = cursor.rowcount
                conn.commit()
                if count > 0:
                    logger.info(f"Cleared {count} scheduled TODO(s) for user {user_id}")
                return count
            except Exception as e:
                logger.error(f"Failed to clear schedules: {e}")
                conn.rollback()
                return 0
            finally:
                conn.close()

    def rebuild_from_todos(self, todo_manager: "TodoManager") -> int:
        """
        Rebuild the index from all TODO files.

        Called on startup to ensure index is in sync with TODO files.

        Args:
            todo_manager: TodoManager instance to read from

        Returns:
            Number of scheduled TODOs indexed
        """
        from .todo_manager import TodoManager

        with self._lock:
            conn = self._get_connection()
            try:
                # Clear existing index
                conn.execute("DELETE FROM scheduled_todos")

                count = 0
                for user_id in todo_manager.get_all_users_with_todos():
                    todo_list = todo_manager.get_todos(user_id)
                    scheduled_todos = todo_list.get_scheduled_todos()
                    logger.info(f"[SCHEDULE DB] Rebuilding: user={user_id}, found {len(scheduled_todos)} scheduled TODOs")
                    for todo in scheduled_todos:
                        if todo.scheduled_for:
                            timestamp = _datetime_to_timestamp(todo.scheduled_for)
                            logger.info(f"[SCHEDULE DB] Indexing: todo_id={todo.id}, task='{todo.task[:50]}...', scheduled_for={todo.scheduled_for} (tzinfo={todo.scheduled_for.tzinfo}), timestamp={timestamp}, thread_id={todo.thread_id}")
                            conn.execute(
                                """
                                INSERT INTO scheduled_todos
                                (todo_id, user_id, thread_id, scheduled_for, task_preview, created_at)
                                VALUES (?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    todo.id,
                                    user_id,
                                    todo.thread_id,
                                    timestamp,
                                    todo.task[:100],
                                    time.time(),
                                ),
                            )
                            count += 1

                conn.commit()
                current_time = time.time()
                logger.info(f"Rebuilt schedule index with {count} scheduled TODO(s). Current time: {current_time}")
                return count
            except Exception as e:
                logger.error(f"Failed to rebuild schedule index: {e}")
                conn.rollback()
                return 0
            finally:
                conn.close()
