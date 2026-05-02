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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)


TOKEN_PREFIX = "nym_"
TOKEN_BYTES = 32  # entropy of the random part (base64url)
TOKEN_PATTERN = re.compile(r"nym_[A-Za-z0-9_-]{32,}")

UserRole = Literal["user", "admin"]
Provider = Literal["discord", "telegram", "twitch"]
BindCodeKind = Literal["thread_bind", "platform_link"]

# Short bind codes use an unambiguous 32-char alphabet (no 0/O, 1/I/L). 8 chars
# at ~5 bits each gives ~40 bits of entropy — enough for the 10-minute TTL.
_BIND_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
BIND_CODE_LENGTH = 8


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AuthenticatedUser:
    """A user resolved from a bearer token. Returned by the auth dependency.

    ``via_act_as`` is True when the caller is an admin who used
    ``X-Nymeria-Act-As`` to impersonate this user. Used by
    ``_require_thread_access`` to allow shared-channel routing through the
    bot service token (admin) while still rejecting non-admin direct API
    callers from guessing shared-channel thread IDs.
    """

    id: str
    email: str
    display_name: str
    role: UserRole
    via_act_as: bool = False


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


@dataclass
class ThreadPlatformBinding:
    """A binding between a Nymeria thread and a chat on a chat-app provider.

    Today only ``provider='telegram'`` is wired end-to-end, but the table is
    provider-agnostic so future providers (Discord, WhatsApp, ...) plug in
    without a schema change.

    ``user_telegram_bot_id`` is None for bindings served by the shared bot
    (``@NymeriaaaaaBot`` etc.), and the row id of a ``user_telegram_bots``
    entry for bindings served by a user-owned bot. The supervisor's outbound
    routing reads this to pick which bot's token to send a reply through.
    """

    id: int
    thread_id: str
    provider: Provider
    platform_chat_id: str
    user_id: str
    created_at: str
    user_telegram_bot_id: Optional[int] = None


@dataclass
class BindCodeClaim:
    """Result of successfully claiming a bind code."""

    kind: BindCodeKind
    provider: Provider
    user_id: str
    thread_id: Optional[str]  # None for platform_link codes


@dataclass
class UserTelegramBot:
    """A user-owned Telegram bot (BYO bot via @BotFather token paste).

    The token itself is **never** put on this dataclass — it lives only in
    ciphertext on ``user_telegram_bots.bot_token_ciphertext``. The
    supervisor uses :meth:`AccountsRepo.list_user_telegram_bots_for_runtime`
    to fetch (metadata, decrypted_token) pairs explicitly.
    """

    id: int
    owner_user_id: str
    bot_username: str
    enabled: bool
    created_at: str
    last_seen_at: Optional[str]


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


def generate_bind_code() -> str:
    """Short alphanumeric code typed by the user into a chat-app bot."""
    return "".join(secrets.choice(_BIND_CODE_ALPHABET) for _ in range(BIND_CODE_LENGTH))


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

CREATE TABLE IF NOT EXISTS thread_platform_bindings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id        TEXT NOT NULL,
    provider         TEXT NOT NULL,
    platform_chat_id TEXT NOT NULL,
    user_id          TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at       TEXT NOT NULL,
    UNIQUE(provider, thread_id),
    UNIQUE(provider, platform_chat_id)
);
CREATE INDEX IF NOT EXISTS idx_thread_platform_bindings_thread ON thread_platform_bindings(thread_id);
CREATE INDEX IF NOT EXISTS idx_thread_platform_bindings_user ON thread_platform_bindings(user_id);

