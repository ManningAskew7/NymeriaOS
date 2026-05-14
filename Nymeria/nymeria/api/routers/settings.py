"""Global settings and model-catalog routes."""

import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from ...config import Settings
from ...config.llm_providers import (
    is_openai_compatible_provider,
    list_llm_provider_specs,
    normalize_llm_provider,
    provider_requires_api_key,
    provider_supports_responses,
    resolve_provider_api_key,
    resolve_provider_base_url,
)
from ...config.settings import get_env_file_paths, get_env_write_path
from ...config.model_capabilities import (
    get_max_output_tokens,
    list_all_models,
    register_model_metadata,
)
from ...core.accounts import AuthenticatedUser
from ...core.llm_credentials import get_llm_provider_credential
from ...vendor.react_agent.cliproxy import looks_like_cliproxy_url
from ..schemas.settings import (
    HIDDEN_CONFIG_SETTINGS,
    LLMProviderSpecResponse,
    LLMProviderTestRequest,
    LLMProviderTestResponse,
    LLMRuntimeDiagnosticsResponse,
    OpenRouterKeyDiagnostics,
    ServerSettingsResponse,
    ServerSettingsUpdate,
)

logger = logging.getLogger(__name__)
_LOCAL_MODEL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}


def _env_mapping() -> dict[str, str]:
    """Return settings-field to environment-variable mapping for PATCH /settings."""
    return {
        "llm_provider": "LLM_PROVIDER",
        "llm_model": "LLM_MODEL",
        "llm_fast_model": "LLM_FAST_MODEL",
        "llm_fallback_models": "LLM_FALLBACK_MODELS",
        "llm_temperature": "LLM_TEMPERATURE",
        "llm_max_tokens": "LLM_MAX_TOKENS",
        "llm_top_p": "LLM_TOP_P",
        "llm_top_k": "LLM_TOP_K",
        "llm_frequency_penalty": "LLM_FREQUENCY_PENALTY",
        "llm_presence_penalty": "LLM_PRESENCE_PENALTY",
        "llm_reasoning_effort": "LLM_REASONING_EFFORT",
        "llm_extended_thinking": "LLM_EXTENDED_THINKING",
        "llm_use_model_defaults": "LLM_USE_MODEL_DEFAULTS",
        "llm_base_url": "LLM_BASE_URL",
        "openai_api_mode": "OPENAI_API_MODE",
        "llm_stream_max_retries": "LLM_STREAM_MAX_RETRIES",
        "llm_stream_retry_initial_delay": "LLM_STREAM_RETRY_INITIAL_DELAY",
        "llm_stream_retry_max_delay": "LLM_STREAM_RETRY_MAX_DELAY",
        "context_management": "CONTEXT_MANAGEMENT",
        "compact_threshold": "COMPACT_THRESHOLD",
        "compact_keep_messages": "COMPACT_KEEP_MESSAGES",
        "compact_model": "COMPACT_MODEL",
        "sliding_window_cycles": "SLIDING_WINDOW_CYCLES",
        "tool_output_max_chars": "TOOL_OUTPUT_MAX_CHARS",
        "log_level": "LOG_LEVEL",
        "watchdog_enabled": "WATCHDOG_ENABLED",
        "watchdog_interval_minutes": "WATCHDOG_INTERVAL_MINUTES",
        "todo_staleness_minutes": "TODO_STALENESS_MINUTES",
        "activity_retention_hours": "ACTIVITY_RETENTION_HOURS",
        "tts_provider": "TTS_PROVIDER",
        "tts_base_url": "TTS_BASE_URL",
        "tts_model": "TTS_MODEL",
        "tts_voice": "TTS_VOICE",
        "tts_output_format": "TTS_OUTPUT_FORMAT",
        "tts_speed": "TTS_SPEED",
        "stt_provider": "STT_PROVIDER",
        "stt_base_url": "STT_BASE_URL",
        "stt_model": "STT_MODEL",
        "stt_language": "STT_LANGUAGE",
        "voice_default_thread_id": "VOICE_DEFAULT_THREAD_ID",
        "perplexity_api_key": "PERPLEXITY_API_KEY",
        "perplexity_search_model": "PERPLEXITY_SEARCH_MODEL",
        "wolfram_alpha_app_id": "WOLFRAM_ALPHA_APP_ID",
        "searxng_base_url": "SEARXNG_BASE_URL",
        "nasa_api_key": "NASA_API_KEY",
        "openweathermap_api_key": "OPENWEATHERMAP_API_KEY",
        "npm_registry_url": "NPM_REGISTRY_URL",
        "github_token": "GITHUB_TOKEN",
        "github_api_base_url": "GITHUB_API_BASE_URL",
        "gitlab_token": "GITLAB_TOKEN",
        "gitlab_base_url": "GITLAB_BASE_URL",
        "bitly_token": "BITLY_TOKEN",
        "bitly_base_url": "BITLY_BASE_URL",
        "brandfetch_api_key": "BRANDFETCH_API_KEY",
        "brandfetch_base_url": "BRANDFETCH_BASE_URL",
        "marketstack_api_key": "MARKETSTACK_API_KEY",
        "marketstack_base_url": "MARKETSTACK_BASE_URL",
        "deepl_api_key": "DEEPL_API_KEY",
        "deepl_api_plan": "DEEPL_API_PLAN",
        "deepl_base_url": "DEEPL_BASE_URL",
        "todoist_api_key": "TODOIST_API_KEY",
        "todoist_base_url": "TODOIST_BASE_URL",
        "trello_api_key": "TRELLO_API_KEY",
        "trello_api_token": "TRELLO_API_TOKEN",
        "trello_base_url": "TRELLO_BASE_URL",
        "asana_access_token": "ASANA_ACCESS_TOKEN",
        "asana_base_url": "ASANA_BASE_URL",
        "linear_api_key": "LINEAR_API_KEY",
        "linear_api_url": "LINEAR_API_URL",
        "jira_email": "JIRA_EMAIL",
        "jira_api_token": "JIRA_API_TOKEN",
        "jira_access_token": "JIRA_ACCESS_TOKEN",
        "jira_base_url": "JIRA_BASE_URL",
        "clickup_access_token": "CLICKUP_ACCESS_TOKEN",
        "clickup_base_url": "CLICKUP_BASE_URL",
        "slack_bot_token": "SLACK_BOT_TOKEN",
        "slack_access_token": "SLACK_ACCESS_TOKEN",
        "slack_base_url": "SLACK_BASE_URL",
        "notion_api_key": "NOTION_API_KEY",
        "notion_version": "NOTION_VERSION",
        "notion_base_url": "NOTION_BASE_URL",
        "airtable_access_token": "AIRTABLE_ACCESS_TOKEN",
        "airtable_api_key": "AIRTABLE_API_KEY",
        "airtable_base_url": "AIRTABLE_BASE_URL",
        "openai_api_key": "OPENAI_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "anthropic_direct_api_key": "ANTHROPIC_DIRECT_API_KEY",
        "openrouter_api_key": "OPENROUTER_API_KEY",
        "embedding_api_key": "EMBEDDING_API_KEY",
        "embedding_base_url": "EMBEDDING_BASE_URL",
        "embedding_model": "EMBEDDING_MODEL",
        "gemini_api_key": "GEMINI_API_KEY",
        "gemini_extraction_model": "GEMINI_EXTRACTION_MODEL",
        "_prv_a_service_account_file": "_PRV_A_SERVICE_ACCOUNT_FILE",
        "user_timezone": "USER_TIMEZONE",
        "ticker_poll_interval": "TICKER_POLL_INTERVAL",
        "max_concurrent_autonomous": "MAX_CONCURRENT_AUTONOMOUS",
        "tool_timeout": "TOOL_TIMEOUT",
        "lock_timeout": "LOCK_TIMEOUT",
        "todo_auto_archive_days": "TODO_AUTO_ARCHIVE_DAYS",
        "redis_url": "REDIS_URL",
        "redis_enabled": "REDIS_ENABLED",
        "postgres_uri": "POSTGRES_URI",
        "nymeria_data_dir": "NYMERIA_DATA_DIR",
        "discord_bot_token": "DISCORD_BOT_TOKEN",
        "discord_webhook_url": "DISCORD_WEBHOOK_URL",
        "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
        "telegram_default_chat_id": "TELEGRAM_DEFAULT_CHAT_ID",
    }


