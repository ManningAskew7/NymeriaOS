"""Deployment-shaping steps: Docker stack, security profile, remote access.

These mirror the high-level decisions in the setup-wizard plan. The Docker-stack
choice (slim vs full) is wired through finalize; security profile and remote
access are framework-real placeholders (single-select, stored on `WizardState`,
shown in review) until the approval gate and remote-access automation land. Each
choice carries a sensible default so a quick run can Enter straight through.
"""

from __future__ import annotations

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
        out.append(Choice(value=option, label=label, description=meta.description))
    return out


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
        choices=_choices(DOCKER_STACK_ORDER, DOCKER_STACK_CHOICES),
        get_initial=get_initial,
        store=store,
        applies=applies,
    )


def make_security_profile_step() -> Step:
    """First-run security posture. Recorded now; enforcement is built out later."""

    def get_initial(state: WizardState) -> SecurityProfile:
        return state.security_profile or SecurityProfile.STANDARD

    def store(state: WizardState, value: SecurityProfile) -> None:
        state.security_profile = value

    return single_select_step(
        step_id="security_profile",
        title="Security profile",
        note=(
            "How much the agent can do without approval. Recorded now; the "
            "approval gate and tool defaults read it as they are built."
        ),
        choices=_choices(SECURITY_PROFILE_ORDER, SECURITY_PROFILE_CHOICES),
        get_initial=get_initial,
        store=store,
    )


def make_external_access_step() -> Step:
    """How to reach the backend from outside this machine. Placeholder guidance."""

    def get_initial(state: WizardState) -> ExternalAccess:
        return state.external_access or ExternalAccess.LOCAL_ONLY

    def store(state: WizardState, value: ExternalAccess) -> None:
        state.external_access = value

    return single_select_step(
        step_id="external_access",
        title="External access",
        note=(
            "How you will reach Nymeria remotely. Placeholder: the wizard records "
            "your choice and prints the matching setup guidance at the end."
        ),
        choices=_choices(EXTERNAL_ACCESS_ORDER, EXTERNAL_ACCESS_CHOICES),
        get_initial=get_initial,
        store=store,
    )


__all__ = [
    "make_docker_stack_step",
    "make_security_profile_step",
    "make_external_access_step",
]
