"""Conversation history projection helpers for NymeriaAgent."""

from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

logger = logging.getLogger(__name__)


def _normalize_legacy_compaction_summary(summary: Any) -> str:
    """Render summaries that were stored as Python repr of content-block lists.

    Pre-fix compactions stored `str(content)` where `content` was a list of
    Anthropic content blocks like `[{'type': 'text', 'text': '...'}]`. Parse
    those back into the joined text so historical compaction notices render
    as markdown instead of raw repr.
    """
    if not isinstance(summary, str):
        return str(summary) if summary is not None else ""
    stripped = summary.lstrip()
    if not (stripped.startswith("[{") and stripped.rstrip().endswith("}]")):
        return summary
    try:
        parsed = ast.literal_eval(stripped)
    except (ValueError, SyntaxError):
        return summary
    if not isinstance(parsed, list):
        return summary
    parts: List[str] = []
    for block in parsed:
        if isinstance(block, dict):
            block_type = block.get("type")
            if block_type in (None, "text", "output_text"):
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        elif isinstance(block, str) and block:
            parts.append(block)
    return "\n".join(parts) if parts else summary


# Regex to strip injected time context from user messages in history.
# Matches: [Current Time: ...]\n[Trigger: ...]\n\n  OR  [Time: ...]\n[Trigger: ...]\n\n
CONTEXT_PREFIX_PATTERN = re.compile(
    r"^\[(?:Current )?Time:[^\]]+\]\n\[Trigger:[^\]]+\]\n\n",
    re.MULTILINE,
)


# Regex to extract the timestamp string from the time context prefix.
TIMESTAMP_EXTRACT_PATTERN = re.compile(
    r"^\[(?:Current )?Time:\s*(.+?)\s*\((\S+)\)\s*\]",
    re.MULTILINE,
)


#: Hard cap on how many checkpoints we'll deserialize when computing
#: message timestamps. Threads without compaction can accumulate
#: thousands of checkpoints; walking all of them to find the creation
#: of messages that live in every checkpoint wastes tens of seconds of
#: CPU on each /history poll and pegs the event loop.
TIMESTAMP_SCAN_LIMIT = 200


ToolResultCleaner = Callable[[str], str]
WorkspaceArtifactExtractor = Callable[[str], List[Dict[str, Any]]]


def extract_timestamp(text: str) -> Optional[str]:
    """Extract ISO timestamp from the injected time-context prefix."""
    from zoneinfo import ZoneInfo

    m = TIMESTAMP_EXTRACT_PATTERN.search(text)
    if not m:
        return None
    try:
        date_str = m.group(1).strip()
        tz_name = m.group(2).strip()
        date_str = date_str.replace(" at ", " ")
        dt = datetime.strptime(date_str, "%A, %B %d, %Y %I:%M %p")
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
        return dt.isoformat()
    except (ValueError, KeyError):
        return None


def build_message_timestamp_map(
    graph: Any,
    thread_id: str,
    target_ids: Optional[set] = None,
) -> Dict[str, str]:
    """Build a message_id -> ISO timestamp map from LangGraph checkpoint history.

    Each checkpoint records a ``created_at`` timestamp. Messages are append-only
    within a compaction window, so a message's earliest appearance in the
    checkpoint stream gives its creation time.

    The history stream is newest-first. When target IDs are provided, this stops
    once every target has aged out of older checkpoints. It also caps the total
    walk at ``TIMESTAMP_SCAN_LIMIT`` so long-running un-compacted threads do not
    deserialize every checkpoint on each /history poll.
    """
    config = {"configurable": {"thread_id": thread_id}}
    timestamp_map: Dict[str, str] = {}

    try:
        state_iter = graph.get_state_history(config, limit=TIMESTAMP_SCAN_LIMIT)
    except Exception as e:
        logger.warning("[Timestamps] Failed to open state history for thread %s: %s", thread_id, e)
        return timestamp_map

    if not target_ids:
        try:
            for i, state in enumerate(state_iter):
                if i >= TIMESTAMP_SCAN_LIMIT:
                    break
                ts = getattr(state, "created_at", None)
                if not ts:
                    continue
                try:
                    msgs = state.values.get("messages", [])
                except Exception:
                    continue
                for msg in msgs:
                    try:
                        mid = getattr(msg, "id", None)
                        if mid:
                            timestamp_map[mid] = ts
                    except Exception:
                        continue
        except Exception as e:
            logger.warning("[Timestamps] Failed to build checkpoint map for thread %s: %s", thread_id, e)
        return timestamp_map

    still_active: set = set(target_ids)
    try:
        for i, state in enumerate(state_iter):
            if not still_active or i >= TIMESTAMP_SCAN_LIMIT:
                break
            ts = getattr(state, "created_at", None)
            if not ts:
                continue
            try:
                msg_ids_here = {
                    getattr(msg, "id", None)
                    for msg in state.values.get("messages", [])
                }
                msg_ids_here.discard(None)
            except Exception:
                continue

            for mid in still_active & msg_ids_here:
                timestamp_map[mid] = ts

            aged_out = still_active - msg_ids_here
            if aged_out:
                still_active -= aged_out
    except Exception as e:
        logger.warning("[Timestamps] Failed to build checkpoint map for thread %s: %s", thread_id, e)

    return timestamp_map


