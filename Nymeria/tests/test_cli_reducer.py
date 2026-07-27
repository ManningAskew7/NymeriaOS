from __future__ import annotations

from dataclasses import replace

from cli_fixtures import event_sequence

from nymeria.triggers.cli.events import normalize_stream_event
from nymeria.triggers.cli.state import (
    AssistantMessage,
    ResponseStep,
    SystemMessage,
    ThinkingStep,
    ToolCallStep,
    UserMessage,
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


def test_activity_phase_quiet_transitions() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "think", now=0.1)
    # Quiet processing (nothing streamed yet) demotes to formulating.
    assert select_activity_phase(state, now=0.5) == "processing"
    assert select_activity_phase(state, now=1.2) == "formulating"

    state = reduce_stream_event(
        state,
        {"type": "thinking", "content": "working"},
        now=1.5,
    )
    # Thinking is sticky through sparse reasoning-delta gaps.
    assert select_activity_phase(state, now=2.0) == "thinking"
    assert select_activity_phase(state, now=9.0) == "thinking"

    state = reduce_stream_event(
        state,
        {"type": "response", "content": "Done."},
        now=9.2,
    )
    # Typing stays honest through short gaps, then reads as finalizing while
    # the server wraps up the turn ahead of the done event.
    assert select_activity_phase(state, now=10.0) == "typing"
    assert select_activity_phase(state, now=11.0) == "finalizing"


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


def test_turn_rewound_removes_refused_exchange_and_appends_notice() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    # A first, healthy exchange stays put.
    state = start_turn(
        state, "hi there", now=0.1,
        user_message_id="user-0", assistant_message_id="assistant-0",
    )
    state = reduce_stream_event(
        state, {"type": "response", "content": "hello!"}, now=0.2
    )
    state = reduce_stream_event(
        state,
        {"type": "done", "thread_id": "thread-1", "tool_call_count": 0},
        now=0.3,
    )
    # The refused turn: user prompt + empty streaming assistant.
    state = start_turn(
        state, "poke the sandbox", now=1.0,
        user_message_id="user-1", assistant_message_id="assistant-1",
    )
    before = len(state.messages)

    state = reduce_stream_event(
        state,
        {
            "type": "turn_rewound",
            "thread_id": "thread-1",
            "reason": "refusal",
            "removed": 2,
            "to_message_id": "user-1",
            "prompt": "poke the sandbox",
            "model": "claude-fable-5",
            "content": "The classifier declined this turn.",
        },
        now=1.5,
    )

    # The refused user+assistant pair is gone; the healthy exchange survives.
    assert before == 4  # user-0, assistant-0, user-1, assistant-1
    kinds = [type(m).__name__ for m in state.messages]
    assert kinds == ["UserMessage", "AssistantMessage", "SystemMessage"]
    assert state.messages[0].id == "user-0"
    notice = state.messages[-1]
    assert isinstance(notice, SystemMessage)
    assert notice.kind == "turn_rewound"
    assert notice.content == "The classifier declined this turn."
    assert notice.details["model"] == "claude-fable-5"
    # The removed assistant was the current one; the pointer is cleared.
    assert state.current_assistant_id is None


def test_turn_rewound_autonomous_preserves_prior_interactive_exchange() -> None:
    # An autonomous turn (TODO/trigger) paints no UserMessage, so a
    # last-UserMessage truncation would wrongly delete the prior interactive
    # exchange. The autonomous branch appends the notice only.
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(
        state, "what is 2+2", now=0.1,
        user_message_id="user-0", assistant_message_id="assistant-0",
    )
    state = reduce_stream_event(
        state, {"type": "response", "content": "4"}, now=0.2
    )
    state = reduce_stream_event(
        state, {"type": "done", "thread_id": "thread-1", "tool_call_count": 0}, now=0.3
    )
    # An autonomous turn wakes on the same viewed thread.
    state = reduce_stream_event(
        state,
        {"type": "task_started", "task_id": "todo-1", "prompt": "check logs",
         "source": "scheduler"},
        now=1.0,
    )
    state = reduce_stream_event(
        state,
        {
            "type": "turn_rewound",
            "thread_id": "thread-1",
            "reason": "refusal",
            "autonomous": True,
            "prompt": "check logs",
            "content": "Declined.",
        },
        now=1.5,
    )
    kinds = [type(m).__name__ for m in state.messages]
    # The completed interactive exchange (user-0 + assistant-0) survives; only
    # a notice is appended (plus the autonomous turn's own system+assistant).
    assert state.messages[0].id == "user-0"
    assert kinds.count("UserMessage") == 1
    assert kinds[-1] == "SystemMessage"
    assert state.messages[-1].kind == "turn_rewound"


