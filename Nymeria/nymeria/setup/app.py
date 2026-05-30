"""The Textual wizard app: owns state, the step list, and navigation.

Navigation maps onto Textual's screen stack: advancing pushes the next
applicable step, going back pops to the previous one. Because every answer
lives in `WizardState` and each screen initializes from it, back/forward is
lossless. Esc goes back; Ctrl+Q quits with a confirm so a stray keypress never
discards a half-finished setup.
"""

from __future__ import annotations

from typing import cast

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Static

from .nav import Navigator, Step
from .state import WizardState
from .steps import build_default_steps


class QuitConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="quit-box"):
            yield Static("Quit setup? Your answers will be discarded.", id="quit-text")
            with Horizontal(id="quit-buttons"):
                yield Button("Quit", variant="error", id="quit-yes")
                yield Button("Keep editing", id="quit-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "quit-yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class SetupWizardApp(App):
    CSS_PATH = "theme.tcss"
    ENABLE_COMMAND_PALETTE = False

    def __init__(
        self,
        state: WizardState,
        steps: list[Step] | None = None,
    ) -> None:
        super().__init__()
        self.state = state
        self.steps = steps if steps is not None else build_default_steps()
        self.nav = Navigator(self.steps, state)
        self.completed = False

    def on_mount(self) -> None:
        idx = self.nav.start()
        if idx is None:
            self.completed = True
            self.exit(self.state)
            return
        self.push_screen(self._screen_for(idx))

    def _screen_for(self, idx: int) -> Screen:
        number, total = self.nav.position()
        return cast(Screen, self.steps[idx].build(self, number, total))

    def advance(self) -> None:
        idx = self.nav.advance()
        if idx is None:
            self.completed = True
            self.exit(self.state)
        else:
            self.push_screen(self._screen_for(idx))

    def go_back(self) -> None:
        if self.nav.back() is not None:
            self.pop_screen()

    def request_quit(self) -> None:
        def handle(result: bool | None) -> None:
            if result:
                self.completed = False
                self.exit(None)

        self.push_screen(QuitConfirmScreen(), handle)


__all__ = ["SetupWizardApp", "QuitConfirmScreen"]
