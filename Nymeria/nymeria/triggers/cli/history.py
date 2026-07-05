"""Projection helpers for loading API conversation history into CLI state."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .state import (
    AssistantMessage,
    CLIUIState,
    ResponseStep,
    SystemMessage,
    ThinkingStep,
    ToolCallStep,
    ToolReloadInfo,
    TranscriptMessage,
    UserMessage,
    WorkspaceArtifact,
    create_initial_state,
)


def cli_state_from_history(
    history: Any,
    *,
    thread_id: str | None,
    user_id: str = "default",
) -> CLIUIState:
    """Convert desktop/API history payloads into reducer-owned CLI state."""

    messages: list[TranscriptMessage] = []
    artifacts: list[WorkspaceArtifact] = []
    for index, raw in enumerate(_history_messages(history)):
        message = _message_from_history(raw, index=index)
        if message is None:
            continue
        messages.append(message)
        if isinstance(message, AssistantMessage):
            for step in message.steps:
                if isinstance(step, ToolCallStep):
                    artifacts.extend(step.artifacts)

    base = create_initial_state(thread_id=thread_id, user_id=user_id)
    latest_timestamp = max(
        (getattr(message, "timestamp", 0.0) for message in messages),
        default=base.updated_at,
    )
    return CLIUIState(
        thread_id=thread_id,
        user_id=user_id,
        messages=tuple(messages),
        turn_status="idle",
        artifacts=tuple(_dedupe_artifacts(artifacts)),
        updated_at=latest_timestamp,
    )


def _history_messages(history: Any) -> list[Mapping[str, Any]]:
    raw = history.get("messages", []) if isinstance(history, Mapping) else history
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _message_from_history(
    message: Mapping[str, Any],
    *,
    index: int,
) -> TranscriptMessage | None:
    role = str(message.get("role") or message.get("type") or "").casefold()
    if role in {"human", "user"}:
        return UserMessage(
            id=_message_id(message, index),
            content=_content_text(message.get("content")),
            attachments=tuple(_mapping_sequence(message.get("attachments"))),
            timestamp=_timestamp(message.get("timestamp")),
            status="complete",
            context_summary=str(message.get("context_summary") or ""),
        )
    if role in {"assistant", "ai"}:
        return _assistant_from_history(message, index=index)
    if role == "system":
        return SystemMessage(
            id=_message_id(message, index),
            kind=_system_kind(message),
            content=str(message.get("content") or ""),
            timestamp=_timestamp(message.get("timestamp")),
            status="complete",
            context_summary=str(message.get("context_summary") or ""),
            messages_removed=_int(message.get("messages_removed")),
            auto_resumed=bool(message.get("auto_resumed", False)),
            details=copy.deepcopy(dict(_mapping_or(message.get("details"), {}))),
        )
    return None


def _assistant_from_history(
    message: Mapping[str, Any],
    *,
    index: int,
) -> AssistantMessage:
    steps = _steps_from_history(message)
    content = _content_text(message.get("content"))
    if not steps and content:
        steps = (
            ResponseStep(
                content=content,
                started_at=_timestamp(message.get("timestamp")),
                updated_at=_timestamp(message.get("timestamp")),
            ),
        )
    tool_reload = _tool_reload_info(message.get("tool_reload_info"))
    return AssistantMessage(
        id=_message_id(message, index),
        content=content,
        steps=steps,
        intermediate_content=str(message.get("intermediate_content") or ""),
        timestamp=_timestamp(message.get("timestamp")),
        status="complete",
        tool_calls=tuple(step for step in steps if isinstance(step, ToolCallStep)),
        tool_reload_info=tool_reload,
        dispatch_info=copy.deepcopy(
            dict(_mapping_or(message.get("dispatch_info"), {}))
        ),
    )


def _steps_from_history(message: Mapping[str, Any]) -> tuple[Any, ...]:
    timestamp = _timestamp(message.get("timestamp"))
    raw_steps = message.get("steps")
    if not isinstance(raw_steps, Sequence) or isinstance(raw_steps, (str, bytes)):
        raw_steps = ()

    steps: list[Any] = []
    for step_index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, Mapping):
            continue
        step = _step_from_history(raw_step, timestamp=timestamp, index=step_index)
        if step is not None:
            steps.append(step)

    if steps:
        return tuple(steps)

    legacy_tool_calls = (
        message.get("tool_calls")
        or message.get("toolCalls")
        or message.get("toolcalls")
        or ()
    )
    if isinstance(legacy_tool_calls, Sequence) and not isinstance(
        legacy_tool_calls,
        (str, bytes),
    ):
        for tool_index, raw_tool in enumerate(legacy_tool_calls):
            if isinstance(raw_tool, Mapping):
                steps.append(
                    _tool_step_from_history(
                        raw_tool,
                        timestamp=timestamp,
                        index=tool_index,
                    )
                )
    return tuple(steps)


def _step_from_history(
    step: Mapping[str, Any],
    *,
    timestamp: float,
    index: int,
) -> Any | None:
    step_type = str(step.get("type") or "").casefold()
    if step_type == "thinking":
        return ThinkingStep(
            content=str(step.get("content") or ""),
            started_at=_timestamp(step.get("started_at"), default=timestamp),
            updated_at=_timestamp(step.get("updated_at"), default=timestamp),
        )
    if step_type == "response":
        return ResponseStep(
            content=_content_text(step.get("content")),
            started_at=_timestamp(step.get("started_at"), default=timestamp),
            updated_at=_timestamp(step.get("updated_at"), default=timestamp),
        )
    if step_type == "tool_call":
        return _tool_step_from_history(step, timestamp=timestamp, index=index)
    return None


def _tool_step_from_history(
    step: Mapping[str, Any],
    *,
    timestamp: float,
    index: int,
) -> ToolCallStep:
    function = _mapping_or(step.get("function"), {})
    tool_id = str(step.get("id") or step.get("tool_call_id") or f"tool-{index}")
    name = str(step.get("name") or function.get("name") or "")
    arguments = (
        step.get("arguments")
        if "arguments" in step
        else step.get("args", function.get("arguments", {}))
    )
    result = copy.deepcopy(step.get("result", ""))
    status = str(step.get("status") or ("success" if result not in (None, "") else "success"))
    raw_duration = step.get("duration_ms")
    duration_ms = (
        int(raw_duration)
        if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool)
        else None
    )
    return ToolCallStep(
        id=tool_id,
        name=name,
        arguments=_arguments_mapping(arguments),
        result=result,
        artifacts=tuple(_artifact_from_history(item) for item in _mapping_sequence(step.get("artifacts"))),
        status=_tool_status(status),
        started_at=_timestamp(step.get("started_at"), default=timestamp),
        updated_at=_timestamp(step.get("updated_at"), default=timestamp),
        ended_at=_timestamp(step.get("ended_at"), default=timestamp),
        duration_ms=duration_ms,
    )


def _tool_reload_info(value: Any) -> ToolReloadInfo | None:
    info = _mapping_or(value, {})
    if not info:
        return None
    tools = info.get("tools") or ()
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes)):
        tools = ()
    return ToolReloadInfo(
        tools=tuple(str(tool) for tool in tools),
        ttl=str(info.get("ttl") or ""),
        ttl_seconds=_optional_int(info.get("ttl_seconds")),
        source=str(info.get("source") or ""),
        skill_name=(
            str(info.get("skill_name"))
            if info.get("skill_name") is not None
            else None
        ),
        reason=str(info.get("reason") or ""),
    )


def _artifact_from_history(value: Mapping[str, Any]) -> WorkspaceArtifact:
    return WorkspaceArtifact(
        path=str(value.get("path") or ""),
        name=str(value.get("name") or ""),
        mime_type=str(value.get("mime_type") or value.get("mimeType") or ""),
        size_bytes=_optional_int(value.get("size_bytes", value.get("sizeBytes"))),
        payload=copy.deepcopy(dict(value)),
    )


def _dedupe_artifacts(artifacts: Sequence[WorkspaceArtifact]) -> list[WorkspaceArtifact]:
    seen: set[str] = set()
    selected: list[WorkspaceArtifact] = []
    for artifact in artifacts:
        key = artifact.path or artifact.name or json.dumps(
            artifact.payload,
            ensure_ascii=True,
            sort_keys=True,
            default=repr,
        )
        if key in seen:
            continue
        seen.add(key)
        selected.append(artifact)
    return selected


def _message_id(message: Mapping[str, Any], index: int) -> str:
    return str(message.get("id") or f"history-{index + 1}")


def _system_kind(message: Mapping[str, Any]) -> Any:
    kind = str(message.get("kind") or "").strip()
    if kind in {
        "compaction_notice",
        "context_attached",
        "dispatch_notice",
        "iteration_limit",
        "error",
        "tool_reload",
        "autonomous",
        "diagnostic",
    }:
        return kind
    content = str(message.get("content") or "").casefold()
    if "compact" in content:
        return "compaction_notice"
    return "diagnostic"


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        return str(content.get("text") or content.get("content") or "")
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            elif block is not None:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)


def _arguments_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {"arguments": value}
        if isinstance(parsed, Mapping):
            return copy.deepcopy(dict(parsed))
        return {"arguments": parsed}
    return {}


def _timestamp(value: Any, *, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return default
    return default


def _mapping_sequence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _mapping_or(value: Any, fallback: Mapping[str, Any]) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else fallback


def _int(value: Any) -> int:
    parsed = _optional_int(value)
    return parsed if parsed is not None else 0


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _tool_status(value: str) -> Any:
    normalized = value.casefold()
    if normalized in {"pending", "running", "success", "error", "cancelled"}:
        return normalized
    if normalized in {"ok", "complete", "completed"}:
        return "success"
    return "success"


__all__ = ["cli_state_from_history"]
