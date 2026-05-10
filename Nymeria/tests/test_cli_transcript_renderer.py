from __future__ import annotations

from dataclasses import replace

from rich.cells import cell_len

from nymeria.triggers.cli.rendering.tool_rows import (
    ToolRowRenderOptions,
    format_tool_row,
)
from nymeria.triggers.cli.rendering.transcript import (
    TranscriptRenderOptions,
    TranscriptRenderer,
    max_line_width,
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

    assert lines[0] == "You: inspect the project"
    assert lines[1] == "Thought"
    assert lines[2].startswith("> filesystem_read ")
    assert "-> line line" in lines[2]
    assert "[artifact: cli-tui-ref" in lines[2]
    assert lines[3].startswith("  artifact: /opt/NymeriaOS/")
    assert lines[4] == "Nymeria: I found the task."
    assert lines[5] == ""
    assert lines[6].strip() == "- It needs a transcript renderer."


def test_transcript_lines_are_bounded_at_common_widths() -> None:
    state = _tool_artifact_state()

    for width in (40, 80, 120, 160):
        text = render_transcript(state, width=width)

        assert max_line_width(text) <= width
        for line in text.splitlines():
            assert cell_len(line) <= width


def test_long_tool_result_is_previewed_not_dumped() -> None:
    text = render_transcript(_tool_artifact_state(), width=80)

    assert "-> line line" in text
    assert ("line " * 80).strip() not in text
    assert max_line_width(text) <= 80


def test_streaming_thinking_content_is_only_visible_when_configured() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "think", now=0.1)
    state = reduce_stream_event(
        state,
        {"type": "thinking", "content": "private reasoning"},
        now=1.0,
    )

    default_text = render_transcript(state, width=80)
    visible_text = render_transcript(
        state,
        width=80,
        options=TranscriptRenderOptions(show_streaming_thinking=True),
    )

    assert "Thinking..." in default_text
    assert "private reasoning" not in default_text
    assert "private reasoning" in visible_text

    complete = reduce_stream_event(state, {"type": "done"}, now=2.0)
    complete_text = render_transcript(
        complete,
        width=80,
        options=TranscriptRenderOptions(show_streaming_thinking=True),
    )

    assert "Thought" in complete_text
    assert "private reasoning" not in complete_text


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
