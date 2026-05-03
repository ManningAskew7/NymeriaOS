"""Server-side thread metadata for cross-surface sync.

Thread metadata (titles, pins, platform info) is stored here so all
surfaces (desktop, web, mobile, Discord, Telegram, Slack) share the
same view. This replaces the previous frontend-only localStorage approach.

Storage: single JSON file per user at data/thread_metadata/{user_id}.json
Pattern: follows todo_manager.py (per-user RLock, atomic writes).
"""

import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .keyed_locks import KeyedRLockMap
from .time_utils import ensure_aware_utc, utc_now

logger = logging.getLogger(__name__)

# Thread-safe locks (keyed by user_id)
_metadata_locks = KeyedRLockMap()


# ---------------------------------------------------------------------------
# Title generation (mirrors frontend generateTitleFromMessage)
# ---------------------------------------------------------------------------

def generate_title(message: str, max_length: int = 40) -> str:
    """Generate a thread title from a user message.

    Replicates the frontend's generateTitleFromMessage() logic:
    truncate to max_length at a word boundary.
    """
    cleaned = " ".join(message.strip().split())
    if not cleaned:
        return "New Chat"
    if len(cleaned) <= max_length:
        return cleaned

    truncated = cleaned[:max_length]
    last_space = truncated.rfind(" ")
    if last_space > max_length * 0.6:
        return truncated[:last_space] + "..."
    return truncated + "..."


# ---------------------------------------------------------------------------
# Platform classification — delegates to the shared module
# ---------------------------------------------------------------------------

from .thread_classification import classify_platform  # noqa: E402  re-export


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ThreadMetadata(BaseModel):
    """Metadata for a single thread."""

    thread_id: str
    title: str = "New Chat"
    pinned: bool = False
    platform: str = "desktop"
    platform_meta: Optional[Dict[str, str]] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    # How the title was set: default | auto | user | callable | platform
    title_source: str = "default"


class ThreadMetadataStore(BaseModel):
    """All thread metadata for a user. Stored as a single JSON file."""

    user_id: str = "default"
    threads: Dict[str, ThreadMetadata] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------

