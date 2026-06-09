"""Catalog of RAG embedder and reranker options for first-run setup.

TUI-free (like ``setup/providers.py``) so the interactive wizard, the headless
finalize path, and the quickstart helper share one source of truth. The
premium / value / local recommendations and the per-option ``metrics`` come from
an internal retrieval-quality eval on a blended 40k-chunk agentic corpus (see
``docs/private/rag/rag-eval-notes.md``; the public summary is in the local-LLM
doc). Metrics are internal avg nDCG@5 across the tool/prose/code layers under a
frozen, set-wise LLM-graded qrels; treat them as relative, corpus-specific
rankings, not absolute scores. Benchmark leaderboard order frequently did NOT
transfer to this corpus, so the picks here are what won OUR eval.

Only providers the backend can actually drive are listed: embedders are
``openai`` / ``cohere`` / ``gemini`` / ``local`` (``memory_index.py``); rerankers
are ``voyage`` / ``cohere`` / ``zeroentropy`` / ``local`` (``rag_quality.py``).
Jina, for instance, is eval-tested but has no production endpoint, so it is not
offered.

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
    metrics: str = ""  # one-line internal-eval summary, shown under the description


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
    metrics: str = ""  # one-line internal-eval summary, shown under the description


# Embedders, in display order (premium, value, local). Vector-only quality on the
# eval corpus ranks Cohere > Gemini (free) > Voyage-large > Voyage-lite ~ OpenAI
# ~ granite; a reranker narrows the gap (it erases the embedder gap on prose), so
# the value and local tiers stay strong picks. Each provider lists its fuller
# lineup so a user can match an account they already hold; the recommended pick
# per tier carries the (recommended) tag.
EMBEDDERS: list[EmbedderOption] = [
    EmbedderOption(
        id="premium-cohere",
        tier="premium",
        label="Cohere embed-v4 (1024-d)",
        description=(
            "Best overall in our eval, with the biggest lead on structured, code, "
            "and tool content. Run at 1024-d (Matryoshka): same quality as full "
            "1536-d for less storage. Needs a Cohere API key (paid)."
        ),
        provider="cohere",
        model="embed-v4.0",
        dimensions=1024,
        requires_key=True,
        key_vendor="cohere",
        recommended=True,
        key_label="Cohere API key",
        metrics="Internal eval avg nDCG@5 0.71 (tool 0.66 / prose 0.63 / code 0.84): #1 of every embedder tested.",
    ),
    EmbedderOption(
        id="premium-cohere-1536",
        tier="premium",
        label="Cohere embed-v4 (1536-d, full)",
        description=(
            "The same embed-v4 model at full 1536-d width. No measured quality gain "
            "over the 1024-d pick and ~50% more vector storage, so prefer 1024-d "
            "unless you specifically want full dimensions. Needs a Cohere API key."
        ),
        provider="cohere",
        model="embed-v4.0",
        dimensions=1536,
        requires_key=True,
        key_vendor="cohere",
        key_label="Cohere API key",
        metrics="Internal eval avg nDCG@5 0.70: statistically tied with 1024-d, which edges it on tool and prose.",
    ),
    EmbedderOption(
        id="premium-voyage-large",
        tier="premium",
        label="Voyage 4 large (1024-d)",
        description=(
            "Strong all-round premium embedder, a notch below Cohere on every "
            "layer. Its edge: one Voyage key also powers the Voyage rerankers, so "
            "embedding and reranking share a single account. Needs a Voyage API key."
        ),
        provider="openai",
        model="voyage-4-large",
        dimensions=1024,
        requires_key=True,
        key_vendor="voyage",
        base_url="https://api.voyageai.com/v1",
        input_type="voyage",
        key_label="Voyage API key",
        metrics="Internal eval avg nDCG@5 0.67 (tool 0.59 / prose 0.61 / code 0.81): premium #2, just behind Cohere.",
    ),
    EmbedderOption(
        id="value-gemini",
        tier="value",
        label="Gemini embedding-001 (free tier)",
        description=(
            "Best quality per dollar: #2 embedder overall and free on Google's "
            "free tier, beating paid Voyage on tool and prose. Mind the free-tier "
            "rate/daily caps. Needs a Google AI (Gemini) API key."
        ),
        provider="gemini",
        model="gemini-embedding-001",
        dimensions=1024,
        requires_key=True,
        key_vendor="gemini",
        recommended=True,
        key_label="Google AI (Gemini) API key",
        metrics="Internal eval avg nDCG@5 0.68 (tool 0.59 / prose 0.61 / code 0.82): #2 overall, free.",
    ),
    EmbedderOption(
        id="value-voyage-lite",
        tier="value",
        label="Voyage 4 lite (1024-d)",
        description=(
            "The cheapest paid Voyage embedder. An all-paid value option if "
            "Gemini's free-tier caps are a problem, but Gemini beats it on both "
            "price and quality. Shares its key with the Voyage rerankers."
        ),
        provider="openai",
        model="voyage-4-lite",
        dimensions=1024,
        requires_key=True,
        key_vendor="voyage",
        base_url="https://api.voyageai.com/v1",
        input_type="voyage",
        key_label="Voyage API key",
        metrics="Internal eval avg nDCG@5 0.64: the weakest API embedder tested; prefer Gemini unless free-tier caps bite.",
    ),
    EmbedderOption(
        id="value-openai-small",
        tier="value",
        label="OpenAI text-embedding-3-small (1536-d)",
        description=(
            "The familiar OpenAI default, offered because many users already have "
            "the key, but a weak baseline on this agentic corpus: the free local "
            "Granite model beats it on prose and code. Prefer Gemini or Cohere for "
            "quality. Needs an OpenAI API key."
        ),
        provider="openai",
        model="text-embedding-3-small",
        dimensions=1536,
        requires_key=True,
        key_vendor="openai",
        key_label="OpenAI API key",
        metrics="Internal eval avg nDCG@5 0.60 (tool 0.55 / prose 0.51 / code 0.74): below the free local Granite.",
    ),
    EmbedderOption(
        id="local-granite",
        tier="local",
        label="Granite small (384-d, on-device)",
        description=(
            "Free, fully private, runs on CPU with no API key. Best local pick in "
            "our eval and competitive on code; 8192-token context, Apache-2.0. "
            "Needs the optional local-rag extra (sentence-transformers)."
        ),
        provider="local",
        model="ibm-granite/granite-embedding-small-english-r2",
        dimensions=384,
        requires_key=False,
        recommended=True,
        metrics="Internal eval avg nDCG@5 0.61 (code 0.79, within 0.03 of premium): best on-device embedder.",
    ),
]


# Rerankers, in display order (none, premium, value, local). Voyage rerank-2.5 is
# the best single all-rounder and owns prose; rerank-2.5-lite matches it at ~40%
# the price; Cohere rerank-v4.0-pro edges on code and shares a Cohere key; zerank-2
# is a code specialist; the Ettin cross-encoders are the free on-device picks (68m
# best, 32m faster). "None" keeps rag_search vector-only (lowest latency), the
# default since a reranker trades latency for accuracy. A reranker erases the
# embedder gap, so it matters most on a cheap or local first stage.
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
            "Best single all-round reranker in our eval, with the biggest lift on "
            "prose. Needs a Voyage API key (reused automatically if you chose a "
            "Voyage embedder)."
        ),
        provider="voyage",
        model="rerank-2.5",
        requires_key=True,
        key_vendor="voyage",
        recommended=True,
        key_label="Voyage API key",
        metrics="Internal eval avg nDCG@5 0.77 on the premium pool (tool 0.73 / prose 0.72 / code 0.86): best all-rounder.",
    ),
    RerankerOption(
        id="premium-cohere-pro",
        tier="premium",
        label="Cohere rerank-v4.0-pro",
        description=(
            "Premium reranker that edges Voyage on code but trails it on prose. "
            "Best paired with the Cohere embedder, whose key it reuses. Needs a "
            "Cohere API key."
        ),
        provider="cohere",
        model="rerank-v4.0-pro",
        requires_key=True,
        key_vendor="cohere",
        key_label="Cohere API key",
        metrics="Internal eval avg nDCG@5 0.76 on the premium pool; best on code (0.87), behind Voyage on prose.",
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
        metrics="Internal eval: within ~0.01 nDCG@5 of the full rerank-2.5, at ~40% of the cost.",
    ),
    RerankerOption(
        id="value-zerank-2",
        tier="value",
        label="ZeroEntropy zerank-2 (code specialist)",
        description=(
            "A code specialist, not a general pick: it tops code in our eval but is "
            "last/near-last on tool and weakest on prose at equal depth. Choose it "
            "only if your assistant mostly works with code. Needs a ZeroEntropy key."
        ),
        provider="zeroentropy",
        model="zerank-2",
        requires_key=True,
        key_vendor="zeroentropy",
        key_label="ZeroEntropy API key",
        metrics="Internal eval: best on CODE (~0.87, top of all rerankers) but worst on prose; code-only winner.",
    ),
    RerankerOption(
        id="local-ettin",
        tier="local",
        label="Ettin 68m cross-encoder (on-device)",
        description=(
            "Free, fully private, runs on CPU with no API key. Best on-device "
            "reranker in our eval; biggest help on cheap/local embeddings. Needs "
            "the optional local-rag extra (sentence-transformers)."
        ),
        provider="local",
        model="cross-encoder/ettin-reranker-68m-v1",
        requires_key=False,
        recommended=True,
        metrics="Internal eval: +0.07 to +0.10 nDCG@5 on local/cheap embeddings, small lift on premium; ~3-5s/query CPU.",
    ),
    RerankerOption(
        id="local-ettin-32m",
        tier="local",
        label="Ettin 32m cross-encoder (on-device, faster)",
        description=(
            "Lighter on-device reranker: ~2.7x faster than the 68m on CPU for a "
            "small quality drop. Pick it when rerank latency is tight. Needs the "
            "optional local-rag extra."
        ),
        provider="local",
        model="cross-encoder/ettin-reranker-32m-v1",
        requires_key=False,
        metrics="Internal eval: ~0.035 nDCG@5 below the 68m but ~2.7x faster on CPU.",
    ),
]


# Recommended embedder + reranker pairings from the eval, by tier. Surfaced in the
# wizard so the intended combos are obvious even though each option is picked
# separately.
RECOMMENDED_COMBOS: list[tuple[str, str, str, str]] = [
    (
        "Premium",
        "premium-cohere",
        "premium-voyage-2.5",
        "SOTA quality (internal eval avg nDCG@5 0.77)",
    ),
    (
        "Value",
        "value-gemini",
        "value-voyage-2.5-lite",
        "~98% of premium at a near-free embedder + a cheap reranker",
    ),
    (
        "Local",
        "local-granite",
        "local-ettin",
        "free, fully private, on-device",
    ),
]

# The reranker recommended to pair with each embedder, plus a one-line rationale.
_RECOMMENDED_RERANKER: dict[str, tuple[str, str]] = {
    "premium-cohere": (
        "premium-voyage-2.5",
        "SOTA pair; for code-heavy work swap to Cohere rerank-v4.0-pro (shares your Cohere key).",
    ),
    "premium-cohere-1536": (
        "premium-voyage-2.5",
        "SOTA pair; code-heavy work can swap to Cohere rerank-v4.0-pro.",
    ),
    "premium-voyage-large": (
        "premium-voyage-2.5",
        "One Voyage key powers embedding and reranking; code-heavy work can use zerank-2.",
    ),
    "value-gemini": (
        "value-voyage-2.5-lite",
        "Value pair: about 98% of premium quality.",
    ),
    "value-voyage-lite": (
        "value-voyage-2.5-lite",
        "All-paid value pair; one Voyage key for both.",
    ),
    "value-openai-small": (
        "value-voyage-2.5-lite",
        "A reranker erases most of the embedder gap, so this lifts a weak first stage.",
    ),
    "local-granite": (
        "local-ettin",
        "Free, fully private, on-device pair.",
    ),
}

_EMBEDDERS_BY_ID = {opt.id: opt for opt in EMBEDDERS}
_RERANKERS_BY_ID = {opt.id: opt for opt in RERANKERS}

# The free, private default the quickstart equips silently.
QUICKSTART_EMBEDDER = "local-granite"
QUICKSTART_RERANKER = "local-ettin"


def get_embedder(option_id: Optional[str]) -> Optional[EmbedderOption]:
    return _EMBEDDERS_BY_ID.get(option_id) if option_id else None


def get_reranker(option_id: Optional[str]) -> Optional[RerankerOption]:
    return _RERANKERS_BY_ID.get(option_id) if option_id else None


def recommended_reranker_for(
    embedder_id: Optional[str],
) -> Optional[tuple[str, str]]:
    """Return ``(reranker_id, rationale)`` recommended for this embedder, or None.

    Falls back to the local pair for any unmapped local embedder so the reranker
    step always has a recommendation to show.
    """
    if not embedder_id:
        return None
    rec = _RECOMMENDED_RERANKER.get(embedder_id)
    if rec is not None:
        return rec
    emb = get_embedder(embedder_id)
    if emb is not None and emb.tier == "local":
        return ("local-ettin", "Free, fully private, on-device pair.")
    return None


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


def _truthy_env(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def embedder_id_for_env(
    provider: Optional[str], model: Optional[str], dimensions: object = None
) -> Optional[str]:
    """Reverse of ``rag_env_for_state``'s embedder half: recover the catalog id.

    Keyed on ``(provider, model, dimensions)`` because some options share a
    provider or a model and differ only on dimensions (e.g. the Cohere 1024/1536
    pair, or the Voyage lineup that all run over an OpenAI-compatible endpoint).
    Falls back to a ``(provider, model)`` match when dimensions is missing or
    unparseable. Returns None for an unknown combo (a hand-edited or newer
    config), leaving the embedder unset rather than guessing.
    """
    if not provider or not model:
        return None
    try:
        dims: Optional[int] = (
            int(str(dimensions)) if dimensions is not None and str(dimensions) != "" else None
        )
    except (TypeError, ValueError):
        dims = None
    if dims is not None:
        for opt in EMBEDDERS:
            if opt.provider == provider and opt.model == model and opt.dimensions == dims:
                return opt.id
    for opt in EMBEDDERS:
        if opt.provider == provider and opt.model == model:
            return opt.id
    return None


def reranker_id_for_env(
    provider: Optional[str], model: Optional[str], enabled: object = None
) -> Optional[str]:
    """Reverse of ``rag_env_for_state``'s reranker half: recover the catalog id.

    A falsey/absent ``RAG_RERANK_ENABLED`` maps to the explicit "none" option.
    Otherwise matches ``(provider, model)`` (model may be absent for providers
    that omit it), loosening to provider-only if the model names a variant not in
    the catalog. Returns None for an unknown provider.
    """
    if not _truthy_env(enabled):
        for opt in RERANKERS:
            if opt.provider == "none":
                return opt.id
        return None
    if not provider:
        return None
    for opt in RERANKERS:
        if opt.provider == provider and (opt.model or None) == (model or None):
            return opt.id
    for opt in RERANKERS:
        if opt.provider == provider:
            return opt.id
    return None


def apply_quickstart_rag(state: "WizardState") -> None:
    """Equip the free, private local RAG stack (granite + Ettin) silently.

    For the skip / quickstart / express path: the user gets working semantic
    memory without choosing an embedder or reranker or seeing what powers it.
    Clears any cloud keys so nothing paid is implied, and marks the state so the
    reranker step is skipped too (a RAG skip skips both screens). The local stack
    needs the optional local-rag extra to produce vectors; without it hybrid
    search still serves BM25, and the vec0 width is already sized for granite so
    installing the extra later lights up vectors with no reconfigure.
    """
    state.embedder = QUICKSTART_EMBEDDER
    state.reranker = QUICKSTART_RERANKER
    state.rag_quickstarted = True
    state.optional_env.pop("EMBEDDING_API_KEY", None)
    state.optional_env.pop("RAG_RERANK_API_KEY", None)


__all__ = [
    "TIER_LABELS",
    "EmbedderOption",
    "RerankerOption",
    "EMBEDDERS",
    "RERANKERS",
    "RECOMMENDED_COMBOS",
    "QUICKSTART_EMBEDDER",
    "QUICKSTART_RERANKER",
    "get_embedder",
    "get_reranker",
    "recommended_reranker_for",
    "rag_env_for_state",
    "embedder_id_for_env",
    "reranker_id_for_env",
    "apply_quickstart_rag",
]
