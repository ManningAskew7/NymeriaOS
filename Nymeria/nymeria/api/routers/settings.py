"""Global settings and model-catalog routes."""

import asyncio
import difflib
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from ...config import Settings
from ...config.llm_providers import (
    get_llm_provider_spec,
    is_google_native_provider,
    is_openai_compatible_provider,
    list_llm_provider_specs,
    normalize_llm_provider,
    provider_requires_api_key,
    provider_supports_responses,
    resolve_provider_route,
    resolve_provider_api_key,
    resolve_provider_base_url,
)
from ...config.env_file import format_env_value, parse_env_value, write_env_file
from ...config.settings import get_env_file_paths, get_env_write_path
from ...config.model_capabilities import (
    list_all_models,
    max_reasoning_effort,
    parse_anthropic_reasoning_capabilities,
    register_model_metadata,
    supported_reasoning_efforts,
)
from ...core.accounts import AuthenticatedUser
from ...core.llm_credentials import get_llm_provider_credential
from ...core.llm_provider_test_suite import (
    ProviderTestSuiteOptions,
    run_provider_test_suite,
)
from ...core.llm_provider_utils import (
    base_url_allows_no_api_key,
    cliproxy_base_url_with_v1,
    cliproxy_probe_billing_system,
    configured_llm_destinations,
    destination_redirects_away_from_config,
    extract_model_metadata,
    http_error_detail,
    provider_probe_headers,
    redact_secrets,
)
from ...vendor.react_agent.providers import resolve_max_output_tokens
from ..schemas.settings import (
    HIDDEN_CONFIG_SETTINGS,
    resolve_settings_field_name,
    server_settings_env_mapping,
    AvailableModelsRequest,
    DreamPromptInfo,
    DreamPromptsResponse,
    DreamPromptsUpdate,
    LLMProviderSpecResponse,
    LLMProviderTestRequest,
    LLMProviderTestResponse,
    LLMProviderTestSuiteRequest,
    LLMProviderTestSuiteResponse,
    LLMRuntimeDiagnosticsResponse,
    OpenRouterKeyDiagnostics,
    RagCatalogResponse,
    RagComboResponse,
    RagEmbedderOptionResponse,
    RagRerankerOptionResponse,
    ServerSettingsResponse,
    ServerSettingsUpdate,
    SystemPromptResponse,
    SystemPromptUpdate,
)

logger = logging.getLogger(__name__)

_CLEARABLE_NULL_SETTINGS = {
    "llm_context_length",
    "llm_ollama_num_ctx",
    "llm_provider_route",
    # Effort cleared = back to provider-default reasoning behavior; without
    # this the frontend "Default" option could never unset a saved effort.
    "llm_reasoning_effort",
    "embedding_dimensions",
    # Base URL / input type cleared = back to the provider's own endpoint;
    # switching e.g. from a Voyage embedder (custom base URL) to Cohere must
    # be able to drop the stale override.
    "embedding_base_url",
    "embedding_input_type",
    # Public URL cleared = OAuth auth_code providers degrade to device_code /
    # use_localhost again; without this a wrong public URL could never be
    # unset through the API.
    "nymeria_public_url",
    "rag_rerank_model",
    # Voice model/voice/base-URL cleared = back to the per-provider default
    # (core/voice.py); a stale explicit value breaks provider switches.
    "tts_model",
    "tts_voice",
    "tts_base_url",
    "stt_model",
    "stt_base_url",
}


def _env_mapping() -> dict[str, str]:
    """Return the settings-field to env-var mapping for PATCH /settings.

    Thin wrapper over `server_settings_env_mapping()` (derived from
    `ServerSettingsUpdate`); kept as a named helper because the settings applier
    and `CommandService` reference it.
    """
    return server_settings_env_mapping()


# Settings that persist immediately but require a process restart to apply.
# Module-level constants (F9): built once at import rather than rebuilt on
# every request. The `_env_categories()` accessor is retained as the read path
# for `serialize_env_entries`. Treat the returned structures as read-only;
# callers only iterate / membership-test them.
_RESTART_REQUIRED_KEYS: frozenset[str] = frozenset({
    "redis_url",
    "redis_enabled",
    "postgres_uri",
    "nymeria_data_dir",
    "discord_bot_token",
    "discord_webhook_url",
    "telegram_bot_token",
    "telegram_default_chat_id",
    # The embedder is baked into each cached MemoryIndex at construction, and a
    # dimension change also needs `nymeria reembed`, so these need a restart.
    "embedding_provider",
    "embedding_model",
    "embedding_dimensions",
    # Captured by the Ticker at construction/start (poll_interval and the
    # executor's max_workers) or read off the Settings instance the Ticker
    # holds, which the applier's hot-reload never rebinds; without the flag a
    # hot PATCH answers "applied" while the running ticker keeps the old value.
    "ticker_poll_interval",
    "max_concurrent_autonomous",
    "todo_auto_archive_days",
})


def _restart_required_keys() -> frozenset[str]:
    """Settings that persist immediately but require process restart to apply."""
    return _RESTART_REQUIRED_KEYS


_ENV_CATEGORIES: dict[str, tuple[str, ...]] = {
    "LLM": (
        "llm_provider",
        "llm_model",
        "llm_fast_model",
        "llm_smart_model",
        "llm_background_model",
        "llm_background_base_url",
        "llm_fallback_models",
        "llm_temperature",
        "llm_max_tokens",
        "llm_top_p",
        "llm_top_k",
        "llm_frequency_penalty",
        "llm_presence_penalty",
        "llm_reasoning_effort",
        "llm_extended_thinking",
        "dynamic_tool_binding",
        "sequential_tool_execution",
        "hooks_enabled",
        "llm_use_model_defaults",
        "llm_base_url",
        "llm_context_length",
        "llm_ollama_num_ctx",
        "llm_provider_route",
        "openai_api_mode",
        "llm_stream_max_retries",
        "llm_stream_retry_initial_delay",
        "llm_stream_retry_max_delay",
        "llm_fallback_hold_seconds",
        "llm_fallback_switch_mode",
        "llm_fallback_prompt_timeout_seconds",
        "llm_refusal_swap_mode",
        "cliproxy_management_url",
    ),
    "API Keys": (
        "cliproxy_management_key",
        "openai_api_key",
        "anthropic_api_key",
        "anthropic_direct_api_key",
        "openrouter_api_key",
        "embedding_api_key",
        "perplexity_api_key",
        "perplexity_search_model",
        "gemini_api_key",
        "gemini_extraction_model",
    ),
    "Context": (
        "context_management",
        "compact_threshold",
        "compact_threshold_mode",
        "compact_threshold_tokens",
        "compact_keep_messages",
        "compact_model",
        "compact_proactive_enabled",
        "compact_proactive_idle_seconds",
        "compact_proactive_min_pct",
        "sliding_window_cycles",
        "memory_char_limit",
        "memory_max_entries",
        "memory_value_max_chars",
    ),
    "System": (
        "log_level",
        "watchdog_enabled",
        "watchdog_interval_minutes",
        "user_timezone",
        "nymeria_data_dir",
        # Public browser base URL; OAuth auth_code providers cannot build a
        # redirect URL without it (its invisibility here caused a 74-day
        # Google Calendar outage: settable all along, advertised nowhere).
        "nymeria_public_url",
        "tool_timeout",
        "nymeria_mcp_chat_wait_seconds",
        "tool_output_max_chars",
        "tool_timing_in_results",
        "agent_max_iterations",
        "lock_timeout",
    ),
    "Tasks": (
        "ticker_poll_interval",
        "max_concurrent_autonomous",
        "max_concurrent_interactive",
        "interactive_admission_wait_seconds",
        "todo_staleness_minutes",
        "todo_auto_archive_days",
        "activity_retention_hours",
    ),
    "Voice": (
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
        "groq_api_key",
        "stt_model",
        "stt_language",
        "voice_default_thread_id",
    ),
    "Infrastructure": (
        "redis_url",
        "redis_enabled",
        "postgres_uri",
    ),
    "Discord": (
        "discord_bot_token",
        "discord_webhook_url",
    ),
    "Telegram": (
        "telegram_bot_token",
        "telegram_default_chat_id",
    ),
    "Twitch": (
        "twitch_client_id",
        "twitch_client_secret",
        "twitch_bot_access_token",
        "twitch_bot_refresh_token",
        "twitch_bot_user_id",
        "twitch_broadcaster_token",
        "twitch_broadcaster_refresh_token",
        "twitch_channel",
        "twitch_buffer_size",
        "twitch_pulse_enabled",
        "twitch_pulse_interval",
        "twitch_pulse_min_messages",
        "twitch_command_context_count",
    ),
}


