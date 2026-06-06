"""Headless finalize step: write config, bootstrap the admin, hand off.

Shared by the interactive Textual wizard (after the app exits and the terminal
is restored) and the non-interactive flag path. All of the secret-handling and
file-writing logic here is lifted unchanged in behavior from the previous setup
wizard so the hard-won atomic-write / token-handoff guarantees are preserved.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import socket
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from rich.console import Console

from .._runtime_paths import default_user_project_root, find_project_root
from ..config.llm_providers import LLMProviderSpec
from ..core.accounts import AccountsRepo, BOOTSTRAP_TOKEN_FILENAME
from ..onboarding import (
    EXTERNAL_ACCESS_CHOICES,
    IMAGE_TIER_CHOICES,
    SECURITY_PROFILE_CHOICES,
    ExternalAccess,
    HostingOption,
    NextAction,
)
from .providers import (
    LLMConnectionError,
    OPTIONAL_ENV_ORDER,
    check_llm_connection_for_spec,
    valid_key_format_for_spec,
)
from .state import WizardState

BOOTSTRAP_TOKEN_REGEX = r"nym_[A-Za-z0-9_-]+"


class FinalizeError(RuntimeError):
    """Raised when finalize cannot complete and should stop with a clear message."""


@dataclass(frozen=True)
class BootstrapTokenCopyCommand:
    command: str
    copies_to_clipboard: bool


def finalize(
    state: WizardState,
    *,
    console: Console,
    non_interactive: bool,
    overwrite_confirmed: bool = False,
) -> int:
    """Validate the collected state, write config, and bootstrap the admin."""

    spec = state.provider_spec()
    api_key = state.api_key.strip()
    base_url = state.base_url.strip()
    model = ""
    provider_auth_validated = False
    if spec is None or (spec.requires_api_key and not api_key):
        # Provider step was skipped (or a required key is missing); write a
        # usable config without LLM creds.
        spec = None
        api_key = ""
        console.print(
            "[yellow]No LLM provider configured. Set one later with "
            "`nymeria init` or by editing config.env.[/yellow]"
        )
    else:
        model = (state.model or spec.default_model or "").strip()
        ok, prefix = valid_key_format_for_spec(spec, api_key)
        if not ok and prefix:
            console.print(
                f"[red]The {spec.label} key should start with `{prefix}`.[/red]"
            )
            return 2
        if not model:
            console.print(
                f"[red]No model set for {spec.label}. Re-run `nymeria init` and "
                "choose a model.[/red]"
            )
            return 2
        if spec.requires_base_url and not base_url:
            console.print(
                f"[red]{spec.label} needs a base URL. Re-run with --base-url.[/red]"
            )
            return 2
        if state.skip_llm_test:
            console.print("[yellow]Skipping LLM connection test.[/yellow]")
        else:
            console.print("\nTesting LLM connection...")
            try:
                result = check_llm_connection_for_spec(
                    spec, model, api_key, base_url=base_url or None
                )
            except LLMConnectionError as exc:
                console.print(f"[red]LLM connection failed:[/red] {exc}")
                return 2
            if result.tested:
                console.print(f"[green]Connected:[/green] {result.model}")
                provider_auth_validated = True
            else:
                console.print(
                    f"[yellow]Skipped live test for {spec.label} "
                    "(cannot verify without extra setup).[/yellow]"
                )

    root = resolve_root(state)
    data_dir = resolve_data_dir(state, root=root)
    try:
        check_writable(root)
        check_writable(data_dir)
    except OSError as exc:
        console.print(f"[red]Cannot write setup files: {exc}[/red]")
        return 2

    if port_in_use(8000):
        console.print("[yellow]Warning:[/yellow] port 8000 is already in use.")

    config_path = root / "config.env"
    if config_path.exists() and not state.force and not overwrite_confirmed:
        if non_interactive:
            console.print(
                f"[red]{config_path} already exists. Re-run with --force to "
                "overwrite.[/red]"
            )
            return 2
        # Interactive callers confirm via the review screen; reaching here
        # without that confirmation means we should not clobber silently.
        console.print(
            f"[red]{config_path} already exists. Re-run with --force to "
            "overwrite.[/red]"
        )
        return 2

    optional_env = _resolve_optional_env(state, spec=spec, api_key=api_key)
    extra_env = _resolve_extra_env(state)

    data_dir.mkdir(parents=True, exist_ok=True)
    console.print("\n[bold]Configuration[/bold]")
    write_config(
        config_path,
        data_dir=data_dir,
        spec=spec,
        model=model,
        api_key=api_key,
        optional_env=optional_env,
        extra_env=extra_env,
    )

    repo = AccountsRepo(data_dir / "accounts.db")
    repo.ensure_bootstrap_admin(data_dir)
    token_path = data_dir / BOOTSTRAP_TOKEN_FILENAME

    console.print(f"[green]Config:[/green] {config_path}")
    console.print(f"[green]Data dir:[/green] {data_dir}")
    print_bootstrap_token_handoff(token_path, console)
    print_capability_summary(spec, optional_env, console)
    print_deployment_summary(state, console)

    doctor_status = _maybe_run_doctor(
        state,
        root=root,
        console=console,
        provider_auth_validated=provider_auth_validated,
    )
    if doctor_status != 0:
        return doctor_status

    print_next_action(state, console)
    return 0


# --- config writing ---------------------------------------------------------


def write_config(
    config_path: Path,
    *,
    data_dir: Path,
    spec: LLMProviderSpec | None = None,
    model: str = "",
    api_key: str = "",
    optional_env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> None:
    """Atomically write config.env with 0600 perms (it holds API keys).

    When `spec` is None (the provider step was skipped), the LLM lines are
    omitted so the backend still starts and a provider can be set later. The key
    is written to the provider's highest-priority env var; for Anthropic that is
    `ANTHROPIC_DIRECT_API_KEY` (the direct, non-proxy key), not `ANTHROPIC_API_KEY`.
    """

    config_path.parent.mkdir(parents=True, exist_ok=True)
    optional_env = optional_env or {}
    extra_env = extra_env or {}
    provider_env = spec.api_key_env_vars[0] if (spec and spec.api_key_env_vars) else None
    lines = ["# Generated by `nymeria init`."]
    if spec is not None:
        lines.append(f"LLM_PROVIDER={spec.id}")
        lines.append(f"LLM_MODEL={_env_value(model)}")
        if api_key and provider_env:
            lines.append(f"{provider_env}={api_key}")
    for env_var, value in extra_env.items():
        if value:
            lines.append(f"{env_var}={_env_value(value)}")
    lines.append("DATABASE_BACKEND=sqlite")
    lines.append(f"NYMERIA_DATA_DIR={_env_value(str(data_dir))}")
    lines.append("API_HOST=0.0.0.0")
    lines.append("API_PORT=8000")
    for env_var in OPTIONAL_ENV_ORDER:
        value = optional_env.get(env_var)
        if value and env_var != provider_env:
            lines.append(f"{env_var}={_env_value(value)}")
    lines.append("")

    content = "\n".join(lines)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(config_path.parent), prefix=f".{config_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass  # chmod may fail on filesystems without POSIX modes
        os.replace(tmp_name, config_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass  # best-effort cleanup; re-raise the original error
        raise


def _env_value(value: str) -> str:
    if value and all(c.isalnum() or c in "/._:-" for c in value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _resolve_optional_env(
    state: WizardState,
    *,
    spec: LLMProviderSpec | None,
    api_key: str,
) -> dict[str, str]:
    optional_env = {k: v.strip() for k, v in state.optional_env.items() if v}
    if spec is not None and "OPENAI_API_KEY" in spec.api_key_env_vars:
        # The primary key already writes OPENAI_API_KEY; never duplicate it.
        optional_env.pop("OPENAI_API_KEY", None)
    return optional_env


def _resolve_extra_env(state: WizardState) -> dict[str, str]:
    extra: dict[str, str] = {}
    if state.base_url:
        extra["LLM_BASE_URL"] = state.base_url.strip()
    if state.api_mode:
        extra["OPENAI_API_MODE"] = state.api_mode.strip()
    return extra


# --- paths ------------------------------------------------------------------


def resolve_root(state: WizardState) -> Path:
    if state.root is not None:
        return Path(state.root).expanduser().resolve()
    return default_init_root()


def resolve_data_dir(state: WizardState, *, root: Path) -> Path:
    if state.data_dir is not None:
        return Path(state.data_dir).expanduser().resolve()
    return root / "data"


def default_init_root() -> Path:
    env_root = os.environ.get("NYMERIA_PROJECT_ROOT")
    if env_root:
        root = Path(env_root).expanduser().resolve()
        if find_project_root(root) != root:
            return root
    return default_user_project_root()


def check_writable(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    probe = root / ".write-test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink(missing_ok=True)


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# --- bootstrap token handoff ------------------------------------------------


def print_bootstrap_token_handoff(token_path: Path, console: Console) -> None:
    copy_command = bootstrap_token_copy_command(token_path)

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


def bootstrap_token_copy_command(token_path: Path) -> BootstrapTokenCopyCommand:
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

    return BootstrapTokenCopyCommand(command=extract_command, copies_to_clipboard=False)


def _posix_token_extract_command(token_path: Path) -> str:
    return (
        f"grep -oE '{BOOTSTRAP_TOKEN_REGEX}' {shlex.quote(str(token_path))} "
        "| head -n 1"
    )


def _powershell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# --- capability summary -----------------------------------------------------


def print_capability_summary(
    spec: LLMProviderSpec | None,
    optional_env: Mapping[str, str],
    console: Console,
) -> None:
    """Show which capabilities are ready and which env var unblocks each.

    Derived from what was written to config.env this run. As the placeholder
    capability steps get real, this can move to the runtime capability
    resolvers without changing the output shape.
    """

    openai_ready = (
        spec is not None and "OPENAI_API_KEY" in spec.api_key_env_vars
    ) or bool(optional_env.get("OPENAI_API_KEY"))
    rows = [
        ("Primary LLM", spec is not None, "set a provider with nymeria init"),
        (
            "Semantic memory / RAG",
            bool(optional_env.get("EMBEDDING_API_KEY")),
            "set EMBEDDING_API_KEY",
        ),
        (
            "OpenAI image / speech tools",
            openai_ready,
            "set OPENAI_API_KEY",
        ),
        (
            "Gemini media tools",
            bool(optional_env.get("GEMINI_API_KEY")),
            "set GEMINI_API_KEY",
        ),
        (
            "Perplexity web search",
            bool(optional_env.get("PERPLEXITY_API_KEY")),
            "set PERPLEXITY_API_KEY",
        ),
    ]
    ready = sum(1 for _name, ok, _hint in rows if ok)
    console.print(f"\n[bold]Capabilities[/bold] ({ready}/{len(rows)} ready)")
    for name, ok, hint in rows:
        if ok:
            console.print(f"  [green]ok[/green] {name}")
        else:
            console.print(f"  [yellow]--[/yellow] {name} ({hint})")


def print_deployment_summary(state: WizardState, console: Console) -> None:
    """Echo the deployment-shaping choices the installer cannot fully act on yet.

    Image tier, security profile, and non-local external access are recorded by
    the wizard but their automation (image building, the approval gate, tunnel
    setup) is not built, so they are surfaced here rather than silently dropped.
    """

    rows: list[tuple[str, str, str]] = []
    if state.image_tier is not None and state.hosting is HostingOption.DOCKER:
        rows.append(
            (
                "Image tier",
                IMAGE_TIER_CHOICES[state.image_tier].label,
                "image building is not wired into setup yet",
            )
        )
    if state.security_profile is not None:
        rows.append(
            (
                "Security profile",
                SECURITY_PROFILE_CHOICES[state.security_profile].label,
                "enforcement is being built out",
            )
        )
    if (
        state.external_access is not None
        and state.external_access is not ExternalAccess.LOCAL_ONLY
    ):
        rows.append(
            (
                "External access",
                EXTERNAL_ACCESS_CHOICES[state.external_access].label,
                "set up separately, see the remote-access doc",
            )
        )
    if not rows:
        return
    console.print("\n[bold]Deployment choices[/bold] (recorded, not yet automated)")
    for name, value, note in rows:
        console.print(f"  {name}: {value} ({note})")


# --- next action and doctor -------------------------------------------------


def print_next_action(state: WizardState, console: Console) -> None:
    start_command = _start_command_for_hosting(state.hosting)

    if state.next_action is NextAction.CLI:
        console.print("\nEnter CLI chat with:")
        _print_command(console, "nymeria cli")
    elif state.hosting is HostingOption.DOCKER:
        console.print("\nStart Nymeria (single-container Docker):")
        _print_command(console, start_command)
    elif state.hosting is HostingOption.SERVICE:
        console.print(
            "\nBackground-service install is not wired up yet. For now start "
            "Nymeria in the foreground with:"
        )
        _print_command(console, start_command)
    else:
        console.print("\nStart Nymeria with:")
        _print_command(console, start_command)

    if state.next_action is not NextAction.CLI:
        console.print("Then open http://localhost:8000 and paste the bootstrap token.")
    console.print("\nRe-run setup anytime with `nymeria init`. Check health with `nymeria doctor`.")


def _start_command_for_hosting(hosting: HostingOption | None) -> str:
    if hosting is HostingOption.DOCKER:
        return "docker compose -f docker-compose.single.yml up -d"
    return "nymeria slim"


def _maybe_run_doctor(
    state: WizardState,
    *,
    root: Path,
    console: Console,
    provider_auth_validated: bool,
) -> int:
    if not (state.run_doctor or state.full_doctor):
        return 0
    skip_llm_test = not state.full_doctor
    command = "nymeria doctor"
    if skip_llm_test:
        command += " --skip-llm-test"
    console.print(f"\nRunning final validation: [bold]{command}[/bold]")
    result = run_doctor_for_root(root, skip_llm_test=skip_llm_test)
    if result != 0:
        console.print(
            "[yellow]Doctor reported failed checks. Fix those before starting "
            "the backend.[/yellow]"
        )
    return result


def run_doctor_for_root(root: Path, *, skip_llm_test: bool) -> int:
    from ..doctor import run_doctor

    return run_doctor(
        argparse.Namespace(project_root=root, skip_llm_test=skip_llm_test)
    )


def _print_command(console: Console, line: str) -> None:
    console.print(f"  {line}", style="bold", markup=False, soft_wrap=True)


__all__ = [
    "FinalizeError",
    "BootstrapTokenCopyCommand",
    "finalize",
    "write_config",
    "resolve_root",
    "resolve_data_dir",
    "default_init_root",
    "check_writable",
    "port_in_use",
    "print_bootstrap_token_handoff",
    "bootstrap_token_copy_command",
    "print_capability_summary",
    "print_deployment_summary",
    "print_next_action",
    "run_doctor_for_root",
]
