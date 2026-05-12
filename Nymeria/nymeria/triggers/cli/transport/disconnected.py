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


def is_disconnected_client(client: Any) -> bool:
    """Return whether ``client`` is the disconnected placeholder transport."""

    return isinstance(client, DisconnectedAgentClient)


__all__ = [
    "DISCONNECTED_MESSAGE",
    "DisconnectedAgentClient",
    "is_disconnected_client",
]
