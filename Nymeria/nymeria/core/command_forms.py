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
only to callers that declared ``supports_forms`` on the request: the
dispatcher strips them for everyone else, first projecting the ACTIVE
tab's choices into the markdown via :func:`form_options_markdown` (#158),
so form-less frontends need zero changes and still see every option.

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


def render_outcome(level: CommandResultLevel, text: str) -> str:
    """THE producer of the markdown outcome artifacts (#132, #144).

    Maps ``error``/``success``/``warning`` to their ``**Error:**`` /
    ``**Done.**`` / ``**Warning:**`` prefixes; ``info`` is a readout and
    returns ``text`` unchanged (no artifact). Every surface reads the
    artifact as the outcome signal, so no other module may spell these
    literals: the artifact ratchet in ``test_command_service.py`` fails the
    build on a new hand-authored copy (this function is its only allowlist
    entry, so keep the literals inside the function body). Dispatch-boundary
    extras (legacy sentinel parse, the info heading heuristic) stay in
    ``command_service._render_result_markdown``, which delegates its
    artifact arms here.
    """
    if level == "error":
        return f"**Error:** {text}"
    if level == "success":
        return f"**Done.** {text}"
    if level == "warning":
        return f"**Warning:** {text}"
    return text


def table_cell(value: Any, *, limit: int | None = None) -> str:
    """THE renderer for one markdown table cell holding free text.

    Every command listing that puts user- or agent-authored text in a table
    needs this, and two of eight tables were doing it by hand: an unescaped
    ``|`` ends the CELL, shifting every column after it so the rest of the row
    reads against the wrong headers, and a newline ends the row. Backticks
    around a cell are NOT protection: GFM splits a row into cells before it
    parses inline code, so ``| `a|b` |`` is still two cells.

    It lives here, in a leaf module every ``command_executor_*`` mixin already
    imports, because the first version lived in ``command_service`` and the
    mixins cannot import that (it imports them). The one table this helper was
    written for and still missed, ``/fallback approvals``, is in a mixin: an
    unreachable helper is one nobody can apply.

    ``limit`` truncates BEFORE escaping, so a cut can never land inside an
    escape sequence; note it therefore bounds the source text, not the
    rendered width. ``None`` and only ``None`` renders empty, so a cell whose
    value is ``0`` or ``False`` still shows its value.

    The tool layer keeps its own copy (``tools/utils._escape_md_table_cell``,
    which has the same escaping rules and its own tests). That is deliberate:
    ``core`` must not import ``tools``, and collapsing them would drag the
    command layer into every tool module.
    """
    text = " ".join(("" if value is None else str(value)).split())
    if limit is not None and len(text) > limit:
        text = f"{text[: max(limit - 3, 0)].rstrip()}..."
    return text.replace("|", "\\|")


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


# -- formless option-list rendering (#158) ---------------------------------

# Mirrors /model list's 25-row cap: past that the list stops being a picker
# and starts being noise, and the honest remainder line keeps the truncation
# visible. Rows are also character-bounded (`_META_CLAMP`), so bot-sized
# output budgets stay safe by construction.
OPTION_ROWS_CAP = 25
_META_CLAMP = 60


def _template_display(template: str) -> str:
    """A submit template rendered for humans: ``{key}`` becomes ``<key>``."""

    return "/" + template.replace("{", "<").replace("}", ">")


def _template_root(template: str) -> str:
    """The literal dispatch root of a submit template (placeholders cut)."""

    root = template.split("{", 1)[0].strip()
    return f"/{root}" if root else ""


