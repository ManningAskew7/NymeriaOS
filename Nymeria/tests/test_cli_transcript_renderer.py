from __future__ import annotations

from dataclasses import replace

from rich.cells import cell_len

from nymeria.triggers.cli.rendering.markdown import render_markdown_lines
from nymeria.triggers.cli.rendering.tool_rows import (
    ToolRowRenderOptions,
    format_tool_row,
)
from nymeria.triggers.cli.rendering.transcript import (
    TranscriptRenderOptions,
    TranscriptRenderer,
    max_line_width,
    render_transcript_lines,
    render_transcript,
)
from nymeria.triggers.cli.state import (
    ResponseStep,
    create_initial_state,
    reduce_stream_event,
    start_turn,
)


def _apply(state, events, *, start: float = 1.0):
    next_state = state
    for offset, event in enumerate(events):
        next_state = reduce_stream_event(next_state, event, now=start + offset)
    return next_state


def _tool_artifact_state():
    state = create_initial_state(thread_id="thread-1", user_id="alice", now=0.0)
    state = start_turn(state, "inspect the project", now=0.1)
    return _apply(
        state,
        [
            {"type": "thinking", "content": "I should inspect files."},
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "filesystem_read",
                "args": {
                    "path": "/opt/NymeriaOS/Nymeria/docs/"
                    "cli-tui-refactor-task-breakdown.md"
                },
            },
            {
                "type": "workspace_artifact",
                "tool_call_id": "call-1",
                "tool_name": "filesystem_read",
                "path": "/opt/NymeriaOS/Nymeria/docs/"
                "cli-tui-refactor-task-breakdown.md",
                "artifact": {
                    "path": "/opt/NymeriaOS/Nymeria/docs/"
                    "cli-tui-refactor-task-breakdown.md",
                    "name": "cli-tui-refactor-task-breakdown.md",
                    "mime_type": "text/markdown",
                    "size_bytes": 2048,
                },
            },
            {
                "type": "tool_result",
                "id": "call-1",
                "name": "filesystem_read",
                "result": "line\n" * 200,
            },
            {
                "type": "response",
                "content": "I found **the task**.\n\n- It needs a transcript renderer.",
            },
            {"type": "done", "tool_call_count": 1},
        ],
        start=1.0,
    )


def test_transcript_snapshot_renders_desktop_like_steps() -> None:
    text = render_transcript(_tool_artifact_state(), width=100)
    lines = text.splitlines()

    assert lines[0].startswith("---- You ")
    assert lines[1] == "  inspect the project"
    assert lines[2] == ""
    assert lines[3].startswith("---- Nymeria ")
    assert lines[4] == "  Thought: I should inspect files."
    assert lines[5].startswith("  - filesystem_read ok 2.0s ")
    assert "-> line line" in lines[5]
    assert "[artifact: cli-tui-" in lines[5]
    assert lines[6] == "  I found the task."
    assert lines[7] == ""
    assert lines[8].strip() == "- It needs a transcript renderer."


def test_transcript_classifies_preamble_and_final_response_steps() -> None:
    state = create_initial_state(thread_id="thread-1", user_id="alice", now=0.0)
    state = start_turn(state, "what is my sunday briefing?", now=0.1)
    state = _apply(
        state,
        [
            {"type": "thinking", "content": "Plan quietly."},
            {"type": "response", "content": "I'll check the main sources first."},
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "web_search",
                "args": {"query": "AI news"},
            },
            {
                "type": "tool_result",
                "id": "call-1",
                "name": "web_search",
                "result": "3 results",
            },
            {"type": "response", "content": "I have news; checking tasks."},
            {
                "type": "tool_call",
                "id": "call-2",
                "name": "nym_todo",
                "args": {"action": "list"},
            },
            {
                "type": "tool_result",
                "id": "call-2",
                "name": "nym_todo",
                "result": "4 pending",
            },
            {
                "type": "response",
                "content": (
                    "# Sunday briefing, 10 May\n\n"
                    "## URGENT\nGoogle Cloud billing is suspended."
                ),
            },
            {"type": "done", "tool_call_count": 2},
        ],
        start=1.0,
    )

    records = render_transcript_lines(state, width=100)
    preamble = [line.text.strip() for line in records if line.kind == "preamble"]
    final = [line.text.strip() for line in records if line.kind == "final"]
    tools = [line.text.strip() for line in records if line.kind == "tool"]

    assert preamble == [
        "I'll check the main sources first.",
        "I have news; checking tasks.",
    ]
    assert tools[0].startswith("- web_search ok 1.0s query=\"AI news\" -> 3 results")
    assert tools[1].startswith("- nym_todo ok 1.0s action=list -> 4 pending")
    assert final[:5] == [
        "Sunday briefing, 10 May",
        "-----------------------",
        "URGENT",
        "------",
        "Google Cloud billing is suspended.",
    ]


