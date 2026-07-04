from __future__ import annotations

from cli_fixtures import event_sequence

from nymeria.triggers.cli.events import normalize_stream_event
from nymeria.triggers.cli.state import (
    AssistantMessage,
    ResponseStep,
    SystemMessage,
    ThinkingStep,
    ToolCallStep,
    create_initial_state,
    reduce_stream_event,
    select_activity_phase,
    select_last_assistant_message,
    select_response_content,
    select_running_tool_calls,
    start_turn,
)


def _apply(state, events, *, start: float = 1.0):
    next_state = state
    for offset, event in enumerate(events):
        next_state = reduce_stream_event(next_state, event, now=start + offset)
    return next_state


def test_queued_event_clears_when_streaming_starts_and_done_stores_metadata() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(
        state,
        "hello",
        now=0.1,
        user_message_id="user-1",
        assistant_message_id="assistant-1",
    )

    state = reduce_stream_event(
        state,
        {"type": "queued", "holder": "worker-1", "thread_id": "thread-1"},
        now=1.0,
    )

    assert state.turn_status == "queued"
    assert state.is_queued is True
    assert state.queue is not None
    assert state.queue.holder == "worker-1"

    state = reduce_stream_event(
        state,
        {"type": "response", "content": "Ready.", "thread_id": "thread-1"},
        now=2.0,
    )

    assert state.turn_status == "streaming"
    assert state.is_queued is False
    assert state.queue is None
    assert select_activity_phase(state, now=2.0) == "typing"

    state = reduce_stream_event(
        state,
        {
            "type": "done",
            "thread_id": "thread-1",
            "context_stats": {"used_tokens": 42, "max_tokens": 1000},
            "model": "test-model",
            "tool_call_count": 0,
        },
        now=3.0,
    )

    assistant = select_last_assistant_message(state)
    assert state.turn_status == "complete"
    assert state.context_stats == {"used_tokens": 42, "max_tokens": 1000}
    assert state.active_model == "test-model"
    assert state.tool_call_count == 0
    assert isinstance(assistant, AssistantMessage)
    assert assistant.status == "complete"
    assert select_response_content(assistant) == "Ready."


def test_interleaved_thinking_tool_and_response_steps_preserve_order() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "use tool", now=0.1)

    state = _apply(
        state,
        [
            {"type": "thinking", "content": "I should search. "},
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "search_memory",
                "args": {"query": "status"},
            },
            {
                "type": "tool_result",
                "id": "call-1",
                "name": "search_memory",
                "result": {"matches": 2},
            },
            {"type": "response", "content": "Found it."},
        ],
        start=1.0,
    )

    assistant = select_last_assistant_message(state)
    assert isinstance(assistant, AssistantMessage)
    assert [step.type for step in assistant.steps] == [
        "thinking",
        "tool_call",
        "response",
    ]
    assert isinstance(assistant.steps[0], ThinkingStep)
    assert isinstance(assistant.steps[1], ToolCallStep)
    assert isinstance(assistant.steps[2], ResponseStep)
    assert assistant.steps[1].status == "success"
    assert assistant.steps[1].result == {"matches": 2}
    assert assistant.content == "Found it."


def test_tool_result_can_match_running_tool_by_name_without_id() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "use tool", now=0.1)
    state = _apply(
        state,
        [
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "search_memory",
                "args": {"query": "status"},
            },
            {
                "type": "tool_result",
                "name": "search_memory",
                "result": "boom",
                "status": "error",
            },
        ],
        start=1.0,
    )

    assistant = select_last_assistant_message(state)
    assert isinstance(assistant, AssistantMessage)
    assert isinstance(assistant.steps[0], ToolCallStep)
    assert assistant.steps[0].id == "call-1"
    assert assistant.steps[0].status == "error"
    assert select_running_tool_calls(state) == ()


