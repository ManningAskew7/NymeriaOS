"""Server-side thread metadata for cross-surface sync.

Thread metadata (titles, pins, platform info) is stored here so all
surfaces (desktop, web, mobile, Discord, Telegram, Slack) share the
same view. This replaces the previous frontend-only localStorage approach.

Storage: single JSON file per user at data/thread_metadata/{user_id}.json
Pattern: follows todo_manager.py (per-user RLock, atomic writes).

A file that does not load is never saved over (#401): the original bytes go
to ``thread_metadata/quarantine/`` and the file is replaced with every row that
still validates (a bad field resets only itself); a file that cannot be read,
or whose original cannot be preserved, is served read-only and every write
raises ``ThreadMetadataUnavailableError``.
"""

import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

from pydantic import BaseModel, Field, PrivateAttr

from .keyed_locks import KeyedRLockMap
from .storage_paths import StoreUnavailableError, safe_path_segment, write_text_atomic
from .store_repair import (
    UnavailableEpisodes,
    read_json,
    record_store_repair,
    repair_file,
    salvage_fields,
)
from .time_utils import utc_now

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
    # Running USD cost across all LLM calls on this thread. Stored as integer
    # micros (USD × 1_000_000) to avoid float drift across many additions.
    # Zero when the thread has only been routed through OAuth-subscription or
    # local endpoints (see ``cost_unavailable`` in ThreadTokenUsage).
    total_cost_usd_micros: int = 0


class ThreadMetadataStore(BaseModel):
    """All thread metadata for a user. Stored as a single JSON file."""

    user_id: str = "default"
    threads: Dict[str, ThreadMetadata] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)

    # Set on a stand-in served while the file cannot be read or repaired:
    # ``save_store`` refuses it, so no caller can write it over the file.
    _read_only: bool = PrivateAttr(default=False)


class ThreadMetadataUnavailableError(StoreUnavailableError):
    """A write was refused because the user's thread list file exists but
    could not be read or repaired (#401). Saving would replace it with a
    stand-in. The API answers it 503."""


@contextmanager
def incidental_metadata_write(what: str):
    """Run a metadata write that is bookkeeping for a larger operation.

    A refused write (the list could not be read or repaired) is logged, not
    raised, so an operation that already committed (a config save, a claim,
    a clear) is not reported as failed, or left half-done, over its thread
    row. Where the metadata write IS the operation (rename, pin), call the
    manager directly and let the refusal reach the caller.
    """
    try:
        yield
    except ThreadMetadataUnavailableError as error:
        logger.warning("Skipped %s: %s", what, error)


# ---------------------------------------------------------------------------
# Loading a file that does not validate (#401)
# ---------------------------------------------------------------------------

# user_ids whose file is unreadable or unrepairable and already reported: one
# ERROR line per episode, since every turn reads the file. No owner alert: a
# transient read error (EMFILE, a network-share hiccup) would page the owner's
# external channels on every flap; the refusal itself says what is wrong (the
# TODO list's #394 precedent). Repairs, which change the file, do alert.
_unavailable = UnavailableEpisodes()
_rebind_warned: set = set()


class _Salvage(NamedTuple):
    store: ThreadMetadataStore
    kept: int
    dropped: int
    reset_fields: int
    parsed: bool  # False: nothing was salvageable (not a JSON object)


def _plural(count: int, noun: str) -> str:
    return f"1 {noun} was" if count == 1 else f"{count} {noun}s were"


def _salvage_store(data: Any, user_id: str) -> _Salvage:
    """Keep every thread row that validates, field by field within a row.

    A row with one bad field (a non-integer cost, an unknown type of
    ``platform_meta``) keeps its title and pin and resets only that field; a
    row that is not an object is dropped. The envelope's own fields are
    salvaged the same way. Non-object data salvages nothing.
    """
    if not isinstance(data, dict):
        return _Salvage(ThreadMetadataStore(user_id=user_id), 0, 0, 0, False)
    threads: Dict[str, ThreadMetadata] = {}
    dropped = reset = 0
    rows = data.get("threads")
    if isinstance(rows, dict):
        for key, row in rows.items():
            if not isinstance(row, dict):
                dropped += 1
                continue
            try:
                threads[key] = ThreadMetadata.model_validate(row)
                continue
            except Exception:  # noqa: BLE001 - salvage field by field below
                pass
            stored_id = row.get("thread_id")
            usable_id = isinstance(stored_id, str)
            meta, lost = salvage_fields(
                ThreadMetadata, row, {"thread_id": stored_id if usable_id else key}
            )
            threads[key] = meta
            reset += len(lost) + (0 if usable_id else 1)
    elif rows is not None:
        dropped += 1  # the whole thread map is unusable; count it once
    embedded = data.get("user_id")
    owner = embedded if isinstance(embedded, str) else user_id
    envelope = {k: v for k, v in data.items() if k not in ("threads", "user_id")}
    store, _ = salvage_fields(ThreadMetadataStore, envelope, {"user_id": owner})
    store.threads = threads
    return _Salvage(store, len(threads), dropped, reset, True)


