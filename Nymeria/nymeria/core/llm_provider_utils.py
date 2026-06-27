"""Shared utilities for LLM provider probing and metadata extraction."""

from __future__ import annotations

from typing import Any

import httpx

from ..config.local_llm import is_local_llm_base_url

# Anthropic Messages API version header value. Shared by the provider-test probe
# and the live model-listing probe so they advertise the same wire version.
ANTHROPIC_API_VERSION = "2023-06-01"

# OpenRouter courtesy attribution headers (their dashboard ranks apps by these).
# Canonical copy; reused by core/llm_provider_test_suite.py.
OPENROUTER_ATTRIBUTION_HEADERS: dict[str, str] = {
    "HTTP-Referer": "https://github.com/ManningAskew7/NymeriaOS",
    "X-Title": "Nymeria",
}


def cliproxy_base_url_with_v1(base_url: str) -> str:
    """Append the ``/v1`` suffix CLIProxy's OpenAI-compatible surface needs.

    CLIProxy serves its OpenAI-compatible API under ``/v1``; a base URL that
    points at the proxy root without it makes every ``/models`` and
    ``/chat/completions`` request 404. Non-CLIProxy URLs, and ones already
    ending in ``/v1``, are returned rstripped but otherwise unchanged.
    """
    # Function-local import: pulling vendor.react_agent.cliproxy at module level
    # would trigger react_agent/__init__ -> providers (langchain), bloating this
    # light core leaf. No cycle (react_agent never imports back into core utils).
    from ..vendor.react_agent.cliproxy import looks_like_cliproxy_url

    clean = base_url.rstrip("/")
    if looks_like_cliproxy_url(clean) and not clean.endswith("/v1"):
        return f"{clean}/v1"
    return clean


def provider_probe_headers(
    provider: str, api_key: str, *, has_custom_base_url: bool = False
) -> dict[str, str]:
    """Auth + identity headers for a direct provider HTTP probe (test or models).

    ``has_custom_base_url`` signals the request targets a non-default base URL
    (e.g. CLIProxy); for anthropic that adds the cloak-skip ``User-Agent``, so
    the provider-test probe and the model listing send the same identity to a
    custom endpoint. The value is irrelevant for OpenAI-compatible providers.
    """
    if provider == "anthropic":
        headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_API_VERSION}
        if has_custom_base_url:
            # Function-local import for the same reason as above.
            from ..vendor.react_agent.cliproxy import CLIPROXY_CLAUDE_USER_AGENT

            headers["User-Agent"] = CLIPROXY_CLAUDE_USER_AGENT
        return headers
    headers = {"Authorization": f"Bearer {api_key}"}
    if provider == "openrouter":
        headers.update(OPENROUTER_ATTRIBUTION_HEADERS)
    return headers


def redact_secrets(text: str, *secrets: str | None) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def http_error_detail(response: httpx.Response, *secrets: str | None) -> str:
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        return redact_secrets(text, *secrets)[:300] or response.reason_phrase

    message: str | None = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            candidate = error.get("message")
            if isinstance(candidate, str) and candidate.strip():
                message = candidate.strip()
        if message is None:
            candidate = body.get("message")
            if isinstance(candidate, str) and candidate.strip():
                message = candidate.strip()

    return redact_secrets(message or response.reason_phrase, *secrets)[:300]


def base_url_allows_no_api_key(base_url: str | None) -> bool:
    return is_local_llm_base_url(base_url)


def first_int(source: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def first_float(source: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            return parsed
    return None


def extract_model_metadata(model: dict[str, Any]) -> dict[str, Any]:
    """Normalize common metadata fields returned by provider /models APIs."""
    architecture = model.get("architecture") or {}
    if not isinstance(architecture, dict):
        architecture = {}
    top_provider = model.get("top_provider") or {}
    if not isinstance(top_provider, dict):
        top_provider = {}
    defaults = model.get("default_parameters") or model.get("defaults") or {}
    if not isinstance(defaults, dict):
        defaults = {}
    pricing = model.get("pricing") or {}
    if not isinstance(pricing, dict):
        pricing = {}

    context_length = (
        first_int(
            model,
            "context_length",
            "context_window",
            "context_size",
            "max_context_length",
            "max_context_tokens",
            "input_token_limit",
            "max_input_tokens",
        )
        or first_int(top_provider, "context_length", "max_context_tokens")
    )
    max_completion_tokens = (
        first_int(
            model,
            "max_completion_tokens",
            "max_output_tokens",
            "output_token_limit",
        )
        or first_int(top_provider, "max_completion_tokens", "max_output_tokens")
    )

    raw_supported = model.get("supported_parameters") or model.get("supported_params") or []
    if not isinstance(raw_supported, (list, tuple, set)):
        raw_supported = []
    supported_parameters = [str(param) for param in raw_supported if param]
    raw_modalities = (
        model.get("input_modalities")
        or architecture.get("input_modalities")
        or model.get("modalities")
        or []
    )
    if not isinstance(raw_modalities, (list, tuple, set)):
        raw_modalities = []
    input_modalities = [str(modality) for modality in raw_modalities if modality]

    return {
        "context_length": context_length,
        "max_completion_tokens": max_completion_tokens,
        "supported_parameters": supported_parameters,
        "input_modalities": input_modalities,
        "tokenizer": architecture.get("tokenizer") or model.get("tokenizer"),
        "default_temperature": first_float(defaults, "temperature"),
        "default_top_p": first_float(defaults, "top_p"),
        "default_frequency_penalty": first_float(defaults, "frequency_penalty"),
        "pricing_prompt": first_float(pricing, "prompt"),
        "pricing_completion": first_float(pricing, "completion"),
    }
