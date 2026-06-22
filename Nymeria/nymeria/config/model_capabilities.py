"""Model capability registry for Nymeria.

Fetches model capabilities dynamically from OpenRouter API.
Falls back to static lists if API is unavailable.
"""

import logging
import math
import re
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Set, TypedDict

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
    # Provider-published effort ladder (Anthropic /v1/models capabilities tree).
    # None means the provider reported nothing; the static family table applies.
    reasoning_efforts: Optional[tuple] = None
    default_temperature: Optional[float] = None
    default_top_p: Optional[float] = None
    default_frequency_penalty: Optional[float] = None
    pricing_prompt: Optional[float] = None           # USD per token
    pricing_completion: Optional[float] = None       # USD per token
    tokenizer: Optional[str] = None                  # "Claude", "GPT", "Llama3"
    # Provider-published attachment limits. None when the provider does not
    # report a per-model number; callers should fall back to the family table.
    max_images_per_request: Optional[int] = None
    max_image_bytes: Optional[int] = None
    max_pdf_pages: Optional[int] = None
    max_total_attachment_bytes: Optional[int] = None


# Single unified cache: model_id (lowercase) -> ModelInfo
_model_cache: Dict[str, ModelInfo] = {}
_live_model_cache: Dict[str, ModelInfo] = {}
_cache_timestamp: float = 0
_cache_populated: bool = False
_cache_failure_timestamp: float = 0
_CACHE_TTL_SECONDS = 3600  # Refresh cache every hour
_CACHE_FAILURE_TTL_SECONDS = 60  # Avoid repeated sync fetches during outages
_cache_lock = threading.Lock()
_cache_refresh_lock = threading.Lock()

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
    "anthropic/claude-opus-4-7": 1000000,
    "anthropic/claude-opus-4-8": 1000000,
    "anthropic/claude-sonnet-4-6": 1000000,
    "claude-sonnet-4": 200000,
    "claude-opus-4": 200000,
    "claude-opus-4-6": 1000000,
    "claude-opus-4-7": 1000000,
    "claude-opus-4-8": 1000000,
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

# Length-desc ordering of the (non-default) entries for the substring fallback
# in get_context_limit. Precomputed once at import: the sort key never changes,
# so re-sorting on every cache-miss call is pure repeated work. Stable sort keeps
# the dict insertion order among equal-length keys, matching the prior inline sort.
_DEFAULT_CONTEXT_LIMITS_BY_LEN = sorted(
    ((name, limit) for name, limit in DEFAULT_CONTEXT_LIMITS.items() if name != "_default"),
    key=lambda item: len(item[0]),
    reverse=True,
)

_OPENAI_SNAPSHOT_SUFFIX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_CAPABILITY_SNAPSHOT_SUFFIX_RE = re.compile(r"-(?:\d{8}|\d{4}-\d{2}-\d{2})$")
_REASONING_SUFFIX_RE = re.compile(r"\((?:none|minimal|low|medium|high|xhigh|max|off)\)$")


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


# ============================================================================
# Per-family attachment-limit table
# ============================================================================
#
# Neither Anthropic's nor OpenAI's /v1/models endpoint reports per-model
# attachment count / size caps, so the cap table is hardcoded by family.
# Sources (research brief 2026-05-20):
#   Anthropic: platform.claude.com/docs/en/build-with-claude/vision +
#     /pdf-support. 100 images/request on 200K-context models, 5 MB inline
#     per image, 32 MB total request, 100 PDF pages on 200K context.
#   OpenAI: developers.openai.com/api/docs/guides/images-vision +
#     /pdf-files. ~1500 images/request, 512 MB total, 50 MB per file input.
#   Gemini: ai.google.dev/gemini-api. 3000 files/request, 100 MB inline,
#     1000 pages per PDF.
# Family matching is substring-based against the lowercased model id; the
# first matching family wins, so list more specific patterns first.

_DEFAULT_ATTACHMENT_LIMITS: Dict[str, Optional[int]] = {
    "max_images_per_request": 16,
    "max_image_bytes": 5 * 1024 * 1024,
    "max_pdf_pages": 100,
    "max_total_bytes": 32 * 1024 * 1024,
}

