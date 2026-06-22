"""
LLM Provider Abstraction

Makes it easy to swap between different LLM providers without changing agent code.
Dispatches to: Anthropic (native ``anthropic_messages`` API), OpenAI, OpenRouter,
any of the 130+ OpenAI-chat-compatible providers registered in
``nymeria/config/llm_providers.py`` (xAI, Gemini, Groq, DeepSeek, Mistral, Azure,
Together, Fireworks, Perplexity, Ollama, LM Studio, llama.cpp, etc.), CLIProxy
(auto-detected from ``LLM_BASE_URL``), and arbitrary custom ``LLM`` objects.
"""

import asyncio
import atexit
import hashlib
import inspect
import json
import logging
import os
import re
import threading
import weakref
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from importlib import metadata as importlib_metadata
from typing import Any, AsyncIterator, Iterator, List
from urllib.parse import urlparse
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool

from .cliproxy import (
    CACHE_CONTROL_EPHEMERAL as _CACHE_CONTROL_EPHEMERAL,
    CLIPROXY_ANTHROPIC_BETA_HEADER,
    CLIPROXY_CLAUDE_USER_AGENT,
    looks_like_cliproxy_url,
)
from .config import LLMConfig
from nymeria.config.llm_providers import (
    get_llm_provider_spec,
    is_openai_compatible_provider,
    normalize_llm_provider,
    provider_requires_api_key,
    provider_supports_route,
    provider_supports_responses,
    resolve_provider_route,
    resolve_provider_api_key,
    resolve_provider_base_url,
)
from nymeria.config.local_llm import is_local_llm_base_url

try:
    from langchain_openai import ChatOpenAI as _LangChainChatOpenAI
except ImportError as exc:
    _CHAT_OPENAI_IMPORT_ERROR = exc

    class _MissingChatOpenAI:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError(
                "langchain-openai is required for OpenAI-compatible providers"
            ) from _CHAT_OPENAI_IMPORT_ERROR

    _LangChainChatOpenAI = _MissingChatOpenAI
else:
    _CHAT_OPENAI_IMPORT_ERROR = None

logger = logging.getLogger(__name__)


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_OPENROUTER_RESPONSES_FALLBACK_EVENTS = {
    "response.reasoning_text.delta",
    "response.reasoning.delta",
    "response.content_part.delta",
}
_RESPONSES_TEXT_FALLBACK_EVENTS = {
    "response.output_text.delta",
    "response.content_part.delta",
}
_RESPONSES_REASONING_FALLBACK_EVENTS = {
    "response.reasoning_summary_text.delta",
    "response.reasoning_text.delta",
    "response.reasoning.delta",
}
_RESPONSES_CONVERTER_FALLBACK_WARNED: set[str] = set()
_UNVERIFIED_PROVIDER_WARNED: set[str] = set()
_LANGCHAIN_ANTHROPIC_PROXY_PATCH_MAX_MAJOR = 2
_LANGCHAIN_ANTHROPIC_PROXY_CONTEXT_MARKER = "context_management.model_dump"
_ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"
_OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_ANTHROPIC_TIMEOUT_MISSING = object()


@dataclass
class _AnthropicAsyncHttpPool:
    loop_ref: weakref.ReferenceType[asyncio.AbstractEventLoop]
    client: Any


@dataclass
class _OpenAIAsyncHttpPool:
    loop_ref: weakref.ReferenceType[asyncio.AbstractEventLoop]
    client: Any


_anthropic_async_http_pool_lock = threading.RLock()
_anthropic_async_http_pools: dict[
    tuple[int, str, tuple[Any, ...], str | None],
    _AnthropicAsyncHttpPool,
] = {}
_openai_async_http_pool_lock = threading.RLock()
_openai_async_http_pools: dict[
    tuple[int, str, tuple[Any, ...], tuple[Any, ...]],
    _OpenAIAsyncHttpPool,
] = {}


def _looks_like_openrouter_base_url(base_url: Any) -> bool:
    """Return True for OpenRouter-compatible base URLs."""
    return "openrouter.ai" in str(base_url or "").lower()


def _looks_like_groq_base_url(base_url: Any) -> bool:
    """Return True for Groq OpenAI-compatible base URLs."""
    return "api.groq.com" in str(base_url or "").lower()


def _looks_like_sambanova_base_url(base_url: Any) -> bool:
    """Return True for SambaNova Cloud base URLs."""
    return "sambanova.ai" in str(base_url or "").lower()


def _looks_like_vercel_ai_gateway_base_url(base_url: Any) -> bool:
    """Return True for the Vercel AI Gateway base URL (not v0.dev)."""
    return "ai-gateway.vercel.sh" in str(base_url or "").lower()


def _looks_like_aihubmix_base_url(base_url: Any) -> bool:
    """Return True for AIHubMix gateway base URLs."""
    return "aihubmix.com" in str(base_url or "").lower()


# Reasoning-replay dispatch keys on the canonical Nymeria provider id (registry
# key), threaded onto the chat model as `nymeria_provider`. The `_looks_like_*`
# base-URL predicates are a frozen fallback for the providers wired before
# id-threading and for wrappers built without an id (e.g. a direct unit test).
# New providers go in these id maps only, never as new base-URL predicates: a
# provider id is unambiguous where a base URL is not (the Alibaba/Qwen family
# alone spans five hosts, one of which carries no `dashscope` substring at all).

# Provider ids whose chat-completions reasoning replays in OpenRouter's
# `reasoning_details` / `reasoning` shape (signature-preserving).
_OPENROUTER_STYLE_REASONING_PROVIDERS = frozenset({"openrouter", "vercel", "aihubmix"})

# Provider ids that carry prior reasoning as a flat top-level `reasoning_content`
# string, mapped to how it must be replayed:
#   "tool_calls_only": echo on tool-call turns, strip otherwise. The DeepSeek
#     thinking-mode contract, shared by Alibaba/Qwen3.5 (reasoning leaks into
#     content with </think> if omitted on a tool turn;
#     alibabacloud.com/help/en/model-studio/deep-thinking) and Baseten-served
#     thinking-by-default models (DeepSeek V4 / GPT-OSS 400 if omitted on a tool
#     turn; baseten.co/library/deepseek-v3-2). Non-tool turns must strip it
#     (deepseek-reasoner 400s; DashScope says drop it from plain history).
#   "all": echo on every assistant turn with a captured trace. Fireworks
#     (`reasoning_history="preserved"`) and Moonshot/Kimi (`thinking.keep="all"`)
#     keep reasoning across all turns once their enable-toggle is set (applied in
#     _apply_chat_reasoning_toggles).
_FLAT_REASONING_CONTENT_REPLAY_BY_PROVIDER: dict[str, str] = {
    "deepseek": "tool_calls_only",
    "alibaba": "tool_calls_only",
    "alibaba-cn": "tool_calls_only",
    "alibaba-coding-plan": "tool_calls_only",
    "alibaba-coding-plan-cn": "tool_calls_only",
    "qwen-oauth": "tool_calls_only",
    "baseten": "tool_calls_only",
    # Multi-model gateways normalize every backend to a flat reasoning_content
    # string. tool_calls_only is the never-400 default: it satisfies the
    # DeepSeek/Qwen-backed models they proxy (which require the echo on a tool
    # turn and 400 if it rides a plain turn), while "all"-style backends
    # (Kimi/GLM-5 preserved) only lose the plain-turn echo, never error. Together
    # returns reasoning as `reasoning` and LiteLLM/Novita as `reasoning_content`;
    # capture normalizes both to reasoning_content, so replay is uniform.
    "togetherai": "tool_calls_only",
    "novita-ai": "tool_calls_only",
    "litellm": "tool_calls_only",
    "fireworks-ai": "all",
    "firepass": "all",
    "moonshotai": "all",
    "moonshotai-cn": "all",
}


def _supports_openrouter_style_reasoning_replay(
    provider: Any, base_url: Any = None
) -> bool:
    """Return True for providers that accept OpenRouter-style reasoning replay.

    OpenRouter, Vercel AI Gateway, and AIHubMix all return and accept back the
    same assistant-message `reasoning_details` / `reasoning` fields (signature-
    preserving), so one chat-completions replay path serves all three. Other
    OpenAI-compatible providers reject these unknown keys, so the replay stays
    scoped to this set. Keys on the provider id first, base URL as a fallback.
    """
    if provider and str(provider) in _OPENROUTER_STYLE_REASONING_PROVIDERS:
        return True
    return (
        _looks_like_openrouter_base_url(base_url)
        or _looks_like_vercel_ai_gateway_base_url(base_url)
        or _looks_like_aihubmix_base_url(base_url)
    )


def _looks_like_deepseek_base_url(base_url: Any) -> bool:
    """Return True for DeepSeek's direct API base URL."""
    return "api.deepseek.com" in str(base_url or "").lower()


def _flat_reasoning_content_replay_mode(
    provider: Any, base_url: Any = None
) -> str | None:
    """Return how to replay a flat `reasoning_content` string for a provider.

    See `_FLAT_REASONING_CONTENT_REPLAY_BY_PROVIDER` for the modes. Keys on the
    provider id first; falls back to the base-URL predicates for the providers
    wired before id-threading (DeepSeek / Fireworks / Moonshot).
    """
    if provider:
        mode = _FLAT_REASONING_CONTENT_REPLAY_BY_PROVIDER.get(str(provider))
        if mode is not None:
            return mode
    if _looks_like_deepseek_base_url(base_url):
        return "tool_calls_only"
    base = str(base_url or "").lower()
    if "fireworks.ai" in base or "moonshot.ai" in base or "moonshot.cn" in base:
        return "all"
    return None


