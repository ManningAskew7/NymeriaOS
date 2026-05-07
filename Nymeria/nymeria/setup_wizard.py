"""First-run setup for packaged Nymeria installations."""

from __future__ import annotations

import argparse
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import prompt
from rich.console import Console

from ._runtime_paths import default_user_project_root, find_project_root
from .core.accounts import AccountsRepo, BOOTSTRAP_TOKEN_FILENAME


@dataclass(frozen=True)
class ProviderOption:
    name: str
    label: str
    env_var: str
    key_prefix: str


PROVIDERS = {
    "anthropic": ProviderOption(
        name="anthropic",
        label="Anthropic (Claude)",
        env_var="ANTHROPIC_API_KEY",
        key_prefix="sk-ant-",
    ),
    "openai": ProviderOption(
        name="openai",
        label="OpenAI (GPT)",
        env_var="OPENAI_API_KEY",
        key_prefix="sk-",
    ),
    "openrouter": ProviderOption(
        name="openrouter",
        label="OpenRouter (multi-model)",
        env_var="OPENROUTER_API_KEY",
        key_prefix="sk-or-",
    ),
}

PROVIDER_ORDER = ("anthropic", "openai", "openrouter")


def run_init(args: argparse.Namespace) -> int:
    """Run the interactive or flag-driven first-run setup."""
    console = Console()
    non_interactive = bool(getattr(args, "non_interactive", False))

    console.print("\n[bold]Welcome to Nymeria.[/bold] Let's get you set up.\n")

    provider = _resolve_provider(args, console, non_interactive)
    model = _resolve_model(args, provider, console, non_interactive)
    api_key = _resolve_api_key(args, provider, console, non_interactive)
    if not _valid_key_format(provider, api_key):
        console.print(
            f"[red]The {provider.label} key should start with "
            f"`{provider.key_prefix}`.[/red]"
        )
        return 2

    root = _resolve_root(args, console, non_interactive)
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
    _write_config(config_path, provider, model, api_key, data_dir)

    repo = AccountsRepo(data_dir / "accounts.db")
    repo.ensure_bootstrap_admin(data_dir)
    token_path = data_dir / BOOTSTRAP_TOKEN_FILENAME

    console.print(f"[green]Config:[/green] {config_path}")
    console.print(f"[green]Data dir:[/green] {data_dir}")
    console.print(f"[green]Bootstrap token:[/green] {token_path}")
    console.print("\nStart Nymeria with:\n  [bold]nymeria api[/bold]\n")
    console.print("Then open http://localhost:8000 and paste the bootstrap token.")
    return 0


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

    console.print("[bold]Step 1/5: LLM Provider[/bold]")
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

    console.print("\n[bold]Step 3/5: API Key[/bold]")
    return prompt(f"Paste your {provider.label} API key: ", is_password=True).strip()


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

    console.print("\n[bold]Step 2/5: Model[/bold]")
    console.print(f"Enter the model identifier to use with {provider.label}.")
    while True:
        model = prompt("> ").strip()
        if model:
            return model
        console.print("[yellow]Model is required.[/yellow]")


def _resolve_root(
    args: argparse.Namespace,
    console: Console,
    non_interactive: bool,
) -> Path:
    configured = getattr(args, "root", None)
    if configured:
        return Path(configured).expanduser().resolve()

    default_root = _default_init_root()
    if non_interactive:
        return default_root

    console.print("\n[bold]Step 4/5: Data Directory[/bold]")
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


def _check_writable(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    probe = root / ".write-test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink(missing_ok=True)


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _write_config(
    config_path: Path,
    provider: ProviderOption,
    model: str,
    api_key: str,
    data_dir: Path,
) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Generated by `nymeria init`.",
        f"LLM_PROVIDER={provider.name}",
        f"LLM_MODEL={_env_value(model)}",
        f"{provider.env_var}={api_key}",
        "DATABASE_BACKEND=sqlite",
        f"NYMERIA_DATA_DIR={_env_value(str(data_dir))}",
        "API_HOST=0.0.0.0",
        "API_PORT=8000",
        "",
    ]
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
    parser.add_argument("--root", default=None, help="Runtime root for config.env and data/")
    parser.add_argument("--force", action="store_true", help="Overwrite config.env if it exists")
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Require flags instead of prompting",
    )
    return run_init(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
