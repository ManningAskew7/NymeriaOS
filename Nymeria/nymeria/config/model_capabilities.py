"""Model capability registry for Nymeria.

Fetches model capabilities dynamically from OpenRouter API.
Falls back to static lists if API is unavailable.
"""

import logging
import time
from typing import Dict, List, Optional, Set

import httpx

logger = logging.getLogger(__name__)

# Cache for model capabilities fetched from OpenRouter
_capabilities_cache: Optional[Dict[str, Set[str]]] = None
_context_limits_cache: Dict[str, int] = {}
_max_output_cache: Dict[str, int] = {}
_cache_timestamp: float = 0
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
    "claude-sonnet-4": 200000,
    "claude-opus-4": 200000,
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


def _fetch_openrouter_models() -> Dict[str, Set[str]]:
    """
    Fetch model capabilities from OpenRouter API.

    Also populates _context_limits_cache with context_length and
    _max_output_cache with max_completion_tokens for each model.

    Returns:
        Dict mapping model_id to set of input modalities (e.g., {"image", "file", "text"})
    """
    global _context_limits_cache, _max_output_cache
    try:
        response = httpx.get(
            "https://openrouter.ai/api/v1/models",
            timeout=10.0
        )
        response.raise_for_status()
        data = response.json()

        capabilities = {}
        for model in data.get("data", []):
            model_id = model.get("id", "")
            architecture = model.get("architecture", {})
            input_modalities = architecture.get("input_modalities", [])

            if model_id:
                model_key = model_id.lower()

                # Extract context length
                context_length = model.get("context_length", 0)
                if context_length:
                    _context_limits_cache[model_key] = context_length

                # Extract max output tokens from top_provider
                top_provider = model.get("top_provider", {})
                max_completion = top_provider.get("max_completion_tokens")
                if max_completion and isinstance(max_completion, int):
                    _max_output_cache[model_key] = max_completion

                # Extract modalities
                if input_modalities:
                    capabilities[model_key] = set(input_modalities)

        logger.info(f"Fetched capabilities for {len(capabilities)} models from OpenRouter")
        logger.info(f"Cached context limits for {len(_context_limits_cache)} models")
        return capabilities

    except Exception as e:
        logger.warning(f"Failed to fetch OpenRouter models: {e}")
        return {}


def _get_capabilities_cache() -> Dict[str, Set[str]]:
    """Get cached capabilities, refreshing if stale."""
    global _capabilities_cache, _cache_timestamp

    now = time.time()
    if _capabilities_cache is None or (now - _cache_timestamp) > _CACHE_TTL_SECONDS:
        _capabilities_cache = _fetch_openrouter_models()
        _cache_timestamp = now

    return _capabilities_cache or {}


def _check_modality(model_id: str, modality: str) -> Optional[bool]:
    """
    Check if a model supports a specific input modality using OpenRouter API.

    Args:
        model_id: The model identifier
        modality: The modality to check ("image", "file", "audio", etc.)

    Returns:
        True if supported, False if not supported, None if unknown (not in cache)
    """
    if not model_id:
        return False

    cache = _get_capabilities_cache()
    model_lower = model_id.lower()

    # Direct match
    if model_lower in cache:
        return modality in cache[model_lower]

    # Try prefix matching (e.g., "anthropic/claude-3-sonnet" matches "anthropic/claude-3-sonnet:beta")
    for cached_id, modalities in cache.items():
        if model_lower.startswith(cached_id) or cached_id.startswith(model_lower):
            return modality in modalities

    return None  # Unknown model


# ============================================================================
# Fallback static lists (used when API is unavailable)
# ============================================================================

VISION_CAPABLE_MODELS = {
    # Anthropic Claude 3+ models
    "anthropic/claude-3-opus",
    "anthropic/claude-3-sonnet",
    "anthropic/claude-3-haiku",
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3.5-haiku",
    "anthropic/claude-sonnet-4",
    "anthropic/claude-opus-4",
    # OpenAI GPT-4 Vision models
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "openai/gpt-4-turbo",
    # Google Gemini models
    "google/gemini-1.5-pro",
    "google/gemini-1.5-flash",
    "google/gemini-2.0-flash",
    "google/gemini-2.5-pro",
    # Meta Llama models with vision
    "meta-llama/llama-3.2-11b-vision-instruct",
    "meta-llama/llama-3.2-90b-vision-instruct",
}

DOCUMENT_CAPABLE_MODELS = {
    # Anthropic Claude 3+ models - all support PDFs natively
    "anthropic/claude-3-opus",
    "anthropic/claude-3-sonnet",
    "anthropic/claude-3-haiku",
    "anthropic/claude-3.5-sonnet",
    "anthropic/claude-3.5-haiku",
    "anthropic/claude-sonnet-4",
    "anthropic/claude-opus-4",
    # Google Gemini models - support documents
    "google/gemini-1.5-pro",
    "google/gemini-1.5-flash",
    "google/gemini-2.0-flash",
    "google/gemini-2.5-pro",
}


