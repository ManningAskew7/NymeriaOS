"""Shared cleanup helpers for LangGraph checkpoint tables.

LangGraph exposes checkpoint read/write helpers but not a public delete API.
Nymeria therefore uses raw SQL for whole-thread cleanup and post-compaction
pruning. Keep that SQL here so each checkpoint backend has one implementation.
"""

from __future__ import annotations

import logging
import sqlite3
from abc import ABC, abstractmethod
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from ..config import get_settings

logger = logging.getLogger(__name__)

CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


class CheckpointCleaner(ABC):
    """Backend-specific checkpoint cleanup interface."""

    @abstractmethod
    def delete_thread(self, thread_id: str) -> Dict[str, int]:
        """Delete all checkpoint rows for ``thread_id``."""

    @abstractmethod
    def prune_before(
        self,
        thread_id: str,
        boundary_checkpoint_id: str,
        floor_channel_versions: Mapping[str, Any],
    ) -> Tuple[int, int, int]:
        """Prune checkpoint history before ``boundary_checkpoint_id``."""


class NoopCheckpointCleaner(CheckpointCleaner):
    """Cleaner for in-memory/no-checkpoint persistence modes."""

    def delete_thread(self, thread_id: str) -> Dict[str, int]:
        return {"checkpoint_rows_remaining": 0}

    def prune_before(
        self,
        thread_id: str,
        boundary_checkpoint_id: str,
        floor_channel_versions: Mapping[str, Any],
    ) -> Tuple[int, int, int]:
        return (0, 0, 0)


class SQLiteCheckpointCleaner(CheckpointCleaner):
    """Checkpoint cleanup for LangGraph's SQLite checkpointer."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)

    def delete_thread(self, thread_id: str) -> Dict[str, int]:
        counts = _empty_delete_counts()
        with closing(sqlite3.connect(str(self.db_path))) as conn:
            existing_tables: list[str] = []
            for table in CHECKPOINT_TABLES:
                if not _sqlite_table_exists(conn, table):
                    continue
                cursor = conn.execute(_delete_thread_sql(table, "?"), (thread_id,))
                counts[f"{table}_deleted"] = _rowcount(cursor.rowcount)
                existing_tables.append(table)
            conn.commit()

            remaining = 0
            for table in existing_tables:
                row = conn.execute(_count_thread_sql(table, "?"), (thread_id,)).fetchone()
                remaining += int(row[0] or 0)
            counts["checkpoint_rows_remaining"] = remaining

        _raise_if_remaining(thread_id, counts["checkpoint_rows_remaining"])
        return counts

    def prune_before(
        self,
        thread_id: str,
        boundary_checkpoint_id: str,
        floor_channel_versions: Mapping[str, Any],
    ) -> Tuple[int, int, int]:
        try:
            with closing(sqlite3.connect(str(self.db_path))) as conn:
                cur = conn.cursor()
                counts = _prune_with_cursor(
                    cur,
                    "?",
                    thread_id,
                    boundary_checkpoint_id,
                    floor_channel_versions,
                )
                conn.commit()
                return counts
        except Exception as e:
            logger.warning(f"Thread {thread_id}: Checkpoint prune (sqlite) failed: {e}")
            return (0, 0, 0)


class PostgresCheckpointCleaner(CheckpointCleaner):
    """Checkpoint cleanup for LangGraph's Postgres checkpointer."""

    def __init__(self, postgres_uri: str):
        self.postgres_uri = postgres_uri

    def delete_thread(self, thread_id: str) -> Dict[str, int]:
        import psycopg  # type: ignore[import-untyped]

        counts = _empty_delete_counts()
        with psycopg.connect(self.postgres_uri) as conn:
            with conn.cursor() as cur:
                cur_any: Any = cur
                existing_tables: list[str] = []
                for table in CHECKPOINT_TABLES:
                    cur_any.execute("SELECT to_regclass(%s)", (table,))
                    if cur_any.fetchone()[0] is None:
                        continue
                    existing_tables.append(table)
                    cur_any.execute(_delete_thread_sql(table, "%s"), (thread_id,))
                    counts[f"{table}_deleted"] = _rowcount(cur_any.rowcount)
                conn.commit()

                remaining = 0
                for table in existing_tables:
                    cur_any.execute(_count_thread_sql(table, "%s"), (thread_id,))
                    remaining += int(cur_any.fetchone()[0] or 0)
                counts["checkpoint_rows_remaining"] = remaining

        _raise_if_remaining(thread_id, counts["checkpoint_rows_remaining"])
        return counts

    def prune_before(
        self,
        thread_id: str,
        boundary_checkpoint_id: str,
        floor_channel_versions: Mapping[str, Any],
    ) -> Tuple[int, int, int]:
        import psycopg  # type: ignore[import-untyped]

        try:
            with psycopg.connect(self.postgres_uri) as conn:
                with conn.cursor() as cur:
                    counts = _prune_with_cursor(
                        cur,
                        "%s",
                        thread_id,
                        boundary_checkpoint_id,
                        floor_channel_versions,
                    )
                conn.commit()
                return counts
        except Exception as e:
            logger.warning(f"Thread {thread_id}: Checkpoint prune (postgres) failed: {e}")
            return (0, 0, 0)