def extract_mime_from_data_url(data_url: str) -> str:
    """Extract MIME type from a data URL like 'data:image/png;base64,...'."""
    if data_url.startswith("data:"):
        header = data_url.split(",", 1)[0]
        mime = header[5:]
        if ";" in mime:
            mime = mime.split(";", 1)[0]
        return mime
    return "application/octet-stream"


def classify_autonomous_source(text: str) -> str:
    """Classify the source of an autonomous wakeup from its stripped prompt."""
    if text.startswith("Work on TODO "):
        return "scheduler"
    if text.startswith("[WATCHDOG ALERT]"):
        return "watchdog"
    return "trigger"


def extract_reasoning_text_from_block(block: Dict[str, Any]) -> List[str]:
    """Extract plaintext reasoning from one Responses API reasoning block."""
    content_parts: List[str] = []
    summary_parts: List[str] = []

    def add(value: Any, parts: List[str]) -> None:
        if isinstance(value, str) and value:
            parts.append(value)
        elif isinstance(value, dict):
            if value.get("type") == "reasoning.encrypted":
                return
            add(
                value.get("text")
                or value.get("content")
                or value.get("reasoning")
                or value.get("summary"),
                parts,
            )
        elif isinstance(value, list):
            for item in value:
                add(item, parts)

    add(block.get("reasoning"), content_parts)
    add(block.get("content"), content_parts)

    summary = block.get("summary")
    if isinstance(summary, str):
        add(summary, summary_parts)
    elif isinstance(summary, list):
        for part in summary:
            add(part, summary_parts)

    sections: List[str] = []
    if content_parts:
        sections.append("".join(content_parts))
    if summary_parts:
        sections.append("\n\n".join(summary_parts))

    joined = "\n\n".join(section for section in sections if section)
    return [joined] if joined else []


def extract_reasoning_text_from_details(details: Any) -> List[str]:
    """Extract displayable plaintext from OpenRouter reasoning_details metadata."""
    parts: List[str] = []

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
    joined = "".join(parts)
    return [joined] if joined else []


THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


def strip_inline_thinking_text(text: str) -> str:
    """Remove provider-leaked inline thinking tags from assistant text."""
    if not isinstance(text, str) or not text:
        return text
    if THINK_OPEN not in text and THINK_CLOSE not in text:
        return text

    response_parts: List[str] = []
    i = 0

    while i < len(text):
        open_idx = text.find(THINK_OPEN, i)
        close_idx = text.find(THINK_CLOSE, i)

        if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
            i = close_idx + len(THINK_CLOSE)
            while i < len(text) and text[i].isspace():
                i += 1
            continue

        if open_idx == -1:
            response_parts.append(text[i:])
            break

        response_parts.append(text[i:open_idx])
        i = open_idx + len(THINK_OPEN)

        close_after_open = text.find(THINK_CLOSE, i)
        if close_after_open == -1:
            break

        i = close_after_open + len(THINK_CLOSE)
        while i < len(text) and text[i].isspace():
            i += 1

    return "".join(response_parts)


