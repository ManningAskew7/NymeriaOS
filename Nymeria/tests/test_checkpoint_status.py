from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from nymeria.core import checkpoint_status


def test_sqlite_revision_lookup_selects_checkpoint_id_only(monkeypatch, tmp_path: Path):
    executed: list[str] = []

    class FakeCursor:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class FakeConnection:
        def execute(self, sql, params=()):
            executed.append(sql)
            if "sqlite_master" in sql:
                return FakeCursor((1,))
            return FakeCursor(("cp-2",))

        def close(self):
            pass

    monkeypatch.setattr(
        checkpoint_status.sqlite3,
        "connect",
        lambda _path: FakeConnection(),
    )

    revision = checkpoint_status.get_sqlite_latest_checkpoint_revision(
        tmp_path / "checkpoints.db",
        "thread-1",
    )

    assert revision == "cp-2"
    assert executed[-1] == (
        "SELECT checkpoint_id FROM checkpoints "
        "WHERE thread_id = ? AND checkpoint_ns = '' "
        "ORDER BY checkpoint_id DESC LIMIT 1"
    )


def test_postgres_revision_lookup_selects_checkpoint_id_only(monkeypatch):
    executed: list[tuple[str, tuple[str, ...]]] = []

    class FakeCursor:
        def __init__(self):
            self._fetches = iter([("checkpoints",), ("cp-9",)])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=()):
            executed.append((sql, tuple(params)))

        def fetchone(self):
            return next(self._fetches)

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda _uri: FakeConnection()),
    )

    revision = checkpoint_status.get_postgres_latest_checkpoint_revision(
        "postgresql://example",
        "thread-1",
    )

    assert revision == "cp-9"
    assert executed[-1] == (
        "SELECT checkpoint_id FROM checkpoints "
        "WHERE thread_id = %s AND checkpoint_ns = '' "
        "ORDER BY checkpoint_id DESC LIMIT 1",
        ("thread-1",),
    )
