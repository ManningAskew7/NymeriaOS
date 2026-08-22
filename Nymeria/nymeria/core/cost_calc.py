"""USD cost extraction and computation for LLM responses.

Normalises provider-specific ``usage`` shapes into a single ``NormalizedUsage``
dataclass, then multiplies each token bucket by its per-token rate to get a USD
figure: non-cached prompt tokens and completion tokens at the model's
input/output rates (which switch to an above-200k tier for large prompts), plus
cache-read and cache-write tokens at their discounted/premium rates
where the provider reports them (``cached_tokens`` is a subset of
``prompt_tokens``; ``reasoning_tokens`` a subset of ``completion_tokens``). If
the provider reports its own cost, that value is preferred.

Pure functions, no I/O. Provider response parsing happens here; the per-token
rates live in :mod:`nymeria.config.pricing_table`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Tuple

from langchain_core.messages import AIMessage

from ..config.pricing_table import ModelRates

logger = logging.getLogger(__name__)

_TIER_THRESHOLD_TOKENS = 200_000


@dataclass
class NormalizedUsage:
    """Normalised per-call usage figures across providers.

    All counts are integers. ``cached_tokens`` is a subset of ``prompt_tokens``;
    ``reasoning_tokens`` is a subset of ``completion_tokens``. Modality
    sub-fields (``audio_in_tokens``, ``image_in_tokens``) are also subsets of
    ``prompt_tokens`` (and likewise for output).
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    reasoning_tokens: int = 0
    audio_in_tokens: int = 0
    image_in_tokens: int = 0
    audio_out_tokens: int = 0
    provider_reported_cost_usd: Optional[float] = None


def _int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _get(obj: Any, *names: str) -> Any:
    """Read the first attribute/key from ``obj`` whose name is in ``names``."""
    if obj is None:
        return None
    for name in names:
        if isinstance(obj, dict):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _provider_reported_cost(usage: Any) -> Optional[float]:
    """Read a dollar amount the provider supplied directly.

    OpenRouter returns ``usage.cost`` as a number; Perplexity returns
    ``usage.cost.total_cost``.
    """
    cost = _get(usage, "cost")
    if isinstance(cost, (int, float)):
        return float(cost)
    if isinstance(cost, dict):
        total = cost.get("total_cost")
        if isinstance(total, (int, float)):
            return float(total)
    return None


