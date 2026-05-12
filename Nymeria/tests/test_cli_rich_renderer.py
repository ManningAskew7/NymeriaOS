from __future__ import annotations

import re

from cli_fixtures import CapturedRenderOutput, FakeTerminalCapabilities

from nymeria.triggers.cli.rendering.rich_repl import RichReplRenderer
from nymeria.triggers.cli.state import (
    create_initial_state,
    reduce_stream_event,
    start_turn,
)

ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _reduce_tool_state():
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "use a tool", now=0.1)
    for offset, event in enumerate(
        [
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "search_memory",
                "args": {"query": "project status"},
            },
            {
                "type": "tool_result",
                "id": "call-1",
                "name": "search_memory",
                "result": "Found 2 matching notes.",
            },
            {
                "type": "response",
                "content": "I found the **notes**.",
            },
            {"type": "done", "tool_call_count": 1},
        ]
    ):
        state = reduce_stream_event(state, event, now=1.0 + offset)
    return state


def test_rich_renderer_renders_transcript_from_reducer_state() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        state=_reduce_tool_state(),
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
    )

    renderer.render_state()

    assert "──── You " in output.stdout_text
    assert "  use a tool" in output.stdout_text
    assert "──── Nymeria " in output.stdout_text
    assert "✓ search_memory ok 1.0s query=\"project status\" -> Found 2 matching notes." in (
        output.stdout_text
    )
    assert "I found the notes." in output.stdout_text
    assert "································" in output.stdout_text


def test_rich_renderer_streams_via_state_diffs_and_compact_tool_rows() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
    )
    renderer.start_turn("use a tool", thread_id="thread-1", now=0.0)

    renderer.render_events(
        [
            {"type": "thinking", "content": "checking"},
            {"type": "response", "content": "I will check memory."},
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "search_memory",
                "args": {"query": "project status"},
            },
            {
                "type": "tool_result",
                "id": "call-1",
                "name": "search_memory",
                "result": "Found 2 matching notes.",
            },
            {"type": "response", "content": "Done with **markdown**."},
            {"type": "done", "tool_call_count": 1},
        ],
        now=1.0,
    )

    assert "──── You " in output.stdout_text
    assert "  use a tool" in output.stdout_text
    assert "──── Nymeria " in output.stdout_text
    assert "│ checking" in output.stdout_text
    assert "I will check memory." in output.stdout_text
    assert "✓ search_memory ok 0ms query=\"project status\" -> Found 2 matching notes." in (
        output.stdout_text
    )
    assert "Done with markdown." in output.stdout_text
    assert "································" in output.stdout_text


def test_rich_renderer_verbose_thinking_streams_deltas() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
    )
    renderer.transcript_verbose = True
    renderer.start_turn("think", thread_id="thread-1", now=0.0)

    renderer.render_events(
        [
            {"type": "thinking", "content": "first chunk"},
            {"type": "thinking", "content": " second chunk"},
            {"type": "response", "content": "Done."},
            {"type": "done", "tool_call_count": 0},
        ],
        now=1.0,
    )

    assert "│ first chunk" in output.stdout_text
    assert "│ second chunk" in output.stdout_text
    assert "Done." in output.stdout_text


def test_rich_renderer_standard_thinking_preview_marks_cutoff() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=58,
    )
    renderer.start_turn("think", thread_id="thread-1", now=0.0)

    renderer.render_event(
        {
            "type": "thinking",
            "content": (
                "The user is asking for a harmless set of tool calls, so I will "
                "pick read-only operations."
            ),
        },
        now=1.0,
    )

    thinking_line = next(
        line for line in output.stdout_text.splitlines() if "│ " in line
    )
    assert thinking_line.endswith("...")
    assert len(thinking_line) <= 58


def test_rich_renderer_verbose_thinking_wraps_full_text() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=46,
    )
    renderer.transcript_verbose = True
    renderer.start_turn("think", thread_id="thread-1", now=0.0)

    renderer.render_event(
        {
            "type": "thinking",
            "content": (
                "This verbose thinking line should wrap instead of losing the "
                "later words entirely."
            ),
        },
        now=1.0,
    )

    assert "instead of losing the later words" in output.stdout_text
    assert "entirely." in output.stdout_text


