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
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
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
from langgraph.prebuilt.tool_node import ToolRuntime
from langgraph.types import Command
from langgraph.errors import GraphBubbleUp
from langchain_core.runnables.config import get_config_list
from pydantic import BaseModel

from .cliproxy import (
    CACHE_CONTROL_EPHEMERAL as _CACHE_CONTROL_EPHEMERAL,
    CLIPROXY_BILLING_SYSTEM_BLOCK,
    looks_like_cliproxy_url,
)
from .state import AgentState
from .config import AgentConfig, LLMConfig, default_config
from .providers import (
    create_llm_with_tools,
    resolve_max_output_tokens,
    streaming_call_kwargs,
)
from .reasoning_passback import record_passback_observation

logger = logging.getLogger(__name__)

# Name of the inert ordered-execution marker tool (``nymeria/tools/tool_order.py``).
# Its presence in a same-turn tool-call batch flips SafeToolNode from concurrent
# dispatch to the ordered loop. Defined here (not imported from nymeria.tools) so
# the vendored fork stays independent of the tool package; a test asserts the two
# agree.
SEQUENTIAL_ORDER_TOOL_NAME = "run_tools_in_order"

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

# ---------------------------------------------------------------------------
# Image/document input-unsupported detection (strip-and-retry, Phase 2).
#
# When a provider rejects a turn because the model cannot accept image (or PDF)
# input, the agent nodes strip the attachment and retry once (see
# is_image_unsupported_error + the agent-node strip-retry). The dominant risk is
# FALSE POSITIVES: most 400s that mention "image" are fixable (bad media type,
# size/count, fetch, decode, empty block), and stripping those is wrong. So the
# classifier requires a positive capability signature AND status 400/422 AND the
# absence of any exclusion marker (exclusions win). Signatures are drawn from a
# cross-provider research brief (OpenAI/Anthropic/OpenRouter/Gemini/LiteLLM/vLLM).
# Match against the flattened, lowercased error text (`_llm_exception_text`),
# which already folds in nested/gateway messages (OpenRouter metadata.raw,
# "OpenAIException - ..."), so no envelope-specific parsing is needed.
_IMAGE_UNSUPPORTED_MATCH_MARKERS = (
    "image_url is only supported by certain models",
    "image urls are only supported by certain models",
    "only supported by certain models",
    "does not support image message content types",
    "does not support image input",
    "does not support image",
    "does not support vision",
    "vision is not supported",
    "does not support multimodal",
    "multimodal input is not supported",
    "does not support the requested modalit",
    "unsupported modalit",
    "no endpoints found that support image",
    # File / document axis.
    "does not support document",
    "does not support pdf",
    "does not support file input",
    "document input is not supported",
)
# Anthropic's generic capability template ("... is not supported for this
# model.") over-matches on its own, so it only counts as an image/file rejection
# when it co-occurs with a modality WORD. Matched with word boundaries, not bare
# substrings: an unrelated capability 400 whose text happens to contain a bound
# tool name like "file_read" (or "profile"/"documentation"/"provision") must NOT
# be read as an image rejection and strip a valid attachment.
_IMAGE_UNSUPPORTED_TEMPLATE_MARKER = "not supported for this model"
_IMAGE_UNSUPPORTED_TEMPLATE_MODALITY_RE = re.compile(
    r"\b(?:image|images|vision|document|documents|pdf|pdfs|file|files"
    r"|modality|modalities|multimodal)\b"
)
_IMAGE_UNSUPPORTED_EXCLUDE_MARKERS = (
    # Decode / format / media-type: fixable by re-encode/convert, not stripping.
    "could not process image",
    "could not process pdf",
    "image does not match the provided media type",
    "input should be 'image/jpeg'",
    "acceptable media types are",
    "unsupported media type",
    "unsupported image",
    "invalid image",
    "invalid image data",
    "provided image is not valid",
    "image url not in expected format",
    "expected an image url, but got an object",
    "required oneof field 'data'",
    # Fetch / URL.
    "failed to download",
    "cannot fetch content from the provided url",
    "invalid or unsupported file uri",
    "invalid_image_url",
    # Size / count / page limits.
    "image too large",
    "pdf pages may be provided",
    "image(s) is allowed per prompt",
    "too many images",
    "maximum number of images",
    # Structure / wrong-key / wrong-endpoint (not a modality rejection).
    "text content blocks must be non-empty",
    "cache_control cannot be set for empty text blocks",
    "invalid value: 'image'. supported values are",
    "does not support chat completions api",
)
# Statuses that are never a capability rejection (auth/credits/moderation/
# rate-limit/server), so a stray phrase match must not trigger a strip-retry.
_IMAGE_UNSUPPORTED_HARD_EXCLUDE_STATUSES = {401, 402, 403, 408, 429}


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


def is_image_unsupported_error(exc: BaseException) -> bool:
    """Return True when a provider rejected the request because the model cannot
    accept image (or PDF/file) input, so the attachment can be stripped and the
    turn retried once.

    Conservative and false-positive-averse (see the marker tables above): a
    fixable 400 that merely mentions "image" (bad media type, size/count, fetch,
    decode, empty block, wrong content-type key) is NOT a capability rejection
    and must not trigger a strip-retry. Requires status 400/422 (or unknown),
    a positive capability signature, and no exclusion marker.
    """
    status = _extract_status_code(exc)
    if status is not None and (
        status in _IMAGE_UNSUPPORTED_HARD_EXCLUDE_STATUSES
        or status >= 500
        or status not in (400, 422)
    ):
        return False

    text = _llm_exception_text(exc)
    if not text:
        return False
    if any(marker in text for marker in _IMAGE_UNSUPPORTED_EXCLUDE_MARKERS):
        return False
    if any(marker in text for marker in _IMAGE_UNSUPPORTED_MATCH_MARKERS):
        return True
    if (
        _IMAGE_UNSUPPORTED_TEMPLATE_MARKER in text
        and _IMAGE_UNSUPPORTED_TEMPLATE_MODALITY_RE.search(text)
    ):
        return True
    return False


# Only ``image_url`` blocks are what the outbound image window actually strips
# (`generated_image_context._is_image_url_block`), so the strip-retry gate is
# scoped to that exact type: gating on a block the window cannot remove would
# rebuild an identical request and burn a wasted LLM round-trip before raising.
_ATTACHMENT_BLOCK_TYPES = frozenset({"image_url"})


def _messages_have_attachment_blocks(messages: List[BaseMessage]) -> bool:
    """True if any message carries an inline image content block the window can strip.

    Gates the image strip-and-retry: with no strippable attachment present,
    stripping cannot change the request, so a capability-shaped error is left to
    the normal raise path instead of a pointless retry.
    """
    for message in messages:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") in _ATTACHMENT_BLOCK_TYPES:
                return True
    return False


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
    hold_overrides: Optional[dict[str, Any]] = None,
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
    if hold_overrides:
        payload.update(hold_overrides)
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


# --------------------------------------------------------------------------- #
# Consent gate (user-consented fallback switching + the refusal swap).
# The host wires an ASYNC ``fallback_decision_callback`` onto LLMConfig; the
# helpers below consult it before a model switch is applied. A missing or
# faulting callback always resolves to {"action": "auto"} (legacy silent
# behavior), so bare vendored users and gate faults never change semantics.
# --------------------------------------------------------------------------- #

_FALLBACK_DECISION_ACTIONS = ("swap", "fail", "auto")


def _turn_is_autonomous(run_config: Any) -> Optional[bool]:
    """The turn-source stamp, or None when this run carries no source.

    ``graph_run_config`` stamps ``hook_is_autonomous`` (which already folds in
    self-invoke) only when the caller knows the source; a missing stamp means
    the consent gate must treat the turn as NOT consent-capable.
    """
    try:
        configurable = (run_config or {}).get("configurable") or {}
    except AttributeError:
        return None
    value = configurable.get("hook_is_autonomous")
    return None if value is None else bool(value)


def _turn_holder_kind(run_config: Any) -> Optional[str]:
    """The ``hook_holder_kind`` stamp (the turn's source label), or None.

    Interactive turns stamp "user"; callable/handoff child turns stamp their
    source, so the consent gate can refuse to park a turn whose "user" is
    actually a waiting parent tool call.
    """
    try:
        configurable = (run_config or {}).get("configurable") or {}
    except AttributeError:
        return None
    value = configurable.get("hook_holder_kind")
    return None if value is None else str(value)


async def _aconsult_fallback_decision(
    llm_config: Optional[LLMConfig],
    payload: dict[str, Any],
    *,
    kind: str,
    is_autonomous: Optional[bool],
    holder_kind: Optional[str] = None,
    sync_surface: bool = False,
) -> dict[str, Any]:
    """Ask the host's decision callback about a pending model switch.

    Returns a dict with ``action`` in ("swap", "fail", "auto") plus optional
    ``hold_seconds``/``hold_permanent`` on a swap. "auto" means "behave as if
    consent were unwired" (transport: silent swap; refusal: no swap).
    """
    callback = getattr(llm_config, "fallback_decision_callback", None)
    if callback is None:
        return {"action": "auto"}
    context = {
        **payload,
        "kind": kind,
        "is_autonomous": is_autonomous,
        "holder_kind": holder_kind,
        "sync_surface": bool(sync_surface),
    }
    try:
        decision = await callback(context)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[LLM FALLBACK] decision callback failed; proceeding as auto",
            exc_info=True,
        )
        return {"action": "auto"}
    if (
        not isinstance(decision, dict)
        or decision.get("action") not in _FALLBACK_DECISION_ACTIONS
    ):
        return {"action": "auto"}
    return decision


def _consult_fallback_decision_sync(
    llm_config: Optional[LLMConfig],
    payload: dict[str, Any],
    *,
    kind: str,
    is_autonomous: Optional[bool],
    holder_kind: Optional[str] = None,
) -> dict[str, Any]:
    """Sync bridge for the decision callback (the sync invoke path).

    Legal because the sync turn runs on an ``asyncio.to_thread`` worker with
    no running loop (same bridge the hook dispatcher uses for its sync
    ``require_approval`` path). If a loop IS running in this thread, parking
    would deadlock it, so the gate degrades to auto instead.

    Stamps ``sync_surface=True``: the sync invoke path serves ``/chat/sync``
    (bots and programmatic callers) whose clients cannot render a consent
    prompt, so the gate never parks these turns; the bridge exists only to
    reach the callback's non-parking decisions.
    """
    if getattr(llm_config, "fallback_decision_callback", None) is None:
        return {"action": "auto"}
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop in this worker thread: exactly the state the
        # asyncio.run bridge below requires, so nothing to handle.
        pass
    else:
        logger.warning(
            "[LLM FALLBACK] sync decision gate called with a running event "
            "loop; proceeding as auto"
        )
        return {"action": "auto"}
    try:
        return asyncio.run(
            _aconsult_fallback_decision(
                llm_config,
                payload,
                kind=kind,
                is_autonomous=is_autonomous,
                holder_kind=holder_kind,
                sync_surface=True,
            )
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[LLM FALLBACK] sync decision bridge failed; proceeding as auto",
            exc_info=True,
        )
        return {"action": "auto"}


