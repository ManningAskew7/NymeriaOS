"""Focused tests for agent streaming helper predicates."""

from __future__ import annotations

from nymeria.core.agent_streaming import (
    ReasoningChunkDeduper,
    has_tool_call_content_delta,
    has_tool_call_delta,
)


def test_has_tool_call_delta_detects_langchain_tool_chunks():
    assert has_tool_call_delta([{"name": "search"}]) is True
    assert has_tool_call_delta([]) is False
    assert has_tool_call_delta(None) is False


def test_has_tool_call_content_delta_detects_provider_tool_blocks():
    assert has_tool_call_content_delta([
        {"type": "text", "text": "thinking"},
        {"type": "function_call", "call_id": "call-1"},
    ]) is True
    assert has_tool_call_content_delta([
        {"type": "reasoning", "summary": []},
        {"type": "text", "text": "answer"},
        "plain text",
    ]) is False
    assert has_tool_call_content_delta("not-blocks") is False


def test_reasoning_chunk_deduper_resets_between_model_calls():
    deduper = ReasoningChunkDeduper()

    assert deduper.should_emit("thought") is True
    assert deduper.should_emit("thought") is False
    assert deduper.should_emit("") is False
    assert deduper.should_emit({"text": "thought"}) is False

    deduper.reset()

    assert deduper.should_emit("thought") is True