def _bind(store: ThreadMetadataStore, user_id: str) -> ThreadMetadataStore:
    """Keep a loaded store saving to the file it came from.

    ``save_store`` routes by the EMBEDDED ``user_id``, so a file whose id names
    another user (a copied or hand-edited file) would write over that user's
    list on its next save. Compared as path segments, since that is what picks
    the file.
    """
    if safe_path_segment(store.user_id) != safe_path_segment(user_id):
        key = (user_id, store.user_id)
        if key not in _rebind_warned:
            _rebind_warned.add(key)
            logger.warning(
                "Thread list loaded for %s claims user_id %r; rebinding it to "
                "the file it was loaded from",
                user_id,
                store.user_id,
            )
        store.user_id = user_id
    return store


def _serialize(store: ThreadMetadataStore) -> str:
    return json.dumps(store.model_dump(mode="json"), indent=2, default=str)


def _send_alert(user_id: str, message: str) -> None:
    """Owner alert off-thread (never blocks or breaks the load)."""

    def _send() -> None:
        try:
            from ..config.settings import get_settings
            from .notification_dispatch import send_owner_alert

            send_owner_alert(message, get_settings(), user_id=user_id)
        except Exception:  # noqa: BLE001
            logger.warning("Thread list alert failed for %s", user_id, exc_info=True)

    threading.Thread(target=_send, name="thread-list-alert", daemon=True).start()


def _report_repair(user_id: str, quarantine_name: str, salvage: _Salvage) -> None:
    where = f"thread_metadata/quarantine/{quarantine_name}"
    if not salvage.parsed:
        detail = "corrupt thread list preserved; the list starts empty"
        alert = (
            f"[THREAD LIST CORRUPT] Your thread list file (titles, pins, "
            f"running costs) could not be parsed (edited by hand or by a "
            f"tool?), so a new, empty one started. No conversation was "
            f"deleted, but threads may be missing from the list or show "
            f"default titles. The original is preserved at {where}, and an "
            f"admin can restore it."
        )
    else:
        detail = (
            f"invalid thread list repaired: kept {salvage.kept} thread(s), "
            f"dropped {salvage.dropped}, reset {salvage.reset_fields} field(s)"
        )
        alert = (
            f"[THREAD LIST REPAIRED] Your thread list file failed validation "
            f"(edited by hand or by a tool?). {_plural(salvage.kept, 'thread')} "
            f"kept"
            + (
                f"; {_plural(salvage.dropped, 'unreadable row')} dropped"
                if salvage.dropped
                else ""
            )
            + (
                f"; {_plural(salvage.reset_fields, 'unreadable field')} reset "
                f"to its default"
                if salvage.reset_fields
                else ""
            )
            + f". The original file is preserved at {where}."
        )
    logger.warning(
        "Thread list for %s did not load; %s (original at %s)", user_id, detail, where
    )
    record_store_repair(
        "thread_metadata",
        f"{detail} (original at quarantine/{quarantine_name})",
        user_id=user_id,
    )
    _send_alert(user_id, alert)


