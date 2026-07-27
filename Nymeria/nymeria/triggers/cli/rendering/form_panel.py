"""Interactive config-form panel for the Rich REPL.

Renders an arrow-key-navigable form between the status bar and the input frame:
an optional tab bar (left/right), a type-to-filter search line, a single-select
(radio) or multi-select (checkbox) option list (up/down to move, Space to
toggle), and a footer hint bar. Modeled on the slash-command panel
(``slash_panel.py``): the data model and rendering live here as pure
functions/state, the prompt_toolkit wiring lives in ``app.py``.

A command opens a form by dispatching ``{"type": "open_form", "spec": FormSpec}``
and returns immediately; the Rich REPL runtime owns the live ``FormState`` and
drives it through these helpers until the user confirms (Enter) or cancels
(Esc). Confirm/cancel side effects live on ``FormSpec.on_confirm`` so the
renderer stays generic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from rich.cells import cell_len

from .markdown import truncate_cell_width

if TYPE_CHECKING:
    from prompt_toolkit.formatted_text import StyleAndTextTuples

    from ..commands.base import CommandResult


FORM_PANEL_MAX_ROWS = 10
FORM_PANEL_NAME_MIN_WIDTH = 14
FORM_PANEL_NAME_MAX_WIDTH = 48
FORM_PANEL_NAME_GUTTER = 2

FieldKind = Literal["search", "text", "radio", "checkbox"]

_DEFAULT_FOOTER = "↑↓ move · Space toggle · Enter set · Esc close"
_RADIO_FOOTER = "↑↓ move · Enter set · Esc close"
_TEXT_FOOTER = "Enter submit · Esc close"
_SECRET_MASK = "•"


@dataclass(frozen=True, slots=True)
class FormOption:
    """A single selectable row in a radio/checkbox list."""

    id: str
    label: str
    meta: str = ""
    description: str = ""
    current: bool = False


@dataclass(frozen=True, slots=True)
class FormField:
    """A field within a tab: a typed input (search/text) or an option list.

    ``secret`` (text fields) masks the rendered value and asks the composer
    to mask its own display while the field is active.
    """

    kind: FieldKind
    key: str
    options: tuple[FormOption, ...] = ()
    placeholder: str = ""
    label: str = ""
    secret: bool = False


@dataclass(frozen=True, slots=True)
class FormTab:
    """A named sub-view of a form (shown as a tab when more than one).

    Invariant: a tab holds at most one search field and at most one option
    (radio/checkbox) list. The renderer only surfaces the first of each kind
    (``search_field`` / ``list_field``); extra list/search fields are ignored.
    """

    label: str
    fields: tuple[FormField, ...]
    # Open the form on this tab (first active tab wins; else tab 0). Chained
    # commands use it as a step rail: the response re-sends all reached
    # steps as tabs with the next undecided one active.
    active: bool = False

    # Invariant addition (text fields): a tab holds at most one TYPED input
    # (search or text), because the composer line feeds exactly one value;
    # ``input_field`` surfaces the first of either kind.


@dataclass(frozen=True, slots=True)
class FormResult:
    """Snapshot of a form's values, passed to ``on_confirm``."""

    spec_title: str
    tab_label: str
    radio_value: str | None = None
    checkbox_values: tuple[str, ...] = ()
    filter_text: str = ""


# A confirm callback receives the FormResult and returns a CommandResult to
# render above the prompt. on_change (optional) fires on every cursor move for
# live-preview forms (e.g. theme) and returns nothing.
ConfirmHandler = Callable[[FormResult], "Awaitable[CommandResult]"]
ChangeHandler = Callable[[FormResult], "Awaitable[None] | None"]


@dataclass(frozen=True, slots=True)
class FormSpec:
    """A complete form definition built by a command handler."""

    title: str
    tabs: tuple[FormTab, ...]
    on_confirm: ConfirmHandler
    on_change: ChangeHandler | None = None
    footer_hint: str = ""


