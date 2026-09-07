"""Deployment-shaping steps: Docker stack, security profile, remote access.

These mirror the high-level decisions in the setup-wizard plan. The Docker-stack
choice (slim vs full) is wired through finalize. The external-access choice
gates the guided tailscale/cloudflare setup steps (steps/external_access.py).
Security profile offers Unleashed only for now: Secure and Standard render
greyed out as "to come" until the approval gate lands (design:
docs/private/security/security-profiles.md). Each choice carries a sensible default so
a quick run can Enter straight through.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from ...onboarding import (
    DOCKER_STACK_CHOICES,
    DOCKER_STACK_ORDER,
    EXTERNAL_ACCESS_CHOICES,
    EXTERNAL_ACCESS_ORDER,
    SECURITY_PROFILE_CHOICES,
    SECURITY_PROFILE_ORDER,
    DockerStack,
    ExternalAccess,
    HostingOption,
    OnboardingChoice,
    SecurityProfile,
)
from ..nav import Step
from ..state import WizardState
from .base import Choice, single_select_step


def _choices(order: tuple, table: Mapping) -> list[Choice]:
    out: list[Choice] = []
    for option in order:
        meta: OnboardingChoice = table[option]
        label = meta.label + (" (recommended)" if meta.recommended else "")
        # The "(to come)" suffix lives here, in the wizard layer, so review and
        # finalize echoes of the clean OnboardingChoice label stay unsuffixed.
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


def _docker_stack_choices() -> list[Choice]:
    """The stack choices, with Full gated on a source checkout.

    A clone-free (pip/uv) install can run the slim shape from the wheel-bundled
    published-image compose, but the full Postgres + Redis compose builds its
    images from the repo, so without a checkout it is shown greyed out (the
    same rendering as "to come" choices) instead of failing in finalize.
    """
    from ..environment import source_checkout_root

    choices = _choices(DOCKER_STACK_ORDER, DOCKER_STACK_CHOICES)
    if source_checkout_root() is not None:
        return choices
    return [
        replace(
            choice,
            label=choice.label + " (needs a source checkout)",
            disabled=True,
        )
        if choice.value is DockerStack.FULL
        else choice
        for choice in choices
    ]


def make_docker_stack_step() -> Step:
    """Docker runtime shape: slim single container vs full Postgres+Redis stack.

    Only applies to Docker hosts. Both shapes run the same agent on the same lean
    image; this is the Axis-1 topology choice (see onboarding.DockerStack).
    """

    def get_initial(state: WizardState) -> DockerStack:
        return state.docker_stack or DockerStack.SLIM

    def store(state: WizardState, value: DockerStack) -> None:
        state.docker_stack = value

    def applies(state: WizardState) -> bool:
        return state.hosting is HostingOption.DOCKER

    return single_select_step(
        step_id="docker_stack",
        title="Docker stack",
        note=(
            "Slim runs one container on SQLite (the containerized local install). "
            "Full runs the Postgres + Redis stack for multi-user support and "
            "scaling. Same features either way."
        ),
        choices=_docker_stack_choices(),
        get_initial=get_initial,
        store=store,
        applies=applies,
    )


def make_security_profile_step() -> Step:
    """First-run security posture. Unleashed-only until the approval gate lands.

    Secure and Standard are shown greyed out with a "(to come)" suffix; their
    enforcement design lives in docs/private/security/security-profiles.md.
    """

    def get_initial(state: WizardState) -> SecurityProfile:
        return state.security_profile or SecurityProfile.UNLEASHED

    def store(state: WizardState, value: SecurityProfile) -> None:
        state.security_profile = value

    return single_select_step(
        step_id="security_profile",
        title="Security profile",
        note=(
            "How much the agent can do without approval. Unleashed is the "
            "current behavior and for now the only selectable profile; Secure "
            "and Standard arrive with the per-tool approval gate."
        ),
        choices=_choices(SECURITY_PROFILE_ORDER, SECURITY_PROFILE_CHOICES),
        get_initial=get_initial,
        store=store,
    )


def store_external_access_choice(state: WizardState, value: ExternalAccess) -> None:
    """Store the choice; CHANGING it retires the prior setup's outputs.

    A public URL produced by an earlier tailscale/cloudflare run (or hydrated
    from disk) must not survive into a different choice, because finalize
    writes whatever `public_url` holds. An already-enabled tunnel itself is
    not torn down here; the finalize summary owns telling the user what is
    still running.
    """
    if state.external_access is not None and state.external_access is not value:
        state.public_url = ""
        state.public_url_verified = False
        state.tailscale_exposure = ""
        state.cloudflare_tunnel_token = ""
    state.external_access = value


def make_external_access_step() -> Step:
    """How to reach the backend from outside this machine."""

    def get_initial(state: WizardState) -> ExternalAccess:
        return state.external_access or ExternalAccess.LOCAL_ONLY

    def store(state: WizardState, value: ExternalAccess) -> None:
        store_external_access_choice(state, value)

    return single_select_step(
        step_id="external_access",
        title="External access",
        note=(
            "How you will reach Nymeria remotely. Tailscale and Cloudflare get "
            "a guided setup on the next step; the resulting URL is checked "
            "(health, and streaming once the backend answers) and written to "
            "the config."
        ),
        choices=_choices(EXTERNAL_ACCESS_ORDER, EXTERNAL_ACCESS_CHOICES),
        get_initial=get_initial,
        store=store,
    )


__all__ = [
    "make_docker_stack_step",
    "make_security_profile_step",
    "make_external_access_step",
    "store_external_access_choice",
]
