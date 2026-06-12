"""Load an existing install's settings back into ``WizardState`` (reconfigure).

When ``nymeria init`` runs against an install that already has a config file,
this fills ``WizardState`` from disk so every step shows the current value as its
default and finalize can merge-write only what changed. Fill-only-if-unset: an
explicit flag (e.g. ``--model``) always wins over a hydrated value, mirroring
``quick.apply_quick_defaults`` / ``rag_catalog.apply_quickstart_rag``.

TUI-free and read-only. Reuses ``dotenv.dotenv_values`` (non-mutating) for the
env file and reads the bootstrap ``profile.json`` directly (not via
``get_profile``, which injects default skills and would mask "never customized").

Deliberately NOT round-tripped (see the setup-wizard doc): secrets are recorded
as present (never re-read into the UI); security_profile is recorded but never
written, so it has no source. Hosting DOES round-trip: finalize writes the
choice to the NYMERIA_HOSTING marker, and marker-less configs (written before
the marker existed) fall back to the installed service artifact to tell LOCAL
from SERVICE. External access DOES round-trip: finalize writes the choice
to NYMERIA_EXTERNAL_ACCESS plus the resolved NYMERIA_PUBLIC_URL/CORS_ORIGINS,
and they hydrate back here. `auth_method` is inferred:
a CLIProxy-looking LLM_BASE_URL selects the subscription branch (see
`_hydrate_cliproxy`), anything else stays the API-key default. Tool/skill picks hydrate
from `profile.json` for local/service installs; for Docker (whose profile lives
in the container volume) they hydrate from the `NYMERIA_INIT_*` carrier lines in
`.env.docker` instead. The carriers record first-boot intent at the env-file
layer, which is what finalize re-writes, so an untouched interactive reconfigure
round-trips them verbatim. One bounded deviation: the interactive fetch step
default-checks the keyless `fetch_url_nymeria` when the hydrated state holds no
fetch_url pick, so that path can (re)seed that one default into the carrier;
harmless, since it is what a fresh first run would seed anyway (quick mode used
to share this deviation, but its fill check is now key-presence, so a hydrated
empty fetch family survives a quick reconfigure). Settings edits made inside a running container are invisible here,
and that is fine because the carriers only ever matter to a future fresh volume. The Docker
stack IS recoverable (the full stack writes POSTGRES_PASSWORD; the slim shape
never does).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from rich.console import Console

from ..onboarding import HOSTING_MARKER_ENV, ExternalAccess, HostingOption
from . import family_catalog, finalize
from .external_access import CORS_ORIGINS_ENV, EXTERNAL_ACCESS_ENV, PUBLIC_URL_ENV
from .providers import OPTIONAL_ENV_ORDER
from .rag_catalog import embedder_id_for_env, reranker_id_for_env
from .state import WizardState

# Env vars whose mere presence means "already configured" (so a blank field keeps
# them and the backend-keys step does not re-prompt). The provider's own key var
# is added dynamically once the provider is known.
_SECRET_ENV_VARS = set(OPTIONAL_ENV_ORDER) | {
    "NYMERIA_SECRETS_KEY",
    "CLIPROXY_MANAGEMENT_KEY",
}

BOOTSTRAP_USER_ID = "default"


def hydrate_state_from_disk(state: WizardState, *, console: Optional[Console] = None) -> bool:
    """Fill ``state`` from an existing on-disk config. Return True if one was found.

    Returns False (and leaves ``state`` untouched) for a fresh install, so the
    caller keeps first-run behavior.
    """
    located = _locate_config(state)
    if located is None:
        return False
    config_path, for_docker = located

    try:
        from dotenv import dotenv_values

        values = {k: v for k, v in dotenv_values(str(config_path)).items() if v is not None}
    except OSError:
        return False

    state.reconfigure = True

    if state.hosting is None:
        if for_docker:
            state.hosting = HostingOption.DOCKER
        else:
            state.hosting = _recover_local_hosting(values)

    if for_docker and state.docker_stack is None:
        # Slim vs full is recoverable: the full stack writes POSTGRES_PASSWORD,
        # the slim shape never does. An explicit --docker-stack flag still wins.
        from ..onboarding import DockerStack

        state.docker_stack = (
            DockerStack.FULL if _get(values, "POSTGRES_PASSWORD") else DockerStack.SLIM
        )

    if _get(values, "API_PORT"):
        try:
            disk_port = int((_get(values, "API_PORT") or "").strip())
        except ValueError:
            disk_port = None  # hand-edited junk; keep the default
        # Out-of-range values are junk too: socket probes raise OverflowError
        # past 65535, so an unvalidated hand-edit would crash detection.
        if disk_port is not None and 1 <= disk_port <= 65535:
            # Stashed so finalize can warn when the port CHANGES on an install
            # whose tunnel ingress still forwards to the old one.
            state.extras.setdefault("api_port_on_disk", disk_port)
            if state.api_port is None:
                state.api_port = disk_port

    if state.provider is None and _get(values, "LLM_PROVIDER"):
        state.provider = _get(values, "LLM_PROVIDER")
    if not state.model and _get(values, "LLM_MODEL"):
        state.model = _get(values, "LLM_MODEL") or ""
    if not state.base_url and _get(values, "LLM_BASE_URL"):
        state.base_url = _get(values, "LLM_BASE_URL") or ""
    if not state.api_mode and _get(values, "OPENAI_API_MODE"):
        state.api_mode = _get(values, "OPENAI_API_MODE") or ""

    # External access: the wizard's own round-trip marker plus the real
    # settings written with it. A bad marker value (hand-edited) is ignored.
    disk_choice = None
    if _get(values, EXTERNAL_ACCESS_ENV):
        try:
            disk_choice = ExternalAccess(_get(values, EXTERNAL_ACCESS_ENV) or "")
        except ValueError:
            pass  # hand-edited marker value; treat as not recorded
    if disk_choice is not None:
        state.external_access_recorded = True
        if state.external_access is None:
            state.external_access = disk_choice
    # An explicit --external-access flag that DIFFERS from the recorded choice
    # is a switch: the old choice's URL must not ride along under the new one
    # (the flag wins before hydrate runs, so store_external_access_choice's
    # clearing never sees the change; mirror it here).
    choice_switched = (
        disk_choice is not None
        and state.external_access is not None
        and state.external_access is not disk_choice
    )
    if not state.public_url and not choice_switched and _get(values, PUBLIC_URL_ENV):
        state.public_url = _get(values, PUBLIC_URL_ENV) or ""
    if not state.existing_cors_origins and _get(values, CORS_ORIGINS_ENV):
        state.existing_cors_origins = _get(values, CORS_ORIGINS_ENV) or ""

    # RAG: reverse-map the env back to a catalog id (only if not already chosen and
    # not auto-quickstarted, which a flag/quick path may have done).
    if state.embedder is None and not state.rag_quickstarted:
        emb_id = embedder_id_for_env(
            _get(values, "EMBEDDING_PROVIDER"),
            _get(values, "EMBEDDING_MODEL"),
            _get(values, "EMBEDDING_DIMENSIONS"),
        )
        if emb_id is not None:
            state.embedder = emb_id
            if _get(values, "RAG_RETRIEVAL_MODE") == "vector":
                state.rag_retrieval_mode = "vector"
            if state.reranker is None:
                rer_id = reranker_id_for_env(
                    _get(values, "RAG_RERANK_PROVIDER"),
                    _get(values, "RAG_RERANK_MODEL"),
                    _get(values, "RAG_RERANK_ENABLED"),
                )
                if rer_id is not None:
                    state.reranker = rer_id

    # Voice: round-trip the provider picks (the env values ARE the choice
    # values). The on-disk provider is also recorded under *_on_disk so
    # finalize and the backend-keys step can tell a provider SWITCH (retire
    # stale provider-scoped lines, re-ask the key) from an untouched
    # reconfigure. Unknown hand-edited values are treated as not recorded.
    from .voice_catalog import STT_VALUES, TTS_VALUES

    tts_on_disk = (_get(values, "TTS_PROVIDER") or "").strip()
    if tts_on_disk in TTS_VALUES:
        state.extras["tts_on_disk"] = tts_on_disk
        state.extras.setdefault("tts", tts_on_disk)
    stt_on_disk = (_get(values, "STT_PROVIDER") or "").strip()
    if stt_on_disk in STT_VALUES:
        state.extras["stt_on_disk"] = stt_on_disk
        state.extras.setdefault("stt", stt_on_disk)
    # Base URLs recorded so a hosting-shape switch can retire a wizard-written
    # sidecar URL that no longer resolves (voice_catalog.voice_drop_env).
    if _get(values, "TTS_BASE_URL"):
        state.extras["tts_base_url_on_disk"] = _get(values, "TTS_BASE_URL")
    if _get(values, "STT_BASE_URL"):
        state.extras["stt_base_url_on_disk"] = _get(values, "STT_BASE_URL")

    # Agent tuning (context strategy, limits, effort + sampling): round-trip
    # the on-disk lines into the wizard's raw input strings so a reconfigure
    # shows current values and an untouched walk-through re-produces them.
    # The seeded keys are recorded so finalize can tell a CLEARED field
    # (blank now means "retire the line, back to default") from a step that
    # was never visited (tuning_catalog.tuning_drop_env).
    from .tuning_catalog import tuning_extras_from_env

    hydrated_tuning = tuning_extras_from_env(lambda var: _get(values, var))
    for key, value in hydrated_tuning.items():
        state.extras.setdefault(key, value)
    if hydrated_tuning:
        state.extras["tuning_on_disk_keys"] = sorted(hydrated_tuning)

    if state.data_dir is None and not for_docker and _get(values, "NYMERIA_DATA_DIR"):
        state.data_dir = Path(_get(values, "NYMERIA_DATA_DIR") or "")

    _record_present_keys(state, values)
    _hydrate_cliproxy(state, values)

    if for_docker:
        _hydrate_carrier_picks(state, values)
    else:
        _hydrate_profile_picks(state, for_docker=for_docker)

    if console is not None:
        where = "Docker (.env.docker)" if for_docker else str(config_path)
        console.print(f"[green]Reconfiguring[/green] the existing install at {where}")
    return True


def _locate_config(state: WizardState) -> Optional[tuple[Path, bool]]:
    """Find the highest-precedence existing config file and its shape.

    Honors an explicit ``--root``; otherwise checks the Docker source-checkout
    root (for ``.env.docker``) and the normal init root (for ``config.env``/
    ``.env``). The first existing file wins; ``.env.docker`` implies Docker.
    """
    candidates: list[tuple[Path, bool]] = []
    if state.root is not None:
        root = Path(state.root).expanduser().resolve()
        candidates = [
            (root / ".env.docker", True),
            (root / "config.env", False),
            (root / ".env", False),
        ]
    else:
        docker_root = finalize.resolve_runtime_root(state, for_docker=True)
        local_root = finalize.resolve_runtime_root(state, for_docker=False)
        candidates = [
            (docker_root / ".env.docker", True),
            (local_root / "config.env", False),
            (local_root / ".env", False),
        ]
    for path, is_docker in candidates:
        if path.exists():
            return path, is_docker
    return None


def _record_present_keys(state: WizardState, values: dict[str, str]) -> None:
    """Record which secret/credential env vars are already set (presence only).

    Every provider key slot is recorded, not just the first: the CLIProxy
    branch writes the gatekeeper into the SECOND Anthropic slot
    (ANTHROPIC_API_KEY), and finalize's keep-existing-key check must see it
    or a Claude-subscription reconfigure downgrades to "no provider".
    """
    secret_vars = set(_SECRET_ENV_VARS)
    spec = state.provider_spec()
    if spec is not None and spec.api_key_env_vars:
        secret_vars.update(spec.api_key_env_vars)
    for var in secret_vars:
        if (values.get(var) or "").strip():
            state.present_env_keys.add(var)


def _hydrate_cliproxy(state: WizardState, values: dict[str, str]) -> None:
    """Recover the CLIProxy subscription branch from a routed install.

    `auth_method` itself is never written to disk; it is inferred from the
    LLM base URL looking like a CLIProxy endpoint (the same signal the
    runtime cloak uses), unless an explicit --auth-method flag won. The
    concrete CLI is inferred from the route shape where unambiguous:
    anthropic at the proxy root is Claude, openai+/v1 in responses mode is
    Codex; the openai+/v1 chat shape is shared by every other CLI, so the
    pick is left for the provider step (prefilled by its model default).
    """
    from ..onboarding import ProviderAuthMethod
    from ..vendor.react_agent.cliproxy import looks_like_cliproxy_url

    base_url = _get(values, "LLM_BASE_URL") or ""
    if not state.cliproxy_management_url and _get(values, "CLIPROXY_MANAGEMENT_URL"):
        state.cliproxy_management_url = _get(values, "CLIPROXY_MANAGEMENT_URL") or ""
    if state.auth_method_explicit or not looks_like_cliproxy_url(base_url):
        return
    # The port heuristic alone is too coarse for an auth-model rewrite (any
    # service on 8317/8318 matches it). Require a second signal: either the
    # install recorded a management endpoint, or the hostname itself says
    # cliproxy. A direct-key install pointing at some other 8318 endpoint
    # stays on the API-key branch.
    host_says_cliproxy = "cli-proxy" in base_url or "cliproxy" in base_url
    if not host_says_cliproxy and not _get(values, "CLIPROXY_MANAGEMENT_URL"):
        return
    state.auth_method = ProviderAuthMethod.CLIPROXY_OAUTH
    if state.cliproxy_provider is None:
        provider = (_get(values, "LLM_PROVIDER") or "").lower()
        on_v1 = base_url.rstrip("/").endswith("/v1")
        api_mode = (_get(values, "OPENAI_API_MODE") or "").lower()
        if provider == "anthropic" and not on_v1:
            state.cliproxy_provider = "claude"
        elif provider == "openai" and on_v1 and api_mode == "responses":
            state.cliproxy_provider = "codex"


def _apply_tool_picks(state: WizardState, default_tools: list[str]) -> None:
    """Partition a ``default_thread_tools`` list back into wizard state.

    Subtracts the core seed, sorts known family members into their family extras,
    and records tools that are neither core nor a known family member as
    ``unmanaged_tools`` so a reconfigure never drops user-added tools.
    Fill-only-if-unset: an explicit flag still wins.
    """
    from .tool_seed import core_seed_tool_names

    core = set(core_seed_tool_names())
    families = {
        "web_search": {c.value for c in family_catalog.web_search_choices()},
        "fetch_url": {c.value for c in family_catalog.fetch_url_choices()},
        "image_gen": {c.value for c in family_catalog.image_gen_choices()},
    }
    matched: dict[str, list[str]] = {fam: [] for fam in families}
    unmanaged: list[str] = []
    for name in default_tools:
        if name in core:
            continue
        for fam, members in families.items():
            if name in members:
                matched[fam].append(name)
                break
        else:
            unmanaged.append(name)
    for fam, names in matched.items():
        if fam not in state.extras:
            state.extras[fam] = names
    if unmanaged and not state.unmanaged_tools:
        state.unmanaged_tools = unmanaged


def _apply_skill_picks(state: WizardState, enabled_skills: list[str]) -> None:
    """Keep the known skill kits from an ``enabled_global_skills`` list.

    Non-kit entries (e.g. the always-on ``self-improve``) are dropped here and
    re-added by ``tool_seed.selected_global_skills_for_state`` on write, so the
    round-trip is exact. Fill-only-if-unset.
    """
    if "skill_kits" in state.extras:
        return
    kit_values = {c.value for c in family_catalog.skill_kit_choices()}
    state.extras["skill_kits"] = [s for s in enabled_skills if s in kit_values]


def _hydrate_profile_picks(state: WizardState, *, for_docker: bool) -> None:
    """Recover the bootstrap admin's tool/skill picks from ``profile.json``.

    Local/service only (the Docker profile lives in the container volume; see
    ``_hydrate_carrier_picks``). Reads the JSON directly so default-skill
    migration does not mask the real picks.
    """
    root = finalize.resolve_runtime_root(state, for_docker=False)
    data_dir = finalize.resolve_data_dir(state, root=root)
    profile_path = data_dir / "users" / BOOTSTRAP_USER_ID / "profile.json"
    if not profile_path.exists():
        return
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return

    default_tools = raw.get("tool_preferences", {}).get("default_thread_tools")
    if isinstance(default_tools, list):
        _apply_tool_picks(state, default_tools)

    enabled_skills = raw.get("enabled_global_skills")
    if isinstance(enabled_skills, list):
        _apply_skill_picks(state, enabled_skills)


def _hydrate_carrier_picks(state: WizardState, values: dict[str, str]) -> None:
    """Recover Docker tool/skill picks from the ``NYMERIA_INIT_*`` carrier lines.

    The host cannot read the container's profile, but the carriers in
    ``.env.docker`` are the env-file layer's own record of the picks, and that
    layer is what a reconfigure rewrites: hydrating them lets finalize re-emit
    unchanged picks (instead of silently keeping a stale line) and lets an
    explicit revert-to-defaults clear the carrier. No carrier lines means
    defaults, which is also what an absent carrier seeds.
    """
    from ..config.init_seed_env import (
        INIT_DEFAULT_THREAD_TOOLS_ENV,
        INIT_ENABLED_GLOBAL_SKILLS_ENV,
        parse_init_name_list,
    )

    tools = parse_init_name_list(values.get(INIT_DEFAULT_THREAD_TOOLS_ENV))
    if tools:
        _apply_tool_picks(state, tools)
    skills = parse_init_name_list(values.get(INIT_ENABLED_GLOBAL_SKILLS_ENV))
    if skills:
        _apply_skill_picks(state, skills)


def _recover_local_hosting(values: dict[str, str]) -> HostingOption:
    """LOCAL vs SERVICE for a non-Docker config.

    The NYMERIA_HOSTING marker (written by finalize) is authoritative, so
    switching away from SERVICE sticks even while the old unit is still
    installed. Marker-less configs predate the marker: fall back to the
    installed service artifact (unit/plist) as the durable SERVICE signal.
    """
    marker = _get(values, HOSTING_MARKER_ENV)
    if marker in (HostingOption.LOCAL.value, HostingOption.SERVICE.value):
        return HostingOption(marker)
    from nymeria.service_install import installed_artifact_path

    return HostingOption.SERVICE if installed_artifact_path() else HostingOption.LOCAL


def _get(values: dict[str, str], key: str) -> Optional[str]:
    raw = values.get(key)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


__all__ = ["hydrate_state_from_disk"]
