"""Shared utilities for LLM provider probing and metadata extraction."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

LOCAL_MODEL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}


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
    if not base_url:
        return False
    parse_target = base_url if "://" in base_url else f"http://{base_url}"
    try:
        parsed = urlparse(parse_target)
    except ValueError:
        return False
    return (parsed.hostname or "").lower() in LOCAL_MODEL_HOSTS


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