_ATTACHMENT_LIMITS_BY_FAMILY: List[tuple[tuple[str, ...], Dict[str, Optional[int]]]] = [
    # Modern Anthropic 4.x models with the 200K-context profile.
    (
        (
            "claude-opus-4-7", "claude-opus-4.7",
            "claude-sonnet-4-6", "claude-sonnet-4.6",
            "claude-haiku-4-5", "claude-haiku-4.5",
            "claude-opus-4-6", "claude-opus-4.6",
            "claude-opus-4-5", "claude-opus-4.5",
            "claude-sonnet-4-5", "claude-sonnet-4.5",
        ),
        {
            "max_images_per_request": 100,
            "max_image_bytes": 5 * 1024 * 1024,
            "max_pdf_pages": 100,
            "max_total_bytes": 32 * 1024 * 1024,
        },
    ),
    # Generic Claude fallback (legacy 3.x, etc.).
    (
        ("claude-", "anthropic/"),
        {
            "max_images_per_request": 100,
            "max_image_bytes": 5 * 1024 * 1024,
            "max_pdf_pages": 100,
            "max_total_bytes": 32 * 1024 * 1024,
        },
    ),
    # OpenAI GPT-5 / 4.x / codex family.
    (
        ("gpt-5", "gpt-4o", "gpt-4.1", "chatgpt-", "codex-", "openai/"),
        {
            "max_images_per_request": 1500,
            "max_image_bytes": 20 * 1024 * 1024,
            # OpenAI does not publish a per-PDF page cap; per-file 50 MB cap
            # is enforced via max_image_bytes for file_input on Responses.
            "max_pdf_pages": None,
            "max_total_bytes": 512 * 1024 * 1024,
        },
    ),
    # Google Gemini.
    (
        ("gemini-", "google/"),
        {
            "max_images_per_request": 3000,
            "max_image_bytes": 20 * 1024 * 1024,
            "max_pdf_pages": 1000,
            "max_total_bytes": 100 * 1024 * 1024,
        },
    ),
]


def _resolve_attachment_limits(model_id: str) -> Dict[str, Optional[int]]:
    """Pattern-match a model id against the per-family attachment-limit table."""
    if not model_id:
        return dict(_DEFAULT_ATTACHMENT_LIMITS)
    lowered = model_id.lower()
    for patterns, limits in _ATTACHMENT_LIMITS_BY_FAMILY:
        if any(p in lowered for p in patterns):
            return dict(limits)
    return dict(_DEFAULT_ATTACHMENT_LIMITS)


# ============================================================================
# Reasoning-effort capability table
# ============================================================================
#
# Nymeria's reasoning-effort axis is a six-step scale. "off" means thinking is
# explicitly disabled (and wins over extended_thinking=True); None stays
# "unset/inherit" and never reaches these helpers. Per-model support is static
# family knowledge (research brief 2026-06): provider /models endpoints do not
# report effort ladders, so the table is hardcoded like the attachment limits
# above. Family matching is substring/pattern based on the lowercased model id
# with any CLIProxy effort suffix (e.g. "(xhigh)") stripped first.

EFFORT_LEVELS: tuple = ("off", "low", "medium", "high", "xhigh", "max")

_EFFORT_RANK: Dict[str, int] = {level: rank for rank, level in enumerate(EFFORT_LEVELS)}

# Unknown model/provider: assume the common ladder but nothing above "high".
_DEFAULT_REASONING_EFFORTS: tuple = ("off", "low", "medium", "high")

_OPENAI_GPT5_MINOR_RE = re.compile(r"gpt-5\.(\d+)")
_OPENAI_O_SERIES_RE = re.compile(r"^o\d")
_GROK_4_MINOR_RE = re.compile(r"grok-4(?:\.(\d+))?")


_ANTHROPIC_MODEL_VERSION_RE = re.compile(
    r"claude-(?:opus|sonnet|haiku)-(\d+)(?:[-.](\d+))?"
)


def anthropic_model_version(model_text: str) -> Optional[tuple]:
    """Parse (major, minor) from a Claude model id, hyphen or dot separated.

    Matches the modern family-first naming only (claude-opus-4-8,
    claude-sonnet-4.6, claude-opus-5); legacy version-first ids
    (claude-3-5-sonnet) return None and take the legacy budget path. A
    missing minor parses as 0 so future major bumps land on the newest API
    shape instead of the legacy one.
    """
    match = _ANTHROPIC_MODEL_VERSION_RE.search(model_text or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2) or 0))


def _anthropic_reasoning_efforts(model_text: str) -> tuple:
    """Effort ladder for Claude models (direct Anthropic API shapes).

    Version-ordinal rules so newly released models land on the right shape
    without a table edit; exact ladders come from the live /v1/models
    capabilities tree when available (see _live_reasoning_efforts).
    """
    if "fable" in model_text or "mythos" in model_text:
        # Thinking cannot be disabled on these models; no "off" tier.
        return ("low", "medium", "high", "xhigh", "max")
    version = anthropic_model_version(model_text)
    if version is not None and version >= (4, 7):
        return EFFORT_LEVELS
    if version == (4, 6):
        # 4.6 adaptive supports max but has no xhigh tier.
        return ("off", "low", "medium", "high", "max")
    # Legacy budget-token path (4.5 and older): every tier maps to a budget.
    return EFFORT_LEVELS


