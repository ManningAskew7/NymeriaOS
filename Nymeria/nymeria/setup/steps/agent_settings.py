"""Agent-tuning steps: context strategy, timezone, agent limits, LLM tuning.

Four `FormStep`s over the TUI-free ``setup/tuning_catalog.py``. Each numeric
input is optional: blank keeps the settings default (or an existing env line),
typed values are validated at collect time and land in ``state.extras`` as raw
strings (finalize parses them again via the catalog when producing env lines).
The timezone step is the one prefilled field: it shows the detected host
timezone for the user to confirm or correct (Enter writes it; clearing the
field keeps the UTC default; skipping records nothing, like every step).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.screen import Screen
from textual.widgets import Input, RadioButton, Static

from ..nav import Step
from ..tuning_catalog import (
    CONTEXT_CHOICES,
    CONTEXT_FIELDS,
    CONTEXT_VALUES,
    EFFORT_CHOICES,
    EFFORT_VALUES,
    LIMIT_FIELDS,
    RECOMMENDED_EFFORT,
    SAMPLING_FIELDS,
    TIMEZONE_FIELD,
    TuningChoice,
    TuningField,
    annotated_effort_choices,
    effort_ladder_note,
    parse_field,
)
from .base import CircleRadioButton, FormStep, code_markup

if TYPE_CHECKING:
    from ..app import SetupWizardApp


def _field_id(field: TuningField) -> str:
    return f"tuning-{field.key.replace('_', '-')}"


class _TuningFormStep(FormStep):
    """Shared radio-group and optional-field machinery for the tuning steps."""

    _choices: tuple[TuningChoice, ...] = ()

    # --- radio group helpers (mirrors SingleSelectStep, plus input fields) ---

    def _buttons(self) -> list[RadioButton]:
        try:
            return list(self.query_one(".radio-group").query(RadioButton))
        except Exception:  # noqa: BLE001 (no radio group on this step)
            return []

    def _compose_choices(self, initial: str) -> ComposeResult:
        sel = next(
            (i for i, c in enumerate(self._choices) if c.value == initial), 0
        )
        with Vertical(classes="radio-group"):
            for i, choice in enumerate(self._choices):
                yield CircleRadioButton(choice.label, value=(i == sel))
        yield Static("", id="choice-desc")

    def _sync_description(self, focused: object) -> None:
        buttons = self._buttons()
        if focused in buttons:
            i = buttons.index(focused)  # type: ignore[arg-type]
            if 0 <= i < len(self._choices):
                # Annotated effort descriptions carry the raw model id, which
                # is user-typed and must not be parsed as markup (code_markup
                # escapes before styling).
                self.query_one("#choice-desc", Static).update(
                    code_markup(self._choices[i].description)
                )

    def _selected_choice(self) -> str | None:
        buttons = self._buttons()
        sel = next((i for i, b in enumerate(buttons) if b.value), None)
        if sel is None or sel >= len(self._choices):
            return None
        return self._choices[sel].value

    def on_mount(self) -> None:
        buttons = self._buttons()
        if buttons:
            selected = next((b for b in buttons if b.value), buttons[0])
            selected.focus()
        else:
            inputs = list(self.query(Input))
            if inputs:
                inputs[0].focus()
        self.call_after_refresh(self._refresh_focus_view)

    # --- optional input fields ------------------------------------------------

    def _compose_field(self, field: TuningField) -> ComposeResult:
        value = self.state.extras.get(field.key)
        yield Static(
            field.label, classes="field-label", id=f"{_field_id(field)}-label"
        )
        yield Input(
            value=value if isinstance(value, str) else "",
            placeholder=field.placeholder,
            id=_field_id(field),
        )

    def _compose_field_grid(self, fields: tuple[TuningField, ...]) -> ComposeResult:
        # Two-up grid (see .field-grid): focus order stays the field order
        # (left to right, then down), so up/down arrows still reach every box.
        with Container(classes="field-grid"):
            for field in fields:
                with Vertical(classes="field-cell"):
                    yield from self._compose_field(field)

    def _collect_fields(self, fields: tuple[TuningField, ...]) -> bool:
        # Two phases: validate everything first, mutate extras only when the
        # whole set passes. Otherwise a failed later field would leave earlier
        # fields committed, and a subsequent skip/back would carry them into
        # env production despite "skip records nothing".
        staged: list[tuple[TuningField, str]] = []
        for field in fields:
            try:
                widget = self.query_one(f"#{_field_id(field)}", Input)
            except Exception:  # noqa: BLE001 (field not on this screen)
                continue
            raw = widget.value.strip()
            _, error = parse_field(field, raw)
            if error:
                self.show_error(error)
                widget.focus()
                return False
            staged.append((field, raw))
        for field, raw in staged:
            if raw:
                self.state.extras[field.key] = raw
            else:
                self.state.extras.pop(field.key, None)
        return True


class _ContextStep(_TuningFormStep):
    """Context-management strategy plus the trigger value for the chosen mode."""

    _choices = CONTEXT_CHOICES

    def compose_body(self) -> ComposeResult:
        initial = self.state.extras.get("context_strategy")
        if not isinstance(initial, str) or initial not in CONTEXT_VALUES:
            initial = "compact_tokens"
        yield from self._compose_choices(initial)
        for field in CONTEXT_FIELDS.values():
            yield from self._compose_field(field)

    def on_mount(self) -> None:
        super().on_mount()
        selected = self._selected_choice() or "compact_tokens"
        self._apply_visibility(selected)

    def _apply_visibility(self, strategy: str) -> None:
        """Show only the trigger field matching the selected strategy."""
        active = CONTEXT_FIELDS.get(strategy)
        for field in CONTEXT_FIELDS.values():
            show = active is not None and field.key == active.key
            for widget_id in (f"{_field_id(field)}-label", _field_id(field)):
                try:
                    self.query_one(f"#{widget_id}").display = show
                except Exception:  # noqa: BLE001 (not mounted yet)
                    continue

    def _on_single_select(self, group: object, button: RadioButton) -> None:
        buttons = self._buttons()
        if button in buttons:
            i = buttons.index(button)
            if 0 <= i < len(self._choices):
                self._apply_visibility(self._choices[i].value)

    def collect(self) -> bool:
        strategy = self._selected_choice()
        if strategy is None:
            self.show_error("Select a strategy, then press Enter.")
            return False
        trigger = CONTEXT_FIELDS.get(strategy)
        if trigger is not None and not self._collect_fields((trigger,)):
            return False
        self.state.extras["context_strategy"] = strategy
        return True


class _TimezoneStep(_TuningFormStep):
    """Confirm the detected host timezone (the one prefilled wizard field).

    The Input starts with the detected IANA name (or a previously hydrated
    value), so Enter confirms it and writes USER_TIMEZONE explicitly. Clearing
    the field keeps the UTC default; Ctrl+S skip records nothing, which is why
    the detection prefills the widget rather than mutating state up front.
    """

    def compose_body(self) -> ComposeResult:
        value = self.state.extras.get(TIMEZONE_FIELD.key)
        if not isinstance(value, str) or not value.strip():
            from ..environment import detect_system_timezone

            value = detect_system_timezone() or ""
        yield Static(
            TIMEZONE_FIELD.label,
            classes="field-label",
            id=f"{_field_id(TIMEZONE_FIELD)}-label",
        )
        yield Input(
            value=value,
            placeholder=TIMEZONE_FIELD.placeholder,
            id=_field_id(TIMEZONE_FIELD),
        )

    def collect(self) -> bool:
        return self._collect_fields((TIMEZONE_FIELD,))


class _AgentLimitsStep(_TuningFormStep):
    """Optional agent caps and defaults; every field blank-keeps-default."""

    def compose_body(self) -> ComposeResult:
        yield Static(
            "All optional: leave a field blank to keep the default shown.",
            classes="field-note",
        )
        yield from self._compose_field_grid(LIMIT_FIELDS)

    def collect(self) -> bool:
        return self._collect_fields(LIMIT_FIELDS)


class _LLMTuningStep(_TuningFormStep):
    """Reasoning effort plus optional sampling overrides for the primary LLM."""

    _choices = EFFORT_CHOICES

    def compose_body(self) -> ComposeResult:
        # Screens are rebuilt on every forward navigation, so the annotations
        # always reflect the model picked on the preceding steps (or hydrated
        # from disk on a section jump).
        self._choices = annotated_effort_choices(self.state)
        initial = self.state.extras.get("llm_effort")
        if not isinstance(initial, str) or initial not in EFFORT_VALUES:
            initial = RECOMMENDED_EFFORT
        ladder = effort_ladder_note(self.state)
        if ladder:
            yield Static(escape(ladder), classes="field-note")
        yield from self._compose_choices(initial)
        yield Static(
            "Optional sampling overrides. Blank keeps the provider default.",
            classes="section-note",
        )
        yield from self._compose_field_grid(SAMPLING_FIELDS)

    def collect(self) -> bool:
        effort = self._selected_choice()
        if effort is None:
            self.show_error("Select a reasoning effort, then press Enter.")
            return False
        if not self._collect_fields(SAMPLING_FIELDS):
            return False
        self.state.extras["llm_effort"] = effort
        return True


def _make_form_step(
    *,
    step_id: str,
    title: str,
    note: str,
    screen_cls: type[_TuningFormStep],
    applies=lambda _state: True,
) -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> Screen:
        return screen_cls(
            wizard, number, total, step_id=step_id, title=title, note=note
        )

    return Step(id=step_id, applies=applies, build=build)


def make_context_step() -> Step:
    return _make_form_step(
        step_id="context",
        title="Context management",
        note=(
            "How long conversations stay inside the model's context window. "
            "Token-triggered auto-compaction is recommended: the agent "
            "summarizes the thread and carries its memory forward."
        ),
        screen_cls=_ContextStep,
    )


def make_timezone_step() -> Step:
    return _make_form_step(
        step_id="timezone",
        title="Your timezone",
        note=(
            "Used for schedules, TODO deadlines, and time-aware replies. "
            "Detected from this machine when possible: press Enter to confirm "
            "it, correct it if wrong, or leave blank for UTC."
        ),
        screen_cls=_TimezoneStep,
    )


def make_agent_limits_step() -> Step:
    return _make_form_step(
        step_id="agent_limits",
        title="Agent limits",
        note=(
            "Memory caps and tool limits. Everything here can be changed "
            "later in settings."
        ),
        screen_cls=_AgentLimitsStep,
    )


def make_llm_tuning_step() -> Step:
    return _make_form_step(
        step_id="llm_tuning",
        title="Model tuning",
        note=(
            "Reasoning effort for your model, plus optional sampling "
            "overrides. Levels the model does not support are adjusted to "
            "the closest level it offers automatically."
        ),
        screen_cls=_LLMTuningStep,
        applies=lambda state: bool(state.provider)
        or state.auth_method_is_cliproxy(),
    )


__all__ = [
    "make_agent_limits_step",
    "make_context_step",
    "make_llm_tuning_step",
    "make_timezone_step",
]
