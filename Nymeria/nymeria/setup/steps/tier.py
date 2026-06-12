"""Setup-tier chooser: Quickstart vs Full vs the desktop-app hand-off.

The first decision after welcome. Picking Quickstart flips `state.quick` and
seeds the quick defaults right away (so later screens and the review reflect
them); switching back to Full unwinds exactly what the quick path seeded, so a
full walk starts from the normal step defaults. The Desktop option renders
greyed out "(to come)" until the desktop app's in-app onboarding wizard ships.

The `--quick` / `--custom` flags preset the tier and skip this screen entirely
(`state.tier_locked`), keeping scripted and non-interactive runs unchanged.
"""

from __future__ import annotations

from ...onboarding import (
    SETUP_TIER_CHOICES,
    SETUP_TIER_ORDER,
    OnboardingChoice,
    SetupTier,
)
from ..nav import Step
from ..quick import apply_quick_defaults, unapply_quick_defaults
from ..state import WizardState
from .base import Choice, single_select_step


def _choices() -> list[Choice]:
    out: list[Choice] = []
    for option in SETUP_TIER_ORDER:
        meta: OnboardingChoice = SETUP_TIER_CHOICES[option]
        label = meta.label + (" (recommended)" if meta.recommended else "")
        if meta.coming_soon:
            label += " (to come)"
        out.append(
            Choice(
                value=option,
                label=label,
                description=meta.description,
                disabled=meta.coming_soon,
            )
        )
    return out


def store_tier_choice(state: WizardState, value: SetupTier) -> None:
    """Record the tier and apply/unwind the quick defaults on transitions.

    Both helpers are idempotent and seed-aware, so chooser round-trips
    (quickstart -> back -> full -> back -> quickstart) never clobber values
    the user set via flags or that hydrate restored from disk.
    """
    if value is SetupTier.QUICKSTART:
        if not state.quick:
            state.quick = True
            apply_quick_defaults(state)
    elif state.quick:
        state.quick = False
        unapply_quick_defaults(state)
    state.extras["tier"] = value.value


def initial_tier(state: WizardState) -> SetupTier:
    """The chooser's preselected tier.

    Reconfigures preselect Full: an existing user re-running init to tweak
    something must not Enter-confirm into quick mode, whose seeds would fill
    capabilities they deliberately left unconfigured (an absent voice provider
    or search backend hydrates as nothing, which is indistinguishable from
    "never asked"). Picking Quickstart explicitly still applies the documented
    quick defaults. Fresh installs preselect Quickstart (the recommendation).
    """
    stored = state.extras.get("tier")
    if isinstance(stored, str):
        try:
            return SetupTier(stored)
        except ValueError:
            pass  # unknown stored value: fall through to the computed default
    return SetupTier.FULL if state.reconfigure else SetupTier.QUICKSTART


def make_setup_tier_step() -> Step:
    def applies(state: WizardState) -> bool:
        return not state.tier_locked

    return single_select_step(
        step_id="tier",
        title="How much do you want to configure now?",
        note=(
            "Quickstart asks only the essentials and picks free, keyless "
            "defaults for the rest; every default can be changed later in "
            "the app or by re-running setup. Full setup walks every step."
        ),
        choices=_choices(),
        get_initial=initial_tier,
        store=store_tier_choice,
        applies=applies,
    )


__all__ = ["initial_tier", "make_setup_tier_step", "store_tier_choice"]