def llm_fallback_hold_overrides(
    decision: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """The hold fields of a consent decision, shaped for the activation payload.

    Permanent wins over a timed hold. Returns None when the decision carries
    no hold choice (public: the core stream processor shares this merge).
    """
    overrides: dict[str, Any] = {}
    if decision.get("hold_permanent"):
        overrides["hold_permanent"] = True
    elif decision.get("hold_seconds") is not None:
        overrides["hold_seconds"] = decision["hold_seconds"]
    return overrides or None


def _payload_with_hold(
    payload: dict[str, Any], decision: dict[str, Any]
) -> dict[str, Any]:
    """Merge a consented hold choice into an activation payload."""
    merged = dict(payload)
    merged.update(llm_fallback_hold_overrides(decision) or {})
    return merged


# --------------------------------------------------------------------------- #
# Model-facing swap notes (persisted-context principle, dev-locked 2026-07-25):
# when the runtime switches models mid-conversation, the incoming model is
# told why IN the conversation itself, exactly once, at the point of the
# switch, and the note is persisted to the checkpoint at the position the
# model saw it. Never ephemeral (context a model saw once and later turns
# cannot see causes confusion), never repeated per-turn (unlike the [Time:]
# metadata block, whose content changes every turn). The note merges into the
# EXISTING conversation tail (the last tool result mid-turn, the prompt
# message itself on a first-call switch) rather than adding a synthetic
# message, for the widest provider/API compatibility. The [System info]
# framing follows the established in-result harness notes ([Auth check],
# [Duration:]).
# --------------------------------------------------------------------------- #


def fallback_note_text(payload: dict[str, Any], *, kind: str, phase: str = "swap") -> str:
    """The model-facing note for a model switch (or the end of one).

    Public: the core hold-end latch (``agent_llm_config``) reuses it so the
    swap and end notes cannot drift apart in voice.
    """
    from_model = str(payload.get("from_model") or "the previous model")
    to_model = str(payload.get("to_model") or "the configured fallback model")
    if phase == "end":
        ended = (
            "was manually reverted"
            if payload.get("reason") == "reverted"
            else "expired"
        )
        return (
            f"[System info]: The fallback hold on this thread {ended}; the "
            f"thread is back on its primary model ({to_model}). {from_model} "
            "handled the conversation since the switch."
        )
    if kind == "refusal":
        return (
            f"[System info]: The previous model ({from_model}) refused to "
            "continue this turn: its safety classifier flagged the request as "
            "potentially harmful, which is often a false positive. No output "
            f"was produced. You are now {to_model}, the configured fallback "
            "model, continuing this conversation. Continue the task "
            "naturally, and notify the user that the model was switched."
        )
    reason = str(payload.get("reason") or "provider error")
    status = payload.get("http_status")
    detail = f"{reason}, HTTP {status}" if status else reason
    return (
        f"[System info]: The previous model ({from_model}) failed after "
        f"retries ({detail}). You are now {to_model}, the configured "
        "fallback model, continuing this conversation. Mention the switch "
        "to the user if it is relevant."
    )


def fallback_note_stamp(
    payload: dict[str, Any], *, kind: str, phase: str = "swap", text: str = ""
) -> dict[str, Any]:
    """The ``additional_kwargs["fallback_note"]`` stamp for a note-carrying
    message: records the EXACT appended text (history strips it from the
    rendered bubble and re-emits it as a typed ``fallback_notice`` entry)
    plus the switch facts. Public for the core hold-end latch."""
    return {
        "text": text or fallback_note_text(payload, kind=kind, phase=phase),
        "phase": phase,
        "kind": kind,
        "from_provider": str(payload.get("from_provider") or ""),
        "from_model": str(payload.get("from_model") or ""),
        "to_provider": str(payload.get("to_provider") or ""),
        "to_model": str(payload.get("to_model") or ""),
        "reason": str(payload.get("reason") or ""),
        "http_status": payload.get("http_status"),
    }


def append_fallback_note(message: Any, stamp: dict[str, Any]) -> Any:
    """A copy of ``message`` (same id) with the note appended to its content
    and the stamp on ``additional_kwargs``. String content gets a blank-line
    suffix (never a prefix: the head position belongs to the [Time:] strip
    frame); block-list content gets a trailing text block.

    Public: the ONE owner of the append shape. The stamped ``text`` is the
    exact-suffix strip contract for every reader (history, composer restore,
    RAG flush), so the core hold-end latch (``agent_streaming_input``) reuses
    this instead of authoring the suffix a second time."""
    note = stamp["text"]
    content = getattr(message, "content", None)
    if isinstance(content, list):
        new_content: Any = [*content, {"type": "text", "text": note}]
    else:
        text = content if isinstance(content, str) else str(content or "")
        new_content = f"{text}\n\n{note}" if text else note
    kwargs = dict(getattr(message, "additional_kwargs", None) or {})
    kwargs["fallback_note"] = stamp
    return message.model_copy(
        update={"content": new_content, "additional_kwargs": kwargs}
    )


def _without_appended_note(message: Any, note_text: str) -> Any:
    """Undo ``append_fallback_note`` on a message (exact-suffix removal), so a
    second note on the same tail can re-append one merged block instead of
    layering. Returns the message unchanged when the suffix is not present."""
    content = getattr(message, "content", None)
    if isinstance(content, list):
        if (
            content
            and isinstance(content[-1], dict)
            and content[-1].get("type") == "text"
            and content[-1].get("text") == note_text
        ):
            return message.model_copy(update={"content": list(content[:-1])})
        return message
    text = content if isinstance(content, str) else str(content or "")
    if text.endswith(note_text):
        stripped = text.removesuffix(note_text)
        stripped = stripped.removesuffix("\n\n")
        return message.model_copy(update={"content": stripped})
    return message


def _attach_fallback_note(
    call_messages: Optional[List[BaseMessage]],
    state_messages: Optional[List[Any]],
    payload: dict[str, Any],
    *,
    kind: str,
) -> Optional[Any]:
    """Attach the swap note to the conversation tail, wire AND state.

    Mutates ``call_messages`` in place (replacing its tail element, so the
    retry loops that re-invoke with the same list object send the note to the
    incoming model BEFORE it generates) and returns a note-carrying copy of
    the ORIGINAL state tail for the node to fold into its result (same id, so
    the add_messages reducer replaces it in place). The state copy is built
    from the STATE message, never the wire one: the wire tail may be a
    windowed/sanitized copy (image window, Anthropic sanitizer), and
    persisting that would e.g. strip attachments from state permanently.

    Returns None (note skipped, switch proceeds without it) when the tails
    do not line up or the tail is not a Human/Tool message; defensive, not
    an expected path.
    """
    if not call_messages or not state_messages:
        return None
    wire_tail = call_messages[-1]
    state_tail = state_messages[-1]
    if not isinstance(state_tail, (HumanMessage, ToolMessage)):
        logger.warning(
            "[LLM FALLBACK] swap note skipped: conversation tail is %s",
            type(state_tail).__name__,
        )
        return None
    # An id is required: the add_messages reducer replaces by id, and a
    # message without one would APPEND a duplicate instead of upserting.
    # Checkpointed state always carries ids; this guards direct-invoke
    # callers (tests, exotic embeddings of the node).
    if not getattr(state_tail, "id", None):
        logger.warning("[LLM FALLBACK] swap note skipped: state tail has no id")
        return None
    if getattr(wire_tail, "id", None) != getattr(state_tail, "id", None):
        logger.warning(
            "[LLM FALLBACK] swap note skipped: wire/state tails do not line up"
        )
        return None
    stamp = fallback_note_stamp(payload, kind=kind)
    # The same tail can take a SECOND note in one node run (a site-3 pending
    # note then a transport failure, or a refusal swap whose fallback then
    # fails transport-wise). Our earlier in-place replacement stamped the
    # WIRE tail (the raw state list still holds the pre-note original), and
    # the stamp is single-valued with `text` as the exact-suffix strip
    # contract, so merge: the new stamp wins on the switch facts, `text`
    # grows to cover BOTH blocks, and each tail is stripped of any prior
    # block it carries before the single merged append (no layering, and
    # wire and state end up byte-identical in content).
    prior = (getattr(wire_tail, "additional_kwargs", None) or {}).get(
        "fallback_note"
    ) or (getattr(state_tail, "additional_kwargs", None) or {}).get(
        "fallback_note"
    )
    if isinstance(prior, dict) and prior.get("text"):
        stamp["text"] = f"{prior['text']}\n\n{stamp['text']}"
        wire_tail = _without_appended_note(wire_tail, str(prior["text"]))
        state_tail = _without_appended_note(state_tail, str(prior["text"]))
    call_messages[-1] = append_fallback_note(wire_tail, stamp)
    return append_fallback_note(state_tail, stamp)


def llm_stamp_pending_fallback_note(
    llm_config: Optional[LLMConfig],
    payload: dict[str, Any],
    *,
    kind: str,
) -> None:
    """Stamp a swap note for the NEXT agent-node run to attach (public: the
    post-chunk recovery path in core switches the model outside the graph and
    re-drives it, so it cannot attach the note itself)."""
    if llm_config is None:
        return
    llm_config.pending_fallback_note = {"payload": dict(payload), "kind": kind}


def _consume_pending_fallback_note(
    llm_config: Optional[LLMConfig],
    call_messages: List[BaseMessage],
    state_messages: Optional[List[Any]],
    note_sink: List[Any],
) -> None:
    """Attach + clear a stamped pending note (see llm_stamp_pending_fallback_note)."""
    pending = getattr(llm_config, "pending_fallback_note", None)
    if not pending:
        return
    llm_config.pending_fallback_note = None
    upsert = _attach_fallback_note(
        call_messages,
        state_messages,
        dict(pending.get("payload") or {}),
        kind=str(pending.get("kind") or "transport"),
    )
    if upsert is not None:
        note_sink.append(upsert)


def _result_with_fallback_notes(result: dict, note_sink: List[Any]) -> dict:
    """Fold attached note upserts into a node result (replace-by-id keeps
    each note at its original conversation position)."""
    if note_sink:
        result["messages"] = [*note_sink, *result.get("messages", [])]
    return result


def _reapply_fallback_notes(
    messages: List[BaseMessage], note_sink: List[Any]
) -> None:
    """Re-apply an attached note to a REBUILT wire list.

    The image strip-and-retry rebuilds the outbound messages from state,
    which does not carry the note until the node returns; without this a
    swap-then-image-strip double failure would send the retry blind to a
    note that still persists to the checkpoint. Tail-only, id-matched;
    reversed so a same-tail second (merged) note wins over the first."""
    if not messages or not note_sink:
        return
    tail = messages[-1]
    for noted in reversed(note_sink):
        if getattr(noted, "id", None) == getattr(tail, "id", None):
            stamp = (getattr(noted, "additional_kwargs", None) or {}).get(
                "fallback_note"
            )
            if stamp:
                messages[-1] = append_fallback_note(tail, stamp)
            return


async def llm_consult_transport_fallback(
    llm_config: Optional[LLMConfig],
    *,
    exc: BaseException,
    is_autonomous: Optional[bool],
    holder_kind: Optional[str] = None,
) -> dict[str, Any]:
    """Consent consult for the post-chunk recovery site (core-side, async).

    Builds the same payload the eventual ``llm_activate_next_fallback`` call
    will build, so the prompt shows the real from/to candidates. When no next
    candidate exists the consult is moot ("auto": the activate call returns
    None and the caller raises).
    """
    from_index = _llm_initial_candidate_index(llm_config)
    to_index = from_index + 1
    if to_index >= _llm_candidate_count(llm_config):
        return {"action": "auto"}
    payload = _llm_fallback_payload(llm_config, from_index, to_index, exc=exc)
    return await _aconsult_fallback_decision(
        llm_config,
        payload,
        kind="transport",
        is_autonomous=is_autonomous,
        holder_kind=holder_kind,
    )


def _is_empty_refusal_response(response: Any) -> bool:
    """True for the EMPTY provider-refusal shape (no text, no tool calls).

    Mirrors ``_finish_response``'s refusal detection exactly: Anthropic spells
    it ``stop_reason="refusal"``, OpenAI-shaped gateways
    ``finish_reason="content_filter"``. Partial-output refusals return False:
    the user already saw content, so they are never swapped (or rewound).
    """
    metadata = getattr(response, "response_metadata", None) or {}
    refused = (
        metadata.get("stop_reason") == "refusal"
        or metadata.get("finish_reason") == "content_filter"
    )
    if not refused:
        return False
    if _visible_text_of(getattr(response, "content", None)):
        return False
    return not bool(getattr(response, "tool_calls", None))


def _llm_refusal_payload(
    llm_config: Optional[LLMConfig],
    from_index: int,
    to_index: int,
) -> dict[str, Any]:
    """A fallback payload for a refusal-triggered swap (no exception)."""
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
        "reason": "refusal",
        "http_status": None,
    }


def _refusal_swap_next_index(
    llm_config: Optional[LLMConfig], candidate_index: int
) -> Optional[int]:
    """The candidate to swap to on an empty refusal, or None when the swap
    cannot apply (no consent wiring, or no candidate left).

    The refusal-swap MODE (off/ask/auto) is host policy and lives inside the
    decision callback: an "off" host answers the consult with
    ``{"action": "fail"}`` and the node falls through to the rewind path.
    A None callback means no consent was wired, so the swap cannot apply."""
    if getattr(llm_config, "fallback_decision_callback", None) is None:
        return None
    next_index = candidate_index + 1
    if next_index >= _llm_candidate_count(llm_config):
        return None
    return next_index


