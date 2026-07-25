"""Shared durable approval-record store (backlog #106).

Three approval systems keep a durable JSON record per pending decision so
resolve surfaces can list what is pending and a crash leaves an auditable
orphan instead of nothing: hook approvals (``core/hook_approvals.py``),
workflow ``nym.approve`` suspensions (``core/workflows/approvals.py``), and
LLM fallback consent (``core/fallback_approvals.py``). Each grew its own
near-identical store half; this module is the extraction of that half, the
same three-copies threshold that produced ``future_rendezvous.py`` for the
coordinator half.

The store owns only the MECHANICS, composed from the package's canonical
helpers (``storage_paths.safe_path_segment``/``write_text_atomic``/
``mtime_sort_key``, ``time_utils``): directory resolution, the id sanitizer,
atomic writes, load/delete, the newest-first corrupt-skipping list, and the
two expiry primitives. Everything semantically feature-owned deliberately
stays in the feature modules: record schemas, pending caps and their check
placement, mint ordering, the public (secret-withholding) views, announce
copy and events, the workflow claim-file mutex, and what a sweep DOES with
an expired record (hook/fallback delete crash orphans; workflow expiry runs
the declared continuation as a decline).

Settings resolve lazily at call time so test fixtures that patch
``nymeria.config.get_settings`` keep working.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .storage_paths import mtime_sort_key, safe_path_segment, write_text_atomic
from .time_utils import parse_usage_timestamp as _parse_iso
from .time_utils import utc_now

logger = logging.getLogger(__name__)


def clamp_window(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    """Clamp an author/config-supplied wait window into the feature's bounds."""
    try:
        window = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(window, maximum))


class ApprovalRecordStore:
    """One durable-record store under ``data_dir/<subdir>/<record_id>.json``.

    ``noun`` labels log lines (e.g. ``"hook approval"``). Records are plain
    dicts carrying at least ``record_id`` and usually ``user_id`` /
    ``expires_at``; the store never inspects anything else.
    """

    def __init__(self, subdir: str, *, noun: str) -> None:
        self._subdir = subdir
        self._noun = noun

    def dir(self) -> Path:
        from ..config import get_settings
        return get_settings().data_dir / self._subdir

    def record_path(self, record_id: str) -> Path:
        # default="" keeps the pre-extraction possibly-empty contract of the
        # three stores' inline sanitizers (byte-identical character set).
        return self.dir() / f"{safe_path_segment(record_id, default='')}.json"

    def write(self, record: Dict[str, Any]) -> None:
        """Atomically persist ``record`` (keyed by its ``record_id``)."""
        path = self.record_path(str(record["record_id"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, json.dumps(record, indent=2, default=str))

    def load(self, record_id: str) -> Optional[Dict[str, Any]]:
        try:
            record = json.loads(self.record_path(record_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None

    def delete(self, record_id: str) -> None:
        try:
            self.record_path(record_id).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "could not delete %s record %s", self._noun, record_id, exc_info=True
            )

    def _iter_records(self) -> Iterator[tuple[Path, Dict[str, Any]]]:
        """Yield ``(path, record)`` pairs, newest first.

        Corrupt or non-dict files are skipped, never raised on: a torn write
        or a hand-edited store file must not take down a resolve surface.
        """
        base = self.dir()
        if not base.is_dir():
            return
        for path in sorted(base.glob("*.json"), key=mtime_sort_key, reverse=True):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(record, dict):
                yield path, record

    def list(self, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Records newest first; ``user_id`` filters to one owner."""
        return [
            record
            for _, record in self._iter_records()
            if user_id is None or record.get("user_id") == user_id
        ]

    def sweep_stale(self, slack_seconds: float) -> int:
        """Delete records past ``expires_at`` + slack (or with junk expiry).

        The hook/fallback sweep shape: timeout enforcement lives with the
        waiter (which deletes its own record on every exit), so anything
        still here past expiry is a crash orphan, and a record whose expiry
        cannot be parsed is treated the same. Deletion is by the FILE the
        record came from, not its self-declared ``record_id`` (an id-less
        record would otherwise be counted as swept every hour while its file
        survives forever). Returns the removed count.
        """
        now = utc_now()
        removed = 0
        for path, record in self._iter_records():
            expiry = _parse_iso(record.get("expires_at"))
            if expiry is None or now > expiry + timedelta(seconds=slack_seconds):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "could not delete %s record %s",
                        self._noun, path.name, exc_info=True,
                    )
                    continue
                removed += 1
        if removed:
            logger.info("swept %d stale %s record(s)", removed, self._noun)
        return removed

    def expired(self) -> List[Dict[str, Any]]:
        """Records whose (valid) ``expires_at`` has passed, newest first.

        The workflow sweep shape: expiry there is a DECISION (the
        continuation runs as a decline), so unparseable expiries are left
        alone rather than destroyed, and the caller decides what to do.
        """
        now = utc_now()
        return [
            record
            for _, record in self._iter_records()
            if (expires := _parse_iso(record.get("expires_at"))) is not None
            and expires <= now
        ]


__all__ = [
    "ApprovalRecordStore",
    "clamp_window",
]