def test_transcript_lines_are_bounded_at_common_widths() -> None:
    state = _tool_artifact_state()

    for width in (40, 80, 120, 160):
        text = render_transcript(state, width=width)

        assert max_line_width(text) <= width
        for line in text.splitlines():
            assert cell_len(line) <= width


def test_terminal_markdown_is_left_aligned_and_ascii_safe() -> None:
    lines = render_markdown_lines(
        "# Heading\n\n- `tool_call` stays readable\n> quoted\n\n```python\nx = 1\n```",
        width=40,
        ascii_only=True,
    )

    assert lines == [
        "Heading",
        "-------",
        "",
        "- tool_call stays readable",
        "> quoted",
        "",
        "    x = 1",
    ]
    assert all(cell_len(line) <= 40 for line in lines)


def test_transcript_uses_unicode_separators_and_tool_symbols_when_enabled() -> None:
    text = render_transcript(
        _tool_artifact_state(),
        width=100,
        options=TranscriptRenderOptions(ascii_only=False),
    )

    assert text.splitlines()[0].startswith("\u2500\u2500\u2500\u2500 You ")
    assert "\n\u2500\u2500\u2500\u2500 Nymeria " in text
    assert "  \u2713 filesystem_read ok 2.0s " in text


def test_long_tool_result_is_previewed_not_dumped() -> None:
    text = render_transcript(_tool_artifact_state(), width=80)

    assert "-> line line" in text
    assert ("line " * 80).strip() not in text
    assert max_line_width(text) <= 80


def test_standard_transcript_shows_thinking_as_one_line_preview() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "think", now=0.1)
    state = reduce_stream_event(
        state,
        {
            "type": "thinking",
            "content": "private reasoning\nwith a second line that stays collapsed",
        },
        now=1.0,
    )

    default_text = render_transcript(state, width=80)
    default_lines = default_text.splitlines()

    assert "  Thinking: private reasoning with a second line that stays collapsed" in default_lines

    complete = reduce_stream_event(state, {"type": "done"}, now=2.0)
    complete_text = render_transcript(complete, width=80)

    assert "Thought: private reasoning with a second line that stays collapsed" in complete_text
    assert all(line.count("private reasoning") <= 1 for line in default_lines)


def test_standard_thinking_preview_is_bounded_to_width() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "think", now=0.1)
    state = reduce_stream_event(
        state,
        {"type": "thinking", "content": "word " * 80},
        now=1.0,
    )

    text = render_transcript(state, width=44)

    assert "  Thinking: word word word" in text
    assert max_line_width(text) <= 44


def test_verbose_transcript_expands_hidden_details() -> None:
    text = render_transcript(
        _tool_artifact_state(),
        width=100,
        options=TranscriptRenderOptions(verbose=True),
    )

    assert "I should inspect files." in text
    assert "args:" in text
    assert "result:" in text
    assert "... truncated ..." in text
    assert max_line_width(text) <= 100


def test_verbose_transcript_renders_full_thinking_text() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "think", now=0.1)
    thinking = f"{'reasoning ' * 120}tail-visible"
    state = reduce_stream_event(
        state,
        {"type": "thinking", "content": thinking},
        now=1.0,
    )

    text = render_transcript(
        state,
        width=80,
        options=TranscriptRenderOptions(verbose=True),
    )

    assert "tail-visible" in text
    assert "... truncated ..." not in text
    assert max_line_width(text) <= 80


