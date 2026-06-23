"""Activity logging for Nymeria dashboard.

Provides an append-only activity log per user for tracking autonomous actions.

On-disk format is JSON Lines (JSONL): one ``ActivityEntry`` JSON object per
line, oldest first. ``log()`` appends a single line per event (O(1)) instead of
rewriting the whole file, and retention/cap enforcement is amortized into a
periodic compaction (every ``COMPACTION_APPEND_INTERVAL`` appends) rather than
running on every write. Legacy ``{user_id}.json`` files (a single JSON array,
the previous format) are migrated to ``{user_id}.jsonl`` lazily on first touch.

Concurrency: a per-user re-entrant lock (the module-global ``_activity_locks``)
serializes all mutators and the lazy migration, so the multiple ``ActivityLog``
instances that share one ``data_dir`` (the ``get_activity_log()`` singleton, the
throwaway instance in ``thread_deletion``, and tests) never race on a user's
file. Disk is the single source of truth; there is no in-memory entry cache.
This is single-process safety only (cross-process concurrent writers are
last-writer-wins for the rewrite paths, same as the previous design; append-mode
line writes are if anything more concurrency-friendly).
"""

import json
import logging
import threading
import uuid
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from .keyed_locks import KeyedRLockMap
from .storage_paths import safe_path_segment
from .time_utils import ensure_aware_utc, utc_now

logger = logging.getLogger(__name__)

# Thread-safe locks for activity operations (keyed by user_id). Module-global so
# every ActivityLog instance over the same data_dir shares them.
_activity_locks = KeyedRLockMap()

# Per-user append counters that trigger periodic compaction. Module-global (with
# a dedicated guard lock) so compaction cadence is coherent across instances,
# mirroring _activity_locks. Mutated only while the per-user lock is held.
_append_counters: Dict[str, int] = {}
_append_counter_lock = threading.Lock()


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
    timestamp: datetime = Field(default_factory=utc_now)
    type: ActivityType
    message: str = Field(..., max_length=500, description="Human-readable description")
    user_id: str = Field(default="default")
    thread_id: Optional[str] = Field(default=None)
    metadata: Optional[dict] = Field(default=None, description="Additional context")

    @field_validator("timestamp")
    @classmethod
    def _timestamp_as_utc(cls, value: datetime) -> datetime:
        return ensure_aware_utc(value)


