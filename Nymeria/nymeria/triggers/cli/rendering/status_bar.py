"""Renderer-agnostic status bar formatting for the full-screen CLI."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from rich.cells import cell_len

from ..state import CLIUIState, select_context_usage
from .markdown import truncate_cell_width
from .indicator import (
    ActivityIndicator,
    PHASE_LABELS,
    activity_state_from_ui_state,
    normalize_detail,
    spinner_frames,
)

NoticeLevel = Literal["info", "warning", "error"]

DEFAULT_NOTICE_TTL_SECONDS = 6.0
STATUS_SEPARATOR = " | "


@dataclass(frozen=True, slots=True, kw_only=True)
class StatusNotice:
    """Short-lived message for the status bar warning/error slot."""

    message: str
    level: NoticeLevel = "info"
    created_at: float = 0.0
    ttl_seconds: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class StatusBarContext:
    """Runtime labels that do not live in reducer state."""

    connection_label: str = ""
    thread_label: str = ""
    model: str = ""
    reasoning_label: str = ""
    cwd: str | Path | None = None
    queued_count: int = 0
    notice: StatusNotice | None = None
    busy: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class StatusSegment:
    """One logical status bar segment before fitting to terminal width."""

    text: str
    style_class: str = "status"
    priority: int = 1
    min_width: int = 4
    removable: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class StatusBarRender:
    """Rendered status bar text plus the surviving fitted segments."""

    text: str
    width: int
    segments: tuple[str, ...]
    fragments: tuple[tuple[str, str], ...] = ()


class StatusBarRenderer:
    """Render a stable one-line status bar from reducer state."""

    def __init__(
        self,
        *,
        indicator: ActivityIndicator | None = None,
        default_notice_ttl_seconds: float = DEFAULT_NOTICE_TTL_SECONDS,
    ) -> None:
        self.indicator = indicator or ActivityIndicator()
        self.default_notice_ttl_seconds = max(0.0, default_notice_ttl_seconds)

    def render(
        self,
        state: CLIUIState,
        *,
        capabilities: Any,
        context: StatusBarContext | None = None,
        width: int | None = None,
        now: float | None = None,
    ) -> StatusBarRender:
        """Render a fitted status bar for the current state."""

        selected_context = context or StatusBarContext()
        current_time = time.monotonic() if now is None else now
        render_width = _positive_width(
            width if width is not None else getattr(capabilities, "width", 80)
        )
        segments = self._segments(
            state,
            capabilities=capabilities,
            context=selected_context,
            now=current_time,
        )
        fitted_segments = _fit_status_segment_records(segments, render_width)
        fitted = [segment.text for segment in fitted_segments]
        return StatusBarRender(
            text=STATUS_SEPARATOR.join(fitted),
            width=render_width,
            segments=tuple(fitted),
            fragments=tuple(_status_fragments(fitted_segments, capabilities)),
        )

    def render_text(
        self,
        state: CLIUIState,
        *,
        capabilities: Any,
        context: StatusBarContext | None = None,
        width: int | None = None,
        now: float | None = None,
    ) -> str:
        """Convenience wrapper for prompt_toolkit controls."""

        return self.render(
            state,
            capabilities=capabilities,
            context=context,
            width=width,
            now=now,
        ).text

    def render_fragments(
        self,
        state: CLIUIState,
        *,
        capabilities: Any,
        context: StatusBarContext | None = None,
        width: int | None = None,
        now: float | None = None,
    ) -> list[tuple[str, str]]:
        """Return prompt_toolkit fragments for styled live status controls."""

        return list(
            self.render(
                state,
                capabilities=capabilities,
                context=context,
                width=width,
                now=now,
            ).fragments
        )

    def _segments(
        self,
        state: CLIUIState,
        *,
        capabilities: Any,
        context: StatusBarContext,
        now: float,
    ) -> list[StatusSegment]:
        notice = select_status_notice(
            state,
            context.notice,
            now=now,
            default_ttl_seconds=self.default_notice_ttl_seconds,
        )
        thread = context.thread_label or state.thread_id or ""
        model = state.active_model or context.model
        context_usage = context_usage_label(state)
        cwd = cwd_label(context.cwd)

        segments = [
            StatusSegment(
                text="Nymeria",
                style_class="status.accent",
                priority=0,
                min_width=3,
                removable=False,
            ),
            StatusSegment(
                text=self.activity_segment(
                    state,
                    capabilities=capabilities,
                    now=now,
                    busy=context.busy,
                ),
                style_class="status.activity",
                priority=0,
                min_width=8,
                removable=False,
            ),
        ]
        if notice:
            segments.append(
                StatusSegment(
                    text=notice,
                    style_class=_notice_style_class(
                        context.notice,
                        state,
                        now=now,
                        default_ttl_seconds=self.default_notice_ttl_seconds,
                    ),
                    priority=0,
                    min_width=8,
                    removable=False,
                )
            )
        if context.connection_label:
            segments.append(StatusSegment(text=context.connection_label, priority=1))
        if model:
            segments.append(StatusSegment(text=str(model), priority=2, min_width=8))
        if context.reasoning_label:
            segments.append(
                StatusSegment(
                    text=context.reasoning_label,
                    style_class="status.accent",
                    priority=1,
                    min_width=6,
                )
            )
        if thread:
            segments.append(
                StatusSegment(
                    text=f"thread {thread}",
                    priority=0,
                    min_width=10,
                    removable=False,
                )
            )
        if context_usage:
            segments.append(StatusSegment(text=context_usage, priority=1, min_width=6))
        if context.queued_count > 0:
            segments.append(
                StatusSegment(
                    text=f"queued {context.queued_count}",
                    priority=1,
                    min_width=8,
                )
            )
        if cwd:
            segments.append(StatusSegment(text=cwd, priority=3, min_width=8))
        return segments

    def activity_segment(
        self,
        state: CLIUIState,
        *,
        capabilities: Any,
        now: float | None = None,
        busy: bool = False,
    ) -> str:
        current_time = time.monotonic() if now is None else now
        activity = activity_state_from_ui_state(state, now=current_time)
        if activity is None:
            return PHASE_LABELS["processing"] if busy else "Ready"

        frame = self.indicator.tick(current_time, capabilities=capabilities)
        label = PHASE_LABELS[activity.phase]
        duration = format_duration(current_time - activity.started_at)
        detail = normalize_detail(activity.detail)
        return " ".join(part for part in (frame, label, duration, detail) if part)


def select_status_notice(
    state: CLIUIState,
    explicit_notice: StatusNotice | None = None,
    *,
    now: float | None = None,
    default_ttl_seconds: float = DEFAULT_NOTICE_TTL_SECONDS,
) -> str:
    """Return the active TTL-bound status notice text, if any."""

    current_time = time.monotonic() if now is None else now
    if explicit_notice is not None and _notice_is_active(
        explicit_notice,
        now=current_time,
        default_ttl_seconds=default_ttl_seconds,
    ):
        return _notice_text(explicit_notice)

    if not state.errors:
        return ""
    error = state.errors[-1]
    notice = StatusNotice(
        message=error.content or error.code,
        level="error",
        created_at=error.timestamp,
        ttl_seconds=default_ttl_seconds,
    )
    if not _notice_is_active(
        notice,
        now=current_time,
        default_ttl_seconds=default_ttl_seconds,
    ):
        return ""
    return _notice_text(notice)


def context_usage_label(state: CLIUIState) -> str:
    """Return compact context usage text when token stats are available."""

    usage = select_context_usage(state)
    percent = usage.get("percent_used")
    if isinstance(percent, (int, float)):
        return f"ctx {percent:.0f}%"

    used = _first_number(usage, "used_tokens", "total_tokens", "input_tokens")
    if used is not None:
        return f"ctx {used:.0f}"
    return ""


def cwd_label(cwd: str | Path | None = None) -> str:
    """Return a compact current-working-directory segment."""

    raw = str(cwd if cwd is not None else Path.cwd())
    if not raw:
        return ""

    home = str(Path.home())
    display = raw
    if raw == home:
        display = "~"
    elif raw.startswith(f"{home}{os.sep}"):
        display = f"~{os.sep}{raw[len(home) + 1 :]}"
    return f"cwd {display}"


def format_duration(seconds: float) -> str:
    """Format a short activity duration for status surfaces."""

    seconds = max(0.0, float(seconds))
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, remaining = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{remaining:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def fit_status_segments(
    segments: list[StatusSegment] | tuple[StatusSegment, ...],
    width: int,
) -> list[str]:
    """Drop low-priority segments, then truncate survivors to fit one line."""

    return [
        segment.text
        for segment in _fit_status_segment_records(segments, width)
    ]


def _fit_status_segment_records(
    segments: list[StatusSegment] | tuple[StatusSegment, ...],
    width: int,
) -> list[StatusSegment]:
    """Drop low-priority segment records, then truncate survivors to fit one line."""

    render_width = _positive_width(width)
    active = [segment for segment in segments if normalize_detail(segment.text)]
    if not active:
        return []

    while _joined_length(active) > render_width:
        removable_indexes = [
            index for index, segment in enumerate(active) if segment.removable
        ]
        if not removable_indexes:
            break
        remove_index = max(
            removable_indexes,
            key=lambda index: (
                active[index].priority,
                _cell_width(active[index].text),
            ),
        )
        active.pop(remove_index)

    while _joined_length(active) > render_width:
        shrinkable_indexes = [
            index
            for index, segment in enumerate(active)
            if _cell_width(segment.text) > max(1, segment.min_width)
        ]
        if not shrinkable_indexes:
            break
        shrink_index = max(
            shrinkable_indexes,
            key=lambda index: _cell_width(active[index].text) - active[index].min_width,
        )
        overflow = _joined_length(active) - render_width
        segment = active[shrink_index]
        target_width = max(segment.min_width, _cell_width(segment.text) - overflow)
        active[shrink_index] = replace(
            segment,
            text=truncate_cell_width(segment.text, target_width),
        )

    text = STATUS_SEPARATOR.join(segment.text for segment in active)
    if _cell_width(text) <= render_width:
        return active
    return [
        StatusSegment(
            text=truncate_cell_width(text, render_width),
            style_class="status",
            priority=0,
            min_width=1,
            removable=False,
        )
    ]


def _notice_is_active(
    notice: StatusNotice,
    *,
    now: float,
    default_ttl_seconds: float,
) -> bool:
    ttl_seconds = (
        default_ttl_seconds if notice.ttl_seconds is None else notice.ttl_seconds
    )
    if ttl_seconds <= 0:
        return False
    return now - notice.created_at <= ttl_seconds


def _notice_text(notice: StatusNotice) -> str:
    message = normalize_detail(notice.message)
    if not message:
        return ""
    if notice.level == "error":
        return f"Error: {message}"
    if notice.level == "warning":
        return f"Warning: {message}"
    return message


def _notice_style_class(
    explicit_notice: StatusNotice | None,
    state: CLIUIState,
    *,
    now: float,
    default_ttl_seconds: float,
) -> str:
    if explicit_notice is not None and _notice_is_active(
        explicit_notice,
        now=now,
        default_ttl_seconds=default_ttl_seconds,
    ):
        if explicit_notice.level == "error":
            return "status.notice.error"
        if explicit_notice.level == "warning":
            return "status.notice.warning"
        return "status.accent"
    if state.errors:
        return "status.notice.error"
    return "status.accent"


def _status_fragments(
    segments: list[StatusSegment] | tuple[StatusSegment, ...],
    capabilities: Any,
) -> list[tuple[str, str]]:
    fragments: list[tuple[str, str]] = []
    for index, segment in enumerate(segments):
        if index:
            fragments.append(("class:status.separator", STATUS_SEPARATOR))
        fragments.extend(_segment_fragments(segment, capabilities))
    return fragments or [("class:status", "")]


def _segment_fragments(
    segment: StatusSegment,
    capabilities: Any,
) -> list[tuple[str, str]]:
    if segment.style_class != "status.activity":
        return [(f"class:{segment.style_class}", segment.text)]
    return _activity_fragments(segment.text, capabilities)


def _activity_fragments(text: str, capabilities: Any) -> list[tuple[str, str]]:
    if not text:
        return []
    frames = {frame for frame in spinner_frames(capabilities) if frame}
    for frame in frames:
        prefix = f"{frame} "
        if text == frame:
            return [("class:status.spinner", text)]
        if text.startswith(prefix):
            return [
                ("class:status.spinner", frame),
                ("class:status", " "),
                ("class:status.accent", text[len(prefix):]),
            ]
    return [("class:status.accent", text)]


def _joined_length(segments: list[StatusSegment]) -> int:
    if not segments:
        return 0
    return sum(_cell_width(segment.text) for segment in segments) + (
        _cell_width(STATUS_SEPARATOR) * (len(segments) - 1)
    )


def _cell_width(text: str) -> int:
    try:
        return cell_len(str(text or ""))
    except Exception:  # noqa: BLE001 - status rendering must be defensive.
        return len(str(text or ""))


def _first_number(payload: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _positive_width(width: int | None) -> int:
    if width is None:
        return 80
    try:
        parsed = int(width)
    except (TypeError, ValueError):
        return 80
    return max(1, parsed)


__all__ = [
    "DEFAULT_NOTICE_TTL_SECONDS",
    "NoticeLevel",
    "STATUS_SEPARATOR",
    "StatusBarContext",
    "StatusBarRender",
    "StatusBarRenderer",
    "StatusNotice",
    "StatusSegment",
    "context_usage_label",
    "cwd_label",
    "fit_status_segments",
    "format_duration",
    "select_status_notice",
]
