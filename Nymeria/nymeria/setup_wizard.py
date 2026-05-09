"""First-run setup for packaged Nymeria installations."""

from __future__ import annotations

import argparse
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx
from prompt_toolkit import prompt
from rich.console import Console

from ._runtime_paths import default_user_project_root, find_project_root
from .core.accounts import AccountsRepo, BOOTSTRAP_TOKEN_FILENAME
from .onboarding import (
    HOSTING_CHOICES,
    HOSTING_ORDER,
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
DEFAULT_SETUP_STYLE = SetupStyle.ADVANCED
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

    if onboarding.setup_style is SetupStyle.RECOMMENDED and not non_interactive:
        console.print("\n[bold]Step 5/7: Optional Capabilities[/bold]")
        console.print("Using recommended defaults; optional keys can be added later.")
        optional_env = _optional_env_from_args(args, provider=provider, api_key=api_key)
    else:
        optional_env = _resolve_optional_capabilities(
            args,
            provider=provider,
            api_key=api_key,
            console=console,
            non_interactive=non_interactive,
        )

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
    console.print("\n[bold]Step 7/7: Configuration[/bold]")
    _write_config(config_path, provider, model, api_key, data_dir, optional_env)

    repo = AccountsRepo(data_dir / "accounts.db")
    repo.ensure_bootstrap_admin(data_dir)
    token_path = data_dir / BOOTSTRAP_TOKEN_FILENAME

    console.print(f"[green]Config:[/green] {config_path}")
    console.print(f"[green]Data dir:[/green] {data_dir}")
    console.print(f"[green]Bootstrap token:[/green] {token_path}")
    _print_next_action(onboarding.next_action, console)
    return 0


def _resolve_onboarding_selection(
    args: argparse.Namespace,
    console: Console,
    non_interactive: bool,
) -> OnboardingSelection:
    return OnboardingSelection(
        hosting=_resolve_hosting(args, console, non_interactive),
        auth_method=_parse_onboarding_arg(
            ProviderAuthMethod,
            getattr(args, "auth_method", None),
            option_name="--auth-method",
            default=DEFAULT_AUTH_METHOD,
        ),
        setup_style=_parse_onboarding_arg(
            SetupStyle,
            getattr(args, "setup_style", None),
            option_name="--setup-style",
            default=DEFAULT_SETUP_STYLE,
        ),
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

    console.print("[bold]Step 1/7: Hosting / Security[/bold]")
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
    console.print(f"  {line}", style="bold", markup=False)


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


def _print_next_action(next_action: NextAction, console: Console) -> None:
    if next_action is NextAction.CLI:
        console.print("\nEnter CLI chat with:\n  [bold]nymeria cli[/bold]\n")
        return
    if next_action is NextAction.START_API_OPEN_FRONTEND:
        console.print("\nStart Nymeria with:\n  [bold]nymeria api[/bold]\n")
        console.print("Then open http://localhost:8000 and paste the bootstrap token.")
        return

    console.print("\nStart Nymeria with:\n  [bold]nymeria api[/bold]\n")
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

    console.print("[bold]Step 2/7: LLM Provider[/bold]")
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

    console.print("\n[bold]Step 4/7: API Key[/bold]")
    return prompt(f"Paste your {provider.label} API key: ", is_password=True).strip()


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

    console.print("\n[bold]Step 5/7: Optional Capabilities[/bold]")

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

    console.print("\n[bold]Step 3/7: Model[/bold]")
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
) -> Path:
    configured = getattr(args, "root", None)
    if configured:
        return Path(configured).expanduser().resolve()

    default_root = _default_init_root()
    if non_interactive:
        return default_root

    console.print("\n[bold]Step 6/7: Data Directory[/bold]")
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
) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    optional_env = optional_env or {}
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
    return run_init(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