def _env_categories() -> dict[str, tuple[str, ...]]:
    """Return grouped admin environment entries for GET /settings/env."""
    return _ENV_CATEGORIES


_SECRET_KEYS: frozenset[str] = frozenset({
    # No _SECRET_KEY_SUFFIXES entry matches the bare *_key here, so the
    # CLIProxy remote-management secret needs an explicit allowlist row.
    "cliproxy_management_key",
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
    "slack_app_token",
    "whatsapp_access_token",
    "whatsapp_webhook_verify_token",
    "whatsapp_app_secret",
    "teams_bot_app_password",
    "microsoft_graph_access_token",
    "postgres_uri",
    "redis_url",
    "tts_api_key",
    "stt_api_key",
    "fcm_credentials_json",
})


# Name suffixes that mark a settings key as credential-bearing. Used so any
# *_api_key / *_token / *_secret / *_password / *_private_key style key is
# masked even when it was never added to the explicit allowlist above. Suffix
# matching (not substring) avoids false positives like ``llm_max_tokens``.
_SECRET_KEY_SUFFIXES = (
    "_api_key",
    "_apikey",
    "_secret_key",
    "_secret",
    "_secrets_key",
    "_access_key",
    "_private_key",
    "_signing_key",
    "_encryption_key",
    "_token",
    "_access_token",
    "_refresh_token",
    "_auth_token",
    "_session_token",
    "_verify_token",
    "_password",
    "_passwd",
    "_app_password",
    "_app_secret",
    "_webhook_secret",
    "_client_secret",
    "_credentials_json",
    "_service_account_json",
)


def _is_secret_setting_key(key: str) -> bool:
    """Return True if a settings key holds a credential and must be masked.

    Combines the explicit allowlist (for secret-bearing keys whose names do not
    follow a credential suffix, e.g. ``postgres_uri``, ``redis_url``,
    ``discord_webhook_url``) with name-suffix derivation so newly added secret
    settings are masked by default rather than leaking until someone remembers
    to extend the allowlist.
    """
    k = key.lower()
    if k in _SECRET_KEYS:
        return True
    return k.endswith(_SECRET_KEY_SUFFIXES)


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


def _resolved_tier_ref(settings: Any, tier: str) -> Optional[str]:
    """Effective tier ref for the GET response (provider:model or bare model).

    Returns the model alone when the tier inherits the primary provider, or
    ``provider:model`` when it targets a different provider, so the per-thread
    tier quick-pick can fill provider+model directly. ``None`` when unresolved.
    """
    from ...config.llm_providers import normalize_llm_provider
    from ...config.model_tiers import resolve_tier

    resolved = resolve_tier(tier, settings)
    if not resolved or not resolved[1]:
        return None
    provider, model = resolved
    if normalize_llm_provider(provider) == normalize_llm_provider(
        getattr(settings, "llm_provider", "")
    ):
        return model
    return f"{provider}:{model}"


def serialize_server_settings(settings: Any) -> ServerSettingsResponse:
    """Single source of truth for the server-settings read model.

    Shared by ``GET /settings`` and ``CommandBackendClient.get_settings`` so the
    HTTP and in-process command shapes cannot drift (the TurnExecutor two-shape
    invariant). This mirrors the write-side applier sharing already done by
    ``apply_server_settings_update`` / ``CommandBackendClient.update_settings``.
    The in-process caller dumps this with ``model_dump(mode="json")``, which
    equals the JSON the HTTP backends parse back from this same route.
    """
    return ServerSettingsResponse(
        llm_provider=settings.llm_provider,
        llm_model=settings.llm_model,
        llm_fast_model=settings.llm_fast_model,
        llm_smart_model=settings.llm_smart_model,
        llm_background_model=settings.llm_background_model,
        llm_background_base_url=settings.llm_background_base_url,
        llm_fast_model_resolved=_resolved_tier_ref(settings, "fast"),
        llm_smart_model_resolved=_resolved_tier_ref(settings, "smart"),
        llm_background_model_resolved=_resolved_tier_ref(settings, "background"),
        llm_fallback_models=_fallback_model_list(settings.llm_fallback_models),
        llm_temperature=settings.llm_temperature,
        llm_max_tokens=settings.llm_max_tokens,
        llm_top_p=settings.llm_top_p,
        llm_top_k=settings.llm_top_k,
        llm_frequency_penalty=settings.llm_frequency_penalty,
        llm_presence_penalty=settings.llm_presence_penalty,
        llm_reasoning_effort=settings.llm_reasoning_effort,
        llm_extended_thinking=settings.llm_extended_thinking,
        dynamic_tool_binding=settings.dynamic_tool_binding,
        sequential_tool_execution=settings.sequential_tool_execution,
        hooks_enabled=settings.hooks_enabled,
        llm_use_model_defaults=settings.llm_use_model_defaults,
        llm_base_url=settings.llm_base_url,
        llm_context_length=settings.llm_context_length,
        llm_ollama_num_ctx=settings.llm_ollama_num_ctx,
        llm_provider_route=settings.llm_provider_route,
        openai_api_mode=settings.openai_api_mode,
        llm_stream_max_retries=settings.llm_stream_max_retries,
        llm_stream_retry_initial_delay=settings.llm_stream_retry_initial_delay,
        llm_stream_retry_max_delay=settings.llm_stream_retry_max_delay,
        llm_fallback_hold_seconds=settings.llm_fallback_hold_seconds,
        llm_fallback_switch_mode=settings.llm_fallback_switch_mode,
        llm_fallback_prompt_timeout_seconds=settings.llm_fallback_prompt_timeout_seconds,
        llm_refusal_swap_mode=settings.llm_refusal_swap_mode,
        context_management=settings.context_management,
        compact_threshold=settings.compact_threshold,
        compact_threshold_mode=settings.compact_threshold_mode,
        compact_threshold_tokens=settings.compact_threshold_tokens,
        compact_keep_messages=settings.compact_keep_messages,
        compact_model=settings.compact_model,
        compact_proactive_enabled=settings.compact_proactive_enabled,
        compact_proactive_idle_seconds=settings.compact_proactive_idle_seconds,
        compact_proactive_min_pct=settings.compact_proactive_min_pct,
        sliding_window_cycles=settings.sliding_window_cycles,
        tool_output_max_chars=settings.tool_output_max_chars,
        tool_timeout=settings.tool_timeout,
        tool_timing_in_results=settings.tool_timing_in_results,
        memory_char_limit=settings.memory_char_limit,
        memory_max_entries=settings.memory_max_entries,
        memory_value_max_chars=settings.memory_value_max_chars,
        agent_max_iterations=settings.agent_max_iterations,
        user_timezone=settings.user_timezone,
        log_level=settings.log_level,
        watchdog_enabled=settings.watchdog_enabled,
        watchdog_interval_minutes=settings.watchdog_interval_minutes,
        todo_staleness_minutes=settings.todo_staleness_minutes,
        activity_retention_hours=settings.activity_retention_hours,
        dream_default_min_interval_hours=settings.dream_default_min_interval_hours,
        dream_default_min_idle_minutes=settings.dream_default_min_idle_minutes,
        dream_default_min_turns_since_last=settings.dream_default_min_turns_since_last,
        dream_default_model=settings.dream_default_model,
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
        embedding_provider=settings.embedding_provider,
        embedding_model=settings.embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        embedding_base_url=settings.embedding_base_url,
        embedding_input_type=settings.embedding_input_type,
        rag_retrieval_mode=settings.rag_retrieval_mode,
        rag_embed_tool_results=settings.rag_embed_tool_results,
        rag_rerank_enabled=settings.rag_rerank_enabled,
        rag_rerank_provider=settings.rag_rerank_provider,
        rag_rerank_model=settings.rag_rerank_model,
    )