def trailing_marker_prefix_length(text: str) -> int:
    """Return suffix length that may be the start of a think marker."""
    max_keep = min(len(text), max(len(THINK_OPEN), len(THINK_CLOSE)) - 1)
    for length in range(max_keep, 0, -1):
        suffix = text[-length:]
        if THINK_OPEN.startswith(suffix) or THINK_CLOSE.startswith(suffix):
            return length
    return 0


class InlineThinkingTextStripper:
    """Streaming sanitizer for provider-leaked inline thinking text."""

    def __init__(self) -> None:
        self._inside_thinking = False
        self._maybe_dangling_thinking = False
        self._buffer = ""

    @property
    def is_holding_possible_inline_thinking(self) -> bool:
        return self._maybe_dangling_thinking and bool(self._buffer)

    @property
    def buffered_length(self) -> int:
        return len(self._buffer)

    def mark_possible_inline_thinking(self) -> None:
        """Buffer text after an empty reasoning placeholder until safe."""
        if not self._inside_thinking and not self._buffer:
            self._maybe_dangling_thinking = True

    def process_text(self, text: str) -> str:
        if not isinstance(text, str) or not text:
            return ""

        self._buffer += text

        if self._maybe_dangling_thinking:
            open_idx = self._buffer.find(THINK_OPEN)
            close_idx = self._buffer.find(THINK_CLOSE)
            if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
                self._buffer = self._buffer[close_idx + len(THINK_CLOSE):].lstrip()
                self._maybe_dangling_thinking = False
            elif open_idx != -1:
                self._maybe_dangling_thinking = False
            else:
                return ""

        return self._drain()

    def flush(self) -> str:
        self._maybe_dangling_thinking = False
        return self._drain(final=True)

    def _drain(self, final: bool = False) -> str:
        output: List[str] = []

        while self._buffer:
            if self._inside_thinking:
                close_idx = self._buffer.find(THINK_CLOSE)
                if close_idx == -1:
                    if final:
                        self._buffer = ""
                    else:
                        keep = trailing_marker_prefix_length(self._buffer)
                        self._buffer = self._buffer[-keep:] if keep else ""
                    break
                self._buffer = self._buffer[close_idx + len(THINK_CLOSE):].lstrip()
                self._inside_thinking = False
                continue

            open_idx = self._buffer.find(THINK_OPEN)
            close_idx = self._buffer.find(THINK_CLOSE)
            if close_idx != -1 and (open_idx == -1 or close_idx < open_idx):
                self._buffer = self._buffer[close_idx + len(THINK_CLOSE):].lstrip()
                continue

            if open_idx != -1:
                output.append(self._buffer[:open_idx])
                self._buffer = self._buffer[open_idx + len(THINK_OPEN):]
                self._inside_thinking = True
                continue

            if final:
                output.append(self._buffer)
                self._buffer = ""
                break

            keep = trailing_marker_prefix_length(self._buffer)
            if keep:
                output.append(self._buffer[:-keep])
                self._buffer = self._buffer[-keep:]
            else:
                output.append(self._buffer)
                self._buffer = ""
            break

        return "".join(output)

    def reset(self) -> None:
        self._inside_thinking = False
        self._maybe_dangling_thinking = False
        self._buffer = ""


def extract_content_parts(content: Any) -> tuple[str, List[str]]:
    """Extract display text and thinking blocks from AIMessage.content."""
    if isinstance(content, str):
        return strip_inline_thinking_text(content), []
    if isinstance(content, list):
        text_parts = []
        thinking_parts = []
        for block in content:
            if isinstance(block, dict):
                block_type = block.get("type")
                if block_type in ("text", "output_text"):
                    text = strip_inline_thinking_text(block.get("text", ""))
                    if text:
                        text_parts.append(text)
                elif block_type == "thinking":
                    thinking_parts.append(block.get("thinking", ""))
                elif block_type == "reasoning":
                    thinking_parts.extend(extract_reasoning_text_from_block(block))
            elif isinstance(block, str):
                text = strip_inline_thinking_text(block)
                if text:
                    text_parts.append(text)
        return "\n".join(text_parts), thinking_parts
    return str(content), []