def parse_usage_from_message(msg: AIMessage, provider: str) -> NormalizedUsage:
    """Extract a :class:`NormalizedUsage` from a LangChain ``AIMessage``.

    Pulls from ``usage_metadata`` first (LangChain's normalised shape) and
    falls back to ``response_metadata`` for provider-specific cache and
    modality breakdowns. ``provider`` is the lower-cased provider key used
    only for handling provider-specific field name differences (DeepSeek
    cache hits, Perplexity reasoning, etc.).
    """
    provider_key = (provider or "").strip().lower()
    usage_metadata = getattr(msg, "usage_metadata", None) or {}
    response_metadata = getattr(msg, "response_metadata", None) or {}
    raw_usage = response_metadata.get("usage") if isinstance(response_metadata, dict) else None
    token_usage = response_metadata.get("token_usage") if isinstance(response_metadata, dict) else None

    prompt = _int(_get(usage_metadata, "input_tokens", "prompt_tokens")) or _int(
        _get(raw_usage, "input_tokens", "prompt_tokens")
    ) or _int(_get(token_usage, "prompt_tokens", "input_tokens"))

    completion = _int(_get(usage_metadata, "output_tokens", "completion_tokens")) or _int(
        _get(raw_usage, "output_tokens", "completion_tokens")
    ) or _int(_get(token_usage, "completion_tokens", "output_tokens"))

    # ``input_token_details`` (LangChain canonical) carries cache_read and
    # audio/image breakdowns. Anthropic native uses cache_creation/cache_read at
    # the top level of the raw usage dict.
    input_details = _get(usage_metadata, "input_token_details") or {}
    output_details = _get(usage_metadata, "output_token_details") or {}

    cached = _int(_get(input_details, "cache_read"))
    if not cached:
        # OpenAI-shape (response_metadata.token_usage.prompt_tokens_details.cached_tokens)
        token_pt_details = (
            token_usage.get("prompt_tokens_details") if isinstance(token_usage, dict) else None
        ) or {}
        cached = _int(_get(token_pt_details, "cached_tokens"))
    if not cached:
        # OpenAI Responses API (response_metadata.usage.prompt_tokens_details.cached_tokens)
        raw_pt_details = (
            raw_usage.get("prompt_tokens_details") if isinstance(raw_usage, dict) else None
        ) or {}
        cached = _int(_get(raw_pt_details, "cached_tokens"))
    if not cached:
        # Anthropic native: ``cache_read_input_tokens`` on the raw usage dict.
        cached = _int(_get(raw_usage, "cache_read_input_tokens"))
    if not cached and provider_key == "deepseek":
        cached = _int(_get(raw_usage, "prompt_cache_hit_tokens"))

    cache_write_5m = _int(_get(input_details, "cache_creation"))
    cache_write_1h = 0
    if not cache_write_5m:
        # Anthropic native: top-level cache_creation_input_tokens, with optional
        # breakdown into ephemeral_5m / ephemeral_1h.
        cache_write_5m = _int(_get(raw_usage, "cache_creation_input_tokens"))
    cache_creation = _get(raw_usage, "cache_creation")
    if isinstance(cache_creation, dict):
        ephemeral_5m = _int(cache_creation.get("ephemeral_5m_input_tokens"))
        ephemeral_1h = _int(cache_creation.get("ephemeral_1h_input_tokens"))
        if ephemeral_5m or ephemeral_1h:
            cache_write_5m = ephemeral_5m
            cache_write_1h = ephemeral_1h
    if not cache_write_5m and not cache_write_1h:
        # OpenRouter (OpenAI shape): prompt_tokens_details.cache_write_tokens,
        # on token_usage or the raw usage dict. Files under the 5m bucket
        # (OR's anthropic-family default TTL). Measured 2026-08-21: OR's
        # prompt_tokens is INCLUSIVE of cache_write_tokens (write call:
        # prompt 15,239 = 15,236 written + 3 uncached), so the uncached
        # subtraction in compute_cost_usd is correct for this shape. Not
        # provider-scoped: OR-shaped gateways share the field, but the
        # inclusive-prompt_tokens premise is measured for OR only; a
        # provider reporting writes EXCLUSIVE of prompt_tokens would
        # under-count uncached input here.
        for details_holder in (token_usage, raw_usage):
            pt_details = (
                details_holder.get("prompt_tokens_details")
                if isinstance(details_holder, dict)
                else None
            ) or {}
            cache_write_5m = _int(_get(pt_details, "cache_write_tokens"))
            if cache_write_5m:
                break

    reasoning = _int(_get(output_details, "reasoning"))
    if not reasoning:
        # OpenAI: completion_tokens_details.reasoning_tokens
        token_ct_details = (
            token_usage.get("completion_tokens_details") if isinstance(token_usage, dict) else None
        ) or {}
        reasoning = _int(_get(token_ct_details, "reasoning_tokens"))
    if not reasoning:
        raw_ct_details = (
            raw_usage.get("completion_tokens_details") if isinstance(raw_usage, dict) else None
        ) or {}
        reasoning = _int(_get(raw_ct_details, "reasoning_tokens"))
    if not reasoning and provider_key == "perplexity":
        reasoning = _int(_get(raw_usage, "reasoning_tokens"))

    audio_in = _int(_get(input_details, "audio"))
    image_in = _int(_get(input_details, "image"))
    if not audio_in or not image_in:
        token_pt_details = (
            token_usage.get("prompt_tokens_details") if isinstance(token_usage, dict) else None
        ) or {}
        if not audio_in:
            audio_in = _int(_get(token_pt_details, "audio_tokens"))
        if not image_in:
            image_in = _int(_get(token_pt_details, "image_tokens"))

    audio_out = _int(_get(output_details, "audio"))
    if not audio_out:
        token_ct_details = (
            token_usage.get("completion_tokens_details") if isinstance(token_usage, dict) else None
        ) or {}
        audio_out = _int(_get(token_ct_details, "audio_tokens"))

    reported_cost = _provider_reported_cost(raw_usage) or _provider_reported_cost(token_usage)

    return NormalizedUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        cached_tokens=cached,
        cache_write_5m_tokens=cache_write_5m,
        cache_write_1h_tokens=cache_write_1h,
        reasoning_tokens=reasoning,
        audio_in_tokens=audio_in,
        image_in_tokens=image_in,
        audio_out_tokens=audio_out,
        provider_reported_cost_usd=reported_cost,
    )


