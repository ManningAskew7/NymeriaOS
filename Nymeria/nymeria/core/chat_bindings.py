"""
Thread <-> chat-app bindings, short-lived bind codes, and user-owned
Telegram bots (BYO bot via @BotFather token paste).

Shares the same ``data/accounts.db`` SQLite database as
:class:`~nymeria.core.accounts.AccountsRepo`.  Schema ownership is split:
``AccountsRepo`` creates user/token/ownership/identity tables;
``ChatBindingsRepo`` creates binding/code/bot tables.  Both use
``CREATE TABLE IF NOT EXISTS``, so initialization order is safe.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Literal, Optional, Tuple

from .accounts import Provider, UserNotFound

logger = logging.getLogger(__name__)


BindCodeKind = Literal["thread_bind", "platform_link"]

_BIND_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
BIND_CODE_LENGTH = 8


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


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
    supervisor uses :meth:`ChatBindingsRepo.list_user_telegram_bots_with_ciphertext`
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


def _hash_code(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_bind_code() -> str:
    """Short alphanumeric code typed by the user into a chat-app bot."""
    return "".join(secrets.choice(_BIND_CODE_ALPHABET) for _ in range(BIND_CODE_LENGTH))


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BindingAlreadyExists(ValueError):
    """A binding for the given (provider, thread_id) or (provider, chat_id) already exists."""


class BindCodeInvalid(LookupError):
    """Bind code is unknown, expired, or already consumed."""


class BotAlreadyRegistered(ValueError):
    """Same bot username already registered (likely a re-registration of the same bot)."""


# ---------------------------------------------------------------------------
# Schema (binding/code/bot tables only)
# ---------------------------------------------------------------------------


CHAT_BINDINGS_SCHEMA = """
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
"""


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class ChatBindingsRepo:
    """Thread-safe SQLite-backed repository for chat-app bindings,
    short-lived bind codes, and user-owned Telegram bots.

    Shares the same ``accounts.db`` file as
    :class:`~nymeria.core.accounts.AccountsRepo`.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        logger.info("ChatBindingsRepo initialized at %s", self.db_path)

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(CHAT_BINDINGS_SCHEMA)
            self._migrate_schema(conn)
            conn.commit()

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        cols = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(thread_platform_bindings)"
            ).fetchall()
        }
        if "user_telegram_bot_id" not in cols:
            conn.execute(
                "ALTER TABLE thread_platform_bindings "
                "ADD COLUMN user_telegram_bot_id INTEGER REFERENCES "
                "user_telegram_bots(id) ON DELETE CASCADE"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_thread_platform_bindings_user_bot "
                "ON thread_platform_bindings(user_telegram_bot_id)"
            )

    # -- internal helpers --------------------------------------------------

    def _require_user(self, conn: sqlite3.Connection, user_id: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            raise UserNotFound(user_id)

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
        now = _now()
        with self._lock, self._connect() as conn:
            self._require_user(conn, user_id)
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
        chat_id = str(platform_chat_id)
        now = _now()
        with self._lock, self._connect() as conn:
            self._require_user(conn, user_id)
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
        raw = generate_bind_code()
        created = datetime.now(timezone.utc)
        expires = created + timedelta(seconds=int(ttl_seconds))
        with self._lock, self._connect() as conn:
            self._require_user(conn, user_id)
            conn.execute(
                "INSERT INTO bind_codes "
                "(code_hash, kind, provider, thread_id, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    _hash_code(raw),
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

    def _validate_bind_code_row(
        self, raw_code: str, *, kind: BindCodeKind, provider: Provider
    ) -> Tuple[str, sqlite3.Row]:
        """Shared validation: hash, SELECT, check state. Returns (code_hash, row)."""
        if not raw_code:
            raise BindCodeInvalid("empty code")
        code_hash = _hash_code(raw_code.strip().upper())
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
                raise BindCodeInvalid("unknown code")
        return code_hash, row

    def inspect_bind_code(
        self, raw_code: str, *, kind: BindCodeKind, provider: Provider
    ) -> BindCodeClaim:
        """Read-only validation of a bind code. Returns the claim info
        without consuming the code, so callers can run authorization checks
        before committing to consumption via :meth:`claim_bind_code`.
        """
        _, row = self._validate_bind_code_row(raw_code, kind=kind, provider=provider)
        return BindCodeClaim(
            kind=row["kind"],
            provider=row["provider"],
            user_id=row["user_id"],
            thread_id=row["thread_id"],
        )

    def claim_bind_code(
        self, raw_code: str, *, kind: BindCodeKind, provider: Provider
    ) -> BindCodeClaim:
        """Atomically consume a bind code. Raises ``BindCodeInvalid`` if the
        code is unknown, expired, already consumed, or has the wrong kind /
        provider for this caller.
        """
        code_hash, row = self._validate_bind_code_row(
            raw_code, kind=kind, provider=provider
        )
        now = _now()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE bind_codes SET consumed_at = ? "
                "WHERE code_hash = ? AND consumed_at IS NULL",
                (now, code_hash),
            )
            conn.commit()
            if cur.rowcount == 0:
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
        now = _now()
        with self._lock, self._connect() as conn:
            self._require_user(conn, owner_user_id)
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


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _row_to_binding(row: sqlite3.Row) -> ThreadPlatformBinding:
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
