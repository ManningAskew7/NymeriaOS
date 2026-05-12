"""Reducer-owned state model for the CLI/TUI transcript."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias


MessageStatus: TypeAlias = Literal["pending", "streaming", "complete", "error"]
TurnStatus: TypeAlias = Literal[
    "idle",
    "submitting",
    "queued",
    "streaming",
    "cancelling",
    "complete",
    "error",
]
AssistantActivityPhase: TypeAlias = Literal[
    "processing",
    "thinking",
    "typing",
    "formulating",
    "processing_results",
    "waiting",
]
ToolCallStatus: TypeAlias = Literal[
    "pending",
    "running",
    "success",
    "error",
    "cancelled",
]
SystemMessageKind: TypeAlias = Literal[
    "compaction_notice",
    "context_attached",
    "dispatch_notice",
    "iteration_limit",
    "error",
    "tool_reload",
    "autonomous",
    "diagnostic",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceArtifact:
    """Workspace artifact attached to a tool call or turn."""

    path: str = ""
    name: str = ""
    mime_type: str = ""
    size_bytes: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class ThinkingStep:
    """Assistant thinking content in transcript order."""

    type: Literal["thinking"] = "thinking"
    content: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class ResponseStep:
    """Assistant response content in transcript order."""

    type: Literal["response"] = "response"
    content: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCallStep:
    """Tool call and result state in transcript order."""

    type: Literal["tool_call"] = "tool_call"
    id: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    result: Any = ""
    artifacts: tuple[WorkspaceArtifact, ...] = ()
    status: ToolCallStatus = "running"
    started_at: float = 0.0
    updated_at: float = 0.0
    ended_at: float | None = None


MessageStep: TypeAlias = ThinkingStep | ResponseStep | ToolCallStep


@dataclass(frozen=True, slots=True, kw_only=True)
class UserMessage:
    """User transcript message."""

    id: str
    role: Literal["user"] = "user"
    content: str = ""
    attachments: tuple[dict[str, Any], ...] = ()
    timestamp: float = 0.0
    status: MessageStatus = "complete"
    context_summary: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolReloadInfo:
    """Tool hot-reload metadata associated with an assistant turn."""

    tools: tuple[str, ...] = ()
    ttl: str = ""
    ttl_seconds: int | None = None
    source: str = ""
    skill_name: str | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class AssistantMessage:
    """Assistant transcript message with desktop-parity ordered steps."""

    id: str
    role: Literal["assistant"] = "assistant"
    content: str = ""
    steps: tuple[MessageStep, ...] = ()
    intermediate_content: str = ""
    timestamp: float = 0.0
    status: MessageStatus = "streaming"
    activity_phase: AssistantActivityPhase = "processing"
    activity_updated_at: float = 0.0
    tool_calls: tuple[ToolCallStep, ...] = ()
    tool_reload_info: ToolReloadInfo | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SystemMessage:
    """System-style transcript notice."""

    id: str
    role: Literal["system"] = "system"
    kind: SystemMessageKind
    content: str = ""
    timestamp: float = 0.0
    status: MessageStatus = "complete"
    context_summary: str = ""
    messages_removed: int = 0
    auto_resumed: bool = False
    details: dict[str, Any] = field(default_factory=dict)


TranscriptMessage: TypeAlias = UserMessage | AssistantMessage | SystemMessage


@dataclass(frozen=True, slots=True, kw_only=True)
class QueueState:
    """Current queued/lock-waiting state."""

    message: str = ""
    holder: str = ""
    held_seconds: float | None = None
    updated_at: float = 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class CompactResult:
    """Last compaction result shown by status surfaces."""

    summary: str = ""
    messages_removed: int = 0
    auto_resumed: bool = False
    timestamp: float = 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorNotice:
    """Structured stream error."""

    content: str = ""
    code: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class DiagnosticNotice:
    """Unknown or malformed stream event retained for diagnostics."""

    source_type: str = ""
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionTokenUsage:
    """Session-wide cumulative token usage for /usage reporting."""

    total_input: int = 0
    total_output: int = 0
    turn_count: int = 0
    per_model: dict[str, tuple[int, int]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class CLIUIState:
    """Immutable reducer state for renderers and command surfaces."""

    thread_id: str | None = None
    user_id: str = "default"
    messages: tuple[TranscriptMessage, ...] = ()
    turn_status: TurnStatus = "idle"
    current_assistant_id: str | None = None
    is_queued: bool = False
    queue: QueueState | None = None
    is_compacting: bool = False
    compacting_message: str = ""
    last_compact_result: CompactResult | None = None
    context_attached_message: str | None = None
    context_stats: dict[str, Any] = field(default_factory=dict)
    active_model: str = ""
    active_tool_calls: dict[str, ToolCallStep] = field(default_factory=dict)
    artifacts: tuple[WorkspaceArtifact, ...] = ()
    errors: tuple[ErrorNotice, ...] = ()
    diagnostics: tuple[DiagnosticNotice, ...] = ()
    tool_call_delta_buffer: str = ""
    tool_call_count: int | None = None
    session_usage: SessionTokenUsage = field(default_factory=SessionTokenUsage)
    updated_at: float = 0.0


__all__ = [
    "AssistantActivityPhase",
    "AssistantMessage",
    "CLIUIState",
    "CompactResult",
    "DiagnosticNotice",
    "ErrorNotice",
    "MessageStatus",
    "MessageStep",
    "QueueState",
    "ResponseStep",
    "SessionTokenUsage",
    "SystemMessage",
    "SystemMessageKind",
    "ThinkingStep",
    "ToolCallStatus",
    "ToolCallStep",
    "ToolReloadInfo",
    "TranscriptMessage",
    "TurnStatus",
    "UserMessage",
    "WorkspaceArtifact",
]
