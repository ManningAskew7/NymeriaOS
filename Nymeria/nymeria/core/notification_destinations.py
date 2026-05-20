"""User-configured notification destinations and profiles.

A *destination* is a concrete delivery target: a named target with a channel
type (``telegram``, ``discord``, ``slack``, ``teams``, ``webhook``, ``fcm``,
``email_outlook``) and type-specific config. Non-secret config lives as JSON
on the row; secret config (webhook bearer tokens, custom headers, etc.) is
Fernet-encrypted via :mod:`nymeria.core.secrets`.

A *profile* is a named bundle of destinations to fire together. The ``notify``
tool resolves a profile name to its destination list and dispatches in
parallel. Users pick a default profile per-account; each thread can override.

Shares the ``data/accounts.db`` SQLite database with
:class:`~nymeria.core.accounts.AccountsRepo`. Schema ownership is split:
each repo creates its own tables via ``CREATE TABLE IF NOT EXISTS`` so init
order is safe.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import secrets as nymeria_secrets

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class NotificationDestination:
    """A user-configured concrete delivery target.

    ``config`` carries non-secret type-specific keys (chat_id, webhook URL,
    email recipient). Secret keys (bearer tokens, custom auth headers) live
    in ``secret_fields`` and are Fernet-encrypted at rest. Callers needing
    the plaintext request it through
    :meth:`NotificationDestinationsRepo.get_secret_field`.

    ``user_id`` may be ``"default"`` for the implicit single-user account; the
    repo enforces ``UNIQUE(user_id, name)`` so two users can each have a
    destination called ``"my-phone"`` without conflict.
    """

    id: str
    user_id: str
    name: str
    type: str
    config: Dict[str, Any]
    enabled: bool
    created_at: str
    updated_at: str


@dataclass
class NotificationProfile:
    """A named bundle of destinations to fire together.

    ``destination_names`` is the user-facing reference; the dispatcher resolves
    each name to a :class:`NotificationDestination` at send time so renaming a
    destination updates every profile that references it.
    """

    id: str
    user_id: str
    name: str
    destination_names: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DestinationNotFound(LookupError):
    """No destination with the given id or name for this user."""


class DestinationAlreadyExists(ValueError):
    """A destination with this name already exists for this user."""


class ProfileNotFound(LookupError):
    """No profile with the given id or name for this user."""


class ProfileAlreadyExists(ValueError):
    """A profile with this name already exists for this user."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


