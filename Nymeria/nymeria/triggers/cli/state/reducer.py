"""Reducer for normalized CLI stream events."""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import replace
from pathlib import PurePath
from typing import Any

from ..events import (
    CompactingEvent,
    CompactedEvent,
    ContextAttachedEvent,
    DiagnosticEvent,
    DispatchedEvent,
    DoneEvent,
    ErrorEvent,
    IterationLimitEvent,
    NormalizedEvent,
    QueuedEvent,
    ResponseEvent,
    TaskCompletedEvent,
    TaskStartedEvent,
    ThinkingEvent,
    ToolCallDeltaEvent,
    ToolCallEvent,
    ToolReloadEvent,
    ToolResultEvent,
    WorkspaceArtifactEvent,
    normalize_stream_event,
)
from .model import (
    AssistantActivityPhase,
    AssistantMessage,
    CLIUIState,
    CompactResult,
    DiagnosticNotice,
    ErrorNotice,
    MessageStatus,
    MessageStep,
    QueueState,
    ResponseStep,
    SessionTokenUsage,
    SystemMessage,
    ThinkingStep,
    ToolCallStatus,
    ToolCallStep,
    ToolReloadInfo,
    TranscriptMessage,
    UserMessage,
    WorkspaceArtifact,
)
from .selectors import (
    select_intermediate_content,
    select_response_content,
    select_tool_calls,
)


def create_initial_state(
    *,
    thread_id: str | None = None,
    user_id: str = "default",
    now: float | None = None,
) -> CLIUIState:
    """Create an empty reducer state."""

    timestamp = _timestamp(now)
    return CLIUIState(thread_id=thread_id, user_id=user_id, updated_at=timestamp)


def start_turn(
    state: CLIUIState,
    message: str,
    *,
    thread_id: str | None = None,
    user_id: str | None = None,
    attachments: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None = None,
    now: float | None = None,
    user_message_id: str | None = None,
    assistant_message_id: str | None = None,
) -> CLIUIState:
    """Append a user message and empty streaming assistant turn."""

    timestamp = _timestamp(now)
    selected_thread_id = thread_id or state.thread_id
    selected_user_id = user_id or state.user_id
    user_message = UserMessage(
        id=user_message_id or _new_id("user"),
        content=message,
        attachments=tuple(copy.deepcopy(list(attachments or []))),
        timestamp=timestamp,
    )
    assistant_message = AssistantMessage(
        id=assistant_message_id or _new_id("assistant"),
        timestamp=timestamp,
        activity_updated_at=timestamp,
    )
    return replace(
        state,
        thread_id=selected_thread_id,
        user_id=selected_user_id,
        messages=state.messages + (user_message, assistant_message),
        turn_status="submitting",
        current_assistant_id=assistant_message.id,
        is_queued=False,
        queue=None,
        active_tool_calls={},
        tool_call_delta_buffer="",
        updated_at=timestamp,
    )


def mark_cancelling(state: CLIUIState, *, now: float | None = None) -> CLIUIState:
    """Move the active turn into the cancelling lifecycle state."""

    timestamp = _timestamp(now)
    state = _update_running_tools(state, "cancelled", timestamp)
    return replace(
        state,
        turn_status="cancelling",
        is_queued=False,
        queue=None,
        updated_at=timestamp,
    )


def reduce_events(
    state: CLIUIState,
    events: list[Any] | tuple[Any, ...],
    *,
    now: float | None = None,
) -> CLIUIState:
    """Apply a sequence of normalized or raw stream events."""

    next_state = state
    timestamp = now
    for event in events:
        next_state = reduce_stream_event(next_state, event, now=timestamp)
    return next_state


