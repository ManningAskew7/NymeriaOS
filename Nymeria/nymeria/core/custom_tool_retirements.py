"""Custom-tool retirement records (backlog #391).

Retiring a published custom tool (``tool_create(action="retire")``, #270, or
the two admin REST deletes) frees its id, while every thread that listed the
name keeps it in ``enabled_tools`` and skips it at graph build. Whoever
publishes under the same id next is therefore bound by all of those threads
with no new consent step. This record closes that window: an id retired by
user A is refused to user B at draft and at publish; A may re-publish (which
clears the record), and the admin dashboard create clears it as the human
override.

Persisted in the shared ``accounts.db`` rather than as a JSON child of
``custom_tools/`` on purpose: the file tools refuse ``accounts.db``, so the
record cannot be planted (to block someone's id) or removed (to bypass the
refusal) with ``file_write``, which a JSON sibling could be; and a new
agent-writable data-dir child would need its own row in the store register,
where a store that drives a decision and has no control is not allowed.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


CUSTOM_TOOL_RETIREMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS custom_tool_retirements (
    tool_id     TEXT PRIMARY KEY,
    retired_by  TEXT NOT NULL,
    retired_at  TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class CustomToolRetirement:
    tool_id: str
    retired_by: str
    retired_at: str


class CustomToolRetirementsRepo:
    """Thread-safe SQLite store of retired custom-tool ids (one row per id;
    a later retire of the same id overwrites the actor)."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.executescript(CUSTOM_TOOL_RETIREMENTS_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def record(self, tool_id: str, *, retired_by: str) -> CustomToolRetirement:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO custom_tool_retirements "
                "(tool_id, retired_by, retired_at) VALUES (?, ?, ?)",
                (tool_id, retired_by, now),
            )
            conn.commit()
        return CustomToolRetirement(tool_id=tool_id, retired_by=retired_by, retired_at=now)

    def get(self, tool_id: str) -> Optional[CustomToolRetirement]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tool_id, retired_by, retired_at FROM custom_tool_retirements "
                "WHERE tool_id = ?",
                (tool_id,),
            ).fetchone()
        if row is None:
            return None
        return CustomToolRetirement(
            tool_id=row["tool_id"], retired_by=row["retired_by"], retired_at=row["retired_at"],
        )

    def clear(self, tool_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM custom_tool_retirements WHERE tool_id = ?", (tool_id,),
            )
            conn.commit()
            return cur.rowcount > 0


def retirement_refusal(retirement: CustomToolRetirement, tool_id: str) -> str:
    """The one sentence every refusing surface shows: who, when, why, and
    the ways forward that actually get the asker the id."""
    day = retirement.retired_at[:10]
    return (
        f"tool_id '{tool_id}' was retired by '{retirement.retired_by}' on {day}. "
        "Drafting or publishing under a retired id is refused because every "
        "thread that still lists the name would bind the new tool silently "
        "(a draft under it could never publish). Choose another "
        "id, or ask an admin to release the reservation "
        f"(DELETE /tools/custom/{tool_id}/retirement)."
    )


_repo: Optional[CustomToolRetirementsRepo] = None
_repo_lock = threading.Lock()


def _default_db_path() -> Path:
    """The shared accounts database. A seam: the test suite redirects it so
    an unpatched publish or delete can never touch a checkout's real db."""
    from ..config import get_settings

    return get_settings().data_dir / "accounts.db"


def get_custom_tool_retirements_repo() -> CustomToolRetirementsRepo:
    """Process singleton over ``<data_dir>/accounts.db`` (lazy: importing this
    module never touches disk)."""
    global _repo
    if _repo is None:
        with _repo_lock:
            if _repo is None:
                _repo = CustomToolRetirementsRepo(_default_db_path())
    return _repo


def record_retirement(tool_id: str, *, retired_by: str) -> bool:
    """Best-effort write for the callers that have ALREADY deleted the file:
    a bookkeeping failure must not turn a completed retire into an error or
    skip the reload that follows. Returns whether the row was written."""
    try:
        get_custom_tool_retirements_repo().record(tool_id, retired_by=retired_by)
        return True
    except Exception:
        logger.warning("could not record the retirement of %s", tool_id, exc_info=True)
        return False


def clear_retirement(tool_id: str) -> bool:
    """Best-effort clear for the create paths (same reasoning as above)."""
    try:
        return get_custom_tool_retirements_repo().clear(tool_id)
    except Exception:
        logger.warning("could not clear the retirement of %s", tool_id, exc_info=True)
        return False
