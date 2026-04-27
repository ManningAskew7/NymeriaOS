"""
LLM Provider Abstraction

Makes it easy to swap between different LLM providers without changing agent code.
Supports OpenRouter, OpenAI, Anthropic, and custom providers.
"""

import logging
from typing import List, Optional
from urllib.parse import urlparse
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from .config import LLMConfig

logger = logging.getLogger(__name__)


_LOCAL_LLM_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}
_CLIPROXY_STREAMING_PORTS = {8317, 8318}


def _looks_like_cliproxy_base_url(base_url: str) -> bool:
    """Return True for CLIProxy hostnames or the local ports used by CLIProxy."""
    parse_target = base_url.strip()
    if "://" not in parse_target:
        parse_target = f"http://{parse_target}"

    try:
        parsed = urlparse(parse_target)
    except ValueError:
        return False

    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    if not host:
        return False
    return "cli-proxy" in host or "cliproxy" in host or port in _CLIPROXY_STREAMING_PORTS


def _normalize_openai_base_url(base_url: str) -> str:
    """Normalize OpenAI-compatible CLIProxy URLs to include the required /v1 path."""
    clean = base_url.strip().rstrip("/")
    if not clean or not _looks_like_cliproxy_base_url(clean):
        return clean

    parse_target = clean
    if "://" not in parse_target:
        parse_target = f"http://{parse_target}"

    try:
        parsed = urlparse(parse_target)
    except ValueError:
        return clean

    if (parsed.path or "").rstrip("/") == "/v1":
        return clean
    return f"{clean}/v1"


def _should_disable_streaming_for_local_base_url(base_url: str) -> bool:
    """Return True for known local inference URLs with fragile tool streaming."""
    parse_target = base_url.strip()
    if "://" not in parse_target:
        parse_target = f"http://{parse_target}"

    try:
        parsed = urlparse(parse_target)
    except ValueError:
        return False

    host = (parsed.hostname or "").lower()
    if not host:
        return False

    # CLIProxy sidecars are OpenAI-compatible proxy servers, not local inference
    # engines, and we rely on streaming to surface reasoning deltas.
    if _looks_like_cliproxy_base_url(base_url):
        return False

    return host in _LOCAL_LLM_HOSTS


def _get_chat_openai_with_reasoning():
    """Return a ChatOpenAI subclass that preserves reasoning deltas.

    langchain-openai deliberately drops provider-specific reasoning fields from
    chat-completions streams (see langchain_openai.chat_models.base docstring,
    which recommends a provider-specific subclass). Two wire conventions are
    in use across the providers we care about:

      - `delta.reasoning_content`  — CLIProxy Codex sidecar (gpt-5.x), DeepSeek,
                                     and any chat-completions path that CLIProxy
                                     translates from an upstream Responses-API
                                     `response.reasoning_summary_text.delta`.
      - `delta.reasoning`          — OpenRouter's unified reasoning field,
                                     emitted when `extra_body.reasoning` is set.

    We normalize both into `additional_kwargs["reasoning_content"]` so the
    agent stream handler in core/agent.py can surface them as `thinking`
    SSE events with a single code path.
    """
    from langchain_openai import ChatOpenAI

    class ChatOpenAIWithReasoning(ChatOpenAI):
        def _convert_chunk_to_generation_chunk(
            self, chunk, default_chunk_class, base_generation_info
        ):
            generation_chunk = super()._convert_chunk_to_generation_chunk(
                chunk, default_chunk_class, base_generation_info
            )
            if generation_chunk is None:
                return None
            try:
                choices = (
                    chunk.get("choices")
                    or chunk.get("chunk", {}).get("choices")
                    or []
                )
                if choices:
                    delta = choices[0].get("delta") or {}
                    reasoning = delta.get("reasoning_content") or delta.get(
                        "reasoning"
                    )
                    if reasoning:
                        generation_chunk.message.additional_kwargs[
                            "reasoning_content"
                        ] = reasoning
            except (AttributeError, KeyError, IndexError, TypeError):
                pass
            return generation_chunk

    return ChatOpenAIWithReasoning


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

    if (
        config.openai_api_mode
        and config.provider != "openai"
        and config.openai_api_mode != "responses"
    ):
        logger.warning(
            "[LLM] Ignoring openai_api_mode=%s for non-OpenAI provider %s",
            config.openai_api_mode,
            config.provider,
        )

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

        # Sort tools by name length descending so prefix-overlapping names
        # (e.g. `todo` vs `todo_list`) are presented longest-first in the
        # generated tool-call grammar. This prevents a streaming-mode parser
        # bug in llama.cpp's peg-native where the parser commits to the
        # shorter prefix (`todo`) on partial input, then backtracks to the
        # longer name (`todo_list`) and emits a duplicate `name` field in
        # the streamed delta. Standard OpenAI streaming clients accumulate
        # tool_call name deltas by concatenation, producing mangled names
        # like `todotodo_list` reaching the agent graph.
        # Upstream bug filed against ggml-org/llama.cpp — this sort is a
        # harmless client-side workaround (tool order does not affect model
        # behavior, only the grammar ordering llama.cpp derives from it).
        sorted_tools = sorted(tools, key=lambda t: len(t.name), reverse=True)

        return llm.bind_tools(sorted_tools)

    return llm


