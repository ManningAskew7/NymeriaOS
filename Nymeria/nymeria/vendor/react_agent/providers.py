"""
LLM Provider Abstraction

Makes it easy to swap between different LLM providers without changing agent code.
Supports OpenRouter, OpenAI, Anthropic, and custom providers.
"""

from typing import List, Optional
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from .config import LLMConfig


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
        return llm.bind_tools(tools)

    return llm


def _create_openrouter_llm(config: LLMConfig) -> BaseChatModel:
    """Create OpenRouter LLM (OpenAI-compatible API)."""
    from langchain_openai import ChatOpenAI

    if not config.api_key:
        raise ValueError("OpenRouter requires OPENROUTER_API_KEY")

    kwargs = {
        "model": config.model,
        "temperature": config.temperature,
        "api_key": config.api_key,
        "base_url": config.base_url or "https://openrouter.ai/api/v1",
    }

    if config.max_tokens is not None:
        kwargs["max_tokens"] = config.max_tokens
    if config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if config.frequency_penalty is not None:
        kwargs["frequency_penalty"] = config.frequency_penalty
    if config.presence_penalty is not None:
        kwargs["presence_penalty"] = config.presence_penalty

    # OpenRouter passes extra params via model_kwargs
    model_kwargs = {}
    if config.top_k is not None:
        model_kwargs["top_k"] = config.top_k
    if config.reasoning_effort is not None:
        model_kwargs["reasoning_effort"] = config.reasoning_effort
    if config.extended_thinking:
        model_kwargs["reasoning"] = {"enabled": True}

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
        "temperature": config.temperature,
        "api_key": api_key,
    }

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
        "temperature": config.temperature,
        "api_key": api_key,
    }

    if config.max_tokens is not None:
        kwargs["max_tokens"] = config.max_tokens
    if config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if config.top_k is not None:
        kwargs["top_k"] = config.top_k

    # Anthropic doesn't support frequency_penalty/presence_penalty directly
    # but does support extended thinking via model_kwargs for compatible models
    if config.extended_thinking or config.reasoning_effort is not None:
        # Map reasoning_effort to Anthropic's thinking budget tokens
        thinking_budget_map = {
            "low": 1024,
            "medium": 4096,
            "high": 16384,
        }
        effort = config.reasoning_effort or "medium"
        budget = thinking_budget_map.get(effort, 4096)
        kwargs["model_kwargs"] = {"thinking": {"type": "enabled", "budget_tokens": budget}}

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
