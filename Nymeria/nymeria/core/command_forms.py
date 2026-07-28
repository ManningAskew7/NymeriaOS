"""Structured command-result payloads: the declarative form contract.

Slash-command handlers normally return legacy strings that the command
service converts to markdown. This module owns the structured ``data``
channel that rides next to that markdown on ``CommandResult.data`` (and so
on ``POST /commands/execute`` responses): a handler that wants to attach a
declarative form or client-state hints returns a :class:`CommandOutput`
instead of a bare string.

Contract v1 (kept deliberately narrow; richer field kinds can be added
behind the version number later):

``data["form"]``::

    {
      "version": 1,
      "title": str,
      "footer_hint": str | None,
      "tabs": [{"label": str,
                "submit": {"command": str} | absent,
                "active": bool | absent,
                "fields": [
          {"kind": "search", "key": str, "placeholder": str | None},
          {"kind": "text", "key": str, "label": str | None,
           "placeholder": str | None, "secret": bool},
          {"kind": "radio" | "checkbox", "key": str, "options": [
              {"id": str, "label": str, "meta": str, "description": str,
               "current": bool},
          ]},
      ]}],
      "submit": {"command": "template with {key} placeholders"},
    }

Submit semantics are declarative: on confirm the client substitutes each
``{key}`` placeholder with the field's value (radio: the selected option id;
checkbox: the selected ids joined by spaces) and dispatches the resulting
string through its normal slash-command path. Only the ACTIVE tab's field
values are substituted, so tabs whose selections mean different actions
(e.g. /provider's Switch vs Test) carry their own tab-level ``submit``
template, which wins over the form-level one; the form-level template stays
required as the default for tabs without their own (and the action an older
client that predates tab submits will apply). Cancel is a no-op. The
markdown fallback is ALWAYS present on the result, so frontends that do not
render forms (bots, plain terminals, current desktop/mobile) need zero
changes and there is no capability negotiation on the wire.

A ``text`` field is a free-typed value substituted like any other field
(``secret: true`` asks the client to mask the display and keep the value
out of its input history; secrets must NEVER be echoed back into form
payloads, which ship to every frontend). A tab holds at most ONE typed
input (search or text) because rich clients feed it from their single
composer line, and needs a typed input or an option list to be renderable.

``active: true`` on a tab asks the client to OPEN the form on that tab
(first active tab wins; absent means the first tab). Chained multi-step
commands use it as a step rail: each step's response re-sends the whole
form with the reached steps as tabs and the next undecided step active, so
arrowing between tabs is back/forward navigation. Clients that predate the
flag start on the first tab, which stays a valid (if less convenient)
rendering.

``data["state"]`` is an optional dict of client-state sync hints (for
example ``{"model": "gpt-5.5"}`` after a model change) that clients apply if
they understand them and ignore otherwise. It replaces the client-local
dispatch side effects that a pure text forwarder would drop.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any

FORM_CONTRACT_VERSION = 1

FORM_FIELD_KINDS = ("search", "text", "radio", "checkbox")


@dataclass(frozen=True)
class CommandOutput:
    """Rich return value for command executor handlers.

    ``text`` is the legacy dispatcher string (``[Info]:``/``[Success]:``/
    ``[Error]:`` prefixes supported as usual); ``data`` becomes
    ``CommandResult.data`` verbatim.
    """

    text: str
    data: dict[str, Any] | None = field(default=None)


def form_option(
    option_id: str,
    label: str | None = None,
    *,
    meta: str = "",
    description: str = "",
    current: bool = False,
) -> dict[str, Any]:
    return {
        "id": option_id,
        "label": label if label is not None else option_id,
        "meta": meta,
        "description": description,
        "current": bool(current),
    }


def search_field(key: str, *, placeholder: str = "") -> dict[str, Any]:
    return {"kind": "search", "key": key, "placeholder": placeholder}


def text_field(
    key: str,
    *,
    label: str = "",
    placeholder: str = "",
    secret: bool = False,
) -> dict[str, Any]:
    """A free-typed input field; ``secret`` asks the client to mask it."""

    return {
        "kind": "text",
        "key": key,
        "label": label,
        "placeholder": placeholder,
        "secret": bool(secret),
    }


def radio_field(key: str, options: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "radio", "key": key, "options": list(options)}


def checkbox_field(key: str, options: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "checkbox", "key": key, "options": list(options)}


def form_tab(
    label: str,
    fields: list[dict[str, Any]],
    *,
    submit_command: str = "",
    active: bool = False,
) -> dict[str, Any]:
    """Build a tab dict; ``submit_command`` (optional) overrides the
    form-level submit template while this tab is active, and ``active``
    asks the client to open the form on this tab."""

    tab: dict[str, Any] = {"label": label, "fields": list(fields)}
    if submit_command:
        tab["submit"] = {"command": submit_command}
    if active:
        tab["active"] = True
    return tab


def form_payload(
    title: str,
    tabs: list[dict[str, Any]],
    *,
    submit_command: str,
    footer_hint: str = "",
) -> dict[str, Any]:
    """Build a v1 ``data["form"]`` payload.

    ``submit_command`` is the slash-command template (without the leading
    slash) whose ``{key}`` placeholders the client substitutes on confirm.
    """

    return {
        "version": FORM_CONTRACT_VERSION,
        "title": title,
        "footer_hint": footer_hint,
        "tabs": list(tabs),
        "submit": {"command": submit_command},
    }


def command_data(
    *,
    form: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Assemble a ``CommandResult.data`` dict, or None when empty."""

    data: dict[str, Any] = {}
    if form:
        data["form"] = form
    if state:
        data["state"] = state
    return data or None


# -- the chained-command step rail (shared by /provider setup + cliproxy) --


def chain_footer(active_tab: dict[str, Any], tab_count: int) -> str:
    """Word the Enter action for the active tab's field kind: a text tab
    submits what was typed, a radio tab applies the selection."""

    enter_word = (
        "Enter submit"
        if any(
            field_def.get("kind") == "text"
            for field_def in active_tab.get("fields") or []
        )
        else "Enter apply"
    )
    return (
        f"←→ step · {enter_word} · Esc close"
        if tab_count > 1
        else f"{enter_word} · Esc close"
    )


def chain_form_output(
    title: str,
    tabs: list[dict[str, Any]],
    active_tab: dict[str, Any],
    lines: list[str],
    *,
    fallback_text: str,
) -> CommandOutput:
    """Render one chained-command response: tabs, one active, guidance
    markdown (the step-rail idiom the module docstring describes)."""

    active_tab["active"] = True
    submit = active_tab.get("submit") or {}
    form = form_payload(
        title,
        tabs,
        submit_command=str(submit.get("command") or ""),
        footer_hint=chain_footer(active_tab, len(tabs)),
    )
    text = "\n".join(line for line in lines if line) or fallback_text
    return CommandOutput("[Info]: " + text, data=command_data(form=form))


def rest_value(rest: str) -> str:
    """Everything after a step token, whitespace-trimmed, case intact.

    Derived from the handler's ``rest`` (not the shlex ``args``) so secrets
    and URLs survive characters the arg splitter would mangle. One
    exception: the form client shell-quotes a value containing whitespace,
    so a value that arrives as exactly one quoted token is unquoted back.
    """

    parts = rest.split(None, 1)
    value = parts[1].strip() if len(parts) > 1 else ""
    if value[:1] in ("'", '"'):
        try:
            tokens = shlex.split(value)
        except ValueError:
            return value
        if len(tokens) == 1:
            return tokens[0]
    return value
