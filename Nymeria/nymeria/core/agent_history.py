"""Conversation history projection helpers for NymeriaAgent."""

from __future__ import annotations

import ast
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from .agent_text_extract import (  # noqa: F401  (re-export surface; several also used internally below)
    THINK_CLOSE,
    THINK_OPEN,
    InlineThinkingTextStripper,
    _walk_reasoning_value,
    extract_content_parts,
    extract_mime_from_data_url,
    extract_reasoning_parts,
    extract_reasoning_text_from_block,
    extract_reasoning_text_from_details,
    strip_inline_thinking_text,
    trailing_marker_prefix_length,
)

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


# Sentinel wrapping hook-injected PROMPT_SUBMIT context on the message tail.
# Injected context persists in the checkpoint (like the time prefix) but is
# stripped from display/RAG so it does not pollute history views or the index.
HOOK_CONTEXT_OPEN = "<hook_context>"
HOOK_CONTEXT_CLOSE = "</hook_context>"
HOOK_CONTEXT_PATTERN = re.compile(
    r"\n*<hook_context>\n.*?\n</hook_context>",
    re.DOTALL,
)


def wrap_hook_context(text: str) -> str:
    """Wrap hook-injected turn context in the strippable sentinel."""
    return f"{HOOK_CONTEXT_OPEN}\n{text}\n{HOOK_CONTEXT_CLOSE}"


def strip_prompt_context(content: str) -> str:
    """Strip injected time/trigger metadata and hook context from a user message.

    Both persist in the checkpoint (so they reach the model in-turn) but are
    turn-local noise in history views and the RAG index, so display and pre-trim
    flush strip them here.
    """
    content = CONTEXT_PREFIX_PATTERN.sub("", content)
    content = HOOK_CONTEXT_PATTERN.sub("", content)
    return content


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
                    logger.debug("[Timestamps] Skipped a state with no readable messages", exc_info=True)
                    continue
                for msg in msgs:
                    try:
                        mid = getattr(msg, "id", None)
                        if mid:
                            timestamp_map[mid] = ts
                    except Exception:
                        logger.debug("[Timestamps] Skipped a message with no readable id", exc_info=True)
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
                logger.debug("[Timestamps] Skipped a state with unreadable message ids", exc_info=True)
                continue

            for mid in still_active & msg_ids_here:
                timestamp_map[mid] = ts

            aged_out = still_active - msg_ids_here
            if aged_out:
                still_active -= aged_out
    except Exception as e:
        logger.warning("[Timestamps] Failed to build checkpoint map for thread %s: %s", thread_id, e)

    return timestamp_map