def _openai_reasoning_efforts(model_text: str) -> tuple:
    """Effort ladder for OpenAI models (Responses/Chat Completions)."""
    minor_match = _OPENAI_GPT5_MINOR_RE.search(model_text)
    minor = int(minor_match.group(1)) if minor_match else None
    if "codex" in model_text:
        # Codex models cannot disable reasoning. xhigh ships on codex-max and
        # the gpt-5.2+ codex lineage (model pages list it for gpt-5.2-codex,
        # gpt-5.3-codex, gpt-5.5-codex); the mini/spark variants are not
        # documented with xhigh.
        has_xhigh = "codex-max" in model_text or (
            minor is not None
            and minor >= 2
            and "mini" not in model_text
            and "spark" not in model_text
        )
        if has_xhigh:
            return ("low", "medium", "high", "xhigh")
        return ("low", "medium", "high")
    if _OPENAI_O_SERIES_RE.match(model_text):
        # o-series accepts low/medium/high only. Omitting the reasoning
        # parameter lets the model reason at its default (medium), which
        # contradicts "off", so "off" is not in the ladder and the clamp
        # maps it to "low".
        return ("low", "medium", "high")
    if "gpt-5" in model_text and "-pro" in model_text:
        # Pro models reject the lower tiers outright. gpt-5-pro accepts only
        # "high"; the gpt-5.2-pro page lists medium/high/xhigh and later pro
        # releases follow that shape. None of them can disable reasoning.
        if minor is not None and minor >= 2:
            return ("medium", "high", "xhigh")
        return ("high",)
    if minor is not None:
        if minor >= 2:
            return ("off", "low", "medium", "high", "xhigh")
        return ("off", "low", "medium", "high")
    if "gpt-5" in model_text:
        # gpt-5 base lineage: "off" maps to effort "minimal" on the wire.
        return ("off", "low", "medium", "high")
    return _DEFAULT_REASONING_EFFORTS


def _google_reasoning_efforts(model_text: str) -> tuple:
    """Effort ladder for Gemini models."""
    if "gemini-3" in model_text or "gemini-4" in model_text:
        # thinking_level tops out at high; "off" maps to "minimal" (cannot
        # fully disable thinking on 3.x+).
        return ("off", "low", "medium", "high")
    # Gemini <= 2.5 thinking_budget: every tier maps to a budget value.
    return EFFORT_LEVELS


def _xai_reasoning_efforts(model_text: str) -> tuple:
    """Effort ladder for xAI Grok models."""
    minor_match = _GROK_4_MINOR_RE.search(model_text)
    if minor_match:
        minor = int(minor_match.group(1)) if minor_match.group(1) else 0
        if minor < 2:
            # grok-4 / grok-4-fast / grok-4.1-fast 400 on any reasoning_effort
            # value; the only honest tier is "off" (param omitted entirely).
            return ("off",)
    return ("off", "low", "medium", "high")


# OpenRouter's unified reasoning config normalizes effort across models and
# accepts up to xhigh; "off" sends effort "none" so thinking is actively
# disabled rather than left at the upstream default.
_OPENROUTER_UNIFIED_LADDER: tuple = ("off", "low", "medium", "high", "xhigh")

# Partners whose wire carries only an on/off thinking toggle. "medium" is the
# single honest "on" tier (mirrors OpenRouter's enabled=true meaning medium).
_PARTNER_BINARY_THINKING: tuple = ("off", "medium")

# DeepSeek's V4 thinking contract: effort high|max (low/medium coerce to high,
# xhigh to max upstream); thinking.type=disabled turns it off.
_DEEPSEEK_V4_LADDER: tuple = ("off", "high", "max")


