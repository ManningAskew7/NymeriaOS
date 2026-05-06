"""Tests for sync consumption of the async agent stream."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nymeria.vendor.react_agent import providers
from nymeria.vendor.react_agent.config import LLMConfig
from nymeria.vendor.react_agent.providers import create_llm
from nymeria.core.stream_bridge import iter_agent_astream, stream_and_collect


class _FakeAgent:
    def __init__(self):
        self.calls = []

    async def astream(self, **kwargs):
        self.calls.append(kwargs)
        yield {"type": "thinking", "content": "working"}
        yield {"type": "tool_result", "name": "tool_create", "result": "ok"}
        yield {"type": "response", "content": "done"}


def test_iter_agent_astream_yields_async_chunks_from_sync_context():
    agent = _FakeAgent()

    chunks = list(iter_agent_astream(
        agent,
        message="build a tool",
        thread_id="thread-a",
        user_id="owner",
        _is_self_invoke=True,
    ))

    assert [chunk["type"] for chunk in chunks] == [
        "thinking",
        "tool_result",
        "response",
    ]
    assert agent.calls == [{
        "message": "build a tool",
        "thread_id": "thread-a",
        "user_id": "owner",
        "_is_self_invoke": True,
    }]


def test_iter_agent_astream_propagates_errors_after_prior_chunks():
    class ErrorAgent:
        async def astream(self, **kwargs):
            yield {"type": "thinking", "content": "started"}
            raise RuntimeError("astream failed")

    iterator = iter_agent_astream(ErrorAgent(), message="x")

    assert next(iterator) == {"type": "thinking", "content": "started"}
    try:
        next(iterator)
    except RuntimeError as exc:
        assert str(exc) == "astream failed"
    else:  # pragma: no cover - assertion guard
        raise AssertionError("expected RuntimeError")


def test_iter_agent_astream_closes_async_generator_when_consumer_stops():
    released = []

    class ReleasingAgent:
        async def astream(self, **kwargs):
            try:
                yield {"type": "thinking", "content": "started"}
                yield {"type": "response", "content": "unreached"}
            finally:
                released.append(True)

    iterator = iter_agent_astream(ReleasingAgent(), message="x")

    assert next(iterator) == {"type": "thinking", "content": "started"}
    iterator.close()

    assert released == [True]


def test_iter_agent_astream_reuses_bridge_loop_for_loop_bound_clients():
    class LoopBoundAgent:
        def __init__(self):
            self.loop = None

        async def astream(self, **kwargs):
            loop = asyncio.get_running_loop()
            if self.loop is None:
                self.loop = loop
            elif loop is not self.loop:
                if self.loop.is_closed():
                    raise RuntimeError("Event loop is closed")
                raise RuntimeError("changed event loop")
            yield {"type": "response", "content": kwargs["message"]}

    agent = LoopBoundAgent()

    assert list(iter_agent_astream(agent, message="first")) == [
        {"type": "response", "content": "first"}
    ]
    assert list(iter_agent_astream(agent, message="second")) == [
        {"type": "response", "content": "second"}
    ]
    assert agent.loop is not None
    assert not agent.loop.is_closed()


def test_iter_agent_astream_uses_one_bridge_loop_for_parallel_sync_consumers():
    loop_ids = set()

    class ConcurrentAgent:
        async def astream(self, **kwargs):
            loop_ids.add(id(asyncio.get_running_loop()))
            await asyncio.sleep(0.01)
            yield {"type": "response", "content": kwargs["message"]}

    agent = ConcurrentAgent()

    def consume(index: int):
        return list(iter_agent_astream(agent, message=f"run-{index}"))

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(consume, range(3)))

    assert results == [
        [{"type": "response", "content": "run-0"}],
        [{"type": "response", "content": "run-1"}],
        [{"type": "response", "content": "run-2"}],
    ]
    assert len(loop_ids) == 1


def test_iter_agent_astream_uses_loop_local_anthropic_client_after_api_loop_touch(
    monkeypatch,
):
    monkeypatch.setattr(
        providers,
        "_should_use_cliproxy_context_management_adapter",
        lambda _chat_model_cls: (True, "test"),
    )

    llm = create_llm(
        LLMConfig(
            provider="anthropic",
            model="claude-sonnet-4-20250514",
            api_key="test-key",
            base_url="http://cli-proxy-api-latest:8317",
            temperature=None,
        )
    )

    async def touch_from_api_loop():
        api_client = llm._async_client
        await providers.close_anthropic_async_http_pools_for_loop()
        return api_client

    api_loop_client = asyncio.run(touch_from_api_loop())

    class CachedGraphAgent:
        async def astream(self, **kwargs):
            bridge_client = llm._async_client
            if bridge_client is api_loop_client:
                raise RuntimeError("bound to a different event loop")
            if bridge_client._client is api_loop_client._client:
                raise RuntimeError("Event loop is closed")
            yield {"type": "response", "content": kwargs["message"]}

    assert list(iter_agent_astream(CachedGraphAgent(), message="ok")) == [
        {"type": "response", "content": "ok"}
    ]


def test_iter_agent_astream_uses_loop_scoped_openai_client_after_api_loop_touch():
    class LoopScopedOpenAIAgent:
        def __init__(self):
            self.http_clients_by_loop = {}

        async def astream(self, **kwargs):
            loop = asyncio.get_running_loop()
            if id(loop) not in self.http_clients_by_loop:
                llm = create_llm(
                    LLMConfig(
                        provider="openai",
                        model="gpt-5.5",
                        api_key="test-key",
                        base_url="http://cli-proxy-api-latest:8317/v1",
                        temperature=None,
                    )
                )
                self.http_clients_by_loop[id(loop)] = llm.root_async_client._client
            yield {"type": "response", "content": kwargs["message"]}

    agent = LoopScopedOpenAIAgent()

    async def collect_from_api_loop():
        return [chunk async for chunk in agent.astream(message="api")]

    api_loop = asyncio.new_event_loop()
    try:
        assert api_loop.run_until_complete(collect_from_api_loop()) == [
            {"type": "response", "content": "api"}
        ]
        api_http_client = next(iter(agent.http_clients_by_loop.values()))

        assert list(iter_agent_astream(agent, message="bridge")) == [
            {"type": "response", "content": "bridge"}
        ]

        bridge_clients = [
            client
            for client in agent.http_clients_by_loop.values()
            if client is not api_http_client
        ]
        assert len(bridge_clients) == 1
        assert not bridge_clients[0].is_closed
    finally:
        api_loop.run_until_complete(
            providers.close_provider_async_http_pools_for_loop(api_loop)
        )
        api_loop.close()


def test_stream_and_collect_tracks_common_stream_state():
    class CollectingAgent:
        async def astream(self, **kwargs):
            yield {"type": "thinking", "content": "working"}
            yield {"type": "tool_call", "name": "lookup"}
            yield {"type": "response", "content": "done"}
            yield {"type": "iteration_limit", "scope": "sub_agent"}
            yield {
                "type": "iteration_limit",
                "scope": "main_agent",
                "reason": "max_iterations",
            }

    seen = []

    def on_chunk(chunk, collection):
        seen.append((chunk["type"], collection.chunk_count, collection.tool_call_count))

    result = stream_and_collect(
        CollectingAgent(),
        astream_kwargs={"message": "x"},
        on_chunk=on_chunk,
        should_mark_iteration_limit=lambda chunk: chunk.get("scope") == "main_agent",
    )

    assert result.response_parts == ["done"]
    assert result.thinking_parts == ["working"]
    assert result.response_text() == "done"
    assert result.chunk_count == 5
    assert result.tool_call_count == 1
    assert result.iteration_limit_hit is True
    assert result.iteration_limit_event == {
        "type": "iteration_limit",
        "scope": "main_agent",
        "reason": "max_iterations",
    }
    assert seen == [
        ("thinking", 1, 0),
        ("tool_call", 2, 1),
        ("response", 3, 1),
        ("iteration_limit", 4, 1),
        ("iteration_limit", 5, 1),
    ]


def test_stream_and_collect_raises_error_after_chunk_callback():
    class ErrorAgent:
        async def astream(self, **kwargs):
            yield {"type": "error", "content": "", "code": "boom"}

    seen = []

    def on_chunk(chunk, collection):
        seen.append((chunk["type"], collection.chunk_count))

    with pytest.raises(RuntimeError, match="custom boom"):
        stream_and_collect(
            ErrorAgent(),
            astream_kwargs={"message": "x"},
            on_chunk=on_chunk,
            error_message_factory=lambda chunk: f"custom {chunk['code']}",
        )

    assert seen == [("error", 1)]


def test_autonomous_callers_use_stream_and_collect_for_collection_loops():
    repo_root = Path(__file__).resolve().parents[1]
    paths = [
        repo_root / "nymeria/core/ticker.py",
        repo_root / "nymeria/core/trigger_manager.py",
        repo_root / "nymeria/core/thread_agent_executor.py",
        repo_root / "nymeria/tools/spawn_thread.py",
    ]

    for path in paths:
        assert "for chunk in iter_agent_astream(" not in path.read_text()
