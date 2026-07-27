"""Focused tests for agent streaming helper predicates."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from nymeria.vendor.react_agent.config import LLMConfig, LLMFallbackConfig
from nymeria.core.agent_compaction import COMPACTING_MESSAGE
from nymeria.core.agent_streaming import (
    GraphStreamProcessor,
    ReasoningChunkDeduper,
    compact_with_progress,
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


class _FlakyGraph:
    def __init__(self, first_events, second_events, exc):
        self.first_events = first_events
        self.second_events = second_events
        self.exc = exc
        self.calls = []

    async def astream_events(self, input_state, config=None, version=None):
        self.calls.append((input_state, config, version))
        events = self.first_events if len(self.calls) == 1 else self.second_events
        for event in events:
            yield event
        if len(self.calls) == 1:
            raise self.exc


class _RetryableStreamError(RuntimeError):
    status_code = 500


def _collect_processor_events(
    events, *, abort_event=None, response_parts=None, extra_events=None, tool_timeout=None
):
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
        tool_timeout=tool_timeout,
        llm_config=LLMConfig(
            provider="custom",
            model="primary",
            stream_max_retries=1,
            stream_retry_initial_delay=0.0,
            stream_retry_max_delay=0.0,
        ),
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

    def extra_events(tool_name, raw_result, run_id, thread_id=None):
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

    # Server-side timing fields are wall-clock dependent; validate shape, then
    # compare the rest exactly.
    tool_call_started_at = chunks[0].pop("started_at")
    tool_result_started_at = chunks[1].pop("started_at")
    duration_ms = chunks[1].pop("duration_ms")
    assert tool_call_started_at == tool_result_started_at
    assert isinstance(tool_call_started_at, str) and tool_call_started_at
    assert isinstance(duration_ms, int) and duration_ms >= 0

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


def test_mcp_tool_events_carry_server_provenance(monkeypatch):
    # An MCP tool's start and end events gain tool_type/server_id/server_name so
    # the client can badge the call with its origin server. The human name comes
    # from the registry; a plain (non-MCP) tool contributes none of these.
    fake_registry = SimpleNamespace(
        get_server=lambda server_id: (
            SimpleNamespace(name="Notion") if server_id == "notion" else None
        )
    )
    monkeypatch.setattr(
        "nymeria.core.mcp_servers.get_mcp_server_registry",
        lambda: fake_registry,
    )

    chunks, _, _ = _collect_processor_events(
        [
            {
                "event": "on_tool_start",
                "run_id": "call-1",
                "name": "mcp__notion__search",
                "data": {"input": {"q": "notes"}},
            },
            {
                "event": "on_tool_end",
                "run_id": "call-1",
                "name": "mcp__notion__search",
                "data": {"output": "hit"},
            },
            {
                "event": "on_tool_start",
                "run_id": "call-2",
                "name": "web_search",
                "data": {"input": {}},
            },
        ]
    )

    tool_call = next(c for c in chunks if c["type"] == "tool_call" and c["id"] == "call-1")
    tool_result = next(c for c in chunks if c["type"] == "tool_result")
    plain_call = next(c for c in chunks if c["id"] == "call-2")

    assert tool_call["tool_type"] == "mcp_server"
    assert tool_call["server_id"] == "notion"
    assert tool_call["server_name"] == "Notion"
    assert tool_result["tool_type"] == "mcp_server"
    assert tool_result["server_id"] == "notion"
    assert tool_result["server_name"] == "Notion"
    assert "tool_type" not in plain_call
    assert "server_id" not in plain_call


def test_mcp_provenance_falls_back_to_server_id_when_unresolved(monkeypatch):
    # Registry miss (unknown server) or a raising registry must not break the
    # stream: provenance still carries, with server_name defaulting to the id.
    def boom():
        raise RuntimeError("registry offline")

    monkeypatch.setattr("nymeria.core.mcp_servers.get_mcp_server_registry", boom)

    chunks, _, _ = _collect_processor_events(
        [
            {
                "event": "on_tool_start",
                "run_id": "call-1",
                "name": "mcp__ghost__do",
                "data": {"input": {}},
            },
        ]
    )

    tool_call = chunks[0]
    assert tool_call["tool_type"] == "mcp_server"
    assert tool_call["server_id"] == "ghost"
    assert tool_call["server_name"] == "ghost"


def test_tool_call_event_carries_timeout_budget_when_configured():
    chunks, _, _ = _collect_processor_events(
        [
            {
                "event": "on_tool_start",
                "run_id": "call-1",
                "name": "lookup",
                "data": {"input": {}},
            },
        ],
        tool_timeout=300,
    )

    assert chunks[0]["type"] == "tool_call"
    assert chunks[0]["timeout_seconds"] == 300
    assert isinstance(chunks[0]["started_at"], str) and chunks[0]["started_at"]


def test_tool_result_carries_stream_measured_timing():
    # Live timing is the processor's own on_tool_start -> on_tool_end diff,
    # measured in the API process (the checkpointed node stamp is not visible
    # on the callback output; it surfaces on history reload instead).
    chunks, _, _ = _collect_processor_events(
        [
            {
                "event": "on_tool_start",
                "run_id": "call-1",
                "name": "lookup",
                "data": {"input": {}},
            },
            {
                "event": "on_tool_end",
                "run_id": "call-1",
                "name": "lookup",
                "data": {"output": "plain string result"},
            },
        ],
    )

    call_chunk = next(c for c in chunks if c["type"] == "tool_call")
    result_chunk = next(c for c in chunks if c["type"] == "tool_result")
    assert result_chunk["started_at"] == call_chunk["started_at"]
    assert isinstance(result_chunk["duration_ms"], int)
    assert result_chunk["duration_ms"] >= 0


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
                    # A real chunk carries reasoning from ONE source: Responses
                    # mode via content reasoning blocks, chat-completions via
                    # additional_kwargs.reasoning_content, never both for the
                    # same text. (The old cross-source per-delta deduper was
                    # removed; see test_repeated_reasoning_deltas_pass_through.)
                    additional_kwargs={},
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
        {"type": "llm_call_started", "model": "primary", "reasoning": False},
        {"type": "tool_call_delta"},
        {"type": "thinking", "content": "think"},
        {"type": "thinking", "content": "plan"},
        {"type": "response", "content": "Answer"},
    ]
    assert response_parts == ["Answer"]


def test_repeated_reasoning_deltas_pass_through():
    """Reasoning token streams legitimately repeat short strings; the live path
    must emit every delta verbatim. The prior per-delta exact-string deduper
    dropped repeats (e.g. a recurring ' the') and fused adjacent words."""
    def _reasoning_stream_event(text):
        return {
            "event": "on_chat_model_stream",
            "run_id": "model-1",
            "data": {
                "chunk": SimpleNamespace(
                    tool_call_chunks=[],
                    content=[{
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": text}],
                    }],
                    additional_kwargs={},
                )
            },
        }

    # "The", " the", " the" repeats the exact delta " the" twice.
    deltas = ["The", " user", " counts", " the", " r", " the", " r"]
    chunks, _response_parts, _graph = _collect_processor_events(
        [{"event": "on_chat_model_start", "run_id": "model-1"}]
        + [_reasoning_stream_event(d) for d in deltas]
        + [{
            "event": "on_chat_model_end",
            "run_id": "model-1",
            "data": {"output": AIMessage(content="")},
        }]
    )

    emitted = [c["content"] for c in chunks if c.get("type") == "thinking"]
    # Every delta survives, repeats included; "".join reconstructs the text.
    assert emitted == deltas
    assert "".join(emitted) == "The user counts the r the r"


def test_graph_stream_processor_recovers_midstream_provider_failure_from_checkpoint():
    response_parts = []
    exc = _RetryableStreamError("server_error after partial stream")
    exc.nymeria_stream_chunks_before_error = 1
    graph = _FlakyGraph(
        first_events=[
            {"event": "on_chat_model_start", "run_id": "model-1"},
            {
                "event": "on_chat_model_stream",
                "run_id": "model-1",
                "data": {
                    "chunk": SimpleNamespace(
                        tool_call_chunks=[],
                        content="partial",
                        additional_kwargs={},
                    )
                },
            },
        ],
        second_events=[
            {"event": "on_chat_model_start", "run_id": "model-2"},
            {
                "event": "on_chat_model_stream",
                "run_id": "model-2",
                "data": {
                    "chunk": SimpleNamespace(
                        tool_call_chunks=[],
                        content="recovered",
                        additional_kwargs={},
                    )
                },
            },
            {
                "event": "on_chat_model_end",
                "run_id": "model-2",
                "data": {"output": AIMessage(content="recovered")},
            },
        ],
        exc=exc,
    )
    processor = GraphStreamProcessor(
        thread_id="thread-a",
        config={"configurable": {"thread_id": "thread-a"}},
        abort_event=threading.Event(),
        is_self_invoke=False,
        response_parts=response_parts,
        clean_tool_result=lambda result: result,
        tool_result_extra_events=lambda *args: [],
        llm_config=LLMConfig(
            provider="custom",
            model="primary",
            stream_max_retries=1,
            stream_retry_initial_delay=0.0,
            stream_retry_max_delay=0.0,
        ),
    )

    async def collect():
        chunks = []
        async for chunk in processor.drive(graph, {"messages": ["input"]}):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect())

    assert chunks == [
        {"type": "llm_call_started", "model": "primary", "reasoning": False},
        {"type": "response", "content": "partial"},
        {
            "type": "provider_retry",
            "provider": "custom",
            "model": "primary",
            "provider_route": None,
            "openai_api_mode": None,
            "attempt": 1,
            "max_retries": 1,
            "delay_seconds": 0.0,
            "reason": "provider_server_error",
            "http_status": 500,
            "rewound": True,
            "stream_chunks": 1,
        },
        # The rolled-back call re-runs, so a second status event fires.
        {"type": "llm_call_started", "model": "primary", "reasoning": False},
        {"type": "response", "content": "recovered"},
    ]
    assert response_parts == ["recovered"]
    assert graph.calls == [
        ({"messages": ["input"]}, {"configurable": {"thread_id": "thread-a"}}, "v2"),
        ({"messages": []}, {"configurable": {"thread_id": "thread-a"}}, "v2"),
    ]


def test_graph_stream_processor_switches_fallback_after_midstream_retry_budget():
    response_parts = []
    exc = _RetryableStreamError("server_error after partial stream")
    exc.nymeria_stream_chunks_before_error = 1
    graph = _FlakyGraph(
        first_events=[
            {"event": "on_chat_model_start", "run_id": "model-1"},
            {
                "event": "on_chat_model_stream",
                "run_id": "model-1",
                "data": {
                    "chunk": SimpleNamespace(
                        tool_call_chunks=[],
                        content="partial",
                        additional_kwargs={},
                    )
                },
            },
        ],
        second_events=[
            {"event": "on_chat_model_start", "run_id": "model-2"},
            {
                "event": "on_chat_model_stream",
                "run_id": "model-2",
                "data": {
                    "chunk": SimpleNamespace(
                        tool_call_chunks=[],
                        content="fallback",
                        additional_kwargs={},
                    )
                },
            },
            {
                "event": "on_chat_model_end",
                "run_id": "model-2",
                "data": {"output": AIMessage(content="fallback")},
            },
        ],
        exc=exc,
    )
    llm_config = LLMConfig(
        provider="custom",
        model="primary",
        stream_max_retries=0,
        stream_retry_initial_delay=0.0,
        stream_retry_max_delay=0.0,
        fallbacks=[LLMFallbackConfig(provider="backup", model="secondary")],
    )
    processor = GraphStreamProcessor(
        thread_id="thread-a",
        config={"configurable": {"thread_id": "thread-a"}},
        abort_event=threading.Event(),
        is_self_invoke=False,
        response_parts=response_parts,
        clean_tool_result=lambda result: result,
        tool_result_extra_events=lambda *args: [],
        llm_config=llm_config,
    )

    async def collect():
        chunks = []
        async for chunk in processor.drive(graph, {"messages": ["input"]}):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(collect())

    assert chunks == [
        {"type": "llm_call_started", "model": "primary", "reasoning": False},
        {"type": "response", "content": "partial"},
        {
            "type": "provider_fallback",
            "from_provider": "custom",
            "from_model": "primary",
            "from_provider_route": None,
            "from_openai_api_mode": None,
            "to_provider": "backup",
            "to_model": "secondary",
            "to_provider_route": None,
            "to_openai_api_mode": None,
            "reason": "provider_server_error",
            "http_status": 500,
            "rewound": True,
            "stream_chunks": 1,
        },
        # The retry call reports the ACTIVE candidate (swap-aware naming).
        {"type": "llm_call_started", "model": "secondary", "reasoning": False},
        {"type": "response", "content": "fallback"},
    ]
    assert response_parts == ["fallback"]
    assert llm_config.active_fallback_candidate_index == 1
    assert graph.calls == [
        ({"messages": ["input"]}, {"configurable": {"thread_id": "thread-a"}}, "v2"),
        ({"messages": []}, {"configurable": {"thread_id": "thread-a"}}, "v2"),
    ]


def test_drive_adopts_graph_baked_llm_config():
    """drive() must mutate the LLMConfig instance the graph's node closures
    read (stashed by create_graph as ``nymeria_llm_config``): a fallback
    activation or pending-note stamp on a freshly resolved equal-valued copy
    is dead state, leaving the re-drive on the primary and the swap note
    unattached. The constructor's llm_config stays only as the fallback for
    graphs without the attribute."""
    exc = _RetryableStreamError("server_error after partial stream")
    exc.nymeria_stream_chunks_before_error = 1
    graph = _FlakyGraph(
        first_events=[{"event": "on_chat_model_start", "run_id": "model-1"}],
        second_events=[
            {
                "event": "on_chat_model_end",
                "run_id": "model-2",
                "data": {"output": AIMessage(content="fallback")},
            },
        ],
        exc=exc,
    )

    def _config():
        return LLMConfig(
            provider="custom",
            model="primary",
            stream_max_retries=0,
            stream_retry_initial_delay=0.0,
            stream_retry_max_delay=0.0,
            fallbacks=[LLMFallbackConfig(provider="backup", model="secondary")],
        )

    graph_config = _config()
    constructor_config = _config()
    graph.nymeria_llm_config = graph_config
    processor = GraphStreamProcessor(
        thread_id="thread-a",
        config={"configurable": {"thread_id": "thread-a"}},
        abort_event=threading.Event(),
        is_self_invoke=False,
        response_parts=[],
        clean_tool_result=lambda result: result,
        tool_result_extra_events=lambda *args: [],
        llm_config=constructor_config,
    )

    async def collect():
        return [chunk async for chunk in processor.drive(graph, {"messages": ["input"]})]

    chunks = asyncio.run(collect())

    assert processor.llm_config is graph_config
    assert any(chunk.get("type") == "provider_fallback" for chunk in chunks)
    # Activation + the site-3 note stamp landed on the GRAPH's config (what
    # the re-driven node reads), not the constructor's throwaway copy.
    assert graph_config.active_fallback_candidate_index == 1
    assert graph_config.pending_fallback_note is not None
    assert graph_config.pending_fallback_note["kind"] == "transport"
    assert constructor_config.active_fallback_candidate_index == 0
    assert constructor_config.pending_fallback_note is None


def test_create_graph_stashes_node_llm_config(monkeypatch):
    """The compiled graph exposes the very LLMConfig its node closures read,
    the identity contract drive()'s adoption (above) depends on."""
    from unittest.mock import MagicMock

    from nymeria.vendor.react_agent import nodes as nodes_module
    from nymeria.vendor.react_agent.config import AgentConfig, CheckpointerConfig
    from nymeria.vendor.react_agent.graph import create_graph

    # Node construction eagerly builds the provider LLM; stub it so the
    # compile needs no credentials.
    monkeypatch.setattr(
        nodes_module, "create_llm_with_tools", lambda config, tools: MagicMock()
    )
    llm = LLMConfig(provider="custom", model="primary")
    compiled = create_graph(
        config=AgentConfig(
            llm=llm,
            checkpointer=CheckpointerConfig(backend="memory"),
        ),
    )
    assert compiled.nymeria_llm_config is llm


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

    assert chunks == [
        {"type": "llm_call_started", "model": "primary", "reasoning": False},
        {"type": "thinking", "content": "live reasoning"},
    ]
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


