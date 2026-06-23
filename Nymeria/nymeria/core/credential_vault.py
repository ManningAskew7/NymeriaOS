"""Encrypted credential vault for Nymeria-native and MCP tool auth.

The vault intentionally separates secret material from public connection
metadata. Callers can list, bind, test, and audit credentials without ever
receiving plaintext. Runtime executors resolve explicit credential references
only at the point where a tool needs to inject a header, env var, token cache,
or similar secret.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets as py_secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from cryptography.fernet import InvalidToken

from . import secrets as nymeria_secrets

logger = logging.getLogger(__name__)


OwnerType = str
CredentialStatus = str


SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    id                   TEXT PRIMARY KEY,
    owner_type           TEXT NOT NULL,
    owner_user_id        TEXT REFERENCES users(id) ON DELETE CASCADE,
    name                 TEXT NOT NULL,
    provider             TEXT NOT NULL,
    kind                 TEXT NOT NULL,
    account_label        TEXT,
    status               TEXT NOT NULL DEFAULT 'active',
    metadata_json        TEXT NOT NULL DEFAULT '{}',
    scopes_json          TEXT NOT NULL DEFAULT '[]',
    allowed_targets_json TEXT NOT NULL DEFAULT '[]',
    expires_at           TEXT,
    last_used_at         TEXT,
    last_tested_at       TEXT,
    disabled_at          TEXT,
    created_by_user_id   TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credentials_owner ON credentials(owner_type, owner_user_id);
CREATE INDEX IF NOT EXISTS idx_credentials_provider ON credentials(provider, kind);
CREATE INDEX IF NOT EXISTS idx_credentials_status ON credentials(status);

CREATE TABLE IF NOT EXISTS credential_secret_fields (
    credential_id TEXT NOT NULL REFERENCES credentials(id) ON DELETE CASCADE,
    field_name    TEXT NOT NULL,
    ciphertext    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (credential_id, field_name)
);

CREATE TABLE IF NOT EXISTS credential_bindings (
    id                 TEXT PRIMARY KEY,
    credential_id      TEXT NOT NULL REFERENCES credentials(id) ON DELETE CASCADE,
    target_type        TEXT NOT NULL,
    target_id          TEXT NOT NULL,
    binding_name       TEXT,
    created_by_user_id TEXT,
    created_at         TEXT NOT NULL,
    UNIQUE(credential_id, target_type, target_id, binding_name)
);
CREATE INDEX IF NOT EXISTS idx_credential_bindings_target
ON credential_bindings(target_type, target_id);

CREATE TABLE IF NOT EXISTS credential_audit_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id TEXT REFERENCES credentials(id) ON DELETE SET NULL,
    actor_user_id TEXT,
    event_type    TEXT NOT NULL,
    target_type   TEXT,
    target_id     TEXT,
    details_json  TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credential_audit_credential
ON credential_audit_events(credential_id, created_at);
"""


CREDENTIAL_REF_PATTERN = re.compile(
    r"\$\{credential:([A-Za-z][A-Za-z0-9_-]{2,127})\.([A-Za-z][A-Za-z0-9_-]{0,63})\}"
)


class CredentialNotFound(LookupError):
    pass


class CredentialAccessDenied(PermissionError):
    pass


class CredentialSecretUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class CredentialRecord:
    id: str
    owner_type: OwnerType
    owner_user_id: Optional[str]
    name: str
    provider: str
    kind: str
    account_label: Optional[str]
    status: CredentialStatus
    metadata: dict[str, Any]
    scopes: list[str]
    allowed_targets: list[str]
    expires_at: Optional[str]
    last_used_at: Optional[str]
    last_tested_at: Optional[str]
    disabled_at: Optional[str]
    created_by_user_id: Optional[str]
    created_at: str
    updated_at: str
    secret_fields: list[str]

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "owner_type": self.owner_type,
            "owner_user_id": self.owner_user_id,
            "name": self.name,
            "provider": self.provider,
            "kind": self.kind,
            "account_label": self.account_label,
            "status": self.status,
            "metadata": self.metadata,
            "scopes": self.scopes,
            "allowed_targets": self.allowed_targets,
            "expires_at": self.expires_at,
            "last_used_at": self.last_used_at,
            "last_tested_at": self.last_tested_at,
            "disabled_at": self.disabled_at,
            "created_by_user_id": self.created_by_user_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "secret_fields": self.secret_fields,
            "has_secret": bool(self.secret_fields),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _json_loads(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except Exception:
        return fallback


def _safe_segment(value: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in value)
    return safe.strip("._") or "default"


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:28]
    return f"{prefix}_{digest}"


