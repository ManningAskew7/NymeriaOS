"""Streaming helpers for NymeriaAgent.astream()."""

from __future__ import annotations

import logging
import time
from typing import Any, AsyncGenerator, Callable, Iterable, List, Optional

from .agent_history import (
    InlineThinkingTextStripper,
    extract_reasoning_text_from_block,
    strip_inline_thinking_text,
)
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
    """Deduplicate reasoning chunks within a single model call."""

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
        tool_result_extra_events: Callable[[str, str, Any], Iterable[dict[str, Any]]],
        stream_logger: Optional[logging.Logger] = None,
    ) -> None:
        self.thread_id = thread_id
        self.config = config
        self.abort_event = abort_event
        self.is_self_invoke = is_self_invoke
        self.response_parts = response_parts
        self.clean_tool_result = clean_tool_result
        self.tool_result_extra_events = tool_result_extra_events
        self.logger = stream_logger or logger

        self._reset_graph_state()

    async def drive(
        self,
        graph_obj: Any,
        input_state: dict[str, Any],
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Drive one graph invocation and yield converted SSE events."""
        self._reset_graph_state()

        async for event in graph_obj.astream_events(
            input_state,
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

    def _reset_graph_state(self) -> None:
        self._emitted_tool_starts: set[Any] = set()
        self._emitted_tool_ends: set[Any] = set()
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

    def _handle_model_start(self, event: dict[str, Any]) -> None:
        self._model_call_count += 1
        self._current_model_stream_events = 0
        self._current_model_started_at = time.monotonic()
        self._streamed_text_in_current_llm_call = False
        self._streamed_reasoning_in_current_llm_call = False
        self._emitted_tool_call_delta = False
        self._reasoning_deduper.reset()
        self._inline_text_stripper.reset()
        self._log_stream_diagnostic(
            "[ASTREAM DIAG] llm_start thread=%s autonomous=%s run_id=%s",
            self.thread_id,
            self.is_self_invoke,
            event.get("run_id"),
        )

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
        return [{
            "type": "tool_call",
            "id": run_id,
            "name": tool_name,
            "args": tool_input,
        }]

    def _handle_tool_end(self, event: dict[str, Any]) -> Iterable[dict[str, Any]]:
        run_id = event.get("run_id")
        if not run_id or run_id in self._emitted_tool_ends:
            return []

        self._emitted_tool_ends.add(run_id)
        tool_name = event.get("name", "")
        output = event.get("data", {}).get("output", "")
        raw_result = self._coerce_tool_result(output)
        display_result = self.clean_tool_result(raw_result)
        events = [{
            "type": "tool_result",
            "id": run_id,
            "name": tool_name,
            "result": display_result,
        }]
        events.extend(self.tool_result_extra_events(tool_name, raw_result, run_id))
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
        if self._should_emit_reasoning(reasoning):
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
            return [
                {"type": "thinking", "content": text}
                for text in reasoning_texts
                if self._mark_and_should_emit_reasoning(text)
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

    def _should_emit_reasoning(self, text: Any) -> bool:
        return self._reasoning_deduper.should_emit(text)

    def _mark_and_should_emit_reasoning(self, text: Any) -> bool:
        should_emit = self._should_emit_reasoning(text)
        if should_emit:
            self._streamed_reasoning_in_current_llm_call = True
        return should_emit

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