def compute_cost_usd(usage: NormalizedUsage, rates: Optional[ModelRates]) -> Optional[float]:
    """Compute USD cost from normalised usage and per-token rates.

    Returns:
        - ``usage.provider_reported_cost_usd`` when the provider supplied a
          dollar figure (OpenRouter / Perplexity) -- treated as ground truth.
        - The cache-aware per-bucket sum otherwise.
        - ``None`` when ``rates`` is None and no provider-reported cost.
    """
    if usage.provider_reported_cost_usd is not None:
        return float(usage.provider_reported_cost_usd)
    if rates is None:
        return None

    # Tier branching: above-threshold rates apply when prompt exceeds threshold.
    input_rate = rates.input_per_token
    output_rate = rates.output_per_token
    if (
        rates.input_above_200k_per_token is not None
        and usage.prompt_tokens > _TIER_THRESHOLD_TOKENS
    ):
        input_rate = rates.input_above_200k_per_token
        if rates.output_above_200k_per_token is not None:
            output_rate = rates.output_above_200k_per_token

    # Uncached input. LangChain normalises Anthropic so ``input_tokens``
    # is the inclusive total (raw input + cache_read + cache_creation), per
    # langchain-anthropic's ``_create_usage_metadata``. Subtract all three
    # subsets here to recover the "fresh" input portion billed at the base
    # input rate. OpenRouter's OpenAI shape is inclusive the same way
    # (measured 2026-08-21, see the cache_write_tokens parse above); plain
    # OpenAI reports no write buckets so this collapses to
    # ``prompt - cached``.
    uncached_input = max(
        0,
        usage.prompt_tokens
        - usage.cached_tokens
        - usage.cache_write_5m_tokens
        - usage.cache_write_1h_tokens,
    )
    cost = uncached_input * input_rate

    if usage.cached_tokens:
        if rates.cache_read_per_token is not None:
            cost += usage.cached_tokens * rates.cache_read_per_token
        else:
            cost += usage.cached_tokens * input_rate

    if usage.cache_write_5m_tokens and rates.cache_write_5m_per_token is not None:
        cost += usage.cache_write_5m_tokens * rates.cache_write_5m_per_token
    if usage.cache_write_1h_tokens and rates.cache_write_1h_per_token is not None:
        cost += usage.cache_write_1h_tokens * rates.cache_write_1h_per_token

    cost += usage.completion_tokens * output_rate

    return cost


def parse_usage_from_messages(
    messages: Iterable[Any], provider: str
) -> Tuple[NormalizedUsage, int]:
    """Sum usage across all ``AIMessage`` entries in ``messages``.

    Returns the summed :class:`NormalizedUsage` plus the count of contributing
    messages (the count is informational; callers may want to log it).
    """
    summed = NormalizedUsage()
    count = 0
    reported_total: Optional[float] = None
    for msg in messages:
        if not isinstance(msg, AIMessage):
            continue
        parsed = parse_usage_from_message(msg, provider)
        if (
            parsed.prompt_tokens == 0
            and parsed.completion_tokens == 0
            and parsed.provider_reported_cost_usd is None
        ):
            continue
        count += 1
        summed.prompt_tokens += parsed.prompt_tokens
        summed.completion_tokens += parsed.completion_tokens
        summed.cached_tokens += parsed.cached_tokens
        summed.cache_write_5m_tokens += parsed.cache_write_5m_tokens
        summed.cache_write_1h_tokens += parsed.cache_write_1h_tokens
        summed.reasoning_tokens += parsed.reasoning_tokens
        summed.audio_in_tokens += parsed.audio_in_tokens
        summed.image_in_tokens += parsed.image_in_tokens
        summed.audio_out_tokens += parsed.audio_out_tokens
        if parsed.provider_reported_cost_usd is not None:
            reported_total = (reported_total or 0.0) + parsed.provider_reported_cost_usd
    summed.provider_reported_cost_usd = reported_total
    return summed, count


def slice_new_ai_messages(messages: List[Any], since_index: int) -> Tuple[List[Any], int]:
    """Return ``messages[since_index:]`` plus the new high-water index.

    The high-water index is the length of ``messages`` so the next call can
    pass it back as ``since_index`` and only see AIMessages added after the
    current turn.
    """
    if since_index < 0:
        since_index = 0
    if since_index >= len(messages):
        return [], len(messages)
    return list(messages[since_index:]), len(messages)
