"""Store-level capture and restore primitives for user-data snapshots.

Three concerns, each consistency-aware for a LIVE system (the api and worker
keep running during an online snapshot; see the persistence inventory in the
backup-and-restore doc):

- SQLite: every ``*.db`` under the data dir is copied with the sqlite3 online
  backup API, which produces a transactionally consistent copy for both WAL
  databases (``accounts.db``) and the rollback-journal ones that api and
  worker write concurrently (``todo_schedule.db``, per-user ``memory.db``).
  A raw file copy of those can capture a torn state; the backup API cannot.
- Plain files: JSON stores are written via atomic replace by their managers,
  so a per-file fd snapshot (open first, fstat the fd, read exactly that
  inode) is always a complete old-or-new version, never a partial.
- Postgres (Docker shape): the four LangGraph checkpoint tables are dumped
  inside ONE ``REPEATABLE READ`` transaction via ``COPY ... TO STDOUT``, so
  the tables are consistent with each other. This is pure psycopg; the app
  image ships no ``pg_dump`` binary and does not need one.

Nothing here imports settings or the agent runtime; callers inject paths and
connection factories, which is also the test seam.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

SQLITE_MAGIC = b"SQLite format 3\x00"

# LangGraph's checkpoint schema, in restore order (migrations first so a
# half-restored database is detectably version-stamped). Dump order is the
# same; each table is one COPY file inside the artifact.
POSTGRES_CHECKPOINT_TABLES = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)

# Data-dir entries never captured. `snapshots` is the artifact output dir
# itself; `backups` is the self-modification source-file rollback store
# (opt-in via include_code_backups); `logs`/`flags` are ephemeral; `voice` is
# a regenerable synthesis cache. Hidden `.pre-restore-*`/`.snapshot-*` dirs
# are prior restore leftovers and in-flight staging.
DEFAULT_EXCLUDED_TOP_LEVEL = frozenset({"snapshots", "logs", "flags", "voice"})
CODE_BACKUPS_DIR = "backups"

_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class SnapshotStoreError(RuntimeError):
    """A store could not be captured or restored."""


class CheckpointSchemaMissing(SnapshotStoreError):
    """The restore target lacks the LangGraph checkpoint tables.

    Raised as its own type so the restore orchestrator can run the schema
    setup and retry without matching on message text.
    """


def is_sqlite_file(path: Path) -> bool:
    """True when ``path`` starts with the SQLite magic header."""
    try:
        with path.open("rb") as fh:
            return fh.read(len(SQLITE_MAGIC)) == SQLITE_MAGIC
    except OSError:
        return False


def is_sqlite_sidecar(path: Path) -> bool:
    """True for ``-wal``/``-shm``/``-journal`` companions of a SQLite DB."""
    return any(path.name.endswith(suffix) for suffix in _SQLITE_SIDECAR_SUFFIXES)


def sqlite_consistent_copy(source: Path, target: Path) -> None:
    """Copy a SQLite database with the online backup API.

    Safe against concurrent writers in either journal mode: the backup API
    takes a proper read snapshot and restarts if a writer invalidates it.
    The 30s timeout matches the busiest existing writer configuration
    (``todo_schedule_db``). The resulting copy is a standalone database with
    no ``-wal``/``-shm`` dependency.

    The source is opened read-only first: a plain ``connect`` on a path that
    vanished mid-walk would silently CREATE an empty database, capture it as
    a valid-but-empty copy, and resurrect the empty file at the live path.
    Read-only fails instead, which the caller turns into a warned skip. The
    read-write fallback exists only for a database that provably still
    exists but cannot be opened read-only (an unclean-shutdown WAL needing
    recovery); the existence re-check keeps the create-on-connect hazard out
    of that path too.
    """
    target.parent.mkdir(parents=True, exist_ok=True)

    def _backup(connect_target: str, *, uri: bool) -> None:
        with closing(
            sqlite3.connect(connect_target, uri=uri, timeout=30.0)
        ) as src, closing(sqlite3.connect(str(target), timeout=30.0)) as dst:
            src.backup(dst)

    try:
        try:
            _backup(f"file:{source}?mode=ro", uri=True)
            return
        except sqlite3.Error:
            if not source.exists():
                raise SnapshotStoreError(
                    f"SQLite database vanished during capture: {source}"
                ) from None
        # Read-only failed but the file provably exists (e.g. an
        # unclean-shutdown WAL needing recovery): retry read-write.
        target.unlink(missing_ok=True)
        _backup(str(source), uri=False)
    except sqlite3.Error as exc:
        raise SnapshotStoreError(
            f"SQLite backup of {source} failed: {exc}"
        ) from exc


def iter_data_dir_files(
    data_dir: Path,
    *,
    include_code_backups: bool = False,
) -> Iterator[Tuple[Path, str]]:
    """Yield ``(absolute_path, relative_arcname)`` for every capturable file.

    Walk-based on purpose: new stores added to the data dir later are picked
    up automatically, with only the explicit exclusions above skipped. SQLite
    sidecars are skipped (the backup API copies are self-contained); the
    caller decides per-file whether to route through the backup API by
    checking :func:`is_sqlite_file`.
    """
    data_dir = data_dir.resolve()
    excluded = set(DEFAULT_EXCLUDED_TOP_LEVEL)
    if not include_code_backups:
        excluded.add(CODE_BACKUPS_DIR)

    def walk(directory: Path) -> Iterator[Tuple[Path, str]]:
        try:
            entries = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            logger.warning("Snapshot walk could not list %s: %s", directory, exc)
            return
        for entry in entries:
            rel = entry.relative_to(data_dir)
            top = rel.parts[0]
            if top in excluded or top.startswith((".pre-restore-", ".snapshot-")):
                continue
            if entry.is_symlink():
                # The data dir should not contain symlinks; a planted one
                # could otherwise exfiltrate host files into the artifact.
                logger.warning("Snapshot skipping symlink %s", entry)
                continue
            if entry.is_dir():
                yield from walk(entry)
            elif entry.is_file():
                if is_sqlite_sidecar(entry) or entry.name.endswith(".tmp"):
                    continue
                yield entry, rel.as_posix()

    yield from walk(data_dir)


# ---------------------------------------------------------------------------
# Postgres checkpoint dump/restore (Docker shape)
# ---------------------------------------------------------------------------

ConnectFn = Callable[[str], object]


def _default_connect(uri: str):
    import psycopg  # type: ignore[import-untyped]  # local import, optional dep

    return psycopg.connect(uri)


def _migration_version(cur) -> Optional[int]:
    cur.execute("SELECT max(v) FROM checkpoint_migrations")
    row = cur.fetchone()
    return None if row is None or row[0] is None else int(row[0])


def dump_postgres_checkpoints(
    postgres_uri: str,
    out_dir: Path,
    *,
    connect: ConnectFn = _default_connect,
) -> Dict[str, Any]:
    """Dump the LangGraph checkpoint tables to ``<table>.copy`` files.

    All tables are read inside one ``REPEATABLE READ`` transaction so the dump
    is cross-table consistent. Returns a summary dict recorded in the
    manifest: ``{"migration_version": int, "row_counts": {table: int}}``.

    Raises :class:`SnapshotStoreError` when the database is unreachable or the
    checkpoint schema is missing; a snapshot silently missing conversation
    history would be worse than a failed one.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    row_counts: Dict[str, int] = {}
    try:
        with closing(connect(postgres_uri)) as conn:  # type: ignore[type-var]
            with conn.cursor() as cur:  # type: ignore[attr-defined]
                cur.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                from .checkpoint_sql import postgres_table_exists

                for table in POSTGRES_CHECKPOINT_TABLES:
                    if not postgres_table_exists(cur, table):
                        raise SnapshotStoreError(
                            f"Postgres checkpoint table {table} does not exist; "
                            "cannot capture conversation history"
                        )
                version = _migration_version(cur)
                for table in POSTGRES_CHECKPOINT_TABLES:
                    target = out_dir / f"{table}.copy"
                    rows = 0
                    with target.open("wb") as fh:
                        with cur.copy(f"COPY {table} TO STDOUT") as copy:
                            for chunk in copy:
                                data = bytes(chunk)
                                rows += data.count(b"\n")
                                fh.write(data)
                    row_counts[table] = rows
            conn.rollback()  # type: ignore[attr-defined]
    except SnapshotStoreError:
        raise
    except Exception as exc:  # noqa: BLE001 - driver errors vary by backend
        raise SnapshotStoreError(f"Postgres checkpoint dump failed: {exc}") from exc
    return {"migration_version": version, "row_counts": row_counts}