def _stable_openrouter_responses_id(prefix: str, item: dict[str, Any], index: int) -> str:
    """Generate a deterministic OpenRouter Responses item id."""
    seed = dict(item)
    seed.pop("id", None)
    serialized = json.dumps(seed, sort_keys=True, default=str, separators=(",", ":"))
    digest = hashlib.sha256(f"{index}:{serialized}".encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _strip_inline_thinking_text(text: str) -> str:
    """Remove provider-leaked inline thinking tags from assistant text."""
    if not isinstance(text, str) or not text:
        return text
    if _THINK_OPEN not in text and _THINK_CLOSE not in text:
        return text

    response_parts: list[str] = []
    i = 0
    while i < len(text):
        open_idx = text.find(_THINK_OPEN, i)
        close_idx = text.find(_THINK_CLOSE, i)
        if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
            i = close_idx + len(_THINK_CLOSE)
            while i < len(text) and text[i].isspace():
                i += 1
            continue
        if open_idx == -1:
            response_parts.append(text[i:])
            break

        response_parts.append(text[i:open_idx])
        i = open_idx + len(_THINK_OPEN)
        close_after_open = text.find(_THINK_CLOSE, i)
        if close_after_open == -1:
            break
        i = close_after_open + len(_THINK_CLOSE)
        while i < len(text) and text[i].isspace():
            i += 1

    return "".join(response_parts)


def _strip_inline_thinking_from_responses_content(content: Any) -> Any:
    """Remove leaked inline thinking from Responses assistant message content."""
    if isinstance(content, str):
        return _strip_inline_thinking_text(content)
    if not isinstance(content, list):
        return content

    cleaned: list[Any] = []
    for part in content:
        if isinstance(part, str):
            text = _strip_inline_thinking_text(part)
            if text:
                cleaned.append(text)
            continue
        if not isinstance(part, dict):
            cleaned.append(part)
            continue

        part_type = part.get("type")
        if part_type in {"text", "output_text"}:
            text = _strip_inline_thinking_text(part.get("text", ""))
            if text:
                clean_part = dict(part)
                clean_part["text"] = text
                cleaned.append(clean_part)
        else:
            cleaned.append(part)

    return cleaned


def _normalize_openrouter_responses_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Fill OpenRouter-required Responses history fields LangChain can omit."""
    payload.pop("previous_response_id", None)

    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return payload

    for index, item in enumerate(input_items):
        if not isinstance(item, dict):
            continue

        item_type = item.get("type")
        if item_type == "message" and item.get("role") == "assistant":
            if "content" in item:
                item["content"] = _strip_inline_thinking_from_responses_content(
                    item.get("content")
                )
            item.setdefault("status", "completed")
            if not item.get("id"):
                item["id"] = _stable_openrouter_responses_id("msg", item, index)
        elif item_type == "function_call":
            if not item.get("id"):
                item["id"] = _stable_openrouter_responses_id("fc", item, index)
        elif item_type == "function_call_output":
            if not item.get("id"):
                item["id"] = _stable_openrouter_responses_id(
                    "fc_output", item, index
                )

    return payload


def _normalize_groq_responses_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt a stock Responses payload to Groq's stateless /responses endpoint.

    Groq's Responses API is beta and stateless-only: it does not persist
    response state and documents `store` / `previous_response_id` as
    unsupported. `store` defaults to false on Groq, so dropping the explicit
    field keeps the same (stateless) behavior while avoiding an "unsupported
    parameter" rejection. Groq accepts the nested `reasoning` object (effort)
    as-is, so no reasoning rewrite is needed.
    See https://console.groq.com/docs/responses-api
    """
    payload.pop("store", None)
    payload.pop("previous_response_id", None)
    return payload


def _normalize_sambanova_responses_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt a stock Responses payload to SambaNova's /responses endpoint.

    SambaNova's Responses API (GA) is stateless-only (history is replayed in
    `input`; `previous_response_id` is unsupported) and controls reasoning depth
    with a top-level `reasoning_effort` scalar rather than OpenAI's nested
    `reasoning` object (whose `summary` field is unsupported). Flatten so a
    configured reasoning effort is honored on the way out.
    See https://docs.sambanova.ai/docs/en/features/responses
    """
    payload.pop("store", None)
    payload.pop("previous_response_id", None)
    reasoning = payload.pop("reasoning", None)
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if effort:
            payload["reasoning_effort"] = effort
    return payload


def _openrouter_event_value(event: Any, key: str, default: Any = None) -> Any:
    """Read a field from OpenAI SDK event objects or raw event dictionaries."""
    if isinstance(event, dict):
        return event.get(key, default)

    value = getattr(event, key, default)
    if value is not default:
        return value

    model_extra = getattr(event, "model_extra", None)
    if isinstance(model_extra, dict):
        return model_extra.get(key, default)

    return default


def _advance_responses_content_index(
    current_index: int,
    current_output_index: int,
    current_sub_index: int,
    output_index: int,
    content_index: int | None = None,
) -> tuple[int, int, int]:
    """Mirror LangChain's Responses stream index advancement for fallback events."""
    if content_index is None:
        if current_output_index != output_index:
            current_index += 1
    else:
        if (
            current_output_index != output_index
            or current_sub_index != content_index
        ):
            current_index += 1
        current_sub_index = content_index
    current_output_index = output_index
    return current_index, current_output_index, current_sub_index


def _convert_openrouter_responses_chunk_to_generation_chunk(
    chunk: Any,
    current_index: int,
    current_output_index: int,
    current_sub_index: int,
    metadata: dict[str, Any] | None = None,
) -> tuple[int, int, int, Any | None]:
    """Convert OpenRouter-specific Responses stream events LangChain may skip."""
    event_type = _openrouter_event_value(chunk, "type")
    if event_type not in _OPENROUTER_RESPONSES_FALLBACK_EVENTS:
        return current_index, current_output_index, current_sub_index, None

    delta = _openrouter_event_value(chunk, "delta", "")
    if not delta:
        return current_index, current_output_index, current_sub_index, None

    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk

    output_index = _openrouter_event_value(chunk, "output_index", 0) or 0
    content_index = _openrouter_event_value(chunk, "content_index", 0) or 0

    if event_type == "response.content_part.delta":
        (
            current_index,
            current_output_index,
            current_sub_index,
        ) = _advance_responses_content_index(
            current_index,
            current_output_index,
            current_sub_index,
            output_index,
            content_index,
        )
        content = [{"type": "text", "text": delta, "index": current_index}]
        additional_kwargs: dict[str, Any] = {}
    else:
        (
            current_index,
            current_output_index,
            current_sub_index,
        ) = _advance_responses_content_index(
            current_index,
            current_output_index,
            current_sub_index,
            output_index,
        )
        reasoning_block: dict[str, Any] = {
            "type": "reasoning",
            "summary": [
                {
                    "index": _openrouter_event_value(chunk, "summary_index", 0) or 0,
                    "type": "summary_text",
                    "text": delta,
                }
            ],
            "index": current_index,
        }
        item_id = (
            _openrouter_event_value(chunk, "item_id")
            or _openrouter_event_value(chunk, "response_id")
            or _openrouter_event_value(chunk, "id")
        )
        if item_id:
            reasoning_block["id"] = item_id
        content = [reasoning_block]
        additional_kwargs = {}

    response_metadata = metadata or {}
    response_metadata["model_provider"] = "openai"

    return (
        current_index,
        current_output_index,
        current_sub_index,
        ChatGenerationChunk(
            message=AIMessageChunk(
                content=content,
                response_metadata=response_metadata,
                additional_kwargs=additional_kwargs,
            )
        ),
    )


def _warn_responses_converter_fallback(reason: str) -> None:
    """Log Responses converter fallback once per reason."""
    if reason in _RESPONSES_CONVERTER_FALLBACK_WARNED:
        return
    _RESPONSES_CONVERTER_FALLBACK_WARNED.add(reason)
    logger.warning(
        "[LLM] langchain-openai Responses stream converter unavailable or "
        "incompatible; using Nymeria's limited local fallback (%s). "
        "Text and plaintext reasoning deltas will stream, but final usage "
        "metadata may be reduced until the LangChain adapter is updated.",
        reason,
    )


def _warn_if_unverified_provider(provider: str) -> None:
    """Warn once per provider when the user picks an unverified provider.

    Providers in the ``native`` and ``gateway`` tiers have a known reasoning
    and tool-call story (dedicated partner package, canonical API, or
    smoke-tested gateway). Everything else is OpenAI-chat "compatible" on
    paper but its divergent corners (tool-call deltas, response_format,
    finish_reason, usage shape, reasoning round-trip) have not been
    exercised, so the user should know that hitting one is at-your-own-risk.

    When the registry attaches a ``notes_for_user`` to the provider (e.g.
    DeepSeek's tool-follow-up 400, xAI's Grok 4 reasoning gap), that text
    is included in the warning so the failure mode is explicit.
    """
    spec = get_llm_provider_spec(provider)
    if spec is None or spec.tier != "unverified":
        return
    if provider in _UNVERIFIED_PROVIDER_WARNED:
        return
    _UNVERIFIED_PROVIDER_WARNED.add(provider)
    if spec.notes_for_user:
        logger.warning(
            "[LLM] Provider %r (%s) is in the unverified tier. %s",
            provider,
            spec.label,
            spec.notes_for_user,
        )
    else:
        logger.warning(
            "[LLM] Provider %r (%s) is in the unverified tier: tool-call "
            "streaming and reasoning round-trips are not smoke-tested. "
            "Native-tier providers: anthropic, openai, google, bedrock, "
            "ollama. See docs/chat_completions_providers.md.",
            provider,
            spec.label,
        )


def _convert_responses_chunk_to_generation_chunk_fallback(
    chunk: Any,
    current_index: int,
    current_output_index: int,
    current_sub_index: int,
    metadata: dict[str, Any] | None = None,
) -> tuple[int, int, int, Any | None]:
    """Convert common Responses stream deltas without LangChain private helpers."""
    event_type = _openrouter_event_value(chunk, "type")
    if not event_type:
        return current_index, current_output_index, current_sub_index, None

    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk

    content: list[dict[str, Any]] = []
    response_metadata = metadata or {}
    response_metadata["model_provider"] = "openai"
    additional_kwargs: dict[str, Any] = {}

    if event_type in _RESPONSES_TEXT_FALLBACK_EVENTS:
        delta = _openrouter_event_value(chunk, "delta", "")
        if not delta:
            return current_index, current_output_index, current_sub_index, None
        output_index = _openrouter_event_value(chunk, "output_index", 0) or 0
        content_index = _openrouter_event_value(chunk, "content_index", 0) or 0
        (
            current_index,
            current_output_index,
            current_sub_index,
        ) = _advance_responses_content_index(
            current_index,
            current_output_index,
            current_sub_index,
            output_index,
            content_index,
        )
        content.append({"type": "text", "text": delta, "index": current_index})
    elif event_type == "response.output_text.done":
        output_index = _openrouter_event_value(chunk, "output_index", 0) or 0
        content_index = _openrouter_event_value(chunk, "content_index", 0) or 0
        (
            current_index,
            current_output_index,
            current_sub_index,
        ) = _advance_responses_content_index(
            current_index,
            current_output_index,
            current_sub_index,
            output_index,
            content_index,
        )
        text_block: dict[str, Any] = {
            "type": "text",
            "text": "",
            "index": current_index,
        }
        item_id = _openrouter_event_value(chunk, "item_id")
        if item_id:
            text_block["id"] = item_id
        content.append(text_block)
    elif event_type in _RESPONSES_REASONING_FALLBACK_EVENTS:
        delta = _openrouter_event_value(chunk, "delta", "")
        if not delta:
            return current_index, current_output_index, current_sub_index, None
        output_index = _openrouter_event_value(chunk, "output_index", 0) or 0
        (
            current_index,
            current_output_index,
            current_sub_index,
        ) = _advance_responses_content_index(
            current_index,
            current_output_index,
            current_sub_index,
            output_index,
        )
        reasoning_block: dict[str, Any] = {
            "type": "reasoning",
            "summary": [
                {
                    "index": _openrouter_event_value(chunk, "summary_index", 0) or 0,
                    "type": "summary_text",
                    "text": delta,
                }
            ],
            "index": current_index,
        }
        item_id = (
            _openrouter_event_value(chunk, "item_id")
            or _openrouter_event_value(chunk, "response_id")
            or _openrouter_event_value(chunk, "id")
        )
        if item_id:
            reasoning_block["id"] = item_id
        content.append(reasoning_block)
    elif event_type == "response.created":
        response = _openrouter_event_value(chunk, "response")
        response_id = _openrouter_event_value(response, "id")
        if not response_id:
            return current_index, current_output_index, current_sub_index, None
        response_metadata["id"] = response_id
    else:
        return current_index, current_output_index, current_sub_index, None

    return (
        current_index,
        current_output_index,
        current_sub_index,
        ChatGenerationChunk(
            message=AIMessageChunk(
                content=content,
                response_metadata=response_metadata,
                additional_kwargs=additional_kwargs,
            )
        ),
    )


