"""Base wizard screen and reusable single/multi-select step screens.

Every step is a Textual `Screen`. The base wires the shared chrome (title, step
counter, note, error slot, hint bar) and the navigation keys: Enter advances,
Esc goes back one step (not exit), Ctrl+Q quits with a confirm. Concrete steps
override `compose_body()` and `collect()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import RadioButton, RadioSet, SelectionList, Static
from textual.widgets.selection_list import Selection

from ..nav import Step

if TYPE_CHECKING:
    from ..app import SetupWizardApp
    from ..state import WizardState


@dataclass(frozen=True)
class Choice:
    value: Any
    label: str
    description: str = ""


def commit_radio_highlight(radio_set: RadioSet) -> int:
    """Press the highlighted radio so the dot matches the cursor, return its index.

    Arrow keys move the RadioSet highlight (`_selected`) but only Space presses
    it; the wizard advances on Enter, so we commit the highlight here so a single
    Enter both selects the highlighted option and moves on. Falls back to the
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


class WizardStep(Screen):
    """Shared chrome and navigation for every wizard step."""

    BINDINGS = [
        Binding("enter", "next", "Next", priority=True),
        Binding("escape", "back", "Back", priority=True),
        Binding("ctrl+s", "skip", "Skip", priority=True),
        Binding("ctrl+q", "quit_wizard", "Quit", priority=True),
    ]

    DEFAULT_HINT = "up/down move   enter next   esc back   ctrl+s skip   ctrl+q quit"

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
        yield Static(self._title, id="wizard-title")
        yield Static(f"Step {self._number} of {self._total}", id="wizard-step")
        if self._note:
            yield Static(self._note, id="wizard-note")
        with VerticalScroll(id="wizard-body"):
            yield from self.compose_body()
        yield Static("", id="wizard-error")
        yield Static(self._hint, id="wizard-hint")

    def compose_body(self) -> ComposeResult:
        return iter(())

    def collect(self) -> bool:
        """Write widget values into state. Return False to veto advancing."""
        return True

    def show_error(self, message: str) -> None:
        self.query_one("#wizard-error", Static).update(message)

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


class SingleSelectStep(WizardStep):
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

    def compose_body(self) -> ComposeResult:
        initial = self._get_initial(self.state)
        buttons = [
            RadioButton(choice.label, value=(choice.value == initial))
            for choice in self._choices
        ]
        if initial is None and buttons:
            buttons[0] = RadioButton(self._choices[0].label, value=True)
        yield RadioSet(*buttons)
        yield Static("", id="choice-desc")

    def on_mount(self) -> None:
        radio_set = self.query_one(RadioSet)
        radio_set.focus()
        idx = radio_set.pressed_index
        if 0 <= idx < len(self._choices):
            self._set_description(idx)

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self._set_description(event.index)

    def _set_description(self, index: int) -> None:
        if 0 <= index < len(self._choices):
            self.query_one("#choice-desc", Static).update(
                self._choices[index].description
            )

    def collect(self) -> bool:
        idx = commit_radio_highlight(self.query_one(RadioSet))
        if idx is None or idx < 0 or idx >= len(self._choices):
            self.show_error("Select an option, then press Enter.")
            return False
        self._store(self.state, self._choices[idx].value)
        return True


class MultiSelectStep(WizardStep):
    """A checkbox list that stores a list of selected values into state."""

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

    def compose_body(self) -> ComposeResult:
        selected = set(self._get_initial(self.state) or [])
        selections = [
            Selection(choice.label, choice.value, choice.value in selected)
            for choice in self._choices
        ]
        yield SelectionList(*selections)

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
    "Choice",
    "WizardStep",
    "SingleSelectStep",
    "MultiSelectStep",
    "single_select_step",
    "multi_select_step",
    "placeholder_step",
]