def _refusal_swap_telemetry(
    response: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    """The ``response_refused`` payload for a refusal that is about to be
    swapped away: same shape ``_finish_response`` emits, plus ``swapped``
    so consumers know the swap (not the notice/rewind path) handles it."""
    return {
        "produced_output": False,
        "output_tokens": (getattr(response, "usage_metadata", None) or {}).get(
            "output_tokens"
        ),
        "model": payload.get("from_model") or "",
        "swapped": True,
    }


def _visible_text_of(content: Any) -> str:
    """Return only the text a user would actually see.

    ``str(content)`` is not a substitute: on a block-shaped response it renders
    thinking blocks and their base64 signatures too, which turns "produced
    nothing" into a five-figure character count.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (
                "text",
                "output_text",
            ):
                # "output_text" is the OpenAI-Responses spelling of the same
                # thing, and every other extractor in this codebase accepts both
                # (agent_streaming.py, agent_text_extract.py, and the sibling
                # accumulator further down THIS file). Accepting only "text"
                # here would read a productive turn as having produced nothing,
                # so a truncated-but-useful answer would be logged as a dead
                # turn and get the "ran out of budget" notice stapled onto real
                # content.
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return ""


_TRUNCATION_NOTICE = (
    "I ran out of output budget while reasoning about this and did not get to "
    "an answer. Nothing was lost on your end, but the turn produced no result. "
    "Raising the model's max output tokens or lowering the reasoning effort "
    "will fix it. Ask again and I'll retry."
)

# Survives only when the refused turn is NOT rewound (tool activity or prior
# content in the exchange gates the automatic rewind, or the rewind failed),
# so it doubles as the recovery guide for exactly those cases: the refused
# content is still in context, and retrying on the same model tends to
# re-refuse until it is removed or the model changes. Delivered live as a
# trailing response chunk by the astream post-turn block (backlog #105); a
# successful rewind removes the whole message, notice included.
_REFUSAL_NOTICE = (
    "The model declined to continue this response (a provider-side refusal), "
    "so this turn produced no result. Refusals are often phrasing-sensitive "
    "and tend to repeat while the content that triggered them stays in "
    "context. To recover, rewind this thread (edit an earlier message, or "
    "/undo in the CLI) and rephrase the request; if it still refuses, "
    "rewind further, compact the thread (/compact), or switch this thread "
    "to a different model and continue from here."
)


def _with_empty_turn_notice(
    response: AIMessage, notice_text: str = _TRUNCATION_NOTICE
) -> AIMessage:
    """Attach a user-visible note to an otherwise empty response.

    Returns a copy rather than mutating in place, but be clear about what that
    does and does not buy: the caller rebinds ``response`` to this copy and
    returns it, so the PATCHED message is what reaches the checkpoint. The copy
    protects the caller's local reference, not the persisted history.

    Deliberate: a turn that produced only a thinking block (truncated cap or a
    provider refusal) is indistinguishable from success to `should_continue`,
    so the message itself has to carry the explanation. Appending keeps
    thinking blocks first and their signatures untouched, which is what
    Anthropic validates on replay.
    """
    try:
        notice = {"type": "text", "text": notice_text}
        if isinstance(response.content, list):
            new_content: Any = [*response.content, notice]
        elif isinstance(response.content, str) and not response.content:
            new_content = notice_text
        else:
            return response
        patched = response.model_copy(update={"content": new_content})
        return patched
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[LLM] Could not attach empty-turn notice: {exc}")
        return response


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


def _emit_hook_activity_sync(records: list, run_config: Any) -> None:
    """Stream collected mutate-plane hook-activity records (sync tool path).

    Each record is an ephemeral ``hook_activity`` custom event surfaced to the
    chat stream as ``{type: "hook_activity", ...}``. Best-effort: a turn without
    a callback manager (or a stream that already closed) simply drops the line.
    """
    for rec in records:
        _dispatch_provider_event("hook_activity", dict(rec), run_config)


async def _emit_hook_activity(records: list, run_config: Any) -> None:
    """Async twin of :func:`_emit_hook_activity_sync` for the concurrent tool path."""
    for rec in records:
        await _adispatch_provider_event("hook_activity", dict(rec), run_config)


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




def _config_for_candidate(
    llm_config: Optional[LLMConfig], candidate_index: int
) -> Optional[LLMConfig]:
    """The LLMConfig for a given retry/fallback candidate, or None if invalid.

    Mid-fallback the candidate is a DIFFERENT model, so resolving a ceiling
    against the primary's config is how you 400 a rescue attempt.
    """
    if llm_config is None:
        return None
    if candidate_index <= 0:
        return llm_config
    fallbacks = _llm_fallbacks(llm_config)
    if candidate_index - 1 >= len(fallbacks):
        return None
    return _llm_config_for_fallback(llm_config, fallbacks[candidate_index - 1])


async def _warm_max_output_ceiling(
    llm_config: Optional[LLMConfig], candidate_index: int
) -> None:
    """Resolve this candidate's output ceiling OFF the event loop.

    WHY THIS EXISTS. The ceiling probe is blocking HTTP, and LLM construction
    (which resolves it) happens inside this async node, i.e. ON the API loop.
    That left two bad options and no good one: probe on the loop and stall every
    request in the process for up to the probe timeout, or refuse to probe on
    the loop and let the factory bake langchain's invented 4096 into an LLM that
    the graph then CACHES, which is the exact dead-turn bug this whole change
    exists to kill (measured: a cold-start graph build pinned sonnet-5 at 4096).

    So resolve it here first, on a worker thread, before anything constructs an
    LLM. By the time the factory asks, the answer is a warm cache hit and no I/O
    happens on the loop at all. The probe's own loop guard stays as the backstop
    for any path that forgets to warm; this is what makes it never fire.
    """
    if llm_config is None:
        return
    config_for_candidate = _config_for_candidate(llm_config, candidate_index)
    if config_for_candidate is None:
        return
    try:
        await asyncio.to_thread(
            resolve_max_output_tokens, config_for_candidate, probe=True
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[LLM] Could not warm max_tokens ceiling: {exc}")


def _streaming_max_tokens_kwargs(
    candidate: Any,
    llm_config: Optional[LLMConfig],
    candidate_index: int = 0,
    cache: Optional[dict[int, int]] = None,
) -> dict:
    """Opt a STREAMING call up to the model's true output ceiling.

    The shared instance is pinned to the non-streaming-safe clamp because
    several async non-streaming callers reach it (`nym.llm`, structured output,
    callable threads, RAG quality, the zero-chunk stream fallback), and async
    non-streaming is the one shape the Anthropic SDK guard rejects. Streaming
    is exempt, so the user-facing path recovers the full ceiling here.

    Resolved against the CANDIDATE's own config, not the primary's: a fallback
    candidate is a different model with a different ceiling, and handing it the
    primary's would turn a rescue attempt into a 400.

    ``cache`` memoizes the resolved ceiling per candidate index for the life of
    the node, and it is NOT an optimisation. This runs on the asyncio event loop
    inside the astream retry loop, and resolution can do blocking HTTP:
    `get_max_output_tokens` -> `_ensure_cache` -> a blocking openrouter fetch
    (10s), then the probe (8s). Those have their own caches, but
    `model_capabilities._CACHE_FAILURE_TTL_SECONDS` is only 60s, so on a host
    that cannot reach openrouter.ai (a keyless public fetch with no opt-out)
    an un-memoized call would stall the whole event loop for up to 10s on every
    streaming attempt, forever. Memoizing bounds it to once per graph build.
    The proper fix is resolving at the `get_llm_config_for_thread` choke point;
    see the deferred note in the shipped doc.

    Module-level rather than a closure so it can be tested directly: the
    fallback branch is otherwise only reachable by driving a real stream
    failure, which is exactly the kind of thing that ends up untested.

    Best-effort throughout: an override that cannot be computed costs the turn
    some output budget, never the turn itself.
    """
    if llm_config is None:
        return {}
    try:
        # Plain membership, no sentinel: only a REAL ceiling is ever stored
        # (see below), so "present" and "known" are the same question. The
        # sentinel this used to need existed solely to tell a cached None from
        # an absent key, and nothing caches None any more.
        if cache is not None and candidate_index in cache:
            return streaming_call_kwargs(candidate, cache[candidate_index])

        config_for_candidate = _config_for_candidate(llm_config, candidate_index)
        if config_for_candidate is None:
            return {}
        resolved = resolve_max_output_tokens(config_for_candidate, probe=True)
        # Memoize a real ceiling only. Caching a None would pin this graph to
        # the degraded value for its whole life: the cache is per graph build,
        # and a None here means the probe was merely COLD (warming off-loop),
        # not that the model has no ceiling. Re-asking next turn costs a dict
        # lookup, because probe_max_output_tokens caches for a day itself.
        if cache is not None and resolved is not None:
            cache[candidate_index] = resolved
        return streaming_call_kwargs(candidate, resolved)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[LLM] Could not resolve streaming max_tokens: {exc}")
        return {}


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


@dataclass
class _RetryDecision:
    """A single retry/fallback decision returned by `_RetryFallbackController`.

    `action` is one of "raise", "retry", or "fallback". The remaining fields
    carry everything a caller needs to perform the side effects (logging,
    event dispatch, backoff sleep) for that decision. `payload` is the raw,
    side-effect-free event payload; the fallback caller must still run it
    through `_activate_llm_fallback` (which invokes the activation callback)
    before dispatch, then call `commit_fallback` after dispatch.
    """

    action: str
    candidate_index: int = 0
    next_index: int = 0
    attempt: int = 0
    max_retries: int = 0
    delay: float = 0.0
    # Empty for "raise" decisions (never dispatched); populated for retry/fallback.
    payload: dict[str, Any] = field(default_factory=dict)


class _RetryFallbackController:
    """Shared retry/fallback state machine for the sync and async agent nodes.

    Owns the candidate index, retry-attempt counter, and the
    retry-vs-fallback-vs-raise decision so the synchronous invoke path and the
    asynchronous streaming path share one policy. The side effects (logging,
    provider-event dispatch, backoff) stay caller-side because they differ
    between sync and async (text, `time.sleep` vs `asyncio.sleep`, sync vs
    awaited dispatch); only the decision logic and payload construction are
    shared here.
    """

    def __init__(self, llm_config: Optional[LLMConfig]) -> None:
        self.llm_config = llm_config
        self.max_retries = _llm_max_retries(llm_config)
        self.candidate_count = _llm_candidate_count(llm_config)
        self.candidate_index = _llm_initial_candidate_index(llm_config)
        self.retry_attempt = 0

    def classify(self, exc: BaseException) -> _RetryDecision:
        """Decide what to do about `exc`, advancing retry state as needed.

        Increments the retry counter for a "retry" decision and builds the raw
        (side-effect-free) event payloads. Does NOT advance the candidate index
        or mark the active fallback for a "fallback" decision: the caller does
        that via `commit_fallback` after it has logged and dispatched, so the
        dispatch-before-mark ordering is preserved.
        """
        if not _is_retryable_llm_error(exc):
            return _RetryDecision(action="raise")

        if self.retry_attempt < self.max_retries:
            self.retry_attempt += 1
            delay = _llm_retry_delay(self.llm_config, self.retry_attempt)
            payload = _llm_retry_payload(
                self.llm_config,
                self.candidate_index,
                attempt=self.retry_attempt,
                max_retries=self.max_retries,
                delay=delay,
                exc=exc,
            )
            return _RetryDecision(
                action="retry",
                candidate_index=self.candidate_index,
                attempt=self.retry_attempt,
                max_retries=self.max_retries,
                delay=delay,
                payload=payload,
            )

        if self.candidate_index + 1 >= self.candidate_count:
            return _RetryDecision(action="raise")

        next_index = self.candidate_index + 1
        payload = _llm_fallback_payload(
            self.llm_config,
            self.candidate_index,
            next_index,
            exc=exc,
        )
        return _RetryDecision(
            action="fallback",
            candidate_index=self.candidate_index,
            next_index=next_index,
            payload=payload,
        )

    def commit_fallback(self, next_index: int) -> None:
        """Activate the next candidate after a fallback has been dispatched."""
        _mark_llm_fallback_active(self.llm_config, next_index)
        self.candidate_index = next_index
        self.retry_attempt = 0


def _invoke_llm_with_retries(
    invoke: Callable[[BaseChatModel], AIMessage],
    llm_config: Optional[LLMConfig],
    primary_llm: BaseChatModel,
    tools: Optional[List[BaseTool]],
    run_config: Any = None,
    *,
    call_messages: Optional[List[BaseMessage]] = None,
    state_messages: Optional[List[Any]] = None,
    note_sink: Optional[List[Any]] = None,
) -> AIMessage:
    """``call_messages``/``state_messages``/``note_sink`` (all-or-nothing,
    optional for compatibility) let a transport fallback attach the
    model-facing swap note before the re-invoke: ``invoke`` closes over the
    same ``call_messages`` list object, so the in-place tail replacement is
    what the fallback candidate sends."""
    controller = _RetryFallbackController(llm_config)
    candidate_cache = {0: primary_llm}

    while True:
        try:
            candidate = _llm_candidate(
                primary_llm,
                candidate_index=controller.candidate_index,
                llm_config=llm_config,
                tools=tools,
                cache=candidate_cache,
            )
            return invoke(candidate)
        except Exception as exc:
            decision = controller.classify(exc)
            if decision.action == "raise":
                raise

            if decision.action == "retry":
                logger.warning(
                    "[LLM RETRY] transient sync call failure on %s; "
                    "retry %d/%d in %.2fs: %s",
                    _llm_candidate_label(llm_config, decision.candidate_index),
                    decision.attempt,
                    decision.max_retries,
                    decision.delay,
                    exc,
                )
                _dispatch_provider_event("provider_retry", decision.payload, run_config)
                if decision.delay > 0:
                    time.sleep(decision.delay)
                continue

            consent = _consult_fallback_decision_sync(
                llm_config,
                decision.payload,
                kind="transport",
                is_autonomous=_turn_is_autonomous(run_config),
                holder_kind=_turn_holder_kind(run_config),
            )
            if consent.get("action") == "fail":
                logger.warning(
                    "[LLM FALLBACK] fallback to %s declined by the user; "
                    "failing the turn with the original error: %s",
                    _llm_candidate_label(llm_config, decision.next_index),
                    exc,
                )
                raise
            logger.warning(
                "[LLM FALLBACK] transient sync call failure on %s after retries; "
                "switching to %s: %s",
                _llm_candidate_label(llm_config, decision.candidate_index),
                _llm_candidate_label(llm_config, decision.next_index),
                exc,
            )
            payload = _activate_llm_fallback(
                llm_config, _payload_with_hold(decision.payload, consent)
            )
            _dispatch_provider_event("provider_fallback", payload, run_config)
            controller.commit_fallback(decision.next_index)
            if note_sink is not None:
                upsert = _attach_fallback_note(
                    call_messages, state_messages, payload, kind="transport"
                )
                if upsert is not None:
                    note_sink.append(upsert)

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
                # The ordered-execution marker is excluded from the repeated-result
                # guard: a constant marker plus a repeated batch shape would collide
                # at the limit, force-stopping legitimate repeated ordered batches.
                # Its sibling tools are still tracked, so real runaway loops of the
                # actual work still trip the guard.
                if tool_name == SEQUENTIAL_ORDER_TOOL_NAME:
                    continue
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
    tool_call_offset: int = 0,
) -> TurnSafetyResult:
    """Detect hard iteration caps and exact repeated tool/result loops.

    The repeated-result guard stops before executing another tool call when the
    last N completed tool exchanges used the same tool args and returned the
    same exact normalized result, and the model asks for that same call again.

    The iteration cap fires on two tail shapes:

    - an ``AIMessage`` with pending ``tool_calls`` (the pre-execution shape,
      used by direct callers and the pre-#27 router), and
    - a ``ToolMessage``-terminal tail (the graceful sub-turn boundary where
      ``route_after_tools`` halts: the crossing batch has executed, its
      results are in history, and the model call is still pending). This is
      also how the turn drivers detect a graceful cap halt post-drive.

    ``tool_call_offset`` anchors the safety window at a resume point: a
    ``/resume`` re-drive has no fresh ``HumanMessage`` to reset the window
    (unlike compaction's resume opener or a drained queued prompt), so the
    resume path stamps the tool-call count at resume time and the effective
    count becomes ``count_since_last_human - offset``. If a queued prompt
    later resets the window mid-continuation, the clamp below just makes the
    offset slack: the cap is a runaway bound, not an exact budget.
    """
    # Compute the current-turn slice once and reuse it for the tool-call count
    # and the completed-exchange walk below (the router runs after every step,
    # so avoid re-walking the slice three times per call).
    current_turn = _current_turn_messages(messages)
    raw_tool_call_count = sum(
        len(msg.tool_calls)
        for msg in current_turn
        if isinstance(msg, AIMessage) and msg.tool_calls
    )
    tool_call_count = max(0, raw_tool_call_count - max(0, int(tool_call_offset or 0)))
    result = TurnSafetyResult(
        should_stop=False,
        tool_call_count=tool_call_count,
        max_iterations=max_iterations,
    )

    if not messages:
        return result

    last_message = messages[-1]
    pending_ai_tail = isinstance(last_message, AIMessage) and bool(last_message.tool_calls)
    tool_terminal_tail = isinstance(last_message, ToolMessage)
    if not (pending_ai_tail or tool_terminal_tail):
        return result

    if max_iterations > 0 and tool_call_count > max_iterations:
        return TurnSafetyResult(
            should_stop=True,
            reason=TURN_SAFETY_REASON_MAX_ITERATIONS,
            tool_call_count=tool_call_count,
            max_iterations=max_iterations,
        )

    # The repeated-result guard needs a pending tool call to compare against;
    # a ToolMessage-terminal tail has none (cap check only).
    if not pending_ai_tail:
        return result

    if repeated_tool_result_limit <= 0:
        return result

    prior_messages = current_turn[:-1] if current_turn and current_turn[-1] is last_message else current_turn
    completed_exchanges = _completed_tool_exchanges(prior_messages)
    if not completed_exchanges:
        return result

    for tool_call in last_message.tool_calls:
        tool_name, signature = _tool_call_signature(tool_call)
        # See _completed_tool_exchanges: the ordered-execution marker never trips
        # the repeated-result guard on its own.
        if tool_name == SEQUENTIAL_ORDER_TOOL_NAME:
            continue
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


def _window_images_for_llm(
    messages: List[BaseMessage],
    llm_config: Optional[LLMConfig],
    thread_id: Optional[str] = None,
) -> List[BaseMessage]:
    from ...core.generated_image_context import window_images_for_llm

    return window_images_for_llm(messages, llm_config, thread_id=thread_id)


def _thread_id_from_config(config: Any) -> Optional[str]:
    """Best-effort extract the thread_id from a LangGraph RunnableConfig."""
    try:
        if isinstance(config, dict):
            configurable = config.get("configurable") or {}
        else:
            configurable = getattr(config, "configurable", {}) or {}
        thread_id = configurable.get("thread_id")
        return str(thread_id) if thread_id else None
    except Exception:  # noqa: BLE001
        return None


def _accumulate_llm_seconds(config: Any, seconds: float) -> None:
    """Add one model call's wall-clock duration to the turn's accumulator.

    ``NymeriaAgent.astream``/``chat`` inject a mutable dict per turn as
    ``config["configurable"]["llm_timing"]``; each successful model call adds
    its streaming duration so the turn's tokens-per-second rate can exclude
    tool execution. Side-channel graph runs (compaction summaries,
    ``nym.llm``, dream seeding) build their own configs without the key and
    are excluded by construction. Timing must never break a turn, so any
    failure is swallowed.
    """
    try:
        if isinstance(config, dict):
            configurable = config.get("configurable") or {}
        else:
            configurable = getattr(config, "configurable", {}) or {}
        timing = configurable.get("llm_timing")
        if isinstance(timing, dict):
            timing["seconds"] = float(timing.get("seconds", 0.0)) + max(0.0, seconds)
            timing["calls"] = int(timing.get("calls", 0)) + 1
    except Exception:  # noqa: BLE001
        pass


def _reasoning_passback_requested(config: Any) -> bool:
    """True when the turn opted into reasoning-passback observation.

    ``NymeriaAgent.astream``/``chat`` set ``configurable["reasoning_passback"]``
    on user-facing turns. Side-channel graph runs (compaction, ``nym.llm``,
    dream seeding) build their own configs without it, so they are excluded.
    """
    try:
        if isinstance(config, dict):
            configurable = config.get("configurable") or {}
        else:
            configurable = getattr(config, "configurable", {}) or {}
        return "reasoning_passback" in configurable
    except Exception:  # noqa: BLE001
        return False


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
    def _prepare_messages(state: AgentState, config: Any = None) -> List[BaseMessage]:
        thread_id = _thread_id_from_config(config)
        messages = _window_images_for_llm(state["messages"], llm_config, thread_id)
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

        # Reasoning-passback live confirmation: observe whether prior-turn
        # reasoning is present to replay. Gated on the per-turn marker set by
        # NymeriaAgent.astream/chat, so pure side-channel LLM calls that build
        # their own configs (compaction summaries, nym.llm) are excluded.
        # Autonomous/dream turns DO run it, but keyed by their own (shadow)
        # thread_id, so they never affect the user thread's status. Never raises.
        if _reasoning_passback_requested(config):
            record_passback_observation(thread_id, llm_config, messages)

        # Prepend system prompt (not stored in state)
        return [
            SystemMessage(content=_format_system_prompt(system_prompt, llm_config))
        ] + messages

    def _image_stripped_retry_messages(
        exc: BaseException,
        state: AgentState,
        config: Any,
    ) -> Optional[List[BaseMessage]]:
        """If ``exc`` is a model-can't-see-images rejection, learn it and rebuild.

        Records the model as image/file incapable in the capability live cache
        (self-correcting: subsequent turns strip proactively), then returns fresh
        outbound messages, which now route through the image window that strips
        the attachment and inserts the "cannot view images. It is saved at
        <path>." placeholder so the model knows what happened. Returns None when
        ``exc`` is not an image/file capability rejection, so the caller falls
        through to the normal retry/raise path. Marks the primary
        ``llm_config.model`` (not a mid-fallback candidate); acceptable for a
        backstop.
        """
        if not is_image_unsupported_error(exc):
            return None
        from ...config.model_capabilities import mark_model_input_unsupported

        model = getattr(llm_config, "model", "") or ""
        mark_model_input_unsupported(model, "image", "file")
        logger.warning(
            "[LLM] Model %s rejected image/file input; stripping attachment and "
            "retrying once: %s",
            model or "?",
            exc,
        )
        return _prepare_messages(state, config)

    # Resolved streaming ceiling per candidate index, for the life of this node
    # (i.e. per graph build, and graphs are cached per thread). Keeps blocking
    # discovery HTTP off the event loop on every astream attempt; see
    # _streaming_max_tokens_kwargs.
    streaming_ceiling_cache: dict[int, int] = {}

    def _finish_response(response: AIMessage, config: Any = None) -> dict:
        # Sanitize tool call names — some models emit leading/trailing whitespace
        # (e.g. ' CalendarAgent' instead of 'CalendarAgent') which breaks routing.
        if hasattr(response, 'tool_calls') and response.tool_calls:
            for tc in response.tool_calls:
                if 'name' in tc and tc['name'] != tc['name'].strip():
                    logger.warning(f"[LLM] Stripped whitespace from tool call name: {tc['name']!r} -> {tc['name'].strip()!r}")
                    tc['name'] = tc['name'].strip()

        # Log response summary at INFO. Measure the USER-VISIBLE text, not
        # str(response.content): stringifying the block list counts thinking
        # blocks and their multi-KB base64 signatures, so a turn that returned
        # nothing but truncated reasoning used to log
        # "[LLM] Response: 19626 chars, final answer": a large number, on the
        # one line an operator would grep, for a turn the user never saw.
        visible_text = _visible_text_of(response.content)
        has_tools = bool(response.tool_calls) if hasattr(response, 'tool_calls') else False
        tool_names = [tc.get('name', '?') for tc in response.tool_calls] if has_tools else []
        logger.info(
            f"[LLM] Response: {len(visible_text)} chars"
            + (f", tool_calls={tool_names}" if has_tools else ", final answer")
        )

        # Truncation detection. Providers spell this two different ways and the
        # check previously only knew the OpenAI one, so it was dead code on the
        # Anthropic path: measured across 658 Anthropic messages, `finish_reason`
        # was present in exactly zero of them.
        metadata = getattr(response, "response_metadata", None) or {}
        truncated = (
            metadata.get("finish_reason") == "length"          # OpenAI-shaped
            or metadata.get("stop_reason") == "max_tokens"     # Anthropic-shaped
        )
        if truncated:
            reason = (
                "stop_reason='max_tokens'"
                if metadata.get("stop_reason") == "max_tokens"
                else "finish_reason='length'"
            )
            output_tokens = (getattr(response, "usage_metadata", None) or {}).get(
                "output_tokens"
            )
            if not visible_text and not has_tools:
                # The fatal shape. Thinking tokens bill against max_tokens and
                # are emitted BEFORE any text or tool_use, so exhausting the cap
                # inside the thinking block yields a message with nothing in it.
                # `should_continue` sees no tool_calls, routes to "end", and the
                # turn "succeeds" while delivering silence. Never let that pass
                # quietly again.
                logger.error(
                    f"[LLM] DEAD TURN: the model hit its output cap ({reason}, "
                    f"output_tokens={output_tokens}) before emitting any text or "
                    f"tool call, so this turn produced nothing to deliver. This "
                    f"is almost always reasoning consuming the whole max_tokens "
                    f"budget: raise max_tokens or lower the reasoning effort."
                )
                _dispatch_provider_event(
                    "output_truncated",
                    {
                        "reason": "max_tokens",
                        "produced_output": False,
                        "output_tokens": output_tokens,
                        "model": getattr(llm_config, "model", "") or "",
                    },
                    config,
                )
                # Give the turn a body. Silence is indistinguishable from a
                # healthy no-op to every downstream consumer (the bots drop an
                # empty text buffer, the graph ends cleanly); a visible note is
                # the difference between a bug report and a mystery.
                response = _with_empty_turn_notice(response)
            else:
                logger.warning(
                    f"[LLM] TRUNCATED: response hit the max_tokens cap "
                    f"({reason}, output_tokens={output_tokens}, "
                    f"visible_chars={len(visible_text)}). Output was cut off "
                    f"mid-generation."
                )
                _dispatch_provider_event(
                    "output_truncated",
                    {
                        "reason": "max_tokens",
                        "produced_output": True,
                        "output_tokens": output_tokens,
                        "model": getattr(llm_config, "model", "") or "",
                    },
                    config,
                )

        # Refusal detection: the provider ended the message because the model
        # declined to continue. Anthropic spells it stop_reason="refusal";
        # OpenAI-shaped gateways spell it finish_reason="content_filter".
        # Measured 2026-07-24 on claude-fable-5 via CLIProxy: a benign-looking
        # "try to break what the bash tool claims" prompt returned a lone
        # 644-char thinking block, no text, no tool calls, stop_reason
        # "refusal", and the turn delivered pure silence that was reported as
        # a client bug. Same fatal shape as the dead turn above, different
        # cause, so it gets the same treatment: never let it pass quietly.
        refused = (
            metadata.get("stop_reason") == "refusal"           # Anthropic-shaped
            or metadata.get("finish_reason") == "content_filter"  # OpenAI-shaped
        )
        if refused:
            output_tokens = (getattr(response, "usage_metadata", None) or {}).get(
                "output_tokens"
            )
            if not visible_text and not has_tools:
                logger.warning(
                    f"[LLM] REFUSED TURN: the provider ended the response with a "
                    f"refusal (output_tokens={output_tokens}) before any text or "
                    f"tool call, so this turn produced nothing to deliver. A "
                    f"visible note was attached; rephrasing the prompt usually "
                    f"resolves it."
                )
                _dispatch_provider_event(
                    "response_refused",
                    {
                        "produced_output": False,
                        "output_tokens": output_tokens,
                        "model": getattr(llm_config, "model", "") or "",
                        "swapped": False,
                    },
                    config,
                )
                response = _with_empty_turn_notice(response, _REFUSAL_NOTICE)
                # Marker for the post-turn refusal rewind (backlog #105,
                # core/agent_context.maybe_rewind_refused_turn): stamped only
                # on the EMPTY shape so partial-output refusals (user already
                # saw content) are never rewound. additional_kwargs persists
                # through the checkpoint (tool_timing rides it the same way).
                # When the rewind succeeds the whole exchange, notice and all,
                # is removed; when it fails the notice above is the fallback.
                response.additional_kwargs["empty_turn_refusal"] = True
            else:
                # Partial output then a refusal: the user already saw content,
                # so log and signal without rewriting the message.
                logger.warning(
                    f"[LLM] Response ended with a provider refusal after "
                    f"producing output (visible_chars={len(visible_text)}, "
                    f"tool_calls={tool_names})."
                )
                _dispatch_provider_event(
                    "response_refused",
                    {
                        "produced_output": True,
                        "output_tokens": output_tokens,
                        "model": getattr(llm_config, "model", "") or "",
                        "swapped": False,
                    },
                    config,
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
        # Sync graph callers use the normal invoke path. User-facing live
        # streaming runs through the async node below.
        messages_with_system = _prepare_messages(state, config)
        # Timing brackets the retry helper, so on the (rare) retried call the
        # accumulated duration includes backoff sleeps; acceptable for the
        # sync path, which the live tokens/s display does not ride.
        call_started_at = time.monotonic()
        image_strip_attempted = False
        refusal_swap_attempted = False
        state_messages = state.get("messages") or []
        note_sink: List[Any] = []
        _consume_pending_fallback_note(
            llm_config, messages_with_system, state_messages, note_sink
        )
        while True:
            try:
                # No clamp needed here: the instance already carries the
                # non-streaming-safe ceiling (see instance_max_output_tokens).
                # Only the streaming node opts up.
                response = _invoke_llm_with_retries(
                    lambda candidate, _msgs=messages_with_system: candidate.invoke(_msgs),
                    llm_config,
                    llm_with_tools,
                    tools,
                    config,
                    call_messages=messages_with_system,
                    state_messages=state_messages,
                    note_sink=note_sink,
                )
                # Refusal swap (#105 P2): discard an EMPTY refusal before it
                # enters graph state and re-run once on the next fallback
                # candidate, subject to the consent gate. Mirrors the async
                # block in async_agent_node (see the comment there).
                if (
                    not refusal_swap_attempted
                    and _is_empty_refusal_response(response)
                ):
                    from_index = _llm_initial_candidate_index(llm_config)
                    next_index = _refusal_swap_next_index(llm_config, from_index)
                    if next_index is not None:
                        payload = _llm_refusal_payload(
                            llm_config, from_index, next_index
                        )
                        consent = _consult_fallback_decision_sync(
                            llm_config,
                            payload,
                            kind="refusal",
                            is_autonomous=_turn_is_autonomous(config),
                            holder_kind=_turn_holder_kind(config),
                        )
                        if consent.get("action") == "swap":
                            logger.warning(
                                "[LLM FALLBACK] empty provider refusal on %s; "
                                "discarding the refused response and re-running "
                                "on %s",
                                _llm_candidate_label(llm_config, from_index),
                                _llm_candidate_label(llm_config, next_index),
                            )
                            # The refusal stays visible in telemetry even
                            # though the swap discards it: #105's premise is
                            # that refusals must never pass silently.
                            _dispatch_provider_event(
                                "response_refused",
                                _refusal_swap_telemetry(response, payload),
                                config,
                            )
                            payload = _activate_llm_fallback(
                                llm_config, _payload_with_hold(payload, consent)
                            )
                            _dispatch_provider_event(
                                "provider_fallback", payload, config
                            )
                            _mark_llm_fallback_active(llm_config, next_index)
                            upsert = _attach_fallback_note(
                                messages_with_system,
                                state_messages,
                                payload,
                                kind="refusal",
                            )
                            if upsert is not None:
                                note_sink.append(upsert)
                            refusal_swap_attempted = True
                            continue
                break
            except Exception as exc:
                # One-shot image strip-and-retry: a model that rejects image/file
                # input gets the attachment stripped (with a placeholder note) and
                # one more try, instead of failing the turn.
                if (
                    not image_strip_attempted
                    and _messages_have_attachment_blocks(messages_with_system)
                ):
                    rebuilt = _image_stripped_retry_messages(exc, state, config)
                    if rebuilt is not None:
                        messages_with_system = rebuilt
                        _reapply_fallback_notes(messages_with_system, note_sink)
                        image_strip_attempted = True
                        _dispatch_provider_event(
                            "image_input_unsupported",
                            {
                                "reason": "image_input_unsupported",
                                "model": getattr(llm_config, "model", "") or "",
                            },
                            config,
                        )
                        continue
                raise
        _accumulate_llm_seconds(config, time.monotonic() - call_started_at)
        return _result_with_fallback_notes(
            _finish_response(response, config), note_sink
        )

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
        messages_with_system = _prepare_messages(state, config)
        merged_chunk = None
        stream_started_at = time.monotonic()
        first_chunk_ms: Optional[int] = None
        stream_chunks = 0
        text_chunks = 0
        text_chars = 0
        reasoning_chunks = 0
        reasoning_chars = 0
        tool_call_chunk_events = 0

        controller = _RetryFallbackController(llm_config)
        candidate_cache = {0: llm_with_tools}
        image_strip_attempted = False
        refusal_swap_attempted = False
        state_messages = state.get("messages") or []
        note_sink: List[Any] = []
        _consume_pending_fallback_note(
            llm_config, messages_with_system, state_messages, note_sink
        )
        while True:
            chunks_this_attempt = 0
            try:
                # Off-loop FIRST: _llm_candidate constructs the LLM, and the
                # Anthropic factory resolves (and may probe for) the output
                # ceiling while doing so. See _warm_max_output_ceiling.
                await _warm_max_output_ceiling(llm_config, controller.candidate_index)
                candidate = _llm_candidate(
                    llm_with_tools,
                    candidate_index=controller.candidate_index,
                    llm_config=llm_config,
                    tools=tools,
                    cache=candidate_cache,
                )
                # Per-attempt clock for the tokens/s accumulator: unlike
                # stream_started_at above (kept for the first_chunk/elapsed
                # log), it resets on every retry/fallback attempt so backoff
                # sleeps never count as generation time.
                attempt_started_at = time.monotonic()
                # Streaming is exempt from the SDK's non-streaming guard, so the
                # user-facing path opts back up to the model's true ceiling that
                # the shared instance had to clamp for its non-streaming callers.
                # Keyed on the candidate index, never on llm_config alone: mid
                # fallback the candidate is a DIFFERENT model, and handing it
                # the primary's ceiling is how you 400 a rescue attempt.
                async for chunk in candidate.astream(
                    messages_with_system,
                    **_streaming_max_tokens_kwargs(
                        candidate,
                        llm_config,
                        controller.candidate_index,
                        cache=streaming_ceiling_cache,
                    ),
                ):
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

                # Refusal swap (#105 P2): an EMPTY provider refusal (no text,
                # no tool calls) is discarded BEFORE it enters graph state and
                # the same call re-runs once on the next fallback candidate,
                # subject to the consent gate. The refused message never
                # reaching state is what satisfies Anthropic's reset-the-
                # refused-turn requirement; a declined/off/second refusal
                # falls through to _finish_response, whose marker drives the
                # shipped post-turn rewind (backlog #105 P1). Mirrors the
                # sync block in agent_node.
                if (
                    not refusal_swap_attempted
                    and _is_empty_refusal_response(response)
                ):
                    next_index = _refusal_swap_next_index(
                        llm_config, controller.candidate_index
                    )
                    if next_index is not None:
                        payload = _llm_refusal_payload(
                            llm_config, controller.candidate_index, next_index
                        )
                        consent = await _aconsult_fallback_decision(
                            llm_config,
                            payload,
                            kind="refusal",
                            is_autonomous=_turn_is_autonomous(config),
                            holder_kind=_turn_holder_kind(config),
                        )
                        if consent.get("action") == "swap":
                            logger.warning(
                                "[LLM FALLBACK] empty provider refusal on %s; "
                                "discarding the refused response and re-running "
                                "on %s",
                                _llm_candidate_label(
                                    llm_config, controller.candidate_index
                                ),
                                _llm_candidate_label(llm_config, next_index),
                            )
                            # The refusal stays visible in telemetry even
                            # though the swap discards it. Deliberate
                            # asymmetry with the transport rule below (which
                            # never retries after a streamed chunk): an EMPTY
                            # refusal streamed at most thinking, never visible
                            # text or tool calls, so re-running duplicates a
                            # reasoning trace on screen but never duplicates
                            # output. Accepted; the Phase 2 consent card
                            # accounts for it.
                            await _adispatch_provider_event(
                                "response_refused",
                                _refusal_swap_telemetry(response, payload),
                                config,
                            )
                            payload = _activate_llm_fallback(
                                llm_config, _payload_with_hold(payload, consent)
                            )
                            await _adispatch_provider_event(
                                "provider_fallback", payload, config
                            )
                            controller.commit_fallback(next_index)
                            upsert = _attach_fallback_note(
                                messages_with_system,
                                state_messages,
                                payload,
                                kind="refusal",
                            )
                            if upsert is not None:
                                note_sink.append(upsert)
                            refusal_swap_attempted = True
                            # The refused attempt completed and billed tokens;
                            # its generation time counts.
                            _accumulate_llm_seconds(
                                config, time.monotonic() - attempt_started_at
                            )
                            merged_chunk = None
                            continue

                # Only successful calls contribute generation time (a failed
                # attempt's tokens are discarded with it).
                _accumulate_llm_seconds(config, time.monotonic() - attempt_started_at)
                break
            except Exception as exc:
                if chunks_this_attempt > 0:
                    try:
                        setattr(exc, "nymeria_stream_chunks_before_error", chunks_this_attempt)
                        setattr(exc, "nymeria_stream_candidate_index", controller.candidate_index)
                        setattr(exc, "nymeria_stream_candidate_label", _llm_candidate_label(llm_config, controller.candidate_index))
                    except Exception:
                        logger.debug("Failed to annotate streaming exception", exc_info=True)
                    logger.warning(
                        "[LLM RETRY] stream failed after %d chunk(s) on %s; "
                        "not retrying to avoid duplicated output: %s",
                        chunks_this_attempt,
                        _llm_candidate_label(llm_config, controller.candidate_index),
                        exc,
                    )
                    raise

                # One-shot image strip-and-retry (chunks_this_attempt == 0 here,
                # so no partial output was emitted): a model that rejects
                # image/file input gets the attachment stripped (with a
                # placeholder note) and one more try before the normal
                # retry/fallback/raise path.
                if (
                    not image_strip_attempted
                    and _messages_have_attachment_blocks(messages_with_system)
                ):
                    rebuilt = _image_stripped_retry_messages(exc, state, config)
                    if rebuilt is not None:
                        messages_with_system = rebuilt
                        _reapply_fallback_notes(messages_with_system, note_sink)
                        image_strip_attempted = True
                        await _adispatch_provider_event(
                            "image_input_unsupported",
                            {
                                "reason": "image_input_unsupported",
                                "model": getattr(llm_config, "model", "") or "",
                            },
                            config,
                        )
                        continue

                decision = controller.classify(exc)
                if decision.action == "raise":
                    raise

                if decision.action == "retry":
                    logger.warning(
                        "[LLM RETRY] transient stream failure before chunks on %s; "
                        "retry %d/%d in %.2fs: %s",
                        _llm_candidate_label(llm_config, decision.candidate_index),
                        decision.attempt,
                        decision.max_retries,
                        decision.delay,
                        exc,
                    )
                    await _adispatch_provider_event(
                        "provider_retry",
                        decision.payload,
                        config,
                    )
                    if decision.delay > 0:
                        await asyncio.sleep(decision.delay)
                    continue

                consent = await _aconsult_fallback_decision(
                    llm_config,
                    decision.payload,
                    kind="transport",
                    is_autonomous=_turn_is_autonomous(config),
                    holder_kind=_turn_holder_kind(config),
                )
                if consent.get("action") == "fail":
                    logger.warning(
                        "[LLM FALLBACK] fallback to %s declined by the user; "
                        "failing the turn with the original error: %s",
                        _llm_candidate_label(llm_config, decision.next_index),
                        exc,
                    )
                    raise
                logger.warning(
                    "[LLM FALLBACK] transient stream failure before chunks on %s "
                    "after retries; switching to %s: %s",
                    _llm_candidate_label(llm_config, decision.candidate_index),
                    _llm_candidate_label(llm_config, decision.next_index),
                    exc,
                )
                payload = _activate_llm_fallback(
                    llm_config, _payload_with_hold(decision.payload, consent)
                )
                await _adispatch_provider_event("provider_fallback", payload, config)
                controller.commit_fallback(decision.next_index)
                upsert = _attach_fallback_note(
                    messages_with_system, state_messages, payload, kind="transport"
                )
                if upsert is not None:
                    note_sink.append(upsert)

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
        return _result_with_fallback_notes(
            _finish_response(response, config), note_sink
        )

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


# Auth-failure detection for the structured [Auth check] signal (dev-todo #44).
# Matched only against error-shaped results of tools that map to a provider in
# the credential-spec registry, so ordinary page/content text cannot trigger it.
# Bare "forbidden" is deliberately NOT an alternative (it appears in ordinary
# error prose, e.g. plan-restriction messages or echoed user data); forbidden
# only counts next to a 403 or an access/permission phrase.
_AUTH_FAILURE_RE = re.compile(
    r"(?i)("
    r"\bHTTP[ /]?40[13]\b"
    r"|\b40[13]\s+(?:unauthorized|forbidden|client error)"
    r"|\bunauthorized\b"
    r"|\b(?:access|permission|request) (?:is )?(?:denied|forbidden)\b"
    r"|\bauthentication (?:failed|error|required)\b"
    r"|\bnot authenticated\b"
    r"|\binvalid (?:api[ _-]?key|token|credentials?)\b"
    r"|\bapi[ _-]?key (?:is )?(?:invalid|missing|required|expired)\b"
    r"|\b(?:token|credential) (?:is |has )?expired\b"
    r"|\bno [\w .-]{1,40} credential found\b"
    r"|\bcredential not found\b"
    r")"
)


def _auth_failure_guidance(tool_name: str, provider: str, status: str) -> str:
    """The [Auth check] block appended to an auth-shaped tool failure.

    Status-tailored next step consuming the credential-spec registry; the
    retry rides the model loop (no silent re-execution).
    """
    if status == "needs_setup":
        step = (
            f'no credential is saved for "{provider}". Ask the user, then use '
            f'request_credential(provider="{provider}") for a hosted form, or '
            'auth_write(operation="create", ...) if they paste a key in chat'
        )
    elif status == "pending":
        step = (
            f'a "{provider}" credential setup is pending; the user has not '
            "finished saving it. Ask them to complete it, or re-request with "
            "request_credential"
        )
    elif status == "optional":
        step = (
            f'"{provider}" works without a credential, but this failure '
            "suggests one may be needed. Ask the user, then use "
            f'request_credential(provider="{provider}") or '
            'auth_write(operation="create", ...) with a user-pasted key'
        )
    else:  # connected: something is saved but the call still failed
        step = (
            f'run auth_test(tool_name="{tool_name}") to verify the saved '
            f'credential; if it is invalid, request_credential(provider="{provider}") '
            'or auth_write(operation="replace_secret", ...) with a user-pasted key'
        )
    return (
        "\n\n[Auth check]: This looks like an authentication failure. "
        f'Provider "{provider}", credential status: {status}. Next: {step}. '
        'Skill(name="credential-management") has the full flow.'
    )


def format_tool_duration_ms(duration_ms: int) -> str:
    """Compact human duration: 850ms, 3.4s, 42s (matches the client badge format)."""
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    seconds = duration_ms / 1000
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{round(seconds)}s"


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
        # The thread's EFFECTIVE (gated, visible) tool-name set, refreshed per
        # batch from the resolver in ``_parse_input``. Used to tell a bound call
        # from an unbound one at dispatch. ``None`` = dynamic binding off or the
        # resolver was unavailable, in which case unbound-call enforcement is
        # skipped (the dispatch table is already the exact bound set).
        self._effective_tool_names: Optional[set[str]] = None

    # ------------------------------------------------------------------ #
    # Lifecycle hooks: PRE_TOOL_USE / POST_TOOL_USE seam.
    #
    # We override _run_one/_arun_one (used by BOTH the concurrent and the
    # sequential execution paths) rather than install a ``wrap_tool_call``
    # interceptor. The parent wraps the interceptor call in a blanket
    # ``except Exception`` that would convert a GraphBubbleUp/GraphInterrupt into
    # an error ToolMessage; calling ``_execute_tool_sync``/``_execute_tool_async``
    # directly preserves the parent's interrupt re-raise. When no PRE/POST tool
    # hook is registered we delegate straight to ``super()``, so the hot path and
    # its error semantics are unchanged by default.
    # ------------------------------------------------------------------ #

    def _run_one(self, call: ToolCall, input_type, tool_runtime: ToolRuntime):
        from ...core import hooks
        config = tool_runtime.config
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        unbound = self._unbound_call_message(call, config)
        if unbound is not None:
            return self._stamp_tool_timing(unbound, started_at, started_monotonic, config)
        registry = self._hook_registry(config)
        if not hooks.tool_hooks_active(registry):
            return self._stamp_tool_timing(
                self._augment_auth_failure(
                    super()._run_one(call, input_type, tool_runtime), call, config
                ),
                started_at,
                started_monotonic,
                config,
            )
        from langgraph.prebuilt.tool_node import ToolCallRequest

        pre_activity: list = []
        pre = hooks.dispatch(
            hooks.HookEvent.PRE_TOOL_USE,
            self._build_tool_hook_ctx(
                hooks.HookEvent.PRE_TOOL_USE, call, config, state=tool_runtime.state
            ),
            registry=registry,
            emit=pre_activity.append,
        )
        _emit_hook_activity_sync(pre_activity, config)
        call, denied = self._apply_pre_tool_outcome(pre, call)
        if denied is not None:
            return denied
        tool = self.tools_by_name.get(call["name"])
        request = ToolCallRequest(
            tool_call=call, tool=tool, state=tool_runtime.state, runtime=tool_runtime
        )
        result = self._execute_tool_sync(request, input_type, config)
        post_ctx = self._build_tool_hook_ctx(
            hooks.HookEvent.POST_TOOL_USE, call, config,
            result_text=self._tool_result_text(result),
            tool_status=self._tool_result_status(result),
            state=tool_runtime.state,
        )
        post_activity: list = []
        post = hooks.dispatch(
            hooks.HookEvent.POST_TOOL_USE, post_ctx, registry=registry,
            emit=post_activity.append,
        )
        _emit_hook_activity_sync(post_activity, config)
        final = self._apply_post_tool_outcome(result, post)
        # Observe-plane side effects (notify/create_todo/webhook) see the same
        # original result; scheduled off-turn, so they neither delay the tool
        # return nor count against the tool timeout budget.
        hooks.schedule_observe(hooks.HookEvent.POST_TOOL_USE, post_ctx, registry=registry)
        return self._stamp_tool_timing(
            self._augment_auth_failure(final, call, config),
            started_at,
            started_monotonic,
            config,
        )

    async def _arun_one(self, call: ToolCall, input_type, tool_runtime: ToolRuntime):
        from ...core import hooks
        config = tool_runtime.config
        started_at = datetime.now(timezone.utc)
        started_monotonic = time.monotonic()
        unbound = self._unbound_call_message(call, config)
        if unbound is not None:
            return self._stamp_tool_timing(unbound, started_at, started_monotonic, config)
        registry = self._hook_registry(config)
        if not hooks.tool_hooks_active(registry):
            return self._stamp_tool_timing(
                await self._a_augment_auth_failure(
                    await super()._arun_one(call, input_type, tool_runtime), call, config
                ),
                started_at,
                started_monotonic,
                config,
            )
        from langgraph.prebuilt.tool_node import ToolCallRequest

        pre_activity: list = []
        pre = await hooks.adispatch(
            hooks.HookEvent.PRE_TOOL_USE,
            self._build_tool_hook_ctx(
                hooks.HookEvent.PRE_TOOL_USE, call, config, state=tool_runtime.state
            ),
            registry=registry,
            emit=pre_activity.append,
        )
        await _emit_hook_activity(pre_activity, config)
        call, denied = self._apply_pre_tool_outcome(pre, call)
        if denied is not None:
            return denied
        tool = self.tools_by_name.get(call["name"])
        request = ToolCallRequest(
            tool_call=call, tool=tool, state=tool_runtime.state, runtime=tool_runtime
        )
        result = await self._execute_tool_async(request, input_type, config)
        post_ctx = self._build_tool_hook_ctx(
            hooks.HookEvent.POST_TOOL_USE, call, config,
            result_text=self._tool_result_text(result),
            tool_status=self._tool_result_status(result),
            state=tool_runtime.state,
        )
        post_activity: list = []
        post = await hooks.adispatch(
            hooks.HookEvent.POST_TOOL_USE, post_ctx, registry=registry,
            emit=post_activity.append,
        )
        await _emit_hook_activity(post_activity, config)
        final = self._apply_post_tool_outcome(result, post)
        # Observe-plane side effects (notify/create_todo/webhook) see the same
        # original result; scheduled off-turn (a loop task), so they neither
        # delay the tool return nor count against the tool timeout budget.
        hooks.schedule_observe(hooks.HookEvent.POST_TOOL_USE, post_ctx, registry=registry)
        return self._stamp_tool_timing(
            await self._a_augment_auth_failure(final, call, config),
            started_at,
            started_monotonic,
            config,
        )

    @staticmethod
    def _auth_failure_candidate(result) -> bool:
        """Cheap in-memory gate: is this result an auth-shaped tool failure?

        No registry or vault access; safe to call inline on any path. Best
        effort: any failure reads as "not a candidate".
        """
        try:
            if not isinstance(result, ToolMessage) or not isinstance(result.content, str):
                return False
            content = result.content
            if "[Auth check]:" in content:
                return False
            stripped = content.lstrip()
            error_shaped = (
                getattr(result, "status", None) == "error"
                or stripped[:8].lower().startswith("[error]")
                or stripped.startswith("Error")
            )
            return bool(error_shaped and _AUTH_FAILURE_RE.search(content[:4000]))
        except Exception:
            return False

    def _augment_auth_failure(self, result, call, config):
        """Append the structured [Auth check] block to auth-shaped failures.

        Runs AFTER post-tool hooks (hooks match the raw tool result, never the
        synthetic guidance). Fires only when the result is error-shaped, the
        text matches the auth-failure patterns, and the tool maps to a provider
        in the credential-spec registry; everything else passes through
        untouched. Best-effort: any failure returns the original result.

        The vault status lookup is a synchronous DB read; async callers gate on
        ``_auth_failure_candidate`` and run this in a thread so the event loop
        never blocks (the candidate gate makes that a rare, error-only hop).
        """
        try:
            if not self._auth_failure_candidate(result):
                return result
            tool_name = str(call.get("name") or "") if isinstance(call, dict) else ""
            from ...tools.credential_registry import (
                provider_credential_status,
                spec_for_tool,
            )

            spec = spec_for_tool(tool_name)
            if spec is None:
                return result
            configurable = (
                config.get("configurable") or {} if isinstance(config, dict) else {}
            )
            user_id = str(configurable.get("user_id") or "default")
            status = provider_credential_status(spec, user_id)
            guidance = _auth_failure_guidance(tool_name, spec.provider, status)
            return result.model_copy(update={"content": result.content + guidance})
        except Exception:
            logger.debug("auth-failure augmentation skipped", exc_info=True)
            return result

    async def _a_augment_auth_failure(self, result, call, config):
        """Async wrapper: thread-offload the vault read for candidates only."""
        if not self._auth_failure_candidate(result):
            return result
        return await asyncio.to_thread(self._augment_auth_failure, result, call, config)

    @staticmethod
    def _timing_in_results_enabled(config) -> bool:
        """Read the per-turn tool_timing_in_results flag off the run config.

        Stamped by ``agent_safety.graph_run_config`` from the global setting;
        absent on read-only/no-source callers, which reads as disabled.
        """
        configurable = config.get("configurable") if isinstance(config, dict) else None
        return bool((configurable or {}).get("tool_timing_in_results"))

    def _stamp_tool_timing(self, result, started_at, started_monotonic, config):
        """Attach server-measured timing to a completed tool result.

        Stamps ``additional_kwargs["tool_timing"]`` ({"started_at": ISO-8601
        UTC, "duration_ms": int}) onto each ToolMessage so timing persists
        through checkpointing and surfaces on history reload. When the
        per-turn ``tool_timing_in_results`` flag is on, also appends a
        compact ``[Duration: ...]`` line to the content the model sees.
        Best-effort: any failure returns the original result untouched;
        Command results pass through unchanged.
        """
        try:
            duration_ms = max(0, int((time.monotonic() - started_monotonic) * 1000))
            include_in_content = self._timing_in_results_enabled(config)

            def stamp(msg):
                if not isinstance(msg, ToolMessage):
                    return msg
                additional = dict(msg.additional_kwargs or {})
                additional["tool_timing"] = {
                    "started_at": started_at.isoformat(),
                    "duration_ms": duration_ms,
                }
                update: dict = {"additional_kwargs": additional}
                if include_in_content:
                    suffix = f"[Duration: {format_tool_duration_ms(duration_ms)}]"
                    content = msg.content
                    if isinstance(content, list):
                        update["content"] = content + [{"type": "text", "text": suffix}]
                    else:
                        update["content"] = f"{content}\n\n{suffix}"
                return msg.model_copy(update=update)

            if isinstance(result, list):
                return [stamp(m) for m in result]
            return stamp(result)
        except Exception:
            logger.debug("tool timing stamp skipped", exc_info=True)
            return result

    @staticmethod
    def _hook_registry(config):
        """Resolve the per-turn hook registry stamped into the run config.

        Falls back to the module ``default_registry`` when nothing is stamped
        (production turns with no enabled hooks, and every existing test), so the
        no-hooks path is byte-identical to before.
        """
        from ...core import hooks
        configurable = config.get("configurable") if isinstance(config, dict) else None
        return (configurable or {}).get("hook_registry") or hooks.default_registry

    @staticmethod
    def _state_messages(state) -> list:
        """Best-effort message list from a graph state (list/dict/model shapes)."""
        if isinstance(state, list):
            return state
        if isinstance(state, dict):
            messages = state.get("messages")
            return messages if isinstance(messages, list) else []
        messages = getattr(state, "messages", None)
        return messages if isinstance(messages, list) else []

    @classmethod
    def _fresh_context_tokens(cls, state, configurable: dict):
        """Current context occupancy for a tool-event hook, or None.

        Freshest signal first: the most recent AIMessage's provider-reported
        input tokens from the running state (the same source
        ``should_subturn_compact`` reads, so a long tool loop sees occupancy
        grow mid-turn). Falls back to the turn-entry stamp from
        ``graph_run_config``. Never raises.
        """
        try:
            from ...core.token_usage import extract_last_from_messages
            input_tokens, _ = extract_last_from_messages(cls._state_messages(state))
            if input_tokens:
                return int(input_tokens)
        except Exception:  # noqa: BLE001 - a stats read must never break a tool call
            logger.debug("hook context-token extraction failed", exc_info=True)
        return configurable.get("hook_context_tokens")

    def _build_tool_hook_ctx(
        self, event, call, config, *, result_text=None, tool_status=None, state=None
    ):
        """Build a PRE/POST tool HookContext from the call + run config.

        thread_id/user_id come from the run config's ``configurable``; turn-source
        fields (``hook_is_autonomous``/``hook_holder_kind``/``hook_trigger_label``)
        are stamped there by ``agent_safety.graph_run_config`` for graph-run turns
        and default when absent (e.g. a read-only state fetch), so a tool hook can
        scope by autonomous-vs-interactive / holder / trigger. The context-usage
        fields combine the turn-stable stamps (window, compact trigger) with the
        freshest occupancy from ``state`` (see ``_fresh_context_tokens``).
        """
        from ...core import hooks

        configurable = {}
        if isinstance(config, dict):
            configurable = config.get("configurable") or {}
        args = call.get("args") if isinstance(call, dict) else None
        return hooks.HookContext(
            event=event,
            thread_id=str(configurable.get("thread_id") or ""),
            user_id=str(configurable.get("user_id") or ""),
            is_autonomous=bool(configurable.get("hook_is_autonomous", False)),
            holder_kind=configurable.get("hook_holder_kind"),
            trigger_label=configurable.get("hook_trigger_label"),
            tool_name=call.get("name") if isinstance(call, dict) else None,
            tool_call_id=call.get("id") if isinstance(call, dict) else None,
            tool_args=args if isinstance(args, dict) else None,
            tool_result_text=result_text,
            tool_status=tool_status,
            context_tokens=self._fresh_context_tokens(state, configurable),
            context_limit=configurable.get("hook_context_limit"),
            compact_trigger_tokens=configurable.get("hook_compact_trigger_tokens"),
        )

    @staticmethod
    def _apply_pre_tool_outcome(
        outcome, call: ToolCall
    ) -> tuple[ToolCall, Optional[ToolMessage]]:
        """Apply a reduced PRE outcome. Returns (call, denied_message_or_None)."""
        decision = getattr(outcome, "decision", "allow") if outcome else "allow"
        if decision == "deny":
            reason = getattr(outcome, "reason", None) or "blocked by a lifecycle hook"
            msg = ToolMessage(
                content=f"blocked: {reason}",
                name=str(call.get("name") or ""),
                tool_call_id=str(call.get("id") or ""),
                status="error",
            )
            return call, msg
        if decision == "modify" and getattr(outcome, "updated_args", None):
            new_args = {**(call.get("args") or {}), **outcome.updated_args}
            return cast(ToolCall, {**call, "args": new_args}), None
        return call, None

    @staticmethod
    def _apply_post_tool_outcome(result, outcome):
        """Apply a reduced POST outcome to a ToolMessage result (or list thereof).

        Command results are passed through unchanged. For multimodal (list)
        content, a text rewrite replaces the text blocks while preserving
        non-text (image/file) blocks; a note is appended as a new text block.
        A fresh ToolMessage is returned via ``model_copy`` rather than mutating
        the original in place (matching ``_truncate_tool_message``).
        """
        if outcome is None:
            return result
        updated = getattr(outcome, "updated_result_text", None)
        extra = getattr(outcome, "additional_context", None)
        if updated is None and not extra:
            return result

        def _is_text_block(block) -> bool:
            return isinstance(block, str) or (
                isinstance(block, dict) and block.get("type") == "text"
            )

        def rewrite(msg):
            if not isinstance(msg, ToolMessage):
                return msg
            content = msg.content
            if updated is not None:
                if isinstance(content, list):
                    # Replace text blocks with the rewrite; keep image/file blocks.
                    non_text = [b for b in content if not _is_text_block(b)]
                    content = non_text + [{"type": "text", "text": updated}]
                else:
                    content = updated
            if extra:
                if isinstance(content, list):
                    content = content + [{"type": "text", "text": extra}]
                else:
                    content = f"{content}\n\n{extra}"
            return msg.model_copy(update={"content": content})

        if isinstance(result, list):
            return [rewrite(m) for m in result]
        return rewrite(result)

    @staticmethod
    def _tool_result_text(result):
        """Plain-text content of a ToolMessage result, else None."""
        if isinstance(result, ToolMessage) and isinstance(result.content, str):
            return result.content
        return None

    @staticmethod
    def _tool_result_status(result):
        """"success"/"error" status of a ToolMessage result, else None."""
        if isinstance(result, ToolMessage):
            return getattr(result, "status", None)
        return None

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
        """Refresh the dispatch table AND the effective-name set per batch.

        The resolver returns the thread's EFFECTIVE (gated, visible) tool set.
        Its names are stashed so ``_unbound_call_message`` can tell a bound call
        from an unbound one, and any effective tool missing from the superset
        dispatch table (a post-build newly enabled or created tool) is
        registered so it can dispatch. A resolver failure fails OPEN (clears the
        effective set) so a transient error never wrongly rejects a real call.
        """
        if self._dynamic_tool_resolver is None:
            self._effective_tool_names = None
            return
        try:
            resolved_tools, _cache_key = self._dynamic_tool_resolver()
        except Exception:  # noqa: BLE001
            logger.warning(
                "Dynamic tool resolver failed while refreshing tools",
                exc_info=True,
            )
            self._effective_tool_names = None
            return
        resolved = resolved_tools or []
        self._effective_tool_names = {
            t.name for t in resolved if getattr(t, "name", None)
        }
        missing = {
            str(call.get("name") or "")
            for call in tool_calls
            if isinstance(call.get("name"), str)
            and call.get("name") not in self.tools_by_name
        }
        missing.discard("")
        if not missing:
            return
        for tool in resolved:
            if getattr(tool, "name", None) in missing:
                self._register_dynamic_tool(tool)

    def _unbound_call_message(self, call: ToolCall, config) -> Optional[ToolMessage]:
        """Return an error result rejecting a call for a tool not bound here.

        Enforcement is dynamic-mode only (an effective-name set exists). A name
        in the effective set dispatches normally; a name absent from the
        superset too is left to the parent's standard invalid-tool error; only a
        real-but-unbound tool is gated. Returns None to allow the call.
        """
        names = self._effective_tool_names
        if names is None:
            return None
        name = call.get("name")
        if not isinstance(name, str) or not name or name in names:
            return None
        if name not in self.tools_by_name:
            # Unknown even to the dispatch superset: the parent ToolNode emits
            # the canonical "not a valid tool" error, so do not shadow it here.
            return None
        return self._gate_unbound_call(name, call, config)

    def _gate_unbound_call(self, name: str, call: ToolCall, config) -> Optional[ToolMessage]:
        """Apply the unbound-call policy for a real-but-unbound tool.

        Strict (default): refuse with a redirect to tool_invoke (one-off) or
        tool_manage (bind). Permissive (``allow_unbound_tool_calls``): apply the
        SAME deferred gates as tool_invoke and dispatch on pass, refuse on gate
        failure. Returns None only when a permissive call passes the gates.
        """
        configurable = (config or {}).get("configurable") or {}
        tool_call_id = call.get("id", "unknown")
        if not bool(configurable.get("allow_unbound_tool_calls")):
            return ToolMessage(
                content=(
                    f"[Error]: {name!r} is not enabled on this thread, so it "
                    "cannot be called directly. To run it once without binding "
                    f'it, use tool_invoke(name="{name}", arguments={{...}}) '
                    "(cache-safe). To use it repeatedly, bind it first with "
                    f'tool_manage(action="enable", tools=["{name}"], ttl="2h").'
                ),
                name=name,
                tool_call_id=tool_call_id,
                status="error",
            )
        # Permissive: the direct path becomes a policy-equivalent twin of
        # tool_invoke. Reuse its single deferred gate so the two never diverge.
        try:
            from ...tools.tool_invoke import deferred_gate_reason
            from ...tools.utils import caller_role, current_agent

            agent = current_agent()
            user_id = str(configurable.get("user_id") or "")
            thread_id = str(configurable.get("thread_id") or "")
            role = caller_role(user_id, agent=agent)
            reason = deferred_gate_reason(agent, name, user_id, thread_id, role)
        except Exception:  # noqa: BLE001 - fail closed on a gate-check error
            logger.warning("Unbound-call gate check failed for %s", name, exc_info=True)
            reason = "the deferred gate check failed"
        if reason:
            return ToolMessage(
                content=f"[Error]: {name!r} cannot be called: {reason}",
                name=name,
                tool_call_id=tool_call_id,
                status="error",
            )
        return None

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
        if self._should_run_sequentially(input, config):
            # Ordered batches run one call after another, so their wall-clock is
            # the SUM of the calls and would blow past the single whole-batch
            # timeout below (which, on fire, marks every call timed out and
            # discards completed results). Bypass it: each call is bounded by its
            # own per-call timeout inside the ordered loop in _func.
            result = super().invoke(input, config, **kwargs)
            return _truncate_tool_messages_in_result(result, self._tool_output_max_chars)
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
        if self._should_run_sequentially(input, config):
            # See invoke(): bypass the whole-batch timeout for ordered batches and
            # rely on the per-call timeouts inside the ordered loop in _afunc.
            result = await super().ainvoke(input, config, **kwargs)
            return _truncate_tool_messages_in_result(result, self._tool_output_max_chars)
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
                        f"Do NOT retry this tool. Report the timeout to the user."
                    ),
                    tool_call_id=tool_call_id,
                ))
        else:
            logger.error(
                f"Tool execution timed out after {self._tool_timeout}s "
                f"but could not extract tool calls from input to build error response."
            )

        return {"messages": error_messages}

    # ------------------------------------------------------------------
    # Ordered (sequential) same-turn tool execution.
    #
    # By default a batch of tool calls in one assistant turn runs concurrently
    # (parent ``_func``/``_afunc`` fan out via ``executor.map`` /
    # ``asyncio.gather``). When the batch includes the inert
    # ``run_tools_in_order`` marker, we instead run the calls one at a time, in
    # the order the model listed them. This orders EXTERNAL side effects and the
    # result-message order; it does NOT make an earlier call's in-graph state
    # update visible to a later call in the same batch (state is snapshotted per
    # call at node entry, like the concurrent path).
    # ------------------------------------------------------------------

    def _extract_raw_tool_calls(self, input) -> Optional[List[ToolCall]]:
        """Raw tool calls for the predicate, mirroring ``_parse_input`` shapes.

        Returns ``None`` (meaning: keep the concurrent hot path) for any shape
        where same-turn batch ordering is meaningless or unsupported: Send-API
        single-call dispatch (``tool_call_with_context`` dict or a trailing
        ``tool_call`` list entry) and unknown shapes. No normalization is done,
        so the concurrent path is never altered by this scan.
        """
        try:
            if isinstance(input, list):
                if input and isinstance(input[-1], dict) and input[-1].get("type") == "tool_call":
                    return None  # Send / tool_calls shape: single call, ordering N/A
                messages = input
            elif isinstance(input, dict) and input.get("__type") == "tool_call_with_context":
                return None  # Send API single-call payload
            elif isinstance(input, dict):
                messages = input.get(self._messages_key) or []
            else:
                messages = getattr(input, self._messages_key, []) or []
            for msg in reversed(messages):
                if isinstance(msg, AIMessage):
                    return list(msg.tool_calls or [])
            return None
        except Exception:  # noqa: BLE001 - predicate must never crash dispatch.
            logger.debug("Sequential predicate could not extract tool calls", exc_info=True)
            return None

    def _should_run_sequentially(self, input, config=None) -> bool:
        """True when the current batch must run in emitted order.

        Pure and stateless (the node is shared across concurrent invocations):
        the decision is derived only from the batch contents plus the per-turn
        ``config``. Precedence is ``control_tool OR (thread OR global) setting``:
        sequential when the batch contains the ``run_tools_in_order`` marker
        (Tier 1), OR when the deterministic ``sequential_tools`` flag was resolved
        into ``config["configurable"]`` for this turn (Tier 2, #67). The flag is
        resolved once per turn (thread override else global default) in
        ``agent_safety.graph_run_config`` so both the outer timeout-bypass
        (``invoke``/``ainvoke``) and the inner dispatch (``_func``/``_afunc``) read
        the same value and cannot disagree.
        """
        calls = self._extract_raw_tool_calls(input)
        if not calls:
            return False
        names = [str(c.get("name") or "") for c in calls]
        if SEQUENTIAL_ORDER_TOOL_NAME in names:
            return True
        # Tier 2 (#67): the deterministic per-thread/global setting, pre-resolved
        # into config by graph_run_config. Robust to config being None or an
        # object (RunnableConfig is a dict in practice; guard both shapes).
        if isinstance(config, dict):
            configurable = config.get("configurable") or {}
        else:
            configurable = getattr(config, "configurable", {}) or {}
        if configurable.get("sequential_tools"):
            return True
        return False

    def _func(self, input, config, runtime):
        # Keep ``(self, input, config, runtime)`` identical to the parent and to
        # ``_afunc``: RunnableCallable derives its config/runtime injection map
        # once from the sync ``_func`` signature and reuses it for both paths.
        if not self._should_run_sequentially(input, config):
            return super()._func(input, config, runtime)
        return self._run_sequential(input, config, runtime)

    async def _afunc(self, input, config, runtime):
        if not self._should_run_sequentially(input, config):
            return await super()._afunc(input, config, runtime)
        return await self._arun_sequential(input, config, runtime)

    def _build_tool_runtime(self, call, cfg, runtime, input):
        """Construct the per-call ToolRuntime exactly as the parent does.

        Mirrors the 9-field block in ``ToolNode._func``/``_afunc`` (verified
        identical in langgraph-prebuilt 1.0.13 and the pinned 1.1.0; 1.1.0 only
        added a method, no new field). The signature-parity guard test asserts
        this field set against the installed langgraph so an upgrade fails loudly.
        """
        # _extract_state takes (input, config) on both pinned langgraph-prebuilt
        # versions (1.0.13 and 1.1.0, verified); pyrefly mis-resolves the arity.
        state = self._extract_state(input, cfg)  # pyrefly: ignore[bad-argument-count]
        return ToolRuntime(
            state=state,
            tool_call_id=call["id"],
            config=cfg,
            context=runtime.context,
            store=runtime.store,
            stream_writer=runtime.stream_writer,
            tools=list(self.tools_by_name.values()),
            execution_info=runtime.execution_info,
            server_info=runtime.server_info,
        )

    def _run_sequential(self, input, config, runtime):
        # Sync ordered loop (parity with the async path; the API runtime drives
        # the async path, this exists for sync callers and tests).
        tool_calls, input_type = self._parse_input(input)
        config_list = get_config_list(config, len(tool_calls))
        abort_event = self._abort_event_for(config)
        stop_on_error = self._stop_on_error_flag(tool_calls)
        logger.info(
            "Sequential tool execution: %d call(s) in emitted order (trigger=%s, stop_on_error=%s)",
            len(tool_calls),
            self._sequential_trigger(tool_calls),
            stop_on_error,
        )
        outputs: list = []
        stop_triggered = False
        for call, cfg in zip(tool_calls, config_list, strict=False):
            name = str(call.get("name") or "")
            if name == SEQUENTIAL_ORDER_TOOL_NAME:
                ack = self._marker_ack_message(call, tool_calls)
                outputs.append(ack)
                self._emit_marker_frames(call, ack, config)
                continue
            if abort_event is not None and abort_event.is_set():
                outputs.append(self._cancelled_message(call))
                continue
            if stop_triggered:
                outputs.append(self._skipped_message(call))
                continue
            tool_runtime = self._build_tool_runtime(call, cfg, runtime, input)
            result = self._run_one_with_timeout(call, input_type, tool_runtime, config)
            outputs.append(result)
            if stop_on_error and self._is_error_output(result):
                stop_triggered = True
        return self._combine_tool_outputs(outputs, input_type)

    async def _arun_sequential(self, input, config, runtime):
        tool_calls, input_type = self._parse_input(input)
        config_list = get_config_list(config, len(tool_calls))
        abort_event = self._abort_event_for(config)
        stop_on_error = self._stop_on_error_flag(tool_calls)
        logger.info(
            "Sequential tool execution: %d call(s) in emitted order (trigger=%s, stop_on_error=%s)",
            len(tool_calls),
            self._sequential_trigger(tool_calls),
            stop_on_error,
        )
        outputs: list = []
        stop_triggered = False
        for call, cfg in zip(tool_calls, config_list, strict=False):
            name = str(call.get("name") or "")
            if name == SEQUENTIAL_ORDER_TOOL_NAME:
                ack = self._marker_ack_message(call, tool_calls)
                outputs.append(ack)
                # The marker is never dispatched, so on_tool_start/on_tool_end
                # never fire and the live stream would otherwise never see it
                # (it shows only on reload). Emit synthetic frames so the card
                # appears live too.
                await self._aemit_marker_frames(call, ack, config)
                continue
            # Per-step abort: a sequential batch's wall-clock is the SUM of its
            # calls, so check the cooperative abort event before each call and
            # synthesize a cancelled result for the remainder (every tool_call
            # still needs a matching ToolMessage).
            if abort_event is not None and abort_event.is_set():
                outputs.append(self._cancelled_message(call))
                continue
            if stop_triggered:
                outputs.append(self._skipped_message(call))
                continue
            tool_runtime = self._build_tool_runtime(call, cfg, runtime, input)
            result = await self._arun_one_with_timeout(call, input_type, tool_runtime, config)
            outputs.append(result)
            if stop_on_error and self._is_error_output(result):
                stop_triggered = True
        return self._combine_tool_outputs(outputs, input_type)

    def _notify_single_call_timeout(self, call, config) -> None:
        """Fire the timeout hook for a single timed-out call in an ordered batch.

        The concurrent path calls ``_notify_timeout`` on a whole-batch timeout so
        ``on_tool_timeout`` can cascade-abort any callable sub-thread that hung.
        The sequential path times out per call, so only the one call that hung
        should be aborted: passing the real input here would also abort sibling
        callable threads (already-finished or not-yet-started) by name. Synthesize
        a one-call input so exactly that call's callable thread is cascaded.
        """
        if not self._on_timeout:
            return
        try:
            tc = {
                "name": call.get("name"),
                "args": call.get("args") if isinstance(call.get("args"), dict) else {},
                "id": call.get("id"),
                "type": "tool_call",
            }
            synthetic = {"messages": [AIMessage(content="", tool_calls=[tc])]}
            self._notify_timeout(synthetic, config)
        except Exception as e:  # a hook failure must never break the ordered loop
            logger.warning(f"on_timeout callback failed: {e}")

    async def _arun_one_with_timeout(self, call, input_type, tool_runtime, config):
        """Run one call with its own timeout (the outer batch timeout is bypassed
        in sequential mode). ``_arun_one`` already converts ordinary tool errors
        to error ToolMessages and re-raises ``GraphBubbleUp``; we must not turn an
        interrupt into an error message, so carve it out explicitly.
        """
        try:
            return await asyncio.wait_for(
                self._arun_one(call, input_type, tool_runtime),
                timeout=self._tool_timeout,
            )
        except GraphBubbleUp:
            raise
        except asyncio.TimeoutError:
            self._notify_single_call_timeout(call, config)
            return self._timeout_message(call)

    def _run_one_with_timeout(self, call, input_type, tool_runtime, config):
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(self._run_one, call, input_type, tool_runtime)
            try:
                return future.result(timeout=self._tool_timeout)
            except concurrent.futures.TimeoutError:
                self._notify_single_call_timeout(call, config)
                return self._timeout_message(call)
            # GraphBubbleUp raised in the worker re-raises from future.result() and
            # is intentionally not caught, so the interrupt propagates (the finally
            # below still releases the pool first).
        finally:
            executor.shutdown(wait=False)

    @staticmethod
    def _stop_on_error_flag(tool_calls) -> bool:
        for call in tool_calls:
            if str(call.get("name") or "") == SEQUENTIAL_ORDER_TOOL_NAME:
                args = call.get("args") if isinstance(call.get("args"), dict) else {}
                return bool(args.get("stop_on_error", False))
        return False

    @staticmethod
    def _sequential_trigger(tool_calls) -> str:
        """Why this batch ran ordered: the model's marker, or the operator setting.

        Both reach the same loop; only the marker leaves a tool_call in the batch,
        so its presence distinguishes the two for log/debug purposes.
        """
        for call in tool_calls:
            if str(call.get("name") or "") == SEQUENTIAL_ORDER_TOOL_NAME:
                return "control_tool"
        return "setting"

    def _marker_ack_message(self, call, tool_calls) -> ToolMessage:
        """Synthesize the inert marker's result from the static batch.

        The marker is never dispatched on this path; its result is computed from
        the emitted ``tool_calls`` so its execution position is irrelevant.
        """
        others = sum(
            1
            for c in tool_calls
            if str(c.get("name") or "") != SEQUENTIAL_ORDER_TOOL_NAME
        )
        if others == 0:
            content = (
                "No other tool calls were in this batch, so nothing was ordered. "
                "Emit the calls you want run in order together with this one in a "
                "single response."
            )
        else:
            content = (
                f"Ordered {others} tool call(s) in this batch to run one at a "
                "time, in the order you listed them."
            )
        return ToolMessage(
            content=content,
            name=str(call.get("name") or SEQUENTIAL_ORDER_TOOL_NAME),
            tool_call_id=call.get("id", "unknown"),
        )

    def _marker_frame_payloads(self, call, ack: ToolMessage) -> list[tuple[str, dict]]:
        """Live SSE frames mirroring the marker's reload card.

        Match the LIVE event shape emitted by GraphStreamProcessor for real
        tools: ``tool_call`` carries ``args`` (no ``status``) and ``tool_result``
        carries ``result``. The marker's own tool_call_id is the frame id (a
        different id-space from the LangChain run_id used for dispatched tools,
        so no collision).
        """
        call_id = call.get("id", "unknown")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        return [
            (
                "tool_call",
                {"id": call_id, "name": SEQUENTIAL_ORDER_TOOL_NAME, "args": args},
            ),
            (
                "tool_result",
                {"id": call_id, "name": SEQUENTIAL_ORDER_TOOL_NAME, "result": ack.content},
            ),
        ]

    def _emit_marker_frames(self, call, ack: ToolMessage, config) -> None:
        """Best-effort sync dispatch of the marker's live frames.

        Mirrors ``_dispatch_provider_event``: swallow a missing callback manager
        (RuntimeError) and any other dispatch failure at debug level so a
        streaming hiccup never breaks tool execution.
        """
        for name, payload in self._marker_frame_payloads(call, ack):
            try:
                dispatch_custom_event(name, payload, config=config)
            except RuntimeError:
                logger.debug("No callback manager for marker %s event", name)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to dispatch marker %s event: %s", name, exc)

    async def _aemit_marker_frames(self, call, ack: ToolMessage, config) -> None:
        """Best-effort async dispatch of the marker's live frames (see sync twin)."""
        for name, payload in self._marker_frame_payloads(call, ack):
            try:
                await adispatch_custom_event(name, payload, config=config)
            except RuntimeError:
                logger.debug("No callback manager for marker %s event", name)
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to dispatch marker %s event: %s", name, exc)

    def _timeout_message(self, call) -> ToolMessage:
        name = call.get("name", "unknown")
        logger.error(
            "Tool '%s' timed out after %ss in a sequential batch.",
            name,
            self._tool_timeout,
        )
        return ToolMessage(
            content=(
                f"[Error]: Tool '{name}' timed out after {self._tool_timeout} seconds. "
                "The operation took too long and was stopped to prevent the agent from hanging. "
                "Do NOT retry this tool. Report the timeout to the user."
            ),
            name=name,
            tool_call_id=call.get("id", "unknown"),
            status="error",
        )

    def _cancelled_message(self, call) -> ToolMessage:
        name = call.get("name", "unknown")
        return ToolMessage(
            content=(
                "[Error]: Tool execution was cancelled before this call ran "
                "(the turn was aborted)."
            ),
            name=name,
            tool_call_id=call.get("id", "unknown"),
            status="error",
        )

    def _skipped_message(self, call) -> ToolMessage:
        name = call.get("name", "unknown")
        return ToolMessage(
            content=(
                "[Error]: Skipped. An earlier call in this ordered batch failed "
                "and stop_on_error was set, so this call was not run."
            ),
            name=name,
            tool_call_id=call.get("id", "unknown"),
            status="error",
        )

    @staticmethod
    def _is_error_output(result) -> bool:
        """A tool output counts as an error if it raised (status=="error") or
        returned a result starting with the codebase's ``[Error]:`` convention
        (most Nymeria tools catch their own failures and RETURN such a string
        rather than raising, so status alone would miss them).
        """
        def _one(msg) -> bool:
            if isinstance(msg, ToolMessage):
                if getattr(msg, "status", None) == "error":
                    return True
                content = msg.content
                return isinstance(content, str) and content.lstrip().startswith("[Error]")
            return False

        if isinstance(result, list):
            return any(_one(m) for m in result)
        return _one(result)

    def _abort_event_for(self, config):
        """Resolve this thread's cooperative abort event, or None.

        The node has no local abort handle, so pull ``thread_id`` from config and
        reach the event via ``get_current_agent()`` (the current-agent lookup, as
        in ``get_effective_image_window_size``), not a local attribute.
        """
        try:
            if isinstance(config, dict):
                configurable = config.get("configurable") or {}
            else:
                configurable = getattr(config, "configurable", {}) or {}
            thread_id = configurable.get("thread_id")
            if not thread_id:
                return None
            from ...core.agent import get_current_agent

            agent = get_current_agent()
            if agent is None:
                return None
            return agent._thread_locks.get_abort_event(str(thread_id))
        except Exception:  # noqa: BLE001 - abort resolution is best-effort.
            logger.debug("Could not resolve abort event for sequential batch", exc_info=True)
            return None


def create_should_continue(
    max_iterations: int = 10,
    repeated_tool_result_limit: int = 5,
) -> Callable[[AgentState], str]:
    """
    Factory for the routing function with the repeated-tool-loop guard.

    The repeat count is derived from the message history rather than a
    closure, making it thread-safe and stateless. Only the current turn's
    exchanges count, so previous turns don't block future ones.

    The max-iterations cap is deliberately NOT enforced here anymore: it
    moved to ``route_after_tools`` (backlog #27) so a cap halt lands AFTER
    the crossing batch executes, on a clean sub-turn boundary that a bare
    ``{"messages": []}`` re-drive can resume (the compaction-resume shape).
    The cap counts EMITTED tool calls, so the relocated check fires at the
    same count, one node later. ``max_iterations`` stays in the signature
    because it still sizes the graph's recursion limit at build time and
    keeps the factory contract stable.

    The repeated-tool guard stays HERE, before execution: its whole point
    is never running the degenerate call again.

    Args:
        max_iterations: Maximum ReAct loops per turn (enforced post-batch
            in ``route_after_tools``)

    Returns:
        Routing function for conditional edges
    """
    def should_continue(state: AgentState) -> str:
        """
        Conditional edge: decides what node to go to next.

        Returns:
            'tools' if agent wants to use tools
            'end' if agent is done or a repeated tool loop was detected
        """
        messages = state["messages"]
        last_message = messages[-1]

        # Check if LLM wants to call tools
        if isinstance(last_message, AIMessage) and last_message.tool_calls:
            safety = analyze_turn_safety(
                messages,
                max_iterations=0,  # cap enforced post-batch in route_after_tools
                repeated_tool_result_limit=repeated_tool_result_limit,
            )

            if safety.should_stop:
                # Force stop to prevent runaway loops.
                logger.warning(
                    "Repeated tool/result loop detected: tool=%s repeat_count=%s. "
                    "Forcing agent to stop before another identical tool call.",
                    safety.repeated_tool_name,
                    safety.repeated_count,
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

            # Sub-turn auto-compaction: if the running context crossed the
            # auto-compact trigger, halt here so astream()/chat() can compact
            # and re-drive. This is a genuine mid-task boundary -- the agent has
            # a pending LLM call to process these tool results, so it is never
            # "done" here (a final no-tool-call response routes via END instead).
            from ...core.agent import get_current_agent
            agent = get_current_agent()
            if agent is not None and agent.should_halt_for_subturn_compaction(
                thread_id, state["messages"]
            ):
                return "end"

            # Turn-safety iteration cap (graceful halt, backlog #27). The cap
            # counts EMITTED tool calls, so checking here (after the batch
            # executed) fires at the same count as the old pre-execution check
            # in should_continue, one node later: the crossing batch runs and
            # the halt lands on this clean sub-turn boundary (results in
            # history, model call pending), which a bare ``{"messages": []}``
            # re-drive can resume (the /resume path). The queue check above
            # deliberately wins: a queued prompt absorbing at this boundary
            # injects a fresh HumanMessage and resets the window, which is the
            # implicit resume-with-a-message behavior this preserves.
            #
            # The cap comes from the ``turn_safety_max_iterations`` stamp that
            # ``graph_run_config`` writes (falling back to the agent's
            # per-thread resolution for configs built elsewhere); with neither
            # available the graph's recursion_limit stays the backstop.
            # ``turn_safety_tool_call_offset`` anchors a resumed turn's window
            # at its resume point.
            cap = configurable.get("turn_safety_max_iterations")
            if cap is None and agent is not None:
                try:
                    cap = agent._max_iterations_for_thread(thread_id)
                except Exception:  # noqa: BLE001 - cap resolution is best-effort
                    cap = None
            if cap:
                safety = analyze_turn_safety(
                    state["messages"],
                    max_iterations=int(cap),
                    repeated_tool_result_limit=0,
                    tool_call_offset=int(
                        configurable.get("turn_safety_tool_call_offset") or 0
                    ),
                )
                if safety.should_stop:
                    logger.warning(
                        "Iteration limit reached (%s/%s). Halting at the "
                        "sub-turn boundary after the crossing batch executed.",
                        safety.tool_call_count,
                        safety.max_iterations,
                    )
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