class ActivityLog:
    """
    Activity log manager with append-only JSONL storage.

    Stores activity in data/activity/{user_id}.jsonl (one entry per line, oldest
    first) with a periodic compaction that prunes entries older than
    ``activity_retention_hours`` and caps the file at ``MAX_ENTRIES_FALLBACK``.
    """

    MAX_ENTRIES_FALLBACK = 4000  # Hard safety cap, enforced at compaction
    COMPACTION_APPEND_INTERVAL = 200  # Appends between retention/cap compactions

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
        """Get the path to a user's activity file (JSONL)."""
        safe_user_id = safe_path_segment(user_id)
        return self.activity_dir / f"{safe_user_id}.jsonl"

    def _legacy_activity_path(self, user_id: str) -> Path:
        """Get the path to a user's legacy JSON-array activity file."""
        safe_user_id = safe_path_segment(user_id)
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
        Log an activity event by appending a single line (O(1)).

        Retention and the hard cap are enforced by a periodic compaction, not on
        every append, so a busy autonomous user no longer pays a full
        read+validate+rewrite of the whole file per event.

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
            self._migrate_legacy_locked(user_id)
            self._append_entry(user_id, entry)

            # Amortized retention/cap enforcement: compact every N appends.
            with _append_counter_lock:
                count = _append_counters.get(user_id, 0) + 1
                should_compact = count >= self.COMPACTION_APPEND_INTERVAL
                _append_counters[user_id] = 0 if should_compact else count
            if should_compact:
                self._compact_locked(user_id)

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
        Get activity entries for a user, newest first.

        Note: retention/cap are enforced by periodic compaction, not at read
        time, so reads may surface entries older than ``activity_retention_hours``
        (or beyond the cap) between compactions. This is a deliberate change from
        the previous design, which pruned on every write. Only the explicit
        ``since``/type/thread filters apply here.

        Args:
            user_id: User identifier
            limit: Maximum number of entries to return
            activity_type: Optional filter by type
            thread_id: Optional filter by thread ID
            since: Optional cutoff, only return entries after this time

        Returns:
            List of ActivityEntry objects, newest first
        """
        lock = self._get_lock(user_id)
        with lock:
            self._migrate_legacy_locked(user_id)
            entries = self._load_entries(user_id)

        if since:
            since = ensure_aware_utc(since)
            entries = [e for e in entries if e.timestamp >= since]

        # Filter by type if specified
        if activity_type:
            entries = [e for e in entries if e.type == activity_type]

        # Filter by thread_id if specified
        if thread_id:
            entries = [e for e in entries if e.thread_id == thread_id]

        # Return newest first, limited
        return list(reversed(entries[-limit:]))

    def _migrate_legacy_locked(self, user_id: str) -> None:
        """Lazily migrate a legacy JSON-array file to JSONL. Caller holds the lock.

        Idempotent and crash-safe: if the JSONL file already exists, any leftover
        legacy file is a stale post-migration remnant (or appends already
        happened against the JSONL), so it is discarded without re-combining,
        which means a legacy entry can be folded into the JSONL at most once (no
        duplicates). The legacy file is only unlinked after the JSONL has been
        written, so a crash mid-migration never loses data.
        """
        legacy_path = self._legacy_activity_path(user_id)
        if not legacy_path.exists():
            return

        jsonl_path = self._get_activity_path(user_id)
        if jsonl_path.exists():
            # JSONL is authoritative; the legacy file is a stale leftover.
            try:
                legacy_path.unlink(missing_ok=True)
            except Exception as e:  # noqa: BLE001 - best-effort cleanup
                logger.error(f"Failed to remove legacy activity file for {user_id}: {e}")
            return

        entries = self._read_legacy_array(legacy_path)
        if entries is None:
            # Could not read the legacy file. Leave it untouched (non-destructive,
            # recoverable) and retry on a later touch rather than deleting it.
            return
        if not self._save_entries(user_id, entries):
            # The atomic rewrite failed, so no .jsonl was created. Keep the legacy
            # file in place so the migration retries next time (no data loss).
            return
        try:
            legacy_path.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001 - best-effort cleanup
            logger.error(f"Failed to remove legacy activity file for {user_id}: {e}")

    def _read_legacy_array(self, path: Path) -> Optional[List[ActivityEntry]]:
        """Read a legacy JSON-array activity file.

        Returns the parsed entries (possibly empty) on success, or ``None`` if the
        file could not be read/parsed, so the caller can leave a corrupt file in
        place rather than destroying it.
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [ActivityEntry.model_validate(item) for item in data]
        except Exception as e:  # noqa: BLE001 - corrupt legacy file is left in place
            logger.error(f"Failed to read legacy activity file {path}: {e}")
            return None

    def _append_entry(self, user_id: str, entry: ActivityEntry) -> None:
        """Append a single entry as one JSONL line. Caller holds the lock."""
        activity_path = self._get_activity_path(user_id)
        try:
            activity_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(entry.model_dump(mode="json"), default=str)
            with open(activity_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:  # noqa: BLE001 - degraded logging is intentional
            logger.error(f"Failed to append activity for {user_id}: {e}")

    def _compact_locked(self, user_id: str) -> None:
        """Prune by retention + hard cap and rewrite the file. Caller holds the lock."""
        entries = self._load_entries(user_id)

        # Time-based retention: prune entries older than retention_hours.
        from ..config import get_settings
        retention_hours = get_settings().activity_retention_hours
        cutoff = utc_now() - timedelta(hours=retention_hours)
        entries = [e for e in entries if e.timestamp >= cutoff]

        # Hard safety cap to prevent unbounded growth.
        if len(entries) > self.MAX_ENTRIES_FALLBACK:
            entries = entries[-self.MAX_ENTRIES_FALLBACK:]

        self._save_entries(user_id, entries)

    def _load_entries(self, user_id: str) -> List[ActivityEntry]:
        """Load activity entries from the JSONL file, oldest first.

        Tolerates a torn/partial final line (e.g. from a crash mid-append) by
        skipping any line that fails to parse, rather than discarding the whole
        file.
        """
        activity_path = self._get_activity_path(user_id)

        if not activity_path.exists():
            return []

        entries: List[ActivityEntry] = []
        try:
            with open(activity_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(ActivityEntry.model_validate(json.loads(line)))
                    except Exception as e:  # noqa: BLE001 - skip one bad line, keep the rest
                        logger.warning(
                            f"Skipping malformed activity line for {user_id}: {e}"
                        )
        except Exception as e:  # noqa: BLE001 - degraded read is intentional
            logger.error(f"Failed to load activity for {user_id}: {e}")
            return []
        return entries

    def _save_entries(self, user_id: str, entries: List[ActivityEntry]) -> bool:
        """Atomically rewrite a user's JSONL file from a full entry list.

        Used by compaction and deletion, and called directly by tests as a seed
        seam. Writes oldest-first, one entry per line.
        """
        activity_path = self._get_activity_path(user_id)

        try:
            activity_path.parent.mkdir(parents=True, exist_ok=True)

            # Write atomically: deterministic temp path so a crash-orphaned temp
            # is reclaimed on the next write rather than accumulating.
            temp_path = activity_path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(json.dumps(entry.model_dump(mode="json"), default=str) + "\n")

            temp_path.replace(activity_path)
            return True
        except Exception as e:  # noqa: BLE001 - degraded write is intentional
            logger.error(f"Failed to save activity for {user_id}: {e}")
            return False

    def clear(self, user_id: str = "default") -> bool:
        """Clear all activity for a user (JSONL and any legacy file)."""
        lock = self._get_lock(user_id)
        with lock:
            ok = True
            for path in (self._get_activity_path(user_id), self._legacy_activity_path(user_id)):
                if path.exists():
                    try:
                        path.unlink()
                    except Exception as e:  # noqa: BLE001 - degraded clear is intentional
                        logger.error(f"Failed to clear activity for {user_id}: {e}")
                        ok = False
            with _append_counter_lock:
                _append_counters.pop(user_id, None)
            return ok

    def delete_for_thread(self, user_id: str, thread_id: str) -> int:
        """Delete activity entries for one thread from one user's log."""
        lock = self._get_lock(user_id)
        with lock:
            self._migrate_legacy_locked(user_id)
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
        # Collect unique user stems across both the new JSONL files and any
        # not-yet-migrated legacy JSON files.
        stems = set()
        for path in self.activity_dir.iterdir():
            if path.is_file() and path.suffix in (".jsonl", ".json"):
                stems.add(path.stem)
        for stem in stems:
            deleted += self.delete_for_thread(stem, thread_id)
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