def get_checkpoint_cleaner(settings: Any | None = None) -> CheckpointCleaner:
    """Build the cleaner matching the configured checkpoint backend."""
    settings = settings or get_settings()
    backend = getattr(settings, "database_backend", "sqlite")
    if backend == "sqlite":
        return SQLiteCheckpointCleaner(Path(settings.db_path))
    if backend == "postgres":
        postgres_uri = getattr(settings, "postgres_uri", None)
        if not postgres_uri:
            raise RuntimeError("Postgres checkpoint backend selected without POSTGRES_URI")
        return PostgresCheckpointCleaner(postgres_uri)
    return NoopCheckpointCleaner()


def delete_thread_checkpoints(settings: Any, thread_id: str) -> Dict[str, int]:
    """Delete all checkpoint rows for a thread using the configured backend."""
    return get_checkpoint_cleaner(settings).delete_thread(thread_id)


def prune_checkpoints_before(
    thread_id: str,
    boundary_checkpoint_id: str,
    floor_channel_versions: Mapping[str, Any],
    *,
    settings: Any | None = None,
) -> Tuple[int, int, int]:
    """Delete pre-compact checkpoint/write/blob rows for a thread.

    Safe to call only while holding the thread lock AND only after the
    compact write has been verified. A prune failure must never fail the
    compaction that already succeeded, so connection/setup errors are logged
    and reported as zero deleted rows.
    """
    settings = settings or get_settings()
    try:
        return get_checkpoint_cleaner(settings).prune_before(
            thread_id,
            boundary_checkpoint_id,
            floor_channel_versions,
        )
    except Exception as e:
        backend = getattr(settings, "database_backend", "unknown")
        logger.warning(f"Thread {thread_id}: Checkpoint prune ({backend}) failed: {e}")
        return (0, 0, 0)


def _empty_delete_counts() -> Dict[str, int]:
    counts = {f"{table}_deleted": 0 for table in CHECKPOINT_TABLES}
    counts["checkpoint_rows_remaining"] = 0
    return counts


def _delete_thread_sql(table: str, placeholder: str) -> str:
    return f"DELETE FROM {table} WHERE thread_id = {placeholder}"


def _count_thread_sql(table: str, placeholder: str) -> str:
    return f"SELECT COUNT(*) FROM {table} WHERE thread_id = {placeholder}"


def _prune_writes_sql(placeholder: str) -> str:
    return (
        "DELETE FROM checkpoint_writes "
        f"WHERE thread_id = {placeholder} AND checkpoint_ns = '' "
        f"AND checkpoint_id < {placeholder}"
    )


def _prune_checkpoints_sql(placeholder: str) -> str:
    return (
        "DELETE FROM checkpoints "
        f"WHERE thread_id = {placeholder} AND checkpoint_ns = '' "
        f"AND checkpoint_id < {placeholder}"
    )


def _prune_blobs_sql(placeholder: str) -> str:
    return (
        "DELETE FROM checkpoint_blobs "
        f"WHERE thread_id = {placeholder} AND channel = {placeholder} "
        f"AND CAST(version AS INTEGER) < {placeholder}"
    )


def _prune_with_cursor(
    cur: Any,
    placeholder: str,
    thread_id: str,
    boundary_checkpoint_id: str,
    floor_channel_versions: Mapping[str, Any],
) -> Tuple[int, int, int]:
    writes_deleted = _safe_prune_execute(
        cur,
        _prune_writes_sql(placeholder),
        (thread_id, boundary_checkpoint_id),
        thread_id,
        "checkpoint_writes",
    )
    checkpoints_deleted = _safe_prune_execute(
        cur,
        _prune_checkpoints_sql(placeholder),
        (thread_id, boundary_checkpoint_id),
        thread_id,
        "checkpoints",
    )

    blobs_deleted = 0
    for channel, floor_v in floor_channel_versions.items():
        try:
            floor_int = int(floor_v)
        except (TypeError, ValueError):
            continue
        blobs_deleted += _safe_prune_execute(
            cur,
            _prune_blobs_sql(placeholder),
            (thread_id, channel, floor_int),
            thread_id,
            f"blob channel={channel}",
        )

    return (checkpoints_deleted, writes_deleted, blobs_deleted)


def _safe_prune_execute(
    cur: Any,
    sql: str,
    params: tuple[Any, ...],
    thread_id: str,
    label: str,
) -> int:
    try:
        cur.execute(sql, params)
        return _rowcount(cur.rowcount)
    except Exception as e:
        logger.warning(f"Thread {thread_id}: prune {label} failed: {e}")
        return 0


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _rowcount(value: int | None) -> int:
    if value is None or value < 0:
        return 0
    return value


def _raise_if_remaining(thread_id: str, remaining: int) -> None:
    if remaining:
        raise RuntimeError(f"{remaining} checkpoint row(s) remain for {thread_id}")
