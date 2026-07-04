from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cli_fixtures import FakeTerminalCapabilities, run

from nymeria.triggers.cli.app import _repl_prompt_style
from nymeria.triggers.cli.commands import CommandContext, CommandRegistry, ListCommandOutputSink
from nymeria.triggers.cli.commands import theme as theme_commands
from nymeria.triggers.cli.rendering.rich_repl import render_tool_row
from nymeria.triggers.cli.state import ToolCallStep
from nymeria.triggers.cli.theme import (
    CLITheme,
    DEFAULT_THEME_VALUES,
    THEME_CONFIG_ENV,
    load_cli_theme,
    save_cli_theme,
)


def test_default_theme_uses_soft_repl_palette() -> None:
    assert DEFAULT_THEME_VALUES["heading"] == "#FFFFFF"
    assert DEFAULT_THEME_VALUES["user_header"] == "#F7C8E0"
    assert DEFAULT_THEME_VALUES["assistant_header"] == "#BBDDFB"
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
    assert str(tool_row.style) == "#444444"
