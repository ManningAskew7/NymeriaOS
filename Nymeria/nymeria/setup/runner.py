"""Entry point for `nymeria init`.

Owns the CLI flag surface (shared with run.py), builds the initial WizardState,
and routes to either the interactive Textual wizard or the headless finalize
path. The Textual import is local so importing this module (and run.py) never
requires Textual unless an interactive setup is actually launched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

from ..onboarding import (
    DockerStack,
    ExternalAccess,
    HostingOption,
    NextAction,
    ProviderAuthMethod,
    SecurityProfile,
    choice_values,
    legacy_cliproxy_provider,
    parse_choice,
)
from ..cliproxy.catalog import list_cliproxy_providers
from ..config.llm_providers import get_llm_provider_spec, list_llm_provider_specs
from . import tuning_catalog, voice_catalog
from .environment import detect_environment, hosting_gates, stack_resource_warnings
from .finalize import finalize
from .quick import SECTION_DEPENDENCIES, apply_quick_defaults, validate_section_id
from .state import WizardState

DEFAULT_NEXT_ACTION = NextAction.PRINT_COMMANDS

_OPTIONAL_KEY_FLAGS = (
    ("embedding_api_key", "EMBEDDING_API_KEY"),
    ("openai_api_key", "OPENAI_API_KEY"),
    ("gemini_api_key", "GEMINI_API_KEY"),
    ("perplexity_api_key", "PERPLEXITY_API_KEY"),
    # Backend credentials for the web_search / fetch_url / image_gen families.
    ("tavily_api_key", "TAVILY_API_KEY"),
    ("exa_api_key", "EXA_API_KEY"),
    ("firecrawl_api_key", "FIRECRAWL_API_KEY"),
    ("brave_api_key", "BRAVE_API_KEY"),
    ("searxng_base_url", "SEARXNG_BASE_URL"),
    ("jina_api_key", "JINA_API_KEY"),
    ("bfl_api_key", "BFL_API_KEY"),
    ("replicate_api_key", "REPLICATE_API_KEY"),
    ("fal_api_key", "FAL_API_KEY"),
    # Voice backends (--tts / --stt picks).
    ("groq_api_key", "GROQ_API_KEY"),
    ("tts_api_key", "TTS_API_KEY"),
    ("tts_voice", "TTS_VOICE"),
)

# CLI flag -> state.extras family key, for seeding tool families non-interactively.
# (argparse dest is the first element with dashes turned to underscores.)
_FAMILY_FLAGS = (
    ("web_search", "web_search"),
    ("fetch_url", "fetch_url"),
    ("image_gen", "image_gen"),
    ("skill_kit", "skill_kits"),
)


def add_init_arguments(parser: argparse.ArgumentParser) -> None:
    """Define the `init` flag surface. Shared by run.py and this module's main()."""

    parser.add_argument(
        "section",
        nargs="?",
        default=None,
        help="Optional wizard section to jump to (for example: provider)",
    )
    parser.add_argument(
        "--provider",
        choices=tuple(spec.id for spec in list_llm_provider_specs()),
        metavar="PROVIDER",
        default=None,
        help="LLM provider id from the registry (e.g. anthropic, openai, openrouter)",
    )
    parser.add_argument("--model", default=None, help="Model identifier")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--base-url", default=None, help="OpenAI-compatible base URL")
    parser.add_argument(
        "--api-mode",
        choices=("responses", "chat_completions"),
        default=None,
        help="OpenAI-compatible API mode",
    )
    parser.add_argument(
        "--hosting",
        choices=choice_values(HostingOption),
        default=None,
        help="How to host the slim backend (local, service, docker)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "API port the backend listens on (default 8000; written as "
            "API_PORT, followed by printed URLs and health checks)"
        ),
    )
    parser.add_argument(
        "--auth-method",
        choices=choice_values(ProviderAuthMethod),
        default=None,
        help=(
            "LLM auth method: api_key (direct key) or cliproxy_oauth "
            "(subscription via CLIProxy; the legacy cliproxy_claude_oauth / "
            "cliproxy_codex_oauth values map onto it)"
        ),
    )
    parser.add_argument(
        "--cliproxy-provider",
        choices=tuple(spec.id for spec in list_cliproxy_providers()),
        default=None,
        help="CLIProxy subscription to route (with --auth-method cliproxy_oauth)",
    )
    parser.add_argument(
        "--cliproxy-management-url",
        default=None,
        help="CLIProxy host root URL (no /v1), e.g. http://localhost:8318",
    )
    parser.add_argument(
        "--cliproxy-management-key",
        default=None,
        help="CLIProxy remote-management secret (plaintext)",
    )
    parser.add_argument(
        "--cliproxy-gatekeeper-key",
        default=None,
        help=(
            "CLIProxy data-plane api-key (cpx-...) written as the LLM key. "
            "Optional when --cliproxy-management-key is given: setup then "
            "reads or mints one through the management API"
        ),
    )
    parser.add_argument(
        "--cliproxy-login",
        action="store_true",
        help=(
            "Log in to the subscription from this terminal without the TUI "
            "(with --non-interactive): prints the OAuth URL, reads a pasted "
            "redirect URL from stdin for browser flows, and polls device "
            "flows to completion. Blocks until the login finishes or the "
            "10-minute session expires"
        ),
    )
    parser.add_argument(
        "--cliproxy-auth-file",
        default=None,
        metavar="PATH",
        help=(
            "Upload an auth-file JSON to the proxy and register it without a "
            "restart (with --non-interactive). For restoring or MOVING a "
            "login only: OAuth refresh tokens are single-use per machine, so "
            "never upload a copy another live proxy still uses (both ends "
            "start failing with invalid_grant)"
        ),
    )
    parser.add_argument(
        "--docker-stack",
        choices=choice_values(DockerStack),
        default=None,
        help="Docker runtime shape for Docker hosts: slim or full (Postgres+Redis)",
    )
    parser.add_argument(
        "--security-profile",
        choices=choice_values(SecurityProfile),
        default=None,
        help=(
            "First-run security posture. Only 'unleashed' (the current "
            "behavior) is accepted until the approval gate ships"
        ),
    )
    parser.add_argument(
        "--external-access",
        choices=choice_values(ExternalAccess),
        default=None,
        help=(
            "How the backend is reached remotely. Interactive runs get a "
            "guided tailscale/cloudflare setup; non-interactive runs pair "
            "this with --public-url"
        ),
    )
    parser.add_argument(
        "--public-url",
        default=None,
        help=(
            "Public origin the backend is reached at remotely (e.g. "
            "https://nymeria.example.com); written to NYMERIA_PUBLIC_URL and "
            "added to CORS_ORIGINS"
        ),
    )
    parser.add_argument(
        "--next-action",
        choices=choice_values(NextAction),
        default=None,
        help="Post-setup handoff action",
    )
    parser.add_argument(
        "--start",
        action="store_true",
        help=(
            "After writing config, start the backend now (docker: detached + "
            "health wait; service: install + start + health wait; local: "
            "foreground). Default just prints the command."
        ),
    )
    parser.add_argument("--embedding-api-key", default=None)
    parser.add_argument("--openai-api-key", default=None)
    parser.add_argument("--gemini-api-key", default=None)
    parser.add_argument("--perplexity-api-key", default=None)
    # Backend credentials for the selected tool families.
    parser.add_argument("--tavily-api-key", default=None)
    parser.add_argument("--exa-api-key", default=None)
    parser.add_argument("--firecrawl-api-key", default=None)
    parser.add_argument("--brave-api-key", default=None)
    parser.add_argument("--searxng-base-url", default=None)
    parser.add_argument("--jina-api-key", default=None)
    parser.add_argument("--bfl-api-key", default=None)
    parser.add_argument("--replicate-api-key", default=None)
    parser.add_argument("--fal-api-key", default=None)
    # Seed tool families non-interactively (repeatable), e.g.
    # --web-search web_search_tavily --image-gen image_gen_gemini. The literal
    # value `none` clears the family (the scripted way to revert a reconfigured
    # install's picks back to empty).
    parser.add_argument(
        "--web-search", action="append", default=None, metavar="TOOL",
        help=(
            "Seed a web_search_* backend into the default tools (repeatable); "
            "'none' clears the family on a reconfigure"
        ),
    )
    parser.add_argument(
        "--fetch-url", action="append", default=None, metavar="TOOL",
        help=(
            "Seed a fetch_url backend into the default tools (repeatable); "
            "'none' clears the family on a reconfigure"
        ),
    )
    parser.add_argument(
        "--image-gen", action="append", default=None, metavar="TOOL",
        help=(
            "Seed an image_gen_* backend into the default tools (repeatable); "
            "'none' clears the family on a reconfigure"
        ),
    )
    parser.add_argument(
        "--skill-kit", action="append", default=None, metavar="KIT",
        help=(
            "Seed a default-on capability kit into enabled_global_skills "
            "(repeatable); 'none' clears the kit picks on a reconfigure"
        ),
    )
    # Voice providers (single picks; values match TTS_PROVIDER/STT_PROVIDER).
    parser.add_argument(
        "--tts",
        choices=tuple(choice.value for choice in voice_catalog.TTS_CHOICES),
        default=None,
        help="Text-to-speech provider (kokoro runs locally; none disables)",
    )
    parser.add_argument(
        "--stt",
        choices=tuple(choice.value for choice in voice_catalog.STT_CHOICES),
        default=None,
        help="Speech-to-text provider (faster-whisper runs locally; none disables)",
    )
    parser.add_argument("--groq-api-key", default=None)
    parser.add_argument("--tts-api-key", default=None)
    parser.add_argument(
        "--tts-voice", default=None,
        help="TTS voice identifier (required for cartesia: a voice UUID)",
    )
    # Agent tuning (single picks; numeric fine-tuning is wizard/settings-only).
    parser.add_argument(
        "--context",
        choices=tuple(choice.value for choice in tuning_catalog.CONTEXT_CHOICES),
        default=None,
        help="Context-management strategy (compact_tokens recommended)",
    )
    parser.add_argument(
        "--timezone", default=None, metavar="TZ",
        help="Your IANA timezone (e.g. Australia/Sydney); written as USER_TIMEZONE",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=tuple(choice.value for choice in tuning_catalog.EFFORT_CHOICES),
        default=None,
        help="Reasoning effort for the primary model (medium recommended)",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="Runtime root for config.env and the default data/ directory",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Data directory to write as NYMERIA_DATA_DIR",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help=(
            "Ask only the essentials (hosting + LLM) and default the rest with "
            "no-extra-auth picks (free local RAG, keyless web fetch, all skill "
            "kits); with --non-interactive, applies the same defaults to the "
            "flag-driven setup"
        ),
    )
    parser.add_argument(
        "--custom",
        action="store_true",
        help="Walk every section (the default); wins over --quick if both are given",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite an existing config from scratch instead of "
            "reconfiguring it (skips the hydrate-and-merge path)"
        ),
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help=(
            "Require flags instead of prompting. Against an existing install "
            "this reconfigures: current values hydrate from disk and only the "
            "flags given change"
        ),
    )
    parser.add_argument(
        "--skip-llm-test",
        action="store_true",
        help=(
            "Skip the connection checks: the pre-write provider API call, the "
            "CLIProxy login preflight, and the post-start chat smoke test"
        ),
    )
    parser.add_argument(
        "--run-doctor",
        action="store_true",
        help="Run nymeria doctor after writing config",
    )
    parser.add_argument(
        "--full-doctor",
        action="store_true",
        help="Run post-init doctor with its live LLM check; implies --run-doctor",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nymeria init",
        description="Initialize Nymeria configuration",
    )
    add_init_arguments(parser)
    return parser


