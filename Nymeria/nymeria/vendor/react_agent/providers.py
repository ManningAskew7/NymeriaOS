"""
LLM Provider Abstraction

Makes it easy to swap between different LLM providers without changing agent code.
Supports OpenRouter, OpenAI, Anthropic, and custom providers.
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
    provider_supports_responses,
    resolve_provider_api_key,
    resolve_provider_base_url,
)

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


_LOCAL_LLM_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}
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
    if looks_like_cliproxy_url(base_url):
        return False

    return host in _LOCAL_LLM_HOSTS


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

    def _get_request_payload(
        self,
        input_,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)

        # OpenRouter documents assistant-message reasoning replay via
        # `message.reasoning` or `message.reasoning_details`. LangChain
        # stores provider-specific data in `additional_kwargs`, but its
        # OpenAI-compatible serializer drops those fields by default.
        # Keep this scoped to OpenRouter payloads so direct OpenAI/local
        # providers don't receive unknown message/history keys.
        if not _looks_like_openrouter_base_url(
            getattr(self, "openai_api_base", None)
        ):
            return payload

        if "input" in payload:
            return _normalize_openrouter_responses_payload(payload)

        if "messages" not in payload:
            return payload

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

    if (
        config.openai_api_mode
        and not is_openai_compatible_provider(config.provider)
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

    reasoning_requested = (
        config.extended_thinking or config.reasoning_effort is not None
    )

    if api_mode == "responses":
        kwargs["use_responses_api"] = True
        kwargs["output_version"] = "responses/v1"
        kwargs["store"] = False

        if reasoning_requested:
            if not has_support_data or "reasoning" in supported:
                kwargs["reasoning"] = {
                    "summary": "auto",
                    "effort": config.reasoning_effort or "medium",
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
    # Build OpenRouter chat-completions reasoning config for extra_body.
    # Only send when extended thinking is explicitly enabled AND the model
    # supports reasoning (or we have no support data to say otherwise).
    elif config.extended_thinking:
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

    _attach_loop_local_openai_async_http_client(kwargs)
    return ChatOpenAIWithReasoning(**kwargs)


def _create_openai_llm(config: LLMConfig) -> BaseChatModel:
    """Create direct OpenAI LLM."""
    api_key = config.api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OpenAI requires OPENAI_API_KEY")

    kwargs = {
        "model": config.model,
        "api_key": api_key,
        "max_retries": 0,
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
    )
    if not base_url:
        raise ValueError(f"{label} requires a base URL")

    kwargs: dict[str, Any] = {
        "model": config.model,
        "api_key": api_key,
        "base_url": _normalize_openai_base_url(base_url),
        "max_retries": 0,
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

    api_mode = config.openai_api_mode or (spec.default_api_mode if spec else "chat_completions")
    if api_mode == "responses":
        if provider_supports_responses(provider):
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
                "[LLM] %s Responses API mode enabled for %s",
                label,
                config.model,
            )
        else:
            logger.info(
                "[LLM] %s does not advertise Responses API support; using Chat Completions for %s",
                label,
                config.model,
            )
    elif config.reasoning_effort is not None and provider in {"azure-openai", "xai"}:
        kwargs["model_kwargs"] = {"reasoning_effort": config.reasoning_effort}

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


_CACHE_CONTROL_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}


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

    chat_model_cls = _anthropic_chat_model_class_for_config(ChatAnthropic, config)
    return chat_model_cls(**kwargs)
