"""Per-token pricing lookup for LLM cost calculation.

Sourced from the LiteLLM ``model_prices_and_context_window.json`` repository,
with the existing OpenRouter model cache as a secondary source for
``openrouter/*`` keys. A bundled snapshot ships in
``config/data/litellm_model_prices.json`` so cold starts always have rates
available before the daily network refresh succeeds.

Rates are normalised to USD per token (LiteLLM stores them that way already).
Returns ``None`` for unknown models -- never silently defaults to zero.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Literal, Optional

import httpx

from . import model_capabilities

logger = logging.getLogger(__name__)

LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/"
    "main/model_prices_and_context_window.json"
)

_BUNDLED_SNAPSHOT_PATH = (
    Path(__file__).resolve().parent / "data" / "litellm_model_prices.json"
)

_REFRESH_TTL_SECONDS = 86_400  # 24h
_FAILURE_BACKOFF_SECONDS = 600  # avoid retry storms after a network failure

_litellm_cache: Dict[str, Dict[str, Any]] = {}
_last_fetch_ts: float = 0.0
_last_failure_ts: float = 0.0
_cache_lock = threading.Lock()
_loaded_from_bundle = False


RateSource = Literal["litellm", "openrouter", "bundled"]


@dataclass
class ModelRates:
    """USD-per-token rates for a single model.

    All ``*_per_token`` values are floats in USD. ``None`` means the upstream
    source did not publish that bucket (e.g. a provider that does not offer
    prompt caching has ``cache_read_per_token=None``).
    """

    input_per_token: float
    output_per_token: float
    cache_read_per_token: Optional[float] = None
    cache_write_5m_per_token: Optional[float] = None
    cache_write_1h_per_token: Optional[float] = None
    input_above_200k_per_token: Optional[float] = None
    output_above_200k_per_token: Optional[float] = None
    supports_prompt_caching: bool = False
    source: RateSource = "litellm"


def _load_bundled_snapshot() -> Dict[str, Dict[str, Any]]:
    """Read the on-disk LiteLLM snapshot, returning an empty dict on failure."""
    try:
        raw = _BUNDLED_SNAPSHOT_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {}
        data.pop("sample_spec", None)
        return data
    except FileNotFoundError:
        logger.warning("Bundled LiteLLM pricing snapshot not found at %s", _BUNDLED_SNAPSHOT_PATH)
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load bundled LiteLLM pricing snapshot: %s", exc)
        return {}


def _ensure_bundle_loaded() -> None:
    """Populate ``_litellm_cache`` from the bundled snapshot once at startup."""
    global _last_fetch_ts, _loaded_from_bundle
    if _loaded_from_bundle:
        return
    with _cache_lock:
        if _loaded_from_bundle:
            return
        bundle = _load_bundled_snapshot()
        if bundle:
            _litellm_cache.update(bundle)
            if _last_fetch_ts == 0.0:
                _last_fetch_ts = time.monotonic()
        _loaded_from_bundle = True


_ensure_bundle_loaded()


def refresh_litellm_pricing(*, force: bool = False, timeout: float = 15.0) -> int:
    """Pull the latest LiteLLM pricing JSON and refresh the in-memory cache.

    Returns the number of model entries currently in the cache after the call.
    A failed refresh keeps the existing cache intact and records a failure
    timestamp so callers do not retry within ``_FAILURE_BACKOFF_SECONDS``.
    """
    global _last_fetch_ts, _last_failure_ts
    _ensure_bundle_loaded()

    now = time.monotonic()
    with _cache_lock:
        fresh = (now - _last_fetch_ts) < _REFRESH_TTL_SECONDS and _last_fetch_ts > 0
        backoff_active = (now - _last_failure_ts) < _FAILURE_BACKOFF_SECONDS and _last_failure_ts > 0
        if not force and (fresh or backoff_active):
            return len(_litellm_cache)

    try:
        response = httpx.get(LITELLM_PRICING_URL, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.info("LiteLLM pricing refresh failed, keeping cache: %s", exc)
        with _cache_lock:
            _last_failure_ts = time.monotonic()
        return len(_litellm_cache)

    if not isinstance(payload, dict):
        with _cache_lock:
            _last_failure_ts = time.monotonic()
        return len(_litellm_cache)

    payload.pop("sample_spec", None)
    with _cache_lock:
        _litellm_cache.clear()
        _litellm_cache.update(payload)
        _last_fetch_ts = time.monotonic()
        _last_failure_ts = 0.0
        size = len(_litellm_cache)
    logger.info("LiteLLM pricing cache refreshed: %d entries", size)
    return size


def _maybe_refresh_lazy() -> None:
    """Trigger a refresh if the cache is older than the TTL."""
    now = time.monotonic()
    with _cache_lock:
        stale = (now - _last_fetch_ts) >= _REFRESH_TTL_SECONDS
        backoff_active = (now - _last_failure_ts) < _FAILURE_BACKOFF_SECONDS and _last_failure_ts > 0
    if stale and not backoff_active:
        refresh_litellm_pricing()


def _candidate_keys(provider: str, model: str) -> list[str]:
    """Build the lookup-key fallback chain per spec §7."""
    candidates: list[str] = []
    seen: set[str] = set()

    def add(key: str) -> None:
        key = key.strip()
        if key and key not in seen:
            seen.add(key)
            candidates.append(key)

    add(model)
    if provider:
        add(f"{provider}/{model}")

    def add_date_stripped(value: str) -> None:
        # Strip dated suffix like ``-2024-08-06`` (ISO) or ``-20240806`` (compact).
        iso = re.fullmatch(r"(.+)-\d{4}-\d{2}-\d{2}", value)
        if iso:
            add(iso.group(1))
            if provider:
                add(f"{provider}/{iso.group(1)}")
            return
        parts = value.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) >= 6:
            add(parts[0])
            if provider:
                add(f"{provider}/{parts[0]}")

    add_date_stripped(model)

    # Strip a leading provider prefix already embedded in the model id.
    if "/" in model:
        bare = model.split("/", 1)[1]
        add(bare)
        if provider:
            add(f"{provider}/{bare}")
        add_date_stripped(bare)

    return candidates


def _rates_from_litellm_entry(entry: Dict[str, Any]) -> Optional[ModelRates]:
    """Translate a LiteLLM JSON entry to ``ModelRates``. ``None`` if rates missing."""
    input_cost = entry.get("input_cost_per_token")
    output_cost = entry.get("output_cost_per_token")
    if input_cost is None or output_cost is None:
        return None
    try:
        input_per_token = float(input_cost)
        output_per_token = float(output_cost)
    except (TypeError, ValueError):
        return None

    def _opt_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    return ModelRates(
        input_per_token=input_per_token,
        output_per_token=output_per_token,
        cache_read_per_token=_opt_float(entry.get("cache_read_input_token_cost")),
        cache_write_5m_per_token=_opt_float(entry.get("cache_creation_input_token_cost")),
        cache_write_1h_per_token=_opt_float(
            entry.get("cache_creation_input_token_cost_1hr")
            or entry.get("cache_creation_input_token_cost_above_1hr")
        ),
        input_above_200k_per_token=_opt_float(
            entry.get("input_cost_per_token_above_200k_tokens")
            or entry.get("input_cost_per_token_above_128k_tokens")
        ),
        output_above_200k_per_token=_opt_float(
            entry.get("output_cost_per_token_above_200k_tokens")
            or entry.get("output_cost_per_token_above_128k_tokens")
        ),
        supports_prompt_caching=bool(entry.get("supports_prompt_caching")),
        source="litellm",
    )


def _rates_from_openrouter(model: str) -> Optional[ModelRates]:
    """Fallback to the OpenRouter model cache for prefixed ids."""
    info = model_capabilities.get_model_info(model)
    if info is None or info.pricing_prompt is None or info.pricing_completion is None:
        return None
    return ModelRates(
        input_per_token=float(info.pricing_prompt),
        output_per_token=float(info.pricing_completion),
        source="openrouter",
    )


def get_rates(provider: str, model: str) -> Optional[ModelRates]:
    """Look up per-token rates for ``model`` under ``provider``.

    Lookup order:
    1. LiteLLM cache (network-refreshed daily, bundled snapshot as warm start)
    2. OpenRouter cache (only useful when the configured model id matches one
       the OpenRouter API knows about, e.g. ``anthropic/claude-sonnet-4.5``)
    3. ``None`` -- caller treats as "rates unavailable".
    """
    if not model:
        return None
    _maybe_refresh_lazy()

    provider_key = (provider or "").strip().lower()
    candidates = _candidate_keys(provider_key, model)

    with _cache_lock:
        snapshot = dict(_litellm_cache)
    for key in candidates:
        entry = snapshot.get(key)
        if not isinstance(entry, dict):
            continue
        rates = _rates_from_litellm_entry(entry)
        if rates is not None:
            return rates

    return _rates_from_openrouter(model)


def cache_size() -> int:
    """Return the current number of LiteLLM pricing entries cached in memory."""
    with _cache_lock:
        return len(_litellm_cache)


def last_fetch_timestamp() -> float:
    """Monotonic timestamp of the last successful refresh or bundled load."""
    return _last_fetch_ts
