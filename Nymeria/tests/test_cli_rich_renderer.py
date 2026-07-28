from __future__ import annotations

import asyncio
import io
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from cli_fixtures import CapturedRenderOutput, FakeAgentClient, FakeTerminalCapabilities
from rich.console import Console

from nymeria.triggers.cli.app import (
    CLIApp,
    CLIRuntimeConfig,
    _RichReplPromptToolkitShell,
    _RichReplRuntime,
)
from nymeria.triggers.cli.history import cli_state_from_history
from nymeria.triggers.cli.rendering.rich_markdown import (
    DEFAULT_CODE_THEME,
    MarkdownBlock,
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


class FakePromptOutput:
    def __init__(
        self,
        *,
        columns: int = 80,
        rows: int = 24,
        rows_below_cursor: int = 99,
    ) -> None:
        self.columns = columns
        self.rows = rows
        self.rows_below_cursor = rows_below_cursor
        self.ops: list[tuple[str, object]] = []

    def get_size(self) -> SimpleNamespace:
        return SimpleNamespace(columns=self.columns, rows=self.rows)

    def get_rows_below_cursor_position(self) -> int:
        return self.rows_below_cursor

    def write_raw(self, data: str) -> None:
        self.ops.append(("raw", data))

    def hide_cursor(self) -> None:
        self.ops.append(("hide_cursor", None))

    def show_cursor(self) -> None:
        self.ops.append(("show_cursor", None))

    def cursor_goto(self, row: int = 0, column: int = 0) -> None:
        self.ops.append(("cursor", (row, column)))

    def flush(self) -> None:
        self.ops.append(("flush", None))

    def erase_screen(self) -> None:
        self.ops.append(("erase_screen", None))


class FakePromptRenderer:
    def __init__(self, output: FakePromptOutput) -> None:
        self.output = output
        self.erase_count = 0
        self.reset_count = 0
        self.render_count = 0
        self._min_available_height = 99
        self._last_screen = object()
        self.cpr_request_count = 0

    def erase(self, *, leave_alternate_screen: bool = True) -> None:
        self.erase_count += 1
        self.output.ops.append(("erase", leave_alternate_screen))

    def reset(self, *, leave_alternate_screen: bool = True) -> None:
        self.reset_count += 1
        self.output.ops.append(("reset", leave_alternate_screen))

    def render(self, app: Any, layout: Any, is_done: bool = False) -> None:
        from prompt_toolkit.application.current import get_app

        self.render_count += 1
        self.output.ops.append(("layout_render", is_done))
        # Layout containers resolve get_app() during the walk; record that
        # the engine entered set_app around the synchronous render.
        self.output.ops.append(("app_is_current", get_app() is app))

    def request_absolute_cursor_position(self) -> None:
        self.cpr_request_count += 1
        self.output.ops.append(("request_cpr", None))


def _block_texts(blocks: list[MarkdownBlock]) -> list[str]:
    return [block.text for block in blocks]


def _assert_blank_line_between(text: str, before: str, after: str) -> None:
    lines = text.splitlines()
    before_index = next(index for index, line in enumerate(lines) if before in line)
    after_index = next(
        index
        for index, line in enumerate(lines[before_index + 1 :], start=before_index + 1)
        if after in line
    )
    assert any(not line.strip() for line in lines[before_index + 1 : after_index])


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


def _make_scroll_region_runtime(
    *,
    width: int = 80,
    height: int = 24,
    rows_below_cursor: int = 99,
) -> tuple[_RichReplRuntime, FakePromptOutput, FakePromptRenderer, SimpleNamespace]:
    app = CLIApp(
        agent=None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    output = FakePromptOutput(
        columns=width,
        rows=height,
        rows_below_cursor=rows_below_cursor,
    )
    prompt_renderer = FakePromptRenderer(output)
    runtime = _RichReplRuntime(
        app=app,
        renderer=RichReplRenderer(
            capabilities=FakeTerminalCapabilities(
                width=width,
                height=height,
                renderer="rich",
            ),
            width=width,
        ),
        capabilities=FakeTerminalCapabilities(
            width=width,
            height=height,
            renderer="rich",
        ),
    )
    controller = SimpleNamespace(
        text_area=SimpleNamespace(buffer=SimpleNamespace(text="")),
        prompt_fragments=lambda: [("class:composer", "› ")],
    )
    application = SimpleNamespace(
        output=output,
        renderer=prompt_renderer,
        layout=SimpleNamespace(),
        is_running=True,
        _on_resize=lambda: None,
    )
    runtime.bind_application(application, controller)
    return runtime, output, prompt_renderer, controller


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
    blocks = buffer.append("\n\nName | Value\n--- | ---:\n")
    assert _block_texts(blocks) == ["Paragraph with **bold**"]
    assert blocks[0].kind == "paragraph"
    assert blocks[0].trailing_blank_lines == 1
    assert buffer.append("**alpha** | 42\n") == []
    blocks = buffer.append("\nNext paragraph")
    assert _block_texts(blocks) == ["Name | Value\n--- | ---:\n**alpha** | 42"]
    assert blocks[0].kind == "table"
    assert blocks[0].trailing_blank_lines == 1
    assert _block_texts(buffer.flush()) == ["Next paragraph"]


def test_markdown_stream_buffer_preserves_dense_block_separators() -> None:
    buffer = MarkdownStreamBuffer()

    blocks = buffer.append(
        "Intro\n\nName | Value\n--- | ---:\nalpha | 42\n\nNext paragraph"
    )
    assert _block_texts(blocks) == [
        "Intro",
        "Name | Value\n--- | ---:\nalpha | 42",
    ]
    assert blocks[1].kind == "table"
    assert blocks[1].trailing_blank_lines == 1
    assert _block_texts(buffer.flush()) == ["Next paragraph"]

    buffer = MarkdownStreamBuffer()
    blocks = buffer.append("```python\nprint('hi')\n```\n")
    assert _block_texts(blocks) == ["```python\nprint('hi')\n```"]
    assert blocks[0].kind == "code"
    assert buffer.append("\nCode Block Test (Bash)") == []
    blocks = buffer.flush()
    assert _block_texts(blocks) == ["Code Block Test (Bash)"]
    assert blocks[0].leading_blank_lines == 1

    buffer = MarkdownStreamBuffer()
    assert buffer.append("Name | Value\n--- | ---:\nalpha | 42\nTight label") == []
    blocks = buffer.flush()
    assert _block_texts(blocks) == [
        "Name | Value\n--- | ---:\nalpha | 42",
        "Tight label",
    ]
    assert blocks[0].kind == "table"
    assert blocks[0].trailing_blank_lines == 0


def test_rich_renderer_markdown_block_spacing_and_replay_match() -> None:
    content = "\n".join(
        [
            "Table Test (Simple)",
            "",
            "Name | Value",
            "--- | ---:",
            "alpha | 42",
            "",
            "Table Test (Wide & Uneven)",
            "",
            "A | B",
            "--- | ---",
            "x | y",
            "",
            "```python",
            "print('hi')",
            "```",
            "",
            "Code Block Test (Bash)",
            "",
            "```bash",
            "echo hi",
            "```",
            "",
            "Inline Formatting Stress Test",
            "",
            "Lists",
            "",
            "- one",
            "",
            "Blockquote",
            "",
            "> quote",
        ]
    )
    chunks = [
        content[:68],
        content[68:139],
        content[139:211],
        content[211:],
    ]

    state = create_initial_state(thread_id="thread-1", now=0.0)
    state = start_turn(state, "markdown spacing", now=0.0)
    for offset, chunk in enumerate(chunks):
        state = reduce_stream_event(
            state,
            {"type": "response", "content": chunk},
            now=1.0 + offset,
        )
    state = reduce_stream_event(state, {"type": "done"}, now=5.0)

    live_output = CapturedRenderOutput()
    live_renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=live_output.stdout,
        stderr=live_output.stderr,
        width=80,
    )
    live_renderer.start_turn("markdown spacing", thread_id="thread-1", now=0.0)
    live_renderer.render_events(
        [{"type": "response", "content": chunk} for chunk in chunks]
        + [{"type": "done"}],
        now=1.0,
    )

    replay_output = CapturedRenderOutput()
    replay_renderer = RichReplRenderer(
        state=state,
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=replay_output.stdout,
        stderr=replay_output.stderr,
        width=80,
    )
    replay_renderer.render_state()

    text = live_output.stdout_text
    _assert_blank_line_between(text, "alpha", "Table Test (Wide & Uneven)")
    _assert_blank_line_between(text, "print('hi')", "Code Block Test (Bash)")
    _assert_blank_line_between(text, "echo hi", "Inline Formatting Stress Test")
    _assert_blank_line_between(text, "Inline Formatting Stress Test", "Lists")
    _assert_blank_line_between(text, "one", "Blockquote")
    assert live_output.stdout_text == replay_output.stdout_text


def test_rich_renderer_adds_fallback_spacing_after_tight_dense_blocks() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=80,
    )
    renderer.start_turn("tight markdown", thread_id="thread-1", now=0.0)

    renderer.render_events(
        [
            {
                "type": "response",
                "content": (
                    "Name | Value\n"
                    "--- | ---:\n"
                    "alpha | 42\n"
                    "Tight Table Label\n\n"
                    "```python\n"
                    "print('tight')\n"
                    "```\n"
                    "Tight Code Label"
                ),
            },
            {"type": "done"},
        ],
        now=1.0,
    )

    _assert_blank_line_between(output.stdout_text, "alpha", "Tight Table Label")
    _assert_blank_line_between(
        output.stdout_text,
        "print('tight')",
        "Tight Code Label",
    )