def _restart_required_keys() -> set[str]:
    """Settings that persist immediately but require process restart to apply."""
    return {
        "redis_url",
        "redis_enabled",
        "postgres_uri",
        "nymeria_data_dir",
        "discord_bot_token",
        "discord_webhook_url",
        "telegram_bot_token",
        "telegram_default_chat_id",
    }


def _env_categories() -> dict[str, list[str]]:
    """Return grouped admin environment entries for GET /settings/env."""
    return {
        "LLM": [
            "llm_provider",
            "llm_model",
            "llm_fast_model",
            "llm_fallback_models",
            "llm_temperature",
            "llm_max_tokens",
            "llm_top_p",
            "llm_top_k",
            "llm_frequency_penalty",
            "llm_presence_penalty",
            "llm_reasoning_effort",
            "llm_extended_thinking",
            "llm_use_model_defaults",
            "llm_base_url",
            "openai_api_mode",
            "llm_stream_max_retries",
            "llm_stream_retry_initial_delay",
            "llm_stream_retry_max_delay",
        ],
        "API Keys": [
            "openai_api_key",
            "anthropic_api_key",
            "anthropic_direct_api_key",
            "openrouter_api_key",
            "embedding_api_key",
            "perplexity_api_key",
            "perplexity_search_model",
            "gemini_api_key",
            "gemini_extraction_model",
        ],
        "Context": [
            "context_management",
            "compact_threshold",
            "compact_keep_messages",
            "compact_model",
            "sliding_window_cycles",
        ],
        "System": [
            "log_level",
            "watchdog_enabled",
            "watchdog_interval_minutes",
            "user_timezone",
            "nymeria_data_dir",
            "tool_timeout",
            "tool_output_max_chars",
            "lock_timeout",
        ],
        "Tasks": [
            "ticker_poll_interval",
            "max_concurrent_autonomous",
            "todo_staleness_minutes",
            "todo_auto_archive_days",
            "activity_retention_hours",
        ],
        "Voice": [
            "tts_provider",
            "tts_base_url",
            "tts_api_key",
            "tts_model",
            "tts_voice",
            "tts_output_format",
            "tts_speed",
            "stt_provider",
            "stt_base_url",
            "stt_api_key",
            "stt_model",
            "stt_language",
            "voice_default_thread_id",
        ],
        "Infrastructure": [
            "redis_url",
            "redis_enabled",
            "postgres_uri",
        ],
        "Discord": [
            "discord_bot_token",
            "discord_webhook_url",
        ],
        "Telegram": [
            "telegram_bot_token",
            "telegram_default_chat_id",
        ],
        "Twitch": [
            "twitch_client_id",
            "twitch_client_secret",
            "twitch_bot_access_token",
            "twitch_bot_refresh_token",
            "twitch_bot_user_id",
            "twitch_broadcaster_token",
            "twitch_broadcaster_refresh_token",
            "twitch_channel",
            "twitch_system_prompt",
            "twitch_buffer_size",
            "twitch_pulse_enabled",
            "twitch_pulse_interval",
            "twitch_respond_mode",
        ],
    }