CREATE TABLE IF NOT EXISTS bind_codes (
    code_hash    TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    provider     TEXT NOT NULL,
    thread_id    TEXT,
    user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    consumed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_bind_codes_user ON bind_codes(user_id);
CREATE INDEX IF NOT EXISTS idx_bind_codes_expires ON bind_codes(expires_at);

CREATE TABLE IF NOT EXISTS user_telegram_bots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bot_username         TEXT NOT NULL UNIQUE,
    bot_token_ciphertext TEXT NOT NULL,
    enabled              INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL,
    last_seen_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_telegram_bots_owner ON user_telegram_bots(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_user_telegram_bots_enabled ON user_telegram_bots(enabled);

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


class LastAdminError(ValueError):
    """Raised when an operation would leave zero enabled admins."""


class UserHasResources(ValueError):
    """Raised when delete_user_cascade is called on a user that still owns
    threads. Caller should empty (or transfer) the user first."""


class TokenNotFound(LookupError):
    pass


class AmbiguousTokenPrefix(ValueError):
    pass


class BindingAlreadyExists(ValueError):
    """A binding for the given (provider, thread_id) or (provider, chat_id) already exists."""


class BindCodeInvalid(LookupError):
    """Bind code is unknown, expired, or already consumed."""


class BotAlreadyRegistered(ValueError):
    """Same bot username already registered (likely a re-registration of the same bot)."""


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
            self._migrate_schema(conn)
            conn.commit()

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Idempotent in-place migrations for additions to existing tables.

        ``CREATE TABLE IF NOT EXISTS`` only creates missing tables — it
        doesn't reconcile column lists. For columns we add later we
        ``PRAGMA table_info`` first and ``ALTER TABLE ADD COLUMN`` only
        when missing, so re-running the schema script is safe.
        """
        cols = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(thread_platform_bindings)"
            ).fetchall()
        }
        if "user_telegram_bot_id" not in cols:
            # NULL = served by the shared bot. Non-null = served by the
            # user-owned bot at user_telegram_bots.id (cascade-delete on
            # bot removal so orphaned bindings can't outlive their bot).
            conn.execute(
                "ALTER TABLE thread_platform_bindings "
                "ADD COLUMN user_telegram_bot_id INTEGER REFERENCES "
                "user_telegram_bots(id) ON DELETE CASCADE"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_thread_platform_bindings_user_bot "
                "ON thread_platform_bindings(user_telegram_bot_id)"
            )

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
            if disabled:
                self._guard_last_admin(conn, user_id, future_role="user", future_disabled=True)
            cur = conn.execute(
                "UPDATE users SET disabled = ?, updated_at = ? WHERE id = ?",
                (1 if disabled else 0, _now(), user_id),
            )
            if cur.rowcount == 0:
                raise UserNotFound(user_id)
            conn.commit()

    def update_user(
        self,
        user_id: str,
        *,
        display_name: Optional[str] = None,
        role: Optional[UserRole] = None,
    ) -> UserRecord:
        """Partial update for display_name and/or role. Last-admin guard
        prevents demoting the only enabled admin."""
        if display_name is None and role is None:
            existing = self.get_user_by_id(user_id)
            if existing is None:
                raise UserNotFound(user_id)
            return existing
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if existing is None:
                raise UserNotFound(user_id)
            new_role = role if role is not None else existing["role"]
            new_disabled = bool(int(existing["disabled"]))
            if role is not None and role != existing["role"]:
                self._guard_last_admin(conn, user_id, future_role=new_role, future_disabled=new_disabled)
            sets = []
            params: List[object] = []
            if display_name is not None:
                sets.append("display_name = ?")
                params.append(display_name)
            if role is not None:
                sets.append("role = ?")
                params.append(role)
            sets.append("updated_at = ?")
            params.append(_now())
            params.append(user_id)
            conn.execute(
                f"UPDATE users SET {', '.join(sets)} WHERE id = ?",
                tuple(params),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            return _row_to_user(row)

    def delete_user_cascade(self, user_id: str) -> None:
        """Delete a user and all their tokens / platform identities (FK cascade
        handles the latter). Refuses if the user still owns any threads — the
        admin must transfer or delete those threads first.

        Last-admin guard also applies: cannot delete the only enabled admin.
        """
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if existing is None:
                raise UserNotFound(user_id)
            self._guard_last_admin(
                conn, user_id, future_role="user", future_disabled=True
            )
            owned = conn.execute(
                "SELECT COUNT(*) AS n FROM thread_owners WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if int(owned["n"]) > 0:
                raise UserHasResources(
                    f"User {user_id} still owns {int(owned['n'])} thread(s)"
                )
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            conn.commit()

    def _guard_last_admin(
        self,
        conn: sqlite3.Connection,
        target_user_id: str,
        *,
        future_role: UserRole,
        future_disabled: bool,
    ) -> None:
        """Raise LastAdminError if applying the (future_role, future_disabled)
        change to ``target_user_id`` would leave zero enabled admins overall.

        Called inside an existing transaction. Reads only — no writes."""
        row = conn.execute(
            "SELECT role, disabled FROM users WHERE id = ?", (target_user_id,)
        ).fetchone()
        if row is None:
            return  # caller will raise UserNotFound separately
        current_admin_active = (
            row["role"] == "admin" and int(row["disabled"]) == 0
        )
        future_admin_active = future_role == "admin" and not future_disabled
        if not current_admin_active or future_admin_active:
            return  # no change to admin headcount, or it stays / increases
        # We're about to remove an enabled admin. Make sure another exists.
        other = conn.execute(
            "SELECT COUNT(*) AS n FROM users "
            "WHERE role = 'admin' AND disabled = 0 AND id != ?",
            (target_user_id,),
        ).fetchone()
        if int(other["n"]) == 0:
            raise LastAdminError(
                "Refusing to leave zero enabled admins: at least one admin "
                "must remain enabled."
            )

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

    def revoke_token(self, user_id: str, token_hash_prefix: str) -> bool:
        """Revoke a single token by the first chars of its sha256 hash.

        Raw tokens aren't recoverable, so the API addresses tokens by a
        prefix of their hash (UI shows e.g. ``a3f9b1c2``). Rejects ambiguous
        prefixes (``AmbiguousTokenPrefix``) and unknown ones (``TokenNotFound``).
        Returns True if a token was newly revoked, False if it was already
        revoked.
        """
        prefix = (token_hash_prefix or "").strip().lower()
        if len(prefix) < 4:
            raise AmbiguousTokenPrefix("Token prefix must be at least 4 chars")
        like = prefix + "%"
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT token_hash, revoked_at FROM user_tokens "
                "WHERE user_id = ? AND token_hash LIKE ?",
                (user_id, like),
            ).fetchall()
            if not rows:
                raise TokenNotFound(prefix)
            if len(rows) > 1:
                raise AmbiguousTokenPrefix(
                    f"Prefix '{prefix}' matches {len(rows)} tokens; use more chars"
                )
            row = rows[0]
            if row["revoked_at"] is not None:
                return False
            conn.execute(
                "UPDATE user_tokens SET revoked_at = ? WHERE token_hash = ?",
                (_now(), row["token_hash"]),
            )
            conn.commit()
            return True

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

    def delete_thread_owner(self, thread_id: str) -> bool:
        """Delete the ownership row for a thread."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM thread_owners WHERE thread_id = ?",
                (thread_id,),
            )
            conn.commit()
            return cur.rowcount > 0

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

    # -- thread <-> chat-app bindings -------------------------------------

    def create_thread_binding(
        self,
        *,
        thread_id: str,
        provider: Provider,
        platform_chat_id: str,
        user_id: str,
        user_telegram_bot_id: Optional[int] = None,
    ) -> ThreadPlatformBinding:
        """Bind a Nymeria thread to a chat on a chat-app provider.

        Raises ``BindingAlreadyExists`` if either the thread or the chat is
        already bound for this provider (the table has unique constraints on
        both ``(provider, thread_id)`` and ``(provider, platform_chat_id)``).

        ``user_telegram_bot_id`` records *which* bot saw the chat. ``None``
        means the shared bot (existing behavior); a row id means a
        user-owned bot. The supervisor uses this to route outbound replies
        through the right bot's token.
        """
        if self.get_user_by_id(user_id) is None:
            raise UserNotFound(user_id)
        now = _now()
        with self._lock, self._connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO thread_platform_bindings "
                    "(thread_id, provider, platform_chat_id, user_id, "
                    " user_telegram_bot_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        thread_id,
                        provider,
                        str(platform_chat_id),
                        user_id,
                        user_telegram_bot_id,
                        now,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as e:
                raise BindingAlreadyExists(str(e)) from e
            return ThreadPlatformBinding(
                id=int(cur.lastrowid),
                thread_id=thread_id,
                provider=provider,
                platform_chat_id=str(platform_chat_id),
                user_id=user_id,
                created_at=now,
                user_telegram_bot_id=user_telegram_bot_id,
            )

    def switch_thread_binding_for_chat(
        self,
        *,
        thread_id: str,
        provider: Provider,
        platform_chat_id: str,
        user_id: str,
        user_telegram_bot_id: Optional[int] = None,
    ) -> Tuple[ThreadPlatformBinding, Optional[str]]:
        """Move a chat-app chat binding to ``thread_id`` atomically.

        Returns ``(binding, previous_thread_id)``. ``previous_thread_id`` is
        None when this created a new binding or the chat was already bound to
        the requested thread.
        """
        if self.get_user_by_id(user_id) is None:
            raise UserNotFound(user_id)
        chat_id = str(platform_chat_id)
        now = _now()
        with self._lock, self._connect() as conn:
            current_row = conn.execute(
                "SELECT * FROM thread_platform_bindings "
                "WHERE provider = ? AND platform_chat_id = ?",
                (provider, chat_id),
            ).fetchone()
            target_row = conn.execute(
                "SELECT * FROM thread_platform_bindings "
                "WHERE provider = ? AND thread_id = ?",
                (provider, thread_id),
            ).fetchone()

            current = _row_to_binding(current_row) if current_row else None
            target = _row_to_binding(target_row) if target_row else None

            if current is not None:
                if (
                    current.user_id != user_id
                    or current.user_telegram_bot_id != user_telegram_bot_id
                ):
                    raise BindingAlreadyExists(
                        "Chat is already bound to another Nymeria user or Telegram bot"
                    )

            if target is not None:
                same_chat = (
                    target.platform_chat_id == chat_id
                    and target.user_id == user_id
                    and target.user_telegram_bot_id == user_telegram_bot_id
                )
                if not same_chat:
                    raise BindingAlreadyExists(
                        "Thread is already bound to another Telegram chat"
                    )
                previous = (
                    current.thread_id
                    if current is not None and current.thread_id != target.thread_id
                    else None
                )
                return target, previous

            previous_thread_id = current.thread_id if current is not None else None
            try:
                if current is not None:
                    conn.execute(
                        "DELETE FROM thread_platform_bindings WHERE id = ?",
                        (current.id,),
                    )
                cur = conn.execute(
                    "INSERT INTO thread_platform_bindings "
                    "(thread_id, provider, platform_chat_id, user_id, "
                    " user_telegram_bot_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        thread_id,
                        provider,
                        chat_id,
                        user_id,
                        user_telegram_bot_id,
                        now,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as e:
                conn.rollback()
                raise BindingAlreadyExists(str(e)) from e
            return (
                ThreadPlatformBinding(
                    id=int(cur.lastrowid),
                    thread_id=thread_id,
                    provider=provider,
                    platform_chat_id=chat_id,
                    user_id=user_id,
                    created_at=now,
                    user_telegram_bot_id=user_telegram_bot_id,
                ),
                previous_thread_id,
            )

    def lookup_thread_binding_by_chat(
        self, provider: Provider, platform_chat_id: str
    ) -> Optional[ThreadPlatformBinding]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM thread_platform_bindings "
                "WHERE provider = ? AND platform_chat_id = ?",
                (provider, str(platform_chat_id)),
            ).fetchone()
            return _row_to_binding(row) if row else None

    def lookup_thread_binding_by_thread(
        self, provider: Provider, thread_id: str
    ) -> Optional[ThreadPlatformBinding]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM thread_platform_bindings "
                "WHERE provider = ? AND thread_id = ?",
                (provider, thread_id),
            ).fetchone()
            return _row_to_binding(row) if row else None

    def list_thread_bindings(self, thread_id: str) -> List[ThreadPlatformBinding]:
        """All chat-app bindings for a single thread (across providers)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM thread_platform_bindings WHERE thread_id = ? "
                "ORDER BY provider ASC, created_at ASC",
                (thread_id,),
            ).fetchall()
            return [_row_to_binding(r) for r in rows]

    def list_bound_thread_ids(self, provider: Provider) -> List[str]:
        """Every thread_id that has a binding on this provider.

        Used by the bot's outbound SSE filter to decide which non-default
        thread IDs (i.e. not ``telegram_<chat_id>``) it should also dispatch.
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT thread_id FROM thread_platform_bindings WHERE provider = ?",
                (provider,),
            ).fetchall()
            return [r["thread_id"] for r in rows]

    def list_thread_bindings_global(
        self, provider: Optional[Provider] = None
    ) -> List[ThreadPlatformBinding]:
        """All bindings, optionally filtered by provider. Used by chat-app bots
        on startup to bulk-populate their local ``chat_id <-> thread_id`` cache.
        """
        with self._lock, self._connect() as conn:
            if provider is not None:
                rows = conn.execute(
                    "SELECT * FROM thread_platform_bindings WHERE provider = ? "
                    "ORDER BY created_at ASC",
                    (provider,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM thread_platform_bindings ORDER BY created_at ASC"
                ).fetchall()
            return [_row_to_binding(r) for r in rows]

    def list_thread_bindings_for_user(self, user_id: str) -> List[ThreadPlatformBinding]:
        """All chat-app bindings owned by a user."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM thread_platform_bindings WHERE user_id = ? "
                "ORDER BY provider ASC, created_at ASC",
                (user_id,),
            ).fetchall()
            return [_row_to_binding(r) for r in rows]

    def delete_thread_binding(self, binding_id: int, *, user_id: str) -> bool:
        """Delete a binding the caller owns. Returns True if a row was deleted.

        Returns False if no row matched (either the id doesn't exist or it
        belongs to a different user). The caller is expected to surface a 404
        in either case — we don't distinguish, by design.
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM thread_platform_bindings WHERE id = ? AND user_id = ?",
                (binding_id, user_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def delete_thread_bindings_for_thread(self, thread_id: str) -> int:
        """Delete all chat-app bindings for a thread."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM thread_platform_bindings WHERE thread_id = ?",
                (thread_id,),
            )
            conn.commit()
            return cur.rowcount or 0

    def delete_bind_codes_for_thread(self, thread_id: str) -> int:
        """Delete pending or consumed bind-code rows tied to a thread."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM bind_codes WHERE thread_id = ?",
                (thread_id,),
            )
            conn.commit()
            return cur.rowcount or 0

    def list_bind_code_thread_ids_for_user(self, user_id: str) -> List[str]:
        """Thread IDs referenced by this user's unexpired thread-bind codes."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT thread_id FROM bind_codes "
                "WHERE user_id = ? AND thread_id IS NOT NULL "
                "AND consumed_at IS NULL AND expires_at >= ?",
                (user_id, _now()),
            ).fetchall()
            return [r["thread_id"] for r in rows if r["thread_id"]]

    # -- short-lived bind codes -------------------------------------------

    def issue_bind_code(
        self,
        *,
        kind: BindCodeKind,
        provider: Provider,
        user_id: str,
        thread_id: Optional[str] = None,
        ttl_seconds: int = 600,
    ) -> str:
        """Mint a fresh short bind code and store its hash. Returns the raw code.

        ``thread_id`` is required for ``kind='thread_bind'``; ignored for
        ``kind='platform_link'``. The code is single-use: ``claim_bind_code``
        atomically marks ``consumed_at``.
        """
        if kind == "thread_bind" and not thread_id:
            raise ValueError("thread_id is required for kind='thread_bind'")
        if self.get_user_by_id(user_id) is None:
            raise UserNotFound(user_id)
        raw = generate_bind_code()
        created = datetime.now(timezone.utc)
        expires = created + timedelta(seconds=int(ttl_seconds))
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO bind_codes "
                "(code_hash, kind, provider, thread_id, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _hash_token(raw),
                    kind,
                    provider,
                    thread_id,
                    user_id,
                    created.isoformat(timespec="seconds"),
                    expires.isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
        return raw

    def claim_bind_code(
        self, raw_code: str, *, kind: BindCodeKind, provider: Provider
    ) -> BindCodeClaim:
        """Atomically consume a bind code. Raises ``BindCodeInvalid`` if the
        code is unknown, expired, already consumed, or has the wrong kind /
        provider for this caller.
        """
        if not raw_code:
            raise BindCodeInvalid("empty code")
        code_hash = _hash_token(raw_code.strip().upper())
        now = _now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bind_codes WHERE code_hash = ?",
                (code_hash,),
            ).fetchone()
            if row is None:
                raise BindCodeInvalid("unknown code")
            if row["consumed_at"] is not None:
                raise BindCodeInvalid("already used")
            if row["expires_at"] < now:
                raise BindCodeInvalid("expired")
            if row["kind"] != kind or row["provider"] != provider:
                # Don't tell the caller why — looks the same as "unknown".
                raise BindCodeInvalid("unknown code")
            cur = conn.execute(
                "UPDATE bind_codes SET consumed_at = ? "
                "WHERE code_hash = ? AND consumed_at IS NULL",
                (now, code_hash),
            )
            conn.commit()
            if cur.rowcount == 0:
                # Lost the race to another consumer.
                raise BindCodeInvalid("already used")
            return BindCodeClaim(
                kind=row["kind"],
                provider=row["provider"],
                user_id=row["user_id"],
                thread_id=row["thread_id"],
            )

    def purge_expired_bind_codes(self) -> int:
        """Delete expired & unconsumed codes. Optional housekeeping."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM bind_codes "
                "WHERE consumed_at IS NULL AND expires_at < ?",
                (_now(),),
            )
            conn.commit()
            return cur.rowcount

    # -- user-owned Telegram bots (BYO bot via @BotFather token paste) ----

    def register_user_telegram_bot(
        self,
        *,
        owner_user_id: str,
        bot_username: str,
        bot_token_ciphertext: str,
    ) -> UserTelegramBot:
        """Store a new user-owned Telegram bot. The token must already be
        encrypted by the caller (the API layer owns the cipher; the repo
        deals only in opaque ciphertext strings).

        Raises ``BotAlreadyRegistered`` if a bot with the same username is
        already registered (Telegram bot usernames are globally unique, so
        this also catches "same user pasting the same token twice").
        """
        if self.get_user_by_id(owner_user_id) is None:
            raise UserNotFound(owner_user_id)
        now = _now()
        with self._lock, self._connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO user_telegram_bots "
                    "(owner_user_id, bot_username, bot_token_ciphertext, "
                    " enabled, created_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (owner_user_id, bot_username, bot_token_ciphertext, now),
                )
                conn.commit()
            except sqlite3.IntegrityError as e:
                raise BotAlreadyRegistered(str(e)) from e
            return UserTelegramBot(
                id=int(cur.lastrowid),
                owner_user_id=owner_user_id,
                bot_username=bot_username,
                enabled=True,
                created_at=now,
                last_seen_at=None,
            )

    def list_user_telegram_bots(self, owner_user_id: str) -> List[UserTelegramBot]:
        """Bots owned by a user (no token material in the result)."""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM user_telegram_bots WHERE owner_user_id = ? "
                "ORDER BY created_at ASC",
                (owner_user_id,),
            ).fetchall()
            return [_row_to_user_telegram_bot(r) for r in rows]

    def get_user_telegram_bot(
        self, bot_id: int, *, owner_user_id: Optional[str] = None
    ) -> Optional[UserTelegramBot]:
        """Single-bot fetch. ``owner_user_id`` scopes the lookup to a user
        (returns None if the bot exists but belongs to someone else) — pass
        None for admin / supervisor paths."""
        with self._lock, self._connect() as conn:
            if owner_user_id is None:
                row = conn.execute(
                    "SELECT * FROM user_telegram_bots WHERE id = ?",
                    (bot_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM user_telegram_bots WHERE id = ? AND owner_user_id = ?",
                    (bot_id, owner_user_id),
                ).fetchone()
            return _row_to_user_telegram_bot(row) if row else None

    def get_user_telegram_bot_by_username(
        self, bot_username: str
    ) -> Optional[UserTelegramBot]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_telegram_bots WHERE bot_username = ?",
                (bot_username,),
            ).fetchone()
            return _row_to_user_telegram_bot(row) if row else None

    def list_user_telegram_bots_with_ciphertext(
        self,
    ) -> List[tuple[UserTelegramBot, str]]:
        """All enabled bots with their (still-encrypted) tokens. The admin
        endpoint decrypts before returning to the supervisor process; the
        plaintext never leaves the API boundary in user-facing responses.
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM user_telegram_bots WHERE enabled = 1 "
                "ORDER BY created_at ASC"
            ).fetchall()
            return [
                (_row_to_user_telegram_bot(r), r["bot_token_ciphertext"])
                for r in rows
            ]

    def update_user_telegram_bot_seen(self, bot_id: int) -> None:
        """Heartbeat — supervisor calls this after each successful poll/refresh
        of this bot so the UI can show last-active time."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE user_telegram_bots SET last_seen_at = ? WHERE id = ?",
                (_now(), bot_id),
            )
            conn.commit()

    def delete_user_telegram_bot(
        self, bot_id: int, *, owner_user_id: str
    ) -> bool:
        """Owner-scoped delete. Bindings cascade-delete via the FK on
        ``thread_platform_bindings.user_telegram_bot_id``."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM user_telegram_bots WHERE id = ? AND owner_user_id = ?",
                (bot_id, owner_user_id),
            )
            conn.commit()
            return cur.rowcount > 0

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


def _row_to_binding(row: sqlite3.Row) -> ThreadPlatformBinding:
    # The user_telegram_bot_id column was added in a later migration. Read
    # tolerantly so historical rows (where the column may not have existed
    # at insert time) still deserialize cleanly.
    try:
        ub = row["user_telegram_bot_id"]
    except (KeyError, IndexError):
        ub = None
    return ThreadPlatformBinding(
        id=int(row["id"]),
        thread_id=row["thread_id"],
        provider=row["provider"],
        platform_chat_id=row["platform_chat_id"],
        user_id=row["user_id"],
        created_at=row["created_at"],
        user_telegram_bot_id=int(ub) if ub is not None else None,
    )


def _row_to_user_telegram_bot(row: sqlite3.Row) -> UserTelegramBot:
    return UserTelegramBot(
        id=int(row["id"]),
        owner_user_id=row["owner_user_id"],
        bot_username=row["bot_username"],
        enabled=bool(int(row["enabled"])),
        created_at=row["created_at"],
        last_seen_at=row["last_seen_at"],
    )
