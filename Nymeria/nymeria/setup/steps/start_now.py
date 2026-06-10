"""Optional final step: offer to start the backend now, or just print the command.

After review, the only thing left is launching the backend. Rather than silently
auto-starting (which would seize the terminal for a local foreground run, or run
Docker the user did not expect), this step asks. The choice maps to
``state.next_action``: ``START_API_OPEN_FRONTEND`` makes ``finalize`` run the
start command (Docker detached then wait-for-health, the background-service
install + start + health verify, or ``nymeria slim`` in the foreground);
``PRINT_COMMANDS`` just prints it.

The default is shape-aware (the first choice is selected): Docker and the
background service default to starting (both are detached and give the terminal
back), local defaults to printing (a foreground start takes over the terminal).
The step is skipped when a flag already chose the CLI handoff. The choice is
mirrored into ``state.extras["start_now"]`` so that navigating back and forward
is lossless even though the global ``next_action`` default is
``PRINT_COMMANDS``.
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
        "Bring the Docker stack up in the background now, wait for the health "
        "check, then show the URL and bootstrap token."
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
_SERVICE_INSTALL = Choice(
    value=NextAction.START_API_OPEN_FRONTEND,
    label="Install and start the service now",
    description=(
        "Install the background service (a systemd user unit on Linux, a "
        "launchd agent on macOS), start it, and wait for the health check."
    ),
)
_SERVICE_PRINT = Choice(
    value=NextAction.PRINT_COMMANDS,
    label="Just print the command",
    description="Finish setup and show `nymeria service install` to run yourself.",
)


def start_now_choices(hosting: "HostingOption | None") -> list[Choice]:
    """Shape-aware options; the first (index 0) is the default selection.

    Docker and the background service default to starting (both are detached
    and return the terminal); local defaults to printing (a foreground start
    seizes the terminal).
    """
    if hosting is HostingOption.DOCKER:
        return [_DOCKER_START, _DOCKER_PRINT]
    if hosting is HostingOption.SERVICE:
        return [_SERVICE_INSTALL, _SERVICE_PRINT]
    return [_LOCAL_PRINT, _LOCAL_START]


def start_now_applies(state: "WizardState") -> bool:
    """For every hosting shape, unless a flag already chose the CLI handoff."""
    return state.next_action is not NextAction.CLI


def _get_initial(state: "WizardState") -> NextAction:
    chosen = state.extras.get("start_now")
    if isinstance(chosen, NextAction):
        return chosen
    if chosen is not None:
        try:
            return NextAction(chosen)
        except ValueError:
            pass  # stored value is not a valid NextAction; fall back to the shape default
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