def _partner_wire_reasoning_efforts(
    provider_text: str,
    bare_model: str,
    provider_route: Optional[str] = None,
) -> Optional[tuple]:
    """Effort ladders for OpenAI-compatible partner providers (June 2026).

    These reflect what each partner's request builder can actually express on
    the wire, not the hosted model's family: advertising a tier the request
    cannot transmit would make the clamp and every "effective" display lie.
    Returns None for providers whose ladder is decided elsewhere.
    """
    if provider_text in {"vercel", "aihubmix"}:
        # Both speak the OpenRouter-style unified reasoning config.
        return _OPENROUTER_UNIFIED_LADDER
    if provider_text in {"fireworks-ai", "firepass"}:
        # reasoning_effort accepts none/low/medium/high/xhigh/max.
        return EFFORT_LEVELS
    if provider_text == "deepseek":
        return _DEEPSEEK_V4_LADDER
    if provider_text in {
        "alibaba",
        "alibaba-cn",
        "alibaba-coding-plan",
        "alibaba-coding-plan-cn",
        "qwen-oauth",
    }:
        # enable_thinking is a boolean; no level control on the compat path.
        return _PARTNER_BINARY_THINKING
    if provider_text in {"moonshotai", "moonshotai-cn"}:
        # thinking.type enabled|disabled only.
        return _PARTNER_BINARY_THINKING
    if provider_text == "togetherai":
        if "deepseek-v4" in bare_model:
            return _DEEPSEEK_V4_LADDER
        # reasoning.enabled boolean for everything else.
        return _PARTNER_BINARY_THINKING
    if provider_text == "novita-ai":
        # enable_thinking boolean.
        return _PARTNER_BINARY_THINKING
    if provider_text == "baseten":
        # Reasoning cannot be disabled on Baseten's thinking-default models;
        # reasoning_effort low/medium/high, plus xhigh on DeepSeek V4 Pro.
        if "deepseek-v4" in bare_model:
            return ("low", "medium", "high", "xhigh")
        return ("low", "medium", "high")
    if provider_text == "litellm":
        if provider_route == "anthropic_messages":
            # Native Claude shapes flow through _create_anthropic_llm; the
            # family ladder applies.
            return None
        # Unified reasoning_effort none|low|medium|high passthrough.
        return ("off", "low", "medium", "high")
    return None


def _live_reasoning_efforts(model_text: str) -> Optional[tuple]:
    """Peek provider-published effort ladders from the live runtime cache.

    Read-only: never triggers a network refresh, so the clamp hot path and
    offline tests stay deterministic. Populated today by the Anthropic
    /v1/models fetcher (capabilities.effort tree).
    """
    for candidate in _model_id_candidates(model_text):
        info = _live_model_cache.get(candidate)
        if info is not None and info.reasoning_efforts:
            return info.reasoning_efforts
    return None


def _cached_openrouter_reasoning_support(model_text: str) -> Optional[bool]:
    """Peek whether OpenRouter metadata says a model takes reasoning config.

    Returns None when no metadata is cached (never fetches). False means the
    catalog positively lists supported_parameters without "reasoning".
    """
    for cache in (_live_model_cache, _model_cache):
        for candidate in _model_id_candidates(model_text):
            info = cache.get(candidate)
            if info is not None and info.supported_parameters:
                return "reasoning" in info.supported_parameters
    return None


def supported_reasoning_efforts(
    provider: str,
    model: str,
    provider_route: Optional[str] = None,
) -> tuple:
    """Return the effort levels a provider/model pair supports, rank-ordered.

    Resolution order: OpenRouter unified ladder (catalog-aware), gpt-oss
    floor, partner wire ladders, live provider-published ladders (Anthropic
    capabilities tree), then static family-pattern knowledge. Unknown models
    fall back to the conservative ladder ("off", "low", "medium", "high").
    Never triggers a network fetch.
    """
    provider_text = (provider or "").strip().lower()
    model_text = _REASONING_SUFFIX_RE.sub("", (model or "").strip().lower())
    bare_model = _without_provider_prefix(model_text)

    if provider_text == "openrouter":
        if _cached_openrouter_reasoning_support(model_text) is False:
            # Catalog says this model takes no reasoning config at all.
            return ("off",)
        return _OPENROUTER_UNIFIED_LADDER
    if "gpt-oss" in bare_model:
        # gpt-oss (Groq, Ollama, Fireworks, etc.) cannot disable reasoning.
        return ("low", "medium", "high")
    partner = _partner_wire_reasoning_efforts(provider_text, bare_model, provider_route)
    if partner is not None:
        return partner
    if (
        provider_text == "anthropic"
        or "claude" in bare_model
        or "fable" in bare_model
        or "mythos" in bare_model
    ):
        live = _live_reasoning_efforts(model_text)
        if live:
            return live
        return _anthropic_reasoning_efforts(bare_model)
    if provider_text == "xai" or "grok" in bare_model:
        return _xai_reasoning_efforts(bare_model)
    if provider_text == "google" or "gemini" in bare_model:
        return _google_reasoning_efforts(bare_model)
    if provider_text == "openai" or _looks_like_openai_model(bare_model):
        return _openai_reasoning_efforts(bare_model)
    return _DEFAULT_REASONING_EFFORTS


