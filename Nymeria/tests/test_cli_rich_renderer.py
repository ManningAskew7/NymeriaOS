from __future__ import annotations

import asyncio
import io
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cli_fixtures import CapturedRenderOutput, FakeAgentClient, FakeTerminalCapabilities
from rich.console import Console

from nymeria.triggers.cli.app import CLIApp, CLIRuntimeConfig, _RichReplRuntime
from nymeria.triggers.cli.history import cli_state_from_history
from nymeria.triggers.cli.rendering.rich_markdown import (
    DEFAULT_CODE_THEME,
    MarkdownStreamBuffer,
    RichMarkdownAdapter,
    rich_markdown_theme,
)
from nymeria.triggers.cli.rendering.rich_repl import RichReplRenderer
from nymeria.triggers.cli.state import (
    AssistantMessage,
    ResponseStep,
    ThinkingStep,
    ToolCallStep,
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


def test_rich_markdown_adapter_themes_blocks_and_tables() -> None:
    stream = io.StringIO()
    console = Console(
        file=stream,
        width=80,
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
    )
    adapter = RichMarkdownAdapter()
    content = "\n".join(
        [
            "## Heading",
            "",
            "Here is **bold**, *italic*, `code`, and [docs](https://example.com).",
            "",
            "> quote",
            "",
            "- item",
            "",
            "Name | Value",
            "--- | ---:",
            "**alpha** | 42",
            "",
            "```python",
            "print('hi')",
            "```",
        ]
    )
    theme = rich_markdown_theme()

    assert any(token.type == "table_open" for token in adapter.parse(content))
    assert adapter.print(console, content) is True
    assert DEFAULT_CODE_THEME == "nord"
    assert str(theme.styles["markdown.h2"]) == "bold #ffffff"
    assert str(theme.styles["markdown.table.header"]) == "bold #ffffff"
    assert str(theme.styles["markdown.link"]) == "#9ccffb"
    assert str(theme.styles["markdown.code"]) == "#e7d6ff"

    text = stream.getvalue()
    assert "\x1b[" in text
    assert "Heading" in text
    assert "\x1b[1mbold\x1b[0m" in text
    assert "code" in text
    assert "▌" in text
    assert "alpha" in text
    assert "print" in text
    assert "--- | ---" not in text


def test_markdown_stream_buffer_commits_only_stable_blocks() -> None:
    buffer = MarkdownStreamBuffer()

    assert buffer.append("Paragraph with **bold**") == []
    assert buffer.append("\n\nName | Value\n--- | ---:\n") == [
        "Paragraph with **bold**"
    ]
    assert buffer.append("**alpha** | 42\n") == []
    assert buffer.append("\nNext paragraph") == [
        "Name | Value\n--- | ---:\n**alpha** | 42"
    ]
    assert buffer.flush() == ["Next paragraph"]


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


def test_update_terminal_width_changes_all_widths() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=80,
    )

    assert renderer.update_terminal_width(120) is True
    assert renderer.width == 120
    assert renderer.console.width == 120
    assert renderer.error_console.width == 120

    assert renderer.update_terminal_width(120) is False
    assert renderer.width == 120


def test_update_terminal_width_affects_rendering() -> None:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "show width", now=0.1)
    state = reduce_stream_event(
        state,
        {"type": "response", "content": "Width-sensitive transcript."},
        now=1.0,
    )
    state = reduce_stream_event(state, {"type": "done"}, now=2.0)
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        state=state,
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=60,
    )

    renderer.render_state()
    narrow_rule = next(
        line for line in output.stdout_text.splitlines() if " You " in line
    )

    output.clear()
    renderer.update_terminal_width(120)
    renderer.render_state()
    wide_rule = next(
        line for line in output.stdout_text.splitlines() if " You " in line
    )

    assert len(narrow_rule) == 60
    assert len(wide_rule) == 120


