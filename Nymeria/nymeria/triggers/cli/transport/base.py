"""Transport protocol for CLI agent clients."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, Protocol

from ..events import NormalizedEvent

Attachment = Mapping[str, Any]


class AgentClient(Protocol):
    """Common async-facing client interface for CLI transports."""

    supports_autonomous_stream: bool
    """Whether ``stream_autonomous`` yields a live background event stream.

    Transports that only expose a no-op ``stream_autonomous`` (e.g. the
    disconnected placeholder) leave this falsy so the autonomous monitor never
    starts against them.
    """

    @property
    def connection_label(self) -> str:
        """Short human-readable transport label for status surfaces."""

    def stream_chat(
        self,
        message: str,
        thread_id: str,
        user_id: str = "default",
        attachments: Sequence[Attachment] | None = None,
        **options: Any,
    ) -> AsyncIterator[NormalizedEvent]:
        """Stream normalized chat events for a user message."""

    def stream_autonomous(
        self,
        user_id: str = "default",
        *,
        client_id: str | None = None,
    ) -> AsyncIterator[NormalizedEvent]:
        """Stream normalized autonomous/background events when supported."""

    async def stop(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Abort in-flight work for a thread."""

    async def get_history(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        include_internal: bool = False,
        **options: Any,
    ) -> Mapping[str, Any]:
        """Return conversation history for a thread."""

    async def list_threads(
        self,
        user_id: str = "default",
    ) -> Sequence[Mapping[str, Any]]:
        """List threads visible to a user."""

    async def list_thread_teams(
        self,
        user_id: str = "default",
    ) -> Sequence[Mapping[str, Any]]:
        """List backend-visible callable thread teams for a user."""

    async def list_todos(
        self,
        user_id: str = "default",
        *,
        filter_status: str | None = None,
        thread_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        """List TODOs visible to a user, optionally filtered."""

    async def list_triggers(
        self,
        user_id: str = "default",
        *,
        enabled_only: bool = False,
        thread_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        """List event triggers visible to a user."""

    async def get_context_stats(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Return context-window usage stats for a thread."""

    async def create_thread(
        self,
        user_id: str = "default",
        *,
        thread_id: str | None = None,
        title: str | None = None,
    ) -> Mapping[str, Any]:
        """Create or claim thread metadata."""

    async def update_thread_metadata(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> Mapping[str, Any]:
        """Update thread title/pin metadata."""

    async def delete_thread(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Delete a thread through the transport boundary."""

    async def branch_thread(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        title: str | None = None,
        from_message_index: int | None = None,
    ) -> Mapping[str, Any]:
        """Create a branch thread from an existing thread."""

    async def execute_command(
        self,
        command: str,
        *,
        thread_id: str | None = None,
        source: str = "cli",
        actor: str | None = None,
        surface: str | None = None,
        user_id: str | None = None,
        supports_forms: bool = False,
    ) -> Mapping[str, Any]:
        """Execute a backend global slash command.

        ``supports_forms`` declares the CALLER renders declarative form
        payloads; only the Rich renderer passes True."""

    async def list_commands(
        self,
        *,
        source: str | None = None,
        actor: str | None = None,
        surface: str | None = None,
        user_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        """List backend global slash commands visible to this transport."""


__all__ = ["AgentClient", "Attachment"]
