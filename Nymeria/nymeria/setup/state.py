"""Shared answer model for the first-run wizard.

`WizardState` is the single source of truth for everything the wizard collects.
Every step reads its initial value from here and writes its answer back here, so
navigating back and forward is lossless even though Textual recreates screens.
The headless finalize path consumes the same object, so the interactive and
non-interactive flows converge on one shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config.llm_providers import LLMProviderSpec, get_llm_provider_spec

if TYPE_CHECKING:
    from .environment import EnvironmentReport
from ..onboarding import (
    DockerStack,
    ExternalAccess,
    HostingOption,
    NextAction,
    ProviderAuthMethod,
    SecurityProfile,
    legacy_cliproxy_provider,
)


@dataclass
class WizardState:
    # Deployment target: how to host the slim backend on this machine.
    hosting: HostingOption | None = None

    # Docker runtime shape (only meaningful for Docker hosts): slim single
    # container vs the full Postgres + Redis stack. See onboarding.DockerStack.
    docker_stack: DockerStack | None = None

    # First-run security posture. Recorded now; enforcement is built out later.
    security_profile: SecurityProfile | None = None

    # API listen port, written as API_PORT; printed URLs, health checks, and
    # the remote-access ingress all follow it. None means the default 8000.
    api_port: int | None = None

    # How the primary LLM is authenticated: a direct API key, or the
    # provider-generic subscription branch through CLIProxy.
    auth_method: ProviderAuthMethod = ProviderAuthMethod.API_KEY
    # True when --auth-method was passed explicitly; hydrate then never
    # re-infers the branch from a CLIProxy-looking LLM_BASE_URL on disk.
    auth_method_explicit: bool = False

    # LLM provider + credentials (API-key path). The CLIProxy branch fills
    # these too, at finalize time, derived from the catalog spec and the
    # hosting shape (see finalize._apply_cliproxy_route).
    provider: str | None = None
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    api_mode: str = ""  # "", "responses", or "chat_completions"

    # --- CLIProxy subscription-OAuth branch (auth_method = CLIPROXY_OAUTH) ----
    # Catalog id of the chosen CLI (see nymeria/cliproxy/catalog.py).
    cliproxy_provider: str | None = None
    # Proxy host root as reachable from THIS machine (the wizard drives the
    # /v0/management OAuth dance through it). Finalize derives the
    # backend-facing URL per hosting shape separately.
    cliproxy_management_url: str = ""
    # Remote-management secret. Blank on a reconfigure means "keep the one on
    # disk" (the steps read it back from the env file at use time, never into
    # the UI).
    cliproxy_management_key: str = ""
    # Data-plane gatekeeper key (cpx-...); fetched or minted through the
    # management API by the login step when not supplied.
    cliproxy_gatekeeper_key: str = ""
    # True when the wizard generated and started <root>/cliproxy/ itself.
    cliproxy_deploy: bool = False
    # Set by the login step once an active auth file exists for the pick.
    cliproxy_logged_in: bool = False

    # How the backend is reached from outside this machine. The choice gates
    # the tailscale/cloudflare setup steps; the resolved public origin lands in
    # NYMERIA_PUBLIC_URL and CORS_ORIGINS at finalize.
    external_access: ExternalAccess | None = None
    # Public origin (https://...) resolved by a setup step, --public-url, or
    # hydrate. Empty means no remote origin is configured.
    public_url: str = ""
    # True once /health (and streaming) answered through public_url this run.
    public_url_verified: bool = False
    # Tailscale exposure mode from the setup step: "serve" (tailnet-only HTTPS,
    # the default) or "funnel" (public HTTPS).
    tailscale_exposure: str = ""
    # Transient: the Cloudflare named-tunnel connector token minted by the
    # setup step. Persisted under <root>/cloudflared/ (0600), never in env.
    cloudflare_tunnel_token: str = ""
    # CORS_ORIGINS found on disk during a reconfigure, so finalize appends the
    # public origin to the operator's list instead of resetting to defaults.
    existing_cors_origins: str = ""
    # True when hydrate found the NYMERIA_EXTERNAL_ACCESS marker on disk: the
    # on-disk public URL was wizard-written, so abandoning the choice may
    # retire it. A hand-set URL (no marker) is never dropped (transient,
    # hydrated, never persisted itself).
    external_access_recorded: bool = False

    # Optional capability keys (EMBEDDING/OPENAI/GEMINI/PERPLEXITY).
    optional_env: dict[str, str] = field(default_factory=dict)

    # Capability/family steps store their selection here, keyed by step id.
    # Multi-select families (web_search, fetch_url, skill_kits) store lists (of
    # concrete tool names, or kit names for skill_kits); single-select
    # placeholders store one value (or "__skip__").
    extras: dict[str, Any] = field(default_factory=dict)

    # RAG: chosen embedder/reranker catalog ids (see setup/rag_catalog.py). The
    # API keys ride in optional_env (EMBEDDING_API_KEY / RAG_RERANK_API_KEY).
    embedder: str | None = None
    reranker: str | None = None
    # RAG retrieval mode: "hybrid" (BM25 + vector, default) or "vector".
    rag_retrieval_mode: str = "hybrid"
    # True once the free local stack (granite + Ettin) was auto-equipped because
    # the user skipped RAG setup (or a quick path applied it), as opposed to an
    # explicit catalog pick. Gates the reranker step off so a skip skips both
    # screens, and lets finalize tell a default apart from a deliberate choice.
    rag_quickstarted: bool = False

    # Quick path (the quickstart tier, or `nymeria init --quick`): gate the
    # wizard to the essentials and default everything else with no-extra-auth
    # picks. See setup/quick.py for the kept-step set and the defaults applied.
    quick: bool = False
    # True when --quick or --custom preset the tier: the chooser screen
    # (steps/tier.py) is skipped so scripted runs behave exactly as before.
    tier_locked: bool = False

    # Paths and post-setup behavior (driven by flags, not wizard screens yet).
    root: Path | None = None
    data_dir: Path | None = None
    next_action: NextAction = NextAction.PRINT_COMMANDS
    # Print the connection URL + account token on the finalization screen. The
    # URL and `nymeria cli` command are non-secret and always print; this gates
    # the token VALUE. Defaults on for the interactive wizard, off for
    # --non-interactive (see runner._build_state) so a token never lands in
    # captured stdout. The start-now step exposes it as a toggle.
    print_credentials: bool = True
    skip_llm_test: bool = False
    force: bool = False
    run_doctor: bool = False
    full_doctor: bool = False

    # --- transient reconfigure state (hydrated from disk, never persisted) ----
    # True when `nymeria init` ran against an existing install (see setup/hydrate).
    # Switches finalize to a merge-write and lets steps show "keep existing" copy.
    reconfigure: bool = False
    # Host detection snapshot (see setup/environment.py), cached once by the
    # runner so the welcome screen, the hosting gates, and the review heads-ups
    # all read one consistent report (transient, never persisted).
    env_report: EnvironmentReport | None = None
    # Secret env vars found set on disk (provider key, *_API_KEY, NYMERIA_SECRETS_KEY,
    # SEARXNG_BASE_URL). Their VALUES are deliberately never read into state; this
    # records presence so a blank field means "keep" and finalize does not blank a
    # working key or downgrade a provider to "unconfigured".
    present_env_keys: set[str] = field(default_factory=set)
    # Tools in the bootstrap profile's default set that are neither the core seed
    # nor a known init family member (user-added). Carried through a reconfigure so
    # the profile-pick update never silently drops them.
    unmanaged_tools: list[str] = field(default_factory=list)

    def resolved_api_port(self) -> int:
        """The chosen API port with the default applied."""
        return self.api_port or 8000

    def provider_spec(self) -> LLMProviderSpec | None:
        """Return the registry LLMProviderSpec for the chosen provider, or None."""
        if self.provider is None:
            return None
        return get_llm_provider_spec(self.provider)

    def auth_method_is_cliproxy(self) -> bool:
        """True on the CLIProxy subscription branch (incl. legacy aliases)."""
        return (
            self.auth_method is ProviderAuthMethod.CLIPROXY_OAUTH
            or legacy_cliproxy_provider(self.auth_method) is not None
        )


__all__ = ["WizardState"]