def test_rich_render_state_matches_live_markdown_styles_and_tables() -> None:
    content = "\n".join(
        [
            "Here is **bold** and `code`.",
            "",
            "## Heading",
            "",
            "> quote **bold**",
            "",
            "- item **one**",
            "",
            "Name | Value",
            "--- | ---:",
            "**alpha** | 42",
        ]
    )
    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "show markdown", now=0.0)
    state = reduce_stream_event(
        state,
        {"type": "response", "content": content},
        now=1.0,
    )
    state = reduce_stream_event(state, {"type": "done"}, now=2.0)

    live_stream = io.StringIO()
    live_console = Console(
        file=live_stream,
        width=80,
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
    )
    live_renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(force_color=True),
        console=live_console,
        error_console=Console(file=io.StringIO(), width=80),
        width=80,
    )
    live_renderer.start_turn("show markdown", thread_id="thread-1", now=0.0)
    live_renderer.render_events(
        [
            {"type": "response", "content": content},
            {"type": "done"},
        ],
        now=1.0,
    )

    stream = io.StringIO()
    console = Console(
        file=stream,
        width=80,
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
    )
    renderer = RichReplRenderer(
        state=state,
        capabilities=FakeTerminalCapabilities(force_color=True),
        console=console,
        error_console=Console(file=io.StringIO(), width=80),
        width=80,
    )

    renderer.render_state()

    text = stream.getvalue()
    assert "\x1b[1mbold\x1b[0m" in text
    assert "▌" in text
    assert "\x1b[1malpha\x1b[0m" in text
    assert text == live_stream.getvalue()


def test_resize_triggers_redraw() -> None:
    async def exercise() -> tuple[Mock, AsyncMock, _RichReplRuntime]:
        app = CLIApp(
            agent=None,
            thread_id="thread-1",
            runtime_config=CLIRuntimeConfig(renderer="rich"),
        )
        renderer = Mock(spec=RichReplRenderer)
        renderer.update_terminal_width.return_value = True
        runtime = _RichReplRuntime(
            app=app,
            renderer=renderer,
            capabilities=FakeTerminalCapabilities(width=80),
        )
        runtime._resize_pending = True
        runtime.terminal_width = Mock(return_value=120)  # type: ignore[method-assign]
        redraw = AsyncMock()
        runtime.redraw = redraw  # type: ignore[method-assign]

        await runtime._maybe_resize_redraw()

        return renderer, redraw, runtime

    renderer, redraw, runtime = asyncio.run(exercise())

    renderer.update_terminal_width.assert_called_once_with(120)
    redraw.assert_awaited_once()
    assert runtime._resize_pending is False


def test_live_width_change_triggers_redraw_without_signal_flag() -> None:
    async def exercise() -> tuple[Mock, AsyncMock]:
        app = CLIApp(
            agent=None,
            thread_id="thread-1",
            runtime_config=CLIRuntimeConfig(renderer="rich"),
        )
        renderer = Mock(spec=RichReplRenderer)
        renderer.width = 80
        renderer.update_terminal_width.return_value = True
        runtime = _RichReplRuntime(
            app=app,
            renderer=renderer,
            capabilities=FakeTerminalCapabilities(width=80),
        )
        runtime.terminal_width = Mock(return_value=120)  # type: ignore[method-assign]
        redraw = AsyncMock()
        runtime.redraw = redraw  # type: ignore[method-assign]

        await runtime._maybe_resize_redraw()

        return renderer, redraw

    renderer, redraw = asyncio.run(exercise())

    renderer.update_terminal_width.assert_called_once_with(120)
    redraw.assert_awaited_once()


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
    assert "──────────" in output.stdout_text


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


def test_rich_renderer_buffers_unstable_paragraph_until_turn_done() -> None:
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

    assert "Streaming now." not in output.stdout_text

    renderer.render_event({"type": "done", "tool_call_count": 0}, now=2.0)

    assert "Streaming now." in output.stdout_text
    assert "  Streaming now." in output.stdout_text
    lines = output.stdout_text.splitlines()
    header_index = next(index for index, line in enumerate(lines) if "──── Nymeria " in line)
    opening_divider_index = next(
        index for index, line in enumerate(lines) if line.strip() and all(c in "─·" for c in line.strip())
    )
    response_index = next(index for index, line in enumerate(lines) if "Streaming now." in line)
    assert header_index < opening_divider_index < response_index


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
    assert "Thread Memory -- empty (fresh thread)" in output.stdout_text
    assert "Global Memory -- 26 entries loaded fine" in output.stdout_text
    assert "\nNymeria |" not in output.stdout_text