def _fallback_check(model_id: str, model_set: Set[str]) -> bool:
    """Fallback substring matching against static model sets."""
    if not model_id:
        return False

    model_lower = model_id.lower()

    for known_model in model_set:
        if known_model.lower() in model_lower or model_lower in known_model.lower():
            return True

    return False


# ============================================================================
# Public API
# ============================================================================

def supports_vision(model_id: str) -> bool:
    """
    Check if a model supports vision/image input.

    First tries OpenRouter API, falls back to static list.

    Args:
        model_id: The model identifier (e.g., "anthropic/claude-3-sonnet")

    Returns:
        True if the model supports vision, False otherwise
    """
    # Try dynamic check first
    result = _check_modality(model_id, "image")
    if result is not None:
        return result

    # Fallback to static list
    return _fallback_check(model_id, VISION_CAPABLE_MODELS)


def supports_documents(model_id: str) -> bool:
    """
    Check if a model supports document/file input (PDFs, text files, etc.).

    First tries OpenRouter API, falls back to static list.

    Args:
        model_id: The model identifier (e.g., "anthropic/claude-3-sonnet")

    Returns:
        True if the model supports documents, False otherwise
    """
    # Try dynamic check first
    result = _check_modality(model_id, "file")
    if result is not None:
        return result

    # Fallback to static list
    return _fallback_check(model_id, DOCUMENT_CAPABLE_MODELS)


def get_model_modalities(model_id: str) -> Set[str]:
    """
    Get all input modalities supported by a model.

    Args:
        model_id: The model identifier

    Returns:
        Set of modality strings (e.g., {"text", "image", "file"})
    """
    if not model_id:
        return set()

    cache = _get_capabilities_cache()
    model_lower = model_id.lower()

    # Direct match
    if model_lower in cache:
        return cache[model_lower].copy()

    # Prefix matching
    for cached_id, modalities in cache.items():
        if model_lower.startswith(cached_id) or cached_id.startswith(model_lower):
            return modalities.copy()

    # Unknown - return empty set
    return set()


def refresh_capabilities_cache() -> int:
    """
    Force refresh the capabilities cache.

    Returns:
        Number of models in the refreshed cache
    """
    global _capabilities_cache, _cache_timestamp

    _capabilities_cache = _fetch_openrouter_models()
    _cache_timestamp = time.time()

    return len(_capabilities_cache)


def get_context_limit(model_id: str) -> int:
    """
    Get context window size (in tokens) for a model.

    First tries the cache populated from OpenRouter API, then falls back
    to DEFAULT_CONTEXT_LIMITS for known models.

    Args:
        model_id: The model identifier (e.g., "anthropic/claude-sonnet-4" or "claude-sonnet-4")

    Returns:
        Context window size in tokens
    """
    if not model_id:
        return DEFAULT_CONTEXT_LIMITS["_default"]

    # Ensure cache is populated
    _get_capabilities_cache()

    model_lower = model_id.lower()

    # Try exact match in dynamic cache first
    if model_lower in _context_limits_cache:
        return _context_limits_cache[model_lower]

    # Try prefix matching in dynamic cache
    for cached_id, limit in _context_limits_cache.items():
        if model_lower.startswith(cached_id) or cached_id.startswith(model_lower):
            return limit

    # Fallback to static defaults
    for known_model, limit in DEFAULT_CONTEXT_LIMITS.items():
        if known_model == "_default":
            continue
        # Flexible matching: either direction
        if known_model.lower() in model_lower or model_lower in known_model.lower():
            return limit

    return DEFAULT_CONTEXT_LIMITS["_default"]


def get_max_output_tokens(model_id: str) -> Optional[int]:
    """
    Get the maximum output token limit for a model.

    Uses ``top_provider.max_completion_tokens`` from the OpenRouter API.
    Returns ``None`` if the model is unknown or has no reported limit.

    Args:
        model_id: The model identifier (e.g., "anthropic/claude-sonnet-4")

    Returns:
        Max output tokens, or None if unknown
    """
    if not model_id:
        return None

    # Ensure cache is populated
    _get_capabilities_cache()

    model_lower = model_id.lower()

    # Exact match
    if model_lower in _max_output_cache:
        return _max_output_cache[model_lower]

    # Prefix matching
    for cached_id, limit in _max_output_cache.items():
        if model_lower.startswith(cached_id) or cached_id.startswith(model_lower):
            return limit

    return None
