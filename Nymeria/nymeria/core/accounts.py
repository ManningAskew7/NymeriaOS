"""
Account, token, thread-ownership, and platform-identity storage.

Dedicated SQLite database at ``data/accounts.db`` (local + Docker). We keep
accounts in their own file rather than co-locating with the LangGraph
checkpoints DB so the code stays single-backend (SQLite only) regardless of
whether checkpoints are SQLite or Postgres. Account volume is tiny and the
data is latency-insensitive, so the dedicated file costs nothing.

Tokens: raw form is ``nym_<32-url-safe-bytes>``; only ``sha256(raw)`` is
stored. The raw value is returned once at creation and never recoverable.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional

logger = logging.getLogger(__name__)


TOKEN_PREFIX = "nym_"
TOKEN_BYTES = 32  # entropy of the random part (base64url)
TOKEN_PATTERN = re.compile(r"nym_[A-Za-z0-9_-]{32,}")

UserRole = Literal["user", "admin"]
Provider = Literal["discord", "telegram", "twitch"]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AuthenticatedUser:
    """A user resolved from a bearer token. Returned by the auth dependency."""

    id: str
    email: str
    display_name: str
    role: UserRole


@dataclass
class UserRecord:
    """A row from the users table (may include disabled users)."""

    id: str
    email: str
    display_name: str
    role: UserRole
    disabled: bool
    created_at: str
    updated_at: str


@dataclass
class TokenRecord:
    token_hash: str
    user_id: str
    label: Optional[str]
    created_at: str
    last_used_at: Optional[str]
    revoked_at: Optional[str]


@dataclass
class PlatformIdentity:
    provider: Provider
    provider_user_id: str
    user_id: str
    created_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_raw_token() -> str:
    """Generate a fresh raw token. Only shown once; hash is what gets stored."""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(TOKEN_BYTES)}"


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    disabled      INTEGER NOT NULL DEFAULT 0,
    password_hash TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_tokens (
    token_hash   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label        TEXT,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_tokens_user ON user_tokens(user_id);

CREATE TABLE IF NOT EXISTS thread_owners (
    thread_id  TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_thread_owners_user ON thread_owners(user_id);

CREATE TABLE IF NOT EXISTS platform_identities (
    provider         TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    user_id          TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (provider, provider_user_id)
);
CREATE INDEX IF NOT EXISTS idx_platform_identities_user ON platform_identities(user_id);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


BOOTSTRAP_USER_ID = "default"
BOOTSTRAP_EMAIL = "owner@localhost"
BOOTSTRAP_DISPLAY_NAME = "Owner"
BOOTSTRAP_TOKEN_LABEL = "bootstrap"
BOOTSTRAP_TOKEN_FILENAME = "BOOTSTRAP_TOKEN.txt"


class UserNotFound(LookupError):
    pass


class UserAlreadyExists(ValueError):
    pass


class AccountsRepo:
    """Thread-safe SQLite-backed repository for users/tokens/ownership."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        logger.info("AccountsRepo initialized at %s", self.db_path)

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    # -- users -------------------------------------------------------------

    def create_user(
        self,
        user_id: str,
        email: str,
        display_name: str,
        role: UserRole = "user",
    ) -> UserRecord:
        now = _now()
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO users (id, email, display_name, role, disabled, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (user_id, email, display_name, role, now, now),
                )
                conn.commit()
            except sqlite3.IntegrityError as e:
                raise UserAlreadyExists(str(e)) from e
        return UserRecord(
            id=user_id,
            email=email,
            display_name=display_name,
            role=role,
            disabled=False,
            created_at=now,
            updated_at=now,
        )

    def get_user_by_id(self, user_id: str) -> Optional[UserRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            return _row_to_user(row) if row else None

    def get_user_by_email(self, email: str) -> Optional[UserRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", (email,)
            ).fetchone()
            return _row_to_user(row) if row else None

    def list_users(self) -> List[UserRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY created_at ASC"
            ).fetchall()
            return [_row_to_user(r) for r in rows]

    def count_users(self) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            return int(row["n"])

    def set_disabled(self, user_id: str, disabled: bool) -> None:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE users SET disabled = ?, updated_at = ? WHERE id = ?",
                (1 if disabled else 0, _now(), user_id),
            )
            if cur.rowcount == 0:
                raise UserNotFound(user_id)
            conn.commit()

    # -- tokens ------------------------------------------------------------

    def issue_token(self, user_id: str, label: Optional[str] = None) -> str:
        """Create a new token for ``user_id``. Returns the raw token once."""
        if self.get_user_by_id(user_id) is None:
            raise UserNotFound(user_id)
        raw = generate_raw_token()
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO user_tokens (token_hash, user_id, label, created_at) "
                "VALUES (?, ?, ?, ?)",
                (_hash_token(raw), user_id, label, _now()),
            )
            conn.commit()
        return raw

    def verify_token(self, raw: str) -> Optional[AuthenticatedUser]:
        """Resolve a raw bearer token → ``AuthenticatedUser``, or None."""
        if not raw or not raw.startswith(TOKEN_PREFIX):
            return None
        token_hash = _hash_token(raw)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT u.id, u.email, u.display_name, u.role, u.disabled, t.revoked_at "
                "FROM user_tokens t JOIN users u ON u.id = t.user_id "
                "WHERE t.token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if row["revoked_at"] is not None or int(row["disabled"]) == 1:
                return None
            conn.execute(
                "UPDATE user_tokens SET last_used_at = ? WHERE token_hash = ?",
                (_now(), token_hash),
            )
            conn.commit()
            return AuthenticatedUser(
                id=row["id"],
                email=row["email"],
                display_name=row["display_name"],
                role=row["role"],
            )

    def revoke_all_tokens(self, user_id: str) -> int:
        """Revoke every active token for a user. Returns count revoked."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE user_tokens SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (_now(), user_id),
            )
            conn.commit()
            return cur.rowcount

    def list_tokens_for_user(self, user_id: str) -> List[TokenRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM user_tokens WHERE user_id = ? ORDER BY created_at ASC",
                (user_id,),
            ).fetchall()
            return [
                TokenRecord(
                    token_hash=r["token_hash"],
                    user_id=r["user_id"],
                    label=r["label"],
                    created_at=r["created_at"],
                    last_used_at=r["last_used_at"],
                    revoked_at=r["revoked_at"],
                )
                for r in rows
            ]

    # -- thread ownership --------------------------------------------------

    def get_thread_owner(self, thread_id: str) -> Optional[str]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM thread_owners WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            return row["user_id"] if row else None

    def claim_thread(self, thread_id: str, user_id: str) -> str:
        """
        First-touch claim. Returns the owner (which may be someone else if we
        lost a race). ``INSERT OR IGNORE`` then re-read makes this race-safe.
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO thread_owners (thread_id, user_id, created_at) "
                "VALUES (?, ?, ?)",
                (thread_id, user_id, _now()),
            )
            conn.commit()
            row = conn.execute(
                "SELECT user_id FROM thread_owners WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            return row["user_id"]

    def list_threads_for_user(self, user_id: str) -> List[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT thread_id FROM thread_owners WHERE user_id = ? ORDER BY created_at ASC",
                (user_id,),
            ).fetchall()
            return [r["thread_id"] for r in rows]

    def backfill_threads(self, thread_ids: List[str], user_id: str) -> int:
        """Assign any thread_ids that have no owner yet to ``user_id``."""
        if not thread_ids:
            return 0
        now = _now()
        inserted = 0
        with self._lock, self._connect() as conn:
            for tid in thread_ids:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO thread_owners (thread_id, user_id, created_at) "
                    "VALUES (?, ?, ?)",
                    (tid, user_id, now),
                )
                inserted += cur.rowcount
            conn.commit()
        return inserted

    # -- platform identities ----------------------------------------------

    def link_platform(
        self, provider: Provider, provider_user_id: str, user_id: str
    ) -> None:
        if self.get_user_by_id(user_id) is None:
            raise UserNotFound(user_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO platform_identities "
                "(provider, provider_user_id, user_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (provider, provider_user_id, user_id, _now()),
            )
            conn.commit()

    def unlink_platform(self, provider: Provider, provider_user_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM platform_identities "
                "WHERE provider = ? AND provider_user_id = ?",
                (provider, provider_user_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def resolve_platform(
        self, provider: Provider, provider_user_id: str
    ) -> Optional[str]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM platform_identities "
                "WHERE provider = ? AND provider_user_id = ?",
                (provider, provider_user_id),
            ).fetchone()
            return row["user_id"] if row else None

    def list_platforms_for_user(self, user_id: str) -> List[PlatformIdentity]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM platform_identities WHERE user_id = ? "
                "ORDER BY provider ASC, created_at ASC",
                (user_id,),
            ).fetchall()
            return [
                PlatformIdentity(
                    provider=r["provider"],
                    provider_user_id=r["provider_user_id"],
                    user_id=r["user_id"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    # -- bootstrap ---------------------------------------------------------

    def ensure_bootstrap_admin(self, data_dir: Path) -> Optional[str]:
        """
        If no users exist yet, create the ``default`` admin and mint a token.
        Writes the raw token to ``data/BOOTSTRAP_TOKEN.txt`` (mode 0600) and
        returns it so the caller can also print it. Returns None if users
        already exist (idempotent).
        """
        if self.count_users() > 0:
            return None
        self.create_user(
            user_id=BOOTSTRAP_USER_ID,
            email=BOOTSTRAP_EMAIL,
            display_name=BOOTSTRAP_DISPLAY_NAME,
            role="admin",
        )
        raw = self.issue_token(BOOTSTRAP_USER_ID, label=BOOTSTRAP_TOKEN_LABEL)
        token_path = Path(data_dir) / BOOTSTRAP_TOKEN_FILENAME
        try:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(
                "FIRST-RUN BOOTSTRAP TOKEN\n"
                f"User: {BOOTSTRAP_EMAIL} ({BOOTSTRAP_USER_ID}, admin)\n"
                f"Token: {raw}\n\n"
                "Paste into the Desktop/Mobile Setup Wizard, then delete this file.\n"
            )
            try:
                token_path.chmod(0o600)
            except OSError:
                pass
        except OSError as e:
            logger.error("Failed to write bootstrap token file: %s", e)
        logger.warning(
            "=" * 60
            + "\nBOOTSTRAP: no users in accounts DB. Created admin '%s'.\n"
            + "Token written to %s (mode 0600).\n"
            + "Paste into the Setup Wizard, then delete the file.\n"
            + "=" * 60,
            BOOTSTRAP_USER_ID,
            token_path,
        )
        return raw


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _row_to_user(row: sqlite3.Row) -> UserRecord:
    return UserRecord(
        id=row["id"],
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        disabled=bool(int(row["disabled"])),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
