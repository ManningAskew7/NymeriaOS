"""Focused tests for agent streaming helper predicates."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from nymeria.core.agent_streaming import (
    GraphStreamProcessor,
    ReasoningChunkDeduper,
    has_tool_call_content_delta,
    has_tool_call_delta,
)


class _FakeGraph:
    def __init__(self, events):
        self.events = events
        self.calls = []

    async def astream_events(self, input_state, config=None, version=None):
        self.calls.append((input_state, config, version))
        for event in self.events:
            yield event


def _collect_processor_events(events, *, abort_event=None, response_parts=None, extra_events=None):
    response_parts = response_parts if response_parts is not None else []
    abort_event = abort_event or threading.Event()

    processor = GraphStreamProcessor(
        thread_id="thread-a",
        config={"configurable": {"thread_id": "thread-a"}},
        abort_event=abort_event,
        is_self_invoke=False,
        response_parts=response_parts,
        clean_tool_result=lambda result: f"display:{result}",
        tool_result_extra_events=extra_events or (lambda *args: []),
    )
    graph = _FakeGraph(events)

    async def collect():
        chunks = []
        async for chunk in processor.drive(graph, {"messages": ["input"]}):
            chunks.append(chunk)
        return chunks

    return asyncio.run(collect()), response_parts, graph


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


def test_graph_stream_processor_converts_auth_prompt_custom_event():
    chunks, response_parts, _graph = _collect_processor_events([
        {
            "event": "on_custom_event",
            "name": "auth_prompt",
            "data": {
                "prompt_id": "prompt-1",
                "connect_url": "https://nymeria.example.test/connect/credentials/prompt-1#tok",
            },
        }
    ])

    assert chunks == [{
        "type": "auth_prompt",
        "prompt_id": "prompt-1",
        "connect_url": "https://nymeria.example.test/connect/credentials/prompt-1#tok",
    }]
    assert response_parts == []


def test_reasoning_chunk_deduper_resets_between_model_calls():
    deduper = ReasoningChunkDeduper()

    assert deduper.should_emit("thought") is True
    assert deduper.should_emit("thought") is False
    assert deduper.should_emit("") is False
    assert deduper.should_emit({"text": "thought"}) is False

    deduper.reset()

    assert deduper.should_emit("thought") is True


def test_graph_stream_processor_converts_tool_events_and_model_end_fallback():
    response_parts = []

    def extra_events(tool_name, raw_result, run_id):
        return [{
            "type": "workspace_artifact",
            "name": tool_name,
            "result": raw_result,
            "id": run_id,
        }]

    chunks, response_parts, graph = _collect_processor_events(
        [
            {
                "event": "on_tool_start",
                "run_id": "call-1",
                "name": "lookup",
                "data": {"input": {"q": "nymeria"}},
            },
            {
                "event": "on_tool_start",
                "run_id": "call-1",
                "name": "lookup",
                "data": {"input": {"q": "duplicate"}},
            },
            {
                "event": "on_tool_end",
                "run_id": "call-1",
                "name": "lookup",
                "data": {
                    "output": ToolMessage(
                        content="raw tool result",
                        tool_call_id="call-1",
                        name="lookup",
                    )
                },
            },
            {
                "event": "on_tool_end",
                "run_id": "call-1",
                "name": "lookup",
                "data": {"output": "duplicate"},
            },
            {
                "event": "on_chat_model_end",
                "run_id": "model-1",
                "data": {"output": AIMessage(content="Done")},
            },
        ],
        response_parts=response_parts,
        extra_events=extra_events,
    )

    assert chunks == [
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "lookup",
            "args": {"q": "nymeria"},
        },
        {
            "type": "tool_result",
            "id": "call-1",
            "name": "lookup",
            "result": "display:raw tool result",
        },
        {
            "type": "workspace_artifact",
            "name": "lookup",
            "result": "raw tool result",
            "id": "call-1",
        },
        {"type": "response", "content": "Done"},
    ]
    assert response_parts == ["Done"]
    assert graph.calls == [(
        {"messages": ["input"]},
        {"configurable": {"thread_id": "thread-a"}},
        "v2",
    )]


def test_graph_stream_processor_streams_reasoning_tool_delta_and_response_text():
    chunks, response_parts, _graph = _collect_processor_events([
        {"event": "on_chat_model_start", "run_id": "model-1"},
        {
            "event": "on_chat_model_stream",
            "run_id": "model-1",
            "data": {
                "chunk": SimpleNamespace(
                    tool_call_chunks=[{"name": "lookup"}],
                    content=None,
                    additional_kwargs={"reasoning_content": "think"},
                )
            },
        },
        {
            "event": "on_chat_model_stream",
            "run_id": "model-1",
            "data": {
                "chunk": SimpleNamespace(
                    tool_call_chunks=[],
                    content=[
                        {
                            "type": "reasoning",
                            "summary": [{"type": "summary_text", "text": "plan"}],
                        },
                        {"type": "text", "text": "Answer"},
                    ],
                    additional_kwargs={"reasoning_content": "think"},
                )
            },
        },
        {
            "event": "on_chat_model_end",
            "run_id": "model-1",
            "data": {"output": AIMessage(content="Answer")},
        },
    ])

    assert chunks == [
        {"type": "tool_call_delta"},
        {"type": "thinking", "content": "think"},
        {"type": "thinking", "content": "plan"},
        {"type": "response", "content": "Answer"},
    ]
    assert response_parts == ["Answer"]


def test_graph_stream_processor_does_not_replay_reasoning_on_model_end():
    chunks, response_parts, _graph = _collect_processor_events([
        {"event": "on_chat_model_start", "run_id": "model-1"},
        {
            "event": "on_chat_model_stream",
            "run_id": "model-1",
            "data": {
                "chunk": SimpleNamespace(
                    tool_call_chunks=[],
                    content=None,
                    additional_kwargs={"reasoning_content": "live reasoning"},
                )
            },
        },
        {
            "event": "on_chat_model_end",
            "run_id": "model-1",
            "data": {
                "output": AIMessage(content=[
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "final reasoning"}],
                    }
                ])
            },
        },
    ])

    assert chunks == [{"type": "thinking", "content": "live reasoning"}]
    assert response_parts == []


def test_graph_stream_processor_emits_cancel_event_and_stops():
    abort_event = threading.Event()
    abort_event.set()

    chunks, response_parts, _graph = _collect_processor_events(
        [
            {
                "event": "on_chat_model_end",
                "run_id": "model-1",
                "data": {"output": AIMessage(content="unreached")},
            }
        ],
        abort_event=abort_event,
    )

    assert chunks == [{
        "type": "error",
        "content": "Operation was cancelled.",
        "code": "cancelled",
    }]
    assert response_parts == []


def test_astream_orchestrates_graph_stream_processor_without_inner_driver():
    agent_source = Path(__file__).resolve().parents[1] / "nymeria/core/agent.py"
    text = agent_source.read_text()

    assert "async def _drive_graph_events" not in text
    assert "GraphStreamProcessor(" in text
