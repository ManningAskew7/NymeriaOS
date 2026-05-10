"""Shared CLI state exports.

This package keeps the legacy ``CLIState`` session object import-compatible
while hosting the reducer-backed TUI state model for the refactor.
"""

from __future__ import annotations

import uuid
from typing import Optional, TYPE_CHECKING

from rich.console import Console

from ....config.settings import get_settings
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
    SystemMessage,
    ThinkingStep,
    ToolCallStatus,
    ToolCallStep,
    ToolReloadInfo,
    TranscriptMessage,
    TurnStatus,
    UserMessage,
    WorkspaceArtifact,
)
from .reducer import (
    create_initial_state,
    mark_cancelling,
    reduce_events,
    reduce_stream_event,
    start_turn,
)
from .selectors import (
    select_activity_phase,
    select_context_usage,
    select_intermediate_content,
    select_last_assistant_message,
    select_response_content,
    select_running_tool_calls,
    select_tool_calls,
)

if TYPE_CHECKING:
    from ....core.agent import NymeriaAgent


class CLIState:
    """Holds mutable state shared across the legacy CLI session."""

    def __init__(
        self,
        agent: "NymeriaAgent | None",
        thread_id: Optional[str] = None,
        user_id: str = "default",
    ) -> None:
        self.agent = agent
        self.console = Console()
        self.thread_id = thread_id or str(uuid.uuid4())[:8]
        self.user_id = user_id
        self.running = True

    # -- Manager shortcuts -------------------------------------------------

    @property
    def settings(self):
        return get_settings()

    @property
    def todo_manager(self):
        if self.agent is None:
            raise RuntimeError("Local agent is not available in API/disconnected CLI mode")
        return self.agent.todo_manager

    @property
    def thread_config_manager(self):
        if self.agent is None:
            raise RuntimeError("Local agent is not available in API/disconnected CLI mode")
        return self.agent.thread_config_manager

    @property
    def thread_metadata_manager(self):
        if self.agent is None:
            raise RuntimeError("Local agent is not available in API/disconnected CLI mode")
        return self.agent.thread_metadata_manager

    @property
    def profile_manager(self):
        if self.agent is None:
            raise RuntimeError("Local agent is not available in API/disconnected CLI mode")
        return self.agent.profile_manager

    # -- Helpers -----------------------------------------------------------

    def get_thread_title(self) -> str:
        """Return title for the current thread, or a truncated ID."""

        if self.agent is None:
            return self.thread_id[:8]
        store = self.thread_metadata_manager.get_store(self.user_id)
        meta = store.threads.get(self.thread_id)
        if meta and meta.title and meta.title != "New Chat":
            return meta.title[:30] + "..." if len(meta.title) > 30 else meta.title
        return self.thread_id[:8]

    def get_effective_model(self) -> str:
        """Resolve per-thread model override vs global default."""

        if self.agent is None:
            return self.settings.llm_model
        tc = self.thread_config_manager.get_config(self.thread_id)
        if tc and tc.llm_config and tc.llm_config.model:
            return tc.llm_config.model
        return self.settings.llm_model

    def switch_thread(self, new_thread_id: str) -> None:
        """Switch the active thread."""

        self.thread_id = new_thread_id

    def new_thread(self, title: Optional[str] = None) -> str:
        """Create a new thread and switch to it."""

        if self.agent is None:
            raise RuntimeError("Local agent is not available in API/disconnected CLI mode")
        new_id = str(uuid.uuid4())[:8]
        self.thread_id = new_id
        if title:
            self.thread_metadata_manager.upsert_thread(
                self.user_id,
                new_id,
                title=title,
                title_source="user",
            )
        return new_id


__all__ = [
    "AssistantActivityPhase",
    "AssistantMessage",
    "CLIState",
    "CLIUIState",
    "CompactResult",
    "DiagnosticNotice",
    "ErrorNotice",
    "MessageStatus",
    "MessageStep",
    "QueueState",
    "ResponseStep",
    "SystemMessage",
    "ThinkingStep",
    "ToolCallStatus",
    "ToolCallStep",
    "ToolReloadInfo",
    "TranscriptMessage",
    "TurnStatus",
    "UserMessage",
    "WorkspaceArtifact",
    "create_initial_state",
    "mark_cancelling",
    "reduce_events",
    "reduce_stream_event",
    "select_activity_phase",
    "select_context_usage",
    "select_intermediate_content",
    "select_last_assistant_message",
    "select_response_content",
    "select_running_tool_calls",
    "select_tool_calls",
    "start_turn",
]