def test_rich_renderer_prints_response_delta_before_turn_done() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
    )
    renderer.start_turn("stream", thread_id="thread-1", now=0.0)

    renderer.render_event(
        {"type": "response", "content": "Streaming **now**."},
        now=1.0,
    )

    assert "Streaming now." in output.stdout_text
    assert "  Streaming now.\n" in output.stdout_text
    lines = output.stdout_text.splitlines()
    header_index = next(index for index, line in enumerate(lines) if "──── Nymeria " in line)
    opening_divider_index = next(
        index for index, line in enumerate(lines) if "································" in line
    )
    response_index = next(index for index, line in enumerate(lines) if "Streaming now." in line)
    assert header_index < opening_divider_index < response_index
    assert output.stdout_text.count("································") == 1


def test_rich_renderer_streams_response_lines_without_cutting_text() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=90,
    )
    renderer.start_turn("summarize checks", thread_id="thread-1", now=0.0)

    renderer.render_events(
        [
            {"type": "response", "content": "Here is a break"},
            {
                "type": "response",
                "content": "down:\n\n- Thread Memory -- empty (fresh thread)\n",
            },
            {"type": "response", "content": "- Global Memory -- 26 entries loaded fine\n"},
            {"type": "done", "tool_call_count": 0},
        ],
        now=1.0,
    )

    assert "  Here is a breakdown:" in output.stdout_text
    assert "  - Thread Memory -- empty (fresh thread)" in output.stdout_text
    assert "  - Global Memory -- 26 entries loaded fine" in output.stdout_text
    assert "\nNymeria |" not in output.stdout_text


def test_rich_renderer_standard_mode_shows_one_preview_per_thinking_step() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
    )
    renderer.start_turn("think once", thread_id="thread-1", now=0.0)

    renderer.render_events(
        [
            {"type": "thinking", "content": "first private thought"},
            {"type": "thinking", "content": " continued private detail"},
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "memory_read",
                "args": {"scope": "thread"},
            },
            {
                "type": "tool_result",
                "id": "call-1",
                "name": "memory_read",
                "result": "[empty]",
            },
            {"type": "thinking", "content": "second private thought"},
            {"type": "response", "content": "Visible answer."},
            {"type": "done", "tool_call_count": 1},
        ],
        now=1.0,
    )

    assert "│ first private thought continued private detail" in output.stdout_text
    assert "│ second private thought" in output.stdout_text
    assert sum(1 for line in output.stdout_text.splitlines() if "│ " in line) == 2
    assert "Visible answer." in output.stdout_text


def test_rich_renderer_standard_thinking_preview_waits_until_width_or_step_boundary() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=120,
    )
    renderer.start_turn("hello", thread_id="thread-1", now=0.0)

    renderer.render_event(
        {
            "type": "thinking",
            "content": "The user is saying hello. Let me check my",
        },
        now=1.0,
    )

    assert "Let me check my..." not in output.stdout_text

    renderer.render_events(
        [
            {
                "type": "thinking",
                "content": " memory and thread context before replying.",
            },
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "memory_read",
                "args": {"scope": "global"},
            },
            {
                "type": "tool_result",
                "id": "call-1",
                "name": "memory_read",
                "result": "Stored memories.",
            },
        ],
        now=2.0,
    )

    assert "Let me check my..." not in output.stdout_text
    assert "Let me check my memory and thread context before replying..." in (
        output.stdout_text
    )


def test_rich_renderer_renders_autonomous_turns_with_distinct_header() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=110,
    )

    renderer.render_events(
        [
            {
                "type": "task_started",
                "thread_id": "thread-1",
                "task_id": "todo-1",
                "todo_id": "todo-1",
                "source": "scheduler",
                "prompt": "Work on TODO todo-1: Run a CLI smoke test",
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
        now=1.0,
    )

    assert "Nymeria · autonomous · scheduler" in output.stdout_text
    assert "Autonomous TODO started: Run a CLI smoke test" in output.stdout_text
    assert "Smoke test passed." in output.stdout_text
    assert "································" in output.stdout_text


def test_rich_renderer_shows_user_attachment_count() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
    )

    renderer.start_turn(
        "summarize this",
        thread_id="thread-1",
        attachments=({"filename": "note.txt", "mime_type": "text/plain"},),
        now=0.0,
    )

    assert "  summarize this" in output.stdout_text
    assert "  1 attachment" in output.stdout_text


def test_rich_renderer_respects_no_color_capability() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
    )
    renderer.start_turn("hello", thread_id="thread-1", now=0.0)

    renderer.render_events(
        [
            {"type": "response", "content": "Hello **there**."},
            {
                "type": "error",
                "content": "Backend unavailable",
                "code": "connection_failed",
            },
        ],
        now=1.0,
    )

    assert "Hello there." in output.stdout_text
    assert "Error: Backend unavailable" in output.stderr_text
    assert not ANSI_RE.search(output.stdout_text)
    assert not ANSI_RE.search(output.stderr_text)
