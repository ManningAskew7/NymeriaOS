"""Base wizard screen and reusable single/multi-select step screens.

Keyboard model (a web form): on a `FormStep`, the arrow keys move focus across
every element of the step top to bottom (option groups AND input fields, never
trapped in one control); Space selects the focused option (single-select picks
one, multi-select toggles independently); Enter locks the highlighted option in
and advances, or, when a required field is still empty, focuses that field and
shows an error. Concrete steps override `compose_body()` and `collect()`.

Single-select option groups are individual `CircleRadioButton`s inside a
`Vertical(classes="radio-group")` so each is independently focusable and
exclusivity is enforced per group (Textual's stock `RadioSet` captures the arrow
keys internally, which would trap focus). Multi-select steps keep Textual's
`SelectionList` (already arrow-to-highlight / space-to-toggle) and stay on the
plain `WizardStep` so the list keeps its internal arrow navigation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.screen import Screen
from textual.widgets import RadioButton, RadioSet, SelectionList, Static
from textual.widgets.selection_list import Selection

from ..nav import Step
from ..widgets import BracketSelectionList

if TYPE_CHECKING:
    from ..app import SetupWizardApp
    from ..state import WizardState


ACCENT = "#bbddfb"
# Secondary text: a grey-blue dimmer than the white primary, bright enough to
# read comfortably. Keep in sync with the secondary color in theme.tcss.
SECONDARY = "#b6c1ce"
_HINT_TEXT = "#6b7280"

_CODE_SPAN = re.compile(r"`([^`\n]+)`")


def code_markup(text: str) -> str:
    """Escape prose for Rich markup, rendering `code` spans in the accent color.

    The wizard's prose widgets (notes, choice descriptions, errors) parse Rich
    markup, not markdown, so literal backticks would otherwise show through.
    Plain text in, markup out: commands like `nymeria slim` read as commands.
    """
    return _CODE_SPAN.sub(lambda m: f"[{ACCENT}]{m.group(1)}[/]", escape(text))


def hint_markup(hint: str) -> str:
    """Style a 'key action   key action' hint line: accent keys, grey actions.

    Segments are split on runs of 2+ spaces; the first word of each segment is
    the key. Plain text in, markup out, so callers keep writing plain hints.
    """
    parts: list[str] = []
    for segment in re.split(r"\s{2,}", hint.strip()):
        key, _, action = segment.partition(" ")
        if action:
            parts.append(f"[{ACCENT}]{key}[/] [{_HINT_TEXT}]{action}[/]")
        elif key:
            parts.append(f"[{ACCENT}]{key}[/]")
    return "   ".join(parts)


@dataclass(frozen=True)
class Choice:
    value: Any
    label: str
    description: str = ""
    # Rendered greyed-out and unfocusable; the choice stays visible so users
    # see what exists, but it cannot be selected (e.g. "to come" options).
    disabled: bool = False


def commit_radio_highlight(radio_set: RadioSet) -> int:
    """Press the highlighted radio so the dot matches the cursor, return its index.

    Used by the connection step's api-mode `SelectingRadioSet` (the wizard
    advances on Enter, so the highlight is committed here). Falls back to the
    already-pressed index if the highlight cannot be read.
    """

    idx = getattr(radio_set, "_selected", None)
    if isinstance(idx, int) and idx >= 0:
        buttons = list(radio_set.query(RadioButton))
        if 0 <= idx < len(buttons):
            if not buttons[idx].value:
                buttons[idx].value = True
            return idx
    return radio_set.pressed_index


class SelectingRadioSet(RadioSet):
    """A `RadioSet` whose pressed dot follows the highlight cursor.

    Retained for the connection step's api-mode picker (a self-contained
    `RadioSet`); the single-select STEPS now use individual `CircleRadioButton`s
    in a `.radio-group` instead (see `FormStep`).
    """

    def align_cursor_to_selection(self) -> None:
        if self.pressed_index >= 0:
            self._selected = self.pressed_index

    def _follow_highlight(self) -> None:
        index = self._selected
        if index is None or not self.is_mounted:
            return
        if 0 <= index < len(self._nodes):
            button = self._nodes[index]
            if isinstance(button, RadioButton) and not button.value:
                button.value = True

    def action_next_button(self) -> None:
        super().action_next_button()
        self._follow_highlight()

    def action_previous_button(self) -> None:
        super().action_previous_button()
        self._follow_highlight()


class CircleRadioButton(RadioButton):
    """A `RadioButton` drawn as a bare circle, with no filled indicator box.

    Renders only the circle glyph (outline when off, filled when on); the
    `toggle--button` component style (theme.tcss) keeps it white over a
    transparent background, so the circle shows with no blue box. Subclasses
    `RadioButton`, so it stays focusable and `query(RadioButton)` still finds it.
    """

    _GLYPH_ON = "●"  # filled circle
    _GLYPH_OFF = "○"  # outline circle

    @property
    def _button(self) -> Content:
        glyph = self._GLYPH_ON if self.value else self._GLYPH_OFF
        return Content.assemble((glyph, self.get_visual_style("toggle--button")))


class WizardStep(Screen):
    """Shared chrome and navigation for every wizard step."""

    BINDINGS = [
        Binding("enter", "next", "Next", priority=True),
        Binding("escape", "back", "Back", priority=True),
        Binding("ctrl+s", "skip", "Skip", priority=True),
        Binding("ctrl+q", "quit_wizard", "Quit", priority=True),
    ]

    DEFAULT_HINT = "up/down move   space select   enter next   esc back   ctrl+s skip   ctrl+q quit"

    def __init__(
        self,
        wizard: "SetupWizardApp",
        number: int,
        total: int,
        *,
        step_id: str,
        title: str,
        note: str = "",
        hint: str | None = None,
    ) -> None:
        super().__init__()
        self._wizard = wizard
        self._number = number
        self._total = total
        self.step_id = step_id
        self._title = title
        self._note = note
        self._hint = hint or self.DEFAULT_HINT

    @property
    def state(self) -> "WizardState":
        return self._wizard.state

    def compose(self) -> ComposeResult:
        with Horizontal(id="wizard-header"):
            yield Static(self._title, id="wizard-title")
            yield Static(f"Step {self._number} of {self._total}", id="wizard-step")
        if self._note:
            yield Static(code_markup(self._note), id="wizard-note")
        # can_focus=False keeps the scroll container out of the arrow-key focus
        # chain (so focus moves option->option, not onto the body); focusing a
        # child still auto-scrolls it into view.
        with VerticalScroll(id="wizard-body", can_focus=False):
            yield from self.compose_body()
        # Both start hidden: an empty Static still reserves a row, and on a
        # small terminal that row is the difference between fitting and
        # scrolling. show_error/_update_scroll_hint toggle visibility.
        error = Static("", id="wizard-error")
        error.display = False
        yield error
        scroll_hint = Static("", id="wizard-scroll-hint")
        scroll_hint.display = False
        yield scroll_hint
        yield Static(hint_markup(self._hint), id="wizard-hint")

    def compose_body(self) -> ComposeResult:
        return iter(())

    def collect(self) -> bool:
        """Write widget values into state. Return False to veto advancing."""
        return True

    def show_error(self, message: str) -> None:
        error = self.query_one("#wizard-error", Static)
        error.update(code_markup(message))
        error.display = bool(message)
        # Toggling the error row resizes the body without a Resize reaching
        # this screen: re-derive the scroll cue and keep the focused widget
        # (usually the offending field) in view once geometry settles.
        self.call_after_refresh(self._settle_after_layout_shift)

    def _settle_after_layout_shift(self) -> None:
        self._update_scroll_hint()
        focused = self.focused
        if focused is not None:
            focused.scroll_visible()

    # --- scroll affordance --------------------------------------------------

    def on_resize(self, _event: object) -> None:
        # Screens always receive Resize; child widgets do NOT when only their
        # container's height changes, so picker lists are re-fit from here.
        from ..widgets import SearchableList

        for picker in self.query(SearchableList):
            picker.fit_list()
        self._update_scroll_hint()

    def _update_scroll_hint(self) -> None:
        """Show a 'more above / more below' cue when the body overflows."""
        try:
            body = self.query_one("#wizard-body", VerticalScroll)
            hint = self.query_one("#wizard-scroll-hint", Static)
        except Exception:
            return
        parts: list[str] = []
        if body.scroll_y > 0.5:
            parts.append("▲ more above")
        if body.scroll_y < body.max_scroll_y - 0.5:
            parts.append("▼ more below")
        hint.update(f"[{ACCENT}]{'    '.join(parts)}[/]" if parts else "")
        hint.display = bool(parts)

    # --- navigation ---------------------------------------------------------

    def action_next(self) -> None:
        self.show_error("")
        if self.collect():
            self._wizard.advance()

    def action_skip(self) -> None:
        """Advance without recording anything for this step."""
        self.show_error("")
        self._wizard.advance()

    def action_back(self) -> None:
        self._wizard.go_back()

    def action_quit_wizard(self) -> None:
        self._wizard.request_quit()


class FormStep(WizardStep):
    """A step whose body is a vertical form: arrows move focus across every
    element, Space selects the focused option, Enter locks it in then validates.

    Subclasses lay out `Vertical(classes="radio-group")` blocks of
    `CircleRadioButton`s and/or `Input`s, implement `collect()` (focusing the
    offending field + showing an error when something required is missing), and
    may override `_sync_description()` / `_on_single_select()` hooks.
    """

    BINDINGS = [
        Binding("down", "field_next", "Next field", priority=True),
        Binding("up", "field_prev", "Prev field", priority=True),
    ]

    def action_field_next(self) -> None:
        self.focus_next()
        self._refresh_focus_view()

    def action_field_prev(self) -> None:
        self.focus_previous()
        self._refresh_focus_view()

    def on_descendant_focus(self, event: object) -> None:
        self._refresh_focus_view(getattr(event, "widget", None))

    def _refresh_focus_view(self, focused: object | None = None) -> None:
        self._update_scroll_hint()
        self._sync_description(focused if focused is not None else self.focused)

    def _sync_description(self, focused: object) -> None:
        """Hook: update a per-option description from the focused option."""
        return

    # --- single-select exclusivity ------------------------------------------

    @staticmethod
    def _radio_group(button: RadioButton):
        for ancestor in button.ancestors:
            has_class = getattr(ancestor, "has_class", None)
            if has_class is not None and ancestor.has_class("radio-group"):
                return ancestor
        return None

    def on_radio_button_changed(self, event: object) -> None:
        button = getattr(event, "radio_button", None)
        if button is None:
            return
        group = self._radio_group(button)
        if group is None:
            return
        if getattr(event, "value", False):
            for other in group.query(RadioButton):
                if other is not button and other.value:
                    other.value = False
            self._on_single_select(group, button)
        elif not any(b.value for b in group.query(RadioButton)):
            # Single-select cannot end up empty: re-select the one just cleared.
            button.value = True

    def _on_single_select(self, group: object, button: RadioButton) -> None:
        """Hook: react to a single-select choice (e.g. show/hide a key field)."""
        return

    def _lock_focused_option(self) -> None:
        """Enter 'locks in the highlighted one': select the focused radio option.

        Clears siblings synchronously (not via the async `Changed` handler) so a
        `collect()` in the same Enter sees exactly one selected button.
        """
        focused = self.focused
        if not isinstance(focused, RadioButton):
            return
        group = self._radio_group(focused)
        if group is None:
            return
        for button in group.query(RadioButton):
            want = button is focused
            if button.value != want:
                button.value = want

    def action_next(self) -> None:
        self.show_error("")
        self._lock_focused_option()
        if self.collect():
            self._wizard.advance()


class SingleSelectStep(FormStep):
    """A radio-button choice that stores one value into state."""

    def __init__(
        self,
        wizard: "SetupWizardApp",
        number: int,
        total: int,
        *,
        step_id: str,
        title: str,
        choices: list[Choice],
        get_initial: Callable[["WizardState"], Any],
        store: Callable[["WizardState", Any], None],
        note: str = "",
    ) -> None:
        super().__init__(
            wizard, number, total, step_id=step_id, title=title, note=note
        )
        self._choices = choices
        self._get_initial = get_initial
        self._store = store

    def _initial_index(self) -> int:
        initial = self._get_initial(self.state)
        for i, choice in enumerate(self._choices):
            if choice.value == initial and not choice.disabled:
                return i
        # Steps must keep at least one enabled choice; the trailing 0 is a
        # defensive fallback only (an all-disabled step cannot be advanced).
        return next(
            (i for i, choice in enumerate(self._choices) if not choice.disabled), 0
        )

    def compose_body(self) -> ComposeResult:
        sel = self._initial_index()
        with Vertical(classes="radio-group"):
            # Disabled choices still yield a button: collect() and
            # _sync_description map button index to choice index positionally.
            for i, choice in enumerate(self._choices):
                yield CircleRadioButton(
                    choice.label, value=(i == sel), disabled=choice.disabled
                )
        yield Static("", id="choice-desc")

    def _buttons(self) -> list[RadioButton]:
        return list(self.query_one(".radio-group").query(RadioButton))

    def on_mount(self) -> None:
        buttons = self._buttons()
        focusable = [b for b in buttons if not b.disabled]
        if focusable:
            selected = next((b for b in focusable if b.value), focusable[0])
            selected.focus()
        self.call_after_refresh(self._refresh_focus_view)

    def _sync_description(self, focused: object) -> None:
        buttons = self._buttons()
        if focused in buttons:
            i = buttons.index(focused)  # type: ignore[arg-type]
            if 0 <= i < len(self._choices):
                self.query_one("#choice-desc", Static).update(
                    code_markup(self._choices[i].description)
                )

    def collect(self) -> bool:
        buttons = self._buttons()
        sel = next((i for i, b in enumerate(buttons) if b.value), None)
        if sel is None or sel >= len(self._choices):
            self.show_error("Select an option, then press Enter.")
            return False
        if self._choices[sel].disabled:
            self.show_error("That option is not available yet; pick another.")
            return False
        self._store(self.state, self._choices[sel].value)
        return True


class MultiSelectStep(WizardStep):
    """A checkbox list that stores a list of selected values into state.

    Stays on `SelectionList` (and plain `WizardStep`, not `FormStep`) so the list
    keeps its own arrow navigation; it is a single field, so cross-field focus
    movement is not needed. Space toggles, Enter advances.
    """

    def __init__(
        self,
        wizard: "SetupWizardApp",
        number: int,
        total: int,
        *,
        step_id: str,
        title: str,
        choices: list[Choice],
        get_initial: Callable[["WizardState"], list[Any]],
        store: Callable[["WizardState", list[Any]], None],
        note: str = "",
        warning_fn: Callable[["WizardState"], str | None] | None = None,
    ) -> None:
        super().__init__(
            wizard,
            number,
            total,
            step_id=step_id,
            title=title,
            note=note,
            hint="up/down move   space toggle   enter next   esc back   ctrl+s skip   ctrl+q quit",
        )
        self._choices = choices
        self._get_initial = get_initial
        self._store = store
        self._warning_fn = warning_fn

    def compose_body(self) -> ComposeResult:
        # Non-blocking nudge (e.g. "this search backend needs a fetch backend"),
        # computed from prior steps' state. Shown above the list; never vetoes.
        warning = self._warning_fn(self.state) if self._warning_fn else None
        if warning:
            yield Static(f"[yellow]{warning}[/yellow]", id="multi-warning")
        selected = set(self._get_initial(self.state) or [])
        selections = [
            Selection(choice.label, choice.value, choice.value in selected)
            for choice in self._choices
        ]
        # BracketSelectionList renders the checkboxes as [x] / [ ]; queries
        # keep using the SelectionList base type.
        yield BracketSelectionList(*selections)

    def on_mount(self) -> None:
        self.query_one(SelectionList).focus()

    def collect(self) -> bool:
        self._store(self.state, list(self.query_one(SelectionList).selected))
        return True


# --- step factories ---------------------------------------------------------


def single_select_step(
    *,
    step_id: str,
    title: str,
    choices: list[Choice],
    get_initial: Callable[["WizardState"], Any],
    store: Callable[["WizardState", Any], None],
    note: str = "",
    applies: Callable[["WizardState"], bool] = lambda _state: True,
) -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> Screen:
        return SingleSelectStep(
            wizard,
            number,
            total,
            step_id=step_id,
            title=title,
            choices=choices,
            get_initial=get_initial,
            store=store,
            note=note,
        )

    return Step(id=step_id, applies=applies, build=build)


def multi_select_step(
    *,
    step_id: str,
    title: str,
    choices: list[Choice],
    get_initial: Callable[["WizardState"], list[Any]],
    store: Callable[["WizardState", list[Any]], None],
    note: str = "",
    applies: Callable[["WizardState"], bool] = lambda _state: True,
    warning_fn: Callable[["WizardState"], str | None] | None = None,
) -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> Screen:
        return MultiSelectStep(
            wizard,
            number,
            total,
            step_id=step_id,
            title=title,
            choices=choices,
            get_initial=get_initial,
            store=store,
            note=note,
            warning_fn=warning_fn,
        )

    return Step(id=step_id, applies=applies, build=build)


def placeholder_step(
    *,
    step_id: str,
    title: str,
    note: str,
    options: list[Choice] | None = None,
) -> Step:
    """A stub single-select step whose answer lands in `state.extras[step_id]`.

    The architecture is real; the option content is intentionally a placeholder
    until each capability is designed for real.
    """

    choices = list(options or [])
    choices.append(Choice(value="__skip__", label="Skip for now", description="Configure this later."))

    def get_initial(state: "WizardState") -> Any:
        return state.extras.get(step_id, "__skip__")

    def store(state: "WizardState", value: Any) -> None:
        state.extras[step_id] = value

    return single_select_step(
        step_id=step_id,
        title=title,
        note=note,
        choices=choices,
        get_initial=get_initial,
        store=store,
    )


__all__ = [
    "ACCENT",
    "SECONDARY",
    "code_markup",
    "hint_markup",
    "Choice",
    "WizardStep",
    "FormStep",
    "CircleRadioButton",
    "SelectingRadioSet",
    "commit_radio_highlight",
    "SingleSelectStep",
    "MultiSelectStep",
    "single_select_step",
    "multi_select_step",
    "placeholder_step",
]