def extract_reasoning_parts(msg: Any) -> List[str]:
    """Extract OpenAI-compatible reasoning saved on AIMessage metadata."""
    additional_kwargs = getattr(msg, "additional_kwargs", None) or {}
    parts: List[str] = []
    seen: set[str] = set()

    def add_text(value: Any) -> None:
        if isinstance(value, str):
            text = value
        elif isinstance(value, dict):
            summary = value.get("summary")
            if isinstance(summary, list):
                for item in summary:
                    add_text(item)
                return
            text = (
                value.get("text")
                or value.get("content")
                or summary
                or value.get("reasoning")
            )
        elif isinstance(value, list):
            for item in value:
                add_text(item)
            return
        else:
            text = None

        if isinstance(text, str) and text and text not in seen:
            parts.append(text)
            seen.add(text)

    for text in extract_reasoning_text_from_details(
        additional_kwargs.get("reasoning_details")
    ):
        add_text(text)

    for key in ("reasoning_content", "reasoning"):
        value = additional_kwargs.get(key)
        if isinstance(value, list):
            for item in value:
                add_text(item)
        else:
            add_text(value)

    return parts


def thinking_steps(thinking_blocks: List[str]) -> List[Dict[str, str]]:
    """Format thinking strings as frontend MessageStep entries."""
    return [
        {"type": "thinking", "content": thinking_text}
        for thinking_text in thinking_blocks
        if thinking_text
    ]


def _default_clean_tool_result(value: str) -> str:
    return value if isinstance(value, str) else str(value)


def _default_extract_workspace_artifacts(_value: str) -> List[Dict[str, Any]]:
    return []


@dataclass
class _HistoryFormatContext:
    thread_id: str
    timestamp_map: Dict[str, str]
    show_autonomous_prompts: bool
    show_prompt_metadata: bool
    tool_results: Dict[str, Any]
    clean_tool_result: ToolResultCleaner
    extract_workspace_artifacts: WorkspaceArtifactExtractor
    history: List[Dict[str, Any]] = field(default_factory=list)
    msg_counter: int = 0
    current_turn: Optional[Dict[str, Any]] = None
    pending_reload_info: Optional[Dict[str, Any]] = None

    def next_entry_id(self) -> str:
        self.msg_counter += 1
        return f"{self.thread_id}-{self.msg_counter}"

    def flush_current_turn(self, *, populate_legacy_fields: bool = False) -> None:
        if not self.current_turn:
            return
        if populate_legacy_fields:
            _populate_legacy_turn_fields(self.current_turn)
        self.history.append(self.current_turn)
        self.current_turn = None


def format_conversation_history(
    messages: List[Any],
    *,
    thread_id: str,
    timestamp_map: Optional[Dict[str, str]] = None,
    include_internal: bool = False,
    show_autonomous_prompts: bool = False,
    show_prompt_metadata: bool = False,
    clean_tool_result: Optional[ToolResultCleaner] = None,
    extract_workspace_artifacts: Optional[WorkspaceArtifactExtractor] = None,
) -> List[Dict[str, Any]]:
    """Format LangChain checkpoint messages for frontend display."""
    timestamp_map = timestamp_map or {}
    clean_tool_result = clean_tool_result or _default_clean_tool_result
    extract_workspace_artifacts = extract_workspace_artifacts or _default_extract_workspace_artifacts

    if not include_internal:
        messages = _filter_internal_messages(
            messages,
            show_autonomous_prompts=show_autonomous_prompts,
        )

    tool_results: Dict[str, Any] = {}
    for msg in messages:
        if isinstance(msg, ToolMessage):
            tool_results[msg.tool_call_id] = msg.content

    ctx = _HistoryFormatContext(
        thread_id=thread_id,
        timestamp_map=timestamp_map,
        show_autonomous_prompts=show_autonomous_prompts,
        show_prompt_metadata=show_prompt_metadata,
        tool_results=tool_results,
        clean_tool_result=clean_tool_result,
        extract_workspace_artifacts=extract_workspace_artifacts,
    )

    for msg in messages:
        if isinstance(msg, ToolMessage):
            continue

        handler = _get_history_message_handler(msg)
        if handler is not None:
            handler(ctx, msg)

    ctx.flush_current_turn(populate_legacy_fields=True)

    return ctx.history


