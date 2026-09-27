"""Persisted scheduler lifecycle state for on/off slim runtimes.

A file that does not load is never saved over (#401): the ticker stamps it at
every boot, which used to replace a corrupt file (and the pending missed-TODO
ids and catch-up pause it held) with defaults. Its bytes now go to
``quarantine/`` in the data dir first; a file that cannot be read or
preserved is left alone and the stamps are skipped.

Beside it lives the one record ANOTHER process writes: the missed-work
release request (#398). Under ``ask`` the hold lives in the process that runs
the scheduler (the Docker worker), while the admin asks the API. The API
writes ``scheduler_release_request.json``; the owner's poll honors a request
newer than the hold it has and releases. The request is never deleted: it is
"the last release request", and a hold detected after it (every restart
detects afresh) is never released by it, so a leftover file is inert and no
process ever read-modify-writes another's state file. That rule holds only
because a request dated in the FUTURE is no request (``read_release_request``
drops one beyond ``RELEASE_REQUEST_MAX_SKEW``): a far-future stamp would
otherwise be "newer" than every hold, and one planted write would release
the hold at every boot, forever.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .storage_paths import write_text_atomic
from .store_repair import UnavailableEpisodes, read_json, record_store_repair, repair_file

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_state() -> dict[str, Any]:
    return {
        "last_started_at": None,
        "last_clean_shutdown_at": None,
        "last_missed_detection_at": None,
        "pending_missed_todo_ids": [],
        "trigger_catchup_paused": False,
    }


_TIMESTAMP_KEYS = ("last_started_at", "last_clean_shutdown_at", "last_missed_detection_at")

# Paths already reported unreadable or unrepairable: one WARNING per episode
# (the API's status route reads the file too).
_unavailable = UnavailableEpisodes()


def _field_ok(key: str, value: Any) -> bool:
    if key in _TIMESTAMP_KEYS:
        return value is None or isinstance(value, str)
    if key == "pending_missed_todo_ids":
        return isinstance(value, list) and all(isinstance(t, str) and t for t in value)
    if key == "trigger_catchup_paused":
        return isinstance(value, bool)
    return True  # unknown keys are carried, as before


def _validated_state(data: Any) -> dict[str, Any] | None:
    """The stored state when it is an object whose known fields have their
    types, else None. A wrong type used to crash boot (an int id list is not
    iterable) or read wrong (a string id list split into characters)."""
    if not isinstance(data, dict):
        return None
    if all(_field_ok(key, value) for key, value in data.items()):
        return data
    return None


def _salvaged_state(data: Any) -> dict[str, Any]:
    """Defaults, plus every stored field that has its type (the string ids of
    a mixed id list are kept)."""
    state = _default_state()
    if not isinstance(data, dict):
        return state
    for key, value in data.items():
        if key == "pending_missed_todo_ids" and isinstance(value, list):
            state[key] = [t for t in value if isinstance(t, str) and t]
        elif _field_ok(key, value):
            state[key] = value
    return state


def _serialize(state: dict[str, Any]) -> str:
    return json.dumps(state, indent=2, sort_keys=True) + "\n"


class SchedulerStateManager:
    """Read and write the scheduler's small JSON lifecycle state file."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "scheduler_state.json"
        self._lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> dict[str, Any]:
        return self._read_unlocked()[0]

    def _read_unlocked(self) -> tuple[dict[str, Any], bool]:
        """The state, and whether the file may be written.

        A file that does not load (not JSON, not an object, a known field of
        the wrong type) is preserved in ``quarantine/`` and replaced with the
        fields that still have their types, defaults for the rest; one that
        cannot be read or preserved is left alone and not saved over.
        """
        key = str(self.path)
        read = read_json(self.path)
        if read.absent:
            _unavailable.end(key)
            return _default_state(), True
        if read.unreadable:
            self._report_unavailable(str(read.error))
            return _default_state(), False
        valid = _validated_state(read.data) if read.parsed else None
        if valid is not None:
            _unavailable.end(key)
            return {**_default_state(), **valid}, True
        assert read.raw is not None
        salvaged = _salvaged_state(read.data if read.parsed else None)
        repaired = repair_file(
            self.path,
            read.raw,
            _serialize(salvaged),
            lambda again: _validated_state(again.data) if again.parsed else None,
            store="scheduler_state",
        )
        if repaired.status == "superseded":
            _unavailable.end(key)
            return {**_default_state(), **repaired.fresh}, True
        if repaired.status == "replaced" and repaired.quarantine is not None:
            _unavailable.end(key)
            detail = (
                "invalid scheduler state repaired; fields that did not load "
                "start from their defaults"
            )
            logger.warning(
                "Scheduler state %s did not load (%s); %s (original at %s)",
                self.path,
                read.error or "invalid fields",
                detail,
                repaired.quarantine,
            )
            record_store_repair(
                "scheduler_state",
                f"{detail} (original at quarantine/{repaired.quarantine.name})",
            )
            return salvaged, True
        self._report_unavailable(f"{read.error or 'invalid fields'}; could not be repaired")
        return salvaged, False

    def _report_unavailable(self, why: str) -> None:
        if _unavailable.start(str(self.path)):
            logger.warning(
                "Scheduler state %s is unavailable (%s); using what could be "
                "read and not saving over it",
                self.path,
                why,
            )

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(self.path, _serialize(state))
        except Exception as exc:
            logger.warning("Failed to write scheduler state %s: %s", self.path, exc)

    def record_start(self) -> dict[str, Any]:
        with self._lock:
            state, writable = self._read_unlocked()
            state["last_started_at"] = _now_iso()
            if writable:
                self._save_unlocked(state)
            return state

    def record_clean_shutdown(self) -> dict[str, Any]:
        with self._lock:
            state, writable = self._read_unlocked()
            state["last_clean_shutdown_at"] = _now_iso()
            if writable:
                self._save_unlocked(state)
            return state

    def set_pending_missed(
        self,
        todo_ids: list[str],
        *,
        trigger_catchup_paused: bool,
    ) -> dict[str, Any]:
        with self._lock:
            state, writable = self._read_unlocked()
            state["last_missed_detection_at"] = _now_iso()
            state["pending_missed_todo_ids"] = [
                str(todo_id) for todo_id in dict.fromkeys(todo_ids) if todo_id
            ]
            state["trigger_catchup_paused"] = bool(trigger_catchup_paused)
            if writable:
                self._save_unlocked(state)
            return state

    def clear_pending_missed(self) -> dict[str, Any]:
        with self._lock:
            state, writable = self._read_unlocked()
            state["pending_missed_todo_ids"] = []
            state["trigger_catchup_paused"] = False
            if writable:
                self._save_unlocked(state)
            return state