def _mask_value(val: str) -> str:
    """Mask a secret value, showing first 4 and last 3 chars."""
    s = str(val)
    if len(s) <= 10:
        return s[:2] + "..." + s[-1:] if len(s) > 3 else "***"
    return s[:4] + "..." + s[-3:]


def serialize_env_entries(settings: Any) -> dict:
    """Single source of truth for the masked env-var read model.

    Shared by ``GET /settings/env`` and ``CommandBackendClient.get_env_vars`` so
    the HTTP and in-process command shapes cannot drift (the TurnExecutor
    two-shape invariant), mirroring ``serialize_server_settings``. Secrecy is the
    suffix-aware ``_is_secret_setting_key`` (NOT the allowlist-only
    ``_SECRET_KEYS`` membership), so a suffix-style secret like ``groq_api_key``
    is masked on both paths; labels come from the canonical
    ``server_settings_env_mapping`` (not a naive ``key.upper()`` that mislabels
    the divergent S3 fields).
    """
    env_name_map = server_settings_env_mapping()
    entries = []
    for category, keys in _env_categories().items():
        for key in keys:
            if key in HIDDEN_CONFIG_SETTINGS:
                continue
            val = getattr(settings, key, None)
            env_var = env_name_map.get(key, key.upper())
            is_secret = _is_secret_setting_key(key)
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


def serialize_env_var(
    settings: Any, key: str, *, reveal: bool, actor: str = "?"
) -> dict:
    """Single source of truth for the single-env-var read model.

    Shared by ``GET /settings/env/{key}`` and
    ``CommandBackendClient.get_env_var`` (the two-shape invariant, mirroring
    ``serialize_env_entries``; the prior in-process copy skipped masking and
    mislabeled with a naive ``key.upper()``). Unset and unknown are DIFFERENT
    answers: a real settings field that is unset returns ``value: None``, and
    only hidden or nonexistent keys 404. Conflating them made /env get report
    "Unknown variable" for the unset NYMERIA_PUBLIC_URL the 2026-08 OAuth
    outage turned on. Known-ness is judged against the ``Settings`` model
    fields ONLY, never an attribute probe: ``hasattr`` also answers True for
    BaseModel methods, and ``GET /settings/env/model_dump`` would then return
    the full Settings repr, every secret unmasked and unaudited, labeled
    ``is_secret: false``. Env-var spellings (``NYMERIA_PUBLIC_URL``, the
    divergent S3 names) resolve through the same mapping ``/env set`` uses.
    """
    resolved = resolve_settings_field_name(key)
    if resolved in HIDDEN_CONFIG_SETTINGS:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")
    if resolved not in Settings.model_fields:
        raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")
    val = getattr(settings, resolved, None)

    is_secret = _is_secret_setting_key(resolved)
    if val is None:
        display_val = None
    elif is_secret and not reveal:
        display_val = _mask_value(str(val))
    else:
        display_val = str(val)

    if is_secret and reveal and val is not None:
        logger.warning(
            "Admin %s revealed plaintext value of secret setting %s",
            actor,
            resolved,
        )

    return {
        "name": resolved,
        "env_var": server_settings_env_mapping().get(resolved, resolved.upper()),
        "value": display_val,
        "is_secret": is_secret,
    }


def _sync_process_env(new_lines: list[str], mapped_env_vars: set[str]) -> None:
    """Sync mapped dotenv values into os.environ after a settings update.

    The written line may carry a quoted RHS (``format_env_value`` quotes
    special-char values), so the value is un-quoted via ``parse_env_value`` before
    it reaches ``os.environ``. Otherwise the env source (which outranks the dotenv
    source) would feed the hot-reloaded Settings a value wrapped in literal quotes.
    """
    for line in new_lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            if key in mapped_env_vars:
                os.environ[key] = parse_env_value(val)


