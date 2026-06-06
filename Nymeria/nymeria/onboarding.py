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
    """How the slim backend is hosted on this machine (first-run step 1)."""

    LOCAL = "local"
    SERVICE = "service"
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


class ImageTier(StrEnum):
    """Which binaries are baked into a container image (first-run, container hosts).

    Tool availability is a property of the image, not the runtime shape: the
    roughly 1,250 optional tools are in-process Python and run on any base. Only a
    small subset shells out to system binaries, so the image choice is three rungs
    rather than a minimal-versus-Kali binary. Placeholder until image generation
    is wired into finalize.
    """

    MINIMAL = "minimal"
    STANDARD = "standard"
    FULL = "full"


class SecurityProfile(StrEnum):
    """First-run security posture. Recorded now; enforcement is built out later.

    Per-tool-call approval gating does not exist yet, so this is a design-forward
    placeholder: the wizard captures the operator's intent so the future approval
    gate, default-bound tools, and bash sandboxing can each read it as built.
    """

    SECURE = "secure"
    STANDARD = "standard"
    UNLEASHED = "unleashed"


class ExternalAccess(StrEnum):
    """How the backend is reached from outside this machine (first-run guidance).

    The wizard cannot fully automate Tailscale or Cloudflare (both need
    interactive browser auth), so this records the chosen path and finalize prints
    the matching guidance. Placeholder until remote-access automation lands.
    """

    LOCAL_ONLY = "local_only"
    TAILSCALE = "tailscale"
    CLOUDFLARE = "cloudflare"
    CHAT_BOTS = "chat_bots"


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
    HostingOption.LOCAL,
    HostingOption.SERVICE,
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
    HostingOption.LOCAL: OnboardingChoice(
        value=HostingOption.LOCAL,
        label="Run on this machine",
        description=(
            "Runs the slim backend as a normal process on this computer with "
            "your user permissions. Simplest option and good for trying "
            "Nymeria. You start it yourself with `nymeria slim`."
        ),
        recommended=True,
    ),
    HostingOption.SERVICE: OnboardingChoice(
        value=HostingOption.SERVICE,
        label="Background service",
        description=(
            "Installs a background service (a systemd user unit on Linux, a "
            "launchd agent on macOS) so the slim backend starts on login and "
            "keeps running. Best for always-on use."
        ),
    ),
    HostingOption.DOCKER: OnboardingChoice(
        value=HostingOption.DOCKER,
        label="Docker (single container)",
        description=(
            "Runs the slim backend inside one Docker container with explicit "
            "volumes. More isolation and easy to reset, and it requires "
            "Docker to be installed."
        ),
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
        description="Show the API start command and local frontend URL after setup.",
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

IMAGE_TIER_ORDER = (
    ImageTier.MINIMAL,
    ImageTier.STANDARD,
    ImageTier.FULL,
)

IMAGE_TIER_CHOICES = {
    ImageTier.MINIMAL: OnboardingChoice(
        value=ImageTier.MINIMAL,
        label="Minimal (Debian)",
        description=(
            "The roughly 1,250 in-process Python tools, no browser, security, or "
            "CLI binaries. Smallest image and the safest default."
        ),
        recommended=True,
    ),
    ImageTier.STANDARD: OnboardingChoice(
        value=ImageTier.STANDARD,
        label="Standard (Debian plus browser, CLI, ffmpeg)",
        description=(
            "Adds Node, Chromium/Playwright, the Claude Code CLI, and ffmpeg for "
            "full everyday capability including browser automation, without the "
            "Kali heft."
        ),
    ),
    ImageTier.FULL: OnboardingChoice(
        value=ImageTier.FULL,
        label="Full (Kali security toolchain)",
        description=(
            "Adds the pentest toolchain (nmap, sqlmap, hashcat, wordlists). "
            "Heaviest at 2 to 3 GB, worth it only for security work."
        ),
        advanced=True,
    ),
}

SECURITY_PROFILE_ORDER = (
    SecurityProfile.SECURE,
    SecurityProfile.STANDARD,
    SecurityProfile.UNLEASHED,
)

SECURITY_PROFILE_CHOICES = {
    SecurityProfile.SECURE: OnboardingChoice(
        value=SecurityProfile.SECURE,
        label="Secure",
        description=(
            "Dangerous tools disabled by default, medium-risk tools require "
            "approval. Strictest posture. (Enforcement is being built out.)"
        ),
    ),
    SecurityProfile.STANDARD: OnboardingChoice(
        value=SecurityProfile.STANDARD,
        label="Standard",
        description=(
            "Full toolset, approval prompts only for risky tools. Balanced "
            "default. (Enforcement is being built out.)"
        ),
        recommended=True,
    ),
    SecurityProfile.UNLEASHED: OnboardingChoice(
        value=SecurityProfile.UNLEASHED,
        label="Unleashed",
        description=(
            "No approval gates, full autonomy, and the self-improve Skill Kit "
            "bound by default. Use only inside a sandbox. (Enforcement is being "
            "built out.)"
        ),
        advanced=True,
    ),
}

EXTERNAL_ACCESS_ORDER = (
    ExternalAccess.LOCAL_ONLY,
    ExternalAccess.TAILSCALE,
    ExternalAccess.CLOUDFLARE,
    ExternalAccess.CHAT_BOTS,
)

EXTERNAL_ACCESS_CHOICES = {
    ExternalAccess.LOCAL_ONLY: OnboardingChoice(
        value=ExternalAccess.LOCAL_ONLY,
        label="Local only",
        description=(
            "Reachable only from this machine. You can add remote access later."
        ),
        recommended=True,
    ),
    ExternalAccess.TAILSCALE: OnboardingChoice(
        value=ExternalAccess.TAILSCALE,
        label="Tailscale",
        description=(
            "Private mesh VPN: zero public exposure, automatic HTTPS, no domain. "
            "Recommended for single-user remote access. (Set up separately.)"
        ),
    ),
    ExternalAccess.CLOUDFLARE: OnboardingChoice(
        value=ExternalAccess.CLOUDFLARE,
        label="Cloudflare tunnel",
        description=(
            "Public hostname via a Cloudflare named or quick tunnel. Needs a "
            "Cloudflare account and dashboard ingress. (Set up separately.)"
        ),
    ),
    ExternalAccess.CHAT_BOTS: OnboardingChoice(
        value=ExternalAccess.CHAT_BOTS,
        label="Chat-app bots only",
        description=(
            "Reach Nymeria through Discord, Telegram, and other bots with no "
            "inbound networking at all."
        ),
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
