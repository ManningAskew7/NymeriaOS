"""Generated picker forms from declared params (backlog #110).

Pins the generation rules the dispatcher's missing-required rescue relies
on: sole-required-primary shape, option sourcing (static choices vs the
resolver registry), the skip set (no_echo, exclusions, free text), scope
tab expansion with distinct keys, and hyphen path rendering.
"""

from __future__ import annotations

from typing import Any

import pytest

from cli_fixtures import run
from nymeria.core.command_form_generation import (
    EXCLUDED_COMMANDS,
    KEEP_CURRENT_REFS,
    SEARCH_THRESHOLD,
    generate_param_form,
)
from nymeria.core.command_forms import form_option
from nymeria.core.command_option_resolvers import OPTION_RESOLVERS
from nymeria.core.command_params import CommandParam
from nymeria.core.command_service import CommandService, _CommandExecutor


def _definition(params: tuple[CommandParam, ...], name: str = "zzpick"):
    service = CommandService()
    service.register(
        name, description="synthetic test command", category="Test", params=params
    )
    command_id = name.replace(" ", ".").replace("-", "_")
    return service._commands[command_id]


def _executor(
    thread_id: str | None = "thread-1", is_admin: bool | None = True
) -> _CommandExecutor:
    return _CommandExecutor(
        api=object(),
        thread_id=thread_id,
        user_id="alice",
        actor="user",
        is_admin=is_admin,
        service=CommandService(),
        surface="cli",
    )


# ── option sourcing ──────────────────────────────────────────────────────────


def test_static_choices_generate_a_single_tab_radio() -> None:
    definition = _definition(
        (CommandParam("thing", required=True, choices=("a", "b")),)
    )
    form = run(generate_param_form(definition, _executor()))

    assert form is not None
    assert form["version"] == 1
    assert form["title"] == "/zzpick"
    assert form["submit"] == {"command": "zzpick {thing}"}
    (tab,) = form["tabs"]
    assert tab["label"] == "Zzpick"
    (field,) = tab["fields"]
    assert field["kind"] == "radio"
    assert [option["id"] for option in field["options"]] == ["a", "b"]


def test_large_option_sets_gain_a_filter_line() -> None:
    many = tuple(f"choice{i}" for i in range(SEARCH_THRESHOLD + 1))
    definition = _definition((CommandParam("thing", required=True, choices=many),))
    form = run(generate_param_form(definition, _executor()))

    assert form is not None
    kinds = [field["kind"] for field in form["tabs"][0]["fields"]]
    assert kinds == ["search", "radio"]


def test_choices_ref_options_come_from_the_resolver_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _resolver(executor: Any) -> list[dict[str, Any]]:
        return [form_option("x", meta="live", current=True), form_option("y")]

    monkeypatch.setitem(OPTION_RESOLVERS, "zzref", _resolver)
    definition = _definition(
        (CommandParam("thing", required=True, choices_ref="zzref"),)
    )
    form = run(generate_param_form(definition, _executor()))

    assert form is not None
    options = form["tabs"][0]["fields"][0]["options"]
    assert [option["id"] for option in options] == ["x", "y"]
    # The resolver's current marker is STRIPPED for refs outside
    # KEEP_CURRENT_REFS: the marker parks the cursor and a reflexive Enter
    # applies it, but a shared resolver cannot know which command it feeds
    # (the models ref marks the CHAT model, wrong for /fast set).
    assert options[0]["current"] is False


def test_kept_ref_current_marker_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # threads is in KEEP_CURRENT_REFS by construction: current = the
    # calling thread, and re-switching to it is a harmless no-op.
    assert "threads" in KEEP_CURRENT_REFS

    async def _resolver(executor: Any) -> list[dict[str, Any]]:
        return [form_option("thread-1", current=True), form_option("thread-2")]

    monkeypatch.setitem(OPTION_RESOLVERS, "threads", _resolver)
    definition = _definition(
        (CommandParam("thread", required=True, choices_ref="threads"),)
    )
    form = run(generate_param_form(definition, _executor()))

    assert form is not None
    options = form["tabs"][0]["fields"][0]["options"]
    assert options[0]["current"] is True


