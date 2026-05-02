"""
Graph Nodes for the ReAct Agent

Modular node creation that accepts configuration for easy framework integration.
"""

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, List, Callable, Optional
from urllib.parse import urlparse
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import message_chunk_to_message
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool
from langchain_core.language_models import BaseChatModel
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from .state import AgentState
from .config import AgentConfig, LLMConfig, default_config
from .providers import create_llm_with_tools
from ...core.tool_reload import latest_tool_batch_queued_reload

logger = logging.getLogger(__name__)

TURN_SAFETY_REASON_MAX_ITERATIONS = "max_iterations"
TURN_SAFETY_REASON_REPEATED_TOOL_RESULT = "repeated_tool_result"

_CLIPROXY_PORTS = {8317, 8318}
_CLIPROXY_BILLING_SYSTEM_BLOCK = {
    "type": "text",
    "text": "x-anthropic-billing-header: cc_version=2.1.63.8f3; cc_entrypoint=cli; cch=54031;",
}
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
_NON_RETRYABLE_ERROR_MARKERS = (
    "context_length_exceeded",
    "context window",
    "maximum context length",
    "max context length",
    "too many tokens",
    "input is too long",
    "prompt is too long",
    "reduce the length",
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
            parts.append(_safe_error_text(getattr(response, "text", None)))
            parts.append(_safe_error_text(getattr(response, "content", None)))
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


def _llm_retry_delay(llm_config: Optional[LLMConfig], retry_index: int) -> float:
    initial = max(0.0, float(getattr(llm_config, "stream_retry_initial_delay", 1.0) or 0.0))
    maximum = max(0.0, float(getattr(llm_config, "stream_retry_max_delay", 8.0) or 0.0))
    if initial == 0.0 or maximum == 0.0:
        return 0.0
    return min(maximum, initial * (2 ** max(0, retry_index - 1)))


def _llm_max_retries(llm_config: Optional[LLMConfig]) -> int:
    return max(0, int(getattr(llm_config, "stream_max_retries", 2) or 0))


def _invoke_llm_with_retries(
    invoke: Callable[[], AIMessage],
    llm_config: Optional[LLMConfig],
) -> AIMessage:
    max_retries = _llm_max_retries(llm_config)

    for attempt in range(max_retries + 1):
        try:
            return invoke()
        except Exception as exc:
            if attempt >= max_retries or not _is_retryable_llm_error(exc):
                raise

            retry_index = attempt + 1
            delay = _llm_retry_delay(llm_config, retry_index)
            logger.warning(
                "[LLM RETRY] transient sync call failure; retry %d/%d in %.2fs: %s",
                retry_index,
                max_retries,
                delay,
                exc,
            )
            if delay > 0:
                time.sleep(delay)

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

    parse_target = llm_config.base_url.strip()
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

    return "cli-proxy" in host or "cliproxy" in host or port in _CLIPROXY_PORTS


def _format_system_prompt(
    system_prompt: str,
    llm_config: Optional[LLMConfig],
) -> str | list[dict[str, Any]]:
    """Add CLIProxy's lightweight OAuth fingerprint without replacing Nymeria."""
    if not _uses_cliproxy_anthropic(llm_config):
        return system_prompt

    if isinstance(system_prompt, list):
        blocks = list(system_prompt)
    else:
        blocks = [{"type": "text", "text": system_prompt}]

    has_billing_block = any(
        isinstance(block, dict)
        and str(block.get("text", "")).startswith("x-anthropic-billing-header:")
        for block in blocks
    )
    if has_billing_block:
        return blocks

    return [dict(_CLIPROXY_BILLING_SYSTEM_BLOCK), *blocks]


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


def create_agent_node(
    llm_with_tools: BaseChatModel,
    system_prompt: str,
    llm_config: Optional[LLMConfig] = None,
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
        messages = _sanitize_messages_for_anthropic(state["messages"], llm_config)

        # Summary line at INFO (always visible)
        tool_rounds = sum(1 for m in messages if isinstance(m, AIMessage) and m.tool_calls)
        logger.info(f"[LLM] Invoking with {len(messages)} messages ({tool_rounds} tool rounds)")

        # Detailed message dump at DEBUG (visible with llm profile)
        if logger.isEnabledFor(logging.DEBUG):
            for i, msg in enumerate(messages):
                msg_type = type(msg).__name__
                content_preview = ""
                if hasattr(msg, 'content') and msg.content:
                    content_str = msg.content if isinstance(msg.content, str) else str(msg.content)
                    content_preview = content_str[:150].replace('\n', ' ')
                tool_info = ""
                if hasattr(msg, 'tool_calls') and msg.tool_calls:
                    tool_names = [tc.get('name', '?') for tc in msg.tool_calls]
                    tool_info = f" [tools: {', '.join(tool_names)}]"
                logger.debug(f"[LLM]   [{i}] {msg_type}{tool_info}: {content_preview}...")

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

    def agent_node(state: AgentState) -> dict:
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
            lambda: llm_with_tools.invoke(messages_with_system),
            llm_config,
        )
        return _finish_response(response)

    async def async_agent_node(state: AgentState) -> dict:
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
        attempt = 0
        while True:
            chunks_this_attempt = 0
            try:
                async for chunk in llm_with_tools.astream(messages_with_system):
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
                    response = await llm_with_tools.ainvoke(messages_with_system)
                else:
                    response = message_chunk_to_message(merged_chunk)
                break
            except Exception as exc:
                if chunks_this_attempt > 0:
                    logger.warning(
                        "[LLM RETRY] stream failed after %d chunk(s); not retrying to avoid duplicated output: %s",
                        chunks_this_attempt,
                        exc,
                    )
                    raise
                if attempt >= max_retries or not _is_retryable_llm_error(exc):
                    raise

                attempt += 1
                delay = _llm_retry_delay(llm_config, attempt)
                logger.warning(
                    "[LLM RETRY] transient stream failure before chunks; retry %d/%d in %.2fs: %s",
                    attempt,
                    max_retries,
                    delay,
                    exc,
                )
                if delay > 0:
                    await asyncio.sleep(delay)

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


def create_tools_node(
    tools: List[BaseTool],
    handle_errors: bool = True,
    tool_timeout: Optional[int] = None,
    on_timeout: Optional[Callable] = None,
    tool_output_max_chars: int = 100000,
) -> "SafeToolNode":
    """
    Create a tools node that executes tool calls.

    Args:
        tools: List of tools the node can execute
        handle_errors: If True, catch tool exceptions and return error messages
                      instead of letting them bubble up
        tool_timeout: Seconds before a tool invocation is terminated (default 300)
        on_timeout: Optional callback invoked with the input dict when a timeout occurs
        tool_output_max_chars: Maximum stored characters per ToolMessage result

    Returns:
        SafeToolNode instance that handles errors and timeouts gracefully
    """
    return SafeToolNode(
        tools,
        handle_tool_errors=handle_errors,
        tool_timeout=tool_timeout,
        on_timeout=on_timeout,
        tool_output_max_chars=tool_output_max_chars,
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
    ):
        super().__init__(tools, handle_tool_errors=handle_tool_errors)
        self._handle_errors = handle_tool_errors
        self._tool_timeout = tool_timeout if tool_timeout is not None else self.DEFAULT_TOOL_TIMEOUT
        self._on_timeout = on_timeout  # Optional callback: fn(input_dict) -> None
        self._tool_output_max_chars = tool_output_max_chars

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
            if self._on_timeout:
                try:
                    self._on_timeout(input)
                except Exception as e:
                    logger.warning(f"on_timeout callback failed: {e}")
            return _truncate_tool_messages_in_result(
                self._build_timeout_response(input),
                self._tool_output_max_chars,
            )

    async def ainvoke(self, input, config=None, **kwargs):
        """Async tool execution with timeout."""
        try:
            result = await asyncio.wait_for(
                super().ainvoke(input, config, **kwargs),
                timeout=self._tool_timeout,
            )
            return _truncate_tool_messages_in_result(result, self._tool_output_max_chars)
        except asyncio.TimeoutError:
            if self._on_timeout:
                try:
                    self._on_timeout(input)
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


def create_should_continue_old(max_iterations: int = 10) -> Callable[[AgentState], str]:
    """
    Deprecated compatibility shim for old imports.
    """
    return create_should_continue(max_iterations=max_iterations)


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


def route_after_tools(state: AgentState) -> str:
    """Route after tool execution, stopping immediately for queued reloads."""
    if latest_tool_batch_queued_reload(state["messages"]):
        return "end"
    return "agent"


class NodeFactory:
    """
    Factory class for creating all nodes from a single configuration.

    Usage:
        factory = NodeFactory(config, tools)
        agent = factory.create_agent_node()
        tools_node = factory.create_tools_node()
        router = factory.create_router()
    """

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        tools: Optional[List[BaseTool]] = None,
    ):
        self.config = config or default_config
        self.tools = tools or []
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
        """Create the agent reasoning node."""
        return create_agent_node(
            self.llm_with_tools,
            self.config.system_prompt,
            self.config.llm,
        )

    def create_tools_node(self) -> ToolNode:
        """Create the tool execution node."""
        return create_tools_node(
            self.tools,
            tool_timeout=self.config.tool_timeout,
            on_timeout=self.config.on_timeout,
            tool_output_max_chars=self.config.tool_output_max_chars,
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


# === BACKWARD COMPATIBILITY ===
# These maintain compatibility with the old interface
# Lazy-initialized to avoid crashing on import when env vars aren't set for the
# vendored defaults (Nymeria creates its own config via agent.py).

from .tools import TOOLS
from dotenv import load_dotenv

load_dotenv()

should_continue = simple_should_continue  # Use simple version for backward compat

_default_factory = None
agent_node = None
tools_node = None


def _init_defaults():
    """Lazily initialize default nodes on first use."""
    global _default_factory, agent_node, tools_node
    if _default_factory is None:
        _default_factory = NodeFactory(default_config, TOOLS)
        agent_node = _default_factory.create_agent_node()
        tools_node = _default_factory.create_tools_node()
