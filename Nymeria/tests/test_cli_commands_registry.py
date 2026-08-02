from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cli_fixtures import run

from nymeria.triggers.cli.commands import (
    Command,
    CommandContext,
    CommandMessage,
    CommandRegistry,
    CommandResult,
    ListCommandOutputSink,
)


def test_parse_and_resolve_root_alias_and_subcommand_alias() -> None:
    registry = CommandRegistry(include_builtins=False)
    registry.register(
        Command(
            name="thread",
            aliases=["/t"],
            description="Manage threads",
            handler=lambda _state, _args: None,
            subcommands={
                "switch": Command(
                    name="switch",
                    aliases=["s"],
                    description="Switch thread",
                    handler=lambda _state, _args: None,
                )
            },
        )
    )

    invocation = registry.parse('/t s "thread one"')
    match = registry.resolve(invocation)

    assert invocation is not None
    assert invocation.command_name == "t"
    assert match is not None
    assert match.path == ("thread", "switch")
    assert match.args == ("thread one",)


def test_async_context_handler_receives_client_and_helpers() -> None:
    registry = CommandRegistry(include_builtins=False)
    client = object()
    actions: list[Any] = []
    prompts: list[str] = []

    async def handler(ctx: CommandContext, args: list[str]) -> CommandResult:
        assert ctx.client is client
        assert ctx.thread_id == "thread-1"
        assert ctx.user_id == "alice"
        prompts.append("confirm")
        assert await ctx.confirm("confirm?") is True
        await ctx.dispatch({"type": "command", "args": args})
        return CommandResult.completed(
            CommandMessage(f"ran {' '.join(args)}", level="success")
        )

    registry.register(
        Command(
            name="ping",
            description="Ping test command",
            handler=handler,
        )
    )
    sink = ListCommandOutputSink()
    context = CommandContext(
        client=client,
        output=sink,
        dispatch_state=actions.append,
        confirm_handler=lambda _prompt: True,
        thread_id="thread-1",
        user_id="alice",
    )

    result = run(registry.dispatch_async(context, "/ping hello world"))

    assert result.ok is True
    assert result.command_path == ("ping",)
    assert actions == [{"type": "command", "args": ["hello", "world"]}]
    assert prompts == ["confirm"]
    assert sink.messages == [
        CommandMessage("ran hello world", level="success")
    ]


def test_json_flag_strips_args_and_emits_result_payload(capsys: Any) -> None:
    registry = CommandRegistry(include_builtins=False)
    sink = ListCommandOutputSink()

    async def handler(ctx: CommandContext, args: list[str]) -> CommandResult:
        assert ctx.metadata["json_output"] is True
        assert args == ["alpha"]
        return CommandResult.completed(
            "human text",
            json_payload={"args": args, "mode": "json"},
        )

    registry.register(
        Command(
            name="echo",
            description="Echo test command",
            handler=handler,
        )
    )
    context = CommandContext(output=sink)

    result = run(registry.dispatch_async(context, "/echo --json alpha"))

    captured = capsys.readouterr()
    assert result.ok is True
    assert json.loads(captured.out) == {"args": ["alpha"], "mode": "json"}
    assert captured.err == ""
    assert sink.messages == []


def test_unknown_command_returns_structured_error() -> None:
    registry = CommandRegistry(include_builtins=False)
    sink = ListCommandOutputSink()

    result = run(
        registry.dispatch_async(CommandContext(output=sink), "/missing value")
    )

    assert result.handled is True
    assert result.status == "error"
    assert result.error_code == "unknown_command"
    assert result.payload["command"] == "missing"
    assert sink.messages[0].level == "error"
    assert "Unknown command: /missing" in sink.messages[0].content


def test_non_command_input_is_unhandled() -> None:
    registry = CommandRegistry(include_builtins=False)
    sink = ListCommandOutputSink()

    result = run(registry.dispatch_async(CommandContext(output=sink), "chat text"))

    assert result.handled is False
    assert result.status == "unhandled"
    assert sink.messages == []


