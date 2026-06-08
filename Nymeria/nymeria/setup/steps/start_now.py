"""Optional final step: offer to start the backend now, or just print the command.

After review, the only thing left is launching the backend. Rather than silently
auto-starting (which would seize the terminal for a local foreground run, or run
Docker the user did not expect), this step asks. The choice maps to
``state.next_action``: ``START_API_OPEN_FRONTEND`` makes ``finalize`` run the
start command (Docker detached then wait-for-health, or ``nymeria slim`` in the
foreground); ``PRINT_COMMANDS`` just prints it.

The default is shape-aware (the first choice is selected): Docker defaults to
starting (detached returns immediately and is safe), local defaults to printing
(a foreground start takes over the terminal). The step is skipped for the
background-service host (start is not wired) and when a flag already chose the
CLI handoff. The choice is mirrored into ``state.extras["start_now"]`` so that
navigating back and forward is lossless even though the global ``next_action``
default is ``PRINT_COMMANDS``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..nav import Step
from ...onboarding import HostingOption, NextAction
from .base import Choice, SingleSelectStep

if TYPE_CHECKING:
    from ..app import SetupWizardApp
    from ..state import WizardState


_DOCKER_START = Choice(
    value=NextAction.START_API_OPEN_FRONTEND,
    label="Start now (Docker, detached)",
    description=(
        "Run the single-container compose in the background now, wait for the "
        "health check, then show the URL and bootstrap token."
    ),
)
_DOCKER_PRINT = Choice(
    value=NextAction.PRINT_COMMANDS,
    label="Just print the command",
    description="Finish setup and show the start command to run yourself.",
)
_LOCAL_PRINT = Choice(
    value=NextAction.PRINT_COMMANDS,
    label="Print the command",
    description="Finish setup and show `nymeria slim` to run yourself.",
)
_LOCAL_START = Choice(
    value=NextAction.START_API_OPEN_FRONTEND,
    label="Start now (foreground)",
    description=(
        "Launch `nymeria slim` now in this terminal. It takes over the terminal; "
        "press Ctrl+C to stop."
    ),
)


def start_now_choices(hosting: "HostingOption | None") -> list[Choice]:
    """Shape-aware options; the first (index 0) is the default selection.

    Docker defaults to starting (detached, returns immediately); local defaults
    to printing (a foreground start seizes the terminal).
    """
    if hosting is HostingOption.DOCKER:
        return [_DOCKER_START, _DOCKER_PRINT]
    return [_LOCAL_PRINT, _LOCAL_START]


def start_now_applies(state: "WizardState") -> bool:
    """Only for a local or Docker host (service start is not wired), and not when
    a flag already chose the CLI handoff."""
    return (
        state.hosting in (HostingOption.LOCAL, HostingOption.DOCKER)
        and state.next_action is not NextAction.CLI
    )


def _get_initial(state: "WizardState") -> NextAction:
    chosen = state.extras.get("start_now")
    if isinstance(chosen, NextAction):
        return chosen
    if chosen is not None:
        try:
            return NextAction(chosen)
        except ValueError:
            pass
    # Nothing chosen yet: fall back to the shape default (index 0).
    return start_now_choices(state.hosting)[0].value


def _store(state: "WizardState", value: NextAction) -> None:
    state.extras["start_now"] = value
    state.next_action = value


def make_start_now_step() -> Step:
    def build(wizard: "SetupWizardApp", number: int, total: int) -> SingleSelectStep:
        return SingleSelectStep(
            wizard,
            number,
            total,
            step_id="start_now",
            title="Start Nymeria now?",
            note=(
                "Setup is ready to write. Choose whether to launch the backend "
                "now or just print the start command."
            ),
            choices=start_now_choices(wizard.state.hosting),
            get_initial=_get_initial,
            store=_store,
        )

    return Step(id="start_now", applies=start_now_applies, build=build)


__all__ = ["make_start_now_step", "start_now_choices", "start_now_applies"]
