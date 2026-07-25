"""CLI fallback-consent form flow (llm-fallback-consent Phase 3).

Covers the under-composer form spec built by ``_RichReplRuntime`` for
``fallback_prompt`` events: the flattened one-radio-list shape (option ids
ARE the ``/fallback approve`` hold argument), the confirm dispatch back
through the slash-command path, and the resolved event closing a stale form.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cli_fixtures import FakeTerminalCapabilities, run

from nymeria.triggers.cli.app import CLIApp, _RichReplRuntime
from nymeria.triggers.cli.events import (
    FallbackPromptEvent,
    FallbackPromptResolvedEvent,
)
from nymeria.triggers.cli.rendering.form_panel import FormResult
from nymeria.triggers.cli.rendering.rich_repl import RichReplRenderer
from nymeria.triggers.cli.theme import THEME_CONFIG_ENV


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


def _prompt_event(**overrides: Any) -> FallbackPromptEvent:
    payload: dict[str, Any] = {
        "record_id": "fb-1",
        "kind": "transport",
        "from_provider": "anthropic",
        "from_model": "claude-fable-5",
        "to_provider": "anthropic",
        "to_model": "claude-opus-4-8",
        "reason": "provider_server_error",
        "http_status": 529,
        "timeout_seconds": 180,
        "hold_options": (600, 3600, 7200, 28800),
        "allow_permanent": True,
        "default_hold_seconds": 7200,
    }
    payload.update(overrides)
    return FallbackPromptEvent(**payload)


def test_fallback_form_spec_flattens_holds_into_one_radio_list(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _make_runtime(tmp_path, monkeypatch)
    spec = runtime._fallback_prompt_form_spec(_prompt_event())

    assert "claude-fable-5 is failing" in spec.title
    assert "HTTP 529" in spec.title
    assert "claude-opus-4-8" in spec.title
    assert len(spec.tabs) == 1
    fields = spec.tabs[0].fields
    assert len(fields) == 1 and fields[0].kind == "radio"

    options = fields[0].options
    # ids are the /fallback approve hold argument (minutes or "permanent").
    assert [option.id for option in options] == [
        "10", "60", "120", "480", "permanent", "deny",
    ]
    current = [option.id for option in options if option.current]
    assert current == ["120"]  # default_hold_seconds=7200
    assert "auto-swap" in (spec.footer_hint or "")


def test_fallback_form_spec_refusal_copy_and_no_permanent(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _make_runtime(tmp_path, monkeypatch)
    spec = runtime._fallback_prompt_form_spec(
        _prompt_event(kind="refusal", allow_permanent=False)
    )

    assert "refused this turn" in spec.title
    options = spec.tabs[0].fields[0].options
    assert [option.id for option in options] == ["10", "60", "120", "480", "deny"]
    deny = options[-1]
    assert "rephrase" in (deny.description or "")


def test_fallback_form_confirm_dispatches_fallback_commands(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _make_runtime(tmp_path, monkeypatch)
    dispatched: list[str] = []

    async def _record_dispatch(command: str, *args: Any, **kwargs: Any) -> None:
        dispatched.append(command)

    monkeypatch.setattr(
        runtime.app, "_dispatch_command_async", _record_dispatch
    )

    spec = runtime._fallback_prompt_form_spec(_prompt_event())
    runtime._pending_fallback_prompt_record = "fb-1"
    assert spec.on_confirm is not None
    run(spec.on_confirm(FormResult(spec_title="t", tab_label="Model swap", radio_value="480")))
    assert dispatched == ["/fallback approve fb-1 480"]
    assert runtime._pending_fallback_prompt_record is None

    run(spec.on_confirm(FormResult(spec_title="t", tab_label="Model swap", radio_value="permanent")))
    run(spec.on_confirm(FormResult(spec_title="t", tab_label="Model swap", radio_value="deny")))
    assert dispatched == [
        "/fallback approve fb-1 480",
        "/fallback approve fb-1 permanent",
        "/fallback deny fb-1",
    ]

    # An empty selection is a quiet no-op.
    run(spec.on_confirm(FormResult(spec_title="t", tab_label="Model swap", radio_value=None)))
    assert len(dispatched) == 3


def test_fallback_resolved_event_closes_matching_stale_form(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _make_runtime(tmp_path, monkeypatch)
    notes: list[str] = []

    async def _record_note(line: str) -> None:
        notes.append(line)

    monkeypatch.setattr(runtime, "_render_form_note", _record_note)

    run(runtime._on_fallback_prompt_event(_prompt_event()))
    assert runtime._pending_fallback_prompt_record == "fb-1"
    assert runtime._active_form is not None
    assert any("Model swap consent" in note for note in notes)

    resolved = FallbackPromptResolvedEvent(
        record_id="fb-1",
        kind="transport",
        outcome="timeout",
    )
    run(runtime._on_fallback_prompt_resolved_event(resolved))
    assert runtime._pending_fallback_prompt_record is None
    assert runtime._active_form is None
    assert any("auto-swapped" in note for note in notes)

    # A resolution for a DIFFERENT record must leave a live pending form
    # untouched (open a fresh prompt first so the assertion has teeth).
    run(runtime._on_fallback_prompt_event(_prompt_event(record_id="fb-2")))
    assert runtime._pending_fallback_prompt_record == "fb-2"
    run(runtime._on_fallback_prompt_resolved_event(
        FallbackPromptResolvedEvent(record_id="fb-other", kind="transport", outcome="approved")
    ))
    assert runtime._pending_fallback_prompt_record == "fb-2"
    assert runtime._active_form is not None


def test_fallback_form_default_hold_preselection_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    """A default hold outside the offered rows preselects the 2h row.

    Covers both the 0 default ("this turn only": not a row, must not be
    upgraded silently NOR land on the first row) and an arbitrary non-preset
    value; desktop-card parity.
    """
    runtime = _make_runtime(tmp_path, monkeypatch)
    for odd_default in (0, 5400):
        spec = runtime._fallback_prompt_form_spec(
            _prompt_event(default_hold_seconds=odd_default)
        )
        current = [o.id for o in spec.tabs[0].fields[0].options if o.current]
        assert current == ["120"], odd_default


def test_fallback_form_row_copy_uses_shared_hold_phrases(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = _make_runtime(tmp_path, monkeypatch)
    spec = runtime._fallback_prompt_form_spec(_prompt_event())
    labels = [o.label for o in spec.tabs[0].fields[0].options]
    assert labels[:4] == [
        "Swap for 10 min",
        "Swap for 1 hour",
        "Swap for 2 hours",
        "Swap for 8 hours",
    ]


def test_form_cancel_clears_pending_consent_markers(
    tmp_path: Path, monkeypatch
) -> None:
    """Esc-dismissing a consent form must clear the pending marker.

    Otherwise the eventual resolved event for the dismissed record closes
    whatever UNRELATED form is open at that moment.
    """
    runtime = _make_runtime(tmp_path, monkeypatch)

    async def _noop_note(line: str) -> None:
        pass

    monkeypatch.setattr(runtime, "_render_form_note", _noop_note)
    run(runtime._on_fallback_prompt_event(_prompt_event()))
    assert runtime._pending_fallback_prompt_record == "fb-1"

    assert runtime.request_form_cancel() is True
    assert runtime._pending_fallback_prompt_record is None
    assert runtime._pending_hook_approval_record is None

    # The dismissed record's later resolution must not close a new form.
    spec = runtime._fallback_prompt_form_spec(_prompt_event(record_id="fb-9"))
    runtime.open_form(spec)
    run(runtime._on_fallback_prompt_resolved_event(
        FallbackPromptResolvedEvent(record_id="fb-1", kind="transport", outcome="timeout")
    ))
    assert runtime._active_form is not None


def test_fallback_resolved_note_copy(tmp_path: Path, monkeypatch) -> None:
    """Approved-without-hold omits the phrase; decline copy is kind-aware."""
    runtime = _make_runtime(tmp_path, monkeypatch)
    notes: list[str] = []

    async def _record_note(line: str) -> None:
        notes.append(line)

    monkeypatch.setattr(runtime, "_render_form_note", _record_note)

    run(runtime._on_fallback_prompt_resolved_event(FallbackPromptResolvedEvent(
        record_id="fb-a", kind="transport", outcome="approved",
    )))
    assert notes[-1] == "Model swap approved; on the fallback."

    run(runtime._on_fallback_prompt_resolved_event(FallbackPromptResolvedEvent(
        record_id="fb-b", kind="transport", outcome="approved",
        hold_permanent=True,
    )))
    assert notes[-1] == "Model swap approved; on the fallback until reverted."

    run(runtime._on_fallback_prompt_resolved_event(FallbackPromptResolvedEvent(
        record_id="fb-c", kind="refusal", outcome="declined",
    )))
    assert notes[-1] == "Model swap declined; the refusal stands."

    run(runtime._on_fallback_prompt_resolved_event(FallbackPromptResolvedEvent(
        record_id="fb-d", kind="transport", outcome="declined",
    )))
    assert notes[-1] == (
        "Model swap declined; the turn fails with the original provider error."
    )