def _build_state(args: argparse.Namespace) -> WizardState:
    optional_env: dict[str, str] = {}
    for attr, env_var in _OPTIONAL_KEY_FLAGS:
        value = getattr(args, attr, None)
        if value:
            optional_env[env_var] = value.strip()

    extras: dict[str, object] = {}
    for attr, family in _FAMILY_FLAGS:
        value = getattr(args, attr, None)
        if value is None:
            continue
        items = [str(item).strip() for item in value if str(item).strip()]
        if not items:
            flag = "--" + attr.replace("_", "-")
            raise SystemExit(
                f"{flag} got an empty value; pass '{flag} none' to clear the family"
            )
        if any(item.lower() == "none" for item in items):
            # Explicit "no picks": the empty list reverts profile picks and
            # retires the Docker carrier lines on a reconfigure. No catalog
            # choice is named "none", so the sentinel cannot collide.
            if len(items) > 1:
                flag = "--" + attr.replace("_", "-")
                raise SystemExit(
                    f"{flag} none cannot be combined with other {flag} picks"
                )
            extras[family] = []
        else:
            extras[family] = items
    for attr in ("tts", "stt"):
        value = getattr(args, attr, None)
        if value:
            extras[attr] = str(value)
    for attr, key in (
        ("context", "context_strategy"),
        ("timezone", "user_timezone"),
        ("reasoning_effort", "llm_effort"),
    ):
        value = getattr(args, attr, None)
        if value:
            extras[key] = str(value)
    timezone = extras.get("user_timezone")
    if isinstance(timezone, str):
        tz_field = next(
            field
            for field in tuning_catalog.LIMIT_FIELDS
            if field.key == "user_timezone"
        )
        _, error = tuning_catalog.parse_field(tz_field, timezone)
        if error:
            raise SystemExit(f"--timezone: {error}")

    hosting = None
    if getattr(args, "hosting", None):
        hosting = parse_choice(HostingOption, args.hosting, option_name="--hosting")

    api_port = getattr(args, "port", None)
    if api_port is not None and not 1 <= api_port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")

    auth_method = ProviderAuthMethod.API_KEY
    auth_method_explicit = bool(getattr(args, "auth_method", None))
    if auth_method_explicit:
        auth_method = parse_choice(
            ProviderAuthMethod, args.auth_method, option_name="--auth-method"
        )
    elif getattr(args, "provider", None) or (getattr(args, "api_key", None) or "").strip():
        # An explicit direct-provider flag pins the API-key branch: a scripted
        # reconfigure that names --provider/--api-key must never be re-routed
        # onto the CLIProxy branch by hydrate's base-URL inference, which would
        # ignore the flags and write the direct key into the proxy's
        # gatekeeper slot.
        auth_method_explicit = True
    cliproxy_provider = getattr(args, "cliproxy_provider", None)
    legacy_provider = legacy_cliproxy_provider(auth_method)
    if legacy_provider is not None:
        # Legacy per-provider methods map onto the generic branch with the
        # CLI pinned (desktop back-compat).
        auth_method = ProviderAuthMethod.CLIPROXY_OAUTH
        cliproxy_provider = cliproxy_provider or legacy_provider

    docker_stack = None
    if getattr(args, "docker_stack", None):
        docker_stack = parse_choice(
            DockerStack, args.docker_stack, option_name="--docker-stack"
        )

    security_profile = None
    if getattr(args, "security_profile", None):
        security_profile = parse_choice(
            SecurityProfile, args.security_profile, option_name="--security-profile"
        )
    if security_profile in (SecurityProfile.SECURE, SecurityProfile.STANDARD):
        # Recording an unenforced profile would hand scripted installs a false
        # sense of security; fail loudly until the approval gate exists.
        raise SystemExit(
            f"--security-profile {args.security_profile} is not available yet: "
            "the approval gate that enforces it is not built. Only 'unleashed' "
            "(the current behavior) is selectable for now; the design lives in "
            "docs/private/security-profiles.md"
        )

    external_access = None
    if getattr(args, "external_access", None):
        external_access = parse_choice(
            ExternalAccess, args.external_access, option_name="--external-access"
        )

    public_url = (getattr(args, "public_url", None) or "").strip()
    if public_url and not public_url.lower().startswith(("http://", "https://")):
        # A non-origin here would be persisted into NYMERIA_PUBLIC_URL and
        # CORS_ORIGINS where it can never match anything.
        raise SystemExit(
            "--public-url must be a full origin starting with http:// or "
            "https:// (for example https://nymeria.example.com)"
        )
    if public_url and external_access is ExternalAccess.LOCAL_ONLY:
        # The local-only gate would silently discard the validated URL.
        raise SystemExit(
            "--public-url conflicts with --external-access local_only; pick "
            "a non-local choice or drop the URL"
        )

    next_action = DEFAULT_NEXT_ACTION
    explicit_next_action = False
    if getattr(args, "next_action", None):
        next_action = parse_choice(
            NextAction, args.next_action, option_name="--next-action"
        )
        explicit_next_action = True
    if getattr(args, "start", False):
        # --start is the convenience opt-in; it wins over --next-action.
        next_action = NextAction.START_API_OPEN_FRONTEND
        explicit_next_action = True
    if explicit_next_action:
        # Pre-select the start-now step to the flag's value in interactive runs;
        # harmless in the non-interactive path (the step never renders there).
        extras["start_now"] = next_action

    root = Path(args.root) if getattr(args, "root", None) else None
    data_dir = Path(args.data_dir) if getattr(args, "data_dir", None) else None

    return WizardState(
        hosting=hosting,
        api_port=api_port,
        docker_stack=docker_stack,
        security_profile=security_profile,
        auth_method=auth_method,
        auth_method_explicit=auth_method_explicit,
        cliproxy_provider=cliproxy_provider,
        cliproxy_management_url=(
            getattr(args, "cliproxy_management_url", None) or ""
        ).strip().rstrip("/"),
        cliproxy_management_key=(
            getattr(args, "cliproxy_management_key", None) or ""
        ).strip(),
        cliproxy_gatekeeper_key=(
            getattr(args, "cliproxy_gatekeeper_key", None) or ""
        ).strip(),
        external_access=external_access,
        public_url=public_url,
        provider=getattr(args, "provider", None),
        api_key=(getattr(args, "api_key", None) or "").strip(),
        model=(getattr(args, "model", None) or "").strip(),
        base_url=(getattr(args, "base_url", None) or "").strip(),
        api_mode=(getattr(args, "api_mode", None) or ""),
        optional_env=optional_env,
        extras=extras,
        root=root,
        data_dir=data_dir,
        next_action=next_action,
        # --quick gates the wizard to the essentials; --custom forces the full
        # walk and wins if both are passed.
        quick=bool(getattr(args, "quick", False)) and not bool(getattr(args, "custom", False)),
        skip_llm_test=bool(getattr(args, "skip_llm_test", False)),
        force=bool(getattr(args, "force", False)),
        run_doctor=bool(getattr(args, "run_doctor", False) or getattr(args, "full_doctor", False)),
        full_doctor=bool(getattr(args, "full_doctor", False)),
    )