def test_turn_rewound_on_first_turn_leaves_only_the_notice() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(
        state, "poke the sandbox", now=0.1,
        user_message_id="user-1", assistant_message_id="assistant-1",
    )
    state = reduce_stream_event(
        state,
        {
            "type": "turn_rewound",
            "thread_id": "thread-1",
            "reason": "refusal",
            "prompt": "poke the sandbox",
            "content": "Declined.",
        },
        now=1.0,
    )
    assert len(state.messages) == 1
    assert isinstance(state.messages[0], SystemMessage)
    assert state.messages[0].kind == "turn_rewound"


def test_turn_rewound_cuts_a_whole_consecutive_user_run() -> None:
    # A queued-prompt batch paints consecutive UserMessages; the server
    # rewinds the whole run, so the local cut must start at its first prompt.
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(
        state, "hi there", now=0.1,
        user_message_id="user-0", assistant_message_id="assistant-0",
    )
    state = reduce_stream_event(
        state, {"type": "response", "content": "hello!"}, now=0.2
    )
    state = reduce_stream_event(
        state, {"type": "done", "thread_id": "thread-1", "tool_call_count": 0},
        now=0.3,
    )
    state = start_turn(
        state, "first queued", now=1.0,
        user_message_id="user-1", assistant_message_id="assistant-1",
    )
    # A second queued prompt injected into the same turn paints another
    # UserMessage directly after the first.
    state = replace(
        state,
        messages=state.messages[:-1]
        + (
            UserMessage(id="user-2", content="second queued", timestamp=1.1),
            state.messages[-1],
        ),
    )
    state = reduce_stream_event(
        state,
        {
            "type": "turn_rewound",
            "thread_id": "thread-1",
            "reason": "refusal",
            "removed": 3,
            "to_message_id": "user-1",
            "prompt": "first queued\n\nsecond queued",
            "content": "Declined.",
        },
        now=1.5,
    )
    kinds = [type(m).__name__ for m in state.messages]
    assert kinds == ["UserMessage", "AssistantMessage", "SystemMessage"]
    assert state.messages[0].id == "user-0"
    assert state.messages[-1].kind == "turn_rewound"


def test_turn_rewound_reduced_twice_never_cuts_the_prior_exchange() -> None:
    # Replay defense: seq bookkeeping makes re-attach replay suffix-only
    # today, but a duplicated turn_rewound frame must still be harmless. The
    # second reduce stops at the first notice instead of walking past it and
    # deleting the healthy prior exchange.
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(
        state, "hi there", now=0.1,
        user_message_id="user-0", assistant_message_id="assistant-0",
    )
    state = reduce_stream_event(
        state, {"type": "response", "content": "hello!"}, now=0.2
    )
    state = reduce_stream_event(
        state, {"type": "done", "thread_id": "thread-1", "tool_call_count": 0},
        now=0.3,
    )
    state = start_turn(
        state, "poke the sandbox", now=1.0,
        user_message_id="user-1", assistant_message_id="assistant-1",
    )
    event = {
        "type": "turn_rewound",
        "thread_id": "thread-1",
        "reason": "refusal",
        "removed": 2,
        "to_message_id": "user-1",
        "prompt": "poke the sandbox",
        "content": "Declined.",
    }
    state = reduce_stream_event(state, event, now=1.5)
    state = reduce_stream_event(state, event, now=1.6)
    kinds = [type(m).__name__ for m in state.messages]
    # The healthy exchange survives both reduces; the duplicate only appends
    # a second notice.
    assert kinds == [
        "UserMessage", "AssistantMessage", "SystemMessage", "SystemMessage",
    ]
    assert state.messages[0].id == "user-0"


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