def _create_openrouter_llm(config: LLMConfig) -> BaseChatModel:
    """Create OpenRouter LLM (OpenAI-compatible API)."""
    ChatOpenAI = _get_chat_openai_with_reasoning()

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
    ChatOpenAI = _get_chat_openai_with_reasoning()
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
        base_url = _normalize_openai_base_url(config.base_url)
        kwargs["base_url"] = base_url
        if base_url != config.base_url.strip().rstrip("/"):
            logger.info(
                "[LLM] Normalized OpenAI CLIProxy base_url from %s to %s",
                config.base_url,
                base_url,
            )

        # Local LLM servers (llama.cpp, KoboldCpp, LM Studio, Ollama) have
        # unreliable tool-call streaming — the OpenAI streaming protocol's
        # tool_calls deltas are a known-fragile area for local backends
        # (see OpenClaw #5769, llama.cpp #19905/#20260/#20837).
        # Disable streaming so tool_calls are parsed from the full response
        # in one shot. Non-local providers keep streaming for the better UX.
        if _should_disable_streaming_for_local_base_url(base_url):
            kwargs["streaming"] = False
            logger.info(
                f"[LLM] Local base_url detected ({base_url}); "
                f"streaming=False for reliable tool-call parsing"
            )

    if config.max_tokens is not None:
        kwargs["max_tokens"] = config.max_tokens
    if config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if config.frequency_penalty is not None:
        kwargs["frequency_penalty"] = config.frequency_penalty
    if config.presence_penalty is not None:
        kwargs["presence_penalty"] = config.presence_penalty

    if config.openai_api_mode == "responses":
        kwargs["use_responses_api"] = True
        kwargs["output_version"] = "responses/v1"
        kwargs["store"] = False

        if config.extended_thinking or config.reasoning_effort is not None:
            reasoning_config = {"summary": "auto"}
            if config.reasoning_effort is not None:
                reasoning_config["effort"] = config.reasoning_effort
            elif config.extended_thinking:
                reasoning_config["effort"] = "medium"
            kwargs["reasoning"] = reasoning_config

        logger.info(
            "[LLM] OpenAI Responses API mode enabled for %s; "
            "replaying checkpointed Responses items",
            config.model,
        )
    # OpenAI chat-completions reasoning models use reasoning_effort via model_kwargs.
    elif config.reasoning_effort is not None:
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
        # When routed through CLIProxy (v6.9.36+), the cloak gate is the *client's*
        # incoming User-Agent. Sending claude-cli/* skips system-prompt injection,
        # fake user_id, and sensitive-word obfuscation — keeping Nymeria's identity
        # intact while still receiving Claude Max subscription tier. Without this,
        # responses come back as "I'm Claude Code, Anthropic's official CLI…".
        # See Nymeria/docs/cliproxy.md → "Cloak gate" for the full explanation.
        kwargs["default_headers"] = {"User-Agent": "claude-cli/2.1.113"}

    # Determine model family for API compatibility
    model_name = (config.model or "").lower()
    # Claude 4.7+ removes support for sampling params (temperature, top_p, top_k)
    # and extended thinking budgets. Use adaptive thinking only.
    is_47_plus = "opus-4-7" in model_name or "sonnet-4-7" in model_name
    is_46_model = "opus-4-6" in model_name or "sonnet-4-6" in model_name
    uses_adaptive = is_47_plus or is_46_model

    # Sampling parameters — 4.7+ returns 400 for non-default values
    if not is_47_plus and config.temperature is not None:
        kwargs["temperature"] = config.temperature

    if config.request_timeout is not None:
        kwargs["timeout"] = config.request_timeout

    if config.max_tokens is not None:
        kwargs["max_tokens"] = config.max_tokens
    if not is_47_plus and config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if not is_47_plus and config.top_k is not None:
        kwargs["top_k"] = config.top_k

    # Extended thinking: use ChatAnthropic's first-class `thinking` parameter
    # (passing via model_kwargs triggers a deprecation warning)
    if config.extended_thinking or config.reasoning_effort is not None:
        if uses_adaptive:
            # Adaptive: Claude decides when/how much to think
            thinking_config = {"type": "adaptive"}
            # 4.7+ omits thinking content by default — opt in for streaming
            if is_47_plus:
                thinking_config["display"] = "summarized"
            kwargs["thinking"] = thinking_config
            if config.reasoning_effort is not None:
                kwargs["model_kwargs"] = {"output_config": {"effort": config.reasoning_effort}}
        else:
            # Legacy: explicit budget_tokens for older models (4.5, 3.7, etc.)
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
