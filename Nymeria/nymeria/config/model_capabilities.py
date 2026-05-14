"""Model capability registry for Nymeria.

Fetches model capabilities dynamically from OpenRouter API.
Falls back to static lists if API is unavailable.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
import time
from typing import Dict, List, Optional, Set

import httpx

logger = logging.getLogger(__name__)

TEXT_DOCUMENT_MIME_TYPES = {
    "text/plain",
    "text/markdown",
    "text/csv",
}

SUPPORTED_DOCUMENT_MIME_TYPES = TEXT_DOCUMENT_MIME_TYPES | {"application/pdf"}

EXTENSION_MIME_FALLBACKS = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


# ============================================================================
# Unified ModelInfo cache
# ============================================================================

@dataclass
class ModelInfo:
    """Unified model metadata from OpenRouter API."""

    id: str                                          # "anthropic/claude-sonnet-4"
    name: str = ""                                   # "Claude Sonnet 4"
    context_length: int = 0
    max_completion_tokens: Optional[int] = None
    input_modalities: Set[str] = field(default_factory=set)
    supported_parameters: Set[str] = field(default_factory=set)
    default_temperature: Optional[float] = None
    default_top_p: Optional[float] = None
    default_frequency_penalty: Optional[float] = None
    pricing_prompt: Optional[float] = None           # USD per token
    pricing_completion: Optional[float] = None       # USD per token
    tokenizer: Optional[str] = None                  # "Claude", "GPT", "Llama3"


# Single unified cache: model_id (lowercase) -> ModelInfo
_model_cache: Dict[str, ModelInfo] = {}
_live_model_cache: Dict[str, ModelInfo] = {}
_cache_timestamp: float = 0
_cache_populated: bool = False
_CACHE_TTL_SECONDS = 3600  # Refresh cache every hour

# Default context limits for common models (fallback when API unavailable)
DEFAULT_CONTEXT_LIMITS = {
    "anthropic/claude-3-opus": 200000,
    "anthropic/claude-3-sonnet": 200000,
    "anthropic/claude-3-haiku": 200000,
    "anthropic/claude-3.5-sonnet": 200000,
    "anthropic/claude-3.5-haiku": 200000,
    "anthropic/claude-sonnet-4": 200000,
    "anthropic/claude-opus-4": 200000,
    "anthropic/claude-opus-4-6": 1000000,
    "anthropic/claude-sonnet-4-6": 1000000,
    "claude-sonnet-4": 200000,
    "claude-opus-4": 200000,
    "claude-opus-4-6": 1000000,
    "claude-sonnet-4-6": 1000000,
    "openai/gpt-5.5": 1050000,
    "openai/gpt-5.5-pro": 1050000,
    "gpt-5.5": 1050000,
    "gpt-5.5-pro": 1050000,
    "gpt-5.5-2026-04-23": 1050000,
    "gpt-5.5-pro-2026-04-23": 1050000,
    "openai/gpt-5.4": 1050000,
    "openai/gpt-5.4-pro": 1050000,
    "gpt-5.4": 1050000,
    "gpt-5.4-pro": 1050000,
    "openai/gpt-5.4-mini": 400000,
    "openai/gpt-5.4-nano": 400000,
    "gpt-5.4-mini": 400000,
    "gpt-5.4-nano": 400000,
    "openai/gpt-5.3-codex": 400000,
    "openai/gpt-5.3-codex-spark": 128000,
    "gpt-5.3-codex": 400000,
    "gpt-5.3-codex-spark": 128000,
    "openai/gpt-5.2": 400000,
    "openai/gpt-5.2-codex": 400000,
    "gpt-5.2": 400000,
    "gpt-5.2-codex": 400000,
    "openai/gpt-5.1": 400000,
    "openai/gpt-5.1-codex": 400000,
    "openai/gpt-5.1-codex-mini": 400000,
    "openai/gpt-5.1-codex-max": 400000,
    "gpt-5.1": 400000,
    "gpt-5.1-codex": 400000,
    "gpt-5.1-codex-mini": 400000,
    "gpt-5.1-codex-max": 400000,
    "openai/gpt-5": 400000,
    "openai/gpt-5-mini": 400000,
    "openai/gpt-5-nano": 400000,
    "openai/gpt-5-codex": 400000,
    "openai/gpt-5-codex-mini": 400000,
    "gpt-5": 400000,
    "gpt-5-mini": 400000,
    "gpt-5-nano": 400000,
    "gpt-5-codex": 400000,
    "gpt-5-codex-mini": 400000,
    "openai/gpt-4o": 128000,
    "openai/gpt-4o-mini": 128000,
    "openai/gpt-4-turbo": 128000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
    "google/gemini-1.5-pro": 1000000,
    "google/gemini-1.5-flash": 1000000,
    "google/gemini-2.0-flash": 1000000,
    "google/gemini-2.5-pro": 1000000,
    "_default": 128000,
}

_OPENAI_SNAPSHOT_SUFFIX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_CAPABILITY_SNAPSHOT_SUFFIX_RE = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")
_REASONING_SUFFIX_RE = re.compile(r"\((?:none|minimal|low|medium|high|xhigh)\)$")


def _dedupe_preserving_order(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _looks_like_openai_model(model_id: str) -> bool:
    return model_id.startswith(("gpt-", "chatgpt-", "codex-")) or re.match(r"^o\d", model_id) is not None


def _strip_openai_snapshot_suffix(model_id: str) -> str:
    return _OPENAI_SNAPSHOT_SUFFIX_RE.sub("", model_id)


def _is_safe_cache_variant_match(requested: str, cached: str) -> bool:
    """Match OpenRouter-style variants without collapsing distinct model IDs.

    Variants such as ``:online`` share the same base model. Hyphenated names
    like ``gpt-5.4`` and ``gpt-5.4-mini`` are distinct models and must not be
    treated as prefix aliases.
    """
    return requested.startswith(f"{cached}:") or cached.startswith(f"{requested}:")


def _model_id_candidates(model_id: str) -> List[str]:
    """Return metadata lookup candidates for provider-qualified and bare IDs.

    CLIProxy's OpenAI-compatible endpoint exposes bare IDs such as ``gpt-5.5``,
    while OpenRouter metadata uses provider-qualified IDs like
    ``openai/gpt-5.5``. Generate both forms so runtime metadata remains useful
    even when the proxy's /v1/models response is intentionally minimal.
    """
    if not model_id:
        return []

    raw = _REASONING_SUFFIX_RE.sub("", model_id.strip().lower())
    if not raw:
        return []

    candidates = [raw]

    provider = ""
    bare = raw
    if "/" in raw:
        provider, bare = raw.split("/", 1)
        candidates.append(bare)

    stripped_raw = _strip_openai_snapshot_suffix(raw)
    stripped_bare = _strip_openai_snapshot_suffix(bare)
    candidates.extend([stripped_raw, stripped_bare])

    if _looks_like_openai_model(bare):
        candidates.append(f"openai/{bare}")
        candidates.append(f"openai/{stripped_bare}")
    elif provider == "openai" and _looks_like_openai_model(stripped_bare):
        candidates.append(f"openai/{stripped_bare}")

    return _dedupe_preserving_order(candidates)


def _without_provider_prefix(model_id: str) -> str:
    if "/" in model_id:
        return model_id.split("/", 1)[1]
    return model_id


def _capability_id_candidates(model_id: str) -> List[str]:
    """Return fallback capability candidates for provider-qualified and bare IDs."""
    candidates = _model_id_candidates(model_id)
    candidates.extend(_without_provider_prefix(candidate) for candidate in list(candidates))
    return _dedupe_preserving_order(candidates)


def _is_safe_capability_match(requested: str, known: str) -> bool:
    """Match exact IDs, OpenRouter variants, and immutable snapshots only."""
    if requested == known:
        return True

    if requested.startswith(f"{known}:"):
        return True

    if not requested.startswith(known):
        return False

    suffix = requested[len(known):]
    return bool(_CAPABILITY_SNAPSHOT_SUFFIX_RE.fullmatch(suffix))


def _safe_float(value, allow_zero: bool = False) -> Optional[float]:
    """Safely parse a pricing string or number to float."""
    if value is None:
        return None
    try:
        f = float(value)
        if allow_zero:
            return f if f >= 0 else None
        return f if f > 0 else None
    except (ValueError, TypeError):
        return None


def _fetch_openrouter_models() -> Dict[str, ModelInfo]:
    """Fetch model metadata from OpenRouter API and populate unified cache."""
    try:
        response = httpx.get(
            "https://openrouter.ai/api/v1/models",
            timeout=10.0
        )
        response.raise_for_status()
        data = response.json()

        cache: Dict[str, ModelInfo] = {}
        for model in data.get("data", []):
            model_id = model.get("id", "")
            if not model_id:
                continue

            model_key = model_id.lower()
            architecture = model.get("architecture", {})
            top_provider = model.get("top_provider", {})
            pricing = model.get("pricing", {})

            # Supported parameters
            supported_params = set()
            raw_params = model.get("supported_parameters") or []
            if isinstance(raw_params, list):
                supported_params = {str(p) for p in raw_params}

            # Default parameters
            default_params = model.get("default_parameters", {}) or {}

            # Max completion tokens
            max_completion = top_provider.get("max_completion_tokens")
            if not (max_completion and isinstance(max_completion, int)):
                max_completion = None

            info = ModelInfo(
                id=model_id,
                name=model.get("name", model_id),
                context_length=model.get("context_length", 0) or 0,
                max_completion_tokens=max_completion,
                input_modalities=set(architecture.get("input_modalities") or []),
                supported_parameters=supported_params,
                default_temperature=_safe_float(default_params.get("temperature"), allow_zero=True),
                default_top_p=_safe_float(default_params.get("top_p"), allow_zero=True),
                default_frequency_penalty=_safe_float(default_params.get("frequency_penalty"), allow_zero=True),
                pricing_prompt=_safe_float(pricing.get("prompt")),
                pricing_completion=_safe_float(pricing.get("completion")),
                tokenizer=architecture.get("tokenizer"),
            )

            cache[model_key] = info

        logger.info(f"Fetched metadata for {len(cache)} models from OpenRouter")
        return cache

    except Exception as e:
        logger.warning(f"Failed to fetch OpenRouter models: {e}")
        return {}


def _ensure_cache() -> Dict[str, ModelInfo]:
    """Ensure cache is populated and not stale."""
    global _model_cache, _cache_timestamp, _cache_populated

    now = time.time()
    if not _cache_populated or (now - _cache_timestamp) > _CACHE_TTL_SECONDS:
        result = _fetch_openrouter_models()
        if result:
            # Preserve live provider metadata registered from /models endpoints.
            # OpenRouter is a broad catalog; the active provider's own metadata
            # is usually more authoritative for the exact ID it returned.
            _model_cache = {**result, **_model_cache}
            _cache_timestamp = now
            _cache_populated = True
        elif not _cache_populated:
            # First fetch failed — allow retry on next call
            pass

    return _model_cache


def _lookup_model(model_id: str) -> Optional[ModelInfo]:
    """Look up a model by ID with prefix-match fallback."""
    if not model_id:
        return None

    candidates = _model_id_candidates(model_id)

    # Runtime /models responses from the selected provider are authoritative
    # for that exact provider, and this lookup avoids an OpenRouter fetch when
    # live metadata was just registered for context-window stats.
    for candidate in candidates:
        if candidate in _live_model_cache:
            return _live_model_cache[candidate]

    for cached_id, info in _live_model_cache.items():
        if any(_is_safe_cache_variant_match(candidate, cached_id) for candidate in candidates):
            return info

    cache = _ensure_cache()

    # Direct and provider-alias matches
    for candidate in candidates:
        if candidate in cache:
            return cache[candidate]

    # Prefix matching (e.g., "anthropic/claude-3-sonnet" matches "anthropic/claude-3-sonnet:beta")
    for cached_id, info in cache.items():
        if any(_is_safe_cache_variant_match(candidate, cached_id) for candidate in candidates):
            return info

    return None


def _check_modality(model_id: str, modality: str) -> Optional[bool]:
    """Check if a model supports a specific input modality."""
    if not model_id:
        return False

    info = _lookup_model(model_id)
    if info is None:
        return None  # Unknown model

    return modality in info.input_modalities


# ============================================================================
# Fallback static lists (used when API is unavailable)
# ============================================================================

ANTHROPIC_VISION_CAPABLE_MODELS = {
    "anthropic/claude-3-opus",
    "anthropic/claude-3-sonnet",
    "anthropic/claude-3-haiku",
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3.5-haiku",
    "anthropic/claude-3.7-sonnet",
    "anthropic/claude-haiku-4.5",
    "anthropic/claude-sonnet-4",
    "anthropic/claude-sonnet-4.5",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-opus-4",
    "anthropic/claude-opus-4.1",
    "anthropic/claude-opus-4.5",
    "anthropic/claude-opus-4.6",
    "anthropic/claude-opus-4.6-fast",
    "anthropic/claude-opus-4.7",
    "claude-3-opus",
    "claude-3-sonnet",
    "claude-3-haiku",
    "claude-3-5-sonnet",
    "claude-3-5-haiku",
    "claude-3-7-sonnet",
    "claude-haiku-4-5",
    "claude-sonnet-4",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4",
    "claude-opus-4-1",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
}

ANTHROPIC_DOCUMENT_CAPABLE_MODELS = {
    "anthropic/claude-3-opus",
    "anthropic/claude-3-sonnet",
    "anthropic/claude-3-haiku",
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3.5-haiku",
    "anthropic/claude-3.7-sonnet",
    "anthropic/claude-haiku-4.5",
    "anthropic/claude-sonnet-4",
    "anthropic/claude-sonnet-4.5",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-opus-4",
    "anthropic/claude-opus-4.1",
    "anthropic/claude-opus-4.5",
    "anthropic/claude-opus-4.6",
    "anthropic/claude-opus-4.7",
    "claude-3-opus",
    "claude-3-sonnet",
    "claude-3-haiku",
    "claude-3-5-sonnet",
    "claude-3-5-haiku",
    "claude-3-7-sonnet",
    "claude-haiku-4-5",
    "claude-sonnet-4",
    "claude-sonnet-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4",
    "claude-opus-4-1",
    "claude-opus-4-5",
    "claude-opus-4-6",
    "claude-opus-4-7",
}

OPENAI_VISION_CAPABLE_MODELS = {
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "openai/gpt-4-turbo",
    "openai/gpt-4.1",
    "openai/gpt-4.1-mini",
    "openai/gpt-4.1-nano",
    "openai/gpt-5",
    "openai/gpt-5-chat",
    "openai/gpt-5-chat-latest",
    "openai/gpt-5-mini",
    "openai/gpt-5-nano",
    "openai/gpt-5-pro",
    "openai/gpt-5-codex",
    "openai/gpt-5-codex-mini",
    "openai/gpt-5.1",
    "openai/gpt-5.1-chat",
    "openai/gpt-5.1-codex",
    "openai/gpt-5.1-codex-max",
    "openai/gpt-5.1-codex-mini",
    "openai/gpt-5.2",
    "openai/gpt-5.2-chat",
    "openai/gpt-5.2-codex",
    "openai/gpt-5.2-pro",
    "openai/gpt-5.3-chat",
    "openai/gpt-5.3-codex",
    "openai/gpt-5.4",
    "openai/gpt-5.4-mini",
    "openai/gpt-5.4-nano",
    "openai/gpt-5.4-pro",
    "openai/gpt-5.5",
    "openai/gpt-5.5-pro",
}

OPENAI_DOCUMENT_CAPABLE_MODELS = {
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "openai/gpt-4.1",
    "openai/gpt-4.1-mini",
    "openai/gpt-4.1-nano",
    "openai/gpt-5",
    "openai/gpt-5-chat",
    "openai/gpt-5-chat-latest",
    "openai/gpt-5-mini",
    "openai/gpt-5-nano",
    "openai/gpt-5-pro",
    "openai/gpt-5.1",
    "openai/gpt-5.1-chat",
    "openai/gpt-5.2",
    "openai/gpt-5.2-chat",
    "openai/gpt-5.2-pro",
    "openai/gpt-5.3-chat",
    "openai/gpt-5.3-codex",
    "openai/gpt-5.4",
    "openai/gpt-5.4-mini",
    "openai/gpt-5.4-nano",
    "openai/gpt-5.4-pro",
    "openai/gpt-5.5",
    "openai/gpt-5.5-pro",
}

GEMINI_VISION_CAPABLE_MODELS = {
    "google/gemini-1.5-pro",
    "google/gemini-1.5-flash",
    "google/gemini-2.0-flash",
    "google/gemini-2.5-pro",
    "google/gemini-2.5-pro-preview",
    "google/gemini-2.5-pro-preview-05-06",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-flash-preview",
    "google/gemini-2.5-flash-lite",
    "google/gemini-2.5-flash-lite-preview-09-2025",
    "google/gemini-2.5-flash-image",
    "google/gemini-3-pro-preview",
    "google/gemini-3-pro-image-preview",
    "google/gemini-3-flash-preview",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.1-pro-preview-customtools",
    "google/gemini-3.1-flash-lite-preview",
    "google/gemini-3.1-flash-image-preview",
}

GEMINI_DOCUMENT_CAPABLE_MODELS = {
    "google/gemini-1.5-pro",
    "google/gemini-1.5-flash",
    "google/gemini-2.0-flash",
    "google/gemini-2.5-pro",
    "google/gemini-2.5-pro-preview",
    "google/gemini-2.5-pro-preview-05-06",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-flash-preview",
    "google/gemini-2.5-flash-lite",
    "google/gemini-2.5-flash-lite-preview-09-2025",
    "google/gemini-3-pro-preview",
    "google/gemini-3-flash-preview",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.1-pro-preview-customtools",
    "google/gemini-3.1-flash-lite-preview",
}

VISION_CAPABLE_MODELS = (
    ANTHROPIC_VISION_CAPABLE_MODELS
    | OPENAI_VISION_CAPABLE_MODELS
    | GEMINI_VISION_CAPABLE_MODELS
    | {
        "meta-llama/llama-3.2-11b-vision-instruct",
        "meta-llama/llama-3.2-90b-vision-instruct",
    }
)

DOCUMENT_CAPABLE_MODELS = (
    ANTHROPIC_DOCUMENT_CAPABLE_MODELS
    | OPENAI_DOCUMENT_CAPABLE_MODELS
    | GEMINI_DOCUMENT_CAPABLE_MODELS
)


def _fallback_check(model_id: str, model_set: Set[str]) -> bool:
    """Fallback capability matching against static model sets."""
    if not model_id:
        return False

    requested_candidates = _capability_id_candidates(model_id)

    for known_model in model_set:
        known_lower = known_model.lower()
        known_candidates = _capability_id_candidates(known_lower)
        if any(
            _is_safe_capability_match(requested, known)
            for requested in requested_candidates
            for known in known_candidates
        ):
            return True

    return False


# ============================================================================
# Public API — existing functions (same signatures, same behavior)
# ============================================================================

def supports_vision(model_id: str) -> bool:
    """Check if a model supports vision/image input."""
    result = _check_modality(model_id, "image")
    if result is not None:
        return result
    return _fallback_check(model_id, VISION_CAPABLE_MODELS)


def supports_documents(model_id: str) -> bool:
    """Check if a model supports document/file input (PDFs, text files, etc.)."""
    result = _check_modality(model_id, "file")
    if result is not None:
        return result
    return _fallback_check(model_id, DOCUMENT_CAPABLE_MODELS)


def get_model_modalities(model_id: str) -> Set[str]:
    """Get all input modalities supported by a model."""
    if not model_id:
        return set()

    info = _lookup_model(model_id)
    if info is not None:
        return info.input_modalities.copy()

    return set()


def refresh_capabilities_cache() -> int:
    """Force refresh the capabilities cache."""
    global _model_cache, _cache_timestamp, _cache_populated

    _model_cache = _fetch_openrouter_models()
    _cache_timestamp = time.time()
    _cache_populated = True

    return len(_model_cache)


def register_model_metadata(
    *,
    model_id: str,
    name: str = "",
    context_length: int | None = None,
    max_completion_tokens: int | None = None,
    input_modalities: Set[str] | None = None,
    supported_parameters: Set[str] | None = None,
    default_temperature: float | None = None,
    default_top_p: float | None = None,
    default_frequency_penalty: float | None = None,
    pricing_prompt: float | None = None,
    pricing_completion: float | None = None,
    tokenizer: str | None = None,
) -> None:
    """Merge provider-returned model metadata into the runtime cache.

    OpenAI-compatible ``/models`` endpoints do not have one universal schema,
    but several providers expose context length and output limits there. Cache
    those hints when seen so context-window percentages use provider metadata
    instead of only OpenRouter's catalog or static fallbacks.
    """
    if not model_id:
        return

    key = model_id.lower()
    existing = _model_cache.get(key)
    info = ModelInfo(
        id=model_id,
        name=name or (existing.name if existing else model_id),
        context_length=(
            context_length
            if context_length and context_length > 0
            else (existing.context_length if existing else 0)
        ),
        max_completion_tokens=(
            max_completion_tokens
            if max_completion_tokens and max_completion_tokens > 0
            else (existing.max_completion_tokens if existing else None)
        ),
        input_modalities=input_modalities
        if input_modalities is not None
        else (existing.input_modalities.copy() if existing else set()),
        supported_parameters=supported_parameters
        if supported_parameters is not None
        else (existing.supported_parameters.copy() if existing else set()),
        default_temperature=(
            default_temperature
            if default_temperature is not None
            else (existing.default_temperature if existing else None)
        ),
        default_top_p=(
            default_top_p
            if default_top_p is not None
            else (existing.default_top_p if existing else None)
        ),
        default_frequency_penalty=(
            default_frequency_penalty
            if default_frequency_penalty is not None
            else (existing.default_frequency_penalty if existing else None)
        ),
        pricing_prompt=(
            pricing_prompt
            if pricing_prompt is not None
            else (existing.pricing_prompt if existing else None)
        ),
        pricing_completion=(
            pricing_completion
            if pricing_completion is not None
            else (existing.pricing_completion if existing else None)
        ),
        tokenizer=tokenizer or (existing.tokenizer if existing else None),
    )
    _live_model_cache[key] = info
    _model_cache[key] = info


def get_context_limit(model_id: str) -> int:
    """Get context window size (in tokens) for a model."""
    if not model_id:
        return DEFAULT_CONTEXT_LIMITS["_default"]

    info = _lookup_model(model_id)
    if info is not None and info.context_length > 0:
        return info.context_length

    # Fallback to static defaults
    candidates = _model_id_candidates(model_id)
    for candidate in candidates:
        limit = DEFAULT_CONTEXT_LIMITS.get(candidate)
        if limit:
            return limit

    model_lower = model_id.lower()
    for known_model, limit in sorted(DEFAULT_CONTEXT_LIMITS.items(), key=lambda item: len(item[0]), reverse=True):
        if known_model == "_default":
            continue
        if known_model.lower() in model_lower or model_lower in known_model.lower():
            return limit

    return DEFAULT_CONTEXT_LIMITS["_default"]


def get_max_output_tokens(model_id: str) -> Optional[int]:
    """Get the maximum output token limit for a model."""
    if not model_id:
        return None

    info = _lookup_model(model_id)
    if info is None or info.max_completion_tokens is None:
        return None

    raw_max_output = info.max_completion_tokens

    # Safety cap: never exceed 50% of context window.
    context_limit = get_context_limit(model_id)
    safety_cap = context_limit // 2
    if raw_max_output > safety_cap:
        logger.info(
            f"Capping max_output_tokens for {model_id}: "
            f"{raw_max_output} -> {safety_cap} (50% of {context_limit} context)"
        )
        return safety_cap

    return raw_max_output


# ============================================================================
# New public API
# ============================================================================

def get_supported_parameters(model_id: str) -> Set[str]:
    """Get the set of API parameters a model supports (e.g. 'tools', 'reasoning', 'temperature')."""
    info = _lookup_model(model_id)
    if info is not None:
        return info.supported_parameters.copy()
    return set()


def get_model_defaults(model_id: str) -> Dict[str, float]:
    """Get model-specific default parameter values (temperature, top_p, frequency_penalty)."""
    info = _lookup_model(model_id)
    if info is None:
        return {}

    defaults: Dict[str, float] = {}
    if info.default_temperature is not None:
        defaults["temperature"] = info.default_temperature
    if info.default_top_p is not None:
        defaults["top_p"] = info.default_top_p
    if info.default_frequency_penalty is not None:
        defaults["frequency_penalty"] = info.default_frequency_penalty
    return defaults


def get_model_info(model_id: str) -> Optional[ModelInfo]:
    """Get full model metadata. Returns None if model is unknown."""
    return _lookup_model(model_id)


def list_all_models() -> List[ModelInfo]:
    """Return all cached model metadata entries."""
    cache = _ensure_cache()
    return list(cache.values())


def get_cache_timestamp() -> float:
    """Return the timestamp of the last cache refresh (0 if never fetched)."""
    return _cache_timestamp


# ============================================================================
# Attachment utilities (unchanged)
# ============================================================================

def infer_mime_type(mime_type: str, file_name: str = "") -> str:
    """Infer a stable MIME type from explicit MIME and optional filename."""
    mime = (mime_type or "").strip().lower()

    if mime and mime not in {"application/octet-stream", "binary/octet-stream"}:
        return mime

    suffix = Path(file_name or "").suffix.lower()
    return EXTENSION_MIME_FALLBACKS.get(suffix, mime)


def normalize_attachment_file_type(file_type: str, mime_type: str, file_name: str = "") -> str:
    """Normalize attachment file type to 'image' | 'document' | 'unknown'."""
    normalized = (file_type or "").strip().lower()
    inferred_mime = infer_mime_type(mime_type, file_name)

    if normalized in {"image", "document"}:
        return normalized
    if inferred_mime.startswith("image/"):
        return "image"
    if inferred_mime in SUPPORTED_DOCUMENT_MIME_TYPES:
        return "document"
    return "unknown"


def evaluate_attachment_compatibility(
    model_id: str,
    provider: str,
    attachments: Optional[List[Dict[str, str]]],
) -> Dict[str, object]:
    """Evaluate whether attachments are likely compatible with a model."""
    normalized_provider = (provider or "").strip().lower()
    modalities = get_model_modalities(model_id) if normalized_provider == "openrouter" else set()

    required_modalities: Set[str] = set()
    unsupported_modalities: Set[str] = set()
    warnings: List[str] = []
    has_pdf = False

    for attachment in attachments or []:
        mime_type = infer_mime_type(
            attachment.get("mime_type", ""),
            attachment.get("file_name", ""),
        )
        file_type = normalize_attachment_file_type(
            attachment.get("file_type", ""),
            mime_type,
            attachment.get("file_name", ""),
        )

        if file_type == "image":
            required_modalities.add("image")
            continue

        if file_type == "document":
            if mime_type == "application/pdf":
                has_pdf = True
                required_modalities.add("file")
                continue

            if mime_type in TEXT_DOCUMENT_MIME_TYPES:
                required_modalities.add("text")
                continue

            unsupported_modalities.add("document")
            warnings.append(
                f"Unsupported document type '{mime_type or 'unknown'}'. "
                "Supported documents are PDF, TXT, MD, and CSV."
            )
            continue

        unsupported_modalities.add("document")
        warnings.append("Unsupported attachment type. Supported types are image and document.")

    if normalized_provider == "openrouter":
        if "image" in required_modalities:
            if modalities:
                if "image" not in modalities:
                    unsupported_modalities.add("image")
            elif not supports_vision(model_id):
                unsupported_modalities.add("image")

        # OpenRouter can parse PDFs even for models without native file input.
        if has_pdf and modalities and "file" not in modalities:
            warnings.append(
                "This model does not report native file input. OpenRouter may parse PDFs before sending text to the model."
            )
    else:
        if "image" in required_modalities and not supports_vision(model_id):
            unsupported_modalities.add("image")

        if has_pdf and not supports_documents(model_id):
            unsupported_modalities.add("file")

    if "text" in required_modalities and modalities and "text" not in modalities:
        unsupported_modalities.add("text")

    return {
        "compatible": len(unsupported_modalities) == 0,
        "model_input_modalities": sorted(modalities),
        "required_modalities": sorted(required_modalities),
        "unsupported_modalities": sorted(unsupported_modalities),
        "warnings": warnings,
    }
