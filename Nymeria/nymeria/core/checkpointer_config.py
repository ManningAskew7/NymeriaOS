"""Checkpointer configuration builders used during NymeriaAgent init.

Extracted from ``NymeriaAgent``. Each function takes ``Settings`` as
its only argument and returns either a ``CheckpointerConfig`` (for
the sync/async builders) or a ``List[str]`` (for the thread-id
enumerator). The builders are invoked only from
``NymeriaAgent.__init__``; ``enumerate_checkpoint_thread_ids`` is the
shared canonical "distinct checkpoint thread ids" query and is also
called from the thread-list API route (``api/routers/threads.py``) and
the in-process CLI transport (``triggers/cli/transport/in_process.py``).

Backend selection follows ``settings.database_backend`` ("postgres" |
"sqlite" | "memory"), PostgreSQL requires ``settings.postgres_uri``,
and SQLite uses ``settings.db_path``. The async builder routes SQLite
traffic to the shared async checkpoint wrapper around Nymeria's durable
sync saver (WAL mode for concurrent read/write).
"""

from __future__ import annotations

import logging
from contextlib import closing
from typing import List

from ..config import Settings
from ..vendor.react_agent import CheckpointerConfig
from .checkpoint_sql import postgres_table_exists, sqlite_table_exists

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

    The canonical "distinct checkpoint thread ids" query, shared by the
    startup ownership backfill (``NymeriaAgent.__init__``), the thread-list
    API route, and the in-process CLI transport. A missing ``checkpoints``
    table (a fresh database) is treated as no threads and returns ``[]``
    silently via the shared table-existence guards; only a genuine query or
    connection failure logs a warning. Empty/NULL thread ids are filtered out
    and values are coerced to ``str``.

    SQLite uses ``contextlib.closing`` so the connection is closed on every
    path (a bare ``with sqlite3.connect(...)`` only commits/rolls back, it does
    not close); psycopg's own context manager closes the connection on exit.
    """
    backend = settings.database_backend
    if backend == "sqlite":
        import sqlite3 as _sqlite3

        try:
            with closing(_sqlite3.connect(str(settings.db_path))) as conn:
                if not sqlite_table_exists(conn, "checkpoints"):
                    return []
                rows = conn.execute(
                    "SELECT DISTINCT thread_id FROM checkpoints"
                ).fetchall()
                return [str(row[0]) for row in rows if row[0]]
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
                    if not postgres_table_exists(cur, "checkpoints"):
                        return []
                    cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
                    return [str(row[0]) for row in cur.fetchall() if row[0]]
        except Exception as e:  # noqa: BLE001
            logger.warning("Enumerate checkpoint thread_ids (postgres) failed: %s", e)
            return []
    return []