def test_activity_phase_formulates_after_quiet_thinking_period() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "think", now=0.1)
    state = reduce_stream_event(
        state,
        {"type": "thinking", "content": "working"},
        now=1.0,
    )

    assert select_activity_phase(state, now=1.5) == "thinking"
    assert select_activity_phase(state, now=2.01) == "formulating"

    state = reduce_stream_event(
        state,
        {"type": "response", "content": "Done."},
        now=2.2,
    )
    assert select_activity_phase(state, now=4.0) == "typing"


def test_compacted_event_replaces_transcript_with_notice() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "hello", now=0.1)
    state = reduce_stream_event(
        state,
        {
            "type": "compacted",
            "summary": "Earlier context summarized.",
            "messages_removed": 12,
            "auto_resumed": True,
        },
        now=1.0,
    )

    assert state.turn_status == "streaming"
    assert state.last_compact_result is not None
    assert state.last_compact_result.messages_removed == 12
    assert len(state.messages) == 2
    assert isinstance(state.messages[0], SystemMessage)
    assert state.messages[0].kind == "compaction_notice"
    assert isinstance(state.messages[1], AssistantMessage)
    assert state.messages[1].status == "streaming"


def test_dispatched_event_inserts_notice_before_streaming_assistant() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "@Research check this", now=0.1)

    state = reduce_stream_event(
        state,
        {
            "type": "dispatched",
            "thread_id": "thread-1",
            "target_thread_id": "thread-2",
            "title": "Research",
            "original_thread_id": "thread-1",
        },
        now=1.0,
    )
    state = reduce_stream_event(
        state,
        {"type": "response", "content": "Found it."},
        now=2.0,
    )
    state = reduce_stream_event(
        state,
        {
            "type": "done",
            "thread_id": "thread-1",
            "context_stats": {"thread_id": "thread-2", "input_tokens": 10},
            "model": "target-model",
            "dispatched_to": {"thread_id": "thread-2", "title": "Research"},
        },
        now=3.0,
    )

    assistant = select_last_assistant_message(state)
    assert isinstance(assistant, AssistantMessage)
    assert assistant.dispatch_info["content"] == "Response from Research (thread-2)"
    assert assistant.dispatch_info["thread_id"] == "thread-2"
    assert select_response_content(assistant) == "Found it."
    assert state.thread_id == "thread-1"
    assert state.context_stats == {}
    assert state.active_model == ""


def test_error_marks_assistant_and_turn_error() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "hello", now=0.1)
    state = reduce_stream_event(
        state,
        {
            "type": "error",
            "content": "Backend unavailable",
            "code": "connection_failed",
            "details": {"retryable": True},
        },
        now=1.0,
    )

    assistant = select_last_assistant_message(state)
    assert state.turn_status == "error"
    assert state.errors[0].code == "connection_failed"
    assert isinstance(assistant, AssistantMessage)
    assert assistant.status == "error"
    assert "Backend unavailable" in assistant.content


def test_workspace_artifact_attaches_to_matching_tool_call() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "write file", now=0.1)
    state = _apply(
        state,
        [
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "write_file",
                "args": {"path": "/tmp/out.txt"},
            },
            {
                "type": "workspace_artifact",
                "tool_call_id": "call-1",
                "tool_name": "write_file",
                "path": "/tmp/out.txt",
                "artifact": {
                    "path": "/tmp/out.txt",
                    "name": "out.txt",
                    "mime_type": "text/plain",
                    "size_bytes": 12,
                },
            },
        ],
        start=1.0,
    )

    assistant = select_last_assistant_message(state)
    assert isinstance(assistant, AssistantMessage)
    assert isinstance(assistant.steps[0], ToolCallStep)
    assert state.artifacts[0].path == "/tmp/out.txt"
    assert assistant.steps[0].artifacts[0].mime_type == "text/plain"