@dataclass(slots=True)
class FormState:
    """Mutable per-session form state owned by the renderer."""

    active_tab: int = 0
    selected_index: dict[str, int] = field(default_factory=dict)
    toggled: dict[str, set[str]] = field(default_factory=dict)
    filter_text: str = ""


# --------------------------------------------------------------------------- #
# Accessors
# --------------------------------------------------------------------------- #


def active_tab(spec: FormSpec, state: FormState) -> FormTab | None:
    if not spec.tabs:
        return None
    index = max(0, min(state.active_tab, len(spec.tabs) - 1))
    return spec.tabs[index]


def list_field(tab: FormTab | None) -> FormField | None:
    """Return the radio/checkbox field of a tab (the navigable list)."""

    if tab is None:
        return None
    for candidate in tab.fields:
        if candidate.kind in ("radio", "checkbox"):
            return candidate
    return None


def search_field(tab: FormTab | None) -> FormField | None:
    if tab is None:
        return None
    for candidate in tab.fields:
        if candidate.kind == "search":
            return candidate
    return None


def input_field(tab: FormTab | None) -> FormField | None:
    """The tab's composer-fed field: the first search OR text field."""

    if tab is None:
        return None
    for candidate in tab.fields:
        if candidate.kind in ("search", "text"):
            return candidate
    return None


def filter_options(field_obj: FormField | None, filter_text: str) -> list[FormOption]:
    """Return options whose label/meta contain ``filter_text`` (case-folded)."""

    if field_obj is None:
        return []
    options = list(field_obj.options)
    needle = str(filter_text or "").strip().casefold()
    if not needle:
        return options
    return [
        option
        for option in options
        if needle in option.label.casefold()
        or needle in option.id.casefold()
        or needle in option.meta.casefold()
    ]


def visible_options(spec: FormSpec, state: FormState) -> list[FormOption]:
    return filter_options(list_field(active_tab(spec, state)), state.filter_text)


def has_navigable_list(spec: FormSpec, state: FormState) -> bool:
    return list_field(active_tab(spec, state)) is not None


def active_field_is_checkbox(spec: FormSpec, state: FormState) -> bool:
    field_obj = list_field(active_tab(spec, state))
    return field_obj is not None and field_obj.kind == "checkbox"


def active_input_is_secret(spec: FormSpec | None, state: FormState | None) -> bool:
    """True when the active tab's composer-fed field is a secret text field.

    Drives the composer's password display mode while the form is open.
    """

    if spec is None or state is None:
        return False
    field_obj = input_field(active_tab(spec, state))
    return field_obj is not None and field_obj.kind == "text" and field_obj.secret


# --------------------------------------------------------------------------- #
# State transitions (pure mutations of FormState)
# --------------------------------------------------------------------------- #


def init_state(spec: FormSpec) -> FormState:
    """Build a fresh state, parking each radio cursor on its current option
    and the active tab on the first tab flagged ``active`` (else tab 0)."""

    state = FormState()
    state.active_tab = next(
        (index for index, tab in enumerate(spec.tabs) if tab.active), 0
    )
    for tab in spec.tabs:
        for field_obj in tab.fields:
            if field_obj.kind not in ("radio", "checkbox"):
                continue
            if field_obj.kind == "radio":
                current = next(
                    (i for i, opt in enumerate(field_obj.options) if opt.current),
                    0,
                )
                state.selected_index[field_obj.key] = current
            else:
                state.selected_index.setdefault(field_obj.key, 0)
                state.toggled.setdefault(
                    field_obj.key,
                    {opt.id for opt in field_obj.options if opt.current},
                )
    return state


def _selected_key(spec: FormSpec, state: FormState) -> str | None:
    field_obj = list_field(active_tab(spec, state))
    return field_obj.key if field_obj is not None else None