class ThreadMetadataManager:
    """Manages thread metadata on disk with thread-safe operations.

    Storage: data/thread_metadata/{user_id}.json
    """

    def __init__(self, data_dir: Path):
        self.metadata_dir = data_dir / "thread_metadata"
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"ThreadMetadataManager initialized: {self.metadata_dir}")

    # -- locking --

    def _get_lock(self, user_id: str) -> threading.RLock:
        return _metadata_locks.get(user_id)

    @contextmanager
    def atomic_update(self, user_id: str = "default"):
        """Context manager for read-modify-write on the metadata store."""
        lock = self._get_lock(user_id)
        with lock:
            store = self.get_store(user_id)
            try:
                yield store
            finally:
                self.save_store(store)

    # -- file I/O --

    def _get_path(self, user_id: str) -> Path:
        safe_id = "".join(c for c in user_id if c.isalnum() or c in "-_")
        if not safe_id:
            safe_id = "default"
        return self.metadata_dir / f"{safe_id}.json"

    def get_store(self, user_id: str = "default") -> ThreadMetadataStore:
        """Load the metadata store for a user, or create an empty one."""
        path = self._get_path(user_id)
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return ThreadMetadataStore.model_validate(data)
            except Exception as e:
                logger.error(f"Failed to load thread metadata for {user_id}: {e}")
                return ThreadMetadataStore(user_id=user_id)
        return ThreadMetadataStore(user_id=user_id)

    def save_store(self, store: ThreadMetadataStore) -> bool:
        """Atomically write the metadata store to disk."""
        path = self._get_path(store.user_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            store.updated_at = utc_now()
            temp_path = path.with_suffix(".tmp")
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(store.model_dump(mode="json"), f, indent=2, default=str)
            temp_path.replace(path)
            return True
        except Exception as e:
            logger.error(f"Failed to save thread metadata for {store.user_id}: {e}")
            return False

    # -- thread CRUD --

    def get_thread(
        self, user_id: str, thread_id: str
    ) -> Optional[ThreadMetadata]:
        """Get metadata for a single thread, or None."""
        store = self.get_store(user_id)
        return store.threads.get(thread_id)

    def upsert_thread(
        self,
        user_id: str,
        thread_id: str,
        **fields,
    ) -> ThreadMetadata:
        """Create or update thread metadata.

        Only supplied fields are changed; existing values are preserved.
        """
        with self.atomic_update(user_id) as store:
            existing = store.threads.get(thread_id)
            if existing:
                update_data = {}
                for key, value in fields.items():
                    if value is not None:
                        update_data[key] = value
                update_data["updated_at"] = utc_now()
                updated = existing.model_copy(update=update_data)
                store.threads[thread_id] = updated
                return updated
            else:
                # New thread — fill in defaults for missing fields
                meta = ThreadMetadata(
                    thread_id=thread_id,
                    platform=fields.get("platform", classify_platform(thread_id)),
                    title=fields.get("title", "New Chat"),
                    pinned=fields.get("pinned", False),
                    platform_meta=fields.get("platform_meta"),
                    title_source=fields.get("title_source", "default"),
                    created_at=fields.get("created_at", utc_now()),
                    updated_at=utc_now(),
                )
                store.threads[thread_id] = meta
                return meta

    def delete_thread(self, user_id: str, thread_id: str) -> bool:
        """Remove a thread's metadata."""
        with self.atomic_update(user_id) as store:
            if thread_id in store.threads:
                del store.threads[thread_id]
                return True
        return False

    def delete_thread_globally(self, thread_id: str) -> int:
        """Remove a thread's metadata from every per-user metadata file."""
        deleted = 0
        if not self.metadata_dir.exists():
            return deleted

        for path in self.metadata_dir.iterdir():
            if not path.is_file() or path.suffix != ".json":
                continue
            user_id = path.stem
            store = self.get_store(user_id)
            if thread_id not in store.threads:
                continue
            with self.atomic_update(user_id) as locked_store:
                if thread_id in locked_store.threads:
                    del locked_store.threads[thread_id]
                    deleted += 1
        return deleted

    def list_threads(self, user_id: str = "default") -> List[ThreadMetadata]:
        """Return all thread metadata for a user."""
        store = self.get_store(user_id)
        return list(store.threads.values())

    # -- title helpers --

    def auto_title(
        self, user_id: str, thread_id: str, first_message: str
    ) -> Optional[str]:
        """Set an auto-generated title only if the current title is default.

        Returns the new title, or None if title was already set by the user.
        """
        with self.atomic_update(user_id) as store:
            meta = store.threads.get(thread_id)
            if meta is None:
                # Thread doesn't exist yet — create it with auto title
                title = generate_title(first_message)
                store.threads[thread_id] = ThreadMetadata(
                    thread_id=thread_id,
                    title=title,
                    title_source="auto",
                    platform=classify_platform(thread_id),
                )
                return title

            if meta.title_source == "default":
                title = generate_title(first_message)
                meta.title = title
                meta.title_source = "auto"
                meta.updated_at = utc_now()
                return title

        return None  # Title already set by user/callable/platform

    def set_title(
        self,
        user_id: str,
        thread_id: str,
        title: str,
        source: str = "user",
    ) -> bool:
        """Explicitly set a thread title (e.g. user rename)."""
        with self.atomic_update(user_id) as store:
            meta = store.threads.get(thread_id)
            if meta is None:
                store.threads[thread_id] = ThreadMetadata(
                    thread_id=thread_id,
                    title=title,
                    title_source=source,
                    platform=classify_platform(thread_id),
                )
                return True
            meta.title = title
            meta.title_source = source
            meta.updated_at = utc_now()
            return True

    def set_pinned(
        self, user_id: str, thread_id: str, pinned: bool
    ) -> bool:
        """Set the pinned status of a thread."""
        with self.atomic_update(user_id) as store:
            meta = store.threads.get(thread_id)
            if meta is None:
                store.threads[thread_id] = ThreadMetadata(
                    thread_id=thread_id,
                    pinned=pinned,
                    platform=classify_platform(thread_id),
                )
                return True
            meta.pinned = pinned
            meta.updated_at = utc_now()
            return True

    # -- migration --

    def migrate_from_frontend(
        self,
        user_id: str,
        threads_data: List[dict],
    ) -> int:
        """Import thread metadata from frontend localStorage format.

        Only imports threads that don't already have metadata (won't overwrite).

        Args:
            threads_data: List of frontend Thread objects with fields:
                id, title, pinned, platform, createdAt, updatedAt

        Returns:
            Number of threads imported.
        """
        imported = 0
        with self.atomic_update(user_id) as store:
            for t in threads_data:
                tid = t.get("id", "")
                if not tid or tid in store.threads:
                    continue

                title = t.get("title", "New Chat")
                # Determine title_source from the title value
                title_source = "default" if title == "New Chat" else "user"

                store.threads[tid] = ThreadMetadata(
                    thread_id=tid,
                    title=title,
                    pinned=t.get("pinned", False),
                    platform=t.get("platform", classify_platform(tid)),
                    platform_meta=t.get("platformMeta"),
                    title_source=title_source,
                    created_at=_parse_dt(t.get("createdAt")),
                    updated_at=_parse_dt(t.get("updatedAt")),
                )
                imported += 1

        if imported:
            logger.info(
                f"Migrated {imported} thread(s) from frontend for user {user_id}"
            )
        return imported


def _parse_dt(value) -> datetime:
    """Parse a datetime from various formats (ISO string, timestamp, etc.)."""
    if value is None:
        return utc_now()
    if isinstance(value, datetime):
        return ensure_aware_utc(value)
    try:
        return ensure_aware_utc(
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except (ValueError, TypeError):
        return utc_now()