def _secret_keys() -> set[str]:
    """Settings that should be masked in GET /settings/env."""
    return {
        "openai_api_key",
        "anthropic_api_key",
        "anthropic_direct_api_key",
        "openrouter_api_key",
        "embedding_api_key",
        "perplexity_api_key",
        "gemini_api_key",
        "discord_bot_token",
        "discord_webhook_url",
        "telegram_bot_token",
        "twitch_client_secret",
        "twitch_bot_access_token",
        "twitch_bot_refresh_token",
        "twitch_broadcaster_token",
        "twitch_broadcaster_refresh_token",
        "slack_bot_token",
        "postgres_uri",
        "redis_url",
        "tts_api_key",
        "stt_api_key",
        "fcm_credentials_json",
    }


def _fallback_model_list(value: Any) -> list[str]:
    """Return a normalized fallback model list for settings responses."""
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = [value]
    models: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        model = str(item or "").strip()
        if not model or model in seen:
            continue
        seen.add(model)
        models.append(model)
    return models


def _mask_value(val: str) -> str:
    """Mask a secret value, showing first 4 and last 3 chars."""
    s = str(val)
    if len(s) <= 10:
        return s[:2] + "..." + s[-1:] if len(s) > 3 else "***"
    return s[:4] + "..." + s[-3:]


def _sync_process_env(new_lines: list[str], mapped_env_vars: set[str]) -> None:
    """Sync mapped dotenv values into os.environ after a settings update."""
    for line in new_lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            if key in mapped_env_vars:
                os.environ[key] = val