# ---------------------------------------------------------------------------
# compact_with_progress (slice 01 F9): the compaction-progress async race.
# ---------------------------------------------------------------------------


def _collect_compact_with_progress(coro_factory):
    """Drive compact_with_progress to completion, returning (events, sink)."""

    async def _run():
        sink: list = []
        events = [evt async for evt in compact_with_progress(coro_factory, sink)]
        return events, sink

    return asyncio.run(_run())


def test_compact_with_progress_emits_compacting_once_when_started_signaled():
    async def _coro(on_started):
        await on_started()
        await asyncio.sleep(0)  # let the start-waiter observe the signal mid-flight
        return {"success": True, "messages_removed": 3, "summary": "s"}

    events, sink = _collect_compact_with_progress(lambda on_started: _coro(on_started))

    assert events == [{"type": "compacting", "message": COMPACTING_MESSAGE}]
    assert sink == [{"success": True, "messages_removed": 3, "summary": "s"}]


def test_compact_with_progress_no_event_when_compaction_skips():
    async def _coro(on_started):
        # Compaction decided not to run: it never signals started.
        return {"success": False, "reason": "below threshold"}

    events, sink = _collect_compact_with_progress(lambda on_started: _coro(on_started))

    assert events == []
    assert sink == [{"success": False, "reason": "below threshold"}]