def form_options_markdown(form: Any, body: str) -> list[str]:
    """Markdown lines carrying the ACTIVE tab's choices to formless callers.

    The dispatcher appends these once, at the form-strip site, making option
    lists a dispatch-level guarantee instead of a per-handler duty: a
    picker's choices exist only in the form payload, and every surface
    except the rich CLI is formless today. Scope is deliberately the active
    tab only (chained rails re-render with the next tab active, so each
    step's choices arrive when its step does).

    Radio/checkbox options render as ``- id (label) (meta) (selected)`` rows
    (label only when it differs from the id; meta clamped so a prose meta
    cannot balloon a row), capped with an honest remainder. A fieldless
    ACTION tab (#139) renders its description (when present) plus dispatch
    command. Text and search fields render nothing: values must never be
    echoed (secrets) and the handler's own guidance carries the typing
    syntax. The derived ``Choose:``/``Run:`` line is suppressed only when
    ``body`` already shows the dispatch root in ARGUMENT shape (the root
    followed by a ``<`` placeholder, e.g. "set with: /model <name>"); a
    body that merely mentions the root as a verb list ("Manage with:
    /provider [setup|...]") does not count, because it never tells the
    caller how to act on the ids (measured live on bare /provider).
    """

    if not isinstance(form, dict):
        return []
    tabs = [tab for tab in (form.get("tabs") or []) if isinstance(tab, dict)]
    if not tabs:
        return []
    active = next((tab for tab in tabs if tab.get("active")), tabs[0])
    fields = [
        field_def
        for field_def in (active.get("fields") or [])
        if isinstance(field_def, dict)
    ]

    rows: list[str] = []
    for field_def in fields:
        if field_def.get("kind") not in ("radio", "checkbox"):
            continue
        options = [
            option
            for option in (field_def.get("options") or [])
            if isinstance(option, dict) and str(option.get("id") or "")
        ]
        for option in options[:OPTION_ROWS_CAP]:
            option_id = str(option.get("id") or "")
            label = str(option.get("label") or "")
            meta = str(option.get("meta") or "")
            if len(meta) > _META_CLAMP:
                # Provider metas are full sentences (notes_for_user); rows
                # are an index, not a card, and unclamped metas measured a
                # 10x blowup on bare /provider (12k bot budgets).
                meta = meta[: _META_CLAMP - 3].rstrip() + "..."
            parts = [f"- {option_id}"]
            if label and label != option_id:
                parts.append(f"({label})")
            if meta:
                parts.append(f"({meta})")
            if option.get("current"):
                parts.append("(selected)")
            rows.append(" ".join(parts))
        remainder = len(options) - OPTION_ROWS_CAP
        if remainder > 0:
            rows.append(f"- ... and {remainder} more")

    action_line = ""
    if not fields:
        # Fieldless ACTION tab (#139): the description is the renderable
        # body and the placeholder-free submit dispatches as-is.
        action_line = str(active.get("description") or "")

    tab_submit = active.get("submit") if isinstance(active.get("submit"), dict) else {}
    form_submit = form.get("submit") if isinstance(form.get("submit"), dict) else {}
    template = str(
        (tab_submit or {}).get("command") or (form_submit or {}).get("command") or ""
    )
    if fields and not rows:
        # A text/search-only step (or an option field with nothing to list)
        # renders nothing here: the handler's own guidance carries the
        # typing syntax, and a bare dispatch line without a value list
        # would only mislead. A FIELDLESS action tab falls through: its
        # Run line must survive even without a description.
        return []
    dispatch_line = ""
    root = _template_root(template)
    if template and root:
        # Suppressed only when the body shows the root in ARGUMENT shape
        # (root followed by a placeholder), not on a bare mention: bare
        # /provider's "Manage with: /provider [setup|...]" names the verb
        # list, never how to act on the ids below it.
        named_with_args = f"{root} <" in (body or "") or f"{root}<" in (body or "")
        if not named_with_args:
            verb = "Run" if not fields else "Choose"
            dispatch_line = f"{verb}: {_template_display(template)}"
    if not rows and not action_line and not dispatch_line:
        return []

    lines: list[str] = []
    if rows:
        label = str(active.get("label") or "").strip()
        lines.append(f"Options ({label}):" if label else "Options:")
        lines.extend(rows)
    elif action_line:
        lines.append(action_line)
    if dispatch_line:
        lines.append(dispatch_line)
    return lines


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
