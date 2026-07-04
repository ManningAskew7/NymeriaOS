"""Tests for the CLI side of the declarative form contract (v1)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional

from cli_fixtures import run
from nymeria.triggers.cli.commands import CommandContext, ListCommandOutputSink
from nymeria.triggers.cli.commands.backend import _execute_backend_command
from nymeria.triggers.cli.commands.form_contract import (
    apply_state_hints,
    form_spec_from_payload,
    substitute_template,
)
from nymeria.triggers.cli.rendering.form_panel import FormResult, FormSpec


def _form_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": 1,
        "title": "Select model",
        "footer_hint": "Enter apply",
        "tabs": [
            {
                "label": "Models",
                "fields": [
                    {"kind": "search", "key": "filter", "placeholder": "Filter…"},
                    {
                        "kind": "radio",
                        "key": "model",
                        "options": [
                            {"id": "gpt-test", "label": "gpt-test", "meta": "128K ctx", "current": True},
                            {"id": "gpt-next", "label": "gpt-next", "meta": "400K ctx", "current": False},
                        ],
                    },
                ],
            }
        ],
        "submit": {"command": "model {model} thread"},
    }
    payload.update(overrides)
    return payload


class _RecordingClient:
    def __init__(self, response: Optional[dict[str, Any]] = None) -> None:
        self.calls: list[str] = []
        self.response = response or {
            "success": True,
            "markdown": "done",
            "command": "model",
            "level": "success",
        }

    async def execute_command(self, command: str, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append(command)
        return self.response


def _context(
    client: Any,
    *,
    rich: bool = True,
) -> tuple[CommandContext, list[Any]]:
    dispatched: list[Any] = []

    def dispatch_state(action: Any) -> None:
        dispatched.append(action)

    context = CommandContext(
        client=client,
        output=ListCommandOutputSink(),
        dispatch_state=dispatch_state,
        thread_id="cli-thread",
        user_id="alice",
        metadata={"capabilities": SimpleNamespace(renderer="rich" if rich else "plain")},
    )
    return context, dispatched


def test_form_spec_from_payload_adapts_v1_payload() -> None:
    context, _dispatched = _context(_RecordingClient())
    spec = form_spec_from_payload(_form_payload(), context=context)

    assert isinstance(spec, FormSpec)
    assert spec.title == "Select model"
    assert spec.footer_hint == "Enter apply"
    (tab,) = spec.tabs
    assert tab.label == "Models"
    assert [field.kind for field in tab.fields] == ["search", "radio"]
    options = tab.fields[1].options
    assert [option.id for option in options] == ["gpt-test", "gpt-next"]
    assert options[0].current is True
    assert options[0].meta == "128K ctx"


def test_form_spec_rejects_malformed_payloads() -> None:
    context, _dispatched = _context(_RecordingClient())

    assert form_spec_from_payload(None, context=context) is None
    assert form_spec_from_payload({"version": 2}, context=context) is None
    assert form_spec_from_payload(_form_payload(submit={}), context=context) is None
    assert form_spec_from_payload(_form_payload(tabs=[]), context=context) is None
    # A tab without a radio/checkbox list cannot submit anything: dropped.
    search_only = _form_payload()
    search_only["tabs"][0]["fields"] = [{"kind": "search", "key": "filter"}]
    assert form_spec_from_payload(search_only, context=context) is None
    # Unknown field kinds are skipped, the rest of the tab survives.
    with_unknown = _form_payload()
    with_unknown["tabs"][0]["fields"].insert(0, {"kind": "textarea", "key": "notes"})
    spec = form_spec_from_payload(with_unknown, context=context)
    assert spec is not None
    assert [field.kind for field in spec.tabs[0].fields] == ["search", "radio"]


def test_confirm_substitutes_selection_and_dispatches_backend_command() -> None:
    client = _RecordingClient()
    context, _dispatched = _context(client)
    spec = form_spec_from_payload(_form_payload(), context=context)
    assert spec is not None

    result = run(
        spec.on_confirm(
            FormResult(
                spec_title="Select model",
                tab_label="Models",
                radio_value="gpt-next",
                filter_text="gpt",
            )
        )
    )

    assert client.calls == ["/model gpt-next thread"]
    assert result.ok is True


def test_confirm_with_empty_selection_is_a_quiet_noop() -> None:
    client = _RecordingClient()
    context, _dispatched = _context(client)
    spec = form_spec_from_payload(_form_payload(), context=context)
    assert spec is not None

    result = run(
        spec.on_confirm(
            FormResult(spec_title="Select model", tab_label="Models", radio_value=None)
        )
    )

    assert client.calls == []
    assert result.ok is True


def test_substitute_template_joins_checkbox_values() -> None:
    assert (
        substitute_template("tools enable {tools}", {"tools": "alpha beta"})
        == "tools enable alpha beta"
    )
    assert substitute_template("model {model}", {"other": "x"}) == "model {model}"


def test_apply_state_hints_dispatches_model_sync() -> None:
    context, dispatched = _context(_RecordingClient())

    run(apply_state_hints({"model": "gpt-next"}, context))
    run(apply_state_hints({"unknown": "ignored"}, context))
    run(apply_state_hints("junk", context))

    assert dispatched == [{"type": "set_model", "model": "gpt-next"}]


def test_apply_state_hints_dispatches_thread_switch() -> None:
    context, dispatched = _context(_RecordingClient())

    run(
        apply_state_hints(
            {"switch_thread": {"thread_id": "thread-2", "thread_label": "Next"}},
            context,
        )
    )
    # A blank thread_id is ignored, not dispatched as an empty switch.
    run(apply_state_hints({"switch_thread": {"thread_id": ""}}, context))

    assert dispatched == [
        {"type": "switch_thread", "thread_id": "thread-2", "thread_label": "Next"}
    ]


def test_apply_state_hints_dispatches_thread_switch_without_label() -> None:
    context, dispatched = _context(_RecordingClient())

    run(apply_state_hints({"switch_thread": {"thread_id": "thread-9"}}, context))

    assert dispatched == [{"type": "switch_thread", "thread_id": "thread-9"}]


def test_apply_state_hints_dispatches_label_and_refresh_actions() -> None:
    context, dispatched = _context(_RecordingClient())

    run(apply_state_hints({"thread_label": "Renamed"}, context))
    run(apply_state_hints({"thread_metadata_updated": True}, context))
    run(apply_state_hints({"thread_context_updated": True}, context))
    # Falsy refresh flags dispatch nothing.
    run(apply_state_hints({"thread_metadata_updated": False}, context))

    assert dispatched == [
        {"type": "set_thread_label", "thread_label": "Renamed"},
        {"type": "thread_metadata_updated"},
        {"type": "thread_context_updated"},
    ]


def test_execute_backend_command_opens_declared_form() -> None:
    client = _RecordingClient(
        response={
            "success": True,
            "markdown": "fallback text",
            "command": "model",
            "level": "info",
            "data": {"form": _form_payload()},
        }
    )
    context, dispatched = _context(client)

    result = run(_execute_backend_command(context, ("model",), []))

    assert result.ok is True
    assert result.payload.get("suppress_transcript") is True
    assert not result.messages
    (action,) = dispatched
    assert action["type"] == "open_form"
    assert isinstance(action["spec"], FormSpec)


def test_execute_backend_command_falls_back_to_markdown_without_form_support() -> None:
    client = _RecordingClient(
        response={
            "success": True,
            "markdown": "fallback text",
            "command": "model",
            "level": "info",
            "data": {"form": _form_payload()},
        }
    )
    context, dispatched = _context(client, rich=False)

    result = run(_execute_backend_command(context, ("model",), []))

    assert result.ok is True
    assert dispatched == []
    assert [message.content for message in result.messages] == ["fallback text"]


def test_execute_backend_command_applies_state_hints() -> None:
    client = _RecordingClient(
        response={
            "success": True,
            "markdown": "Model for this thread set to gpt-next.",
            "command": "model",
            "level": "success",
            "data": {"state": {"model": "gpt-next"}},
        }
    )
    context, dispatched = _context(client)

    result = run(_execute_backend_command(context, ("model",), ["gpt-next", "thread"]))

    assert result.ok is True
    assert dispatched == [{"type": "set_model", "model": "gpt-next"}]
    assert [message.content for message in result.messages] == [
        "Model for this thread set to gpt-next."
    ]


def test_substitute_template_is_single_pass_and_quotes_whitespace() -> None:
    """A value containing another field's placeholder is not re-expanded,
    and multi-word values arrive shell-quoted so the backend's shlex split
    keeps them as one argument."""

    # Single pass: the {b} inside a's value is left alone.
    assert (
        substitute_template("cmd {a} {b}", {"a": "{b}", "b": "x"}) == "cmd {b} x"
    )

    client = _RecordingClient()
    context, _dispatched = _context(client)
    payload = _form_payload()
    payload["tabs"][0]["fields"][1]["options"] = [
        {"id": "my local model", "label": "my local model"},
    ]
    spec = form_spec_from_payload(payload, context=context)
    assert spec is not None

    run(
        spec.on_confirm(
            FormResult(
                spec_title="Select model",
                tab_label="Models",
                radio_value="my local model",
            )
        )
    )

    assert client.calls == ["/model 'my local model' thread"]