def _read_only(store: ThreadMetadataStore, user_id: str, why: str) -> ThreadMetadataStore:
    """Serve ``store`` read-only, logging once per episode."""
    store._read_only = True
    if _unavailable.start(user_id):
        logger.error(
            "Thread list for %s is unavailable (%s); serving it read-only and "
            "refusing writes",
            user_id,
            why,
        )
    else:
        logger.debug("Thread list for %s still unavailable: %s", user_id, why)
    return store


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
        """Context manager for read-modify-write on the metadata store.

        Raises ``ThreadMetadataUnavailableError`` before the body runs when the
        file exists but could not be read or repaired.
        """
        lock = self._get_lock(user_id)
        with lock:
            store = self.get_store(user_id)
            if store._read_only:
                raise ThreadMetadataUnavailableError(
                    f"Thread list for user {user_id} could not be read or "
                    f"repaired; refusing to overwrite it"
                )
            try:
                yield store
            finally:
                self.save_store(store)

    # -- file I/O --

    def _get_path(self, user_id: str) -> Path:
        safe_id = safe_path_segment(user_id)
        return self.metadata_dir / f"{safe_id}.json"

    def get_store(self, user_id: str = "default") -> ThreadMetadataStore:
        """Load the metadata store for a user, or create an empty one.

        A healthy read takes no lock. A file that does not load is repaired
        under the user's lock (``_repair``); one that cannot be read comes
        back read-only.
        """
        read = read_json(self._get_path(user_id))
        if read.absent:
            _unavailable.end(user_id)
            return ThreadMetadataStore(user_id=user_id)
        if read.unreadable:
            return _read_only(ThreadMetadataStore(user_id=user_id), user_id, str(read.error))
        store = self._validated(read.data, user_id) if read.parsed else None
        if store is not None:
            _unavailable.end(user_id)
            return store
        return self._repair(user_id)

    @staticmethod
    def _validated(data: Any, user_id: str) -> Optional[ThreadMetadataStore]:
        try:
            store = ThreadMetadataStore.model_validate(data)
        except Exception:  # noqa: BLE001 - the caller repairs
            return None
        return _bind(store, user_id)

    def _repair(self, user_id: str) -> ThreadMetadataStore:
        """Salvage what validates, preserve the original, replace the file.

        Under the user's lock (re-entrant, so ``atomic_update``'s own hold is
        fine) and re-reading under it, since a concurrent load may already
        have repaired the file. A blocking acquire is safe here, unlike the
        TODO list's (#394): no ``atomic_update`` body takes another lock, so
        no holder of this lock can be waiting on one a reader holds.
        """
        path = self._get_path(user_id)
        with self._get_lock(user_id):
            read = read_json(path)
            if read.absent:
                return ThreadMetadataStore(user_id=user_id)
            if read.unreadable:
                return _read_only(ThreadMetadataStore(user_id=user_id), user_id, str(read.error))
            healthy = self._validated(read.data, user_id) if read.parsed else None
            if healthy is not None:
                _unavailable.end(user_id)
                return healthy
            assert read.raw is not None
            salvage = _salvage_store(read.data, user_id)
            store = _bind(salvage.store, user_id)
            repaired = repair_file(
                path,
                read.raw,
                _serialize(store),
                lambda again: self._validated(again.data, user_id) if again.parsed else None,
                store="thread_metadata",
                user_id=user_id,
            )
            if repaired.status == "replaced" and repaired.quarantine is not None:
                _unavailable.end(user_id)
                _report_repair(user_id, repaired.quarantine.name, salvage)
                return store
            if repaired.status == "superseded":
                # Another writer replaced the file after the read; theirs is
                # kept (the bytes this load saw are in quarantine).
                _unavailable.end(user_id)
                return repaired.fresh
            return _read_only(store, user_id, f"{read.error or 'invalid'}; could not be repaired")

    def save_store(self, store: ThreadMetadataStore) -> bool:
        """Atomically write the metadata store to disk.

        Refuses a read-only stand-in (a file that could not be read or
        repaired), which would replace the real file.
        """
        if store._read_only:
            logger.error(
                "Refusing to save the read-only thread list stand-in for %s",
                store.user_id,
            )
            return False
        path = self._get_path(store.user_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            store.updated_at = utc_now()
            write_text_atomic(path, _serialize(store))
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
                # Validated, unlike ``model_copy(update=...)``: a wrong-typed
                # field would otherwise be saved into a row that fails the
                # model on the next load and sends the list into repair.
                updated = ThreadMetadata.model_validate(
                    {**existing.model_dump(), **update_data}
                )
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

    def ensure_thread(self, user_id: str, thread_id: str) -> ThreadMetadata:
        """Make sure a metadata row exists; never touch one that does.

        The listing and every by-name command resolve against this store, so a
        thread that exists elsewhere (a config row, a checkpoint) but has no
        row here is unaddressable (#272). Callers that merely PROVE a thread
        exists (a config write, a turn that errored before its title step) use
        this rather than ``upsert_thread``, which stamps ``updated_at`` on an
        existing row: a config write is not activity, and must not reorder
        the recent-threads list.
        """
        present = self.get_store(user_id).threads.get(thread_id)
        if present is not None:
            return present  # no write: the common case must not touch the file
        with self.atomic_update(user_id) as store:
            existing = store.threads.get(thread_id)
            if existing is not None:
                return existing
            meta = ThreadMetadata(
                thread_id=thread_id, platform=classify_platform(thread_id)
            )
            store.threads[thread_id] = meta
            return meta

    def ensure_thread_unless_deleting(
        self, user_id: str, thread_id: str
    ) -> Optional[ThreadMetadata]:
        """:meth:`ensure_thread`, atomic against a concurrent thread deletion.

        A bare epoch read followed by the write leaves a window in which
        ``begin_thread_deletion`` + the metadata delete interleave and the row
        is resurrected; holding the admission guard across the write (the
        turn runner's shape) closes it. Returns None while deleting.
        """
        from .thread_lock_manager import thread_admission_guard

        with thread_admission_guard(thread_id) as epoch:
            if epoch < 0:
                return None
            return self.ensure_thread(user_id, thread_id)

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
            try:
                with self.atomic_update(user_id) as locked_store:
                    if thread_id in locked_store.threads:
                        del locked_store.threads[thread_id]
                        deleted += 1
            except ThreadMetadataUnavailableError:
                # One user's unrepairable file must not stop the others'
                # cleanup; its row stays until the file loads again.
                logger.warning(
                    "Thread %s left in %s's unavailable thread list", thread_id, user_id
                )
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