# --- the missed-work release request (#398) -------------------------------

RELEASE_REQUEST_FILENAME = "scheduler_release_request.json"

# Clock slack for a request stamped by another process on the same host (the
# Docker containers share the kernel clock; the lock module already scopes
# out cross-host data dirs). A request dated later than now plus this is
# ignored: see the module notes.
RELEASE_REQUEST_MAX_SKEW = timedelta(seconds=60)


@dataclass(frozen=True)
class ReleaseRequest:
    requested_at: datetime
    requested_by: str


def parse_state_time(value: Any) -> Optional[datetime]:
    """A stored ISO timestamp as an aware datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def release_request_path(data_dir: Path) -> Path:
    return Path(data_dir) / RELEASE_REQUEST_FILENAME


def write_release_request(data_dir: Path, requested_by: str) -> ReleaseRequest:
    """Record an admin's request to release held missed work. Raises OSError
    when the data dir cannot be written (the caller reports it)."""
    request = ReleaseRequest(
        requested_at=datetime.now(timezone.utc),
        requested_by=_printable(requested_by) or "unknown",
    )
    write_text_atomic(
        release_request_path(data_dir),
        json.dumps(
            {
                "requested_at": request.requested_at.isoformat(),
                "requested_by": request.requested_by,
            }
        )
        + "\n",
    )
    return request


def read_release_request(data_dir: Path) -> Optional[ReleaseRequest]:
    """The last release request, or None (absent, unreadable, malformed, or
    dated in the future: a request that does not parse is no request, and
    nothing saves over it but the next request)."""
    path = release_request_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.debug("Unreadable scheduler release request %s", path, exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    requested_at = parse_state_time(data.get("requested_at"))
    if requested_at is None:
        return None
    if requested_at > datetime.now(timezone.utc) + RELEASE_REQUEST_MAX_SKEW:
        logger.warning(
            "Ignoring scheduler release request %s: it is dated in the future (%s)",
            path,
            requested_at.isoformat(timespec="seconds"),
        )
        return None
    return ReleaseRequest(requested_at, _printable(data.get("requested_by")) or "unknown")


def _printable(value: Any) -> str:
    # Written by whoever can reach the data dir, read into logs and replies.
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable())[:200]