def _new_id() -> str:
    return f"cred_{py_secrets.token_urlsafe(18).replace('-', '_')}"


def _provider_from_cache_filename(cache_filename: str) -> str:
    stem = cache_filename.rsplit(".", 1)[0].lower()
    return {
        "microsoft": "microsoft",
        "google_calendar": "google_calendar",
        "google_docs": "google_docs",
        "google_gmail": "google_gmail",
    }.get(stem, stem)


def _cache_display_name(cache_filename: str) -> str:
    provider = _provider_from_cache_filename(cache_filename)
    return {
        "microsoft": "Microsoft / Outlook",
        "google_calendar": "Google Calendar",
        "google_docs": "Google Docs / Drive / Sheets",
        "google_gmail": "Google Gmail",
    }.get(provider, provider.replace("_", " ").title())


def _summarize_cache(cache: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str], Optional[str]]:
    accounts = cache.get("accounts") if isinstance(cache, dict) else None
    summaries: list[dict[str, Any]] = []
    scopes: set[str] = set()
    expires: list[str] = []

    if isinstance(accounts, dict):
        for account_id, account in accounts.items():
            if not isinstance(account, dict):
                continue
            saved_scopes = account.get("scopes")
            if isinstance(saved_scopes, list):
                scopes.update(str(s) for s in saved_scopes)
            expires_at = account.get("expires_at")
            if expires_at is not None:
                expires.append(str(expires_at))
            summaries.append(
                {
                    "account_id": str(account_id),
                    "email": account.get("email") or account.get("mail"),
                    "name": account.get("name") or account.get("displayName"),
                    "expires_at": expires_at,
                    "scopes": saved_scopes if isinstance(saved_scopes, list) else [],
                    "has_refresh_token": bool(account.get("refresh_token")),
                    "has_access_token": bool(account.get("access_token")),
                }
            )

    if "moodle" in cache and isinstance(cache.get("moodle"), dict):
        moodle = cache["moodle"]
        summaries.append(
            {
                "account_id": "moodle",
                "email": None,
                "name": moodle.get("sitename") or moodle.get("site_name") or "Moodle",
                "expires_at": None,
                "scopes": [],
                "has_refresh_token": False,
                "has_access_token": bool(moodle.get("wstoken")),
            }
        )
    if cache.get("calendar_url"):
        summaries.append(
            {
                "account_id": "calendar",
                "name": "Calendar export",
                "has_access_token": True,
            }
        )
    if cache.get("rss_feeds"):
        summaries.append(
            {
                "account_id": "rss",
                "name": "RSS feeds",
                "count": len(cache.get("rss_feeds") or []),
                "has_access_token": True,
            }
        )

    return summaries, sorted(scopes), min(expires) if expires else None


