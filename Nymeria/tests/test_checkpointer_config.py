"""Unit tests for the checkpointer-config builders extracted from NymeriaAgent."""

from __future__ import annotations

import logging
import sqlite3
import sys
import types
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nymeria.core.checkpointer_config import (
    build_async_checkpointer_config,
    build_checkpointer_config,
    enumerate_checkpoint_thread_ids,
)


def _fake_settings(
    *,
    database_backend: str,
    db_path: Path | None = None,
    postgres_uri: str | None = None,
    postgres_pool_min_size: int = 1,
    postgres_pool_max_size: int = 10,
    checkpoint_executor_max_workers: int = 8,
) -> Any:
    """SimpleNamespace stand-in for ``Settings`` exposing the attrs the builders read."""
    return SimpleNamespace(
        database_backend=database_backend,
        db_path=db_path,
        postgres_uri=postgres_uri,
        postgres_pool_min_size=postgres_pool_min_size,
        postgres_pool_max_size=postgres_pool_max_size,
        checkpoint_executor_max_workers=checkpoint_executor_max_workers,
    )


def _make_checkpoints_db(
    db_path: Path,
    rows: list[tuple[Any, str, str]],
    *,
    thread_id_type: str = "TEXT",
) -> None:
    """Create a minimal ``checkpoints`` table and insert ``rows``.

    Only ``thread_id`` matters to the enumerator; the namespace/id columns
    mirror enough of the real LangGraph schema to make DISTINCT meaningful.
    ``thread_id_type=""`` declares a no-affinity column so an inserted integer
    is preserved (used to exercise the ``str(...)`` coercion).
    """
    col = f"thread_id {thread_id_type}".strip()
    with closing(sqlite3.connect(str(db_path))) as conn:
        conn.execute(
            f"CREATE TABLE checkpoints ({col}, checkpoint_ns TEXT, checkpoint_id TEXT)"
        )
        conn.executemany(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id) "
            "VALUES (?, ?, ?)",
            rows,
        )
        conn.commit()


def test_build_checkpointer_config_memory():
    settings = _fake_settings(database_backend="memory")
    cfg = build_checkpointer_config(cast(Any, settings))
    assert cfg.backend == "memory"


def test_build_checkpointer_config_sqlite_creates_parent_dir(tmp_path: Path):
    db_path = tmp_path / "nested" / "checkpoints.db"
    settings = _fake_settings(database_backend="sqlite", db_path=db_path)

    cfg = build_checkpointer_config(cast(Any, settings))

    assert cfg.backend == "sqlite"
    assert cfg.sqlite_path == str(db_path)
    assert db_path.parent.is_dir()  # parent created


def test_build_checkpointer_config_postgres_requires_uri():
    settings = _fake_settings(database_backend="postgres", postgres_uri=None)
    with pytest.raises(ValueError, match="POSTGRES_URI required"):
        build_checkpointer_config(cast(Any, settings))


def test_build_checkpointer_config_postgres_ok():
    settings = _fake_settings(
        database_backend="postgres",
        postgres_uri="postgresql://user:pw@host/db",
    )
    cfg = build_checkpointer_config(cast(Any, settings))
    assert cfg.backend == "postgres"
    assert cfg.postgres_uri == "postgresql://user:pw@host/db"
    assert cfg.postgres_pool_min_size == 1
    assert cfg.postgres_pool_max_size == 10


def test_build_checkpointer_config_postgres_threads_pool_sizes():
    settings = _fake_settings(
        database_backend="postgres",
        postgres_uri="postgresql://user:pw@host/db",
        postgres_pool_min_size=2,
        postgres_pool_max_size=6,
    )

    sync_cfg = build_checkpointer_config(cast(Any, settings))
    async_cfg = build_async_checkpointer_config(cast(Any, settings))

    for cfg in (sync_cfg, async_cfg):
        assert cfg.postgres_pool_min_size == 2
        assert cfg.postgres_pool_max_size == 6


def test_build_checkpointer_config_postgres_clamps_inverted_pool_sizes(
    caplog: pytest.LogCaptureFixture,
):
    # A max below min is lifted to min (with a warning) instead of refusing
    # to start or handing psycopg_pool an invalid pair.
    settings = _fake_settings(
        database_backend="postgres",
        postgres_uri="postgresql://user:pw@host/db",
        postgres_pool_min_size=5,
        postgres_pool_max_size=2,
    )

    with caplog.at_level(logging.WARNING):
        cfg = build_checkpointer_config(cast(Any, settings))

    assert cfg.postgres_pool_min_size == 5
    assert cfg.postgres_pool_max_size == 5
    assert any(
        "POSTGRES_POOL_MAX_SIZE" in r.getMessage() for r in caplog.records
    )


def test_builders_thread_checkpoint_executor_workers(tmp_path: Path):
    pg = _fake_settings(
        database_backend="postgres",
        postgres_uri="postgresql://user:pw@host/db",
        checkpoint_executor_max_workers=4,
    )
    lite = _fake_settings(
        database_backend="sqlite",
        db_path=tmp_path / "ckpt.db",
        checkpoint_executor_max_workers=4,
    )

    for cfg in (
        build_checkpointer_config(cast(Any, pg)),
        build_async_checkpointer_config(cast(Any, pg)),
        build_checkpointer_config(cast(Any, lite)),
        build_async_checkpointer_config(cast(Any, lite)),
    ):
        assert cfg.checkpoint_executor_max_workers == 4