def test_compact_with_progress_single_emit_when_started_and_completes_together():
    # Guards the double-emit invariant: a coroutine that signals start and
    # returns in one step must still yield exactly one "compacting" event.
    async def _coro(on_started):
        await on_started()
        return {"success": True}

    events, sink = _collect_compact_with_progress(lambda on_started: _coro(on_started))

    assert events == [{"type": "compacting", "message": COMPACTING_MESSAGE}]
    assert events.count({"type": "compacting", "message": COMPACTING_MESSAGE}) == 1
    assert sink == [{"success": True}]


def test_compact_with_progress_surfaces_none_result():
    async def _coro(on_started):
        await on_started()
        return None

    events, sink = _collect_compact_with_progress(lambda on_started: _coro(on_started))

    assert events == [{"type": "compacting", "message": COMPACTING_MESSAGE}]
    assert sink == [None]
    # The caller idiom `sink[0] if sink else None` recovers a None result.
    assert (sink[0] if sink else None) is None


def test_compact_with_progress_propagates_coroutine_exception():
    async def _coro(on_started):
        await on_started()
        raise RuntimeError("boom")

    async def _run():
        sink: list = []
        collected: list = []
        with pytest.raises(RuntimeError, match="boom"):
            async for evt in compact_with_progress(lambda on_started: _coro(on_started), sink):
                collected.append(evt)
        return collected, sink

    collected, sink = asyncio.run(_run())

    # The "compacting" event is emitted before the await re-raises; the result
    # is never appended (the await raised), matching the inline `await` behavior.
    assert collected == [{"type": "compacting", "message": COMPACTING_MESSAGE}]
    assert sink == []


