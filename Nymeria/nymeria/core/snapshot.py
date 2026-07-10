"""User-data snapshot create/restore orchestration (disaster recovery).

One artifact captures everything a Nymeria deployment cannot regenerate:

- the whole data dir (SQLite databases via the online backup API, JSON
  stores via per-inode reads; see ``core/snapshot_stores.py``),
- the LangGraph Postgres checkpoint tables in the Docker shape (one
  cross-table-consistent COPY dump),
- the durable workspace subtrees (``threads/`` attachments and ``images/``,
  which survive compaction and are referenced by restored threads),
- the ``NYMERIA_SECRETS_KEY`` Fernet vault key, embedded so a single
  passphrase-encrypted file restores a bare host, credential vault included.

The artifact is a tar.gz streamed through the chunked AES-GCM envelope in
``core/snapshot_crypto.py`` (or written plain with ``encrypt=False``, in which
case the key is deliberately NOT embedded).

Member layout, in order: ``meta.json`` first (so ``snapshot list`` can read
identity without streaming the whole file), then ``data/...``,
``postgres/<table>.copy``, ``workspace/...``, ``secrets/fernet.key``, and
``manifest.json`` last (per-member sha256 digests plus counts, written after
everything it describes).

Consistency contract: per-store consistent, cross-store best-effort within
the capture window; the stack may keep running. Restore is offline-only and
refuses when the stack looks live. This module is deliberately independent of
the agent runtime; only settings, stdlib, and the sibling snapshot modules.

The "snapshot" namespace is intentional: "backup" (``core/backup.py``,
``settings.backups_dir``, ``data/backups/``) already means the
self-modification source-file rollback system.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import socket
import tarfile
import tempfile
import urllib.error
import urllib.request
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Tuple

from .secrets import SECRETS_KEY_ENV_VAR
from .snapshot_crypto import (
    SnapshotDecryptError,
    open_decrypting_reader,
    open_encrypting_writer,
    sniff_artifact,
)
from .snapshot_stores import (
    POSTGRES_CHECKPOINT_TABLES,
    CheckpointSchemaMissing,
    SnapshotStoreError,
    dump_postgres_checkpoints,
    ensure_postgres_checkpoint_schema,
    is_sqlite_file,
    iter_data_dir_files,
    restore_postgres_checkpoints,
    sqlite_consistent_copy,
)

logger = logging.getLogger(__name__)

MANIFEST_FORMAT = 1
META_MEMBER = "meta.json"
MANIFEST_MEMBER = "manifest.json"
SECRETS_MEMBER = "secrets/fernet.key"
DATA_PREFIX = "data/"
POSTGRES_PREFIX = "postgres/"
WORKSPACE_PREFIX = "workspace/"
ENCRYPTED_SUFFIX = ".nysnap"
PLAIN_SUFFIX = ".tar.gz"

# Durable workspace subtrees. The rest of the workspace is agent scratch
# (bash temp files and similar) and is intentionally not captured.
WORKSPACE_SUBDIRS = ("threads", "images")


class SnapshotError(RuntimeError):
    """Create/restore level failure with an operator-facing message."""


@dataclass
class SnapshotResult:
    """Outcome of a successful create."""

    artifact: Path
    manifest: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)


@dataclass
class ExtractedSnapshot:
    """A snapshot unpacked into a working directory, hashes verified."""

    root: Path
    meta: Dict[str, Any]
    manifest: Dict[str, Any]
    hash_failures: List[str] = field(default_factory=list)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _nymeria_version() -> str:
    try:
        from importlib.metadata import version

        return version("nymeriaos")
    except Exception:  # noqa: BLE001 - version is informational only
        return "unknown"


def _workspace_dir() -> Path:
    # Mirrors tools/filesystem.get_workspace_dir without importing the tools
    # package (which pulls the agent runtime into what must stay a standalone
    # maintenance path).
    return Path(os.environ.get("NYMERIA_WORKSPACE_DIR", "/workspace")).resolve()


class _HashingReader(io.RawIOBase):
    """Wrap a file object, hashing exactly the bytes tarfile consumes."""

    def __init__(self, fileobj: BinaryIO) -> None:
        super().__init__()
        self._inner = fileobj
        self.digest = hashlib.sha256()

    def readable(self) -> bool:  # pragma: no cover - io protocol
        return True

    def read(self, size: int = -1) -> bytes:  # type: ignore[override]
        data = self._inner.read(size)
        if data:
            self.digest.update(data)
        return data


def _add_file_snapshot(
    tar: tarfile.TarFile,
    path: Path,
    arcname: str,
    files_index: Dict[str, Dict[str, Any]],
) -> None:
    """Add one file to the tar from a single open fd.

    Opening first and fstat-ing the fd pins one inode: the JSON store writers
    replace files atomically, so the member is always a complete old-or-new
    version even if the store is saved mid-capture.
    """
    with path.open("rb") as fh:
        st = os.fstat(fh.fileno())
        info = tarfile.TarInfo(arcname)
        info.size = st.st_size
        info.mtime = st.st_mtime
        info.mode = 0o600
        reader = _HashingReader(fh)
        tar.addfile(info, reader)
    files_index[arcname] = {
        "sha256": reader.digest.hexdigest(),
        "size": st.st_size,
    }


def _add_bytes(
    tar: tarfile.TarFile,
    arcname: str,
    payload: bytes,
    files_index: Optional[Dict[str, Dict[str, Any]]],
) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(payload)
    info.mtime = datetime.now(timezone.utc).timestamp()
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(payload))
    if files_index is not None:
        files_index[arcname] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }


def _sqlite_counts(extract_or_temp: Path) -> Dict[str, Optional[int]]:
    """Best-effort row counts from captured copies, for the manifest."""
    import sqlite3

    counts: Dict[str, Optional[int]] = {"users": None, "credentials": None}

    def _count(db: Path, query: str) -> Optional[int]:
        if not db.exists():
            return None
        try:
            with closing(
                sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10.0)
            ) as conn:
                row = conn.execute(query).fetchone()
                return int(row[0]) if row else None
        except sqlite3.Error:
            return None

    accounts = extract_or_temp / "accounts.db"
    counts["users"] = _count(accounts, "SELECT count(*) FROM users")
    counts["credentials"] = _count(accounts, "SELECT count(*) FROM credentials")
    checkpoints = extract_or_temp / "nymeria.db"
    threads = _count(checkpoints, "SELECT count(DISTINCT thread_id) FROM checkpoints")
    if threads is not None:
        counts["threads"] = threads
    return counts


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_snapshot(
    settings: Any,
    *,
    output: Optional[Path] = None,
    passphrase: Optional[str] = None,
    encrypt: bool = True,
    include_workspace: bool = True,
    include_code_backups: bool = False,
) -> SnapshotResult:
    """Capture a snapshot artifact. The stack may keep running (online)."""
    if encrypt and not passphrase:
        raise SnapshotError("Encryption requires a passphrase")
    data_dir = Path(settings.data_dir)
    if not data_dir.is_dir():
        raise SnapshotError(f"Data dir does not exist: {data_dir}")

    warnings: List[str] = []
    backend = getattr(settings, "database_backend", "sqlite")
    postgres_summary: Optional[Dict[str, Any]] = None

    snapshots_dir = Path(getattr(settings, "snapshots_dir", data_dir / "snapshots"))
    if output is None:
        suffix = ENCRYPTED_SUFFIX if encrypt else PLAIN_SUFFIX
        output = snapshots_dir / f"snapshot-{_utc_stamp()}{suffix}"
    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)

    secrets_key = os.environ.get(SECRETS_KEY_ENV_VAR)
    include_key = bool(encrypt and secrets_key)
    if encrypt and not secrets_key:
        warnings.append(
            f"{SECRETS_KEY_ENV_VAR} is not set; the artifact will not embed a "
            "vault key. Encrypted vault rows (if any) will be unrecoverable "
            "from this snapshot alone."
        )
    if not encrypt:
        warnings.append(
            "Snapshot is NOT encrypted and does not embed the vault key. "
            "Back up NYMERIA_SECRETS_KEY separately and protect the file."
        )

    files_index: Dict[str, Dict[str, Any]] = {}
    created_at = datetime.now(timezone.utc).isoformat()
    meta = {
        "format": MANIFEST_FORMAT,
        "kind": "nymeria-user-data-snapshot",
        "created_at": created_at,
        "nymeria_version": _nymeria_version(),
        "checkpoint_backend": backend,
        "encrypted": encrypt,
        "includes_key": include_key,
        "includes_workspace": include_workspace,
        "hostname": socket.gethostname(),
    }

    partial = output.with_name(output.name + ".partial")
    tmp_dir = Path(tempfile.mkdtemp(prefix="nymeria-snapshot-"))
    try:
        # Fail fast on the Postgres side before writing anything.
        pg_dump_dir = tmp_dir / "postgres"
        if backend == "postgres":
            postgres_uri = getattr(settings, "postgres_uri", None)
            if not postgres_uri:
                raise SnapshotError(
                    "database_backend=postgres but POSTGRES_URI is not set"
                )
            postgres_summary = dump_postgres_checkpoints(postgres_uri, pg_dump_dir)

        with partial.open("wb") as raw:
            stream: Any
            if encrypt:
                stream = open_encrypting_writer(raw, passphrase or "")
            else:
                stream = raw
            try:
                with tarfile.open(
                    fileobj=stream, mode="w|gz", format=tarfile.PAX_FORMAT
                ) as tar:
                    _add_bytes(
                        tar,
                        META_MEMBER,
                        json.dumps(meta, indent=2).encode("utf-8"),
                        files_index,
                    )
                    sqlite_temp = tmp_dir / "sqlite"
                    for abs_path, rel in iter_data_dir_files(
                        data_dir, include_code_backups=include_code_backups
                    ):
                        arcname = DATA_PREFIX + rel
                        try:
                            if is_sqlite_file(abs_path):
                                copy_target = sqlite_temp / rel
                                sqlite_consistent_copy(abs_path, copy_target)
                                _add_file_snapshot(
                                    tar, copy_target, arcname, files_index
                                )
                                copy_target.unlink(missing_ok=True)
                            else:
                                _add_file_snapshot(tar, abs_path, arcname, files_index)
                        except (OSError, SnapshotStoreError) as exc:
                            # A vanished scratch file must not kill the whole
                            # snapshot, but the operator has to see the gap.
                            warnings.append(f"Skipped {rel}: {exc}")

                    if backend == "postgres":
                        for table in POSTGRES_CHECKPOINT_TABLES:
                            dump_file = pg_dump_dir / f"{table}.copy"
                            _add_file_snapshot(
                                tar,
                                dump_file,
                                f"{POSTGRES_PREFIX}{table}.copy",
                                files_index,
                            )

                    if include_workspace:
                        workspace = _workspace_dir()
                        for sub in WORKSPACE_SUBDIRS:
                            root = workspace / sub
                            if not root.is_dir():
                                continue
                            for file_path in sorted(root.rglob("*")):
                                if not file_path.is_file() or file_path.is_symlink():
                                    continue
                                rel = file_path.relative_to(workspace).as_posix()
                                try:
                                    _add_file_snapshot(
                                        tar,
                                        file_path,
                                        WORKSPACE_PREFIX + rel,
                                        files_index,
                                    )
                                except OSError as exc:
                                    warnings.append(f"Skipped workspace/{rel}: {exc}")

                    if include_key and secrets_key:
                        _add_bytes(
                            tar,
                            SECRETS_MEMBER,
                            secrets_key.encode("ascii"),
                            files_index,
                        )

                    manifest = {
                        **meta,
                        "postgres": postgres_summary,
                        "counts": _count_manifest_stats(
                            data_dir, files_index, postgres_summary
                        ),
                        "warnings": warnings,
                        "files": files_index,
                    }
                    _add_bytes(
                        tar,
                        MANIFEST_MEMBER,
                        json.dumps(manifest, indent=2).encode("utf-8"),
                        None,
                    )
            finally:
                if encrypt:
                    stream.close()
            raw.flush()
            os.fsync(raw.fileno())
        partial.replace(output)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return SnapshotResult(artifact=output, manifest=manifest, warnings=warnings)


def _count_manifest_stats(
    data_dir: Path,
    files_index: Dict[str, Dict[str, Any]],
    postgres_summary: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Summary counts for the manifest (informational, never fatal)."""
    counts: Dict[str, Any] = {
        "data_files": sum(1 for name in files_index if name.startswith(DATA_PREFIX)),
        "workspace_files": sum(
            1 for name in files_index if name.startswith(WORKSPACE_PREFIX)
        ),
    }
    counts.update(_sqlite_counts_from_live(data_dir))
    if postgres_summary:
        counts["checkpoint_rows"] = postgres_summary.get("row_counts", {}).get(
            "checkpoints"
        )
    return counts


