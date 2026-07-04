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
      "tabs": [{"label": str, "fields": [
          {"kind": "search", "key": str, "placeholder": str | None},
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
string through its normal slash-command path. Cancel is a no-op. The
markdown fallback is ALWAYS present on the result, so frontends that do not
render forms (bots, plain terminals, current desktop/mobile) need zero
changes and there is no capability negotiation on the wire.

``data["state"]`` is an optional dict of client-state sync hints (for
example ``{"model": "gpt-5.5"}`` after a model change) that clients apply if
they understand them and ignore otherwise. It replaces the client-local
dispatch side effects that a pure text forwarder would drop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FORM_CONTRACT_VERSION = 1

FORM_FIELD_KINDS = ("search", "radio", "checkbox")


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


def radio_field(key: str, options: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "radio", "key": key, "options": list(options)}


def checkbox_field(key: str, options: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": "checkbox", "key": key, "options": list(options)}


def form_tab(label: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {"label": label, "fields": list(fields)}


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
