"""Normalized stream events for CLI transports and renderers."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterable, AsyncIterator, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal, TypeAlias


KnownEventType: TypeAlias = Literal[
    "thinking",
    "tool_call_delta",
    "tool_call",
    "tool_result",
    "response",
    "workspace_artifact",
    "tool_reload",
    "dispatched",
    "queued",
    "compacting",
    "compacted",
    "context_attached",
    "iteration_limit",
    "turn_rewound",
    "task_started",
    "task_completed",
    "hook_approval",
    "hook_approval_resolved",
    "fallback_prompt",
    "fallback_prompt_resolved",
    "provider_fallback",
    "provider_retry",
    "cli_config",
    "error",
    "done",
]
NormalizedEventType: TypeAlias = KnownEventType | Literal["diagnostic"]


@dataclass(frozen=True, slots=True, kw_only=True)
class CLIStreamEvent:
    """Base normalized CLI stream event.

    ``raw`` preserves the source payload for diagnostics, but it is excluded
    from equality so API/local events can compare equal after normalization.
    """

    type: NormalizedEventType
    thread_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    def as_dict(
        self,
        *,
        include_raw: bool = False,
        omit_empty: bool = False,
    ) -> dict[str, Any]:
        """Return a dictionary representation for renderer/test adapters."""

        data = asdict(self)
        if not include_raw:
            data.pop("raw", None)
        if omit_empty:
            data = {
                key: value
                for key, value in data.items()
                if value not in (None, "", {}, [], ())
            }
        return data


@dataclass(frozen=True, slots=True, kw_only=True)
class ThinkingEvent(CLIStreamEvent):
    type: Literal["thinking"] = "thinking"
    content: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallDeltaEvent(CLIStreamEvent):
    type: Literal["tool_call_delta"] = "tool_call_delta"
    content: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallEvent(CLIStreamEvent):
    type: Literal["tool_call"] = "tool_call"
    id: str = ""
    name: str = ""
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolResultEvent(CLIStreamEvent):
    type: Literal["tool_result"] = "tool_result"
    id: str = ""
    name: str = ""
    result: Any = ""
    status: str = "success"
    # Server-measured execution time; None on older backends.
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ResponseEvent(CLIStreamEvent):
    type: Literal["response"] = "response"
    content: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceArtifactEvent(CLIStreamEvent):
    type: Literal["workspace_artifact"] = "workspace_artifact"
    tool_call_id: str = ""
    tool_name: str = ""
    path: str = ""
    artifact: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolReloadEvent(CLIStreamEvent):
    type: Literal["tool_reload"] = "tool_reload"
    tools: tuple[str, ...] = ()
    ttl: str = ""
    ttl_seconds: int | None = None
    source: str = ""
    skill_name: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class DispatchedEvent(CLIStreamEvent):
    type: Literal["dispatched"] = "dispatched"
    target_thread_id: str = ""
    title: str = ""
    original_thread_id: str = ""
    matched_ref: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class QueuedEvent(CLIStreamEvent):
    type: Literal["queued"] = "queued"
    message: str = ""
    holder: str = ""
    held_seconds: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CompactingEvent(CLIStreamEvent):
    type: Literal["compacting"] = "compacting"
    message: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class CompactedEvent(CLIStreamEvent):
    type: Literal["compacted"] = "compacted"
    summary: str = ""
    messages_removed: int = 0
    auto_resumed: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextAttachedEvent(CLIStreamEvent):
    type: Literal["context_attached"] = "context_attached"
    summary: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class IterationLimitEvent(CLIStreamEvent):
    type: Literal["iteration_limit"] = "iteration_limit"
    content: str = ""
    scope: str = ""
    reason: str = ""
    max_iterations: int | None = None
    tool_call_count: int | None = None
    agent_name: str = ""
    repeated_tool_name: str = ""
    repeated_count: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnRewoundEvent(CLIStreamEvent):
    """A pre-output provider refusal was rewound server-side (backlog #105).

    The backend already removed the refused exchange from the checkpoint;
    the CLI truncates the matching local transcript tail, restores ``prompt``
    to the composer, and prints ``content`` as a notice. Nothing is
    auto-resent.
    """

    type: Literal["turn_rewound"] = "turn_rewound"
    content: str = ""
    prompt: str = ""
    to_message_id: str = ""
    reason: str = ""
    removed: int | None = None
    model: str = ""
    # True for autonomous turns (TODO/trigger/dream): the CLI shows the notice
    # but never truncates the interactive transcript or restores the prompt.
    autonomous: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskStartedEvent(CLIStreamEvent):
    type: Literal["task_started"] = "task_started"
    task_id: str = ""
    prompt: str = ""
    todo_id: str = ""
    source: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class TaskCompletedEvent(CLIStreamEvent):
    type: Literal["task_completed"] = "task_completed"
    task_id: str = ""
    content: str = ""
    todo_id: str = ""
    error: bool = False
    error_message: str = ""
    notify: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class HookApprovalEvent(CLIStreamEvent):
    """A require_approval hook is holding a tool call for a user decision."""

    type: Literal["hook_approval"] = "hook_approval"
    record_id: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    prompt: str = ""
    tool_args_preview: str = ""
    created_at: str = ""
    expires_at: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class HookApprovalResolvedEvent(CLIStreamEvent):
    """A held tool call was resolved (any surface, any outcome)."""

    type: Literal["hook_approval_resolved"] = "hook_approval_resolved"
    record_id: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    outcome: str = ""
    resolved_by: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class FallbackPromptEvent(CLIStreamEvent):
    """A turn parked on a model-swap consent prompt (fallback consent)."""

    type: Literal["fallback_prompt"] = "fallback_prompt"
    record_id: str = ""
    kind: str = ""
    from_provider: str = ""
    from_model: str = ""
    to_provider: str = ""
    to_model: str = ""
    reason: str = ""
    http_status: int | None = None
    timeout_seconds: int | None = None
    hold_options: tuple[int, ...] = ()
    allow_permanent: bool = True
    default_hold_seconds: int | None = None
    created_at: str = ""
    expires_at: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class FallbackPromptResolvedEvent(CLIStreamEvent):
    """A parked model swap was resolved (any surface, any outcome)."""

    type: Literal["fallback_prompt_resolved"] = "fallback_prompt_resolved"
    record_id: str = ""
    kind: str = ""
    outcome: str = ""
    resolved_by: str = ""
    hold_seconds: int | None = None
    hold_permanent: bool = False
    note: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderFallbackEvent(CLIStreamEvent):
    """An applied model switch to a fallback candidate (any consent mode)."""

    type: Literal["provider_fallback"] = "provider_fallback"
    from_provider: str = ""
    from_model: str = ""
    to_provider: str = ""
    to_model: str = ""
    hold_seconds: int | None = None
    permanent: bool = False
    expires_at: str = ""
    reason: str = ""
    http_status: int | None = None
    # True when the post-stream recovery site rolled the turn back and
    # re-drove it: partial text already rendered was superseded, not
    # continued (the GUIs trim their transcript on this flag).
    rewound: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderRetryEvent(CLIStreamEvent):
    """A transient provider failure being retried on the same model."""

    type: Literal["provider_retry"] = "provider_retry"
    provider: str = ""
    model: str = ""
    attempt: int | None = None
    max_retries: int | None = None
    delay_seconds: float | None = None
    reason: str = ""
    http_status: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CLIConfigEvent(CLIStreamEvent):
    """A backend-pushed CLI configuration command (cli_statusbar_* tools).

    User-scoped, not thread-scoped: every connected CLI applies it and
    POSTs its outcome to ``/cli-config/{command_id}/result``.
    """

    type: Literal["cli_config"] = "cli_config"
    command_id: str = ""
    command_type: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    timeout_seconds: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorEvent(CLIStreamEvent):
    type: Literal["error"] = "error"
    content: str = ""
    code: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class DoneEvent(CLIStreamEvent):
    type: Literal["done"] = "done"
    context_stats: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    title: str = ""
    title_source: str = ""
    status: str = ""
    tool_call_count: int | None = None
    dispatched_to: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class DiagnosticEvent(CLIStreamEvent):
    type: Literal["diagnostic"] = "diagnostic"
    source_type: str = ""
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


NormalizedEvent: TypeAlias = (
    CLIStreamEvent
    | ThinkingEvent
    | ToolCallDeltaEvent
    | ToolCallEvent
    | ToolResultEvent
    | ResponseEvent
    | WorkspaceArtifactEvent
    | ToolReloadEvent
    | DispatchedEvent
    | QueuedEvent
    | CompactingEvent
    | CompactedEvent
    | ContextAttachedEvent
    | IterationLimitEvent
    | TurnRewoundEvent
    | TaskStartedEvent
    | TaskCompletedEvent
    | HookApprovalEvent
    | HookApprovalResolvedEvent
    | CLIConfigEvent
    | ErrorEvent
    | DoneEvent
    | DiagnosticEvent
)


def normalize_stream_event(
    event: Any,
    *,
    default_thread_id: str | None = None,
) -> NormalizedEvent:
    """Normalize one API SSE or local ``astream()`` event.

    Local ``astream()`` chunks often do not include ``thread_id`` because the
    caller already knows it. Pass ``default_thread_id`` to make those chunks
    compare equal to API events where the router injects ``thread_id``.
    """

    if isinstance(event, CLIStreamEvent):
        if default_thread_id and event.thread_id is None:
            return replace(event, thread_id=default_thread_id)
        return event

    if not isinstance(event, Mapping):
        return DiagnosticEvent(
            source_type="malformed",
            message="Malformed stream event: expected a mapping.",
            payload={"value": repr(event)},
        )

    raw = _copy_mapping(event)
    payload = _event_payload(event)
    event_type = _text(_first(payload, "type"), default="")
    thread_id = (
        _optional_text(
            _first(payload, "thread_id", "threadId", default=None),
        )
        or default_thread_id
    )

    if event_type == "thinking":
        return ThinkingEvent(
            thread_id=thread_id,
            content=_text(_first(payload, "content", "message"), default=""),
            raw=raw,
        )

    if event_type == "tool_call_delta":
        return ToolCallDeltaEvent(
            thread_id=thread_id,
            content=_text(_first(payload, "content", "message"), default=""),
            raw=raw,
        )

    if event_type == "tool_call":
        return ToolCallEvent(
            thread_id=thread_id,
            id=_text(_first(payload, "id", "tool_call_id", "toolCallId"), default=""),
            name=_text(_first(payload, "name", "tool_name", "toolName"), default=""),
            args=_as_dict(_first(payload, "args", "arguments"), default={}),
            raw=raw,
        )

    if event_type == "tool_result":
        raw_duration = _first(payload, "duration_ms", "durationMs")
        return ToolResultEvent(
            thread_id=thread_id,
            id=_text(_first(payload, "id", "tool_call_id", "toolCallId"), default=""),
            name=_text(_first(payload, "name", "tool_name", "toolName"), default=""),
            result=_first(payload, "result", "content", default=""),
            status=_text(_first(payload, "status"), default="success"),
            duration_ms=(
                int(raw_duration)
                if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool)
                else None
            ),
            raw=raw,
        )

    if event_type == "response":
        return ResponseEvent(
            thread_id=thread_id,
            content=_text(_first(payload, "content", "message"), default=""),
            raw=raw,
        )

    if event_type == "workspace_artifact":
        artifact = _workspace_artifact_payload(payload)
        return WorkspaceArtifactEvent(
            thread_id=thread_id,
            tool_call_id=_text(
                _first(payload, "tool_call_id", "toolCallId"),
                default="",
            ),
            tool_name=_text(_first(payload, "tool_name", "toolName"), default=""),
            path=_text(_first(payload, "path", default=artifact.get("path")), default=""),
            artifact=artifact,
            raw=raw,
        )

    if event_type == "tool_reload":
        return ToolReloadEvent(
            thread_id=thread_id,
            tools=_tuple_of_text(_first(payload, "tools"), default=()),
            ttl=_text(_first(payload, "ttl"), default=""),
            ttl_seconds=_optional_int(_first(payload, "ttl_seconds", "ttlSeconds")),
            source=_text(_first(payload, "source"), default=""),
            skill_name=_optional_text(_first(payload, "skill_name", "skillName")),
            reason=_text(_first(payload, "reason"), default=""),
            raw=raw,
        )

    if event_type == "dispatched":
        dispatched_to = _as_dict(
            _first(payload, "dispatched_to", "dispatchedTo"),
            default={},
        )
        return DispatchedEvent(
            thread_id=thread_id,
            target_thread_id=_text(
                _first(
                    payload,
                    "target_thread_id",
                    "targetThreadId",
                    default=_first(dispatched_to, "thread_id", "threadId"),
                ),
                default="",
            ),
            title=_text(
                _first(payload, "title", default=_first(dispatched_to, "title")),
                default="",
            ),
            original_thread_id=_text(
                _first(payload, "original_thread_id", "originalThreadId"),
                default="",
            ),
            matched_ref=_text(_first(payload, "matched_ref", "matchedRef"), default=""),
            raw=raw,
        )

    if event_type == "queued":
        return QueuedEvent(
            thread_id=thread_id,
            message=_text(_first(payload, "content", "message"), default=""),
            holder=_text(_first(payload, "holder"), default=""),
            held_seconds=_optional_float(
                _first(payload, "held_seconds", "heldSeconds"),
            ),
            raw=raw,
        )

    if event_type == "compacting":
        return CompactingEvent(
            thread_id=thread_id,
            message=_text(_first(payload, "message", "content"), default=""),
            raw=raw,
        )

    if event_type in {"compact_result", "compacted"}:
        result = _as_dict(_first(payload, "result"), default={})
        compacted_payload = result or payload
        return CompactedEvent(
            thread_id=thread_id,
            summary=_text(_first(compacted_payload, "summary"), default=""),
            messages_removed=_int(
                _first(compacted_payload, "messages_removed", "messagesRemoved"),
                default=0,
            ),
            auto_resumed=_bool(
                _first(compacted_payload, "auto_resumed", "autoResumed"),
                default=False,
            ),
            raw=raw,
        )

    if event_type == "context_attached":
        return ContextAttachedEvent(
            thread_id=thread_id,
            summary=_text(_first(payload, "summary", "content"), default=""),
            raw=raw,
        )

    if event_type == "iteration_limit":
        return IterationLimitEvent(
            thread_id=thread_id,
            content=_text(_first(payload, "content", "message"), default=""),
            scope=_text(_first(payload, "scope"), default=""),
            reason=_text(_first(payload, "reason"), default=""),
            max_iterations=_optional_int(
                _first(payload, "max_iterations", "maxIterations"),
            ),
            tool_call_count=_optional_int(
                _first(payload, "tool_call_count", "toolCallCount"),
            ),
            agent_name=_text(_first(payload, "agent_name", "agentName"), default=""),
            repeated_tool_name=_text(
                _first(payload, "repeated_tool_name", "repeatedToolName"),
                default="",
            ),
            repeated_count=_optional_int(
                _first(payload, "repeated_count", "repeatedCount"),
            ),
            raw=raw,
        )

    if event_type == "turn_rewound":
        return TurnRewoundEvent(
            thread_id=thread_id,
            content=_text(_first(payload, "content", "message"), default=""),
            prompt=_text(_first(payload, "prompt"), default=""),
            to_message_id=_text(
                _first(payload, "to_message_id", "toMessageId"), default=""
            ),
            reason=_text(_first(payload, "reason"), default=""),
            removed=_optional_int(_first(payload, "removed")),
            model=_text(_first(payload, "model", "model_name", "modelName"), default=""),
            autonomous=_bool(_first(payload, "autonomous"), default=False),
            raw=raw,
        )

    if event_type == "task_started":
        return TaskStartedEvent(
            thread_id=thread_id,
            task_id=_text(_first(payload, "task_id", "taskId"), default=""),
            prompt=_text(_first(payload, "prompt", "message", "content"), default=""),
            todo_id=_text(_first(payload, "todo_id", "todoId"), default=""),
            source=_text(_first(payload, "source"), default=""),
            raw=raw,
        )

    if event_type == "task_completed":
        return TaskCompletedEvent(
            thread_id=thread_id,
            task_id=_text(_first(payload, "task_id", "taskId"), default=""),
            content=_text(_first(payload, "content", "message"), default=""),
            todo_id=_text(_first(payload, "todo_id", "todoId"), default=""),
            error=_bool(_first(payload, "error"), default=False),
            error_message=_text(
                _first(payload, "error_message", "errorMessage"),
                default="",
            ),
            notify=_bool(_first(payload, "notify"), default=False),
            raw=raw,
        )

    if event_type == "hook_approval":
        return HookApprovalEvent(
            thread_id=thread_id,
            record_id=_text(_first(payload, "record_id", "recordId"), default=""),
            tool_call_id=_text(
                _first(payload, "tool_call_id", "toolCallId"),
                default="",
            ),
            tool_name=_text(_first(payload, "tool_name", "toolName"), default=""),
            prompt=_text(_first(payload, "prompt"), default=""),
            tool_args_preview=_text(
                _first(payload, "tool_args_preview", "toolArgsPreview"),
                default="",
            ),
            created_at=_text(_first(payload, "created_at", "createdAt"), default=""),
            expires_at=_text(_first(payload, "expires_at", "expiresAt"), default=""),
            raw=raw,
        )

    if event_type == "hook_approval_resolved":
        return HookApprovalResolvedEvent(
            thread_id=thread_id,
            record_id=_text(_first(payload, "record_id", "recordId"), default=""),
            tool_call_id=_text(
                _first(payload, "tool_call_id", "toolCallId"),
                default="",
            ),
            tool_name=_text(_first(payload, "tool_name", "toolName"), default=""),
            outcome=_text(_first(payload, "outcome"), default=""),
            resolved_by=_text(
                _first(payload, "resolved_by", "resolvedBy"),
                default="",
            ),
            note=_text(_first(payload, "note"), default=""),
            raw=raw,
        )

    if event_type == "fallback_prompt":
        return FallbackPromptEvent(
            thread_id=thread_id,
            record_id=_text(_first(payload, "record_id", "recordId"), default=""),
            kind=_text(_first(payload, "kind"), default="transport"),
            from_provider=_text(
                _first(payload, "from_provider", "fromProvider"), default=""
            ),
            from_model=_text(_first(payload, "from_model", "fromModel"), default=""),
            to_provider=_text(
                _first(payload, "to_provider", "toProvider"), default=""
            ),
            to_model=_text(_first(payload, "to_model", "toModel"), default=""),
            reason=_text(_first(payload, "reason"), default=""),
            http_status=_optional_int(_first(payload, "http_status", "httpStatus")),
            timeout_seconds=_optional_int(
                _first(payload, "timeout_seconds", "timeoutSeconds"),
            ),
            hold_options=_int_tuple(_first(payload, "hold_options", "holdOptions")),
            allow_permanent=_bool(
                _first(payload, "allow_permanent", "allowPermanent"), default=True
            ),
            default_hold_seconds=_optional_int(
                _first(payload, "default_hold_seconds", "defaultHoldSeconds"),
            ),
            created_at=_text(_first(payload, "created_at", "createdAt"), default=""),
            expires_at=_text(_first(payload, "expires_at", "expiresAt"), default=""),
            raw=raw,
        )

    if event_type == "fallback_prompt_resolved":
        return FallbackPromptResolvedEvent(
            thread_id=thread_id,
            record_id=_text(_first(payload, "record_id", "recordId"), default=""),
            kind=_text(_first(payload, "kind"), default="transport"),
            outcome=_text(_first(payload, "outcome"), default=""),
            resolved_by=_text(
                _first(payload, "resolved_by", "resolvedBy"), default=""
            ),
            hold_seconds=_optional_int(
                _first(payload, "hold_seconds", "holdSeconds"),
            ),
            hold_permanent=_bool(
                _first(payload, "hold_permanent", "holdPermanent"), default=False
            ),
            note=_text(_first(payload, "note"), default=""),
            raw=raw,
        )

    if event_type == "provider_fallback":
        return ProviderFallbackEvent(
            thread_id=thread_id,
            from_provider=_text(
                _first(payload, "from_provider", "fromProvider"), default=""
            ),
            from_model=_text(_first(payload, "from_model", "fromModel"), default=""),
            to_provider=_text(
                _first(payload, "to_provider", "toProvider"), default=""
            ),
            to_model=_text(_first(payload, "to_model", "toModel"), default=""),
            hold_seconds=_optional_int(
                _first(payload, "hold_seconds", "holdSeconds"),
            ),
            permanent=_bool(_first(payload, "permanent"), default=False),
            expires_at=_text(_first(payload, "expires_at", "expiresAt"), default=""),
            reason=_text(_first(payload, "reason"), default=""),
            http_status=_optional_int(_first(payload, "http_status", "httpStatus")),
            rewound=_bool(_first(payload, "rewound"), default=False),
            raw=raw,
        )

    if event_type == "provider_retry":
        return ProviderRetryEvent(
            thread_id=thread_id,
            provider=_text(_first(payload, "provider"), default=""),
            model=_text(_first(payload, "model"), default=""),
            attempt=_optional_int(_first(payload, "attempt")),
            max_retries=_optional_int(
                _first(payload, "max_retries", "maxRetries"),
            ),
            delay_seconds=_optional_float(
                _first(payload, "delay_seconds", "delaySeconds"),
            ),
            reason=_text(_first(payload, "reason"), default=""),
            http_status=_optional_int(_first(payload, "http_status", "httpStatus")),
            raw=raw,
        )

    if event_type == "cli_config":
        return CLIConfigEvent(
            thread_id=thread_id,
            command_id=_text(_first(payload, "command_id", "commandId"), default=""),
            command_type=_text(
                _first(payload, "command_type", "commandType"),
                default="",
            ),
            args=_as_dict(_first(payload, "args"), default={}),
            timeout_seconds=_optional_int(
                _first(payload, "timeout_seconds", "timeoutSeconds"),
            ),
            raw=raw,
        )

    if event_type == "error":
        return ErrorEvent(
            thread_id=thread_id,
            content=_text(
                _first(payload, "content", "message", "error"),
                default="Unknown error",
            ),
            code=_text(_first(payload, "code"), default=""),
            details=_as_dict(_first(payload, "details"), default={}),
            raw=raw,
        )

    if event_type == "done":
        return DoneEvent(
            thread_id=thread_id,
            context_stats=_as_dict(
                _first(payload, "context_stats", "contextStats"),
                default={},
            ),
            model=_text(_first(payload, "model"), default=""),
            title=_text(_first(payload, "title"), default=""),
            title_source=_text(
                _first(payload, "title_source", "titleSource"),
                default="",
            ),
            status=_text(_first(payload, "status"), default=""),
            tool_call_count=_optional_int(
                _first(payload, "tool_call_count", "toolCallCount"),
            ),
            dispatched_to=_as_dict(
                _first(payload, "dispatched_to", "dispatchedTo"),
                default={},
            ),
            raw=raw,
        )

    return DiagnosticEvent(
        thread_id=thread_id,
        source_type=event_type or "unknown",
        message=(
            f"Unknown stream event type: {event_type}"
            if event_type
            else "Malformed stream event: missing type."
        ),
        payload=_copy_mapping(payload),
        raw=raw,
    )


def normalize_stream_events(
    events: Iterable[Any],
    *,
    default_thread_id: str | None = None,
) -> Iterator[NormalizedEvent]:
    """Yield normalized events from a synchronous stream."""

    for event in events:
        yield normalize_stream_event(event, default_thread_id=default_thread_id)


async def normalize_async_stream_events(
    events: AsyncIterable[Any],
    *,
    default_thread_id: str | None = None,
) -> AsyncIterator[NormalizedEvent]:
    """Yield normalized events from an asynchronous stream."""

    async for event in events:
        yield normalize_stream_event(event, default_thread_id=default_thread_id)


def _event_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = event.get("data")
    if not isinstance(nested, Mapping):
        return event

    payload: dict[str, Any] = dict(nested)
    for key in ("type", "thread_id", "threadId"):
        if key in event:
            payload.setdefault(key, event[key])
    return payload


def _workspace_artifact_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    artifact = _as_dict(_first(payload, "artifact"), default={})
    for key in ("path", "name", "mime_type", "mimeType", "size_bytes", "sizeBytes"):
        if key in payload and payload[key] is not None:
            artifact.setdefault(key, copy.deepcopy(payload[key]))
    return artifact


def _first(
    payload: Mapping[str, Any],
    *keys: str,
    default: Any = None,
) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return default


def _text(value: Any, *, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _optional_text(value: Any) -> str | None:
    text = _text(value, default="")
    return text or None


def _int(value: Any, *, default: int = 0) -> int:
    parsed = _optional_int(value)
    return default if parsed is None else parsed


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _int_tuple(value: Any) -> tuple[int, ...]:
    """Coerce a payload list into an int tuple, dropping junk entries."""
    if not isinstance(value, (list, tuple)):
        return ()
    out: list[int] = []
    for item in value:
        parsed = _optional_int(item)
        if parsed is not None:
            out.append(parsed)
    return tuple(out)


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _as_dict(value: Any, *, default: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return copy.deepcopy(default)
    return _copy_mapping(value)


def _copy_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return copy.deepcopy(dict(value))
    except Exception:  # noqa: BLE001 - stream diagnostics must not crash.
        return dict(value)


def _tuple_of_text(value: Any, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Iterable):
        return tuple(_text(item, default="") for item in value)
    return default


__all__ = [
    "CLIStreamEvent",
    "KnownEventType",
    "NormalizedEvent",
    "NormalizedEventType",
    "ThinkingEvent",
    "ToolCallDeltaEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "ResponseEvent",
    "WorkspaceArtifactEvent",
    "ToolReloadEvent",
    "QueuedEvent",
    "CompactingEvent",
    "CompactedEvent",
    "ContextAttachedEvent",
    "IterationLimitEvent",
    "TurnRewoundEvent",
    "TaskStartedEvent",
    "TaskCompletedEvent",
    "HookApprovalEvent",
    "HookApprovalResolvedEvent",
    "FallbackPromptEvent",
    "FallbackPromptResolvedEvent",
    "ProviderFallbackEvent",
    "ProviderRetryEvent",
    "CLIConfigEvent",
    "ErrorEvent",
    "DoneEvent",
    "DiagnosticEvent",
    "normalize_async_stream_events",
    "normalize_stream_event",
    "normalize_stream_events",
]
