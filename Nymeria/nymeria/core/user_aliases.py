"""Per-user command aliases (backlog #133).

A small persisted table mapping a user's single-token spelling to a WHOLE
canonical command expansion (`/gpt5` -> `model` plus the model id). The
dispatch seam in :meth:`~nymeria.core.command_service.CommandService.execute`
consults it before parsing; expansion produces canonical tokens, so every
gate reads the resolved real command and an alias can never widen access.

Shares ``data/accounts.db`` with :class:`~nymeria.core.accounts.AccountsRepo`
and the other account-scoped repos; schema ownership is split, each repo
creates its own tables via ``CREATE TABLE IF NOT EXISTS``.

**Control (control-store-matrix rule 3).** Alias rows drive execution and the
agent may author them (dev decision, 2026-08-03), so each row carries an
execution-trust stamp: SHA-256 over canonical JSON of the security-relevant
fields (user, name, expansion tokens, authoring actor), written ONLY by the
authoring path (the ``/alias`` command handler through
:meth:`UserAliasesRepo.create_alias`) and recomputed at every dispatch
resolution. A row edited out of band goes INERT: dispatch skips it with a
warning and ``/alias list`` flags it for re-creation. Residual, stated per
the matrix: the hash is unkeyed and lives beside the fields it covers, so a
writer who can reach the database can forge it; ``accounts.db`` is on the
file-tool denylist (prefix-matched with its WAL/SHM sidecars), and the only
boundary Nymeria relies on is the operating system (SECURITY.md section 2.2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)

# Shape/volume bound in the triggers idiom (MAX_TRIGGERS): constrains what a
# runaway author can plant, never who may author.
MAX_ALIASES_PER_USER = 100


@dataclass
class UserCommandAlias:
    """One user's alias row.

    ``tokens`` is the full expansion (canonical target path plus any injected
    argument tokens); ``command_id`` records the target resolved at authoring
    time so listings can flag a target that has since vanished.
    ``stamp_valid`` is computed on read, never stored.
    """

    id: str
    user_id: str
    name: str
    command_id: str
    tokens: tuple[str, ...]
    author_actor: str
    created_at: str
    stamp_valid: bool


class AliasAlreadyExists(ValueError):
    """An alias with this name already exists for this user."""


class AliasLimitReached(ValueError):
    """The per-user alias cap would be exceeded."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp(
    user_id: str,
    name: str,
    command_id: str,
    tokens: Sequence[str],
    author_actor: str,
) -> str:
    """The authoring-path execution-trust stamp (module docstring).

    ``command_id`` is covered even though dispatch expands from ``tokens``:
    the listing derives its dormancy verdict from it, and a field the
    listing trusts but the stamp ignores is a field an out-of-band writer
    can lie through.
    """
    canonical = json.dumps(
        {
            "author": author_actor,
            "command_id": command_id,
            "name": name,
            "tokens": list(tokens),
            "user_id": user_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


USER_ALIASES_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_command_aliases (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    command_id   TEXT NOT NULL,
    tokens_json  TEXT NOT NULL,
    author_actor TEXT NOT NULL,
    stamp        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE(user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_user_command_aliases_user
    ON user_command_aliases(user_id);
"""


class UserAliasesRepo:
    """Thread-safe SQLite-backed repo for per-user command aliases.

    Name/expansion VALIDATION (single token, loses to every registered path
    and built-in alias, target must resolve) belongs to the command layer,
    which holds the registry; the repo owns persistence, the cap, and the
    stamp.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(USER_ALIASES_SCHEMA)
            conn.commit()

    def create_alias(
        self,
        *,
        user_id: str,
        name: str,
        command_id: str,
        tokens: Sequence[str],
        author_actor: str,
    ) -> UserCommandAlias:
        """Create an alias, stamping it. THE authoring path; nothing else
        writes rows. Raises :class:`AliasAlreadyExists` on a duplicate
        (user_id, name) and :class:`AliasLimitReached` at the cap.
        """
        now = _now()
        alias_id = uuid.uuid4().hex[:16]
        token_tuple = tuple(str(token) for token in tokens)
        stamp = _stamp(user_id, name, command_id, token_tuple, author_actor)
        with self._lock, self._connect() as conn:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM user_command_aliases WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if count >= MAX_ALIASES_PER_USER:
                raise AliasLimitReached(
                    f"User {user_id} already has {count} aliases "
                    f"(cap {MAX_ALIASES_PER_USER}); delete one first"
                )
            try:
                conn.execute(
                    "INSERT INTO user_command_aliases "
                    "(id, user_id, name, command_id, tokens_json, "
                    "author_actor, stamp, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        alias_id,
                        user_id,
                        name,
                        command_id,
                        json.dumps(list(token_tuple)),
                        author_actor,
                        stamp,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AliasAlreadyExists(
                    f"Alias '{name}' already exists for user {user_id}"
                ) from exc
            conn.commit()
        return UserCommandAlias(
            id=alias_id,
            user_id=user_id,
            name=name,
            command_id=command_id,
            tokens=token_tuple,
            author_actor=author_actor,
            created_at=now,
            stamp_valid=True,
        )

    def delete_alias(self, *, user_id: str, name: str) -> bool:
        """Delete one alias; True when a row was removed."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM user_command_aliases WHERE user_id = ? AND name = ?",
                (user_id, name),
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_aliases(self, user_id: str) -> List[UserCommandAlias]:
        """Every alias for one user, stamp-checked (stale rows flagged, not
        hidden: ``/alias list`` is where a user learns to re-create one)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM user_command_aliases WHERE user_id = ? "
                "ORDER BY name",
                (user_id,),
            ).fetchall()
        return [self._row_to_alias(row) for row in rows]

    def resolve_for_dispatch(
        self, user_id: str, name: str
    ) -> Optional[UserCommandAlias]:
        """The dispatch-time read: the alias ONLY if its stamp verifies.

        This is the control's consumption point (module docstring). A stale
        row is skipped with a warning, so an out-of-band edit yields an
        unknown command rather than a rewritten dispatch.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_command_aliases "
                "WHERE user_id = ? AND name = ?",
                (user_id, name),
            ).fetchone()
        if row is None:
            return None
        alias = self._row_to_alias(row)
        if not alias.stamp_valid:
            logger.warning(
                "user alias %r for user %s failed its authoring stamp; "
                "skipping (re-create it via /alias create)",
                name,
                user_id,
            )
            return None
        return alias

    def _row_to_alias(self, row: sqlite3.Row) -> UserCommandAlias:
        try:
            tokens = tuple(str(token) for token in json.loads(row["tokens_json"]))
        except (ValueError, TypeError):
            tokens = ()
        expected = _stamp(
            row["user_id"],
            row["name"],
            row["command_id"],
            tokens,
            row["author_actor"],
        )
        return UserCommandAlias(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            command_id=row["command_id"],
            tokens=tokens,
            author_actor=row["author_actor"],
            created_at=row["created_at"],
            stamp_valid=bool(tokens) and expected == row["stamp"],
        )


_repo: Optional[UserAliasesRepo] = None
_repo_lock = threading.Lock()


def get_user_aliases_repo() -> UserAliasesRepo:
    """Get or create the global :class:`UserAliasesRepo` on ``accounts.db``.

    Lazy-initialized so importing this module never touches disk.
    """
    global _repo
    if _repo is None:
        with _repo_lock:
            if _repo is None:
                from ..config import get_settings

                settings = get_settings()
                _repo = UserAliasesRepo(settings.data_dir / "accounts.db")
    return _repo


def reset_user_aliases_repo_for_tests() -> None:
    """Test helper: drop the cached singleton so the next call re-reads
    ``settings.data_dir`` (tests typically point that at a tmp path)."""
    global _repo
    with _repo_lock:
        _repo = None
