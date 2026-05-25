"""
Graph Nodes for the ReAct Agent

Modular node creation that accepts configuration for easy framework integration.
"""

import asyncio
import concurrent.futures
import hashlib
import inspect
import json
import logging
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, List, Literal, Optional, cast
from langchain_core.callbacks.manager import (
    adispatch_custom_event,
    dispatch_custom_event,
)
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, SystemMessage, HumanMessage, ToolCall, ToolMessage
from langchain_core.messages.utils import message_chunk_to_message
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool
from langchain_core.language_models import BaseChatModel
from langgraph.prebuilt import ToolNode
from langgraph.types import Command
from pydantic import BaseModel

from .cliproxy import CLIPROXY_BILLING_SYSTEM_BLOCK, looks_like_cliproxy_url
from .state import AgentState
from .config import AgentConfig, LLMConfig, default_config
from .providers import create_llm_with_tools

logger = logging.getLogger(__name__)

TURN_SAFETY_REASON_MAX_ITERATIONS = "max_iterations"
TURN_SAFETY_REASON_REPEATED_TOOL_RESULT = "repeated_tool_result"
_RETRYABLE_STATUS_CODES = {408, 409, 425, 429}
_RETRYABLE_EXCEPTION_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "ConnectError",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectTimeout",
    "HTTPError",
    "NetworkError",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "TimeoutError",
    "WriteError",
    "WriteTimeout",
}
_RETRYABLE_ERROR_MARKERS = (
    "server_error",
    "internal server error",
    "temporarily unavailable",
    "service unavailable",
    "overloaded",
    "upstream error",
    "bad gateway",
    "gateway timeout",
    "connection reset",
    "connection aborted",
    "connection failed",
    "connection error",
    "stream disconnected",
    "stream closed",
    "remote protocol",
    "read timeout",
    "timed out",
    "timeout",
)
_CONTEXT_OVERFLOW_ERROR_MARKERS = (
    "context_length_exceeded",
    "context window",
    "maximum context length",
    "max context length",
    "too many tokens",
    "input is too long",
    "prompt is too long",
    "reduce the length",
)
_NON_RETRYABLE_ERROR_MARKERS = (
    *_CONTEXT_OVERFLOW_ERROR_MARKERS,
    "bad request",
    "invalid_request_error",
    "invalid request",
    "invalid schema",
    "schema validation",
    "invalid tool",
    "tool payload",
    "invalid function",
    "invalid json",
    "malformed",
    "unauthorized",
    "authentication",
    "api key",
    "permission denied",
    "forbidden",
    "insufficient_quota",
    "insufficient credits",
    "insufficient credit",
    "quota exceeded",
    "billing",
)


@dataclass(frozen=True)
class TurnSafetyResult:
    """Result of evaluating whether the current ReAct turn should stop."""

    should_stop: bool = False
    reason: Optional[str] = None
    tool_call_count: int = 0
    max_iterations: int = 0
    repeated_tool_name: Optional[str] = None
    repeated_count: Optional[int] = None


@dataclass(frozen=True)
class _CompletedToolExchange:
    signature: str
    tool_name: str
    result_hash: str


def _json_default(value: Any) -> str:
    return repr(value)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=_json_default,
        )
    except Exception:
        return repr(value)


