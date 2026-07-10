"""Regression tests for the vendored checkpointer async wrapper."""

from __future__ import annotations

import ast
import asyncio
import sys
import types
from pathlib import Path

import pytest

from nymeria.vendor.react_agent import graph as graph_module
from nymeria.vendor.react_agent.config import CheckpointerConfig
from nymeria.vendor.react_agent.graph import (
    AsyncCheckpointSaverWrapper,
    close_checkpointer_connections,
    create_checkpointer,
)


NYMERIA_ROOT = Path(__file__).resolve().parents[1]


class FakeSyncSaver:
    def __init__(self):
        self.calls = []

    def get_tuple(self, config):
        self.calls.append(("get_tuple", config))
        return {"config": config}

    def list(self, config, *, filter=None, before=None, limit=None):
        self.calls.append(("list", config, filter, before, limit))
        return ["checkpoint"]

    def put(self, config, checkpoint, metadata, new_versions):
        self.calls.append(("put", config, checkpoint, metadata, new_versions))
        return {"saved": True}

    def put_writes(self, config, writes, task_id):
        self.calls.append(("put_writes", config, writes, task_id))
        return {"writes_saved": True}


def test_checkpoint_saver_wrapper_forwards_sync_methods_to_same_saver():
    saver = FakeSyncSaver()
    wrapper = AsyncCheckpointSaverWrapper(saver, "Test")

    assert wrapper.get_tuple("cfg") == {"config": "cfg"}
    assert wrapper.list(
        "cfg", filter={"source": "test"}, before="b", limit=1
    ) == ["checkpoint"]
    assert wrapper.put("cfg", {"id": 1}, {"source": "test"}, {"v": 2}) == {
        "saved": True
    }
    assert wrapper.put_writes("cfg", [("channel", "value")], "task-1") == {
        "writes_saved": True
    }

    assert saver.calls == [
        ("get_tuple", "cfg"),
        ("list", "cfg", {"source": "test"}, "b", 1),
        ("put", "cfg", {"id": 1}, {"source": "test"}, {"v": 2}),
        ("put_writes", "cfg", [("channel", "value")], "task-1"),
    ]


def test_checkpoint_saver_wrapper_runs_async_methods_through_same_saver():
    async def run():
        saver = FakeSyncSaver()
        wrapper = AsyncCheckpointSaverWrapper(saver, "Test")

        assert await wrapper.aget_tuple("cfg") == {"config": "cfg"}
        assert await wrapper.alist(
            "cfg", filter={"source": "test"}, before="b", limit=1
        ) == ["checkpoint"]
        assert await wrapper.aput("cfg", {"id": 1}, {"source": "test"}, {"v": 2}) == {
            "saved": True
        }
        assert await wrapper.aput_writes(
            "cfg", [("channel", "value")], "task-1"
        ) == {"writes_saved": True}

        return saver.calls

    assert asyncio.run(run()) == [
        ("get_tuple", "cfg"),
        ("list", "cfg", {"source": "test"}, "b", 1),
        ("put", "cfg", {"id": 1}, {"source": "test"}, {"v": 2}),
        ("put_writes", "cfg", [("channel", "value")], "task-1"),
    ]


def test_vendor_graph_has_single_async_checkpoint_wrapper_class():
    graph_path = NYMERIA_ROOT / "nymeria/vendor/react_agent/graph.py"
    tree = ast.parse(graph_path.read_text(encoding="utf-8"), filename=str(graph_path))

    wrapper_classes = [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name.endswith("SaverWrapper")
    ]

    assert wrapper_classes == ["AsyncCheckpointSaverWrapper"]


def test_create_checkpointer_reuses_wrapper_for_sqlite_sync_and_async(tmp_path):
    close_checkpointer_connections()
    db_path = tmp_path / "checkpoints.db"

    try:
        sync_checkpointer = create_checkpointer(
            CheckpointerConfig(backend="sqlite", sqlite_path=str(db_path))
        )
        async_checkpointer = create_checkpointer(
            CheckpointerConfig(backend="sqlite_async", sqlite_path=str(db_path))
        )

        assert isinstance(sync_checkpointer, AsyncCheckpointSaverWrapper)
        assert async_checkpointer is sync_checkpointer
    finally:
        close_checkpointer_connections()


# ---------------------------------------------------------------------------
# Postgres: one shared connection pool per URI, lightweight savers per graph.
# Real postgres deps are optional (absent in the unit environment), so these
# tests inject fake ``psycopg_pool`` / ``langgraph.checkpoint.postgres``
# modules; the imports in graph.py are function-local, so the fakes are picked
# up at call time.
# ---------------------------------------------------------------------------


