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
    """Supported ways to authenticate the primary LLM provider.

    `CLIPROXY_OAUTH` is the provider-generic subscription branch (the concrete
    CLI is a separate pick from the CLIProxy catalog). `LOCAL_MODEL` is the
    no-auth local branch: it pins the provider to Ollama and skips the
    provider picker (there is no key to collect). The two legacy per-provider
    values stay valid inputs because the desktop setup flow hardcodes them;
    `legacy_cliproxy_provider` maps them onto the generic branch.
    """

    API_KEY = "api_key"
    CLIPROXY_OAUTH = "cliproxy_oauth"
    LOCAL_MODEL = "local_model"
    CLIPROXY_CLAUDE_OAUTH = "cliproxy_claude_oauth"
    CLIPROXY_CODEX_OAUTH = "cliproxy_codex_oauth"


# Legacy per-provider auth methods -> the CLIProxy catalog id they pin.
LEGACY_CLIPROXY_AUTH_PROVIDERS: dict[ProviderAuthMethod, str] = {
    ProviderAuthMethod.CLIPROXY_CLAUDE_OAUTH: "claude",
    ProviderAuthMethod.CLIPROXY_CODEX_OAUTH: "codex",
}


def legacy_cliproxy_provider(method: "ProviderAuthMethod") -> str | None:
    """The pinned CLIProxy provider id for a legacy auth method, else None."""
    return LEGACY_CLIPROXY_AUTH_PROVIDERS.get(method)


class SetupStyle(StrEnum):
    """Amount of configuration detail requested during onboarding."""

    RECOMMENDED = "recommended"
    ADVANCED = "advanced"


class SetupTier(StrEnum):
    """Wizard depth, chosen on the screen after welcome (or preset via flags).

    QUICKSTART walks only the irreducible questions (hosting, the LLM, a
    timezone confirm, external access, start now) and defaults the rest with
    free, keyless picks (see setup/quick.py). FULL walks every step. DESKTOP
    is the planned hand-off to the desktop app's in-app onboarding wizard;
    until that ships it renders greyed out "(to come)". The `--quick` and
    `--custom` flags preset QUICKSTART/FULL and skip the chooser screen, so
    scripted runs behave exactly as before.
    """

    QUICKSTART = "quickstart"
    FULL = "full"
    DESKTOP = "desktop"


class NextAction(StrEnum):
    """Post-setup action offered after config is written."""

    START_API_OPEN_FRONTEND = "start_api_open_frontend"
    PRINT_COMMANDS = "print_commands"
    CLI = "cli"


class DockerStack(StrEnum):
    """Which Docker runtime shape to deploy (first-run, Docker hosts).

    This is the Axis-1 topology choice, not an image-toolset choice: both shapes
    run the same agent and ship the same lean image. SLIM is one container on
    SQLite with an in-memory bus (the containerized twin of the local install);
    FULL is the Postgres + Redis multi-container stack, functionally identical but
    with multi-user support, scaling headroom, and per-container fault isolation.
    """

    SLIM = "slim"
    FULL = "full"


class SecurityProfile(StrEnum):
    """First-run security posture. Recorded now; enforcement is built out later.

    Per-tool-call approval gating does not exist yet, so the wizard offers only
    UNLEASHED (the current behavior) and shows SECURE and STANDARD greyed out
    as "to come". The full enforcement design (approval gate, always-allow
    lists, slimmed Secure defaults) lives in docs/private/security/security-profiles.md.
    """

    SECURE = "secure"
    STANDARD = "standard"
    UNLEASHED = "unleashed"