def _safe_error_text(value: Any, *, max_chars: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", "replace")
    elif isinstance(value, str):
        text = value
    else:
        text = repr(value)
    return text[:max_chars]


def _safe_response_error_parts(response: Any) -> List[str]:
    parts: List[str] = []
    for attr in ("status_code", "reason_phrase"):
        try:
            parts.append(_safe_error_text(getattr(response, attr, None)))
        except Exception:
            continue
    for attr in ("text", "content"):
        try:
            parts.append(_safe_error_text(getattr(response, attr, None)))
        except Exception:
            continue
    return parts


def _iter_exception_chain(exc: BaseException) -> List[BaseException]:
    seen: set[int] = set()
    stack: List[BaseException] = [exc]
    chain: List[BaseException] = []

    while stack:
        current = stack.pop(0)
        ident = id(current)
        if ident in seen:
            continue
        seen.add(ident)
        chain.append(current)

        grouped = getattr(current, "exceptions", None)
        if isinstance(grouped, (list, tuple)):
            stack.extend(e for e in grouped if isinstance(e, BaseException))

        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        if isinstance(cause, BaseException):
            stack.append(cause)
        if isinstance(context, BaseException):
            stack.append(context)

    return chain


def _extract_status_code(exc: BaseException) -> Optional[int]:
    for current in _iter_exception_chain(exc):
        for attr in ("status_code", "status", "http_status"):
            status = getattr(current, attr, None)
            if isinstance(status, int):
                return status
        response = getattr(current, "response", None)
        if response is not None:
            status = getattr(response, "status_code", None)
            if isinstance(status, int):
                return status
    return None


def _llm_exception_text(exc: BaseException) -> str:
    parts: List[str] = []
    for current in _iter_exception_chain(exc):
        parts.append(_safe_error_text(current))
        parts.append(_safe_error_text(repr(current)))
        for attr in ("code", "type", "body", "message", "error"):
            parts.append(_safe_error_text(getattr(current, attr, None)))
        response = getattr(current, "response", None)
        if response is not None:
            parts.extend(_safe_response_error_parts(response))
    return " ".join(part for part in parts if part).lower()


def _is_retryable_llm_error(exc: BaseException) -> bool:
    text = _llm_exception_text(exc)
    if any(marker in text for marker in _NON_RETRYABLE_ERROR_MARKERS):
        return False

    status_code = _extract_status_code(exc)
    if status_code is not None:
        return status_code in _RETRYABLE_STATUS_CODES or status_code >= 500

    if any(
        current.__class__.__name__ in _RETRYABLE_EXCEPTION_NAMES
        for current in _iter_exception_chain(exc)
    ):
        return True

    return any(marker in text for marker in _RETRYABLE_ERROR_MARKERS)


def is_retryable_llm_error(exc: BaseException) -> bool:
    return _is_retryable_llm_error(exc)


def is_context_overflow_error(exc: BaseException) -> bool:
    """Return True when a provider error means the request exceeded context."""
    text = _llm_exception_text(exc)
    return any(marker in text for marker in _CONTEXT_OVERFLOW_ERROR_MARKERS)


def _llm_retry_delay(llm_config: Optional[LLMConfig], retry_index: int) -> float:
    initial = max(0.0, float(getattr(llm_config, "stream_retry_initial_delay", 1.0) or 0.0))
    maximum = max(0.0, float(getattr(llm_config, "stream_retry_max_delay", 8.0) or 0.0))
    if initial == 0.0 or maximum == 0.0:
        return 0.0
    return min(maximum, initial * (2 ** max(0, retry_index - 1)))


def _llm_max_retries(llm_config: Optional[LLMConfig]) -> int:
    return max(0, int(getattr(llm_config, "stream_max_retries", 2) or 0))


def llm_max_retries(llm_config: Optional[LLMConfig]) -> int:
    return _llm_max_retries(llm_config)


def llm_retry_delay(llm_config: Optional[LLMConfig], retry_index: int) -> float:
    return _llm_retry_delay(llm_config, retry_index)


def _llm_fallbacks(llm_config: Optional[LLMConfig]) -> list[Any]:
    if llm_config is None:
        return []
    fallbacks = getattr(llm_config, "fallbacks", None) or []
    return list(fallbacks)


def _llm_candidate_count(llm_config: Optional[LLMConfig]) -> int:
    return 1 + len(_llm_fallbacks(llm_config))


def _llm_initial_candidate_index(llm_config: Optional[LLMConfig]) -> int:
    if llm_config is None:
        return 0
    candidate_count = _llm_candidate_count(llm_config)
    try:
        active_index = int(getattr(llm_config, "active_fallback_candidate_index", 0) or 0)
    except (TypeError, ValueError):
        return 0
    if active_index <= 0:
        return 0
    return min(active_index, max(0, candidate_count - 1))


def _mark_llm_fallback_active(
    llm_config: Optional[LLMConfig],
    candidate_index: int,
) -> None:
    if llm_config is None:
        return
    try:
        llm_config.active_fallback_candidate_index = candidate_index
    except Exception:
        logger.debug("[LLM FALLBACK] Failed to mark active fallback candidate")


def _llm_candidate_label(llm_config: Optional[LLMConfig], candidate_index: int) -> str:
    if candidate_index == 0:
        model = getattr(llm_config, "model", None) if llm_config is not None else None
        return str(model or "primary")

    fallbacks = _llm_fallbacks(llm_config)
    try:
        fallback = fallbacks[candidate_index - 1]
    except IndexError:
        return f"fallback #{candidate_index}"

    if isinstance(fallback, str):
        return fallback
    model = str(getattr(fallback, "model", "") or "").strip()
    provider = str(getattr(fallback, "provider", "") or "").strip()
    if provider and model:
        return f"{provider}:{model}"
    return model or f"fallback #{candidate_index}"


def _llm_candidate_descriptor(
    llm_config: Optional[LLMConfig],
    candidate_index: int,
) -> dict[str, Any]:
    if llm_config is None:
        return {
            "provider": "unknown",
            "model": _llm_candidate_label(None, candidate_index),
            "provider_route": None,
            "openai_api_mode": None,
        }
    if candidate_index == 0:
        return {
            "provider": llm_config.provider,
            "model": llm_config.model,
            "provider_route": getattr(llm_config, "provider_route", None),
            "openai_api_mode": getattr(llm_config, "openai_api_mode", None),
        }

    fallbacks = _llm_fallbacks(llm_config)
    try:
        fallback = fallbacks[candidate_index - 1]
    except IndexError:
        return {
            "provider": llm_config.provider,
            "model": _llm_candidate_label(llm_config, candidate_index),
            "provider_route": getattr(llm_config, "provider_route", None),
            "openai_api_mode": getattr(llm_config, "openai_api_mode", None),
        }
    if isinstance(fallback, str):
        return {
            "provider": llm_config.provider,
            "model": fallback,
            "provider_route": getattr(llm_config, "provider_route", None),
            "openai_api_mode": getattr(llm_config, "openai_api_mode", None),
        }
    return {
        "provider": getattr(fallback, "provider", None) or llm_config.provider,
        "model": getattr(fallback, "model", "") or "",
        "provider_route": getattr(fallback, "provider_route", None)
        or getattr(llm_config, "provider_route", None),
        "openai_api_mode": getattr(fallback, "openai_api_mode", None)
        or getattr(llm_config, "openai_api_mode", None),
    }


def _llm_retry_reason(exc: BaseException) -> str:
    status_code = _extract_status_code(exc)
    if status_code is not None:
        if status_code >= 500:
            return "provider_server_error"
        if status_code == 429:
            return "rate_limited"
        return "retryable_http_error"

    if any(
        current.__class__.__name__ in _RETRYABLE_EXCEPTION_NAMES
        for current in _iter_exception_chain(exc)
    ):
        text = _llm_exception_text(exc)
        if "timeout" in text or "timed out" in text:
            return "timeout"
        return "transport_error"
    return "transient_provider_error"


def _llm_retry_payload(
    llm_config: Optional[LLMConfig],
    candidate_index: int,
    *,
    attempt: int,
    max_retries: int,
    delay: float,
    exc: BaseException,
) -> dict[str, Any]:
    candidate = _llm_candidate_descriptor(llm_config, candidate_index)
    return {
        "provider": candidate["provider"],
        "model": candidate["model"],
        "provider_route": candidate["provider_route"],
        "openai_api_mode": candidate["openai_api_mode"],
        "attempt": attempt,
        "max_retries": max_retries,
        "delay_seconds": delay,
        "reason": _llm_retry_reason(exc),
        "http_status": _extract_status_code(exc),
    }


def llm_retry_payload_for_active_candidate(
    llm_config: Optional[LLMConfig],
    *,
    attempt: int,
    max_retries: int,
    delay: float,
    exc: BaseException,
) -> dict[str, Any]:
    return _llm_retry_payload(
        llm_config,
        _llm_initial_candidate_index(llm_config),
        attempt=attempt,
        max_retries=max_retries,
        delay=delay,
        exc=exc,
    )


def llm_activate_next_fallback(
    llm_config: Optional[LLMConfig],
    *,
    exc: BaseException,
) -> Optional[dict[str, Any]]:
    from_index = _llm_initial_candidate_index(llm_config)
    to_index = from_index + 1
    if to_index >= _llm_candidate_count(llm_config):
        return None
    payload = _llm_fallback_payload(
        llm_config,
        from_index,
        to_index,
        exc=exc,
    )
    payload = _activate_llm_fallback(llm_config, payload)
    _mark_llm_fallback_active(llm_config, to_index)
    return payload


def _llm_fallback_payload(
    llm_config: Optional[LLMConfig],
    from_index: int,
    to_index: int,
    *,
    exc: BaseException,
) -> dict[str, Any]:
    from_candidate = _llm_candidate_descriptor(llm_config, from_index)
    to_candidate = _llm_candidate_descriptor(llm_config, to_index)
    return {
        "from_provider": from_candidate["provider"],
        "from_model": from_candidate["model"],
        "from_provider_route": from_candidate["provider_route"],
        "from_openai_api_mode": from_candidate["openai_api_mode"],
        "to_provider": to_candidate["provider"],
        "to_model": to_candidate["model"],
        "to_provider_route": to_candidate["provider_route"],
        "to_openai_api_mode": to_candidate["openai_api_mode"],
        "reason": _llm_retry_reason(exc),
        "http_status": _extract_status_code(exc),
    }


def _activate_llm_fallback(
    llm_config: Optional[LLMConfig],
    payload: dict[str, Any],
) -> dict[str, Any]:
    callback = getattr(llm_config, "fallback_activation_callback", None)
    if callback is None:
        return payload
    try:
        activation = callback(dict(payload))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[LLM FALLBACK] fallback activation callback failed: %s", exc)
        return payload
    if isinstance(activation, dict):
        return {**payload, **activation}
    return payload


def _dispatch_provider_event(
    name: str,
    payload: dict[str, Any],
    run_config: Any,
) -> None:
    try:
        dispatch_custom_event(name, payload, config=run_config)
    except RuntimeError:
        logger.debug("[LLM] No callback manager for %s event", name)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[LLM] Failed to dispatch %s event: %s", name, exc)


async def _adispatch_provider_event(
    name: str,
    payload: dict[str, Any],
    run_config: Any,
) -> None:
    try:
        await adispatch_custom_event(name, payload, config=run_config)
    except RuntimeError:
        logger.debug("[LLM] No callback manager for %s event", name)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[LLM] Failed to dispatch %s event: %s", name, exc)


def _llm_config_for_fallback(
    llm_config: LLMConfig,
    fallback: Any,
) -> LLMConfig:
    if isinstance(fallback, str):
        model = fallback
        provider = llm_config.provider
        api_key = llm_config.api_key
        base_url = llm_config.base_url
        provider_route = getattr(llm_config, "provider_route", None)
        openai_api_mode = llm_config.openai_api_mode
        context_length = getattr(llm_config, "context_length", None)
        ollama_num_ctx = getattr(llm_config, "ollama_num_ctx", None)
    else:
        provider = getattr(fallback, "provider", None) or llm_config.provider
        model = getattr(fallback, "model", "")
        api_key = getattr(fallback, "api_key", None)
        base_url = getattr(fallback, "base_url", None)
        provider_route = getattr(fallback, "provider_route", None)
        context_length = getattr(fallback, "context_length", None)
        ollama_num_ctx = getattr(fallback, "ollama_num_ctx", None)
        openai_api_mode = (
            getattr(fallback, "openai_api_mode", None)
            or llm_config.openai_api_mode
        )
        if provider == llm_config.provider:
            if api_key is None:
                api_key = llm_config.api_key
            if base_url is None:
                base_url = llm_config.base_url
            if provider_route is None:
                provider_route = getattr(llm_config, "provider_route", None)
            if context_length is None:
                context_length = getattr(llm_config, "context_length", None)
            if ollama_num_ctx is None:
                ollama_num_ctx = getattr(llm_config, "ollama_num_ctx", None)

    return replace(
        llm_config,
        provider=provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        provider_route=provider_route,
        openai_api_mode=openai_api_mode,
        context_length=context_length,
        ollama_num_ctx=ollama_num_ctx,
        fallbacks=[],
        custom_llm=None,
    )


def _llm_candidate(
    primary_llm: BaseChatModel,
    *,
    candidate_index: int,
    llm_config: Optional[LLMConfig],
    tools: Optional[List[BaseTool]],
    cache: dict[int, BaseChatModel],
) -> BaseChatModel:
    cached = cache.get(candidate_index)
    if cached is not None:
        return cached
    if candidate_index == 0 or llm_config is None:
        cache[candidate_index] = primary_llm
        return primary_llm

    fallback = _llm_fallbacks(llm_config)[candidate_index - 1]
    fallback_config = _llm_config_for_fallback(llm_config, fallback)
    llm = create_llm_with_tools(fallback_config, list(tools or []))
    cache[candidate_index] = llm
    return llm


def _invoke_llm_with_retries(
    invoke: Callable[[BaseChatModel], AIMessage],
    llm_config: Optional[LLMConfig],
    primary_llm: BaseChatModel,
    tools: Optional[List[BaseTool]],
    run_config: Any = None,
) -> AIMessage:
    max_retries = _llm_max_retries(llm_config)
    candidate_count = _llm_candidate_count(llm_config)
    candidate_index = _llm_initial_candidate_index(llm_config)
    retry_attempt = 0
    candidate_cache = {0: primary_llm}

    while True:
        try:
            candidate = _llm_candidate(
                primary_llm,
                candidate_index=candidate_index,
                llm_config=llm_config,
                tools=tools,
                cache=candidate_cache,
            )
            return invoke(candidate)
        except Exception as exc:
            if not _is_retryable_llm_error(exc):
                raise

            if retry_attempt < max_retries:
                retry_attempt += 1
                delay = _llm_retry_delay(llm_config, retry_attempt)
                logger.warning(
                    "[LLM RETRY] transient sync call failure on %s; "
                    "retry %d/%d in %.2fs: %s",
                    _llm_candidate_label(llm_config, candidate_index),
                    retry_attempt,
                    max_retries,
                    delay,
                    exc,
                )
                payload = _llm_retry_payload(
                    llm_config,
                    candidate_index,
                    attempt=retry_attempt,
                    max_retries=max_retries,
                    delay=delay,
                    exc=exc,
                )
                _dispatch_provider_event("provider_retry", payload, run_config)
                if delay > 0:
                    time.sleep(delay)
                continue

            if candidate_index + 1 >= candidate_count:
                raise

            next_index = candidate_index + 1
            logger.warning(
                "[LLM FALLBACK] transient sync call failure on %s after retries; "
                "switching to %s: %s",
                _llm_candidate_label(llm_config, candidate_index),
                _llm_candidate_label(llm_config, next_index),
                exc,
            )
            payload = _llm_fallback_payload(
                llm_config,
                candidate_index,
                next_index,
                exc=exc,
            )
            payload = _activate_llm_fallback(llm_config, payload)
            _dispatch_provider_event("provider_fallback", payload, run_config)
            _mark_llm_fallback_active(llm_config, next_index)
            candidate_index = next_index
            retry_attempt = 0

    raise RuntimeError("LLM retry loop exited unexpectedly")


def _truncate_tool_content(content: Any, max_chars: int) -> Any:
    if max_chars <= 0:
        return content

    if isinstance(content, str):
        text = content
    else:
        text = _canonical_json(content)
        if len(text) <= max_chars:
            return content

    if len(text) <= max_chars:
        return content

    head_chars = min(75000, max(1, int(max_chars * 0.75)))
    tail_chars = min(25000, max(1, max_chars - head_chars))
    omitted_chars = max(0, len(text) - head_chars - tail_chars)
    marker = (
        "\n\n[Tool output truncated: "
        f"original {len(text)} chars, omitted {omitted_chars} chars. "
        f"Showing first {head_chars} chars and last {tail_chars} chars.]\n\n"
    )
    return text[:head_chars] + marker + text[-tail_chars:]


def _truncate_tool_message(message: ToolMessage, max_chars: int) -> ToolMessage:
    content = getattr(message, "content", None)
    truncated = _truncate_tool_content(content, max_chars)
    if truncated == content:
        return message

    logger.warning(
        "[TOOLS] Truncated tool output call_id=%s original_chars=%d max_chars=%d",
        getattr(message, "tool_call_id", None),
        len(content) if isinstance(content, str) else len(_canonical_json(content)),
        max_chars,
    )
    return message.model_copy(update={"content": truncated})


def _truncate_tool_messages_in_result(result: Any, max_chars: int) -> Any:
    if max_chars <= 0:
        return result

    if isinstance(result, ToolMessage):
        return _truncate_tool_message(result, max_chars)

    if isinstance(result, list):
        updated = [_truncate_tool_messages_in_result(item, max_chars) for item in result]
        return result if all(a is b for a, b in zip(updated, result)) else updated

    if isinstance(result, dict):
        messages = result.get("messages")
        if isinstance(messages, list):
            updated_messages = [
                _truncate_tool_messages_in_result(item, max_chars)
                for item in messages
            ]
            if any(a is not b for a, b in zip(updated_messages, messages)):
                updated = dict(result)
                updated["messages"] = updated_messages
                return updated
        return result

    if isinstance(result, Command):
        updated = _truncate_tool_messages_in_result(result.update, max_chars)
        if updated is not result.update:
            return replace(result, update=updated)
        return result

    return result


def _tool_call_signature(tool_call: dict) -> tuple[str, str]:
    tool_name = str(tool_call.get("name") or "")
    args_json = _canonical_json(tool_call.get("args", {}))
    return tool_name, f"{tool_name}:{args_json}"


def _tool_result_hash(content: Any) -> str:
    if isinstance(content, str):
        normalized = content.strip()
    else:
        normalized = _canonical_json(content)
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()


def _current_turn_messages(messages: List[BaseMessage]) -> List[BaseMessage]:
    current_turn: List[BaseMessage] = []
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            break
        current_turn.append(msg)
    current_turn.reverse()
    return current_turn


def _count_current_turn_tool_calls(messages: List[BaseMessage]) -> int:
    return sum(
        len(msg.tool_calls)
        for msg in _current_turn_messages(messages)
        if isinstance(msg, AIMessage) and msg.tool_calls
    )


def _completed_tool_exchanges(
    current_turn_messages: List[BaseMessage],
) -> List[_CompletedToolExchange]:
    pending: dict[str, tuple[str, str]] = {}
    exchanges: List[_CompletedToolExchange] = []

    for msg in current_turn_messages:
        if isinstance(msg, AIMessage) and msg.tool_calls:
            for tool_call in msg.tool_calls:
                tool_call_id = tool_call.get("id")
                if not tool_call_id:
                    continue
                tool_name, signature = _tool_call_signature(tool_call)
                pending[str(tool_call_id)] = (signature, tool_name)
        elif isinstance(msg, ToolMessage):
            tool_call_id = getattr(msg, "tool_call_id", None)
            if not tool_call_id or tool_call_id not in pending:
                continue
            signature, tool_name = pending.pop(tool_call_id)
            exchanges.append(
                _CompletedToolExchange(
                    signature=signature,
                    tool_name=tool_name,
                    result_hash=_tool_result_hash(msg.content),
                )
            )

    return exchanges


def analyze_turn_safety(
    messages: List[BaseMessage],
    max_iterations: int,
    repeated_tool_result_limit: int = 5,
) -> TurnSafetyResult:
    """Detect hard iteration caps and exact repeated tool/result loops.

    The repeated-result guard stops before executing another tool call when the
    last N completed tool exchanges used the same tool args and returned the
    same exact normalized result, and the model asks for that same call again.
    """
    tool_call_count = _count_current_turn_tool_calls(messages)
    result = TurnSafetyResult(
        should_stop=False,
        tool_call_count=tool_call_count,
        max_iterations=max_iterations,
    )

    if not messages:
        return result

    last_message = messages[-1]
    if not (isinstance(last_message, AIMessage) and last_message.tool_calls):
        return result

    if max_iterations > 0 and tool_call_count > max_iterations:
        return TurnSafetyResult(
            should_stop=True,
            reason=TURN_SAFETY_REASON_MAX_ITERATIONS,
            tool_call_count=tool_call_count,
            max_iterations=max_iterations,
        )

    if repeated_tool_result_limit <= 0:
        return result

    current_turn = _current_turn_messages(messages)
    prior_messages = current_turn[:-1] if current_turn and current_turn[-1] is last_message else current_turn
    completed_exchanges = _completed_tool_exchanges(prior_messages)
    if not completed_exchanges:
        return result

    for tool_call in last_message.tool_calls:
        tool_name, signature = _tool_call_signature(tool_call)
        repeat_count = 0
        repeated_result_hash: Optional[str] = None

        for exchange in reversed(completed_exchanges):
            if exchange.signature != signature:
                break
            if repeated_result_hash is None:
                repeated_result_hash = exchange.result_hash
            elif exchange.result_hash != repeated_result_hash:
                break
            repeat_count += 1
            if repeat_count >= repeated_tool_result_limit:
                return TurnSafetyResult(
                    should_stop=True,
                    reason=TURN_SAFETY_REASON_REPEATED_TOOL_RESULT,
                    tool_call_count=tool_call_count,
                    max_iterations=max_iterations,
                    repeated_tool_name=tool_name,
                    repeated_count=repeat_count,
                )

    return result


def _uses_cliproxy_anthropic(llm_config: Optional[LLMConfig]) -> bool:
    """Return True for Anthropic requests routed through CLIProxy."""
    if not llm_config or llm_config.provider != "anthropic" or not llm_config.base_url:
        return False
    return looks_like_cliproxy_url(llm_config.base_url)


def _uses_direct_anthropic(llm_config: Optional[LLMConfig]) -> bool:
    """Return True for direct Anthropic API calls (not through CLIProxy)."""
    if not llm_config or llm_config.provider != "anthropic":
        return False
    return not _uses_cliproxy_anthropic(llm_config)


_CACHE_CONTROL_EPHEMERAL = {"type": "ephemeral"}


def _format_system_prompt(
    system_prompt: str,
    llm_config: Optional[LLMConfig],
) -> str | list[dict[str, Any]]:
    """Format system prompt with provider-specific annotations.

    For CLIProxy Anthropic: prepends billing fingerprint block.
    For direct Anthropic: adds cache_control breakpoint on the last block
    so the system prompt is cached across turns.
    """
    is_cliproxy = _uses_cliproxy_anthropic(llm_config)
    is_direct = _uses_direct_anthropic(llm_config)

    if not is_cliproxy and not is_direct:
        return system_prompt

    if isinstance(system_prompt, list):
        blocks = list(system_prompt)
    else:
        blocks = [{"type": "text", "text": system_prompt}]

    if is_cliproxy:
        has_billing_block = any(
            isinstance(block, dict)
            and str(block.get("text", "")).startswith("x-anthropic-billing-header:")
            for block in blocks
        )
        if has_billing_block:
            return blocks
        return [dict(CLIPROXY_BILLING_SYSTEM_BLOCK), *blocks]

    # Direct Anthropic: add cache_control to the last block
    if blocks:
        blocks[-1] = {
            **blocks[-1],
            "cache_control": dict(_CACHE_CONTROL_EPHEMERAL),
        }
    return blocks


def _inject_conversation_cache_breakpoint(
    messages: List[BaseMessage],
) -> List[BaseMessage]:
    """Add cache_control to the second-to-last human message for multi-turn caching.

    Per Anthropic docs, placing a breakpoint on the second-to-last user message
    caches the conversation prefix. Only modifies messages if there are at least
    2 human messages and no existing cache_control annotations.
    """
    human_indices = [
        i for i, m in enumerate(messages) if isinstance(m, HumanMessage)
    ]
    if len(human_indices) < 2:
        return messages

    target_idx = human_indices[-2]
    target = messages[target_idx]

    # Don't add if any message already has cache_control
    for msg in messages:
        if isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, dict) and "cache_control" in block:
                    return messages

    if isinstance(target.content, str):
        new_content = [
            {
                "type": "text",
                "text": target.content,
                "cache_control": dict(_CACHE_CONTROL_EPHEMERAL),
            }
        ]
    elif isinstance(target.content, list):
        new_content = list(target.content)
        # Add cache_control to the last text block
        for i in range(len(new_content) - 1, -1, -1):
            block = new_content[i]
            if isinstance(block, dict) and block.get("type") == "text":
                new_content[i] = {
                    **block,
                    "cache_control": dict(_CACHE_CONTROL_EPHEMERAL),
                }
                break
        else:
            return messages
    else:
        return messages

    messages = list(messages)
    messages[target_idx] = _copy_message_with_content(target, new_content)
    return messages


