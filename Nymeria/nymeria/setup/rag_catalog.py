"""Catalog of RAG embedder and reranker options for first-run setup.

TUI-free (like ``setup/providers.py``) so the interactive wizard, the headless
finalize path, and the quickstart helper share one source of truth. The
premium / value / local recommendations come from an internal retrieval-quality
eval on a blended 40k-chunk agentic corpus (see
``docs/private/rag/rag-eval-notes.md``; the public summary is in the local-LLM
doc).

Each option maps a stable id (stored on ``WizardState``) to the production env
vars the backend reads (``EMBEDDING_*`` / ``RAG_RERANK_*``). Secrets ride in
``WizardState.optional_env`` (``EMBEDDING_API_KEY`` / ``RAG_RERANK_API_KEY``);
everything else is emitted by ``rag_env_for_state()``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .state import WizardState

TIER_LABELS = {
    "premium": "Premium",
    "value": "Value",
    "local": "Local",
    "none": "None",
}


@dataclass(frozen=True)
class EmbedderOption:
    id: str
    tier: str  # premium | value | local
    label: str
    description: str
    provider: str  # EMBEDDING_PROVIDER: openai | cohere | gemini | local
    model: str  # EMBEDDING_MODEL
    dimensions: int  # EMBEDDING_DIMENSIONS
    requires_key: bool
    key_vendor: Optional[str] = None  # reuse tag shared with rerankers
    base_url: Optional[str] = None  # EMBEDDING_BASE_URL
    input_type: Optional[str] = None  # EMBEDDING_INPUT_TYPE
    recommended: bool = False
    key_label: str = "API key"


@dataclass(frozen=True)
class RerankerOption:
    id: str
    tier: str  # premium | value | local | none
    label: str
    description: str
    provider: str  # RAG_RERANK_PROVIDER: voyage | cohere | zeroentropy | local | none
    model: Optional[str]  # RAG_RERANK_MODEL
    requires_key: bool
    key_vendor: Optional[str] = None
    onnx_file: Optional[str] = None  # RAG_RERANK_LOCAL_ONNX_FILE
    recommended: bool = False
    key_label: str = "API key"


# Embedders, in display order (premium, value, local). The vector-only quality
# ranking on the eval corpus is Cohere > Gemini (free) > Voyage-lite > granite;
# a reranker narrows the gap, so the value and local tiers stay strong picks.
EMBEDDERS: list[EmbedderOption] = [
    EmbedderOption(
        id="premium-cohere",
        tier="premium",
        label="Cohere embed-v4 (1024-d)",
        description=(
            "Top retrieval quality in our eval, with the biggest lead on "
            "structured, code, and tool content. Needs a Cohere API key (paid)."
        ),
        provider="cohere",
        model="embed-v4.0",
        dimensions=1024,
        requires_key=True,
        key_vendor="cohere",
        recommended=True,
        key_label="Cohere API key",
    ),
    EmbedderOption(
        id="value-gemini",
        tier="value",
        label="Gemini embedding-001 (free tier)",
        description=(
            "About 98% of premium quality at roughly free on Google's free tier: "
            "the best quality per dollar. Needs a Google AI (Gemini) API key."
        ),
        provider="gemini",
        model="gemini-embedding-001",
        dimensions=1024,
        requires_key=True,
        key_vendor="gemini",
        recommended=True,
        key_label="Google AI (Gemini) API key",
    ),
    EmbedderOption(
        id="value-voyage-lite",
        tier="value",
        label="Voyage 4 lite (1024-d)",
        description=(
            "All-paid value pick if Gemini's free-tier limits are a problem. "
            "Shares its key with the Voyage reranker. Needs a Voyage API key."
        ),
        provider="openai",
        model="voyage-4-lite",
        dimensions=1024,
        requires_key=True,
        key_vendor="voyage",
        base_url="https://api.voyageai.com/v1",
        input_type="voyage",
        key_label="Voyage API key",
    ),
    EmbedderOption(
        id="local-granite",
        tier="local",
        label="Granite small (384-d, on-device)",
        description=(
            "Free, fully private, runs on CPU with no API key. Best local pick in "
            "our eval. Needs the optional local-rag extra (sentence-transformers)."
        ),
        provider="local",
        model="ibm-granite/granite-embedding-small-english-r2",
        dimensions=384,
        requires_key=False,
        recommended=True,
    ),
]


# Rerankers, in display order. Voyage rerank-2.5 is the best single all-rounder;
# rerank-2.5-lite matches it at ~40% the price; zerank-2 wins code-heavy corpora;
# Ettin is the free on-device pick. "None" keeps rag_search vector-only (lowest
# latency), which is the default since a reranker trades latency for accuracy.
RERANKERS: list[RerankerOption] = [
    RerankerOption(
        id="none",
        tier="none",
        label="No reranker (vector-only)",
        description=(
            "Lowest latency. rag_search returns the fused vector/keyword results "
            "directly. A reranker improves accuracy but adds a little latency."
        ),
        provider="none",
        model=None,
        requires_key=False,
    ),
    RerankerOption(
        id="premium-voyage-2.5",
        tier="premium",
        label="Voyage rerank-2.5",
        description=(
            "Best single all-round reranker in our eval. Needs a Voyage API key "
            "(reused automatically if you chose the Voyage embedder)."
        ),
        provider="voyage",
        model="rerank-2.5",
        requires_key=True,
        key_vendor="voyage",
        recommended=True,
        key_label="Voyage API key",
    ),
    RerankerOption(
        id="value-voyage-2.5-lite",
        tier="value",
        label="Voyage rerank-2.5-lite",
        description=(
            "Matches the full rerank-2.5 at about 40% of the price: the best "
            "quality per dollar. Needs a Voyage API key (reused if available)."
        ),
        provider="voyage",
        model="rerank-2.5-lite",
        requires_key=True,
        key_vendor="voyage",
        recommended=True,
        key_label="Voyage API key",
    ),
    RerankerOption(
        id="value-zerank-2",
        tier="value",
        label="ZeroEntropy zerank-2 (code specialist)",
        description=(
            "Wins on code-heavy corpora in our eval; weaker on prose. Pick this if "
            "your assistant mostly works with code. Needs a ZeroEntropy API key."
        ),
        provider="zeroentropy",
        model="zerank-2",
        requires_key=True,
        key_vendor="zeroentropy",
        key_label="ZeroEntropy API key",
    ),
    RerankerOption(
        id="local-ettin",
        tier="local",
        label="Ettin cross-encoder (on-device)",
        description=(
            "Free, fully private, runs on CPU with no API key. Best local reranker "
            "in our eval. Needs the optional local-rag extra (sentence-transformers)."
        ),
        provider="local",
        model="cross-encoder/ettin-reranker-68m-v1",
        requires_key=False,
        recommended=True,
    ),
]

_EMBEDDERS_BY_ID = {opt.id: opt for opt in EMBEDDERS}
_RERANKERS_BY_ID = {opt.id: opt for opt in RERANKERS}

# The free, private default the quickstart equips silently.
QUICKSTART_EMBEDDER = "local-granite"
QUICKSTART_RERANKER = "local-ettin"


def get_embedder(option_id: Optional[str]) -> Optional[EmbedderOption]:
    return _EMBEDDERS_BY_ID.get(option_id) if option_id else None


def get_reranker(option_id: Optional[str]) -> Optional[RerankerOption]:
    return _RERANKERS_BY_ID.get(option_id) if option_id else None


def rag_env_for_state(state: "WizardState") -> dict[str, str]:
    """Translate the chosen embedder/reranker ids into backend env vars.

    Returns only the non-secret config (EMBEDDING_PROVIDER/MODEL/DIMENSIONS/
    BASE_URL/INPUT_TYPE and RAG_RERANK_*). The keys themselves live in
    ``state.optional_env`` and are written via OPTIONAL_ENV_ORDER. Empty when no
    embedder was chosen, so a skipped RAG step writes no RAG config (unchanged
    default behavior).
    """
    env: dict[str, str] = {}
    emb = get_embedder(state.embedder)
    if emb is not None:
        env["EMBEDDING_PROVIDER"] = emb.provider
        env["EMBEDDING_MODEL"] = emb.model
        env["EMBEDDING_DIMENSIONS"] = str(emb.dimensions)
        if emb.base_url:
            env["EMBEDDING_BASE_URL"] = emb.base_url
        if emb.input_type:
            env["EMBEDDING_INPUT_TYPE"] = emb.input_type
        # Only emit the retrieval mode when it differs from the hybrid default,
        # keeping config.env minimal.
        if getattr(state, "rag_retrieval_mode", "hybrid") == "vector":
            env["RAG_RETRIEVAL_MODE"] = "vector"
    rer = get_reranker(state.reranker)
    if rer is not None and rer.provider != "none":
        env["RAG_RERANK_ENABLED"] = "true"
        env["RAG_RERANK_PROVIDER"] = rer.provider
        if rer.model:
            env["RAG_RERANK_MODEL"] = rer.model
        if rer.onnx_file:
            env["RAG_RERANK_LOCAL_ONNX_FILE"] = rer.onnx_file
    elif rer is not None and rer.provider == "none":
        env["RAG_RERANK_ENABLED"] = "false"
    return env


def apply_quickstart_rag(state: "WizardState") -> None:
    """Equip the free, private local RAG stack (granite + Ettin) silently.

    For the quickstart/express path: the user gets working semantic memory
    without choosing an embedder or reranker or seeing what powers it. Clears any
    cloud keys so nothing paid is implied.
    """
    state.embedder = QUICKSTART_EMBEDDER
    state.reranker = QUICKSTART_RERANKER
    state.optional_env.pop("EMBEDDING_API_KEY", None)
    state.optional_env.pop("RAG_RERANK_API_KEY", None)


__all__ = [
    "TIER_LABELS",
    "EmbedderOption",
    "RerankerOption",
    "EMBEDDERS",
    "RERANKERS",
    "QUICKSTART_EMBEDDER",
    "QUICKSTART_RERANKER",
    "get_embedder",
    "get_reranker",
    "rag_env_for_state",
    "apply_quickstart_rag",
]