def _clear_settings_cache(get_settings_fn: Callable[[], Any]) -> None:
    cache_clear = getattr(get_settings_fn, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()


def _redact_secrets(text: str, *secrets: str | None) -> str:
    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted


def _http_error_detail(response: httpx.Response, *secrets: str | None) -> str:
    try:
        body = response.json()
    except ValueError:
        text = response.text.strip()
        return _redact_secrets(text, *secrets)[:300] or response.reason_phrase

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

    return _redact_secrets(message or response.reason_phrase, *secrets)[:300]


def _normalize_openai_test_base_url(provider: str, base_url: str | None) -> str:
    provider = normalize_llm_provider(provider)
    if not base_url:
        resolved = resolve_provider_base_url(provider)
        if resolved:
            return resolved
        if provider == "openrouter":
            return "https://openrouter.ai/api/v1"
        return "https://api.openai.com/v1"

    clean = base_url.strip().rstrip("/")
    if provider == "openai" and looks_like_cliproxy_url(clean) and not clean.endswith("/v1"):
        return f"{clean}/v1"
    return clean


def _base_url_allows_no_api_key(base_url: str | None) -> bool:
    if not base_url:
        return False
    parse_target = base_url if "://" in base_url else f"http://{base_url}"
    try:
        parsed = urlparse(parse_target)
    except ValueError:
        return False
    return (parsed.hostname or "").lower() in _LOCAL_MODEL_HOSTS


def _first_int(source: dict[str, Any], *keys: str) -> int | None:
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


def _first_float(source: dict[str, Any], *keys: str) -> float | None:
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


def _extract_model_metadata(model: dict[str, Any]) -> dict[str, Any]:
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
        _first_int(
            model,
            "context_length",
            "context_window",
            "context_size",
            "max_context_length",
            "max_context_tokens",
            "input_token_limit",
            "max_input_tokens",
        )
        or _first_int(top_provider, "context_length", "max_context_tokens")
    )
    max_completion_tokens = (
        _first_int(
            model,
            "max_completion_tokens",
            "max_output_tokens",
            "output_token_limit",
        )
        or _first_int(top_provider, "max_completion_tokens", "max_output_tokens")
    )

    raw_supported = model.get("supported_parameters") or model.get("supported_params") or []
    if not isinstance(raw_supported, (list, tuple, set)):
        raw_supported = []
    supported_parameters = [
        str(param)
        for param in raw_supported
        if param
    ]
    raw_modalities = (
        model.get("input_modalities")
        or architecture.get("input_modalities")
        or model.get("modalities")
        or []
    )
    if not isinstance(raw_modalities, (list, tuple, set)):
        raw_modalities = []
    input_modalities = [
        str(modality)
        for modality in raw_modalities
        if modality
    ]

    return {
        "context_length": context_length,
        "max_completion_tokens": max_completion_tokens,
        "supported_parameters": supported_parameters,
        "input_modalities": input_modalities,
        "tokenizer": architecture.get("tokenizer") or model.get("tokenizer"),
        "default_temperature": _first_float(defaults, "temperature"),
        "default_top_p": _first_float(defaults, "top_p"),
        "default_frequency_penalty": _first_float(defaults, "frequency_penalty"),
        "pricing_prompt": _first_float(pricing, "prompt"),
        "pricing_completion": _first_float(pricing, "completion"),
    }


async def _post_llm_test_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> None:
    request_headers = {
        "Content-Type": "application/json",
        **headers,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, headers=request_headers, json=payload)
        response.raise_for_status()


