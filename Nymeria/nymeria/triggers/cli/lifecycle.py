"""Lifecycle helpers for CLI/TUI turn cancellation and shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from .events import ErrorEvent
from .state import CLIUIState, mark_cancelling
from .transport.base import AgentClient

StopStatus = Literal["stopping", "already_stopping", "no_active_turn", "failed"]


@dataclass(frozen=True, slots=True)
class LifecycleStopResult:
    """Result of a lifecycle-managed stop request."""

    status: StopStatus
    reason: str
    message: str
    response: Mapping[str, Any] | None = None
    error: Exception | None = field(default=None, compare=False)

    @property
    def ok(self) -> bool:
        return self.status != "failed"

    @property
    def requested(self) -> bool:
        return self.status in {"stopping", "already_stopping"}


class TurnLifecycleController:
    """Owns per-turn stop idempotency and reducer cancellation effects."""

    def __init__(
        self,
        *,
        client: AgentClient,
        state_getter: Callable[[], CLIUIState],
        state_setter: Callable[[CLIUIState], None],
        thread_id_getter: Callable[[], str],
        user_id_getter: Callable[[], str],
        is_active: Callable[[], bool],
    ) -> None:
        self.client = client
        self._state_getter = state_getter
        self._state_setter = state_setter
        self._thread_id_getter = thread_id_getter
        self._user_id_getter = user_id_getter
        self._is_active = is_active
        self._stop_requested = False

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    def begin_turn(self) -> None:
        """Reset stop state for a newly submitted turn."""

        self._stop_requested = False

    def finish_turn(self) -> None:
        """Hook retained for symmetry and future per-turn cleanup."""

    async def request_stop(
        self,
        *,
        reason: str = "user",
        now: float | None = None,
    ) -> LifecycleStopResult:
        """Call the transport stop endpoint at most once for the active turn."""

        if not self._is_active():
            return LifecycleStopResult(
                status="no_active_turn",
                reason=reason,
                message="No active turn",
            )
        if self._stop_requested:
            return LifecycleStopResult(
                status="already_stopping",
                reason=reason,
                message="Stop already requested",
            )

        self._stop_requested = True
        thread_id = self._thread_id_getter()
        user_id = self._user_id_getter()
        try:
            response = await self.client.stop(thread_id, user_id)
        except Exception as exc:  # noqa: BLE001 - stop failures surface in UI.
            self._stop_requested = False
            return LifecycleStopResult(
                status="failed",
                reason=reason,
                message=str(exc) or exc.__class__.__name__,
                error=exc,
            )

        self._state_setter(mark_cancelling(self._state_getter(), now=now))
        if isinstance(response, Mapping) and response.get("status") == "already_stopping":
            return LifecycleStopResult(
                status="already_stopping",
                reason=reason,
                message="Stop already requested",
                response=response,
            )
        return LifecycleStopResult(
            status="stopping",
            reason=reason,
            message="Stop requested",
            response=response if isinstance(response, Mapping) else None,
        )


async def cancel_task(task: asyncio.Task[Any] | None) -> None:
    """Cancel and drain a task without self-cancelling the caller."""

    if task is None or task.done() or task is asyncio.current_task():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def stream_error_event_from_exception(
    exc: Exception,
    *,
    thread_id: str | None,
    explicit_stop_requested: bool,
) -> ErrorEvent:
    """Represent an unhandled transport stream exception as a reducer event."""

    if explicit_stop_requested:
        return ErrorEvent(
            thread_id=thread_id,
            content="Cancelled.",
            code="cancelled",
            details={
                "error_type": exc.__class__.__name__,
                "explicit_stop": True,
            },
        )

    return ErrorEvent(
        thread_id=thread_id,
        content=str(exc) or "Stream disconnected.",
        code="cli_stream_error",
        details={
            "error_type": exc.__class__.__name__,
            "explicit_stop": False,
        },
    )


class ExitSignalHandlers:
    """Temporarily install process signal handlers for full-screen exits."""

    def __init__(
        self,
        callback: Callable[[int], None],
        *,
        signals: Sequence[int] | None = None,
    ) -> None:
        self.callback = callback
        self.signals = tuple(signals if signals is not None else default_exit_signals())
        self._previous: dict[int, Any] = {}

    def __enter__(self) -> "ExitSignalHandlers":
        try:
            for signum in self.signals:
                self._previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle_signal)
        except (OSError, RuntimeError, ValueError):
            self._restore()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._restore()

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        self.callback(signum)

    def _restore(self) -> None:
        for signum, previous in self._previous.items():
            with contextlib.suppress(OSError, RuntimeError, ValueError):
                signal.signal(signum, previous)
        self._previous.clear()


def default_exit_signals() -> tuple[int, ...]:
    """Signals that should request a graceful TUI shutdown."""

    values: list[int] = []
    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if isinstance(signum, signal.Signals) and int(signum) not in values:
            values.append(int(signum))
    return tuple(values)


def signal_name(signum: int) -> str:
    """Return a readable signal name for status/debug labels."""

    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal-{signum}"


__all__ = [
    "ExitSignalHandlers",
    "LifecycleStopResult",
    "TurnLifecycleController",
    "cancel_task",
    "default_exit_signals",
    "signal_name",
    "stream_error_event_from_exception",
]
