"""Headless finalize step: write config, bootstrap the admin, hand off.

Shared by the interactive Textual wizard (after the app exits and the terminal
is restored) and the non-interactive flag path. All of the secret-handling and
file-writing logic here is lifted unchanged in behavior from the previous setup
wizard so the hard-won atomic-write / token-handoff guarantees are preserved.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from rich.console import Console
from rich.markup import escape

from .. import __version__ as _PACKAGE_VERSION
from .._runtime_paths import (
    configure_project_root,
    find_project_root,
)
from ..config.env_file import format_env_value, write_env_file
from ..config.llm_providers import LLMProviderSpec
from ..core import secrets as nymeria_secrets
from ..core.accounts import AccountsRepo, BOOTSTRAP_TOKEN_FILENAME
from ..core.service_bootstrap import SLIM_SERVICE_TOKEN_FILENAME
from ..onboarding import (
    EXTERNAL_ACCESS_CHOICES,
    HOSTING_MARKER_ENV,
    SECURITY_PROFILE_CHOICES,
    DockerStack,
    ExternalAccess,
    HostingOption,
    NextAction,
)
# `source_checkout_root` is reached through the module, not bound by name: it
# is the one symbol tests fake, and a single patch point (environment.py, where
# it lives) beats every importer needing its own.
from . import environment
from .environment import DOCKER_SINGLE_COMPOSE, clone_free_docker_blocked_reason
from .external_access import (
    CORS_ORIGINS_ENV,
    EXTERNAL_ACCESS_ENV,
    PUBLIC_URL_ENV,
    check_public_health,
    check_public_sse,
    merged_cors_origins,
    public_origin,
    qr_ascii,
)
from .providers import (
    LLMConnectionError,
    OPTIONAL_ENV_ORDER,
    check_llm_connection_for_spec,
    valid_key_format_for_spec,
)
from .rag_catalog import apply_quickstart_rag, rag_env_for_state
from .server_browser_catalog import server_browser_selected
from .tuning_catalog import tuning_drop_env, tuning_env_for_state
from .voice_catalog import (
    needs_local_voice_extra,
    uses_voice_sidecar,
    voice_drop_env,
    voice_env_for_state,
)
from ..subprocess_env import NETWORK_RUNTIME_PASSTHROUGH, scrubbed_subprocess_env
from .state import WizardState
from .tool_seed import (
    default_thread_tools_for_state,
    docker_init_seed_env,
    selected_global_skills_for_state,
)

if TYPE_CHECKING:
    from ..core.user_profile import UserProfile, UserProfileManager

logger = logging.getLogger(__name__)

BOOTSTRAP_TOKEN_REGEX = r"nym_[A-Za-z0-9_-]+"

# Single-container Docker (slim) artifacts. The compose file
# (`DOCKER_SINGLE_COMPOSE`, named beside the checkout detection in
# environment.py) reads `.env.docker` and runs `python run.py slim` as
# service `nymeria-single`, which mints the bootstrap admin + token into its
# `/data` volume on first boot.
DOCKER_SINGLE_SERVICE = "nymeria-single"

# Full Postgres + Redis stack artifacts. The multi-container compose is the
# default `docker-compose.yml` (`DOCKER_FULL_COMPOSE`), read with
# `--env-file .env.docker` (compose interpolation, not a service `env_file:`).
# The `api` service runs `run.py api` and mints the bootstrap admin + token
# into the `nymeria_data` volume.
DOCKER_FULL_SERVICE = "api"

# Clone-free single-container artifacts: the same slim shape, but pulling the
# published image instead of building from a checkout. The compose file ships
# inside the wheel (`setup/assets/`, kept byte-identical to the canonical
# `Nymeria/docker-compose.single.published.yml` by a drift test) and finalize
# materializes it into the runtime root next to `.env.docker`. The image tag is
# pinned via a `NYMERIA_VERSION` line in `.env.docker` (release images are
# tagged with the bare package version, `v` stripped).
DOCKER_SINGLE_PUBLISHED_COMPOSE = "docker-compose.single.published.yml"

# SearXNG sidecar config for the published compose's `search` profile. Ships in
# the wheel (kept byte-identical to the canonical `Nymeria/searxng/settings.yml`
# by a drift test) and materializes to `<root>/searxng/settings.yml`, where the
# compose bind-mounts it.
SEARXNG_SETTINGS_ASSET = "searxng-settings.yml"


@dataclass(frozen=True)
class _DockerStackSpec:
    """How to drive one Docker stack's compose file from finalize.

    Captures the differences between the slim single container and the full
    Postgres + Redis stack so the start/health/token helpers stay stack-agnostic.
    `compose_args` are the args between `docker compose` and the subcommand;
    `command_env` is any environment the compose invocation needs; `health_timeout`
    bounds the readiness poll. Both stacks now mint their own internal service
    token (slim in-process, full from the api into the shared `nymeria_data`
    volume), so finalize no longer does any post-boot token provisioning.
    """

    compose_args: tuple[str, ...]
    service: str
    health_url: str
    label: str
    command_env: tuple[tuple[str, str], ...] = ()
    health_timeout: float = 40.0
    # Host-side API port the compose files interpolate; _compose_env pins it
    # into the subprocess environment because run.py's import-time dotenv load
    # puts the OLD config's API_PORT into os.environ, and compose gives the
    # process env precedence over --env-file (a port-change reconfigure would
    # otherwise bring the stack up on the old port).
    api_port: int = 8000
    # Clone-free only: the published image tag (`NYMERIA_VERSION`) this install
    # should pull. _compose_env pins it for the same process-env-beats-env-file
    # reason as api_port (a reconfigure after a wheel upgrade has the OLD
    # version in os.environ). None for source-checkout stacks, which build
    # their images locally.
    image_version: str | None = None


def _searxng_sidecar_selected(state: WizardState) -> bool:
    """True when the SearXNG backend is among this run's web-search picks.

    Drives the `search` compose profile (all three compose files carry the
    sidecar behind it) and the turnkey SEARXNG_BASE_URL / SEARXNG_SECRET env
    seeding for Docker hosting.
    """
    selected = state.extras.get("web_search")
    return isinstance(selected, list) and "web_search_searxng" in selected


def _docker_stack_spec(state: WizardState) -> _DockerStackSpec:
    """The stack descriptor for this install (defaults to slim).

    Built per-state because the host-side port follows `API_PORT`: both compose
    files map `127.0.0.1:${API_PORT:-8000}:8000` (the container side stays
    8000), interpolated from `.env.docker` via `--env-file`. The full stack
    always passes `--env-file`; the slim spec adds it only for a non-default
    port so the default compose command stays the documented short form.

    Clone-free installs (no source checkout) drive the published-image compose
    that finalize materialized into the runtime root, and always pass
    `--env-file`: the `NYMERIA_VERSION` image-tag pin lives in `.env.docker`,
    and only `--env-file` feeds compose interpolation (the service-level
    `env_file:` reaches the container, not the `image:` line).

    A SearXNG pick adds `--profile search` to every compose invocation (up,
    logs, down) so the sidecar starts, stops, and reports with the stack; the
    profile also forces `--env-file` on the slim default-port path because the
    generated SEARXNG_SECRET reaches the sidecar only via interpolation.
    """
    port = state.resolved_api_port()
    # A non-default port rides the printed manual commands as an env prefix
    # (API_PORT=N docker compose ...): the copy-paste path must survive a
    # shell whose environment carries a stale API_PORT, for the same
    # process-env-beats-env-file reason _DockerStackSpec.api_port exists.
    command_env: tuple[tuple[str, str], ...] = (
        (("API_PORT", str(port)),) if port != 8000 else ()
    )
    search_profile = _searxng_sidecar_selected(state)
    profile_args: tuple[str, ...] = ("--profile", "search") if search_profile else ()
    if (state.docker_stack or DockerStack.SLIM) is DockerStack.SLIM:
        clone_free = environment.source_checkout_root() is None
        compose_file = (
            DOCKER_SINGLE_PUBLISHED_COMPOSE if clone_free else DOCKER_SINGLE_COMPOSE
        )
        compose_args: tuple[str, ...] = ("-f", compose_file)
        if port != 8000 or clone_free or search_profile:
            compose_args += ("--env-file", ".env.docker")
        compose_args += profile_args
        return _DockerStackSpec(
            compose_args=compose_args,
            service=DOCKER_SINGLE_SERVICE,
            health_url=f"http://localhost:{port}/health",
            label=(
                "published-image single-container Docker"
                if clone_free
                else "single-container Docker"
            ),
            command_env=command_env,
            api_port=port,
            image_version=_PACKAGE_VERSION if clone_free else None,
        )
    return _DockerStackSpec(
        compose_args=("--env-file", ".env.docker", *profile_args),
        command_env=command_env,
        api_port=port,
        service=DOCKER_FULL_SERVICE,
        # The full stack's /ready is a deep check (503 until Postgres + Redis
        # connect), exactly the "the stack is up" signal we want to wait on.
        health_url=f"http://localhost:{port}/ready",
        label="full Docker stack (Postgres + Redis)",
        # No DISCORD_BOT_TOKEN sentinel: docker-compose.yml fully defaults it
        # (`${DISCORD_BOT_TOKEN:-}`), so compose validates without it and the api
        # sees an empty value (cleanly "not configured") rather than the literal
        # "disabled" it would otherwise treat as a real token.
        # First boot builds the image and waits on a deep Postgres + Redis check,
        # so allow longer than the single container.
        health_timeout=120.0,
    )


def local_base_url(state: WizardState) -> str:
    """The localhost origin the chosen API port answers on."""
    return f"http://localhost:{state.resolved_api_port()}"


def _compose_argv(spec: _DockerStackSpec, *subcommand: str) -> list[str]:
    """`docker compose <compose_args> <subcommand>` as an argv list."""
    return ["docker", "compose", *spec.compose_args, *subcommand]


def _compose_env(spec: _DockerStackSpec) -> dict[str, str]:
    """Process env for a compose invocation (os.environ plus the stack's env).

    API_PORT is always pinned to the spec's port: os.environ holds whatever
    run.py's import-time dotenv load saw (the OLD config on a reconfigure, or
    a foreign checkout's .env.docker), and compose resolves `${API_PORT}` from
    the process env BEFORE --env-file.
    """
    env = dict(os.environ)
    env.update(dict(spec.command_env))
    env["API_PORT"] = str(spec.api_port)
    if spec.image_version:
        # Same hazard as API_PORT: after a wheel upgrade, the OLD version is in
        # os.environ (run.py's import-time dotenv load of the previous
        # `.env.docker`) and would beat the freshly written --env-file value,
        # pulling a stale image.
        env["NYMERIA_VERSION"] = spec.image_version
    return env


def _compose_command_str(spec: _DockerStackSpec, *subcommand: str) -> str:
    """Human-readable, copy-pasteable compose command including any env prefix."""
    prefix = "".join(f"{key}={value} " for key, value in spec.command_env)
    return f"{prefix}docker compose {' '.join([*spec.compose_args, *subcommand])}"


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

    # Clone-free (pip/uv) installs have no checkout holding the compose files.
    # While no images are published (the beta gate in environment.py) that
    # rules out Docker hosting altogether; once they are, the slim shape works
    # (finalize materializes the wheel-bundled published-image compose into
    # the runtime root) but the full stack's compose still builds its images
    # from the repo. Either way, reject up front, before any LLM test, OAuth
    # flow, or file write happens. The interactive picker and the headless
    # hosting gate already grey the shape out; this catches hydrated state.
    if state.hosting is HostingOption.DOCKER and environment.source_checkout_root() is None:
        if clone_free_docker_blocked_reason():
            console.print(
                "[red]Docker hosting needs a source checkout for now: no "
                "published Nymeria images exist yet, so a clone-free (pip/uv) "
                "install has nothing to pull. Install from source with "
                "install.sh --source, or run `python run.py init` from a "
                "clone (a packaged `nymeria init` stays clone-free wherever "
                "you launch it), or choose to run on this machine or as a "
                "background service.[/red]"
            )
            return 2
        if _is_full_stack(state):
            console.print(
                "[red]The full Postgres + Redis stack needs a source checkout: "
                "its compose file builds the images from the repo. Clone the "
                "repo and re-run setup as `python run.py init` from it (a "
                "packaged `nymeria init` stays clone-free wherever you launch "
                "it), or choose the single-container stack.[/red]"
            )
            return 2

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

    # CLIProxy subscription branch: derive the shared LLM fields (provider,
    # base_url, api_mode, model default, gatekeeper key) from the catalog pick
    # and hosting shape. The returned URL is the host-reachable one to live-test
    # against; the written base_url may be a container alias.
    cliproxy_test_base = _apply_cliproxy_route(state)
    key_env_override = cliproxy_key_env_override(state)

    spec = state.provider_spec()
    api_key = state.api_key.strip()
    base_url = state.base_url.strip()
    model = ""
    provider_auth_validated = False
    if (
        state.auth_method_is_cliproxy()
        and state.cliproxy_provider
        and state.cliproxy_logged_in
        and not api_key
        and cliproxy_key_env_override(state) not in state.present_env_keys
    ):
        # A completed subscription login with no usable gatekeeper must not
        # silently downgrade to a no-provider config; the operator would only
        # see a yellow note while chats stay broken.
        console.print(
            "[red]No CLIProxy gatekeeper key is available. Re-run the wizard's "
            "CLIProxy endpoint/login steps (or pass --cliproxy-gatekeeper-key) "
            "so the proxy's cpx- api-key can be written as the LLM key.[/red]"
        )
        return 2
    # Reconfigure: an empty key field means "keep the key already on disk", so a
    # provider whose key is present must not be downgraded to "unconfigured".
    # On the CLIProxy branch the key lives in the override var, not the
    # registry's first (direct) slot.
    provider_key_env = key_env_override or (
        spec.api_key_env_vars[0] if spec and spec.api_key_env_vars else None
    )
    key_present = bool(provider_key_env and provider_key_env in state.present_env_keys)
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
        # The CLIProxy branch carries a cpx- gatekeeper, not a provider key,
        # so the provider prefix check does not apply there.
        if api_key and not state.auth_method_is_cliproxy():
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
                    spec,
                    model,
                    api_key,
                    # CLIProxy: test through the host-reachable proxy URL; the
                    # written base_url may be a docker-network alias.
                    base_url=(cliproxy_test_base or base_url) or None,
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

    # Config target is shape-aware. The Docker single-container shape writes
    # `.env.docker` next to the compose file that reads it via `env_file`.
    # local/service hosting defers to `get_env_write_path(root)`, the same helper
    # the runtime uses to pick which dotenv to read/update: a source checkout gets
    # `.env` (gitignored), a packaged `~/.nymeria` root gets `config.env`. run.py's
    # loader reads `.env`, `config.env`, and `.env.docker`, so either lands.
    for_docker = state.hosting is HostingOption.DOCKER
    root = resolve_runtime_root(state, for_docker=for_docker)
    data_dir = resolve_data_dir(state, root=root)
    # Slim Docker without a checkout: finalize owns the compose file (the
    # wheel-bundled published-image one, materialized below). The full stack
    # was already rejected up front.
    clone_free_docker = for_docker and environment.source_checkout_root() is None
    try:
        check_writable(root)
        if not for_docker:
            check_writable(data_dir)
    except OSError as exc:
        console.print(f"[red]Cannot write setup files: {exc}[/red]")
        return 2

    api_port = state.resolved_api_port()
    if port_in_use(api_port):
        console.print(f"[yellow]Warning:[/yellow] port {api_port} is already in use.")
    port_on_disk = state.extras.get("api_port_on_disk")
    if (
        isinstance(port_on_disk, int)
        and port_on_disk != api_port
        and active_public_url(state)
        and not state.public_url_verified
    ):
        # A tunnel ingress (tailscale serve / Cloudflare) keeps forwarding to
        # the old port until the external-access setup runs again; a verified
        # URL means it was just re-exercised this run, so stay quiet then.
        console.print(
            f"[yellow]The API port is changing from {port_on_disk} to "
            f"{api_port}, but the existing remote-access ingress still "
            "forwards to the old port. Re-run the external-access setup "
            "(nymeria init external_access) or update the tunnel by "
            "hand.[/yellow]"
        )

    if for_docker:
        config_path = root / ".env.docker"
    else:
        # Lazy import: settings pulls in a heavy graph and finalize is imported
        # early, so match the existing in-function imports of config.settings.
        from ..config.settings import get_env_write_path

        config_path = get_env_write_path(root)
        if config_path.name == ".env.docker":
            # get_env_write_path picks the highest-precedence *existing* dotenv,
            # and `.env.docker` outranks the others, so a leftover Docker file
            # (e.g. from a prior Docker init in this root) would otherwise capture
            # local/service config. Never write the Docker-shape file here: prefer
            # an existing non-Docker dotenv (config.env outranks .env at load), and
            # fall back to the shape convention (checkout -> .env, packaged ->
            # config.env).
            if (root / "config.env").exists():
                config_path = root / "config.env"
            elif (root / ".env").exists():
                config_path = root / ".env"
            else:
                config_path = root / (
                    ".env" if find_project_root(root) == root else "config.env"
                )
    if merge and not config_path.exists():
        # Reconfigure whose hosting shape changed (the source file was a different
        # shape). Fall back to a fresh write of the new-shape file and note it.
        merge = False
        console.print(
            "[yellow]Hosting shape changed; writing a new config file.[/yellow]"
        )
        if not api_key and key_present:
            # The key requirement (and the LLM test) was waived because the key
            # is on disk, but it lives in the OLD shape's file and a fresh write
            # of the new file cannot carry it over.
            console.print(
                "[yellow]The existing LLM key lives in the old config file and "
                "cannot be carried into the new hosting shape. Re-run with "
                "--api-key (or copy the key line into the new file) or this "
                "install will have no LLM credentials.[/yellow]"
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

    if for_docker and _searxng_sidecar_selected(state):
        # The bundled sidecar is turnkey: default the base URL to the compose
        # service and generate a per-install secret so the compose default
        # "change-me" never reaches a real install. Both interpolate via
        # --env-file (_docker_stack_spec always passes it when the search
        # profile is active). present_env_keys guards MERGE writes only:
        # hydrate records on-disk values by presence, not value, and a
        # produced value would overwrite a hand-set URL (or rotate the
        # secret) on merge. On a fresh write (--force, or a hosting-shape
        # switch where merge fell back above) nothing carries over, so the
        # presence of a value in the OLD file must not suppress the seed.
        for var, value in (
            ("SEARXNG_BASE_URL", "http://searxng:8080"),
            ("SEARXNG_SECRET", secrets.token_urlsafe(32)),
        ):
            if not state.optional_env.get(var) and (
                not merge or var not in state.present_env_keys
            ):
                state.optional_env[var] = value

    optional_env = _resolve_optional_env(state, spec=spec, api_key=api_key)
    extra_env = _resolve_extra_env(state)
    # The server browser's home rides the config so the backend (its refusals)
    # and `nymeria doctor` know this install has one; recorded when selected or
    # when a rig already exists (a skip on reconfigure keeps the rig).
    rig_home = _server_browser_home_for_config(state, root=root)
    from ..server_browser import HOME_ENV_KEY

    if rig_home is not None:
        extra_env[HOME_ENV_KEY] = str(rig_home)
    drop_stale_server_browser = _server_browser_drop_env(rig_home)
    secrets_key = _resolve_secrets_key(config_path)
    is_full_stack = _is_full_stack(state)
    # Docker owns its `/data` volume, so the host cannot seed the bootstrap profile
    # (that is why the Docker branch below skips seed_bootstrap_profile). Carry the
    # picks into the container via `.env.docker` instead; it reads them once on
    # first boot. Empty for a no-pick install. Both Docker shapes deliver them:
    # the single-container compose injects the whole file via `env_file:`, and the
    # full stack's shared api/worker `environment:` anchor passes the two
    # `NYMERIA_INIT_*` vars through `--env-file` interpolation.
    init_seed_env = docker_init_seed_env(state) if for_docker else None
    # The full Postgres + Redis stack needs minted DB/cache passwords in
    # `.env.docker` (compose fails fast without them). Slim and non-Docker shapes
    # write nothing extra here.
    full_stack_env = _resolve_full_stack_env(config_path) if is_full_stack else None

    if not for_docker:
        data_dir.mkdir(parents=True, exist_ok=True)
    # Snapshot the keys the PRE-write env files define while they are still on
    # disk: run.py loaded those into os.environ at import, and a key the
    # rewrite REMOVES (e.g. leaving the CLIProxy branch drops LLM_BASE_URL)
    # cannot be cleared by replaying the new files, so the doctor run needs
    # this set to drop the removed ones first.
    pre_write_env_keys = _file_defined_env_keys(root)
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
        api_port=api_port,
        merge=merge,
        init_seed_env=init_seed_env,
        full_stack_env=full_stack_env,
        provider_key_env=key_env_override,
        image_version=_PACKAGE_VERSION if clone_free_docker else None,
        drop_cliproxy_management=not state.auth_method_is_cliproxy(),
        drop_public_url=should_drop_public_url(state),
        drop_stale_voice=voice_drop_env(state),
        drop_stale_server_browser=drop_stale_server_browser,
        drop_stale_tuning=tuning_drop_env(state),
    )

    console.print(f"[green]Config:[/green] {config_path}")
    if clone_free_docker:
        try:
            compose_path = _materialize_published_compose(root)
        except OSError as exc:
            console.print(f"[red]Cannot write the compose file: {exc}[/red]")
            return 2
        console.print(
            f"[green]Compose:[/green] {compose_path} (pulls the published "
            "image; rewritten by every `nymeria init` run, so keep changes "
            "in .env.docker)"
        )
        # The wizard's own `up -d` pins NYMERIA_VERSION into the subprocess env
        # (_compose_env), but the printed copy-paste commands rely on
        # `--env-file`, which a shell-exported NYMERIA_VERSION silently beats.
        _warn_shadowing_process_env(
            console, {"NYMERIA_VERSION": _PACKAGE_VERSION}
        )
    # The raw account token to surface at the end (URL + token + `nymeria cli`),
    # set only for a fresh non-Docker bootstrap. Docker mints in-container and a
    # reconfigure must not re-print an already-consumed token, so both leave this
    # None and the end-of-run printer falls back to the file-path handoff.
    connect_token: str | None = None
    # The accounts repo of a native install; Docker mints in-container, so the
    # server-browser hook takes the two-phase path there (see
    # _maybe_install_server_browser) and this stays None.
    repo: AccountsRepo | None = None
    if for_docker:
        # The container owns its data: it mints the bootstrap admin + token into
        # its `/data` volume on first boot. A host-side bootstrap would be
        # invisible to it and would print the wrong token, so skip it and read
        # the real token back from the running container (see run_next_action).
        console.print(
            "[green]Data:[/green] container-managed volume (the backend creates "
            "its admin and bootstrap token on first start)"
        )
        if is_full_stack and full_stack_env:
            shadow_keys = dict(full_stack_env)
            if secrets_key:
                shadow_keys["NYMERIA_SECRETS_KEY"] = secrets_key
            # The pick carriers ride the same `${VAR}` interpolation as the
            # DB/cache passwords, so a process-env value shadows them too.
            if init_seed_env:
                shadow_keys.update(init_seed_env)
            _warn_shadowing_process_env(console, shadow_keys)
    else:
        # Honor the configured account TTL/cap policy (backlog #107 fold-in):
        # a bare construction silently minted with constructor defaults.
        # Resolve settings against the TARGET root's env files (an `init
        # --root` must not mint with the launch root's policy) and NEVER let
        # this crash: init is deliberately validation-free (run.py registers
        # it with full_validation=False) precisely so a wizard re-run can fix
        # a broken config, so an invalid pre-existing value falls back to
        # constructor defaults instead of aborting a half-finished install.
        # Residual caveat, consistent with the alternate-root story (backlog
        # #101 entries 6/10/15a): process env, which run.py already merged
        # from the launch root's dotenv, still outranks the target root's
        # files inside Settings.
        try:
            from ..config.settings import Settings as _Settings
            from ..config.settings import get_env_file_paths as _env_paths

            _account_settings = _Settings(
                _env_file=tuple(str(p) for p in _env_paths(root))
            )
        except Exception:  # noqa: BLE001 - init must fix broken installs
            _account_settings = None
        repo = AccountsRepo.from_settings(_account_settings, data_dir / "accounts.db")
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
            connect_token = admin_token

    # Local RAG (granite + Ettin, including the Ctrl+S quickstart default) needs
    # the optional local-rag extra to load its in-process models; offer to install
    # it now so the stack works out of the box instead of failing every ingest.
    # Runs for the full flow and a scoped embedder/reranker reconfigure; other
    # scoped jumps stay focused on their one setting.
    if scoped_section in (None, "embedder", "reranker"):
        _maybe_install_local_rag(
            extra_env,
            console,
            for_docker=for_docker,
            full_stack=is_full_stack,
            non_interactive=non_interactive,
        )

    # The server browser (a headless Chrome the agent drives out of the box):
    # install, connect to this install as the admin, and supervise. Runs for
    # the full flow and its own scoped jump.
    if scoped_section in (None, "server_browser"):
        _finalize_server_browser_guarded(
            state,
            console,
            root=root,
            data_dir=data_dir,
            repo=repo,
            for_docker=for_docker,
        )

    if scoped_section is not None:
        # A focused `init <section>` jump: report the one change and stop. No token
        # handoff, post-setup launch, or doctor for a single-setting edit.
        console.print(f"\n[green]Updated[/green] the {scoped_section} settings.")
        if scoped_section == "hosting":
            if state.hosting is HostingOption.SERVICE:
                # Scoped runs never launch anything, so hand over the one
                # command that makes the new hosting choice real.
                console.print(
                    "Install the background service with "
                    f"`{_service_install_command(state)}`."
                )
            else:
                _warn_stale_service_artifact(state, console)
        return 0

    print_capability_summary(
        spec,
        optional_env,
        console,
        extra_env=extra_env,
        keyless_search_selected=(
            "web_search_ddgs" in (state.extras.get("web_search") or [])
        ),
        for_docker=for_docker,
        full_stack=is_full_stack,
    )
    print_deployment_summary(state, console)

    doctor_status = _maybe_run_doctor(
        state,
        root=root,
        console=console,
        provider_auth_validated=provider_auth_validated,
        stale_env_keys=pre_write_env_keys,
    )
    if doctor_status != 0:
        return doctor_status

    return run_next_action(
        state,
        console,
        root=root,
        connect_token=connect_token,
        non_interactive=non_interactive,
    )


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
    api_port: int = 8000,
    merge: bool = False,
    init_seed_env: Mapping[str, str] | None = None,
    full_stack_env: Mapping[str, str] | None = None,
    provider_key_env: str | None = None,
    image_version: str | None = None,
    drop_cliproxy_management: bool = False,
    drop_public_url: bool = False,
    drop_stale_voice: tuple[str, ...] = (),
    drop_stale_tuning: tuple[str, ...] = (),
    drop_stale_server_browser: tuple[str, ...] = (),
) -> None:
    """Atomically write the env file with 0600 perms (it holds API keys).

    When `spec` is None (the provider step was skipped), the LLM lines are
    omitted so the backend still starts and a provider can be set later. The key
    is written to the provider's highest-priority env var; for Anthropic that is
    `ANTHROPIC_DIRECT_API_KEY` (the direct, non-proxy key), not `ANTHROPIC_API_KEY`.
    `provider_key_env` overrides that target: the CLIProxy branch passes the
    catalog's slot so the cpx- gatekeeper lands in `ANTHROPIC_API_KEY` /
    `OPENAI_API_KEY` instead of the direct slot.

    `for_docker` selects the Docker flavor (a `.env.docker`, for either the slim
    single container or the full Postgres + Redis stack): the storage/host lines
    (`DATABASE_BACKEND`, `NYMERIA_DATA_DIR`, `API_HOST`) are omitted because the
    compose file sets `NYMERIA_DATA_DIR=/data` and (slim) forces sqlite + redis-off
    inside the container; writing a host data dir here would only mislead.
    `secrets_key`, when set, is the credential-vault Fernet key
    (`NYMERIA_SECRETS_KEY`) and is written for every shape.

    `merge` (reconfigure) overlays only the keys this run produces onto the
    existing file, preserving untouched lines and comments. An omitted key (e.g.
    the provider key when the field was left blank to keep the existing one) is
    left as-is on disk rather than blanked.

    `init_seed_env` (both Docker shapes) carries the bootstrap admin's tool/skill
    picks into the container, which reads them once on first boot
    (`setup/tool_seed.docker_init_seed_env`, contract in `config/init_seed_env.py`).
    The values are `:`-joined identifier lists, so they write unquoted. The slim
    compose delivers them via `env_file:`; the full stack via the `NYMERIA_INIT_*`
    passthroughs in its shared api/worker `environment:` anchor. On a merge,
    a carrier the state no longer produces is REMOVED, not preserved: hydrate
    reads existing carriers back into state, so absence means the user reverted
    the picks to defaults, and keeping the stale line would re-seed the old
    picks on a future fresh volume.

    `full_stack_env` (full Docker stack only) carries the Postgres/Redis settings
    the multi-container compose interpolates (`POSTGRES_PASSWORD`/`REDIS_PASSWORD`
    are required-or-error there), minted once and preserved across re-runs.

    `image_version` (clone-free Docker only) writes the `NYMERIA_VERSION` image
    tag the published compose interpolates, pinning the pulled image to the
    installed wheel's version.
    """

    config_path.parent.mkdir(parents=True, exist_ok=True)
    optional_env = optional_env or {}
    extra_env = extra_env or {}
    init_seed_env = init_seed_env or {}
    full_stack_env = full_stack_env or {}
    provider_env = provider_key_env or (
        spec.api_key_env_vars[0] if (spec and spec.api_key_env_vars) else None
    )
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
    for env_var, value in full_stack_env.items():
        # Postgres/Redis settings the full-stack compose interpolates. The DB/cache
        # passwords (already in format_env_value's safe set) write unquoted.
        if value:
            produced.append((env_var, _env_value(value)))
    # Written for every shape: slim/local read it at startup, and both Docker
    # compose files interpolate it into the host-side port binding.
    produced.append(("API_PORT", str(api_port)))
    if image_version:
        # Clone-free Docker: pin the published image tag to this wheel's
        # version so `up -d` pulls the matching image (the compose default is
        # `latest`). Re-produced on every run, so a wheel upgrade plus
        # reconfigure moves the pin forward.
        produced.append(("NYMERIA_VERSION", image_version))
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
        # safe set, so `_env_value` leaves them unquoted for both delivery paths
        # (slim `env_file:`, full-stack `--env-file` interpolation).
        if value:
            produced.append((env_var, _env_value(value)))
    from .local_rag_install import DOCKER_LOCAL_RAG_ENV, requires_local_rag

    if for_docker and full_stack_env and requires_local_rag(extra_env):
        # Full stack + local embedder/reranker: sentence-transformers has to be
        # baked into nymeria-full, and this flag is the compose build arg that
        # does it (docker-compose.yml `x-nymeria-full-image` -> Dockerfile.full).
        # There is no pip env to install into after the fact in a container.
        produced.append((DOCKER_LOCAL_RAG_ENV, "1"))

    # For the carriers, absent-from-produced means "picks equal defaults", not
    # "unchanged": a Docker reconfigure must be able to retire a stale carrier,
    # or a later fresh volume re-seeds picks the user reverted. Safe because
    # hydrate restores existing carriers into state (so an untouched reconfigure
    # re-produces them) and a produced key always wins over the drop list.
    drop_env: tuple[str, ...] = ()
    if for_docker:
        from ..config.init_seed_env import (
            INIT_DEFAULT_THREAD_TOOLS_ENV,
            INIT_ENABLED_GLOBAL_SKILLS_ENV,
        )

        # The local-RAG image flag follows the same rule: a reconfigure onto a
        # hosted embedder retires it so the next rebuild goes back to the lean
        # image; when the local pick is kept, the produced line wins.
        drop_env = (
            INIT_DEFAULT_THREAD_TOOLS_ENV,
            INIT_ENABLED_GLOBAL_SKILLS_ENV,
            DOCKER_LOCAL_RAG_ENV,
        )
        if not image_version:
            # A clone-free root later reconfigured from a source checkout
            # retires the stale image-tag pin (the checkout composes never
            # interpolate it); when a pin IS produced this run it wins anyway.
            drop_env = drop_env + ("NYMERIA_VERSION",)
    if drop_cliproxy_management:
        # Leaving the subscription branch retires the management endpoint
        # lines; keeping them would leave the backend's /cliproxy routes wired
        # to an abandoned proxy. Never dropped ON the branch (a blank key
        # there means keep the on-disk secret). LLM_BASE_URL/OPENAI_API_MODE
        # ride along: when the run clears them on a branch exit (the hydrated
        # values described the proxy route), the stale lines would otherwise
        # survive the merge and keep routing chats through the proxy. Inert
        # for every other non-CLIProxy reconfigure: a hydrated or flagged
        # value is in `produced`, which always wins over the drop.
        drop_env = drop_env + (
            "CLIPROXY_MANAGEMENT_URL",
            "CLIPROXY_MANAGEMENT_KEY",
            "LLM_BASE_URL",
            "OPENAI_API_MODE",
        )
    if drop_public_url:
        # Switching to local-only retires the stale public URL; keeping it
        # would advertise (and hydrate back) an origin the user abandoned.
        # CORS_ORIGINS is deliberately NOT dropped: the operator may maintain
        # it by hand, and a stale extra origin is hygiene, not breakage.
        drop_env = drop_env + (PUBLIC_URL_ENV,)
    # Voice provider switches retire the old provider's model/voice/URL/key
    # lines (computed in voice_catalog.voice_drop_env); anything this run
    # re-collects is in `produced` and wins over the drop.
    drop_env = drop_env + drop_stale_voice
    # Agent-tuning lines: cleared hydrated fields and abandoned context
    # strategies' trigger lines (tuning_catalog.tuning_drop_env); same
    # produced-wins-over-drop semantics.
    drop_env = drop_env + drop_stale_tuning
    # SERVER_BROWSER_HOME when this install has neither a rig nor a pick;
    # a produced key (the normal case) still wins over the drop.
    drop_env = drop_env + drop_stale_server_browser

    # Reconfigure overlays produced keys onto the existing file; first-run writes
    # a fresh file with the generated-by header. Both go through the shared atomic
    # 0600 writer (`config/env_file.py`), the same one `PATCH /settings` uses.
    write_env_file(
        config_path,
        produced,
        merge=(merge and config_path.exists()),
        header="# Generated by `nymeria init`.",
        drop=drop_env,
    )


# Value formatting and the merge/atomic-write mechanics now live in the shared
# writer (`config/env_file.py`) so finalize and `PATCH /settings` cannot drift.
# `_env_value` stays as a module-local alias for brevity at the internal call
# sites below; the canonical implementation is `config/env_file.format_env_value`.
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
    return _read_env_value_from_file(config_path, "NYMERIA_SECRETS_KEY")


def _read_env_value_from_file(config_path: Path, key: str) -> str | None:
    """Return the (unquoted) value of `key` in an existing env file, or None."""
    if not config_path.exists():
        return None
    try:
        for raw_line in config_path.read_text(encoding="utf-8").splitlines():
            stripped = raw_line.strip()
            if not stripped.startswith(f"{key}="):
                continue
            value = stripped.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value or None
    except OSError:
        return None
    return None


def _resolve_full_stack_env(config_path: Path) -> dict[str, str]:
    """Postgres/Redis settings for the full stack's `.env.docker`.

    Mints `POSTGRES_PASSWORD` / `REDIS_PASSWORD` (preserving any already on disk so
    a reconfigure or `--force` re-run keeps the same credentials, which MUST match
    the already-initialized postgres volume), and sets the user/db names the
    compose interpolates. The compose supplies sane defaults for CORS and the
    user/db names, but writing them keeps the generated file self-describing. An
    operator-set `NYMERIA_SERVICE_TOKEN` already on disk is carried forward so a
    fresh (non-merge) re-write does not drop it; when absent the api self-mints
    the internal token onto the shared volume on first boot, so finalize writes
    no service-token line of its own.
    """
    env = {
        "POSTGRES_USER": "nymeria",
        "POSTGRES_DB": "nymeria",
        "POSTGRES_PASSWORD": (
            _read_env_value_from_file(config_path, "POSTGRES_PASSWORD")
            or secrets.token_urlsafe(24)
        ),
        "REDIS_PASSWORD": (
            _read_env_value_from_file(config_path, "REDIS_PASSWORD")
            or secrets.token_urlsafe(24)
        ),
    }
    existing_token = _read_env_value_from_file(config_path, "NYMERIA_SERVICE_TOKEN")
    if existing_token:
        env["NYMERIA_SERVICE_TOKEN"] = existing_token
    return env


def _resolve_optional_env(
    state: WizardState,
    *,
    spec: LLMProviderSpec | None,
    api_key: str,
) -> dict[str, str]:
    optional_env = {k: v.strip() for k, v in state.optional_env.items() if v}
    if spec is not None:
        # The primary provider key already writes its own env var(s); never
        # duplicate or shadow them from the optional pool. Generalized from
        # the OPENAI_API_KEY-only pop when the antigravity CLIProxy route
        # made GEMINI_API_KEY a primary slot too (2026-08-07): without this,
        # a typed Gemini media key silently shadowed or was shadowed by the
        # proxy gatekeeper, and the capability summary reported Gemini media
        # tools ready off a proxy-local key that cannot serve them.
        for env_name in spec.api_key_env_vars:
            optional_env.pop(env_name, None)
    return optional_env


def _apply_profile_picks(
    manager: "UserProfileManager", profile: "UserProfile", state: WizardState
) -> tuple[int, int]:
    """Recompute and persist the bootstrap admin's tool/skill picks.

    Shared apply-and-save core of ``seed_bootstrap_profile`` (first run) and
    ``update_bootstrap_profile`` (reconfigure): recompute ``default_thread_tools``
    and ``enabled_global_skills`` from the wizard state, save the profile, and
    return ``(tool_count, skill_count)`` for the caller's console summary. Any
    exception propagates to the caller's best-effort guard.
    """

    tools = default_thread_tools_for_state(state)
    profile.tool_preferences.default_thread_tools = tools
    skills = selected_global_skills_for_state(state)
    profile.enabled_global_skills = skills
    manager.save_profile(profile)
    return len(tools), len(skills)


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

    try:
        manager = UserProfileManager(data_dir)
        if manager._get_profile_path(BOOTSTRAP_USER_ID).exists():
            return  # existing profile: do not overwrite the user's customizations
        profile = manager.get_profile(BOOTSTRAP_USER_ID)
        n_tools, n_skills = _apply_profile_picks(manager, profile, state)
        console.print(
            f"[green]Default thread tools:[/green] {n_tools} seeded "
            "(core set + your picks)"
        )
        console.print(
            f"[green]Default skill kits:[/green] {n_skills} enabled "
            "(self-improve + your picks)"
        )
    except Exception as exc:  # pragma: no cover - best-effort seeding
        logger.warning("Failed to seed bootstrap profile picks", exc_info=True)
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

    try:
        manager = UserProfileManager(data_dir)
        if not manager._get_profile_path(BOOTSTRAP_USER_ID).exists():
            seed_bootstrap_profile(data_dir, state, console)
            return
        profile = manager.get_profile(BOOTSTRAP_USER_ID)
        n_tools, n_skills = _apply_profile_picks(manager, profile, state)
        console.print(f"[green]Default thread tools:[/green] {n_tools} (updated)")
        console.print(f"[green]Default skill kits:[/green] {n_skills} (updated)")
    except Exception as exc:  # pragma: no cover - best-effort
        logger.warning("Failed to update bootstrap profile picks", exc_info=True)
        console.print(
            f"[yellow]Could not update default tools/skills ({exc}). "
            "Set them in settings.[/yellow]"
        )


def _cliproxy_backend_host(state: WizardState) -> str:
    """The proxy host root as the BACKEND will reach it, by hosting shape.

    The wizard talks to the proxy on a host-reachable URL
    (`state.cliproxy_management_url`); the backend may live elsewhere: inside
    the full Docker stack it reaches the proxy by network alias, inside the
    slim container through the host gateway, and on a local/service install
    the host URL works as-is.
    """
    if not state.auth_method_is_cliproxy():
        return ""
    if state.hosting is HostingOption.DOCKER:
        if state.docker_stack is DockerStack.FULL:
            return "http://cli-proxy-api:8317"
        return "http://host.docker.internal:8318"
    return (state.cliproxy_management_url or "").strip().rstrip("/") or (
        "http://localhost:8318"
    )


def _apply_cliproxy_route(state: WizardState) -> str | None:
    """Fill the shared LLM fields from the CLIProxy catalog pick.

    Returns the host-reachable base URL to LIVE-TEST against (the written
    `state.base_url` may be a container alias the wizard host cannot reach),
    or None when the branch is inactive/incomplete. Deterministic from the
    pick + hosting shape, so an untouched reconfigure rewrites identical
    lines.
    """
    if not state.auth_method_is_cliproxy() or not state.cliproxy_provider:
        return None
    from ..cliproxy.catalog import cliproxy_data_plane_url, get_cliproxy_provider

    cspec = get_cliproxy_provider(state.cliproxy_provider)
    if cspec is None:
        return None
    state.provider = cspec.nymeria_provider
    state.base_url = cliproxy_data_plane_url(_cliproxy_backend_host(state), cspec)
    state.api_mode = cspec.api_mode
    if not state.model:
        state.model = cspec.default_model
    if state.cliproxy_gatekeeper_key.strip():
        state.api_key = state.cliproxy_gatekeeper_key.strip()
    host_url = (state.cliproxy_management_url or "").strip()
    if not host_url:
        return None
    return cliproxy_data_plane_url(host_url, cspec)


def cliproxy_key_env_override(state: WizardState) -> str | None:
    """The env var the gatekeeper key writes to on the CLIProxy branch.

    The catalog routes the cpx- gatekeeper to ANTHROPIC_API_KEY /
    OPENAI_API_KEY; the registry default for Anthropic would be the DIRECT
    (pay-per-token) slot, which must stay reserved for real Anthropic keys
    (see core/agent_llm_config.py).
    """
    if not state.auth_method_is_cliproxy() or not state.cliproxy_provider:
        return None
    from ..cliproxy.catalog import get_cliproxy_provider

    cspec = get_cliproxy_provider(state.cliproxy_provider)
    return cspec.key_env_var if cspec else None


def _resolve_extra_env(state: WizardState) -> dict[str, str]:
    extra: dict[str, str] = {}
    if state.base_url:
        extra["LLM_BASE_URL"] = state.base_url.strip()
    if state.api_mode:
        extra["OPENAI_API_MODE"] = state.api_mode.strip()
    # CLIProxy branch: persist the management endpoint so the backend (and its
    # /cliproxy admin routes) can drive the proxy at runtime. The URL is the
    # backend-facing one computed by _apply_cliproxy_route; a blank key on a
    # reconfigure stays omitted, so the merge keeps the existing secret line.
    if state.auth_method_is_cliproxy():
        backend_url = _cliproxy_backend_host(state)
        if backend_url:
            extra["CLIPROXY_MANAGEMENT_URL"] = backend_url
        if state.cliproxy_management_key.strip():
            extra["CLIPROXY_MANAGEMENT_KEY"] = state.cliproxy_management_key.strip()
    # Embedder/reranker choices -> EMBEDDING_* / RAG_RERANK_* env vars (the API
    # keys ride in optional_env). Empty when no embedder was chosen.
    extra.update(rag_env_for_state(state))
    # Voice picks -> TTS_PROVIDER / STT_PROVIDER (+ Docker sidecar base URLs);
    # the keys ride in optional_env via the backend-keys step. Empty when the
    # voice steps were never reached.
    extra.update(voice_env_for_state(state))
    # Agent tuning (context strategy, limits, reasoning effort + sampling).
    # Blank fields write nothing, so settings defaults stay in charge.
    extra.update(tuning_env_for_state(state))
    # External access: persist the choice (the wizard's round-trip marker; the
    # runtime does not read it) and, when this run stands behind a public
    # origin (a setup step, --public-url, or hydrate), the real settings: the
    # public URL plus a CORS list extended with that origin (seeded from the
    # on-disk list on reconfigure). A LOCAL_ONLY run yields no active URL, so
    # nothing here writes; the matching retirement drop lives in finalize().
    if state.external_access is not None:
        extra[EXTERNAL_ACCESS_ENV] = state.external_access.value
    # Hosting: same wizard-only marker pattern. Without it LOCAL vs SERVICE
    # is only recoverable from the installed unit, so switching away from
    # SERVICE would silently flip back on the next reconfigure (the artifact
    # outlives the choice; teardown is the user's call). A hosting-less run
    # (no flag, fresh root) is the local shape, so default the marker too;
    # hosting=None can never be a Docker run (for_docker derives from it).
    extra[HOSTING_MARKER_ENV] = (state.hosting or HostingOption.LOCAL).value
    active_url = active_public_url(state)
    if active_url:
        origin = public_origin(active_url)
        extra[PUBLIC_URL_ENV] = origin
        extra[CORS_ORIGINS_ENV] = merged_cors_origins(
            state.existing_cors_origins, origin
        )
    return extra


def active_public_url(state: WizardState) -> str:
    """The public origin this run stands behind; empty for a local-only choice.

    `public_url` can hold a stale hydrated value after the user switches the
    choice to local-only on a reconfigure, so every consumer (env writes,
    summaries, post-start verification) goes through this gate instead of
    reading the field directly. CHAT_BOTS keeps the URL: webhook-based bot
    platforms need NYMERIA_PUBLIC_URL.
    """
    if state.external_access is ExternalAccess.LOCAL_ONLY:
        return ""
    return state.public_url.strip()


def should_drop_public_url(state: WizardState) -> bool:
    """Retire the NYMERIA_PUBLIC_URL line only when the wizard owns it.

    `external_access_recorded` means the on-disk URL came with the wizard's
    round-trip marker, so abandoning the choice retires it. A hand-set URL on
    a pre-marker install (the remote-access doc tells operators to set one
    for Caddy or an existing tunnel) must survive an unrelated reconfigure
    that Enters through the local-only default.
    """
    return state.external_access_recorded and not active_public_url(state)


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
        checkout = environment.source_checkout_root()
        if checkout is not None:
            return checkout
    return default_init_root()


def _materialize_published_compose(root: Path) -> Path:
    """Write the wheel-bundled published compose bundle into the runtime root.

    Clone-free installs have no checkout to run compose from, so init owns the
    compose file next to `.env.docker`: it is rewritten on every run, keeping
    it in lockstep with the installed wheel, while `.env.docker` carries all
    user-editable state (including the `NYMERIA_VERSION` image-tag pin). The
    SearXNG sidecar config (`searxng/settings.yml`, which the compose
    bind-mounts when the `search` profile is up) ships and rewrites the same
    way; operators who need a customized instance should run their own and
    point `SEARXNG_BASE_URL` at it instead of editing the materialized copy.
    """
    setup_assets = resources.files("nymeria.setup").joinpath("assets")
    root.mkdir(parents=True, exist_ok=True)

    target = root / DOCKER_SINGLE_PUBLISHED_COMPOSE
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(setup_assets.joinpath(DOCKER_SINGLE_PUBLISHED_COMPOSE).read_bytes())
    os.replace(tmp, target)

    searxng_target = root / "searxng" / "settings.yml"
    searxng_target.parent.mkdir(parents=True, exist_ok=True)
    tmp = searxng_target.with_name(searxng_target.name + ".tmp")
    tmp.write_bytes(setup_assets.joinpath(SEARXNG_SETTINGS_ASSET).read_bytes())
    os.replace(tmp, searxng_target)

    return target


def resolve_data_dir(state: WizardState, *, root: Path) -> Path:
    if state.data_dir is not None:
        return Path(state.data_dir).expanduser().resolve()
    return root / "data"


def default_init_root() -> Path:
    """Where `nymeria init` writes config + data.

    MUST equal the project root the runtime resolves (see
    `config.settings._get_project_root` / `_runtime_paths.configure_project_root`)
    so the config the wizard writes is the config the backend later loads. Both
    resolvers honor `NYMERIA_PROJECT_ROOT` first, so honoring it here too keeps
    init and runtime in lockstep: editable/source installs resolve it to the
    checkout, wheel/frozen installs to `~/.nymeria`. Rejecting a checkout root
    here (as a previous version did) split the two apart: init wrote to
    `~/.nymeria` while the backend read the checkout, so the wizard's provider,
    key, and data-dir choices were silently ignored at runtime.
    """
    env_root = os.environ.get("NYMERIA_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    # No explicit override (e.g. init invoked through a path that did not run the
    # cli_entry bootstrap): fall back to the same resolver the runtime uses rather
    # than blindly `~/.nymeria`, so a source checkout is still detected.
    return configure_project_root()


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
    keyless_search_selected: bool = False,
    for_docker: bool = False,
    full_stack: bool = False,
) -> None:
    """Show which capabilities are ready and which env var unblocks each.

    Derived from what was written to config.env this run, with one live check:
    a local embedder or reranker on a bare-metal shape is ready only when the
    local-rag extra can actually import (a declined, deferred, or failed
    install must not read as ok). On Docker the extra lives in the image: the
    full stack bakes it in via the build flag finalize wrote, the
    single-container image omits it, so ``for_docker``/``full_stack`` decide.
    As the placeholder capability steps get real, this can move to the runtime
    capability resolvers without changing the output shape.
    ``keyless_search_selected`` covers web_search_ddgs, which is ready with no
    env var at all.
    """
    from .local_rag_install import (
        build_install_command,
        local_rag_importable,
        manual_install_hint,
        requires_local_rag,
    )

    rag_env = extra_env or {}
    rag_configured = bool(rag_env.get("EMBEDDING_PROVIDER")) or bool(
        optional_env.get("EMBEDDING_API_KEY")
    )
    rag_ready = rag_configured
    rag_hint = "choose an embedder in nymeria init"
    if rag_configured and requires_local_rag(rag_env):
        if for_docker:
            rag_ready = full_stack
            if not full_stack:
                rag_hint = (
                    "the single-container image omits the local-rag extra: "
                    "build one with it, or pick a hosted embedder in nymeria init"
                )
        elif not local_rag_importable():
            rag_ready = False
            # escape() keeps Rich from eating the [local-rag] in the command.
            rag_hint = (
                "install the local-rag extra: "
                f"{escape(manual_install_hint(build_install_command()))}"
            )

    openai_ready = (
        spec is not None and "OPENAI_API_KEY" in spec.api_key_env_vars
    ) or bool(optional_env.get("OPENAI_API_KEY"))
    search_ready = (
        keyless_search_selected
        or bool(optional_env.get("PERPLEXITY_API_KEY"))
        or any(
            optional_env.get(env)
            for env in ("TAVILY_API_KEY", "EXA_API_KEY", "FIRECRAWL_API_KEY",
                        "BRAVE_API_KEY", "SEARXNG_BASE_URL")
        )
    )
    image_ready = openai_ready or any(
        optional_env.get(env)
        for env in ("GEMINI_API_KEY", "BFL_API_KEY", "REPLICATE_API_KEY", "FAL_API_KEY")
    )
    rows = [
        ("Primary LLM", spec is not None, "set a provider with nymeria init"),
        ("Semantic memory / RAG", rag_ready, rag_hint),
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
    """Echo the security-profile choice and the external-access outcome.

    Security profile is recorded but not persisted, so it is surfaced rather
    than silently dropped. Only Unleashed (current behavior) is selectable;
    the approval-gated profiles are still being built. External access
    reports what the wizard actually did: the configured public origin and its
    verification state, or the matching manual guidance. The Docker-stack
    choice is acted on (it selects the compose file), so it is not echoed here.
    """

    if state.security_profile is not None:
        console.print(
            "\n[bold]Security profile[/bold] "
            f"{SECURITY_PROFILE_CHOICES[state.security_profile].label} "
            "(recorded; approval-gated profiles are still being built)"
        )
    print_external_access_summary(state, console)
    _warn_stale_service_artifact(state, console)
    _print_voice_hints(state, console)


def _print_voice_hints(state: WizardState, console: Console) -> None:
    """Surface what a voice pick still needs to actually run.

    Bare-metal local engines need the optional pip extra (checked live so a
    ready install prints nothing); the Docker full stack needs the matching
    compose profile up (speaches for kokoro/faster-whisper, voice-gpu for
    qwen3); the slim single container supports neither shape of local voice,
    so that combo gets an explicit warning instead of silently dead config.
    Hosted picks need nothing beyond their key.
    """
    from .voice_catalog import (
        LOCAL_STT_PROVIDERS,
        LOCAL_TTS_PROVIDERS,
        selected_stt,
        selected_tts,
        slim_docker_local_voice,
        uses_qwen3_sidecar,
    )

    if needs_local_voice_extra(state):
        from nymeria.core.voice_local import local_stt_importable, local_tts_importable

        missing = []
        if selected_tts(state) in LOCAL_TTS_PROVIDERS and not local_tts_importable():
            missing.append("kokoro-onnx")
        if selected_stt(state) in LOCAL_STT_PROVIDERS and not local_stt_importable():
            missing.append("faster-whisper")
        if missing:
            from .local_rag_install import extra_install_hint

            # The shared helper reinstalls a uv tool with the receipt's extras
            # kept (so a local-rag install is not dropped) and quotes for the
            # platform; escape() stops Rich from eating [voice-local].
            install_cmd = escape(extra_install_hint("voice-local"))
            console.print(
                "\n[yellow]Local voice needs the voice extra "
                f"({' and '.join(missing)} not installed). Run, with Nymeria "
                f"not running: {install_cmd}. Models download on first use.[/yellow]"
            )
    if uses_voice_sidecar(state):
        console.print(
            "\n[bold]Voice sidecar[/bold] The local voice picks use the "
            "speaches container: start the stack with the voice profile, "
            "e.g. `docker compose --env-file .env.docker --profile voice up -d`, "
            "then pull its models once (see the compose file's speaches comments)."
        )
    if uses_qwen3_sidecar(state):
        console.print(
            "\n[bold]Voice sidecar[/bold] Qwen3-TTS runs in the GPU profile: "
            "pin QWEN3_TTS_IMAGE to a digest, then start it with "
            "`docker compose --env-file .env.docker --profile voice-gpu up -d`."
        )
    if slim_docker_local_voice(state):
        console.print(
            "\n[yellow]The single-container Docker image includes neither the "
            "in-process voice engines nor the speaches sidecar, so the local "
            "voice pick will not work as written. Run a speaches container "
            "yourself and set TTS_BASE_URL / STT_BASE_URL to it, or pick a "
            "hosted provider.[/yellow]"
        )


def _server_browser_drop_env(rig_home: Path | None) -> tuple[str, ...]:
    """The env keys this run must RETIRE for the server browser.

    Exactly the inverse of writing the key: no pick and no rig on disk means the
    line must go, or the refusals keep naming a browser the operator removed
    (`nymeria browser service uninstall` plus an rm, then a reconfigure with
    `--no-server-browser`, leaves `has_server_browser()` answering True off a
    stale line forever). A produced key always wins over the drop, so this is
    inert on every run that does have a rig.
    """
    from ..server_browser import HOME_ENV_KEY

    return () if rig_home is not None else (HOME_ENV_KEY,)


def _rig_home_for_root(root: Path):
    """This ROOT's rig home, ignoring the launch process's environment.

    Every setup-side rig lookup (the key write, the native and Docker
    provisioning phases, hydration) goes through here. A bare
    `resolve_rig_home(root)` reads `os.environ` first, which is right for the
    CLI and wrong for setup: `run.py` loads the LAUNCH root's dotenv at import,
    so on a two-install host `nymeria init --root /other` would resolve THIS
    install's rig, write that path into the other install's config, and re-bake
    the running rig with the other install's URL and token. Destructive, not
    merely stale. The precedence chain itself (env files, the `configure
    --home` pointer, the default) stays the launcher's: re-spelling it here is
    how a source got missed once already.
    """
    from .. import server_browser as sb

    return sb.resolve_rig_home(root, process_env=False)


def _server_browser_home_for_config(state: WizardState, *, root: Path) -> Path | None:
    """The rig home to record as SERVER_BROWSER_HOME, or None to retire the key.

    Recorded when the step selected the server browser, or when a rig already
    exists at this root (a skip on reconfigure keeps the rig, so the key must
    stay true).

    The key means "this install is MEANT to have a server browser", not "one is
    installed and healthy": it is written from the pick, before provisioning
    runs, and deliberately survives a provisioning failure, because the failure
    path prints the commands that finish the job and the refusals should point
    at `nymeria browser status` rather than at a Chrome extension popup the
    operator was never going to use. What it must never do is outlive the rig:
    None retires the key (see `drop_stale_server_browser` in `write_config`), so
    a skip after the rig was deleted stops the refusals naming a browser that is
    gone.
    """
    home = _rig_home_for_root(root)
    if server_browser_selected(state) or home.rig_json.exists():
        return home.path
    return None


def _finalize_server_browser_guarded(
    state: WizardState,
    console: Console,
    *,
    root: Path,
    data_dir: Path,
    repo: "AccountsRepo | None",
    for_docker: bool,
) -> None:
    """Run the server-browser hook so that nothing it does can end setup.

    Containment, not belt-and-braces. `provision` promises never to raise and is
    written to keep that promise, but it drives a 200 MB download, an archive
    extract, a service install and four chmods, so the ways it can surprise us
    are open-ended: a full disk mid-extract, a read-only mount, a non-ASCII
    Windows path in the scheduled-task shim. By the time this runs, config.env
    is written and the bootstrap admin is minted, and what comes AFTER it is the
    capability summary, the doctor run, the bootstrap-token handoff and the
    start-now action. Losing those over an optional browser is the one outcome
    worth a bare `except`. Loud in the log, loud on screen, and the install
    finishes.
    """
    try:
        _maybe_install_server_browser(
            state,
            console,
            root=root,
            data_dir=data_dir,
            repo=repo,
            for_docker=for_docker,
        )
    except Exception as exc:  # noqa: BLE001 - setup finishes regardless
        logger.warning("Server browser provisioning failed", exc_info=True)
        console.print(
            f"\n[yellow]The server browser could not be set up "
            f"({escape(str(exc))}). Everything else finished. Retry with: "
            f"nymeria browser install --root {escape(str(root))}[/yellow]"
        )


def _maybe_install_server_browser(
    state: WizardState,
    console: Console,
    *,
    root: Path,
    data_dir: Path,
    repo: "AccountsRepo | None",
    for_docker: bool,
) -> None:
    """Give the install a browser the agent can drive: Chrome for Testing plus
    the Nymeria extension, connected to this install as the admin, supervised.

    Native shapes do it all here: the accounts DB is local, so a token labelled
    `server-browser` is minted in-process (any earlier token of that label is
    revoked first, so re-runs do not pile them up), the rig is provisioned
    (`server_browser.provision`, which never raises), and the admin profile
    gets the rig as its labelled account-default browser. Docker mints in the
    container, which exists only once the stack runs, so this phase only
    downloads Chrome; `_finish_docker_server_browser` completes it after the
    start-now health check, or the manual steps are printed.

    Unlike the local-rag hook there is no confirm prompt: the wizard's own
    step (or `--no-server-browser`) already asked, and a `--quick` run
    applies the default deliberately (the download is the out-of-the-box
    path, not a compiled extra). Failures never fail setup.
    """
    from .. import server_browser as sb

    rig = _rig_home_for_root(root)
    existing = sb.RigConfig.load(rig)
    if not server_browser_selected(state):
        if existing is not None:
            console.print(
                f"\n[yellow]Server browser kept as-is at {escape(str(rig.path))} "
                "(skipped this run). Remove it with "
                f"`nymeria browser service uninstall --root {escape(str(root))}`.[/yellow]"
            )
        return

    console.print("\n[bold]Server browser[/bold]")
    log = _server_browser_logger(console)
    if for_docker:
        try:
            sb.install(rig, log=log)
        except sb.ServerBrowserError as exc:
            console.print(
                f"[yellow]Could not install Chrome for Testing: {escape(str(exc))}[/yellow]"
            )
            for hint in exc.hints:
                console.print(f"  - {escape(hint)}")
            return
        if state.next_action is NextAction.START_API_OPEN_FRONTEND:
            console.print(
                "Chrome is ready; the extension is connected once the stack is up (below)."
            )
        else:
            _print_docker_server_browser_steps(console, state, root=root)
        return

    if repo is None:
        console.print(
            "[yellow]No accounts database to mint the browser's token from; "
            f"finish with: nymeria browser configure --root {escape(str(root))} "
            "--token-file <file>[/yellow]"
        )
        return
    token = _mint_server_browser_token(repo, console)
    if token is None:
        return
    client_id = existing.client_id if existing is not None else sb.new_client_id()
    report = sb.provision(
        root,
        base_url=local_base_url(state),
        token=token,
        client_id=client_id,
        label=sb.DEFAULT_LABEL,
        home=rig,
        log=log,
    )
    _print_provision_report(console, report)
    if report.ok and report.config is not None:
        _write_server_browser_prefs(
            data_dir,
            client_id=report.config.client_id,
            label=report.config.label,
            console=console,
        )


def _server_browser_logger(console: Console):
    def log(line: str) -> None:
        style = "yellow" if line.startswith("WARNING") else None
        text = escape(line)
        console.print(f"[{style}]{text}[/{style}]" if style else text)

    return log


def _mint_server_browser_token(repo: "AccountsRepo", console: Console) -> str | None:
    """A fresh `server-browser` token for the admin; earlier ones of that label revoked."""
    from ..core.accounts import BOOTSTRAP_USER_ID
    from ..server_browser import TOKEN_LABEL

    try:
        for record in repo.list_tokens_for_user(BOOTSTRAP_USER_ID):
            if record.label == TOKEN_LABEL and getattr(record, "revoked_at", None) is None:
                repo.revoke_token(BOOTSTRAP_USER_ID, record.hash_prefix)
        return repo.issue_token(BOOTSTRAP_USER_ID, label=TOKEN_LABEL)
    except Exception as exc:  # noqa: BLE001 - setup must finish; the fix is printed
        console.print(
            f"[yellow]Could not mint a token for the server browser ({escape(str(exc))}). "
            "Mint one later (Desktop > Account > Tokens, or `nymeria users issue-token "
            "default --label server-browser`) and run `nymeria browser configure "
            "--token-file <file>`.[/yellow]"
        )
        return None


def _write_server_browser_prefs(
    data_dir: Path, *, client_id: str, label: str, console: Console
) -> None:
    """Name the rig and make it the admin's account-default browser.

    Direct profile write (the API is not up yet during finalize): the same
    `browser` preference block `/browser default` and `/browser rename` edit.
    A thread's own `chrome_target` still overrides; the user's later rename
    wins because this runs only at provisioning. Best-effort.
    """
    from ..core.accounts import BOOTSTRAP_USER_ID
    from ..core.user_profile import UserProfileManager

    try:
        manager = UserProfileManager(data_dir)
        with manager.atomic_update(BOOTSTRAP_USER_ID) as profile:
            prefs = profile.get_browser_preferences()
            labels = dict(prefs.get("labels") or {})
            labels[client_id] = label
            profile.set_browser_preference("labels", labels)
            profile.set_browser_preference("default_target", client_id)
        console.print(
            f"[green]Browser default:[/green] {escape(label)} ({escape(client_id)}) "
            "for your account; a thread can switch with chrome_target."
        )
    except Exception as exc:  # noqa: BLE001 - best-effort, like the profile seeding
        logger.warning("Failed to write server-browser preferences", exc_info=True)
        console.print(
            f"[yellow]Could not record the server browser as your default ({escape(str(exc))}); "
            f"set it later with `/browser default {escape(client_id)}`.[/yellow]"
        )


def _print_provision_report(console: Console, report) -> None:
    for line in report.lines:
        console.print(f"[green]{escape(line)}[/green]")
    for warning in report.warnings:
        console.print(f"[yellow]{escape(warning)}[/yellow]")
    if report.manual_commands:
        console.print("Finish the server browser by hand with:")
        for command in report.manual_commands:
            _print_command(console, command)


def _print_docker_server_browser_steps(
    console: Console, state: WizardState, *, root: Path
) -> None:
    """The three commands that connect the server browser to a Docker stack.

    Printed by phase 1 when the wizard is not starting the stack, and by every
    early return from `_start_now_docker`: phase 1 withholds them on the promise
    that phase 2 runs, so a start that fails must hand them over instead of
    leaving a downloaded Chrome, a written SERVER_BROWSER_HOME and no rig.
    Silent for an install that declined the browser.
    """
    from ..server_browser import TOKEN_LABEL

    if not server_browser_selected(state):
        return
    spec = _docker_stack_spec(state)
    console.print(
        "Once the stack is up, connect the server browser (mint a token in the "
        "container, save it to a file, then configure and supervise the browser):"
    )
    _print_command(
        console,
        _compose_command_str(
            spec, "exec", "-T", spec.service, "python", "run.py", "users",
            "issue-token", "default", "--label", TOKEN_LABEL, "--replace",
        ),
    )
    _print_command(
        console,
        f"nymeria browser configure --root {root} --base-url {local_base_url(state)} "
        "--token-file <file>",
    )
    _print_command(console, f"nymeria browser service install --root {root}")


def _finish_docker_server_browser(
    state: WizardState, console: Console, *, root: Path, spec: _DockerStackSpec
) -> None:
    """Docker phase 2: mint in-container, provision the rig, set the account default.

    Called once the stack answered its health check. The account default and
    label go through the API (`/browser rename`, `/browser default`) because
    the profile lives in the container's volume; `/browser default` resolves
    only browsers the backend knows, so this waits (bounded) for the rig's
    extension to subscribe first. Every failure prints the manual steps.
    """
    from .. import server_browser as sb

    if not server_browser_selected(state):
        return
    rig = _rig_home_for_root(root)
    try:
        if rig.chrome_binary() is None:
            return  # phase 1 failed and already said so
    except sb.ServerBrowserError:
        return
    console.print("\n[bold]Server browser[/bold]")
    token = _mint_docker_server_browser_token(spec=spec, root=root)
    if token is None:
        console.print(
            "[yellow]Could not mint the browser's token in the container.[/yellow]"
        )
        _print_docker_server_browser_steps(console, state, root=root)
        return
    existing = sb.RigConfig.load(rig)
    client_id = existing.client_id if existing is not None else sb.new_client_id()
    report = sb.provision(
        root,
        base_url=local_base_url(state),
        token=token,
        client_id=client_id,
        label=sb.DEFAULT_LABEL,
        home=rig,
        log=_server_browser_logger(console),
    )
    _print_provision_report(console, report)
    if report.ok and report.config is not None:
        _apply_server_browser_default_via_api(
            base_url=local_base_url(state),
            token=token,
            client_id=report.config.client_id,
            label=report.config.label,
            home=rig,
            console=console,
        )


def _mint_docker_server_browser_token(*, spec: _DockerStackSpec, root: Path) -> str | None:
    """Issue a `server-browser` token for the admin inside the running container."""
    from ..server_browser import TOKEN_LABEL

    # `--replace` revokes this label's live tokens before minting. Without it
    # every re-run leaves another admin-scoped token behind, baked into a
    # config.json that has since been overwritten so nothing will ever revoke
    # it, until account_max_active_tokens_per_user trips and minting simply
    # starts failing. The native path has always revoked; this is the same rule.
    command = _compose_argv(
        spec, "exec", "-T", spec.service, "python", "run.py", "users",
        "issue-token", "default", "--label", TOKEN_LABEL, "--replace",
    )
    try:
        # env-gate: full-copy - same `_compose_env` as the token read above:
        # compose resolves the project from `${...}` in the process env on
        # the no-`--env-file` path, and this execs into an already-running
        # container rather than starting a new image.
        result = subprocess.run(
            command,
            cwd=str(root),
            env=_compose_env(spec),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = re.search(BOOTSTRAP_TOKEN_REGEX, result.stdout)
    return match.group(0) if match else None


def _apply_server_browser_default_via_api(
    *,
    base_url: str,
    token: str,
    client_id: str,
    label: str,
    home,
    console: Console,
    wait_seconds: float = 30.0,
) -> None:
    from .. import server_browser as sb

    deadline = time.monotonic() + wait_seconds
    connected = False
    while time.monotonic() < deadline:
        try:
            if sb.status(home).connected_per_backend:
                connected = True
                break
        except Exception:  # noqa: BLE001 - polling
            pass
        time.sleep(2.0)
    if not connected:
        console.print(
            "[yellow]The server browser has not connected yet, so it is not the "
            f"account default; once it shows in `/browser list`, run "
            f"`/browser default {escape(client_id)}`.[/yellow]"
        )
        return
    # Only the default is set here. The NAME arrives on its own: the extension
    # announces its baked label when it subscribes, and the backend seeds it for
    # a browser that has none. `/browser rename` could not carry it anyway, its
    # `label` is a single plain positional with no rest capture, so the two-word
    # default label parsed as an extra argument and the command failed with
    # "Unexpected argument". It failed at level="error" inside an HTTP 200, so
    # nothing raised and success was printed regardless. Do not re-add it
    # without making the command take a multi-word label.
    try:
        sb.http_json(
            f"{base_url}/commands/execute",
            token=token,
            method="POST",
            body={"command": f"/browser default {client_id}", "surface": "cli"},
            timeout=10.0,
        )
        console.print(
            f"[green]Browser default:[/green] {escape(label)} ({escape(client_id)}) for your account."
        )
    except Exception as exc:  # noqa: BLE001 - best-effort
        console.print(
            f"[yellow]Could not set the server browser as the account default ({escape(str(exc))}); "
            f"run `/browser default {escape(client_id)}` from any Nymeria surface.[/yellow]"
        )


def _maybe_install_local_rag(
    extra_env: Mapping[str, str],
    console: Console,
    *,
    for_docker: bool,
    full_stack: bool = False,
    non_interactive: bool,
) -> None:
    """Make a selected local RAG stack runnable out of the box.

    ``EMBEDDING_PROVIDER=local`` / ``RAG_RERANK_PROVIDER=local`` (including the
    Ctrl+S quickstart default, which equips granite + Ettin) need the optional
    local-rag extra (sentence-transformers + torch); without it the embedder
    fails on every ingest. On the bare-metal shapes we offer to install it now
    (confirm, default yes) so the user never has to learn the command. Docker
    carries its dependencies in the image, not a uv/pip env, so it gets a hint
    instead. An already-installed extra or a non-local stack is a silent no-op.

    Unlike the voice-local extra (``_print_voice_hints`` only prints a command),
    this offers to install: the local stack is also the silent Ctrl+S quickstart
    default the user never explicitly chose, so a bare hint would strand the most
    common path. Voice is always an explicit, visible pick, so a hint suffices.

    One shape gets the hint regardless: a uv tool install on Windows, where the
    install would rebuild the environment this very process runs from and the
    OS keeps its files locked (``local_rag_install.in_process_install_blocked``
    has the mechanism). Setup completes and the user runs the printed command
    afterwards, with Nymeria not running.
    """
    from .local_rag_install import (
        DOCKER_LOCAL_RAG_ENV,
        build_install_command,
        in_process_install_blocked,
        local_rag_importable,
        manual_install_hint,
        requires_local_rag,
    )

    if not requires_local_rag(extra_env) or local_rag_importable():
        return

    if for_docker:
        if full_stack:
            # write_config already wrote the build flag; say what it does and
            # the one case `up -d` does not cover (an image built before the
            # flag was set is reused as-is).
            console.print(
                "\n[yellow]The local RAG stack (granite + Ettin) runs in-process "
                "via the sentence-transformers extra, which the lean default "
                f"image omits. {DOCKER_LOCAL_RAG_ENV}=1 was written to "
                ".env.docker so the compose build bakes it into nymeria-full "
                "(CPU-only torch, roughly 1.5 GB more image). A first `up -d` "
                "builds it in; a stack whose image already exists needs "
                "`docker compose --env-file .env.docker up -d --build api worker` "
                "once.[/yellow]"
            )
            return
        console.print(
            "\n[yellow]The local RAG stack (granite + Ettin) needs the "
            "sentence-transformers extra baked into the image; the default "
            "Docker images omit it to stay small. Build an image that includes "
            "the local-rag extra, or pick a hosted embedder/reranker.[/yellow]"
        )
        return

    command = build_install_command()
    # escape() stops Rich from eating the [local-rag] in the command as markup.
    hint = escape(manual_install_hint(command))
    blocked = in_process_install_blocked()
    if blocked:
        console.print(
            "\n[yellow]The local RAG stack (granite + Ettin) needs the local-rag "
            "extra: sentence-transformers plus PyTorch (a few hundred MB; the "
            f"models download on first use). {escape(blocked)} After setup "
            f"finishes, with Nymeria not running, run: {hint}[/yellow]"
        )
        return
    if command is None:
        console.print(
            "\n[yellow]The local RAG stack needs the local-rag extra "
            f"(sentence-transformers). Install it with: {hint}[/yellow]"
        )
        return
    # A prompt needs an interactive terminal; --non-interactive or a non-tty
    # stdin (a pipe, a test harness) cannot answer, so surface the command
    # instead of blocking on input that will never come.
    if non_interactive or not sys.stdin.isatty():
        console.print(
            "\n[yellow]The local RAG stack needs the local-rag extra "
            f"(sentence-transformers). Install it with: {hint}[/yellow]"
        )
        return

    console.print(
        "\n[bold]Local semantic memory (granite + Ettin)[/bold] needs the "
        "local-rag extra: sentence-transformers plus PyTorch (a few hundred MB; "
        "the models download on first use)."
    )
    # Default yes: a bare Enter (or anything not starting with "n") proceeds; any
    # "n..." answer declines. EOF (Ctrl+D) or Ctrl+C at the prompt is treated as a
    # decline, not a crash, so a finished init never ends on a traceback.
    try:
        answer = console.input(r"Install it now? \[Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer.startswith("n"):
        console.print(f"[yellow]Skipped. Install later with: {hint}[/yellow]")
        return

    console.print("\nInstalling the local-rag extra (this can take a few minutes)...")
    try:
        # The installer runs arbitrary build and post-install code from a
        # package index, which makes this the one wizard spawn where remote
        # content meets local secrets. run.py loads the whole deployment .env
        # at import (master key, DB and Redis credentials, provider keys), so
        # a bare inherit would put all of it inside a third party's setup.py.
        # The network passthrough keeps proxies, custom CA bundles and the XDG
        # cache dirs working, which is everything uv/pip legitimately needs.
        result = subprocess.run(
            command, env=scrubbed_subprocess_env(NETWORK_RUNTIME_PASSTHROUGH)
        )
    except OSError as exc:
        console.print(
            f"[yellow]Could not run the installer ({exc}). Install it yourself, "
            f"then restart: {hint}[/yellow]"
        )
        return
    if result.returncode != 0:
        console.print(
            "\n[yellow]The local-rag install did not finish cleanly. Run it "
            f"yourself, then restart: {hint}[/yellow]"
        )
        return
    console.print("[green]Local RAG dependencies installed.[/green]")


def _warn_stale_service_artifact(state: WizardState, console: Console) -> None:
    """Warn when a previously installed background service was switched away.

    The wizard never tears the service down on a hosting switch (that is the
    user's call), but staying silent would leave the old unit running and,
    for a Docker switch, fighting the new stack for the API port.
    """
    if state.hosting in (HostingOption.SERVICE, None):
        return
    from nymeria.service_install import installed_artifact_path

    if installed_artifact_path() is None:
        return
    console.print(
        "\n[yellow]A background service from a previous setup is still "
        "installed and may be running (it binds the API port). Remove it with "
        "`nymeria service uninstall`.[/yellow]"
    )


def print_external_access_summary(state: WizardState, console: Console) -> None:
    """Report the external-access outcome: URL + verification, or guidance."""

    access = state.external_access
    if access is None or access is ExternalAccess.LOCAL_ONLY:
        if state.public_url.strip():
            # An explicit --public-url, or a hand-set/hydrated URL, gated out
            # by the local-only choice: say so instead of silently dropping.
            console.print(
                "\n[yellow]Note:[/yellow] a public URL is known "
                f"({public_origin(state.public_url)}) but the external-access "
                "choice is local-only, so this run did not write it."
            )
        return
    label = EXTERNAL_ACCESS_CHOICES[access].label
    console.print(f"\n[bold]External access[/bold] ({label})")
    if access is ExternalAccess.CHAT_BOTS:
        console.print(
            "  Chat-app bots need no inbound networking: set a bot token and "
            "start the bot. See the remote-access doc for per-platform steps."
        )
        return
    active_url = active_public_url(state)
    if active_url:
        origin = public_origin(active_url)
        status = (
            "verified: health and streaming work through it"
            if state.public_url_verified
            else (
                "configured, not fully verified yet; after the backend "
                f"starts, check {origin}/health/stream (the Docker "
                "start-now path re-checks automatically)"
            )
        )
        console.print(f"  [green]Public URL:[/green] {origin} ({status})")
        console.print(
            "  Written to NYMERIA_PUBLIC_URL, and the origin was added to "
            "CORS_ORIGINS."
        )
        if access is ExternalAccess.CLOUDFLARE:
            if state.cloudflare_tunnel_token:
                console.print(
                    "  The cloudflared connector is running detached; see "
                    "cloudflared/README.txt to install it as a system service."
                )
            console.print(
                "  Tip: behind a colocated tunnel every request reaches the "
                "API from one local address, so per-client rate limiting "
                "collapses; set NYMERIA_FORWARDED_ALLOW_IPS=127.0.0.1 to "
                "key it on real client IPs (see the configuration doc)."
            )
        qr = qr_ascii(origin)
        if qr:
            console.print("  Scan to open Nymeria on another device:")
            console.print(qr, soft_wrap=True)
    else:
        console.print(
            "  Not set up in this run. See the remote-access doc, or re-run "
            "`nymeria init external_access`."
        )


def verify_public_url_now(state: WizardState, console: Console) -> None:
    """Check health and streaming through the public URL (backend must be up).

    Runs after a successful start (the Docker start-now path); the local
    start-now path blocks in the foreground, so it prints the URL to check
    instead. Updates ``state.public_url_verified`` on success.
    """

    active_url = active_public_url(state)
    if not active_url:
        return
    import asyncio

    origin = public_origin(active_url)
    console.print(f"Checking the public URL ({origin})...")
    try:
        probe = asyncio.run(check_public_health(origin))
        if probe.status != "healthy":
            console.print(
                f"[yellow]The public URL did not answer healthy:[/yellow] "
                f"{probe.detail}"
            )
            return
        sse = asyncio.run(check_public_sse(origin))
    except RuntimeError as exc:
        # No usable event loop context (embedding callers); skip, not fail.
        console.print(f"[yellow]Skipped the public URL check ({exc}).[/yellow]")
        return
    if sse.ok:
        state.public_url_verified = True
        console.print(
            "[green]Public URL verified:[/green] health and streaming both "
            "work through it."
        )
    else:
        console.print(
            "[yellow]The public URL answers, but streaming looks broken "
            f"through it:[/yellow] {sse.detail}. Chat will not stream until "
            "this is fixed."
        )


# --- next action and doctor -------------------------------------------------


def print_next_action(
    state: WizardState, console: Console, *, connect_token: str | None = None
) -> None:
    if state.next_action is NextAction.CLI:
        console.print("\nEnter CLI chat with:")
        _print_command(console, "nymeria cli")
        console.print(
            "\nRe-run setup anytime with `nymeria init`. Check health with "
            "`nymeria doctor`."
        )
        return
    if state.hosting is HostingOption.DOCKER:
        # The container mints its own token on first boot; the docker printer points
        # at the in-container token and follow-up commands (stack-aware).
        _print_docker_next_steps(console, state)
        spec = _docker_stack_spec(state)
        _print_chat_smoke_recipe(
            console,
            token_command=_compose_command_str(
                spec, "exec", "-T", spec.service, "cat",
                f"/data/{SLIM_SERVICE_TOKEN_FILENAME}",
            ),
            base_url=local_base_url(state),
        )
        return

    if state.hosting is HostingOption.SERVICE:
        console.print(
            "\nInstall and start the background service (starts on login, "
            "keeps running) with:"
        )
        _print_command(console, _service_install_command(state))
        console.print("Check it anytime with `nymeria service status`.")
        console.print("Remove it with `nymeria service uninstall`.")
    else:
        console.print("\nStart Nymeria with:")
        _print_command(console, _start_command_for_hosting(state))
    if connect_token:
        console.print(
            f"Then open {local_base_url(state)} and paste the bootstrap token "
            "from the handoff printed above."
        )
    else:
        console.print(f"Then open {local_base_url(state)} and sign in.")
    if active_public_url(state):
        console.print(
            f"Remote devices use {public_origin(active_public_url(state))} "
            "once the backend is up."
        )
    console.print("\nStart the terminal chat client:")
    _print_command(console, "nymeria cli")
    # Connection details. The URL is non-secret and always prints; the token
    # value prints only when the user opted in (default on in the interactive
    # wizard, off in --non-interactive so it never lands in captured stdout) and
    # only for a fresh bootstrap (connect_token is set). Otherwise the file-path
    # handoff above is the way to retrieve it.
    if state.print_credentials and connect_token:
        bootstrap_path = (
            resolve_data_dir(state, root=resolve_runtime_root(state, for_docker=False))
            / BOOTSTRAP_TOKEN_FILENAME
        )
        console.print("\nConnect any client (desktop, mobile, or another machine) to:")
        console.print(f"  URL:   {local_base_url(state)}")
        console.print(f"  Token: {connect_token}")
        console.print(f"         (also saved to {bootstrap_path})")
    token_path = (
        resolve_data_dir(state, root=resolve_runtime_root(state, for_docker=False))
        / SLIM_SERVICE_TOKEN_FILENAME
    )
    _print_chat_smoke_recipe(
        console,
        token_command=f"cat {shlex.quote(str(token_path))}",
        base_url=local_base_url(state),
    )
    console.print(
        "\nRe-run setup anytime with `nymeria init`. Check health with "
        "`nymeria doctor`."
    )


def _start_command_for_hosting(state: WizardState) -> str:
    # Only reached for LOCAL/None hosting: DOCKER and SERVICE return earlier
    # in print_next_action with their own command blocks.
    if state.hosting is HostingOption.DOCKER:
        return _compose_command_str(_docker_stack_spec(state), "up", "-d")
    return "nymeria slim"


def _print_docker_next_steps(console: Console, state: WizardState) -> None:
    """Print the start command and token handoff for the chosen Docker stack.

    Both stacks are now a single `up -d` plus the in-container bootstrap-token
    read: the slim container mints its internal service token in-process, and the
    full stack's api mints it onto the shared `nymeria_data` volume where the
    worker / mcp containers read it, so there is no host-side
    service-token step to run.
    """
    spec = _docker_stack_spec(state)
    console.print(f"\nStart Nymeria ({spec.label}):")
    if DOCKER_SINGLE_PUBLISHED_COMPOSE in spec.compose_args:
        # Clone-free: the compose file and .env.docker live in the runtime
        # root rather than a checkout the user is standing in, so the relative
        # compose commands below only work from there.
        root = resolve_runtime_root(state, for_docker=True)
        console.print(f"Run these from {root}:")
    _print_command(console, _compose_command_str(spec, "up", "-d"))
    _print_docker_token_command(console, spec)
    if state.auth_method_is_cliproxy() and _is_full_stack(state) and not state.cliproxy_deploy:
        # A wizard-generated deployment already joins the stack's edge network;
        # a pre-existing proxy must be attached by hand or the api/worker
        # containers cannot resolve the cli-proxy-api alias.
        console.print(
            "\nFull stack + an existing CLIProxy: make sure the proxy container "
            "is attached to the stack's edge network with the `cli-proxy-api` "
            "alias, e.g.:"
        )
        _print_command(
            console,
            "docker network connect --alias cli-proxy-api nymeria_edge "
            "<your-cliproxy-container>",
        )


# --- opt-in start -----------------------------------------------------------


def browser_token_handoff_allowed(
    state: WizardState, *, connect_token: str | None, non_interactive: bool
) -> bool:
    """Whether the post-start browser auto-open (#token fragment) may run.

    All conditions required: a token minted THIS run (a reconfigure or a
    Docker install leaves ``connect_token`` None), an interactive terminal a
    human is actually watching, credential surfacing not opted out, a native
    hosting shape on this machine, and a browser that would land in front of
    this user (``browser_launch_blocked_reason``: SSH would open it on the
    wrong machine, containers and display-less hosts have nowhere to open
    one). The opened URL carries the raw token in its fragment, so it is
    never printed or logged; the printed handoff above stays the fallback.
    """
    if not connect_token:
        return False
    if non_interactive or not sys.stdin.isatty():
        return False
    if not state.print_credentials:
        return False
    if state.hosting not in (HostingOption.LOCAL, HostingOption.SERVICE):
        return False
    from .environment import browser_launch_blocked_reason

    return browser_launch_blocked_reason() == ""


def _open_browser_with_token(base_url: str, token: str) -> bool:
    """Open the served web UI pre-authenticated via a ``#token=`` fragment.

    The fragment never reaches the server, its access logs, or a Referer
    header; the frontend scrubs it from the address bar before using it. The
    URL contains the raw token: never print or log it.
    """
    import webbrowser

    try:
        return webbrowser.open(f"{base_url}/#token={token}")
    except Exception as exc:  # noqa: BLE001 (best effort; printed handoff remains)
        logger.debug("webbrowser.open failed: %s", exc)
        return False


def run_next_action(
    state: WizardState,
    console: Console,
    *,
    root: Path,
    connect_token: str | None = None,
    non_interactive: bool = False,
) -> int:
    """Hand off after config is written.

    When the user opted in (``NextAction.START_API_OPEN_FRONTEND``), actually
    launch the backend: detached for Docker, foreground for local, and a
    service install + start + health verify for the background-service host.
    Otherwise just print the start command (the default). Returns the process
    exit code, which is non-zero only when a foreground local start exits
    non-zero. A failed auto-start falls back to printing the manual command
    and returns 0 (config was written fine).

    ``handoff_token`` below is the pre-gated browser auto-open credential: on
    a fresh interactive native install, the started backend's web UI is
    opened already signed in (the URL fragment carries the token), so the
    local happy path never requires learning what a token is.
    """

    if state.next_action is NextAction.START_API_OPEN_FRONTEND:
        handoff_token = (
            connect_token
            if browser_token_handoff_allowed(
                state, connect_token=connect_token, non_interactive=non_interactive
            )
            else None
        )
        if state.hosting is HostingOption.DOCKER:
            return _start_now_docker(console, state=state, root=root)
        if state.hosting is HostingOption.LOCAL:
            return _start_now_local(
                console,
                state=state,
                root=root,
                handoff_token=handoff_token,
                token_printed=connect_token is not None,
            )
        if state.hosting is HostingOption.SERVICE:
            return _start_now_service(
                console, state=state, root=root, handoff_token=handoff_token
            )
    print_next_action(state, console, connect_token=connect_token)
    return 0


def _start_now_docker(console: Console, *, state: WizardState, root: Path) -> int:
    spec = _docker_stack_spec(state)
    # One `up -d` brings up the whole stack: the full stack's `depends_on` health
    # gates order Postgres + Redis before the api, the api self-mints the internal
    # service token onto the shared volume during its own startup, and the worker /
    # mcp read that token from disk once the api is healthy. No host-side
    # service-token provisioning is needed for either stack.
    up_command = _compose_command_str(spec, "up", "-d")
    console.print(f"\nStarting Nymeria ({spec.label})...")
    _print_command(console, up_command)
    try:
        # env-gate: full-copy - compose interpolation reads the process
        # environment, and on the slim default-port source-checkout path
        # `--env-file` is deliberately NOT passed (see _docker_stack_spec), so
        # the process env is the ONLY source for `${...}` in the compose file.
        # Scrubbing here would silently substitute empty strings into the
        # containers this is provisioning. The values ARE the stack's
        # configuration and compose is the thing that installs them, so this is
        # the same category as the API re-exec rather than a leak: what makes
        # it acceptable is that the child is bringing up Nymeria itself.
        result = subprocess.run(
            _compose_argv(spec, "up", "-d"),
            cwd=str(root),
            env=_compose_env(spec),
        )
    except (OSError, ValueError) as exc:
        console.print(
            f"[yellow]Could not start Docker automatically ({exc}). "
            "Run it yourself:[/yellow]"
        )
        _print_docker_next_steps(console, state)
        _print_docker_server_browser_steps(console, state, root=root)
        return 0
    if result.returncode != 0:
        console.print(
            "[yellow]The stack did not start cleanly. Check the output above, "
            "or run it yourself:[/yellow]"
        )
        if spec.image_version:
            console.print(
                "[yellow]If the pull failed (e.g. 'manifest unknown'), the "
                f"published image tag {spec.image_version} may not exist yet; "
                "set NYMERIA_VERSION in .env.docker to an available tag (e.g. "
                "latest) and re-run the start command.[/yellow]"
            )
        _print_docker_next_steps(console, state)
        _print_docker_server_browser_steps(console, state, root=root)
        return 0
    if not wait_for_health(
        console=console, url=spec.health_url, timeout=spec.health_timeout
    ):
        console.print(
            "[yellow]Started, but the health check has not passed yet. It may "
            "still be coming up; check "
            f"`{_compose_command_str(spec, 'logs', '-f')}`.[/yellow]"
        )
        if DOCKER_SINGLE_PUBLISHED_COMPOSE in spec.compose_args:
            # Clone-free users never cd'ed anywhere: the wizard ran compose for
            # them, so say where the relative commands work from.
            console.print(f"Run compose commands from {root}.")
        _print_docker_token_command(console, spec)
        _print_docker_server_browser_steps(console, state, root=root)
        return 0
    console.print("[green]Nymeria is up.[/green]")
    verify_public_url_now(state, console)
    smoke_token = None
    if not state.skip_llm_test:
        # The in-container service token, not the bootstrap token: the first
        # bootstrap-token auth deletes its file, breaking the handoff below.
        smoke_token = _read_docker_token_file(
            spec=spec, root=root, filename=SLIM_SERVICE_TOKEN_FILENAME
        )
    _run_inline_chat_smoke(state, console, token=smoke_token)
    _print_docker_bootstrap_token(
        console, spec=spec, root=root, base_url=local_base_url(state)
    )
    # Phase 2 of the server browser for Docker: the stack is up, so a token
    # can be minted in-container and the rig connected (see the hook).
    _finish_docker_server_browser(state, console, root=root, spec=spec)
    return 0


def _start_now_local(
    console: Console,
    *,
    state: WizardState,
    root: Path,
    handoff_token: str | None = None,
    token_printed: bool = False,
) -> int:
    """Start the slim backend in this terminal.

    ``handoff_token`` is the pre-gated browser auto-open credential;
    ``token_printed`` says whether this run printed a bootstrap token at all
    (a reconfigure of an existing admin prints none), so the copy never tells
    the user to paste something they were not shown.
    """
    from ..service_install import resolve_exec_argv

    console.print("\nStarting Nymeria in the foreground (Ctrl+C to stop).")
    if handoff_token:
        console.print(
            "Once it is up, your browser opens already signed in (the token "
            f"printed above and {local_base_url(state)} are the fallback)."
        )
    elif token_printed:
        console.print(
            f"Once it is up, open {local_base_url(state)} and paste the bootstrap "
            "token from the handoff printed above."
        )
    else:
        console.print(f"Once it is up, open {local_base_url(state)} and sign in.")
    if active_public_url(state):
        console.print(
            f"Remote devices use {public_origin(active_public_url(state))} "
            "once the backend is up."
        )
    # Re-run this entry point with the `slim` subcommand. resolve_exec_argv is
    # the one place that knows every install shape: a source checkout
    # (`python3 run.py init`), a `-m` launch, a frozen build, and an installed
    # console script, including uv's Windows trampoline, which runs
    # nymeria.exe as a zipapp and leaves a sys.argv[0] of
    # `...\.local\bin\nymeria` (no such file); the answer there is to re-run
    # the archive, with `shutil.which("nymeria")` as the fallback. The old
    # `[sys.executable, sys.argv[0], "slim"]` died there with "can't open file"
    # on the first public-beta Windows test. Run in the runtime root so slim
    # finds config.env.
    command = resolve_exec_argv()
    env = dict(os.environ)
    env["NYMERIA_PROJECT_ROOT"] = str(root)
    # The server owns this terminal from here, so the smoke turn runs from a
    # daemon thread that prints one [smoke] line into the server's output, and
    # the pre-gated browser auto-open waits for health from its own thread.
    stop = threading.Event()
    smoke_thread = _spawn_local_smoke_thread(state, root=root, stop=stop)
    browser_thread = (
        _spawn_local_browser_open_thread(state, token=handoff_token, stop=stop)
        if handoff_token
        else None
    )
    try:
        # env-gate: full-copy - same shape as the API self-restart, one layer
        # out: the wizard launching `<entry point> slim`, which is Nymeria
        # itself. It adds NYMERIA_PROJECT_ROOT and otherwise hands over the
        # environment the operator just finished configuring, because the child
        # is the backend they asked to start.
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
    finally:
        stop.set()
        for worker in (smoke_thread, browser_thread):
            if worker is not None:
                try:
                    worker.join(timeout=2.0)
                except KeyboardInterrupt:
                    pass  # second Ctrl+C during the bounded join: just leave
    return result.returncode


def _service_install_command(state: WizardState) -> str:
    """The exact manual install command for THIS config's root.

    A bare `nymeria service install` resolves its own root (env var or
    source-checkout discovery), which can differ from where the wizard just
    wrote config.env; the explicit --root removes the ambiguity.
    """
    return f"nymeria service install --root {shlex.quote(str(state.root))}"


def _start_now_service(
    console: Console,
    *,
    state: WizardState,
    root: Path,
    handoff_token: str | None = None,
) -> int:
    """Install the background service, start it, and verify the backend is up.

    Failures never fail setup (config was written fine): unavailable or broken
    service managers fall back to printing the manual commands and return 0.
    Dynamic content (errors, hints, command stderr) is markup-escaped: raw
    output like `[boot]` would otherwise be eaten as a rich tag.
    """
    from nymeria.service_install import (
        ServiceInstallError,
        ServiceUnavailableError,
        resolve_exec_argv,
        service_manager,
    )

    console.print("\nInstalling the background service...")
    try:
        manager = service_manager()
        report = manager.install(exec_argv=resolve_exec_argv(), root=root)
    except ServiceUnavailableError as exc:
        console.print(
            f"[yellow]Cannot install a background service here: {escape(str(exc))}[/yellow]"
        )
        for hint in exc.hints:
            console.print(f"  - {escape(hint)}")
        console.print("Start Nymeria in the foreground instead:")
        _print_command(console, "nymeria slim")
        return 0
    except (ServiceInstallError, OSError) as exc:
        console.print(f"[yellow]Service install failed: {escape(str(exc))}[/yellow]")
        console.print("Fix the cause, then install it yourself:")
        _print_command(console, _service_install_command(state))
        return 0
    for line in report.lines:
        console.print(f"[green]{escape(line)}[/green]")
    for warning in report.warnings:
        console.print(f"[yellow]{escape(warning)}[/yellow]")
    for note in report.notes:
        console.print(escape(note))
    if not wait_for_health(console=console, url=f"{local_base_url(state)}/health"):
        console.print(
            "[yellow]Installed, but the health check has not passed yet. It "
            f"may still be coming up; check `{escape(manager.log_hint())}` "
            "and `nymeria service status`.[/yellow]"
        )
        console.print(
            f"Once it answers, open {local_base_url(state)} and finish in the "
            "browser (the one-time bootstrap token, if any, was printed above)."
        )
        return 0
    console.print("[green]Nymeria is up.[/green]")
    if handoff_token:
        # Health passed, so the fragment lands on the served web UI, which
        # consumes it and signs in without the user ever seeing the token.
        # Fire-and-forget from a daemon thread: with $BROWSER set to a bare
        # command, webbrowser resolves a GenericBrowser whose open() BLOCKS
        # (Popen().wait()) until the browser exits, which would freeze the
        # wizard here (the LOCAL path isolates the same call for the same
        # reason).
        threading.Thread(
            target=_open_browser_with_token,
            args=(local_base_url(state), handoff_token),
            daemon=True,
            name="init-browser-open",
        ).start()
    verify_public_url_now(state, console)
    smoke_token = None
    if not state.skip_llm_test:
        smoke_token = _wait_for_host_service_token(resolve_data_dir(state, root=root))
    _run_inline_chat_smoke(state, console, token=smoke_token)
    if handoff_token:
        console.print(
            "\nYour browser should open already signed in; if it does not, "
            f"open {local_base_url(state)} and paste the token printed above. "
            "The service starts on login from now on; check it with "
            "`nymeria service status`."
        )
    else:
        console.print(
            f"\nOpen {local_base_url(state)} to finish in the browser (paste the "
            "one-time bootstrap token if one was printed above). The service "
            "starts on login from now on; check it with `nymeria service status`."
        )
    return 0


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


# --- post-start chat smoke test ----------------------------------------------

CHAT_SMOKE_TIMEOUT_SECONDS = 120.0  # one full agent turn, with LLM headroom
CHAT_SMOKE_HEALTH_TIMEOUT_SECONDS = 120.0  # foreground cold start
CHAT_SMOKE_TOKEN_TIMEOUT_SECONDS = 30.0
CHAT_SMOKE_MESSAGE = "Reply with exactly: INIT SMOKE OK"


def run_chat_smoke_test(
    *,
    token: str,
    base_url: str = "http://localhost:8000",
    timeout: float = CHAT_SMOKE_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """One real chat turn through POST /chat/sync. Returns (ok, detail); never raises.

    The deepest install check there is: the turn exercises auth, the agent
    runtime, and the configured LLM end to end (the pre-write provider test
    only proves the key works against the provider). Uses a throwaway thread
    id and best-effort deletes the thread afterward, so the smoke turn leaves
    nothing in the user's thread list. Authenticates with the slim service
    token: the bootstrap token is consumed by its first auth, which would
    break the printed handoff command.
    """
    import json
    import urllib.error
    import urllib.request

    thread_id = f"init-smoke-{secrets.token_hex(4)}"
    request = urllib.request.Request(
        f"{base_url}/chat/sync",
        data=json.dumps(
            {"message": CHAT_SMOKE_MESSAGE, "thread_id": thread_id}
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace").strip()[:200]
            return False, f"HTTP {exc.code} from /chat/sync: {detail}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # ValueError covers urllib's own rejects (e.g. a token with
            # control characters failing header validation).
            return False, f"could not reach /chat/sync: {exc}"
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False, "/chat/sync returned a non-JSON body"
        response_text = ""
        if isinstance(payload, dict):
            response_text = str(payload.get("response") or "").strip()
        if not response_text:
            return False, "/chat/sync answered without a model response"
        return True, f"the model answered ({response_text[:80]})"
    finally:
        cleanup = urllib.request.Request(
            f"{base_url}/threads/{thread_id}",
            headers={"Authorization": f"Bearer {token}"},
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(cleanup, timeout=10.0):  # noqa: S310
                pass
        except Exception:  # noqa: BLE001 (best-effort; a 409 mid-turn is fine)
            pass


def _smoke_health_ok(url: str) -> bool:
    """One quiet /health probe (the smoke thread's own loop, not wait_for_health)."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=2.0) as resp:  # noqa: S310
            return 200 <= getattr(resp, "status", 200) < 300
    except (urllib.error.URLError, OSError):
        return False


def _wait_for_host_service_token(
    data_dir: Path,
    *,
    timeout: float = CHAT_SMOKE_TOKEN_TIMEOUT_SECONDS,
    stop: threading.Event | None = None,
) -> str | None:
    """Poll for the API-minted service token file on the host filesystem.

    The slim server writes it during startup, so it should exist by the time
    /health answers; the short poll covers the write racing the health flip.
    """
    path = Path(data_dir) / SLIM_SERVICE_TOKEN_FILENAME
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            return None
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
        if raw:
            return raw
        if stop is not None:
            if stop.wait(0.5):
                return None
        else:
            time.sleep(0.5)
    return None


def _run_inline_chat_smoke(
    state: WizardState, console: Console, *, token: str | None
) -> None:
    """Run the post-start smoke turn and report. Never affects the exit code.

    Gated only on --skip-llm-test, NOT on the keep-existing-key reconfigure
    path: the server holds the key, so the smoke turn works where the
    pre-write provider check cannot.
    """
    if state.skip_llm_test:
        console.print(
            "[yellow]Skipping the chat smoke test (--skip-llm-test).[/yellow]"
        )
        return
    if not token:
        console.print(
            "[yellow]Could not read the service token, skipping the chat "
            "smoke test (the file is not minted when an operator-set "
            "NYMERIA_SERVICE_TOKEN is in use).[/yellow]"
        )
        return
    console.print(
        "Running a chat smoke test (one real chat turn; may take a minute)..."
    )
    ok, detail = run_chat_smoke_test(token=token, base_url=local_base_url(state))
    if ok:
        console.print(f"[green]Chat smoke test passed:[/green] {escape(detail)}")
    else:
        console.print(
            f"[yellow]Chat smoke test FAILED: {escape(detail)}.[/yellow] The "
            "backend is up; check the LLM credentials with `nymeria doctor`."
        )


def _spawn_local_smoke_thread(
    state: WizardState, *, root: Path, stop: threading.Event
) -> threading.Thread | None:
    """Start the foreground shape's background smoke worker (None when skipped)."""
    if state.skip_llm_test:
        return None
    thread = threading.Thread(
        target=_local_smoke_worker,
        args=(resolve_data_dir(state, root=root), stop, local_base_url(state)),
        daemon=True,
        name="init-chat-smoke",
    )
    thread.start()
    return thread


def _spawn_local_browser_open_thread(
    state: WizardState, *, token: str, stop: threading.Event
) -> threading.Thread:
    """Start the foreground shape's browser auto-open worker.

    The caller has already gated the handoff (`browser_token_handoff_allowed`);
    this only defers the open until the server answers /health.
    """
    thread = threading.Thread(
        target=_local_browser_open_worker,
        args=(local_base_url(state), token, stop),
        daemon=True,
        name="init-browser-open",
    )
    thread.start()
    return thread


def _local_browser_open_worker(
    base_url: str, token: str, stop: threading.Event
) -> None:
    """Wait for the foreground server to become healthy, then open the browser.

    Same discipline as `_local_smoke_worker`: quiet health polling (never
    `wait_for_health`, never a subprocess of its own beyond what
    `webbrowser.open` does), never raises, exits silently once `stop` is set.
    The opened URL carries the raw token in its fragment: nothing here prints.
    """
    try:
        deadline = time.monotonic() + CHAT_SMOKE_HEALTH_TIMEOUT_SECONDS
        while True:
            if stop.is_set():
                return
            if _smoke_health_ok(f"{base_url}/health"):
                break
            if time.monotonic() >= deadline:
                return
            if stop.wait(1.0):
                return
        _open_browser_with_token(base_url, token)
    except Exception:  # noqa: BLE001 (best effort; must never disturb the server)
        return


def _local_smoke_worker(
    data_dir: Path, stop: threading.Event, base_url: str = "http://localhost:8000"
) -> None:
    """Body of the foreground shape's smoke thread.

    The wizard blocks while the slim server owns the terminal, so this daemon
    thread waits for health and the minted service token, fires the smoke
    turn, and prints a single plain [smoke] line into the server's output.
    Exits silently whenever `stop` is set (the server already shut down).
    Never raises, never calls subprocess, and polls health with its own quiet
    probe rather than wait_for_health (the local start-now path is pinned to
    zero wait_for_health calls and exactly one subprocess call).
    """
    try:
        deadline = time.monotonic() + CHAT_SMOKE_HEALTH_TIMEOUT_SECONDS
        while True:
            if stop.is_set():
                return
            if _smoke_health_ok(f"{base_url}/health"):
                break
            if time.monotonic() >= deadline:
                # Never healthy: the user is watching the server output
                # directly, so a smoke line would only add noise.
                return
            if stop.wait(1.0):
                return
        token = _wait_for_host_service_token(data_dir, stop=stop)
        if stop.is_set():
            return
        if not token:
            print(
                "[smoke] chat smoke test skipped: no service token file yet",
                flush=True,
            )
            return
        ok, detail = run_chat_smoke_test(token=token, base_url=base_url)
        if stop.is_set():
            return
        if ok:
            print(f"[smoke] chat smoke test passed: {detail}", flush=True)
        else:
            print(
                f"[smoke] chat smoke test FAILED: {detail} (config was "
                "written fine; check `nymeria doctor`)",
                flush=True,
            )
    except Exception:  # noqa: BLE001 (a smoke worker must never take down the server)
        return


def _print_chat_smoke_recipe(
    console: Console, *, token_command: str, base_url: str = "http://localhost:8000"
) -> None:
    """The copy-paste smoke turn for the manual (print-commands) handoff."""
    console.print(
        "\nVerify a real chat turn once the backend is up (the deepest health "
        "check) with:"
    )
    _print_command(
        console,
        f"curl -s -X POST {base_url}/chat/sync "
        f'-H "Authorization: Bearer $({token_command})" '
        '-H "Content-Type: application/json" '
        "--data '{\"message\":\"Reply with exactly: INIT SMOKE OK\"}'",
    )


def _docker_token_command(spec: _DockerStackSpec) -> str:
    return _compose_command_str(
        spec, "exec", spec.service, "cat", f"/data/{BOOTSTRAP_TOKEN_FILENAME}"
    )


def _print_docker_token_command(console: Console, spec: _DockerStackSpec) -> None:
    console.print(
        "\nOnce the container is healthy, read your one-time bootstrap token "
        "(paste it into the Desktop/Mobile Setup Wizard) with:"
    )
    _print_command(console, _docker_token_command(spec))
    console.print(
        "\nRe-run setup anytime with `nymeria init`. Check health with "
        "`nymeria doctor`."
    )


def _read_docker_bootstrap_token(*, spec: _DockerStackSpec, root: Path) -> str | None:
    """Read the freshly minted bootstrap token from the stack's /data volume.

    The agent container creates the real `nym_...` token inside its named volume
    on first boot; this execs in to fetch it. Returns None if it cannot be read
    yet (e.g. the container is not ready or `docker` is absent).
    """
    return _read_docker_token_file(
        spec=spec, root=root, filename=BOOTSTRAP_TOKEN_FILENAME
    )


def _read_docker_token_file(
    *, spec: _DockerStackSpec, root: Path, filename: str
) -> str | None:
    """Exec a `nym_...` token file out of the stack's /data volume (None on failure).

    Shared by the bootstrap-token handoff and the chat smoke test (which reads
    the in-container service token instead; both are `nym_` account tokens).
    """
    command = _compose_argv(spec, "exec", "-T", spec.service, "cat", f"/data/{filename}")
    try:
        # env-gate: full-copy - same `_compose_env` as the `up -d` above and
        # for the same reason: compose resolves the service and file from
        # `${...}` in the process environment on the no-`--env-file` path. This
        # one only execs `cat` inside an already-running container, so the
        # environment does not reach a new image, but compose itself still
        # needs it to identify the project.
        result = subprocess.run(
            command,
            cwd=str(root),
            env=_compose_env(spec),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = re.search(BOOTSTRAP_TOKEN_REGEX, result.stdout)
    return match.group(0) if match else None


def _print_docker_bootstrap_token(
    console: Console,
    *,
    spec: _DockerStackSpec,
    root: Path,
    base_url: str = "http://localhost:8000",
) -> None:
    token = _read_docker_bootstrap_token(spec=spec, root=root)
    if token:
        console.print(f"\n[green]Bootstrap token:[/green] {token}", soft_wrap=True)
        console.print(
            "Paste this one-time `nym_...` token into the Desktop/Mobile Setup "
            "Wizard (it is consumed on first use). It is NOT your provider API key."
        )
        console.print(
            f"\nOpen {base_url} to use the web UI. Re-run setup anytime "
            "with `nymeria init`."
        )
    else:
        console.print(
            "\n[yellow]Could not read the bootstrap token from the container "
            "yet.[/yellow] Once it is healthy, run:"
        )
        _print_command(console, _docker_token_command(spec))


def _is_full_stack(state: WizardState) -> bool:
    return (
        state.hosting is HostingOption.DOCKER
        and (state.docker_stack or DockerStack.SLIM) is DockerStack.FULL
    )


def _warn_shadowing_process_env(console: Console, written: Mapping[str, str]) -> None:
    """Warn when a value written to `.env.docker` is shadowed by the environment.

    docker compose resolves `${VAR}` from the process environment BEFORE the
    `--env-file`, so a `POSTGRES_PASSWORD` / `REDIS_PASSWORD` / `NYMERIA_SECRETS_KEY`
    already in the environment with a different value silently wins over the one
    just generated. The shadowing value can come from the operator's shell OR from
    another `.env.docker` auto-loaded at startup (a common out-of-tree-install case),
    so the message does not assume the source. The mismatch is invisible now but
    breaks Postgres auth or vault decryption on a later boot, so flag it.
    """
    clashes = sorted(
        key
        for key, value in written.items()
        if value and key in os.environ and os.environ[key].strip() != value
    )
    if not clashes:
        return
    joined = ", ".join(clashes)
    console.print(
        f"[yellow]Note:[/yellow] the environment already defines {joined} with a "
        "value different from what was just written to .env.docker (exported in your "
        "shell, or auto-loaded from another .env.docker). docker compose reads "
        "${VAR} from the environment before --env-file, so that value, not the "
        "generated one, would take effect. Clear it before bringing the stack up:"
    )
    # Printed via _print_command so the command stays on one unwrapped line.
    _print_command(console, f"unset {' '.join(clashes)}")


def _file_defined_env_keys(root: Path) -> frozenset[str]:
    """Keys the on-disk env files define right now. Never raises.

    Covers both the runtime root run.py loaded its import-time environment
    from (the source of the process-env pollution) and the target ``root``;
    they differ when a packaged install reconfigures an alternate ``--root``.
    """
    from dotenv import dotenv_values

    from ..config.settings import get_env_file_paths

    keys: set[str] = set()
    for path in (*get_env_file_paths(), *get_env_file_paths(root)):
        try:
            if path.is_file():
                keys.update(key for key in dotenv_values(str(path)) if key)
        except (OSError, UnicodeDecodeError):
            continue
    return frozenset(keys)


def _maybe_run_doctor(
    state: WizardState,
    *,
    root: Path,
    console: Console,
    provider_auth_validated: bool,
    stale_env_keys: frozenset[str] | None = None,
) -> int:
    if not (state.run_doctor or state.full_doctor):
        return 0
    skip_llm_test = not state.full_doctor
    command = "nymeria doctor"
    if skip_llm_test:
        command += " --skip-llm-test"
    console.print(f"\nRunning final validation: [bold]{command}[/bold]")
    # run.py loaded the PRE-wizard config into os.environ at import time
    # (override=True), and pydantic-settings reads the live env over
    # `_env_file`, so doctor would validate the old values after a reconfigure
    # (a stale API_PORT probe was the visible symptom). Drop the keys the old
    # files defined (a replay cannot clear a key the rewrite REMOVED), then
    # replay the files this run just wrote. Scoped to the doctor call and
    # restored afterwards so nothing leaks past it.
    from dotenv import load_dotenv

    from ..config.settings import get_env_file_paths

    saved_env = os.environ.copy()
    try:
        for key in stale_env_keys or ():
            os.environ.pop(key, None)
        for path in get_env_file_paths(root):
            try:
                if path.is_file():
                    load_dotenv(path, override=True)
            except (OSError, UnicodeDecodeError):
                continue
        result = run_doctor_for_root(root, skip_llm_test=skip_llm_test)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
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
    "resolve_data_dir",
    "default_init_root",
    "check_writable",
    "port_in_use",
    "print_bootstrap_token_handoff",
    "bootstrap_token_copy_command",
    "print_capability_summary",
    "print_deployment_summary",
    "print_next_action",
    "browser_token_handoff_allowed",
    "run_next_action",
    "wait_for_health",
    "run_doctor_for_root",
]