def max_reasoning_effort(
    provider: str,
    model: str,
    provider_route: Optional[str] = None,
) -> str:
    """Return the highest-ranked effort level the provider/model supports."""
    supported = supported_reasoning_efforts(provider, model, provider_route)
    return max(supported, key=lambda level: _EFFORT_RANK[level])


def clamp_reasoning_effort(
    provider: str,
    model: str,
    effort: str,
    provider_route: Optional[str] = None,
) -> str:
    """Clamp a requested effort level onto the provider/model's ladder.

    Rules (in order):
    - Supported as requested: returned unchanged.
    - Requested at or above the model's highest tier: the model's highest tier
      (an over-ask means "most thinking available", e.g. xhigh on an Anthropic
      4.6 model clamps UP to max).
    - Unsupported mid-ladder or below-floor (e.g. "off" on a model that cannot
      disable thinking): the next supported tier above the request.

    Unknown tokens are returned unchanged; the provider factories keep their
    defensive ``.get(effort, default)`` fallbacks.
    """
    requested = str(effort or "").strip().lower()
    if requested not in _EFFORT_RANK:
        return requested

    supported = supported_reasoning_efforts(provider, model, provider_route)
    if requested in supported:
        return requested

    requested_rank = _EFFORT_RANK[requested]
    supported_ranks = sorted(_EFFORT_RANK[level] for level in supported)
    if requested_rank >= supported_ranks[-1]:
        return EFFORT_LEVELS[supported_ranks[-1]]
    for rank in supported_ranks:
        if rank > requested_rank:
            return EFFORT_LEVELS[rank]
    return EFFORT_LEVELS[supported_ranks[0]]


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


def _fetch_anthropic_models(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, ModelInfo]:
    """Fetch model metadata from Anthropic /v1/models (or CLIProxy passthrough).

    Anthropic's response includes ``capabilities.image_input.supported`` and
    ``capabilities.pdf_input.supported`` per model, which lets the agent decide
    natively whether a Claude model accepts attachments rather than relying on
    the static fallback table. The endpoint requires an API key; if the env
    doesn't have one, the call is a no-op.
    """
    import os as _os

    if api_key is None:
        api_key = _os.environ.get("ANTHROPIC_API_KEY", "")
    if base_url is None:
        configured_base = _os.environ.get("LLM_BASE_URL", "").strip()
        configured_provider = _os.environ.get("LLM_PROVIDER", "").strip().lower()
        if configured_provider == "anthropic" and configured_base:
            base_url = configured_base.rstrip("/")
        else:
            base_url = "https://api.anthropic.com"

    if not api_key:
        logger.debug("Skipping Anthropic /v1/models fetch — no api key configured")
        return {}

    url = f"{base_url}/v1/models"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    try:
        response = httpx.get(url, headers=headers, timeout=10.0)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.warning(f"Failed to fetch Anthropic models from {url}: {e}")
        return {}

    cache: Dict[str, ModelInfo] = {}
    for model in data.get("data", []):
        model_id = model.get("id") or ""
        if not model_id:
            continue

        capabilities_obj = model.get("capabilities") or {}
        input_modalities: Set[str] = {"text"}
        image_reported = "image_input" in capabilities_obj
        pdf_reported = "pdf_input" in capabilities_obj
        if (capabilities_obj.get("image_input") or {}).get("supported"):
            input_modalities.add("image")
        if (capabilities_obj.get("pdf_input") or {}).get("supported"):
            input_modalities.add("file")
        # Some Anthropic-compatible gateways (notably CLIProxy) return /v1/models
        # without a capabilities block. Do NOT assert text-only in that case: it
        # would mark every vision-capable Claude model as non-vision in the live
        # cache (which is authoritative over the static fallback) and silently
        # disable all image input. Defer to the static capability sets for any
        # modality the provider did not explicitly report.
        if not image_reported and _fallback_check(model_id, VISION_CAPABLE_MODELS):
            input_modalities.add("image")
        if not pdf_reported and _fallback_check(model_id, DOCUMENT_CAPABLE_MODELS):
            input_modalities.add("file")
        reasoning_efforts = parse_anthropic_reasoning_capabilities(capabilities_obj)

        context_length = int(model.get("max_input_tokens") or 0)
        max_output = model.get("max_output_tokens")
        max_completion_tokens = (
            int(max_output) if isinstance(max_output, (int, float)) and max_output > 0 else None
        )

        family_limits = _resolve_attachment_limits(model_id)

        info = ModelInfo(
            id=model_id,
            name=model.get("display_name") or model.get("name") or model_id,
            context_length=context_length,
            max_completion_tokens=max_completion_tokens,
            input_modalities=input_modalities,
            reasoning_efforts=reasoning_efforts,
            max_images_per_request=family_limits.get("max_images_per_request"),
            max_image_bytes=family_limits.get("max_image_bytes"),
            max_pdf_pages=family_limits.get("max_pdf_pages"),
            max_total_attachment_bytes=family_limits.get("max_total_bytes"),
        )
        cache[model_id.lower()] = info

    logger.info(f"Fetched metadata for {len(cache)} models from Anthropic")
    return cache


