"""Renderer-agnostic activity labels and throbber frames."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from ..state.model import AssistantActivityPhase, CLIUIState
from ..state.selectors import (
    QUIET_TO_FORMULATING_SECONDS,
    select_activity_phase,
    select_last_assistant_message,
    select_running_tool_calls,
)
from .markdown import coerce_width

ActivityPhase = AssistantActivityPhase

QUIET_TO_FORMULATING_MS = int(QUIET_TO_FORMULATING_SECONDS * 1000)
FRAME_INTERVAL_SECONDS = 0.1

PHASE_LABELS: dict[ActivityPhase, str] = {
    "processing": "Processing...",
    "thinking": "Thinking...",
    "typing": "Streaming...",
    "formulating": "Formulating...",
    "compacting": "Compacting...",
    "processing_results": "Processing results...",
    "waiting": "Waiting...",
}
LABEL_SEGMENT_WIDTH = max(len(label) for label in PHASE_LABELS.values())

UNICODE_FRAMES: tuple[str, ...] = (
    "⠋",
    "⠙",
    "⠹",
    "⠸",
    "⠼",
    "⠴",
    "⠦",
    "⠧",
    "⠇",
    "⠏",
)
ASCII_FRAMES: tuple[str, ...] = ("|", "/", "-", "\\")
NO_ANIMATION_FRAMES: tuple[str, ...] = ("",)


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityState:
    """Current working-state indicator data, independent of renderer type."""

    phase: ActivityPhase
    detail: str = ""
    started_at: float = 0.0
    updated_at: float = 0.0
    active: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivityRender:
    """Single rendered activity line with structured pieces for renderers."""

    text: str
    phase: ActivityPhase
    label: str
    label_segment: str
    detail: str
    frame: str
    width: int
    active: bool = True


class ActivityIndicator:
    """Small stateful throbber that can be advanced by a renderer timer."""

    def __init__(self, *, frame_interval_seconds: float = FRAME_INTERVAL_SECONDS) -> None:
        self.frame_interval_seconds = max(frame_interval_seconds, 0.001)
        self._frame_index = 0
        self._last_tick_at: float | None = None
        self._last_frame_family: tuple[str, ...] | None = None

    def tick(self, now: float | None = None, *, capabilities: Any) -> str:
        """Advance the spinner frame based on elapsed time."""

        current_time = time.monotonic() if now is None else now
        frames = spinner_frames(capabilities)
        if frames != self._last_frame_family:
            self._frame_index = 0
            self._last_tick_at = current_time
            self._last_frame_family = frames
            return frames[0]

        if len(frames) <= 1:
            self._last_tick_at = current_time
            return frames[0]

        if self._last_tick_at is None:
            self._last_tick_at = current_time
            return frames[self._frame_index]

        elapsed = max(0.0, current_time - self._last_tick_at)
        steps = int(elapsed / self.frame_interval_seconds)
        if steps:
            self._frame_index = (self._frame_index + steps) % len(frames)
            self._last_tick_at += steps * self.frame_interval_seconds
        return frames[self._frame_index]

    def render(
        self,
        activity: ActivityState | None,
        *,
        capabilities: Any,
        width: int | None = None,
        now: float | None = None,
    ) -> ActivityRender | None:
        """Render an activity line, or ``None`` when no indicator is visible."""

        if activity is None or not activity.active:
            return None

        render_width = coerce_width(
            width if width is not None else getattr(capabilities, "width", 80)
        )
        label = PHASE_LABELS[activity.phase]
        label_segment = f"{label:<{LABEL_SEGMENT_WIDTH}}"
        detail = normalize_detail(activity.detail)
        frame = self.tick(now, capabilities=capabilities)
        parts = [part for part in (frame, label_segment, detail) if part]
        text = truncate_text(" ".join(parts), render_width)

        return ActivityRender(
            text=text,
            phase=activity.phase,
            label=label,
            label_segment=label_segment,
            detail=detail,
            frame=frame,
            width=render_width,
        )

    def render_from_state(
        self,
        state: CLIUIState,
        *,
        capabilities: Any,
        width: int | None = None,
        now: float | None = None,
        detail: str | None = None,
    ) -> ActivityRender | None:
        """Build and render an activity state from reducer-owned UI state."""

        activity = activity_state_from_ui_state(state, now=now, detail=detail)
        return self.render(activity, capabilities=capabilities, width=width, now=now)


def activity_state_from_ui_state(
    state: CLIUIState,
    *,
    now: float | None = None,
    detail: str | None = None,
) -> ActivityState | None:
    """Derive a throbber-safe activity state from reducer state."""

    current_time = time.monotonic() if now is None else now

    if state.is_queued or state.turn_status == "queued":
        updated_at = state.queue.updated_at if state.queue else state.updated_at
        return ActivityState(
            phase="waiting",
            detail=detail if detail is not None else _queue_detail(state),
            started_at=updated_at,
            updated_at=updated_at,
        )

    if state.is_compacting:
        return ActivityState(
            phase="compacting",
            detail=detail if detail is not None else state.compacting_message,
            started_at=state.updated_at,
            updated_at=state.updated_at,
        )

    phase = select_activity_phase(state, now=current_time)
    if phase is None:
        return None

    assistant = select_last_assistant_message(state)
    started_at = assistant.timestamp if assistant is not None else state.updated_at
    updated_at = (
        assistant.activity_updated_at if assistant is not None else state.updated_at
    )

    return ActivityState(
        phase=phase,
        detail=detail if detail is not None else _phase_detail(state, phase),
        started_at=started_at,
        updated_at=updated_at,
    )


def render_activity_indicator(
    state: CLIUIState,
    *,
    capabilities: Any,
    indicator: ActivityIndicator | None = None,
    width: int | None = None,
    now: float | None = None,
    detail: str | None = None,
) -> ActivityRender | None:
    """Convenience wrapper for one-off reducer-state rendering."""

    selected_indicator = indicator or ActivityIndicator()
    return selected_indicator.render_from_state(
        state,
        capabilities=capabilities,
        width=width,
        now=now,
        detail=detail,
    )


def spinner_frames(capabilities: Any) -> tuple[str, ...]:
    """Choose throbber frames from terminal capabilities."""

    if not bool(getattr(capabilities, "animation_enabled", False)):
        return NO_ANIMATION_FRAMES
    if bool(getattr(capabilities, "unicode_enabled", False)):
        return UNICODE_FRAMES
    return ASCII_FRAMES


def normalize_detail(detail: str) -> str:
    """Collapse renderer detail into one status-safe line."""

    return " ".join(str(detail or "").split())


def truncate_text(text: str, width: int) -> str:
    """Truncate a rendered line to the available terminal width."""

    width = coerce_width(width)
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return f"{text[: width - 3].rstrip()}..."


def _phase_detail(state: CLIUIState, phase: ActivityPhase) -> str:
    if phase != "waiting":
        return ""
    running = select_running_tool_calls(state)
    if not running:
        return ""
    names = [call.name or "tool" for call in running]
    return ", ".join(names)


def _queue_detail(state: CLIUIState) -> str:
    if not state.queue:
        return ""
    if state.queue.holder:
        return f"thread lock: {state.queue.holder}"
    return "thread lock"


__all__ = [
    "ASCII_FRAMES",
    "FRAME_INTERVAL_SECONDS",
    "LABEL_SEGMENT_WIDTH",
    "NO_ANIMATION_FRAMES",
    "PHASE_LABELS",
    "QUIET_TO_FORMULATING_MS",
    "UNICODE_FRAMES",
    "ActivityIndicator",
    "ActivityPhase",
    "ActivityRender",
    "ActivityState",
    "activity_state_from_ui_state",
    "normalize_detail",
    "render_activity_indicator",
    "spinner_frames",
    "truncate_text",
]
