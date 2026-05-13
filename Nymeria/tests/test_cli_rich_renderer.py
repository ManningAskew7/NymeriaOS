from __future__ import annotations

import asyncio
import io
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cli_fixtures import CapturedRenderOutput, FakeAgentClient, FakeTerminalCapabilities
from rich.console import Console

from nymeria.triggers.cli.app import (
    CLIApp,
    CLIRuntimeConfig,
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
        self._min_available_height = 99
        self._last_screen = object()
        self.cpr_request_count = 0

    def erase(self, *, leave_alternate_screen: bool = True) -> None:
        self.erase_count += 1
        self.output.ops.append(("erase", leave_alternate_screen))

    def reset(self, *, leave_alternate_screen: bool = True) -> None:
        self.reset_count += 1
        self.output.ops.append(("reset", leave_alternate_screen))

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

    assert application._on_resize.__self__ is runtime
    assert application._on_resize.__func__ is runtime.handle_terminal_resize.__func__


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
        runtime._terminal_size = (80, 24)
        runtime._resize_pending = True
        output.rows = 30
        redraw = AsyncMock()
        runtime.redraw = redraw  # type: ignore[method-assign]

        await runtime._maybe_resize_redraw()

        return renderer, redraw, runtime

    renderer, redraw, runtime = asyncio.run(exercise())

    renderer.update_terminal_width.assert_called_once_with(80)
    redraw.assert_awaited_once()
    assert runtime._terminal_size == (80, 30)
    assert runtime._resize_pending is False


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
    runtime._pinned_footer_active = True
    runtime._pinned_footer_height = 5
    runtime._pinned_scroll_bottom = 25
    runtime._pinned_terminal_size = (100, 30)

    runtime._redraw_follow_footer()

    assert output.ops.index(("raw", "\x1b[r")) < output.ops.index(("erase_screen", None))
    assert ("hide_cursor", None) in output.ops
    assert ("cursor", (0, 0)) in output.ops
    assert ("reset", False) in output.ops
    assert output.ops[-2:] == [("raw", "\x1b7"), ("flush", None)]
    assert runtime.pinned_footer_active() is False


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
    runtime._resize_pending = True
    runtime._resize_task = PendingTask()  # type: ignore[assignment]

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
            runtime_config=CLIRuntimeConfig(renderer="rich"),
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
        output = FakePromptOutput(columns=80, rows=24, rows_below_cursor=0)
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
            is_running=True,
        )
        events: list[str] = []

        assert runtime.save_follow_footer_transcript_cursor() is True
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
    assert ("raw", "\x1b8") in activation_ops
    assert ("raw", "\r\n" * 5) in activation_ops
    assert events == ["rendered"]
    assert ops.count(("hide_cursor", None)) == 2
    assert ("raw", "\x1b[1;19r") in ops
    assert ("raw", "\x1b8") in ops
    assert ("raw", "\x1b7") in ops
    assert ("raw", "\x1b[r") in ops
    assert ("erase", False) not in ops
    assert erase_count == 1


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
