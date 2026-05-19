"""Unit tests for the checkpointer-config builders extracted from NymeriaAgent."""

from __future__ import annotations

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
) -> Any:
    """SimpleNamespace stand-in for ``Settings`` exposing the attrs the builders read."""
    return SimpleNamespace(
        database_backend=database_backend,
        db_path=db_path,
        postgres_uri=postgres_uri,
    )


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