class FakeConnectionPool:
    instances: list["FakeConnectionPool"] = []

    def __init__(
        self,
        conninfo,
        *,
        min_size=None,
        max_size=None,
        kwargs=None,
        check=None,
        name=None,
        timeout=None,
        open=True,
    ):
        self.conninfo = conninfo
        self.min_size = min_size
        self.max_size = max_size
        self.kwargs = kwargs
        self.check = check
        self.name = name
        self.timeout = timeout
        self.open_arg = open
        self.open_calls: list[tuple] = []
        self.closed = False
        FakeConnectionPool.instances.append(self)

    @staticmethod
    def check_connection(conn):  # pragma: no cover - identity-compared only
        pass

    def open(self, wait=False, timeout=None):
        self.open_calls.append((wait, timeout))

    def close(self):
        self.closed = True


class FakePostgresSaver:
    instances: list["FakePostgresSaver"] = []

    def __init__(self, conn):
        self.conn = conn
        self.setup_calls = 0
        FakePostgresSaver.instances.append(self)

    def setup(self):
        self.setup_calls += 1


def _postgres_config(uri="postgresql://user:pw@host/db", **overrides):
    return CheckpointerConfig(
        backend="postgres",
        postgres_uri=uri,
        postgres_pool_min_size=overrides.pop("min_size", 2),
        postgres_pool_max_size=overrides.pop("max_size", 5),
    )


@pytest.fixture
def fake_postgres_modules(monkeypatch):
    FakeConnectionPool.instances = []
    FakePostgresSaver.instances = []

    pool_module = types.ModuleType("psycopg_pool")
    pool_module.ConnectionPool = FakeConnectionPool  # type: ignore[attr-defined]
    saver_module = types.ModuleType("langgraph.checkpoint.postgres")
    saver_module.PostgresSaver = FakePostgresSaver  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "psycopg_pool", pool_module)
    monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres", saver_module)

    close_checkpointer_connections()
    yield pool_module
    close_checkpointer_connections()


def test_create_checkpointer_postgres_shares_one_pool_across_builds(
    fake_postgres_modules,
):
    first = create_checkpointer(_postgres_config())
    second = create_checkpointer(_postgres_config())

    # One pool, opened once, set up once; but a distinct lightweight saver
    # per call so checkpoint-I/O serialization stays per-graph.
    assert len(FakeConnectionPool.instances) == 1
    pool = FakeConnectionPool.instances[0]
    assert isinstance(first, AsyncCheckpointSaverWrapper)
    assert isinstance(second, AsyncCheckpointSaverWrapper)
    assert first is not second
    assert first._saver is not second._saver
    assert first._saver.conn is pool
    assert second._saver.conn is pool
    assert sum(s.setup_calls for s in FakePostgresSaver.instances) == 1


def test_postgres_pool_constructed_with_expected_parameters(fake_postgres_modules):
    create_checkpointer(_postgres_config())

    pool = FakeConnectionPool.instances[0]
    assert pool.conninfo == "postgresql://user:pw@host/db"
    assert pool.min_size == 2
    assert pool.max_size == 5
    assert pool.kwargs == {"autocommit": True, "prepare_threshold": 0}
    # Checkout-time liveness check: what recovers dead connections after a
    # Postgres bounce without an API restart.
    assert pool.check is FakeConnectionPool.check_connection
    # Opened explicitly (fail-fast), not via the deprecated open=True path.
    assert pool.open_arg is False
    assert pool.open_calls == [(True, graph_module._POSTGRES_POOL_TIMEOUT)]


def test_postgres_pools_are_per_uri(fake_postgres_modules):
    create_checkpointer(_postgres_config("postgresql://user:pw@host/db1"))
    create_checkpointer(_postgres_config("postgresql://user:pw@host/db2"))

    assert len(FakeConnectionPool.instances) == 2


def test_close_checkpointer_connections_closes_and_resets_postgres_pool(
    fake_postgres_modules,
):
    create_checkpointer(_postgres_config())
    pool = FakeConnectionPool.instances[0]

    close_checkpointer_connections()

    assert pool.closed
    # A later build recreates the pool from scratch.
    create_checkpointer(_postgres_config())
    assert len(FakeConnectionPool.instances) == 2
    assert not FakeConnectionPool.instances[1].closed


def test_failed_postgres_pool_is_closed_and_not_cached(fake_postgres_modules):
    class ExplodingPool(FakeConnectionPool):
        fail_next = True

        def open(self, wait=False, timeout=None):
            if ExplodingPool.fail_next:
                ExplodingPool.fail_next = False
                raise RuntimeError("connection refused")
            super().open(wait=wait, timeout=timeout)

    fake_postgres_modules.ConnectionPool = ExplodingPool

    with pytest.raises(RuntimeError, match="connection refused"):
        create_checkpointer(_postgres_config())

    # The failed pool was closed and not cached: the retry builds a fresh
    # pool and succeeds.
    assert FakeConnectionPool.instances[0].closed
    wrapper = create_checkpointer(_postgres_config())
    assert isinstance(wrapper, AsyncCheckpointSaverWrapper)
    assert len(FakeConnectionPool.instances) == 2
    assert not FakeConnectionPool.instances[1].closed


def test_create_checkpointer_postgres_requires_uri(fake_postgres_modules):
    with pytest.raises(ValueError, match="requires postgres_uri"):
        create_checkpointer(CheckpointerConfig(backend="postgres", postgres_uri=None))
