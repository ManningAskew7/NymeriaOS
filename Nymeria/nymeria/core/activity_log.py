"""Activity logging for Nymeria dashboard.

Provides a simple append-only JSON log per user for tracking autonomous actions.
"""

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .keyed_locks import KeyedRLockMap

logger = logging.getLogger(__name__)

# Thread-safe locks for activity operations (keyed by user_id)
_activity_locks = KeyedRLockMap()


class ActivityType(str, Enum):
    """Type of activity event."""

    SELF_INVOKE = "self_invoke"
    WATCHDOG_NUDGE = "watchdog_nudge"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TODO_ADDED = "todo_added"
    TODO_UPDATED = "todo_updated"
    TODO_COMPLETED = "todo_completed"
    TODO_DELETED = "todo_deleted"
    TRIGGER_COMPLETED = "trigger_completed"
    USER_MESSAGE = "user_message"
    NOTIFICATION_SENT = "notification_sent"


class ActivityEntry(BaseModel):
    """A single activity log entry."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    type: ActivityType
    message: str = Field(..., max_length=500, description="Human-readable description")
    user_id: str = Field(default="default")
    thread_id: Optional[str] = Field(default=None)
    metadata: Optional[dict] = Field(default=None, description="Additional context")


class ActivityLog:
    """
    Activity log manager with append-only JSON storage.

    Stores activity in data/activity/{user_id}.json with automatic
    retention of the last MAX_ENTRIES entries.
    """

    MAX_ENTRIES_FALLBACK = 4000  # Hard safety cap

    def __init__(self, data_dir: Path):
        """
        Initialize the activity log manager.

        Args:
            data_dir: Base data directory (activity stored in data_dir/activity/)
        """
        self.activity_dir = data_dir / "activity"
        self.activity_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"ActivityLog initialized with directory: {self.activity_dir}")

    def _get_lock(self, user_id: str) -> threading.RLock:
        """Get or create a lock for a specific user."""
        return _activity_locks.get(user_id)

    def _get_activity_path(self, user_id: str) -> Path:
        """Get the path to a user's activity file."""
        safe_user_id = "".join(c for c in user_id if c.isalnum() or c in "-_")
        if not safe_user_id:
            safe_user_id = "default"
        return self.activity_dir / f"{safe_user_id}.json"

    def log(
        self,
        activity_type: ActivityType,
        message: str,
        user_id: str = "default",
        thread_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> ActivityEntry:
        """
        Log an activity event.

        Args:
            activity_type: Type of activity
            message: Human-readable description
            user_id: User identifier
            thread_id: Optional thread ID for context
            metadata: Optional additional data

        Returns:
            The created ActivityEntry
        """
        entry = ActivityEntry(
            type=activity_type,
            message=message[:500],
            user_id=user_id,
            thread_id=thread_id,
            metadata=metadata,
        )

        lock = self._get_lock(user_id)
        with lock:
            entries = self._load_entries(user_id)
            entries.append(entry)

            # Time-based retention: prune entries older than retention_hours
            from ..config import get_settings
            retention_hours = get_settings().activity_retention_hours
            cutoff = datetime.utcnow() - timedelta(hours=retention_hours)
            entries = [e for e in entries if e.timestamp >= cutoff]

            # Hard safety cap to prevent unbounded growth
            if len(entries) > self.MAX_ENTRIES_FALLBACK:
                entries = entries[-self.MAX_ENTRIES_FALLBACK:]

            self._save_entries(user_id, entries)

        logger.debug(f"Activity logged for {user_id}: {activity_type.value} - {message[:50]}")
        return entry

    def get_entries(
        self,
        user_id: str = "default",
        limit: int = 50,
        activity_type: Optional[ActivityType] = None,
        thread_id: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[ActivityEntry]:
        """
        Get activity entries for a user.

        Args:
            user_id: User identifier
            limit: Maximum number of entries to return
            activity_type: Optional filter by type
            thread_id: Optional filter by thread ID
            since: Optional cutoff — only return entries after this time

        Returns:
            List of ActivityEntry objects, newest first
        """
        lock = self._get_lock(user_id)
        with lock:
            entries = self._load_entries(user_id)

        if since:
            entries = [e for e in entries if e.timestamp >= since]

        # Filter by type if specified
        if activity_type:
            entries = [e for e in entries if e.type == activity_type]

        # Filter by thread_id if specified
        if thread_id:
            entries = [e for e in entries if e.thread_id == thread_id]

        # Return newest first, limited
        return list(reversed(entries[-limit:]))

    def _load_entries(self, user_id: str) -> List[ActivityEntry]:
        """Load activity entries from disk."""
        activity_path = self._get_activity_path(user_id)

        if not activity_path.exists():
            return []

        try:
            with open(activity_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [ActivityEntry.model_validate(item) for item in data]
        except Exception as e:
            logger.error(f"Failed to load activity for {user_id}: {e}")
            return []

    def _save_entries(self, user_id: str, entries: List[ActivityEntry]) -> bool:
        """Save activity entries to disk."""
        activity_path = self._get_activity_path(user_id)

        try:
            activity_path.parent.mkdir(parents=True, exist_ok=True)

            # Write atomically
            temp_path = activity_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(
                    [e.model_dump(mode="json") for e in entries],
                    f,
                    indent=2,
                    default=str,
                )

            temp_path.replace(activity_path)
            return True
        except Exception as e:
            logger.error(f"Failed to save activity for {user_id}: {e}")
            return False

    def clear(self, user_id: str = "default") -> bool:
        """Clear all activity for a user."""
        lock = self._get_lock(user_id)
        with lock:
            activity_path = self._get_activity_path(user_id)
            if activity_path.exists():
                try:
                    activity_path.unlink()
                    return True
                except Exception as e:
                    logger.error(f"Failed to clear activity for {user_id}: {e}")
                    return False
        return True

    def delete_for_thread(self, user_id: str, thread_id: str) -> int:
        """Delete activity entries for one thread from one user's log."""
        lock = self._get_lock(user_id)
        with lock:
            entries = self._load_entries(user_id)
            kept = [entry for entry in entries if entry.thread_id != thread_id]
            deleted = len(entries) - len(kept)
            if deleted:
                self._save_entries(user_id, kept)
            return deleted

    def delete_thread_globally(self, thread_id: str) -> int:
        """Delete activity entries for one thread from all user logs."""
        deleted = 0
        if not self.activity_dir.exists():
            return deleted
        for path in self.activity_dir.iterdir():
            if path.is_file() and path.suffix == ".json":
                deleted += self.delete_for_thread(path.stem, thread_id)
        return deleted


# Global activity log instance (initialized lazily)
_activity_log: Optional[ActivityLog] = None


def get_activity_log() -> ActivityLog:
    """Get or create the global activity log."""
    global _activity_log
    if _activity_log is None:
        from ..config import get_settings

        settings = get_settings()
        _activity_log = ActivityLog(settings.data_dir)
    return _activity_log


def log_activity(
    activity_type: ActivityType,
    message: str,
    user_id: str = "default",
    thread_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> ActivityEntry:
    """
    Convenience function to log an activity event.

    Args:
        activity_type: Type of activity
        message: Human-readable description
        user_id: User identifier
        thread_id: Optional thread ID
        metadata: Optional additional data

    Returns:
        The created ActivityEntry
    """
    return get_activity_log().log(
        activity_type=activity_type,
        message=message,
        user_id=user_id,
        thread_id=thread_id,
        metadata=metadata,
    )