def _convert_responses_chunk_to_generation_chunk_compat(
    chunk: Any,
    current_index: int,
    current_output_index: int,
    current_sub_index: int,
    schema: Any | None = None,
    metadata: dict[str, Any] | None = None,
    has_reasoning: bool = False,
    output_version: str | None = None,
) -> tuple[int, int, int, Any | None]:
    """Call LangChain's private Responses converter with a local fallback."""
    try:
        from langchain_openai.chat_models import base as lc_openai_base

        convert_chunk = getattr(
            lc_openai_base,
            "_convert_responses_chunk_to_generation_chunk",
        )
    except Exception as exc:
        _warn_responses_converter_fallback(f"missing private converter: {exc}")
        return _convert_responses_chunk_to_generation_chunk_fallback(
            chunk,
            current_index,
            current_output_index,
            current_sub_index,
            metadata=metadata,
        )

    try:
        return convert_chunk(
            chunk,
            current_index,
            current_output_index,
            current_sub_index,
            schema=schema,
            metadata=metadata,
            has_reasoning=has_reasoning,
            output_version=output_version,
        )
    except (AttributeError, KeyError, TypeError) as exc:
        fallback = _convert_responses_chunk_to_generation_chunk_fallback(
            chunk,
            current_index,
            current_output_index,
            current_sub_index,
            metadata=metadata,
        )
        if fallback[3] is not None:
            _warn_responses_converter_fallback(
                f"private converter rejected {type(exc).__name__}: {exc}"
            )
            return fallback
        raise


def _extract_reasoning_text_from_reasoning_details(details: Any) -> str:
    """Extract displayable plaintext from OpenRouter reasoning_details blocks."""
    parts: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value:
            parts.append(value)
        elif isinstance(value, dict):
            if value.get("type") == "reasoning.encrypted":
                return
            add(
                value.get("text")
                or value.get("content")
                or value.get("reasoning")
                or value.get("summary")
            )
        elif isinstance(value, list):
            for item in value:
                add(item)

    add(details)
    return "".join(parts)


def _reasoning_details_for_storage(details: Any) -> Any:
    """Return reasoning_details shaped so LangChain chunk merging keeps order.

    langchain-core merges lists of dicts by matching `index`, which is correct
    for tool-call deltas but corrupts streamed OpenRouter reasoning details by
    concatenating repeated metadata fields such as `format`. The OpenRouter
    `index` field is optional, so dropping it before checkpoint storage keeps
    each streamed detail as a distinct ordered entry.
    """
    if not isinstance(details, list):
        return details

    sanitized: list[Any] = []
    for item in details:
        if isinstance(item, dict):
            clean = dict(item)
            clean.pop("index", None)
            sanitized.append(clean)
        else:
            sanitized.append(item)
    return sanitized


def _reasoning_details_for_payload(details: Any) -> Any:
    """Remove Nymeria/LangChain merge artifacts before replaying details."""
    if not isinstance(details, list):
        return details

    cleaned: list[Any] = []
    known_formats = (
        "unknown",
        "openai-responses-v1",
        "azure-openai-responses-v1",
        "xai-responses-v1",
        "anthropic-claude-v1",
        "google-gemini-v1",
    )
    for item in details:
        if not isinstance(item, dict):
            cleaned.append(item)
            continue
        clean = dict(item)
        fmt = clean.get("format")
        if isinstance(fmt, str):
            for known in known_formats:
                if fmt != known and len(fmt) % len(known) == 0:
                    repeats = len(fmt) // len(known)
                    if repeats > 1 and fmt == known * repeats:
                        clean["format"] = known
                        break
        cleaned.append(clean)
    return cleaned


def _normalize_openai_base_url(base_url: str) -> str:
    """Normalize OpenAI-compatible CLIProxy URLs to include the required /v1 path."""
    clean = base_url.strip().rstrip("/")
    if not clean or not looks_like_cliproxy_url(clean):
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
    # CLIProxy sidecars are OpenAI-compatible proxy servers, not local inference
    # engines, and we rely on streaming to surface reasoning deltas.
    if looks_like_cliproxy_url(base_url):
        return False

    return is_local_llm_base_url(base_url)


def _merge_extra_body(kwargs: dict[str, Any], extra_body: dict[str, Any]) -> None:
    if not extra_body:
        return
    merged = dict(kwargs.get("extra_body") or {})
    for key, value in extra_body.items():
        if key == "options" and isinstance(value, dict):
            options = dict(merged.get("options") or {})
            options.update(value)
            merged["options"] = options
        else:
            merged[key] = value
    kwargs["extra_body"] = merged


def _local_llm_extra_body(config: LLMConfig, base_url: str | None) -> dict[str, Any]:
    """Provider-specific request body additions for local OpenAI-compatible servers."""
    if not base_url or not is_local_llm_base_url(base_url):
        return {}
    provider = normalize_llm_provider(config.provider)
    is_ollama = provider == "ollama" or bool(config.ollama_num_ctx)
    if not is_ollama:
        return {}

    extra_body: dict[str, Any] = {}
    if config.ollama_num_ctx:
        extra_body["options"] = {"num_ctx": int(config.ollama_num_ctx)}

    effort = str(config.reasoning_effort or "").strip().lower()
    reasoning_disabled = (
        (not config.extended_thinking and not effort)
        or effort in {"none", "off"}
    )
    if reasoning_disabled:
        extra_body["think"] = False
    return extra_body


