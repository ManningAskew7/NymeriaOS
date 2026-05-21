from __future__ import annotations

from nymeria.triggers.cli.commands import Command, CommandRegistry
from nymeria.triggers.cli.rendering.slash_panel import (
    filter_commands,
    slash_panel_fragments,
    slash_panel_height,
    slash_usage_hint,
    slash_panel_visible,
)


def _registry() -> CommandRegistry:
    registry = CommandRegistry()
    registry.register(
        Command(
            name="loop",
            description="Run a prompt on a recurring interval",
            handler=lambda _s, _a: None,
            aliases=["/l"],
        )
    )
    registry.register(
        Command(
            name="login",
            description="Authenticate with the API",
            handler=lambda _s, _a: None,
        )
    )
    registry.register(
        Command(
            name="model",
            description="Change the active LLM",
            handler=lambda _s, _a: None,
            subcommands={
                "gpt-5.5": Command(
                    name="gpt-5.5",
                    description="Switch to GPT-5.5",
                    handler=lambda _s, _a: None,
                ),
                "claude": Command(
                    name="claude",
                    description="Switch to Claude",
                    handler=lambda _s, _a: None,
                ),
            },
        )
    )
    return registry


def test_visibility_requires_leading_slash() -> None:
    assert slash_panel_visible("/") is True
    assert slash_panel_visible("  /loop") is False
    assert slash_panel_visible("") is False
    assert slash_panel_visible("hello") is False
    assert slash_panel_visible(" hi /loop") is False


def test_filter_shows_all_roots_for_bare_slash() -> None:
    matches = filter_commands("/", _registry())
    names = {match.text for match in matches}
    assert {"/loop", "/login", "/model"}.issubset(names)
    # No aliases shown in the panel
    assert "/l" not in names
    # No subcommands until space is typed
    assert "/model gpt-5.5" not in names


def test_filter_prefix_narrows_root_commands() -> None:
    matches = filter_commands("/lo", _registry())
    names = sorted(match.text for match in matches)
    assert names == ["/login", "/loop"]


def test_filter_switches_to_subcommands_on_space() -> None:
    matches = filter_commands("/model ", _registry())
    names = sorted(match.text for match in matches)
    assert names == ["/model claude", "/model gpt-5.5"]


def test_filter_narrows_subcommands_by_suffix() -> None:
    matches = filter_commands("/model gpt", _registry())
    names = [match.text for match in matches]
    assert names == ["/model gpt-5.5"]


def test_usage_hint_uses_exact_command_usage_suffix() -> None:
    registry = CommandRegistry()
    registry.register(
        Command(
            name="model",
            description="Change the active LLM",
            usage="/model [name] [global|thread]",
            handler=lambda _s, _a: None,
            subcommands={
                "set": Command(
                    name="set",
                    description="Set model",
                    usage="set <model-id>",
                    handler=lambda _s, _a: None,
                )
            },
        )
    )

    assert slash_usage_hint("/help", registry) == " [query]"
    assert slash_usage_hint("/help ", registry) == "[query]"
    assert slash_usage_hint("/model", registry) == " [name] [global|thread]"
    assert slash_usage_hint("/model ", registry) == "[name] [global|thread]"
    assert slash_usage_hint("/model set", registry) == " <model-id>"
    assert slash_usage_hint("/model set ", registry) == "<model-id>"


def test_usage_hint_ignores_non_exact_or_hidden_commands() -> None:
    registry = CommandRegistry()
    registry.register(
        Command(
            name="color",
            description="Set session color",
            usage="/color [red|blue|default]",
            handler=lambda _s, _a: None,
        )
    )
    registry.register(
        Command(
            name="secret",
            description="Hidden command",
            usage="/secret <value>",
            handler=lambda _s, _a: None,
            hidden=True,
        )
    )

    assert slash_usage_hint("/co", registry) == ""
    assert slash_usage_hint("/color red", registry) == ""
    assert slash_usage_hint(" /color", registry) == ""
    assert slash_usage_hint("/secret", registry) == ""
    assert slash_usage_hint("/cls", registry) == ""


def test_height_matches_visible_rows() -> None:
    registry = _registry()
    assert slash_panel_height("", registry) == 0
    assert slash_panel_height("/zzz", registry) == 1  # "no matches" hint
    assert slash_panel_height("/lo", registry) == 2  # /login + /loop
    # The bare "/" height should equal the number of visible root commands
    # (registry includes built-in /help and /quit alongside our test commands)
    assert slash_panel_height("/", registry) == len(filter_commands("/", registry))


def test_height_caps_with_overflow_hint() -> None:
    registry = CommandRegistry()
    for index in range(15):
        registry.register(
            Command(
                name=f"cmd{index:02d}",
                description=f"Command {index}",
                handler=lambda _s, _a: None,
            )
        )
    # Default max_rows=10 visible + 1 overflow hint
    assert slash_panel_height("/", registry) == 11


def test_fragments_render_panel_width() -> None:
    fragments = slash_panel_fragments("/lo", _registry(), width=60)
    assert fragments  # non-empty when visible
    # Each line should pad to panel width (verified by no fragment exceeding it)
    line_widths: list[int] = []
    accumulator = 0
    for _style, text in fragments:
        if "\n" in text:
            line_widths.append(accumulator)
            accumulator = 0
        else:
            accumulator += len(text)
    line_widths.append(accumulator)
    assert all(width <= 60 for width in line_widths)
    assert all(width > 0 for width in line_widths)


def test_fragments_include_command_text() -> None:
    fragments = slash_panel_fragments("/loo", _registry(), width=80)
    rendered = "".join(text for _style, text in fragments)
    assert "/loop" in rendered
    assert "recurring interval" in rendered


def test_fragments_mark_selected_row() -> None:
    fragments = slash_panel_fragments("/lo", _registry(), width=80, selected_index=1)

    selected_names = [
        text
        for style, text in fragments
        if style == "class:slash-panel.selected.name"
    ]

    assert selected_names
    assert selected_names[0].strip() == "/loop"


def test_fragments_scroll_window_to_selected_overflow_row() -> None:
    registry = CommandRegistry()
    for index in range(15):
        registry.register(
            Command(
                name=f"cmd{index:02d}",
                description=f"Command {index}",
                handler=lambda _s, _a: None,
            )
        )

    target_index = next(
        index
        for index, item in enumerate(filter_commands("/", registry))
        if item.text == "/cmd14"
    )
    fragments = slash_panel_fragments(
        "/",
        registry,
        width=80,
        selected_index=target_index,
    )
    rendered = "".join(text for _style, text in fragments)
    selected_names = [
        text
        for style, text in fragments
        if style == "class:slash-panel.selected.name"
    ]

    assert "/cmd14" in rendered
    assert "/cmd00" not in rendered
    assert selected_names[0].strip() == "/cmd14"


def test_fragments_empty_when_panel_hidden() -> None:
    assert slash_panel_fragments("", _registry(), width=80) == []
    assert slash_panel_fragments(" /lo", _registry(), width=80) == []
    assert slash_panel_fragments("hello", _registry(), width=80) == []
