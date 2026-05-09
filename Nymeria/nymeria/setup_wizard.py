"""First-run setup for packaged Nymeria installations."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx
from prompt_toolkit import prompt
from rich.console import Console
import yaml

from ._runtime_paths import default_user_project_root, find_project_root
from .core.accounts import AccountsRepo, BOOTSTRAP_TOKEN_FILENAME
from .onboarding import (
    HOSTING_CHOICES,
    HOSTING_ORDER,
    NEXT_ACTION_CHOICES,
    NEXT_ACTION_ORDER,
    PROVIDER_AUTH_METHOD_CHOICES,
    PROVIDER_AUTH_METHOD_ORDER,
    SETUP_STYLE_CHOICES,
    SETUP_STYLE_ORDER,
    HostingOption,
    NextAction,
    OnboardingSelection,
    ProviderAuthMethod,
    ProviderOption,
    SetupStyle,
    choice_values,
    parse_choice,
)


@dataclass(frozen=True)
class LLMConnectionResult:
    model: str


class LLMConnectionError(RuntimeError):
    """Raised when first-run provider validation cannot complete."""


@dataclass(frozen=True)
class BootstrapTokenCopyCommand:
    command: str
    copies_to_clipboard: bool


@dataclass(frozen=True)
class CLIProxyDeployment:
    root: Path
    config_path: Path
    compose_path: Path
    auth_dir: Path


@dataclass(frozen=True)
class CLIProxyClaudeSetup:
    config_base_url: str
    smoke_base_url: str
    gatekeeper_key: str
    auth_dir: Path
    model: str


class CLIProxySetupError(RuntimeError):
    """Raised when CLIProxy setup cannot be completed automatically."""


BOOTSTRAP_TOKEN_REGEX = r"nym_[A-Za-z0-9_-]+"
CLIPROXY_CONTAINER_NAME = "cli-proxy-api-latest"
CLIPROXY_RELATIVE_ROOT = Path("CLIProxyAPI-main") / "temp" / "latest"
CLIPROXY_DEFAULT_HOST_BASE_URL = "http://localhost:8317"
CLIPROXY_DOCKER_BASE_URL = "http://cli-proxy-api:8317"

PROVIDERS = {
    "anthropic": ProviderOption(
        name="anthropic",
        label="Anthropic (Claude)",
        env_var="ANTHROPIC_API_KEY",
        key_prefix="sk-ant-",
        default_model="claude-sonnet-4-6",
    ),
    "openai": ProviderOption(
        name="openai",
        label="OpenAI (GPT)",
        env_var="OPENAI_API_KEY",
        key_prefix="sk-",
        default_model="gpt-5.5",
    ),
    "openrouter": ProviderOption(
        name="openrouter",
        label="OpenRouter (multi-model)",
        env_var="OPENROUTER_API_KEY",
        key_prefix="sk-or-",
        default_model="anthropic/claude-sonnet-4-6",
    ),
}

PROVIDER_ORDER = ("anthropic", "openai", "openrouter")
OPTIONAL_ENV_ORDER = (
    "EMBEDDING_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "PERPLEXITY_API_KEY",
)

DEFAULT_HOSTING = HostingOption.VENV
DEFAULT_AUTH_METHOD = ProviderAuthMethod.API_KEY
DEFAULT_INTERACTIVE_SETUP_STYLE = SetupStyle.RECOMMENDED
DEFAULT_NON_INTERACTIVE_SETUP_STYLE = SetupStyle.ADVANCED
DEFAULT_NEXT_ACTION = NextAction.PRINT_COMMANDS


def run_init(args: argparse.Namespace) -> int:
    """Run the interactive or flag-driven first-run setup."""
    console = Console()
    non_interactive = bool(getattr(args, "non_interactive", False))

    console.print("\n[bold]Welcome to Nymeria.[/bold] Let's get you set up.\n")

    try:
        onboarding = _resolve_onboarding_selection(args, console, non_interactive)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    if onboarding.auth_method is ProviderAuthMethod.CLIPROXY_CLAUDE_OAUTH:
        return _run_cliproxy_claude_setup(
            args,
            onboarding=onboarding,
            console=console,
            non_interactive=non_interactive,
        )
    if onboarding.auth_method is not ProviderAuthMethod.API_KEY:
        return _run_cliproxy_planning_gate(onboarding, console, non_interactive)
    if onboarding.hosting is HostingOption.DOCKER:
        _print_docker_hosting_handoff(console)
        return 0

    provider = _resolve_provider(args, console, non_interactive)
    model = _resolve_model(args, provider, console, non_interactive)
    api_key = _resolve_api_key(args, provider, console, non_interactive)
    if not _valid_key_format(provider, api_key):
        console.print(
            f"[red]The {provider.label} key should start with "
            f"`{provider.key_prefix}`.[/red]"
        )
        return 2
    provider_auth_validated = False
    if getattr(args, "skip_llm_test", False):
        console.print("[yellow]Skipping LLM connection test.[/yellow]")
    else:
        console.print("\nTesting LLM connection...")
        try:
            result = _test_llm_connection(provider, model, api_key)
        except LLMConnectionError as exc:
            console.print(f"[red]LLM connection failed:[/red] {exc}")
            return 2
        console.print(f"[green]Connected:[/green] {result.model}")
        provider_auth_validated = True

    setup_style = onboarding.setup_style
    if _should_prompt_setup_style(args, non_interactive):
        setup_style = _prompt_setup_style(console)

    try:
        if setup_style is SetupStyle.RECOMMENDED:
            optional_env = _resolve_recommended_defaults(
                args,
                provider=provider,
                api_key=api_key,
                console=console,
            )
        else:
            optional_env = _resolve_optional_capabilities(
                args,
                provider=provider,
                api_key=api_key,
                console=console,
                non_interactive=non_interactive,
            )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    root = _resolve_root(args, console, non_interactive, setup_style=setup_style)
    data_dir = root / "data"

    try:
        _check_writable(root)
    except OSError as exc:
        console.print(f"[red]Cannot write to {root}: {exc}[/red]")
        return 2

    if _port_in_use(8000):
        console.print("[yellow]Warning:[/yellow] port 8000 is already in use.")

    config_path = root / "config.env"
    if config_path.exists() and not getattr(args, "force", False):
        if non_interactive:
            console.print(
                f"[red]{config_path} already exists. Re-run with --force to overwrite.[/red]"
            )
            return 2
        answer = prompt(f"{config_path} exists. Overwrite it? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            console.print("Setup cancelled.")
            return 1

    data_dir.mkdir(parents=True, exist_ok=True)
    console.print("\n[bold]Configuration[/bold]")
    _write_config(config_path, provider, model, api_key, data_dir, optional_env)

    repo = AccountsRepo(data_dir / "accounts.db")
    repo.ensure_bootstrap_admin(data_dir)
    token_path = data_dir / BOOTSTRAP_TOKEN_FILENAME

    console.print(f"[green]Config:[/green] {config_path}")
    console.print(f"[green]Data dir:[/green] {data_dir}")
    _print_bootstrap_token_handoff(token_path, console)
    doctor_status = _offer_post_setup_doctor(
        args,
        root=root,
        console=console,
        non_interactive=non_interactive,
        provider_auth_validated=provider_auth_validated,
    )
    if doctor_status != 0:
        return doctor_status
    next_action = _resolve_next_action(args, console, non_interactive)
    _print_next_action(next_action, console)
    return 0


def _resolve_onboarding_selection(
    args: argparse.Namespace,
    console: Console,
    non_interactive: bool,
) -> OnboardingSelection:
    hosting = _resolve_hosting(args, console, non_interactive)
    if hosting is HostingOption.DOCKER and getattr(args, "auth_method", None) is None:
        auth_method = DEFAULT_AUTH_METHOD
    else:
        auth_method = _resolve_auth_method(args, console, non_interactive)
    return OnboardingSelection(
        hosting=hosting,
        auth_method=auth_method,
        setup_style=_configured_or_default_setup_style(args, non_interactive),
        next_action=_parse_onboarding_arg(
            NextAction,
            getattr(args, "next_action", None),
            option_name="--next-action",
            default=DEFAULT_NEXT_ACTION,
        ),
    )


def _parse_onboarding_arg(enum_type, value, *, option_name: str, default):
    if value is None:
        return default
    if isinstance(value, enum_type):
        return value
    return parse_choice(enum_type, str(value), option_name=option_name)


def _resolve_hosting(
    args: argparse.Namespace,
    console: Console,
    non_interactive: bool,
) -> HostingOption:
    configured = getattr(args, "hosting", None)
    if configured is not None:
        return _parse_onboarding_arg(
            HostingOption,
            configured,
            option_name="--hosting",
            default=DEFAULT_HOSTING,
        )
    if non_interactive:
        return DEFAULT_HOSTING

    console.print("[bold]Step 1: Hosting / Security[/bold]")
    console.print("Choose where the Nymeria backend will run.")
    default_index = HOSTING_ORDER.index(DEFAULT_HOSTING) + 1
    for idx, hosting in enumerate(HOSTING_ORDER, start=1):
        choice = HOSTING_CHOICES[hosting]
        suffix = ""
        if hosting is DEFAULT_HOSTING:
            suffix = " - default for package/local setup"
        elif choice.recommended:
            suffix = " - recommended for beta/server isolation"
        console.print(f"  [{idx}] {choice.label}{suffix}")
        console.print(f"      {choice.description}")

    answer = prompt(f"> [{default_index}] ").strip()
    if not answer:
        return DEFAULT_HOSTING

    try:
        return HOSTING_ORDER[int(answer) - 1]
    except (ValueError, IndexError):
        try:
            return parse_choice(HostingOption, answer, option_name="hosting choice")
        except ValueError:
            default_choice = HOSTING_CHOICES[DEFAULT_HOSTING].label
            console.print(f"[yellow]Unknown choice, using {default_choice}.[/yellow]")
            return DEFAULT_HOSTING


def _resolve_auth_method(
    args: argparse.Namespace,
    console: Console,
    non_interactive: bool,
) -> ProviderAuthMethod:
    configured = getattr(args, "auth_method", None)
    if configured is not None:
        return _parse_onboarding_arg(
            ProviderAuthMethod,
            configured,
            option_name="--auth-method",
            default=DEFAULT_AUTH_METHOD,
        )
    if non_interactive:
        return DEFAULT_AUTH_METHOD

    console.print("\n[bold]Step 2: Provider Authentication[/bold]")
    console.print("Choose how Nymeria will authenticate to the primary LLM provider.")
    default_index = PROVIDER_AUTH_METHOD_ORDER.index(DEFAULT_AUTH_METHOD) + 1
    for idx, auth_method in enumerate(PROVIDER_AUTH_METHOD_ORDER, start=1):
        choice = PROVIDER_AUTH_METHOD_CHOICES[auth_method]
        suffix = ""
        if auth_method is DEFAULT_AUTH_METHOD:
            suffix = " - default"
        elif choice.advanced:
            suffix = " - advanced"
        console.print(f"  [{idx}] {choice.label}{suffix}")
        console.print(f"      {choice.description}")

    answer = prompt(f"> [{default_index}] ").strip()
    if not answer:
        return DEFAULT_AUTH_METHOD

    try:
        return PROVIDER_AUTH_METHOD_ORDER[int(answer) - 1]
    except (ValueError, IndexError):
        try:
            return parse_choice(
                ProviderAuthMethod,
                answer,
                option_name="provider authentication choice",
            )
        except ValueError:
            default_choice = PROVIDER_AUTH_METHOD_CHOICES[DEFAULT_AUTH_METHOD].label
            console.print(f"[yellow]Unknown choice, using {default_choice}.[/yellow]")
            return DEFAULT_AUTH_METHOD


def _configured_or_default_setup_style(
    args: argparse.Namespace,
    non_interactive: bool,
) -> SetupStyle:
    default = (
        DEFAULT_NON_INTERACTIVE_SETUP_STYLE
        if non_interactive
        else DEFAULT_INTERACTIVE_SETUP_STYLE
    )
    return _parse_onboarding_arg(
        SetupStyle,
        getattr(args, "setup_style", None),
        option_name="--setup-style",
        default=default,
    )


def _should_prompt_setup_style(
    args: argparse.Namespace,
    non_interactive: bool,
) -> bool:
    return not non_interactive and getattr(args, "setup_style", None) is None


def _prompt_setup_style(console: Console) -> SetupStyle:
    console.print("\n[bold]Step 6: Setup Style[/bold]")
    console.print("Choose how much configuration to collect now.")
    default_index = SETUP_STYLE_ORDER.index(DEFAULT_INTERACTIVE_SETUP_STYLE) + 1
    for idx, setup_style in enumerate(SETUP_STYLE_ORDER, start=1):
        choice = SETUP_STYLE_CHOICES[setup_style]
        suffix = ""
        if setup_style is DEFAULT_INTERACTIVE_SETUP_STYLE:
            suffix = " - default"
        elif choice.advanced:
            suffix = " - advanced"
        console.print(f"  [{idx}] {choice.label}{suffix}")
        console.print(f"      {choice.description}")

    answer = prompt(f"> [{default_index}] ").strip()
    if not answer:
        return DEFAULT_INTERACTIVE_SETUP_STYLE

    try:
        return SETUP_STYLE_ORDER[int(answer) - 1]
    except (ValueError, IndexError):
        try:
            return parse_choice(SetupStyle, answer, option_name="setup style")
        except ValueError:
            default_choice = SETUP_STYLE_CHOICES[
                DEFAULT_INTERACTIVE_SETUP_STYLE
            ].label
            console.print(f"[yellow]Unknown choice, using {default_choice}.[/yellow]")
            return DEFAULT_INTERACTIVE_SETUP_STYLE


def _run_cliproxy_claude_setup(
    args: argparse.Namespace,
    *,
    onboarding: OnboardingSelection,
    console: Console,
    non_interactive: bool,
) -> int:
    if not non_interactive:
        console.print("\n[bold]CLIProxy Claude OAuth Setup[/bold]")
        console.print(
            "This advanced path uses the existing pinned CLIProxy deployment, "
            "adds a local cpx-* gatekeeper key when needed, verifies Claude "
            "OAuth auth files, and runs the cloak smoke test before writing "
            "Nymeria config.env."
        )
        if not _yes_no("Continue with CLIProxy Claude OAuth setup?", default=True):
            console.print(
                "Setup cancelled. Re-run with --auth-method api_key for direct setup."
            )
            return 1

    try:
        cliproxy = _prepare_cliproxy_claude_setup(
            args,
            onboarding=onboarding,
            console=console,
            non_interactive=non_interactive,
        )
    except CLIProxySetupError as exc:
        console.print(f"[red]CLIProxy Claude OAuth setup is not complete:[/red] {exc}")
        _print_cliproxy_claude_manual_steps(console)
        return 2

    root = _resolve_root(
        args,
        console,
        non_interactive,
        setup_style=onboarding.setup_style,
    )
    data_dir = root / "data"

    try:
        _check_writable(root)
    except OSError as exc:
        console.print(f"[red]Cannot write to {root}: {exc}[/red]")
        return 2

    if _port_in_use(8000):
        console.print("[yellow]Warning:[/yellow] port 8000 is already in use.")

    config_path = root / "config.env"
    if config_path.exists() and not getattr(args, "force", False):
        if non_interactive:
            console.print(
                f"[red]{config_path} already exists. Re-run with --force to overwrite.[/red]"
            )
            return 2
        answer = prompt(f"{config_path} exists. Overwrite it? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            console.print("Setup cancelled.")
            return 1

    data_dir.mkdir(parents=True, exist_ok=True)
    console.print("\n[bold]Configuration[/bold]")
    _write_config(
        config_path,
        PROVIDERS["anthropic"],
        cliproxy.model,
        cliproxy.gatekeeper_key,
        data_dir,
        extra_env={"LLM_BASE_URL": cliproxy.config_base_url},
    )

    repo = AccountsRepo(data_dir / "accounts.db")
    repo.ensure_bootstrap_admin(data_dir)
    token_path = data_dir / BOOTSTRAP_TOKEN_FILENAME

    console.print(f"[green]Config:[/green] {config_path}")
    console.print(f"[green]Data dir:[/green] {data_dir}")
    console.print(
        f"[green]CLIProxy:[/green] Claude OAuth verified at {cliproxy.smoke_base_url}"
    )
    _print_bootstrap_token_handoff(token_path, console)
    doctor_status = _offer_post_setup_doctor(
        args,
        root=root,
        console=console,
        non_interactive=non_interactive,
        provider_auth_validated=True,
    )
    if doctor_status != 0:
        return doctor_status
    next_action = _resolve_next_action(args, console, non_interactive)
    _print_next_action(next_action, console)
    return 0


def _prepare_cliproxy_claude_setup(
    args: argparse.Namespace,
    *,
    onboarding: OnboardingSelection,
    console: Console,
    non_interactive: bool,
) -> CLIProxyClaudeSetup:
    deployment = _resolve_cliproxy_deployment(args)
    _ensure_cliproxy_config_file(deployment)
    smoke_base_url = _resolve_cliproxy_smoke_base_url(args, deployment)
    config_base_url = _resolve_cliproxy_config_base_url(
        args,
        onboarding=onboarding,
        smoke_base_url=smoke_base_url,
    )

    _ensure_cliproxy_container_ready(deployment, smoke_base_url, console)
    auth_files = _ensure_cliproxy_claude_auth_files(
        deployment,
        console=console,
        non_interactive=non_interactive,
    )
    auth_changed = _ensure_tool_prefix_disabled(auth_files)
    if auth_changed:
        console.print(
            "[green]CLIProxy auth:[/green] added tool_prefix_disabled=true to "
            f"{len(auth_changed)} active Claude auth file(s)."
        )

    gatekeeper_key, config_changed = _resolve_cliproxy_gatekeeper_key(
        args,
        deployment=deployment,
        console=console,
        non_interactive=non_interactive,
    )
    if config_changed or auth_changed:
        _restart_cliproxy_container(console)
        _ensure_cliproxy_container_ready(deployment, smoke_base_url, console)

    _run_cliproxy_cloak_check(
        smoke_base_url,
        gatekeeper_key=gatekeeper_key,
        auth_dir=deployment.auth_dir,
        console=console,
    )

    model = (
        getattr(args, "model", None) or PROVIDERS["anthropic"].default_model
    ).strip()
    return CLIProxyClaudeSetup(
        config_base_url=config_base_url,
        smoke_base_url=smoke_base_url,
        gatekeeper_key=gatekeeper_key,
        auth_dir=deployment.auth_dir,
        model=model,
    )


def _resolve_cliproxy_deployment(args: argparse.Namespace) -> CLIProxyDeployment:
    configured_root = getattr(args, "cliproxy_root", None)
    if configured_root:
        root = Path(configured_root).expanduser().resolve()
    else:
        backend_root = find_project_root(Path(__file__).resolve())
        if not backend_root:
            raise CLIProxySetupError(
                "could not locate a source checkout containing CLIProxyAPI-main."
            )
        root = (backend_root.parent / CLIPROXY_RELATIVE_ROOT).resolve()

    compose_path = root / "docker-compose.yml"
    auth_dir = root / "auths"
    config_path = root / "config.yaml"
    if not root.exists():
        raise CLIProxySetupError(f"CLIProxy directory does not exist: {root}")
    if not compose_path.exists():
        raise CLIProxySetupError(f"CLIProxy compose file not found: {compose_path}")
    return CLIProxyDeployment(
        root=root,
        config_path=config_path,
        compose_path=compose_path,
        auth_dir=auth_dir,
    )


def _ensure_cliproxy_config_file(deployment: CLIProxyDeployment) -> None:
    if deployment.config_path.exists():
        return
    example = deployment.root / "config.yaml.example"
    if not example.exists():
        raise CLIProxySetupError(
            f"CLIProxy config.yaml is missing and no example exists at {example}."
        )
    shutil.copyfile(example, deployment.config_path)


def _resolve_cliproxy_gatekeeper_key(
    args: argparse.Namespace,
    *,
    deployment: CLIProxyDeployment,
    console: Console,
    non_interactive: bool,
) -> tuple[str, bool]:
    configured = getattr(args, "api_key", None)
    if configured:
        gatekeeper_key = configured.strip()
    elif non_interactive:
        gatekeeper_key = _generate_cliproxy_gatekeeper_key()
    else:
        answer = prompt(
            "CLIProxy gatekeeper key [generate new cpx-*]: ",
            is_password=True,
        ).strip()
        gatekeeper_key = answer or _generate_cliproxy_gatekeeper_key()

    if not gatekeeper_key.startswith("cpx-"):
        raise CLIProxySetupError(
            "CLIProxy Claude OAuth requires a local cpx-* gatekeeper key, "
            "not an upstream Anthropic API key."
        )

    config = _read_cliproxy_config(deployment.config_path)
    existing_keys = _configured_cliproxy_api_keys(config)
    next_keys = _normalized_cliproxy_api_keys(existing_keys, gatekeeper_key)
    if next_keys == existing_keys:
        console.print(
            f"[green]CLIProxy key:[/green] using an existing cpx-* key from "
            f"{deployment.config_path}"
        )
        return gatekeeper_key, False

    config["api-keys"] = next_keys
    _write_cliproxy_config(deployment.config_path, config)
    console.print(
        f"[green]CLIProxy key:[/green] wrote a cpx-* gatekeeper key to "
        f"{deployment.config_path}"
    )
    return gatekeeper_key, True


def _generate_cliproxy_gatekeeper_key() -> str:
    return f"cpx-nymeria-{secrets.token_urlsafe(24)}"


def _read_cliproxy_config(config_path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CLIProxySetupError(f"could not parse {config_path}: {exc}") from exc
    except OSError as exc:
        raise CLIProxySetupError(f"could not read {config_path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise CLIProxySetupError(f"{config_path} must contain a YAML mapping.")
    return loaded


def _write_cliproxy_config(config_path: Path, config: Mapping[str, Any]) -> None:
    try:
        config_path.write_text(
            yaml.safe_dump(dict(config), sort_keys=False),
            encoding="utf-8",
        )
    except OSError as exc:
        raise CLIProxySetupError(f"could not write {config_path}: {exc}") from exc


def _configured_cliproxy_api_keys(config: Mapping[str, Any]) -> list[str]:
    raw_keys = config.get("api-keys")
    if not isinstance(raw_keys, list):
        return []
    keys: list[str] = []
    for raw_key in raw_keys:
        if not isinstance(raw_key, str):
            continue
        key = raw_key.strip()
        if key:
            keys.append(key)
    return keys


def _normalized_cliproxy_api_keys(
    existing_keys: list[str],
    required_key: str,
) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for key in existing_keys:
        if _is_placeholder_cliproxy_key(key):
            continue
        if key in seen:
            continue
        normalized.append(key)
        seen.add(key)
    if required_key not in seen:
        normalized.append(required_key)
    return normalized


def _is_placeholder_cliproxy_key(key: str) -> bool:
    return "<" in key or ">" in key


def _resolve_cliproxy_config_base_url(
    args: argparse.Namespace,
    *,
    onboarding: OnboardingSelection,
    smoke_base_url: str,
) -> str:
    configured = getattr(args, "cliproxy_base_url", None)
    if configured:
        return _normalize_cliproxy_claude_base_url(configured)
    if onboarding.hosting is HostingOption.DOCKER:
        return CLIPROXY_DOCKER_BASE_URL
    return smoke_base_url


def _resolve_cliproxy_smoke_base_url(
    args: argparse.Namespace,
    deployment: CLIProxyDeployment,
) -> str:
    configured = getattr(args, "cliproxy_base_url", None)
    if configured:
        return _normalize_cliproxy_claude_base_url(configured)
    return _compose_host_cliproxy_base_url(deployment.compose_path)


def _normalize_cliproxy_claude_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3].rstrip("/")
    if not normalized:
        raise CLIProxySetupError("CLIProxy base URL cannot be empty.")
    return normalized


def _compose_host_cliproxy_base_url(compose_path: Path) -> str:
    try:
        compose = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return CLIPROXY_DEFAULT_HOST_BASE_URL
    services = compose.get("services")
    if not isinstance(services, dict):
        return CLIPROXY_DEFAULT_HOST_BASE_URL
    service = services.get(CLIPROXY_CONTAINER_NAME) or services.get("cli-proxy-api")
    if not isinstance(service, dict):
        return CLIPROXY_DEFAULT_HOST_BASE_URL
    ports = service.get("ports")
    if not isinstance(ports, list):
        return CLIPROXY_DEFAULT_HOST_BASE_URL
    for port in ports:
        published = _published_port_for_container_port(port, "8317")
        if published:
            return f"http://localhost:{published}"
    return CLIPROXY_DEFAULT_HOST_BASE_URL


def _published_port_for_container_port(port: object, container_port: str) -> str | None:
    if isinstance(port, str):
        parts = port.split(":")
        if len(parts) == 2 and parts[1].split("/", 1)[0] == container_port:
            return parts[0]
        if len(parts) == 3 and parts[2].split("/", 1)[0] == container_port:
            return parts[1]
        if len(parts) == 1 and parts[0].split("/", 1)[0] == container_port:
            return container_port
    if isinstance(port, dict):
        target = str(port.get("target", ""))
        if target != container_port:
            return None
        published = str(port.get("published", "")).strip()
        return published or container_port
    return None


def _ensure_cliproxy_container_ready(
    deployment: CLIProxyDeployment,
    base_url: str,
    console: Console,
) -> None:
    if _wait_for_cliproxy_root(base_url, attempts=1, delay_seconds=0):
        console.print(f"[green]CLIProxy:[/green] reachable at {base_url}")
        return
    if not shutil.which("docker"):
        raise CLIProxySetupError(
            "Docker is not available, and the CLIProxy HTTP endpoint is not reachable."
        )

    console.print("[yellow]CLIProxy is not reachable; starting Docker compose.[/yellow]")
    result = subprocess.run(
        ["docker", "compose", "-f", str(deployment.compose_path), "up", "-d"],
        cwd=deployment.root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise CLIProxySetupError(_subprocess_failure("docker compose up -d", result))
    if not _wait_for_cliproxy_root(base_url):
        raise CLIProxySetupError(
            f"CLIProxy did not become reachable at {base_url} after docker compose up."
        )
    console.print(f"[green]CLIProxy:[/green] reachable at {base_url}")


def _wait_for_cliproxy_root(
    base_url: str,
    *,
    attempts: int = 15,
    delay_seconds: float = 1.0,
) -> bool:
    for attempt in range(attempts):
        if _cliproxy_root_available(base_url):
            return True
        if delay_seconds and attempt < attempts - 1:
            time.sleep(delay_seconds)
    return False


def _cliproxy_root_available(base_url: str) -> bool:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/", timeout=3.0)
    except (httpx.HTTPError, ValueError):
        return False
    return response.status_code < 500 and "CLI Proxy API Server" in response.text


def _ensure_cliproxy_claude_auth_files(
    deployment: CLIProxyDeployment,
    *,
    console: Console,
    non_interactive: bool,
) -> list[Path]:
    deployment.auth_dir.mkdir(parents=True, exist_ok=True)
    auth_files = _active_claude_auth_files(deployment.auth_dir)
    if auth_files:
        console.print(
            f"[green]CLIProxy auth:[/green] found {len(auth_files)} active "
            "Claude OAuth auth file(s)."
        )
        return auth_files

    login_command = (
        f"docker exec -it {CLIPROXY_CONTAINER_NAME} "
        "./CLIProxyAPI --claude-login --no-browser"
    )
    if non_interactive:
        raise CLIProxySetupError(
            "no active Claude OAuth auth JSON was found. Run this command, then "
            f"re-run nymeria init: {login_command}"
        )

    console.print("No active Claude OAuth auth JSON was found.")
    _print_command(console, login_command)
    if not _yes_no("Run Claude OAuth login now?", default=True):
        raise CLIProxySetupError("Claude OAuth login was not run.")

    result = subprocess.run(
        [
            "docker",
            "exec",
            "-it",
            CLIPROXY_CONTAINER_NAME,
            "./CLIProxyAPI",
            "--claude-login",
            "--no-browser",
        ],
        check=False,
    )
    if result.returncode != 0:
        raise CLIProxySetupError("Claude OAuth login command failed.")

    auth_files = _active_claude_auth_files(deployment.auth_dir)
    if not auth_files:
        raise CLIProxySetupError(
            f"Claude OAuth login did not create an active claude-*.json file in "
            f"{deployment.auth_dir}."
        )
    return auth_files


def _active_claude_auth_files(auth_dir: Path) -> list[Path]:
    auth_files: list[Path] = []
    if not auth_dir.is_dir():
        return auth_files
    for path in sorted(auth_dir.glob("claude-*.json")):
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("type", "")).lower() != "claude":
            continue
        if _cliproxy_bool(metadata.get("disabled")) is True:
            continue
        auth_files.append(path)
    return auth_files


def _ensure_tool_prefix_disabled(auth_files: list[Path]) -> list[Path]:
    changed: list[Path] = []
    for path in auth_files:
        try:
            metadata = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CLIProxySetupError(f"could not read {path}: {exc}") from exc
        if not isinstance(metadata, dict):
            raise CLIProxySetupError(f"{path} must contain a JSON object.")
        if _cliproxy_bool(metadata.get("tool_prefix_disabled")) is True:
            continue
        metadata["tool_prefix_disabled"] = True
        try:
            path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise CLIProxySetupError(f"could not update {path}: {exc}") from exc
        changed.append(path)
    return changed


def _cliproxy_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        trimmed = value.strip().lower()
        if trimmed in {"1", "t", "true"}:
            return True
        if trimmed in {"0", "f", "false"}:
            return False
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    return None


def _restart_cliproxy_container(console: Console) -> None:
    if not shutil.which("docker"):
        raise CLIProxySetupError("Docker is required to restart CLIProxy.")
    console.print("[yellow]Restarting CLIProxy to load config/auth changes.[/yellow]")
    result = subprocess.run(
        ["docker", "restart", CLIPROXY_CONTAINER_NAME],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise CLIProxySetupError(_subprocess_failure("docker restart", result))


def _run_cliproxy_cloak_check(
    base_url: str,
    *,
    gatekeeper_key: str,
    auth_dir: Path,
    console: Console,
) -> None:
    script_path = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "check_cliproxy_cloak.py"
    )
    if not script_path.exists():
        raise CLIProxySetupError(f"smoke-test script not found: {script_path}")
    if not auth_dir.is_dir():
        raise CLIProxySetupError(
            f"local CLIProxy auth directory is not accessible: {auth_dir}"
        )

    console.print("[bold]Running CLIProxy cloak smoke test...[/bold]")
    result = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--base-url",
            base_url,
            "--api-key",
            gatekeeper_key,
            "--auth-dir",
            str(auth_dir),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = "\n".join(
            line
            for line in (result.stdout + "\n" + result.stderr).splitlines()
            if line.strip()
        )
        if len(detail) > 2000:
            detail = detail[:2000] + "\n..."
        raise CLIProxySetupError(
            "check_cliproxy_cloak.py failed; fix CLIProxy before writing "
            f"Nymeria config.\n{detail}"
        )
    console.print("[green]CLIProxy smoke test passed.[/green]")


def _subprocess_failure(command: str, result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    if len(detail) > 600:
        detail = detail[:600] + "..."
    if detail:
        return f"{command} failed: {detail}"
    return f"{command} failed with exit code {result.returncode}."


def _print_cliproxy_claude_manual_steps(console: Console) -> None:
    console.print("\n[bold]Manual CLIProxy Claude OAuth steps[/bold]")
    console.print(
        "Complete these steps, then re-run `nymeria init --auth-method "
        "cliproxy_claude_oauth`."
    )
    _print_cliproxy_claude_commands(console)


def _run_cliproxy_planning_gate(
    onboarding: OnboardingSelection,
    console: Console,
    non_interactive: bool,
) -> int:
    if not non_interactive:
        console.print("\n[bold]CLIProxy OAuth Setup[/bold]")
        console.print(
            "This is an advanced planning handoff. It prints CLIProxy setup "
            "commands and exits without writing config.env or .env.docker."
        )
        if not _yes_no("Print CLIProxy setup commands?", default=True):
            console.print(
                "Setup cancelled. Re-run with --auth-method api_key for direct setup."
            )
            return 1

    _print_cliproxy_planning_handoff(onboarding, console)
    return 0


def _print_cliproxy_planning_handoff(
    onboarding: OnboardingSelection,
    console: Console,
) -> None:
    auth_method = onboarding.auth_method
    is_claude = auth_method is ProviderAuthMethod.CLIPROXY_CLAUDE_OAUTH
    method_label = "Claude OAuth" if is_claude else "Codex/OpenAI OAuth"
    repo_hint = _repo_root_hint()

    console.print(f"\n[bold]CLIProxy {method_label} Planning Gate[/bold]")
    console.print(
        "CLIProxy OAuth is advanced. Use Docker for the proxy whenever possible; "
        "the known-good setup depends on the pinned CLIProxy image and the "
        "Nymeria HTTP behavior documented in docs/cliproxy.md."
    )
    console.print(
        "The key you put in Nymeria is the CLIProxy gatekeeper key from "
        "CLIProxyAPI-main/temp/latest/config.yaml, not an upstream Anthropic "
        "or OpenAI API key."
    )

    console.print(
        f"\n[bold]1. Prepare the pinned CLIProxy container from {repo_hint}[/bold]"
    )
    for line in (
        (
            "cp -n CLIProxyAPI-main/temp/latest/config.yaml.example "
            "CLIProxyAPI-main/temp/latest/config.yaml"
        ),
        (
            "edit CLIProxyAPI-main/temp/latest/config.yaml and replace api-keys "
            "with your own cpx-* gatekeeper keys"
        ),
        "docker compose -f CLIProxyAPI-main/temp/latest/docker-compose.yml up -d",
    ):
        _print_command(console, line)

    if is_claude:
        _print_cliproxy_claude_commands(console)
    else:
        _print_cliproxy_codex_commands(console)

    console.print(
        "\nNo config.env or .env.docker was written. Provider, model, and API-key "
        "flags, if supplied, were not written."
    )


def _repo_root_hint() -> str:
    backend_root = find_project_root(Path(__file__).resolve())
    if backend_root and (backend_root.parent / "CLIProxyAPI-main").exists():
        return str(backend_root.parent)
    return "<NymeriaOS>"


def _print_command(console: Console, line: str) -> None:
    console.print(f"  {line}", style="bold", markup=False, soft_wrap=True)


def _print_bootstrap_token_handoff(token_path: Path, console: Console) -> None:
    copy_command = _bootstrap_token_copy_command(token_path)

    console.print(f"[green]Bootstrap token:[/green] {token_path}", soft_wrap=True)
    console.print(
        "The Desktop/Mobile Setup Wizard wants the `nym_...` account token "
        "from this file, not your Anthropic, OpenAI, or OpenRouter provider API key."
    )
    console.print(
        "This command reads the token from the file, so the raw token is not "
        "stored in your shell history:"
    )
    if copy_command.copies_to_clipboard:
        console.print("Copy the token:")
    else:
        console.print(
            "No clipboard helper was found; display only the token value for manual copy:"
        )
    _print_command(console, copy_command.command)


def _bootstrap_token_copy_command(token_path: Path) -> BootstrapTokenCopyCommand:
    if sys.platform == "win32":
        path = _powershell_single_quote(str(token_path))
        command = (
            'powershell -NoProfile -Command "'
            f"(Select-String -Path {path} -Pattern '{BOOTSTRAP_TOKEN_REGEX}')"
            ".Matches.Value | Select-Object -First 1 | Set-Clipboard"
            '"'
        )
        return BootstrapTokenCopyCommand(command=command, copies_to_clipboard=True)

    extract_command = _posix_token_extract_command(token_path)
    if sys.platform == "darwin":
        return BootstrapTokenCopyCommand(
            command=f"{extract_command} | pbcopy",
            copies_to_clipboard=True,
        )

    linux_clipboard_commands = (
        ("wl-copy", "wl-copy"),
        ("xclip", "xclip -selection clipboard"),
        ("xsel", "xsel --clipboard --input"),
    )
    for executable, pipe_command in linux_clipboard_commands:
        if shutil.which(executable):
            return BootstrapTokenCopyCommand(
                command=f"{extract_command} | {pipe_command}",
                copies_to_clipboard=True,
            )

    return BootstrapTokenCopyCommand(
        command=extract_command,
        copies_to_clipboard=False,
    )


def _posix_token_extract_command(token_path: Path) -> str:
    return (
        f"grep -oE '{BOOTSTRAP_TOKEN_REGEX}' {shlex.quote(str(token_path))} "
        "| head -n 1"
    )


def _powershell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _print_cliproxy_claude_commands(console: Console) -> None:
    gatekeeper = "cpx-<your-claude-gatekeeper-key>"
    console.print("\n[bold]2. Authenticate Claude through CLIProxy[/bold]")
    for line in (
        "docker exec -it cli-proxy-api-latest ./CLIProxyAPI --claude-login --no-browser",
        "open the printed URL, sign in, and let CLIProxy save auths/claude-*.json",
        (
            "python3 -c \"import glob,json; p=glob.glob('CLIProxyAPI-main/temp/latest/"
            "auths/claude-*.json')[0]; d=json.load(open(p)); "
            "d['tool_prefix_disabled']=True; json.dump(d, open(p,'w'), indent=2)\""
        ),
        "docker restart cli-proxy-api-latest",
    ):
        _print_command(console, line)

    console.print("\n[bold]3. Verify before routing Nymeria traffic[/bold]")
    for line in (
        "python3 Nymeria/tools/check_cliproxy_cloak.py \\",
        "  --base-url http://localhost:8318 \\",
        f"  --api-key {gatekeeper} \\",
        "  --auth-dir CLIProxyAPI-main/temp/latest/auths",
    ):
        _print_command(console, line)

    console.print("\n[bold]4. Nymeria config shape after verification[/bold]")
    for line in (
        "LLM_PROVIDER=anthropic",
        "LLM_BASE_URL=http://localhost:8318        # host Python/venv; root URL, no /v1",
        "LLM_BASE_URL=http://cli-proxy-api:8317   # Docker backend; root URL, no /v1",
        f"ANTHROPIC_API_KEY={gatekeeper}",
    ):
        console.print(f"  {line}")


def _print_cliproxy_codex_commands(console: Console) -> None:
    gatekeeper = "cpx-<your-codex-gatekeeper-key>"
    console.print("\n[bold]2. Authenticate Codex/OpenAI through CLIProxy[/bold]")
    for line in (
        "docker exec -it cli-proxy-api-latest ./CLIProxyAPI --codex-device-login --no-browser",
        "open the printed device URL, enter the code, and let CLIProxy save auths/codex-*.json",
    ):
        _print_command(console, line)

    console.print("\n[bold]3. Verify before routing Nymeria traffic[/bold]")
    for line in (
        "curl -s http://localhost:8318/v1/responses \\",
        f"  -H 'Authorization: Bearer {gatekeeper}' \\",
        "  -H 'Content-Type: application/json' \\",
        "  -d '{\"model\":\"gpt-5.5\",\"input\":\"Reply with ok.\",\"max_output_tokens\":16}'",
    ):
        _print_command(console, line)

    console.print("\n[bold]4. Nymeria config shape after verification[/bold]")
    for line in (
        "LLM_PROVIDER=openai",
        "OPENAI_API_MODE=responses",
        "LLM_BASE_URL=http://localhost:8318/v1        # host Python/venv; must end in /v1",
        "LLM_BASE_URL=http://cli-proxy-api:8317/v1   # Docker backend; must end in /v1",
        f"OPENAI_API_KEY={gatekeeper}",
    ):
        console.print(f"  {line}")
    console.print(
        "  EMBEDDING_API_KEY should stay unset or use a real embeddings key; "
        "do not reuse the cpx-* gatekeeper key for embeddings or voice."
    )


def _print_docker_hosting_handoff(console: Console) -> None:
    source_root = find_project_root(Path(__file__).resolve())
    source_hint = str(source_root) if source_root else "<NymeriaOS>/Nymeria"

    console.print("\n[bold]Docker Setup[/bold]")
    console.print(
        "Docker hosting currently uses the source-checkout compose flow. "
        "`nymeria init` does not write Docker env files yet, so it will not "
        "write provider credentials to config.env or .env.docker."
    )
    console.print("\nFrom a source checkout:")
    console.print(f"  [bold]cd {source_hint}[/bold]")
    console.print("  [bold]cp .env.docker.example .env.docker[/bold]")
    console.print(
        "  Edit .env.docker with LLM_PROVIDER, the matching provider API key, "
        "REDIS_PASSWORD, and POSTGRES_PASSWORD."
    )
    console.print(
        "  [bold]DISCORD_BOT_TOKEN=disabled docker compose "
        "--env-file .env.docker up -d --build[/bold]"
    )
    console.print(
        "\nNo config.env or .env.docker was written. Provider flags, if supplied, "
        "were not written."
    )


def _offer_post_setup_doctor(
    args: argparse.Namespace,
    *,
    root: Path,
    console: Console,
    non_interactive: bool,
    provider_auth_validated: bool,
) -> int:
    requested = bool(
        getattr(args, "run_doctor", False) or getattr(args, "full_doctor", False)
    )
    if not requested:
        if non_interactive:
            return 0
        console.print("\n[bold]Final Validation[/bold]")
        if not _yes_no("Run nymeria doctor now?", default=True):
            console.print("Skipped. You can run `nymeria doctor` later.")
            return 0

    include_llm_test = bool(getattr(args, "full_doctor", False))
    if not include_llm_test and not non_interactive:
        if provider_auth_validated:
            include_llm_test = _yes_no(
                "Provider auth was already tested. Run the full doctor LLM check again?",
                default=False,
            )
        else:
            include_llm_test = _yes_no(
                "Include the live provider LLM check in doctor?",
                default=False,
            )

    skip_llm_test = not include_llm_test
    command = "nymeria doctor"
    if skip_llm_test:
        command += " --skip-llm-test"
    console.print(f"\nRunning final validation: [bold]{command}[/bold]")
    result = _run_doctor_for_root(root, skip_llm_test=skip_llm_test)
    if result != 0:
        console.print(
            "[yellow]Doctor reported failed checks. Fix those before starting "
            "the backend.[/yellow]"
        )
    return result


def _run_doctor_for_root(root: Path, *, skip_llm_test: bool) -> int:
    from .doctor import run_doctor

    return run_doctor(
        argparse.Namespace(
            project_root=root,
            skip_llm_test=skip_llm_test,
        )
    )


def _resolve_next_action(
    args: argparse.Namespace,
    console: Console,
    non_interactive: bool,
) -> NextAction:
    configured = getattr(args, "next_action", None)
    if configured is not None:
        return _parse_onboarding_arg(
            NextAction,
            configured,
            option_name="--next-action",
            default=DEFAULT_NEXT_ACTION,
        )
    if non_interactive:
        return DEFAULT_NEXT_ACTION

    console.print("\n[bold]Next Action[/bold]")
    console.print("Choose what to do after setup.")
    default_index = NEXT_ACTION_ORDER.index(DEFAULT_NEXT_ACTION) + 1
    for idx, action in enumerate(NEXT_ACTION_ORDER, start=1):
        choice = NEXT_ACTION_CHOICES[action]
        suffix = " - default" if action is DEFAULT_NEXT_ACTION else ""
        console.print(f"  [{idx}] {choice.label}{suffix}")
        console.print(f"      {choice.description}")

    answer = prompt(f"> [{default_index}] ").strip()
    if not answer:
        return DEFAULT_NEXT_ACTION

    try:
        return NEXT_ACTION_ORDER[int(answer) - 1]
    except (ValueError, IndexError):
        try:
            return parse_choice(NextAction, answer, option_name="next action")
        except ValueError:
            default_choice = NEXT_ACTION_CHOICES[DEFAULT_NEXT_ACTION].label
            console.print(f"[yellow]Unknown choice, using {default_choice}.[/yellow]")
            return DEFAULT_NEXT_ACTION


def _print_next_action(next_action: NextAction, console: Console) -> None:
    if next_action is NextAction.CLI:
        console.print("\nEnter CLI chat with:")
        _print_command(console, "nymeria cli")
        return
    if next_action is NextAction.START_API_OPEN_FRONTEND:
        console.print(
            "\nProcess spawning is not reliable across every package/source "
            "environment, so start the backend in the foreground with:"
        )
        _print_command(console, "nymeria api")
        console.print("Then open http://localhost:8000 and paste the bootstrap token.")
        return

    console.print("\nStart Nymeria with:")
    _print_command(console, "nymeria api")
    console.print("Then open http://localhost:8000 and paste the bootstrap token.")


def _resolve_provider(
    args: argparse.Namespace,
    console: Console,
    non_interactive: bool,
) -> ProviderOption:
    provider_name = getattr(args, "provider", None)
    if provider_name:
        return PROVIDERS[provider_name]
    if non_interactive:
        raise SystemExit("--provider is required with --non-interactive")

    console.print("[bold]Step 3: LLM Provider[/bold]")
    for idx, key in enumerate(PROVIDER_ORDER, start=1):
        suffix = " - recommended" if key == "anthropic" else ""
        console.print(f"  [{idx}] {PROVIDERS[key].label}{suffix}")
    answer = prompt("> ").strip() or "1"
    try:
        return PROVIDERS[PROVIDER_ORDER[int(answer) - 1]]
    except (ValueError, IndexError):
        console.print("[yellow]Unknown choice, using Anthropic.[/yellow]")
        return PROVIDERS["anthropic"]


def _resolve_api_key(
    args: argparse.Namespace,
    provider: ProviderOption,
    console: Console,
    non_interactive: bool,
) -> str:
    api_key = getattr(args, "api_key", None)
    if api_key:
        return api_key.strip()
    if non_interactive:
        raise SystemExit("--api-key is required with --non-interactive")

    console.print("\n[bold]Step 5: API Key[/bold]")
    return prompt(f"Paste your {provider.label} API key: ", is_password=True).strip()


def _resolve_recommended_defaults(
    args: argparse.Namespace,
    *,
    provider: ProviderOption,
    api_key: str,
    console: Console,
) -> dict[str, str]:
    optional_env = _optional_env_from_args(args, provider=provider, api_key=api_key)
    if optional_env:
        env_names = ", ".join(sorted(optional_env))
        raise ValueError(
            "Recommended setup writes only the primary provider credential. "
            f"Optional capability keys were supplied for {env_names}; re-run "
            "with --setup-style advanced to write them."
        )

    console.print("\n[bold]Recommended Defaults[/bold]")
    console.print(
        "Using SQLite local storage, the default data directory, and no optional "
        "capability keys."
    )
    console.print(
        "Embeddings/RAG, OpenAI tools, Gemini, Perplexity, and custom data paths "
        "can be configured later with advanced setup or manual config edits."
    )
    return {}


def _resolve_optional_capabilities(
    args: argparse.Namespace,
    *,
    provider: ProviderOption,
    api_key: str,
    console: Console,
    non_interactive: bool,
) -> dict[str, str]:
    optional_env = _optional_env_from_args(args, provider=provider, api_key=api_key)
    if non_interactive:
        return optional_env

    console.print("\n[bold]Advanced Optional Capabilities[/bold]")

    if _yes_no("Enable semantic memory/RAG embeddings?", default=False):
        embedding_key = prompt(
            "Embedding API key (OpenAI-compatible): ",
            is_password=True,
        ).strip()
        if embedding_key:
            optional_env["EMBEDDING_API_KEY"] = embedding_key

    if provider.name == "openai":
        console.print("OpenAI features will use your primary OpenAI provider key.")
    elif _yes_no("Add an OpenAI API key for image generation/STT?", default=False):
        openai_key = prompt("OpenAI API key: ", is_password=True).strip()
        if openai_key:
            optional_env["OPENAI_API_KEY"] = openai_key

    if _yes_no("Add a Gemini API key for Gemini image/document/TTS tools?", default=False):
        gemini_key = prompt("Gemini API key: ", is_password=True).strip()
        if gemini_key:
            optional_env["GEMINI_API_KEY"] = gemini_key

    if _yes_no("Enable Perplexity web search?", default=False):
        perplexity_key = prompt("Perplexity API key: ", is_password=True).strip()
        if perplexity_key:
            optional_env["PERPLEXITY_API_KEY"] = perplexity_key

    return optional_env


def _optional_env_from_args(
    args: argparse.Namespace,
    *,
    provider: ProviderOption,
    api_key: str,
) -> dict[str, str]:
    optional_env: dict[str, str] = {}

    arg_mapping = {
        "embedding_api_key": "EMBEDDING_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "gemini_api_key": "GEMINI_API_KEY",
        "perplexity_api_key": "PERPLEXITY_API_KEY",
    }
    for attr, env_var in arg_mapping.items():
        value = getattr(args, attr, None)
        if value:
            optional_env[env_var] = value.strip()

    if provider.env_var == "OPENAI_API_KEY":
        openai_key = optional_env.get("OPENAI_API_KEY")
        if openai_key and openai_key != api_key:
            raise SystemExit("--openai-api-key conflicts with the primary OpenAI API key")
        optional_env.pop("OPENAI_API_KEY", None)

    return optional_env


def _resolve_model(
    args: argparse.Namespace,
    provider: ProviderOption,
    console: Console,
    non_interactive: bool,
) -> str:
    model = getattr(args, "model", None)
    if model:
        return model.strip()
    if non_interactive:
        raise SystemExit("--model is required with --non-interactive")

    console.print("\n[bold]Step 4: Model[/bold]")
    console.print(f"Recommended default: [bold]{provider.default_model}[/bold]")
    console.print(
        f"Press Enter to use it, or type another {provider.label} model identifier."
    )
    model = prompt(f"> [{provider.default_model}] ").strip()
    return model or provider.default_model


def _resolve_root(
    args: argparse.Namespace,
    console: Console,
    non_interactive: bool,
    *,
    setup_style: SetupStyle,
) -> Path:
    configured = getattr(args, "root", None)
    if configured:
        return Path(configured).expanduser().resolve()

    default_root = _default_init_root()
    if non_interactive:
        return default_root
    if setup_style is SetupStyle.RECOMMENDED:
        console.print(f"Using data directory: {default_root / 'data'}")
        return default_root

    console.print("\n[bold]Advanced Data Directory[/bold]")
    console.print(f"  [1] {default_root} (default)")
    console.print("  [2] Custom path")
    answer = prompt("> ").strip() or "1"
    if answer == "2":
        custom = prompt("Path: ").strip()
        if custom:
            return Path(custom).expanduser().resolve()
    return default_root


def _default_init_root() -> Path:
    env_root = os.environ.get("NYMERIA_PROJECT_ROOT")
    if env_root:
        root = Path(env_root).expanduser().resolve()
        if find_project_root(root) != root:
            return root
    return default_user_project_root()


def _valid_key_format(provider: ProviderOption, api_key: str) -> bool:
    return bool(api_key) and api_key.startswith(provider.key_prefix)


def _test_llm_connection(
    provider: ProviderOption,
    model: str,
    api_key: str,
) -> LLMConnectionResult:
    try:
        if provider.name == "anthropic":
            _post_json(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": model,
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "Reply with ok."}],
                },
            )
        elif provider.name == "openai":
            _post_json(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "input": "Reply with ok.",
                    "max_output_tokens": 16,
                },
            )
        elif provider.name == "openrouter":
            _post_json(
                "https://openrouter.ai/api/v1/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "HTTP-Referer": "https://github.com/ManningAskew7/NymeriaOS",
                    "X-Title": "Nymeria",
                },
                json={
                    "model": model,
                    "input": "Reply with ok.",
                    "max_output_tokens": 16,
                },
            )
        else:
            raise LLMConnectionError(f"Unsupported provider: {provider.name}")
    except httpx.TimeoutException as exc:
        raise LLMConnectionError("provider did not respond before the 15s timeout") from exc
    except httpx.HTTPStatusError as exc:
        detail = _http_error_detail(exc.response)
        raise LLMConnectionError(
            f"{provider.label} returned HTTP {exc.response.status_code}: {detail}"
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMConnectionError(str(exc)) from exc

    return LLMConnectionResult(model=model)


def _post_json(
    url: str,
    *,
    headers: Mapping[str, str],
    json: Mapping[str, Any],
) -> None:
    request_headers = {
        "Content-Type": "application/json",
        **headers,
    }
    with httpx.Client(timeout=15.0) as client:
        response = client.post(url, headers=request_headers, json=json)
        response.raise_for_status()


def _http_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:300] or response.reason_phrase

    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()[:300]
        message = body.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:300]
    return response.reason_phrase


def _check_writable(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    probe = root / ".write-test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink(missing_ok=True)


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _yes_no(question: str, *, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = prompt(f"{question} {suffix} ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _write_config(
    config_path: Path,
    provider: ProviderOption,
    model: str,
    api_key: str,
    data_dir: Path,
    optional_env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    optional_env = optional_env or {}
    extra_env = extra_env or {}
    lines = [
        "# Generated by `nymeria init`.",
        f"LLM_PROVIDER={provider.name}",
        f"LLM_MODEL={_env_value(model)}",
        f"{provider.env_var}={api_key}",
        "DATABASE_BACKEND=sqlite",
        f"NYMERIA_DATA_DIR={_env_value(str(data_dir))}",
        "API_HOST=0.0.0.0",
        "API_PORT=8000",
    ]
    extra_lines = [
        f"{env_var}={_env_value(value)}"
        for env_var, value in extra_env.items()
        if value
    ]
    if extra_lines:
        lines[4:4] = extra_lines
    for env_var in OPTIONAL_ENV_ORDER:
        value = optional_env.get(env_var)
        if value and env_var != provider.env_var:
            lines.append(f"{env_var}={_env_value(value)}")
    lines.append("")
    config_path.write_text("\n".join(lines), encoding="utf-8")
    try:
        config_path.chmod(0o600)
    except OSError:
        pass  # chmod may fail on filesystems that do not support POSIX modes


def _env_value(value: str) -> str:
    if value and all(c.isalnum() or c in "/._:-" for c in value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize Nymeria configuration")
    parser.add_argument("--provider", choices=tuple(PROVIDERS), default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument(
        "--hosting",
        choices=choice_values(HostingOption),
        default=None,
        help="Onboarding hosting profile",
    )
    parser.add_argument(
        "--auth-method",
        choices=choice_values(ProviderAuthMethod),
        default=None,
        help="Provider authentication method",
    )
    parser.add_argument(
        "--setup-style",
        choices=choice_values(SetupStyle),
        default=None,
        help="Amount of setup detail to collect",
    )
    parser.add_argument(
        "--next-action",
        choices=choice_values(NextAction),
        default=None,
        help="Post-setup handoff action",
    )
    parser.add_argument(
        "--cliproxy-root",
        default=None,
        help="CLIProxy temp/latest directory for OAuth setup",
    )
    parser.add_argument(
        "--cliproxy-base-url",
        default=None,
        help="Host-reachable CLIProxy root URL for Claude OAuth setup",
    )
    parser.add_argument(
        "--embedding-api-key",
        default=None,
        help="Optional OpenAI-compatible key for RAG/memory/skill embeddings",
    )
    parser.add_argument(
        "--openai-api-key",
        default=None,
        help="Optional OpenAI key for image generation/STT/OpenAI-backed tools",
    )
    parser.add_argument(
        "--gemini-api-key",
        default=None,
        help="Optional Gemini key for Gemini image, document extraction, and TTS tools",
    )
    parser.add_argument(
        "--perplexity-api-key",
        default=None,
        help="Optional Perplexity key for web search",
    )
    parser.add_argument("--root", default=None, help="Runtime root for config.env and data/")
    parser.add_argument("--force", action="store_true", help="Overwrite config.env if it exists")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Require flags instead of prompting",
    )
    parser.add_argument(
        "--skip-llm-test",
        action="store_true",
        help="Write config without making the provider smoke-test API call",
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
    return run_init(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