MessageHistoryHandler = Callable[[_HistoryFormatContext, Any], None]


def _get_history_message_handler(msg: Any) -> Optional[MessageHistoryHandler]:
    for message_type, handler in _HISTORY_MESSAGE_HANDLERS.items():
        if isinstance(msg, message_type):
            return handler
    return None


def _handle_human_history_message(
    ctx: _HistoryFormatContext,
    msg: HumanMessage,
) -> None:
    if msg.additional_kwargs.get("internal_type") in ("compaction_marker", "memory_seed_marker"):
        ctx.flush_current_turn()

        marker_kwargs = msg.additional_kwargs or {}
        timestamp_iso = (
            marker_kwargs.get("timestamp")
            or (ctx.timestamp_map.get(msg.id) if msg.id else None)
        )
        entry: Dict[str, Any] = {
            "id": ctx.next_entry_id(),
            "role": "system",
            "kind": "compaction_notice",
            "content": "Context compacted",
            "context_summary": _normalize_legacy_compaction_summary(
                marker_kwargs.get("summary") or ""
            ),
            "messages_removed": marker_kwargs.get("messages_removed", 0),
            "auto_resumed": bool(marker_kwargs.get("auto_resumed", False)),
        }
        if timestamp_iso:
            entry["timestamp"] = timestamp_iso
        ctx.history.append(entry)
        return

    if msg.additional_kwargs.get("internal_type") == "tool_reload_resume":
        ctx.flush_current_turn()
        content_str = msg.content if isinstance(msg.content, str) else str(msg.content)
        ctx.pending_reload_info = {
            "tools": msg.additional_kwargs.get("tool_reload_tools", []),
            "ttl": msg.additional_kwargs.get("tool_reload_ttl", ""),
            "ttl_seconds": msg.additional_kwargs.get("tool_reload_ttl_seconds"),
            "source": msg.additional_kwargs.get("tool_reload_source", "tool_search"),
            "skill_name": msg.additional_kwargs.get("tool_reload_skill_name"),
            "reason": msg.additional_kwargs.get("tool_reload_reason"),
            "resume_prompt": content_str,
        }
        return

    ctx.flush_current_turn()

    entry: Dict[str, Any] = {
        "id": ctx.next_entry_id(),
        "role": "user",
    }

    raw_content, attachments = _parse_human_content(
        msg.content,
        getattr(msg, "additional_kwargs", None) or {},
        ctx.msg_counter,
    )
    timestamp_iso = ctx.timestamp_map.get(msg.id) if msg.id else None
    if not timestamp_iso:
        timestamp_iso = extract_timestamp(raw_content)

    if ctx.show_prompt_metadata:
        entry["content"] = raw_content
    else:
        entry["content"] = CONTEXT_PREFIX_PATTERN.sub("", raw_content)
    if attachments:
        entry["attachments"] = attachments
    if timestamp_iso:
        entry["timestamp"] = timestamp_iso

    if (
        ctx.show_autonomous_prompts
        and msg.additional_kwargs.get("internal_type") == "autonomous_wakeup"
    ):
        entry["autonomous_source"] = classify_autonomous_source(entry["content"])

    ctx.history.append(entry)