NOTIFICATION_DESTINATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS notification_destinations (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL,
    config_json  TEXT NOT NULL DEFAULT '{}',
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    UNIQUE(user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_notification_destinations_user
    ON notification_destinations(user_id);

CREATE TABLE IF NOT EXISTS notification_destination_secrets (
    destination_id TEXT NOT NULL REFERENCES notification_destinations(id) ON DELETE CASCADE,
    field_name     TEXT NOT NULL,
    ciphertext     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (destination_id, field_name)
);

CREATE TABLE IF NOT EXISTS notification_profiles (
    id                      TEXT PRIMARY KEY,
    user_id                 TEXT NOT NULL,
    name                    TEXT NOT NULL,
    destination_names_json  TEXT NOT NULL DEFAULT '[]',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    UNIQUE(user_id, name)
);
CREATE INDEX IF NOT EXISTS idx_notification_profiles_user
    ON notification_profiles(user_id);
"""


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class NotificationDestinationsRepo:
    """Thread-safe SQLite-backed repo for notification destinations + profiles.

    Shares ``accounts.db`` with :class:`~nymeria.core.accounts.AccountsRepo`,
    :class:`~nymeria.core.chat_bindings.ChatBindingsRepo`, and
    :class:`~nymeria.core.credential_vault.CredentialVaultRepo`.

    The repo does NOT validate destination ``config`` against any per-type
    schema — :mod:`nymeria.core.notification_channels` owns that, and the API
    layer validates inbound requests before they reach the repo.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        logger.info("NotificationDestinationsRepo initialized at %s", self.db_path)

    # -- connection --------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(NOTIFICATION_DESTINATIONS_SCHEMA)
            conn.commit()

    # -- destinations ------------------------------------------------------

    def create_destination(
        self,
        *,
        user_id: str,
        name: str,
        type: str,
        config: Optional[Dict[str, Any]] = None,
        secret_fields: Optional[Dict[str, str]] = None,
        enabled: bool = True,
    ) -> NotificationDestination:
        """Create a destination. Raises :class:`DestinationAlreadyExists` on
        duplicate (user_id, name).
        """
        now = _now()
        dest_id = _new_id()
        config_json = json.dumps(config or {}, sort_keys=True)
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO notification_destinations "
                    "(id, user_id, name, type, config_json, enabled, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (dest_id, user_id, name, type, config_json, 1 if enabled else 0, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise DestinationAlreadyExists(
                    f"Destination '{name}' already exists for user {user_id}"
                ) from exc
            if secret_fields:
                for field_name, plaintext in secret_fields.items():
                    conn.execute(
                        "INSERT INTO notification_destination_secrets "
                        "(destination_id, field_name, ciphertext, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (dest_id, field_name, nymeria_secrets.encrypt(plaintext), now),
                    )
            conn.commit()
        return NotificationDestination(
            id=dest_id,
            user_id=user_id,
            name=name,
            type=type,
            config=config or {},
            enabled=enabled,
            created_at=now,
            updated_at=now,
        )

    def update_destination(
        self,
        *,
        user_id: str,
        dest_id: str,
        name: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        secret_fields: Optional[Dict[str, Optional[str]]] = None,
        enabled: Optional[bool] = None,
    ) -> NotificationDestination:
        """Partial update. ``secret_fields`` values of ``None`` delete that
        field; otherwise the field is re-encrypted with the new plaintext.
        """
        now = _now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_destinations WHERE id = ? AND user_id = ?",
                (dest_id, user_id),
            ).fetchone()
            if row is None:
                raise DestinationNotFound(dest_id)

            new_name = name if name is not None else row["name"]
            new_config = config if config is not None else json.loads(row["config_json"])
            new_enabled = enabled if enabled is not None else bool(row["enabled"])

            if name is not None and name != row["name"]:
                # Rename: cascade to every profile that references the old name.
                self._rename_destination_in_profiles_locked(
                    conn, user_id, row["name"], name,
                )

            try:
                conn.execute(
                    "UPDATE notification_destinations SET "
                    "name = ?, config_json = ?, enabled = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        new_name,
                        json.dumps(new_config, sort_keys=True),
                        1 if new_enabled else 0,
                        now,
                        dest_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DestinationAlreadyExists(
                    f"Destination '{new_name}' already exists for user {user_id}"
                ) from exc

            if secret_fields:
                for field_name, plaintext in secret_fields.items():
                    if plaintext is None:
                        conn.execute(
                            "DELETE FROM notification_destination_secrets "
                            "WHERE destination_id = ? AND field_name = ?",
                            (dest_id, field_name),
                        )
                    else:
                        conn.execute(
                            "INSERT OR REPLACE INTO notification_destination_secrets "
                            "(destination_id, field_name, ciphertext, updated_at) "
                            "VALUES (?, ?, ?, ?)",
                            (dest_id, field_name, nymeria_secrets.encrypt(plaintext), now),
                        )
            conn.commit()

        return NotificationDestination(
            id=dest_id,
            user_id=user_id,
            name=new_name,
            type=row["type"],
            config=new_config,
            enabled=new_enabled,
            created_at=row["created_at"],
            updated_at=now,
        )

    def delete_destination(self, *, user_id: str, dest_id: str) -> bool:
        """Delete a destination and cascade-remove its name from every profile."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT name FROM notification_destinations WHERE id = ? AND user_id = ?",
                (dest_id, user_id),
            ).fetchone()
            if row is None:
                return False
            self._remove_destination_from_profiles_locked(conn, user_id, row["name"])
            conn.execute(
                "DELETE FROM notification_destinations WHERE id = ? AND user_id = ?",
                (dest_id, user_id),
            )
            conn.commit()
            return True

    def get_destination(
        self, *, user_id: str, dest_id: str,
    ) -> Optional[NotificationDestination]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_destinations WHERE id = ? AND user_id = ?",
                (dest_id, user_id),
            ).fetchone()
        return self._row_to_destination(row) if row else None

    def get_destination_by_name(
        self, *, user_id: str, name: str,
    ) -> Optional[NotificationDestination]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_destinations WHERE user_id = ? AND name = ?",
                (user_id, name),
            ).fetchone()
        return self._row_to_destination(row) if row else None

    def list_destinations(self, *, user_id: str) -> List[NotificationDestination]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notification_destinations WHERE user_id = ? ORDER BY name",
                (user_id,),
            ).fetchall()
        return [self._row_to_destination(r) for r in rows]

    def get_secret_field(self, *, dest_id: str, field_name: str) -> Optional[str]:
        """Return the plaintext for a destination's secret field, or ``None``
        if not set. Raises :class:`~nymeria.core.secrets.SecretsKeyMissing` if
        ``NYMERIA_SECRETS_KEY`` is not configured.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT ciphertext FROM notification_destination_secrets "
                "WHERE destination_id = ? AND field_name = ?",
                (dest_id, field_name),
            ).fetchone()
        if row is None:
            return None
        return nymeria_secrets.decrypt(row["ciphertext"])

    def list_secret_field_names(self, *, dest_id: str) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT field_name FROM notification_destination_secrets "
                "WHERE destination_id = ? ORDER BY field_name",
                (dest_id,),
            ).fetchall()
        return [r["field_name"] for r in rows]

    # -- profiles ----------------------------------------------------------

    def create_profile(
        self,
        *,
        user_id: str,
        name: str,
        destination_names: Optional[List[str]] = None,
    ) -> NotificationProfile:
        now = _now()
        profile_id = _new_id()
        dests = list(destination_names or [])
        with self._lock, self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO notification_profiles "
                    "(id, user_id, name, destination_names_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (profile_id, user_id, name, json.dumps(dests), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ProfileAlreadyExists(
                    f"Profile '{name}' already exists for user {user_id}"
                ) from exc
            conn.commit()
        return NotificationProfile(
            id=profile_id,
            user_id=user_id,
            name=name,
            destination_names=dests,
            created_at=now,
            updated_at=now,
        )

    def update_profile(
        self,
        *,
        user_id: str,
        profile_id: str,
        name: Optional[str] = None,
        destination_names: Optional[List[str]] = None,
    ) -> NotificationProfile:
        now = _now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_profiles WHERE id = ? AND user_id = ?",
                (profile_id, user_id),
            ).fetchone()
            if row is None:
                raise ProfileNotFound(profile_id)
            new_name = name if name is not None else row["name"]
            new_dests = (
                list(destination_names)
                if destination_names is not None
                else json.loads(row["destination_names_json"])
            )
            try:
                conn.execute(
                    "UPDATE notification_profiles SET "
                    "name = ?, destination_names_json = ?, updated_at = ? "
                    "WHERE id = ?",
                    (new_name, json.dumps(new_dests), now, profile_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ProfileAlreadyExists(
                    f"Profile '{new_name}' already exists for user {user_id}"
                ) from exc
            conn.commit()
        return NotificationProfile(
            id=profile_id,
            user_id=user_id,
            name=new_name,
            destination_names=new_dests,
            created_at=row["created_at"],
            updated_at=now,
        )

    def delete_profile(self, *, user_id: str, profile_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM notification_profiles WHERE id = ? AND user_id = ?",
                (profile_id, user_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_profile(
        self, *, user_id: str, profile_id: str,
    ) -> Optional[NotificationProfile]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_profiles WHERE id = ? AND user_id = ?",
                (profile_id, user_id),
            ).fetchone()
        return self._row_to_profile(row) if row else None

    def get_profile_by_name(
        self, *, user_id: str, name: str,
    ) -> Optional[NotificationProfile]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_profiles WHERE user_id = ? AND name = ?",
                (user_id, name),
            ).fetchone()
        return self._row_to_profile(row) if row else None

    def list_profiles(self, *, user_id: str) -> List[NotificationProfile]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notification_profiles WHERE user_id = ? ORDER BY name",
                (user_id,),
            ).fetchall()
        return [self._row_to_profile(r) for r in rows]

    # -- internal helpers --------------------------------------------------

    def _rename_destination_in_profiles_locked(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        old_name: str,
        new_name: str,
    ) -> None:
        now = _now()
        rows = conn.execute(
            "SELECT id, destination_names_json FROM notification_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        for row in rows:
            names = json.loads(row["destination_names_json"])
            if old_name in names:
                updated = [new_name if n == old_name else n for n in names]
                conn.execute(
                    "UPDATE notification_profiles SET "
                    "destination_names_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(updated), now, row["id"]),
                )

    def _remove_destination_from_profiles_locked(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        name: str,
    ) -> None:
        now = _now()
        rows = conn.execute(
            "SELECT id, destination_names_json FROM notification_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        for row in rows:
            names = json.loads(row["destination_names_json"])
            if name in names:
                updated = [n for n in names if n != name]
                conn.execute(
                    "UPDATE notification_profiles SET "
                    "destination_names_json = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(updated), now, row["id"]),
                )

    @staticmethod
    def _row_to_destination(row: sqlite3.Row) -> NotificationDestination:
        return NotificationDestination(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            type=row["type"],
            config=json.loads(row["config_json"]),
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _row_to_profile(row: sqlite3.Row) -> NotificationProfile:
        return NotificationProfile(
            id=row["id"],
            user_id=row["user_id"],
            name=row["name"],
            destination_names=json.loads(row["destination_names_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


_repo: Optional[NotificationDestinationsRepo] = None
_repo_lock = threading.Lock()


def get_destinations_repo() -> NotificationDestinationsRepo:
    """Get or create the global :class:`NotificationDestinationsRepo`.

    Uses the same ``accounts.db`` path the rest of the auth/binding stack uses.
    Lazy-initialized so importing this module never touches disk.
    """
    global _repo
    if _repo is None:
        with _repo_lock:
            if _repo is None:
                from ..config import get_settings

                settings = get_settings()
                _repo = NotificationDestinationsRepo(settings.data_dir / "accounts.db")
    return _repo


def reset_destinations_repo_for_tests() -> None:
    """Test helper: drop the cached singleton so the next call re-reads
    ``settings.data_dir`` (tests typically point that at a tmp path)."""
    global _repo
    with _repo_lock:
        _repo = None