def test_autonomous_task_events_create_and_complete_assistant_turn() -> None:
    state = create_initial_state(thread_id="thread-1", user_id="alice", now=0.0)

    state = _apply(
        state,
        [
            {
                "type": "task_started",
                "thread_id": "thread-1",
                "task_id": "todo-1",
                "todo_id": "todo-1",
                "prompt": "Work on TODO todo-1: Check the smoke test",
            },
            {"type": "response", "thread_id": "thread-1", "content": "Smoke"},
            {
                "type": "task_completed",
                "thread_id": "thread-1",
                "task_id": "todo-1",
                "todo_id": "todo-1",
                "content": "Smoke test passed.",
            },
        ],
        start=1.0,
    )

    assert isinstance(state.messages[0], SystemMessage)
    assert state.messages[0].kind == "autonomous"
    assert "Check the smoke test" in state.messages[0].content
    assistant = select_last_assistant_message(state)
    assert isinstance(assistant, AssistantMessage)
    assert assistant.status == "complete"
    assert select_response_content(assistant) == "Smoke test passed."
    assert state.turn_status == "complete"


def test_autonomous_task_completed_without_chunks_renders_completion_content() -> None:
    state = create_initial_state(thread_id="thread-1", user_id="alice", now=0.0)

    state = reduce_stream_event(
        state,
        {
            "type": "task_completed",
            "thread_id": "thread-1",
            "task_id": "todo-1",
            "content": "Finished from aggregate content.",
        },
        now=1.0,
    )

    assistant = select_last_assistant_message(state)
    assert isinstance(assistant, AssistantMessage)
    assert assistant.status == "complete"
    assert select_response_content(assistant) == "Finished from aggregate content."


def test_fixture_streams_can_be_reduced_after_normalization() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "use fixture", now=0.1)
    events = [
        normalize_stream_event(event, default_thread_id="thread-1")
        for event in event_sequence("tool_call_result")
    ]

    state = _apply(state, events, start=1.0)

    assistant = select_last_assistant_message(state)
    assert state.turn_status == "complete"
    assert isinstance(assistant, AssistantMessage)
    assert [step.type for step in assistant.steps] == [
        "tool_call",
        "response",
    ]

def test_session_usage_skips_turns_marked_unrecorded() -> None:
    """``turn_recorded=False`` on the done event's context_stats means the
    server extracted no usage this turn; the client must not accumulate the
    zeros (or, on pre-repair servers, stale values) into session usage."""
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(
        state,
        "hello",
        now=0.1,
        user_message_id="user-1",
        assistant_message_id="assistant-1",
    )

    state = reduce_stream_event(
        state,
        {
            "type": "done",
            "thread_id": "thread-1",
            "context_stats": {
                "input_tokens": 120,
                "output_tokens": 40,
                "turn_recorded": True,
            },
            "model": "test-model",
        },
        now=1.0,
    )
    assert state.session_usage.total_input == 120
    assert state.session_usage.total_output == 40
    assert state.session_usage.turn_count == 1

    # Unrecorded turn: same non-zero numbers must NOT accumulate again.
    state = start_turn(
        state,
        "again",
        now=2.0,
        user_message_id="user-2",
        assistant_message_id="assistant-2",
    )
    state = reduce_stream_event(
        state,
        {
            "type": "done",
            "thread_id": "thread-1",
            "context_stats": {
                "input_tokens": 120,
                "output_tokens": 40,
                "turn_recorded": False,
            },
            "model": "test-model",
        },
        now=3.0,
    )
    assert state.session_usage.total_input == 120
    assert state.session_usage.turn_count == 1

    # Legacy server (no turn_recorded key): the zero-guard still applies,
    # non-zero values still accumulate.
    state = start_turn(
        state,
        "legacy",
        now=4.0,
        user_message_id="user-3",
        assistant_message_id="assistant-3",
    )
    state = reduce_stream_event(
        state,
        {
            "type": "done",
            "thread_id": "thread-1",
            "context_stats": {"input_tokens": 10, "output_tokens": 5},
            "model": "test-model",
        },
        now=5.0,
    )
    assert state.session_usage.total_input == 130
    assert state.session_usage.turn_count == 2