def _clear_settings_cache(get_settings_fn: Callable[[], Any]) -> None:
    cache_clear = getattr(get_settings_fn, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()


# Settings whose change requires rebuilding the agent's compiled graph: the LLM
# parameters, tool-binding, and the provider credentials baked into the model client.
# Shared by both update paths so they can never trigger different rebuilds.
_LLM_FIELDS = frozenset(
    {
        "llm_provider",
        "llm_model",
        "llm_fast_model",
        "llm_smart_model",
        "llm_fallback_models",
        "llm_temperature",
        "llm_max_tokens",
        "llm_top_p",
        "llm_top_k",
        "llm_frequency_penalty",
        "llm_presence_penalty",
        "llm_reasoning_effort",
        "llm_extended_thinking",
        "dynamic_tool_binding",
        "llm_use_model_defaults",
        "llm_base_url",
        "llm_context_length",
        "llm_ollama_num_ctx",
        "llm_provider_route",
        "openai_api_mode",
        "llm_stream_max_retries",
        "llm_stream_retry_initial_delay",
        "llm_stream_retry_max_delay",
        "llm_fallback_hold_seconds",
        # The consent modes + prompt timeout bake into the per-thread decision
        # callback closure at LLMConfig build, so changing them must rebuild.
        "llm_fallback_switch_mode",
        "llm_fallback_prompt_timeout_seconds",
        "llm_refusal_swap_mode",
    }
)
_LLM_CREDENTIAL_FIELDS = frozenset(
    {
        "anthropic_api_key",
        "anthropic_direct_api_key",
        "openai_api_key",
        # An LLM-route credential since the antigravity native route
        # (catalog key_setting, 2026-08-07), not just a media-tools key.
        "gemini_api_key",
        "openrouter_api_key",
        # The generic slot (routed to the selected provider's declared key var)
        # is a credential change like the four above: the key is baked into the
        # model client at graph build, so it must trigger a rebuild too.
        "llm_api_key",
    }
)


def _llm_api_key_env_var(provider: str) -> str | None:
    """The env var the generic ``llm_api_key`` slot writes for ``provider``.

    Mirrors the CLI wizard finalize: the FIRST entry of the spec's
    ``api_key_env_vars`` (e.g. GROQ_API_KEY, or ANTHROPIC_DIRECT_API_KEY for
    anthropic). None when the provider is unknown or declares no key slot.
    """
    spec = get_llm_provider_spec(provider)
    if spec is None or not spec.api_key_env_vars:
        return None
    return spec.api_key_env_vars[0]
# tool_timeout is captured into SafeToolNode at graph build, so a hot PATCH must
# rebuild the graph (otherwise the cached kill-timeout drifts from settings; the
# claude_code tool's inline-vs-detach budget depends on the two staying in sync).
_GRAPH_REBUILD_FIELDS = (
    _LLM_FIELDS | {"tool_output_max_chars", "tool_timeout"} | _LLM_CREDENTIAL_FIELDS
)


def apply_server_settings_update(
    updates: ServerSettingsUpdate,
    *,
    settings: Settings,
    agent: Any,
    get_settings_fn: Callable[[], Any],
) -> dict:
    """Persist a settings update to the env file and hot-reload it in place.

    The single canonical path shared by `PATCH /settings` and
    `CommandService.update_settings`: filter the update (honoring nullable settings
    that were explicitly cleared), overlay the changed keys onto the env file via the
    shared atomic-0600 writer (`config/env_file.py`), sync `os.environ`, clear the
    settings cache, re-bind the agent, rebuild the agent graph when an LLM / tool /
    credential field changed, and report whether a restart is still required. Returns
    the response dict both callers send back.
    """
    # Unknown keys land in model_extra (the schema's extra="allow" exists for
    # exactly this) and reject the WHOLE update: a partial apply would report
    # success while dropping the caller's real intent, the same lie the
    # extra="ignore" default told for 74 days about NYMERIA_PUBLIC_URL.
    unknown = sorted((updates.model_extra or {}).keys())
    if unknown:
        env_names = server_settings_env_mapping()
        suggestions: list[str] = []
        for key in unknown:
            for match in difflib.get_close_matches(
                key.lower(), list(ServerSettingsUpdate.model_fields), n=2, cutoff=0.6
            ):
                label = (
                    f"{match} ({env_names[match]})" if match in env_names else match
                )
                if label not in suggestions:
                    suggestions.append(label)
        plural = "s" if len(unknown) > 1 else ""
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown setting{plural}: {', '.join(unknown)}.{hint}"
                " No changes were applied."
            ),
        )

    env_path = get_env_write_path(settings.project_root)
    env_mapping = _env_mapping()
    dumped_updates = updates.model_dump()
    explicitly_set = updates.model_fields_set
    updates_dict = {
        k: v
        for k, v in dumped_updates.items()
        if v is not None
        or (k in _CLEARABLE_NULL_SETTINGS and k in explicitly_set)
    }

    # The generic llm_api_key slot has no fixed env var (VIRTUAL_UPDATE_FIELDS):
    # route it to the declared key var of the provider being configured, the same
    # var the CLI wizard finalize writes. Resolved against the update's provider
    # when the caller switches provider and key together (the normal GUI flow),
    # falling back to the currently configured provider for a key-only rotation.
    extra_env_pairs: list[tuple[str, str]] = []
    llm_api_key = (updates_dict.get("llm_api_key") or "").strip()
    if "llm_api_key" in updates_dict and not llm_api_key:
        updates_dict.pop("llm_api_key")
    elif llm_api_key:
        target_provider = updates_dict.get("llm_provider") or settings.llm_provider
        key_env_var = _llm_api_key_env_var(target_provider)
        if not key_env_var:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Provider '{target_provider}' does not declare an API key "
                    "slot, so llm_api_key cannot be stored for it."
                ),
            )
        extra_env_pairs.append((key_env_var, format_env_value(llm_api_key)))

    if not updates_dict:
        return {
            "message": "No updates provided",
            "updated": [],
            "restart_required": False,
            "warnings": [],
        }

    # Validate every value against the REAL Settings field (range, length, and
    # type constraints) BEFORE anything is written. ServerSettingsUpdate fields
    # do not mirror the Settings constraints, and the env write precedes the
    # hot-reload, so without this gate an out-of-range value (e.g.
    # twitch_buffer_size=10 against ge=50) persists to the env file and then
    # EVERY get_settings() raises, bricking the API across restarts until the
    # file is hand-edited. Reject at the choke point instead.
    _validate_against_settings_fields(updates_dict)

    # Overlay only the changed keys onto the existing file, preserving untouched
    # lines, comments, and the secrets key, via the shared atomic 0600 writer the
    # offline `nymeria init` finalize also uses. The formatter quotes special-char
    # values (plain alnum values, the common case, stay unquoted).
    produced = [
        (env_mapping[name], format_env_value(value))
        for name, value in updates_dict.items()
        if name in env_mapping
    ] + extra_env_pairs
    new_lines = write_env_file(env_path, produced, merge=True)
    _sync_process_env(
        new_lines,
        set(env_mapping.values()) | {var for var, _ in extra_env_pairs},
    )

    _clear_settings_cache(get_settings_fn)
    new_settings = get_settings_fn()
    logger.info(
        "[SETTINGS] After hot-reload: TTS_PROVIDER=%s, STT_PROVIDER=%s, "
        "env TTS_PROVIDER=%s",
        new_settings.tts_provider,
        new_settings.stt_provider,
        os.environ.get("TTS_PROVIDER"),
    )

    agent.settings = new_settings

    rebuild = _GRAPH_REBUILD_FIELDS & set(updates_dict.keys())
    if rebuild:
        agent._rebuild_default_graphs()
        logger.info("Hot-reloaded graph settings: %s", rebuild)

    needs_restart = bool(_restart_required_keys() & set(updates_dict.keys()))
    warnings: list[str] = []
    public_url = updates_dict.get("nymeria_public_url")
    if isinstance(public_url, str) and public_url.strip():
        warnings.extend(_public_url_warnings(public_url.strip()))
    return {
        "message": "Settings updated and applied" + (
            " (some changes require /restart api to take effect)"
            if needs_restart
            else ""
        ),
        "updated": list(updates_dict.keys()),
        "restart_required": needs_restart,
        "warnings": warnings,
    }


def _validate_against_settings_fields(updates_dict: dict) -> None:
    """400 when a value violates the Settings model's own field constraints.

    The applier's counterpart to the unknown-key rejection: unknown keys and
    invalid values both fail loudly BEFORE the env write. Virtual fields
    (llm_api_key) and any key without a Settings twin are skipped; None values
    only reach here for _CLEARABLE_NULL_SETTINGS entries, whose Settings
    fields are Optional by construction.
    """
    from typing import Annotated

    from pydantic import TypeAdapter, ValidationError

    problems: list[str] = []
    for key, value in updates_dict.items():
        field = Settings.model_fields.get(key)
        if field is None:
            continue
        try:
            TypeAdapter(Annotated[field.annotation, field]).validate_python(value)
        except ValidationError as exc:
            first = exc.errors()[0]
            problems.append(f"{key}: {first.get('msg', 'invalid value')}")
    if problems:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid value{'s' if len(problems) > 1 else ''}: "
                f"{'; '.join(problems)}. No changes were applied."
            ),
        )