def reduce_stream_event(
    state: CLIUIState,
    event: Any,
    *,
    now: float | None = None,
) -> CLIUIState:
    """Apply one normalized stream event to the immutable CLI state."""

    normalized = _normalize(event, default_thread_id=state.thread_id)
    timestamp = _timestamp(now)
    state = _with_event_thread(state, normalized, timestamp)

    if isinstance(normalized, QueuedEvent):
        return _reduce_queued(state, normalized, timestamp)
    if isinstance(normalized, ThinkingEvent):
        return _reduce_thinking(state, normalized, timestamp)
    if isinstance(normalized, ToolCallDeltaEvent):
        return _reduce_tool_call_delta(state, normalized, timestamp)
    if isinstance(normalized, ToolCallEvent):
        return _reduce_tool_call(state, normalized, timestamp)
    if isinstance(normalized, ToolResultEvent):
        return _reduce_tool_result(state, normalized, timestamp)
    if isinstance(normalized, WorkspaceArtifactEvent):
        return _reduce_workspace_artifact(state, normalized, timestamp)
    if isinstance(normalized, ResponseEvent):
        return _reduce_response(state, normalized, timestamp)
    if isinstance(normalized, CompactingEvent):
        return _reduce_compacting(state, normalized, timestamp)
    if isinstance(normalized, CompactedEvent):
        return _reduce_compacted(state, normalized, timestamp)
    if isinstance(normalized, ContextAttachedEvent):
        return _reduce_context_attached(state, normalized, timestamp)
    if isinstance(normalized, IterationLimitEvent):
        return _reduce_iteration_limit(state, normalized, timestamp)
    if isinstance(normalized, TaskStartedEvent):
        return _reduce_task_started(state, normalized, timestamp)
    if isinstance(normalized, TaskCompletedEvent):
        return _reduce_task_completed(state, normalized, timestamp)
    if isinstance(normalized, ToolReloadEvent):
        return _reduce_tool_reload(state, normalized, timestamp)
    if isinstance(normalized, DispatchedEvent):
        return _reduce_dispatched(state, normalized, timestamp)
    if isinstance(normalized, ErrorEvent):
        return _reduce_error(state, normalized, timestamp)
    if isinstance(normalized, DoneEvent):
        return _reduce_done(state, normalized, timestamp)
    if isinstance(normalized, DiagnosticEvent):
        return _reduce_diagnostic(state, normalized, timestamp)

    return state


def _reduce_queued(
    state: CLIUIState,
    event: QueuedEvent,
    timestamp: float,
) -> CLIUIState:
    return replace(
        state,
        turn_status="queued",
        is_queued=True,
        queue=QueueState(
            message=event.message,
            holder=event.holder,
            held_seconds=event.held_seconds,
            updated_at=timestamp,
        ),
        updated_at=timestamp,
    )


def _reduce_thinking(
    state: CLIUIState,
    event: ThinkingEvent,
    timestamp: float,
) -> CLIUIState:
    state = _ensure_streaming_assistant(state, timestamp, phase="thinking")
    return _update_last_assistant(
        state,
        lambda message: _with_appended_thinking(message, event.content, timestamp),
        timestamp,
        turn_status="streaming",
    )


def _reduce_tool_call_delta(
    state: CLIUIState,
    event: ToolCallDeltaEvent,
    timestamp: float,
) -> CLIUIState:
    state = _ensure_streaming_assistant(state, timestamp, phase="processing")
    return replace(
        state,
        turn_status="streaming",
        is_queued=False,
        queue=None,
        tool_call_delta_buffer=state.tool_call_delta_buffer + event.content,
        updated_at=timestamp,
    )


def _reduce_tool_call(
    state: CLIUIState,
    event: ToolCallEvent,
    timestamp: float,
) -> CLIUIState:
    state = _ensure_streaming_assistant(state, timestamp, phase="waiting")
    tool_id = event.id or _new_id(event.name or "tool")
    step = ToolCallStep(
        id=tool_id,
        name=event.name,
        arguments=copy.deepcopy(event.args),
        status="running",
        started_at=timestamp,
        updated_at=timestamp,
    )
    active = dict(state.active_tool_calls)
    active[tool_id] = step

    state = _update_last_assistant(
        state,
        lambda message: _with_tool_call(message, step, timestamp),
        timestamp,
        active_tool_calls=active,
        turn_status="streaming",
    )
    return replace(state, tool_call_delta_buffer="")


