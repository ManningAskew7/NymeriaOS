"""Global settings and model-catalog routes."""

import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from ...config import Settings
from ...config.model_capabilities import get_max_output_tokens, list_all_models
from ...core.accounts import AuthenticatedUser
from ..schemas.settings import (
    HIDDEN_CONFIG_SETTINGS,
    LLMRuntimeDiagnosticsResponse,
    OpenRouterKeyDiagnostics,
    ServerSettingsResponse,
    ServerSettingsUpdate,
)

logger = logging.getLogger(__name__)


def _env_mapping() -> dict[str, str]:
    """Return settings-field to environment-variable mapping for PATCH /settings."""
    return {
        "llm_provider": "LLM_PROVIDER",
        "llm_model": "LLM_MODEL",
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
            str(settings.project_root / filename)
            for filename in (".env", ".env.docker")
            if (settings.project_root / filename).exists()
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
        env_docker_path = settings.project_root / ".env.docker"
        env_path = (
            env_docker_path
            if env_docker_path.exists()
            else settings.project_root / ".env"
        )

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
                "Provider to fetch models for (anthropic, openai). Defaults to "
                "global provider."
            ),
        ),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Fetch available models from the configured LLM provider or CLIProxy."""
        effective_provider = provider or settings.llm_provider

        base_url = settings.llm_base_url
        if effective_provider == "anthropic":
            api_key = settings.anthropic_direct_api_key or settings.anthropic_api_key
            if not base_url:
                base_url = "https://api.anthropic.com"
        elif effective_provider == "openai":
            api_key = settings.openai_api_key
            if not base_url:
                base_url = "https://api.openai.com"
        else:
            return []

        if settings.llm_base_url:
            api_key = settings.get_api_key_for_provider()

        if not api_key:
            return []

        headers = {
            "x-api-key": api_key,
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
        }

        clean_base = base_url.rstrip("/")
        if clean_base.endswith("/v1"):
            clean_base = clean_base[:-3]

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                url = f"{clean_base}/v1/models"
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            raw_models = data.get("data", [])
            result = []
            for m in sorted(raw_models, key=lambda x: x.get("id", "")):
                model_id = m.get("id", "")
                result.append({
                    "id": model_id,
                    "name": m.get("name") or model_id,
                    "owned_by": m.get("owned_by", ""),
                    "created": m.get("created"),
                })
            return result
        except Exception as e:
            logger.warning("Failed to fetch models from %s: %s", base_url, e)
            return []

    return router