def restore_postgres_checkpoints(
    postgres_uri: str,
    dump_dir: Path,
    *,
    expected_migration_version: Optional[int],
    connect: ConnectFn = _default_connect,
) -> Dict[str, int]:
    """Load ``<table>.copy`` dumps back into Postgres.

    The target schema must already exist (a fresh host runs LangGraph's own
    ``PostgresSaver.setup()`` first; ``core/snapshot.py`` handles that) and its
    migration version must EQUAL the version recorded in the manifest,
    otherwise the row shapes may not match and the restore refuses. All
    truncates and loads happen in one transaction: the target ends fully
    restored or untouched.
    """
    missing = [
        table
        for table in POSTGRES_CHECKPOINT_TABLES
        if not (dump_dir / f"{table}.copy").exists()
    ]
    if missing:
        raise SnapshotStoreError(
            f"Snapshot is missing checkpoint dump(s): {', '.join(missing)}"
        )
    row_counts: Dict[str, int] = {}
    try:
        with closing(connect(postgres_uri)) as conn:  # type: ignore[type-var]
            with conn.cursor() as cur:  # type: ignore[attr-defined]
                from .checkpoint_sql import postgres_table_exists

                for table in POSTGRES_CHECKPOINT_TABLES:
                    if not postgres_table_exists(cur, table):
                        raise CheckpointSchemaMissing(
                            f"Postgres checkpoint table {table} does not exist "
                            "on the restore target; run the schema setup first"
                        )
                target_version = _migration_version(cur)
                if (
                    expected_migration_version is not None
                    and target_version != expected_migration_version
                ):
                    raise SnapshotStoreError(
                        "Checkpoint schema version mismatch: snapshot was taken "
                        f"at migration {expected_migration_version}, the target "
                        f"is at {target_version}. Install the matching langgraph "
                        "checkpoint version before restoring."
                    )
                tables_sql = ", ".join(POSTGRES_CHECKPOINT_TABLES)
                cur.execute(f"TRUNCATE {tables_sql}")
                for table in POSTGRES_CHECKPOINT_TABLES:
                    rows = 0
                    with (dump_dir / f"{table}.copy").open("rb") as fh:
                        with cur.copy(f"COPY {table} FROM STDIN") as copy:
                            while True:
                                chunk = fh.read(1024 * 1024)
                                if not chunk:
                                    break
                                rows += chunk.count(b"\n")
                                copy.write(chunk)
                    row_counts[table] = rows
            conn.commit()  # type: ignore[attr-defined]
    except SnapshotStoreError:
        raise
    except Exception as exc:  # noqa: BLE001 - driver errors vary by backend
        raise SnapshotStoreError(f"Postgres checkpoint restore failed: {exc}") from exc
    return row_counts


