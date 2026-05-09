"""Data model for first-run Nymeria onboarding choices.

This module intentionally stays independent of the interactive chat CLI. The
setup wizard, frontend setup flows, and tests can share these plain structures
without importing or depending on CLI internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar


class HostingOption(StrEnum):
    """Supported runtime hosting profiles for first-run setup."""

    BARE_METAL = "bare_metal"
    VENV = "venv"
    DOCKER = "docker"


class ProviderAuthMethod(StrEnum):
    """Supported ways to authenticate the primary LLM provider."""

    API_KEY = "api_key"
    CLIPROXY_CLAUDE_OAUTH = "cliproxy_claude_oauth"
    CLIPROXY_CODEX_OAUTH = "cliproxy_codex_oauth"


class SetupStyle(StrEnum):
    """Amount of configuration detail requested during onboarding."""

    RECOMMENDED = "recommended"
    ADVANCED = "advanced"


class NextAction(StrEnum):
    """Post-setup action offered after config is written."""

    START_API_OPEN_FRONTEND = "start_api_open_frontend"
    PRINT_COMMANDS = "print_commands"
    CLI = "cli"


@dataclass(frozen=True)
class ProviderOption:
    name: str
    label: str
    env_var: str
    key_prefix: str
    default_model: str


@dataclass(frozen=True)
class OnboardingChoice:
    value: StrEnum
    label: str
    description: str
    recommended: bool = False
    advanced: bool = False


@dataclass(frozen=True)
class OnboardingSelection:
    hosting: HostingOption
    auth_method: ProviderAuthMethod
    setup_style: SetupStyle
    next_action: NextAction


HOSTING_ORDER = (
    HostingOption.BARE_METAL,
    HostingOption.VENV,
    HostingOption.DOCKER,
)

PROVIDER_AUTH_METHOD_ORDER = (
    ProviderAuthMethod.API_KEY,
    ProviderAuthMethod.CLIPROXY_CLAUDE_OAUTH,
    ProviderAuthMethod.CLIPROXY_CODEX_OAUTH,
)

SETUP_STYLE_ORDER = (
    SetupStyle.RECOMMENDED,
    SetupStyle.ADVANCED,
)

NEXT_ACTION_ORDER = (
    NextAction.START_API_OPEN_FRONTEND,
    NextAction.PRINT_COMMANDS,
    NextAction.CLI,
)

HOSTING_CHOICES = {
    HostingOption.BARE_METAL: OnboardingChoice(
        value=HostingOption.BARE_METAL,
        label="Bare metal",
        description=(
            "Runs directly on your PC with your user permissions. It can "
            "control your machine and access files your account can access "
            "when tools are enabled. Most power, least isolation."
        ),
    ),
    HostingOption.VENV: OnboardingChoice(
        value=HostingOption.VENV,
        label="Python virtual environment / pipx",
        description=(
            "Runs as a normal user Python process with isolated Python "
            "packages. It is not an OS security sandbox; Nymeria can still "
            "access files your user can access when tools are enabled."
        ),
    ),
    HostingOption.DOCKER: OnboardingChoice(
        value=HostingOption.DOCKER,
        label="Docker",
        description=(
            "Runs backend services in containers with explicit volumes and "
            "network boundaries. More setup, easier to reset, and the best "
            "isolation of these options."
        ),
        recommended=True,
    ),
}

PROVIDER_AUTH_METHOD_CHOICES = {
    ProviderAuthMethod.API_KEY: OnboardingChoice(
        value=ProviderAuthMethod.API_KEY,
        label="Direct API key",
        description="Use an Anthropic, OpenAI, or OpenRouter API key directly.",
    ),
    ProviderAuthMethod.CLIPROXY_CLAUDE_OAUTH: OnboardingChoice(
        value=ProviderAuthMethod.CLIPROXY_CLAUDE_OAUTH,
        label="CLIProxy Claude OAuth",
        description=(
            "Advanced path for Anthropic-compatible routing through a separate "
            "CLIProxy deployment."
        ),
        advanced=True,
    ),
    ProviderAuthMethod.CLIPROXY_CODEX_OAUTH: OnboardingChoice(
        value=ProviderAuthMethod.CLIPROXY_CODEX_OAUTH,
        label="CLIProxy Codex/OpenAI OAuth",
        description=(
            "Advanced path for OpenAI-compatible routing through a separate "
            "CLIProxy deployment."
        ),
        advanced=True,
    ),
}

SETUP_STYLE_CHOICES = {
    SetupStyle.RECOMMENDED: OnboardingChoice(
        value=SetupStyle.RECOMMENDED,
        label="Recommended defaults",
        description="Write minimal safe config and defer optional capabilities.",
        recommended=True,
    ),
    SetupStyle.ADVANCED: OnboardingChoice(
        value=SetupStyle.ADVANCED,
        label="Advanced/manual configuration",
        description="Prompt for optional keys, paths, and provider details.",
    ),
}

NEXT_ACTION_CHOICES = {
    NextAction.START_API_OPEN_FRONTEND: OnboardingChoice(
        value=NextAction.START_API_OPEN_FRONTEND,
        label="Start backend and open web UI",
        description="Launch the API and open the local frontend after setup.",
    ),
    NextAction.PRINT_COMMANDS: OnboardingChoice(
        value=NextAction.PRINT_COMMANDS,
        label="Print commands only",
        description="Show the commands to run next without starting processes.",
    ),
    NextAction.CLI: OnboardingChoice(
        value=NextAction.CLI,
        label="Enter CLI chat command path",
        description="Launch or print the nymeria cli handoff command.",
    ),
}

ChoiceT = TypeVar("ChoiceT", bound=StrEnum)


def choice_values(enum_type: type[ChoiceT]) -> tuple[str, ...]:
    """Return stable string values for argparse choices and tests."""
    return tuple(item.value for item in enum_type)


def parse_choice(enum_type: type[ChoiceT], value: str, *, option_name: str) -> ChoiceT:
    """Parse a persisted or flag-provided onboarding choice."""
    try:
        return enum_type(value.strip())
    except ValueError as exc:
        allowed = ", ".join(choice_values(enum_type))
        raise ValueError(f"{option_name} must be one of: {allowed}") from exc
