"""
LLM Provider Abstraction

Makes it easy to swap between different LLM providers without changing agent code.
Supports OpenRouter, OpenAI, Anthropic, and custom providers.
"""

import logging
from typing import List, Optional
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from .config import LLMConfig

logger = logging.getLogger(__name__)


def create_llm(config: LLMConfig) -> BaseChatModel:
    """
    Create an LLM instance based on configuration.

    Args:
        config: LLMConfig with provider settings

    Returns:
        A LangChain chat model ready to use

    Raises:
        ValueError: If provider is unknown or config is invalid
    """
    if config.custom_llm is not None:
        return config.custom_llm

    if config.provider == "openrouter":
        return _create_openrouter_llm(config)
    elif config.provider == "openai":
        return _create_openai_llm(config)
    elif config.provider == "anthropic":
        return _create_anthropic_llm(config)
    elif config.provider == "custom":
        if config.custom_llm is None:
            raise ValueError("Custom provider requires custom_llm to be set")
        return config.custom_llm
    else:
        raise ValueError(f"Unknown provider: {config.provider}")


def create_llm_with_tools(config: LLMConfig, tools: List[BaseTool]) -> BaseChatModel:
    """
    Create an LLM with tools bound to it.

    Args:
        config: LLMConfig with provider settings
        tools: List of tools to bind

    Returns:
        LLM with tools bound (can generate tool_calls)
    """
    llm = create_llm(config)

    if tools:
        # For OpenRouter: check if model reports supported_parameters and skip
        # bind_tools if "tools" is positively excluded. If no support data,
        # assume tools are supported (conservative policy).
        if config.provider == "openrouter":
            try:
                from nymeria.config.model_capabilities import get_supported_parameters
                supported = get_supported_parameters(config.model)
                if supported and "tools" not in supported:
                    logger.warning(
                        f"[LLM] Model {config.model} does not list 'tools' in supported_parameters — "
                        f"skipping bind_tools. Agent will respond in text only."
                    )
                    return llm
            except Exception as e:
                logger.debug(f"Could not check tool support: {e}")

        return llm.bind_tools(tools)

    return llm


def _create_openrouter_llm(config: LLMConfig) -> BaseChatModel:
    """Create OpenRouter LLM (OpenAI-compatible API)."""
    from langchain_openai import ChatOpenAI

    if not config.api_key:
        raise ValueError("OpenRouter requires OPENROUTER_API_KEY")

    kwargs = {
        "model": config.model,
        "api_key": config.api_key,
        "base_url": config.base_url or "https://openrouter.ai/api/v1",
    }

    # Temperature: only send if not None (None = let OpenRouter apply model defaults)
    if config.temperature is not None:
        kwargs["temperature"] = config.temperature

    # HTTP read timeout — prevents hanging on stalled OpenRouter connections.
    if config.request_timeout is not None:
        kwargs["timeout"] = config.request_timeout

    if config.max_tokens is not None:
        kwargs["max_tokens"] = config.max_tokens
    else:
        # User selected "Default (model limit)" -- look up the model's actual
        # max output tokens from OpenRouter so we don't rely on the upstream
        # provider's default (which can be very low for some models).
        try:
            from nymeria.config.model_capabilities import get_max_output_tokens
            model_limit = get_max_output_tokens(config.model)
            if model_limit:
                kwargs["max_tokens"] = model_limit
                logger.info(
                    f"[LLM] max_tokens not set, using model limit: "
                    f"{model_limit} for {config.model}"
                )
        except Exception as e:
            logger.debug(f"Could not look up model output limit: {e}")

    if config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if config.frequency_penalty is not None:
        kwargs["frequency_penalty"] = config.frequency_penalty
    if config.presence_penalty is not None:
        kwargs["presence_penalty"] = config.presence_penalty

    # Check supported_parameters for smart gating.
    # Conservative policy: only skip a param when we have positive data saying
    # it's unsupported. If cache is empty, no gates activate.
    supported: set = set()
    has_support_data = False
    try:
        from nymeria.config.model_capabilities import get_supported_parameters
        supported = get_supported_parameters(config.model)
        has_support_data = bool(supported)
    except Exception as e:
        logger.debug(f"Could not fetch supported_parameters: {e}")

    # Build OpenRouter reasoning config for extra_body.
    # Only send when extended thinking is explicitly enabled AND the model
    # supports reasoning (or we have no support data to say otherwise).
    if config.extended_thinking:
        if not has_support_data or "reasoning" in supported:
            reasoning_config = {"enabled": True}
            if config.reasoning_effort is not None:
                reasoning_config["effort"] = config.reasoning_effort
            kwargs["extra_body"] = {"reasoning": reasoning_config}
        else:
            logger.warning(
                f"[LLM] Skipping reasoning config for {config.model} — "
                f"'reasoning' not in supported_parameters"
            )

    # Other provider-specific params that aren't first-class ChatOpenAI fields
    model_kwargs = {}
    if config.top_k is not None:
        model_kwargs["top_k"] = config.top_k
    if model_kwargs:
        kwargs["model_kwargs"] = model_kwargs

    return ChatOpenAI(**kwargs)


def _create_openai_llm(config: LLMConfig) -> BaseChatModel:
    """Create direct OpenAI LLM."""
    from langchain_openai import ChatOpenAI
    import os

    api_key = config.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OpenAI requires OPENAI_API_KEY")

    kwargs = {
        "model": config.model,
        "api_key": api_key,
    }

    if config.temperature is not None:
        kwargs["temperature"] = config.temperature

    if config.request_timeout is not None:
        kwargs["timeout"] = config.request_timeout

    if config.base_url:
        kwargs["base_url"] = config.base_url

    if config.max_tokens is not None:
        kwargs["max_tokens"] = config.max_tokens
    if config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if config.frequency_penalty is not None:
        kwargs["frequency_penalty"] = config.frequency_penalty
    if config.presence_penalty is not None:
        kwargs["presence_penalty"] = config.presence_penalty

    # OpenAI reasoning models use reasoning_effort via model_kwargs
    if config.reasoning_effort is not None:
        kwargs["model_kwargs"] = {"reasoning_effort": config.reasoning_effort}

    return ChatOpenAI(**kwargs)