def test_empty_resolver_result_generates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _resolver(executor: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setitem(OPTION_RESOLVERS, "zzref", _resolver)
    definition = _definition(
        (CommandParam("thing", required=True, choices_ref="zzref"),)
    )
    assert run(generate_param_form(definition, _executor())) is None


def test_unresolved_ref_generates_nothing() -> None:
    definition = _definition(
        (CommandParam("thing", required=True, choices_ref="zz_no_such_ref"),)
    )
    assert run(generate_param_form(definition, _executor())) is None


def test_free_text_primary_generates_nothing() -> None:
    # Pickers only where picking helps: typing the argument IS the form.
    definition = _definition((CommandParam("title", kind="rest", required=True),))
    assert run(generate_param_form(definition, _executor())) is None


def test_label_alternation_without_choices_stays_free_text() -> None:
    # The #129 trap, pinned by name: a display label advertising an
    # alternation ("on|off|toggle") without enforced choices is a
    # handler-validated free field. Options must never be derived from the
    # label string.
    definition = _definition(
        (CommandParam("state", required=True, label="on|off|toggle"),)
    )
    assert run(generate_param_form(definition, _executor())) is None


# ── the skip set ─────────────────────────────────────────────────────────────


def test_no_echo_param_disables_generation() -> None:
    definition = _definition(
        (
            CommandParam("thing", required=True, choices=("a",)),
            CommandParam("values", kind="option", no_echo=True),
        )
    )
    assert run(generate_param_form(definition, _executor())) is None


def test_excluded_command_generates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # fallback.set is declaration-generatable (required rest, models ref)
    # but a single-select picker would quietly write a one-model chain.
    assert "fallback.set" in EXCLUDED_COMMANDS

    async def _resolver(executor: Any) -> list[dict[str, Any]]:
        return [form_option("some-model")]

    monkeypatch.setitem(OPTION_RESOLVERS, "models", _resolver)
    definition = CommandService()._commands["fallback.set"]
    assert run(generate_param_form(definition, _executor())) is None


def test_two_required_params_generate_nothing() -> None:
    definition = _definition(
        (
            CommandParam("first", required=True, choices=("a",)),
            CommandParam("second", required=True, choices=("b",)),
        )
    )
    assert run(generate_param_form(definition, _executor())) is None


def test_required_option_generates_nothing() -> None:
    definition = _definition(
        (CommandParam("event", kind="option", required=True, choices=("a",)),)
    )
    assert run(generate_param_form(definition, _executor())) is None


# ── scope expansion ──────────────────────────────────────────────────────────


def test_scope_param_expands_to_tabs_with_distinct_keys() -> None:
    definition = _definition(
        (
            CommandParam("value", required=True, choices=("on", "off")),
            CommandParam("scope", kind="scope"),
        )
    )
    form = run(generate_param_form(definition, _executor()))

    assert form is not None
    labels = [tab["label"] for tab in form["tabs"]]
    assert labels == ["This thread", "Global"]
    # Distinct keys per tab (the /think cursor-collision rule) and per-tab
    # submits carrying the matching scope token.
    assert form["tabs"][0]["fields"][0]["key"] == "value_thread"
    assert form["tabs"][1]["fields"][0]["key"] == "value_global"
    assert form["tabs"][0]["submit"] == {"command": "zzpick {value_thread} thread"}
    assert form["tabs"][1]["submit"] == {"command": "zzpick {value_global} global"}
    assert form["submit"] == {"command": "zzpick {value_thread} thread"}


def test_scope_without_thread_offers_global_only() -> None:
    definition = _definition(
        (
            CommandParam("value", required=True, choices=("on", "off")),
            CommandParam("scope", kind="scope"),
        )
    )
    form = run(generate_param_form(definition, _executor(thread_id=None)))

    assert form is not None
    assert [tab["label"] for tab in form["tabs"]] == ["Global"]
    assert form["submit"] == {"command": "zzpick {value_global} global"}


def test_scope_hides_global_from_a_non_admin() -> None:
    # Writable scopes only (the /think picker's rule): the global write
    # gate is per-scope inside the handlers, so a non-admin global tab
    # would submit a command the gate then refuses.
    definition = _definition(
        (
            CommandParam("value", required=True, choices=("on", "off")),
            CommandParam("scope", kind="scope"),
        )
    )
    form = run(generate_param_form(definition, _executor(is_admin=False)))

    assert form is not None
    assert [tab["label"] for tab in form["tabs"]] == ["This thread"]
    assert form["submit"] == {"command": "zzpick {value_thread} thread"}


def test_scope_generates_nothing_for_a_threadless_non_admin() -> None:
    definition = _definition(
        (
            CommandParam("value", required=True, choices=("on", "off")),
            CommandParam("scope", kind="scope"),
        )
    )
    assert (
        run(generate_param_form(definition, _executor(thread_id=None, is_admin=False)))
        is None
    )


# ── path rendering ───────────────────────────────────────────────────────────


def test_hyphenated_paths_render_dispatchable_submits() -> None:
    definition = _definition(
        (CommandParam("thing", required=True, choices=("a",)),),
        name="zzpick deep-verb",
    )
    form = run(generate_param_form(definition, _executor()))

    assert form is not None
    assert form["title"] == "/zzpick deep-verb"
    assert form["submit"] == {"command": "zzpick deep-verb {thing}"}
    assert form["tabs"][0]["label"] == "Deep-verb"
