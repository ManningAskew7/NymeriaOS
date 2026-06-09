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
from typing import Any

from ..config.llm_providers import LLMProviderSpec, get_llm_provider_spec
from ..onboarding import (
    ExternalAccess,
    HostingOption,
    ImageTier,
    NextAction,
    ProviderAuthMethod,
    SecurityProfile,
)


@dataclass
class WizardState:
    # Deployment target: how to host the slim backend on this machine.
    hosting: HostingOption | None = None

    # Container image capability tier (only meaningful for container hosts).
    # Placeholder: image generation is not wired into finalize yet.
    image_tier: ImageTier | None = None

    # First-run security posture. Recorded now; enforcement is built out later.
    security_profile: SecurityProfile | None = None

    # How the primary LLM is authenticated. Only the direct API-key path is wired;
    # subscription OAuth via CLIProxy is deferred (the auth step gates it).
    auth_method: ProviderAuthMethod = ProviderAuthMethod.API_KEY

    # LLM provider + credentials (API-key path).
    provider: str | None = None
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    api_mode: str = ""  # "", "responses", or "chat_completions"

    # How the backend is reached from outside this machine. Placeholder: the
    # wizard records the choice and finalize prints the matching guidance.
    external_access: ExternalAccess | None = None

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

    # Quick path (`nymeria init --quick`): gate the wizard to the essentials
    # (hosting + LLM) and default everything else with no-extra-auth picks. See
    # setup/quick.py for the kept-step set and the defaults applied.
    quick: bool = False

    # Paths and post-setup behavior (driven by flags, not wizard screens yet).
    root: Path | None = None
    data_dir: Path | None = None
    next_action: NextAction = NextAction.PRINT_COMMANDS
    skip_llm_test: bool = False
    force: bool = False
    run_doctor: bool = False
    full_doctor: bool = False

    # --- transient reconfigure state (hydrated from disk, never persisted) ----
    # True when `nymeria init` ran against an existing install (see setup/hydrate).
    # Switches finalize to a merge-write and lets steps show "keep existing" copy.
    reconfigure: bool = False
    # Secret env vars found set on disk (provider key, *_API_KEY, NYMERIA_SECRETS_KEY,
    # SEARXNG_BASE_URL). Their VALUES are deliberately never read into state; this
    # records presence so a blank field means "keep" and finalize does not blank a
    # working key or downgrade a provider to "unconfigured".
    present_env_keys: set[str] = field(default_factory=set)
    # Tools in the bootstrap profile's default set that are neither the core seed
    # nor a known init family member (user-added). Carried through a reconfigure so
    # the profile-pick update never silently drops them.
    unmanaged_tools: list[str] = field(default_factory=list)

    def provider_spec(self) -> LLMProviderSpec | None:
        """Return the registry LLMProviderSpec for the chosen provider, or None."""
        if self.provider is None:
            return None
        return get_llm_provider_spec(self.provider)


__all__ = ["WizardState"]