def _strip_malformed_anthropic_thinking_blocks(content: Any) -> tuple[Any, int]:
    """Remove Anthropic thinking blocks that cannot be replayed."""
    if not isinstance(content, list):
        return content, 0

    cleaned = []
    removed = 0
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "thinking"
            and "thinking" not in block
            and "redacted_thinking" not in block
        ):
            removed += 1
            continue
        cleaned.append(block)

    if not removed:
        return content, 0
    return cleaned, removed


def _copy_message_with_content(message: BaseMessage, content: Any) -> BaseMessage:
    if hasattr(message, "model_copy"):
        return message.model_copy(update={"content": content})
    return message.copy(update={"content": content})


def _message_content_preview(content: Any, max_chars: int = 150) -> str:
    """Build a debug preview without dumping inline base64 image data."""
    if not content:
        return ""

    if isinstance(content, list):
        redacted: list[Any] = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "image_url"
                and isinstance(block.get("image_url"), dict)
            ):
                image_url = dict(block["image_url"])
                if isinstance(image_url.get("url"), str) and image_url["url"].startswith("data:image/"):
                    image_url["url"] = "<inline image data redacted>"
                redacted.append({**block, "image_url": image_url})
            else:
                redacted.append(block)
        content_str = str(redacted)
    else:
        content_str = content if isinstance(content, str) else str(content)
    return content_str[:max_chars].replace("\n", " ")


