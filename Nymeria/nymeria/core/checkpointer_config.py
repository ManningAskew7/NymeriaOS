"""Checkpointer configuration builders used during NymeriaAgent init.

Extracted from ``NymeriaAgent``. Each function takes ``Settings`` as
its only argument and returns either a ``CheckpointerConfig`` (for
the sync/async builders) or a ``List[str]`` (for the thread-id
enumerator). The agent imports them and invokes them from
``__init__`` -- there are no other callers anywhere.

Behavior is unchanged: backend selection follows
``settings.database_backend`` ("postgres" | "sqlite" | "memory"),
PostgreSQL requires ``settings.postgres_uri``, and SQLite uses
``settings.db_path``. The async builder routes SQLite traffic to the
shared async checkpoint wrapper around Nymeria's durable sync saver
(WAL mode for concurrent read/write).
"""

from __future__ import annotations

import logging
from typing import List

from ..config import Settings
from ..vendor.react_agent import CheckpointerConfig

logger = logging.getLogger(__name__)


def build_checkpointer_config(settings: Settings) -> CheckpointerConfig:
    """Build the (sync) checkpointer configuration."""
    backend = settings.database_backend

    if backend == "postgres":
        if not settings.postgres_uri:
            raise ValueError("POSTGRES_URI required when database_backend=postgres")
        logger.info("Using PostgreSQL for conversation persistence")
        return CheckpointerConfig(
            backend="postgres",
            postgres_uri=settings.postgres_uri,
        )
    elif backend == "sqlite":
        db_path = settings.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Using SQLite for conversation persistence: {db_path}")
        return CheckpointerConfig(
            backend="sqlite",
            sqlite_path=str(db_path),
        )
    else:  # memory
        logger.info("Using in-memory storage (conversations will not persist)")
        return CheckpointerConfig(backend="memory")


def build_async_checkpointer_config(settings: Settings) -> CheckpointerConfig:
    """Build async checkpointer config for async streaming.

    Uses the shared async checkpoint wrapper around Nymeria's durable
    sync saver, ensuring sync and async paths share one serialization
    path and checkpoint store. WAL mode enables concurrent SQLite
    read/write access.
    """
    backend = settings.database_backend

    if backend == "postgres":
        if not settings.postgres_uri:
            raise ValueError("POSTGRES_URI required when database_backend=postgres")
        return CheckpointerConfig(
            backend="postgres",
            postgres_uri=settings.postgres_uri,
        )
    elif backend == "sqlite":
        db_path = settings.db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return CheckpointerConfig(
            backend="sqlite_async",
            sqlite_path=str(db_path),
        )
    else:  # memory
        return CheckpointerConfig(backend="memory")


def enumerate_checkpoint_thread_ids(settings: Settings) -> List[str]:
    """Return distinct thread_ids present in the checkpoint database.

    Used by the startup ownership backfill. Tolerates missing tables
    and connection errors (returns empty list with a warning).
    """
    backend = settings.database_backend
    if backend == "sqlite":
        import sqlite3 as _sqlite3
        try:
            conn = _sqlite3.connect(str(settings.db_path))
            try:
                rows = conn.execute(
                    "SELECT DISTINCT thread_id FROM checkpoints"
                ).fetchall()
                return [r[0] for r in rows]
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            logger.warning("Enumerate checkpoint thread_ids (sqlite) failed: %s", e)
            return []
    if backend == "postgres":
        import psycopg  # type: ignore[import-untyped]
        if not settings.postgres_uri:
            return []
        try:
            with psycopg.connect(settings.postgres_uri) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
                    return [row[0] for row in cur.fetchall()]
        except Exception as e:  # noqa: BLE001
            logger.warning("Enumerate checkpoint thread_ids (postgres) failed: %s", e)
            return []
    return []
