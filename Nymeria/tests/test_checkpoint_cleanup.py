"""Regression tests for shared checkpoint cleanup helpers."""

from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

from nymeria.core.checkpoint_cleanup import (
    CHECKPOINT_TABLES,
    PostgresCheckpointCleaner,
    SQLiteCheckpointCleaner,
)


def _create_delete_tables(db_path: Path, target: str, survivor: str) -> None:
    with sqlite3.connect(db_path) as conn:
        for table in CHECKPOINT_TABLES:
            conn.execute(f"CREATE TABLE {table} (thread_id TEXT, payload TEXT)")
            conn.execute(
                f"INSERT INTO {table} (thread_id, payload) VALUES (?, ?)",
                (target, "delete-me"),
            )
            conn.execute(
                f"INSERT INTO {table} (thread_id, payload) VALUES (?, ?)",
                (survivor, "keep-me"),
            )
        conn.commit()


def _count_rows(db_path: Path, table: str, thread_id: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()[0]
        )


def test_sqlite_delete_thread_removes_only_target_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "nymeria.db"
    target = "thread-delete"
    survivor = "thread-keep"
    _create_delete_tables(db_path, target, survivor)

    counts = SQLiteCheckpointCleaner(db_path).delete_thread(target)

    assert counts == {
        "checkpoint_writes_deleted": 1,
        "checkpoint_blobs_deleted": 1,
        "checkpoints_deleted": 1,
        "checkpoint_rows_remaining": 0,
    }
    for table in CHECKPOINT_TABLES:
        assert _count_rows(db_path, table, target) == 0
        assert _count_rows(db_path, table, survivor) == 1


def test_sqlite_delete_thread_tolerates_missing_checkpoint_tables(tmp_path: Path) -> None:
    counts = SQLiteCheckpointCleaner(tmp_path / "empty.db").delete_thread("thread")

    assert counts == {
        "checkpoint_writes_deleted": 0,
        "checkpoint_blobs_deleted": 0,
        "checkpoints_deleted": 0,
        "checkpoint_rows_remaining": 0,
    }


def test_sqlite_prune_before_keeps_boundary_namespace_and_survivor_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nymeria.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE checkpoint_writes ("
            "thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE checkpoints ("
            "thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT)"
        )
        conn.execute(
            "CREATE TABLE checkpoint_blobs ("
            "thread_id TEXT, channel TEXT, version TEXT)"
        )
        for table in ("checkpoint_writes", "checkpoints"):
            conn.executemany(
                f"INSERT INTO {table} (thread_id, checkpoint_ns, checkpoint_id) "
                "VALUES (?, ?, ?)",
                [
                    ("thread", "", "0001"),
                    ("thread", "", "0002"),
                    ("thread", "other", "0001"),
                    ("survivor", "", "0001"),
                ],
            )
        conn.executemany(
            "INSERT INTO checkpoint_blobs (thread_id, channel, version) VALUES (?, ?, ?)",
            [
                ("thread", "messages", "1"),
                ("thread", "messages", "2"),
                ("thread", "other", "1"),
                ("survivor", "messages", "1"),
            ],
        )
        conn.commit()

    counts = SQLiteCheckpointCleaner(db_path).prune_before(
        "thread",
        "0002",
        {"messages": "2", "bad": "not-an-int"},
    )

    assert counts == (1, 1, 1)
    with sqlite3.connect(db_path) as conn:
        for table in ("checkpoint_writes", "checkpoints"):
            rows = conn.execute(
                f"SELECT thread_id, checkpoint_ns, checkpoint_id FROM {table} "
                "ORDER BY thread_id, checkpoint_ns, checkpoint_id"
            ).fetchall()
            assert rows == [
                ("survivor", "", "0001"),
                ("thread", "", "0002"),
                ("thread", "other", "0001"),
            ]
        blob_rows = conn.execute(
            "SELECT thread_id, channel, version FROM checkpoint_blobs "
            "ORDER BY thread_id, channel, version"
        ).fetchall()
        assert blob_rows == [
            ("survivor", "messages", "1"),
            ("thread", "messages", "2"),
            ("thread", "other", "1"),
        ]


class _FakePostgresCursor:
    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple[object, ...]]] = []
        self.rowcount = 0
        self._fetchone: tuple[object, ...] = (None,)

    def __enter__(self) -> "_FakePostgresCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> "_FakePostgresCursor":
        self.commands.append((sql, params))
        if sql == "SELECT to_regclass(%s)":
            self._fetchone = (params[0],)
            self.rowcount = 0
        elif sql.startswith("SELECT COUNT(*)"):
            self._fetchone = (0,)
            self.rowcount = 0
        elif sql.startswith("DELETE FROM checkpoint_writes"):
            self.rowcount = 2
        elif sql.startswith("DELETE FROM checkpoint_blobs"):
            self.rowcount = 3
        elif sql.startswith("DELETE FROM checkpoints"):
            self.rowcount = 4
        else:
            raise AssertionError(f"unexpected SQL: {sql}")
        return self

    def fetchone(self) -> tuple[object, ...]:
        return self._fetchone


class _FakePostgresConnection:
    def __init__(self, cursor: _FakePostgresCursor) -> None:
        self.cursor_obj = cursor
        self.commits = 0

    def __enter__(self) -> "_FakePostgresConnection":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def cursor(self) -> _FakePostgresCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1


def _install_fake_psycopg(monkeypatch, conn: _FakePostgresConnection) -> None:
    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        types.SimpleNamespace(connect=lambda uri: conn),
    )


def test_postgres_delete_thread_uses_shared_backend_cleaner(monkeypatch) -> None:
    cursor = _FakePostgresCursor()
    conn = _FakePostgresConnection(cursor)
    _install_fake_psycopg(monkeypatch, conn)

    counts = PostgresCheckpointCleaner("postgres://example").delete_thread("thread")

    assert counts == {
        "checkpoint_writes_deleted": 2,
        "checkpoint_blobs_deleted": 3,
        "checkpoints_deleted": 4,
        "checkpoint_rows_remaining": 0,
    }
    assert conn.commits == 1
    assert ("SELECT to_regclass(%s)", ("checkpoint_writes",)) in cursor.commands
    assert (
        "DELETE FROM checkpoints WHERE thread_id = %s",
        ("thread",),
    ) in cursor.commands


def test_postgres_prune_before_uses_backend_placeholders(monkeypatch) -> None:
    cursor = _FakePostgresCursor()
    conn = _FakePostgresConnection(cursor)
    _install_fake_psycopg(monkeypatch, conn)

    counts = PostgresCheckpointCleaner("postgres://example").prune_before(
        "thread",
        "0002",
        {"messages": 2},
    )

    assert counts == (4, 2, 3)
    assert conn.commits == 1
    assert (
        "DELETE FROM checkpoint_writes "
        "WHERE thread_id = %s AND checkpoint_ns = '' AND checkpoint_id < %s",
        ("thread", "0002"),
    ) in cursor.commands
    assert (
        "DELETE FROM checkpoint_blobs "
        "WHERE thread_id = %s AND channel = %s AND CAST(version AS INTEGER) < %s",
        ("thread", "messages", 2),
    ) in cursor.commands
