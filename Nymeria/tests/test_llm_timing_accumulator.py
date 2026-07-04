"""Tests for the per-turn LLM-time accumulator (tokens/s, backlog #63).

The agent node adds each successful model call's wall-clock duration to a
mutable dict injected per-turn as ``config["configurable"]["llm_timing"]``.
Side-channel graph runs (compaction summaries, ``nym.llm``) build their own
configs without the key and must stay excluded.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from nymeria.vendor.react_agent.nodes import (
    _accumulate_llm_seconds,
    create_agent_node,
)


class TestAccumulateHelper:
    def test_accumulates_seconds_and_calls(self):
        timing: dict = {}
        config = {"configurable": {"llm_timing": timing}}
        _accumulate_llm_seconds(config, 1.5)
        _accumulate_llm_seconds(config, 0.5)
        assert timing == {"seconds": 2.0, "calls": 2}

    def test_negative_seconds_clamped(self):
        timing: dict = {}
        config = {"configurable": {"llm_timing": timing}}
        _accumulate_llm_seconds(config, -3.0)
        assert timing == {"seconds": 0.0, "calls": 1}

    def test_noop_without_accumulator_key(self):
        # Compaction / nym.llm configs never carry llm_timing.
        config = {"configurable": {"thread_id": "t"}}
        _accumulate_llm_seconds(config, 1.0)
        assert "llm_timing" not in config["configurable"]

    def test_noop_on_malformed_config(self):
        _accumulate_llm_seconds(None, 1.0)
        _accumulate_llm_seconds({"configurable": {"llm_timing": "not-a-dict"}}, 1.0)
        _accumulate_llm_seconds(object(), 1.0)


class _FakeSyncLLM:
    """Minimal chat-model stand-in for the sync node path."""

    def invoke(self, _messages):
        return AIMessage(content="ok")


class _FakeStreamLLM:
    """Minimal chat-model stand-in for the async streaming node path."""

    async def astream(self, _messages):
        yield AIMessageChunk(content="he")
        yield AIMessageChunk(content="llo")


def _state():
    return {"messages": [HumanMessage(content="hi")]}


def _config_with_timing() -> tuple[dict, dict]:
    timing: dict = {}
    config = {
        "configurable": {"thread_id": "t1", "llm_timing": timing},
    }
    return config, timing


def test_sync_agent_node_accumulates_llm_time():
    node = create_agent_node(_FakeSyncLLM(), "system prompt")
    config, timing = _config_with_timing()

    result = node.invoke(_state(), config)

    assert result["messages"][0].content == "ok"
    assert timing.get("calls") == 1
    assert timing.get("seconds", -1) >= 0.0


@pytest.mark.asyncio
async def test_async_agent_node_accumulates_stream_time():
    node = create_agent_node(_FakeStreamLLM(), "system prompt")
    config, timing = _config_with_timing()

    result = await node.ainvoke(_state(), config)

    assert result["messages"][0].content == "hello"
    assert timing.get("calls") == 1
    assert timing.get("seconds", -1) >= 0.0


@pytest.mark.asyncio
async def test_async_agent_node_without_accumulator_is_untimed_noop():
    node = create_agent_node(_FakeStreamLLM(), "system prompt")
    config = {"configurable": {"thread_id": "t1"}}

    result = await node.ainvoke(_state(), config)

    assert result["messages"][0].content == "hello"
    assert "llm_timing" not in config["configurable"]