def test_builtin_help_exit_and_clear_are_context_commands() -> None:
    registry = CommandRegistry()
    registry.register(
        Command(
            name="thread",
            description="Manage threads",
            usage="/thread list",
            handler=lambda _ctx, _args: None,
        )
    )
    registry.register(
        Command(
            name="help",
            aliases=["/legacy-help"],
            description="A re-registration should not replace builtin help",
            handler=lambda _ctx, _args: None,
        )
    )
    sink = ListCommandOutputSink()
    context = CommandContext(output=sink)

    help_result = run(registry.dispatch_async(context, "/help thread"))
    exit_result = run(registry.dispatch_async(context, "/exit"))
    clear_result = run(registry.dispatch_async(context, "/cls"))

    assert help_result.status == "ok"
    assert "/thread list" in sink.messages[0].content
    assert exit_result.status == "exit"
    assert clear_result.status == "clear"
    assert "/legacy-help" in registry.get_completions()


def test_help_deduplicates_default_subcommands_and_wraps_long_rows() -> None:
    registry = CommandRegistry()
    registry.register(
        Command(
            name="tools",
            description="List/manage tools",
            usage="/tools list",
            handler=lambda _state, _args: None,
            category="Tools",
            subcommands={
                "list": Command(
                    name="list",
                    description="List tools",
                    usage="list",
                    handler=lambda _state, _args: None,
                    category="Tools",
                ),
                "test": Command(
                    name="test",
                    description="Test a custom tool",
                    usage="test <custom-tool-id> [json-or-key=value...]",
                    handler=lambda _state, _args: None,
                    category="Tools",
                ),
            },
        )
    )
    sink = ListCommandOutputSink()
    context = CommandContext(
        output=sink,
        metadata={"capabilities": SimpleNamespace(width=60)},
    )

    result = run(registry.dispatch_async(context, "/help tools"))

    assert result.status == "ok"
    content = sink.messages[0].content
    assert content.count("/tools list") == 1
    assert "List/manage tools" not in content
    assert "Tools" in content
    lines = content.splitlines()
    long_usage_index = next(
        index for index, line in enumerate(lines) if "/tools test" in line
    )
    assert lines[long_usage_index].strip().startswith("/tools test")
    assert lines[long_usage_index + 1].strip() == "Test a custom tool"


def test_completion_items_and_palette_entries_include_descriptions() -> None:
    registry = CommandRegistry(include_builtins=False)
    registry.register(
        Command(
            name="tools",
            description="Manage tools",
            usage="/tools",
            handler=lambda _state, _args: None,
            subcommands={
                "enable": Command(
                    name="enable",
                    aliases=["on"],
                    description="Enable a tool",
                    usage="enable <name>",
                    handler=lambda _state, _args: None,
                )
            },
        )
    )

    completions = registry.get_completion_items()
    palette_entries = registry.get_palette_entries()

    assert any(
        item.text == "/tools enable"
        and item.description == "Enable a tool"
        for item in completions
    )
    assert any(item.text == "/tools on" for item in completions)
    assert any(
        entry.text == "/tools enable"
        and entry.usage == "/tools enable <name>"
        for entry in palette_entries
    )




# ── Rich console sink: legacy-markdown artifact translation ─────────────────

class _RecordingConsole:
    def __init__(self) -> None:
        self.printed: list[Any] = []

    def print(self, content: Any = "", **_kwargs: Any) -> None:
        self.printed.append(content)


# Slot hexes the sink resolves through the default theme; pinned here so a
# palette change is a conscious test update, not silent drift.
_SUCCESS_HEX = "#A9DCB4"
_WARNING_HEX = "#F5D08C"
_ERROR_HEX = "#FCA5A5"


