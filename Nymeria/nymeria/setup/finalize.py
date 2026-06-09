"""Headless finalize step: write config, bootstrap the admin, hand off.

Shared by the interactive Textual wizard (after the app exits and the terminal
is restored) and the non-interactive flag path. All of the secret-handling and
file-writing logic here is lifted unchanged in behavior from the previous setup
wizard so the hard-won atomic-write / token-handoff guarantees are preserved.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from rich.console import Console

from .._runtime_paths import default_user_project_root, find_project_root
from ..config.env_file import format_env_value, write_env_file
from ..config.llm_providers import LLMProviderSpec
from ..core import secrets as nymeria_secrets
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
from .rag_catalog import apply_quickstart_rag, rag_env_for_state
from .state import WizardState
from .tool_seed import docker_init_seed_env

BOOTSTRAP_TOKEN_REGEX = r"nym_[A-Za-z0-9_-]+"

# Single-container Docker (slim) artifacts. The compose file reads `.env.docker`
# and runs `python run.py slim` as service `nymeria-single`, which mints the
# bootstrap admin + token into its `/data` volume on first boot.
DOCKER_SINGLE_COMPOSE = "docker-compose.single.yml"
DOCKER_SINGLE_SERVICE = "nymeria-single"


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
    merge: bool = False,
    scoped_section: str | None = None,
) -> int:
    """Validate the collected state, write config, and bootstrap the admin.

    `merge` (reconfigure mode) merges this run's keys into an existing config file
    instead of overwriting it, updates the bootstrap profile's picks in place, and
    suppresses the one-time token re-print. `scoped_section` (an `init <section>`
    jump) further limits the run to a concise "Updated <section>" outcome: no
    bootstrap-token handoff, no post-setup launch, no doctor.
    """

    # Out-of-the-box RAG: if nothing RAG-related was configured (no embedder chosen
    # and no embedding key supplied), equip the free, private local stack
    # (granite + Ettin) so semantic memory works with no API key and no cost. This
    # covers the interactive embedder skip (which already applies it for an
    # accurate review screen, making this a no-op then), a future quick path, and
    # an unattended --non-interactive run that named no embedder. A bare
    # --embedding-api-key with no embedder is respected as an intent to use the
    # keyed OpenAI default, so it is left untouched. Degrades to BM25 until the
    # optional local-rag extra is installed, at which point vectors light up with
    # no reconfigure (the vec0 width is already sized for granite).
    if state.embedder is None and not state.optional_env.get("EMBEDDING_API_KEY"):
        apply_quickstart_rag(state)

    spec = state.provider_spec()
    api_key = state.api_key.strip()
    base_url = state.base_url.strip()
    model = ""
    provider_auth_validated = False
    # Reconfigure: an empty key field means "keep the key already on disk", so a
    # provider whose key is present must not be downgraded to "unconfigured".
    key_present = bool(
        spec
        and spec.api_key_env_vars
        and spec.api_key_env_vars[0] in state.present_env_keys
    )
    if spec is None or (spec.requires_api_key and not api_key and not key_present):
        # Provider step was skipped (or a required key is missing); write a
        # usable config without LLM creds.
        spec = None
        api_key = ""
        console.print(
            "[yellow]No LLM provider configured. Set one later with "
            "`nymeria init` or by editing config.env.[/yellow]"
        )
    else:
        # Reconfigure with a blank key field: keep the on-disk key. There is no
        # value to format-check or live-test, so skip both and let the merge-write
        # preserve the existing key line.
        keep_existing_key = not api_key and key_present
        model = (state.model or spec.default_model or "").strip()
        if api_key:
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
        if state.skip_llm_test or keep_existing_key:
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

    # Config target is shape-aware: local/service write `config.env` (read by
    # run.py from the runtime root); the Docker single-container shape writes
    # `.env.docker` next to the compose file that reads it via `env_file`.
    for_docker = state.hosting is HostingOption.DOCKER
    root = resolve_runtime_root(state, for_docker=for_docker)
    data_dir = resolve_data_dir(state, root=root)
    if for_docker and state.root is None and source_checkout_root() is None:
        console.print(
            "[yellow]Could not find docker-compose.single.yml in a source "
            "checkout.[/yellow] Writing .env.docker to "
            f"{root}; move it next to the compose file before bringing the "
            "container up."
        )
    try:
        check_writable(root)
        if not for_docker:
            check_writable(data_dir)
    except OSError as exc:
        console.print(f"[red]Cannot write setup files: {exc}[/red]")
        return 2

    if port_in_use(8000):
        console.print("[yellow]Warning:[/yellow] port 8000 is already in use.")

    config_path = root / (".env.docker" if for_docker else "config.env")
    if merge and not config_path.exists():
        # Reconfigure whose hosting shape changed (the source file was a different
        # shape). Fall back to a fresh write of the new-shape file and note it.
        merge = False
        console.print(
            "[yellow]Hosting shape changed; writing a new config file.[/yellow]"
        )
    if config_path.exists() and not merge and not state.force and not overwrite_confirmed:
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
    secrets_key = _resolve_secrets_key(config_path)
    # Docker owns its `/data` volume, so the host cannot seed the bootstrap
    # profile (that is why the Docker branch below skips seed_bootstrap_profile).
    # Carry the picks into the container via `.env.docker` instead; it reads them
    # once on first boot. Empty for a no-pick install (writes nothing extra).
    init_seed_env = docker_init_seed_env(state) if for_docker else None

    if not for_docker:
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
        secrets_key=secrets_key,
        for_docker=for_docker,
        merge=merge,
        init_seed_env=init_seed_env,
    )

    console.print(f"[green]Config:[/green] {config_path}")
    if for_docker:
        # The container owns its data: it mints the bootstrap admin + token into
        # its `/data` volume on first boot. A host-side bootstrap would be
        # invisible to it and would print the wrong token, so skip it and read
        # the real token back from the running container (see run_next_action).
        console.print(
            "[green]Data:[/green] container-managed volume (the backend creates "
            "its admin and bootstrap token on first start)"
        )
    else:
        repo = AccountsRepo(data_dir / "accounts.db")
        admin_token = repo.ensure_bootstrap_admin(data_dir)
        token_path = data_dir / BOOTSTRAP_TOKEN_FILENAME
        console.print(f"[green]Data dir:[/green] {data_dir}")
        if merge:
            update_bootstrap_profile(
                data_dir, state, console, scoped_section=scoped_section
            )
        else:
            seed_bootstrap_profile(data_dir, state, console)
        # Print the one-time bootstrap token only when the admin was just created
        # this run; a reconfigure of an existing install must not re-print a token
        # that was already consumed.
        if admin_token is not None:
            print_bootstrap_token_handoff(token_path, console)

    if scoped_section is not None:
        # A focused `init <section>` jump: report the one change and stop. No token
        # handoff, post-setup launch, or doctor for a single-setting edit.
        console.print(f"\n[green]Updated[/green] the {scoped_section} settings.")
        return 0

    print_capability_summary(spec, optional_env, console, extra_env=extra_env)
    print_deployment_summary(state, console)

    doctor_status = _maybe_run_doctor(
        state,
        root=root,
        console=console,
        provider_auth_validated=provider_auth_validated,
    )
    if doctor_status != 0:
        return doctor_status

    return run_next_action(state, console, root=root)


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
    secrets_key: str = "",
    for_docker: bool = False,
    merge: bool = False,
    init_seed_env: Mapping[str, str] | None = None,
) -> None:
    """Atomically write the env file with 0600 perms (it holds API keys).

    When `spec` is None (the provider step was skipped), the LLM lines are
    omitted so the backend still starts and a provider can be set later. The key
    is written to the provider's highest-priority env var; for Anthropic that is
    `ANTHROPIC_DIRECT_API_KEY` (the direct, non-proxy key), not `ANTHROPIC_API_KEY`.

    `for_docker` selects the Docker single-container flavor (a `.env.docker`):
    the storage/host lines (`DATABASE_BACKEND`, `NYMERIA_DATA_DIR`, `API_HOST`)
    are omitted because the compose file sets `NYMERIA_DATA_DIR=/data` and slim
    forces sqlite + redis-off inside the container; writing a host data dir here
    would only mislead. `secrets_key`, when set, is the credential-vault Fernet
    key (`NYMERIA_SECRETS_KEY`) and is written for every shape.

    `merge` (reconfigure) overlays only the keys this run produces onto the
    existing file, preserving untouched lines and comments. An omitted key (e.g.
    the provider key when the field was left blank to keep the existing one) is
    left as-is on disk rather than blanked.

    `init_seed_env` (Docker shape only) carries the bootstrap admin's tool/skill
    picks into the container, which reads them once on first boot
    (`setup/tool_seed.docker_init_seed_env`, contract in `config/init_seed_env.py`).
    The values are `:`-joined identifier lists, so they write unquoted.
    """

    config_path.parent.mkdir(parents=True, exist_ok=True)
    optional_env = optional_env or {}
    extra_env = extra_env or {}
    init_seed_env = init_seed_env or {}
    provider_env = spec.api_key_env_vars[0] if (spec and spec.api_key_env_vars) else None
    produced: list[tuple[str, str]] = []
    if spec is not None:
        produced.append(("LLM_PROVIDER", spec.id))
        produced.append(("LLM_MODEL", _env_value(model)))
        if api_key and provider_env:
            produced.append((provider_env, api_key))
    for env_var, value in extra_env.items():
        if value:
            produced.append((env_var, _env_value(value)))
    if not for_docker:
        produced.append(("DATABASE_BACKEND", "sqlite"))
        produced.append(("NYMERIA_DATA_DIR", _env_value(str(data_dir))))
        produced.append(("API_HOST", "0.0.0.0"))
    produced.append(("API_PORT", "8000"))
    for env_var in OPTIONAL_ENV_ORDER:
        value = optional_env.get(env_var)
        if value and env_var != provider_env:
            produced.append((env_var, _env_value(value)))
    if secrets_key:
        # Credential-vault encryption key (Fernet). Without it the first vault
        # write (OAuth connect, BYO bot token, integration secret) raises
        # SecretsKeyMissing. Read straight from the env by nymeria/core/secrets.py.
        produced.append(("NYMERIA_SECRETS_KEY", _env_value(secrets_key)))
    for env_var, value in init_seed_env.items():
        # Docker first-boot pick carriers (`:`-joined name lists). Already in the
        # safe set, so `_env_value` leaves them unquoted for Docker `env_file`.
        if value:
            produced.append((env_var, _env_value(value)))

    # Reconfigure overlays produced keys onto the existing file; first-run writes
    # a fresh file with the generated-by header. Both go through the shared atomic
    # 0600 writer (`config/env_file.py`), the same one `PATCH /settings` uses.
    write_env_file(
        config_path,
        produced,
        merge=(merge and config_path.exists()),
        header="# Generated by `nymeria init`.",
    )


# Value formatting and the merge/atomic-write mechanics now live in the shared
# writer (`config/env_file.py`) so finalize and `PATCH /settings` cannot drift.
# `_env_value` stays as a module-local alias because call sites and a test
# (`tests/test_setup_wizard.py::test_env_value_leaves_base64_unquoted`) use it.
_env_value = format_env_value


def _resolve_secrets_key(config_path: Path) -> str:
    """Return a stable `NYMERIA_SECRETS_KEY`: preserve an existing one, else mint.

    Never rotate an existing key (rotation orphans every secret already encrypted
    with it). Looks first in the env file we are about to write (so a `--force`
    re-run keeps the same key), then the process environment, then mints a fresh
    Fernet key via `nymeria.core.secrets.generate_key`.
    """
    existing = _read_secrets_key_from_file(config_path)
    if existing:
        return existing
    env_key = os.environ.get("NYMERIA_SECRETS_KEY")
    if env_key and env_key.strip():
        return env_key.strip()
    return nymeria_secrets.generate_key()


def _read_secrets_key_from_file(config_path: Path) -> str | None:
    if not config_path.exists():
        return None
    try:
        for raw_line in config_path.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if not stripped.startswith("NYMERIA_SECRETS_KEY="):
                continue
            value = stripped.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value or None
    except OSError:
        return None
    return None


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


def seed_bootstrap_profile(
    data_dir: Path, state: WizardState, console: Console
) -> None:
    """Seed the bootstrap admin's defaults from the init picks.

    Writes ``data/users/default/profile.json`` with an explicit
    ``default_thread_tools`` (the core seed plus the chosen ``web_search_*`` /
    ``fetch_url_*`` / ``image_gen_*`` backends) and ``enabled_global_skills`` (the
    self-improve guidance skill plus the chosen capability kits) so a new thread
    inherits the picks by default. Only when no profile exists yet, so re-running
    init never clobbers a customized profile. Best-effort: a failure here never
    aborts init (config.env and the bootstrap token are already written).
    """

    from ..core.accounts import BOOTSTRAP_USER_ID
    from ..core.user_profile import UserProfileManager
    from .tool_seed import (
        default_thread_tools_for_state,
        selected_global_skills_for_state,
    )

    try:
        manager = UserProfileManager(data_dir)
        if manager._get_profile_path(BOOTSTRAP_USER_ID).exists():
            return  # existing profile: do not overwrite the user's customizations
        profile = manager.get_profile(BOOTSTRAP_USER_ID)
        tools = default_thread_tools_for_state(state)
        profile.tool_preferences.default_thread_tools = tools
        skills = selected_global_skills_for_state(state)
        profile.enabled_global_skills = skills
        manager.save_profile(profile)
        console.print(
            f"[green]Default thread tools:[/green] {len(tools)} seeded "
            "(core set + your picks)"
        )
        console.print(
            f"[green]Default skill kits:[/green] {len(skills)} enabled "
            "(self-improve + your picks)"
        )
    except Exception as exc:  # pragma: no cover - best-effort seeding
        console.print(
            f"[yellow]Could not seed default thread tools ({exc}). "
            "Set them later in settings.[/yellow]"
        )


def update_bootstrap_profile(
    data_dir: Path,
    state: WizardState,
    console: Console,
    *,
    scoped_section: str | None = None,
) -> None:
    """Reconfigure the bootstrap admin's tool/skill picks in place.

    Unlike ``seed_bootstrap_profile`` (first-run, write-only-if-absent), this
    updates an EXISTING profile from the hydrated-then-edited state.
    ``default_thread_tools`` and ``enabled_global_skills`` are recomputed from the
    init picks; user-added tools captured during hydration
    (``state.unmanaged_tools``) are preserved by ``default_thread_tools_for_state``.
    A scoped jump that touched no pick section is a no-op. Falls back to seeding
    when no profile exists yet (e.g. the admin was just created this run).
    Best-effort: a failure here never aborts the reconfigure.
    """

    pick_sections = {"web_search", "fetch_url", "image_gen", "skill_kits"}
    if scoped_section is not None and scoped_section not in pick_sections:
        return  # this jump cannot have changed the profile

    from ..core.accounts import BOOTSTRAP_USER_ID
    from ..core.user_profile import UserProfileManager
    from .tool_seed import (
        default_thread_tools_for_state,
        selected_global_skills_for_state,
    )

    try:
        manager = UserProfileManager(data_dir)
        if not manager._get_profile_path(BOOTSTRAP_USER_ID).exists():
            seed_bootstrap_profile(data_dir, state, console)
            return
        profile = manager.get_profile(BOOTSTRAP_USER_ID)
        tools = default_thread_tools_for_state(state)
        profile.tool_preferences.default_thread_tools = tools
        skills = selected_global_skills_for_state(state)
        profile.enabled_global_skills = skills
        manager.save_profile(profile)
        console.print(f"[green]Default thread tools:[/green] {len(tools)} (updated)")
        console.print(f"[green]Default skill kits:[/green] {len(skills)} (updated)")
    except Exception as exc:  # pragma: no cover - best-effort
        console.print(
            f"[yellow]Could not update default tools/skills ({exc}). "
            "Set them in settings.[/yellow]"
        )


def _resolve_extra_env(state: WizardState) -> dict[str, str]:
    extra: dict[str, str] = {}
    if state.base_url:
        extra["LLM_BASE_URL"] = state.base_url.strip()
    if state.api_mode:
        extra["OPENAI_API_MODE"] = state.api_mode.strip()
    # Embedder/reranker choices -> EMBEDDING_* / RAG_RERANK_* env vars (the API
    # keys ride in optional_env). Empty when no embedder was chosen.
    extra.update(rag_env_for_state(state))
    return extra


# --- paths ------------------------------------------------------------------


def resolve_root(state: WizardState) -> Path:
    if state.root is not None:
        return Path(state.root).expanduser().resolve()
    return default_init_root()


def resolve_runtime_root(state: WizardState, *, for_docker: bool) -> Path:
    """Where the generated env file (and, for Docker, the compose invocation) lives.

    Honors an explicit `--root`. For Docker hosting with no override, prefers the
    source-checkout root that holds `docker-compose.single.yml` so the generated
    `.env.docker` sits next to the compose file the user runs; falls back to the
    normal init root (`~/.nymeria`) when no checkout is found (clone-free install).
    """
    if state.root is not None:
        return Path(state.root).expanduser().resolve()
    if for_docker:
        checkout = source_checkout_root()
        if checkout is not None:
            return checkout
    return default_init_root()


def source_checkout_root() -> Path | None:
    """The source-checkout dir holding `docker-compose.single.yml`, or None.

    The single-container compose file only exists in a source checkout; a
    clone-free (pip/uv) install does not ship it, so None signals the deferred
    clone-free Docker case (finalize then writes `.env.docker` to the init root
    and prints guidance to move it next to the compose file).
    """
    root = find_project_root(Path(__file__).resolve())
    if root is not None and (root / DOCKER_SINGLE_COMPOSE).exists():
        return root
    return None


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
    extra_env: Mapping[str, str] | None = None,
) -> None:
    """Show which capabilities are ready and which env var unblocks each.

    Derived from what was written to config.env this run. As the placeholder
    capability steps get real, this can move to the runtime capability
    resolvers without changing the output shape.
    """

    openai_ready = (
        spec is not None and "OPENAI_API_KEY" in spec.api_key_env_vars
    ) or bool(optional_env.get("OPENAI_API_KEY"))
    search_ready = bool(optional_env.get("PERPLEXITY_API_KEY")) or any(
        optional_env.get(env)
        for env in ("TAVILY_API_KEY", "EXA_API_KEY", "FIRECRAWL_API_KEY",
                    "BRAVE_API_KEY", "SEARXNG_BASE_URL")
    )
    image_ready = openai_ready or any(
        optional_env.get(env)
        for env in ("GEMINI_API_KEY", "BFL_API_KEY", "REPLICATE_API_KEY", "FAL_API_KEY")
    )
    rows = [
        ("Primary LLM", spec is not None, "set a provider with nymeria init"),
        (
            "Semantic memory / RAG",
            bool((extra_env or {}).get("EMBEDDING_PROVIDER"))
            or bool(optional_env.get("EMBEDDING_API_KEY")),
            "choose an embedder in nymeria init",
        ),
        (
            "Web search backends",
            search_ready,
            "add a search backend key in nymeria init",
        ),
        (
            "Image generation",
            image_ready,
            "add an image provider key in nymeria init",
        ),
        (
            "Gemini media tools",
            bool(optional_env.get("GEMINI_API_KEY")),
            "set GEMINI_API_KEY",
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
        # The container mints its own token into its /data volume on first boot;
        # there is no host token file to paste, so point at the in-container one.
        _print_docker_token_command(console)
    elif state.hosting is HostingOption.SERVICE:
        console.print(
            "\nBackground-service install is not wired up yet. For now start "
            "Nymeria in the foreground with:"
        )
        _print_command(console, start_command)
    else:
        console.print("\nStart Nymeria with:")
        _print_command(console, start_command)

    if state.next_action is not NextAction.CLI and state.hosting is not HostingOption.DOCKER:
        console.print("Then open http://localhost:8000 and paste the bootstrap token.")
    console.print("\nRe-run setup anytime with `nymeria init`. Check health with `nymeria doctor`.")


def _start_command_for_hosting(hosting: HostingOption | None) -> str:
    if hosting is HostingOption.DOCKER:
        return "docker compose -f docker-compose.single.yml up -d"
    return "nymeria slim"


# --- opt-in start -----------------------------------------------------------


def run_next_action(state: WizardState, console: Console, *, root: Path) -> int:
    """Hand off after config is written.

    When the user opted in (``NextAction.START_API_OPEN_FRONTEND``) on a local or
    Docker host, actually launch the backend; otherwise just print the start
    command (the default). Returns the process exit code, which is non-zero only
    when a foreground local start exits non-zero. A failed auto-start falls back
    to printing the manual command and returns 0 (config was written fine).
    """

    if state.next_action is NextAction.START_API_OPEN_FRONTEND:
        if state.hosting is HostingOption.DOCKER:
            return _start_now_docker(console, root=root)
        if state.hosting is HostingOption.LOCAL:
            return _start_now_local(console, root=root)
    print_next_action(state, console)
    return 0


def _start_now_docker(console: Console, *, root: Path) -> int:
    command = f"docker compose -f {DOCKER_SINGLE_COMPOSE} up -d"
    console.print("\nStarting Nymeria (single-container Docker)...")
    _print_command(console, command)
    try:
        result = subprocess.run(shlex.split(command), cwd=str(root))
    except (OSError, ValueError) as exc:
        console.print(
            f"[yellow]Could not start Docker automatically ({exc}). "
            "Run it yourself:[/yellow]"
        )
        _print_command(console, command)
        _print_docker_token_command(console)
        return 0
    if result.returncode != 0:
        console.print(
            "[yellow]The container did not start cleanly. Check the output above, "
            "or run it yourself:[/yellow]"
        )
        _print_command(console, command)
        _print_docker_token_command(console)
        return 0
    if wait_for_health(console=console):
        console.print("[green]Nymeria is up.[/green]")
        _print_docker_bootstrap_token(console, root=root)
    else:
        console.print(
            "[yellow]Started, but the health check has not passed yet. It may "
            "still be coming up; check "
            f"`docker compose -f {DOCKER_SINGLE_COMPOSE} logs -f`.[/yellow]"
        )
        _print_docker_token_command(console)
    return 0


def _start_now_local(console: Console, *, root: Path) -> int:
    console.print("\nStarting Nymeria in the foreground (Ctrl+C to stop).")
    console.print(
        "Once it is up, open http://localhost:8000 and paste the bootstrap token "
        "shown above."
    )
    # Re-invoke this same entry point with the `slim` subcommand so it works from
    # both a source checkout (`python3 run.py init`) and an installed console
    # script (`nymeria init`). Run in the runtime root so slim finds config.env.
    script = os.path.abspath(sys.argv[0])
    command = [sys.executable, script, "slim"]
    env = dict(os.environ)
    env["NYMERIA_PROJECT_ROOT"] = str(root)
    try:
        result = subprocess.run(command, cwd=str(root), env=env)
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        console.print(
            f"[yellow]Could not launch the slim backend automatically ({exc}). "
            "Start it yourself:[/yellow]"
        )
        _print_command(console, "nymeria slim")
        return 0
    return result.returncode


def wait_for_health(
    *,
    console: Console,
    url: str = "http://localhost:8000/health",
    timeout: float = 40.0,
    interval: float = 1.0,
) -> bool:
    """Poll the API health endpoint until it answers 2xx or the timeout lapses."""

    import urllib.error
    import urllib.request

    console.print("Waiting for the backend to become healthy...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:  # noqa: S310
                if 200 <= getattr(resp, "status", 200) < 300:
                    return True
        except (urllib.error.URLError, OSError):
            pass  # backend not up yet; keep polling until the deadline lapses
        time.sleep(interval)
    return False


def _docker_token_command() -> str:
    return (
        f"docker compose -f {DOCKER_SINGLE_COMPOSE} exec {DOCKER_SINGLE_SERVICE} "
        f"cat /data/{BOOTSTRAP_TOKEN_FILENAME}"
    )


def _print_docker_token_command(console: Console) -> None:
    console.print(
        "\nOnce the container is healthy, read your one-time bootstrap token "
        "(paste it into the Desktop/Mobile Setup Wizard) with:"
    )
    _print_command(console, _docker_token_command())
    console.print(
        "\nRe-run setup anytime with `nymeria init`. Check health with "
        "`nymeria doctor`."
    )


def _read_docker_bootstrap_token(*, root: Path) -> str | None:
    """Read the container's freshly minted bootstrap token from its /data volume.

    The single-container backend creates the real `nym_...` token inside its
    named volume on first boot; this execs in to fetch it. Returns None if it
    cannot be read yet (e.g. the container is not ready or `docker` is absent).
    """
    command = [
        "docker", "compose", "-f", DOCKER_SINGLE_COMPOSE, "exec", "-T",
        DOCKER_SINGLE_SERVICE, "cat", f"/data/{BOOTSTRAP_TOKEN_FILENAME}",
    ]
    try:
        result = subprocess.run(
            command, cwd=str(root), capture_output=True, text=True, timeout=15
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = re.search(BOOTSTRAP_TOKEN_REGEX, result.stdout)
    return match.group(0) if match else None


def _print_docker_bootstrap_token(console: Console, *, root: Path) -> None:
    token = _read_docker_bootstrap_token(root=root)
    if token:
        console.print(f"\n[green]Bootstrap token:[/green] {token}", soft_wrap=True)
        console.print(
            "Paste this one-time `nym_...` token into the Desktop/Mobile Setup "
            "Wizard (it is consumed on first use). It is NOT your provider API key."
        )
        console.print(
            "\nOpen http://localhost:8000 to use the web UI. Re-run setup anytime "
            "with `nymeria init`."
        )
    else:
        console.print(
            "\n[yellow]Could not read the bootstrap token from the container "
            "yet.[/yellow] Once it is healthy, run:"
        )
        _print_command(console, _docker_token_command())


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
    "seed_bootstrap_profile",
    "update_bootstrap_profile",
    "resolve_root",
    "resolve_runtime_root",
    "source_checkout_root",
    "resolve_data_dir",
    "default_init_root",
    "check_writable",
    "port_in_use",
    "print_bootstrap_token_handoff",
    "bootstrap_token_copy_command",
    "print_capability_summary",
    "print_deployment_summary",
    "print_next_action",
    "run_next_action",
    "wait_for_health",
    "run_doctor_for_root",
]
