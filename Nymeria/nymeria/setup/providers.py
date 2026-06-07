"""Provider data layer and live checks for first-run setup.

This module is deliberately free of any TUI dependency so the interactive Textual
wizard and the headless/non-interactive path share one source of truth. Providers
come from the canonical registry in `config/llm_providers.py` (the same list the
desktop global-settings picker uses), grouped by backend tier.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from ..config.llm_providers import (
    LLMProviderSpec,
    list_llm_provider_specs,
    normalize_llm_provider,
    resolve_provider_base_url,
)
from ..core.llm_provider_utils import base_url_allows_no_api_key, extract_model_metadata

logger = logging.getLogger(__name__)

# Optional capability keys offered by the capability steps / CLI flags. Unrelated
# to the primary provider, so the registry change leaves this untouched.
OPTIONAL_ENV_ORDER = (
    "EMBEDDING_API_KEY",
    "RAG_RERANK_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "PERPLEXITY_API_KEY",
)

# Tier grouping, mirroring how nymeria-desktop's buildProviderGroups() orders
# providers: native reasoning first, then gateways, then unverified.
TIER_ORDER: tuple[str, ...] = ("native", "gateway", "unverified")
TIER_LABELS: dict[str, str] = {
    "native": "Native reasoning",
    "gateway": "Gateway",
    "unverified": "Unverified",
}

# Providers with a stable, documented key prefix we can validate against. Every
# other provider passes key validation on any non-empty value.
_KEY_PREFIXES: dict[str, str] = {
    "anthropic": "sk-ant-",
    "openai": "sk-",
    "openrouter": "sk-or-",
}

# The three first-class providers keep their original POST smoke-test.
_SMOKE_POST_PROVIDERS = frozenset({"anthropic", "openai", "openrouter"})


@dataclass(frozen=True)
class LLMConnectionResult:
    model: str
    # False when the provider could not be smoke-tested (no key / non-key auth /
    # no resolvable endpoint). Config is still written; finalize notes it.
    tested: bool = True


class LLMConnectionError(RuntimeError):
    """Raised when first-run provider validation cannot complete."""


@dataclass(frozen=True)
class ModelChoice:
    id: str
    name: str = ""
    context_length: int | None = None


# --- provider catalog -------------------------------------------------------


def grouped_provider_specs() -> list[tuple[str, list[LLMProviderSpec]]]:
    """Registry providers grouped by tier (native -> gateway -> unverified).

    Each group's specs stay label-sorted because `list_llm_provider_specs()`
    already sorts by label. Used to build the searchable provider picker.
    """
    buckets: dict[str, list[LLMProviderSpec]] = {tier: [] for tier in TIER_ORDER}
    for spec in list_llm_provider_specs():
        buckets.setdefault(spec.tier, []).append(spec)

    groups: list[tuple[str, list[LLMProviderSpec]]] = []
    for tier in TIER_ORDER:
        specs = buckets.get(tier) or []
        if specs:
            groups.append((TIER_LABELS[tier], specs))
    # Any unexpected tier value lands in its own trailing group rather than
    # disappearing silently.
    for tier, specs in buckets.items():
        if tier not in TIER_ORDER and specs:
            groups.append((tier.title(), specs))
    return groups


def key_prefix_for_spec(spec: LLMProviderSpec) -> str:
    """The documented key prefix for this provider, or "" if none is known."""
    return _KEY_PREFIXES.get(normalize_llm_provider(spec.id), "")


def valid_key_format_for_spec(spec: LLMProviderSpec, api_key: str) -> tuple[bool, str]:
    """Return (ok, expected_prefix) for the chosen provider.

    A non-empty key is required only when the provider needs one. A prefix is
    enforced only for the few providers with a documented prefix; everything else
    passes on any non-empty value (the live model fetch validates the key for real).
    """
    key = (api_key or "").strip()
    if not spec.requires_api_key:
        return (True, "")
    if not key:
        return (False, "")
    prefix = key_prefix_for_spec(spec)
    if prefix and not key.startswith(prefix):
        return (False, prefix)
    return (True, "")


# --- live model listing -----------------------------------------------------


def _models_request(
    spec: LLMProviderSpec,
    api_key: str,
    base_url: str | None,
) -> tuple[str, dict[str, str]] | None:
    """Build (url, headers) for a GET /models probe, or None if not listable.

    Mirrors the backend `GET /models/available` logic so the wizard lists the
    same models a running backend would.
    """
    key = (api_key or "").strip()
    if spec.api_format == "anthropic_messages":
        base = (base_url or spec.default_base_url or "https://api.anthropic.com").strip().rstrip("/")
        if not base:
            return None
        url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        headers = {
            "x-api-key": key or "not-needed",
            "anthropic-version": "2023-06-01",
        }
        return url, headers
    if spec.api_format == "openai_chat":
        base = resolve_provider_base_url(spec.id, configured_base_url=(base_url or None))
        if not base or "${" in base:
            return None
        if not key and (not spec.requires_api_key or base_url_allows_no_api_key(base)):
            key = "not-needed"
        if not key:
            return None
        url = f"{base.rstrip('/')}/models"
        return url, {"Authorization": f"Bearer {key}"}
    # google_genai / ollama_native / bedrock_converse: no OpenAI-shaped /models.
    return None


async def fetch_models_for_spec(
    spec: LLMProviderSpec,
    *,
    api_key: str | None,
    base_url: str | None = None,
    timeout: float = 8.0,
) -> list[ModelChoice]:
    """List a provider's models via its /models endpoint.

    Returns [] on any error / empty response and never raises, so the model step
    can fall back to free-text entry. Bounded by `timeout` so the UI worker cannot
    hang.
    """
    request = _models_request(spec, api_key or "", base_url)
    if request is None:
        return []
    url, headers = request
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # network, HTTP, decode: all degrade to free text
        logger.warning("setup: could not list models from %s: %s", url, exc)
        return []

    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    choices: list[ModelChoice] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or "").strip()
        if not model_id:
            continue
        metadata = extract_model_metadata(row)
        choices.append(
            ModelChoice(
                id=model_id,
                name=str(row.get("name") or model_id),
                context_length=metadata.get("context_length"),
            )
        )
    choices.sort(key=lambda choice: choice.id)
    return choices


# --- live connection check --------------------------------------------------


def check_llm_connection_for_spec(
    spec: LLMProviderSpec,
    model: str,
    api_key: str,
    *,
    base_url: str | None = None,
) -> LLMConnectionResult:
    """Validate provider credentials with one tiny live request.

    The three first-class providers keep their original POST smoke-test (when no
    custom base URL is set); every other provider is validated with a GET /models
    probe. Providers that cannot be checked without non-key auth (locals with no
    base URL, bedrock, vertex) return `tested=False` instead of raising, so
    finalize still writes a usable config.
    """
    provider_id = normalize_llm_provider(spec.id)
    custom_base = bool(base_url and base_url.strip())
    try:
        if provider_id in _SMOKE_POST_PROVIDERS and not custom_base:
            _smoke_test_post(provider_id, model, api_key)
        else:
            request = _models_request(spec, api_key, base_url)
            if request is None:
                return LLMConnectionResult(model=model, tested=False)
            url, headers = request
            with httpx.Client(timeout=15.0) as client:
                client.get(url, headers=headers).raise_for_status()
    except httpx.TimeoutException as exc:
        raise LLMConnectionError("provider did not respond before the 15s timeout") from exc
    except httpx.HTTPStatusError as exc:
        detail = _http_error_detail(exc.response)
        raise LLMConnectionError(
            f"{spec.label} returned HTTP {exc.response.status_code}: {detail}"
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMConnectionError(str(exc)) from exc

    return LLMConnectionResult(model=model, tested=True)


def _smoke_test_post(provider_id: str, model: str, api_key: str) -> None:
    """One tiny live request confirming the key and model work for the big three."""
    if provider_id == "anthropic":
        _post_json(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": model,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "Reply with ok."}],
            },
        )
    elif provider_id == "openai":
        _post_json(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "input": "Reply with ok.",
                "max_output_tokens": 16,
            },
        )
    elif provider_id == "openrouter":
        _post_json(
            "https://openrouter.ai/api/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/ManningAskew7/NymeriaOS",
                "X-Title": "Nymeria",
            },
            json={
                "model": model,
                "input": "Reply with ok.",
                "max_output_tokens": 16,
            },
        )
    else:  # pragma: no cover - guarded by the caller's provider-id check
        raise LLMConnectionError(f"Unsupported provider: {provider_id}")


def _post_json(
    url: str,
    *,
    headers: Mapping[str, str],
    json: Mapping[str, Any],
) -> None:
    request_headers = {"Content-Type": "application/json", **headers}
    with httpx.Client(timeout=15.0) as client:
        response = client.post(url, headers=request_headers, json=json)
        response.raise_for_status()


def _http_error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:300] or response.reason_phrase

    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()[:300]
        message = body.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:300]
    return response.reason_phrase


__all__ = [
    "OPTIONAL_ENV_ORDER",
    "TIER_ORDER",
    "TIER_LABELS",
    "LLMConnectionResult",
    "LLMConnectionError",
    "ModelChoice",
    "grouped_provider_specs",
    "key_prefix_for_spec",
    "valid_key_format_for_spec",
    "fetch_models_for_spec",
    "check_llm_connection_for_spec",
]
