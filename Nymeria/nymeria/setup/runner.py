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
    parse_choice,
)
from ..config.llm_providers import get_llm_provider_spec, list_llm_provider_specs
from .finalize import finalize
from .quick import apply_quick_defaults
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
        "--auth-method",
        choices=(ProviderAuthMethod.API_KEY.value,),
        default=None,
        help="LLM auth method. Only api_key is wired; CLIProxy OAuth is deferred",
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
        help="First-run security posture (recorded; enforcement is built out later)",
    )
    parser.add_argument(
        "--external-access",
        choices=choice_values(ExternalAccess),
        default=None,
        help="How the backend is reached remotely (placeholder guidance)",
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
            "health wait; local: foreground). Default just prints the command."
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
    # --web-search web_search_tavily --image-gen image_gen_gemini.
    parser.add_argument(
        "--web-search", action="append", default=None, metavar="TOOL",
        help="Seed a web_search_* backend into the default tools (repeatable)",
    )
    parser.add_argument(
        "--fetch-url", action="append", default=None, metavar="TOOL",
        help="Seed a fetch_url backend into the default tools (repeatable)",
    )
    parser.add_argument(
        "--image-gen", action="append", default=None, metavar="TOOL",
        help="Seed an image_gen_* backend into the default tools (repeatable)",
    )
    parser.add_argument(
        "--skill-kit", action="append", default=None, metavar="KIT",
        help="Seed a default-on capability kit into enabled_global_skills (repeatable)",
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
            "no-extra-auth picks (free local RAG, keyless web fetch, all skill kits)"
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
        help="Overwrite config.env if it exists",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Require flags instead of prompting",
    )
    parser.add_argument(
        "--skip-llm-test",
        action="store_true",
        help="Write config without the provider smoke-test API call",
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
        if value:
            extras[family] = [str(item) for item in value]

    hosting = None
    if getattr(args, "hosting", None):
        hosting = parse_choice(HostingOption, args.hosting, option_name="--hosting")

    auth_method = ProviderAuthMethod.API_KEY
    if getattr(args, "auth_method", None):
        auth_method = parse_choice(
            ProviderAuthMethod, args.auth_method, option_name="--auth-method"
        )

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

    external_access = None
    if getattr(args, "external_access", None):
        external_access = parse_choice(
            ExternalAccess, args.external_access, option_name="--external-access"
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
        docker_stack=docker_stack,
        security_profile=security_profile,
        auth_method=auth_method,
        external_access=external_access,
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


def run_init(args: argparse.Namespace) -> int:
    """Run the interactive wizard or the flag-driven headless setup."""

    console = Console()
    non_interactive = bool(getattr(args, "non_interactive", False))
    state = _build_state(args)

    if non_interactive:
        if not state.provider:
            raise SystemExit("--provider is required with --non-interactive")
        spec = get_llm_provider_spec(state.provider)
        if not state.model:
            raise SystemExit("--model is required with --non-interactive")
        if spec is not None and spec.requires_api_key and not state.api_key:
            raise SystemExit("--api-key is required with --non-interactive")
        if spec is not None and spec.requires_base_url and not state.base_url:
            raise SystemExit(
                "--base-url is required with --non-interactive for this provider"
            )
        return finalize(state, console=console, non_interactive=True)

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

    # Section jump (`nymeria init <section>`): run only that section (plus its
    # dependency closure and the welcome/review bookends). Validated against the
    # known step ids; `None` runs the full wizard.
    section = getattr(args, "section", None)
    steps = None
    if section:
        from .steps import build_section_steps, default_step_ids

        valid = default_step_ids()
        if section not in valid:
            jumpable = ", ".join(s for s in valid if s not in {"welcome", "review"})
            raise SystemExit(
                f"Unknown section '{section}'. Choose one of: {jumpable}"
            )
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
