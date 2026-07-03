from __future__ import annotations

import asyncio
from typing import Any

from nymeria.triggers.cli.events import (
    CompactingEvent,
    CompactedEvent,
    ContextAttachedEvent,
    DiagnosticEvent,
    DispatchedEvent,
    DoneEvent,
    ErrorEvent,
    HookApprovalEvent,
    HookApprovalResolvedEvent,
    IterationLimitEvent,
    QueuedEvent,
    ResponseEvent,
    TaskCompletedEvent,
    TaskStartedEvent,
    ThinkingEvent,
    ToolCallDeltaEvent,
    ToolCallEvent,
    ToolReloadEvent,
    ToolResultEvent,
    WorkspaceArtifactEvent,
    normalize_async_stream_events,
    normalize_stream_event,
    normalize_stream_events,
)


def test_normalizes_hook_approval_events() -> None:
    held = normalize_stream_event(
        {
            "type": "hook_approval",
            "thread_id": "thread-a",
            "record_id": "rec-1",
            "tool_call_id": "call-1",
            "tool_name": "bash_execute",
            "prompt": "Approve tool call bash_execute?",
            "tool_args_preview": '{"command": "rm -rf build"}',
            "created_at": "2026-07-03T10:00:00+00:00",
            "expires_at": "2026-07-03T10:03:00+00:00",
        }
    )
    assert held == HookApprovalEvent(
        thread_id="thread-a",
        record_id="rec-1",
        tool_call_id="call-1",
        tool_name="bash_execute",
        prompt="Approve tool call bash_execute?",
        tool_args_preview='{"command": "rm -rf build"}',
        created_at="2026-07-03T10:00:00+00:00",
        expires_at="2026-07-03T10:03:00+00:00",
    )

    resolved = normalize_stream_event(
        {
            "type": "hook_approval_resolved",
            "thread_id": "thread-a",
            "record_id": "rec-1",
            "tool_call_id": "call-1",
            "tool_name": "bash_execute",
            "outcome": "timeout",
            "resolved_by": "",
            "note": "",
        }
    )
    assert resolved == HookApprovalResolvedEvent(
        thread_id="thread-a",
        record_id="rec-1",
        tool_call_id="call-1",
        tool_name="bash_execute",
        outcome="timeout",
    )