def _sqlite_counts_from_live(data_dir: Path) -> Dict[str, Optional[int]]:
    # Counts are read from the live DBs (read-only, cheap) rather than the
    # temp copies, which are deleted as they stream into the tar.
    return _sqlite_counts(data_dir)


# ---------------------------------------------------------------------------
# Read / extract
# ---------------------------------------------------------------------------


def _open_artifact_stream(artifact: Path, passphrase: Optional[str]) -> BinaryIO:
    kind = sniff_artifact(artifact)
    if kind is None:
        raise SnapshotError(f"{artifact} is not a snapshot artifact")
    raw = artifact.open("rb")
    if kind == "encrypted":
        if not passphrase:
            raw.close()
            raise SnapshotError(
                "Artifact is encrypted; a passphrase is required "
                "(--passphrase-file, NYMERIA_SNAPSHOT_PASSPHRASE, or prompt)"
            )
        try:
            return open_decrypting_reader(raw, passphrase)  # type: ignore[return-value]
        except SnapshotDecryptError:
            raw.close()
            raise
    return raw


def _safe_member_target(root: Path, name: str) -> Path:
    parts = Path(name).parts
    if not parts or name.startswith("/") or ".." in parts:
        raise SnapshotError(f"Unsafe member path in artifact: {name!r}")
    allowed = (
        name in (META_MEMBER, MANIFEST_MEMBER, SECRETS_MEMBER)
        or name.startswith((DATA_PREFIX, POSTGRES_PREFIX, WORKSPACE_PREFIX))
    )
    if not allowed:
        raise SnapshotError(f"Unexpected member in artifact: {name!r}")
    return root / Path(*parts)


