"""Disconnected CLI transport."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Mapping

from ..events import DoneEvent, ErrorEvent, NormalizedEvent
from .base import Attachment

DISCONNECTED_MESSAGE = (
    "Not connected. Run /login to connect to a running backend, "
    "or 'nymeria init' to set one up."
)


class DisconnectedAgentClient:
    """Minimal client used before the CLI has API credentials."""

    connection_label = "disconnected"

    def __init__(
        self,
        *,
        default_user_id: str = "default",
        startup_error: str = "",
        reconnect_api_url: str = "",
        reconnect_api_key: str = "",
        reconnect_user_id: str = "",
        suggested_url: str = "",
    ) -> None:
        self.default_user_id = default_user_id or "default"
        self.startup_error = startup_error
        # A different local backend we detected while the saved one was down
        # (e.g. saved profile points at :8098 but a backend is live on :8000).
        # Surfaced as a "did you mean <url>?" hint; empty when none was found.
        self.suggested_url = suggested_url
        # Retained saved-profile fields. When this placeholder stands in for a
        # backend that was simply not up yet at startup (rather than bad
        # credentials), the CLI keeps the url/token here so it can silently
        # re-validate and upgrade to a live API client once the backend comes
        # up, without re-prompting for /login. Empty for a plain disconnect
        # (no saved profile, or an auth failure where retrying is pointless).
        self.reconnect_api_url = reconnect_api_url
        self.reconnect_api_key = reconnect_api_key
        self.reconnect_user_id = reconnect_user_id or self.default_user_id

    @property
    def can_reconnect(self) -> bool:
        """Whether a retained saved profile is available for a silent retry."""

        return bool(self.reconnect_api_url.strip() and self.reconnect_api_key.strip())

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
