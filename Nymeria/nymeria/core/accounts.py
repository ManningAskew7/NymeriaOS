"""
Account, token, thread-ownership, and platform-identity storage.

Dedicated SQLite database at ``data/accounts.db`` (local + Docker). We keep
accounts in their own file rather than co-locating with the LangGraph
checkpoints DB so the code stays single-backend (SQLite only) regardless of
whether checkpoints are SQLite or Postgres. Account volume is tiny and the
data is latency-insensitive, so the dedicated file costs nothing.

Chat-app bindings, short-lived bind codes, and user-owned Telegram bots live
in :mod:`~nymeria.core.chat_bindings` (same DB file, separate repo class).

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
from typing import List, Literal, NamedTuple, Optional

from .storage_paths import canonical_segment_error, segment_collisions

logger = logging.getLogger(__name__)


TOKEN_PREFIX = "nym_"
TOKEN_BYTES = 32  # entropy of the random part (base64url)
TOKEN_PATTERN = re.compile(r"nym_[A-Za-z0-9_-]{32,}")
DEFAULT_TOKEN_TTL_DAYS = 90
DEFAULT_BOOTSTRAP_TOKEN_TTL_HOURS = 24
DEFAULT_MAX_ACTIVE_TOKENS_PER_USER = 10
# The server browser's baked token is a MACHINE credential: nothing renews it,
# and the browser it authenticates has no way to ask a human for a new one, so
# the ordinary 90-day lifetime would silently 401 every install a quarter after
# setup. It gets its own long lifetime instead, still bounded and still
# revocable (`nymeria browser configure` revokes and re-mints on every re-run).
# Keep this label in step with `server_browser.TOKEN_LABEL`; a test pins them.
SERVER_BROWSER_TOKEN_LABEL = "server-browser"
DEFAULT_SERVER_BROWSER_TOKEN_TTL_DAYS = 3650

UserRole = Literal["user", "admin"]
Provider = Literal[
    "discord",
    "telegram",
    "twitch",
    "slack",
    "whatsapp",
    "teams",
]


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


# How many leading hash characters address a token. Lives here, beside the
# record it describes, so the API schema and the command listing cannot drift
# apart on it; revoke_token independently refuses anything under four chars.
TOKEN_HASH_PREFIX_LEN = 8


@dataclass
class TokenRecord:
    token_hash: str
    user_id: str
    label: Optional[str]
    created_at: str
    expires_at: str
    last_used_at: Optional[str]
    revoked_at: Optional[str]

    @property
    def hash_prefix(self) -> str:
        """The addressable short form: what ``revoke_token`` takes.

        Raw tokens are not recoverable, so every surface addresses a token by
        the first characters of its hash, and this is the ONE derivation of
        that value: the API schema and the command listing both read it here.
        They did not before, and the listing asked the dataclass for a
        ``token_hash_prefix`` field it has never had, so the getattr default
        won and the column that feeds ``/account tokens revoke`` was blank for
        every token.
        """
        return self.token_hash[:TOKEN_HASH_PREFIX_LEN]


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


def _parse_timestamp(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    expires_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_tokens_user ON user_tokens(user_id);
-- idx_user_tokens_expires is created in _migrate_token_expiry after the
-- column exists; legacy DBs predate expires_at and would crash here.

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


class InvalidIdentityId(ValueError):
    """A NEW user or thread id is not its own storage segment.

    Raised before any row is written; ``str(exc)`` is the user-facing copy
    (it names the segment the id would fold onto and how to fix it).
    """


def _require_canonical_id(value: str, *, label: str) -> None:
    error = canonical_segment_error(value, label=label)
    if error is not None:
        raise InvalidIdentityId(error)


def _require_no_case_variant(
    conn: sqlite3.Connection, table: str, column: str, value: str, *, label: str
) -> None:
    """Refuse a NEW id that differs from an existing row only by case.

    Windows and macOS (shipped targets) have case-insensitive filesystems, so
    ``Alice`` and ``alice`` are two rows over one store file there. SQLite's
    ``lower()`` folds ASCII only, which covers the ids the platform mints and
    the realistic operator typo; a non-ASCII case variant is not caught.
    """
    row = conn.execute(
        f"SELECT {column} FROM {table} WHERE lower({column}) = lower(?) AND {column} != ?",
        (value, value),
    ).fetchone()
    if row is not None:
        raise InvalidIdentityId(
            f"{label} {value!r} differs only by case from the existing id "
            f"{row[0]!r}; the two would share one store on a case-insensitive "
            "filesystem. Pick a different id."
        )


class LastAdminError(ValueError):
    """Raised when an operation would leave zero enabled admins."""


class UserHasResources(ValueError):
    """Raised when delete_user_cascade is called on a user that still owns
    threads. Caller should empty (or transfer) the user first."""


class TokenNotFound(LookupError):
    pass


class AmbiguousTokenPrefix(ValueError):
    pass


class TokenLimitExceeded(ValueError):
    pass


class AccountsRepo:
    """Thread-safe SQLite-backed repository for users/tokens/ownership."""

    def __init__(
        self,
        db_path: Path,
        *,
        token_ttl_days: int = DEFAULT_TOKEN_TTL_DAYS,
        max_active_tokens_per_user: int = DEFAULT_MAX_ACTIVE_TOKENS_PER_USER,
        bootstrap_token_ttl_hours: int = DEFAULT_BOOTSTRAP_TOKEN_TTL_HOURS,
        server_browser_token_ttl_days: int = DEFAULT_SERVER_BROWSER_TOKEN_TTL_DAYS,
    ):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self.token_ttl_days = max(1, int(token_ttl_days))
        self.max_active_tokens_per_user = max(1, int(max_active_tokens_per_user))
        self.bootstrap_token_ttl_hours = max(1, int(bootstrap_token_ttl_hours))
        self.server_browser_token_ttl_days = max(1, int(server_browser_token_ttl_days))
        self.bootstrap_token_path = self.db_path.parent / BOOTSTRAP_TOKEN_FILENAME
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        logger.info("AccountsRepo initialized at %s", self.db_path)

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate_token_expiry(conn)
            conn.commit()

    def _migrate_token_expiry(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(user_tokens)").fetchall()
        }
        if "expires_at" not in columns:
            conn.execute("ALTER TABLE user_tokens ADD COLUMN expires_at TEXT")
        rows = conn.execute(
            "SELECT token_hash, label, created_at FROM user_tokens "
            "WHERE expires_at IS NULL OR expires_at = ''"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE user_tokens SET expires_at = ? WHERE token_hash = ?",
                (
                    self._expiry_for_label(row["label"], created_at=row["created_at"]),
                    row["token_hash"],
                ),
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_tokens_expires ON user_tokens(expires_at)"
        )

    # -- users -------------------------------------------------------------

    def create_user(
        self,
        user_id: str,
        email: str,
        display_name: str,
        role: UserRole = "user",
    ) -> UserRecord:
        # Identity ids are canonical storage segments (see InvalidIdentityId);
        # every per-user store keys its file on safe_path_segment(user_id).
        _require_canonical_id(user_id, label="User id")
        now = _now()
        with self._lock, self._connect() as conn:
            _require_no_case_variant(conn, "users", "id", user_id, label="User id")
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
        """Create a new token for ``user_id``. Returns the raw token once.

        The user-existence check, active-token count, and insert run inside one
        ``self._lock`` hold so a concurrent ``delete_user_cascade`` on the same
        ``AccountsRepo`` instance (which takes the same lock) cannot delete the
        user between the check and the insert. The existence query is inlined
        rather than calling the lock-acquiring ``get_user_by_id`` because
        ``self._lock`` is non-reentrant.

        ``self._lock`` only serializes a single instance, so a cross-process
        delete (the ``nymeria users`` CLI has direct repo access alongside the
        API) can still drop the user between the check and the insert and trip
        the ``ON DELETE CASCADE`` FK, enforced immediately under
        ``PRAGMA foreign_keys = ON``. The insert converts that ``IntegrityError``
        into a clean ``UserNotFound`` when the user has vanished, so callers
        never see a raw integrity error from this race.
        """
        with self._lock, self._connect() as conn:
            if (
                conn.execute(
                    "SELECT 1 FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                is None
            ):
                raise UserNotFound(user_id)
            self._revoke_expired_tokens_locked(conn, user_id=user_id)
            active = conn.execute(
                "SELECT COUNT(*) AS n FROM user_tokens "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (user_id,),
            ).fetchone()
            if int(active["n"]) >= self.max_active_tokens_per_user:
                raise TokenLimitExceeded(
                    f"User {user_id} already has the maximum "
                    f"{self.max_active_tokens_per_user} active token(s)"
                )
            raw = generate_raw_token()
            created_at = _now()
            try:
                conn.execute(
                    "INSERT INTO user_tokens "
                    "(token_hash, user_id, label, created_at, expires_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        _hash_token(raw),
                        user_id,
                        label,
                        created_at,
                        self._expiry_for_label(label, created_at=created_at),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                # A cross-process delete can drop the user between the check
                # above and this insert (self._lock only serializes one
                # instance), tripping the user_id FK. Surface the clean
                # UserNotFound when the user is gone; re-raise anything else.
                if (
                    conn.execute(
                        "SELECT 1 FROM users WHERE id = ?", (user_id,)
                    ).fetchone()
                    is None
                ):
                    raise UserNotFound(user_id) from None
                raise
        return raw

    @classmethod
    def from_settings(cls, settings: object, db_path: Path) -> "AccountsRepo":
        """Construct honoring the three ``account_*`` policy settings.

        The single settings-driven construction path (backlog #107 review):
        before it existed, three call sites (agent runtime, users CLI, wizard
        finalize) each hand-passed the kwargs, and the ones that forgot minted
        tokens with silent wrong defaults. Tolerates ``None`` or partial
        settings objects by falling back to the constructor defaults, so
        callers that must never crash on a broken config (e.g. ``nymeria
        init``) can pass whatever they managed to load.
        """
        return cls(
            db_path,
            token_ttl_days=getattr(
                settings, "account_token_ttl_days", DEFAULT_TOKEN_TTL_DAYS
            ),
            max_active_tokens_per_user=getattr(
                settings,
                "account_max_active_tokens_per_user",
                DEFAULT_MAX_ACTIVE_TOKENS_PER_USER,
            ),
            bootstrap_token_ttl_hours=getattr(
                settings,
                "account_bootstrap_token_ttl_hours",
                DEFAULT_BOOTSTRAP_TOKEN_TTL_HOURS,
            ),
            server_browser_token_ttl_days=getattr(
                settings,
                "account_server_browser_token_ttl_days",
                DEFAULT_SERVER_BROWSER_TOKEN_TTL_DAYS,
            ),
        )

    def _expiry_for_label(self, label: Optional[str], *, created_at: str) -> str:
        """Lifetime by label: two labels are machine tokens with their own rule.

        The bootstrap token is deliberately short (a human pastes it once). The
        server browser's is deliberately long (nothing renews it and no human is
        in the loop). Everything else takes the account default. This runs on the
        migration path too (`:334`), so an existing token's expiry is recomputed
        from its label, which is how a rig baked before this rule gets the long
        lifetime without a re-mint.
        """
        base = _parse_timestamp(created_at) or datetime.now(timezone.utc)
        if label == BOOTSTRAP_TOKEN_LABEL:
            expires_at = base + timedelta(hours=self.bootstrap_token_ttl_hours)
        elif label == SERVER_BROWSER_TOKEN_LABEL:
            expires_at = base + timedelta(days=self.server_browser_token_ttl_days)
        else:
            expires_at = base + timedelta(days=self.token_ttl_days)
        return expires_at.isoformat(timespec="seconds")

    def _token_expired(self, expires_at: str | None) -> bool:
        parsed = _parse_timestamp(expires_at)
        if parsed is None:
            return True
        return parsed <= datetime.now(timezone.utc)

    def _revoke_expired_tokens_locked(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: Optional[str] = None,
    ) -> int:
        now = _now()
        if user_id is None:
            rows = conn.execute(
                "SELECT token_hash, expires_at FROM user_tokens "
                "WHERE revoked_at IS NULL"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT token_hash, expires_at FROM user_tokens "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (user_id,),
            ).fetchall()

        expired_hashes = [
            row["token_hash"] for row in rows if self._token_expired(row["expires_at"])
        ]
        for token_hash in expired_hashes:
            conn.execute(
                "UPDATE user_tokens SET revoked_at = ? WHERE token_hash = ? "
                "AND revoked_at IS NULL",
                (now, token_hash),
            )
        return len(expired_hashes)

    def _delete_bootstrap_token_file(self) -> None:
        try:
            self.bootstrap_token_path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "Failed to delete bootstrap token file %s after successful auth: %s",
                self.bootstrap_token_path,
                exc,
            )

    def verify_token(self, raw: str) -> Optional[AuthenticatedUser]:
        """Resolve a raw bearer token → ``AuthenticatedUser``, or None."""
        if not raw or not raw.startswith(TOKEN_PREFIX):
            return None
        token_hash = _hash_token(raw)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT u.id, u.email, u.display_name, u.role, u.disabled, "
                "t.token_hash, t.label, t.expires_at, t.revoked_at "
                "FROM user_tokens t JOIN users u ON u.id = t.user_id "
                "WHERE t.token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if row["revoked_at"] is not None or int(row["disabled"]) == 1:
                return None
            if self._token_expired(row["expires_at"]):
                conn.execute(
                    "UPDATE user_tokens SET revoked_at = ? WHERE token_hash = ?",
                    (_now(), token_hash),
                )
                conn.commit()
                return None
            conn.execute(
                "UPDATE user_tokens SET last_used_at = ? WHERE token_hash = ?",
                (_now(), token_hash),
            )
            conn.commit()
            if row["id"] == BOOTSTRAP_USER_ID and row["label"] == BOOTSTRAP_TOKEN_LABEL:
                self._delete_bootstrap_token_file()
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

    def list_unrevoked_tokens_expiring_before(self, cutoff: datetime) -> List[TokenRecord]:
        """Cross-user, READ-ONLY probe for tokens expiring before ``cutoff``.

        Backs the service-token expiry-warning sweep (backlog #107). Unlike
        ``list_tokens_for_user`` this deliberately performs no lazy
        revocation: a warning check must not write. Rows whose ``expires_at``
        fails to parse count as expiring (mirroring ``_token_expired``), and
        already-expired-but-unrevoked rows are included so a missed warning
        still surfaces after the fact.
        """
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM user_tokens WHERE revoked_at IS NULL"
            ).fetchall()
        records = []
        for r in rows:
            expires = _parse_timestamp(r["expires_at"])
            if expires is not None and expires > cutoff:
                continue
            records.append(
                TokenRecord(
                    token_hash=r["token_hash"],
                    user_id=r["user_id"],
                    label=r["label"],
                    created_at=r["created_at"],
                    expires_at=r["expires_at"],
                    last_used_at=r["last_used_at"],
                    revoked_at=r["revoked_at"],
                )
            )
        records.sort(key=lambda t: t.expires_at or "")
        return records

    def list_tokens_for_user(self, user_id: str) -> List[TokenRecord]:
        with self._lock, self._connect() as conn:
            self._revoke_expired_tokens_locked(conn, user_id=user_id)
            rows = conn.execute(
                "SELECT * FROM user_tokens WHERE user_id = ? ORDER BY created_at ASC",
                (user_id,),
            ).fetchall()
            conn.commit()
            return [
                TokenRecord(
                    token_hash=r["token_hash"],
                    user_id=r["user_id"],
                    label=r["label"],
                    created_at=r["created_at"],
                    expires_at=r["expires_at"],
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

        The first touch is where a client-chosen thread id enters the thread
        index, so it is also where a non-canonical id is refused
        (:class:`InvalidIdentityId`, before any write). An id that already has
        a row is never re-checked: pre-existing threads keep working whatever
        their id looks like, and ``backfill_threads`` (the boot path that
        registers them) stays ungated on purpose.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT user_id FROM thread_owners WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
            if row is None:
                _require_canonical_id(thread_id, label="Thread id")
                _require_no_case_variant(
                    conn, "thread_owners", "thread_id", thread_id, label="Thread id"
                )
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

    def list_thread_ids(self) -> List[str]:
        """Every thread id in the ownership index, all users."""
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT thread_id FROM thread_owners").fetchall()
            return [r["thread_id"] for r in rows]

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
                pass  # chmod may fail on some filesystems
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


class IdentityCollision(NamedTuple):
    kind: str  # "user" or "thread"
    segment: str
    ids: List[str]


def find_identity_collisions(repo: AccountsRepo) -> List[IdentityCollision]:
    """Ids in the account table and the thread index that share a store file.

    Pre-existing ids were never validated (creation refuses non-canonical ids
    only since the rule shipped), so a deployment can hold ``alice.smith``
    beside ``alicesmith``: two identities, one ``alicesmith.json`` in every
    per-user store. This is the read-only source for the startup audit that
    reports them; it never renames or deletes anything. Users and threads are
    grouped separately because their stores live in different directories.
    """
    collisions = [
        IdentityCollision("user", segment, ids)
        for segment, ids in segment_collisions(u.id for u in repo.list_users())
    ]
    collisions.extend(
        IdentityCollision("thread", segment, ids)
        for segment, ids in segment_collisions(repo.list_thread_ids())
    )
    return collisions


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


# ---------------------------------------------------------------------------
# Backward-compatible re-exports from chat_bindings (same DB, separate repo)
# ---------------------------------------------------------------------------
from .chat_bindings import (  # noqa: E402, F401
    BindCodeInvalid,
    BindCodeKind,
    BindingAlreadyExists,
    BotAlreadyRegistered,
    ThreadPlatformBinding,
    BindCodeClaim,
    UserTelegramBot,
    ChatBindingsRepo,
    generate_bind_code,
)