def _public_url_warnings(value: str) -> list[str]:
    """Advisory checks for a freshly set public base URL (warn, never block).

    OAuth auth_code redirect URLs are built from this value, so a mangled one
    breaks every provider that needs it. The concrete failure this guards:
    pasting a URL out of an email client whose rewriter (Outlook Safe Links,
    Proofpoint urldefense) wrapped the real site in a tracking redirector.
    """
    from urllib.parse import parse_qs, urlsplit

    unparseable = [
        f"'{value}' does not look like an http(s) URL; OAuth redirect URLs"
        " built from it will not work."
    ]
    try:
        # urlsplit raises on some malformed inputs (e.g. an unclosed IPv6
        # bracket); a helper whose job is judging pasted junk must never
        # crash on it, especially since it runs after the value is applied.
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
    except ValueError:
        return unparseable
    if parts.scheme not in ("http", "https") or not host:
        return unparseable
    if host == "safelinks.protection.outlook.com" or host.endswith(
        ".safelinks.protection.outlook.com"
    ):
        message = (
            "This looks like an Outlook Safe Links wrapper, not the real site URL."
        )
        candidate = (parse_qs(parts.query).get("url") or [""])[0]
        if candidate:
            message += (
                f" The wrapped destination is {candidate}; you probably want"
                " that value instead."
            )
        return [message]
    if host in ("urldefense.com", "urldefense.proofpoint.com") or host.endswith(
        (".urldefense.com", ".urldefense.proofpoint.com")
    ):
        return [
            "This looks like a Proofpoint urldefense wrapper, not the real site"
            " URL; unwrap it before relying on OAuth redirects."
        ]
    return []



def _normalize_openai_test_base_url(
    provider: str,
    base_url: str | None,
    *,
    provider_route: str | None = None,
) -> str:
    provider = normalize_llm_provider(provider)
    if not base_url:
        resolved = resolve_provider_base_url(
            provider,
            provider_route=provider_route,
        )
        if resolved:
            return resolved
        if provider == "openrouter":
            return "https://openrouter.ai/api/v1"
        return "https://api.openai.com/v1"

    clean = base_url.strip()
    if provider == "openai":
        return cliproxy_base_url_with_v1(clean)
    return clean.rstrip("/")




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
    settings: Settings | None = None,
    vault: Any | None = None,
    owner_user_id: str | None = None,
) -> LLMProviderTestResponse:
    provider = normalize_llm_provider(request.llm_provider)
    model = request.llm_model
    api_key = request.api_key.get_secret_value() if request.api_key else None
    base_url = request.llm_base_url
    provider_route = resolve_provider_route(
        provider,
        route_override=request.provider_route,
        global_route=getattr(settings, "llm_provider_route", None) if settings else None,
    )
    openai_api_mode = request.openai_api_mode or "chat_completions"

    if not api_key:
        credential = get_llm_provider_credential(
            provider, vault=vault, owner_user_id=owner_user_id,
        )
        if credential and credential.api_key:
            api_key = credential.api_key
            if not base_url and credential.base_url:
                base_url = credential.base_url

    if not api_key:
        api_key = resolve_provider_api_key(provider, settings=settings)

    if not api_key:
        resolved_base = base_url or resolve_provider_base_url(
            provider,
            provider_route=provider_route,
            settings=settings,
        )
        if base_url_allows_no_api_key(resolved_base):
            api_key = "not-needed"
        elif provider_requires_api_key(provider):
            return LLMProviderTestResponse(
                ok=False,
                provider=provider,
                model=model,
                provider_route=provider_route,
                openai_api_mode=None,
                message="No API key provided and none found in vault, settings, or environment.",
                error_type="missing_api_key",
            )
        else:
            api_key = "not-needed"

    google_native = (
        is_google_native_provider(provider) and provider_route != "openai_compat"
    )

    if provider == "anthropic":
        clean_base = base_url or "https://api.anthropic.com"
        url = f"{clean_base}/v1/messages"
        # base_url adds the safe Anthropic-Beta override on CLIProxy-shaped
        # routes inside the helper (single owner of probe header identity).
        headers = provider_probe_headers(
            provider,
            api_key,
            has_custom_base_url=bool(base_url),
            base_url=base_url,
        )
        payload = {
            "model": model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "Reply with ok."}],
        }
        # The probe sends the production cloak-skip identity (headers above),
        # which requires the OAuth billing fingerprint on a CLIProxy base or
        # premium Claude models 429 on a perfectly valid token; with it, a
        # residual 429 really means quota-window exhaustion. Shared helper
        # (#161): this payload and the provider test suite's must not drift.
        system_blocks = cliproxy_probe_billing_system(base_url)
        if system_blocks:
            payload["system"] = system_blocks
        response_api_mode = None
    elif google_native:
        # Native Gemini wire (google-genai REST shape). The configured base
        # is honored so CLIProxy's /v1beta inbound surface (antigravity
        # native route, catalog 2026-08-07) is testable; without a base this
        # probes Google directly. Bearer/chat-completions here would 404 the
        # proxy root, which silently broke antigravity credential verify and
        # gated the desktop wizard's Save behind a test that could not pass.
        clean_base = (base_url or "https://generativelanguage.googleapis.com").rstrip(
            "/"
        )
        url = f"{clean_base}/v1beta/models/{model}:generateContent"
        # Shared with the models listing so the two google probes cannot
        # drift; Content-Type comes from the json= post.
        headers = provider_probe_headers(provider, api_key)
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "Reply with ok."}]}],
            "generationConfig": {"maxOutputTokens": 64},
        }
        response_api_mode = None
    else:
        if (
            not is_openai_compatible_provider(provider)
            and provider_route != "openai_compat"
            and not base_url
        ):
            return LLMProviderTestResponse(
                ok=False,
                provider=provider,
                model=model,
                provider_route=provider_route,
                openai_api_mode=None,
                message=(
                    f"Provider '{provider}' is not in Nymeria's OpenAI-compatible "
                    "registry. Provide an API base URL to test it as a custom endpoint."
                ),
                error_type="unknown_provider",
            )
        clean_base = _normalize_openai_test_base_url(
            provider,
            base_url,
            provider_route=provider_route,
        )
        headers = provider_probe_headers(
            provider, api_key, has_custom_base_url=bool(base_url)
        )

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
            provider_route=provider_route,
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
        detail = http_error_detail(exc.response, api_key, base_url)
        return LLMProviderTestResponse(
            ok=False,
            provider=provider,
            model=model,
            provider_route=provider_route,
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
            provider_route=provider_route,
            openai_api_mode=response_api_mode,
            message=redact_secrets(str(exc), api_key, base_url)[:300],
            error_type=type(exc).__name__,
        )

    return LLMProviderTestResponse(
        ok=True,
        provider=provider,
        model=model,
        provider_route=provider_route,
        openai_api_mode=response_api_mode,
        message="Provider test succeeded.",
    )