def _reduce_tool_result(
    state: CLIUIState,
    event: ToolResultEvent,
    timestamp: float,
) -> CLIUIState:
    state = _ensure_streaming_assistant(state, timestamp, phase="processing_results")
    tool_id = event.id or _find_running_tool_id_by_name(state, event.name)
    tool_id = tool_id or _new_id(event.name or "tool-result")
    status = _normalize_tool_status(event.status)
    existing = state.active_tool_calls.get(tool_id)
    step = ToolCallStep(
        id=tool_id,
        name=event.name or (existing.name if existing else ""),
        arguments=copy.deepcopy(existing.arguments if existing else {}),
        result=copy.deepcopy(event.result),
        artifacts=existing.artifacts if existing else (),
        status=status,
        started_at=existing.started_at if existing else timestamp,
        updated_at=timestamp,
        ended_at=timestamp,
    )
    active = dict(state.active_tool_calls)
    active[tool_id] = step

    return _update_last_assistant(
        replace(state, active_tool_calls=active),
        lambda message: _with_tool_result(message, step, timestamp),
        timestamp,
        turn_status="streaming",
    )


def _reduce_workspace_artifact(
    state: CLIUIState,
    event: WorkspaceArtifactEvent,
    timestamp: float,
) -> CLIUIState:
    artifact = _artifact_from_event(event)
    artifacts = _merge_artifact(state.artifacts, artifact)
    tool_id = event.tool_call_id or _find_tool_id_by_name(state, event.tool_name)

    active = dict(state.active_tool_calls)
    if tool_id and tool_id in active:
        active[tool_id] = replace(
            active[tool_id],
            artifacts=_merge_artifact(active[tool_id].artifacts, artifact),
            updated_at=timestamp,
        )

    if tool_id:
        state = _update_last_assistant(
            replace(state, artifacts=artifacts, active_tool_calls=active),
            lambda message: _with_tool_artifact(
                message,
                tool_id,
                artifact,
                timestamp,
            ),
            timestamp,
        )
        return state

    return replace(state, artifacts=artifacts, updated_at=timestamp)


def _reduce_response(
    state: CLIUIState,
    event: ResponseEvent,
    timestamp: float,
) -> CLIUIState:
    state = _ensure_streaming_assistant(state, timestamp, phase="typing")
    return _update_last_assistant(
        state,
        lambda message: _with_appended_response(message, event.content, timestamp),
        timestamp,
        turn_status="streaming",
    )


def _reduce_compacting(
    state: CLIUIState,
    event: CompactingEvent,
    timestamp: float,
) -> CLIUIState:
    return replace(
        state,
        is_compacting=True,
        compacting_message=event.message,
        updated_at=timestamp,
    )


def _reduce_compacted(
    state: CLIUIState,
    event: CompactedEvent,
    timestamp: float,
) -> CLIUIState:
    notice = SystemMessage(
        id=_new_id("system"),
        kind="compaction_notice",
        content="Context compacted",
        context_summary=event.summary,
        messages_removed=event.messages_removed,
        auto_resumed=event.auto_resumed,
        timestamp=timestamp,
    )
    messages: tuple[TranscriptMessage, ...] = (notice,)
    current_assistant_id = None
    turn_status = "complete"
    if event.auto_resumed:
        assistant = AssistantMessage(
            id=_new_id("assistant"),
            timestamp=timestamp,
            activity_updated_at=timestamp,
        )
        messages = (notice, assistant)
        current_assistant_id = assistant.id
        turn_status = "streaming"

    return replace(
        state,
        messages=messages,
        turn_status=turn_status,
        current_assistant_id=current_assistant_id,
        is_queued=False,
        queue=None,
        is_compacting=False,
        compacting_message="",
        last_compact_result=CompactResult(
            summary=event.summary,
            messages_removed=event.messages_removed,
            auto_resumed=event.auto_resumed,
            timestamp=timestamp,
        ),
        active_tool_calls={},
        updated_at=timestamp,
    )