def test_normalizes_all_known_stream_event_types() -> None:
    cases: list[tuple[dict[str, Any], object]] = [
        (
            {"type": "thinking", "content": "checking context"},
            ThinkingEvent(thread_id="thread-a", content="checking context"),
        ),
        (
            {"type": "tool_call_delta", "content": "{"},
            ToolCallDeltaEvent(thread_id="thread-a", content="{"),
        ),
        (
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "search_memory",
                "args": {"query": "project"},
            },
            ToolCallEvent(
                thread_id="thread-a",
                id="call-1",
                name="search_memory",
                args={"query": "project"},
            ),
        ),
        (
            {
                "type": "tool_result",
                "id": "call-1",
                "name": "search_memory",
                "result": {"matches": 2},
            },
            ToolResultEvent(
                thread_id="thread-a",
                id="call-1",
                name="search_memory",
                result={"matches": 2},
            ),
        ),
        (
            {"type": "response", "content": "Hello"},
            ResponseEvent(thread_id="thread-a", content="Hello"),
        ),
        (
            {
                "type": "workspace_artifact",
                "tool_call_id": "call-1",
                "tool_name": "file_write",
                "path": "/workspace/report.csv",
                "name": "report.csv",
                "mime_type": "text/csv",
                "size_bytes": 123,
            },
            WorkspaceArtifactEvent(
                thread_id="thread-a",
                tool_call_id="call-1",
                tool_name="file_write",
                path="/workspace/report.csv",
                artifact={
                    "path": "/workspace/report.csv",
                    "name": "report.csv",
                    "mime_type": "text/csv",
                    "size_bytes": 123,
                },
            ),
        ),
        (
            {
                "type": "tool_reload",
                "tools": ["calc", "calendar"],
                "ttl": "2h",
                "ttl_seconds": 7200,
                "source": "tool_search",
                "skill_name": "planning",
                "reason": "user_enabled",
            },
            ToolReloadEvent(
                thread_id="thread-a",
                tools=("calc", "calendar"),
                ttl="2h",
                ttl_seconds=7200,
                source="tool_search",
                skill_name="planning",
                reason="user_enabled",
            ),
        ),
        (
            {
                "type": "dispatched",
                "target_thread_id": "thread-b",
                "title": "Research",
                "original_thread_id": "thread-a",
                "matched_ref": "Research",
            },
            DispatchedEvent(
                thread_id="thread-a",
                target_thread_id="thread-b",
                title="Research",
                original_thread_id="thread-a",
                matched_ref="Research",
            ),
        ),
        (
            {
                "type": "queued",
                "content": "Waiting for thread lock",
                "holder": "autonomous",
                "held_seconds": 1.5,
            },
            QueuedEvent(
                thread_id="thread-a",
                message="Waiting for thread lock",
                holder="autonomous",
                held_seconds=1.5,
            ),
        ),
        (
            {"type": "compacting", "message": "Compacting context..."},
            CompactingEvent(thread_id="thread-a", message="Compacting context..."),
        ),
        (
            {
                "type": "compacted",
                "summary": "Earlier context summarized.",
                "messages_removed": 12,
                "auto_resumed": True,
            },
            CompactedEvent(
                thread_id="thread-a",
                summary="Earlier context summarized.",
                messages_removed=12,
                auto_resumed=True,
            ),
        ),
        (
            {"type": "context_attached", "summary": "Prior work"},
            ContextAttachedEvent(thread_id="thread-a", summary="Prior work"),
        ),
        (
            {
                "type": "iteration_limit",
                "content": "Stopped before looping.",
                "scope": "sub_agent",
                "reason": "repeated_tool_result",
                "max_iterations": 30,
                "tool_call_count": 31,
                "agent_name": "Researcher",
                "repeated_tool_name": "lookup",
                "repeated_count": 5,
            },
            IterationLimitEvent(
                thread_id="thread-a",
                content="Stopped before looping.",
                scope="sub_agent",
                reason="repeated_tool_result",
                max_iterations=30,
                tool_call_count=31,
                agent_name="Researcher",
                repeated_tool_name="lookup",
                repeated_count=5,
            ),
        ),
        (
            {
                "type": "task_started",
                "task_id": "todo-1",
                "prompt": "Work on TODO todo-1: Check status",
                "todo_id": "todo-1",
                "source": "scheduler",
            },
            TaskStartedEvent(
                thread_id="thread-a",
                task_id="todo-1",
                prompt="Work on TODO todo-1: Check status",
                todo_id="todo-1",
                source="scheduler",
            ),
        ),
        (
            {
                "type": "task_completed",
                "task_id": "todo-1",
                "content": "Done.",
                "todo_id": "todo-1",
                "error": False,
                "notify": True,
            },
            TaskCompletedEvent(
                thread_id="thread-a",
                task_id="todo-1",
                content="Done.",
                todo_id="todo-1",
                notify=True,
            ),
        ),
        (
            {
                "type": "error",
                "content": "Backend unavailable",
                "code": "connection_failed",
                "details": {"retryable": True},
            },
            ErrorEvent(
                thread_id="thread-a",
                content="Backend unavailable",
                code="connection_failed",
                details={"retryable": True},
            ),
        ),
        (
            {
                "type": "done",
                "context_stats": {"total_tokens": 42},
                "model": "claude-test",
                "title": "Project status",
                "title_source": "auto",
                "tool_call_count": 1,
                "dispatched_to": {"thread_id": "thread-b", "title": "Research"},
            },
            DoneEvent(
                thread_id="thread-a",
                context_stats={"total_tokens": 42},
                model="claude-test",
                title="Project status",
                title_source="auto",
                tool_call_count=1,
                dispatched_to={"thread_id": "thread-b", "title": "Research"},
            ),
        ),
    ]

    for raw, expected in cases:
        assert normalize_stream_event(raw, default_thread_id="thread-a") == expected