async def _available_models(
    *,
    provider: Optional[str],
    base_url: Optional[str],
    api_key: Optional[str],
    vault: Any,
    owner_user_id: Optional[str],
    settings: Any,
) -> list[dict]:
    """Fetch available models from the configured LLM provider or CLIProxy.

    Module-scope on purpose (mirroring ``_test_llm_provider_config``): the
    single implementation behind GET/POST /models/available AND
    ``CommandBackendClient.list_available_models``, so the two TurnExecutor
    shapes cannot drift (the previous in-process hand-copy lacked the
    CLIProxy /v1 normalization and probe headers). ``api_key`` is an
    EPHEMERAL credential override (nothing stored, never logged): it wins
    over the vault/settings resolution so the /provider setup flow can list
    models with a just-pasted key before saving it.

    The gate: when the DESTINATION came from the request rather than from
    configuration, every STORED credential source is suppressed (vault, then
    the per-provider settings fallbacks further down), so an address the caller
    named gets an unauthenticated probe or nothing. This is the same shape as
    ``anthropic_probe_base_url`` in the vendored provider factory, which states
    the principle plainly: the gate lives at the resolver every caller shares,
    so a caller that forgets gets no egress rather than a leak. The property
    defended is that the key never reaches the wire, not that the request never
    happens.

    "Came from the request" is asked with the SHARED predicate, the same one
    the per-thread resolver uses (``core/llm_provider_utils``), and that
    sharing is the point: these are the two consumption points of one
    invariant, and the audit's fix map calls them one fix. Two consequences,
    both corrections to an earlier shape that tested ``bool(base_url)`` and
    exempted admins.

    Naming a destination configuration ALREADY uses is not a redirection, so
    listing models against the deployment's own CLIProxy host keeps working
    instead of returning an empty list for no security benefit.

    And there is no role exemption, which is why this function takes no role.
    The earlier reasoning (an admin already reaches destination-plus-key
    through the admin-gated POST route) does not survive the single-user
    deployment this ships as by default, where the only account IS an admin and
    the exemption therefore switches the control off everywhere. A per-role
    policy, if one is ever wanted, re-adds the argument in the same three lines
    it was removed from; keeping a parameter nothing reads would only invite a
    reader to believe a role still decides something here. The ephemeral
    ``api_key`` argument is unaffected either way, since a caller supplying
    both address and key is spending only their own credential.
    """
    effective_provider = normalize_llm_provider(provider or settings.llm_provider)
    google_native = is_google_native_provider(effective_provider)
    use_stored_credentials = not destination_redirects_away_from_config(
        base_url,
        configured=configured_llm_destinations(effective_provider, settings=settings),
    )
    credential = (
        get_llm_provider_credential(
            effective_provider,
            vault=vault,
            owner_user_id=owner_user_id,
        )
        if use_stored_credentials
        else None
    )

    effective_base_url = base_url
    api_key = (api_key or "").strip() or (
        credential.api_key if credential else None
    )
    if effective_base_url:
        effective_base_url = effective_base_url.strip().rstrip("/")
    if (
        not effective_base_url
        and effective_provider == normalize_llm_provider(settings.llm_provider)
    ):
        effective_base_url = settings.llm_base_url
    if not effective_base_url and credential and credential.base_url:
        effective_base_url = credential.base_url

    # Whether a non-default base URL was configured (query/settings/credential)
    # before provider defaults are applied. Drives the anthropic cloak header
    # below, matching the provider-test path.
    had_custom_base = bool(effective_base_url)

    if effective_provider == "anthropic":
        if use_stored_credentials:
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
    elif google_native:
        # Native Gemini wire (google-genai REST shape), mirroring the
        # provider-test branch: the configured base is honored so CLIProxy's
        # /v1beta inbound (the antigravity native route) is listable with the
        # CLIProxy local key; without a base this lists Google directly.
        # Before this branch the google provider fell through to the empty
        # return below, and "/models" rendering "No models returned from
        # provider." is what pushed the blind model pick in the 2026-08-09
        # wedged-thread incident (see the google history sanitizer in
        # vendor/react_agent/providers.py). No provider_route check here,
        # unlike the test probe: this function has no route parameter, and
        # for the default native base the listing serves the same pool
        # either route. Known limits: an EXPLICIT compat-shim base
        # (.../v1beta/openai) still gets the native URL appended and 404s
        # into the same empty list the old code returned (route-awareness
        # needs the parameter plumbed through), and only the FIRST page is
        # read (real Google paginates via nextPageToken, default page size
        # 50, so a direct-Google listing can miss tail models; CLIProxy
        # returns one page).
        if use_stored_credentials:
            api_key = api_key or resolve_provider_api_key(
                effective_provider,
                settings=settings,
            )
        effective_base_url = (
            effective_base_url or "https://generativelanguage.googleapis.com"
        )
        clean_base = effective_base_url.rstrip("/")
        models_url = f"{clean_base}/v1beta/models"
    elif is_openai_compatible_provider(effective_provider):
        if use_stored_credentials:
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
        models_base = effective_base_url.rstrip("/")
        if effective_provider == "openai":
            # CLIProxy serves its OpenAI surface under /v1; mirror the
            # provider-test path so a proxy root without /v1 still lists models.
            models_base = cliproxy_base_url_with_v1(models_base)
        models_url = f"{models_base}/models"
    else:
        return []

    if (
        not api_key
        and provider_requires_api_key(effective_provider)
        and not base_url_allows_no_api_key(effective_base_url)
    ):
        return []
    if not api_key:
        api_key = "not-needed"

    headers = provider_probe_headers(
        effective_provider,
        api_key,
        has_custom_base_url=had_custom_base,
        base_url=effective_base_url,
    )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(models_url, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        if google_native:
            # Gemini listing shape: {"models": [{"name": "models/<id>", ...}]}.
            # Normalize into the id/name shape the shared loop expects; the
            # camelCase token limits ride along for extract_model_metadata
            # (which reads inputTokenLimit/outputTokenLimit). Rows that cannot
            # generateContent (embedding models, aqa) are not chat-pickable
            # and are filtered when the listing declares methods at all.
            raw_models = []
            for m in data.get("models", []) or []:
                methods = m.get("supportedGenerationMethods") or []
                if methods and "generateContent" not in methods:
                    continue
                model_id = str(m.get("name") or "").removeprefix("models/")
                if not model_id:
                    continue
                raw_models.append(
                    {**m, "id": model_id, "name": m.get("displayName") or model_id}
                )
        else:
            raw_models = data.get("data", [])
        result = []
        for m in sorted(raw_models, key=lambda x: x.get("id", "")):
            model_id = m.get("id", "")
            if not model_id:
                continue
            model_name = m.get("name") or m.get("display_name") or model_id
            metadata = extract_model_metadata(m)
            # Anthropic publishes a per-model capabilities tree with
            # per-effort-level support flags; registering it here makes
            # the advertised ladder exact for newly released models
            # without a static-table edit.
            live_efforts = parse_anthropic_reasoning_capabilities(
                m.get("capabilities")
            )
            register_model_metadata(
                model_id=model_id,
                name=model_name,
                context_length=metadata["context_length"],
                max_completion_tokens=metadata["max_completion_tokens"],
                input_modalities=set(metadata["input_modalities"]),
                supported_parameters=set(metadata["supported_parameters"]),
                reasoning_efforts=live_efforts,
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
                # Effort ladder computed with the provider this listing was
                # proxied for, so it matches the runtime clamp for threads
                # routed through that provider.
                "supported_reasoning_efforts": list(
                    supported_reasoning_efforts(effective_provider, model_id)
                ),
                "max_reasoning_effort": max_reasoning_effort(
                    effective_provider, model_id
                ),
            })
        return result
    except httpx.HTTPStatusError as e:
        # Distinguish provider-side rejections (e.g. 401 bad key, 404 wrong
        # endpoint) from transport failures so the log is actionable. The
        # response contract stays "[] == no models" for the frontend.
        logger.warning(
            "Model fetch from %s returned HTTP %s: %s",
            models_url,
            e.response.status_code,
            e,
        )
        return []
    except httpx.HTTPError as e:
        logger.warning("Model fetch from %s failed (transport): %s", models_url, e)
        return []
    except Exception as e:
        logger.error(
            "Unexpected error fetching models from %s: %s",
            models_url,
            e,
            exc_info=True,
        )
        return []


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
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Get current server settings. Admin-only because values expose topology."""
        return serialize_server_settings(settings)

    def _system_prompt_response() -> SystemPromptResponse:
        """Build the current system-prompt editor payload from disk."""
        settings = get_settings_fn()
        default_content = (
            settings.soul_path.read_text(encoding="utf-8")
            if settings.soul_path.exists()
            else ""
        )
        override_path = settings.system_prompt_override_path
        is_override = bool(
            override_path.exists()
            and override_path.read_text(encoding="utf-8").strip()
        )
        return SystemPromptResponse(
            content=settings.load_soul(),
            default_content=default_content,
            is_override=is_override,
        )

    @router.get("/settings/system-prompt", response_model=SystemPromptResponse)
    async def get_system_prompt(
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Get the effective base system prompt, the shipped default, and override status."""
        return _system_prompt_response()

    @router.put("/settings/system-prompt", response_model=SystemPromptResponse)
    async def update_system_prompt(
        request: SystemPromptUpdate,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Set or clear the base system-prompt override, then hot-reload the agent.

        Blank content clears the override (reset to the shipped soul.md). The
        packaged soul.md is never modified; the override lives in the data dir.
        """
        override_path = settings.system_prompt_override_path
        content = request.content.strip()
        if content:
            override_path.parent.mkdir(parents=True, exist_ok=True)
            override_path.write_text(content, encoding="utf-8")
        elif override_path.exists():
            override_path.unlink()

        get_agent_fn().reload_base_system_prompt()
        return _system_prompt_response()

    @router.delete("/settings/system-prompt", response_model=SystemPromptResponse)
    async def reset_system_prompt(
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Delete the override and restore the shipped soul.md, then hot-reload."""
        override_path = settings.system_prompt_override_path
        if override_path.exists():
            override_path.unlink()
        get_agent_fn().reload_base_system_prompt()
        return _system_prompt_response()

    def _dream_prompt_info(default_path: Path, override_path: Path, effective: str) -> DreamPromptInfo:
        default_content = (
            default_path.read_text(encoding="utf-8") if default_path.exists() else ""
        )
        is_override = bool(
            override_path.exists()
            and override_path.read_text(encoding="utf-8").strip()
        )
        return DreamPromptInfo(
            content=effective,
            default_content=default_content,
            is_override=is_override,
        )

    def _dream_prompts_response() -> DreamPromptsResponse:
        """Build the dream-prompt editor payload (system + kickoff) from disk."""
        settings = get_settings_fn()
        return DreamPromptsResponse(
            system=_dream_prompt_info(
                settings.dream_prompt_path,
                settings.dream_prompt_override_path,
                settings.load_dream_prompt(),
            ),
            kickoff=_dream_prompt_info(
                settings.dream_kickoff_path,
                settings.dream_kickoff_override_path,
                settings.load_dream_kickoff_prompt(),
            ),
        )

    def _write_dream_override(override_path: Path, content: Optional[str]) -> None:
        """Write a non-blank override, or clear it when blank. None leaves it alone."""
        if content is None:
            return
        text = content.strip()
        if text:
            override_path.parent.mkdir(parents=True, exist_ok=True)
            override_path.write_text(text, encoding="utf-8")
        elif override_path.exists():
            override_path.unlink()

    @router.get("/settings/dream-prompts", response_model=DreamPromptsResponse)
    async def get_dream_prompts(
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Get the global dream prompts (system + kickoff), defaults, and override flags."""
        return _dream_prompts_response()

    @router.put("/settings/dream-prompts", response_model=DreamPromptsResponse)
    async def update_dream_prompts(
        request: DreamPromptsUpdate,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Set or clear the global dream-prompt overrides.

        Per field: a non-blank string writes the data-dir override, a blank string
        clears it (resets to the shipped default), and an omitted field is left
        untouched. No agent reload is needed: invoke_dream reads these fresh on every
        dream cycle. The shipped package files are never modified.
        """
        _write_dream_override(settings.dream_prompt_override_path, request.system)
        _write_dream_override(settings.dream_kickoff_override_path, request.kickoff)
        return _dream_prompts_response()

    @router.post("/settings/llm/test", response_model=LLMProviderTestResponse)
    async def test_llm_provider_config(
        request: LLMProviderTestRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Test an arbitrary LLM provider configuration without writing it."""
        agent = get_agent_fn()
        return await _test_llm_provider_config(
            request,
            settings=settings,
            vault=getattr(agent, "credential_vault", None),
            owner_user_id=user.id,
        )

    @router.post(
        "/settings/llm/test-suite",
        response_model=LLMProviderTestSuiteResponse,
    )
    async def test_llm_provider_suite(
        request: LLMProviderTestSuiteRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Run the production-readiness suite for an LLM provider setup."""
        agent = get_agent_fn()
        api_key = request.api_key.get_secret_value() if request.api_key else None
        report = await run_provider_test_suite(
            ProviderTestSuiteOptions(
                provider=request.llm_provider,
                model=request.llm_model,
                api_key=api_key,
                base_url=request.llm_base_url,
                api_mode=request.openai_api_mode,
                settings=settings,
                vault=getattr(agent, "credential_vault", None),
                owner_user_id=user.id,
                run_model_list=request.run_model_list,
                run_chat_completion=request.run_chat_completion,
                run_tool_call=request.run_tool_call,
                allow_billable=request.allow_billable,
                prefer_free_model=request.prefer_free_model,
                timeout_seconds=request.timeout_seconds,
            )
        )
        return LLMProviderTestSuiteResponse(**report.to_dict())

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
                tier=spec.tier,
                notes_for_user=spec.notes_for_user,
                supported_routes=list(spec.supported_routes),
                default_route=spec.default_route,
                openai_compat_base_url=spec.openai_compat_base_url,
                verified=spec.verified,
                anthropic_native_for_claude=spec.anthropic_native_for_claude,
                reasoning_passback_verified=spec.reasoning_passback_verified,
                signup_url=spec.signup_url or "",
                signup_guidance=spec.signup_guidance or "",
            )
            for spec in list_llm_provider_specs()
        ]

    @router.get("/settings/rag/catalog", response_model=RagCatalogResponse)
    async def get_rag_catalog(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Return the RAG embedder/reranker catalog the CLI wizard renders.

        Served from ``setup/rag_catalog.py`` (TUI-free by design) so GUI
        onboarding and the CLI wizard share one source of truth instead of a
        hand-copied frontend table. Function-local import to keep the setup
        package off the API's import path until first use.
        """
        from ...setup.rag_catalog import (
            EMBEDDERS,
            QUICKSTART_EMBEDDER,
            QUICKSTART_RERANKER,
            RECOMMENDED_COMBOS,
            RERANKERS,
        )

        return RagCatalogResponse(
            embedders=[
                RagEmbedderOptionResponse(
                    id=opt.id,
                    tier=opt.tier,
                    label=opt.label,
                    description=opt.description,
                    provider=opt.provider,
                    model=opt.model,
                    dimensions=opt.dimensions,
                    requires_key=opt.requires_key,
                    key_vendor=opt.key_vendor,
                    base_url=opt.base_url,
                    input_type=opt.input_type,
                    pricing=opt.pricing,
                    key_label=opt.key_label,
                    eval_tag=opt.eval_tag,
                )
                for opt in EMBEDDERS
            ],
            rerankers=[
                RagRerankerOptionResponse(
                    id=opt.id,
                    tier=opt.tier,
                    label=opt.label,
                    description=opt.description,
                    provider=opt.provider,
                    model=opt.model,
                    requires_key=opt.requires_key,
                    key_vendor=opt.key_vendor,
                    pricing=opt.pricing,
                    key_label=opt.key_label,
                    eval_tag=opt.eval_tag,
                )
                for opt in RERANKERS
            ],
            combos=[
                RagComboResponse(
                    label=label,
                    embedder_id=embedder_id,
                    reranker_id=reranker_id,
                    description=description,
                )
                for label, embedder_id, reranker_id, description in RECOMMENDED_COMBOS
            ],
            quickstart_embedder=QUICKSTART_EMBEDDER,
            quickstart_reranker=QUICKSTART_RERANKER,
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

        # Report the ceiling a real turn actually gets. This used to resolve for
        # OpenRouter only, so once the Anthropic factory started discovering a
        # ceiling too the diagnostic reported None while the wire carried a real
        # number: the one place a user looks to check this was the last place to
        # learn it. Route through the same resolver the factories use.
        #
        # The probe speaks the Anthropic dialect, so it is opt-in per provider,
        # exactly as in the factories. It is cached and normally warm here (the
        # agent's own LLM primed it), but resolution can do blocking HTTP, and
        # this is an async route: hand it to a thread rather than stalling the
        # event loop for every other request in flight.
        #
        # Reports the UNCLAMPED ceiling deliberately: interactive turns stream,
        # and streaming opts back up past the instance's non-streaming clamp.
        effective_max_tokens = llm_cfg.max_tokens
        if effective_max_tokens is None:
            try:
                effective_max_tokens = await asyncio.to_thread(
                    resolve_max_output_tokens,
                    llm_cfg,
                    probe=llm_cfg.provider == "anthropic",
                )
            except Exception as e:
                logger.warning("Failed to resolve max output tokens: %s", e)

        source_env_files = [
            str(path)
            for path in get_env_file_paths(settings.project_root)
            if path.exists()
        ]

        response = LLMRuntimeDiagnosticsResponse(
            provider=llm_cfg.provider,
            model=llm_cfg.model,
            provider_route=llm_cfg.provider_route,
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

            try:
                async with httpx.AsyncClient(timeout=6) as client:
                    api_response = await client.get(
                        "https://openrouter.ai/api/v1/key",
                        headers={
                            "Authorization": f"Bearer {llm_cfg.api_key}",
                            "Content-Type": "application/json",
                        },
                    )
                    api_response.raise_for_status()
                    payload = api_response.json()
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
            except httpx.HTTPStatusError as e:
                body = e.response.text[:300]
                response.openrouter = OpenRouterKeyDiagnostics(
                    fetch_error=f"HTTP {e.response.status_code}: {body}"
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
        return apply_server_settings_update(
            updates,
            settings=settings,
            agent=get_agent_fn(),
            get_settings_fn=get_settings_fn,
        )

    @router.get("/settings/env")
    async def get_env_vars(
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Get settable environment variables with masked sensitive values."""
        return serialize_env_entries(settings)

    @router.get("/settings/env/{key}")
    async def get_env_var(
        key: str,
        reveal: bool = False,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Get a single environment variable. Admin-only.

        Secret-named values (``*_api_key``/``*_token``/``*_secret``/... and the
        allowlist) are masked unless ``reveal=true`` is passed, which is an
        explicit admin reveal and is audit-logged. Non-secret values always
        return raw.
        """
        return serialize_env_var(
            settings, key, reveal=reveal, actor=str(getattr(user, "id", "?"))
        )

    @router.get("/models")
    async def get_cached_models(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Return cached model metadata for frontend enrichment."""
        models = list_all_models()
        result = []
        for m in models:
            # Provider-qualified ids (e.g. "x-ai/grok-4") come from the
            # OpenRouter catalog, where OpenRouter's unified reasoning config
            # decides the ladder; bare ids keep the per-family lookup. This
            # matches what the runtime clamp does for openrouter threads.
            provider_hint = "openrouter" if "/" in m.id else ""
            result.append({
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
                # Static reasoning-effort ladder (rank-ordered) so frontends
                # can warn before a per-model clamp kicks in.
                "supported_reasoning_efforts": list(
                    supported_reasoning_efforts(provider_hint, m.id)
                ),
                "max_reasoning_effort": max_reasoning_effort(provider_hint, m.id),
            })
        return result

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
        """List models. Any authenticated user, so NO stored key may egress.

        ``base_url`` is a live, unsaved value typed into a settings or
        per-thread model field, which is why it stays: the local-endpoint flows
        (Ollama, LM Studio, CLIProxy, a self-hosted gateway) all list against an
        address the user is still editing, and per-thread model config is
        deliberately not admin-gated.

        What it must NOT do is name a destination that a server-held credential
        is then mailed to. ``destination_redirects_away_from_config``, shared
        with the per-thread resolver, is that gate: when the address is one the
        deployment is NOT configured for, vault and settings key resolution is
        skipped entirely, so that host gets an unauthenticated probe or
        nothing. Naming an address configuration already uses is not a
        redirection and keeps its credential, so listing models against the
        deployment's own CLIProxy host still works. Keyless endpoints work
        either way. The admin-gated POST twin below is where a caller-supplied
        destination may carry a caller-supplied key.

        No role is exempt. See ``_available_models`` for why the earlier admin
        exemption was removed rather than widened.

        A residual remains and is accepted: any authenticated caller can still
        make the server issue a credential-free GET to an address of their
        choosing. This is a credential-egress gate, not an SSRF control, and
        ``validate_http_egress_url`` would not serve here because loopback is a
        legitimate target for exactly the local-LLM flows above. ``SECURITY.md``
        2.5 records it.
        """
        return await _available_models(
            provider=provider,
            base_url=base_url,
            api_key=None,
            vault=getattr(get_agent_fn(), "credential_vault", None),
            owner_user_id=user.id,
            settings=settings,
        )

    @router.post("/models/available")
    async def list_models_with_credentials(
        request: AvailableModelsRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """List models with an ephemeral credential override (nothing stored).

        POST so the key never appears in a URL or access log; admin-gated like
        the provider connectivity test. Backs the /provider setup flow's model
        step, where the pasted key exists only in the pending-setup state.
        """
        return await _available_models(
            provider=request.provider,
            base_url=request.base_url,
            api_key=(
                request.api_key.get_secret_value() if request.api_key else None
            ),
            vault=getattr(get_agent_fn(), "credential_vault", None),
            owner_user_id=user.id,
            settings=settings,
        )

    return router