class ChatOpenAIWithReasoning(_LangChainChatOpenAI):
    """ChatOpenAI subclass that preserves OpenAI-compatible reasoning deltas.

    langchain-openai deliberately drops provider-specific reasoning fields from
    chat-completions streams (see langchain_openai.chat_models.base docstring,
    which recommends a provider-specific subclass). Two wire conventions are
    in use across the providers we care about:

    - ``delta.reasoning_content``: CLIProxy Codex sidecar (gpt-5.x), DeepSeek,
      and any chat-completions path that CLIProxy translates from an upstream
      Responses-API ``response.reasoning_summary_text.delta``.
    - ``delta.reasoning``: OpenRouter's unified reasoning field, emitted when
      ``extra_body.reasoning`` is set.

    Both are normalized into ``additional_kwargs["reasoning_content"]`` so the
    agent stream handler can surface them as ``thinking`` SSE events with a
    single code path.
    """

    nymeria_provider: str | None = None
    """Canonical Nymeria provider id (registry key) threaded from the factory.

    Reasoning-replay dispatch keys on this first, falling back to base-URL
    matching when unset (e.g. a wrapper constructed directly in a unit test).
    It is internal bookkeeping only and never reaches the wire payload, which is
    built from the stock OpenAI parameter set.
    """

    def _get_request_payload(
        self,
        input_,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        base_url = getattr(self, "openai_api_base", None)
        provider = getattr(self, "nymeria_provider", None)

        # (A) Provider-scoped Responses-payload normalization for endpoints
        # whose /responses shape diverges from stock OpenAI. Responses payloads
        # are identified by the `input` array (chat-completions use `messages`).
        # Each normalizer is a small, unit-tested pure function. Providers not
        # listed here (openai, nvidia, vercel, xai, azure) take the stock shape.
        if "input" in payload:
            if _looks_like_groq_base_url(base_url):
                return _normalize_groq_responses_payload(payload)
            if _looks_like_sambanova_base_url(base_url):
                return _normalize_sambanova_responses_payload(payload)
            if _looks_like_openrouter_base_url(base_url):
                # OpenRouter's Responses beta requires item id/status fields that
                # LangChain omits; other Responses providers do not.
                return _normalize_openrouter_responses_payload(payload)
            return payload

        # (B) Chat-completions reasoning replay. OpenRouter and OpenRouter-shaped
        # gateways (Vercel AI Gateway, AIHubMix) document assistant-message
        # reasoning replay via `message.reasoning_details` / `message.reasoning`.
        # LangChain stores it in `additional_kwargs` but its serializer drops it.
        # Keep this scoped so direct OpenAI/local providers don't receive unknown
        # message keys.
        if "messages" not in payload:
            return payload

        # OpenRouter-style providers replay reasoning as `reasoning_details` /
        # `reasoning` assistant-message fields.
        if _supports_openrouter_style_reasoning_replay(provider, base_url):
            source_messages = self._convert_input(input_).to_messages()
            for source, wire_message in zip(source_messages, payload["messages"]):
                if (
                    not isinstance(source, AIMessage)
                    or wire_message.get("role") != "assistant"
                ):
                    continue

                if "content" in wire_message:
                    wire_message["content"] = (
                        _strip_inline_thinking_from_responses_content(
                            wire_message.get("content")
                        )
                    )

                extras = source.additional_kwargs or {}
                reasoning_details = extras.get("reasoning_details")
                if reasoning_details:
                    wire_message["reasoning_details"] = (
                        _reasoning_details_for_payload(reasoning_details)
                    )
                    continue

                reasoning = extras.get("reasoning") or extras.get(
                    "reasoning_content"
                )
                if reasoning:
                    wire_message["reasoning"] = reasoning

            return payload

        # (C) Flat `reasoning_content` replay (DeepSeek, Alibaba/Qwen, Baseten,
        # Fireworks, Moonshot). The prior chain of thought rides back as a
        # top-level string on the assistant message. Capture into
        # additional_kwargs already happens on the way in
        # (_convert_chunk_to_generation_chunk).
        flat_replay = _flat_reasoning_content_replay_mode(provider, base_url)
        if flat_replay is not None:
            source_messages = self._convert_input(input_).to_messages()
            for source, wire_message in zip(source_messages, payload["messages"]):
                if (
                    not isinstance(source, AIMessage)
                    or wire_message.get("role") != "assistant"
                ):
                    continue
                reasoning_content = (source.additional_kwargs or {}).get(
                    "reasoning_content"
                )
                if flat_replay == "tool_calls_only":
                    if getattr(source, "tool_calls", None):
                        # Thinking-mode contract (DeepSeek V4 / Qwen3.5 /
                        # Baseten): reasoning_content MUST ride back on tool-call
                        # turns or the provider 400s, or leaks </think> into
                        # content. An empty string satisfies the constraint when
                        # no trace was captured (e.g. pre-feature history).
                        wire_message["reasoning_content"] = reasoning_content or ""
                    else:
                        # deepseek-reasoner 400s if it is present on a non-tool
                        # turn; DashScope says drop it from plain history.
                        wire_message.pop("reasoning_content", None)
                    continue
                if reasoning_content:
                    wire_message["reasoning_content"] = reasoning_content

            return payload

        return payload

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
                reasoning_details = delta.get("reasoning_details")
                reasoning = (
                    delta.get("reasoning_content")
                    or delta.get("reasoning")
                    or _extract_reasoning_text_from_reasoning_details(
                        reasoning_details
                    )
                )
                if reasoning:
                    generation_chunk.message.additional_kwargs[
                        "reasoning_content"
                    ] = reasoning
                if reasoning_details:
                    generation_chunk.message.additional_kwargs[
                        "reasoning_details"
                    ] = _reasoning_details_for_storage(reasoning_details)
        except (AttributeError, KeyError, IndexError, TypeError):
            pass  # reasoning metadata shape varies by provider
        return generation_chunk

    def _stream(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Iterator[Any]:
        """Route Responses streams through the OpenRouter-aware adapter."""
        if self._use_responses_api({**kwargs, **self.model_kwargs}):
            yield from self._stream_responses(*args, **kwargs)
        else:
            yield from super()._stream(*args, **kwargs)

    async def _astream(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        """Route async Responses streams through the OpenRouter-aware adapter."""
        if self._use_responses_api({**kwargs, **self.model_kwargs}):
            async for chunk in self._astream_responses(*args, **kwargs):
                yield chunk
        else:
            async for chunk in super()._astream(*args, **kwargs):
                yield chunk

    def _stream_responses(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ):
        """Route OpenRouter Responses stream events through LangChain first."""
        try:
            import openai
            from langchain_openai.chat_models import base as lc_openai_base
            handle_bad_request = getattr(
                lc_openai_base, "_handle_openai_bad_request", None
            )
            handle_api_error = getattr(
                lc_openai_base, "_handle_openai_api_error", None
            )
        except Exception:
            yield from super()._stream_responses(  # type: ignore[attr-defined]
                messages,
                stop=stop,
                run_manager=run_manager,
                **kwargs,
            )
            return

        self._ensure_sync_client_available()
        kwargs["stream"] = True
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        try:
            if self.include_response_headers:
                raw_context_manager = (
                    self.root_client.with_raw_response.responses.create(**payload)
                )
                context_manager = raw_context_manager.parse()
                headers = {"headers": dict(raw_context_manager.headers)}
            else:
                context_manager = self.root_client.responses.create(**payload)
                headers = {}
            original_schema_obj = kwargs.get("response_format")

            with context_manager as response:
                is_first_chunk = True
                current_index = -1
                current_output_index = -1
                current_sub_index = -1
                has_reasoning = False
                is_openrouter = _looks_like_openrouter_base_url(
                    getattr(self, "openai_api_base", None)
                )
                for chunk in response:
                    metadata = headers if is_first_chunk else {}
                    generation_chunk = None
                    event_type = _openrouter_event_value(chunk, "type")
                    if (
                        is_openrouter
                        and event_type in _OPENROUTER_RESPONSES_FALLBACK_EVENTS
                    ):
                        (
                            current_index,
                            current_output_index,
                            current_sub_index,
                            generation_chunk,
                        ) = _convert_openrouter_responses_chunk_to_generation_chunk(
                            chunk,
                            current_index,
                            current_output_index,
                            current_sub_index,
                            metadata=metadata,
                        )

                    if generation_chunk is None:
                        try:
                            (
                                current_index,
                                current_output_index,
                                current_sub_index,
                                generation_chunk,
                            ) = _convert_responses_chunk_to_generation_chunk_compat(
                                chunk,
                                current_index,
                                current_output_index,
                                current_sub_index,
                                schema=original_schema_obj,
                                metadata=metadata,
                                has_reasoning=has_reasoning,
                                output_version=self.output_version,
                            )
                        except (AttributeError, KeyError, TypeError):
                            if not is_openrouter:
                                raise
                            generation_chunk = None

                    if generation_chunk:
                        if run_manager:
                            run_manager.on_llm_new_token(
                                generation_chunk.text,
                                chunk=generation_chunk,
                            )
                        is_first_chunk = False
                        chunk_content = generation_chunk.message.content
                        if (
                            "reasoning" in generation_chunk.message.additional_kwargs
                            or (
                                isinstance(chunk_content, list)
                                and any(
                                    isinstance(block, dict)
                                    and block.get("type") == "reasoning"
                                    for block in chunk_content
                                )
                            )
                        ):
                            has_reasoning = True
                        yield generation_chunk
        except openai.BadRequestError as e:
            if handle_bad_request:
                handle_bad_request(e)
            else:
                raise
        except openai.APIError as e:
            if handle_api_error:
                handle_api_error(e)
            else:
                raise

    async def _astream_responses(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ):
        """Async OpenRouter Responses stream adapter."""
        try:
            import openai
            from langchain_openai.chat_models import base as lc_openai_base
            handle_bad_request = getattr(
                lc_openai_base, "_handle_openai_bad_request", None
            )
            handle_api_error = getattr(
                lc_openai_base, "_handle_openai_api_error", None
            )
        except Exception:
            async for chunk in super()._astream_responses(  # type: ignore[attr-defined]
                messages,
                stop=stop,
                run_manager=run_manager,
                **kwargs,
            ):
                yield chunk
            return

        kwargs["stream"] = True
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        try:
            if self.include_response_headers:
                raw_context_manager = (
                    await self.root_async_client.with_raw_response.responses.create(
                        **payload
                    )
                )
                context_manager = raw_context_manager.parse()
                headers = {"headers": dict(raw_context_manager.headers)}
            else:
                context_manager = await self.root_async_client.responses.create(
                    **payload
                )
                headers = {}
            original_schema_obj = kwargs.get("response_format")

            async with context_manager as response:
                is_first_chunk = True
                current_index = -1
                current_output_index = -1
                current_sub_index = -1
                has_reasoning = False
                is_openrouter = _looks_like_openrouter_base_url(
                    getattr(self, "openai_api_base", None)
                )
                async for chunk in response:
                    metadata = headers if is_first_chunk else {}
                    generation_chunk = None
                    event_type = _openrouter_event_value(chunk, "type")
                    if (
                        is_openrouter
                        and event_type in _OPENROUTER_RESPONSES_FALLBACK_EVENTS
                    ):
                        (
                            current_index,
                            current_output_index,
                            current_sub_index,
                            generation_chunk,
                        ) = _convert_openrouter_responses_chunk_to_generation_chunk(
                            chunk,
                            current_index,
                            current_output_index,
                            current_sub_index,
                            metadata=metadata,
                        )

                    if generation_chunk is None:
                        try:
                            (
                                current_index,
                                current_output_index,
                                current_sub_index,
                                generation_chunk,
                            ) = _convert_responses_chunk_to_generation_chunk_compat(
                                chunk,
                                current_index,
                                current_output_index,
                                current_sub_index,
                                schema=original_schema_obj,
                                metadata=metadata,
                                has_reasoning=has_reasoning,
                                output_version=self.output_version,
                            )
                        except (AttributeError, KeyError, TypeError):
                            if not is_openrouter:
                                raise
                            generation_chunk = None

                    if generation_chunk:
                        if run_manager:
                            await run_manager.on_llm_new_token(
                                generation_chunk.text,
                                chunk=generation_chunk,
                            )
                        is_first_chunk = False
                        chunk_content = generation_chunk.message.content
                        if (
                            "reasoning" in generation_chunk.message.additional_kwargs
                            or (
                                isinstance(chunk_content, list)
                                and any(
                                    isinstance(block, dict)
                                    and block.get("type") == "reasoning"
                                    for block in chunk_content
                                )
                            )
                        ):
                            has_reasoning = True
                        yield generation_chunk
        except openai.BadRequestError as e:
            if handle_bad_request:
                handle_bad_request(e)
            else:
                raise
        except openai.APIError as e:
            if handle_api_error:
                handle_api_error(e)
            else:
                raise


def _get_chat_openai_with_reasoning():
    """Return the stable, importable ChatOpenAI reasoning subclass."""
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

    provider = normalize_llm_provider(config.provider)
    if provider != config.provider:
        config = dataclass_replace(config, provider=provider)
    provider_route = resolve_provider_route(
        provider,
        route_override=getattr(config, "provider_route", None),
    )
    if provider_route != getattr(config, "provider_route", None):
        config = dataclass_replace(config, provider_route=provider_route)

    _warn_if_unverified_provider(provider)

    if (
        config.openai_api_mode
        and not is_openai_compatible_provider(config.provider)
        and config.provider_route != "openai_compat"
        and config.openai_api_mode != "responses"
    ):
        logger.warning(
            "[LLM] Ignoring openai_api_mode=%s for non-OpenAI-compatible provider %s",
            config.openai_api_mode,
            config.provider,
        )

    if config.provider == "openrouter":
        return _create_openrouter_llm(config)
    elif config.provider == "openai":
        return _create_openai_llm(config)
    elif config.provider == "anthropic":
        return _create_anthropic_llm(config)
    elif config.provider_route == "anthropic_messages" and provider_supports_route(
        config.provider,
        "anthropic_messages",
    ):
        # Route a gateway's Claude models through langchain-anthropic against the
        # gateway's own /v1/messages endpoint (native thinking + signatures).
        # Resolve the gateway's key/base_url the same way the openai_compat path
        # does, so _create_anthropic_llm targets the gateway, not api.anthropic.com.
        gateway_key = config.api_key or resolve_provider_api_key(config.provider)
        if not gateway_key and not provider_requires_api_key(config.provider):
            gateway_key = "not-needed"
        resolved = dataclass_replace(
            config,
            api_key=gateway_key,
            base_url=resolve_provider_base_url(
                config.provider,
                configured_base_url=config.base_url,
                provider_route="anthropic_messages",
            ),
        )
        return _create_anthropic_llm(resolved)
    elif config.provider_route == "openai_compat" and provider_supports_route(
        config.provider,
        "openai_compat",
    ):
        return _create_openai_compatible_llm(config)
    elif config.provider == "google":
        return _create_google_genai_llm(config)
    elif config.provider == "bedrock":
        return _create_bedrock_llm(config)
    elif config.provider == "ollama":
        return _create_ollama_native_llm(config)
    elif is_openai_compatible_provider(config.provider):
        return _create_openai_compatible_llm(config)
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
        # (e.g. `nym_todo` vs `nym_todo_list`) are presented longest-first in the
        # generated tool-call grammar. This prevents a streaming-mode parser
        # bug in llama.cpp's peg-native where the parser commits to the
        # shorter prefix (`nym_todo`) on partial input, then backtracks to the
        # longer name (`nym_todo_list`) and emits a duplicate `name` field in
        # the streamed delta. Standard OpenAI streaming clients accumulate
        # tool_call name deltas by concatenation, producing mangled names
        # like `nym_todonym_todo_list` reaching the agent graph.
        # Upstream bug filed against ggml-org/llama.cpp — this sort is a
        # harmless client-side workaround (tool order does not affect model
        # behavior, only the grammar ordering llama.cpp derives from it).
        sorted_tools = sorted(tools, key=lambda t: (-len(t.name), t.name))

        return llm.bind_tools(sorted_tools)

    return llm


def _create_openrouter_llm(config: LLMConfig) -> BaseChatModel:
    """Create OpenRouter LLM (OpenAI-compatible API)."""
    if not config.api_key:
        raise ValueError("OpenRouter requires OPENROUTER_API_KEY")

    api_mode = config.openai_api_mode or "responses"
    kwargs = {
        "model": config.model,
        "api_key": config.api_key,
        "base_url": config.base_url or "https://openrouter.ai/api/v1",
        "max_retries": 0,
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

    # OpenRouter's unified reasoning config accepts effort values up to xhigh
    # plus "none"; "max" maps onto xhigh and explicit "off" sends effort
    # "none" so reasoning is actively disabled instead of left at the
    # upstream model default (and wins over extended_thinking=True).
    effort_text = str(config.reasoning_effort or "").strip().lower()
    reasoning_disabled = effort_text == "off"
    reasoning_requested = (
        config.extended_thinking or config.reasoning_effort is not None
    ) and not reasoning_disabled
    effort_value = "xhigh" if effort_text == "max" else effort_text

    if api_mode == "responses":
        kwargs["use_responses_api"] = True
        kwargs["output_version"] = "responses/v1"
        kwargs["store"] = False

        if reasoning_disabled or reasoning_requested:
            if not has_support_data or "reasoning" in supported:
                if reasoning_disabled:
                    # No summary key: there is no reasoning to summarize.
                    kwargs["reasoning"] = {"effort": "none"}
                else:
                    kwargs["reasoning"] = {
                        "summary": "auto",
                        "effort": effort_value or "medium",
                    }
            else:
                logger.warning(
                    f"[LLM] Skipping reasoning config for {config.model} — "
                    f"'reasoning' not in supported_parameters"
                )

        logger.info(
            "[LLM] OpenRouter Responses API mode enabled for %s; "
            "replaying full checkpointed history",
            config.model,
        )
    # Chat-completions explicit "off": mirror of the enabled extra_body shape
    # with enabled=false and effort "none" so reasoning is actively disabled.
    elif reasoning_disabled:
        if not has_support_data or "reasoning" in supported:
            kwargs["extra_body"] = {
                "reasoning": {"enabled": False, "effort": "none"}
            }
    # Build OpenRouter chat-completions reasoning config for extra_body.
    # Sent whenever reasoning is requested (extended thinking on, or an
    # explicit effort level alone, matching the Responses branch) AND the
    # model supports reasoning (or we have no support data to say otherwise).
    elif reasoning_requested:
        if not has_support_data or "reasoning" in supported:
            reasoning_config = {"enabled": True}
            if effort_value:
                reasoning_config["effort"] = effort_value
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

    _attach_loop_local_openai_async_http_client(kwargs)
    return ChatOpenAIWithReasoning(**kwargs)


_OPENAI_GPT5_MINOR_RE = re.compile(r"gpt-5\.(\d+)")
_OPENAI_O_SERIES_RE = re.compile(r"(?:^|/)o\d")


def _openai_reasoning_effort_value(model: str, effort: str) -> str | None:
    """Map Nymeria's effort scale onto OpenAI's reasoning effort values.

    Returns the wire value, or None when the reasoning parameter must be
    omitted entirely (unknown models with effort "off").
    Unsupported values HARD-400 on OpenAI, so over-asks degrade to the
    model's ceiling here as a defensive fallback; the real per-model clamp
    happens upstream in agent_llm_config.
    """
    model_text = (model or "").strip().lower()
    effort_text = (effort or "").strip().lower()
    is_codex = "codex" in model_text
    is_o_series = bool(_OPENAI_O_SERIES_RE.search(model_text))
    is_pro = "gpt-5" in model_text and "-pro" in model_text
    minor_match = _OPENAI_GPT5_MINOR_RE.search(model_text)
    if minor_match:
        gpt5_minor = int(minor_match.group(1))
    elif "gpt-5" in model_text:
        gpt5_minor = 0
    else:
        gpt5_minor = None
    supports_xhigh = "codex-max" in model_text or (
        gpt5_minor is not None
        and gpt5_minor >= 2
        and not (is_codex and ("mini" in model_text or "spark" in model_text))
    )

    if is_pro:
        # Pro models reject the lower tiers: gpt-5-pro accepts only "high";
        # gpt-5.2-pro and later list medium/high/xhigh. Reasoning cannot be
        # disabled, so off/low degrade to the model's floor.
        if gpt5_minor is not None and gpt5_minor >= 2:
            if effort_text in {"xhigh", "max"}:
                return "xhigh"
            if effort_text in {"off", "low"}:
                return "medium"
            return effort_text or "high"
        return "high"

    if effort_text == "off":
        if is_o_series:
            # o-series cannot disable reasoning, and omitting the parameter
            # would let it reason at its default medium; lowest tier instead.
            # The upstream clamp already maps off to low for o-series, so
            # this is a defensive fallback for unclamped callers.
            return "low"
        if is_codex:
            # Codex models cannot disable reasoning; lowest tier instead.
            return "low"
        if gpt5_minor is not None and gpt5_minor >= 1:
            return "none"
        if gpt5_minor is not None:
            return "minimal"
        return None
    if effort_text in {"xhigh", "max"}:
        return "xhigh" if supports_xhigh else "high"
    return effort_text or "medium"


def _create_openai_llm(config: LLMConfig) -> BaseChatModel:
    """Create direct OpenAI LLM."""
    base_url = _normalize_openai_base_url(config.base_url) if config.base_url else None
    api_key = config.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key and base_url and is_local_llm_base_url(base_url):
        api_key = "not-needed"
    if not api_key:
        raise ValueError("OpenAI requires OPENAI_API_KEY")

    kwargs = {
        "model": config.model,
        "api_key": api_key,
        "max_retries": 0,
        # Direct OpenAI and the CLIProxy Codex sidecar both ride this path;
        # neither does flat reasoning_content replay, but tagging the id keeps
        # dispatch off base-URL guesswork.
        "nymeria_provider": "openai",
    }

    if config.temperature is not None:
        kwargs["temperature"] = config.temperature

    if config.request_timeout is not None:
        kwargs["timeout"] = config.request_timeout

    if base_url:
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
        _merge_extra_body(kwargs, _local_llm_extra_body(config, base_url))

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
            wire_effort = _openai_reasoning_effort_value(
                config.model,
                str(config.reasoning_effort or "") or "medium",
            )
            if wire_effort is not None:
                reasoning_config = {"effort": wire_effort}
                if wire_effort != "none":
                    # No reasoning happens at effort "none"; omit the summary
                    # request rather than asking for a summary of nothing.
                    reasoning_config["summary"] = "auto"
                kwargs["reasoning"] = reasoning_config

        logger.info(
            "[LLM] OpenAI Responses API mode enabled for %s; "
            "replaying checkpointed Responses items",
            config.model,
        )
    # OpenAI chat-completions reasoning models use reasoning_effort via model_kwargs.
    elif config.reasoning_effort is not None:
        wire_effort = _openai_reasoning_effort_value(
            config.model,
            config.reasoning_effort,
        )
        if wire_effort is not None:
            kwargs["model_kwargs"] = {"reasoning_effort": wire_effort}

    preserve_stream_usage = (
        not config.base_url
        and "OPENAI_API_BASE" not in os.environ
        and "OPENAI_BASE_URL" not in os.environ
    )
    _attach_loop_local_openai_async_http_client(
        kwargs,
        preserve_direct_stream_usage_default=preserve_stream_usage,
    )
    return ChatOpenAIWithReasoning(**kwargs)


_GROK_4_MINOR_RE = re.compile(r"grok-4(?:\.(\d+))?")


def _grok_lacks_reasoning_effort(model_text: str) -> bool:
    """True for the grok-4 lineage (grok-4/-fast/4.1) that 400s on any
    reasoning_effort value. grok-4.2+ accepts none|low|medium|high."""
    match = _GROK_4_MINOR_RE.search(model_text)
    if not match:
        return False
    minor = int(match.group(1)) if match.group(1) else 0
    return minor < 2


def _set_model_kwarg(kwargs: dict[str, Any], key: str, value: Any) -> None:
    model_kwargs = dict(kwargs.get("model_kwargs") or {})
    model_kwargs[key] = value
    kwargs["model_kwargs"] = model_kwargs


def _apply_chat_reasoning_toggles(
    kwargs: dict[str, Any], provider: str, config: LLMConfig
) -> None:
    """Set chat-completions reasoning params + per-provider passback toggles.

    Called only when reasoning is requested (an effort level, "off" included,
    or extended_thinking) and the provider is using Chat Completions. Two
    jobs: translate the clamped effort onto each partner's wire contract, and
    set the per-provider passback toggles that make the flat
    `reasoning_content` replay in `ChatOpenAIWithReasoning._get_request_payload`
    honored. Effort "off" sends each provider's documented disable form where
    one exists; providers without one keep the no-toggle path (model default
    wins). Providers not listed here get no toggle (a no-op).
    """
    effort_text = str(config.reasoning_effort or "").strip().lower()
    is_off = effort_text == "off"
    has_effort = config.reasoning_effort is not None

    if provider == "azure-openai":
        # Azure serves OpenAI models, so reuse the OpenAI per-family wire
        # mapping: "off" becomes none/minimal/low (or omission) by family,
        # and xhigh passes through on families that support it.
        if has_effort:
            wire_effort = _openai_reasoning_effort_value(
                config.model,
                config.reasoning_effort,
            )
            if wire_effort is not None:
                _set_model_kwarg(kwargs, "reasoning_effort", wire_effort)
        return

    if provider == "xai":
        if _grok_lacks_reasoning_effort((config.model or "").lower()):
            # grok-4 / grok-4-fast / grok-4.1 reject the parameter outright.
            return
        if has_effort:
            if is_off:
                effort_value = "none"
            elif effort_text in {"xhigh", "max"}:
                effort_value = "high"
            else:
                effort_value = effort_text
            _set_model_kwarg(kwargs, "reasoning_effort", effort_value)
        return

    if provider in {"fireworks-ai", "firepass"}:
        # Fireworks reasoning_effort accepts none/low/medium/high/xhigh/max
        # (chat-completions API reference); "none" disables on models that
        # allow it. reasoning_history="preserved" keeps prior-turn reasoning
        # in context and only applies while thinking is on.
        if is_off:
            _set_model_kwarg(kwargs, "reasoning_effort", "none")
            return
        model_kwargs = dict(kwargs.get("model_kwargs") or {})
        model_kwargs["reasoning_history"] = "preserved"
        if has_effort:
            model_kwargs["reasoning_effort"] = effort_text
        kwargs["model_kwargs"] = model_kwargs
        return

    if provider in {"moonshotai", "moonshotai-cn"}:
        # Kimi: thinking.type enabled|disabled; keep="all" preserves the chain
        # of thought across turns (platform.kimi.ai K2 thinking guide). The
        # wire has no depth levels: any non-off effort means "enabled".
        if is_off:
            _merge_extra_body(kwargs, {"thinking": {"type": "disabled"}})
        else:
            _merge_extra_body(
                kwargs, {"thinking": {"type": "enabled", "keep": "all"}}
            )
        return

    if provider in {
        "alibaba",
        "alibaba-cn",
        "alibaba-coding-plan",
        "alibaba-coding-plan-cn",
        "qwen-oauth",
    }:
        # Alibaba/Qwen3.x: enable_thinking toggles the chain of thought on the
        # DashScope OpenAI-compatible endpoint (false actively disables hybrid
        # models); reasoning_content then round-trips on tool-call turns via
        # the "tool_calls_only" flat replay
        # (alibabacloud.com/help/en/model-studio/deep-thinking). The wire has
        # no depth levels.
        _merge_extra_body(kwargs, {"enable_thinking": not is_off})
        return

    if provider == "deepseek":
        # DeepSeek V4 thinking contract: thinking.type enabled|disabled plus
        # reasoning_effort high|max (low/medium coerce to high and xhigh to
        # max upstream, api-docs.deepseek.com/guides/thinking_mode).
        if is_off:
            _merge_extra_body(kwargs, {"thinking": {"type": "disabled"}})
            return
        _merge_extra_body(kwargs, {"thinking": {"type": "enabled"}})
        if has_effort:
            _set_model_kwarg(
                kwargs,
                "reasoning_effort",
                "max" if effort_text in {"xhigh", "max"} else "high",
            )
        return

    if provider == "togetherai":
        # Together: reasoning.enabled toggles hybrid models; DeepSeek-V4 and
        # gpt-oss additionally honor reasoning_effort
        # (docs.together.ai/docs/inference/chat/reasoning).
        _merge_extra_body(kwargs, {"reasoning": {"enabled": not is_off}})
        if is_off or not has_effort:
            return
        model_text = (config.model or "").lower()
        if "deepseek-v4" in model_text:
            _set_model_kwarg(
                kwargs,
                "reasoning_effort",
                "max" if effort_text in {"xhigh", "max"} else "high",
            )
        elif "gpt-oss" in model_text:
            _set_model_kwarg(
                kwargs,
                "reasoning_effort",
                "high" if effort_text in {"xhigh", "max"} else effort_text,
            )
        return

    if provider == "novita-ai":
        # Novita: enable_thinking boolean on supported models; no levels.
        _merge_extra_body(kwargs, {"enable_thinking": not is_off})
        return

    if provider == "baseten":
        # Baseten: thinking-default models cannot be disabled, so "off" sends
        # nothing; reasoning_effort low/medium/high rides the payload, with
        # xhigh only on DeepSeek V4 Pro (docs.baseten.co reasoning guide).
        if is_off or not has_effort:
            return
        model_text = (config.model or "").lower()
        if effort_text in {"xhigh", "max"}:
            wire_effort = "xhigh" if "deepseek-v4" in model_text else "high"
        else:
            wire_effort = effort_text
        _set_model_kwarg(kwargs, "reasoning_effort", wire_effort)
        return

    if provider == "litellm":
        # LiteLLM translates its unified reasoning_effort per backend (budgets
        # for legacy Claude, output_config for adaptive, passthrough for
        # OpenAI); "none" disables. Documented set is none|low|medium|high
        # (docs.litellm.ai/docs/reasoning_content), so higher tiers degrade.
        if has_effort or config.extended_thinking:
            if is_off:
                wire_effort = "none"
            elif effort_text in {"xhigh", "max"}:
                wire_effort = "high"
            else:
                wire_effort = effort_text or "medium"
            _set_model_kwarg(kwargs, "reasoning_effort", wire_effort)
        return

    if provider == "vercel":
        # Vercel AI Gateway speaks the OpenRouter-style unified reasoning
        # object on chat completions (effort none..xhigh; "none" disables).
        if is_off:
            _merge_extra_body(
                kwargs, {"reasoning": {"enabled": False, "effort": "none"}}
            )
            return
        reasoning_config: dict[str, Any] = {"enabled": True}
        if has_effort:
            reasoning_config["effort"] = (
                "xhigh" if effort_text == "max" else effort_text
            )
        _merge_extra_body(kwargs, {"reasoning": reasoning_config})
        return

    if provider == "aihubmix":
        # AIHubMix: unified top-level reasoning_effort none..xhigh on chat
        # completions (docs.aihubmix.com unified inference); binary-toggle
        # backends treat any non-none value as "enable thinking".
        if has_effort or config.extended_thinking:
            if is_off:
                wire_effort = "none"
            elif effort_text in {"xhigh", "max"}:
                wire_effort = "xhigh"
            else:
                wire_effort = effort_text or "medium"
            _set_model_kwarg(kwargs, "reasoning_effort", wire_effort)
        return


def _create_openai_compatible_llm(config: LLMConfig) -> BaseChatModel:
    """Create a non-OpenAI provider that speaks the OpenAI chat API shape."""
    provider = normalize_llm_provider(config.provider)
    spec = get_llm_provider_spec(provider)
    label = spec.label if spec else provider
    api_key = config.api_key or resolve_provider_api_key(provider)
    if not api_key and provider_requires_api_key(provider):
        env_hint = ", ".join(spec.api_key_env_vars) if spec else f"{provider.upper()}_API_KEY"
        raise ValueError(f"{label} requires an API key ({env_hint})")
    if not api_key:
        api_key = "not-needed"

    base_url = resolve_provider_base_url(
        provider,
        configured_base_url=config.base_url,
        provider_route=getattr(config, "provider_route", None),
    )
    if not base_url:
        raise ValueError(f"{label} requires a base URL")

    kwargs: dict[str, Any] = {
        "model": config.model,
        "api_key": api_key,
        "base_url": _normalize_openai_base_url(base_url),
        "max_retries": 0,
        # Canonical provider id drives reasoning-replay dispatch in
        # ChatOpenAIWithReasoning (provider-first, base-URL fallback).
        "nymeria_provider": provider,
    }

    if config.temperature is not None:
        kwargs["temperature"] = config.temperature
    if config.request_timeout is not None:
        kwargs["timeout"] = config.request_timeout
    if config.max_tokens is not None:
        kwargs["max_tokens"] = config.max_tokens
    if config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if config.frequency_penalty is not None:
        kwargs["frequency_penalty"] = config.frequency_penalty
    if config.presence_penalty is not None:
        kwargs["presence_penalty"] = config.presence_penalty

    if _should_disable_streaming_for_local_base_url(base_url):
        kwargs["streaming"] = False
        logger.info(
            "[LLM] Local OpenAI-compatible base_url detected (%s); "
            "streaming=False for reliable tool-call parsing",
            base_url,
        )
    _merge_extra_body(kwargs, _local_llm_extra_body(config, base_url))

    api_mode = config.openai_api_mode or (spec.default_api_mode if spec else "chat_completions")
    uses_responses = api_mode == "responses" and provider_supports_responses(provider)
    effort_text = str(config.reasoning_effort or "").strip().lower()
    if uses_responses:
        kwargs["use_responses_api"] = True
        kwargs["output_version"] = "responses/v1"
        kwargs["store"] = False
        reasoning_requested = (
            config.extended_thinking or config.reasoning_effort is not None
        )
        if provider == "vercel":
            # Vercel AI Gateway accepts the OpenRouter-style effort scale
            # (none..xhigh) on Responses too; "off" sends "none" so thinking
            # is actively disabled instead of left at the gateway default.
            if effort_text == "off":
                # No summary key: there is no reasoning to summarize.
                kwargs["reasoning"] = {"effort": "none"}
            elif reasoning_requested:
                reasoning_config = {"summary": "auto"}
                if effort_text in {"xhigh", "max"}:
                    reasoning_config["effort"] = "xhigh"
                else:
                    reasoning_config["effort"] = effort_text or "medium"
                kwargs["reasoning"] = reasoning_config
        # Effort "off" keeps the disabled path (no reasoning config) and wins
        # over extended_thinking. xhigh/max degrade to "high": no other
        # generic compat provider advertises tiers above high.
        elif reasoning_requested and effort_text != "off":
            reasoning_config = {"summary": "auto"}
            if effort_text in {"xhigh", "max"}:
                reasoning_config["effort"] = "high"
            elif effort_text:
                reasoning_config["effort"] = effort_text
            elif config.extended_thinking:
                reasoning_config["effort"] = "medium"
            kwargs["reasoning"] = reasoning_config
        logger.info(
            "[LLM] %s Responses API mode enabled for %s",
            label,
            config.model,
        )
    else:
        if api_mode == "responses":
            logger.info(
                "[LLM] %s does not advertise Responses API support; using Chat Completions for %s",
                label,
                config.model,
            )
        # Chat-completions reasoning params + per-provider multi-turn passback
        # toggles, applied only when reasoning is requested.
        if config.extended_thinking or config.reasoning_effort is not None:
            _apply_chat_reasoning_toggles(kwargs, provider, config)

    model_kwargs = dict(kwargs.get("model_kwargs") or {})
    if config.top_k is not None:
        model_kwargs["top_k"] = config.top_k
    if model_kwargs:
        kwargs["model_kwargs"] = model_kwargs

    _attach_loop_local_openai_async_http_client(kwargs)
    return ChatOpenAIWithReasoning(**kwargs)


class _DictContextManagement:
    """Small adapter matching the Pydantic method LangChain expects."""

    def __init__(self, value: dict[str, Any]):
        self._value = value

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return self._value


def _get_langchain_anthropic_version() -> str | None:
    try:
        return importlib_metadata.version("langchain-anthropic")
    except importlib_metadata.PackageNotFoundError:
        return None


def _langchain_anthropic_version_allows_proxy_patch(
    version: str | None,
) -> tuple[bool, str]:
    if version is None:
        return True, "package version unavailable"

    match = re.match(r"\s*(\d+)", version)
    if not match:
        return False, f"unparseable langchain-anthropic version {version!r}"

    major = int(match.group(1))
    if major >= _LANGCHAIN_ANTHROPIC_PROXY_PATCH_MAX_MAJOR:
        return (
            False,
            f"langchain-anthropic {version} is outside the verified patch range",
        )
    return True, f"langchain-anthropic {version}"


def _langchain_anthropic_method_still_needs_proxy_patch(method: Any) -> tuple[bool, str]:
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError) as exc:
        return False, f"cannot inspect Anthropic stream converter signature: {exc}"

    if "event" not in signature.parameters:
        return False, "Anthropic stream converter signature no longer has event"

    try:
        source = inspect.getsource(method)
    except (OSError, TypeError):
        return True, "source unavailable; assuming known dict incompatibility"

    if _LANGCHAIN_ANTHROPIC_PROXY_CONTEXT_MARKER not in source:
        return False, "upstream converter no longer calls context_management.model_dump()"

    return True, "upstream converter still expects context_management.model_dump()"


def _should_use_cliproxy_context_management_adapter(
    chat_model_cls: type[Any],
) -> tuple[bool, str]:
    version_allowed, version_reason = _langchain_anthropic_version_allows_proxy_patch(
        _get_langchain_anthropic_version()
    )
    if not version_allowed:
        return False, version_reason

    method = getattr(
        chat_model_cls,
        "_make_message_chunk_from_anthropic_event",
        None,
    )
    if method is None:
        return False, "Anthropic stream converter method is missing"

    method_needs_patch, method_reason = (
        _langchain_anthropic_method_still_needs_proxy_patch(method)
    )
    if not method_needs_patch:
        return False, method_reason

    return True, f"{version_reason}; {method_reason}"


def _wrap_cliproxy_context_management_event(event: Any) -> Any:
    context_management = getattr(event, "context_management", None)
    if not isinstance(context_management, dict):
        return event

    wrapped = _DictContextManagement(context_management)
    try:
        event.context_management = wrapped
        logger.debug(
            "[LLM] Wrapped CLIProxy context_management dict for "
            "langchain-anthropic streaming"
        )
        return event
    except Exception:
        model_copy = getattr(event, "model_copy", None)
        if callable(model_copy):
            copied_event = model_copy(update={"context_management": wrapped})
            logger.debug(
                "[LLM] Copied CLIProxy stream event with wrapped "
                "context_management dict for langchain-anthropic streaming"
            )
            return copied_event
        raise


def _get_running_or_current_event_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop


def _normalise_timeout_for_cache(timeout: Any) -> tuple[Any, ...]:
    if timeout is _ANTHROPIC_TIMEOUT_MISSING:
        return ("missing",)

    as_dict = getattr(timeout, "as_dict", None)
    if callable(as_dict):
        return ("httpx.Timeout", tuple(sorted(as_dict().items())))

    try:
        hash(timeout)
    except TypeError:
        return (
            "repr",
            type(timeout).__module__,
            type(timeout).__qualname__,
            repr(timeout),
        )
    return ("value", timeout)


def _normalise_anthropic_base_url(base_url: Any) -> str:
    return str(
        base_url
        or os.environ.get("ANTHROPIC_BASE_URL")
        or _ANTHROPIC_DEFAULT_BASE_URL
    )


def _normalise_anthropic_timeout_for_cache(timeout: Any) -> tuple[Any, ...]:
    return _normalise_timeout_for_cache(timeout)


def _normalise_openai_http_base_url(base_url: Any) -> str:
    return str(
        base_url
        or os.environ.get("OPENAI_API_BASE")
        or os.environ.get("OPENAI_BASE_URL")
        or _OPENAI_DEFAULT_BASE_URL
    )


def _openai_socket_options_for_default_async_client() -> tuple[Any, ...]:
    try:
        from langchain_openai.chat_models import _client_utils as client_utils

        if client_utils._should_bypass_socket_options_for_proxy_env(
            http_socket_options=None,
            http_client=None,
            http_async_client=None,
            openai_proxy=None,
        ):
            return ()
        return client_utils._resolve_socket_options(None)
    except Exception:
        logger.debug(
            "[LLM] Falling back to plain OpenAI async HTTP client settings",
            exc_info=True,
        )
        return ()


def _get_loop_local_openai_http_client(
    *,
    loop: asyncio.AbstractEventLoop,
    base_url: Any,
    timeout: Any = None,
) -> Any:
    from langchain_openai.chat_models import _client_utils as client_utils

    resolved_base_url = _normalise_openai_http_base_url(base_url)
    socket_options = _openai_socket_options_for_default_async_client()
    cache_key = (
        id(loop),
        resolved_base_url,
        _normalise_timeout_for_cache(timeout),
        tuple(socket_options),
    )

    with _openai_async_http_pool_lock:
        cached = _openai_async_http_pools.get(cache_key)
        if (
            cached is not None
            and cached.loop_ref() is loop
            and not getattr(cached.client, "is_closed", False)
        ):
            return cached.client

        http_client = client_utils._build_async_httpx_client(
            resolved_base_url,
            timeout,
            socket_options,
        )
        _openai_async_http_pools[cache_key] = _OpenAIAsyncHttpPool(
            loop_ref=weakref.ref(loop),
            client=http_client,
        )
        return http_client


def _attach_loop_local_openai_async_http_client(
    kwargs: dict[str, Any],
    *,
    preserve_direct_stream_usage_default: bool = False,
) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    kwargs["http_async_client"] = _get_loop_local_openai_http_client(
        loop=loop,
        base_url=kwargs.get("base_url"),
        timeout=kwargs.get("timeout"),
    )
    if preserve_direct_stream_usage_default:
        kwargs.setdefault("stream_usage", True)


def _get_loop_local_anthropic_http_client(
    *,
    loop: asyncio.AbstractEventLoop,
    base_url: Any,
    timeout: Any = _ANTHROPIC_TIMEOUT_MISSING,
    proxy: str | None = None,
) -> Any:
    import anthropic

    resolved_base_url = _normalise_anthropic_base_url(base_url)
    cache_key = (
        id(loop),
        resolved_base_url,
        _normalise_anthropic_timeout_for_cache(timeout),
        str(proxy) if proxy is not None else None,
    )

    with _anthropic_async_http_pool_lock:
        cached = _anthropic_async_http_pools.get(cache_key)
        if (
            cached is not None
            and cached.loop_ref() is loop
            and not getattr(cached.client, "is_closed", False)
        ):
            return cached.client

        client_kwargs: dict[str, Any] = {"base_url": resolved_base_url}
        if timeout is not _ANTHROPIC_TIMEOUT_MISSING:
            client_kwargs["timeout"] = timeout
        if proxy is not None:
            client_kwargs["proxy"] = proxy

        http_client = anthropic.DefaultAsyncHttpxClient(**client_kwargs)
        _anthropic_async_http_pools[cache_key] = _AnthropicAsyncHttpPool(
            loop_ref=weakref.ref(loop),
            client=http_client,
        )
        return http_client


async def close_anthropic_async_http_pools_for_loop(
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Close Anthropic async HTTP pools owned by one event loop."""
    target_loop = loop if loop is not None else asyncio.get_running_loop()
    target_loop_id = id(target_loop)

    with _anthropic_async_http_pool_lock:
        pools = [
            _anthropic_async_http_pools.pop(key)
            for key, pool in list(_anthropic_async_http_pools.items())
            if key[0] == target_loop_id and pool.loop_ref() is target_loop
        ]

    for pool in pools:
        client = pool.client
        if getattr(client, "is_closed", False):
            continue
        try:
            await client.aclose()
        except Exception:
            logger.debug(
                "[LLM] Failed to close Anthropic async HTTP pool for loop %s",
                target_loop_id,
                exc_info=True,
            )


async def close_openai_async_http_pools_for_loop(
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Close OpenAI-compatible async HTTP pools owned by one event loop."""
    target_loop = loop if loop is not None else asyncio.get_running_loop()
    target_loop_id = id(target_loop)

    with _openai_async_http_pool_lock:
        pools = [
            _openai_async_http_pools.pop(key)
            for key, pool in list(_openai_async_http_pools.items())
            if key[0] == target_loop_id and pool.loop_ref() is target_loop
        ]

    for pool in pools:
        client = pool.client
        if getattr(client, "is_closed", False):
            continue
        try:
            await client.aclose()
        except Exception:
            logger.debug(
                "[LLM] Failed to close OpenAI-compatible async HTTP pool for loop %s",
                target_loop_id,
                exc_info=True,
            )


async def close_provider_async_http_pools_for_loop(
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """Close all provider async HTTP pools owned by one event loop."""
    target_loop = loop if loop is not None else asyncio.get_running_loop()
    await close_anthropic_async_http_pools_for_loop(target_loop)
    await close_openai_async_http_pools_for_loop(target_loop)


def _close_async_http_pool_at_exit(
    provider_label: str,
    loop: asyncio.AbstractEventLoop | None,
    client: Any,
) -> None:
    if getattr(client, "is_closed", False):
        return

    try:
        if loop is not None and not loop.is_closed() and loop.is_running():
            future = asyncio.run_coroutine_threadsafe(client.aclose(), loop)
            future.result(timeout=2)
        elif loop is not None and not loop.is_closed():
            loop.run_until_complete(client.aclose())
        else:
            asyncio.run(client.aclose())
    except Exception:
        logger.debug(
            "[LLM] Failed to close %s async HTTP pool during process exit",
            provider_label,
            exc_info=True,
        )


def _close_remaining_anthropic_async_http_pools_at_exit() -> None:
    with _anthropic_async_http_pool_lock:
        pools = list(_anthropic_async_http_pools.values())
        _anthropic_async_http_pools.clear()

    for pool in pools:
        _close_async_http_pool_at_exit("Anthropic", pool.loop_ref(), pool.client)


def _close_remaining_openai_async_http_pools_at_exit() -> None:
    with _openai_async_http_pool_lock:
        pools = list(_openai_async_http_pools.values())
        _openai_async_http_pools.clear()

    for pool in pools:
        _close_async_http_pool_at_exit("OpenAI-compatible", pool.loop_ref(), pool.client)


atexit.register(_close_remaining_anthropic_async_http_pools_at_exit)
atexit.register(_close_remaining_openai_async_http_pools_at_exit)


def _inject_tool_cache_control(payload: dict) -> None:
    """Add cache_control to the last tool definition in an Anthropic API payload.

    Mirrors CLIProxy's strategy: a breakpoint on the last tool caches all
    tool definitions as a prefix. Skips injection if any tool already has
    cache_control (the caller may have set explicit breakpoints).
    """
    tools = payload.get("tools")
    if not tools or not isinstance(tools, list):
        return
    for tool in tools:
        if isinstance(tool, dict) and "cache_control" in tool:
            return
    last_tool = tools[-1]
    if isinstance(last_tool, dict):
        last_tool["cache_control"] = dict(_CACHE_CONTROL_EPHEMERAL)


def _anthropic_chat_model_class_for_config(
    chat_model_cls: type[Any],
    config: LLMConfig,
) -> type[Any]:
    class NymeriaChatAnthropic(chat_model_cls):
        """Anthropic chat model with loop-local async HTTP clients.

        langchain-anthropic caches its default async httpx client globally by
        base URL/timeout. Nymeria can stream normal chat on the API event loop
        while synchronous callable/autonomous workers consume async streams on a
        bridge loop. Cached graph/model instances can be reused on both loops,
        so the SDK HTTP pool underneath each model wrapper must stay loop-local.
        """

        _nymeria_uses_instance_async_client = True

        @property
        def _async_client(self) -> Any:
            import anthropic

            loop = _get_running_or_current_event_loop()
            loop_id = id(loop)
            with _anthropic_async_http_pool_lock:
                wrapper_cache = self.__dict__.setdefault(
                    "_nymeria_async_clients_by_loop",
                    {},
                )
                cached_client = wrapper_cache.get(loop_id)
                cached_http_client = getattr(cached_client, "_client", None)
                if cached_client is not None and not getattr(
                    cached_http_client,
                    "is_closed",
                    False,
                ):
                    return cached_client

                client_params = self._client_params
                http_client_timeout = client_params.get(
                    "timeout",
                    _ANTHROPIC_TIMEOUT_MISSING,
                )
                if http_client_timeout is None:
                    http_client_timeout = _ANTHROPIC_TIMEOUT_MISSING
                http_client = _get_loop_local_anthropic_http_client(
                    loop=loop,
                    base_url=client_params["base_url"],
                    timeout=http_client_timeout,
                    proxy=self.anthropic_proxy or None,
                )
                async_client_params = dict(client_params)
                if async_client_params.get("timeout") is None:
                    async_client_params.pop("timeout", None)
                async_client = anthropic.AsyncClient(
                    **async_client_params,
                    http_client=http_client,
                )
                wrapper_cache[loop_id] = async_client
                return async_client

    if not config.base_url or not looks_like_cliproxy_url(config.base_url):
        # Direct Anthropic: inject cache_control on the last tool definition
        # so the full tool schema prefix is cached across turns.
        class NymeriaChatAnthropicWithToolCaching(NymeriaChatAnthropic):
            def _get_request_payload(self, *args: Any, **kwargs: Any) -> dict:
                payload = super()._get_request_payload(*args, **kwargs)
                _inject_tool_cache_control(payload)
                return payload

        return NymeriaChatAnthropicWithToolCaching

    use_adapter, reason = _should_use_cliproxy_context_management_adapter(
        chat_model_cls
    )
    if not use_adapter:
        logger.info("[LLM] CLIProxy context_management adapter not enabled: %s", reason)
        return NymeriaChatAnthropic

    logger.info("[LLM] Enabling CLIProxy context_management adapter: %s", reason)

    class CLIProxyCompatibleChatAnthropic(NymeriaChatAnthropic):
        def _make_message_chunk_from_anthropic_event(
            self,
            event: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            return super()._make_message_chunk_from_anthropic_event(
                _wrap_cliproxy_context_management_event(event),
                *args,
                **kwargs,
            )

    return CLIProxyCompatibleChatAnthropic


def _create_anthropic_llm(config: LLMConfig) -> BaseChatModel:
    """Create Anthropic Claude LLM."""
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError:
        raise ImportError(
            "langchain-anthropic is required for Anthropic provider. "
            "Install with: pip install langchain-anthropic"
        )

    api_key = config.api_key or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("Anthropic requires ANTHROPIC_API_KEY")

    kwargs = {
        "model": config.model,
        "api_key": api_key,
        "max_retries": 0,
    }

    is_cliproxy_base_url = bool(
        config.base_url and looks_like_cliproxy_url(config.base_url)
    )

    if config.base_url:
        kwargs["anthropic_api_url"] = config.base_url
        if is_cliproxy_base_url:
            # CLIProxy v6.9.36+ gates full cloaking on the client's incoming
            # User-Agent, and its own Anthropic-Beta default can redact visible
            # thinking. Override both only on CLIProxy routes so direct Anthropic
            # calls keep SDK defaults.
            # See Nymeria/docs/cliproxy.md for the full constraints.
            kwargs["default_headers"] = {
                "User-Agent": CLIPROXY_CLAUDE_USER_AGENT,
                "Anthropic-Beta": CLIPROXY_ANTHROPIC_BETA_HEADER,
            }

    # Determine model family for API compatibility
    model_name = (config.model or "").lower()
    from nymeria.config.model_capabilities import anthropic_model_version

    model_version = anthropic_model_version(model_name)
    # Claude 4.7+ removes support for sampling params (temperature, top_p, top_k)
    # and extended thinking budgets. Use adaptive thinking only. The 4.8 /
    # fable / mythos generations share the 4.7 API surface, as do future
    # version bumps (ordinal check, not a marker list).
    is_47_plus = (
        (model_version is not None and model_version >= (4, 7))
        or "fable" in model_name
        or "mythos" in model_name
    )
    is_46_model = model_version == (4, 6)
    uses_adaptive = is_47_plus or is_46_model
    # fable/mythos cannot disable thinking; effort "off" degrades to "low".
    thinking_not_disableable = "fable" in model_name or "mythos" in model_name

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
    effort_text = str(config.reasoning_effort or "").strip().lower()
    if effort_text == "off" and not thinking_not_disableable:
        # Explicit off wins over extended_thinking: omit the thinking /
        # adaptive block entirely (4.6 and legacy disable the same way).
        pass
    elif config.extended_thinking or config.reasoning_effort is not None:
        if uses_adaptive:
            # Adaptive: Claude decides when/how much to think
            thinking_config = {"type": "adaptive"}
            # 4.7+ omits thinking content by default — opt in for streaming
            if is_47_plus:
                thinking_config["display"] = "summarized"
            kwargs["thinking"] = thinking_config
            effort_value = effort_text or None
            if effort_value == "off":
                # fable/mythos cannot disable thinking: lowest tier instead.
                effort_value = "low"
            elif effort_value == "xhigh" and not is_47_plus:
                # 4.6 has no xhigh tier; max is the next tier above.
                effort_value = "max"
            if effort_value is not None:
                kwargs["model_kwargs"] = {"output_config": {"effort": effort_value}}
        else:
            # Legacy: explicit budget_tokens for older models (4.5, 3.7, etc.)
            thinking_budget_map = {
                "low": 1024,
                "medium": 4096,
                "high": 16384,
                "xhigh": 32768,
                "max": 49152,
            }
            effort = effort_text or "medium"
            budget = thinking_budget_map.get(effort, 4096)
            # budget_tokens must stay strictly below max_tokens.
            if config.max_tokens is not None and budget >= config.max_tokens:
                budget = max(1024, config.max_tokens - 1024)
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Legacy thinking requires temperature=1
            if "temperature" in kwargs and kwargs["temperature"] != 1:
                logger.warning(
                    "Extended thinking (type=enabled) requires temperature=1, "
                    f"overriding configured value of {kwargs['temperature']}"
                )
            kwargs["temperature"] = 1

    chat_model_cls = _anthropic_chat_model_class_for_config(ChatAnthropic, config)
    return chat_model_cls(**kwargs)


# langchain-google-genai 4.x interprets max_retries=0 as "use the Google SDK
# default of 5 retries", not "disable retries". Setting it to 1 is the only way
# to actually defer retry policy to Nymeria's centralized stream retry logic.
# https://github.com/langchain-ai/langchain-google/discussions/1422
_GOOGLE_GENAI_DISABLE_RETRIES = 1


def _create_google_genai_llm(config: LLMConfig) -> BaseChatModel:
    """Create a Google Gemini LLM using the dedicated partner package.

    Gemini 3+ returns thought signatures on its reasoning content blocks that
    must round-trip on every tool-call follow-up or the API returns 4xx. The
    OpenAI-compatible shim at https://generativelanguage.googleapis.com/v1beta/openai
    does not surface those signatures, so multi-turn agentic flows with
    reasoning enabled were silently broken. ``langchain-google-genai`` 4.x
    handles signature round-trip natively.

    Trade-off (accepted): the 4.0 release switched transport from gRPC to REST,
    which has been measured by the upstream community at a 50-90% latency
    increase on small Gemini Flash calls. That regression is the price of
    correctness for reasoning + tool-call interleaving on Gemini 3+.
    """
    try:
        from langchain_google_genai import ChatGoogleGenerativeAI
    except ImportError as exc:
        raise ImportError(
            "langchain-google-genai is required for the Google Gemini "
            "provider. Install with: pip install langchain-google-genai"
        ) from exc

    api_key = (
        config.api_key
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_GENERATIVE_AI_API_KEY")
    )
    if not api_key:
        raise ValueError(
            "Google Gemini requires GEMINI_API_KEY (or GOOGLE_GENERATIVE_AI_API_KEY)"
        )

    kwargs: dict[str, Any] = {
        "model": config.model,
        "google_api_key": api_key,
        "max_retries": _GOOGLE_GENAI_DISABLE_RETRIES,
    }

    if config.temperature is not None:
        kwargs["temperature"] = config.temperature
    if config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if config.top_k is not None:
        kwargs["top_k"] = config.top_k
    if config.max_tokens is not None:
        # Note: Gemini uses max_output_tokens, not max_tokens.
        kwargs["max_output_tokens"] = config.max_tokens
    if config.request_timeout is not None:
        kwargs["timeout"] = config.request_timeout

    # Gemini 3+ uses thinking_level (low/medium/high); Gemini 2.5 uses
    # thinking_budget (int tokens). Map Nymeria's reasoning_effort axis onto
    # whichever the model accepts. We pick thinking_level when extended
    # thinking is requested for any 3.x model, and thinking_budget for older
    # 2.x reasoning models.
    if config.extended_thinking or config.reasoning_effort is not None:
        model_name = (config.model or "").lower()
        is_gemini_3_plus = "gemini-3" in model_name or "gemini-4" in model_name
        effort = (str(config.reasoning_effort or "") or "medium").strip().lower()
        if is_gemini_3_plus:
            # thinking_level tops out at "high"; 3.x cannot fully disable
            # thinking, so "off" maps to the documented "minimal" floor.
            # The 3.x pro line does not list "minimal" (3.1 Pro is
            # low/medium/high), so its floor is "low".
            floor = "low" if "pro" in model_name else "minimal"
            level_map = {
                "off": floor,
                "low": "low",
                "medium": "medium",
                "high": "high",
                "xhigh": "high",
                "max": "high",
            }
            kwargs["thinking_level"] = level_map.get(effort, "medium")
        elif effort == "off":
            # 2.5 flash/flash-lite disable with budget 0; 2.5 pro rejects 0
            # and uses 128 as its documented minimum.
            kwargs["thinking_budget"] = 128 if "pro" in model_name else 0
        else:
            budget_map = {
                "low": 1024,
                "medium": 4096,
                "high": 16384,
                "xhigh": 24576,
                "max": 32768,
            }
            budget = budget_map.get(effort, 4096)
            if "pro" not in model_name and budget > 24576:
                # 2.5 flash/flash-lite cap thinking_budget at 24576; only
                # 2.5 pro accepts up to 32768.
                budget = 24576
            kwargs["thinking_budget"] = budget

    return ChatGoogleGenerativeAI(**kwargs)


def _create_bedrock_llm(config: LLMConfig) -> BaseChatModel:
    """Create an AWS Bedrock chat model via ChatBedrockConverse.

    Uses the Converse API (recommended over the legacy ChatBedrock) so the
    same ``thinking`` parameter shape works across all Bedrock models that
    support extended reasoning (Anthropic Claude, etc.). Credentials are
    resolved through boto3's default chain (env vars, ~/.aws/credentials,
    IAM role); Nymeria does not parse inline access-key/secret pairs.

    Future: detect ``model_id`` starting with ``anthropic.`` and specialize to
    ``ChatAnthropicBedrock`` for closer parity with native Anthropic feature
    flags. Out of scope for the Tier 1 adoption.
    """
    try:
        from langchain_aws import ChatBedrockConverse
    except ImportError as exc:
        raise ImportError(
            "langchain-aws is required for the AWS Bedrock provider. "
            "Install with: pip install langchain-aws"
        ) from exc

    kwargs: dict[str, Any] = {
        "model_id": config.model,
        "max_retries": 0,
    }

    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if region:
        kwargs["region_name"] = region

    endpoint = config.base_url or os.getenv("AWS_BEDROCK_ENDPOINT_URL")
    if endpoint:
        kwargs["endpoint_url"] = endpoint

    if config.temperature is not None:
        kwargs["temperature"] = config.temperature
    if config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if config.max_tokens is not None:
        kwargs["max_tokens"] = config.max_tokens
    if config.request_timeout is not None:
        # langchain-aws plumbs HTTP timeouts via the boto3 Config object,
        # not a top-level kwarg. Surface as a hint via additional_kwargs so
        # downstream wrappers can act on it without forcing a botocore import.
        kwargs.setdefault("additional_model_request_fields", {})

    return ChatBedrockConverse(**kwargs)


def _create_ollama_native_llm(config: LLMConfig) -> BaseChatModel:
    """Create an Ollama chat model speaking the native /api/chat protocol.

    ``langchain-ollama`` covers Ollama's native protocol only (port 11434,
    ``/api/chat`` and ``/api/generate``). Reasoning-capable Ollama builds
    (qwen3, deepseek-r1, gpt-oss) surface ``<think>`` content via the
    ``reasoning_content`` channel here, which round-trips natively.

    The OpenAI-compatible shim at ``/v1/chat/completions`` routes through
    ``_create_openai_compatible_llm`` when ``provider_route`` is
    ``openai_compat``. The default ``ollama`` route is native because that is
    the only route that preserves reasoning round-trip for qwen3, deepseek-r1,
    and gpt-oss.
    """
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:
        raise ImportError(
            "langchain-ollama is required for the native Ollama provider. "
            "Install with: pip install langchain-ollama"
        ) from exc

    base_url = config.base_url or os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434"
    # Strip trailing /v1 if present: the native protocol lives at the root, not
    # under /v1 (which is the OpenAI-compat shim).
    base_url = base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[: -len("/v1")]

    kwargs: dict[str, Any] = {
        "model": config.model,
        "base_url": base_url,
    }

    if config.temperature is not None:
        kwargs["temperature"] = config.temperature
    if config.top_p is not None:
        kwargs["top_p"] = config.top_p
    if config.top_k is not None:
        kwargs["top_k"] = config.top_k
    if config.max_tokens is not None:
        # Ollama uses num_predict (max tokens to generate).
        kwargs["num_predict"] = config.max_tokens
    if config.ollama_num_ctx is not None:
        kwargs["num_ctx"] = int(config.ollama_num_ctx)
    if config.request_timeout is not None:
        kwargs["timeout"] = config.request_timeout

    # Native Ollama exposes reasoning via the `reasoning` toggle (string for
    # gpt-oss, bool for qwen3/deepseek-r1). Map both onto reasoning_effort when
    # extended thinking is requested; let the ChatOllama default handle the
    # remaining models.
    if config.extended_thinking or config.reasoning_effort is not None:
        effort = str(config.reasoning_effort or "").strip().lower()
        model_name = (config.model or "").lower()
        if "gpt-oss" in model_name:
            # gpt-oss accepts low/medium/high only and cannot disable
            # reasoning: off degrades to low, xhigh/max to high.
            if effort == "off":
                effort = "low"
            elif effort in {"xhigh", "max"}:
                effort = "high"
            kwargs["reasoning"] = (
                effort if effort in {"low", "medium", "high"} else True
            )
        elif effort == "off":
            # Explicit off wins over extended_thinking.
            kwargs["reasoning"] = False
        else:
            kwargs["reasoning"] = True

    return ChatOllama(**kwargs)
