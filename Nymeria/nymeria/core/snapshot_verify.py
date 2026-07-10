"""Verification checks for snapshot artifacts.

Runs against an :class:`~nymeria.core.snapshot.ExtractedSnapshot` (the shared
extraction path used by both ``snapshot verify`` and ``snapshot restore``, so
a restore always operates on exactly what was verified):

- manifest presence + per-member sha256/size comparison (computed during
  extraction),
- ``PRAGMA integrity_check`` on every captured SQLite database,
- JSON parse of every ``.json`` store copy,
- shape checks on the Postgres COPY dumps (present, row counts match the
  manifest),
- the vault canary: decrypt one real ``credential_secret_fields`` ciphertext
  with the key embedded in the artifact, proving key and data actually
  belong together (the failure mode that makes a volume-only backup
  worthless),
- key-presence status.

The check report mirrors ``nymeria doctor``'s pass/warn/fail shape but stays
import-light on purpose: doctor pulls the LLM provider stack in, which a
bare-host restore path must not need.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from .snapshot import (
    DATA_PREFIX,
    POSTGRES_PREFIX,
    SECRETS_MEMBER,
    ExtractedSnapshot,
)
from .snapshot_stores import (
    POSTGRES_CHECKPOINT_TABLES,
    is_sqlite_file,
    sqlite_integrity_ok,
)

logger = logging.getLogger(__name__)

Status = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class SnapshotCheck:
    """One verification result line."""

    name: str
    status: Status
    detail: str


def verify_extracted(extracted: ExtractedSnapshot) -> List[SnapshotCheck]:
    """Run every verification check against an extracted snapshot."""
    checks: List[SnapshotCheck] = []
    root = extracted.root
    manifest = extracted.manifest

    # --- manifest / member hashes ------------------------------------------
    if extracted.hash_failures:
        preview = "; ".join(extracted.hash_failures[:5])
        more = len(extracted.hash_failures) - 5
        if more > 0:
            preview += f" (+{more} more)"
        checks.append(SnapshotCheck("Manifest", "fail", preview))
    else:
        file_count = len(manifest.get("files", {}))
        checks.append(
            SnapshotCheck("Manifest", "pass", f"{file_count} members, all digests match")
        )

    # --- SQLite copies -------------------------------------------------------
    data_root = root / DATA_PREFIX.rstrip("/")
    db_total = 0
    db_failures: List[str] = []
    if data_root.is_dir():
        for db_path in sorted(data_root.rglob("*")):
            if not db_path.is_file() or not is_sqlite_file(db_path):
                continue
            db_total += 1
            ok, detail = sqlite_integrity_ok(db_path)
            if not ok:
                rel = db_path.relative_to(data_root).as_posix()
                db_failures.append(f"{rel}: {detail}")
    if db_failures:
        checks.append(SnapshotCheck("SQLite", "fail", "; ".join(db_failures[:5])))
    elif db_total:
        checks.append(
            SnapshotCheck("SQLite", "pass", f"{db_total} database(s) pass integrity_check")
        )
    else:
        checks.append(SnapshotCheck("SQLite", "warn", "no SQLite databases captured"))

    # --- JSON stores ---------------------------------------------------------
    json_total = 0
    json_failures: List[str] = []
    if data_root.is_dir():
        for json_path in sorted(data_root.rglob("*.json")):
            if not json_path.is_file():
                continue
            json_total += 1
            try:
                json.loads(json_path.read_text(encoding="utf-8"))
            except (ValueError, OSError, UnicodeDecodeError) as exc:
                rel = json_path.relative_to(data_root).as_posix()
                json_failures.append(f"{rel}: {exc.__class__.__name__}")
    if json_failures:
        checks.append(SnapshotCheck("JSON stores", "fail", "; ".join(json_failures[:5])))
    else:
        checks.append(SnapshotCheck("JSON stores", "pass", f"{json_total} file(s) parse"))

    # --- Postgres dumps ------------------------------------------------------
    checks.append(_check_postgres_dumps(root, manifest))

    # --- vault canary + key --------------------------------------------------
    checks.extend(_check_vault_key(root, manifest))

    return checks


def _check_postgres_dumps(root: Path, manifest: Dict[str, Any]) -> SnapshotCheck:
    backend = manifest.get("checkpoint_backend")
    if backend != "postgres":
        return SnapshotCheck(
            "Checkpoints", "pass", f"{backend} backend (inside the data dir)"
        )
    dump_dir = root / POSTGRES_PREFIX.rstrip("/")
    summary = manifest.get("postgres") or {}
    recorded_counts = summary.get("row_counts") or {}
    problems: List[str] = []
    for table in POSTGRES_CHECKPOINT_TABLES:
        dump = dump_dir / f"{table}.copy"
        if not dump.is_file():
            problems.append(f"missing {table}.copy")
            continue
        expected = recorded_counts.get(table)
        if expected is None:
            continue
        actual = _copy_row_count(dump)
        if actual != expected:
            problems.append(f"{table}: {actual} rows, manifest says {expected}")
    if problems:
        return SnapshotCheck("Checkpoints", "fail", "; ".join(problems))
    version = summary.get("migration_version")
    total = recorded_counts.get("checkpoints")
    return SnapshotCheck(
        "Checkpoints",
        "pass",
        f"4 COPY dumps, {total} checkpoint row(s), schema migration {version}",
    )


def _copy_row_count(dump: Path) -> int:
    # COPY text format escapes embedded newlines, so raw 0x0A bytes are
    # exactly the row terminators.
    count = 0
    with dump.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                return count
            count += chunk.count(b"\n")


def _check_vault_key(root: Path, manifest: Dict[str, Any]) -> List[SnapshotCheck]:
    checks: List[SnapshotCheck] = []
    key_file = root / SECRETS_MEMBER
    accounts_db = root / DATA_PREFIX.rstrip("/") / "accounts.db"
    ciphertext = _sample_vault_ciphertext(accounts_db)

    if not key_file.is_file():
        if manifest.get("includes_key"):
            checks.append(
                SnapshotCheck("Vault key", "fail", "manifest claims a key but none found")
            )
        elif ciphertext is not None:
            checks.append(
                SnapshotCheck(
                    "Vault key",
                    "warn",
                    "no key embedded but the vault holds encrypted secrets; "
                    "restore requires the original NYMERIA_SECRETS_KEY",
                )
            )
        else:
            checks.append(
                SnapshotCheck("Vault key", "pass", "no key embedded, no vault secrets")
            )
        return checks

    key = key_file.read_text(encoding="ascii").strip()
    if ciphertext is None:
        checks.append(
            SnapshotCheck(
                "Vault canary", "pass", "key embedded; vault has no secrets to test"
            )
        )
        return checks
    try:
        from cryptography.fernet import Fernet

        Fernet(key.encode("ascii")).decrypt(ciphertext.encode("ascii"))
        checks.append(
            SnapshotCheck(
                "Vault canary",
                "pass",
                "embedded key decrypts a real vault ciphertext",
            )
        )
    except Exception as exc:  # noqa: BLE001 - any failure means the pair is broken
        checks.append(
            SnapshotCheck(
                "Vault canary",
                "fail",
                f"embedded key cannot decrypt the vault ({exc.__class__.__name__}); "
                "key and data do not belong together",
            )
        )
    return checks


def _sample_vault_ciphertext(accounts_db: Path) -> Optional[str]:
    if not accounts_db.is_file():
        return None
    import sqlite3
    from contextlib import closing

    try:
        with closing(
            sqlite3.connect(f"file:{accounts_db}?mode=ro", uri=True, timeout=10.0)
        ) as conn:
            row = conn.execute(
                "SELECT ciphertext FROM credential_secret_fields LIMIT 1"
            ).fetchone()
            return str(row[0]) if row else None
    except sqlite3.Error:
        return None


def has_failures(checks: List[SnapshotCheck]) -> bool:
    return any(check.status == "fail" for check in checks)


__all__ = [
    "SnapshotCheck",
    "Status",
    "has_failures",
    "verify_extracted",
]