def _sanitize_messages_for_anthropic(
    messages: List[BaseMessage],
    llm_config: Optional[LLMConfig],
) -> List[BaseMessage]:
    """Drop invalid signature-only thinking blocks before Anthropic replay."""
    if not llm_config or llm_config.provider != "anthropic":
        return messages

    sanitized: List[BaseMessage] = []
    removed_total = 0
    for message in messages:
        if isinstance(message, AIMessage):
            content, removed = _strip_malformed_anthropic_thinking_blocks(message.content)
            if removed:
                message = _copy_message_with_content(message, content)
                removed_total += removed
        sanitized.append(message)

    if removed_total:
        logger.warning(
            "[LLM] Dropped %d malformed Anthropic thinking block(s) before replay",
            removed_total,
        )
    return sanitized


def _hydrate_generated_images_for_llm(
    messages: List[BaseMessage],
    llm_config: Optional[LLMConfig],
) -> List[BaseMessage]:
    from ...core.generated_image_context import hydrate_generated_images_for_llm

    return hydrate_generated_images_for_llm(messages, llm_config)


def create_agent_node(
    llm_with_tools: BaseChatModel,
    system_prompt: str,
    llm_config: Optional[LLMConfig] = None,
    tools: Optional[List[BaseTool]] = None,
) -> Callable[[AgentState], dict]:
    """
    Factory function to create an agent node with custom LLM and prompt.

    Args:
        llm_with_tools: LLM with tools already bound
        system_prompt: System prompt for the agent

    Returns:
        Agent node function compatible with LangGraph
    """
    def _prepare_messages(state: AgentState) -> List[BaseMessage]:
        messages = _hydrate_generated_images_for_llm(state["messages"], llm_config)
        messages = _sanitize_messages_for_anthropic(messages, llm_config)

        # Summary line at INFO (always visible)
        tool_rounds = sum(1 for m in messages if isinstance(m, AIMessage) and m.tool_calls)
        logger.info(f"[LLM] Invoking with {len(messages)} messages ({tool_rounds} tool rounds)")

        # Detailed message dump at DEBUG (visible with llm profile)
        if logger.isEnabledFor(logging.DEBUG):
            for i, msg in enumerate(messages):
                msg_type = type(msg).__name__
                content_preview = ""
                if hasattr(msg, 'content') and msg.content:
                    content_preview = _message_content_preview(msg.content)
                tool_info = ""
                if hasattr(msg, 'tool_calls') and msg.tool_calls:
                    tool_names = [tc.get('name', '?') for tc in msg.tool_calls]
                    tool_info = f" [tools: {', '.join(tool_names)}]"
                logger.debug(f"[LLM]   [{i}] {msg_type}{tool_info}: {content_preview}...")

        # For direct Anthropic: add cache_control breakpoint on the
        # second-to-last human message so the conversation prefix is cached
        # across turns. This mirrors what CLIProxy does automatically.
        if _uses_direct_anthropic(llm_config):
            messages = _inject_conversation_cache_breakpoint(messages)

        # Prepend system prompt (not stored in state)
        return [
            SystemMessage(content=_format_system_prompt(system_prompt, llm_config))
        ] + messages

    def _finish_response(response: AIMessage) -> dict:
        # Sanitize tool call names — some models emit leading/trailing whitespace
        # (e.g. ' CalendarAgent' instead of 'CalendarAgent') which breaks routing.
        if hasattr(response, 'tool_calls') and response.tool_calls:
            for tc in response.tool_calls:
                if 'name' in tc and tc['name'] != tc['name'].strip():
                    logger.warning(f"[LLM] Stripped whitespace from tool call name: {tc['name']!r} -> {tc['name'].strip()!r}")
                    tc['name'] = tc['name'].strip()

        # Log response summary at INFO
        resp_content = response.content if isinstance(response.content, str) else str(response.content)
        has_tools = bool(response.tool_calls) if hasattr(response, 'tool_calls') else False
        tool_names = [tc.get('name', '?') for tc in response.tool_calls] if has_tools else []
        logger.info(
            f"[LLM] Response: {len(resp_content)} chars"
            + (f", tool_calls={tool_names}" if has_tools else ", final answer")
        )

        # Check if response was truncated due to hitting max_tokens
        if hasattr(response, 'response_metadata'):
            finish_reason = response.response_metadata.get('finish_reason')
            if finish_reason == 'length':
                logger.warning(
                    f"[LLM] TRUNCATED — Response hit max_tokens limit "
                    f"(finish_reason='length', content_length={len(resp_content)}). "
                    f"The model's output was cut off mid-generation."
                )

        return {"messages": [response]}

    def agent_node(state: AgentState, config: Any = None) -> dict:
        """
        The 'reasoning' node - asks the LLM what to do next.

        Returns AIMessage that either:
        - Has content (final answer to user)
        - Has tool_calls (instructions to call tools)
        - Has both (explaining what it's about to do)
        """
        messages_with_system = _prepare_messages(state)

        # Sync graph callers use the normal invoke path. User-facing live
        # streaming runs through the async node below.
        response = _invoke_llm_with_retries(
            lambda candidate: candidate.invoke(messages_with_system),
            llm_config,
            llm_with_tools,
            tools,
            config,
        )
        return _finish_response(response)

    async def async_agent_node(state: AgentState, config: Any = None) -> dict:
        """
        Async reasoning node that consumes the LLM stream.

        LangGraph only emits `on_chat_model_stream` events when the model is
        actually consumed through its streaming interface. The sync node above
        calls `invoke()`, which collapses provider deltas into a single final
        model event. Autonomous and API streaming both use `astream_events()`,
        so this async implementation preserves token-level provider chunks and
        then merges them back into the final AIMessage required by the graph.
        """
        messages_with_system = _prepare_messages(state)
        merged_chunk = None
        stream_started_at = time.monotonic()
        first_chunk_ms: Optional[int] = None
        stream_chunks = 0
        text_chunks = 0
        text_chars = 0
        reasoning_chunks = 0
        reasoning_chars = 0
        tool_call_chunk_events = 0

        max_retries = _llm_max_retries(llm_config)
        candidate_count = _llm_candidate_count(llm_config)
        candidate_index = _llm_initial_candidate_index(llm_config)
        retry_attempt = 0
        candidate_cache = {0: llm_with_tools}
        while True:
            chunks_this_attempt = 0
            try:
                candidate = _llm_candidate(
                    llm_with_tools,
                    candidate_index=candidate_index,
                    llm_config=llm_config,
                    tools=tools,
                    cache=candidate_cache,
                )
                async for chunk in candidate.astream(messages_with_system):
                    chunks_this_attempt += 1
                    stream_chunks += 1
                    if first_chunk_ms is None:
                        first_chunk_ms = int((time.monotonic() - stream_started_at) * 1000)

                    content = getattr(chunk, "content", None)
                    if isinstance(content, str):
                        if content:
                            text_chunks += 1
                            text_chars += len(content)
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, str):
                                if block:
                                    text_chunks += 1
                                    text_chars += len(block)
                                continue
                            if not isinstance(block, dict):
                                continue
                            block_type = block.get("type")
                            if block_type in ("text", "output_text"):
                                text = block.get("text", "")
                                if text:
                                    text_chunks += 1
                                    text_chars += len(text)
                            elif block_type in ("thinking", "reasoning"):
                                # Count typed reasoning blocks without logging content.
                                reasoning_chunks += 1

                    extras = getattr(chunk, "additional_kwargs", None) or {}
                    reasoning = extras.get("reasoning_content")
                    if isinstance(reasoning, str) and reasoning:
                        reasoning_chunks += 1
                        reasoning_chars += len(reasoning)

                    if getattr(chunk, "tool_call_chunks", None):
                        tool_call_chunk_events += 1

                    if merged_chunk is None:
                        merged_chunk = chunk
                    else:
                        merged_chunk = merged_chunk + chunk

                if merged_chunk is None:
                    # Defensive fallback for custom models that implement astream() but
                    # produce no chunks.
                    logger.warning("[LLM STREAM] async astream yielded zero chunks; falling back to ainvoke()")
                    response = await candidate.ainvoke(messages_with_system)
                else:
                    response = message_chunk_to_message(merged_chunk)
                break
            except Exception as exc:
                if chunks_this_attempt > 0:
                    try:
                        setattr(exc, "nymeria_stream_chunks_before_error", chunks_this_attempt)
                        setattr(exc, "nymeria_stream_candidate_index", candidate_index)
                        setattr(exc, "nymeria_stream_candidate_label", _llm_candidate_label(llm_config, candidate_index))
                    except Exception:
                        pass
                    logger.warning(
                        "[LLM RETRY] stream failed after %d chunk(s) on %s; "
                        "not retrying to avoid duplicated output: %s",
                        chunks_this_attempt,
                        _llm_candidate_label(llm_config, candidate_index),
                        exc,
                    )
                    raise
                if not _is_retryable_llm_error(exc):
                    raise

                if retry_attempt < max_retries:
                    retry_attempt += 1
                    delay = _llm_retry_delay(llm_config, retry_attempt)
                    logger.warning(
                        "[LLM RETRY] transient stream failure before chunks on %s; "
                        "retry %d/%d in %.2fs: %s",
                        _llm_candidate_label(llm_config, candidate_index),
                        retry_attempt,
                        max_retries,
                        delay,
                        exc,
                    )
                    payload = _llm_retry_payload(
                        llm_config,
                        candidate_index,
                        attempt=retry_attempt,
                        max_retries=max_retries,
                        delay=delay,
                        exc=exc,
                    )
                    await _adispatch_provider_event(
                        "provider_retry",
                        payload,
                        config,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                    continue

                if candidate_index + 1 >= candidate_count:
                    raise

                next_index = candidate_index + 1
                logger.warning(
                    "[LLM FALLBACK] transient stream failure before chunks on %s "
                    "after retries; switching to %s: %s",
                    _llm_candidate_label(llm_config, candidate_index),
                    _llm_candidate_label(llm_config, next_index),
                    exc,
                )
                payload = _llm_fallback_payload(
                    llm_config,
                    candidate_index,
                    next_index,
                    exc=exc,
                )
                payload = _activate_llm_fallback(llm_config, payload)
                await _adispatch_provider_event("provider_fallback", payload, config)
                _mark_llm_fallback_active(llm_config, next_index)
                candidate_index = next_index
                retry_attempt = 0

        logger.info(
            "[LLM STREAM] async_complete chunks=%d text_chunks=%d text_chars=%d "
            "reasoning_chunks=%d reasoning_chars=%d tool_call_chunk_events=%d "
            "first_chunk_ms=%s elapsed_ms=%d",
            stream_chunks,
            text_chunks,
            text_chars,
            reasoning_chunks,
            reasoning_chars,
            tool_call_chunk_events,
            first_chunk_ms if first_chunk_ms is not None else "none",
            int((time.monotonic() - stream_started_at) * 1000),
        )
        return _finish_response(response)

    return RunnableLambda(agent_node, afunc=async_agent_node, name="agent")


def create_dynamic_agent_node(
    system_prompt: str,
    llm_config: LLMConfig,
    tool_resolver: Callable[[], tuple],
) -> Callable[[AgentState], dict]:
    """Build an agent node whose bound tools are recomputed per-step.

    The resolver is called on every invocation and returns
    ``(tools_list, cache_key_hash)``. When ``cache_key_hash`` matches the
    previous step's, the previously bound LLM (and its inner RunnableLambda)
    are reused verbatim — this keeps the Anthropic prompt cache prefix
    stable across consecutive steps with no tool-config mutation. When the
    hash changes, the LLM is rebound via ``create_llm_with_tools`` and a
    fresh inner agent node is built around it.

    All retry/streaming/fallback machinery is delegated to ``create_agent_node``;
    this function only adds the resolve→cache→rebind→delegate envelope.

    Args:
        system_prompt: System prompt for the agent (unchanged across steps).
        llm_config: LLMConfig used to rebuild the LLM when tools change.
        tool_resolver: ``() -> (List[BaseTool], str)`` callable. Typically
            produced by ``NymeriaAgent._make_dynamic_tool_resolver``.

    Returns:
        A RunnableLambda compatible with LangGraph that dispatches to a
        per-step rebound inner agent node.
    """
    # Mutable cache shared by sync + async closures. None on first call.
    cache: dict = {"hash": None, "tools": None, "node": None}

    def _ensure_node():
        tools, cache_key = tool_resolver()
        if cache["hash"] != cache_key or cache["node"] is None:
            logger.info(
                "[DynamicAgent] Rebinding tools (hash %s -> %s, %d tools)",
                cache["hash"] or "init",
                cache_key,
                len(tools),
            )
            llm_with_tools = create_llm_with_tools(llm_config, tools)
            cache["hash"] = cache_key
            cache["tools"] = tools
            cache["node"] = create_agent_node(
                llm_with_tools,
                system_prompt,
                llm_config,
                tools,
            )
        return cache["node"]

    def agent_node(state: AgentState) -> dict:
        return _ensure_node().invoke(state)

    async def async_agent_node(state: AgentState) -> dict:
        return await _ensure_node().ainvoke(state)

    return RunnableLambda(agent_node, afunc=async_agent_node, name="agent")


def create_tools_node(
    tools: List[BaseTool],
    handle_errors: bool = True,
    tool_timeout: Optional[int] = None,
    on_timeout: Optional[Callable] = None,
    tool_output_max_chars: int = 100000,
    dynamic_tool_resolver: Optional[Callable[[], tuple]] = None,
) -> "SafeToolNode":
    """
    Create a tools node that executes tool calls.

    Args:
        tools: List of tools the node can execute
        handle_errors: If True, catch tool exceptions and return error messages
                      instead of letting them bubble up
        tool_timeout: Seconds before a tool invocation is terminated (default 300)
        on_timeout: Optional callback invoked with the input dict and runnable
            config when a timeout occurs
        tool_output_max_chars: Maximum stored characters per ToolMessage result
        dynamic_tool_resolver: Optional resolver used by dynamic-binding mode.
            If the model calls a tool that was enabled after graph construction,
            SafeToolNode refreshes from this resolver before rejecting it.

    Returns:
        SafeToolNode instance that handles errors and timeouts gracefully
    """
    return SafeToolNode(
        tools,
        handle_tool_errors=handle_errors,
        tool_timeout=tool_timeout,
        on_timeout=on_timeout,
        tool_output_max_chars=tool_output_max_chars,
        dynamic_tool_resolver=dynamic_tool_resolver,
    )


class SafeToolNode(ToolNode):
    """
    A ToolNode wrapper that catches exceptions and returns them as tool results,
    and enforces a per-invocation timeout to prevent hanging tools from blocking
    the agent indefinitely.

    This prevents tool failures from crashing the entire agent and allows
    the LLM to see the error and potentially retry or handle it.
    """

    DEFAULT_TOOL_TIMEOUT = 300  # 5 minutes

    def __init__(
        self,
        tools: List[BaseTool],
        handle_tool_errors: bool = True,
        tool_timeout: Optional[int] = None,
        on_timeout: Optional[Callable] = None,
        tool_output_max_chars: int = 100000,
        dynamic_tool_resolver: Optional[Callable[[], tuple]] = None,
    ):
        super().__init__(tools, handle_tool_errors=handle_tool_errors)
        self._handle_errors = handle_tool_errors
        self._tool_timeout = tool_timeout if tool_timeout is not None else self.DEFAULT_TOOL_TIMEOUT
        self._on_timeout = on_timeout  # Optional callback: fn(input_dict, config=None) -> None
        self._tool_output_max_chars = tool_output_max_chars
        self._json_arg_expectations: dict[str, dict[str, set[str]]] = {}
        self._dynamic_tool_resolver = dynamic_tool_resolver
        self._dynamic_tool_lock = threading.RLock()

    def _register_dynamic_tool(self, tool: BaseTool) -> None:
        """Add a late-bound tool to this ToolNode's dispatch table."""
        if not getattr(tool, "name", None):
            return
        with self._dynamic_tool_lock:
            if tool.name in self.tools_by_name:
                return
            self._tools_by_name[tool.name] = tool
            try:
                from langgraph.prebuilt.tool_node import _get_all_injected_args

                self._injected_args[tool.name] = _get_all_injected_args(tool)
            except Exception:  # noqa: BLE001 - injection cache is best-effort.
                logger.debug(
                    "Failed to cache injected args for dynamically bound tool %s",
                    tool.name,
                    exc_info=True,
                )
            self._json_arg_expectations.pop(tool.name, None)
            logger.info("Dynamically registered tool for dispatch: %s", tool.name)

    def _ensure_dynamic_tools_for_calls(self, tool_calls: list[ToolCall]) -> None:
        """Refresh dynamic tools when a call targets a post-build tool."""
        if self._dynamic_tool_resolver is None:
            return
        missing = {
            str(call.get("name") or "")
            for call in tool_calls
            if isinstance(call.get("name"), str)
            and call.get("name") not in self.tools_by_name
        }
        missing.discard("")
        if not missing:
            return
        try:
            resolved_tools, _cache_key = self._dynamic_tool_resolver()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Dynamic tool resolver failed while refreshing missing tools %s",
                sorted(missing),
                exc_info=True,
            )
            return
        for tool in resolved_tools or []:
            if getattr(tool, "name", None) in missing:
                self._register_dynamic_tool(tool)

    @staticmethod
    def _resolve_json_schema_ref(ref: str, root_schema: dict[str, Any]) -> dict[str, Any] | None:
        if not ref.startswith("#/"):
            return None
        target: Any = root_schema
        for raw_part in ref[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                return None
            target = target[part]
        return target if isinstance(target, dict) else None

    @classmethod
    def _schema_allows_json_type(
        cls,
        field_schema: dict[str, Any],
        json_type: str,
        root_schema: dict[str, Any],
        seen_refs: set[str] | None = None,
    ) -> bool:
        schema_type = field_schema.get("type")
        if schema_type == json_type:
            return True
        if isinstance(schema_type, list) and json_type in schema_type:
            return True

        ref = field_schema.get("$ref")
        if isinstance(ref, str):
            seen_refs = seen_refs or set()
            if ref in seen_refs:
                return False
            resolved = cls._resolve_json_schema_ref(ref, root_schema)
            if resolved is not None:
                return cls._schema_allows_json_type(
                    resolved,
                    json_type,
                    root_schema,
                    seen_refs | {ref},
                )

        for union_key in ("anyOf", "oneOf", "allOf"):
            variants = field_schema.get(union_key)
            if isinstance(variants, list) and any(
                isinstance(variant, dict)
                and cls._schema_allows_json_type(
                    variant,
                    json_type,
                    root_schema,
                    seen_refs,
                )
                for variant in variants
            ):
                return True
        return False

    @staticmethod
    def _tool_schema(tool: BaseTool) -> dict[str, Any]:
        for attr in ("tool_call_schema", "args_schema"):
            schema_obj = getattr(tool, attr, None)
            if schema_obj is None:
                continue
            if isinstance(schema_obj, dict):
                return schema_obj
            if hasattr(schema_obj, "model_json_schema"):
                try:
                    return schema_obj.model_json_schema()
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "Failed to inspect tool schema for %s via %s",
                        getattr(tool, "name", "<unknown>"),
                        attr,
                        exc_info=True,
                    )
        return {}

    def _json_arg_types_for_tool(self, tool: BaseTool) -> dict[str, set[str]]:
        cached = self._json_arg_expectations.get(tool.name)
        if cached is not None:
            return cached

        schema = self._tool_schema(tool)
        properties = schema.get("properties") if isinstance(schema, dict) else None
        expectations: dict[str, set[str]] = {}
        if isinstance(properties, dict):
            for arg_name, field_schema in properties.items():
                if not isinstance(field_schema, dict):
                    continue
                expected: set[str] = set()
                if self._schema_allows_json_type(field_schema, "array", schema):
                    expected.add("array")
                if self._schema_allows_json_type(field_schema, "object", schema):
                    expected.add("object")
                if expected:
                    expectations[arg_name] = expected

        self._json_arg_expectations[tool.name] = expectations
        return expectations

    def _normalize_tool_call_args(self, call: ToolCall) -> ToolCall:
        tool_name = call.get("name")
        tool = self.tools_by_name.get(tool_name) if isinstance(tool_name, str) else None
        args = call.get("args")
        if tool is None or not isinstance(args, dict):
            return call

        expectations = self._json_arg_types_for_tool(tool)
        if not expectations:
            return call

        normalized_args = args
        for arg_name, value in args.items():
            expected_types = expectations.get(arg_name)
            if not expected_types or not isinstance(value, str):
                continue
            raw = value.strip()
            if not raw or raw[0] not in "[{":
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue

            parsed_type = None
            if isinstance(parsed, list):
                parsed_type = "array"
            elif isinstance(parsed, dict):
                parsed_type = "object"
            if parsed_type is None or parsed_type not in expected_types:
                continue

            if normalized_args is args:
                normalized_args = dict(args)
            normalized_args[arg_name] = parsed
            logger.debug(
                "Decoded JSON-encoded tool argument: tool=%s arg=%s type=%s",
                tool_name,
                arg_name,
                parsed_type,
            )

        if normalized_args is args:
            return call
        normalized_call = cast(ToolCall, dict(call))
        normalized_call["args"] = normalized_args
        return normalized_call

    def _parse_input(
        self,
        input: list[AnyMessage] | dict[str, Any] | BaseModel,
    ) -> tuple[list[ToolCall], Literal["list", "dict", "tool_calls"]]:
        tool_calls, input_type = super()._parse_input(input)
        self._ensure_dynamic_tools_for_calls(tool_calls)
        return [self._normalize_tool_call_args(call) for call in tool_calls], input_type

    def _notify_timeout(self, input, config=None) -> None:
        """Invoke the timeout hook while preserving legacy one-argument hooks."""
        if not self._on_timeout:
            return

        try:
            signature = inspect.signature(self._on_timeout)
        except (TypeError, ValueError):
            self._on_timeout(input, config)
            return

        params = signature.parameters
        values = list(params.values())
        if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in values):
            self._on_timeout(input, config)
            return
        if "config" in params:
            self._on_timeout(input, config=config)
            return
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in values):
            self._on_timeout(input, config=config)
            return

        positional = [
            p
            for p in values
            if p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        if len(positional) >= 2:
            self._on_timeout(input, config)
        else:
            self._on_timeout(input)

    def _record_capability_usage(self, input, config=None) -> None:
        """Best-effort record of tool/skill use for cleanup heuristics."""
        try:
            configurable = {}
            if isinstance(config, dict):
                configurable = config.get("configurable") or {}
            else:
                configurable = getattr(config, "configurable", {}) or {}
            thread_id = str(configurable.get("thread_id") or "")
            user_id = str(configurable.get("user_id") or "default")
            if not thread_id:
                return

            messages = input.get("messages", []) if isinstance(input, dict) else []
            last_message = messages[-1] if messages else None
            if not isinstance(last_message, AIMessage) or not last_message.tool_calls:
                return

            tools: list[str] = []
            skills: list[str] = []
            for tc in last_message.tool_calls:
                tool_name = str(tc.get("name") or "").strip()
                if not tool_name:
                    continue
                if tool_name == "Skill":
                    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                    skill_name = str(args.get("name") or "").strip()
                    if skill_name:
                        skills.append(skill_name)
                else:
                    tools.append(tool_name)

            if tools or skills:
                from nymeria.core.capability_usage import record_capability_usage

                record_capability_usage(
                    user_id=user_id,
                    thread_id=thread_id,
                    tools=tools,
                    skills=skills,
                )
        except Exception:
            logger.debug("Failed to record capability usage", exc_info=True)

    def invoke(self, input, config=None, **kwargs):
        """Execute tools with a timeout to prevent indefinite hangs.

        Wraps the parent ToolNode.invoke() in a thread with a timeout.
        If the timeout fires, returns error ToolMessages for all pending
        tool calls so the agent can recover gracefully.

        Note: on timeout, the underlying thread may continue running in the
        background (Python cannot forcibly kill threads), but the agent is
        unblocked and can proceed. The executor is shut down with wait=False
        so the caller is not blocked by ThreadPoolExecutor cleanup.
        """
        self._record_capability_usage(input, config)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(super().invoke, input, config, **kwargs)
        try:
            result = future.result(timeout=self._tool_timeout)
            executor.shutdown(wait=False)
            return _truncate_tool_messages_in_result(result, self._tool_output_max_chars)
        except concurrent.futures.TimeoutError:
            # shutdown(wait=False) returns immediately — the daemon worker
            # thread will finish on its own (or when the process exits).
            executor.shutdown(wait=False)
            try:
                self._notify_timeout(input, config)
            except Exception as e:
                logger.warning(f"on_timeout callback failed: {e}")
            return _truncate_tool_messages_in_result(
                self._build_timeout_response(input),
                self._tool_output_max_chars,
            )

    async def ainvoke(self, input, config=None, **kwargs):
        """Async tool execution with timeout."""
        self._record_capability_usage(input, config)
        try:
            result = await asyncio.wait_for(
                super().ainvoke(input, config, **kwargs),
                timeout=self._tool_timeout,
            )
            return _truncate_tool_messages_in_result(result, self._tool_output_max_chars)
        except asyncio.TimeoutError:
            try:
                self._notify_timeout(input, config)
            except Exception as e:
                logger.warning(f"on_timeout callback failed: {e}")
            return _truncate_tool_messages_in_result(
                self._build_timeout_response(input),
                self._tool_output_max_chars,
            )

    def _build_timeout_response(self, input) -> dict:
        """Build error ToolMessages for timed-out tool calls.

        LangGraph requires a matching ToolMessage for every tool_call in the
        AIMessage, so we produce one error message per pending call.
        """
        messages = input.get("messages", []) if isinstance(input, dict) else []
        last_message = messages[-1] if messages else None

        error_messages = []
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            tool_names = [tc.get("name", "unknown") for tc in last_message.tool_calls]
            logger.error(
                f"Tool execution timed out after {self._tool_timeout}s. "
                f"Tools: {tool_names}"
            )
            for tc in last_message.tool_calls:
                tool_name = tc.get("name", "unknown")
                tool_call_id = tc.get("id", "unknown")
                error_messages.append(ToolMessage(
                    content=(
                        f"[Error]: Tool '{tool_name}' timed out after {self._tool_timeout} seconds. "
                        f"The operation took too long and was stopped to prevent the agent from hanging. "
                        f"Do NOT retry this tool — report the timeout to the user."
                    ),
                    tool_call_id=tool_call_id,
                ))
        else:
            logger.error(
                f"Tool execution timed out after {self._tool_timeout}s "
                f"but could not extract tool calls from input to build error response."
            )

        return {"messages": error_messages}


def create_should_continue(
    max_iterations: int = 10,
    repeated_tool_result_limit: int = 5,
) -> Callable[[AgentState], str]:
    """
    Factory for the routing function with iteration limit.

    The iteration count is derived from the message history (counting tool calls
    since the last HumanMessage) rather than using a closure, making it
    thread-safe and stateless. Only the current turn's tool calls count toward
    the limit, so previous turns don't block future ones.

    Args:
        max_iterations: Maximum ReAct loops per turn before forcing end

    Returns:
        Routing function for conditional edges
    """
    def should_continue(state: AgentState) -> str:
        """
        Conditional edge: decides what node to go to next.

        Returns:
            'tools' if agent wants to use tools
            'end' if agent is done or iteration limit reached
        """
        messages = state["messages"]
        last_message = messages[-1]

        # Check if LLM wants to call tools
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            safety = analyze_turn_safety(
                messages,
                max_iterations=max_iterations,
                repeated_tool_result_limit=repeated_tool_result_limit,
            )

            if safety.should_stop:
                # Force stop to prevent runaway loops.
                if safety.reason == TURN_SAFETY_REASON_REPEATED_TOOL_RESULT:
                    logger.warning(
                        "Repeated tool/result loop detected: tool=%s repeat_count=%s. "
                        "Forcing agent to stop before another identical tool call.",
                        safety.repeated_tool_name,
                        safety.repeated_count,
                    )
                else:
                    logger.warning(
                        f"Iteration limit reached ({safety.tool_call_count}/{max_iterations}). "
                        f"Forcing agent to stop. The agent wanted to call more tools but was cut off."
                    )
                return "end"

            return "tools"

        return "end"

    return should_continue


def simple_should_continue(state: AgentState) -> str:
    """
    Simple routing function without iteration tracking.

    For use when you want basic routing without limits,
    or when you're managing iteration limits externally.
    """
    messages = state["messages"]
    last_message = messages[-1]

    if isinstance(last_message, AIMessage) and last_message.tool_calls:
        return "tools"

    return "end"


def _latest_tool_batch_queued_reload(messages: List[BaseMessage]) -> bool:
    from ...core.tool_reload import latest_tool_batch_queued_reload

    return latest_tool_batch_queued_reload(messages)


def route_after_tools(state: AgentState, config=None) -> str:
    """Route after tool execution.

    Stops the graph immediately if either:

    1. The just-finished tool batch flagged a queued tool-reload
       (in-turn rebind path).
    2. The pending-prompt queue for this thread is non-empty -- a new
       prompt arrived mid-turn and the agent should halt at this
       sub-turn boundary so ``astream()`` can drain it, inject one
       ``HumanMessage`` per prompt, and re-drive the graph.

    LangGraph's ``RunnableCallable`` auto-injects ``config`` when the
    routing function's signature accepts it. The ``config`` parameter
    is intentionally untyped here -- ``RunnableCallable`` only injects
    when the annotation is ``RunnableConfig``, ``Optional[RunnableConfig]``,
    or absent (see ``langgraph/_internal/_runnable.py``
    ``KWARGS_CONFIG_KEYS``). Leaving it bare keeps the injection
    working without dragging a ``RunnableConfig`` import into this
    module.
    """
    if _latest_tool_batch_queued_reload(state["messages"]):
        return "end"

    if config:
        configurable = config.get("configurable") or {}
        thread_id = configurable.get("thread_id")
        if thread_id:
            # Function-local import keeps the vendored fork independent
            # of ``nymeria.core.*`` at module load time -- see
            # [[feedback_extraction_circular_imports]].
            from ...core.pending_prompt_queue import get_pending_queue
            backend = get_pending_queue()
            pending = backend.size(thread_id)
            if pending > 0:
                backend.mark_halt_observed(thread_id, pending)
                return "end"

    return "agent"


class NodeFactory:
    """
    Factory class for creating all nodes from a single configuration.

    Usage (static-binding mode):
        factory = NodeFactory(config, tools)
        agent = factory.create_agent_node()
        tools_node = factory.create_tools_node()
        router = factory.create_router()

    Usage (dynamic-binding mode):
        factory = NodeFactory(
            config, tools=superset,
            dynamic_tool_resolver=resolver,
            superset_tools=superset,
        )
        # agent node rebinds tools per-step via resolver;
        # tools_node dispatches against superset_tools.
    """

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        tools: Optional[List[BaseTool]] = None,
        *,
        dynamic_tool_resolver: Optional[Callable[[], tuple]] = None,
        superset_tools: Optional[List[BaseTool]] = None,
    ):
        self.config = config or default_config
        self.tools = tools or []
        self._dynamic_tool_resolver = dynamic_tool_resolver
        self._superset_tools = superset_tools
        self._llm_with_tools = None

    @property
    def llm_with_tools(self) -> BaseChatModel:
        """Lazily create and cache the LLM with tools."""
        if self._llm_with_tools is None:
            self._llm_with_tools = create_llm_with_tools(
                self.config.llm,
                self.tools
            )
        return self._llm_with_tools

    def create_agent_node(self) -> Callable[[AgentState], dict]:
        """Create the agent reasoning node.

        Dispatches to ``create_dynamic_agent_node`` when a resolver is set
        (dynamic-binding mode), otherwise to the static ``create_agent_node``.
        """
        if self._dynamic_tool_resolver is not None:
            return create_dynamic_agent_node(
                self.config.system_prompt,
                self.config.llm,
                self._dynamic_tool_resolver,
            )
        return create_agent_node(
            self.llm_with_tools,
            self.config.system_prompt,
            self.config.llm,
            self.tools,
        )

    def create_tools_node(self) -> ToolNode:
        """Create the tool execution node.

        In dynamic mode the superset is used so any tool the model might
        bind on any step can be dispatched. The model's actual bound list
        (a subset) is controlled by the dynamic agent node.
        """
        tools_for_node = (
            self._superset_tools
            if self._superset_tools is not None
            else self.tools
        )
        return create_tools_node(
            tools_for_node,
            tool_timeout=self.config.tool_timeout,
            on_timeout=self.config.on_timeout,
            tool_output_max_chars=self.config.tool_output_max_chars,
            dynamic_tool_resolver=self._dynamic_tool_resolver,
        )

    def create_router(self) -> Callable[[AgentState], str]:
        """Create the routing function."""
        return create_should_continue(
            self.config.max_iterations,
            self.config.repeated_tool_result_limit,
        )

    def create_tools_router(self) -> Callable[[AgentState], str]:
        """Create the post-tools routing function."""
        return route_after_tools