def ensure_postgres_checkpoint_schema(postgres_uri: str) -> None:
    """Create the LangGraph checkpoint schema on a fresh restore target.

    Delegates to LangGraph's own ``PostgresSaver.setup()`` so the schema
    always matches the installed library version (which the migration-version
    guard above then compares against the snapshot).
    """
    try:
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(postgres_uri) as saver:
            saver.setup()
    except Exception as exc:  # noqa: BLE001 - surface a single actionable error
        raise SnapshotStoreError(
            f"Could not create the checkpoint schema on the target: {exc}"
        ) from exc


def sqlite_integrity_ok(path: Path) -> Tuple[bool, str]:
    """Run ``PRAGMA integrity_check`` on a database copy."""
    try:
        with closing(
            sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
        ) as conn:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        return False, str(exc)
    messages: List[str] = [str(row[0]) for row in rows]
    if messages == ["ok"]:
        return True, "ok"
    return False, "; ".join(messages) or "no integrity result"


__all__ = [
    "CODE_BACKUPS_DIR",
    "CheckpointSchemaMissing",
    "DEFAULT_EXCLUDED_TOP_LEVEL",
    "POSTGRES_CHECKPOINT_TABLES",
    "SQLITE_MAGIC",
    "SnapshotStoreError",
    "dump_postgres_checkpoints",
    "ensure_postgres_checkpoint_schema",
    "is_sqlite_file",
    "is_sqlite_sidecar",
    "iter_data_dir_files",
    "restore_postgres_checkpoints",
    "sqlite_consistent_copy",
    "sqlite_integrity_ok",
]