class ExternalAccess(StrEnum):
    """How the backend is reached from outside this machine.

    Tailscale and Cloudflare get a guided setup step in the wizard (detect the
    tool, drive login/provisioning, expose the backend, verify the public URL
    end to end including streaming) and the resolved origin is written to
    NYMERIA_PUBLIC_URL and CORS_ORIGINS at finalize. Chat bots need no inbound
    networking at all; local-only writes nothing.
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
    # Shown greyed-out and unselectable in the wizard with a "(to come)" label
    # suffix: the choice exists in the design but its enforcement is not built.
    coming_soon: bool = False


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

# Wizard-only round-trip marker for the hosting choice (like
# NYMERIA_EXTERNAL_ACCESS): written to the env file by finalize, read back by
# hydrate so LOCAL vs SERVICE sticks across reconfigures, ignored by the
# runtime settings model.
HOSTING_MARKER_ENV = "NYMERIA_HOSTING"

# Visible order in the auth step: three equal, capability-gated paths (the
# beta-readiness 03 flattening). The generic subscription branch replaced the
# two legacy per-provider rows (which stay valid enum inputs, just not shown).
PROVIDER_AUTH_METHOD_ORDER = (
    ProviderAuthMethod.API_KEY,
    ProviderAuthMethod.CLIPROXY_OAUTH,
    ProviderAuthMethod.LOCAL_MODEL,
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
        label="Docker",
        description=(
            "Runs the backend in Docker with explicit volumes. More isolation and "
            "easy to reset, and it requires Docker to be installed. You choose the "
            "slim or full stack next."
        ),
    ),
}

PROVIDER_AUTH_METHOD_CHOICES = {
    ProviderAuthMethod.API_KEY: OnboardingChoice(
        value=ProviderAuthMethod.API_KEY,
        label="Direct API key",
        description=(
            "Pick a provider (Anthropic, OpenAI, Google Gemini, OpenRouter, "
            "and many more) and paste an API key. Several providers have "
            "free tiers; the picker shows how to get each key."
        ),
    ),
    ProviderAuthMethod.CLIPROXY_OAUTH: OnboardingChoice(
        value=ProviderAuthMethod.CLIPROXY_OAUTH,
        label="Subscription login via CLIProxy",
        description=(
            "Route an existing AI subscription (Claude Max/Pro, ChatGPT "
            "Plus/Pro, Gemini, Kimi, Grok, and more) through a CLIProxy "
            "deployment instead of paying per token. Carries "
            "terms-of-service risk; a disclaimer follows."
        ),
    ),
    ProviderAuthMethod.LOCAL_MODEL: OnboardingChoice(
        value=ProviderAuthMethod.LOCAL_MODEL,
        label="Local model (Ollama)",
        description=(
            "Run a free open model on this machine with Ollama "
            "(ollama.com). No account or API key, and chats stay local. "
            "Expect a multi-GB model download; a capable machine (16GB+ "
            "RAM or a GPU) makes it comfortable."
        ),
    ),
    ProviderAuthMethod.CLIPROXY_CLAUDE_OAUTH: OnboardingChoice(
        value=ProviderAuthMethod.CLIPROXY_CLAUDE_OAUTH,
        label="CLIProxy Claude OAuth (legacy)",
        description=(
            "Legacy alias for the generic CLIProxy branch pinned to Claude."
        ),
        advanced=True,
    ),
    ProviderAuthMethod.CLIPROXY_CODEX_OAUTH: OnboardingChoice(
        value=ProviderAuthMethod.CLIPROXY_CODEX_OAUTH,
        label="CLIProxy Codex/OpenAI OAuth (legacy)",
        description=(
            "Legacy alias for the generic CLIProxy branch pinned to Codex."
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

SETUP_TIER_ORDER = (
    SetupTier.QUICKSTART,
    SetupTier.FULL,
    SetupTier.DESKTOP,
)

SETUP_TIER_CHOICES = {
    SetupTier.QUICKSTART: OnboardingChoice(
        value=SetupTier.QUICKSTART,
        label="Quickstart",
        description=(
            "The fastest path: how to host it, your LLM, your timezone, and "
            "remote access. Everything else gets free, keyless defaults "
            "(local semantic memory, keyless web search and fetch, all skill "
            "kits) that you can change later in the app."
        ),
        recommended=True,
    ),
    SetupTier.FULL: OnboardingChoice(
        value=SetupTier.FULL,
        label="Full setup",
        description=(
            "Walk every step: tool families, semantic memory, image "
            "generation, voice, skill kits, context tuning, and agent limits."
        ),
    ),
    SetupTier.DESKTOP: OnboardingChoice(
        value=SetupTier.DESKTOP,
        label="Set up in the desktop app",
        description=(
            "Minimal terminal setup, then finish onboarding in the Nymeria "
            "desktop app's guided wizard."
        ),
        coming_soon=True,
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

DOCKER_STACK_ORDER = (
    DockerStack.SLIM,
    DockerStack.FULL,
)

DOCKER_STACK_CHOICES = {
    DockerStack.SLIM: OnboardingChoice(
        value=DockerStack.SLIM,
        label="Slim (single container)",
        description=(
            "One container running the slim backend on SQLite with an in-memory "
            "event bus. The containerized twin of a local install. Simplest, and "
            "the right default for a single user."
        ),
        recommended=True,
    ),
    DockerStack.FULL: OnboardingChoice(
        value=DockerStack.FULL,
        label="Full (Postgres plus Redis stack)",
        description=(
            "The multi-container stack: Postgres for durable checkpoints and Redis "
            "for the event bus, plus separate worker and MCP containers. Same "
            "features as slim, with better multi-user support, scaling headroom, "
            "and per-container fault isolation. Heavier; needs more resources."
        ),
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
            "approval. Strictest posture. (Not selectable yet; ships with the "
            "approval gate.)"
        ),
        coming_soon=True,
    ),
    SecurityProfile.STANDARD: OnboardingChoice(
        value=SecurityProfile.STANDARD,
        label="Standard",
        description=(
            "Full toolset, approval prompts only for risky tools. Balanced "
            "default. (Not selectable yet; ships with the approval gate.)"
        ),
        coming_soon=True,
    ),
    SecurityProfile.UNLEASHED: OnboardingChoice(
        value=SecurityProfile.UNLEASHED,
        label="Unleashed",
        description=(
            "No approval gates, full autonomy, and the capability-expansion kits "
            "(tool, skill, MCP, and credential management) enabled by default. Use "
            "only inside a sandbox. Currently the only selectable profile; Secure "
            "and Standard arrive with the approval gate."
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
            "Private mesh VPN: zero public exposure, automatic HTTPS, no "
            "domain. Recommended for single-user remote access; your devices "
            "run the Tailscale app. The wizard sets it up on the next step."
        ),
    ),
    ExternalAccess.CLOUDFLARE: OnboardingChoice(
        value=ExternalAccess.CLOUDFLARE,
        label="Cloudflare tunnel",
        description=(
            "Public HTTPS hostname via a Cloudflare named tunnel: works in "
            "any browser with no extra apps. Needs a free Cloudflare account, "
            "a domain on Cloudflare DNS, and an API token. The wizard drives "
            "it on the next step (or accepts an existing public URL)."
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