def parse_anthropic_reasoning_capabilities(capabilities: Any) -> Optional[tuple]:
    """Extract a rank-ordered effort ladder from an Anthropic capabilities tree.

    Anthropic's /v1/models response carries per-effort-level support booleans
    (``capabilities.effort.<level>.supported``) plus thinking-type flags
    (``capabilities.thinking.types.<type>.supported``). "off" is derived from
    the ``disabled`` thinking type since it never appears in the effort tree.
    Returns None when the response has no effort tree (older proxies, missing
    capabilities) so the static family table applies.
    """
    if not isinstance(capabilities, dict):
        return None
    effort_tree = capabilities.get("effort")
    if not isinstance(effort_tree, dict) or not effort_tree:
        return None
    levels = {
        level
        for level in EFFORT_LEVELS
        if isinstance(effort_tree.get(level), dict)
        and effort_tree[level].get("supported")
    }
    if not levels:
        return None
    thinking_types = (capabilities.get("thinking") or {}).get("types")
    if isinstance(thinking_types, dict) and (
        (thinking_types.get("disabled") or {}).get("supported")
    ):
        levels.add("off")
    return tuple(level for level in EFFORT_LEVELS if level in levels)


def refresh_anthropic_models(
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> int:
    """Fetch Anthropic /v1/models and merge into the live cache.

    Intended to be called once at agent startup when ``LLM_PROVIDER=anthropic``
    (direct or via CLIProxy). Safe to call repeatedly. Returns the number of
    models registered. The merge writes to the runtime live cache so a later
    OpenRouter refresh does not overwrite the Anthropic capability bits.
    """
    fetched = _fetch_anthropic_models(base_url=base_url, api_key=api_key)
    if not fetched:
        return 0
    with _cache_lock:
        for key, info in fetched.items():
            _live_model_cache[key] = info
            # Seed _model_cache too so prefix lookups during the next
            # OpenRouter refresh cycle still see Anthropic data.
            _model_cache.setdefault(key, info)
    return len(fetched)


def _ensure_cache() -> Dict[str, ModelInfo]:
    """Ensure cache is populated and not stale."""
    global _model_cache, _cache_timestamp, _cache_populated, _cache_failure_timestamp

    now = time.time()
    with _cache_lock:
        if _cache_populated and (now - _cache_timestamp) <= _CACHE_TTL_SECONDS:
            return _model_cache
        if (
            _cache_failure_timestamp
            and (now - _cache_failure_timestamp) <= _CACHE_FAILURE_TTL_SECONDS
        ):
            return _model_cache

    with _cache_refresh_lock:
        now = time.time()
        with _cache_lock:
            if _cache_populated and (now - _cache_timestamp) <= _CACHE_TTL_SECONDS:
                return _model_cache
            if (
                _cache_failure_timestamp
                and (now - _cache_failure_timestamp) <= _CACHE_FAILURE_TTL_SECONDS
            ):
                return _model_cache

        result = _fetch_openrouter_models()
        with _cache_lock:
            if result:
                _model_cache = {**result, **_model_cache}
                _cache_timestamp = time.time()
                _cache_populated = True
                _cache_failure_timestamp = 0
            else:
                _cache_failure_timestamp = time.time()

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
    "anthropic/claude-opus-4.8",
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
    "claude-opus-4-8",
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
    "anthropic/claude-opus-4.8",
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
    "claude-opus-4-8",
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
    global _model_cache, _cache_timestamp, _cache_populated, _cache_failure_timestamp

    result = _fetch_openrouter_models()
    with _cache_lock:
        _model_cache = result
        _cache_timestamp = time.time()
        _cache_populated = True
        _cache_failure_timestamp = 0 if result else _cache_timestamp

    return len(result)


def register_model_metadata(
    *,
    model_id: str,
    name: str = "",
    context_length: int | None = None,
    max_completion_tokens: int | None = None,
    input_modalities: Set[str] | None = None,
    supported_parameters: Set[str] | None = None,
    reasoning_efforts: tuple | None = None,
    default_temperature: float | None = None,
    default_top_p: float | None = None,
    default_frequency_penalty: float | None = None,
    pricing_prompt: float | None = None,
    pricing_completion: float | None = None,
    tokenizer: str | None = None,
    max_images_per_request: int | None = None,
    max_image_bytes: int | None = None,
    max_pdf_pages: int | None = None,
    max_total_attachment_bytes: int | None = None,
) -> None:
    """Merge provider-returned model metadata into the runtime cache.

    OpenAI-compatible ``/models`` endpoints do not have one universal schema,
    but several providers expose context length and output limits there. Cache
    those hints when seen so context-window percentages use provider metadata
    instead of only OpenRouter's catalog or static fallbacks.
    """
    if not model_id:
        return

    # Per-field "provided" filter: an entry mapped to None here means the caller
    # did not supply a meaningful value, so the existing cached value (or the
    # dataclass default) survives the merge. Numeric limits treat <= 0 as
    # "not provided"; name/tokenizer treat "" as "not provided". Anything left in
    # ``updates`` overrides the base via ``dataclasses.replace``, so adding a new
    # ModelInfo field only needs one row here (no risk of forgetting the
    # existing-value fallback, the silent data-loss trap of the old hand merge).
    provided: dict[str, Any] = {
        "name": name or None,
        "context_length": context_length if (context_length and context_length > 0) else None,
        "max_completion_tokens": (
            max_completion_tokens if (max_completion_tokens and max_completion_tokens > 0) else None
        ),
        "input_modalities": input_modalities,
        "supported_parameters": supported_parameters,
        "reasoning_efforts": reasoning_efforts,
        "default_temperature": default_temperature,
        "default_top_p": default_top_p,
        "default_frequency_penalty": default_frequency_penalty,
        "pricing_prompt": pricing_prompt,
        "pricing_completion": pricing_completion,
        "tokenizer": tokenizer or None,
        "max_images_per_request": max_images_per_request,
        "max_image_bytes": max_image_bytes,
        "max_pdf_pages": max_pdf_pages,
        "max_total_attachment_bytes": max_total_attachment_bytes,
    }
    updates = {field_name: value for field_name, value in provided.items() if value is not None}

    key = model_id.lower()
    with _cache_lock:
        # A fresh entry defaults name to model_id (ModelInfo's own default is "").
        base = _model_cache.get(key) or ModelInfo(id=model_id, name=model_id)
        # Always own independent copies of the mutable set fields so a later
        # mutation of the cached entry never aliases a prior cache generation or
        # the caller's set.
        info = replace(
            base,
            id=model_id,
            input_modalities=set(updates.pop("input_modalities", base.input_modalities)),
            supported_parameters=set(updates.pop("supported_parameters", base.supported_parameters)),
            **updates,
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
    for known_model, limit in _DEFAULT_CONTEXT_LIMITS_BY_LEN:
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
    merged = {**cache, **_live_model_cache}
    return list(merged.values())


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


class AttachmentCompatibilityReport(TypedDict):
    compatible: bool
    model_input_modalities: List[str]
    required_modalities: List[str]
    unsupported_modalities: List[str]
    warnings: List[str]


def evaluate_attachment_compatibility(
    model_id: str,
    provider: str,
    attachments: Optional[List[Dict[str, str]]],
) -> AttachmentCompatibilityReport:
    """Evaluate whether attachments are likely compatible with a model."""
    normalized_provider = (provider or "").strip().lower()
    # Anthropic and OpenRouter both expose machine-readable input modalities;
    # for other providers we rely on the static fallback tables.
    consult_live_modalities = normalized_provider in {"openrouter", "anthropic"}
    modalities = get_model_modalities(model_id) if consult_live_modalities else set()

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
    elif normalized_provider == "anthropic":
        if "image" in required_modalities:
            if modalities:
                if "image" not in modalities:
                    unsupported_modalities.add("image")
            elif not supports_vision(model_id):
                unsupported_modalities.add("image")

        if has_pdf:
            if modalities:
                if "file" not in modalities:
                    unsupported_modalities.add("file")
            elif not supports_documents(model_id):
                unsupported_modalities.add("file")
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


def get_attachment_limits(model_id: str) -> Dict[str, Optional[int]]:
    """Return per-model attachment caps as a flat dict.

    Provider /v1/models endpoints do not return these numbers today, so the
    primary source is the per-family table at the top of this module. When a
    live fetcher (Anthropic, future) populates ModelInfo with explicit caps,
    those override the family default for the keys they fill.

    Keys: ``max_images_per_request``, ``max_image_bytes``, ``max_pdf_pages``,
    ``max_total_bytes`` (request payload cap).
    """
    limits = _resolve_attachment_limits(model_id)
    info = _lookup_model(model_id)
    if info is not None:
        if info.max_images_per_request is not None:
            limits["max_images_per_request"] = info.max_images_per_request
        if info.max_image_bytes is not None:
            limits["max_image_bytes"] = info.max_image_bytes
        if info.max_pdf_pages is not None:
            limits["max_pdf_pages"] = info.max_pdf_pages
        if info.max_total_attachment_bytes is not None:
            limits["max_total_bytes"] = info.max_total_attachment_bytes
    return limits


# ============================================================================
# Per-provider image-token estimation
# ============================================================================
#
# A LOCAL estimate of how many input tokens one image costs, used ONLY as a
# fallback when the provider does not report image tokens in its usage metadata
# (providers that do already fold image tokens into ``prompt_tokens``, so adding
# this on top would double-count). Formulas mirror the public token math as of
# 2026-06; treat the numbers as estimates, not exact billing.

# Anthropic tiles an image into 28x28-pixel patches (one patch per visual
# token) after clamping the long edge to a per-model ceiling. Most models cap at
# 1568 tokens / 1568px; Opus 4.7/4.8, Fable 5, and Mythos 5 raise that to
# 4784 tokens / 2576px.
_ANTHROPIC_HIRES_MODELS = (
    "opus-4-7", "opus-4.7",
    "opus-4-8", "opus-4.8",
    "fable-5", "fable5",
    "mythos-5", "mythos5",
)
_ANTHROPIC_PATCH_PX = 28
_ANTHROPIC_TOKENS_STD = 1568
_ANTHROPIC_LONG_EDGE_STD = 1568
_ANTHROPIC_TOKENS_HIRES = 4784
_ANTHROPIC_LONG_EDGE_HIRES = 2576

# Flat per-image estimate when dimensions are unknown or the family is
# unrecognized: roughly a 1-megapixel image on the standard Anthropic model.
_DEFAULT_IMAGE_TOKENS = 1300


def _image_token_family(model_id: str) -> str:
    """Return ``'anthropic' | 'openai' | 'gemini' | ''`` for image-token math."""
    if not model_id:
        return ""
    lowered = model_id.lower()
    if any(p in lowered for p in ("claude", "anthropic", "fable", "mythos")):
        return "anthropic"
    if any(p in lowered for p in ("gpt", "openai", "chatgpt", "codex")):
        return "openai"
    if "gemini" in lowered or "google" in lowered:
        return "gemini"
    return ""


def estimate_image_tokens(
    model_id: str,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> int:
    """Estimate the input-token cost of one image for ``model_id``.

    Per-provider formulas:

    - Anthropic: 28x28-pixel patches (one patch per visual token) after the long
      edge is clamped to the per-model ceiling (1568px, or 2576px on hi-res
      models), capped at 1568 / 4784 tokens.
    - OpenAI: 85 base tokens + 170 per 512x512 tile, after fitting within
      2048x2048 and scaling the shortest side to 768px (the GPT-4o/4.1 model; a
      reasonable approximation for newer patch-based models).
    - Gemini: 258 tokens when both dimensions are <= 384px, else 258 per
      768x768 tile.

    Falls back to a flat conservative estimate when dimensions are unavailable
    or the family is unrecognized. This is a fallback estimator only (see the
    section comment): never add it on top of provider-reported image tokens.
    """
    family = _image_token_family(model_id)
    if not width or not height or width <= 0 or height <= 0:
        return _DEFAULT_IMAGE_TOKENS

    if family == "anthropic":
        hires = any(p in model_id.lower() for p in _ANTHROPIC_HIRES_MODELS)
        long_edge = _ANTHROPIC_LONG_EDGE_HIRES if hires else _ANTHROPIC_LONG_EDGE_STD
        token_cap = _ANTHROPIC_TOKENS_HIRES if hires else _ANTHROPIC_TOKENS_STD
        w, h = float(width), float(height)
        longest = max(w, h)
        if longest > long_edge:
            scale = long_edge / longest
            w *= scale
            h *= scale
        tokens = math.ceil(w / _ANTHROPIC_PATCH_PX) * math.ceil(h / _ANTHROPIC_PATCH_PX)
        return min(tokens, token_cap)

    if family == "openai":
        w, h = float(width), float(height)
        longest = max(w, h)
        if longest > 2048:
            scale = 2048 / longest
            w *= scale
            h *= scale
        shortest = min(w, h)
        if shortest > 768:
            scale = 768 / shortest
            w *= scale
            h *= scale
        tiles = math.ceil(w / 512) * math.ceil(h / 512)
        return 85 + 170 * tiles

    if family == "gemini":
        if width <= 384 and height <= 384:
            return 258
        tiles = math.ceil(width / 768) * math.ceil(height / 768)
        return 258 * tiles

    return _DEFAULT_IMAGE_TOKENS
