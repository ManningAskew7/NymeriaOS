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
    HostingOption,
    NextAction,
    choice_values,
    parse_choice,
)
from ..config.llm_providers import get_llm_provider_spec, list_llm_provider_specs
from .finalize import finalize
from .state import WizardState

DEFAULT_NEXT_ACTION = NextAction.PRINT_COMMANDS

_OPTIONAL_KEY_FLAGS = (
    ("embedding_api_key", "EMBEDDING_API_KEY"),
    ("openai_api_key", "OPENAI_API_KEY"),
    ("gemini_api_key", "GEMINI_API_KEY"),
    ("perplexity_api_key", "PERPLEXITY_API_KEY"),
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
        "--next-action",
        choices=choice_values(NextAction),
        default=None,
        help="Post-setup handoff action",
    )
    parser.add_argument("--embedding-api-key", default=None)
    parser.add_argument("--openai-api-key", default=None)
    parser.add_argument("--gemini-api-key", default=None)
    parser.add_argument("--perplexity-api-key", default=None)
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
    parser.add_argument("--quick", action="store_true", help="Ask the minimum")
    parser.add_argument("--custom", action="store_true", help="Walk every section")
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

    hosting = None
    if getattr(args, "hosting", None):
        hosting = parse_choice(HostingOption, args.hosting, option_name="--hosting")

    next_action = DEFAULT_NEXT_ACTION
    if getattr(args, "next_action", None):
        next_action = parse_choice(
            NextAction, args.next_action, option_name="--next-action"
        )

    root = Path(args.root) if getattr(args, "root", None) else None
    data_dir = Path(args.data_dir) if getattr(args, "data_dir", None) else None

    return WizardState(
        hosting=hosting,
        provider=getattr(args, "provider", None),
        api_key=(getattr(args, "api_key", None) or "").strip(),
        model=(getattr(args, "model", None) or "").strip(),
        base_url=(getattr(args, "base_url", None) or "").strip(),
        api_mode=(getattr(args, "api_mode", None) or ""),
        optional_env=optional_env,
        root=root,
        data_dir=data_dir,
        next_action=next_action,
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

    # Interactive: collect answers in the Textual wizard, then finalize headless
    # so the bootstrap token prints to normal scrollback.
    from .app import SetupWizardApp

    app = SetupWizardApp(state)
    app.run()
    if not app.completed:
        console.print("Setup cancelled.")
        return 1
    return finalize(
        state,
        console=console,
        non_interactive=False,
        overwrite_confirmed=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run_init(args)


__all__ = ["add_init_arguments", "build_parser", "run_init", "main"]
