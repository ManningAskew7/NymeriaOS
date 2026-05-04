"""Regression tests for the vendored checkpointer async wrapper."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

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
