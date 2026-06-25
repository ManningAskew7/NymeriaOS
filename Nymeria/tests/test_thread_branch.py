"""Tests for the checkpoint-branch SQL in ``core/thread_branch.py``.

These lock the behaviour of ``clone_thread_checkpoints`` / ``_delete_branch_checkpoint_rows``
after the SQLite/Postgres dialect-adapter refactor (slice 05 F2). The previously
untested message-index (recursive-CTE) path is covered here for the first time.

psycopg is not installed in the local test environment, so the Postgres dialect is
exercised only by the shared recursive-CTE template render check and a
``pytest.importorskip``-guarded param-packing test; the real Postgres end-to-end proof
is a manual Docker smoke test (see the slice doc).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import nymeria.core.thread_branch as tb
from nymeria.core.thread_branch import (
    _SELECTED_CHECKPOINT_IDS_CTE,
    CHECKPOINT_BRANCH_TABLES,
    ThreadBranchError,
    clone_thread_checkpoints,
)

# Real LangGraph-shaped DDL. checkpoint_blobs intentionally has NO checkpoint_id column
# (it is keyed by version), which the message-index filter must skip.
_CREATE_CHECKPOINTS = (
    "CREATE TABLE checkpoints ("
    "thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
    "parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB, "
    "PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id))"
)
_CREATE_WRITES = (
    "CREATE TABLE writes ("
    "thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, "
    "task_id TEXT, idx INTEGER, channel TEXT, type TEXT, value BLOB)"
)
_CREATE_BLOBS = (
    "CREATE TABLE checkpoint_blobs ("
    "thread_id TEXT, checkpoint_ns TEXT, channel TEXT, "
    "version TEXT, type TEXT, blob BLOB)"
)


def _sqlite_settings(db_path: Path) -> SimpleNamespace:
    return SimpleNamespace(database_backend="sqlite", db_path=str(db_path))


def _connect(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path))


# ---------------------------------------------------------------------------
# Recursive-CTE template (the headline dedup): load-bearing drift guard.
# ---------------------------------------------------------------------------


def test_selected_checkpoint_ids_cte_renders_byte_equivalent_per_dialect():
    sqlite_cte = _SELECTED_CHECKPOINT_IDS_CTE.format(ph="?")
    postgres_cte = _SELECTED_CHECKPOINT_IDS_CTE.format(ph="%s")

    # Exactly three bound placeholders per dialect; nothing left unrendered.
    assert sqlite_cte.count("?") == 3
    assert postgres_cte.count("%s") == 3
    assert "{ph}" not in sqlite_cte and "{ph}" not in postgres_cte
    assert "%s" not in sqlite_cte
    assert "?" not in postgres_cte

    # The two render to identical text apart from the placeholder token.
    assert sqlite_cte.replace("?", "<P>") == postgres_cte.replace("%s", "<P>")

    # Key structure / placeholder positions are pinned.
    assert "WITH RECURSIVE selected(checkpoint_ns, checkpoint_id, parent_checkpoint_id)" in sqlite_cte
    assert "WHERE thread_id = ? AND checkpoint_ns = '' AND checkpoint_id = ?" in sqlite_cte
    assert "ON c.thread_id = ?" in sqlite_cte
    assert "SELECT checkpoint_id FROM selected" in sqlite_cte


# ---------------------------------------------------------------------------
# clone_thread_checkpoints: SQLite, full copy (no message index).
# ---------------------------------------------------------------------------


def test_clone_full_copies_only_source_thread(tmp_path: Path):
    db = tmp_path / "checkpoints.db"
    with closing(_connect(db)) as conn:
        conn.execute(_CREATE_CHECKPOINTS)
        conn.execute(_CREATE_WRITES)
        conn.executemany(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("source", "", "0001", None, "json", b"old", b"{}"),
                ("source", "", "0002", "0001", "json", b"new", b"{}"),
                ("other", "", "9999", None, "json", b"other", b"{}"),
            ],
        )
        conn.execute(
            "INSERT INTO writes "
            "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("source", "", "0002", "task", 0, "messages", "json", b"write"),
        )
        conn.commit()

    counts = clone_thread_checkpoints(_sqlite_settings(db), "source", "target")

    assert counts["checkpoints_copied"] == 2
    assert counts["writes_copied"] == 1
    # checkpoint_writes / checkpoint_blobs tables do not exist -> 0.
    assert counts["checkpoint_writes_copied"] == 0
    assert counts["checkpoint_blobs_copied"] == 0

    with closing(_connect(db)) as conn:
        rows = conn.execute(
            "SELECT checkpoint_id, parent_checkpoint_id, checkpoint "
            "FROM checkpoints WHERE thread_id = ? ORDER BY checkpoint_id",
            ("target",),
        ).fetchall()
        other = conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ?", ("other",)
        ).fetchall()
    assert rows == [("0001", None, b"old"), ("0002", "0001", b"new")]
    assert other == [("9999",)]  # unrelated thread untouched


# ---------------------------------------------------------------------------
# clone_thread_checkpoints: SQLite, message-index (recursive CTE) path.
# ---------------------------------------------------------------------------


def test_clone_message_index_copies_only_ancestors(tmp_path: Path):
    db = tmp_path / "checkpoints.db"
    with closing(_connect(db)) as conn:
        conn.execute(_CREATE_CHECKPOINTS)
        conn.execute(_CREATE_WRITES)
        conn.execute(_CREATE_BLOBS)
        conn.executemany(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("source", "", "0001", None, "json", b"c1", b"{}"),
                ("source", "", "0002", "0001", "json", b"c2", b"{}"),
                ("source", "", "0003", "0002", "json", b"c3", b"{}"),
                # Side branch off 0001, must NOT be copied when branching from 0002.
                ("source", "", "0009", "0001", "json", b"c9", b"{}"),
            ],
        )
        conn.executemany(
            "INSERT INTO writes "
            "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("source", "", "0001", "t", 0, "messages", "json", b"w1"),
                ("source", "", "0002", "t", 0, "messages", "json", b"w2"),
                ("source", "", "0003", "t", 0, "messages", "json", b"w3"),
            ],
        )
        # checkpoint_blobs has no checkpoint_id column -> copied wholesale, selection ignored.
        conn.executemany(
            "INSERT INTO checkpoint_blobs "
            "(thread_id, checkpoint_ns, channel, version, type, blob) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("source", "", "messages", "1", "json", b"b1"),
                ("source", "", "messages", "2", "json", b"b2"),
            ],
        )
        conn.commit()

    counts = clone_thread_checkpoints(
        _sqlite_settings(db), "source", "target", source_checkpoint_id="0002"
    )

    # Only ancestors {0001, 0002} are copied, not 0003 (descendant) or 0009 (side branch).
    assert counts["checkpoints_copied"] == 2
    assert counts["writes_copied"] == 2
    # No checkpoint_id column -> the selection filter is skipped, both blob rows copied.
    assert counts["checkpoint_blobs_copied"] == 2

    with closing(_connect(db)) as conn:
        ckpts = conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ? ORDER BY checkpoint_id",
            ("target",),
        ).fetchall()
        writes = conn.execute(
            "SELECT checkpoint_id FROM writes WHERE thread_id = ? ORDER BY checkpoint_id",
            ("target",),
        ).fetchall()
        blobs = conn.execute(
            "SELECT version FROM checkpoint_blobs WHERE thread_id = ? ORDER BY version",
            ("target",),
        ).fetchall()
    assert ckpts == [("0001",), ("0002",)]
    assert writes == [("0001",), ("0002",)]
    assert blobs == [("1",), ("2",)]


def test_clone_message_index_missing_checkpoints_table_sqlite(tmp_path: Path):
    """Divergence guard: with no `checkpoints` table, SQLite returns an empty
    selection (NOT a raise; Postgres would raise). checkpoint_id-bearing tables then
    copy 0 rows; a table without checkpoint_id still copies by thread_id."""
    db = tmp_path / "checkpoints.db"
    with closing(_connect(db)) as conn:
        conn.execute(_CREATE_WRITES)  # has checkpoint_id
        conn.execute(_CREATE_BLOBS)  # no checkpoint_id
        conn.execute(
            "INSERT INTO writes "
            "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("source", "", "0001", "t", 0, "messages", "json", b"w1"),
        )
        conn.execute(
            "INSERT INTO checkpoint_blobs "
            "(thread_id, checkpoint_ns, channel, version, type, blob) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("source", "", "messages", "1", "json", b"b1"),
        )
        conn.commit()

    counts = clone_thread_checkpoints(
        _sqlite_settings(db), "source", "target", source_checkpoint_id="zzz"
    )

    assert counts["checkpoints_copied"] == 0  # table absent
    assert counts["writes_copied"] == 0  # empty selection -> early return 0
    assert counts["checkpoint_blobs_copied"] == 1  # no checkpoint_id -> copied by thread_id


def test_clone_message_index_target_exists_raises_after_empty_selection_sqlite(tmp_path: Path):
    """Raise-precedence (SQLite): with `checkpoints` absent the selection returns set()
    first (no raise), then `_raise_if_target_exists` fires on the pre-seeded target rows.
    Postgres would instead raise the selection error first, a preserved divergence."""
    db = tmp_path / "checkpoints.db"
    with closing(_connect(db)) as conn:
        conn.execute(_CREATE_WRITES)
        conn.execute(
            "INSERT INTO writes "
            "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("target", "", "0001", "t", 0, "messages", "json", b"pre"),
        )
        conn.commit()

    with pytest.raises(ThreadBranchError, match="already has checkpoint rows"):
        clone_thread_checkpoints(
            _sqlite_settings(db), "source", "target", source_checkpoint_id="zzz"
        )


def test_clone_raises_when_target_has_rows(tmp_path: Path):
    db = tmp_path / "checkpoints.db"
    with closing(_connect(db)) as conn:
        conn.execute(_CREATE_CHECKPOINTS)
        conn.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("target", "", "0001", None, "json", b"x", b"{}"),
        )
        conn.commit()

    with pytest.raises(ThreadBranchError, match="already has checkpoint rows"):
        clone_thread_checkpoints(_sqlite_settings(db), "source", "target")


def test_clone_skips_branch_table_without_thread_id_column(tmp_path: Path):
    """A branch table that exists but lacks a thread_id column is skipped (count 0),
    not errored. This pins the loop the refactor restructured around table_columns."""
    db = tmp_path / "checkpoints.db"
    with closing(_connect(db)) as conn:
        conn.execute(_CREATE_CHECKPOINTS)
        conn.execute("CREATE TABLE checkpoint_writes (a TEXT, b TEXT)")
        conn.execute("INSERT INTO checkpoint_writes (a, b) VALUES ('x', 'y')")
        conn.execute(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("source", "", "0001", None, "json", b"c", b"{}"),
        )
        conn.commit()

    counts = clone_thread_checkpoints(_sqlite_settings(db), "source", "target")
    assert counts["checkpoints_copied"] == 1
    assert counts["checkpoint_writes_copied"] == 0  # no thread_id column -> skipped

    # Delete must likewise skip the no-thread_id table and leave its row intact.
    tb._delete_branch_checkpoint_rows(_sqlite_settings(db), "source")
    with closing(_connect(db)) as conn:
        junk = conn.execute("SELECT COUNT(*) FROM checkpoint_writes").fetchone()[0]
    assert junk == 1


# ---------------------------------------------------------------------------
# Backend dispatch edges.
# ---------------------------------------------------------------------------


def test_clone_no_persistent_backend_returns_zero_and_rejects_message_index():
    settings = SimpleNamespace(database_backend="memory")
    counts = clone_thread_checkpoints(settings, "s", "t")
    assert counts == {f"{table}_copied": 0 for table in CHECKPOINT_BRANCH_TABLES}

    with pytest.raises(ThreadBranchError, match="persistent checkpoint backend"):
        clone_thread_checkpoints(settings, "s", "t", source_checkpoint_id="x")


def test_clone_postgres_without_uri_raises():
    settings = SimpleNamespace(database_backend="postgres", postgres_uri=None)
    with pytest.raises(ThreadBranchError, match="POSTGRES_URI"):
        clone_thread_checkpoints(settings, "s", "t")


# ---------------------------------------------------------------------------
# _delete_branch_checkpoint_rows: SQLite.
# ---------------------------------------------------------------------------


def test_delete_branch_checkpoint_rows_only_target(tmp_path: Path):
    db = tmp_path / "checkpoints.db"
    with closing(_connect(db)) as conn:
        conn.execute(_CREATE_CHECKPOINTS)
        conn.execute(_CREATE_WRITES)
        conn.executemany(
            "INSERT INTO checkpoints "
            "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("target", "", "0001", None, "json", b"x", b"{}"),
                ("keep", "", "0001", None, "json", b"y", b"{}"),
            ],
        )
        conn.execute(
            "INSERT INTO writes "
            "(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("target", "", "0001", "t", 0, "messages", "json", b"w"),
        )
        conn.commit()

    tb._delete_branch_checkpoint_rows(_sqlite_settings(db), "target")

    with closing(_connect(db)) as conn:
        target_ckpts = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", ("target",)
        ).fetchone()[0]
        target_writes = conn.execute(
            "SELECT COUNT(*) FROM writes WHERE thread_id = ?", ("target",)
        ).fetchone()[0]
        keep_ckpts = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", ("keep",)
        ).fetchone()[0]
    assert target_ckpts == 0
    assert target_writes == 0
    assert keep_ckpts == 1


def test_delete_branch_checkpoint_rows_no_persistent_backend_noop():
    # Must not raise and must not attempt a connection.
    tb._delete_branch_checkpoint_rows(SimpleNamespace(database_backend="memory"), "t")


# ---------------------------------------------------------------------------
# Postgres dialect param-packing (skips locally; runs where psycopg is present).
# ---------------------------------------------------------------------------


def test_postgres_copy_table_rows_packs_any_filter_as_single_list():
    pytest.importorskip("psycopg")

    captured: dict[str, Any] = {}

    class _FakeCursor:
        rowcount = 5

        def execute(self, query: Any, params: Any) -> None:
            captured["query"] = query
            captured["params"] = params

    dialect = tb._PostgresBranchDialect("postgresql://unused")
    columns = ["thread_id", "checkpoint_id", "checkpoint"]
    rows = dialect.copy_table_rows(
        _FakeCursor(), "checkpoints", columns, "src", "tgt", {"0002", "0001"}
    )

    assert rows == 5
    # = ANY(%s) packs the ids as ONE sorted list param (not spread like SQLite's IN).
    assert captured["params"] == ("tgt", "src", ["0001", "0002"])
