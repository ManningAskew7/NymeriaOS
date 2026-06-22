"""Tests for the shared checkpoint-table existence probes.

These two helpers replaced five hand-copied ``_sqlite_table_exists`` definitions
and three open-coded ``to_regclass`` probes across the checkpoint-touching
modules (status, cleanup, branch, doctor, the CLI in-process transport).
"""

from __future__ import annotations

import sqlite3
from typing import Any

from nymeria.core.checkpoint_sql import postgres_table_exists, sqlite_table_exists


def test_sqlite_table_exists_true_for_present_table():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE checkpoints (thread_id TEXT)")
        assert sqlite_table_exists(conn, "checkpoints") is True
    finally:
        conn.close()


def test_sqlite_table_exists_false_for_absent_table():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE checkpoints (thread_id TEXT)")
        assert sqlite_table_exists(conn, "users") is False
    finally:
        conn.close()


def test_sqlite_table_exists_false_on_empty_database():
    conn = sqlite3.connect(":memory:")
    try:
        assert sqlite_table_exists(conn, "checkpoints") is False
    finally:
        conn.close()


def test_sqlite_table_exists_distinguishes_table_from_index():
    # ``type = 'table'`` must not match a same-named index.
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (a TEXT)")
        conn.execute("CREATE INDEX idx_only ON t (a)")
        assert sqlite_table_exists(conn, "idx_only") is False
        assert sqlite_table_exists(conn, "t") is True
    finally:
        conn.close()


def test_sqlite_table_exists_binds_name_as_parameter():
    # The name must be a bound parameter, not interpolated SQL. A malicious
    # name is treated as a (non-matching) literal value and the real table
    # survives the call.
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE checkpoints (thread_id TEXT)")
        assert sqlite_table_exists(conn, "checkpoints'; DROP TABLE checkpoints; --") is False
        assert sqlite_table_exists(conn, "checkpoints") is True
    finally:
        conn.close()


class _FakeCursor:
    """Minimal psycopg-cursor stand-in recording the executed probe."""

    def __init__(self, regclass_row: Any):
        self._regclass_row = regclass_row
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.executed.append((sql, tuple(params)))

    def fetchone(self) -> Any:
        return self._regclass_row


def test_postgres_table_exists_true_when_regclass_resolves():
    cur = _FakeCursor(("public.checkpoints",))
    assert postgres_table_exists(cur, "checkpoints") is True
    assert cur.executed == [("SELECT to_regclass(%s)", ("checkpoints",))]


def test_postgres_table_exists_false_when_regclass_null():
    cur = _FakeCursor((None,))
    assert postgres_table_exists(cur, "missing") is False
    assert cur.executed == [("SELECT to_regclass(%s)", ("missing",))]


def test_postgres_table_exists_false_when_no_row():
    # Defensive: to_regclass always returns a row, but an empty fetch must not
    # raise (the old open-coded copies in cleanup/branch indexed [0] blindly).
    cur = _FakeCursor(None)
    assert postgres_table_exists(cur, "checkpoints") is False