def _handle_ai_history_message(
    ctx: _HistoryFormatContext,
    msg: AIMessage,
) -> None:
    text_content, thinking_blocks = extract_content_parts(msg.content)
    reasoning_blocks = extract_reasoning_parts(msg)

    if msg.tool_calls:
        if ctx.current_turn is None:
            ctx.current_turn = {
                "id": ctx.next_entry_id(),
                "role": "assistant",
                "content": "",
                "steps": [],
            }
            if ctx.pending_reload_info:
                ctx.current_turn["tool_reload_info"] = ctx.pending_reload_info
                ctx.pending_reload_info = None
            turn_ts = ctx.timestamp_map.get(msg.id) if msg.id else None
            if turn_ts:
                ctx.current_turn["timestamp"] = turn_ts

        ctx.current_turn["steps"].extend(thinking_steps(reasoning_blocks))
        _append_tool_call_steps(
            ctx.current_turn,
            msg,
            text_content=text_content,
            tool_results=ctx.tool_results,
            clean_tool_result=ctx.clean_tool_result,
            extract_workspace_artifacts=ctx.extract_workspace_artifacts,
        )
        return

    if ctx.current_turn is not None:
        ctx.current_turn["steps"].extend(
            thinking_steps(reasoning_blocks + thinking_blocks)
        )

        if text_content:
            ctx.current_turn["steps"].append({
                "type": "response",
                "content": text_content,
            })

        ctx.current_turn["content"] = text_content

        if "timestamp" not in ctx.current_turn:
            backfill_ts = ctx.timestamp_map.get(msg.id) if msg.id else None
            if backfill_ts:
                ctx.current_turn["timestamp"] = backfill_ts

        ctx.flush_current_turn(populate_legacy_fields=True)
        return

    entry: dict[str, Any] = {
        "id": ctx.next_entry_id(),
        "role": "assistant",
        "content": text_content,
    }
    if ctx.pending_reload_info:
        entry["tool_reload_info"] = ctx.pending_reload_info
        ctx.pending_reload_info = None
    all_thinking_blocks = reasoning_blocks + thinking_blocks
    if all_thinking_blocks:
        steps = thinking_steps(all_thinking_blocks)
        if text_content:
            steps.append({
                "type": "response",
                "content": text_content,
            })
        entry["steps"] = steps
        entry["intermediate_content"] = "\n".join(
            s["content"] for s in steps if s["type"] == "thinking"
        ) or None
    standalone_ts = ctx.timestamp_map.get(msg.id) if msg.id else None
    if standalone_ts:
        entry["timestamp"] = standalone_ts
    ctx.history.append(entry)


def _handle_system_history_message(
    ctx: _HistoryFormatContext,
    msg: SystemMessage,
) -> None:
    ctx.flush_current_turn()
    ctx.history.append({
        "id": ctx.next_entry_id(),
        "role": "system",
        "content": msg.content if isinstance(msg.content, str) else str(msg.content),
    })


_HISTORY_MESSAGE_HANDLERS: Dict[type, MessageHistoryHandler] = {
    HumanMessage: _handle_human_history_message,
    AIMessage: _handle_ai_history_message,
    SystemMessage: _handle_system_history_message,
}


def _filter_internal_messages(
    messages: List[Any],
    *,
    show_autonomous_prompts: bool,
) -> List[Any]:
    """Filter internal system prompts while preserving displayable outputs."""
    filtered_messages = []
    skip_until_next_human = False

    for msg in messages:
        if isinstance(msg, HumanMessage):
            is_internal = msg.additional_kwargs.get("internal", False)

            if is_internal:
                internal_type = msg.additional_kwargs.get("internal_type", "")
                if internal_type == "autonomous_wakeup":
                    if show_autonomous_prompts:
                        skip_until_next_human = False
                        filtered_messages.append(msg)
                    else:
                        skip_until_next_human = False
                    continue
                if internal_type == "compaction_marker":
                    skip_until_next_human = False
                    filtered_messages.append(msg)
                    continue
                if internal_type == "memory_seed_marker":
                    # Keep the resume opener (rendered as a compaction notice),
                    # but suppress the read-back AI/Tool messages that follow it
                    # from the user-facing view. They remain in LLM context.
                    filtered_messages.append(msg)
                    skip_until_next_human = True
                    continue
                if internal_type == "auto_resume":
                    skip_until_next_human = False
                    continue
                if internal_type == "tool_reload_resume":
                    filtered_messages.append(msg)
                    skip_until_next_human = False
                    continue
                skip_until_next_human = True
                continue

            skip_until_next_human = False
            filtered_messages.append(msg)
        elif skip_until_next_human:
            continue
        else:
            filtered_messages.append(msg)

    return filtered_messages