def test_compact_with_progress_aclose_during_yield_is_generatorexit_safe():
    # Mirrors an SSE disconnect mid-"compacting": closing the generator while it
    # is suspended at the yield runs the sync `finally` without awaiting (the
    # GeneratorExit-safe contract) and leaves the in-flight compaction task
    # running (orphaned), exactly as the inline code did.
    async def _run():
        sink: list = []
        gate = asyncio.Event()  # never set: keeps the compaction in-flight

        async def _coro(on_started):
            await on_started()
            await gate.wait()  # block so the helper suspends at the compacting yield
            return {"success": True}

        gen = compact_with_progress(lambda on_started: _coro(on_started), sink)
        first = await anext(gen)
        await gen.aclose()  # GeneratorExit thrown into the suspended yield
        pending = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        for t in pending:  # clean up the orphan so the loop does not warn
            t.cancel()
        return first, sink, pending

    first, sink, pending = asyncio.run(_run())

    assert first == {"type": "compacting", "message": COMPACTING_MESSAGE}
    assert sink == []  # the result was never appended (compaction never completed)
    assert len(pending) == 1  # the compaction task survived aclose (orphaned)


def test_compact_with_progress_cancels_start_waiter_on_skip():
    # The skip path leaves the start-waiter task pending (the started Event is
    # never set); the sync `finally` must cancel it so no task leaks.
    async def _coro(on_started):
        return {"success": False, "reason": "skip"}

    async def _run():
        sink: list = []
        events = [evt async for evt in compact_with_progress(lambda on_started: _coro(on_started), sink)]
        await asyncio.sleep(0)  # let a cancelled start-waiter settle
        leaked = [
            t
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        ]
        return events, sink, leaked

    events, sink, leaked = asyncio.run(_run())

    assert events == []
    assert sink == [{"success": False, "reason": "skip"}]
    assert leaked == []