def selected_index(spec: FormSpec, state: FormState) -> int:
    key = _selected_key(spec, state)
    if key is None:
        return 0
    options = visible_options(spec, state)
    if not options:
        return 0
    return max(0, min(state.selected_index.get(key, 0), len(options) - 1))


def clamp_selection(spec: FormSpec, state: FormState) -> None:
    key = _selected_key(spec, state)
    if key is None:
        return
    state.selected_index[key] = selected_index(spec, state)


def sync_filter(spec: FormSpec, state: FormState, text: str) -> None:
    """Update the filter from the composer buffer and reclamp the cursor.

    Newlines are stripped: the filter is a single-line search, and a stray
    newline (Ctrl+J, or a paste with a trailing newline) would otherwise render
    a multi-row search line that ``form_panel_height`` does not account for,
    clipping the fixed-height footer panel.
    """

    cleaned = str(text or "").replace("\r", "").replace("\n", "")
    if cleaned != state.filter_text:
        state.filter_text = cleaned
    clamp_selection(spec, state)


def move_selection(spec: FormSpec, state: FormState, delta: int) -> bool:
    key = _selected_key(spec, state)
    options = visible_options(spec, state)
    if key is None or not options:
        return False
    current = selected_index(spec, state)
    state.selected_index[key] = (current + delta) % len(options)
    return True


def move_tab(spec: FormSpec, state: FormState, delta: int) -> bool:
    if len(spec.tabs) <= 1:
        return False
    state.active_tab = (state.active_tab + delta) % len(spec.tabs)
    clamp_selection(spec, state)
    return True


def toggle_current(spec: FormSpec, state: FormState) -> bool:
    field_obj = list_field(active_tab(spec, state))
    if field_obj is None or field_obj.kind != "checkbox":
        return False
    options = visible_options(spec, state)
    if not options:
        return False
    option = options[selected_index(spec, state)]
    chosen = state.toggled.setdefault(field_obj.key, set())
    if option.id in chosen:
        chosen.discard(option.id)
    else:
        chosen.add(option.id)
    return True


