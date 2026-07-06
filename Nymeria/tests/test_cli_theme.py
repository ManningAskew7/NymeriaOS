from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cli_fixtures import FakeTerminalCapabilities, run

from nymeria.triggers.cli.app import _repl_prompt_style
from nymeria.triggers.cli.commands import CommandContext, CommandRegistry, ListCommandOutputSink
from nymeria.triggers.cli.commands import theme as theme_commands
from nymeria.triggers.cli.commands import toolicon as toolicon_commands
from nymeria.triggers.cli.rendering.rich_repl import render_tool_row
from nymeria.triggers.cli.state import ToolCallStep
from nymeria.triggers.cli.theme import (
    CLITheme,
    DEFAULT_THEME_VALUES,
    DEFAULT_TOOL_ICON,
    THEME_CONFIG_ENV,
    ThemeConfigError,
    load_cli_theme,
    load_tool_icon,
    normalize_tool_icon,
    save_cli_theme,
    save_tool_icon,
)


def test_default_theme_uses_soft_repl_palette() -> None:
    assert DEFAULT_THEME_VALUES["heading"] == "#FFFFFF"
    assert DEFAULT_THEME_VALUES["assistant_header"] == "#BBDDFB"
    assert "user_header" not in DEFAULT_THEME_VALUES  # retired with the "You" rule
    assert DEFAULT_THEME_VALUES["artifact"] == "#9CCFFB"
    assert DEFAULT_THEME_VALUES["error"] == "#FCA5A5"
    assert DEFAULT_THEME_VALUES["tool"] != "#FBBF24"


def test_theme_load_save_persists_only_changed_slots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "cli.json"
    monkeypatch.setenv(THEME_CONFIG_ENV, str(config_path))

    theme = load_cli_theme().with_override("prompt", "#123456")
    save_cli_theme(theme)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data == {"theme": {"prompt": "#123456"}}
    assert load_cli_theme().color("prompt") == "#123456"

    reset = load_cli_theme().with_override("prompt", DEFAULT_THEME_VALUES["prompt"])
    save_cli_theme(reset)

    assert json.loads(config_path.read_text(encoding="utf-8")) == {}


def test_theme_command_show_set_reset_and_preset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(THEME_CONFIG_ENV, str(tmp_path / "cli.json"))
    registry = CommandRegistry(include_builtins=False)
    theme_commands.register(registry)
    sink = ListCommandOutputSink()
    actions: list[Any] = []
    context = CommandContext(output=sink, dispatch_state=actions.append)

    show = run(registry.dispatch_async(context, "/theme show"))
    set_result = run(registry.dispatch_async(context, "/theme set prompt #123456"))
    assert load_cli_theme().color("prompt") == "#123456"
    reset_result = run(registry.dispatch_async(context, "/theme reset prompt"))
    run(registry.dispatch_async(context, "/theme set tool #654321"))
    preset_result = run(registry.dispatch_async(context, "/theme preset default"))

    assert show.ok is True
    assert "status_fg" in sink.messages[0].content
    assert set_result.ok is True
    assert load_cli_theme().color("prompt") == DEFAULT_THEME_VALUES["prompt"]
    assert reset_result.ok is True
    assert preset_result.ok is True
    assert json.loads((tmp_path / "cli.json").read_text(encoding="utf-8")) == {}
    assert [action["type"] for action in actions] == [
        "theme_updated",
        "theme_updated",
        "theme_updated",
        "theme_updated",
    ]


def test_theme_command_rejects_invalid_slot_and_hex(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(THEME_CONFIG_ENV, str(tmp_path / "cli.json"))
    registry = CommandRegistry(include_builtins=False)
    theme_commands.register(registry)

    invalid_slot = run(
        registry.dispatch_async(CommandContext(), "/theme set missing #123456")
    )
    invalid_hex = run(
        registry.dispatch_async(CommandContext(), "/theme set prompt 123456")
    )

    assert invalid_slot.status == "error"
    assert "Unknown theme slot" in invalid_slot.messages[0].content
    assert invalid_hex.status == "error"
    assert "Use #RRGGBB" in invalid_hex.messages[0].content


def test_theme_flows_into_prompt_toolkit_and_rich_styles() -> None:
    theme = (
        CLITheme()
        .with_override("status_fg", "#111111")
        .with_override("prompt_busy", "#333333")
        .with_override("tool", "#444444")
    )

    style = _repl_prompt_style(FakeTerminalCapabilities(), theme=theme)
    status_attrs = style.get_attrs_for_style_str("class:status")
    repl_attrs = style.get_attrs_for_style_str("class:composer.busy")
    tool_row = render_tool_row(
        ToolCallStep(id="call-1", name="search_memory", status="success"),
        theme=theme,
    )

    assert status_attrs.color == "111111"
    assert repl_attrs.color == "333333"
    span_styles = [str(span.style) for span in tool_row.spans]
    assert any("#444444" in span_style for span_style in span_styles)  # tool name
    assert any("#FFFFFF" in span_style for span_style in span_styles)  # tool icon


def test_tool_icon_load_save_and_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "cli.json"
    monkeypatch.setenv(THEME_CONFIG_ENV, str(config_path))

    # Missing config falls back to the default.
    assert load_tool_icon() == DEFAULT_TOOL_ICON

    save_tool_icon("❈")
    assert json.loads(config_path.read_text(encoding="utf-8")) == {"tool_icon": "❈"}
    assert load_tool_icon() == "❈"

    # Theme overrides and the icon share one config file without clobbering.
    save_cli_theme(load_cli_theme().with_override("prompt", "#123456"))
    assert load_tool_icon() == "❈"
    assert load_cli_theme().color("prompt") == "#123456"

    # Saving None (or the default) clears the key.
    save_tool_icon(None)
    assert "tool_icon" not in json.loads(config_path.read_text(encoding="utf-8"))
    assert load_tool_icon() == DEFAULT_TOOL_ICON

    # Invalid stored values fall back instead of breaking startup.
    config_path.write_text(json.dumps({"tool_icon": "wide"}), encoding="utf-8")
    assert load_tool_icon() == DEFAULT_TOOL_ICON

    for invalid in ("", "ab", "❖❖", "宽"):
        with pytest.raises(ThemeConfigError):
            normalize_tool_icon(invalid)


def test_toolicon_command_show_set_bare_glyph_and_reset(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(THEME_CONFIG_ENV, str(tmp_path / "cli.json"))
    registry = CommandRegistry(include_builtins=False)
    toolicon_commands.register(registry)
    sink = ListCommandOutputSink()
    actions: list[Any] = []
    context = CommandContext(output=sink, dispatch_state=actions.append)

    show = run(registry.dispatch_async(context, "/toolicon"))
    bare_set = run(registry.dispatch_async(context, "/toolicon ❈"))
    assert load_tool_icon() == "❈"
    explicit_set = run(registry.dispatch_async(context, "/toolicon set ✦"))
    assert load_tool_icon() == "✦"
    reset = run(registry.dispatch_async(context, "/toolicon reset"))
    invalid = run(registry.dispatch_async(context, "/toolicon set wide"))

    assert show.ok is True
    assert DEFAULT_TOOL_ICON in sink.messages[0].content
    assert bare_set.ok is True
    assert explicit_set.ok is True
    assert reset.ok is True
    assert load_tool_icon() == DEFAULT_TOOL_ICON
    assert invalid.status == "error"
    assert [action["type"] for action in actions] == [
        "tool_icon_updated",
        "tool_icon_updated",
        "tool_icon_updated",
    ]
    assert actions[-1]["icon"] == DEFAULT_TOOL_ICON