def classify_autonomous_source(text: str) -> str:
    """Classify the source of an autonomous wakeup from its stripped prompt."""
    if text.startswith("Work on TODO "):
        return "scheduler"
    if text.startswith("[WATCHDOG ALERT]"):
        return "watchdog"
    return "trigger"


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
    # When True, autonomous wakeups the show_autonomous_prompts filter would
    # drop are emitted as invisible stub entries (hidden: true, message_id
    # only) so live-attach viewers can anchor-trim precisely (backlog #90).
    include_hidden_anchors: bool = False
    tool_timings: Dict[str, Any] = field(default_factory=dict)
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
    include_hidden_anchors: bool = False,
    clean_tool_result: Optional[ToolResultCleaner] = None,
    extract_workspace_artifacts: Optional[WorkspaceArtifactExtractor] = None,
) -> List[Dict[str, Any]]:
    """Format LangChain checkpoint messages for frontend display."""
    timestamp_map = timestamp_map or {}
    clean_tool_result = clean_tool_result or _default_clean_tool_result
    extract_workspace_artifacts = extract_workspace_artifacts or _default_extract_workspace_artifacts

    # Hidden-anchor stubs only make sense when the internal filter runs;
    # include_internal renders wakeups in full already.
    include_hidden_anchors = include_hidden_anchors and not include_internal

    if not include_internal:
        messages = _filter_internal_messages(
            messages,
            show_autonomous_prompts=show_autonomous_prompts,
            include_hidden_anchors=include_hidden_anchors,
        )

    tool_results: Dict[str, Any] = {}
    tool_timings: Dict[str, Any] = {}
    for msg in messages:
        if isinstance(msg, ToolMessage):
            tool_results[msg.tool_call_id] = msg.content
            timing = (msg.additional_kwargs or {}).get("tool_timing")
            if isinstance(timing, dict):
                tool_timings[msg.tool_call_id] = timing

    ctx = _HistoryFormatContext(
        thread_id=thread_id,
        timestamp_map=timestamp_map,
        show_autonomous_prompts=show_autonomous_prompts,
        show_prompt_metadata=show_prompt_metadata,
        tool_results=tool_results,
        clean_tool_result=clean_tool_result,
        extract_workspace_artifacts=extract_workspace_artifacts,
        include_hidden_anchors=include_hidden_anchors,
        tool_timings=tool_timings,
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

    if (
        ctx.include_hidden_anchors
        and not ctx.show_autonomous_prompts
        and msg.additional_kwargs.get("internal_type") == "autonomous_wakeup"
    ):
        # Present only for anchoring: the show_autonomous_prompts filter
        # would have dropped this wakeup, so emit an invisible stub carrying
        # the graph message id. Clients render nothing for hidden entries
        # (and exclude them from rewind/edit targets); live-attach viewers
        # trim after the stub exactly as they would after a visible anchor.
        ctx.flush_current_turn()
        stub: Dict[str, Any] = {
            "id": ctx.next_entry_id(),
            "role": "user",
            "hidden": True,
            "content": "",
        }
        if msg.id:
            stub["message_id"] = msg.id
        ctx.history.append(stub)
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
        entry["content"] = strip_prompt_context(raw_content)
    if attachments:
        entry["attachments"] = attachments
    if timestamp_iso:
        entry["timestamp"] = timestamp_iso

    if (
        ctx.show_autonomous_prompts
        and msg.additional_kwargs.get("internal_type") == "autonomous_wakeup"
    ):
        entry["autonomous_source"] = classify_autonomous_source(entry["content"])

    # Expose the underlying LangGraph message id so clients can target this
    # exact prompt with POST /threads/{id}/rewind to_message_id (the entry
    # "id" above is a synthetic per-render counter, not a stable graph id).
    if msg.id:
        entry["message_id"] = msg.id

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
            tool_timings=ctx.tool_timings,
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
    include_hidden_anchors: bool = False,
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
                    skip_until_next_human = False
                    # include_hidden_anchors keeps an otherwise-filtered
                    # wakeup in the stream; the human handler renders it as
                    # an invisible stub so live-attach viewers can trim at
                    # its message_id (backlog #90).
                    if show_autonomous_prompts or include_hidden_anchors:
                        filtered_messages.append(msg)
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
        logger.debug("Failed to parse human content; returning text-only fallback", exc_info=True)
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
    tool_timings: Optional[Dict[str, Any]] = None,
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
            current_turn["steps"].extend(
                _content_block_to_steps(
                    block,
                    tc_args_by_id,
                    tool_results=tool_results,
                    clean_tool_result=clean_tool_result,
                    extract_workspace_artifacts=extract_workspace_artifacts,
                    tool_timings=tool_timings,
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
                    tool_timings=tool_timings,
                )
            )


def _content_block_to_steps(
    block: Any,
    tc_args_by_id: Dict[str, Any],
    *,
    tool_results: Dict[str, Any],
    clean_tool_result: ToolResultCleaner,
    extract_workspace_artifacts: WorkspaceArtifactExtractor,
    tool_timings: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Project a single AIMessage content block into history step dicts.

    Returns zero, one, or (for ``reasoning`` blocks) several step dicts. Non-dict
    string blocks render as response text; unknown block types yield no steps.
    """
    if not isinstance(block, dict):
        if isinstance(block, str) and block:
            text = strip_inline_thinking_text(block)
            if text:
                return [{"type": "response", "content": text}]
        return []

    block_type = block.get("type")
    if block_type == "thinking":
        thinking_text = block.get("thinking", "")
        if thinking_text:
            return [{"type": "thinking", "content": thinking_text}]
        return []
    if block_type == "reasoning":
        return thinking_steps(extract_reasoning_text_from_block(block))
    if block_type in ("text", "output_text"):
        text = strip_inline_thinking_text(block.get("text", ""))
        if text:
            return [{"type": "response", "content": text}]
        return []
    if block_type in ("tool_use", "function_call", "custom_tool_call"):
        return [
            _tool_call_block_to_step(
                block,
                tc_args_by_id,
                tool_results=tool_results,
                clean_tool_result=clean_tool_result,
                extract_workspace_artifacts=extract_workspace_artifacts,
                tool_timings=tool_timings,
            )
        ]
    return []


def _tool_call_block_to_step(
    block: Dict[str, Any],
    tc_args_by_id: Dict[str, Any],
    *,
    tool_results: Dict[str, Any],
    clean_tool_result: ToolResultCleaner,
    extract_workspace_artifacts: WorkspaceArtifactExtractor,
    tool_timings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a tool_call step from a tool_use/function_call/custom_tool_call block.

    Precondition: ``block`` is one of those three content-block types. Resolves
    the call id (``id``, or ``call_id`` for the non-``tool_use`` shapes) and the
    arguments (trying ``input``, then ``arguments``, then a JSON-decoded string,
    then the matching ``tool_calls`` entry), then delegates to ``_tool_call_step``.
    """
    block_type = block.get("type")
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
    return _tool_call_step(
        tool_call_id=tool_call_id,
        name=block.get("name", ""),
        arguments=block_input,
        tool_results=tool_results,
        clean_tool_result=clean_tool_result,
        extract_workspace_artifacts=extract_workspace_artifacts,
        tool_timings=tool_timings,
    )


def _tool_call_step(
    *,
    tool_call_id: str,
    name: str,
    arguments: Any,
    tool_results: Dict[str, Any],
    clean_tool_result: ToolResultCleaner,
    extract_workspace_artifacts: WorkspaceArtifactExtractor,
    tool_timings: Optional[Dict[str, Any]] = None,
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
    _apply_tool_timing_to_step(step, (tool_timings or {}).get(tool_call_id))
    return step


def _apply_tool_timing_to_step(step: Dict[str, Any], timing: Any) -> None:
    """Surface SafeToolNode's checkpointed tool_timing stamp on a history step.

    Emits camelCase ``startTime``/``endTime`` ISO strings (the keys the
    desktop/mobile history normalizers already parse) plus ``duration_ms``
    for other API consumers. Best-effort: malformed stamps are ignored.
    """
    if not isinstance(timing, dict):
        return
    started_iso = timing.get("started_at")
    duration_ms = timing.get("duration_ms")
    if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool):
        step["duration_ms"] = int(duration_ms)
    if isinstance(started_iso, str) and started_iso:
        step["startTime"] = started_iso
        if "duration_ms" in step:
            try:
                started_dt = datetime.fromisoformat(started_iso)
                step["endTime"] = (
                    started_dt + timedelta(milliseconds=step["duration_ms"])
                ).isoformat()
            except ValueError:
                pass  # unparseable startTime: clients fall back to duration_ms


def _populate_legacy_turn_fields(turn: Dict[str, Any]) -> None:
    if "steps" in turn and "intermediate_content" not in turn:
        turn["intermediate_content"] = "\n".join(
            s["content"] for s in turn["steps"] if s["type"] == "thinking"
        ) or None
        turn["tool_calls"] = [
            s for s in turn["steps"] if s["type"] == "tool_call"
        ]