def _row_to_record(row: sqlite3.Row, secret_fields: Iterable[str]) -> CredentialRecord:
    return CredentialRecord(
        id=row["id"],
        owner_type=row["owner_type"],
        owner_user_id=row["owner_user_id"],
        name=row["name"],
        provider=row["provider"],
        kind=row["kind"],
        account_label=row["account_label"],
        status=row["status"],
        metadata=_json_loads(row["metadata_json"], {}),
        scopes=list(_json_loads(row["scopes_json"], [])),
        allowed_targets=list(_json_loads(row["allowed_targets_json"], [])),
        expires_at=row["expires_at"],
        last_used_at=row["last_used_at"],
        last_tested_at=row["last_tested_at"],
        disabled_at=row["disabled_at"],
        created_by_user_id=row["created_by_user_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        secret_fields=sorted(secret_fields),
    )


class CredentialVaultRepo:
    """Thread-safe SQLite repository for encrypted credentials."""

    # SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds; 500 stays well under it.
    _SECRET_FIELDS_BATCH = 500

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()
        logger.info("CredentialVaultRepo initialized at %s", self.db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _secret_field_names(self, conn: sqlite3.Connection, credential_id: str) -> list[str]:
        rows = conn.execute(
            "SELECT field_name FROM credential_secret_fields WHERE credential_id = ?",
            (credential_id,),
        ).fetchall()
        return [r["field_name"] for r in rows]

    def _secret_field_names_for(
        self, conn: sqlite3.Connection, credential_ids: list[str]
    ) -> dict[str, list[str]]:
        """Group secret field names for many credentials in one query per batch.

        Avoids the N+1 the per-row ``_secret_field_names`` would issue when
        building records for a whole list. Field order within each list is left
        as returned; callers sort via ``_row_to_record``.
        """
        grouped: dict[str, list[str]] = {}
        if not credential_ids:
            return grouped
        for start in range(0, len(credential_ids), self._SECRET_FIELDS_BATCH):
            batch = credential_ids[start : start + self._SECRET_FIELDS_BATCH]
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT credential_id, field_name FROM credential_secret_fields "
                f"WHERE credential_id IN ({placeholders})",
                tuple(batch),
            ).fetchall()
            for row in rows:
                grouped.setdefault(row["credential_id"], []).append(row["field_name"])
        return grouped

    def create_credential(
        self,
        *,
        owner_type: OwnerType,
        owner_user_id: Optional[str],
        name: str,
        provider: str,
        kind: str,
        secret_fields: Optional[dict[str, str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        scopes: Optional[list[str]] = None,
        allowed_targets: Optional[list[str]] = None,
        account_label: Optional[str] = None,
        expires_at: Optional[str] = None,
        status: CredentialStatus = "active",
        created_by_user_id: Optional[str] = None,
        credential_id: Optional[str] = None,
    ) -> CredentialRecord:
        if owner_type not in {"user", "system"}:
            raise ValueError("owner_type must be 'user' or 'system'")
        if owner_type == "user" and not owner_user_id:
            raise ValueError("owner_user_id is required for user-owned credentials")
        cid = credential_id or _new_id()
        now = _now()
        metadata = metadata or {}
        scopes = scopes or []
        allowed_targets = allowed_targets or []
        secret_fields = secret_fields or {}

        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO credentials (
                    id, owner_type, owner_user_id, name, provider, kind,
                    account_label, status, metadata_json, scopes_json,
                    allowed_targets_json, expires_at, created_by_user_id,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cid,
                    owner_type,
                    owner_user_id,
                    name,
                    provider,
                    kind,
                    account_label,
                    status,
                    _json_dumps(metadata),
                    _json_dumps(scopes),
                    _json_dumps(allowed_targets),
                    expires_at,
                    created_by_user_id,
                    now,
                    now,
                ),
            )
            for field_name, plaintext in secret_fields.items():
                conn.execute(
                    """
                    INSERT INTO credential_secret_fields
                    (credential_id, field_name, ciphertext, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (cid, field_name, nymeria_secrets.encrypt(plaintext), now),
                )
            conn.commit()
            self._audit_locked(
                conn,
                credential_id=cid,
                actor_user_id=created_by_user_id,
                event_type="created",
                details={"provider": provider, "kind": kind, "secret_fields": sorted(secret_fields)},
            )
            conn.commit()
        return self.get_credential(cid)  # type: ignore[return-value]

    def upsert_credential(
        self,
        *,
        credential_id: str,
        owner_type: OwnerType,
        owner_user_id: Optional[str],
        name: str,
        provider: str,
        kind: str,
        secret_fields: Optional[dict[str, str]] = None,
        metadata: Optional[dict[str, Any]] = None,
        scopes: Optional[list[str]] = None,
        allowed_targets: Optional[list[str]] = None,
        account_label: Optional[str] = None,
        expires_at: Optional[str] = None,
        status: CredentialStatus = "active",
        actor_user_id: Optional[str] = None,
    ) -> CredentialRecord:
        if owner_type not in {"user", "system"}:
            raise ValueError("owner_type must be 'user' or 'system'")
        if owner_type == "user" and not owner_user_id:
            raise ValueError("owner_user_id is required for user-owned credentials")
        now = _now()
        metadata = metadata or {}
        scopes = scopes or []
        allowed_targets = allowed_targets or []
        secret_fields = secret_fields or {}

        with self._lock, self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM credentials WHERE id = ?", (credential_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE credentials
                    SET owner_type = ?, owner_user_id = ?, name = ?, provider = ?,
                        kind = ?, account_label = ?, status = ?, metadata_json = ?,
                        scopes_json = ?, allowed_targets_json = ?, expires_at = ?,
                        updated_at = ?, disabled_at = NULL
                    WHERE id = ?
                    """,
                    (
                        owner_type,
                        owner_user_id,
                        name,
                        provider,
                        kind,
                        account_label,
                        status,
                        _json_dumps(metadata),
                        _json_dumps(scopes),
                        _json_dumps(allowed_targets),
                        expires_at,
                        now,
                        credential_id,
                    ),
                )
                event_type = "updated"
            else:
                conn.execute(
                    """
                    INSERT INTO credentials (
                        id, owner_type, owner_user_id, name, provider, kind,
                        account_label, status, metadata_json, scopes_json,
                        allowed_targets_json, expires_at, created_by_user_id,
                        created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        credential_id,
                        owner_type,
                        owner_user_id,
                        name,
                        provider,
                        kind,
                        account_label,
                        status,
                        _json_dumps(metadata),
                        _json_dumps(scopes),
                        _json_dumps(allowed_targets),
                        expires_at,
                        actor_user_id,
                        now,
                        now,
                    ),
                )
                event_type = "created"

            for field_name, plaintext in secret_fields.items():
                conn.execute(
                    """
                    INSERT INTO credential_secret_fields
                    (credential_id, field_name, ciphertext, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(credential_id, field_name) DO UPDATE SET
                        ciphertext = excluded.ciphertext,
                        updated_at = excluded.updated_at
                    """,
                    (
                        credential_id,
                        field_name,
                        nymeria_secrets.encrypt(plaintext),
                        now,
                    ),
                )
            self._audit_locked(
                conn,
                credential_id=credential_id,
                actor_user_id=actor_user_id,
                event_type=event_type,
                details={"provider": provider, "kind": kind, "secret_fields": sorted(secret_fields)},
            )
            conn.commit()
        return self.get_credential(credential_id)  # type: ignore[return-value]

    def get_credential(self, credential_id: str) -> Optional[CredentialRecord]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM credentials WHERE id = ?", (credential_id,)
            ).fetchone()
            if row is None:
                return None
            return _row_to_record(row, self._secret_field_names(conn, credential_id))

    def list_credentials(
        self,
        *,
        owner_user_id: Optional[str] = None,
        include_system: bool = False,
        include_disabled: bool = False,
        owner_type: Optional[OwnerType] = None,
    ) -> list[CredentialRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if owner_type:
            clauses.append("owner_type = ?")
            params.append(owner_type)
        elif owner_user_id is not None and include_system:
            clauses.append("(owner_user_id = ? OR owner_type = 'system')")
            params.append(owner_user_id)
        elif owner_user_id is not None:
            clauses.append("owner_user_id = ?")
            params.append(owner_user_id)
        elif not include_system:
            clauses.append("owner_type != 'system'")
        if not include_disabled:
            clauses.append("status != 'disabled'")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM credentials {where} ORDER BY provider ASC, name ASC",
                tuple(params),
            ).fetchall()
            field_names = self._secret_field_names_for(conn, [row["id"] for row in rows])
            return [
                _row_to_record(row, field_names.get(row["id"], []))
                for row in rows
            ]

    def delete_credential(
        self,
        credential_id: str,
        *,
        actor_user_id: Optional[str] = None,
        actor_is_admin: bool = False,
    ) -> bool:
        with self._lock, self._connect() as conn:
            record = self._record_locked(conn, credential_id)
            if record is None:
                return False
            self._require_actor_can_access(
                record,
                actor_user_id=actor_user_id,
                actor_is_admin=actor_is_admin,
            )
            self._audit_locked(
                conn,
                credential_id=credential_id,
                actor_user_id=actor_user_id,
                event_type="deleted",
            )
            cur = conn.execute("DELETE FROM credentials WHERE id = ?", (credential_id,))
            conn.commit()
            return cur.rowcount > 0

    def disable_credential(
        self,
        credential_id: str,
        *,
        actor_user_id: Optional[str] = None,
        actor_is_admin: bool = False,
    ) -> bool:
        now = _now()
        with self._lock, self._connect() as conn:
            record = self._record_locked(conn, credential_id)
            if record is None or record.status == "disabled":
                return False
            self._require_actor_can_access(
                record,
                actor_user_id=actor_user_id,
                actor_is_admin=actor_is_admin,
            )
            cur = conn.execute(
                """
                UPDATE credentials
                SET status = 'disabled', disabled_at = ?, updated_at = ?
                WHERE id = ? AND status != 'disabled'
                """,
                (now, now, credential_id),
            )
            if cur.rowcount:
                self._audit_locked(
                    conn,
                    credential_id=credential_id,
                    actor_user_id=actor_user_id,
                    event_type="disabled",
                )
            conn.commit()
            return cur.rowcount > 0

    def add_allowed_target(
        self,
        credential_id: str,
        *,
        target: str,
        actor_user_id: Optional[str] = None,
    ) -> bool:
        """Add ``target`` (``"type:id"`` form) to a credential's
        ``allowed_targets_json``.

        Idempotent: returns False if the target was already present, True if it
        was actually added. Raises CredentialNotFound if the credential does not
        exist. Use this when an in-flight prompt resolves and binding to a
        specific MCP server or native tool needs to be recorded on the
        credential row itself, not just in the bookkeeping ``credential_bindings``
        table (which ``_target_allowed`` does not consult).
        """
        target = target.strip()
        if not target:
            raise ValueError("target must be a non-empty 'type:id' string")
        record = self.get_credential(credential_id)
        if record is None:
            raise CredentialNotFound(credential_id)
        existing = list(record.allowed_targets or [])
        if target in existing:
            return False
        existing.append(target)
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE credentials
                SET allowed_targets_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (_json_dumps(existing), now, credential_id),
            )
            self._audit_locked(
                conn,
                credential_id=credential_id,
                actor_user_id=actor_user_id,
                event_type="allowed_target_added",
                target_type=target.split(":", 1)[0] if ":" in target else None,
                target_id=target.split(":", 1)[1] if ":" in target else None,
                details={"target": target},
            )
            conn.commit()
        return True

    def remove_allowed_target(
        self,
        credential_id: str,
        *,
        target: str,
        actor_user_id: Optional[str] = None,
        actor_is_admin: bool = False,
    ) -> bool:
        """Remove ``target`` from a credential's runtime allowed targets.

        Idempotent: returns False if the target was not present, True if it was
        removed. Raises CredentialNotFound if the credential does not exist.
        """
        target = target.strip()
        if not target:
            raise ValueError("target must be a non-empty 'type:id' string")
        now = _now()
        with self._lock, self._connect() as conn:
            record = self._record_locked(conn, credential_id)
            if record is None:
                raise CredentialNotFound(credential_id)
            self._require_actor_can_access(
                record,
                actor_user_id=actor_user_id,
                actor_is_admin=actor_is_admin,
            )
            existing = list(record.allowed_targets or [])
            if target not in existing:
                return False
            updated = [item for item in existing if item != target]
            conn.execute(
                """
                UPDATE credentials
                SET allowed_targets_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (_json_dumps(updated), now, credential_id),
            )
            self._audit_locked(
                conn,
                credential_id=credential_id,
                actor_user_id=actor_user_id,
                event_type="allowed_target_removed",
                target_type=target.split(":", 1)[0] if ":" in target else None,
                target_id=target.split(":", 1)[1] if ":" in target else None,
                details={"target": target},
            )
            conn.commit()
        return True

    def bind_credential(
        self,
        credential_id: str,
        *,
        target_type: str,
        target_id: str,
        binding_name: Optional[str] = None,
        actor_user_id: Optional[str] = None,
    ) -> str:
        if not self.get_credential(credential_id):
            raise CredentialNotFound(credential_id)
        binding_id = _stable_id("cbind", credential_id, target_type, target_id, binding_name or "")
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO credential_bindings
                (id, credential_id, target_type, target_id, binding_name, created_by_user_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding_id,
                    credential_id,
                    target_type,
                    target_id,
                    binding_name,
                    actor_user_id,
                    now,
                ),
            )
            self._audit_locked(
                conn,
                credential_id=credential_id,
                actor_user_id=actor_user_id,
                event_type="bound",
                target_type=target_type,
                target_id=target_id,
                details={"binding_name": binding_name},
            )
            conn.commit()
        return binding_id

    def list_bindings(self, credential_id: Optional[str] = None) -> list[dict[str, Any]]:
        where = ""
        params: tuple[Any, ...] = ()
        if credential_id:
            where = "WHERE credential_id = ?"
            params = (credential_id,)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM credential_bindings {where} ORDER BY created_at DESC",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def get_binding(self, binding_id: str) -> Optional[dict[str, Any]]:
        """Return a single binding row by id, or None if it does not exist.

        Indexed lookup on the ``credential_bindings`` primary key, mirroring the
        row shape of ``list_bindings`` so callers can build a
        ``CredentialBindingResponse`` directly.
        """
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM credential_bindings WHERE id = ?", (binding_id,)
            ).fetchone()
            return dict(row) if row else None

    def delete_binding(self, binding_id: str, *, actor_user_id: Optional[str] = None) -> bool:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM credential_bindings WHERE id = ?", (binding_id,)
            ).fetchone()
            cur = conn.execute("DELETE FROM credential_bindings WHERE id = ?", (binding_id,))
            if row:
                self._audit_locked(
                    conn,
                    credential_id=row["credential_id"],
                    actor_user_id=actor_user_id,
                    event_type="unbound",
                    target_type=row["target_type"],
                    target_id=row["target_id"],
                    details={"binding_name": row["binding_name"]},
                )
            conn.commit()
            return cur.rowcount > 0

    def _target_allowed(self, record: CredentialRecord, target_type: Optional[str], target_id: Optional[str]) -> bool:
        allowed = record.allowed_targets or []
        if not allowed or "*" in allowed:
            return True
        if not target_type:
            return False
        candidates = {f"{target_type}:*", f"{target_type}:{target_id or ''}"}
        return bool(candidates.intersection(set(allowed)))

    def _record_locked(
        self,
        conn: sqlite3.Connection,
        credential_id: str,
    ) -> Optional[CredentialRecord]:
        row = conn.execute("SELECT * FROM credentials WHERE id = ?", (credential_id,)).fetchone()
        if row is None:
            return None
        return _row_to_record(row, self._secret_field_names(conn, credential_id))

    def _require_actor_can_access(
        self,
        record: CredentialRecord,
        *,
        actor_user_id: Optional[str],
        actor_is_admin: bool = False,
    ) -> None:
        if actor_user_id is None or actor_is_admin or record.owner_type != "user":
            return
        if record.owner_user_id == actor_user_id:
            return
        raise CredentialAccessDenied(
            f"Credential {record.id} is not owned by actor {actor_user_id}"
        )

    def get_secret_field(
        self,
        credential_id: str,
        field_name: str,
        *,
        actor_user_id: Optional[str] = None,
        actor_is_admin: bool = False,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
    ) -> str:
        with self._lock, self._connect() as conn:
            record = self._record_locked(conn, credential_id)
            if record is None:
                raise CredentialNotFound(credential_id)
            self._require_actor_can_access(
                record,
                actor_user_id=actor_user_id,
                actor_is_admin=actor_is_admin,
            )
            if record.status == "disabled":
                raise CredentialAccessDenied(f"Credential {credential_id} is disabled")
            if not self._target_allowed(record, target_type, target_id):
                raise CredentialAccessDenied(
                    f"Credential {credential_id} is not allowed for {target_type}:{target_id}"
                )
            secret_row = conn.execute(
                """
                SELECT ciphertext FROM credential_secret_fields
                WHERE credential_id = ? AND field_name = ?
                """,
                (credential_id, field_name),
            ).fetchone()
            if secret_row is None:
                raise CredentialSecretUnavailable(
                    f"Credential {credential_id} has no secret field {field_name}"
                )
            try:
                plaintext = nymeria_secrets.decrypt(secret_row["ciphertext"])
            except (nymeria_secrets.SecretsKeyMissing, nymeria_secrets.SecretsKeyInvalid, InvalidToken) as exc:
                self._audit_locked(
                    conn,
                    credential_id=credential_id,
                    actor_user_id=actor_user_id,
                    event_type="decrypt_failed",
                    target_type=target_type,
                    target_id=target_id,
                    details={"field_name": field_name, "error": exc.__class__.__name__},
                )
                conn.commit()
                raise CredentialSecretUnavailable(str(exc)) from exc
            now = _now()
            conn.execute(
                "UPDATE credentials SET last_used_at = ?, updated_at = ? WHERE id = ?",
                (now, now, credential_id),
            )
            self._audit_locked(
                conn,
                credential_id=credential_id,
                actor_user_id=actor_user_id,
                event_type="used",
                target_type=target_type,
                target_id=target_id,
                details={"field_name": field_name},
            )
            conn.commit()
            return plaintext

    def get_secret_fields_for_test(
        self,
        credential_id: str,
        *,
        actor_user_id: Optional[str] = None,
        actor_is_admin: bool = False,
    ) -> dict[str, str]:
        """Return all secret fields for a credential owner/admin test probe.

        Credential tests validate the credential itself, not a runtime target,
        so this performs owner/admin checks without applying allowed_targets.
        """
        with self._lock, self._connect() as conn:
            record = self._record_locked(conn, credential_id)
            if record is None:
                raise CredentialNotFound(credential_id)
            self._require_actor_can_access(
                record,
                actor_user_id=actor_user_id,
                actor_is_admin=actor_is_admin,
            )
            if record.status == "disabled":
                raise CredentialAccessDenied(f"Credential {credential_id} is disabled")
            rows = conn.execute(
                """
                SELECT field_name, ciphertext FROM credential_secret_fields
                WHERE credential_id = ?
                """,
                (credential_id,),
            ).fetchall()
            out: dict[str, str] = {}
            for row in rows:
                try:
                    out[str(row["field_name"])] = nymeria_secrets.decrypt(row["ciphertext"])
                except (
                    nymeria_secrets.SecretsKeyMissing,
                    nymeria_secrets.SecretsKeyInvalid,
                    InvalidToken,
                ) as exc:
                    self._audit_locked(
                        conn,
                        credential_id=credential_id,
                        actor_user_id=actor_user_id,
                        event_type="decrypt_failed",
                        target_type="credential_test",
                        target_id=credential_id,
                        details={
                            "field_name": row["field_name"],
                            "error": exc.__class__.__name__,
                        },
                    )
                    conn.commit()
                    raise CredentialSecretUnavailable(str(exc)) from exc
            return out

    def resolve_references(
        self,
        value: str,
        *,
        actor_user_id: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        used_credentials: Optional[set[str]] = None,
        redact_values: Optional[set[str]] = None,
    ) -> str:
        def repl(match: re.Match[str]) -> str:
            credential_id = match.group(1)
            field_name = match.group(2)
            plaintext = self.get_secret_field(
                credential_id,
                field_name,
                actor_user_id=actor_user_id,
                target_type=target_type,
                target_id=target_id,
            )
            if used_credentials is not None:
                used_credentials.add(credential_id)
            if redact_values is not None:
                redact_values.add(plaintext)
            return plaintext

        return CREDENTIAL_REF_PATTERN.sub(repl, value)

    def upsert_legacy_cache(
        self,
        user_id: str,
        cache_filename: str,
        cache: dict[str, Any],
        *,
        actor_user_id: Optional[str] = None,
    ) -> CredentialRecord:
        accounts, scopes, expires_at = _summarize_cache(cache)
        provider = _provider_from_cache_filename(cache_filename)
        name = _cache_display_name(cache_filename)
        account_label = None
        if accounts:
            labels = [
                str(a.get("email") or a.get("name") or a.get("account_id"))
                for a in accounts
                if a.get("email") or a.get("name") or a.get("account_id")
            ]
            if labels:
                account_label = ", ".join(labels[:3])
                if len(labels) > 3:
                    account_label += f" +{len(labels) - 3}"
        metadata = {
            "cache_filename": cache_filename,
            "source": "auth_cache_utils",
            "accounts": accounts,
            "account_count": len(accounts),
        }
        credential_id = _stable_id("cred_lcache", _safe_segment(user_id), cache_filename)
        return self.upsert_credential(
            credential_id=credential_id,
            owner_type="user",
            owner_user_id=user_id,
            name=name,
            provider=provider,
            kind="legacy_token_cache",
            account_label=account_label,
            metadata=metadata,
            scopes=scopes,
            expires_at=expires_at,
            secret_fields={"cache_json": json.dumps(cache, default=str)},
            actor_user_id=actor_user_id or user_id,
        )

    def load_legacy_cache(self, user_id: str, cache_filename: str) -> Optional[dict[str, Any]]:
        credential_id = _stable_id("cred_lcache", _safe_segment(user_id), cache_filename)
        record = self.get_credential(credential_id)
        if not record or record.status == "disabled":
            return None
        raw = self.get_secret_field(
            credential_id,
            "cache_json",
            actor_user_id=user_id,
            target_type="native_auth_cache",
            target_id=cache_filename,
        )
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}

    def delete_legacy_cache(self, user_id: str, cache_filename: str) -> bool:
        credential_id = _stable_id("cred_lcache", _safe_segment(user_id), cache_filename)
        return self.delete_credential(credential_id, actor_user_id=user_id)

    def mark_tested(
        self,
        credential_id: str,
        *,
        status: str,
        actor_user_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        now = _now()
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE credentials SET status = ?, last_tested_at = ?, updated_at = ? WHERE id = ?",
                (status, now, now, credential_id),
            )
            self._audit_locked(
                conn,
                credential_id=credential_id,
                actor_user_id=actor_user_id,
                event_type="tested",
                details=details or {"status": status},
            )
            conn.commit()

    def _audit_locked(
        self,
        conn: sqlite3.Connection,
        *,
        credential_id: Optional[str],
        actor_user_id: Optional[str],
        event_type: str,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO credential_audit_events
            (credential_id, actor_user_id, event_type, target_type, target_id, details_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                credential_id,
                actor_user_id,
                event_type,
                target_type,
                target_id,
                _json_dumps(details or {}),
                _now(),
            ),
        )