def _reduce_context_attached(
    state: CLIUIState,
    event: ContextAttachedEvent,
    timestamp: float,
) -> CLIUIState:
    messages = list(state.messages)
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, UserMessage):
            messages[index] = replace(message, context_summary=event.summary)
            break
    return replace(
        state,
        messages=tuple(messages),
        context_attached_message=event.summary,
        updated_at=timestamp,
    )


def _reduce_iteration_limit(
    state: CLIUIState,
    event: IterationLimitEvent,
    timestamp: float,
) -> CLIUIState:
    content = event.content or "Reached turn safety limit."
    notice = SystemMessage(
        id=_new_id("system"),
        kind="iteration_limit",
        content=content,
        timestamp=timestamp,
        details={
            "scope": event.scope,
            "reason": event.reason,
            "max_iterations": event.max_iterations,
            "tool_call_count": event.tool_call_count,
            "agent_name": event.agent_name,
            "repeated_tool_name": event.repeated_tool_name,
            "repeated_count": event.repeated_count,
        },
    )
    return replace(
        state,
        messages=state.messages + (notice,),
        updated_at=timestamp,
    )


def _reduce_task_started(
    state: CLIUIState,
    event: TaskStartedEvent,
    timestamp: float,
) -> CLIUIState:
    notice = SystemMessage(
        id=_new_id("system"),
        kind="autonomous",
        content=_autonomous_started_content(event),
        timestamp=timestamp,
        details={
            "task_id": event.task_id,
            "todo_id": event.todo_id,
            "source": event.source,
        },
    )
    assistant = AssistantMessage(
        id=_new_id("assistant"),
        timestamp=timestamp,
        activity_phase="processing",
        activity_updated_at=timestamp,
    )
    return replace(
        state,
        messages=state.messages + (notice, assistant),
        current_assistant_id=assistant.id,
        turn_status="streaming",
        is_queued=False,
        queue=None,
        active_tool_calls={},
        tool_call_delta_buffer="",
        updated_at=timestamp,
    )


def _reduce_task_completed(
    state: CLIUIState,
    event: TaskCompletedEvent,
    timestamp: float,
) -> CLIUIState:
    state = _ensure_streaming_assistant(state, timestamp, phase="typing")
    content = event.content or (
        f"Task failed: {event.error_message}" if event.error_message else ""
    )
    if content:
        state = _update_last_assistant(
            state,
            lambda message: _with_completion_content(message, content, timestamp),
            timestamp,
        )

    message_status: MessageStatus = "error" if event.error else "complete"
    state = _update_running_tools(state, "error" if event.error else "success", timestamp)
    errors = state.errors
    if event.error:
        errors = errors + (
            ErrorNotice(
                content=event.error_message or event.content or "Autonomous task failed.",
                code="autonomous_task_error",
                details={
                    "task_id": event.task_id,
                    "todo_id": event.todo_id,
                },
                timestamp=timestamp,
            ),
        )
    return _update_last_assistant(
        state,
        lambda message: _finalize_assistant(message, message_status),
        timestamp,
        turn_status="error" if event.error else "complete",
        is_queued=False,
        queue=None,
        active_tool_calls={},
        tool_call_delta_buffer="",
        errors=errors,
    )


def _reduce_tool_reload(
    state: CLIUIState,
    event: ToolReloadEvent,
    timestamp: float,
) -> CLIUIState:
    info = ToolReloadInfo(
        tools=event.tools,
        ttl=event.ttl,
        ttl_seconds=event.ttl_seconds,
        source=event.source,
        skill_name=event.skill_name,
        reason=event.reason,
    )
    if _last_assistant_index(state.messages) is not None:
        return _update_last_assistant(
            state,
            lambda message: replace(message, tool_reload_info=info),
            timestamp,
        )

    notice = SystemMessage(
        id=_new_id("system"),
        kind="tool_reload",
        content="Tools reloaded",
        timestamp=timestamp,
        details={
            "tools": event.tools,
            "ttl": event.ttl,
            "ttl_seconds": event.ttl_seconds,
            "source": event.source,
            "skill_name": event.skill_name,
            "reason": event.reason,
        },
    )
    return replace(state, messages=state.messages + (notice,), updated_at=timestamp)


