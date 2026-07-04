"""Configurable status bars (backlog #53, CLI modernization Phase 4).

Covers the layout value object and cli.json persistence
(``statusbar_config.py``), the renderer's layout-driven segment selection
(builtin keys, ``text:`` literals, ``script:`` refs), the ``/statusbar``
command, the ScriptSegmentRunner, and the Rich REPL under-prompt bar wiring.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from cli_fixtures import FakeTerminalCapabilities, run

from nymeria.triggers.cli.app import CLIApp, _RichReplRuntime
from nymeria.triggers.cli.commands import (
    CommandContext,
    CommandRegistry,
    ListCommandOutputSink,
)
from nymeria.triggers.cli.commands import statusbar as statusbar_commands
from nymeria.triggers.cli.rendering.rich_repl import RichReplRenderer
from nymeria.triggers.cli.rendering.status_bar import (
    DEFAULT_SEGMENT_KEYS,
    StatusBarContext,
    StatusBarRenderer,
)
from nymeria.triggers.cli.script_segments import ScriptSegmentRunner
from nymeria.triggers.cli.state import create_initial_state
from nymeria.triggers.cli.statusbar_config import (
    StatusBarConfigError,
    StatusBarLayout,
    load_statusbar_layout,
    normalize_bar_name,
    normalize_segment_ref,
    save_statusbar_layout,
)
from nymeria.triggers.cli.theme import THEME_CONFIG_ENV


# ---------------------------------------------------------------------------
# Layout value object + persistence
# ---------------------------------------------------------------------------


def test_layout_roundtrip_preserves_other_config_keys(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "cli.json"
    monkeypatch.setenv(THEME_CONFIG_ENV, str(config_path))
    config_path.write_text(
        json.dumps({"theme": {"prompt": "#123456"}}),
        encoding="utf-8",
    )

    layout = StatusBarLayout(
        top=("model", "context"),
        under_prompt=("tps", "text:hi"),
    )
    save_statusbar_layout(layout)

    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert data["theme"] == {"prompt": "#123456"}
    assert data["status_bar"] == {
        "top": ["model", "context"],
        "under_prompt": ["tps", "text:hi"],
    }
    assert load_statusbar_layout() == layout


def test_default_layout_removes_config_section(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "cli.json"
    monkeypatch.setenv(THEME_CONFIG_ENV, str(config_path))

    save_statusbar_layout(StatusBarLayout(top=("model",)))
    assert "status_bar" in json.loads(config_path.read_text(encoding="utf-8"))

    save_statusbar_layout(StatusBarLayout())
    assert json.loads(config_path.read_text(encoding="utf-8")) == {}
    assert load_statusbar_layout().is_default


def test_load_drops_malformed_refs_and_sections(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "cli.json"
    monkeypatch.setenv(THEME_CONFIG_ENV, str(config_path))
    config_path.write_text(
        json.dumps(
            {
                "status_bar": {
                    "top": ["model", "no-such-segment", 42, "text:ok"],
                    "under_prompt": "not-a-list",
                }
            }
        ),
        encoding="utf-8",
    )

    layout = load_statusbar_layout()
    assert layout.top == ("model", "text:ok")
    assert layout.under_prompt == ()


def test_ref_and_bar_validation() -> None:
    assert normalize_bar_name("TOP") == "top"
    assert normalize_bar_name("bottom") == "under"
    assert normalize_bar_name("under_prompt") == "under"
    with pytest.raises(StatusBarConfigError):
        normalize_bar_name("sideways")

    assert normalize_segment_ref("Model") == "model"
    assert normalize_segment_ref("text:hello world") == "text:hello world"
    assert normalize_segment_ref("script:~/bin/x.sh") == "script:~/bin/x.sh"
    for bad in ("", "text:", "script:  ", "no-such-segment"):
        with pytest.raises(StatusBarConfigError):
            normalize_segment_ref(bad)


def test_script_commands_deduped_across_bars() -> None:
    layout = StatusBarLayout(
        top=("model", "script:one", "script:two"),
        under_prompt=("script:one", "text:x"),
    )
    assert layout.script_commands() == ("one", "two")
    assert StatusBarLayout().script_commands() == ()


# ---------------------------------------------------------------------------
# Renderer layout-driven segments
# ---------------------------------------------------------------------------


def _render(renderer: StatusBarRenderer, **context_kwargs: Any) -> str:
    state = create_initial_state(thread_id="thread-1", now=0.0)
    return renderer.render_text(
        state,
        capabilities=FakeTerminalCapabilities(width=160),
        context=StatusBarContext(**context_kwargs),
        now=0.0,
    )


def test_explicit_layout_pins_order_and_renders_text_literals() -> None:
    renderer = StatusBarRenderer()
    renderer.set_layout(("model", "text:release-42", "brand"))

    text = _render(renderer, model="claude-test", connection_label="local agent")

    assert text == "claude-test | release-42 | Nymeria"
    # The connection segment is registered but not in the layout.
    assert "local agent" not in text


def test_script_ref_uses_source_and_skips_when_unavailable() -> None:
    renderer = StatusBarRenderer()
    renderer.set_layout(("brand", "script:my-status.sh"))

    # No source installed: the ref renders nothing.
    assert _render(renderer) == "Nymeria"

    renderer.set_script_source(lambda command: f"ran {command}")
    assert _render(renderer) == "Nymeria | ran my-status.sh"

    renderer.set_script_source(lambda command: None)
    assert _render(renderer) == "Nymeria"

    def _boom(_command: str) -> str:
        raise RuntimeError("script source failure")

    renderer.set_script_source(_boom)
    assert _render(renderer) == "Nymeria"


def test_unknown_layout_key_is_skipped_and_none_restores_default() -> None:
    renderer = StatusBarRenderer()
    renderer.set_layout(("brand", "no-such-key"))
    assert _render(renderer) == "Nymeria"

    renderer.set_layout(None)
    assert renderer.layout is None
    text = _render(renderer, connection_label="local agent")
    assert text.startswith("Nymeria | Ready | local agent")


def test_empty_layout_renders_nothing() -> None:
    renderer = StatusBarRenderer()
    renderer.set_layout(())
    assert _render(renderer, model="claude-test") == ""


# ---------------------------------------------------------------------------
# /statusbar command
# ---------------------------------------------------------------------------


def _command_context() -> tuple[CommandContext, ListCommandOutputSink, list[Any]]:
    sink = ListCommandOutputSink()
    actions: list[Any] = []
    return CommandContext(output=sink, dispatch_state=actions.append), sink, actions


def test_statusbar_command_show_set_reset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(THEME_CONFIG_ENV, str(tmp_path / "cli.json"))
    registry = CommandRegistry(include_builtins=False)
    statusbar_commands.register(registry)
    context, sink, actions = _command_context()

    show = run(registry.dispatch_async(context, "/statusbar show"))
    set_under = run(
        registry.dispatch_async(
            context,
            '/statusbar set under tps "text:hello world"',
        )
    )
    assert load_statusbar_layout().under_prompt == ("tps", "text:hello world")
    set_top = run(registry.dispatch_async(context, "/statusbar set top model context"))
    assert load_statusbar_layout().top == ("model", "context")
    reset_under = run(registry.dispatch_async(context, "/statusbar reset under"))
    assert load_statusbar_layout().under_prompt == ()
    reset_all = run(registry.dispatch_async(context, "/statusbar reset"))

    assert show.ok is True
    assert "Built-in segments" in sink.messages[0].content
    assert all(result.ok for result in (set_under, set_top, reset_under, reset_all))
    assert load_statusbar_layout().is_default
    assert [action["type"] for action in actions] == ["statusbar_updated"] * 4
    assert all(
        isinstance(action["layout"], StatusBarLayout) for action in actions
    )


def test_statusbar_command_rejects_invalid_input(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(THEME_CONFIG_ENV, str(tmp_path / "cli.json"))
    registry = CommandRegistry(include_builtins=False)
    statusbar_commands.register(registry)

    bad_bar = run(
        registry.dispatch_async(CommandContext(), "/statusbar set sideways model")
    )
    bad_ref = run(
        registry.dispatch_async(CommandContext(), "/statusbar set top no-such-segment")
    )
    missing_refs = run(
        registry.dispatch_async(CommandContext(), "/statusbar set top")
    )

    assert bad_bar.status == "error"
    assert "Unknown bar" in bad_bar.messages[0].content
    assert bad_ref.status == "error"
    assert "Unknown segment" in bad_ref.messages[0].content
    assert missing_refs.status == "error"


# ---------------------------------------------------------------------------
# ScriptSegmentRunner
# ---------------------------------------------------------------------------


def _snapshot() -> dict[str, Any]:
    return {"model": {"display_name": "claude-test"}}


@pytest.mark.asyncio
async def test_refresh_pipes_snapshot_and_caches_first_line() -> None:
    runner = ScriptSegmentRunner(snapshot_provider=_snapshot)
    command = (
        'python3 -c "import sys,json; d=json.load(sys.stdin); '
        "print('model=' + d['model']['display_name']); print('junk')\""
    )

    changed = await runner._refresh_command(command)

    assert changed is True
    assert runner.lookup(command) == "model=claude-test"
    # Same output again: cached value unchanged.
    assert await runner._refresh_command(command) is False


@pytest.mark.asyncio
async def test_failing_and_empty_scripts_render_nothing() -> None:
    runner = ScriptSegmentRunner(snapshot_provider=_snapshot)

    assert await runner._refresh_command("exit 3") is False
    assert runner.lookup("exit 3") is None
    assert await runner._refresh_command("true") is False  # no output
    assert runner.lookup("true") is None


@pytest.mark.asyncio
async def test_timed_out_script_is_killed_and_raises() -> None:
    runner = ScriptSegmentRunner(
        snapshot_provider=_snapshot,
        timeout_seconds=0.2,
    )
    with pytest.raises(asyncio.TimeoutError):
        await runner._refresh_command("sleep 30")
    assert runner.lookup("sleep 30") is None


@pytest.mark.asyncio
async def test_watcher_lifecycle_and_on_update() -> None:
    updated = asyncio.Event()
    runner = ScriptSegmentRunner(
        snapshot_provider=_snapshot,
        on_update=updated.set,
        interval_seconds=30.0,
    )
    runner.set_commands(("printf 'live'",))
    assert runner._task is not None

    await asyncio.wait_for(updated.wait(), timeout=5.0)
    assert runner.lookup("printf 'live'") == "live"

    runner.set_commands(())
    assert runner.lookup("printf 'live'") is None  # cache pruned
    await runner.stop_async()


# ---------------------------------------------------------------------------
# Rich REPL runtime wiring (under-prompt bar + apply)
# ---------------------------------------------------------------------------


def _make_runtime(tmp_path: Path, monkeypatch) -> _RichReplRuntime:
    monkeypatch.setenv(THEME_CONFIG_ENV, str(tmp_path / "cli.json"))
    capabilities = FakeTerminalCapabilities(width=100)
    cli_app = CLIApp(None, thread_id="thread-1")
    renderer = RichReplRenderer(capabilities=capabilities, width=100)
    return _RichReplRuntime(
        app=cli_app,
        renderer=renderer,
        capabilities=capabilities,
    )


def test_runtime_under_bar_hidden_by_default(tmp_path: Path, monkeypatch) -> None:
    runtime = _make_runtime(tmp_path, monkeypatch)
    assert runtime.statusbar_layout.is_default
    assert runtime.under_status_visible() is False
    assert runtime.under_status_height() == 0
    assert runtime.status_bar_renderer.layout is None


def test_runtime_applies_layout_and_footer_grows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _make_runtime(tmp_path, monkeypatch)
    base_height = runtime.footer_height()

    runtime.apply_statusbar_layout(
        StatusBarLayout(top=("model", "context"), under_prompt=("text:under-bar",))
    )

    assert runtime.under_status_visible() is True
    assert runtime.under_status_height() == 1
    assert runtime.footer_height() == base_height + 1
    assert runtime.status_bar_renderer.layout == ("model", "context")
    assert runtime.under_status_bar_renderer.layout == ("text:under-bar",)
    fragments_text = "".join(text for _style, text in runtime.under_status_fragments())
    assert "under-bar" in fragments_text

    runtime.apply_statusbar_layout(StatusBarLayout())
    assert runtime.footer_height() == base_height
    assert runtime.under_status_visible() is False


def test_runtime_startup_load_applies_persisted_layout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = tmp_path / "cli.json"
    config_path.write_text(
        json.dumps({"status_bar": {"under_prompt": ["text:persisted"]}}),
        encoding="utf-8",
    )
    runtime = _make_runtime(tmp_path, monkeypatch)

    assert runtime.under_status_visible() is True
    assert runtime.under_status_bar_renderer.layout == ("text:persisted",)
    # No script refs: the runner is never created.
    assert runtime._script_runner is None


def test_runtime_script_layout_creates_and_stops_runner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = _make_runtime(tmp_path, monkeypatch)

    runtime.apply_statusbar_layout(
        StatusBarLayout(top=("brand", "script:echo hi"))
    )
    runner = runtime._script_runner
    assert runner is not None
    assert runner.commands == ("echo hi",)

    runtime.apply_statusbar_layout(StatusBarLayout())
    assert runner.commands == ()
    runtime.stop_script_segments()


def test_default_segment_keys_are_the_documented_builtins() -> None:
    # /statusbar's validation and help text both key off this tuple.
    assert DEFAULT_SEGMENT_KEYS == (
        "brand",
        "activity",
        "notice",
        "connection",
        "model",
        "fast",
        "reasoning",
        "thread",
        "context",
        "tps",
        "queued",
        "cwd",
    )
