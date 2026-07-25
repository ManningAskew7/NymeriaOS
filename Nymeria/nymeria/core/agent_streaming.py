"""Streaming helpers for NymeriaAgent.astream()."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    List,
    Optional,
)

from ..vendor.react_agent.nodes import (
    is_retryable_llm_error,
    llm_activate_next_fallback,
    llm_consult_transport_fallback,
    llm_fallback_hold_overrides,
    llm_max_retries,
    llm_retry_delay,
    llm_retry_payload_for_active_candidate,
)
from .agent_compaction import COMPACTING_MESSAGE
from .agent_text_extract import (
    InlineThinkingTextStripper,
    extract_reasoning_text_from_block,
    strip_inline_thinking_text,
)
from .mcp_tool_names import parse_mcp_tool_name
from .pending_prompt_queue import PendingPrompt


logger = logging.getLogger(__name__)


async def drive_with_fanout(
    stream_processor: "GraphStreamProcessor",
    graph_obj: Any,
    input_state: dict[str, Any],
    pending_prompts: List[PendingPrompt],
) -> AsyncGenerator[dict[str, Any], None]:
    """Drive one graph invocation, mirroring each event into queuer mailboxes.

    Each pending prompt with an attached ``fanout_mailbox`` receives
    every event the holder yields, so the queuer's SSE consumer sees
    the live response in real time -- even though the holder and
    queuer run on different event loops. ``FanoutMailbox.put`` is
    explicitly cross-loop-safe (uses ``call_soon_threadsafe``).
    """
    async for evt in stream_processor.drive(graph_obj, input_state):
        for prompt in pending_prompts:
            if prompt.fanout_mailbox is not None:
                prompt.fanout_mailbox.put(evt)
        yield evt


# A factory that, given the helper-internal ``on_started`` callback, returns the
# compaction coroutine to run (e.g. ``check_and_compact``/``rewind_and_compact``).
CompactionCoroFactory = Callable[
    [Callable[[], Awaitable[None]]],
    Coroutine[Any, Any, Optional[dict[str, Any]]],
]


async def compact_with_progress(
    coro_factory: CompactionCoroFactory,
    result_sink: list[Optional[dict[str, Any]]],
) -> AsyncGenerator[dict[str, Any], None]:
    """Run a compaction coroutine, emitting a single ``compacting`` event.

    Races the compaction coroutine against an internal ``started`` signal so the
    ``compacting`` progress event can be yielded *mid-flight* (during the
    potentially multi-second summarization) without blocking, and is emitted
    **exactly once** (the double-guard covers the case where the coroutine
    signals start and completes within the same ``asyncio.wait`` batch). The
    coroutine's return value is appended to ``result_sink`` (a one-element sink)
    so the caller can build its own ``compacted`` event; an async generator
    cannot ``return`` a value. ``coro_factory`` receives the internal
    ``on_started`` callback and must return the compaction coroutine.

    The ``finally`` only cancels the lightweight start-waiter task
    **synchronously**, so this is safe on the SSE-disconnect (``GeneratorExit``)
    path where awaiting is forbidden. ``compact_task`` is intentionally not
    cancelled here (it never was inline either): on disconnect it is orphaned to
    run to completion on the loop, matching the prior behavior.
    """
    compact_started = asyncio.Event()

    async def _on_compaction_started() -> None:
        compact_started.set()

    compact_task = asyncio.create_task(coro_factory(_on_compaction_started))
    start_task = asyncio.create_task(compact_started.wait())
    sent_compacting = False
    try:
        await asyncio.wait(
            {compact_task, start_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if compact_started.is_set():
            yield {"type": "compacting", "message": COMPACTING_MESSAGE}
            sent_compacting = True
        result_sink.append(await compact_task)
        if compact_started.is_set() and not sent_compacting:
            yield {"type": "compacting", "message": COMPACTING_MESSAGE}
    finally:
        if not start_task.done():
            start_task.cancel()


TOOL_CALL_CONTENT_DELTA_TYPES = frozenset({
    "function_call",
    "tool_call",
    "tool_call_chunk",
    "tool_use",
    "input_json_delta",
})


def has_tool_call_delta(tool_call_chunks: Any) -> bool:
    """Return True when LangChain exposed provider tool-call chunks."""
    return bool(tool_call_chunks)


def has_tool_call_content_delta(content: Any) -> bool:
    """Return True when streamed content blocks represent tool-call deltas."""
    if not isinstance(content, list):
        return False
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in TOOL_CALL_CONTENT_DELTA_TYPES:
            return True
    return False


class ReasoningChunkDeduper:
    """Deduplicate reasoning chunks in the model-end fallback path.

    Used ONLY by _fallback_content_events, which re-emits reasoning from a
    model's final output when nothing streamed live. It must never run on the
    live per-delta paths: reasoning token streams legitimately repeat short
    strings (spaces, newlines, common words), and exact-string dedupe there
    drops the repeats and fuses adjacent words.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def should_emit(self, text: Any) -> bool:
        if not isinstance(text, str) or not text:
            return False
        if text in self._seen:
            return False
        self._seen.add(text)
        return True

    def reset(self) -> None:
        self._seen.clear()