def test_provider_fallback_and_retry_reduce_to_status_notices() -> None:
    """Model switches and retries render as one transcript line each instead
    of falling through to a raw unknown-event diagnostic (Phase 3)."""
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "hello", now=0.1)
    state = reduce_stream_event(
        state,
        {
            "type": "provider_retry",
            "provider": "anthropic",
            "model": "claude-fable-5",
            "attempt": 1,
            "max_retries": 2,
            "reason": "timeout",
        },
        now=1.0,
    )
    state = reduce_stream_event(
        state,
        {
            "type": "provider_fallback",
            "from_model": "claude-fable-5",
            "to_model": "claude-haiku-4-5-20251001",
            "hold_seconds": 7200,
            "reason": "provider_server_error",
            "http_status": 529,
        },
        now=2.0,
    )

    notices = [
        message
        for message in state.messages
        if isinstance(message, SystemMessage) and message.kind == "provider_status"
    ]
    assert len(notices) == 2
    assert "Retrying claude-fable-5 (1/2): timeout." == notices[0].content
    assert (
        "Switched to fallback claude-haiku-4-5-20251001 for 2 hours "
        "(provider_server_error, HTTP 529). Revert with /fallback revert."
        == notices[1].content
    )
    assert not state.diagnostics


def test_provider_fallback_refusal_copy_is_distinct() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = reduce_stream_event(
        state,
        {
            "type": "provider_fallback",
            "from_model": "claude-fable-5",
            "to_model": "claude-opus-4-8",
            "permanent": True,
            "reason": "refusal",
        },
        now=1.0,
    )
    notice = state.messages[-1]
    assert isinstance(notice, SystemMessage)
    assert notice.kind == "provider_status"
    assert "refused this turn" in notice.content
    assert "not an error" in notice.content
    assert "until reverted" in notice.content


def test_provider_fallback_rewound_notice_flags_superseded_output() -> None:
    # The mid-stream recovery shape rolls the turn back and re-drives it; the
    # CLI transcript keeps the already-rendered partial text, so the notice
    # must say it was superseded.
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = reduce_stream_event(
        state,
        {
            "type": "provider_fallback",
            "from_model": "claude-fable-5",
            "to_model": "claude-opus-4-8",
            "hold_seconds": 7200,
            "reason": "stream_error",
            "rewound": True,
            "stream_chunks": 12,
        },
        now=1.0,
    )
    notice = state.messages[-1]
    assert isinstance(notice, SystemMessage)
    assert "superseded" in notice.content


def test_provider_fallback_hold_phrase_matches_bot_surfaces() -> None:
    # The reducer feeds the SHARED formatter: a 90s hold must render "for 1
    # min" exactly like the bots (the retired local helper said "for 90s").
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = reduce_stream_event(
        state,
        {
            "type": "provider_fallback",
            "to_model": "claude-opus-4-8",
            "hold_seconds": 90,
            "reason": "provider_server_error",
        },
        now=1.0,
    )
    notice = state.messages[-1]
    assert isinstance(notice, SystemMessage)
    assert "for 1 min" in notice.content


# ---------------------------------------------------------------------------
# Turn wall-time stamping (turn-summary line)
# ---------------------------------------------------------------------------


def test_turn_timing_stamped_on_start_and_closed_on_done() -> None:
    state = create_initial_state(thread_id="t", now=0.0)
    state = start_turn(state, "hello", now=10.0)
    assert state.turn_started_at == 10.0
    state = reduce_stream_event(state, {"type": "response", "content": "hi"}, now=11.0)
    state = reduce_stream_event(state, {"type": "done"}, now=22.5)
    assert state.turn_started_at is None
    assert state.last_turn_duration_seconds == 12.5
    assert state.last_turn_outcome == "complete"


