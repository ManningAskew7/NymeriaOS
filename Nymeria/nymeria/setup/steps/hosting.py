"""Step 1: how to host the slim backend on this machine."""

from __future__ import annotations

from ...onboarding import HOSTING_CHOICES, HOSTING_ORDER, HostingOption
from ..nav import Step
from ..state import WizardState
from .base import Choice, single_select_step


def make_hosting_step() -> Step:
    choices: list[Choice] = []
    for option in HOSTING_ORDER:
        meta = HOSTING_CHOICES[option]
        label = meta.label + (" (recommended)" if meta.recommended else "")
        choices.append(Choice(value=option, label=label, description=meta.description))

    def get_initial(state: WizardState) -> HostingOption:
        return state.hosting or HostingOption.LOCAL

    def store(state: WizardState, value: HostingOption) -> None:
        state.hosting = value

    return single_select_step(
        step_id="hosting",
        title="How should Nymeria run on this machine?",
        note="You can change this later by re-running setup.",
        choices=choices,
        get_initial=get_initial,
        store=store,
    )


__all__ = ["make_hosting_step"]