def test_rich_renderer_streaming_table_split_uses_rich_markdown() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=80,
    )
    renderer.start_turn("table", thread_id="thread-1", now=0.0)

    renderer.render_events(
        [
            {"type": "response", "content": "Name | Value\n"},
            {"type": "response", "content": "--- | ---:\n"},
            {"type": "response", "content": "**alpha** | 42\n"},
            {"type": "done", "tool_call_count": 0},
        ],
        now=1.0,
    )

    assert "Name" in output.stdout_text
    assert "Value" in output.stdout_text
    assert "alpha" in output.stdout_text
    assert "--- | ---" not in output.stdout_text


def test_rich_renderer_tool_boundary_flushes_pending_markdown() -> None:
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
            {"type": "response", "content": "I will use **memory**."},
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
                "result": "Found one note.",
            },
            {"type": "done", "tool_call_count": 1},
        ],
        now=1.0,
    )

    response_index = output.stdout_text.index("I will use memory.")
    tool_index = output.stdout_text.index("search_memory")
    assert response_index < tool_index


def test_rich_renderer_mid_stream_redraw_does_not_duplicate_buffered_text() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=80,
    )
    renderer.start_turn("resize", thread_id="thread-1", now=0.0)
    renderer.render_event(
        {"type": "response", "content": "Pending **paragraph**"},
        now=1.0,
    )

    renderer.update_terminal_width(100)
    renderer.reset_state(renderer.state)
    renderer.render_state()
    renderer.render_events(
        [
            {"type": "response", "content": " after redraw.\n\n"},
            {"type": "done", "tool_call_count": 0},
        ],
        now=2.0,
    )

    assert output.stdout_text.count("Pending paragraph") == 1


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
    assert "──────────" in output.stdout_text


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


def test_rich_renderer_keeps_dispatched_turn_in_assistant_pipeline() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=110,
    )
    renderer.start_turn("@Research test", thread_id="thread-1", now=0.0)

    renderer.render_events(
        [
            {
                "type": "dispatched",
                "thread_id": "thread-1",
                "target_thread_id": "thread-2",
                "title": "Research",
            },
            {
                "type": "thinking",
                "content": "I will check the requested thread before answering.",
            },
            {"type": "response", "content": "Checking first."},
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
            {"type": "response", "content": "Done."},
            {
                "type": "done",
                "dispatched_to": {"thread_id": "thread-2", "title": "Research"},
            },
        ],
        now=1.0,
    )

    text = output.stdout_text
    assert "Response from Research (thread-2)" in text
    assert "──── System " not in text
    assert "I will check the requested thread before answering" in text
    assert "Checking first." in text
    assert "✓ memory_read ok" in text
    assert "Done." in text


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


def test_history_payload_converts_to_ordered_cli_state_and_renders_divider() -> None:
    state = cli_state_from_history(
        {
            "messages": [
                {"id": "u1", "role": "user", "content": "Inspect"},
                {
                    "id": "a1",
                    "role": "assistant",
                    "content": "Done.",
                    "steps": [
                        {"type": "thinking", "content": "Plan"},
                        {"type": "response", "content": "Checking first."},
                        {
                            "type": "tool_call",
                            "id": "call-1",
                            "name": "filesystem_read",
                            "arguments": {"path": "README.md"},
                            "status": "success",
                            "result": "ok",
                            "artifacts": [{"path": "/workspace/report.md"}],
                        },
                        {"type": "response", "content": "Done."},
                    ],
                },
                {
                    "id": "s1",
                    "role": "system",
                    "kind": "compaction_notice",
                    "content": "Context compacted",
                    "messages_removed": 3,
                },
            ]
        },
        thread_id="thread-1",
        user_id="alice",
    )
    assistant = state.messages[1]

    assert isinstance(assistant, AssistantMessage)
    assert [type(step) for step in assistant.steps] == [
        ThinkingStep,
        ResponseStep,
        ToolCallStep,
        ResponseStep,
    ]
    assert state.artifacts[0].path == "/workspace/report.md"

    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        state=state,
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
    )
    renderer.render_state()

    lines = output.stdout_text.splitlines()
    header_index = next(index for index, line in enumerate(lines) if "──── Nymeria " in line)
    divider_index = next(
        index for index, line in enumerate(lines) if "················" in line
    )
    assert header_index < divider_index
    assert "Context compacted." in output.stdout_text


