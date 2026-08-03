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
      "notes": [str, ...] | absent,
      "tabs": [{"label": str,
                "submit": {"command": str} | absent,
                "active": bool | absent,
                "description": str | absent,
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
markdown fallback is ALWAYS present on the result, and form payloads ship
only to callers that declared ``supports_forms`` on the request (the
dispatcher strips them for everyone else), so form-less frontends need
zero changes.

A ``text`` field is a free-typed value substituted like any other field
(``secret: true`` asks the client to mask the display and keep the value
out of its input history; secrets must NEVER be echoed back into form
payloads, which ship only to callers that declared ``supports_forms``). A
tab holds at most ONE typed input (search or text) because rich clients
feed it from their single composer line, and needs a typed input, an
option list, or an action shape (below) to be renderable.

A tab with NO fields is a described ACTION (#139): its own ``submit``
template carries no placeholders and the client dispatches it as-is on
Enter, rendering ``description`` where a fielded tab shows its input or
list. The empty-selection no-op applies only to templates that HAVE
substituted keys; a placeholder-free template always dispatches.

``notes`` (optional, chained commands) carries the step's DELTA lines; see
:func:`chain_form_output`. Clients that predate it ignore an unknown key.

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
from typing import Any, Literal

FORM_CONTRACT_VERSION = 1

FORM_FIELD_KINDS = ("search", "text", "radio", "checkbox")

# The authored outcome of a command (#132). Defined here (the leaf module)
# so both the dispatcher and the handler mixins import one spelling;
# ``api/schemas/commands.py`` carries the wire twin.
CommandResultLevel = Literal["info", "success", "warning", "error"]


@dataclass(frozen=True)
class CommandOutput:
    """Rich return value for command executor handlers.

    ``text`` is the result BODY (plain text or markdown, no sentinels);
    ``level`` is the authored outcome, which the dispatcher renders into
    the markdown artifacts every surface reads (``**Error:**`` /
    ``**Done.**`` / ``**Warning:**``); ``data`` becomes
    ``CommandResult.data`` verbatim (dropped when the level is ``error``,
    the established failure-drops-data rule). Prefer the
    ``command_info``/``command_success``/``command_warning``/
    ``command_error`` constructors over instantiating this directly.

    Transition note (#132): text carrying a legacy ``[Info]:``/
    ``[Success]:``/``[Error]:`` sentinel still wins over ``level`` at the
    dispatch boundary until the handler sweep retires the last prefixed
    return.
    """

    text: str
    data: dict[str, Any] | None = field(default=None)
    level: CommandResultLevel = field(default="info")


def command_info(
    text: str, *, data: dict[str, Any] | None = None
) -> CommandOutput:
    """A neutral readout (lists, status views): no artifact, no glyph."""
    return CommandOutput(text, data=data, level="info")


def command_success(
    text: str, *, data: dict[str, Any] | None = None
) -> CommandOutput:
    """A completed ACTION confirmation: renders the ``**Done.**`` artifact."""
    return CommandOutput(text, data=data, level="success")


def command_warning(
    text: str, *, data: dict[str, Any] | None = None
) -> CommandOutput:
    """Completed with a caveat worth surfacing: ``**Warning:**`` artifact,
    ``success`` stays True so ``data`` survives."""
    return CommandOutput(text, data=data, level="warning")


def command_error(
    text: str, *, data: dict[str, Any] | None = None
) -> CommandOutput:
    """A failure: ``**Error:**`` artifact, ``success`` False, ``data``
    dropped at the boundary."""
    return CommandOutput(text, data=data, level="error")


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
    description: str = "",
) -> dict[str, Any]:
    """Build a tab dict; ``submit_command`` (optional) overrides the
    form-level submit template while this tab is active, and ``active``
    asks the client to open the form on this tab.

    A tab with NO fields is a described ACTION (#139): it must carry its
    own ``submit_command`` (whose template needs no placeholders; the
    client dispatches it as-is on Enter), and ``description`` is the one
    line the client renders in place of an input or list."""

    tab: dict[str, Any] = {"label": label, "fields": list(fields)}
    if submit_command:
        tab["submit"] = {"command": submit_command}
    if active:
        tab["active"] = True
    if description:
        tab["description"] = description
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
    submits what was typed, a radio tab applies the selection.

    The navigation segment differs by kind too. On a text step the arrows edit
    the value and only cross to a neighbouring step from the ends, so Tab is
    advertised as the reliable step key; on a list step the arrows step
    directly."""

    is_text = any(
        field_def.get("kind") == "text"
        for field_def in active_tab.get("fields") or []
    )
    enter_word = "Enter submit" if is_text else "Enter apply"
    if tab_count <= 1:
        return f"{enter_word} · Esc close"
    nav_word = "←→ move · Tab step" if is_text else "←→ step"
    return f"{nav_word} · {enter_word} · Esc close"


def chain_form_output(
    title: str,
    tabs: list[dict[str, Any]],
    active_tab: dict[str, Any],
    guidance: list[str],
    *,
    fallback_text: str,
    notes: list[str] | None = None,
) -> CommandOutput:
    """Render one chained-command response: tabs, one active, guidance
    markdown (the step-rail idiom the module docstring describes).

    ``notes`` are this step's DELTA lines (what just happened: "Callback
    delivered", "Login failed: ..."), attached as ``form["notes"]``. A
    form-rendering client MAY print only the notes instead of the full
    markdown, so the EMITTER'S DUTY is to attach notes only on a response
    whose ``guidance`` is redundant with what that surface already printed
    (a re-render of a rail shown one step ago). A step whose guidance is
    load-bearing beyond the panel (a review/confirmation table, an auth
    URL the user has not seen, an honesty caveat like a degraded model
    list) must pass everything through ``guidance`` and no notes. When
    notes are attached, the markdown is composed HERE as notes + guidance,
    so the fallback always contains every note by construction: a
    form-less surface can never see less than a form-rendering one.
    """

    active_tab["active"] = True
    submit = active_tab.get("submit") or {}
    form = form_payload(
        title,
        tabs,
        submit_command=str(submit.get("command") or ""),
        footer_hint=chain_footer(active_tab, len(tabs)),
    )
    cleaned_notes = [note for note in (notes or []) if note]
    if cleaned_notes:
        form["notes"] = cleaned_notes
    text = "\n".join(
        line for line in [*cleaned_notes, *guidance] if line
    ) or fallback_text
    return command_info(text, data=command_data(form=form))


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
