"""Disconnected CLI transport."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

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


def is_disconnected_client(client: Any) -> bool:
    """Return whether ``client`` is the disconnected placeholder transport."""

    return isinstance(client, DisconnectedAgentClient)


__all__ = [
    "DISCONNECTED_MESSAGE",
    "DisconnectedAgentClient",
    "is_disconnected_client",
]
