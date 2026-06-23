"""LLM provider compatibility test suite.

The settings router exposes a small one-shot provider probe for setup forms.
This module is the heavier production-readiness check: it validates provider
metadata, resolves credentials the same way runtime calls do, probes `/models`,
optionally avoids billable models, and verifies basic chat/tool-call behavior.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from ..config.llm_providers import (
    get_llm_provider_spec,
    is_openai_compatible_provider,
    normalize_llm_provider,
    provider_requires_api_key,
    provider_supports_responses,
    resolve_provider_api_key,
    resolve_provider_base_url,
)
from ..config.local_llm import (
    detect_local_server_type,
    query_local_context_length,
)
from ..config.model_capabilities import register_model_metadata
from ..vendor.react_agent.cliproxy import looks_like_cliproxy_url
from .llm_credentials import get_llm_provider_credential
from .llm_provider_utils import (
    base_url_allows_no_api_key,
    extract_model_metadata,
    first_float,
    http_error_detail,
    redact_secrets,
)

logger = logging.getLogger(__name__)

StepStatus = Literal["passed", "failed", "warning", "skipped"]
ApiMode = Literal["chat_completions", "responses"]
_OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/ManningAskew7/NymeriaOS",
    "X-Title": "Nymeria",
}


@dataclass(frozen=True)
class ProviderTestSuiteOptions:
    """Options for one provider compatibility run."""

    provider: str
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    api_mode: str | None = None
    settings: Any | None = None
    vault: Any | None = None
    owner_user_id: str | None = None
    run_model_list: bool = True
    run_chat_completion: bool = True
    run_tool_call: bool = True
    allow_billable: bool = False
    prefer_free_model: bool = True
    timeout_seconds: float = 15.0


@dataclass
class ProviderTestStep:
    """One check in the provider test suite."""

    name: str
    status: StepStatus
    message: str
    url: str | None = None
    status_code: int | None = None
    latency_ms: int | None = None
    error_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {"passed", "warning", "skipped"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "ok": self.ok,
            "message": self.message,
            "url": self.url,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            "error_type": self.error_type,
            "metadata": self.metadata,
        }


@dataclass
class ProviderTestSuiteReport:
    """Sanitized provider compatibility report."""

    ok: bool
    provider: str
    requested_provider: str
    model: str | None
    effective_base_url: str | None
    effective_api_mode: ApiMode | None
    credential_source: str
    models_count: int | None
    message: str
    steps: list[ProviderTestStep]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "provider": self.provider,
            "requested_provider": self.requested_provider,
            "model": self.model,
            "effective_base_url": self.effective_base_url,
            "effective_api_mode": self.effective_api_mode,
            "credential_source": self.credential_source,
            "models_count": self.models_count,
            "message": self.message,
            "steps": [step.to_dict() for step in self.steps],
        }




def _normalize_openai_base_url(provider: str, base_url: str | None) -> str | None:
    if not base_url:
        return None
    clean = base_url.strip().rstrip("/")
    if provider == "openai" and looks_like_cliproxy_url(clean) and not clean.endswith("/v1"):
        return f"{clean}/v1"
    return clean


def _append_endpoint(base_url: str, endpoint: str) -> str:
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"




def _extract_models(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        raw_models = body
    elif isinstance(body, dict):
        raw_models = body.get("data") or body.get("models") or body.get("model_list") or []
    else:
        raw_models = []

    models: list[dict[str, Any]] = []
    if not isinstance(raw_models, list):
        return models
    for item in raw_models:
        if isinstance(item, str):
            models.append({"id": item, "name": item})
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("name") or item.get("model")
            if isinstance(model_id, str) and model_id.strip():
                model = dict(item)
                model["id"] = model_id.strip()
                models.append(model)
    return models


def _is_free_model(model: dict[str, Any] | None, *, local_provider: bool) -> bool | None:
    if local_provider:
        return True
    if not model:
        return None

    model_id = str(model.get("id") or "").lower()
    name = str(model.get("name") or "").lower()
    if model_id.endswith(":free") or " free" in f" {name}" or "free/" in model_id:
        return True

    pricing = model.get("pricing")
    if not isinstance(pricing, dict):
        return None
    prompt = first_float(pricing, "prompt")
    completion = first_float(pricing, "completion")
    if prompt is None and completion is None:
        return None
    return (prompt or 0) == 0 and (completion or 0) == 0


def _select_model(
    *,
    requested_model: str | None,
    default_model: str | None,
    models: list[dict[str, Any]],
    prefer_free: bool,
    local_provider: bool,
) -> tuple[str | None, dict[str, Any] | None]:
    if requested_model:
        found = next((model for model in models if model.get("id") == requested_model), None)
        return requested_model, found

    if prefer_free:
        free_model = next(
            (
                model
                for model in models
                if _is_free_model(model, local_provider=local_provider) is True
            ),
            None,
        )
        if free_model:
            return str(free_model["id"]), free_model

    if default_model:
        found = next((model for model in models if model.get("id") == default_model), None)
        return default_model, found

    if models:
        return str(models[0]["id"]), models[0]
    return None, None


def _headers(provider: str, api_key: str, *, api_format: str) -> dict[str, str]:
    if api_format == "anthropic_messages":
        return {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider == "openrouter":
        headers.update(_OPENROUTER_HEADERS)
    return headers


def _response_has_chat_text(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if not isinstance(first, dict):
            return False
        message = first.get("message") or {}
        if isinstance(message, dict) and message.get("content"):
            return True
        if first.get("text"):
            return True
    return False


def _response_has_responses_text(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    if body.get("output_text"):
        return True
    output = body.get("output")
    if not isinstance(output, list):
        return False
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, str) and content:
            return True
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and (part.get("text") or part.get("type") == "output_text"):
                return True
    return False


def _response_has_tool_call(body: Any, *, api_mode: ApiMode) -> bool:
    if not isinstance(body, dict):
        return False
    if api_mode == "responses":
        output = body.get("output")
        if not isinstance(output, list):
            return False
        return any(isinstance(item, dict) and item.get("type") == "function_call" for item in output)

    choices = body.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if isinstance(message, dict) and message.get("tool_calls"):
            return True
        if isinstance(message, dict) and message.get("function_call"):
            return True
    return False


async def _timed_get(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    secrets: tuple[str | None, ...],
) -> tuple[ProviderTestStep, Any | None]:
    started = time.perf_counter()
    try:
        response = await client.get(url, headers=headers)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            status = "failed" if response.status_code in {401, 403} else "warning"
            return (
                ProviderTestStep(
                    name="model_list",
                    status=status,
                    message=f"Provider /models returned HTTP {response.status_code}: {http_error_detail(response, *secrets)}",
                    url=url,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    error_type="http_error",
                ),
                None,
            )
        return (
            ProviderTestStep(
                name="model_list",
                status="passed",
                message="Provider /models endpoint returned successfully.",
                url=url,
                status_code=response.status_code,
                latency_ms=latency_ms,
            ),
            response.json(),
        )
    except httpx.TimeoutException:
        return (
            ProviderTestStep(
                name="model_list",
                status="warning",
                message="Provider /models did not respond before the timeout.",
                url=url,
                error_type="timeout",
            ),
            None,
        )
    except httpx.HTTPError as exc:
        return (
            ProviderTestStep(
                name="model_list",
                status="warning",
                message=redact_secrets(str(exc), *secrets)[:300],
                url=url,
                error_type=type(exc).__name__,
            ),
            None,
        )


async def _timed_post(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    step_name: str,
    success_message: str,
    validator: Any,
    secrets: tuple[str | None, ...],
) -> tuple[ProviderTestStep, Any | None]:
    started = time.perf_counter()
    try:
        response = await client.post(url, headers=headers, json=payload)
        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            return (
                ProviderTestStep(
                    name=step_name,
                    status="failed",
                    message=f"Provider returned HTTP {response.status_code}: {http_error_detail(response, *secrets)}",
                    url=url,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    error_type="http_error",
                ),
                None,
            )
        body = response.json()
        if not validator(body):
            return (
                ProviderTestStep(
                    name=step_name,
                    status="failed",
                    message="Provider returned HTTP 200 but the response shape did not match the expected API mode.",
                    url=url,
                    status_code=response.status_code,
                    latency_ms=latency_ms,
                    error_type="invalid_response_shape",
                ),
                body,
            )
        return (
            ProviderTestStep(
                name=step_name,
                status="passed",
                message=success_message,
                url=url,
                status_code=response.status_code,
                latency_ms=latency_ms,
            ),
            body,
        )
    except httpx.TimeoutException:
        return (
            ProviderTestStep(
                name=step_name,
                status="failed",
                message="Provider did not respond before the timeout.",
                url=url,
                error_type="timeout",
            ),
            None,
        )
    except httpx.HTTPError as exc:
        return (
            ProviderTestStep(
                name=step_name,
                status="failed",
                message=redact_secrets(str(exc), *secrets)[:300],
                url=url,
                error_type=type(exc).__name__,
            ),
            None,
        )


def _chat_payload(*, api_format: str, api_mode: ApiMode, model: str) -> dict[str, Any]:
    if api_format == "anthropic_messages":
        return {
            "model": model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply exactly: ok"}],
        }
    if api_mode == "responses":
        return {
            "model": model,
            "input": "Reply exactly: ok",
            "max_output_tokens": 16,
            "store": False,
        }
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Reply exactly: ok"}],
        "max_tokens": 16,
        "temperature": 0,
        "stream": False,
    }


def _tool_payload(*, api_mode: ApiMode, model: str) -> dict[str, Any]:
    parameters = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    if api_mode == "responses":
        return {
            "model": model,
            "input": "Call nymeria_provider_ping with ok=true.",
            "max_output_tokens": 32,
            "store": False,
            "tools": [
                {
                    "type": "function",
                    "name": "nymeria_provider_ping",
                    "description": "Provider compatibility ping tool.",
                    "parameters": parameters,
                    "strict": False,
                }
            ],
            "tool_choice": {"type": "function", "name": "nymeria_provider_ping"},
        }
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Call nymeria_provider_ping with ok=true."}],
        "max_tokens": 32,
        "temperature": 0,
        "stream": False,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "nymeria_provider_ping",
                    "description": "Provider compatibility ping tool.",
                    "parameters": parameters,
                },
            }
        ],
        "tool_choice": {
            "type": "function",
            "function": {"name": "nymeria_provider_ping"},
        },
    }


def _report(
    *,
    provider: str,
    requested_provider: str,
    model: str | None,
    base_url: str | None,
    api_mode: ApiMode | None,
    credential_source: str,
    models_count: int | None,
    steps: list[ProviderTestStep],
) -> ProviderTestSuiteReport:
    failed_steps = [step for step in steps if step.status == "failed"]
    required_skips = {
        step.name
        for step in steps
        if step.status == "skipped" and step.name in {"chat_completion", "tool_call"}
    }
    ok = not failed_steps and not required_skips
    if ok:
        message = "Provider passed the Nymeria compatibility suite."
    elif failed_steps:
        message = f"Provider failed {len(failed_steps)} required compatibility check(s)."
    else:
        message = "Provider setup is incomplete; required live checks were skipped."

    return ProviderTestSuiteReport(
        ok=ok,
        provider=provider,
        requested_provider=requested_provider,
        model=model,
        effective_base_url=base_url,
        effective_api_mode=api_mode,
        credential_source=credential_source,
        models_count=models_count,
        message=message,
        steps=steps,
    )


def _resolve_credentials(
    options: ProviderTestSuiteOptions, provider: str
) -> tuple[str | None, str | None, str]:
    """Resolve ``(api_key, base_url, credential_source)`` from the request, then
    the credential vault, then settings/env, mirroring runtime resolution. Pure
    resolution with no guards: the api-key-required guard and the
    ``not-required`` fallback stay inline in the caller. ``base_url`` is seeded
    from a vault credential only when the request did not supply one, and is
    returned so the caller can normalize it."""
    credential_source = "request" if options.api_key else "none"
    api_key = options.api_key
    base_url = options.base_url.strip().rstrip("/") if options.base_url else None

    if not api_key:
        credential = get_llm_provider_credential(
            provider,
            vault=options.vault,
            owner_user_id=options.owner_user_id,
        )
        if credential and credential.api_key:
            api_key = credential.api_key
            credential_source = f"vault:{credential.credential_id}"
            if not base_url and credential.base_url:
                base_url = credential.base_url

    if not api_key:
        api_key = resolve_provider_api_key(provider, settings=options.settings)
        if api_key:
            credential_source = "settings/env"

    return api_key, base_url, credential_source


def _resolve_effective_base_url(
    provider: str,
    base_url: str | None,
    options: ProviderTestSuiteOptions,
    *,
    is_known_openai: bool,
) -> tuple[str | None, str | None]:
    """Normalize the provider base URL. Returns ``(base_url, clean_base_url)``:
    the resolved-but-unnormalized ``base_url`` is still referenced by the
    redaction ``secrets`` tuple, while ``clean_base_url`` drives every endpoint."""
    if provider == "anthropic":
        base_url = (
            base_url
            or resolve_provider_base_url(provider, settings=options.settings)
            or "https://api.anthropic.com"
        )
        clean_base_url = base_url.strip().rstrip("/")
    else:
        if is_known_openai:
            base_url = resolve_provider_base_url(
                provider,
                configured_base_url=base_url,
                settings=options.settings,
            )
        clean_base_url = _normalize_openai_base_url(provider, base_url)
    return base_url, clean_base_url


async def _run_live_checks(
    client: httpx.AsyncClient,
    *,
    options: ProviderTestSuiteOptions,
    api_format: str,
    effective_api_mode: ApiMode,
    clean_base_url: str,
    selected_model: str,
    selected_metadata: dict[str, Any] | None,
    headers: dict[str, str],
    secrets: tuple[str | None, ...],
) -> list[ProviderTestStep]:
    """Run the optional chat-completion and tool-call checks against an
    already-open client. Returns the steps to append, in emission order. Reached
    only after a non-empty model has been selected (the caller's missing-model
    guard returns first)."""
    out: list[ProviderTestStep] = []
    chat_endpoint = (
        "v1/messages"
        if api_format == "anthropic_messages" and not clean_base_url.endswith("/v1")
        else "messages"
        if api_format == "anthropic_messages"
        else "responses"
        if effective_api_mode == "responses"
        else "chat/completions"
    )
    chat_url = _append_endpoint(clean_base_url, chat_endpoint)

    if options.run_chat_completion:
        validator = (
            _response_has_responses_text
            if effective_api_mode == "responses"
            else _response_has_chat_text
            if api_format == "openai_chat"
            else lambda body: isinstance(body, dict) and bool(body.get("content"))
        )
        step, _body = await _timed_post(
            client,
            chat_url,
            headers=headers,
            payload=_chat_payload(
                api_format=api_format,
                api_mode=effective_api_mode,
                model=selected_model,
            ),
            step_name="chat_completion",
            success_message="Provider returned a valid non-streaming chat response.",
            validator=validator,
            secrets=secrets,
        )
        out.append(step)

    if options.run_tool_call:
        if api_format != "openai_chat":
            out.append(
                ProviderTestStep(
                    name="tool_call",
                    status="skipped",
                    message="Tool-call suite currently covers OpenAI-compatible providers only.",
                )
            )
        else:
            supported_params = set(extract_model_metadata(selected_metadata or {}).get("supported_parameters") or [])
            if selected_metadata and supported_params and "tools" not in supported_params:
                out.append(
                    ProviderTestStep(
                        name="tool_call",
                        status="failed",
                        message="Selected model metadata does not advertise the Chat Completions tools parameter.",
                        error_type="tools_not_supported",
                        metadata={"supported_parameters": sorted(supported_params)},
                    )
                )
            else:
                step, _body = await _timed_post(
                    client,
                    chat_url,
                    headers=headers,
                    payload=_tool_payload(api_mode=effective_api_mode, model=selected_model),
                    step_name="tool_call",
                    success_message="Provider accepted a forced tool call and returned tool-call metadata.",
                    validator=lambda body: _response_has_tool_call(
                        body,
                        api_mode=effective_api_mode,
                    ),
                    secrets=secrets,
                )
                out.append(step)
    return out


async def run_provider_test_suite(
    options: ProviderTestSuiteOptions,
) -> ProviderTestSuiteReport:
    """Run a sanitized compatibility suite for one LLM provider setup."""
    requested_provider = options.provider
    provider = normalize_llm_provider(options.provider)
    spec = get_llm_provider_spec(provider)
    steps: list[ProviderTestStep] = []

    def _finish(
        *,
        model: str | None,
        base_url: str | None,
        api_mode: ApiMode | None,
        credential_source: str,
        models_count: int | None,
    ) -> ProviderTestSuiteReport:
        """Build the terminal report over the stable provider/steps context,
        collapsing the repeated ``_report(...)`` argument list at every return."""
        return _report(
            provider=provider,
            requested_provider=requested_provider,
            model=model,
            base_url=base_url,
            api_mode=api_mode,
            credential_source=credential_source,
            models_count=models_count,
            steps=steps,
        )

    is_known_openai = is_openai_compatible_provider(provider)
    api_format = spec.api_format if spec else "openai_chat"
    if provider == "anthropic":
        api_format = "anthropic_messages"

    if spec is None and not options.base_url:
        steps.append(
            ProviderTestStep(
                name="configuration",
                status="failed",
                message=(
                    f"Provider '{provider}' is not registered. Supply a base URL "
                    "to test it as a custom OpenAI-compatible endpoint."
                ),
                error_type="unknown_provider",
            )
        )
        return _finish(
            model=options.model,
            base_url=None,
            api_mode=None,
            credential_source="none",
            models_count=None,
        )

    api_key, base_url, credential_source = _resolve_credentials(options, provider)
    base_url, clean_base_url = _resolve_effective_base_url(
        provider, base_url, options, is_known_openai=is_known_openai
    )

    local_provider = base_url_allows_no_api_key(clean_base_url)
    if not api_key and provider_requires_api_key(provider) and not local_provider:
        steps.append(
            ProviderTestStep(
                name="authentication",
                status="failed",
                message="No API key was provided or found in the credential vault/settings/env.",
                error_type="missing_api_key",
            )
        )
        return _finish(
            model=options.model,
            base_url=clean_base_url,
            api_mode=None,
            credential_source=credential_source,
            models_count=None,
        )
    if not api_key:
        api_key = "not-needed"
        credential_source = "not-required"

    if not clean_base_url:
        steps.append(
            ProviderTestStep(
                name="configuration",
                status="failed",
                message="No API base URL is configured for this provider.",
                error_type="missing_base_url",
            )
        )
        return _finish(
            model=options.model,
            base_url=None,
            api_mode=None,
            credential_source=credential_source,
            models_count=None,
        )

    effective_api_mode: ApiMode = "chat_completions"
    if (
        options.api_mode == "responses"
        and api_format == "openai_chat"
        and (provider_supports_responses(provider) or (spec is None and bool(clean_base_url)))
    ):
        effective_api_mode = "responses"

    steps.append(
        ProviderTestStep(
            name="configuration",
            status="passed",
            message="Provider metadata, base URL, and credential source resolved.",
            metadata={
                "known_provider": spec is not None,
                "api_format": api_format,
                "credential_source": credential_source,
            },
        )
    )

    headers = _headers(provider, api_key, api_format=api_format)
    secrets = (api_key, options.api_key, base_url, clean_base_url)
    models: list[dict[str, Any]] = []
    models_count: int | None = None

    async with httpx.AsyncClient(timeout=options.timeout_seconds) as client:
        if options.run_model_list:
            if api_format == "anthropic_messages":
                models_url = (
                    _append_endpoint(clean_base_url, "models")
                    if clean_base_url.endswith("/v1")
                    else _append_endpoint(clean_base_url, "v1/models")
                )
            else:
                models_url = _append_endpoint(clean_base_url, "models")
            step, body = await _timed_get(client, models_url, headers=headers, secrets=secrets)
            models = _extract_models(body)
            models_count = len(models)
            if step.status == "passed":
                step.metadata["models_count"] = models_count
                if models_count == 0:
                    step.status = "warning"
                    step.message = "Provider /models responded but did not return any model IDs."
            steps.append(step)

        selected_model, selected_metadata = _select_model(
            requested_model=options.model.strip() if options.model else None,
            default_model=spec.default_model if spec else None,
            models=models,
            prefer_free=options.prefer_free_model,
            local_provider=local_provider,
        )
        if selected_model and local_provider:
            local_metadata = extract_model_metadata(selected_metadata or {})
            if not local_metadata.get("context_length"):
                server_type = detect_local_server_type(clean_base_url, api_key=api_key)
                detected_context = query_local_context_length(
                    selected_model,
                    clean_base_url,
                    api_key=api_key,
                    server_type=server_type,
                )
                if detected_context:
                    selected_metadata = {
                        **(selected_metadata or {"id": selected_model}),
                        "context_length": detected_context,
                    }
        if selected_metadata:
            metadata = extract_model_metadata(selected_metadata)
            register_model_metadata(
                model_id=selected_model or str(selected_metadata.get("id") or ""),
                name=str(selected_metadata.get("name") or selected_model or ""),
                context_length=metadata.get("context_length"),
                max_completion_tokens=metadata.get("max_completion_tokens"),
                input_modalities=set(metadata.get("input_modalities") or []),
                supported_parameters=set(metadata.get("supported_parameters") or []),
                default_temperature=metadata.get("default_temperature"),
                default_top_p=metadata.get("default_top_p"),
                default_frequency_penalty=metadata.get("default_frequency_penalty"),
                pricing_prompt=metadata.get("pricing_prompt"),
                pricing_completion=metadata.get("pricing_completion"),
                tokenizer=metadata.get("tokenizer"),
            )

        model_is_free = _is_free_model(selected_metadata, local_provider=local_provider)
        if selected_model:
            steps.append(
                ProviderTestStep(
                    name="model_selection",
                    status="passed",
                    message=f"Selected model '{selected_model}' for live checks.",
                    metadata={
                        "requested_model": bool(options.model),
                        "free_model": model_is_free,
                        **(extract_model_metadata(selected_metadata) if selected_metadata else {}),
                    },
                )
            )
        else:
            steps.append(
                ProviderTestStep(
                    name="model_selection",
                    status="failed",
                    message="No model was provided and no model could be selected from provider metadata.",
                    error_type="missing_model",
                )
            )
            return _finish(
                model=None,
                base_url=clean_base_url,
                api_mode=effective_api_mode,
                credential_source=credential_source,
                models_count=models_count,
            )

        allow_live_generation = (
            options.allow_billable
            or local_provider
            or model_is_free is True
            or not provider_requires_api_key(provider)
        )
        if options.run_chat_completion and not allow_live_generation:
            steps.append(
                ProviderTestStep(
                    name="chat_completion",
                    status="skipped",
                    message=(
                        "Skipped live generation because the selected model is not known to be free. "
                        "Set allow_billable=true to run the production smoke test."
                    ),
                    metadata={"free_model": model_is_free},
                )
            )
            if options.run_tool_call:
                steps.append(
                    ProviderTestStep(
                        name="tool_call",
                        status="skipped",
                        message="Skipped tool-call check because live generation was not allowed.",
                    )
                )
            return _finish(
                model=selected_model,
                base_url=clean_base_url,
                api_mode=effective_api_mode,
                credential_source=credential_source,
                models_count=models_count,
            )

        steps.extend(
            await _run_live_checks(
                client,
                options=options,
                api_format=api_format,
                effective_api_mode=effective_api_mode,
                clean_base_url=clean_base_url,
                selected_model=selected_model,
                selected_metadata=selected_metadata,
                headers=headers,
                secrets=secrets,
            )
        )

    return _finish(
        model=selected_model,
        base_url=clean_base_url,
        api_mode=effective_api_mode,
        credential_source=credential_source,
        models_count=models_count,
    )
