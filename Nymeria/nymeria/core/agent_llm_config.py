"""Per-thread LLM configuration resolution.

Extracted from ``NymeriaAgent``. Builds an :class:`LLMConfig` by layering
per-thread overrides on top of global settings, resolving provider
credentials through the credential vault, deriving CLIProxy-compatible
base URLs for cross-provider overrides, and assembling the fallback
provider chain.

The single entry point ``get_llm_config_for_thread`` is wired into
``NymeriaAgent`` as a thin facade so external callers (agent_graph,
agent_compaction, thread_overview, the chat/settings/threads routers,
and the platform bot routers) keep their existing call shape.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ..config.llm_providers import (
    ALL_LLM_PROVIDERS,
    normalize_llm_provider,
    resolve_provider_api_key,
    resolve_provider_base_url,
)
from ..config.local_llm import (
    detect_local_server_type,
    is_local_llm_base_url,
    query_local_context_length,
    query_ollama_num_ctx,
)
from ..config.model_capabilities import register_model_metadata
from ..vendor.react_agent import LLMConfig, LLMFallbackConfig
from .llm_credentials import (
    get_llm_provider_credential,
    resolve_credential_references,
)

if TYPE_CHECKING:
    from .agent import NymeriaAgent  # noqa: F401


_FALLBACK_PROVIDER_PREFIXES = {"custom", *ALL_LLM_PROVIDERS.keys()}
_LOCAL_PROVIDER_IDS = {"ollama", "lmstudio", "llamacpp", "vllm", "localai", "litellm", "tgi"}


def _resolve_thread_llm_override(thread_value: Any, global_value: Any) -> Any:
    """Resolve a thread LLM override against its global fallback."""
    if thread_value is None:
        return global_value
    if isinstance(thread_value, str) and thread_value == "":
        return global_value
    return thread_value


def _parse_llm_fallback_models(value: Any) -> list[str]:
    """Parse comma/newline-separated fallback model settings."""
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


def _positive_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _split_llm_fallback_ref(
    value: str,
    default_provider: str,
) -> tuple[str, str]:
    """Split provider:model fallback refs while preserving model IDs with colons."""
    prefix, separator, remainder = str(value or "").partition(":")
    provider = normalize_llm_provider(prefix.strip().casefold())
    if separator and provider in _FALLBACK_PROVIDER_PREFIXES and remainder.strip():
        return provider, remainder.strip()
    return str(default_provider or "").strip(), str(value or "").strip()


def get_llm_config_for_thread(
    agent: "NymeriaAgent",
    thread_id: str = "",
) -> LLMConfig:
    """Build LLMConfig with per-thread overrides applied on top of global settings."""
    tc = None
    if thread_id:
        tc_obj = agent.thread_config_manager.get_config(thread_id)
        if tc_obj:
            tc = tc_obj.llm_config

    def resolve(attr: str, global_value: Any) -> Any:
        thread_value = getattr(tc, attr, None) if tc else None
        return _resolve_thread_llm_override(thread_value, global_value)

    provider = normalize_llm_provider(resolve("provider", agent.settings.llm_provider))
    global_provider = normalize_llm_provider(agent.settings.llm_provider)
    model = resolve("model", agent.settings.llm_model)
    temperature = resolve("temperature", agent.settings.llm_temperature)
    max_tokens = resolve("max_tokens", agent.settings.llm_max_tokens)
    extended_thinking = resolve("extended_thinking", agent.settings.llm_extended_thinking)
    reasoning_effort = resolve("reasoning_effort", agent.settings.llm_reasoning_effort)
    use_model_defaults = resolve("use_model_defaults", agent.settings.llm_use_model_defaults)
    context_length_override = _positive_int(
        resolve("context_length", getattr(agent.settings, "llm_context_length", None))
    )
    ollama_num_ctx_override = _positive_int(
        resolve("ollama_num_ctx", getattr(agent.settings, "llm_ollama_num_ctx", None))
    )

    top_p = agent.settings.llm_top_p
    frequency_penalty = agent.settings.llm_frequency_penalty
    presence_penalty = agent.settings.llm_presence_penalty

    # When use_model_defaults is enabled, don't send temperature/top_p/frequency_penalty/
    # presence_penalty — let the provider apply model-specific optimal defaults.
    if use_model_defaults:
        temperature = None
        top_p = None
        frequency_penalty = None
        presence_penalty = None

    owner_user_id = (
        agent.accounts_repo.get_thread_owner(thread_id)
        if thread_id and hasattr(agent, "accounts_repo")
        else None
    )
    credential_vault = getattr(agent, "credential_vault", None)

    provider_credentials: dict[str, Any] = {}

    def vault_credential_for(credential_provider: str):
        credential_provider = normalize_llm_provider(credential_provider)
        if credential_provider not in provider_credentials:
            provider_credentials[credential_provider] = get_llm_provider_credential(
                credential_provider,
                vault=credential_vault,
                owner_user_id=owner_user_id,
                thread_id=thread_id or None,
            )
        return provider_credentials[credential_provider]

    def resolve_explicit_secret(value: str | None, credential_provider: str) -> str | None:
        return resolve_credential_references(
            value,
            vault=credential_vault,
            owner_user_id=owner_user_id,
            provider=credential_provider,
            thread_id=thread_id or None,
        )

    provider_credential = vault_credential_for(provider)

    # Resolve base_url: per-thread override > global when the thread is using
    # the global provider. Empty string ("") = explicit direct API.
    if tc and tc.base_url is not None:
        base_url = resolve_explicit_secret(tc.base_url or None, provider)
    elif provider != global_provider:
        # Per-thread provider differs from global. CLIProxy hosts both the
        # anthropic OAuth path (port 8317 root) and the openai-compat path
        # (port 8317 + /v1) on the same container, so derive the matching
        # URL from the global one when it points at CLIProxy. Without this,
        # the "Anthropic (Subscription)" per-thread option silently falls
        # through to api.anthropic.com direct + ANTHROPIC_DIRECT_API_KEY,
        # billing per-token instead of using the subscription.
        global_url = (agent.settings.llm_base_url or "").rstrip("/")
        if (
            provider == "anthropic"
            and global_url
            and ("cli-proxy" in global_url or "cliproxy" in global_url)
        ):
            base_url = global_url[:-3] if global_url.endswith("/v1") else global_url
        else:
            base_url = None
    else:
        base_url = resolve_explicit_secret(agent.settings.llm_base_url, provider)

    if not base_url and provider_credential and provider_credential.base_url:
        base_url = provider_credential.base_url
    if not base_url:
        base_url = resolve_provider_base_url(
            provider,
            settings=agent.settings,
            include_default=False,
        )

    # Resolve API key: per-thread override → per-provider env key →
    # generic proxy-mode key (global provider). Lets a thread point at a
    # different CLIProxy sidecar with its own auth without touching
    # global settings, while preserving today's behavior when no override
    # is set.
    api_key_override = resolve("api_key", None)
    if api_key_override is not None:
        api_key = resolve_explicit_secret(api_key_override, provider)
    elif provider_credential and provider_credential.api_key:
        api_key = provider_credential.api_key
    else:
        if provider == "anthropic":
            # A configured Anthropic base_url means CLIProxy or another
            # proxy; it expects ANTHROPIC_API_KEY (usually cpx-*). Only use
            # ANTHROPIC_DIRECT_API_KEY for direct Anthropic calls.
            api_key = (
                agent.settings.anthropic_api_key
                if base_url
                else (
                    agent.settings.anthropic_direct_api_key
                    or agent.settings.anthropic_api_key
                )
            )
        else:
            api_key = resolve_provider_api_key(provider, settings=agent.settings)

    probe_base_url = base_url
    if not probe_base_url and provider in _LOCAL_PROVIDER_IDS:
        probe_base_url = resolve_provider_base_url(
            provider,
            settings=agent.settings,
            include_default=True,
        )

    context_length = context_length_override
    ollama_num_ctx = ollama_num_ctx_override
    if model and probe_base_url and is_local_llm_base_url(probe_base_url):
        server_type = detect_local_server_type(probe_base_url, api_key=api_key)
        if context_length is None:
            context_length = query_local_context_length(
                str(model),
                probe_base_url,
                api_key=api_key,
                server_type=server_type,
            )
        if ollama_num_ctx is None and (server_type == "ollama" or provider == "ollama"):
            detected_num_ctx = query_ollama_num_ctx(
                str(model),
                probe_base_url,
                api_key=api_key,
            )
            if detected_num_ctx:
                if context_length_override and detected_num_ctx > context_length_override:
                    ollama_num_ctx = context_length_override
                else:
                    ollama_num_ctx = detected_num_ctx

    if model and context_length:
        register_model_metadata(
            model_id=str(model),
            name=str(model),
            context_length=context_length,
        )

    def base_url_for_provider(fallback_provider: str) -> str | None:
        if fallback_provider == provider:
            return base_url
        global_url = (agent.settings.llm_base_url or "").rstrip("/")
        fallback_credential = vault_credential_for(fallback_provider)
        if fallback_provider == global_provider:
            resolved_global_base = resolve_explicit_secret(
                agent.settings.llm_base_url,
                fallback_provider,
            )
            if resolved_global_base:
                return resolved_global_base
        if global_url and ("cli-proxy" in global_url or "cliproxy" in global_url):
            if fallback_provider == "anthropic":
                return global_url[:-3] if global_url.endswith("/v1") else global_url
            if fallback_provider == "openai":
                return global_url if global_url.endswith("/v1") else f"{global_url}/v1"
        if fallback_credential and fallback_credential.base_url:
            return fallback_credential.base_url
        env_base_url = resolve_provider_base_url(
            fallback_provider,
            settings=agent.settings,
            include_default=False,
        )
        if env_base_url:
            return env_base_url
        return None

    def api_key_for_provider(
        fallback_provider: str,
        fallback_base_url: str | None,
    ) -> str | None:
        if fallback_provider == provider and api_key_override is not None:
            return resolve_explicit_secret(api_key_override, fallback_provider)
        fallback_credential = vault_credential_for(fallback_provider)
        if fallback_credential and fallback_credential.api_key:
            return fallback_credential.api_key
        if fallback_provider == "anthropic":
            return (
                agent.settings.anthropic_api_key
                if fallback_base_url
                else (
                    agent.settings.anthropic_direct_api_key
                    or agent.settings.anthropic_api_key
                )
            )
        return resolve_provider_api_key(fallback_provider, settings=agent.settings)

    fallbacks: list[LLMFallbackConfig] = []
    for fallback_ref in _parse_llm_fallback_models(
        getattr(agent.settings, "llm_fallback_models", "")
    ):
        fallback_provider, fallback_model = _split_llm_fallback_ref(
            fallback_ref,
            str(provider),
        )
        if not fallback_model or (
            fallback_provider == provider and fallback_model == model
        ):
            continue
        fallback_base_url = base_url_for_provider(fallback_provider)
        fallbacks.append(
            LLMFallbackConfig(
                provider=fallback_provider,
                model=fallback_model,
                api_key=api_key_for_provider(
                    fallback_provider,
                    fallback_base_url,
                ),
                base_url=fallback_base_url,
                openai_api_mode=resolve(
                    "openai_api_mode",
                    agent.settings.openai_api_mode,
                ),
                context_length=None,
                ollama_num_ctx=None,
            )
        )

    return LLMConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=top_p,
        top_k=agent.settings.llm_top_k,
        frequency_penalty=frequency_penalty,
        presence_penalty=presence_penalty,
        reasoning_effort=reasoning_effort,
        extended_thinking=extended_thinking,
        context_length=context_length,
        ollama_num_ctx=ollama_num_ctx,
        openai_api_mode=resolve("openai_api_mode", agent.settings.openai_api_mode),
        stream_max_retries=agent.settings.llm_stream_max_retries,
        stream_retry_initial_delay=agent.settings.llm_stream_retry_initial_delay,
        stream_retry_max_delay=agent.settings.llm_stream_retry_max_delay,
        fallbacks=fallbacks,
    )
