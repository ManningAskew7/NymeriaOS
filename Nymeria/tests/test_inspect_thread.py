"""Tests for the checkpoint inspection CLI helper."""

from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from tools import inspect_thread


def _write_sqlite_checkpoint(db_path: Path, thread_id: str, messages: list[object]) -> None:
    with sqlite3.connect(db_path) as conn:
        saver = SqliteSaver(conn)
        saver.setup()
        checkpoint = empty_checkpoint()
        version = "00000000000000000000000000000001.0.123"
        checkpoint["channel_values"] = {"messages": messages}
        checkpoint["channel_versions"] = {"messages": version}
        saver.put(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}},
            checkpoint,
            {"source": "test"},
            {"messages": version},
        )


def test_list_threads_uses_checkpoints_table_and_counts_latest_messages(
    tmp_path: Path,
    capsys,
) -> None:
    db_path = tmp_path / "nymeria.db"
    _write_sqlite_checkpoint(
        db_path,
        "thread-a",
        [HumanMessage(content="hello"), AIMessage(content="hi")],
    )
    _write_sqlite_checkpoint(db_path, "thread-b", [HumanMessage(content="solo")])

    inspect_thread.list_threads(
        inspect_thread.StoreConfig(backend="sqlite", sqlite_path=db_path)
    )

    output = capsys.readouterr().out
    assert "Checkpoint backend: sqlite" in output
    assert "thread-a" in output
    assert "       2" in output
    assert "thread-b" in output
    assert "       1" in output
    assert "2 threads total" in output


def test_inspect_thread_reads_latest_checkpoint_messages(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "nymeria.db"
    _write_sqlite_checkpoint(
        db_path,
        "thread-a",
        [
            HumanMessage(content="hello"),
            AIMessage(
                content="checking",
                tool_calls=[{"name": "lookup", "args": {"q": "x"}, "id": "call-1"}],
            ),
        ],
    )

    inspect_thread.inspect_thread(
        inspect_thread.StoreConfig(backend="sqlite", sqlite_path=db_path),
        "thread-a",
        last_n=1,
    )

    output = capsys.readouterr().out
    assert "Showing last 1 of 2 messages" in output
    assert "[1] AIMessage" in output
    assert "checking" in output
    assert "-> tool_call: lookup({'q': 'x'})" in output
    assert "hello" not in output


def test_inspect_thread_reports_missing_checkpoint(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "nymeria.db"
    _write_sqlite_checkpoint(db_path, "thread-a", [HumanMessage(content="hello")])

    inspect_thread.inspect_thread(
        inspect_thread.StoreConfig(backend="sqlite", sqlite_path=db_path),
        "missing",
    )

    assert "No checkpoint found for thread: missing" in capsys.readouterr().out


def test_list_threads_handles_uninitialized_sqlite_database(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "nymeria.db"
    sqlite3.connect(db_path).close()

    inspect_thread.list_threads(
        inspect_thread.StoreConfig(backend="sqlite", sqlite_path=db_path)
    )

    output = capsys.readouterr().out
    assert "Checkpoint backend: sqlite" in output
    assert "0 threads total" in output


def test_postgres_store_uses_postgres_saver_and_checkpoint_query(monkeypatch) -> None:
    class FakeCursor:
        def __init__(self, conn: "FakeConn"):
            self.conn = conn

        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, *exc_info: object) -> None:
            return None

        def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
            self.conn.queries.append(query)

        def fetchone(self) -> tuple[str]:
            return ("checkpoints",)

        def fetchall(self) -> list[tuple[str]]:
            return [("pg-thread",)]

    class FakeConn:
        def __init__(self):
            self.queries: list[str] = []
            self.closed = False

        def cursor(self) -> FakeCursor:
            return FakeCursor(self)

        def close(self) -> None:
            self.closed = True

    class FakePostgresSaver:
        def __init__(self, conn: FakeConn):
            self.conn = conn

        def get_tuple(self, config: dict[str, object]) -> SimpleNamespace:
            assert config == {
                "configurable": {"thread_id": "pg-thread", "checkpoint_ns": ""}
            }
            return SimpleNamespace(
                checkpoint={
                    "channel_values": {"messages": [HumanMessage(content="from pg")]}
                }
            )

    fake_conn = FakeConn()

    def fake_connect(uri: str, autocommit: bool = False) -> FakeConn:
        assert uri == "postgresql://nymeria:secret@localhost/nymeria"
        assert autocommit is True
        return fake_conn

    postgres_module = types.ModuleType("langgraph.checkpoint.postgres")
    postgres_module.PostgresSaver = FakePostgresSaver
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", postgres_module)
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=fake_connect))

    store = inspect_thread.CheckpointStore.open(
        inspect_thread.StoreConfig(
            backend="postgres",
            postgres_uri="postgresql://nymeria:secret@localhost/nymeria",
        )
    )
    try:
        assert store.thread_ids() == ["pg-thread"]
        assert [msg.content for msg in store.latest_messages("pg-thread")] == ["from pg"]
    finally:
        store.close()

    assert fake_conn.queries == [
        "SELECT to_regclass(%s)",
        "SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id"
    ]
    assert fake_conn.closed is True