def enforce_hosting_gates(
    state: WizardState, console: Console, *, hosting_explicit: bool
) -> None:
    """Headless mirror of the hosting picker's detection gates (hybrid policy).

    An impossible shape (the picker would grey it out) is rejected when the
    value came from an explicit --hosting flag, and only warned about when it
    was hydrated from disk: a scripted edit of an unrelated section must not
    die because, say, the docker CLI is missing right now. Degraded warnings
    here cover what light detection can see (low RAM/disk for the chosen
    stack); the subprocess-probed degradations (daemon stopped, compose
    missing) are interactive-only signals, since headless runs never probe
    deep.
    """
    report = state.env_report
    if report is None:
        return
    hosting = state.hosting or HostingOption.LOCAL
    gate = hosting_gates(report).get(hosting)
    if gate is not None and gate.disabled:
        message = (
            f"The '{hosting.value}' hosting shape is unavailable on this "
            f"machine: {gate.reason}."
        )
        if hosting_explicit:
            raise SystemExit(message)
        console.print(
            f"[yellow]{message} Continuing anyway: this run leaves the "
            "hosting shape unchanged.[/yellow]"
        )
    elif gate is not None and gate.warning:
        console.print(f"[yellow]Warning:[/yellow] {gate.warning}")
    if hosting is HostingOption.DOCKER:
        for warning in stack_resource_warnings(report, state.docker_stack):
            console.print(f"[yellow]Warning:[/yellow] {warning}")


