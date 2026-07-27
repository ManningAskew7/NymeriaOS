"""Client side of the declarative form contract (v1).

Backend slash commands can attach a form payload to their result
(``CommandResult.data["form"]``, built by ``core/command_forms.py``; the
schema is documented there). This module adapts that JSON payload to the
Rich REPL's :class:`FormSpec` and supplies the one shared confirm handler:
substitute the active tab's field values into the submit template (the
tab-level template when the tab declares one, else the form-level default)
and dispatch the resulting slash command through the normal backend path.

It also applies ``data["state"]`` sync hints (client-state side effects a
pure text forwarder would otherwise drop, e.g. the status-bar model label).

Adaptation is defensive: unknown versions, unsupported field kinds, or
malformed payloads yield ``None`` and the caller falls back to the markdown
the backend always includes.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from typing import Any

from . import CommandContext, CommandResult
from ..rendering.form_panel import FormField, FormOption, FormResult, FormSpec, FormTab

FORM_CONTRACT_VERSION = 1

_LIST_KINDS = ("radio", "checkbox")


def form_spec_from_payload(
    payload: Any,
    *,
    context: CommandContext,
) -> FormSpec | None:
    """Adapt a v1 ``data["form"]`` payload to a FormSpec, or None."""

    if not isinstance(payload, Mapping):
        return None
    if payload.get("version") != FORM_CONTRACT_VERSION:
        return None
    title = str(payload.get("title") or "").strip()
    submit = payload.get("submit")
    template = ""
    if isinstance(submit, Mapping):
        template = str(submit.get("command") or "").strip()
    if not title or not template:
        return None

    tabs: list[FormTab] = []
    templates: dict[str, str] = {}
    for raw_tab in _sequence(payload.get("tabs")):
        tab = _tab_from_payload(raw_tab)
        if tab is not None:
            tabs.append(tab)
            # setdefault: duplicate labels resolve FIRST-wins, matching
            # _active_tab's first-match lookup (values and template must
            # come from the same tab).
            templates.setdefault(tab.label, _tab_submit_template(raw_tab) or template)
    if not tabs:
        return None

    tab_tuple = tuple(tabs)
    return FormSpec(
        title=title,
        tabs=tab_tuple,
        on_confirm=lambda result: _confirm(context, templates, tab_tuple, result),
        footer_hint=str(payload.get("footer_hint") or ""),
    )


async def apply_state_hints(state: Any, context: CommandContext) -> None:
    """Apply known ``data["state"]`` sync hints; unknown keys are ignored.

    Each hint reconstructs the exact CLI dispatch action the retired local
    handler fired, so the forwarder path is lossless: ``model`` -> ``set_model``,
    ``reasoning`` -> ``set_reasoning``, ``switch_thread`` -> ``switch_thread``,
    ``thread_label`` -> ``set_thread_label``, and the two header-refresh flags
    map to the existing ``thread_metadata_updated`` / ``thread_context_updated``
    actions.
    """

    if not isinstance(state, Mapping):
        return
    model = state.get("model")
    if isinstance(model, str) and model.strip():
        await context.dispatch({"type": "set_model", "model": model.strip()})

    reasoning = state.get("reasoning")
    if isinstance(reasoning, Mapping):
        await context.dispatch({
            "type": "set_reasoning",
            "enabled": bool(reasoning.get("enabled")),
            "effort": str(reasoning.get("effort") or ""),
        })

    switch = state.get("switch_thread")
    if isinstance(switch, Mapping):
        thread_id = str(switch.get("thread_id") or "").strip()
        if thread_id:
            action: dict[str, Any] = {"type": "switch_thread", "thread_id": thread_id}
            label = switch.get("thread_label")
            if isinstance(label, str) and label.strip():
                action["thread_label"] = label.strip()
            await context.dispatch(action)

    label = state.get("thread_label")
    if isinstance(label, str) and label.strip():
        await context.dispatch({"type": "set_thread_label", "thread_label": label.strip()})

    if state.get("thread_metadata_updated"):
        await context.dispatch({"type": "thread_metadata_updated"})
    if state.get("thread_context_updated"):
        await context.dispatch({"type": "thread_context_updated"})


def substitute_template(
    template: str,
    values: Mapping[str, str],
) -> str:
    """Replace ``{key}`` placeholders in one pass; only known keys are touched.

    Single-pass regex substitution, so a substituted value that itself
    contains another field's ``{key}`` text is never re-expanded.
    """

    if not values:
        return template
    pattern = re.compile(
        "|".join(re.escape("{" + key + "}") for key in values)
    )
    return pattern.sub(lambda match: values[match.group(0)[1:-1]], template)


async def _confirm(
    context: CommandContext,
    templates: Mapping[str, str],
    tabs: tuple[FormTab, ...],
    result: FormResult,
) -> CommandResult:
    active = _active_tab(tabs, result)
    template = templates.get(active.label, "") if active is not None else ""
    if not template:
        return CommandResult.completed()
    values = _result_values(active, result)
    list_values = [
        value for key, value in values.items() if "{" + key + "}" in template
    ]
    if not any(value.strip() for value in list_values):
        # Nothing selected for any substituted field: dismiss quietly, the
        # same no-op the local /model picker used for an empty selection.
        return CommandResult.completed()

    command = substitute_template(template, values).strip()
    tokens = command.split()
    if not tokens:
        return CommandResult.completed()

    from .backend import _execute_backend_command

    return await _execute_backend_command(context, (tokens[0],), tokens[1:])


def _active_tab(
    tabs: tuple[FormTab, ...],
    result: FormResult,
) -> FormTab | None:
    active = next((tab for tab in tabs if tab.label == result.tab_label), None)
    if active is None and tabs:
        active = tabs[0]
    return active


def _result_values(
    active: FormTab | None,
    result: FormResult,
) -> dict[str, str]:
    """Map field keys of the submitted tab to their selected values.

    Values containing whitespace are shell-quoted so the backend's posix
    shlex tokenizer keeps them as one argument after the forwarder rejoins
    the command string (checkbox ids are quoted individually, staying
    separate arguments).
    """

    values: dict[str, str] = {}
    for field in active.fields if active is not None else ():
        if field.kind == "radio":
            values[field.key] = _quote_value(result.radio_value or "")
        elif field.kind == "checkbox":
            values[field.key] = " ".join(
                _quote_value(value) for value in result.checkbox_values
            )
        elif field.kind == "search":
            values[field.key] = _quote_value(result.filter_text)
    return values


def _quote_value(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return ""
    if any(ch.isspace() for ch in stripped):
        return shlex.quote(stripped)
    return stripped


def _tab_submit_template(raw_tab: Any) -> str:
    """The tab-level submit template, or "" when the tab declares none."""

    if not isinstance(raw_tab, Mapping):
        return ""
    submit = raw_tab.get("submit")
    if not isinstance(submit, Mapping):
        return ""
    return str(submit.get("command") or "").strip()


def _tab_from_payload(raw_tab: Any) -> FormTab | None:
    if not isinstance(raw_tab, Mapping):
        return None
    label = str(raw_tab.get("label") or "").strip()
    if not label:
        return None
    fields: list[FormField] = []
    for raw_field in _sequence(raw_tab.get("fields")):
        field = _field_from_payload(raw_field)
        if field is not None:
            fields.append(field)
    if not any(field.kind in _LIST_KINDS for field in fields):
        return None
    return FormTab(label=label, fields=tuple(fields))


def _field_from_payload(raw_field: Any) -> FormField | None:
    if not isinstance(raw_field, Mapping):
        return None
    kind = str(raw_field.get("kind") or "")
    key = str(raw_field.get("key") or "").strip()
    if not key:
        return None
    if kind == "search":
        return FormField(
            kind="search",
            key=key,
            placeholder=str(raw_field.get("placeholder") or ""),
        )
    if kind == "radio" or kind == "checkbox":
        options = [
            option
            for option in (
                _option_from_payload(raw) for raw in _sequence(raw_field.get("options"))
            )
            if option is not None
        ]
        if not options:
            return None
        return FormField(kind=kind, key=key, options=tuple(options))
    return None


def _option_from_payload(raw_option: Any) -> FormOption | None:
    if not isinstance(raw_option, Mapping):
        return None
    option_id = str(raw_option.get("id") or "").strip()
    if not option_id:
        return None
    return FormOption(
        id=option_id,
        label=str(raw_option.get("label") or option_id),
        meta=str(raw_option.get("meta") or ""),
        description=str(raw_option.get("description") or ""),
        current=bool(raw_option.get("current")),
    )


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return value
    return ()
