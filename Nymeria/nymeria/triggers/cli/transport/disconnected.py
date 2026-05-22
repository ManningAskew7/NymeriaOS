"""Disconnected CLI transport."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Mapping

from ..events import DoneEvent, ErrorEvent, NormalizedEvent
from .base import Attachment

DISCONNECTED_MESSAGE = "Not connected. Run /login to connect to a running Nymeria backend."


class DisconnectedAgentClient:
    """Minimal client used before the CLI has API credentials."""

    connection_label = "disconnected"

    def __init__(
        self,
        *,
        default_user_id: str = "default",
        startup_error: str = "",
    ) -> None:
        self.default_user_id = default_user_id or "default"
        self.startup_error = startup_error

    async def close(self) -> None:
        """No-op close hook for lifecycle symmetry."""

    async def stream_chat(
        self,
        message: str,
        thread_id: str,
        user_id: str = "default",
        attachments: Sequence[Attachment] | None = None,
        **options: Any,
    ) -> AsyncIterator[NormalizedEvent]:
        """Render a helpful error instead of sending chat while disconnected."""

        del message, user_id, attachments, options
        yield ErrorEvent(
            thread_id=thread_id,
            content=DISCONNECTED_MESSAGE,
            code="cli_not_connected",
            details={"connection_label": self.connection_label},
        )
        yield DoneEvent(thread_id=thread_id, status="error")

    async def stream_autonomous(
        self,
        user_id: str = "default",
        *,
        client_id: str | None = None,
    ) -> AsyncIterator[NormalizedEvent]:
        """Disconnected mode has no autonomous event source."""

        if False:
            yield ErrorEvent(thread_id=None, content=user_id or client_id or "")

    async def get_history(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        include_internal: bool = False,
        **options: Any,
    ) -> Mapping[str, Any]:
        """Return an empty history while disconnected."""

        del user_id, include_internal, options
        return {"thread_id": thread_id, "messages": []}

    async def list_threads(self, user_id: str = "default") -> Sequence[Mapping[str, Any]]:
        """Return no backend threads while disconnected."""

        del user_id
        return []

    async def list_thread_teams(
        self,
        user_id: str = "default",
    ) -> Sequence[Mapping[str, Any]]:
        del user_id
        return []

    async def list_todos(
        self,
        user_id: str = "default",
        *,
        filter_status: str | None = None,
        thread_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        del user_id, filter_status, thread_id
        return []

    async def list_triggers(
        self,
        user_id: str = "default",
        *,
        enabled_only: bool = False,
        thread_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        del user_id, enabled_only, thread_id
        return []

    async def stop(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        del thread_id, user_id
        return {"ok": False, "reason": "disconnected"}

    async def get_context_stats(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        del thread_id, user_id
        return {}

    async def create_thread(
        self,
        user_id: str = "default",
        *,
        thread_id: str | None = None,
        title: str | None = None,
    ) -> Mapping[str, Any]:
        del user_id, thread_id, title
        return {}

    async def update_thread_metadata(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> Mapping[str, Any]:
        del thread_id, user_id, title, pinned
        return {}

    async def delete_thread(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        del thread_id, user_id
        return {}

    async def branch_thread(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        title: str | None = None,
        from_message_index: int | None = None,
    ) -> Mapping[str, Any]:
        del thread_id, user_id, title, from_message_index
        return {}

    async def execute_command(
        self,
        command: str,
        *,
        thread_id: str | None = None,
        source: str = "cli",
        actor: str | None = None,
        surface: str | None = None,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        del command, thread_id, source, actor, surface, user_id
        return {"success": False, "markdown": DISCONNECTED_MESSAGE}

    async def list_commands(
        self,
        *,
        source: str | None = None,
        actor: str | None = None,
        surface: str | None = None,
        user_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        del source, actor, surface, user_id
        return []


def is_disconnected_client(client: Any) -> bool:
    """Return whether ``client`` is the disconnected placeholder transport."""

    return isinstance(client, DisconnectedAgentClient)


__all__ = [
    "DISCONNECTED_MESSAGE",
    "DisconnectedAgentClient",
    "is_disconnected_client",
]