def _llm_call_started_chunks(llm_config, events):
    processor = GraphStreamProcessor(
        thread_id="thread-a",
        config={"configurable": {"thread_id": "thread-a"}},
        abort_event=threading.Event(),
        is_self_invoke=False,
        response_parts=[],
        clean_tool_result=lambda result: result,
        tool_result_extra_events=lambda *args: [],
        llm_config=llm_config,
    )

    async def collect():
        chunks = []
        async for chunk in processor.drive(_FakeGraph(events), {"messages": ["input"]}):
            chunks.append(chunk)
        return chunks

    return [c for c in asyncio.run(collect()) if c["type"] == "llm_call_started"]


def test_llm_call_started_reasoning_flag_reflects_config():
    """The status event tells clients whether the call's first output will be
    thinking, so activity labels can read Thinking through the TTFT window."""
    start = [{"event": "on_chat_model_start", "run_id": "model-1"}]

    on = _llm_call_started_chunks(
        LLMConfig(provider="custom", model="primary", reasoning_effort="high"),
        start,
    )
    assert on == [
        {"type": "llm_call_started", "model": "primary", "reasoning": True}
    ]

    # "off" wins over extended_thinking (the documented LLMConfig contract).
    off = _llm_call_started_chunks(
        LLMConfig(
            provider="custom",
            model="primary",
            reasoning_effort="off",
            extended_thinking=True,
        ),
        start,
    )
    assert off[0]["reasoning"] is False

    # None effort defers to the extended_thinking flag.
    ext = _llm_call_started_chunks(
        LLMConfig(provider="custom", model="primary", extended_thinking=True),
        start,
    )
    assert ext[0]["reasoning"] is True

    # Capability gate (shared with classify_reasoning_passback): a model
    # whose effort ladder has no "off" (gpt-oss) reasons by default, even
    # with nothing requested.
    always_on = _llm_call_started_chunks(
        LLMConfig(provider="openai", model="gpt-oss-120b"),
        start,
    )
    assert always_on[0]["reasoning"] is True

    # Swap-aware: the flag is evaluated against the ACTIVE candidate's
    # model, not the primary's (whose ladder here would say False).
    swapped = _llm_call_started_chunks(
        LLMConfig(
            provider="custom",
            model="primary",
            fallbacks=[LLMFallbackConfig(provider="openai", model="gpt-oss-120b")],
            active_fallback_candidate_index=1,
        ),
        start,
    )
    assert swapped == [
        {"type": "llm_call_started", "model": "gpt-oss-120b", "reasoning": True}
    ]


def test_llm_call_started_prefers_live_instance_model_name():
    chunks = _llm_call_started_chunks(
        LLMConfig(provider="custom", model="primary"),
        [{
            "event": "on_chat_model_start",
            "run_id": "model-1",
            "metadata": {"ls_model_name": "primary-live"},
        }],
    )
    assert chunks == [
        {"type": "llm_call_started", "model": "primary-live", "reasoning": False}
    ]
