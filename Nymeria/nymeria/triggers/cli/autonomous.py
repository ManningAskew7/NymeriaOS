"""Shared autonomous stream monitoring for CLI renderers."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .events import NormalizedEvent, normalize_stream_event


NoticeCallback = Callable[[str, str, float | None], None]
EventCallback = Callable[[NormalizedEvent], bool | None | Awaitable[bool | None]]


@dataclass(frozen=True, slots=True)
class AutonomousEventDecision:
    """Renderer-neutral decision for one autonomous stream event."""

    accepted: bool
    event: NormalizedEvent | None = None
    notice: str = ""
    notice_level: str = "info"
    notice_ttl_seconds: float | None = None


class AutonomousStreamMonitor:
    """Consume autonomous events and apply shared filtering/reconnect rules."""

    def __init__(
        self,
        *,
        client_getter: Callable[[], Any],
        user_id_getter: Callable[[], str],
        thread_id_getter: Callable[[], str | None],
        client_id: str,
        apply_event: EventCallback,
        set_notice: NoticeCallback,
    ) -> None:
        self.client_getter = client_getter
        self.user_id_getter = user_id_getter
        self.thread_id_getter = thread_id_getter
        self.client_id = client_id
        self.apply_event = apply_event
        self.set_notice = set_notice

    def can_start(self) -> bool:
        """Return whether the selected client exposes API autonomous events."""

        return supports_autonomous_stream(self.client_getter())

    async def run_forever(self) -> None:
        """Run the autonomous subscription with bounded reconnect backoff."""

        reconnect_delay = 1.0
        max_delay = 30.0
        while True:
            try:
                await self.consume_once()
                reconnect_delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - background stream is best effort.
                detail = str(exc or exc.__class__.__name__)
                self.set_notice(
                    f"Autonomous stream disconnected: {detail}",
                    "warning",
                    5,
                )
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_delay)

    async def consume_once(self) -> None:
        """Consume one stream until it ends or is cancelled."""

        client = self.client_getter()
        stream_attr = getattr(client, "stream_autonomous", None)
        if not callable(stream_attr):
            return
        stream: Any = stream_attr
        async for event in stream(
            self.user_id_getter(),
            client_id=self.client_id,
        ):
            decision = decide_autonomous_event(
                event,
                active_thread_id=self.thread_id_getter(),
            )
            if decision.accepted and decision.event is not None:
                result = self.apply_event(decision.event)
                if inspect.isawaitable(result):
                    await result
            elif decision.notice:
                self.set_notice(
                    decision.notice,
                    decision.notice_level,
                    decision.notice_ttl_seconds,
                )


def supports_autonomous_stream(client: Any) -> bool:
    """Return True for transports that expose a live autonomous event stream.

    Reads the transport's ``supports_autonomous_stream`` capability flag rather
    than sniffing the connection label, so both the thin ``APIAgentClient`` and
    the fat ``InProcessAgentClient`` (which subscribes to the in-process event
    bus) qualify, while the disconnected placeholder, whose ``stream_autonomous``
    is a no-op, stays excluded.
    """

    stream = getattr(client, "stream_autonomous", None)
    if not callable(stream):
        return False
    return bool(getattr(client, "supports_autonomous_stream", False))


def decide_autonomous_event(
    event: Any,
    *,
    active_thread_id: str | None,
) -> AutonomousEventDecision:
    """Normalize and filter one autonomous event for the active CLI thread."""

    # Fanout-mirror events (stream_bridge fanout marker) replay a holder
    # turn under a queuer's task id; the holder's own events already render,
    # so drop the mirror before it doubles/interleaves the transcript.
    fanout = (
        event.get("fanout")
        if isinstance(event, dict)
        else getattr(event, "fanout", None)
    )
    if fanout:
        return AutonomousEventDecision(accepted=False)

    normalized = normalize_stream_event(event)
    event_type = getattr(normalized, "type", "")
    if event_type == "cli_config":
        # Client-scoped, not thread-scoped: a cli_config command targets
        # every connected CLI for the user regardless of the active thread
        # (the publishing thread's id rides along for abort bookkeeping).
        return AutonomousEventDecision(accepted=True, event=normalized)
    event_thread_id = getattr(normalized, "thread_id", None)
    if event_thread_id != active_thread_id:
        if event_thread_id is None and event_type == "error":
            message = str(
                getattr(normalized, "content", "") or "Autonomous stream error."
            )
            return AutonomousEventDecision(
                accepted=False,
                notice=_first_status_line(message),
                notice_level="warning",
                notice_ttl_seconds=5,
            )
        return AutonomousEventDecision(accepted=False)
    if event_type == "diagnostic":
        return AutonomousEventDecision(accepted=False)
    return AutonomousEventDecision(accepted=True, event=normalized)


def _first_status_line(text: str) -> str:
    lines = str(text or "").strip().splitlines()
    return lines[0] if lines else "Command completed."