@pytest.fixture(autouse=True)
def _isolated_theme(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the theme config away from the operator's real cli.json: sinks
    constructed without an injected theme load from disk, and operator
    overrides must not leak into these assertions."""

    monkeypatch.setenv("NYMERIA_CLI_CONFIG", str(tmp_path / "cli.json"))


def _sink(console: Any, theme: Any = None):
    from nymeria.triggers.cli.rendering.command_output import (
        RichConsoleCommandOutputSink,
    )

    if theme is None:
        return RichConsoleCommandOutputSink(console)
    return RichConsoleCommandOutputSink(console, theme=theme)


def _sink_output(message: CommandMessage, theme: Any = None) -> list[Any]:
    console = _RecordingConsole()
    _sink(console, theme).emit(message)
    return console.printed


def _plains(printed: list[Any]) -> list[str]:
    """Flatten printed items (Texts, Padding-wrapped blocks, blanks)."""

    from rich.padding import Padding

    out: list[str] = []
    for item in printed:
        if isinstance(item, Padding):
            item = item.renderable
        out.append(getattr(item, "plain", str(item)))
    return out


def _span_styles(text: Any) -> list[str]:
    return [str(span.style) for span in text.spans]


def _command_console(width: int):
    import io

    from rich.console import Console

    return Console(
        width=width,
        record=True,
        file=io.StringIO(),
        force_terminal=True,
        color_system="truecolor",
    )


def _rendered_output(
    message: CommandMessage, *, width: int = 60, styles: bool = False
) -> str:
    """Emit through a real recording Console: the markdown-adapter path
    prints renderables only a real console can flatten."""

    console = _command_console(width)
    _sink(console).emit(message)
    return console.export_text(styles=styles)


def _rendered_lines(message: CommandMessage, *, width: int = 60) -> list[str]:
    lines = [
        line.rstrip()
        for line in _rendered_output(message, width=width).splitlines()
    ]
    while lines and lines[-1] == "":
        lines.pop()
    return lines


def test_sink_renders_heading_via_adapter_and_body_verbatim() -> None:
    lines = _rendered_lines(
        CommandMessage(
            "### Provider\n\nActive provider  Anthropic\nActive model     claude"
        )
    )
    # Heading styled (no literal hashes), blank preserved, aligned body
    # rows verbatim on their own lines, all in the 2-col column.
    assert lines == [
        "  Provider",
        "",
        "  Active provider  Anthropic",
        "  Active model     claude",
    ]


def test_sink_translates_error_and_done_prefixes_into_glyphs() -> None:
    printed = _sink_output(
        CommandMessage("**Error:** something broke", level="error")
    )
    assert _plains(printed) == ["✗ something broke"]
    assert any(_ERROR_HEX in style for style in _span_styles(printed[0]))

    printed = _sink_output(
        CommandMessage("**Done.** Model set.", level="success")
    )
    assert _plains(printed) == ["✓ Model set."]
    # The glyph carries the success accent, and ONLY the glyph: the text
    # spans must not be washed.
    glyph_span = printed[0].spans[0]
    assert _SUCCESS_HEX in str(glyph_span.style)
    assert not any(
        _SUCCESS_HEX in str(span.style) or "green" in str(span.style)
        for span in printed[0].spans[1:]
    )


def test_sink_success_readout_has_no_wash_and_no_glyph() -> None:
    """The headline fix: a plain success readout (every backend listing)
    renders in the normal palette: no green anywhere, no check mark."""

    message = CommandMessage(
        "### Status\n\n  Provider   cliproxy\n  Model      claude-fable-5",
        level="success",
    )
    styled = _rendered_output(message, styles=True)
    assert "✓" not in styled
    assert "\x1b[32m" not in styled  # classic ANSI green
    assert "38;2;169;220;180" not in styled  # the success slot's RGB
    lines = _rendered_lines(message)
    assert lines == [
        "  Status",
        "",
        "    Provider   cliproxy",
        "    Model      claude-fable-5",
    ]


def test_sink_error_and_warning_levels_get_glyph_and_tint() -> None:
    printed = _sink_output(CommandMessage("Login failed: x", level="error"))
    assert _plains(printed) == ["✗ Login failed: x"]
    assert any(_ERROR_HEX in style for style in _span_styles(printed[0]))

    printed = _sink_output(CommandMessage("key looks short", level="warning"))
    assert _plains(printed) == ["! key looks short"]
    assert any(_WARNING_HEX in style for style in _span_styles(printed[0]))


def test_sink_glyph_attaches_to_a_heading_first_line() -> None:
    printed = _sink_output(
        CommandMessage("### Broken\n\nDetail   row", level="error")
    )
    assert _plains(printed) == ["✗ Broken", "", "Detail   row"]
    styles = _span_styles(printed[0])
    assert any(_ERROR_HEX in style for style in styles)  # the glyph
    assert any("bold" in style for style in styles)  # the heading text


def test_sink_keeps_literal_brackets_out_of_markup() -> None:
    # Command output carries literal bracket runs ([logged in: ...] badges,
    # [NATIVE] tier tags); the escaped inline renderer must print them
    # verbatim instead of feeding them to Rich's markup parser.
    printed = _sink_output(
        CommandMessage("Tier native [NATIVE]\n  [logged in: alice@example.com]")
    )
    assert _plains(printed) == [
        "Tier native [NATIVE]\n  [logged in: alice@example.com]"
    ]


def test_sink_preserves_authored_line_breaks_in_plain_bodies() -> None:
    """Backend command bodies are LINE-ORIENTED: markdown softbreak-join
    fused `Provider: x / Model: y` readouts into one line (found in
    review, demonstrated on four real command shapes)."""

    lines = _rendered_lines(
        CommandMessage("Provider: cliproxy\nModel: claude-fable-5\nEffort: xhigh")
    )
    assert lines == [
        "  Provider: cliproxy",
        "  Model: claude-fable-5",
        "  Effort: xhigh",
    ]


def test_sink_styles_inline_markdown_on_the_verbatim_path() -> None:
    printed = _sink_output(CommandMessage("state is **enabled** now"))
    assert _plains(printed) == ["state is enabled now"]
    from rich.padding import Padding

    body = printed[0]
    assert isinstance(body, Padding)
    assert any("bold" in style for style in _span_styles(body.renderable))


def test_sink_renders_markdown_list_and_pipe_table_as_blocks() -> None:
    rendered = _rendered_output(
        CommandMessage("- alpha\n- beta\n\n| col | val |\n| --- | --- |\n| a | 1 |")
    )
    # Rich bullets, not literal dashes; a laid-out table, not pipe syntax.
    assert "• alpha" in rendered
    assert "• beta" in rendered
    assert "col" in rendered and "val" in rendered
    assert "| ---" not in rendered


def test_sink_fenced_code_keeps_interior_lines_and_alignment() -> None:
    # A fence is markdown-safe by kind even when its interior is aligned:
    # the code renderer preserves LINES and spacing by construction (fence
    # content parsed as markdown would softbreak-join the rows into one
    # line), and fence markers never print as literal backtick lines.
    rendered = _rendered_output(
        CommandMessage("```\nrow1  aligned\nrow2  aligned\n```")
    )
    lines = [line.strip() for line in rendered.splitlines()]
    assert "row1  aligned" in lines
    assert "row2  aligned" in lines
    assert "```" not in rendered


def test_sink_unclosed_fence_tail_is_not_dropped() -> None:
    rendered = _rendered_output(CommandMessage("```\nrow1  x\nrow2  y"))
    assert "row1  x" in rendered
    assert "row2  y" in rendered


def test_sink_aligned_list_items_stay_verbatim() -> None:
    # A markdown list whose items carry column alignment must NOT reflow:
    # the alignment guard beats the markdown classification.
    printed = _sink_output(
        CommandMessage("- claude    active   ok\n- gpt-5.5   backup   ok")
    )
    assert _plains(printed) == [
        "- claude    active   ok\n- gpt-5.5   backup   ok"
    ]


def test_sink_setext_heading_cannot_reflow_aligned_rows() -> None:
    # Aligned rows directly above a --- underline classify as a SETEXT
    # heading; the alignment guard must still force them verbatim.
    lines = _rendered_lines(
        CommandMessage("Provider   cliproxy   ok\n------")
    )
    assert lines == ["  Provider   cliproxy   ok", "  ------"]


def test_sink_styles_heading_line_alone() -> None:
    # A heading-only message emits just the styled heading, no blank body,
    # and a bare success level adds no glyph and no success accent.
    message = CommandMessage("### Ready", level="success")
    assert _rendered_lines(message) == ["  Ready"]
    styled = _rendered_output(message, styles=True)
    # Bold + the white heading slot, as one combined SGR sequence.
    assert "\x1b[1;38;2;255;255;255m" in styled
    assert "✓" not in styled
    assert "38;2;169;220;180" not in styled


def test_sink_contiguous_glyph_body_stays_tight() -> None:
    """The glyph first line must not tear a blank into a contiguous body
    (found in review: every command typo printed a stray blank line)."""

    lines = _rendered_lines(
        CommandMessage(
            "Unknown command: /thred\nType /help for available commands.",
            level="error",
        )
    )
    assert lines == [
        "✗ Unknown command: /thred",
        "  Type /help for available commands.",
    ]
    # ...while a source blank line stays a blank line.
    lines = _rendered_lines(
        CommandMessage("**Done.** Saved.\n\nNext: /provider test", level="success")
    )
    assert lines == ["✓ Saved.", "", "  Next: /provider test"]


def test_sink_blocks_never_double_blank() -> None:
    rendered = _rendered_output(
        CommandMessage("Steps:\n\n- alpha\n- beta\n\nEnd.")
    )
    lines = [line.rstrip() for line in rendered.splitlines()]
    assert "  End." in lines
    for first, second in zip(lines, lines[1:]):
        assert not (first == "" and second == ""), lines


def test_sink_long_verbatim_lines_wrap_inside_the_gutter() -> None:
    # A row longer than the console must wrap INSIDE the 2-col column,
    # never dropping its continuation to column 0 (found in review: 42 of
    # /help's 140 rows overflowed at width 80).
    lines = _rendered_lines(
        CommandMessage("  label      " + "x" * 40), width=30
    )
    assert len(lines) > 1
    assert all(line.startswith("  ") for line in lines if line)


def test_help_output_width_reserves_the_transcript_gutter() -> None:
    from nymeria.triggers.cli.commands.registry import _help_output_width

    sized = CommandContext(
        output=ListCommandOutputSink(),
        metadata={"capabilities": SimpleNamespace(width=100)},
    )
    assert _help_output_width(sized) == 98
    fallback = CommandContext(output=ListCommandOutputSink(), metadata={})
    assert _help_output_width(fallback) == 86


def test_sink_uses_the_injected_live_theme() -> None:
    from nymeria.triggers.cli.theme import DEFAULT_CLI_THEME

    theme = DEFAULT_CLI_THEME.with_override("error", "#112233")
    printed = _sink_output(CommandMessage("nope", level="error"), theme=theme)
    assert any("#112233" in style for style in _span_styles(printed[0]))


def test_sink_default_theme_loads_operator_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Constructed WITHOUT an injected theme (standalone use), the sink
    # loads the persisted /theme overrides rather than the defaults.
    config = tmp_path / "cli.json"
    config.write_text(
        json.dumps({"theme": {"error": "#445566"}}), encoding="utf-8"
    )
    monkeypatch.setenv("NYMERIA_CLI_CONFIG", str(config))
    printed = _sink_output(CommandMessage("nope", level="error"))
    assert any("#445566" in style for style in _span_styles(printed[0]))


def test_sink_renders_crlf_content_clean() -> None:
    # Owned by the block splitter (newline normalize) and the inline
    # renderer (never emits \r); the sink adds no normalize of its own,
    # measured redundant. This pins the integration contract on both the
    # glyph and the block path.
    message = CommandMessage("line one\r\nline two", level="error")
    assert _rendered_lines(message) == ["✗ line one", "  line two"]
    assert "\r" not in _rendered_output(message)
    assert _rendered_lines(CommandMessage("line one\r\nline two")) == [
        "  line one",
        "  line two",
    ]


def test_sink_empty_content_prints_nothing() -> None:
    assert _sink_output(CommandMessage("")) == []
    # A level glyph with nothing to say must not print a bare ✗ / ! line.
    assert _sink_output(CommandMessage("", level="error")) == []
    assert _sink_output(CommandMessage("   \n  ", level="warning")) == []