async def _test_llm_provider_config(
    request: LLMProviderTestRequest,
) -> LLMProviderTestResponse:
    provider = normalize_llm_provider(request.llm_provider)
    model = request.llm_model
    api_key = request.api_key.get_secret_value()
    base_url = request.llm_base_url
    openai_api_mode = request.openai_api_mode or "responses"

    if provider == "anthropic":
        clean_base = base_url or "https://api.anthropic.com"
        url = f"{clean_base}/v1/messages"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        if base_url:
            headers["User-Agent"] = "claude-cli/2.1.113"
        payload = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "Reply with ok."}],
        }
        response_api_mode = None
    else:
        if not is_openai_compatible_provider(provider) and not base_url:
            return LLMProviderTestResponse(
                ok=False,
                provider=provider,
                model=model,
                openai_api_mode=None,
                message=(
                    f"Provider '{provider}' is not in Nymeria's OpenAI-compatible "
                    "registry. Provide an API base URL to test it as a custom endpoint."
                ),
                error_type="unknown_provider",
            )
        clean_base = _normalize_openai_test_base_url(provider, base_url)
        headers = {"Authorization": f"Bearer {api_key}"}
        if provider == "openrouter":
            headers.update({
                "HTTP-Referer": "https://github.com/ManningAskew7/NymeriaOS",
                "X-Title": "Nymeria",
            })

        effective_api_mode = (
            openai_api_mode
            if openai_api_mode == "responses"
            and (
                provider_supports_responses(provider)
                or bool(base_url and not is_openai_compatible_provider(provider))
            )
            else "chat_completions"
        )

        if effective_api_mode == "chat_completions":
            url = f"{clean_base}/chat/completions"
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with ok."}],
                "max_tokens": 16,
            }
        else:
            url = f"{clean_base}/responses"
            payload = {
                "model": model,
                "input": "Reply with ok.",
                "max_output_tokens": 16,
            }
        response_api_mode = effective_api_mode

    try:
        await _post_llm_test_json(url, headers=headers, payload=payload)
    except httpx.TimeoutException:
        logger.info("LLM provider test timed out: provider=%s", provider)
        return LLMProviderTestResponse(
            ok=False,
            provider=provider,
            model=model,
            openai_api_mode=response_api_mode,
            message="Provider did not respond before the 15s timeout.",
            error_type="timeout",
        )
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        logger.info(
            "LLM provider test returned HTTP error: provider=%s status=%s",
            provider,
            status_code,
        )
        detail = _http_error_detail(exc.response, api_key, base_url)
        return LLMProviderTestResponse(
            ok=False,
            provider=provider,
            model=model,
            openai_api_mode=response_api_mode,
            message=f"Provider returned HTTP {status_code}: {detail}",
            status_code=status_code,
            error_type="http_error",
        )
    except httpx.HTTPError as exc:
        logger.info(
            "LLM provider test transport error: provider=%s error=%s",
            provider,
            type(exc).__name__,
        )
        return LLMProviderTestResponse(
            ok=False,
            provider=provider,
            model=model,
            openai_api_mode=response_api_mode,
            message=_redact_secrets(str(exc), api_key, base_url)[:300],
            error_type=type(exc).__name__,
        )

    return LLMProviderTestResponse(
        ok=True,
        provider=provider,
        model=model,
        openai_api_mode=response_api_mode,
        message="Provider test succeeded.",
    )