_vault_repo: Optional[CredentialVaultRepo] = None
_vault_repo_lock = threading.Lock()


def get_credential_vault_repo(db_path: Optional[Path] = None) -> CredentialVaultRepo:
    global _vault_repo
    if db_path is None:
        from ..config import get_settings

        db_path = get_settings().data_dir / "accounts.db"
    db_path = Path(db_path)
    # Fast path: already initialized for this db. Guard the lazy construction
    # with a lock so concurrent first-access from request handlers cannot race
    # two CredentialVaultRepo builds onto the global.
    repo = _vault_repo
    if repo is not None and repo.db_path == db_path:
        return repo
    with _vault_repo_lock:
        if _vault_repo is None or _vault_repo.db_path != db_path:
            _vault_repo = CredentialVaultRepo(db_path)
        return _vault_repo


def migrate_auth_token_files(data_dir: Path, repo: CredentialVaultRepo) -> list[str]:
    """Import first-level per-user auth token JSON files into the vault.

    Only files like ``data/auth_tokens/<user_id>/microsoft.json`` are moved.
    Export files under ``mcp/`` are intentionally left alone because external
    MCP servers still need those provider-specific files at runtime.
    """
    logs: list[str] = []
    auth_root = Path(data_dir) / "auth_tokens"
    if not auth_root.exists():
        return logs
    for user_dir in sorted(p for p in auth_root.iterdir() if p.is_dir()):
        user_id = user_dir.name
        for path in sorted(user_dir.glob("*.json")):
            try:
                text = path.read_text(encoding="utf-8")
                if not text.strip() or text.strip() == "{}":
                    continue
                data = json.loads(text)
                if not isinstance(data, dict):
                    continue
                repo.upsert_legacy_cache(user_id, path.name, data, actor_user_id=user_id)
                path.unlink()
                logs.append(f"Migrated auth cache {path.name} for user {user_id} into credential vault.")
            except Exception as exc:
                logs.append(f"Skipped auth cache migration for {path}: {exc}")
                logger.warning("Auth cache migration failed for %s: %s", path, exc)
    return logs