def _parse_human_content(
    content: Any,
    additional_kwargs: Dict[str, Any],
    msg_counter: int,
) -> tuple[str, List[Dict[str, Any]]]:
    """Parse user text plus image/document attachments from message content.

    Phase B+ turns persist attachment metadata on ``additional_kwargs`` so the
    original filename, byte size, and (for sandbox-routed documents) the
    attachment id survive a thread reload. Prefer that path when present;
    fall back to the legacy synthesis from content blocks for older history.
    """
    attachments: List[Dict[str, Any]] = []

    try:
        metadata = additional_kwargs.get("attachments") if additional_kwargs else None
        if isinstance(metadata, list) and metadata:
            return _parse_human_content_from_metadata(content, metadata, msg_counter)

        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") in ("text", "output_text"):
                        text_parts.append(part.get("text", ""))
                    elif part.get("type") == "image_url":
                        data_url = part.get("image_url", {}).get("url", "")
                        mime = extract_mime_from_data_url(data_url)
                        attachments.append({
                            "id": f"att-{msg_counter}-{len(attachments)}",
                            "type": "image",
                            "dataUrl": data_url,
                            "mimeType": mime,
                            "name": f"image.{mime.split('/')[-1] if '/' in mime else 'png'}",
                            "size": len(data_url),
                        })
                    elif part.get("type") == "file":
                        file_mime = part.get("mime_type", "application/octet-stream")
                        file_data = part.get("data", "")
                        data_url = f"data:{file_mime};base64,{file_data}"
                        ext = file_mime.split("/")[-1] if "/" in file_mime else "bin"
                        attachments.append({
                            "id": f"att-{msg_counter}-{len(attachments)}",
                            "type": "document",
                            "dataUrl": data_url,
                            "mimeType": file_mime,
                            "name": f"document.{ext}",
                            "size": len(file_data),
                        })
                elif isinstance(part, str):
                    text_parts.append(part)
            return "\n".join(text_parts), attachments
        return content if isinstance(content, str) else str(content), attachments
    except Exception:
        return str(content), []