def create_settings_router(
    verify_api_key: Callable[..., Any],
    require_admin_user: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Any],
) -> APIRouter:
    """Create the settings/model-catalog router with app dependencies injected."""
    router = APIRouter(tags=["Settings"])

    @router.get("/settings", response_model=ServerSettingsResponse)
    async def get_server_settings(
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Get current server settings."""
        return ServerSettingsResponse(
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model,
            llm_fast_model=settings.llm_fast_model,
            llm_fallback_models=_fallback_model_list(settings.llm_fallback_models),
            llm_temperature=settings.llm_temperature,
            llm_max_tokens=settings.llm_max_tokens,
            llm_top_p=settings.llm_top_p,
            llm_top_k=settings.llm_top_k,
            llm_frequency_penalty=settings.llm_frequency_penalty,
            llm_presence_penalty=settings.llm_presence_penalty,
            llm_reasoning_effort=settings.llm_reasoning_effort,
            llm_extended_thinking=settings.llm_extended_thinking,
            llm_use_model_defaults=settings.llm_use_model_defaults,
            llm_base_url=settings.llm_base_url,
            openai_api_mode=settings.openai_api_mode,
            llm_stream_max_retries=settings.llm_stream_max_retries,
            llm_stream_retry_initial_delay=settings.llm_stream_retry_initial_delay,
            llm_stream_retry_max_delay=settings.llm_stream_retry_max_delay,
            context_management=settings.context_management,
            compact_threshold=settings.compact_threshold,
            compact_keep_messages=settings.compact_keep_messages,
            compact_model=settings.compact_model,
            sliding_window_cycles=settings.sliding_window_cycles,
            tool_output_max_chars=settings.tool_output_max_chars,
            log_level=settings.log_level,
            watchdog_enabled=settings.watchdog_enabled,
            watchdog_interval_minutes=settings.watchdog_interval_minutes,
            todo_staleness_minutes=settings.todo_staleness_minutes,
            activity_retention_hours=settings.activity_retention_hours,
            tts_provider=settings.tts_provider,
            tts_base_url=settings.tts_base_url,
            tts_model=settings.tts_model,
            tts_voice=settings.tts_voice,
            tts_output_format=settings.tts_output_format,
            tts_speed=settings.tts_speed,
            stt_provider=settings.stt_provider,
            stt_base_url=settings.stt_base_url,
            stt_model=settings.stt_model,
            stt_language=settings.stt_language,
            voice_default_thread_id=settings.voice_default_thread_id,
        )

    @router.post("/settings/llm/test", response_model=LLMProviderTestResponse)
    async def test_llm_provider_config(
        request: LLMProviderTestRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Test an arbitrary LLM provider configuration without writing it."""
        return await _test_llm_provider_config(request)

    @router.get(
        "/settings/llm/providers",
        response_model=list[LLMProviderSpecResponse],
    )
    async def get_llm_provider_catalog(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Return known LLM provider compatibility metadata."""
        return [
            LLMProviderSpecResponse(
                id=spec.id,
                label=spec.label,
                api_format=spec.api_format,
                default_base_url=spec.default_base_url,
                api_key_env_vars=list(spec.api_key_env_vars),
                base_url_env_vars=list(spec.base_url_env_vars),
                default_model=spec.default_model,
                default_api_mode=spec.default_api_mode,
                supports_chat_completions=spec.supports_chat_completions,
                supports_responses=spec.supports_responses,
                requires_api_key=spec.requires_api_key,
                requires_base_url=spec.requires_base_url,
                docs_url=spec.docs_url,
                notes=spec.notes,
                aliases=list(spec.aliases),
            )
            for spec in list_llm_provider_specs()
        ]

    @router.get(
        "/settings/llm/runtime",
        response_model=LLMRuntimeDiagnosticsResponse,
    )
    async def get_llm_runtime_diagnostics(
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Get runtime LLM diagnostics including active OpenRouter key budget details."""
        agent = get_agent_fn()
        llm_cfg = agent._get_llm_config_for_thread("")

        effective_max_tokens = llm_cfg.max_tokens
        if effective_max_tokens is None and llm_cfg.provider == "openrouter":
            try:
                effective_max_tokens = get_max_output_tokens(llm_cfg.model)
            except Exception as e:
                logger.warning("Failed to resolve OpenRouter max output tokens: %s", e)

        source_env_files = [
            str(path)
            for path in get_env_file_paths(settings.project_root)
            if path.exists()
        ]

        response = LLMRuntimeDiagnosticsResponse(
            provider=llm_cfg.provider,
            model=llm_cfg.model,
            llm_max_tokens=settings.llm_max_tokens,
            effective_max_tokens=effective_max_tokens,
            source_env_files=source_env_files,
        )

        if llm_cfg.provider == "openrouter":
            if not llm_cfg.api_key:
                response.openrouter = OpenRouterKeyDiagnostics(
                    fetch_error="OPENROUTER_API_KEY is missing in active runtime settings"
                )
                return response

            request_obj = urllib.request.Request(
                "https://openrouter.ai/api/v1/key",
                headers={
                    "Authorization": f"Bearer {llm_cfg.api_key}",
                    "Content-Type": "application/json",
                },
            )

            try:
                with urllib.request.urlopen(request_obj, timeout=6) as api_response:
                    payload = json.loads(api_response.read().decode("utf-8"))
                data = payload.get("data", {}) if isinstance(payload, dict) else {}

                if isinstance(data, dict):
                    response.openrouter = OpenRouterKeyDiagnostics(
                        label=data.get("label"),
                        limit=data.get("limit"),
                        limit_remaining=data.get("limit_remaining"),
                        usage=data.get("usage"),
                        limit_reset=data.get("limit_reset"),
                        include_byok_in_limit=data.get("include_byok_in_limit"),
                        is_management_key=data.get("is_management_key"),
                    )
                else:
                    response.openrouter = OpenRouterKeyDiagnostics(
                        fetch_error="Unexpected response shape from OpenRouter /key endpoint"
                    )
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="ignore")
                response.openrouter = OpenRouterKeyDiagnostics(
                    fetch_error=f"HTTP {e.code}: {body[:300]}"
                )
            except Exception as e:
                response.openrouter = OpenRouterKeyDiagnostics(
                    fetch_error=f"{type(e).__name__}: {e}"
                )

        return response

    @router.patch("/settings")
    async def update_server_settings(
        updates: ServerSettingsUpdate,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """
        Update server settings with hot-reload. Admin-only because settings are
        global and can include provider credentials.
        """
        env_path = get_env_write_path(settings.project_root)

        existing_lines = []
        if env_path.exists():
            existing_lines = env_path.read_text(encoding="utf-8").splitlines()

        env_mapping = _env_mapping()
        updates_dict = {
            k: v for k, v in updates.model_dump().items() if v is not None
        }

        if not updates_dict:
            return {"message": "No updates provided", "restart_required": False}

        updated_vars = set()
        new_lines = []

        for line in existing_lines:
            updated = False
            for setting_name, env_var in env_mapping.items():
                if setting_name in updates_dict and line.startswith(f"{env_var}="):
                    value = updates_dict[setting_name]
                    if isinstance(value, bool):
                        value = str(value).lower()
                    new_lines.append(f"{env_var}={value}")
                    updated_vars.add(setting_name)
                    updated = True
                    break

            if not updated:
                new_lines.append(line)

        for setting_name, value in updates_dict.items():
            if setting_name not in updated_vars:
                env_var = env_mapping.get(setting_name)
                if env_var:
                    if isinstance(value, bool):
                        value = str(value).lower()
                    new_lines.append(f"{env_var}={value}")

        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        _sync_process_env(new_lines, set(env_mapping.values()))

        _clear_settings_cache(get_settings_fn)
        new_settings = get_settings_fn()
        logger.info(
            "[SETTINGS] After hot-reload: TTS_PROVIDER=%s, STT_PROVIDER=%s, "
            "env TTS_PROVIDER=%s",
            new_settings.tts_provider,
            new_settings.stt_provider,
            os.environ.get("TTS_PROVIDER"),
        )

        agent = get_agent_fn()
        agent.settings = new_settings

        llm_fields = {
            "llm_provider",
            "llm_model",
            "llm_fast_model",
            "llm_fallback_models",
            "llm_temperature",
            "llm_max_tokens",
            "llm_top_p",
            "llm_top_k",
            "llm_frequency_penalty",
            "llm_presence_penalty",
            "llm_reasoning_effort",
            "llm_extended_thinking",
            "llm_use_model_defaults",
            "llm_base_url",
            "openai_api_mode",
            "llm_stream_max_retries",
            "llm_stream_retry_initial_delay",
            "llm_stream_retry_max_delay",
        }
        graph_fields = llm_fields | {"tool_output_max_chars"}
        llm_credential_fields = {
            "anthropic_api_key",
            "anthropic_direct_api_key",
            "openai_api_key",
            "openrouter_api_key",
        }
        graph_fields = graph_fields | llm_credential_fields
        if graph_fields & set(updates_dict.keys()):
            with agent._graph_cache_lock:
                agent._user_graphs.clear()
                agent._async_user_graphs.clear()
            agent._default_graph = agent._build_graph_with_prompt(
                agent._base_system_prompt
            )
            agent._default_async_graph = agent._build_async_graph_with_prompt(
                agent._base_system_prompt
            )
            logger.info(
                "Hot-reloaded graph settings: %s",
                graph_fields & set(updates_dict.keys()),
            )

        needs_restart = bool(_restart_required_keys() & set(updates_dict.keys()))
        return {
            "message": "Settings updated and applied" + (
                " (some changes require /restart api to take effect)"
                if needs_restart
                else ""
            ),
            "updated": list(updates_dict.keys()),
            "restart_required": needs_restart,
        }

    @router.get("/settings/env")
    async def get_env_vars(
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Get settable environment variables with masked sensitive values."""
        entries = []
        secret_keys = _secret_keys()
        for category, keys in _env_categories().items():
            for key in keys:
                if key in HIDDEN_CONFIG_SETTINGS:
                    continue
                val = getattr(settings, key, None)
                env_var = key.upper()
                is_secret = key in secret_keys
                display_val = None
                if val is not None:
                    display_val = _mask_value(str(val)) if is_secret else str(val)
                entries.append({
                    "name": key,
                    "env_var": env_var,
                    "value": display_val,
                    "is_set": val is not None and str(val) != "",
                    "is_secret": is_secret,
                    "category": category,
                })

        return {"entries": entries}

    @router.get("/settings/env/{key}")
    async def get_env_var(
        key: str,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Get a single environment variable's unmasked value. Admin-only."""
        key_lower = key.lower()
        if key_lower in HIDDEN_CONFIG_SETTINGS:
            raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")

        val = getattr(settings, key, None)
        if val is None:
            val = getattr(settings, key_lower, None)
            if val is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Unknown setting: {key}",
                )
            key = key_lower
        return {
            "name": key,
            "env_var": key.upper(),
            "value": str(val) if val is not None else None,
        }

    @router.get("/models")
    async def get_openrouter_models(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Return cached OpenRouter model metadata for frontend enrichment."""
        models = list_all_models()
        return [
            {
                "id": m.id,
                "name": m.name,
                "context_length": m.context_length,
                "max_completion_tokens": m.max_completion_tokens,
                "pricing_prompt": m.pricing_prompt,
                "pricing_completion": m.pricing_completion,
                "supported_parameters": sorted(m.supported_parameters),
                "input_modalities": sorted(m.input_modalities),
                "tokenizer": m.tokenizer,
                "default_temperature": m.default_temperature,
                "default_top_p": m.default_top_p,
                "default_frequency_penalty": m.default_frequency_penalty,
            }
            for m in models
        ]

    @router.get("/models/available")
    async def get_available_models(
        provider: Optional[str] = Query(
            default=None,
            description=(
                "Provider to fetch models for. Defaults to global provider."
            ),
        ),
        base_url: Optional[str] = Query(
            default=None,
            description="Optional OpenAI-compatible base URL override for unsaved provider settings.",
        ),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Fetch available models from the configured LLM provider or CLIProxy."""
        effective_provider = normalize_llm_provider(provider or settings.llm_provider)
        agent = get_agent_fn()
        credential = get_llm_provider_credential(
            effective_provider,
            vault=getattr(agent, "credential_vault", None),
            owner_user_id=user.id,
        )

        effective_base_url = base_url
        api_key = credential.api_key if credential else None
        if effective_base_url:
            effective_base_url = effective_base_url.strip().rstrip("/")
        if (
            not effective_base_url
            and effective_provider == normalize_llm_provider(settings.llm_provider)
        ):
            effective_base_url = settings.llm_base_url
        if not effective_base_url and credential and credential.base_url:
            effective_base_url = credential.base_url

        if effective_provider == "anthropic":
            api_key = api_key or (
                settings.anthropic_direct_api_key or settings.anthropic_api_key
            )
            effective_base_url = effective_base_url or "https://api.anthropic.com"
            clean_base = effective_base_url.rstrip("/")
            models_url = (
                f"{clean_base}/models"
                if clean_base.endswith("/v1")
                else f"{clean_base}/v1/models"
            )
        elif is_openai_compatible_provider(effective_provider):
            api_key = api_key or resolve_provider_api_key(
                effective_provider,
                settings=settings,
            )
            effective_base_url = effective_base_url or resolve_provider_base_url(
                effective_provider,
                settings=settings,
            )
            if not effective_base_url:
                return []
            models_url = f"{effective_base_url.rstrip('/')}/models"
        else:
            return []

        if (
            not api_key
            and provider_requires_api_key(effective_provider)
            and not _base_url_allows_no_api_key(effective_base_url)
        ):
            return []
        if not api_key:
            api_key = "not-needed"

        headers = {
            "x-api-key": api_key,
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(models_url, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            raw_models = data.get("data", [])
            result = []
            for m in sorted(raw_models, key=lambda x: x.get("id", "")):
                model_id = m.get("id", "")
                if not model_id:
                    continue
                model_name = m.get("name") or model_id
                metadata = _extract_model_metadata(m)
                register_model_metadata(
                    model_id=model_id,
                    name=model_name,
                    context_length=metadata["context_length"],
                    max_completion_tokens=metadata["max_completion_tokens"],
                    input_modalities=set(metadata["input_modalities"]),
                    supported_parameters=set(metadata["supported_parameters"]),
                    default_temperature=metadata["default_temperature"],
                    default_top_p=metadata["default_top_p"],
                    default_frequency_penalty=metadata["default_frequency_penalty"],
                    pricing_prompt=metadata["pricing_prompt"],
                    pricing_completion=metadata["pricing_completion"],
                    tokenizer=metadata["tokenizer"],
                )
                result.append({
                    "id": model_id,
                    "name": model_name,
                    "owned_by": m.get("owned_by", ""),
                    "created": m.get("created"),
                    **metadata,
                })
            return result
        except Exception as e:
            logger.warning("Failed to fetch models from %s: %s", models_url, e)
            return []

    return router