class GraphStreamProcessor:
    """Convert LangGraph astream_events output into Nymeria SSE chunks."""

    def __init__(
        self,
        *,
        thread_id: str,
        config: dict[str, Any],
        abort_event: Any,
        is_self_invoke: bool,
        response_parts: list[str],
        clean_tool_result: Callable[[str], str],
        tool_result_extra_events: Callable[..., Iterable[dict[str, Any]]],
        stream_logger: Optional[logging.Logger] = None,
        llm_config: Any = None,
        tool_timeout: Optional[int] = None,
    ) -> None:
        self.thread_id = thread_id
        self.config = config
        self.abort_event = abort_event
        self.is_self_invoke = is_self_invoke
        self.response_parts = response_parts
        self.clean_tool_result = clean_tool_result
        self.tool_result_extra_events = tool_result_extra_events
        self.logger = stream_logger or logger
        self.llm_config = llm_config
        # Per-tool timeout budget (seconds) surfaced on tool_call events so
        # clients can render elapsed/max. Same value SafeToolNode enforces.
        self.tool_timeout = tool_timeout

        self._reset_graph_state()

    async def drive(
        self,
        graph_obj: Any,
        input_state: dict[str, Any],
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Drive one graph invocation and yield converted SSE events."""
        self._reset_graph_state()
        attempt_input_state = input_state
        recovery_attempt = 0
        max_retries = llm_max_retries(self.llm_config)

        while True:
            try:
                async for event in graph_obj.astream_events(
                    attempt_input_state,
                    config=self.config,
                    version="v2",
                ):
                    if self.abort_event.is_set():
                        self.logger.info(
                            "[ASTREAM] Thread %s: Aborted by cancel signal",
                            self.thread_id,
                        )
                        yield {
                            "type": "error",
                            "content": "Operation was cancelled.",
                            "code": "cancelled",
                        }
                        return

                    async for converted in self._handle_event(event):
                        yield converted

                self._log_graph_done()
                return
            except Exception as exc:
                stream_chunks = int(getattr(exc, "nymeria_stream_chunks_before_error", 0) or 0)
                if stream_chunks <= 0 or not is_retryable_llm_error(exc):
                    raise

                if recovery_attempt < max_retries:
                    recovery_attempt += 1
                    delay = llm_retry_delay(self.llm_config, recovery_attempt)
                    self._rollback_current_model_output()
                    payload = llm_retry_payload_for_active_candidate(
                        self.llm_config,
                        attempt=recovery_attempt,
                        max_retries=max_retries,
                        delay=delay,
                        exc=exc,
                    )
                    payload["rewound"] = True
                    payload["stream_chunks"] = stream_chunks
                    self.logger.warning(
                        "[LLM RECOVERY] thread=%s stream failed after %d chunk(s); "
                        "rewound to latest checkpoint and retrying graph %d/%d in %.2fs: %s",
                        self.thread_id,
                        stream_chunks,
                        recovery_attempt,
                        max_retries,
                        delay,
                        exc,
                    )
                    yield {**payload, "type": "provider_retry"}
                    if delay > 0:
                        await asyncio.sleep(delay)
                else:
                    # Consent gate (user-consented fallback switching): ask
                    # before switching the thread's model. "fail" re-raises
                    # the original error (the primary already exhausted its
                    # retries); "swap" carries the user-chosen hold into the
                    # activation payload; "auto" keeps today's silent switch.
                    consent = await llm_consult_transport_fallback(
                        self.llm_config,
                        exc=exc,
                        is_autonomous=self._turn_is_autonomous(),
                        holder_kind=self._turn_holder_kind(),
                    )
                    if consent.get("action") == "fail":
                        self.logger.warning(
                            "[LLM RECOVERY] thread=%s fallback declined by the "
                            "user; failing the turn with the original error",
                            self.thread_id,
                        )
                        raise
                    payload = llm_activate_next_fallback(
                        self.llm_config,
                        exc=exc,
                        hold_overrides=llm_fallback_hold_overrides(consent),
                    )
                    if payload is None:
                        raise
                    self._rollback_current_model_output()
                    payload["rewound"] = True
                    payload["stream_chunks"] = stream_chunks
                    self.logger.warning(
                        "[LLM RECOVERY] thread=%s stream failed after %d chunk(s); "
                        "rewound to latest checkpoint and switching fallback to %s/%s: %s",
                        self.thread_id,
                        stream_chunks,
                        payload.get("to_provider"),
                        payload.get("to_model"),
                        exc,
                    )
                    yield {**payload, "type": "provider_fallback"}
                    recovery_attempt = 0

                # Re-enter the graph from the latest checkpoint without
                # appending the original HumanMessage again.
                attempt_input_state = {"messages": []}
                self._reset_graph_state()

    def _turn_is_autonomous(self) -> Optional[bool]:
        """Turn-source signal for the fallback consent gate.

        Self-invoke turns are autonomous by definition; otherwise defer to the
        ``hook_is_autonomous`` stamp on the run config (None = source unknown,
        which the gate treats as not consent-capable).
        """
        if self.is_self_invoke:
            return True
        try:
            value = (self.config.get("configurable") or {}).get("hook_is_autonomous")
        except AttributeError:
            return None
        return None if value is None else bool(value)

    def _turn_holder_kind(self) -> Optional[str]:
        """The ``hook_holder_kind`` stamp for the consent gate (None = unknown)."""
        try:
            value = (self.config.get("configurable") or {}).get("hook_holder_kind")
        except AttributeError:
            return None
        return None if value is None else str(value)

    def _reset_graph_state(self) -> None:
        self._emitted_tool_starts: set[Any] = set()
        self._emitted_tool_ends: set[Any] = set()
        # run_id -> (monotonic start, wall-clock ISO start) for server-side
        # tool timing on the live stream.
        self._tool_call_started: dict[Any, tuple[float, str]] = {}
        # server_id -> resolved human server name, so an MCP tool's start and
        # end events share a single registry lookup for provenance.
        self._mcp_server_names: dict[str, str] = {}
        self._reasoning_deduper = ReasoningChunkDeduper()
        self._emitted_tool_call_delta = False
        self._streamed_text_in_current_llm_call = False
        self._streamed_reasoning_in_current_llm_call = False
        self._inline_text_stripper = InlineThinkingTextStripper()
        self._model_call_count = 0
        self._model_stream_event_count = 0
        self._model_end_without_stream_count = 0
        self._model_end_fallback_count = 0
        self._current_model_stream_events = 0
        self._current_model_started_at: Optional[float] = None
        self._current_model_response_parts_start = len(self.response_parts)
        self._graph_stream_started_at = time.monotonic()
        self._inline_hold_log_count = 0
        self._inline_release_log_count = 0
        self._inline_mark_log_count = 0

    async def _handle_event(
        self,
        event: dict[str, Any],
    ) -> AsyncGenerator[dict[str, Any], None]:
        event_type = event.get("event")

        if event_type == "on_chat_model_start":
            self._handle_model_start(event)
        elif event_type == "on_tool_start":
            for converted in self._handle_tool_start(event):
                yield converted
        elif event_type == "on_tool_end":
            for converted in self._handle_tool_end(event):
                yield converted
        elif event_type == "on_chat_model_stream":
            for converted in self._handle_model_stream(event):
                yield converted
        elif event_type == "on_chat_model_end":
            for converted in self._handle_model_end(event):
                yield converted
        elif event_type == "on_custom_event":
            converted = self._handle_custom_event(event)
            if converted:
                yield converted

    def _handle_custom_event(self, event: dict[str, Any]) -> Optional[dict[str, Any]]:
        name = event.get("name")
        if not isinstance(name, str) or not name:
            return None
        data = event.get("data")
        payload = data if isinstance(data, dict) else {"data": data}
        return {**payload, "type": name}

    def _handle_model_start(self, event: dict[str, Any]) -> None:
        self._model_call_count += 1
        self._current_model_stream_events = 0
        self._current_model_started_at = time.monotonic()
        self._streamed_text_in_current_llm_call = False
        self._streamed_reasoning_in_current_llm_call = False
        self._emitted_tool_call_delta = False
        self._reasoning_deduper.reset()
        self._inline_text_stripper.reset()
        self._current_model_response_parts_start = len(self.response_parts)
        self._log_stream_diagnostic(
            "[ASTREAM DIAG] llm_start thread=%s autonomous=%s run_id=%s",
            self.thread_id,
            self.is_self_invoke,
            event.get("run_id"),
        )

    def _mcp_provenance(self, tool_name: str) -> dict[str, Any]:
        """Return SSE provenance fields for a managed MCP tool, or ``{}``.

        Keyed off the internal ``mcp__<server_id>__<tool>`` name so the client
        can badge the call with its origin server. Only MCP tools touch the
        registry (to resolve the human server name); everything else short
        circuits on a pure string parse. Best-effort: a registry miss or error
        falls back to the server id, and non-MCP tools contribute nothing.
        """
        parsed = parse_mcp_tool_name(tool_name)
        if parsed is None:
            return {}
        server_id, _ = parsed
        server_name = self._mcp_server_names.get(server_id)
        if server_name is None:
            server_name = server_id
            try:
                from .mcp_servers import get_mcp_server_registry

                defn = get_mcp_server_registry().get_server(server_id)
                if defn is not None and getattr(defn, "name", ""):
                    server_name = defn.name
            except Exception:  # noqa: BLE001 - provenance is best-effort UI metadata
                pass
            self._mcp_server_names[server_id] = server_name
        return {
            "tool_type": "mcp_server",
            "server_id": server_id,
            "server_name": server_name,
        }

    def _handle_tool_start(self, event: dict[str, Any]) -> Iterable[dict[str, Any]]:
        run_id = event.get("run_id")
        if not run_id or run_id in self._emitted_tool_starts:
            return []

        self._emitted_tool_starts.add(run_id)
        tool_name = event.get("name", "")
        tool_input = event.get("data", {}).get("input", {})
        data = event.get("data", {})
        data_keys = list(data.keys()) if isinstance(data, dict) else type(data)
        self.logger.debug(
            "[ASTREAM] tool_start: name=%s, input=%s, raw_data_keys=%s",
            tool_name,
            tool_input,
            data_keys,
        )
        started_at = datetime.now(timezone.utc).isoformat()
        self._tool_call_started[run_id] = (time.monotonic(), started_at)
        chunk: dict[str, Any] = {
            "type": "tool_call",
            "id": run_id,
            "name": tool_name,
            "args": tool_input,
            "started_at": started_at,
        }
        if self.tool_timeout:
            chunk["timeout_seconds"] = int(self.tool_timeout)
        chunk.update(self._mcp_provenance(tool_name))
        return [chunk]

    def _handle_tool_end(self, event: dict[str, Any]) -> Iterable[dict[str, Any]]:
        run_id = event.get("run_id")
        if not run_id or run_id in self._emitted_tool_ends:
            return []

        self._emitted_tool_ends.add(run_id)
        tool_name = event.get("name", "")
        output = event.get("data", {}).get("output", "")
        raw_result = self._coerce_tool_result(output)
        display_result = self.clean_tool_result(raw_result)
        chunk: dict[str, Any] = {
            "type": "tool_result",
            "id": run_id,
            "name": tool_name,
            "result": display_result,
        }
        chunk.update(self._mcp_provenance(tool_name))
        # Server-authoritative live timing: diff our own monotonic clock from
        # tool_start (measured in the API process, around the BaseTool run).
        # The on_tool_end output is the pre-stamp inner ToolMessage, so
        # SafeToolNode's checkpointed tool_timing stamp (a slightly wider
        # bracket that also covers PRE/POST tool hooks) is not visible here;
        # it surfaces on history reload via agent_history instead.
        started = self._tool_call_started.pop(run_id, None)
        if started is not None:
            chunk["started_at"] = started[1]
            chunk["duration_ms"] = max(0, int((time.monotonic() - started[0]) * 1000))
        events = [chunk]
        events.extend(
            self.tool_result_extra_events(tool_name, raw_result, run_id, self.thread_id)
        )
        return events

    def _handle_model_stream(self, event: dict[str, Any]) -> Iterable[dict[str, Any]]:
        self._model_stream_event_count += 1
        self._current_model_stream_events += 1
        if self._current_model_stream_events == 1:
            first_ms = (
                int((time.monotonic() - self._current_model_started_at) * 1000)
                if self._current_model_started_at is not None
                else -1
            )
            self._log_stream_diagnostic(
                "[ASTREAM DIAG] first_model_stream thread=%s autonomous=%s "
                "run_id=%s after_ms=%d",
                self.thread_id,
                self.is_self_invoke,
                event.get("run_id"),
                first_ms,
            )

        chunk = event.get("data", {}).get("chunk")
        if not chunk:
            return []

        events: list[dict[str, Any]] = []
        tool_call_chunks = getattr(chunk, "tool_call_chunks", None)
        content = getattr(chunk, "content", None)
        if (
            not self._emitted_tool_call_delta
            and (
                has_tool_call_delta(tool_call_chunks)
                or has_tool_call_content_delta(content)
            )
        ):
            self._emitted_tool_call_delta = True
            events.append({"type": "tool_call_delta"})

        extras = getattr(chunk, "additional_kwargs", None) or {}
        reasoning = extras.get("reasoning_content")
        # Emit reasoning deltas verbatim. These are per-chunk deltas (a single
        # canonical source is picked upstream in _capture_reasoning_into), and a
        # reasoning token stream legitimately repeats short strings (a space, a
        # newline, common words). Exact-string dedupe here silently dropped
        # every repeat and fused the surrounding words together.
        if isinstance(reasoning, str) and reasoning:
            self._inline_text_stripper.reset()
            self._streamed_reasoning_in_current_llm_call = True
            events.append({"type": "thinking", "content": reasoning})

        if content:
            events.extend(self._stream_content_events(content, event))

        return events

    def _stream_content_events(
        self,
        content: Any,
        event: dict[str, Any],
    ) -> Iterable[dict[str, Any]]:
        if isinstance(content, list):
            events: list[dict[str, Any]] = []
            for block in content:
                events.extend(self._stream_content_block_events(block, event))
            return events

        if isinstance(content, str):
            self._streamed_text_in_current_llm_call = True
            clean_text = self._process_visible_text(content, event.get("run_id"))
            if clean_text:
                return [self._response_event(clean_text)]

        return []

    def _stream_content_block_events(
        self,
        block: Any,
        event: dict[str, Any],
    ) -> Iterable[dict[str, Any]]:
        if not isinstance(block, dict):
            if isinstance(block, str) and block:
                self._streamed_text_in_current_llm_call = True
                text = self._process_visible_text(block, event.get("run_id"))
                if text:
                    return [self._response_event(text)]
            return []

        block_type = block.get("type")
        if block_type == "thinking":
            text = block.get("thinking", "")
            if text:
                self._inline_text_stripper.reset()
                self._streamed_reasoning_in_current_llm_call = True
                return [{"type": "thinking", "content": text}]
        elif block_type == "reasoning":
            reasoning_texts = extract_reasoning_text_from_block(block)
            if reasoning_texts:
                self._inline_text_stripper.reset()
                self._streamed_reasoning_in_current_llm_call = True
            else:
                self._inline_text_stripper.mark_possible_inline_thinking()
                self._inline_mark_log_count += 1
                if self._inline_mark_log_count <= 3:
                    self._log_stream_diagnostic(
                        "[ASTREAM DIAG] inline_thinking_possible "
                        "thread=%s autonomous=%s run_id=%s stream_events=%d",
                        self.thread_id,
                        self.is_self_invoke,
                        event.get("run_id"),
                        self._current_model_stream_events,
                    )
            # Emit each reasoning delta verbatim (see the reasoning_content note
            # above). Deduping per delta dropped repeated tokens and fused
            # words; the flag set above already suppresses the model-end
            # fallback re-emit.
            return [
                {"type": "thinking", "content": text}
                for text in reasoning_texts
                if isinstance(text, str) and text
            ]
        elif block_type in ("text", "output_text"):
            text = block.get("text", "")
            if text:
                self._streamed_text_in_current_llm_call = True
                clean_text = self._process_visible_text(text, event.get("run_id"))
                if clean_text:
                    return [self._response_event(clean_text)]

        return []

    def _handle_model_end(self, event: dict[str, Any]) -> Iterable[dict[str, Any]]:
        self._log_model_end(event)
        events: list[dict[str, Any]] = []

        was_holding_inline_text = (
            self._inline_text_stripper.is_holding_possible_inline_thinking
        )
        buffered_inline_chars = self._inline_text_stripper.buffered_length
        clean_text = self._inline_text_stripper.flush()
        if was_holding_inline_text:
            self._log_stream_diagnostic(
                "[ASTREAM DIAG] inline_thinking_buffer_flush "
                "thread=%s autonomous=%s run_id=%s "
                "buffered_chars=%d emitted_chars=%d",
                self.thread_id,
                self.is_self_invoke,
                event.get("run_id"),
                buffered_inline_chars,
                len(clean_text),
            )
        if clean_text:
            events.append(self._response_event(clean_text))

        if self._streamed_text_in_current_llm_call:
            return events

        output = event.get("data", {}).get("output")
        if output and hasattr(output, "content") and output.content:
            self._model_end_fallback_count += 1
            self._log_stream_diagnostic(
                "[ASTREAM DIAG] model_end_response_fallback thread=%s "
                "autonomous=%s run_id=%s stream_events=%d",
                self.thread_id,
                self.is_self_invoke,
                event.get("run_id"),
                self._current_model_stream_events,
                warning=self.is_self_invoke and self._current_model_stream_events == 0,
            )
            events.extend(self._fallback_content_events(output.content))

        return events

    def _fallback_content_events(self, content: Any) -> Iterable[dict[str, Any]]:
        if isinstance(content, str) and content.strip():
            text = strip_inline_thinking_text(content)
            if text:
                return [self._response_event(text)]
            return []

        if not isinstance(content, list):
            return []

        events: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                if isinstance(block, str) and block:
                    text = strip_inline_thinking_text(block)
                    if text:
                        events.append(self._response_event(text))
                continue

            if block.get("type") == "reasoning":
                if self._streamed_reasoning_in_current_llm_call:
                    continue
                for text in extract_reasoning_text_from_block(block):
                    if self._should_emit_reasoning(text):
                        events.append({"type": "thinking", "content": text})
            elif block.get("type") in ("text", "output_text"):
                text = strip_inline_thinking_text(block.get("text", ""))
                if text:
                    events.append(self._response_event(text))

        return events

    def _coerce_tool_result(self, output: Any) -> str:
        # LangGraph Command results wrap ToolMessage content in update["messages"].
        if hasattr(output, "update") and hasattr(output, "goto"):
            cmd_msgs = (
                (output.update or {}).get("messages")
                if isinstance(output.update, dict)
                else None
            )
            if cmd_msgs:
                output = cmd_msgs[-1]

        if hasattr(output, "content"):
            result = output.content
        else:
            result = str(output)

        return result if isinstance(result, str) else str(result)

    def _response_event(self, text: str) -> dict[str, Any]:
        self.response_parts.append(text)
        return {"type": "response", "content": text}

    def _rollback_current_model_output(self) -> None:
        del self.response_parts[self._current_model_response_parts_start:]

    def _should_emit_reasoning(self, text: Any) -> bool:
        return self._reasoning_deduper.should_emit(text)

    def _process_visible_text(self, text: str, run_id: Any) -> str:
        """Sanitize answer text and log when possible preamble is held."""
        was_holding = self._inline_text_stripper.is_holding_possible_inline_thinking
        clean_text = self._inline_text_stripper.process_text(text)
        is_holding = self._inline_text_stripper.is_holding_possible_inline_thinking

        if is_holding and not clean_text:
            self._inline_hold_log_count += 1
            if self._inline_hold_log_count <= 3 or self._inline_hold_log_count % 25 == 0:
                self._log_stream_diagnostic(
                    "[ASTREAM DIAG] inline_thinking_buffer_hold "
                    "thread=%s autonomous=%s run_id=%s "
                    "raw_chars=%d buffered_chars=%d hold_count=%d",
                    self.thread_id,
                    self.is_self_invoke,
                    run_id,
                    len(text),
                    self._inline_text_stripper.buffered_length,
                    self._inline_hold_log_count,
                )
        elif was_holding and not is_holding:
            self._inline_release_log_count += 1
            self._log_stream_diagnostic(
                "[ASTREAM DIAG] inline_thinking_buffer_release "
                "thread=%s autonomous=%s run_id=%s emitted_chars=%d "
                "buffered_chars=%d release_count=%d",
                self.thread_id,
                self.is_self_invoke,
                run_id,
                len(clean_text),
                self._inline_text_stripper.buffered_length,
                self._inline_release_log_count,
            )

        return clean_text

    def _log_model_end(self, event: dict[str, Any]) -> None:
        if self._current_model_stream_events == 0:
            self._model_end_without_stream_count += 1
            self._log_stream_diagnostic(
                "[ASTREAM DIAG] llm_end_without_stream thread=%s "
                "autonomous=%s run_id=%s",
                self.thread_id,
                self.is_self_invoke,
                event.get("run_id"),
                warning=self.is_self_invoke,
            )
            return

        self._log_stream_diagnostic(
            "[ASTREAM DIAG] llm_end thread=%s autonomous=%s "
            "run_id=%s stream_events=%d",
            self.thread_id,
            self.is_self_invoke,
            event.get("run_id"),
            self._current_model_stream_events,
        )

    def _log_graph_done(self) -> None:
        self._log_stream_diagnostic(
            "[ASTREAM DIAG] graph_done thread=%s autonomous=%s "
            "model_calls=%d model_stream_events=%d "
            "model_end_without_stream=%d model_end_fallbacks=%d elapsed_ms=%d",
            self.thread_id,
            self.is_self_invoke,
            self._model_call_count,
            self._model_stream_event_count,
            self._model_end_without_stream_count,
            self._model_end_fallback_count,
            int((time.monotonic() - self._graph_stream_started_at) * 1000),
        )

    def _log_stream_diagnostic(
        self,
        message: str,
        *args: Any,
        warning: bool = False,
    ) -> None:
        if warning:
            self.logger.warning(message, *args)
        elif self.is_self_invoke:
            self.logger.info(message, *args)
        else:
            self.logger.debug(message, *args)