def test_turn_timing_survives_cancelled_error_and_closes_on_done() -> None:
    state = create_initial_state(thread_id="t", now=0.0)
    state = start_turn(state, "hello", now=5.0)
    state = reduce_stream_event(
        state,
        {"type": "error", "content": "stopped", "code": "cancelled"},
        now=6.0,
    )
    # The cancelled-code error is not terminal: the done that follows the
    # stop closes the turn and its timing with the cancelled outcome.
    assert state.turn_started_at == 5.0
    state = reduce_stream_event(
        state, {"type": "done", "status": "cancelled"}, now=13.0
    )
    assert state.last_turn_duration_seconds == 8.0
    assert state.last_turn_outcome == "cancelled"


def test_turn_timing_closes_on_terminal_stream_error() -> None:
    state = create_initial_state(thread_id="t", now=0.0)
    state = start_turn(state, "hello", now=1.0)
    state = reduce_stream_event(
        state,
        {"type": "error", "content": "boom", "code": "provider_error"},
        now=4.5,
    )
    assert state.turn_started_at is None
    assert state.last_turn_duration_seconds == 3.5
    assert state.last_turn_outcome == "error"


def test_no_turn_timing_without_local_turn_start() -> None:
    """A mid-turn viewer attach (done without a locally-seen start) must not
    record a misleading partial duration. The outcome is still stamped so
    stats-derived summary segments can tell how the turn ended."""
    state = create_initial_state(thread_id="t", now=0.0)
    state = reduce_stream_event(state, {"type": "response", "content": "x"}, now=1.0)
    state = reduce_stream_event(state, {"type": "done"}, now=2.0)
    assert state.last_turn_duration_seconds is None
    assert state.last_turn_outcome == "complete"


def test_attach_turn_end_clears_previous_turn_timing() -> None:
    """A turn observed only mid-flight must not inherit the PREVIOUS turn's
    recorded duration onto its own summary line."""
    state = create_initial_state(thread_id="t", now=0.0)
    state = start_turn(state, "hello", now=0.0)
    state = reduce_stream_event(state, {"type": "done"}, now=12.5)
    assert state.last_turn_duration_seconds == 12.5

    # Next turn: the start was never seen locally (viewer attach), only its
    # tail. Closing it clears the stale 12.5s instead of carrying it over.
    state = reduce_stream_event(state, {"type": "response", "content": "x"}, now=20.0)
    state = reduce_stream_event(state, {"type": "done"}, now=21.0)
    assert state.last_turn_duration_seconds is None
    assert state.last_turn_outcome == "complete"


def test_redundant_done_after_terminal_error_preserves_timing() -> None:
    """The done that can trail a terminal stream error is redundant: the
    error already closed and rendered the turn, so its recorded timing must
    survive (no turn is in flight to clear it for)."""
    state = create_initial_state(thread_id="t", now=0.0)
    state = start_turn(state, "hello", now=1.0)
    state = reduce_stream_event(
        state,
        {"type": "error", "content": "boom", "code": "provider_error"},
        now=4.5,
    )
    assert state.last_turn_duration_seconds == 3.5
    state = reduce_stream_event(state, {"type": "done"}, now=5.0)
    assert state.last_turn_duration_seconds == 3.5
    assert state.last_turn_outcome == "error"


def test_autonomous_task_turn_timing() -> None:
    state = create_initial_state(thread_id="t", now=0.0)
    state = reduce_stream_event(
        state,
        {"type": "task_started", "task_id": "todo-1", "prompt": "check logs",
         "source": "scheduler"},
        now=1.0,
    )
    assert state.turn_started_at == 1.0
    state = reduce_stream_event(
        state,
        {"type": "task_completed", "task_id": "todo-1", "content": "done"},
        now=31.0,
    )
    assert state.turn_started_at is None
    assert state.last_turn_duration_seconds == 30.0
    assert state.last_turn_outcome == "complete"
