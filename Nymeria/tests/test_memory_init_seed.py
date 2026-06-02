"""Tests for the fresh-thread memory-init seeding on NymeriaAgent."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from nymeria.core import agent_memory_seed
from nymeria.core.agent import NymeriaAgent

SENTINEL_EXCHANGE = [
    HumanMessage(content="opener", additional_kwargs={"internal": True, "internal_type": "memory_init"}),
    AIMessage(content="", tool_calls=[{"id": "g", "name": "memory_read", "args": {"scope": "global"}, "type": "tool_call"}]),
    ToolMessage(content="G", tool_call_id="g", name="memory_read"),
    AIMessage(content="Memory loaded."),
]


def _fake_self(skip_seed: bool = False):
    return SimpleNamespace(
        _memory_seeded_threads=set(),
        _thread_skip_memory_seed=lambda thread_id: skip_seed,
    )


class FakeSyncGraph:
    def __init__(self, existing_messages, *, raise_on_get=False):
        self._existing = existing_messages
        self._raise_on_get = raise_on_get
        self.updated = None
        self.get_state_calls = 0

    def get_state(self, config):
        self.get_state_calls += 1
        if self._raise_on_get:
            raise RuntimeError("state unavailable")
        return SimpleNamespace(values={"messages": self._existing})

    def update_state(self, config, payload):
        self.updated = payload


class FakeAsyncGraph:
    def __init__(self, existing_messages):
        self._existing = existing_messages
        self.updated = None

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": self._existing})

    async def aupdate_state(self, config, payload):
        self.updated = payload


def _patch_builder(monkeypatch):
    monkeypatch.setattr(
        agent_memory_seed, "build_init_seed_exchange", lambda u, t: list(SENTINEL_EXCHANGE)
    )


def test_sync_seeds_empty_thread(monkeypatch):
    _patch_builder(monkeypatch)
    me = _fake_self()
    graph = FakeSyncGraph([])

    seeded = NymeriaAgent._seed_memory_init_if_empty_sync(me, graph, {}, "t1", "u1")

    assert seeded is True
    assert graph.updated == {"messages": SENTINEL_EXCHANGE}
    assert "t1" in me._memory_seeded_threads


def test_sync_skips_non_empty_thread(monkeypatch):
    _patch_builder(monkeypatch)
    me = _fake_self()
    graph = FakeSyncGraph([HumanMessage(content="hi")])

    seeded = NymeriaAgent._seed_memory_init_if_empty_sync(me, graph, {}, "t1", "u1")

    assert seeded is False
    assert graph.updated is None
    assert "t1" in me._memory_seeded_threads  # remembered so we don't re-check


def test_sync_short_circuits_when_already_seeded(monkeypatch):
    _patch_builder(monkeypatch)
    me = _fake_self()
    me._memory_seeded_threads.add("t1")
    graph = FakeSyncGraph([])

    seeded = NymeriaAgent._seed_memory_init_if_empty_sync(me, graph, {}, "t1", "u1")

    assert seeded is False
    assert graph.get_state_calls == 0  # no redundant state read
    assert graph.updated is None


def test_sync_handles_state_error(monkeypatch):
    _patch_builder(monkeypatch)
    me = _fake_self()
    graph = FakeSyncGraph([], raise_on_get=True)

    seeded = NymeriaAgent._seed_memory_init_if_empty_sync(me, graph, {}, "t1", "u1")

    assert seeded is False
    assert graph.updated is None


def test_async_seeds_empty_thread(monkeypatch):
    _patch_builder(monkeypatch)
    me = _fake_self()
    graph = FakeAsyncGraph([])

    seeded = asyncio.run(
        NymeriaAgent._seed_memory_init_if_empty(me, graph, {}, "t1", "u1")
    )

    assert seeded is True
    assert graph.updated == {"messages": SENTINEL_EXCHANGE}
    assert "t1" in me._memory_seeded_threads


def test_async_skips_non_empty_thread(monkeypatch):
    _patch_builder(monkeypatch)
    me = _fake_self()
    graph = FakeAsyncGraph([HumanMessage(content="hi")])

    seeded = asyncio.run(
        NymeriaAgent._seed_memory_init_if_empty(me, graph, {}, "t1", "u1")
    )

    assert seeded is False
    assert graph.updated is None


def test_sync_skips_inherited_history_thread(monkeypatch):
    """Shadow/dream threads (skip_seed=True) are not freshly seeded; they carry
    the parent's memory via their cloned checkpoint."""
    _patch_builder(monkeypatch)
    me = _fake_self(skip_seed=True)
    graph = FakeSyncGraph([])

    seeded = NymeriaAgent._seed_memory_init_if_empty_sync(me, graph, {}, "dream-x", "u1")

    assert seeded is False
    assert graph.updated is None
    assert graph.get_state_calls == 0  # guard returns before reading state
    assert "dream-x" in me._memory_seeded_threads
