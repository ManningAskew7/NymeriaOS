"""Step 1: how to host the slim backend on this machine."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...onboarding import HOSTING_CHOICES, HOSTING_ORDER, HostingOption
from ..environment import hosting_gates
from ..nav import Step
from ..state import WizardState
from .base import Choice, SingleSelectStep

if TYPE_CHECKING:
    from ..app import SetupWizardApp


def hosting_choices_for(state: WizardState) -> list[Choice]:
    """Detection-aware choices: grey out impossible shapes, warn on degraded.

    The "(recommended)" tag follows the detected recommendation (the static
    onboarding metadata is no longer consulted for it), and the
    "(unavailable: ...)" suffix lives in the wizard layer, like the deployment
    step's "to come" tags, so review and finalize echoes stay clean. Without a
    cached report (state.env_report is None) every shape stays available and
    LOCAL keeps the recommendation, matching the pre-detection behavior.
    """
    report = state.env_report
    gates = hosting_gates(report) if report is not None else {}
    recommended = (
        report.recommended_hosting if report is not None else HostingOption.LOCAL
    )
    choices: list[Choice] = []
    for option in HOSTING_ORDER:
        meta = HOSTING_CHOICES[option]
        gate = gates.get(option)
        disabled = False
        label = meta.label
        description = meta.description
        if gate is not None:
            disabled = gate.disabled
            if gate.disabled:
                label += f" (unavailable: {gate.reason})"
            elif gate.warning:
                description = f"{description} Warning: {gate.warning}"
        if option is recommended and not disabled:
            label = f"{meta.label} (recommended)"
        choices.append(
            Choice(
                value=option, label=label, description=description, disabled=disabled
            )
        )
    return choices


def make_hosting_step() -> Step:
    def get_initial(state: WizardState) -> HostingOption:
        if state.hosting is not None:
            return state.hosting
        report = state.env_report
        return report.recommended_hosting if report is not None else HostingOption.LOCAL

    def store(state: WizardState, value: HostingOption) -> None:
        state.hosting = value

    def build(wizard: "SetupWizardApp", number: int, total: int) -> SingleSelectStep:
        # Built per-show (not at factory time) so the choices see the hydrated
        # state and the cached environment report.
        state = wizard.state
        note = "You can change this later by re-running setup."
        report = state.env_report
        gate = (
            hosting_gates(report).get(state.hosting)
            if report is not None and state.hosting is not None
            else None
        )
        if gate is not None and gate.disabled:
            # A hydrated shape this host can no longer run: the row is greyed
            # out and the selection falls to another shape, so say so rather
            # than silently flipping it on Enter.
            note = (
                "Heads up: your current hosting shape is unavailable here "
                f"({gate.reason}), so advancing selects a different one."
            )
        return SingleSelectStep(
            wizard,
            number,
            total,
            step_id="hosting",
            title="How should Nymeria run on this machine?",
            note=note,
            choices=hosting_choices_for(state),
            get_initial=get_initial,
            store=store,
        )

    return Step(id="hosting", applies=lambda _state: True, build=build)


__all__ = ["make_hosting_step", "hosting_choices_for"]