def test_rich_thread_switch_resets_transcript_and_loads_selected_history() -> None:
    async def exercise() -> tuple[CLIApp, RichReplRenderer, FakeAgentClient, str]:
        client = FakeAgentClient(
            threads=[
                {"thread_id": "thread-1", "title": "Old"},
                {"thread_id": "thread-2", "title": "Loaded Thread"},
            ],
            history={
                "alice:thread-2": {
                    "messages": [
                        {"id": "u2", "role": "user", "content": "Earlier question"},
                        {
                            "id": "a2",
                            "role": "assistant",
                            "content": "Earlier answer",
                            "steps": [
                                {"type": "response", "content": "Earlier answer"}
                            ],
                        },
                    ]
                }
            },
        )
        stream = io.StringIO()
        console = Console(file=stream, width=100, force_terminal=False)
        caps = SimpleNamespace(
            width=100,
            renderer="rich",
            color_enabled=False,
            unicode_enabled=True,
        )
        app = CLIApp(
            agent=None,
            thread_id="thread-1",
            runtime_config=CLIRuntimeConfig(renderer="rich"),
        )
        app.state.console = console
        app.state.user_id = "alice"
        app._client = client
        renderer = RichReplRenderer(
            state=create_initial_state(thread_id="thread-1", user_id="alice"),
            capabilities=caps,
            console=console,
            error_console=Console(file=io.StringIO(), width=100),
            width=100,
        )
        renderer.start_turn("old visible text", thread_id="thread-1", user_id="alice")
        runtime = _RichReplRuntime(app=app, renderer=renderer, capabilities=caps)
        app._active_repl_renderer = renderer
        app._active_rich_runtime = runtime
        app._active_capabilities = caps

        await app._dispatch_command_async(
            "/thread switch Loaded Thread",
            caps,
            renderer,
            runtime=runtime,
        )
        return app, renderer, client, stream.getvalue()

    app, renderer, client, output = asyncio.run(exercise())

    assert app.state.thread_id == "thread-2"
    assert renderer.state.thread_id == "thread-2"
    assert [message.content for message in renderer.state.messages] == [
        "Earlier question",
        "Earlier answer",
    ]
    assert "Earlier answer" in output
    assert client.chat_requests == []


def test_startup_thread_ref_selects_existing_thread_and_missing_exits() -> None:
    caps = SimpleNamespace(
        width=100,
        renderer="rich",
        color_enabled=False,
        unicode_enabled=True,
    )

    async def select_existing() -> CLIApp:
        app = CLIApp(
            agent=None,
            runtime_config=CLIRuntimeConfig(
                renderer="rich",
                startup_thread_ref="Loaded",
            ),
        )
        app.state.console = Console(file=io.StringIO(), width=100, force_terminal=False)
        app._active_capabilities = caps
        app._client = FakeAgentClient(
            threads=[{"thread_id": "thread-2", "title": "Loaded Thread"}]
        )
        handled = await app._handle_startup_thread_intents_async(caps)
        assert handled is False
        return app

    async def select_missing() -> CLIApp:
        app = CLIApp(
            agent=None,
            runtime_config=CLIRuntimeConfig(
                renderer="rich",
                startup_thread_ref="Missing",
            ),
        )
        app.state.console = Console(file=io.StringIO(), width=100, force_terminal=False)
        app._active_capabilities = caps
        app._client = FakeAgentClient(
            threads=[{"thread_id": "thread-2", "title": "Loaded Thread"}]
        )
        handled = await app._handle_startup_thread_intents_async(caps)
        assert handled is True
        return app

    selected = asyncio.run(select_existing())
    missing = asyncio.run(select_missing())

    assert selected.state.thread_id == "thread-2"
    assert selected._startup_history_thread_id == "thread-2"
    assert missing.state.thread_id != "thread-2"
    assert missing._startup_history_thread_id is None