def _reduce_dispatched(
    state: CLIUIState,
    event: DispatchedEvent,
    timestamp: float,
) -> CLIUIState:
    title = event.title or event.target_thread_id or "thread"
    short_id = event.target_thread_id[:8] if event.target_thread_id else ""
    suffix = f" ({short_id})" if short_id and short_id != title else ""
    dispatch_info = {
        "content": f"Response from {title}{suffix}",
        "thread_id": event.target_thread_id,
        "title": event.title,
        "original_thread_id": event.original_thread_id,
        "matched_ref": event.matched_ref,
    }
    return _update_last_assistant(
        state,
        lambda assistant: replace(
            assistant,
            dispatch_info={
                **assistant.dispatch_info,
                **dispatch_info,
            },
        ),
        timestamp,
    )


def _reduce_error(
    state: CLIUIState,
    event: ErrorEvent,
    timestamp: float,
) -> CLIUIState:
    state = _ensure_streaming_assistant(state, timestamp, phase="processing")
    notice = ErrorNotice(
        content=event.content,
        code=event.code,
        details=copy.deepcopy(event.details),
        timestamp=timestamp,
    )
    if event.code == "cancelled":
        state = _update_running_tools(state, "cancelled", timestamp)
        turn_status = "cancelling"
        message_status: MessageStatus = "complete"
    else:
        turn_status = "error"
        message_status = "error"

    content = event.content or "Unknown error"
    state = _update_last_assistant(
        state,
        lambda message: _with_error_response(message, content, timestamp, message_status),
        timestamp,
        turn_status=turn_status,
        errors=state.errors + (notice,),
        is_queued=False,
        queue=None,
    )
    return state


def _reduce_done(
    state: CLIUIState,
    event: DoneEvent,
    timestamp: float,
) -> CLIUIState:
    if event.status == "cancelled":
        state = _update_running_tools(state, "cancelled", timestamp)

    is_dispatched = bool(event.dispatched_to)
    context_stats = (
        state.context_stats
        if is_dispatched
        else copy.deepcopy(event.context_stats)
        if event.context_stats
        else state.context_stats
    )
    active_model = state.active_model if is_dispatched else event.model or state.active_model
    turn_status = "error" if state.turn_status == "error" else "complete"
    message_status: MessageStatus = "error" if state.turn_status == "error" else "complete"

    session_usage = (
        state.session_usage
        if is_dispatched
        else _accumulate_session_usage(
            state.session_usage,
            context_stats,
            active_model,
        )
    )

    state = _update_last_assistant(
        state,
        lambda message: _finalize_assistant(message, message_status),
        timestamp,
        turn_status=turn_status,
        is_queued=False,
        queue=None,
        is_compacting=False,
        compacting_message="",
        context_stats=context_stats,
        active_model=active_model,
        tool_call_count=event.tool_call_count,
        session_usage=session_usage,
    )
    return state


def _reduce_diagnostic(
    state: CLIUIState,
    event: DiagnosticEvent,
    timestamp: float,
) -> CLIUIState:
    notice = DiagnosticNotice(
        source_type=event.source_type,
        message=event.message,
        payload=copy.deepcopy(event.payload),
        timestamp=timestamp,
    )
    return replace(
        state,
        diagnostics=state.diagnostics + (notice,),
        updated_at=timestamp,
    )


def _with_event_thread(
    state: CLIUIState,
    event: NormalizedEvent,
    timestamp: float,
) -> CLIUIState:
    if event.thread_id and event.thread_id != state.thread_id:
        return replace(state, thread_id=event.thread_id, updated_at=timestamp)
    return state