def test_build_async_checkpointer_config_sqlite_uses_async_wrapper(tmp_path: Path):
    db_path = tmp_path / "async.db"
    settings = _fake_settings(database_backend="sqlite", db_path=db_path)

    cfg = build_async_checkpointer_config(cast(Any, settings))

    assert cfg.backend == "sqlite_async"
    assert cfg.sqlite_path == str(db_path)


def test_build_async_checkpointer_config_postgres_requires_uri():
    settings = _fake_settings(database_backend="postgres", postgres_uri=None)
    with pytest.raises(ValueError, match="POSTGRES_URI required"):
        build_async_checkpointer_config(cast(Any, settings))


def test_enumerate_checkpoint_thread_ids_sqlite_tolerates_missing_table(tmp_path: Path):
    db_path = tmp_path / "empty.db"
    settings = _fake_settings(database_backend="sqlite", db_path=db_path)

    result = enumerate_checkpoint_thread_ids(cast(Any, settings))

    assert result == []


def test_enumerate_checkpoint_thread_ids_memory_backend_returns_empty():
    settings = _fake_settings(database_backend="memory")
    assert enumerate_checkpoint_thread_ids(cast(Any, settings)) == []


def test_enumerate_checkpoint_thread_ids_unknown_backend_returns_empty():
    settings = _fake_settings(database_backend="something-else")
    assert enumerate_checkpoint_thread_ids(cast(Any, settings)) == []


def test_enumerate_checkpoint_thread_ids_sqlite_returns_distinct(tmp_path: Path):
    db_path = tmp_path / "ckpt.db"
    _make_checkpoints_db(
        db_path,
        [
            ("alpha", "", "c1"),
            ("alpha", "", "c2"),  # same thread, multiple checkpoints -> deduped
            ("beta", "ns", "c1"),
        ],
    )
    settings = _fake_settings(database_backend="sqlite", db_path=db_path)

    result = enumerate_checkpoint_thread_ids(cast(Any, settings))

    assert sorted(result) == ["alpha", "beta"]
    assert all(isinstance(t, str) for t in result)


def test_enumerate_checkpoint_thread_ids_sqlite_filters_null_and_empty(tmp_path: Path):
    db_path = tmp_path / "ckpt.db"
    _make_checkpoints_db(
        db_path,
        [
            (None, "", "c1"),  # NULL thread_id -> dropped
            ("", "", "c2"),  # empty string -> dropped
            ("0", "", "c3"),  # non-empty but falsy-looking -> KEPT
            ("keep", "", "c4"),
        ],
    )
    settings = _fake_settings(database_backend="sqlite", db_path=db_path)

    result = enumerate_checkpoint_thread_ids(cast(Any, settings))

    assert sorted(result) == ["0", "keep"]


def test_enumerate_checkpoint_thread_ids_sqlite_coerces_to_str(tmp_path: Path):
    db_path = tmp_path / "ckpt.db"
    # A no-affinity thread_id column keeps the integer, so str() must coerce it.
    _make_checkpoints_db(db_path, [(12345, "", "c1")], thread_id_type="")
    settings = _fake_settings(database_backend="sqlite", db_path=db_path)

    result = enumerate_checkpoint_thread_ids(cast(Any, settings))

    assert result == ["12345"]
    assert isinstance(result[0], str)


def test_enumerate_checkpoint_thread_ids_sqlite_missing_table_is_silent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    # A fresh DB with no checkpoints table returns [] via the table-existence
    # guard, with NO warning (a fresh install must not log spurious failures).
    db_path = tmp_path / "fresh.db"
    settings = _fake_settings(database_backend="sqlite", db_path=db_path)

    with caplog.at_level(logging.WARNING):
        result = enumerate_checkpoint_thread_ids(cast(Any, settings))

    assert result == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_enumerate_checkpoint_thread_ids_sqlite_real_failure_warns(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    # Pointing db_path at a directory makes the connection fail: a genuine
    # error that must still be logged (the guard must not silence real faults).
    db_dir = tmp_path / "not_a_db"
    db_dir.mkdir()
    settings = _fake_settings(database_backend="sqlite", db_path=db_dir)

    with caplog.at_level(logging.WARNING):
        result = enumerate_checkpoint_thread_ids(cast(Any, settings))

    assert result == []
    assert any(
        "Enumerate checkpoint thread_ids (sqlite) failed" in r.getMessage()
        for r in caplog.records
    )


def test_enumerate_checkpoint_thread_ids_postgres_missing_uri_skips_connect(
    monkeypatch: pytest.MonkeyPatch,
):
    # No postgres_uri must short-circuit to [] WITHOUT calling psycopg.connect
    # (it must never pass None into connect).
    fake_psycopg = types.ModuleType("psycopg")

    def _no_connect(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("psycopg.connect must not be called without a URI")

    fake_psycopg.connect = _no_connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    settings = _fake_settings(database_backend="postgres", postgres_uri=None)

    assert enumerate_checkpoint_thread_ids(cast(Any, settings)) == []