def migrate_mcp_encrypted_env_vars(mcp_servers_dir: Path, repo: CredentialVaultRepo) -> list[str]:
    """Move legacy MCP encrypted env vars into reusable system credentials."""
    logs: list[str] = []
    servers_dir = Path(mcp_servers_dir)
    if not servers_dir.exists():
        return logs
    for path in sorted(servers_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logs.append(f"Skipped MCP credential migration for {path.name}: {exc}")
            continue
        encrypted = data.get("encrypted_env_vars") or {}
        if not isinstance(encrypted, dict) or not encrypted:
            continue
        server_id = str(data.get("id") or path.stem)
        server_name = str(data.get("name") or server_id)
        env_vars = dict(data.get("env_vars") or {})
        migrated_any = False
        for env_name, ciphertext in list(encrypted.items()):
            try:
                plaintext = nymeria_secrets.decrypt(str(ciphertext))
            except Exception as exc:
                logs.append(f"Could not decrypt MCP secret {server_id}.{env_name}: {exc.__class__.__name__}")
                continue
            credential_id = _stable_id("cred_mcp", server_id, str(env_name))
            repo.upsert_credential(
                credential_id=credential_id,
                owner_type="system",
                owner_user_id=None,
                name=f"{server_name} {env_name}",
                provider="mcp",
                kind="env_var",
                account_label=str(env_name),
                metadata={"server_id": server_id, "env_name": str(env_name), "source": "mcp_encrypted_env_vars"},
                allowed_targets=[f"mcp_server:{server_id}"],
                secret_fields={"value": plaintext},
            )
            env_vars[str(env_name)] = f"${{credential:{credential_id}.value}}"
            encrypted.pop(env_name, None)
            migrated_any = True
        if migrated_any:
            data["env_vars"] = env_vars
            data["encrypted_env_vars"] = encrypted
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
            logs.append(f"Migrated encrypted MCP env vars for {server_id} into credential vault.")
    return logs


__all__ = [
    "CREDENTIAL_REF_PATTERN",
    "CredentialAccessDenied",
    "CredentialNotFound",
    "CredentialRecord",
    "CredentialSecretUnavailable",
    "CredentialVaultRepo",
    "get_credential_vault_repo",
    "migrate_auth_token_files",
    "migrate_mcp_encrypted_env_vars",
]
