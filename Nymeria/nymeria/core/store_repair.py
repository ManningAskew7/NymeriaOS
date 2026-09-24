"""Shared plumbing for JSON stores that repair a file they cannot load.

The rule these helpers exist for (#394, #400, #401): a store that cannot load
its file must never answer an empty default that its next save then writes
over the file. The original bytes are preserved first, then the file is
replaced with whatever still validates; when the bytes cannot be preserved,
or the file cannot be read at all, the store is read-only until it can.

The TODO list (``todo_manager``) and the user profile (``user_profile``) grew
their own copies of this dance first; the six #401 stores (thread metadata,
notifications, FCM tokens, workflow state, scheduler state, capability usage)
share these instead. Each store keeps its own lock, salvage rule and
read-only semantics; what is common is how a file is read, how an original is
preserved and replaced (``repair_file``), and the audit row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Hashable,
    List,
    NamedTuple,
    Optional,
    Tuple,
    Type,
    TypeVar,
)

from pydantic import BaseModel

from .storage_paths import quarantine_copy, write_text_atomic

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)


class JsonRead(NamedTuple):
    """One read of a JSON store file.

    ``raw`` is the bytes read (None when the file is absent or unreadable),
    ``data`` the parsed document (None unless it parsed), ``error`` a log-safe
    reason when it could not be read or parsed.
    """

    raw: Optional[bytes]
    data: Any
    error: Optional[str]

    @property
    def absent(self) -> bool:
        return self.raw is None and self.error is None

    @property
    def unreadable(self) -> bool:
        return self.raw is None and self.error is not None

    @property
    def parsed(self) -> bool:
        return self.raw is not None and self.error is None


def read_json(path: Path) -> JsonRead:
    """Read and parse ``path`` without ever raising.

    Parses the BYTES: ``json.loads`` detects UTF-8 (with or without a BOM),
    UTF-16 and UTF-32, so a file saved by a Windows editor or a PowerShell
    redirect loads; text in another encoding (a cp1252 save with a non-ASCII
    byte) fails to decode and is reported as not JSON, like truncated text,
    and so is nesting too deep to parse (``RecursionError``: some thousands of
    open brackets, which the #394/#400 loaders already treat as corrupt).
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return JsonRead(None, None, None)
    except OSError as error:
        return JsonRead(None, None, f"unreadable ({type(error).__name__}: {error})")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as error:  # incl. JSONDecode/UnicodeDecode
        return JsonRead(raw, None, f"not valid JSON ({type(error).__name__}: {error})")
    return JsonRead(raw, data, None)


class RepairResult(NamedTuple):
    """What :func:`replace_preserving` did.

    ``replaced``: the live file now holds the repaired content and
    ``quarantine`` holds the original. ``changed``: the file changed after it
    was read, so nothing was replaced (``quarantine`` still holds the bytes
    that were read); re-read it. Neither: nothing was preserved and the live
    file is untouched, so the caller must not write the store.
    """

    quarantine: Optional[Path]
    replaced: bool
    changed: bool


def replace_preserving(
    path: Path, raw: bytes, content: str, *, mode: Optional[int] = None
) -> RepairResult:
    """Copy ``raw`` (the bytes just read from ``path``) to quarantine, then
    atomically replace ``path`` with ``content``. Never raises.

    Copy-then-replace rather than a rename: the file never goes absent, so a
    lock-free reader never takes a missing file for an empty store. Just
    before replacing, the file is checked unchanged since the read; the
    stores' locks are in-process only, so this narrows (does not close) the
    window in which another process's newer write is replaced by a stale
    repair. A failed write-back removes the copy: the original is still the
    live file, and a full disk must not grow one copy per load.
    """
    quarantine = quarantine_copy(path, raw)
    if quarantine is None:
        return RepairResult(None, False, False)
    try:
        unchanged = path.read_bytes() == raw
    except OSError:
        unchanged = False
    if not unchanged:
        return RepairResult(quarantine, False, True)
    try:
        write_text_atomic(path, content, mode=mode)
    except Exception:  # noqa: BLE001 - any failure leaves the original live
        logger.warning("Could not write back repaired %s", path, exc_info=True)
        try:
            quarantine.unlink()
        except OSError:
            pass  # a leftover copy is harmless; the store still reports unrepaired
        return RepairResult(None, False, False)
    return RepairResult(quarantine, True, False)


# A repair of these exact bytes failed at this monotonic time, keyed by path:
# a store read on a hot path (every turn, every tool call) must not copy,
# fsync and unlink a whole file on every read while the disk stays full or
# the directory unwritable. Retried once the bytes change or the window ends.
REPAIR_RETRY_SECONDS = 60.0
_failed_repairs: Dict[str, Tuple[str, float]] = {}
_failed_repairs_lock = threading.Lock()


class Repaired(NamedTuple):
    """What :func:`repair_file` did.

    ``replaced``: the file now holds the repaired content, the original is at
    ``quarantine``. ``superseded``: the file changed during the repair and the
    newer one validates (``fresh``, the caller's validated form); it is kept,
    and ``quarantine`` holds the bytes that were read. ``failed``: nothing was
    replaced (``quarantine`` may hold a copy when the newer file also failed),
    so the caller must treat the store as read-only for this load.
    """

    status: str
    quarantine: Optional[Path]
    fresh: Any


def repair_file(
    path: Path,
    raw: bytes,
    content: str,
    validate: Callable[[JsonRead], Any],
    *,
    store: str,
    user_id: str = "default",
    mode: Optional[int] = None,
) -> Repaired:
    """Preserve ``raw`` (bytes of ``path`` that did not load), then replace
    ``path`` with ``content``. Never raises.

    ``validate(read)`` returns the caller's validated document, or None when
    the read does not load; it judges the newer file when another writer
    replaced ``path`` during the repair. A copy left behind by that race gets
    its own audit row here (``store``, ``user_id``); the caller writes the row
    for a ``replaced`` result, since only it can describe the salvage.
    A repair of identical bytes that failed within ``REPAIR_RETRY_SECONDS``
    is not attempted again (``failed``, no copy).
    """
    key = str(path)
    digest = hashlib.sha256(raw).hexdigest()
    now = time.monotonic()
    with _failed_repairs_lock:
        previous = _failed_repairs.get(key)
    if previous is not None and previous[0] == digest and now - previous[1] < REPAIR_RETRY_SECONDS:
        return Repaired("failed", None, None)
    result = replace_preserving(path, raw, content, mode=mode)
    if result.replaced:
        with _failed_repairs_lock:
            _failed_repairs.pop(key, None)
        return Repaired("replaced", result.quarantine, None)
    if result.changed and result.quarantine is not None:
        with _failed_repairs_lock:
            _failed_repairs.pop(key, None)
        try:
            fresh = validate(read_json(path))
        except Exception:  # noqa: BLE001 - a validator bug reads as "does not load"
            fresh = None
        record_store_repair(
            store,
            f"original preserved while the file changed during its repair; "
            f"the newer file was {'kept' if fresh is not None else 'left for the next load'} "
            f"(original at quarantine/{result.quarantine.name})",
            user_id=user_id,
        )
        if fresh is not None:
            return Repaired("superseded", result.quarantine, fresh)
        return Repaired("failed", result.quarantine, None)
    with _failed_repairs_lock:
        _failed_repairs[key] = (digest, now)
    return Repaired("failed", None, None)


def salvage_fields(
    model_cls: Type[ModelT], data: Dict[str, Any], base: Dict[str, Any]
) -> Tuple[ModelT, List[str]]:
    """Keep each field of ``data`` that validates alongside those already kept.

    ``base`` seeds the kept fields (a record's required identity, which the
    caller supplies when the stored one is unusable) and wins over ``data``.
    Fields are tried one at a time against the growing record, so a model with
    required fields salvages field by field and a cross-field validator still
    sees what it validates against. Unknown keys are ignored, as validation
    would. Returns the model (``base`` alone when nothing else fits) and the
    names of the fields dropped. ``base`` itself must validate.
    """
    kept: Dict[str, Any] = dict(base)
    dropped: List[str] = []
    for key, value in data.items():
        if key in base or key not in model_cls.model_fields:
            continue
        candidate = {**kept, key: value}
        try:
            model_cls.model_validate(candidate)
        except Exception:  # noqa: BLE001 - pydantic errors, bad types alike
            dropped.append(key)
            continue
        kept = candidate
    return model_cls.model_validate(kept), dropped


def record_store_repair(store: str, detail: str, *, user_id: str = "default") -> None:
    """The ``external_edit`` audit row for a repaired or preserved store file.

    Never raises: the audit must not break the load that triggered it.
    """
    try:
        from .activity_log import log_external_edit

        log_external_edit(store, detail, user_id=user_id)
    except Exception:  # noqa: BLE001
        logger.debug("Failed to record %s repair audit", store, exc_info=True)


class UnavailableEpisodes:
    """Once-per-episode reporting for a store that is serving read-only.

    Hot paths read these stores (every turn, every tool call), so an
    unreadable file must log one ERROR when it becomes unavailable, not one
    per read. ``start`` answers True the first time for a key; ``end`` (an
    authoritative load) re-arms it.
    """

    def __init__(self) -> None:
        self._keys: set = set()
        self._lock = threading.Lock()

    def start(self, key: Hashable) -> bool:
        with self._lock:
            if key in self._keys:
                return False
            self._keys.add(key)
            return True

    def end(self, key: Hashable) -> None:
        with self._lock:
            self._keys.discard(key)