def _patch_langchain_anthropic_proxy_compat():
    """Patch langchain-anthropic to handle CLIProxyAPI response format.

    CLIProxyAPI returns `context_management` as a plain dict, but
    langchain-anthropic expects a Pydantic model with .model_dump().
    """
    try:
        from langchain_anthropic import chat_models

        original = chat_models.ChatAnthropic._make_message_chunk_from_anthropic_event

        def patched(self, event, **kwargs):
            # Wrap dict context_management so .model_dump() works
            if hasattr(event, "context_management") and isinstance(
                event.context_management, dict
            ):

                class _DictWrapper:
                    def __init__(self, d):
                        self._d = d

                    def model_dump(self, **kw):
                        return self._d

                event.context_management = _DictWrapper(event.context_management)
            return original(self, event, **kwargs)

        chat_models.ChatAnthropic._make_message_chunk_from_anthropic_event = patched
    except Exception:
        pass


_patch_langchain_anthropic_proxy_compat()


def _create_anthropic_llm(config: LLMConfig) -> BaseChatModel:
    """Create Anthropic Claude LLM."""
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError:
        raise ImportError(
            "langchain-anthropic is required for Anthropic provider. "
            "Install with: pip install langchain-anthropic"
        )

    import os

    api_key = config.api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("Anthropic requires ANTHROPIC_API_KEY")

    kwargs = {
        "model": config.model,
        "api_key": api_key,
    }

    if config.base_url:
        kwargs["anthropic_api_url"] = config.base_url

    if config.temperature is not None:
        kwargs["temperature"] = config.temperature

    if config.request_timeout is not None:
        kwargs["timeout"] = config.request_timeout

    if config.max_tokens is not None:
        kwargs["max_tokens"] = config.max_tokens
    if config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if config.top_k is not None:
        kwargs["top_k"] = config.top_k

    # Extended thinking: use ChatAnthropic's first-class `thinking` parameter
    # (passing via model_kwargs triggers a deprecation warning)
    if config.extended_thinking or config.reasoning_effort is not None:
        # Claude 4.6 models use adaptive thinking (type=enabled is deprecated)
        # Older models (4.5, 3.7, etc.) still require type=enabled with budget_tokens
        model_name = (config.model or "").lower()
        is_46_model = "opus-4-6" in model_name or "sonnet-4-6" in model_name

        if is_46_model:
            # Adaptive: Claude decides when/how much to think
            # effort defaults to "high" when omitted
            kwargs["thinking"] = {"type": "adaptive"}
            if config.reasoning_effort is not None:
                kwargs["model_kwargs"] = {"output_config": {"effort": config.reasoning_effort}}
        else:
            # Legacy: explicit budget_tokens for older models
            thinking_budget_map = {
                "low": 1024,
                "medium": 4096,
                "high": 16384,
            }
            effort = config.reasoning_effort or "medium"
            budget = thinking_budget_map.get(effort, 4096)
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Legacy thinking requires temperature=1
            if "temperature" in kwargs and kwargs["temperature"] != 1:
                logger.warning(
                    "Extended thinking (type=enabled) requires temperature=1, "
                    f"overriding configured value of {kwargs['temperature']}"
                )
            kwargs["temperature"] = 1

    return ChatAnthropic(**kwargs)


# Models known to have issues with tool calling
# These models may fail when receiving ToolMessage responses
MODELS_WITH_TOOL_ISSUES = {
    "moonshotai/kimi-k2.5": "Provider (Novita) returns error on ToolMessage",
    # Add more as discovered
}

# Models confirmed to work well with tools
MODELS_TOOL_COMPATIBLE = [
    "anthropic/claude-haiku-4.5",
    "anthropic/claude-sonnet-4",
    "google/gemini-2.0-flash-001",
    "openai/gpt-4o",
    "openai/gpt-4o-mini",
    "meta-llama/llama-3.3-70b-instruct",
]


def check_model_compatibility(model: str) -> tuple[bool, str]:
    """
    Check if a model is known to have tool calling issues.

    Args:
        model: The model identifier

    Returns:
        Tuple of (is_compatible, warning_message)
    """
    if model in MODELS_WITH_TOOL_ISSUES:
        return False, MODELS_WITH_TOOL_ISSUES[model]
    return True, ""


# Provider presets for convenience
PROVIDER_PRESETS = {
    "openrouter-gemini": LLMConfig(
        provider="openrouter",
        model="google/gemini-2.0-flash-001",
    ),
    "openrouter-claude": LLMConfig(
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
    ),
    "openrouter-gpt4": LLMConfig(
        provider="openrouter",
        model="openai/gpt-4o",
    ),
    "openai-gpt4": LLMConfig(
        provider="openai",
        model="gpt-4o",
    ),
    "anthropic-claude": LLMConfig(
        provider="anthropic",
        model="claude-sonnet-4-20250514",
    ),
}


def get_preset(name: str) -> LLMConfig:
    """Get a provider preset by name."""
    if name not in PROVIDER_PRESETS:
        raise ValueError(f"Unknown preset: {name}. Available: {list(PROVIDER_PRESETS.keys())}")
    return PROVIDER_PRESETS[name]
