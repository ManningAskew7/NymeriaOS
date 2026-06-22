"""Lightweight checkpoint status lookups.

The status endpoint only needs an opaque revision marker. Querying
``checkpoint_id`` directly avoids loading or deserializing checkpoint blobs.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .checkpoint_sql import postgres_table_exists, sqlite_table_exists

logger = logging.getLogger(__name__)


def has_direct_checkpoint_revision_backend(settings: Any) -> bool:
    """Return true when the configured backend supports a raw revision query."""
    return getattr(settings, "database_backend", "sqlite") in {"sqlite", "postgres"}


def get_latest_checkpoint_revision(settings: Any, thread_id: str) -> str | None:
    """Return the latest checkpoint ID for a thread without reading blobs."""
    backend = getattr(settings, "database_backend", "sqlite")
    if backend == "sqlite":
        return get_sqlite_latest_checkpoint_revision(Path(settings.db_path), thread_id)
    if backend == "postgres":
        postgres_uri = getattr(settings, "postgres_uri", None)
        if not postgres_uri:
            logger.warning("Postgres checkpoint backend selected without POSTGRES_URI")
            return None
        return get_postgres_latest_checkpoint_revision(postgres_uri, thread_id)
    return None


def get_sqlite_latest_checkpoint_revision(
    db_path: Path,
    thread_id: str,
) -> str | None:
    """Return the latest SQLite checkpoint ID for a thread."""
    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            if not sqlite_table_exists(conn, "checkpoints"):
                return None
            row = conn.execute(
                "SELECT checkpoint_id FROM checkpoints "
                "WHERE thread_id = ? AND checkpoint_ns = '' "
                "ORDER BY checkpoint_id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
    except Exception as e:
        logger.warning("Failed to query SQLite checkpoint status for %s: %s", thread_id, e)
        return None
    return _coerce_revision(row[0] if row else None)


def get_postgres_latest_checkpoint_revision(
    postgres_uri: str,
    thread_id: str,
) -> str | None:
    """Return the latest PostgreSQL checkpoint ID for a thread."""
    try:
        import psycopg  # type: ignore[import-untyped]

        with psycopg.connect(postgres_uri) as conn:
            with conn.cursor() as cur:
                if not postgres_table_exists(cur, "checkpoints"):
                    return None
                cur.execute(
                    "SELECT checkpoint_id FROM checkpoints "
                    "WHERE thread_id = %s AND checkpoint_ns = '' "
                    "ORDER BY checkpoint_id DESC LIMIT 1",
                    (thread_id,),
                )
                row = cur.fetchone()
    except Exception as e:
        logger.warning("Failed to query PostgreSQL checkpoint status for %s: %s", thread_id, e)
        return None
    return _coerce_revision(row[0] if row else None)


def get_graph_state_revision(agent: Any, thread_id: str) -> str | None:
    """Fallback revision lookup for non-SQL checkpoint backends."""
    graph = getattr(agent, "_default_graph", None)
    if graph is None:
        return None
    try:
        state = graph.get_state({"configurable": {"thread_id": thread_id}})
    except Exception as e:
        logger.warning("Failed to query graph state revision for %s: %s", thread_id, e)
        return None
    config = getattr(state, "config", None) or {}
    configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
    return _coerce_revision(configurable.get("checkpoint_id"))


def _coerce_revision(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