def _ensure_streaming_assistant(
    state: CLIUIState,
    timestamp: float,
    *,
    phase: AssistantActivityPhase,
) -> CLIUIState:
    index = _last_assistant_index(state.messages)
    if index is None or not isinstance(state.messages[index], AssistantMessage):
        assistant = AssistantMessage(
            id=_new_id("assistant"),
            timestamp=timestamp,
            activity_phase=phase,
            activity_updated_at=timestamp,
        )
        return replace(
            state,
            messages=state.messages + (assistant,),
            current_assistant_id=assistant.id,
            turn_status="streaming",
            is_queued=False,
            queue=None,
            updated_at=timestamp,
        )

    message = state.messages[index]
    assert isinstance(message, AssistantMessage)
    if message.status == "streaming" and message.activity_phase == phase:
        return replace(
            state,
            turn_status="streaming",
            is_queued=False,
            queue=None,
            updated_at=timestamp,
        )

    updated = replace(
        message,
        status="streaming",
        activity_phase=phase,
        activity_updated_at=timestamp,
    )
    return _replace_message(
        state,
        index,
        updated,
        timestamp,
        turn_status="streaming",
        is_queued=False,
        queue=None,
        current_assistant_id=updated.id,
    )


def _update_last_assistant(
    state: CLIUIState,
    updater,
    timestamp: float,
    **state_updates: Any,
) -> CLIUIState:
    index = _last_assistant_index(state.messages)
    if index is None:
        state = _ensure_streaming_assistant(state, timestamp, phase="processing")
        index = _last_assistant_index(state.messages)
    assert index is not None
    message = state.messages[index]
    assert isinstance(message, AssistantMessage)
    updated = updater(message)
    return _replace_message(state, index, updated, timestamp, **state_updates)


def _replace_message(
    state: CLIUIState,
    index: int,
    message: TranscriptMessage,
    timestamp: float,
    **state_updates: Any,
) -> CLIUIState:
    messages = (
        state.messages[:index]
        + (message,)
        + state.messages[index + 1 :]
    )
    return replace(
        state,
        messages=messages,
        updated_at=timestamp,
        **state_updates,
    )


def _with_appended_thinking(
    message: AssistantMessage,
    content: str,
    timestamp: float,
) -> AssistantMessage:
    steps = list(message.steps)
    if steps and isinstance(steps[-1], ThinkingStep):
        last = steps[-1]
        steps[-1] = replace(
            last,
            content=last.content + content,
            updated_at=timestamp,
        )
    else:
        steps.append(
            ThinkingStep(
                content=content,
                started_at=timestamp,
                updated_at=timestamp,
            )
        )
    next_message = replace(
        message,
        steps=tuple(steps),
        activity_phase="thinking",
        activity_updated_at=timestamp,
    )
    return _with_derived_fields(next_message)


def _with_appended_response(
    message: AssistantMessage,
    content: str,
    timestamp: float,
) -> AssistantMessage:
    steps = list(message.steps)
    if steps and isinstance(steps[-1], ResponseStep):
        last = steps[-1]
        steps[-1] = replace(
            last,
            content=last.content + content,
            updated_at=timestamp,
        )
    else:
        steps.append(
            ResponseStep(
                content=content,
                started_at=timestamp,
                updated_at=timestamp,
            )
        )
    next_message = replace(
        message,
        steps=tuple(steps),
        activity_phase="typing",
        activity_updated_at=timestamp,
    )
    return _with_derived_fields(next_message)


def _with_tool_call(
    message: AssistantMessage,
    step: ToolCallStep,
    timestamp: float,
) -> AssistantMessage:
    steps = [existing for existing in message.steps if not _same_tool(existing, step.id)]
    steps.append(step)
    next_message = replace(
        message,
        steps=tuple(steps),
        activity_phase="waiting",
        activity_updated_at=timestamp,
    )
    return _with_derived_fields(next_message)


