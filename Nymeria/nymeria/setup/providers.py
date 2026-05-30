"""Provider catalog and the live connection check for first-run setup.

This is lifted, unchanged in behavior, from the previous monolithic setup
wizard. It is deliberately free of any TUI dependency so both the interactive
Textual wizard and the headless/non-interactive path share one source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import httpx

from ..onboarding import ProviderOption

PROVIDERS: dict[str, ProviderOption] = {
    "anthropic": ProviderOption(
        name="anthropic",
        label="Anthropic (Claude)",
        env_var="ANTHROPIC_API_KEY",
        key_prefix="sk-ant-",
        default_model="claude-sonnet-4-6",
    ),
    "openai": ProviderOption(
        name="openai",
        label="OpenAI (GPT)",
        env_var="OPENAI_API_KEY",
        key_prefix="sk-",
        default_model="gpt-5.5",
    ),
    "openrouter": ProviderOption(
        name="openrouter",
        label="OpenRouter (multi-model)",
        env_var="OPENROUTER_API_KEY",
        key_prefix="sk-or-",
        default_model="anthropic/claude-sonnet-4-6",
    ),
}

PROVIDER_ORDER = ("anthropic", "openai", "openrouter")

# Backend tier grouping, mirroring how nymeria-desktop's buildProviderGroups()
# orders providers (Native reasoning first, then Gateway). OAuth-only display
# options are intentionally omitted in this phase.
PROVIDER_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Native reasoning", ("anthropic", "openai")),
    ("Gateway", ("openrouter",)),
)

OPTIONAL_ENV_ORDER = (
    "EMBEDDING_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "PERPLEXITY_API_KEY",
)

# Providers that expose an OpenAI-style API mode toggle (responses vs chat
# completions). Anthropic does not, mirroring the desktop ModelConfigTab.
API_MODE_PROVIDERS = ("openai", "openrouter")


@dataclass(frozen=True)
class LLMConnectionResult:
    model: str


class LLMConnectionError(RuntimeError):
    """Raised when first-run provider validation cannot complete."""


def valid_key_format(provider: ProviderOption, api_key: str) -> bool:
    return bool(api_key) and api_key.startswith(provider.key_prefix)


def check_llm_connection(
    provider: ProviderOption,
    model: str,
    api_key: str,
) -> LLMConnectionResult:
    """Make one tiny live request to confirm the key and model work."""

    try:
        if provider.name == "anthropic":
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
        elif provider.name == "openai":
            _post_json(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "input": "Reply with ok.",
                    "max_output_tokens": 16,
                },
            )
        elif provider.name == "openrouter":
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
        else:
            raise LLMConnectionError(f"Unsupported provider: {provider.name}")
    except httpx.TimeoutException as exc:
        raise LLMConnectionError("provider did not respond before the 15s timeout") from exc
    except httpx.HTTPStatusError as exc:
        detail = _http_error_detail(exc.response)
        raise LLMConnectionError(
            f"{provider.label} returned HTTP {exc.response.status_code}: {detail}"
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMConnectionError(str(exc)) from exc

    return LLMConnectionResult(model=model)


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
    "PROVIDERS",
    "PROVIDER_ORDER",
    "PROVIDER_GROUPS",
    "OPTIONAL_ENV_ORDER",
    "API_MODE_PROVIDERS",
    "LLMConnectionResult",
    "LLMConnectionError",
    "valid_key_format",
    "check_llm_connection",
]