def test_equivalent_api_and_local_events_compare_equal_after_normalization() -> None:
    api_event = {
        "type": "tool_call",
        "thread_id": "thread-a",
        "id": "call-1",
        "name": "lookup",
        "args": {"query": "status"},
    }
    local_event = {
        "type": "tool_call",
        "id": "call-1",
        "name": "lookup",
        "args": {"query": "status"},
    }

    assert normalize_stream_event(api_event) == normalize_stream_event(
        local_event,
        default_thread_id="thread-a",
    )


def test_normalizes_frontend_style_wrapped_event_payloads() -> None:
    wrapped = {
        "type": "tool_call",
        "threadId": "thread-a",
        "data": {
            "id": "call-1",
            "name": "lookup",
            "arguments": {"query": "status"},
        },
    }
    raw = {
        "type": "tool_call",
        "thread_id": "thread-a",
        "id": "call-1",
        "name": "lookup",
        "args": {"query": "status"},
    }

    assert normalize_stream_event(wrapped) == normalize_stream_event(raw)


def test_compact_result_legacy_event_maps_to_compacted_event() -> None:
    event = normalize_stream_event(
        {
            "type": "compact_result",
            "result": {
                "summary": "Summarized.",
                "messages_removed": "7",
                "auto_resumed": "true",
            },
        },
        default_thread_id="thread-a",
    )

    assert event == CompactedEvent(
        thread_id="thread-a",
        summary="Summarized.",
        messages_removed=7,
        auto_resumed=True,
    )


def test_missing_optional_fields_are_defensive() -> None:
    assert normalize_stream_event({"type": "tool_call"}) == ToolCallEvent()
    assert normalize_stream_event({"type": "tool_result"}) == ToolResultEvent()
    assert normalize_stream_event({"type": "done"}) == DoneEvent()
    assert normalize_stream_event({"type": "error"}) == ErrorEvent(
        content="Unknown error",
    )


def test_unknown_event_type_is_preserved_as_diagnostic() -> None:
    event = normalize_stream_event(
        {
            "type": "future_event",
            "thread_id": "thread-a",
            "content": "new payload",
            "extra": {"value": 1},
        }
    )

    assert event == DiagnosticEvent(
        thread_id="thread-a",
        source_type="future_event",
        message="Unknown stream event type: future_event",
        payload={
            "type": "future_event",
            "thread_id": "thread-a",
            "content": "new payload",
            "extra": {"value": 1},
        },
    )
    assert event.raw["extra"] == {"value": 1}


def test_malformed_events_are_diagnostics_not_exceptions() -> None:
    missing_type = normalize_stream_event({"content": "no type"})
    non_mapping = normalize_stream_event(None)

    assert missing_type == DiagnosticEvent(
        source_type="unknown",
        message="Malformed stream event: missing type.",
        payload={"content": "no type"},
    )
    assert non_mapping == DiagnosticEvent(
        source_type="malformed",
        message="Malformed stream event: expected a mapping.",
        payload={"value": "None"},
    )


def test_normalize_stream_events_handles_mixed_sync_streams() -> None:
    stream = [
        {"type": "response", "content": "ok"},
        {"content": "missing type"},
        {"type": "done"},
    ]

    normalized = list(normalize_stream_events(stream, default_thread_id="thread-a"))

    assert [event.type for event in normalized] == ["response", "diagnostic", "done"]
    assert [event.thread_id for event in normalized] == [
        "thread-a",
        "thread-a",
        "thread-a",
    ]


def test_normalize_async_stream_events_handles_api_streams() -> None:
    async def source():
        yield {"type": "thinking", "content": "plan", "thread_id": "thread-a"}
        yield {"type": "response", "content": "answer", "thread_id": "thread-a"}
        yield {"type": "done", "thread_id": "thread-a"}

    async def collect():
        return [event async for event in normalize_async_stream_events(source())]

    normalized = asyncio.run(collect())

    assert normalized == [
        ThinkingEvent(thread_id="thread-a", content="plan"),
        ResponseEvent(thread_id="thread-a", content="answer"),
        DoneEvent(thread_id="thread-a"),
    ]