def _parse_human_content_from_metadata(
    content: Any,
    metadata: List[Any],
    msg_counter: int,
) -> tuple[str, List[Dict[str, Any]]]:
    """Build attachment pills from the canonical additional_kwargs payload.

    The metadata list mirrors the shape written by ``agent_streaming_input``:
        - ``type: "image"`` entries carry the inline ``data_url`` so the
          original image still renders in the bubble.
        - ``type: "document"`` entries reference a sandbox file by ``id``; the
          frontend uses the owner-scoped download endpoint instead of an
          inline payload, so ``dataUrl`` is left empty here.
    """
    text_parts: List[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in ("text", "output_text"):
                    text_parts.append(part.get("text", ""))
            elif isinstance(part, str):
                text_parts.append(part)
    elif isinstance(content, str):
        text_parts.append(content)

    # Defensive: pull image data URLs straight from the content blocks too, in
    # case a producer wrote metadata without echoing ``data_url`` on the image
    # entry. Order preserved so we can match them positionally.
    inline_image_urls: List[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                inline_image_urls.append(part.get("image_url", {}).get("url", ""))

    attachments_out: List[Dict[str, Any]] = []
    image_idx = 0
    for entry in metadata:
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        if entry_type == "image":
            data_url = entry.get("data_url") or (
                inline_image_urls[image_idx] if image_idx < len(inline_image_urls) else ""
            )
            image_idx += 1
            attachments_out.append({
                "id": entry.get("id") or f"att-{msg_counter}-{len(attachments_out)}",
                "type": "image",
                "dataUrl": data_url,
                "mimeType": entry.get("mime_type", "") or extract_mime_from_data_url(data_url),
                "name": entry.get("name") or "image",
                "size": entry.get("size") or len(data_url),
            })
        elif entry_type == "document":
            attachments_out.append({
                "id": entry.get("id") or f"att-{msg_counter}-{len(attachments_out)}",
                "type": "document",
                # Documents live in the per-thread sandbox; frontend fetches the
                # bytes via /threads/{tid}/attachments/{aid}/download when the
                # user clicks the pill.
                "dataUrl": "",
                "mimeType": entry.get("mime_type", "application/octet-stream"),
                "name": entry.get("name") or "document",
                "size": entry.get("size") or 0,
            })
    return "\n".join(text_parts), attachments_out


def _append_tool_call_steps(
    current_turn: Dict[str, Any],
    msg: AIMessage,
    *,
    text_content: str,
    tool_results: Dict[str, Any],
    clean_tool_result: ToolResultCleaner,
    extract_workspace_artifacts: WorkspaceArtifactExtractor,
) -> None:
    """Append response/tool-call steps from an AIMessage with tool calls."""
    if isinstance(msg.content, list):
        tc_args_by_id = {}
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tc_id = tc.get("id", "")
                if tc_id:
                    tc_args_by_id[tc_id] = tc.get("args", {})
        for block in msg.content:
            if not isinstance(block, dict):
                if isinstance(block, str) and block:
                    text = strip_inline_thinking_text(block)
                    if text:
                        current_turn["steps"].append({
                            "type": "response",
                            "content": text,
                        })
                continue
            block_type = block.get("type")
            if block_type == "thinking":
                thinking_text = block.get("thinking", "")
                if thinking_text:
                    current_turn["steps"].append({
                        "type": "thinking",
                        "content": thinking_text,
                    })
            elif block_type == "reasoning":
                current_turn["steps"].extend(
                    thinking_steps(extract_reasoning_text_from_block(block))
                )
            elif block_type in ("text", "output_text"):
                text = strip_inline_thinking_text(block.get("text", ""))
                if text:
                    current_turn["steps"].append({
                        "type": "response",
                        "content": text,
                    })
            elif block_type in ("tool_use", "function_call", "custom_tool_call"):
                tool_call_id = block.get("id", "")
                if block_type != "tool_use":
                    tool_call_id = block.get("call_id", tool_call_id)
                block_input = block.get("input", {})
                if not block_input:
                    block_input = block.get("arguments", {})
                if isinstance(block_input, str):
                    try:
                        block_input = json.loads(block_input)
                    except json.JSONDecodeError:
                        block_input = {"arguments": block_input}
                if not block_input and tool_call_id in tc_args_by_id:
                    block_input = tc_args_by_id[tool_call_id]
                current_turn["steps"].append(
                    _tool_call_step(
                        tool_call_id=tool_call_id,
                        name=block.get("name", ""),
                        arguments=block_input,
                        tool_results=tool_results,
                        clean_tool_result=clean_tool_result,
                        extract_workspace_artifacts=extract_workspace_artifacts,
                    )
                )
    else:
        if text_content:
            current_turn["steps"].append({
                "type": "response",
                "content": text_content,
            })
        for tc in msg.tool_calls:
            tool_call_id = tc.get("id") or ""
            current_turn["steps"].append(
                _tool_call_step(
                    tool_call_id=tool_call_id,
                    name=tc.get("name", ""),
                    arguments=tc.get("args", {}),
                    tool_results=tool_results,
                    clean_tool_result=clean_tool_result,
                    extract_workspace_artifacts=extract_workspace_artifacts,
                )
            )


def _tool_call_step(
    *,
    tool_call_id: str,
    name: str,
    arguments: Any,
    tool_results: Dict[str, Any],
    clean_tool_result: ToolResultCleaner,
    extract_workspace_artifacts: WorkspaceArtifactExtractor,
) -> Dict[str, Any]:
    step = {
        "type": "tool_call",
        "id": tool_call_id,
        "name": name,
        "arguments": arguments,
        "status": "success",
    }
    if tool_call_id in tool_results:
        raw_tool_result = tool_results[tool_call_id]
        raw_tool_result_text = (
            raw_tool_result if isinstance(raw_tool_result, str) else str(raw_tool_result)
        )
        step["result"] = clean_tool_result(raw_tool_result_text)
        artifacts = extract_workspace_artifacts(raw_tool_result_text)
        if artifacts:
            step["artifacts"] = artifacts
    return step


def _populate_legacy_turn_fields(turn: Dict[str, Any]) -> None:
    if "steps" in turn and "intermediate_content" not in turn:
        turn["intermediate_content"] = "\n".join(
            s["content"] for s in turn["steps"] if s["type"] == "thinking"
        ) or None
        turn["tool_calls"] = [
            s for s in turn["steps"] if s["type"] == "tool_call"
        ]