def _with_tool_result(
    message: AssistantMessage,
    step: ToolCallStep,
    timestamp: float,
) -> AssistantMessage:
    steps: list[MessageStep] = []
    replaced = False
    for existing in message.steps:
        if _same_tool(existing, step.id):
            steps.append(step)
            replaced = True
        else:
            steps.append(existing)
    if not replaced:
        steps.append(step)
    running = any(
        isinstance(existing, ToolCallStep) and existing.status == "running"
        for existing in steps
    )
    next_message = replace(
        message,
        steps=tuple(steps),
        activity_phase="waiting" if running else "processing_results",
        activity_updated_at=timestamp,
    )
    return _with_derived_fields(next_message)


def _with_tool_artifact(
    message: AssistantMessage,
    tool_id: str,
    artifact: WorkspaceArtifact,
    timestamp: float,
) -> AssistantMessage:
    steps: list[MessageStep] = []
    for existing in message.steps:
        if _same_tool(existing, tool_id) and isinstance(existing, ToolCallStep):
            steps.append(
                replace(
                    existing,
                    artifacts=_merge_artifact(existing.artifacts, artifact),
                    updated_at=timestamp,
                )
            )
        else:
            steps.append(existing)
    return _with_derived_fields(replace(message, steps=tuple(steps)))


def _with_error_response(
    message: AssistantMessage,
    content: str,
    timestamp: float,
    status: MessageStatus,
) -> AssistantMessage:
    prefix = "\n\n---\nError: "
    step_text = f"{prefix}{content}"
    next_message = _with_appended_response(message, step_text, timestamp)
    return replace(next_message, status=status)


def _with_completion_content(
    message: AssistantMessage,
    content: str,
    timestamp: float,
) -> AssistantMessage:
    existing = select_response_content(message)
    if not existing:
        return _with_appended_response(message, content, timestamp)
    if content.startswith(existing):
        suffix = content[len(existing) :]
        if suffix:
            return _with_appended_response(message, suffix, timestamp)
    return message


def _finalize_assistant(
    message: AssistantMessage,
    status: MessageStatus,
) -> AssistantMessage:
    return _with_derived_fields(replace(message, status=status))


def _with_derived_fields(message: AssistantMessage) -> AssistantMessage:
    return replace(
        message,
        content=select_response_content(message),
        intermediate_content=select_intermediate_content(message),
        tool_calls=select_tool_calls(message),
    )


def _update_running_tools(
    state: CLIUIState,
    status: ToolCallStatus,
    timestamp: float,
) -> CLIUIState:
    active = {
        tool_id: (
            replace(call, status=status, updated_at=timestamp, ended_at=timestamp)
            if call.status == "running"
            else call
        )
        for tool_id, call in state.active_tool_calls.items()
    }

    def update_message(message: AssistantMessage) -> AssistantMessage:
        steps: list[MessageStep] = []
        for step in message.steps:
            if isinstance(step, ToolCallStep) and step.status == "running":
                steps.append(
                    replace(
                        step,
                        status=status,
                        updated_at=timestamp,
                        ended_at=timestamp,
                    )
                )
            else:
                steps.append(step)
        return _with_derived_fields(replace(message, steps=tuple(steps)))

    return _update_last_assistant(
        replace(state, active_tool_calls=active),
        update_message,
        timestamp,
    )


def _last_assistant_index(
    messages: tuple[TranscriptMessage, ...],
) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], AssistantMessage):
            return index
    return None


def _same_tool(step: MessageStep, tool_id: str) -> bool:
    return isinstance(step, ToolCallStep) and step.id == tool_id


def _find_running_tool_id_by_name(state: CLIUIState, name: str) -> str:
    if not name:
        return ""
    for tool_id, call in state.active_tool_calls.items():
        if call.name == name and call.status == "running":
            return tool_id
    return ""


def _find_tool_id_by_name(state: CLIUIState, name: str) -> str:
    if not name:
        return ""
    for tool_id, call in state.active_tool_calls.items():
        if call.name == name:
            return tool_id
    return ""


