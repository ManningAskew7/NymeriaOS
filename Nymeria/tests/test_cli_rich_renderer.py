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

    assert "You: use a tool" in output.stdout_text
    assert "> search_memory project status -> Found 2 matching notes." in (
        output.stdout_text
    )
    assert "I found the notes." in output.stdout_text


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

    assert "> search_memory project status -> Found 2 matching notes." in (
        output.stdout_text
    )
    assert "Done with markdown." in output.stdout_text


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
