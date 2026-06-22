"""Shared table-existence probes for LangGraph checkpoint tables.

Every module that touches the checkpoint store with raw SQL needs the same
"does this table exist?" guard before querying: the status lookups, the
whole-thread cleanup, branch cloning, the `nymeria doctor` diagnostics, and
the CLI in-process transport each had a private copy. Centralizing the two
probes here keeps the checkpoint-table contract (the SQLite ``sqlite_master``
query and the PostgreSQL ``to_regclass`` query) in one place, so a schema or
backend change touches one file.

This module intentionally imports nothing from the package (only ``sqlite3``
and ``typing``) so any checkpoint-touching module can import it without a
cycle. ``postgres_table_exists`` takes an already-open psycopg cursor, so the
optional ``psycopg`` dependency stays at the call site, not here.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if ``table`` exists in the SQLite database behind ``conn``."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def postgres_table_exists(cur: Any, table: str) -> bool:
    """Return True if ``table`` exists in the connected PostgreSQL database.

    ``cur`` is an open psycopg cursor. ``to_regclass`` resolves a relation name
    to its OID and yields NULL when the table is absent. The empty-row guard is
    defensive: ``to_regclass`` always returns exactly one row, but tolerating a
    missing row keeps every call site safe.
    """
    cur.execute("SELECT to_regclass(%s)", (table,))
    row = cur.fetchone()
    return bool(row and row[0] is not None)