def _normalize_tool_status(status: str) -> ToolCallStatus:
    if status in {"success", "error", "cancelled", "running", "pending"}:
        return status  # type: ignore[return-value]
    if status in {"failed", "failure"}:
        return "error"
    return "success"


def _autonomous_started_content(event: TaskStartedEvent) -> str:
    label = "Autonomous TODO started" if event.todo_id else "Autonomous task started"
    prompt = _summarize_autonomous_prompt(event.prompt, event.todo_id)
    return f"{label}: {prompt}" if prompt else f"{label}."


def _summarize_autonomous_prompt(prompt: str, todo_id: str) -> str:
    text = " ".join(str(prompt or "").split())
    if not text:
        return ""
    if todo_id:
        prefix = f"Work on TODO {todo_id}:"
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    if text.startswith("Work on TODO "):
        _head, separator, tail = text.partition(":")
        if separator and tail.strip():
            return tail.strip()
    return text


def _artifact_from_event(event: WorkspaceArtifactEvent) -> WorkspaceArtifact:
    payload = copy.deepcopy(event.artifact)
    path = event.path or str(payload.get("path") or "")
    name = str(payload.get("name") or (PurePath(path).name if path else ""))
    mime_type = str(payload.get("mime_type") or payload.get("mimeType") or "")
    size = payload.get("size_bytes", payload.get("sizeBytes"))
    size_bytes = size if isinstance(size, int) and not isinstance(size, bool) else None
    return WorkspaceArtifact(
        path=path,
        name=name,
        mime_type=mime_type,
        size_bytes=size_bytes,
        payload=payload,
    )


def _merge_artifact(
    artifacts: tuple[WorkspaceArtifact, ...],
    incoming: WorkspaceArtifact,
) -> tuple[WorkspaceArtifact, ...]:
    if not incoming.path:
        return artifacts + (incoming,)
    result = list(artifacts)
    for index, artifact in enumerate(result):
        if artifact.path == incoming.path:
            result[index] = incoming
            return tuple(result)
    result.append(incoming)
    return tuple(result)


def _accumulate_session_usage(
    current: SessionTokenUsage,
    context_stats: dict[str, Any],
    model: str,
) -> SessionTokenUsage:
    # ``turn_recorded=False`` marks a turn whose usage extraction found
    # nothing: the server keeps token fields honest-zero and clients must not
    # accumulate (pre-repair servers could carry the PREVIOUS turn's non-zero
    # values here, which double-counted). Absent key = older server; fall
    # back to the zero-guard alone.
    if context_stats.get("turn_recorded") is False:
        return current
    input_tokens = _safe_int(context_stats.get("input_tokens"))
    output_tokens = _safe_int(context_stats.get("output_tokens"))
    if input_tokens == 0 and output_tokens == 0:
        return current

    per_model = dict(current.per_model)
    model_key = model or "unknown"
    prev_in, prev_out = per_model.get(model_key, (0, 0))
    per_model[model_key] = (prev_in + input_tokens, prev_out + output_tokens)

    return SessionTokenUsage(
        total_input=current.total_input + input_tokens,
        total_output=current.total_output + output_tokens,
        turn_count=current.turn_count + 1,
        per_model=per_model,
    )


def _safe_int(value: Any) -> int:
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return 0


def _normalize(event: Any, *, default_thread_id: str | None) -> NormalizedEvent:
    return normalize_stream_event(event, default_thread_id=default_thread_id)


def _timestamp(now: float | None) -> float:
    return time.monotonic() if now is None else now


def _new_id(prefix: str) -> str:
    cleaned = "".join(ch for ch in prefix.lower() if ch.isalnum() or ch == "-")
    return f"{cleaned or 'id'}-{uuid.uuid4().hex[:10]}"


__all__ = [
    "create_initial_state",
    "mark_cancelling",
    "reduce_events",
    "reduce_stream_event",
    "start_turn",
]