def run_init(args: argparse.Namespace) -> int:
    """Run the interactive wizard or the flag-driven headless setup."""

    console = Console()
    non_interactive = bool(getattr(args, "non_interactive", False))
    state = _build_state(args)

    # The headless CLIProxy auth flags are run-mode directives (like
    # --non-interactive itself), validated up front so a misplaced flag fails
    # before anything touches disk or the proxy.
    cliproxy_login_flag = bool(getattr(args, "cliproxy_login", False))
    cliproxy_auth_file = getattr(args, "cliproxy_auth_file", None)
    if cliproxy_login_flag and cliproxy_auth_file:
        raise SystemExit(
            "pass either --cliproxy-login or --cliproxy-auth-file, not both"
        )
    if (cliproxy_login_flag or cliproxy_auth_file) and not non_interactive:
        raise SystemExit(
            "--cliproxy-login and --cliproxy-auth-file require "
            "--non-interactive; the interactive wizard has its own login step"
        )

    if non_interactive:
        # Scripted reconfigure: against an existing install, hydrate the
        # current values from disk (fill-only-if-unset, so explicit flags win)
        # and merge-write only what changed, mirroring the interactive path.
        # --force keeps meaning a destructive fresh write: no hydrate, no merge.
        section = getattr(args, "section", None)
        if section:
            validate_section_id(section)
        reconfigure = False
        if not state.force:
            from .hydrate import hydrate_state_from_disk

            reconfigure = hydrate_state_from_disk(state, console=console)
        if section and not reconfigure:
            # A scoped run skips the bootstrap-token handoff a fresh install
            # needs, so sections only make sense against an existing config.
            raise SystemExit(
                f"Section '{section}' edits an existing install, but no "
                "config was found to reconfigure (--force also skips the "
                "reconfigure path). Run a full non-interactive init first, "
                "without a section argument."
            )
        if state.quick:
            # Same defaults the interactive quick path seeds. The LLM flags
            # below stay required: quick never defaults provider/model/key.
            apply_quick_defaults(state)
        # Light detection only (no subprocess probes): scripted runs stay fast,
        # and the gates still catch a hosting choice this host cannot run.
        state.env_report = detect_environment(
            port=state.resolved_api_port(), deep=False
        )
        enforce_hosting_gates(
            state, console, hosting_explicit=bool(getattr(args, "hosting", None))
        )
        if (
            cliproxy_login_flag or cliproxy_auth_file
        ) and not state.auth_method_is_cliproxy():
            raise SystemExit(
                "--cliproxy-login and --cliproxy-auth-file require "
                "--auth-method cliproxy_oauth (or an existing CLIProxy-routed "
                "install to reconfigure)"
            )
        # A scoped jump to a non-LLM section edits only that section: the LLM
        # branch checks (and the CLIProxy login preflight, a network call) are
        # skipped, and finalize re-derives the LLM lines from the hydrated
        # state unchanged. This also lets a provider-less install take scoped
        # edits.
        llm_scoped = (
            section is None
            or section in SECTION_DEPENDENCIES["auth_method"]
            or cliproxy_login_flag
            or bool(cliproxy_auth_file)
        )
        leaving_cliproxy = False
        if reconfigure and not state.auth_method_is_cliproxy():
            from ..vendor.react_agent.cliproxy import looks_like_cliproxy_url

            base_url_hydrated = not (getattr(args, "base_url", None) or "").strip()
            # Mirror hydrate's two-signal heuristic so a direct-key install
            # pointing at some unrelated 8318 endpoint keeps its base URL.
            second_signal = (
                "cli-proxy" in state.base_url
                or "cliproxy" in state.base_url
                or bool(state.cliproxy_management_url)
            )
            if (
                base_url_hydrated
                and looks_like_cliproxy_url(state.base_url)
                and second_signal
            ):
                # Leaving the subscription branch: the hydrated base URL and
                # API mode describe the abandoned proxy route, not user data,
                # and the on-disk key slot holds the proxy's cpx- gatekeeper,
                # not a provider key. Clear the route fields (finalize's
                # leaving-cliproxy drop list retires the stale env lines) and
                # require a real key for the new direct provider.
                state.base_url = ""
                state.api_mode = ""
                leaving_cliproxy = True
        if state.auth_method_is_cliproxy():
            # Explicit intent means the user is setting up or changing the
            # subscription route this run. A branch merely inferred from disk
            # on a plain reconfigure keeps its working route untouched: when
            # hydrate could not name the CLI (every non-claude/codex route
            # shares the openai+/v1 shape) the preparation step is skipped
            # entirely rather than demanding --cliproxy-provider for an
            # unrelated edit, and when it could, the login preflight runs in
            # lenient mode (warn, never block the edit).
            explicit_cliproxy_intent = bool(
                getattr(args, "cliproxy_provider", None)
                or cliproxy_login_flag
                or cliproxy_auth_file
                or state.auth_method_explicit
            )
            if llm_scoped and (
                not reconfigure
                or explicit_cliproxy_intent
                or state.cliproxy_provider is not None
            ):
                from .cliproxy_login import prepare_headless_cliproxy

                rc = prepare_headless_cliproxy(
                    state,
                    console=console,
                    login=cliproxy_login_flag,
                    auth_file=cliproxy_auth_file,
                    strict=not reconfigure or explicit_cliproxy_intent,
                )
                if rc != 0:
                    return rc
        elif llm_scoped:
            if not state.provider:
                raise SystemExit("--provider is required with --non-interactive")
            spec = get_llm_provider_spec(state.provider)
            if not state.model:
                raise SystemExit("--model is required with --non-interactive")
            # Reconfigure: a key already on disk satisfies the requirement
            # (finalize's keep-existing-key path preserves the line). Mirrors
            # finalize's own key_present check. Not honored when leaving the
            # CLIProxy branch: the present key is the proxy gatekeeper.
            key_present = not leaving_cliproxy and bool(
                spec is not None
                and spec.api_key_env_vars
                and spec.api_key_env_vars[0] in state.present_env_keys
            )
            if (
                spec is not None
                and spec.requires_api_key
                and not state.api_key
                and not key_present
            ):
                raise SystemExit("--api-key is required with --non-interactive")
            if spec is not None and spec.requires_base_url and not state.base_url:
                raise SystemExit(
                    "--base-url is required with --non-interactive for this provider"
                )
        return finalize(
            state,
            console=console,
            non_interactive=True,
            merge=reconfigure,
            scoped_section=section,
        )

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        console.print(
            "[red]nymeria init needs an interactive terminal. Re-run with "
            "--non-interactive and the provider flags for an unattended setup.[/red]"
        )
        return 2

    # Reconfigure: when an install already exists, load its settings so every step
    # shows the current value as its default and finalize merge-writes only what
    # changed. Returns False on a fresh install (first-run behavior unchanged).
    from .hydrate import hydrate_state_from_disk

    reconfigure = hydrate_state_from_disk(state, console=console)

    # Deep detection (docker daemon, compose plugin, running containers, port
    # owner) runs once here, before the TUI starts, and is cached on state: the
    # welcome screen renders it and the hosting step gates its choices from it.
    console.print("Detecting environment...")
    state.env_report = detect_environment(port=state.resolved_api_port(), deep=True)

    # Section jump (`nymeria init <section>`): run only that section (plus its
    # dependency closure and the welcome/review bookends). Validated against the
    # known step ids; `None` runs the full wizard.
    section = getattr(args, "section", None)
    steps = None
    if section:
        from .steps import build_section_steps

        validate_section_id(section)
        steps = build_section_steps(section)

    # Quick path: seed the skipped steps' no-extra-auth defaults (free local RAG,
    # keyless web fetch) before the wizard runs so the review screen is accurate.
    if state.quick:
        apply_quick_defaults(state)

    # Interactive: collect answers in the Textual wizard, then finalize headless
    # so the bootstrap token prints to normal scrollback.
    from .app import SetupWizardApp

    app = SetupWizardApp(state, steps=steps)
    app.run()
    if not app.completed:
        console.print("Setup cancelled.")
        return 1
    return finalize(
        state,
        console=console,
        non_interactive=False,
        overwrite_confirmed=True,
        merge=reconfigure,
        scoped_section=section,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_init(args)


__all__ = ["add_init_arguments", "build_parser", "run_init", "main"]