def read_snapshot_meta(
    artifact: Path, passphrase: Optional[str] = None
) -> Dict[str, Any]:
    """Read the leading ``meta.json`` without streaming the whole artifact."""
    with closing(_open_artifact_stream(artifact, passphrase)) as stream:
        with tarfile.open(fileobj=stream, mode="r|gz") as tar:
            for member in tar:
                if member.name != META_MEMBER:
                    raise SnapshotError(
                        f"Artifact does not start with {META_MEMBER}; "
                        "not a snapshot artifact or an unsupported version"
                    )
                fileobj = tar.extractfile(member)
                if fileobj is None:
                    raise SnapshotError("Could not read snapshot meta member")
                try:
                    return json.loads(fileobj.read().decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise SnapshotError(f"Corrupt snapshot meta: {exc}") from exc
    raise SnapshotError("Empty snapshot artifact")


def extract_snapshot(
    artifact: Path,
    passphrase: Optional[str],
    workdir: Path,
) -> ExtractedSnapshot:
    """Stream the artifact into ``workdir``, hashing every member.

    Members are written under their archive paths after traversal checks
    (the encrypted envelope authenticates content, but plain artifacts get
    the same treatment). Returns the parsed meta + manifest and any
    hash-mismatch failures against the manifest.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    digests: Dict[str, str] = {}
    sizes: Dict[str, int] = {}
    meta: Optional[Dict[str, Any]] = None
    manifest: Optional[Dict[str, Any]] = None

    with closing(_open_artifact_stream(artifact, passphrase)) as stream:
        with tarfile.open(fileobj=stream, mode="r|gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                target = _safe_member_target(workdir, member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                fileobj = tar.extractfile(member)
                if fileobj is None:
                    raise SnapshotError(f"Unreadable member: {member.name}")
                digest = hashlib.sha256()
                size = 0
                with target.open("wb") as out:
                    while True:
                        chunk = fileobj.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                        out.write(chunk)
                try:
                    os.utime(target, (member.mtime, member.mtime))
                except OSError:
                    pass  # mtime preservation is best-effort
                digests[member.name] = digest.hexdigest()
                sizes[member.name] = size
            # Drain past the tar end-of-archive padding so a truncated
            # encrypted artifact (missing frames after the last member)
            # still fails authentication here, not silently.
            while stream.read(1024 * 1024):
                pass

    meta_path = workdir / META_MEMBER
    manifest_path = workdir / MANIFEST_MEMBER
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"Snapshot is missing or has corrupt meta/manifest: {exc}")

    failures: List[str] = []
    recorded = manifest.get("files", {})
    for name, entry in recorded.items():
        if name == META_MEMBER:
            # meta is re-serialized identically, but guard anyway
            pass
        if name not in digests:
            failures.append(f"missing member: {name}")
            continue
        if digests[name] != entry.get("sha256"):
            failures.append(f"sha256 mismatch: {name}")
        elif sizes[name] != entry.get("size"):
            failures.append(f"size mismatch: {name}")
    for name in digests:
        if name not in recorded and name != MANIFEST_MEMBER:
            failures.append(f"member not in manifest: {name}")

    return ExtractedSnapshot(
        root=workdir, meta=meta, manifest=manifest, hash_failures=failures
    )


# ---------------------------------------------------------------------------
# Liveness probe (restore safety)
# ---------------------------------------------------------------------------


def stack_liveness_signs(settings: Any) -> List[str]:
    """Reasons to believe the stack is currently running. Empty = looks down."""
    signs: List[str] = []
    port = int(getattr(settings, "api_port", 8000) or 8000)
    for url in _health_probe_urls(port):
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                if resp.status < 500:
                    signs.append(f"API responded at {url}")
        except (urllib.error.URLError, OSError, ValueError):
            continue
    try:
        from .service_health import (
            DEFAULT_MAX_AGE_SECONDS,
            HEARTBEAT_SERVICES,
            heartbeat_path,
        )

        now = datetime.now(timezone.utc).timestamp()
        for service in sorted(HEARTBEAT_SERVICES):
            path = heartbeat_path(service)
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age < DEFAULT_MAX_AGE_SECONDS:
                signs.append(f"Fresh {service} heartbeat ({int(age)}s old)")
    except Exception:  # noqa: BLE001 - heartbeat probing is best-effort
        pass
    return signs


def _health_probe_urls(port: int) -> List[str]:
    # The compose-internal hostname matters: the documented Docker restore
    # runs in a one-off `docker compose run` container with an isolated
    # loopback, where 127.0.0.1 can never see a still-running api container
    # but `nymeria-api` resolves on the shared network. On non-Docker hosts
    # the name fails DNS immediately, so the extra probe costs nothing.
    urls = [
        f"http://127.0.0.1:{port}/health",
        "http://nymeria-api:8000/health",
    ]
    api_url = os.environ.get("NYMERIA_API_URL")
    if api_url:
        urls.append(api_url.rstrip("/") + "/health")
    return urls


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


@dataclass
class RestoreReport:
    """What a restore did, for CLI rendering."""

    data_moved_aside: Optional[Path]
    postgres_rows: Optional[Dict[str, int]]
    key_status: str
    key_value: Optional[str]
    warnings: List[str] = field(default_factory=list)


def restore_snapshot(
    settings: Any,
    extracted: ExtractedSnapshot,
    *,
    write_env: Optional[Path] = None,
) -> RestoreReport:
    """Swap an extracted snapshot into place. Callers verify + confirm first.

    Order: Postgres first (transactional; fails without touching files), then
    the data-dir content swap (pure same-filesystem renames), then workspace,
    then vault-key reconciliation. The previous data dir contents are moved
    into ``.pre-restore-<ts>/`` inside the data dir, never deleted.
    """
    data_dir = Path(settings.data_dir)
    manifest = extracted.manifest
    warnings: List[str] = []

    if extracted.hash_failures:
        # Callers are expected to verify first, but never swap live data for
        # content that failed its manifest digests regardless.
        preview = "; ".join(extracted.hash_failures[:3])
        raise SnapshotError(
            f"Refusing to restore: extraction digest failures ({preview})"
        )

    backend = getattr(settings, "database_backend", "sqlite")
    snap_backend = manifest.get("checkpoint_backend")
    if snap_backend != backend:
        raise SnapshotError(
            f"Snapshot was taken with checkpoint_backend={snap_backend!r} but "
            f"this deployment uses {backend!r}. Cross-backend restore is not "
            "supported; configure the matching backend first."
        )

    postgres_rows: Optional[Dict[str, int]] = None
    if backend == "postgres":
        postgres_uri = getattr(settings, "postgres_uri", None)
        if not postgres_uri:
            raise SnapshotError("database_backend=postgres but POSTGRES_URI unset")
        dump_dir = extracted.root / POSTGRES_PREFIX.rstrip("/")
        expected = (manifest.get("postgres") or {}).get("migration_version")
        try:
            postgres_rows = restore_postgres_checkpoints(
                postgres_uri,
                dump_dir,
                expected_migration_version=expected,
            )
        except CheckpointSchemaMissing:
            # Fresh restore target: let LangGraph create its schema, then
            # load the dump into it.
            ensure_postgres_checkpoint_schema(postgres_uri)
            postgres_rows = restore_postgres_checkpoints(
                postgres_uri,
                dump_dir,
                expected_migration_version=expected,
            )

    # --- data dir content swap -------------------------------------------
    stamp = _utc_stamp()
    pre_restore = data_dir / f".pre-restore-{stamp}"
    pre_restore.mkdir(parents=True, exist_ok=False)
    extracted_root = extracted.root.resolve()
    for entry in sorted(data_dir.iterdir(), key=lambda p: p.name):
        if entry.resolve() == extracted_root or entry == pre_restore:
            continue
        if entry.name.startswith(".pre-restore-"):
            continue
        entry.rename(pre_restore / entry.name)

    restored_data = extracted.root / DATA_PREFIX.rstrip("/")
    if restored_data.is_dir():
        for entry in sorted(restored_data.iterdir(), key=lambda p: p.name):
            _move_into(entry, data_dir / entry.name)

    # --- workspace ---------------------------------------------------------
    restored_workspace = extracted.root / WORKSPACE_PREFIX.rstrip("/")
    if restored_workspace.is_dir():
        workspace = _workspace_dir()
        for sub in WORKSPACE_SUBDIRS:
            src = restored_workspace / sub
            if not src.is_dir():
                continue
            # Per-subtree isolation: one failed subtree must not abort the
            # others. The move-aside crosses volumes in Docker (/workspace
            # and /data are separate mounts), so it must go through the
            # cross-filesystem-safe _move_into, never a raw rename.
            try:
                workspace.mkdir(parents=True, exist_ok=True)
                target = workspace / sub
                if target.exists():
                    _move_into(target, pre_restore / f"workspace-{sub}")
                _move_into(src, target)
            except OSError as exc:
                warnings.append(f"Workspace restore incomplete for {sub}: {exc}")

    # --- vault key reconciliation ------------------------------------------
    key_status, key_value = _reconcile_secrets_key(
        extracted.root / SECRETS_MEMBER, write_env=write_env, warnings=warnings
    )

    # --- clean the (now mostly empty) staging dir --------------------------
    shutil.rmtree(extracted.root, ignore_errors=True)

    return RestoreReport(
        data_moved_aside=pre_restore,
        postgres_rows=postgres_rows,
        key_status=key_status,
        key_value=key_value,
        warnings=warnings,
    )


def _move_into(src: Path, dst: Path) -> None:
    """Rename ``src`` to ``dst``, falling back to copy across filesystems."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.rename(dst)
    except OSError:
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
            shutil.rmtree(src, ignore_errors=True)
        else:
            shutil.copy2(src, dst)
            src.unlink(missing_ok=True)


def _reconcile_secrets_key(
    key_file: Path,
    *,
    write_env: Optional[Path],
    warnings: List[str],
) -> Tuple[str, Optional[str]]:
    """Compare the artifact's Fernet key with the environment.

    Returns ``(status, key_value)`` where status is one of ``match``,
    ``absent`` (artifact has no key), ``env-missing`` (key printed/written),
    ``mismatch`` (env has a DIFFERENT key; the restored vault needs the
    artifact's). The key is written to an env file only when the operator
    passes ``--write-env`` explicitly.
    """
    if not key_file.is_file():
        return "absent", None
    artifact_key = key_file.read_text(encoding="ascii").strip()
    env_key = os.environ.get(SECRETS_KEY_ENV_VAR, "").strip()

    if env_key and env_key == artifact_key:
        return "match", None

    status = "env-missing" if not env_key else "mismatch"
    if write_env is not None:
        from ..config.env_file import format_env_value, write_env_file

        write_env_file(
            write_env,
            [(SECRETS_KEY_ENV_VAR, format_env_value(artifact_key))],
            merge=True,
        )
        warnings.append(
            f"Wrote {SECRETS_KEY_ENV_VAR} from the snapshot into {write_env}."
        )
        if status == "mismatch":
            warnings.append(
                "The environment previously held a DIFFERENT vault key; "
                "anything encrypted with the old key is now undecryptable."
            )
        return status, None
    return status, artifact_key


__all__ = [
    "DATA_PREFIX",
    "ENCRYPTED_SUFFIX",
    "ExtractedSnapshot",
    "MANIFEST_MEMBER",
    "META_MEMBER",
    "PLAIN_SUFFIX",
    "POSTGRES_PREFIX",
    "RestoreReport",
    "SECRETS_MEMBER",
    "SnapshotError",
    "SnapshotResult",
    "WORKSPACE_PREFIX",
    "WORKSPACE_SUBDIRS",
    "create_snapshot",
    "extract_snapshot",
    "read_snapshot_meta",
    "restore_snapshot",
    "stack_liveness_signs",
]