def test_tool_row_can_include_duration_when_enabled() -> None:
    state = _tool_artifact_state()
    tool = state.active_tool_calls["call-1"]

    row = format_tool_row(
        tool,
        width=100,
        options=ToolRowRenderOptions(show_duration=True),
    )

    assert "2.0s" in row
    assert cell_len(row) <= 100


def test_transcript_renderer_reuses_unchanged_message_blocks() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "hello", now=0.1)
    state = reduce_stream_event(
        state,
        {"type": "response", "content": "Hello"},
        now=1.0,
    )
    renderer = TranscriptRenderer()

    renderer.render(state, width=80)
    first_count = renderer.rendered_message_count

    assistant = state.messages[-1]
    assert hasattr(assistant, "steps")
    step = assistant.steps[-1]
    assert isinstance(step, ResponseStep)
    updated_step = replace(step, content="Hello there.")
    updated_assistant = replace(assistant, steps=assistant.steps[:-1] + (updated_step,))
    updated_state = replace(state, messages=state.messages[:-1] + (updated_assistant,))

    renderer.render(updated_state, width=80)

    assert first_count == 2
    assert renderer.rendered_message_count == 3


def test_autonomous_output_has_distinct_header() -> None:
    state = create_initial_state(thread_id="thread-1", user_id="alice", now=0.0)
    state = _apply(
        state,
        [
            {
                "type": "task_started",
                "thread_id": "thread-1",
                "task_id": "task-1",
                "todo_id": "todo-1",
                "source": "scheduler",
                "prompt": "Work on TODO todo-1: Run a CLI smoke test",
            },
            {"type": "response", "thread_id": "thread-1", "content": "Smoke"},
            {
                "type": "task_completed",
                "thread_id": "thread-1",
                "task_id": "task-1",
                "todo_id": "todo-1",
                "content": "Smoke test passed.",
            },
        ],
        start=1.0,
    )

    text = render_transcript(state, width=100)

    assert "Nymeria - autonomous - scheduler" in text
    assert "Autonomous TODO started: Run a CLI smoke test" in text
    assert "  Smoke test passed." in text


def test_compacted_context_notice_renders_as_system_turn() -> None:
    state = create_initial_state(thread_id="thread-1", user_id="alice", now=0.0)
    state = _apply(
        state,
        [
            {
                "type": "compacted",
                "summary": "Earlier context was summarized.",
                "messages_removed": 12,
            }
        ],
        start=1.0,
    )

    assert render_transcript(state, width=100).splitlines() == [
        "---- System ----------------------------------------------------------------------------------------",
        "  Context compacted. Earlier context was summarized. Removed 12 messages.",
    ]


def test_tool_error_cancelled_and_running_states_are_compact() -> None:
    state = create_initial_state(thread_id="thread-1", user_id="alice", now=0.0)
    state = start_turn(state, "tools", now=0.1)
    running_state = _apply(
        state,
        [{"type": "tool_call", "id": "running", "name": "slow", "args": {}}],
        start=1.0,
    )
    running_rows = [
        line.text.strip()
        for line in render_transcript_lines(running_state, width=80)
        if line.kind == "tool"
    ]

    assert running_rows == ["- slow running"]

    state = _apply(
        running_state,
        [
            {"type": "tool_call", "id": "error", "name": "bad", "args": {}},
            {
                "type": "tool_result",
                "id": "error",
                "name": "bad",
                "status": "error",
                "result": "failed",
            },
            {"type": "error", "content": "Cancelled.", "code": "cancelled"},
        ],
        start=1.0,
    )

    tool_rows = [
        line.text.strip()
        for line in render_transcript_lines(state, width=80)
        if line.kind == "tool"
    ]

    assert any(row.startswith("! slow cancelled") for row in tool_rows)
    assert any(row.startswith("x bad error") and "-> failed" in row for row in tool_rows)