def build_result(spec: FormSpec, state: FormState) -> FormResult:
    tab = active_tab(spec, state)
    field_obj = list_field(tab)
    radio_value: str | None = None
    checkbox_values: tuple[str, ...] = ()
    if field_obj is not None:
        options = visible_options(spec, state)
        if field_obj.kind == "radio" and options:
            radio_value = options[selected_index(spec, state)].id
        elif field_obj.kind == "checkbox":
            checkbox_values = tuple(sorted(state.toggled.get(field_obj.key, set())))
    return FormResult(
        spec_title=spec.title,
        tab_label=tab.label if tab is not None else "",
        radio_value=radio_value,
        checkbox_values=checkbox_values,
        filter_text=state.filter_text,
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _footer_hint(spec: FormSpec, state: FormState) -> str:
    if spec.footer_hint:
        return spec.footer_hint
    if not has_navigable_list(spec, state):
        return _TEXT_FOOTER
    return _DEFAULT_FOOTER if active_field_is_checkbox(spec, state) else _RADIO_FOOTER


def form_panel_height(
    spec: FormSpec | None,
    state: FormState | None,
    *,
    max_rows: int = FORM_PANEL_MAX_ROWS,
) -> int:
    """Return the number of terminal rows the form will occupy (0 when empty)."""

    if spec is None or state is None or not spec.tabs:
        return 0
    rows = 1  # header
    if len(spec.tabs) > 1:
        rows += 1
    if input_field(active_tab(spec, state)) is not None:
        rows += 1
    options = visible_options(spec, state)
    if has_navigable_list(spec, state):
        visible = min(len(options), max(1, max_rows)) if options else 1
        rows += visible
        if options and len(options) > visible:
            rows += 1  # overflow indicator
    rows += 1  # footer hint
    return rows


def form_panel_fragments(
    spec: FormSpec | None,
    state: FormState | None,
    *,
    width: int,
    max_rows: int = FORM_PANEL_MAX_ROWS,
) -> "StyleAndTextTuples":
    """Render the form as prompt_toolkit fragments."""

    if spec is None or state is None or not spec.tabs:
        return []

    panel_width = max(8, int(width or 0))
    fragments: "StyleAndTextTuples" = []
    lines: list[list[tuple[str, str]]] = []

    options = visible_options(spec, state)
    cursor = selected_index(spec, state)

    # Header: title (position/total)
    total = len(options)
    position = (cursor + 1) if total else 0
    header = f"{spec.title}  ({position}/{total})" if has_navigable_list(spec, state) else spec.title
    lines.append([("class:form-panel.title", _pad_line(header, panel_width))])

    # Tab bar (only with more than one tab)
    if len(spec.tabs) > 1:
        lines.append(_tab_bar_fragments(spec, state, panel_width))

    # Typed-input line (search filter or text value)
    field_input = input_field(active_tab(spec, state))
    if field_input is not None:
        lines.append(_input_fragments(field_input, state, panel_width))

    # Option list
    if has_navigable_list(spec, state):
        lines.extend(_list_fragments(spec, state, options, cursor, panel_width, max_rows))

    # Footer hint
    lines.append([("class:form-panel.footer", _pad_line(_footer_hint(spec, state), panel_width))])

    for index, line in enumerate(lines):
        fragments.extend(line)
        if index < len(lines) - 1:
            fragments.append(("", "\n"))
    return fragments


def _tab_bar_fragments(
    spec: FormSpec,
    state: FormState,
    panel_width: int,
) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    used = 0
    for index, tab in enumerate(spec.tabs):
        if used >= panel_width:
            break
        label = f" {tab.label} "
        if cell_len(label) > panel_width - used:
            label = truncate_cell_width(label, panel_width - used)
        style = (
            "class:form-panel.tab.active"
            if index == state.active_tab
            else "class:form-panel.tab"
        )
        parts.append((style, label))
        used += cell_len(label)
        if index < len(spec.tabs) - 1 and used + 2 <= panel_width:
            parts.append(("class:form-panel", "  "))
            used += 2
    if used < panel_width:
        parts.append(("class:form-panel", " " * (panel_width - used)))
    return parts


def _input_fragments(
    field_obj: FormField,
    state: FormState,
    panel_width: int,
) -> list[tuple[str, str]]:
    """Render the composer-fed line: the search filter or a text value.

    Secret text renders as one mask bullet per character, never the value.
    """

    if field_obj.kind == "text":
        prefix = f"{field_obj.label or 'Input'}: "
        default_placeholder = "type, then Enter"
    else:
        prefix = "Filter: "
        default_placeholder = "type to filter"
    text = state.filter_text
    if text:
        shown = _SECRET_MASK * len(text) if field_obj.secret else text
        body = _fit_cell(shown, max(1, panel_width - cell_len(prefix)))
        return [
            ("class:form-panel.search", prefix),
            ("class:form-panel.search", body),
            ("class:form-panel.search", " " * max(0, panel_width - cell_len(prefix) - cell_len(body))),
        ]
    placeholder = field_obj.placeholder or default_placeholder
    body = _fit_cell(placeholder, max(1, panel_width - cell_len(prefix)))
    return [
        ("class:form-panel.search", prefix),
        ("class:form-panel.placeholder", body),
        ("class:form-panel.placeholder", " " * max(0, panel_width - cell_len(prefix) - cell_len(body))),
    ]


def _list_fragments(
    spec: FormSpec,
    state: FormState,
    options: list[FormOption],
    cursor: int,
    panel_width: int,
    max_rows: int,
) -> list[list[tuple[str, str]]]:
    lines: list[list[tuple[str, str]]] = []
    field_obj = list_field(active_tab(spec, state))
    if field_obj is None:
        return lines
    if not options:
        return [[("class:form-panel.placeholder", _pad_line("No matches", panel_width))]]

    chosen = state.toggled.get(field_obj.key, set()) if field_obj.kind == "checkbox" else set()
    rows = _row_texts(field_obj, options, chosen)
    name_width = _name_column_width(rows, panel_width)
    desc_width = max(1, panel_width - name_width - FORM_PANEL_NAME_GUTTER)

    visible, window_start = _visible_window(options, max_rows=max_rows, selected_index=cursor)
    hidden = len(options) - len(visible)

    for offset, option in enumerate(visible):
        absolute = window_start + offset
        selected = absolute == cursor
        base_style = "class:slash-panel.selected" if selected else "class:slash-panel"
        name_style = "class:slash-panel.selected.name" if selected else "class:slash-panel.name"
        desc_style = "class:slash-panel.selected.desc" if selected else "class:slash-panel.desc"
        caret = "› " if selected else "  "
        name_cell = _fit_cell(caret + rows[absolute], name_width)
        desc_cell = _fit_cell(option.meta or option.description or "", desc_width)
        line = [
            (name_style, name_cell),
            (base_style, " " * FORM_PANEL_NAME_GUTTER),
            (desc_style, desc_cell),
        ]
        used = name_width + FORM_PANEL_NAME_GUTTER + desc_width
        if used < panel_width:
            line.append((base_style, " " * (panel_width - used)))
        lines.append(line)

    if hidden > 0:
        lines.append([("class:form-panel.more", _pad_line(f"↓ {hidden} more below", panel_width))])
    return lines


def _row_texts(
    field_obj: FormField,
    options: list[FormOption],
    chosen: set[str],
) -> list[str]:
    rows: list[str] = []
    for option in options:
        if field_obj.kind == "checkbox":
            marker = "[x]" if option.id in chosen else "[ ]"
        else:
            marker = "◉" if option.current else "○"
        rows.append(f"{marker} {option.label}")
    return rows


def _name_column_width(rows: list[str], panel_width: int) -> int:
    caret = 2  # "› " / "  " prefix added at render time
    longest = max((cell_len(row) for row in rows), default=0) + caret
    target = max(FORM_PANEL_NAME_MIN_WIDTH, longest)
    cap = min(FORM_PANEL_NAME_MAX_WIDTH, max(1, panel_width - 4))
    return max(1, min(target, cap))


def _visible_window(
    options: list[FormOption],
    *,
    max_rows: int,
    selected_index: int,
) -> tuple[list[FormOption], int]:
    rows = max(1, max_rows)
    if len(options) <= rows:
        return options, 0
    bounded = max(0, min(selected_index, len(options) - 1))
    start = min(max(0, bounded - rows + 1), max(0, len(options) - rows))
    return options[start : start + rows], start


def _fit_cell(text: str, width: int) -> str:
    width = max(1, width)
    cells = cell_len(text)
    if cells == width:
        return text
    if cells > width:
        return truncate_cell_width(text, width)
    return text + " " * (width - cells)


def _pad_line(text: str, width: int) -> str:
    cells = cell_len(text)
    if cells >= width:
        return truncate_cell_width(text, width)
    return text + " " * (width - cells)


__all__ = [
    "FORM_PANEL_MAX_ROWS",
    "ChangeHandler",
    "ConfirmHandler",
    "FormField",
    "FormOption",
    "FormResult",
    "FormSpec",
    "FormState",
    "FormTab",
    "active_field_is_checkbox",
    "active_input_is_secret",
    "active_tab",
    "build_result",
    "clamp_selection",
    "filter_options",
    "form_panel_fragments",
    "form_panel_height",
    "has_navigable_list",
    "init_state",
    "input_field",
    "list_field",
    "move_selection",
    "move_tab",
    "search_field",
    "selected_index",
    "sync_filter",
    "toggle_current",
    "visible_options",
]