def test_rich_renderer_keeps_consecutive_headings_compact() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=80,
    )
    renderer.start_turn("headings", thread_id="thread-1", now=0.0)

    renderer.render_events(
        [
            {"type": "response", "content": "## First\n\n### Second\n\nBody"},
            {"type": "done"},
        ],
        now=1.0,
    )

    lines = output.stdout_text.splitlines()
    first_index = next(index for index, line in enumerate(lines) if "First" in line)
    second_index = next(index for index, line in enumerate(lines) if "Second" in line)
    assert second_index == first_index + 1


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

    assert "─" * 100 in output.stdout_text
    assert "› use a tool" in output.stdout_text
    assert "Nymeria" not in output.stdout_text
    assert "❖ search_memory(query=\"project status\") 1.0s -> Found 2 matching notes." in (
        output.stdout_text
    )
    assert "I found the notes." in output.stdout_text
    assert "····" not in output.stdout_text


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
        line for line in output.stdout_text.splitlines() if set(line) == {"─"}
    )

    output.clear()
    renderer.update_terminal_width(120)
    renderer.render_state()
    wide_rule = next(
        line for line in output.stdout_text.splitlines() if set(line) == {"─"}
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
        runtime.footer._resize_pending = True
        runtime.footer.terminal_width = Mock(return_value=120)  # type: ignore[method-assign]
        redraw = AsyncMock()
        runtime.footer.redraw = redraw  # type: ignore[method-assign]

        await runtime.footer._maybe_resize_redraw()

        return renderer, redraw, runtime

    renderer, redraw, runtime = asyncio.run(exercise())

    renderer.update_terminal_width.assert_called_once_with(120)
    redraw.assert_awaited_once()
    assert runtime.footer._resize_pending is False


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
        runtime.footer.terminal_width = Mock(return_value=120)  # type: ignore[method-assign]
        redraw = AsyncMock()
        runtime.footer.redraw = redraw  # type: ignore[method-assign]

        await runtime.footer._maybe_resize_redraw()

        return renderer, redraw

    renderer, redraw = asyncio.run(exercise())

    renderer.update_terminal_width.assert_called_once_with(120)
    redraw.assert_awaited_once()


def test_scroll_region_runtime_owns_prompt_toolkit_resize_handler() -> None:
    app = CLIApp(
        agent=None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    output = FakePromptOutput(columns=80, rows=24)
    prompt_renderer = FakePromptRenderer(output)
    runtime = _RichReplRuntime(
        app=app,
        renderer=RichReplRenderer(
            capabilities=FakeTerminalCapabilities(
                width=80,
                height=24,
                renderer="rich",
            ),
            width=80,
        ),
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
    )
    application = SimpleNamespace(
        output=output,
        renderer=prompt_renderer,
        is_running=False,
        _on_resize=lambda: None,
    )

    runtime.bind_application(application, None)

    assert application._on_resize.__self__ is runtime.footer
    assert application._on_resize.__func__ is runtime.footer.handle_terminal_resize.__func__


def test_scroll_region_runtime_suppresses_prompt_toolkit_startup_height_probe() -> None:
    runtime, output, prompt_renderer, _controller = _make_scroll_region_runtime()
    prompt_renderer._min_available_height = 20

    runtime.application._request_absolute_cursor_position()

    assert prompt_renderer._min_available_height == 0
    assert ("request_cpr", None) not in output.ops


def test_scroll_region_runtime_keeps_nymeria_pin_probe_available() -> None:
    runtime, output, prompt_renderer, _controller = _make_scroll_region_runtime()
    prompt_renderer._min_available_height = 0

    runtime.footer._request_follow_footer_pin_probe()

    assert prompt_renderer._min_available_height == 0
    assert ("request_cpr", None) in output.ops


def test_scroll_region_runtime_does_not_probe_before_first_transcript_write() -> None:
    runtime, output, prompt_renderer, _controller = _make_scroll_region_runtime()
    prompt_renderer._min_available_height = 0

    runtime.prepare_follow_footer_render()

    assert prompt_renderer.cpr_request_count == 0
    assert ("request_cpr", None) not in output.ops


def test_rich_scroll_region_shell_layout_uses_exact_footer_height(tmp_path: Path) -> None:
    app = CLIApp(
        agent=None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    runtime = _RichReplRuntime(
        app=app,
        renderer=RichReplRenderer(
            capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
            width=80,
        ),
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
    )
    shell = _RichReplPromptToolkitShell(
        cli_app=app,
        runtime=runtime,
        renderer=runtime.renderer,
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
        history_path=tmp_path / "history",
    )

    prompt_app = shell.build_application()

    assert prompt_app.layout.container.preferred_height(80, 24).preferred == (
        runtime.footer_height()
    )


def test_scroll_region_height_resize_triggers_hard_redraw() -> None:
    async def exercise() -> tuple[Mock, AsyncMock, _RichReplRuntime]:
        app = CLIApp(
            agent=None,
            thread_id="thread-1",
            runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
        )
        output = FakePromptOutput(columns=80, rows=24)
        prompt_renderer = FakePromptRenderer(output)
        renderer = Mock(spec=RichReplRenderer)
        renderer.width = 80
        renderer.update_terminal_width.return_value = False
        runtime = _RichReplRuntime(
            app=app,
            renderer=renderer,
            capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
        )
        runtime.application = SimpleNamespace(
            output=output,
            renderer=prompt_renderer,
            is_running=False,
        )
        runtime.footer._terminal_size = (80, 24)
        runtime.footer._resize_pending = True
        output.rows = 30
        redraw = AsyncMock()
        runtime.footer.redraw = redraw  # type: ignore[method-assign]

        await runtime.footer._maybe_resize_redraw()

        return renderer, redraw, runtime

    renderer, redraw, runtime = asyncio.run(exercise())

    renderer.update_terminal_width.assert_called_once_with(80)
    redraw.assert_awaited_once_with(rebuild_scrollback=True)
    assert runtime.footer._terminal_size == (80, 30)
    assert runtime.footer._resize_pending is False


def test_scroll_region_resize_replay_resets_pinned_margins_before_clear() -> None:
    render_output = CapturedRenderOutput()
    app = CLIApp(
        agent=None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    output = FakePromptOutput(columns=100, rows=30)
    prompt_renderer = FakePromptRenderer(output)
    runtime = _RichReplRuntime(
        app=app,
        renderer=RichReplRenderer(
            capabilities=FakeTerminalCapabilities(
                width=100,
                height=30,
                renderer="rich",
            ),
            stdout=render_output.stdout,
            stderr=render_output.stderr,
            width=100,
        ),
        capabilities=FakeTerminalCapabilities(width=100, height=30, renderer="rich"),
    )
    runtime.application = SimpleNamespace(
        output=output,
        renderer=prompt_renderer,
        is_running=False,
    )
    runtime.footer._pinned_footer_active = True
    runtime.footer._pinned_footer_height = 5
    runtime.footer._pinned_scroll_bottom = 25
    runtime.footer._pinned_terminal_size = (100, 30)

    runtime.footer._redraw_follow_footer()

    assert output.ops.index(("raw", "\x1b[r")) < output.ops.index(("erase_screen", None))
    assert ("raw", "\x1b[3J") not in output.ops
    assert ("hide_cursor", None) in output.ops
    assert ("cursor", (0, 0)) in output.ops
    assert ("reset", False) in output.ops
    assert output.ops[-2:] == [("raw", "\x1b7"), ("flush", None)]
    assert runtime.pinned_footer_active() is False


def test_scroll_region_resize_rebuild_clears_scrollback_before_replay() -> None:
    render_output = CapturedRenderOutput()
    app = CLIApp(
        agent=None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    app.state.console = Console(
        file=render_output.stdout,
        width=100,
        force_terminal=False,
    )
    output = FakePromptOutput(columns=100, rows=30)
    prompt_renderer = FakePromptRenderer(output)
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(
            width=100,
            height=30,
            renderer="rich",
        ),
        stdout=render_output.stdout,
        stderr=render_output.stderr,
        width=100,
    )
    renderer.state = start_turn(
        create_initial_state(thread_id="thread-1", now=0.0),
        "resize check",
        now=0.1,
    )
    runtime = _RichReplRuntime(
        app=app,
        renderer=renderer,
        capabilities=FakeTerminalCapabilities(width=100, height=30, renderer="rich"),
    )
    runtime.application = SimpleNamespace(
        output=output,
        renderer=prompt_renderer,
        is_running=False,
    )

    runtime.footer._redraw_follow_footer(rebuild_scrollback=True)

    erase_index = output.ops.index(("erase_screen", None))
    clear_scrollback_index = output.ops.index(("raw", "\x1b[3J"))
    cursor_index = output.ops.index(("cursor", (0, 0)))
    assert erase_index < clear_scrollback_index < cursor_index
    assert ("reset", False) in output.ops
    assert output.ops[-2:] == [("raw", "\x1b7"), ("flush", None)]
    text = ANSI_RE.sub("", render_output.stdout_text)
    assert "N Y M E R I A" in text
    assert "resize check" in text
    assert text.index("N Y M E R I A") < text.index("resize check")


def test_scroll_region_resize_redraw_does_not_overlap_existing_task() -> None:
    class PendingTask:
        def done(self) -> bool:
            return False

    app = CLIApp(
        agent=None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    output = FakePromptOutput(columns=80, rows=24)
    prompt_renderer = FakePromptRenderer(output)
    runtime = _RichReplRuntime(
        app=app,
        renderer=RichReplRenderer(
            capabilities=FakeTerminalCapabilities(
                width=80,
                height=24,
                renderer="rich",
            ),
            width=80,
        ),
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
    )
    create_background_task = Mock()
    runtime.application = SimpleNamespace(
        output=output,
        renderer=prompt_renderer,
        is_running=True,
        create_background_task=create_background_task,
    )
    runtime.footer._resize_pending = True
    runtime.footer._resize_task = PendingTask()  # type: ignore[assignment]

    runtime.schedule_resize_redraw()

    create_background_task.assert_not_called()


def test_rich_runtime_scroll_region_gates_to_safe_interactive_rich_terminals() -> None:
    app = CLIApp(
        agent=None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
        width=80,
    )

    assert _RichReplRuntime(
        app=app,
        renderer=renderer,
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
    ).scroll_region_enabled() is True
    assert _RichReplRuntime(
        app=app,
        renderer=renderer,
        capabilities=FakeTerminalCapabilities(width=80, height=8, renderer="rich"),
    ).scroll_region_enabled() is False
    assert _RichReplRuntime(
        app=app,
        renderer=renderer,
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="plain"),
    ).scroll_region_enabled() is False
    assert _RichReplRuntime(
        app=CLIApp(
            agent=None,
            thread_id="thread-1",
            runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=False),
        ),
        renderer=renderer,
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
    ).scroll_region_enabled() is False


def test_rich_runtime_follow_footer_wraps_transcript_writes_without_terminal_run() -> None:
    async def exercise() -> tuple[
        list[tuple[str, object]],
        list[str],
        int,
        FakePromptRenderer,
    ]:
        app = CLIApp(
            agent=None,
            thread_id="thread-1",
            runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
        )
        output = FakePromptOutput(columns=80, rows=24)
        prompt_renderer = FakePromptRenderer(output)
        runtime = _RichReplRuntime(
            app=app,
            renderer=RichReplRenderer(
                capabilities=FakeTerminalCapabilities(
                    width=80,
                    height=24,
                    renderer="rich",
                ),
                width=80,
            ),
            capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
        )
        runtime.application = SimpleNamespace(
            output=output,
            renderer=prompt_renderer,
            layout=SimpleNamespace(),
            is_running=True,
        )
        events: list[str] = []

        assert runtime.save_follow_footer_transcript_cursor() is True
        runtime.prepare_follow_footer_render()
        await runtime.render_above_prompt(lambda: events.append("rendered"))

        return output.ops, events, prompt_renderer.erase_count, prompt_renderer

    ops, events, erase_count, prompt_renderer = asyncio.run(exercise())

    assert events == ["rendered"]
    assert prompt_renderer._min_available_height == 0
    assert ("hide_cursor", None) in ops
    assert ("erase", False) in ops
    assert ("request_cpr", None) in ops
    assert ("raw", "\x1b7") in ops
    assert not any(op == ("raw", "\x1b[1;14r") for op in ops)
    assert not any(op == ("raw", "\x1b[r") for op in ops)
    assert erase_count == 1
    # Synchronized-output window: set before any visible mutation, reset
    # after the cursor save, so the write paints as one frame.
    sync_set = ops.index(("raw", "\x1b[?2026h"))
    sync_reset = ops.index(("raw", "\x1b[?2026l"))
    assert sync_set < ops.index(("hide_cursor", None))
    assert ops.index(("raw", "\x1b7")) < sync_reset
    # The set is flushed immediately: the Rich transcript rides a different
    # buffered stream, so an unflushed set could land after the content.
    assert ops[sync_set + 1] == ("flush", None)
    # The pt layout repaints synchronously INSIDE the window (after the
    # cursor save, before the reset), so the footer is never visible in its
    # erased state: erase + transcript + repaint paint as one frame.
    render_index = ops.index(("layout_render", False))
    assert ops.index(("raw", "\x1b7")) < render_index < sync_reset
    assert ("app_is_current", True) in ops  # set_app wrapped the render


def test_rich_runtime_pins_footer_after_follow_footer_reaches_bottom() -> None:
    async def exercise() -> tuple[
        list[tuple[str, object]],
        list[tuple[str, object]],
        list[str],
        int,
        bool,
    ]:
        app = CLIApp(
            agent=None,
            thread_id="thread-1",
            runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
        )
        output = FakePromptOutput(columns=80, rows=24, rows_below_cursor=1)
        prompt_renderer = FakePromptRenderer(output)
        prompt_renderer._min_available_height = 0
        runtime = _RichReplRuntime(
            app=app,
            renderer=RichReplRenderer(
                capabilities=FakeTerminalCapabilities(
                    width=80,
                    height=24,
                    renderer="rich",
                ),
                width=80,
            ),
            capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
        )
        runtime.application = SimpleNamespace(
            output=output,
            renderer=prompt_renderer,
            layout=SimpleNamespace(),
            is_running=True,
        )
        events: list[str] = []

        assert runtime.save_follow_footer_transcript_cursor() is True
        runtime.footer._follow_footer_pin_probe_pending = True
        runtime.prepare_follow_footer_render()
        activation_ops = list(output.ops)
        output.ops.clear()
        await runtime.render_above_prompt(lambda: events.append("rendered"))
        runtime.finish_follow_footer_render()

        return (
            activation_ops,
            output.ops,
            events,
            prompt_renderer.erase_count,
            runtime.pinned_footer_active(),
        )

    activation_ops, ops, events, erase_count, pinned = asyncio.run(exercise())

    assert pinned is True
    assert ("erase", False) in activation_ops
    # Activation now moves cursor to terminal bottom (row 24) and scrolls
    # `footer_height - rows_below + 1 - max(0, last_h - rows_below)` times
    # rather than restoring a stale \x1b7 cursor and over-scrolling.
    assert ("raw", "\x1b[24;1H") in activation_ops
    assert ("raw", "\r\n" * 5) in activation_ops
    assert events == ["rendered"]
    assert ops.count(("hide_cursor", None)) == 1
    assert ("raw", "\x1b[1;19r") in ops
    assert ("raw", "\x1b8") in ops
    assert ("raw", "\x1b7") in ops
    assert ("raw", "\x1b[r") in ops
    assert ("erase", False) not in ops
    assert erase_count == 1
    # Synchronized-output window brackets the whole pinned write (margins,
    # restore, callback, save, cursor restore) into one painted frame.
    sync_set = ops.index(("raw", "\x1b[?2026h"))
    sync_reset = ops.index(("raw", "\x1b[?2026l"))
    assert sync_set < ops.index(("raw", "\x1b[1;19r"))
    assert ops.index(("raw", "\x1b[r")) < sync_reset


def test_rich_runtime_restores_input_cursor_after_pinned_transcript_write() -> None:
    async def exercise() -> list[tuple[str, object]]:
        runtime, output, prompt_renderer, _controller = _make_scroll_region_runtime()
        runtime.footer._follow_footer_transcript_cursor_saved = True
        runtime.footer._pinned_footer_active = True
        runtime.footer._pinned_footer_height = 5
        runtime.footer._pinned_scroll_bottom = 19
        runtime.footer._pinned_terminal_size = (80, 24)
        prompt_renderer._cursor_pos = SimpleNamespace(x=4, y=3)

        runtime.finish_follow_footer_render()
        output.ops.clear()
        await runtime.render_above_prompt(lambda: None)
        return output.ops

    ops = asyncio.run(exercise())

    restore_index = ops.index(("raw", "\x1b[23;5H"))
    show_index = ops.index(("show_cursor", None))
    assert ops.index(("raw", "\x1b[r")) < restore_index < show_index
    assert ops.count(("hide_cursor", None)) == 1


def test_rich_runtime_hides_cursor_before_pinned_footer_repaint_anchor() -> None:
    runtime, output, _prompt_renderer, _controller = _make_scroll_region_runtime()
    runtime.footer._follow_footer_transcript_cursor_saved = True
    runtime.footer._pinned_footer_active = True
    runtime.footer._pinned_footer_height = 5
    runtime.footer._pinned_scroll_bottom = 19
    runtime.footer._pinned_terminal_size = (80, 24)

    runtime.footer._prepare_pinned_footer_render()

    hide_index = output.ops.index(("hide_cursor", None))
    reset_index = output.ops.index(("raw", "\x1b[r"))
    anchor_index = output.ops.index(("raw", "\x1b[20;1H"))
    assert hide_index < reset_index < anchor_index


def test_rich_runtime_resizes_pinned_footer_when_composer_grows() -> None:
    runtime, output, prompt_renderer, controller = _make_scroll_region_runtime(
        width=20,
        height=24,
    )
    runtime.footer._follow_footer_transcript_cursor_saved = True
    runtime.footer._pinned_footer_active = True
    runtime.footer._pinned_footer_height = 5
    runtime.footer._pinned_scroll_bottom = 19
    runtime.footer._pinned_terminal_size = (20, 24)
    controller.text_area.buffer.text = "abcdefghij " * 4

    runtime.footer._prepare_pinned_footer_render()

    assert runtime.pinned_footer_active() is True
    assert runtime.footer_height() == 7
    assert runtime.footer._pinned_footer_height == 7
    assert runtime.footer._pinned_scroll_bottom == 17
    assert ("raw", "\x1b[1;19r") in output.ops
    assert ("raw", "\r\n\r\n") in output.ops
    assert ("raw", "\x1b[17;1H") in output.ops
    assert ("raw", "\x1b[18;1H\x1b[J") in output.ops
    assert ("erase", False) not in output.ops
    assert prompt_renderer._last_screen is None


def test_rich_runtime_rebuilds_transcript_when_pinned_footer_shrinks() -> None:
    """A shrinking footer rebuilds the transcript onto the larger scroll area.

    Within a DECSTBM region, scrolling content downward would push the
    topmost transcript rows out of the region (and lose them — they do
    not enter terminal scrollback).  Replaying the reducer state onto
    the freshly enlarged area is the only way to avoid a visible gap
    between the last transcript line and the now-smaller footer.
    """

    runtime, output, prompt_renderer, controller = _make_scroll_region_runtime(
        width=20,
        height=24,
    )
    runtime.footer._follow_footer_transcript_cursor_saved = True
    runtime.footer._pinned_footer_active = True
    runtime.footer._pinned_footer_height = 7
    runtime.footer._pinned_scroll_bottom = 17
    runtime.footer._pinned_terminal_size = (20, 24)
    controller.text_area.buffer.text = ""

    runtime.footer._prepare_pinned_footer_render()

    # Shrink path deactivates the pinned footer and triggers a transcript
    # replay; the next render cycle's probe re-activates the pin.
    assert runtime.pinned_footer_active() is False
    assert runtime.footer._pinned_footer_height == 0
    assert runtime.footer._pinned_scroll_bottom == 0
    assert runtime.footer._follow_footer_pin_probe_pending is True
    assert runtime.footer._follow_footer_transcript_cursor_saved is True
    assert ("raw", "\x1b[r") in output.ops  # scroll region reset
    assert ("erase_screen", None) in output.ops
    # Scrollback must be cleared as well — otherwise the previously-visible
    # transcript stays in scrollback and the replay stacks a duplicate copy.
    assert ("raw", "\x1b[3J") in output.ops
    assert ("raw", "\x1b7") in output.ops  # transcript cursor save


def test_rich_runtime_streams_through_resized_pinned_scroll_region() -> None:
    async def exercise() -> tuple[list[tuple[str, object]], list[str]]:
        runtime, output, _prompt_renderer, controller = _make_scroll_region_runtime(
            width=20,
            height=24,
        )
        runtime.footer._follow_footer_transcript_cursor_saved = True
        runtime.footer._pinned_footer_active = True
        runtime.footer._pinned_footer_height = 5
        runtime.footer._pinned_scroll_bottom = 19
        runtime.footer._pinned_terminal_size = (20, 24)
        controller.text_area.buffer.text = "abcdefghij " * 4
        runtime.footer._prepare_pinned_footer_render()
        output.ops.clear()
        events: list[str] = []

        await runtime.render_above_prompt(lambda: events.append("rendered"))

        return output.ops, events

    ops, events = asyncio.run(exercise())

    assert events == ["rendered"]
    assert ("raw", "\x1b[1;17r") in ops
    assert ("raw", "\x1b8") in ops
    assert ("raw", "\x1b7") in ops
    assert ("erase", False) not in ops


def test_rich_runtime_exit_cleanup_moves_shell_prompt_below_follow_footer() -> None:
    app = CLIApp(
        agent=None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    output = FakePromptOutput(columns=80, rows=24)
    runtime = _RichReplRuntime(
        app=app,
        renderer=RichReplRenderer(
            capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
            width=80,
        ),
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
    )
    runtime.application = SimpleNamespace(output=output, is_running=False)
    runtime.footer._follow_footer_transcript_cursor_saved = True

    runtime.reset_follow_footer(prepare_shell_cursor=True)

    assert ("raw", "\x1b[r") in output.ops
    assert ("raw", "\x1b[24;1H\r\n") in output.ops
    assert ("raw", "\x1b8") not in output.ops
    assert runtime.pinned_footer_active() is False


def test_rich_runtime_exit_cleanup_moves_shell_prompt_below_pinned_footer() -> None:
    app = CLIApp(
        agent=None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    output = FakePromptOutput(columns=80, rows=24)
    runtime = _RichReplRuntime(
        app=app,
        renderer=RichReplRenderer(
            capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
            width=80,
        ),
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
    )
    runtime.application = SimpleNamespace(output=output, is_running=False)
    runtime.footer._follow_footer_transcript_cursor_saved = True
    runtime.footer._pinned_footer_active = True
    runtime.footer._pinned_footer_height = runtime.footer_height()
    runtime.footer._pinned_scroll_bottom = 24 - runtime.footer_height()
    runtime.footer._pinned_terminal_size = (80, 24)

    runtime.reset_follow_footer(prepare_shell_cursor=True)

    assert ("raw", "\x1b[r") in output.ops
    assert ("raw", "\x1b[24;1H\r\n") in output.ops
    assert ("raw", "\x1b8") not in output.ops
    assert runtime.pinned_footer_active() is False


def test_rich_shell_exit_hint_uses_current_thread_id() -> None:
    app = CLIApp(
        agent=None,
        thread_id="thread-abc",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    output = FakePromptOutput(columns=80, rows=24)
    runtime = _RichReplRuntime(
        app=app,
        renderer=RichReplRenderer(
            capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
            width=80,
        ),
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
    )
    runtime.application = SimpleNamespace(output=output, is_running=False)
    shell = _RichReplPromptToolkitShell(
        cli_app=app,
        runtime=runtime,
        renderer=runtime.renderer,
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
        history_path=Path("/tmp/nymeria-cli-history"),
    )

    shell._write_resume_hint_after_exit()

    assert (
        "raw",
        "Use nymeria cli --resume thread-abc to return to this thread.\r\n",
    ) in output.ops


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

    assert "─" * 100 in output.stdout_text
    assert "› use a tool" in output.stdout_text
    assert "Nymeria" not in output.stdout_text
    assert "│ checking" in output.stdout_text
    assert "I will check memory." in output.stdout_text
    assert "❖ search_memory(query=\"project status\") 0ms -> Found 2 matching notes." in (
        output.stdout_text
    )
    assert "Done with markdown." in output.stdout_text


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


def test_rich_renderer_scroll_region_mode_streams_lines_before_done() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
        stream_rich_response_lines=True,
    )
    renderer.start_turn("stream", thread_id="thread-1", now=0.0)

    renderer.render_event(
        {"type": "response", "content": "Line one\nLine two"},
        now=1.0,
    )

    assert "Line one" in output.stdout_text
    assert "Line two" not in output.stdout_text

    renderer.render_event({"type": "done", "tool_call_count": 0}, now=2.0)

    assert "Line two" in output.stdout_text


def test_rich_renderer_scroll_region_mode_streams_sentence_before_done() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
        stream_rich_response_lines=True,
    )
    renderer.start_turn("stream", thread_id="thread-1", now=0.0)

    renderer.render_event({"type": "response", "content": "Streaming "}, now=1.0)
    assert "Streaming" not in output.stdout_text

    renderer.render_event({"type": "response", "content": "now."}, now=1.1)

    assert "Streaming now." in output.stdout_text


def test_rich_renderer_scroll_region_mode_uses_rich_markdown_for_blocks() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
        stream_rich_response_lines=True,
    )
    renderer.start_turn("markdown", thread_id="thread-1", now=0.0)

    renderer.render_events(
        [
            {"type": "response", "content": "## Heading\n\nName | Value\n"},
            {"type": "response", "content": "--- | ---:\n"},
            {"type": "response", "content": "**alpha** | 42\n\n```python\n"},
            {"type": "response", "content": "print('hi')\n```\n"},
            {"type": "done", "tool_call_count": 0},
        ],
        now=1.0,
    )

    assert "Heading" in output.stdout_text
    assert "## Heading" not in output.stdout_text
    assert "alpha" in output.stdout_text
    assert "--- | ---" not in output.stdout_text
    assert "print" in output.stdout_text
    assert "```python" not in output.stdout_text


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

    assert "❖ Nymeria · autonomous · scheduler" in output.stdout_text
    assert "Autonomous TODO started: Run a CLI smoke test" in output.stdout_text
    assert "Smoke test passed." in output.stdout_text
    # The autonomous marker is a turn boundary: the response is separated from
    # it by a blank line, matching the transcript replay (no gluing).
    _assert_blank_line_between(
        output.stdout_text,
        "Autonomous TODO started",
        "Smoke",
    )


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

    assert "› summarize this" in output.stdout_text
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
    assert "❖ memory_read(scope=thread)" in text
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
                            "duration_ms": 2500,
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
    # Server-measured timing persists through history reload.
    tool_step = assistant.steps[2]
    assert isinstance(tool_step, ToolCallStep)
    assert tool_step.duration_ms == 2500
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
    prompt_index = next(
        index for index, line in enumerate(lines) if line.startswith("› Inspect")
    )
    tool_index = next(
        index for index, line in enumerate(lines) if "❖ filesystem_read" in line
    )
    assert prompt_index < tool_index
    assert "2.5s" in lines[tool_index]
    assert "Context compacted." in output.stdout_text


class _ThreadSwitchBackendClient(FakeAgentClient):
    """FakeAgentClient that emulates the backend ``/thread switch`` handler.

    Since the /thread tree migrated to the backend command registry, the CLI
    forwards ``/thread switch <ref>`` to ``execute_command`` and applies the
    returned ``switch_thread`` state hint (see form_contract.apply_state_hints).
    This mirrors the fake-execute_command pattern in test_cli_model_form.py.
    """

    async def execute_command(
        self,
        command: str,
        *,
        thread_id: str | None = None,
        source: str = "cli",
        actor: str | None = None,
        surface: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        from nymeria.core.command_executor_threads import resolve_thread_reference

        tokens = command.lstrip("/").split()
        if len(tokens) >= 3 and tokens[0] == "thread" and tokens[1] in {"switch", "s"}:
            resolution = resolve_thread_reference(self.threads, " ".join(tokens[2:]))
            if resolution.matched and resolution.thread is not None:
                tid = str(
                    resolution.thread.get("thread_id")
                    or resolution.thread.get("id")
                    or ""
                )
                title = str(resolution.thread.get("title") or "").strip() or "New Chat"
                return {
                    "success": True,
                    "markdown": f"**Done.** Switched to {tid[:8]} {title}",
                    "command": "thread switch",
                    "level": "success",
                    "data": {
                        "state": {
                            "switch_thread": {"thread_id": tid, "thread_label": title}
                        }
                    },
                }
            return {
                "success": False,
                "markdown": f"No thread matching '{' '.join(tokens[2:])}'.",
                "command": "thread switch",
                "level": "error",
            }
        return {"success": True, "markdown": "Done.", "command": tokens[0] if tokens else "", "level": "success"}


def test_rich_thread_switch_resets_transcript_and_loads_selected_history() -> None:
    async def exercise() -> tuple[CLIApp, RichReplRenderer, FakeAgentClient, str]:
        client = _ThreadSwitchBackendClient(
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


def test_turn_end_prints_summary_line_once_and_not_on_replay() -> None:
    from cli_fixtures import FakeTerminalCapabilities as Caps

    output = io.StringIO()
    console = Console(file=output, width=80, force_terminal=False, no_color=True)
    renderer = RichReplRenderer(
        capabilities=Caps(width=80),
        console=console,
        width=80,
    )
    calls: list[Any] = []

    def source(state: Any) -> str:
        calls.append(state)
        return "❋ 12s · 31 tok/s"

    renderer.turn_summary_source = source

    renderer.start_turn("hello", thread_id="t1", user_id="u1", now=100.0)
    renderer.render_event({"type": "response", "content": "The answer."}, now=104.0)
    renderer.render_event(
        {
            "type": "done",
            "context_stats": {"tokens_per_second": 31.0, "turn_recorded": True},
        },
        now=112.0,
    )

    text = output.getvalue()
    assert text.count("❋ 12s · 31 tok/s") == 1
    # The line lands after the response, separated by exactly one blank.
    tail = [line.rstrip() for line in text.splitlines()[-3:]]
    assert tail[0].endswith("The answer.")
    assert tail[1] == ""
    assert tail[2] == "❋ 12s · 31 tok/s"
    assert len(calls) == 1

    # Replaying the same completed state must not print another summary.
    replay_output = io.StringIO()
    replay_console = Console(
        file=replay_output, width=80, force_terminal=False, no_color=True
    )
    replay_renderer = RichReplRenderer(
        capabilities=Caps(width=80),
        console=replay_console,
        width=80,
    )
    replay_renderer.turn_summary_source = source
    replay_renderer.reset_state(renderer.state)
    replay_renderer.render_state()
    assert "❋" not in replay_output.getvalue()


def test_turn_end_summary_failure_never_breaks_the_render() -> None:
    from cli_fixtures import FakeTerminalCapabilities as Caps

    output = io.StringIO()
    console = Console(file=output, width=80, force_terminal=False, no_color=True)
    renderer = RichReplRenderer(
        capabilities=Caps(width=80),
        console=console,
        width=80,
    )

    def broken_source(state: Any) -> str:
        raise RuntimeError("segment fault")

    renderer.turn_summary_source = broken_source
    renderer.start_turn("hello", thread_id="t1", user_id="u1", now=1.0)
    renderer.render_event({"type": "response", "content": "Fine."}, now=2.0)
    renderer.render_event({"type": "done"}, now=3.0)
    assert "Fine." in output.getvalue()


# ---- live tool rows: print-on-call + in-place completion flip -------------- #


def _live_row_renderer(
    *,
    scroll_bottom: int = 20,
    generation: int = 1,
    width: int = 100,
) -> tuple[RichReplRenderer, CapturedRenderOutput, dict[str, Any]]:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=width,
    )
    context: dict[str, Any] = {"value": (scroll_bottom, generation)}
    renderer.live_row_context = lambda: context["value"]
    return renderer, output, context


def test_rich_renderer_live_tool_row_prints_at_call_time_and_flips_in_place() -> None:
    renderer, output, _context = _live_row_renderer()
    renderer.start_turn("use a tool", thread_id="thread-1", now=0.0)
    renderer.render_event(
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "search_memory",
            "args": {"query": "project status"},
        },
        now=1.0,
    )
    plain = ANSI_RE.sub("", output.stdout_text)
    assert 'search_memory(query="project status") running' in plain
    assert renderer.has_live_tool_rows() is True
    before_flip = output.stdout_text

    renderer.render_event(
        {
            "type": "tool_result",
            "id": "call-1",
            "name": "search_memory",
            "result": "Found 2 matching notes.",
        },
        now=2.0,
    )
    tail = output.stdout_text[len(before_flip) :]
    # In-place rewrite: jump to the row (one above the write cursor), clear,
    # reprint, jump back. No newline means nothing was appended.
    assert tail.startswith("\x1b[1A\r\x1b[2K")
    assert tail.endswith("\r\x1b[1B")
    assert "Found 2 matching notes." in tail
    assert "running" not in ANSI_RE.sub("", tail)
    assert "\n" not in tail
    assert renderer.has_live_tool_rows() is False


def test_rich_renderer_live_parallel_tool_rows_flip_at_their_own_rows() -> None:
    renderer, output, _context = _live_row_renderer()
    renderer.start_turn("two tools", thread_id="thread-1", now=0.0)
    renderer.render_event(
        {"type": "tool_call", "id": "a", "name": "alpha_tool", "args": {}},
        now=1.0,
    )
    renderer.render_event(
        {"type": "tool_call", "id": "b", "name": "beta_tool", "args": {}},
        now=1.0,
    )
    plain = ANSI_RE.sub("", output.stdout_text)
    assert plain.index("alpha_tool") < plain.index("beta_tool")
    before = output.stdout_text

    renderer.render_event(
        {"type": "tool_result", "id": "b", "name": "beta_tool", "result": "B done"},
        now=2.0,
    )
    renderer.render_event(
        {"type": "tool_result", "id": "a", "name": "alpha_tool", "result": "A done"},
        now=3.0,
    )
    tail = output.stdout_text[len(before) :]
    # beta printed last sits one above the cursor; alpha one higher.
    assert "\x1b[1A\r\x1b[2K" in tail
    assert "\x1b[2A\r\x1b[2K" in tail
    assert "B done" in tail
    assert "A done" in tail
    assert "\n" not in tail


def test_rich_renderer_live_flip_accounts_for_interleaved_lines() -> None:
    renderer, output, _context = _live_row_renderer()
    renderer.start_turn("tool with chatter", thread_id="thread-1", now=0.0)
    renderer.render_event(
        {"type": "tool_call", "id": "call-1", "name": "slow_tool", "args": {}},
        now=1.0,
    )
    counter_after_call = renderer._line_counter.count
    renderer.render_event(
        {"type": "compacted", "context_summary": "trimmed", "messages_removed": 2},
        now=2.0,
    )
    lines_between = renderer._line_counter.count - counter_after_call
    assert lines_between > 0
    before = output.stdout_text

    renderer.render_event(
        {"type": "tool_result", "id": "call-1", "name": "slow_tool", "result": "ok"},
        now=3.0,
    )
    tail = output.stdout_text[len(before) :]
    expected_up = lines_between + 1
    assert f"\x1b[{expected_up}A\r\x1b[2K" in tail
    assert "\n" not in tail


def test_rich_renderer_live_flip_falls_back_after_generation_change() -> None:
    renderer, output, context = _live_row_renderer()
    renderer.start_turn("tool", thread_id="thread-1", now=0.0)
    renderer.render_event(
        {"type": "tool_call", "id": "call-1", "name": "slow_tool", "args": {}},
        now=1.0,
    )
    context["value"] = (20, 2)  # resize/redraw invalidated the geometry
    before = output.stdout_text

    renderer.render_event(
        {"type": "tool_result", "id": "call-1", "name": "slow_tool", "result": "ok"},
        now=2.0,
    )
    tail = output.stdout_text[len(before) :]
    assert "\x1b[2K" not in tail
    assert "slow_tool" in ANSI_RE.sub("", tail)
    assert "\n" in tail  # appended as a fresh completed row
    assert renderer.has_live_tool_rows() is False


def test_rich_renderer_live_flip_falls_back_when_row_scrolled_off() -> None:
    renderer, output, _context = _live_row_renderer(scroll_bottom=3)
    renderer.start_turn("tool", thread_id="thread-1", now=0.0)
    renderer.render_event(
        {"type": "tool_call", "id": "call-1", "name": "slow_tool", "args": {}},
        now=1.0,
    )
    renderer._line_counter.count += 10  # a screenful printed since
    before = output.stdout_text

    renderer.render_event(
        {"type": "tool_result", "id": "call-1", "name": "slow_tool", "result": "ok"},
        now=2.0,
    )
    tail = output.stdout_text[len(before) :]
    assert "\x1b[2K" not in tail
    assert "slow_tool" in ANSI_RE.sub("", tail)
    assert "\n" in tail


def test_rich_renderer_live_rows_skip_verbose_mode() -> None:
    renderer, output, _context = _live_row_renderer()
    renderer.transcript_verbose = True
    renderer.start_turn("tool", thread_id="thread-1", now=0.0)
    before_call = output.stdout_text
    renderer.render_event(
        {"type": "tool_call", "id": "call-1", "name": "slow_tool", "args": {}},
        now=1.0,
    )
    assert output.stdout_text == before_call
    assert renderer.has_live_tool_rows() is False

    renderer.render_event(
        {"type": "tool_result", "id": "call-1", "name": "slow_tool", "result": "ok"},
        now=2.0,
    )
    assert "slow_tool" in ANSI_RE.sub("", output.stdout_text)


def test_rich_renderer_without_live_context_keeps_completion_only_rows() -> None:
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(no_color=True),
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
    )
    renderer.start_turn("tool", thread_id="thread-1", now=0.0)
    renderer.render_event(
        {"type": "tool_call", "id": "call-1", "name": "slow_tool", "args": {}},
        now=1.0,
    )
    assert "slow_tool" not in output.stdout_text

    renderer.render_event(
        {"type": "tool_result", "id": "call-1", "name": "slow_tool", "result": "ok"},
        now=2.0,
    )
    plain = ANSI_RE.sub("", output.stdout_text)
    assert plain.count("slow_tool") == 1
    assert "\x1b[2K" not in output.stdout_text


def test_rich_renderer_running_tick_updates_elapsed_in_place() -> None:
    renderer, output, _context = _live_row_renderer()
    renderer.start_turn("tool", thread_id="thread-1", now=0.0)
    renderer.render_event(
        {"type": "tool_call", "id": "call-1", "name": "slow_tool", "args": {}},
        now=1.0,
    )
    before = output.stdout_text

    renderer.render_running_tick(now=13.0)
    tail = output.stdout_text[len(before) :]
    assert tail.startswith("\x1b[1A\r\x1b[2K")
    assert tail.endswith("\r\x1b[1B")
    assert "running 12s" in ANSI_RE.sub("", tail)
    assert "\n" not in tail
    assert renderer.has_live_tool_rows() is True


def test_follow_footer_live_row_context_tracks_pin_generation() -> None:
    app = CLIApp(
        agent=None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    output = FakePromptOutput(columns=80, rows=24, rows_below_cursor=1)
    prompt_renderer = FakePromptRenderer(output)
    prompt_renderer._min_available_height = 0
    runtime = _RichReplRuntime(
        app=app,
        renderer=RichReplRenderer(
            capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
            width=80,
        ),
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
    )
    runtime.application = SimpleNamespace(
        output=output,
        renderer=prompt_renderer,
        is_running=True,
    )
    assert runtime.footer.live_row_context() is None

    assert runtime.save_follow_footer_transcript_cursor() is True
    runtime.footer._follow_footer_pin_probe_pending = True
    runtime.prepare_follow_footer_render()
    first = runtime.footer.live_row_context()
    assert first is not None
    assert first[0] == 19  # 24 rows - 5 footer rows

    runtime.footer._deactivate_pinned_footer(reset_terminal=False)
    assert runtime.footer.live_row_context() is None

    assert runtime.save_follow_footer_transcript_cursor() is True
    runtime.footer._follow_footer_pin_probe_pending = True
    prompt_renderer._min_available_height = 0
    runtime.prepare_follow_footer_render()
    second = runtime.footer.live_row_context()
    assert second is not None
    assert second[1] > first[1]


def test_runtime_wires_live_row_context_into_renderer() -> None:
    runtime, _output, _prompt_renderer, _controller = _make_scroll_region_runtime()
    assert runtime.renderer.live_row_context == runtime.footer.live_row_context


def test_tool_row_ticker_runs_while_live_rows_exist(monkeypatch: Any) -> None:
    from nymeria.triggers.cli import repl_runtime as repl_runtime_module

    monkeypatch.setattr(repl_runtime_module, "_TOOL_ROW_TICK_SECONDS", 0.01)

    async def exercise() -> None:
        runtime, _output, _prompt_renderer, _controller = _make_scroll_region_runtime()
        runtime.footer._pinned_footer_active = True
        runtime.footer._pinned_scroll_bottom = 19
        live = {"rows": True}
        ticks: list[float] = []
        runtime.renderer = SimpleNamespace(
            has_live_tool_rows=lambda: live["rows"],
            render_running_tick=lambda now: ticks.append(now),
        )

        async def fake_render(callback: Any) -> None:
            callback()

        runtime.footer.render_above_prompt = fake_render  # type: ignore[method-assign]
        runtime._ensure_tool_row_ticker()
        task = runtime._tool_row_ticker_task
        assert task is not None
        await asyncio.sleep(0.06)
        assert ticks

        live["rows"] = False
        await asyncio.sleep(0.06)
        assert runtime._tool_row_ticker_task is None or runtime._tool_row_ticker_task.done()
        await runtime.stop_tool_row_ticker_async()

    asyncio.run(exercise())


def test_tool_row_ticker_ticks_while_floating(monkeypatch: Any) -> None:
    # Float-phase ticks are safe again: the render window repaints the pt
    # layout synchronously inside its synchronized-output bracket, so the
    # elapsed timer counts from the very first pre-pin row.
    from nymeria.triggers.cli import repl_runtime as repl_runtime_module

    monkeypatch.setattr(repl_runtime_module, "_TOOL_ROW_TICK_SECONDS", 0.01)

    async def exercise() -> None:
        runtime, _output, _prompt_renderer, _controller = _make_scroll_region_runtime()
        assert runtime.pinned_footer_active() is False  # floating
        live = {"rows": True}
        ticks: list[float] = []
        runtime.renderer = SimpleNamespace(
            has_live_tool_rows=lambda: live["rows"],
            render_running_tick=lambda now: ticks.append(now),
        )

        async def fake_render(callback: Any) -> None:
            callback()

        runtime.footer.render_above_prompt = fake_render  # type: ignore[method-assign]
        runtime._ensure_tool_row_ticker()
        assert runtime._tool_row_ticker_task is not None
        await asyncio.sleep(0.06)
        assert ticks

        live["rows"] = False
        await asyncio.sleep(0.06)
        await runtime.stop_tool_row_ticker_async()

    asyncio.run(exercise())


def test_follow_footer_sync_output_reset_survives_callback_exception() -> None:
    # The whole point of the try/finally: a raising transcript callback
    # must still emit exactly one mode-2026 reset on both render windows,
    # or the terminal stops painting.
    async def exercise() -> tuple[list[tuple[str, object]], list[tuple[str, object]]]:
        runtime, output, _prompt_renderer, _controller = _make_scroll_region_runtime()

        def boom() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError, match="boom"):
            await runtime.render_above_prompt(boom)
        float_ops = list(output.ops)
        output.ops.clear()

        runtime.footer._follow_footer_transcript_cursor_saved = True
        runtime.footer._pinned_footer_active = True
        runtime.footer._pinned_footer_height = 5
        runtime.footer._pinned_scroll_bottom = 19
        runtime.footer._pinned_terminal_size = (80, 24)
        with pytest.raises(RuntimeError, match="boom"):
            await runtime.render_above_prompt(boom)
        return float_ops, list(output.ops)

    float_ops, pinned_ops = asyncio.run(exercise())
    for ops in (float_ops, pinned_ops):
        assert ops.count(("raw", "\x1b[?2026h")) == 1
        assert ops.count(("raw", "\x1b[?2026l")) == 1
        assert ops.index(("raw", "\x1b[?2026h")) < ops.index(("raw", "\x1b[?2026l"))


def test_follow_footer_skips_in_window_repaint_during_run_in_terminal() -> None:
    # Mirror of Application._redraw's guard: during run_in_terminal (raw
    # input() prompts, e.g. /connect) the layout must NOT be painted over
    # the user's half-typed line in cooked mode.
    async def exercise() -> list[tuple[str, object]]:
        runtime, output, _prompt_renderer, _controller = _make_scroll_region_runtime()
        runtime.application._running_in_terminal = True
        await runtime.render_above_prompt(lambda: None)
        return output.ops

    ops = asyncio.run(exercise())
    assert not any(op[0] == "layout_render" for op in ops)
    # The synchronized window itself still brackets the transcript write.
    assert ops.count(("raw", "\x1b[?2026h")) == 1
    assert ops.count(("raw", "\x1b[?2026l")) == 1


def test_follow_footer_repaint_fault_schedules_full_resync() -> None:
    # A mid-render fault leaves pt's cursor belief behind the physical
    # state; the fallback must be the engine's full redraw (clear + replay),
    # never a bare invalidate that would repaint from a stale origin.
    async def exercise() -> tuple[list[tuple[str, object]], Any]:
        runtime, output, prompt_renderer, _controller = _make_scroll_region_runtime()

        def broken_render(app: Any, layout: Any, is_done: bool = False) -> None:
            raise RuntimeError("layout walk fault")

        prompt_renderer.render = broken_render  # type: ignore[method-assign]
        redraw_mock = AsyncMock()
        runtime.footer.redraw = redraw_mock  # type: ignore[method-assign]
        await runtime.render_above_prompt(lambda: None)  # must not raise
        await asyncio.sleep(0)  # let the resync task run
        return output.ops, redraw_mock

    ops, redraw_mock = asyncio.run(exercise())
    assert redraw_mock.await_count == 1
    assert ops.count(("raw", "\x1b[?2026l")) == 1  # window still closed


def test_follow_footer_activation_compensates_live_pt_scroll() -> None:
    # With the in-window repaint, _last_screen is populated at pin time, so
    # activation's pt_scroll compensation runs live: last_h=3 with
    # rows_below=1 means pt already scrolled 2 of the desired 5 rows, and
    # activation must add only the remaining 3.
    app = CLIApp(
        agent=None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    output = FakePromptOutput(columns=80, rows=24, rows_below_cursor=99)
    prompt_renderer = FakePromptRenderer(output)
    prompt_renderer._min_available_height = 1  # CPR: 1 row below cursor
    prompt_renderer._last_screen = SimpleNamespace(height=3)
    runtime = _RichReplRuntime(
        app=app,
        renderer=RichReplRenderer(
            capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
            width=80,
        ),
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
    )
    runtime.application = SimpleNamespace(
        output=output,
        renderer=prompt_renderer,
        layout=SimpleNamespace(),
        is_running=True,
    )
    assert runtime.save_follow_footer_transcript_cursor() is True
    runtime.footer._follow_footer_pin_probe_pending = True
    runtime.prepare_follow_footer_render()
    assert runtime.pinned_footer_active() is True
    # desired_total_scroll = footer(5) - rb(1) + 1 = 5; pt_scroll = 3 - 1 = 2.
    assert ("raw", "\r\n" * 3) in output.ops
    assert ("raw", "\r\n" * 5) not in output.ops


def test_rich_renderer_orphaned_running_steps_stay_invisible() -> None:
    # A dead stream (e.g. turn_lost) leaves running steps in history with no
    # entry in active_tool_calls; later turns must not re-print stale rows.
    renderer, output, _context = _live_row_renderer()
    renderer.start_turn("first", thread_id="thread-1", now=0.0)
    renderer.render_event(
        {"type": "tool_call", "id": "call-1", "name": "slow_tool", "args": {}},
        now=1.0,
    )
    renderer.render_event(
        {"type": "error", "code": "turn_lost", "content": "stream lost"},
        now=2.0,
    )

    renderer.start_turn("second", thread_id="thread-1", now=3.0)
    assert renderer.has_live_tool_rows() is False
    before = output.stdout_text
    renderer.render_event({"type": "response", "content": "Hello again."}, now=4.0)
    renderer.render_event({"type": "done"}, now=5.0)
    tail = ANSI_RE.sub("", output.stdout_text[len(before) :])
    assert "slow_tool" not in tail
    assert renderer.has_live_tool_rows() is False


def test_rich_renderer_counts_attached_transcript_console_lines() -> None:
    # Slash-command/form/header output rides the app's own console into the
    # same transcript region; those lines must shift later flips down.
    renderer, output, _context = _live_row_renderer()
    extra = Console(
        file=output.stdout,
        force_terminal=False,
        no_color=True,
        width=100,
        highlight=False,
    )
    renderer.attach_transcript_console(extra)
    renderer.start_turn("tool", thread_id="thread-1", now=0.0)
    renderer.render_event(
        {"type": "tool_call", "id": "call-1", "name": "slow_tool", "args": {}},
        now=1.0,
    )
    extra.print("command output line")
    extra.print("second line")
    before = output.stdout_text

    renderer.render_event(
        {"type": "tool_result", "id": "call-1", "name": "slow_tool", "result": "ok"},
        now=2.0,
    )
    tail = output.stdout_text[len(before) :]
    # Two counted foreign lines push the row two further above the cursor.
    assert tail.startswith("\x1b[3A\r\x1b[2K")
    assert "\n" not in tail


def test_rich_renderer_attach_transcript_console_failure_disables_live_rows() -> None:
    renderer, output, _context = _live_row_renderer()

    class NoFileConsole:
        @property
        def file(self) -> Any:
            raise RuntimeError("no file")

    renderer.attach_transcript_console(NoFileConsole())  # type: ignore[arg-type]
    renderer.start_turn("tool", thread_id="thread-1", now=0.0)
    before = output.stdout_text
    renderer.render_event(
        {"type": "tool_call", "id": "call-1", "name": "slow_tool", "args": {}},
        now=1.0,
    )
    assert output.stdout_text == before  # dormant: completion-only again


def test_rich_renderer_uncounted_stderr_keeps_flip_rows_stable() -> None:
    # StringIO stderr is not a tty, so its lines do not scroll the terminal
    # and must not move the row arithmetic.
    renderer, output, _context = _live_row_renderer()
    renderer.start_turn("tool", thread_id="thread-1", now=0.0)
    renderer.render_event(
        {"type": "tool_call", "id": "call-1", "name": "slow_tool", "args": {}},
        now=1.0,
    )
    renderer.error_console.print("Error: boom")
    before = output.stdout_text

    renderer.render_event(
        {"type": "tool_result", "id": "call-1", "name": "slow_tool", "result": "ok"},
        now=2.0,
    )
    tail = output.stdout_text[len(before) :]
    assert tail.startswith("\x1b[1A\r\x1b[2K")


def test_rich_renderer_running_tick_clears_slots_when_context_dies() -> None:
    renderer, output, context = _live_row_renderer()
    renderer.start_turn("tool", thread_id="thread-1", now=0.0)
    renderer.render_event(
        {"type": "tool_call", "id": "call-1", "name": "slow_tool", "args": {}},
        now=1.0,
    )
    assert renderer.has_live_tool_rows() is True
    context["value"] = None  # unpinned / geometry gate went false
    before = output.stdout_text

    renderer.render_running_tick(now=5.0)
    assert output.stdout_text == before
    assert renderer.has_live_tool_rows() is False


def test_follow_footer_live_row_context_respects_dynamic_region_gate() -> None:
    app = CLIApp(
        agent=None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    output = FakePromptOutput(columns=80, rows=24, rows_below_cursor=1)
    prompt_renderer = FakePromptRenderer(output)
    prompt_renderer._min_available_height = 0
    runtime = _RichReplRuntime(
        app=app,
        renderer=RichReplRenderer(
            capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
            width=80,
        ),
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
    )
    runtime.application = SimpleNamespace(
        output=output,
        renderer=prompt_renderer,
        is_running=True,
    )
    assert runtime.save_follow_footer_transcript_cursor() is True
    runtime.footer._follow_footer_pin_probe_pending = True
    runtime.prepare_follow_footer_render()
    assert runtime.footer.live_row_context() is not None

    output.rows = 8  # shrank below the scroll-region minimum while pinned
    assert runtime.footer.live_row_context() is None


def test_runtime_attaches_app_console_to_line_counter() -> None:
    runtime, _output, _prompt_renderer, _controller = _make_scroll_region_runtime()
    from nymeria.triggers.cli.rendering.rich_repl import _NewlineCountingWriter

    app_file = runtime.app.state.console.file
    assert isinstance(app_file, _NewlineCountingWriter)
    assert app_file._counter is runtime.renderer._line_counter


def test_tool_row_result_preview_strips_terminal_control_codes() -> None:
    from nymeria.triggers.cli.rendering.tool_rows import format_result_preview

    preview = format_result_preview("ok\x1b[5A\x1b[2Kdone\x9bxyz")
    assert "\x1b" not in preview
    assert "\x9b" not in preview
    assert "ok" in preview and "done" in preview


def test_rich_renderer_running_rows_use_hollow_marker_and_fill_on_completion() -> None:
    renderer, output, _context = _live_row_renderer()
    renderer.start_turn("tool", thread_id="thread-1", now=0.0)
    renderer.render_event(
        {"type": "tool_call", "id": "call-1", "name": "slow_tool", "args": {}},
        now=1.0,
    )
    plain = ANSI_RE.sub("", output.stdout_text)
    assert "✧ slow_tool running" in plain  # hollow while in flight
    before = output.stdout_text

    renderer.render_event(
        {"type": "tool_result", "id": "call-1", "name": "slow_tool", "result": "ok"},
        now=2.0,
    )
    tail = ANSI_RE.sub("", output.stdout_text[len(before) :])
    assert "❖ slow_tool" in tail  # fills in on landing
    assert "✧" not in tail


def test_follow_footer_float_snapshot_supplies_live_row_context() -> None:
    # Before the footer first pins, the pin-probe CPR responses give the
    # engine cursor knowledge; live rows must work in that float phase too.
    app = CLIApp(
        agent=None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    output = FakePromptOutput(columns=80, rows=24, rows_below_cursor=99)
    prompt_renderer = FakePromptRenderer(output)
    prompt_renderer._min_available_height = 10  # CPR answered: 10 rows below
    runtime = _RichReplRuntime(
        app=app,
        renderer=RichReplRenderer(
            capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
            width=80,
        ),
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
    )
    runtime.application = SimpleNamespace(
        output=output,
        renderer=prompt_renderer,
        is_running=True,
    )
    assert runtime.footer.live_row_context() is None  # no snapshot yet

    assert runtime.save_follow_footer_transcript_cursor() is True
    runtime.footer._follow_footer_pin_probe_pending = True
    runtime.prepare_follow_footer_render()
    # 10 rows below > footer height: still floating, but snapshot stashed.
    assert runtime.pinned_footer_active() is False
    context = runtime.footer.live_row_context()
    assert context is not None
    anchor, generation = context
    # Deliberately one row ABOVE the true CPR cursor row (24 - 10 + 1): a
    # conservative margin, so the guard only ever skips a flip, never
    # mis-addresses one.
    assert anchor == 24 - 10

    # Printing transcript lines moves the anchor down with the cursor.
    runtime.renderer._line_counter.count += 3
    assert runtime.footer.live_row_context() == (24 - 10 + 3, generation)

    # The anchor clamps at height - footer: prompt_toolkit scrolls the
    # write point back to that boundary whenever its layout grows, and
    # those scrolls are uncounted, so the estimate must never pass it.
    runtime.renderer._line_counter.count += 50
    clamped = runtime.footer.live_row_context()
    assert clamped is not None
    assert clamped[0] == 24 - 5  # 5 footer rows in this fixture

    # A resize voids the snapshot (rows_below was measured against the old
    # geometry); matching geometry brings it back.
    output.rows = 30
    assert runtime.footer.live_row_context() is None
    output.rows = 24
    assert runtime.footer.live_row_context() == clamped

    # Destructive transitions drop the snapshot with the generation bump.
    runtime.footer._deactivate_pinned_footer(reset_terminal=False)
    assert runtime.footer.live_row_context() is None


def test_follow_footer_live_rows_survive_float_to_pin_activation() -> None:
    # A tool row registered during the float phase must stay flippable when
    # the growing transcript pins the footer: activation lands the content
    # tail one row above scroll_bottom (the same relative geometry the float
    # phase maintained), so the generation deliberately does not bump.
    app = CLIApp(
        agent=None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    output = FakePromptOutput(columns=80, rows=24, rows_below_cursor=99)
    prompt_renderer = FakePromptRenderer(output)
    prompt_renderer._min_available_height = 10  # float: 10 rows below > footer
    runtime = _RichReplRuntime(
        app=app,
        renderer=RichReplRenderer(
            capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
            width=80,
        ),
        capabilities=FakeTerminalCapabilities(width=80, height=24, renderer="rich"),
    )
    runtime.application = SimpleNamespace(
        output=output,
        renderer=prompt_renderer,
        is_running=True,
    )
    assert runtime.save_follow_footer_transcript_cursor() is True
    runtime.footer._follow_footer_pin_probe_pending = True
    runtime.prepare_follow_footer_render()
    float_context = runtime.footer.live_row_context()
    assert float_context is not None
    assert runtime.pinned_footer_active() is False

    # The transcript grows until the next probe reports the footer at the
    # bottom (rows below == footer height); activation pins without
    # invalidating live registrations.
    assert runtime.save_follow_footer_transcript_cursor() is True
    runtime.footer._follow_footer_pin_probe_pending = True
    prompt_renderer._min_available_height = 5
    runtime.prepare_follow_footer_render()
    assert runtime.pinned_footer_active() is True
    pinned_context = runtime.footer.live_row_context()
    assert pinned_context is not None
    assert pinned_context[0] == 19  # 24 rows - 5 footer rows
    assert pinned_context[1] == float_context[1]  # same generation
