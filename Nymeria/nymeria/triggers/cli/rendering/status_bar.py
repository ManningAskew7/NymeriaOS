"""Renderer-agnostic status bar formatting for the full-screen CLI."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from rich.cells import cell_len

from ..state import CLIUIState, select_context_usage
from .markdown import coerce_width, truncate_cell_width
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

# Segment-ref grammar shared with statusbar_config (defined here so the
# layout module can import them without a circular dependency).
TEXT_REF_PREFIX = "text:"
SCRIPT_REF_PREFIX = "script:"


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
    disconnected: bool = False
    reasoning_label: str = ""
    fast_mode_active: bool = False
    cwd: str | Path | None = None
    queued_count: int = 0
    notice: StatusNotice | None = None
    busy: bool = False
    compact_settings: Any | None = None


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


# A segment provider builds one keyed StatusSegment (or None to omit it this
# frame). Signature: (renderer, state, capabilities, context, now).
SegmentProvider = Callable[
    ["StatusBarRenderer", CLIUIState, Any, StatusBarContext, float],
    "StatusSegment | None",
]

# A script source resolves a ``script:<command>`` ref to its cached first
# stdout line (or None while unavailable). It must never block: execution
# happens off the render loop (see ``script_segments.ScriptSegmentRunner``).
ScriptSource = Callable[[str], "str | None"]


class StatusBarRenderer:
    """Render a stable one-line status bar from keyed segment providers.

    The built-in segments register in a keyed, ordered provider registry
    (``DEFAULT_SEGMENT_KEYS``). Later phases plug into the same registry:
    layout config reorders or removes by key, and script or agent-pushed
    segments register alongside the built-ins via ``register_segment``.
    """

    def __init__(
        self,
        *,
        indicator: ActivityIndicator | None = None,
        default_notice_ttl_seconds: float = DEFAULT_NOTICE_TTL_SECONDS,
    ) -> None:
        self.indicator = indicator or ActivityIndicator()
        self.default_notice_ttl_seconds = max(0.0, default_notice_ttl_seconds)
        self._segment_providers: dict[str, SegmentProvider] = dict(
            _DEFAULT_SEGMENT_PROVIDERS
        )
        # Extras: resolvable by explicit layout refs only, never part of the
        # default order (and so never in ``segment_keys``); a
        # ``register_segment`` with the same key shadows the extra.
        self._extra_providers: dict[str, SegmentProvider] = dict(
            _EXTRA_SEGMENT_PROVIDERS
        )
        self._layout: tuple[str, ...] | None = None
        self._script_source: ScriptSource | None = None

    @property
    def segment_keys(self) -> tuple[str, ...]:
        """Registered segment keys in render order."""

        return tuple(self._segment_providers)

    @property
    def layout(self) -> tuple[str, ...] | None:
        """Configured segment refs, or None for the registry default order."""

        return self._layout

    def set_layout(self, refs: tuple[str, ...] | None) -> None:
        """Pin the bar to an explicit ordered ref list (None = default).

        Refs follow the statusbar_config grammar: a registered segment key,
        ``text:<literal>``, or ``script:<command>``. Unknown keys and script
        refs without a source resolve to nothing at render time.
        """

        self._layout = tuple(refs) if refs is not None else None

    def set_script_source(self, source: ScriptSource | None) -> None:
        """Install the resolver for ``script:`` refs (must not block)."""

        self._script_source = source

    def register_segment(
        self,
        key: str,
        provider: SegmentProvider,
        *,
        before: str | None = None,
        after: str | None = None,
    ) -> None:
        """Register or replace a segment provider by key.

        Re-registering an existing key replaces it in place. A new key is
        appended, or inserted relative to ``before``/``after`` when the
        anchor key exists (unknown anchors fall back to append).
        """

        if before is not None and after is not None:
            raise ValueError("register_segment takes before or after, not both")
        providers = self._segment_providers
        if key in providers:
            providers[key] = provider
            return
        anchor = before or after
        if anchor is None or anchor not in providers:
            providers[key] = provider
            return
        reordered: dict[str, SegmentProvider] = {}
        for existing_key, existing in providers.items():
            if before is not None and existing_key == anchor:
                reordered[key] = provider
            reordered[existing_key] = existing
            if after is not None and existing_key == anchor:
                reordered[key] = provider
        self._segment_providers = reordered

    def remove_segment(self, key: str) -> bool:
        """Remove a segment provider by key; True when it existed."""

        return self._segment_providers.pop(key, None) is not None

    def render(
        self,
        state: CLIUIState,
        *,
        capabilities: Any,
        context: StatusBarContext | None = None,
        width: int | None = None,
        now: float | None = None,
        separator: str = STATUS_SEPARATOR,
    ) -> StatusBarRender:
        """Render a fitted status bar for the current state.

        ``separator`` must be cell-width-identical to ``STATUS_SEPARATOR``
        (the fitter's width math assumes 3 cells per joint); the turn-summary
        line passes its quieter middle-dot variant.
        """

        selected_context = context or StatusBarContext()
        current_time = time.monotonic() if now is None else now
        render_width = coerce_width(
            width if width is not None else getattr(capabilities, "width", 80)
        )
        segments = self._segments(
            state,
            capabilities=capabilities,
            context=selected_context,
            now=current_time,
        )
        fitted_segments = _fit_status_segment_records(
            segments, render_width, separator=separator
        )
        fitted = [segment.text for segment in fitted_segments]
        return StatusBarRender(
            text=separator.join(fitted),
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
        segments: list[StatusSegment] = []
        if self._layout is None:
            for provider in self._segment_providers.values():
                segment = provider(self, state, capabilities, context, now)
                if segment is not None:
                    segments.append(segment)
            return segments
        for ref in self._layout:
            segment = self._segment_for_ref(
                ref,
                state,
                capabilities=capabilities,
                context=context,
                now=now,
            )
            if segment is not None:
                segments.append(segment)
        return segments

    def _segment_for_ref(
        self,
        ref: str,
        state: CLIUIState,
        *,
        capabilities: Any,
        context: StatusBarContext,
        now: float,
    ) -> StatusSegment | None:
        if ref.startswith(TEXT_REF_PREFIX):
            literal = ref[len(TEXT_REF_PREFIX):].strip()
            if not literal:
                return None
            return StatusSegment(text=literal, priority=2, min_width=1)
        if ref.startswith(SCRIPT_REF_PREFIX):
            command = ref[len(SCRIPT_REF_PREFIX):].strip()
            if not command or self._script_source is None:
                return None
            try:
                text = self._script_source(command)
            except Exception:
                return None
            if not text:
                return None
            return StatusSegment(text=text, priority=2, min_width=1)
        provider = self._segment_providers.get(ref) or self._extra_providers.get(ref)
        if provider is None:
            return None
        return provider(self, state, capabilities, context, now)

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


# ----- built-in segment providers (render order = registration order) ----- #


def _brand_provider(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    capabilities: Any,
    context: StatusBarContext,
    now: float,
) -> StatusSegment | None:
    return StatusSegment(
        text="Nymeria",
        style_class="status.accent",
        priority=0,
        min_width=3,
        removable=False,
    )


def _activity_provider(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    capabilities: Any,
    context: StatusBarContext,
    now: float,
) -> StatusSegment | None:
    return StatusSegment(
        text=renderer.activity_segment(
            state,
            capabilities=capabilities,
            now=now,
            busy=context.busy,
        ),
        style_class="status.activity",
        priority=0,
        min_width=8,
        removable=False,
    )


def _notice_provider(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    capabilities: Any,
    context: StatusBarContext,
    now: float,
) -> StatusSegment | None:
    notice = select_status_notice(
        state,
        context.notice,
        now=now,
        default_ttl_seconds=renderer.default_notice_ttl_seconds,
    )
    if not notice:
        return None
    return StatusSegment(
        text=notice,
        style_class=_notice_style_class(
            context.notice,
            state,
            now=now,
            default_ttl_seconds=renderer.default_notice_ttl_seconds,
        ),
        priority=0,
        min_width=8,
        removable=False,
    )


def _connection_provider(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    capabilities: Any,
    context: StatusBarContext,
    now: float,
) -> StatusSegment | None:
    if not context.connection_label:
        return None
    return StatusSegment(text=context.connection_label, priority=1)


def _model_provider(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    capabilities: Any,
    context: StatusBarContext,
    now: float,
) -> StatusSegment | None:
    # Disconnected bars omit the model segment entirely; the "disconnected"
    # connection label already conveys state, and there is no backend model
    # to report (the local Settings default must never surface as active).
    model = "" if context.disconnected else (state.active_model or context.model)
    if not model:
        return None
    return StatusSegment(text=str(model), priority=2, min_width=8)


def _fast_provider(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    capabilities: Any,
    context: StatusBarContext,
    now: float,
) -> StatusSegment | None:
    if not context.fast_mode_active:
        return None
    return StatusSegment(
        text="FAST",
        style_class="status.accent",
        priority=1,
        min_width=4,
    )


def _reasoning_provider(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    capabilities: Any,
    context: StatusBarContext,
    now: float,
) -> StatusSegment | None:
    if not context.reasoning_label:
        return None
    return StatusSegment(
        text=context.reasoning_label,
        style_class="status.accent",
        priority=1,
        min_width=6,
    )


def _thread_provider(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    capabilities: Any,
    context: StatusBarContext,
    now: float,
) -> StatusSegment | None:
    thread = context.thread_label or state.thread_id or ""
    if not thread:
        return None
    return StatusSegment(
        text=f"thread {thread}",
        priority=0,
        min_width=10,
        removable=False,
    )


def _context_usage_provider(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    capabilities: Any,
    context: StatusBarContext,
    now: float,
) -> StatusSegment | None:
    context_usage = context_usage_label(
        state, compact_settings=context.compact_settings,
    )
    if not context_usage:
        return None
    ctx_usage = select_context_usage(state)
    ctx_pct = ctx_usage.get("percent_used")
    return StatusSegment(
        text=context_usage,
        style_class=_context_style_class(ctx_pct),
        priority=1,
        min_width=6,
    )


def _tokens_per_second_provider(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    capabilities: Any,
    context: StatusBarContext,
    now: float,
) -> StatusSegment | None:
    """Last turn's output rate (output tokens / LLM-stream seconds).

    Server-computed on the done event's ``context_stats``
    (``tokens_per_second``); a post-turn average that excludes tool
    execution. Hidden until the first recorded turn supplies a rate.
    """
    stats = state.context_stats or {}
    rate = stats.get("tokens_per_second")
    if not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate <= 0:
        return None
    label = f"{rate:.0f}" if rate >= 10 else f"{rate:.1f}"
    return StatusSegment(
        text=f"{label} tok/s",
        priority=2,
        min_width=7,
    )


def _queued_provider(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    capabilities: Any,
    context: StatusBarContext,
    now: float,
) -> StatusSegment | None:
    if context.queued_count <= 0:
        return None
    return StatusSegment(
        text=f"queued {context.queued_count}",
        priority=1,
        min_width=8,
    )


def _cwd_provider(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    capabilities: Any,
    context: StatusBarContext,
    now: float,
) -> StatusSegment | None:
    cwd = cwd_label(context.cwd)
    if not cwd:
        return None
    return StatusSegment(text=cwd, priority=3, min_width=8)


def _turn_time_provider(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    capabilities: Any,
    context: StatusBarContext,
    now: float,
) -> StatusSegment | None:
    """Last turn's total wall time: submit to done, tool execution included.

    Client-stamped by the reducer (``last_turn_duration_seconds``); absent
    until a turn completes in this session, so history replays and mid-turn
    viewer attaches never show a misleading partial time.
    """
    duration = state.last_turn_duration_seconds
    if duration is None:
        return None
    text = format_duration(duration)
    if state.last_turn_outcome == "cancelled":
        text = f"stopped after {text}"
    elif state.last_turn_outcome == "error":
        text = f"failed after {text}"
    return StatusSegment(text=text, priority=1, min_width=5)


def _cost_provider(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    capabilities: Any,
    context: StatusBarContext,
    now: float,
) -> StatusSegment | None:
    """Last turn's LLM cost, when the backend priced it (``cost_usd_last``)."""
    stats = state.context_stats or {}
    if stats.get("cost_unavailable"):
        return None
    cost = stats.get("cost_usd_last")
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost <= 0:
        return None
    if cost < 0.095:
        text = f"${cost:.4f}"
        if text == "$0.0000":
            # A sub-0.1-cent cost would render as zero; show a floor instead.
            text = "<$0.0001"
    else:
        text = f"${cost:.2f}"
    return StatusSegment(text=text, priority=2, min_width=5)


_DEFAULT_SEGMENT_PROVIDERS: dict[str, SegmentProvider] = {
    "brand": _brand_provider,
    "activity": _activity_provider,
    "notice": _notice_provider,
    "connection": _connection_provider,
    "model": _model_provider,
    "fast": _fast_provider,
    "reasoning": _reasoning_provider,
    "thread": _thread_provider,
    "context": _context_usage_provider,
    "tps": _tokens_per_second_provider,
    "queued": _queued_provider,
    "cwd": _cwd_provider,
}

# Built-in segments that are valid layout refs everywhere but deliberately
# NOT part of the default bar order (layout None): they exist for explicit
# layouts, primarily the turn-summary line's defaults.
_EXTRA_SEGMENT_PROVIDERS: dict[str, SegmentProvider] = {
    "turn_time": _turn_time_provider,
    "cost": _cost_provider,
}

DEFAULT_SEGMENT_KEYS: tuple[str, ...] = tuple(_DEFAULT_SEGMENT_PROVIDERS)
EXTRA_SEGMENT_KEYS: tuple[str, ...] = tuple(_EXTRA_SEGMENT_PROVIDERS)
ALL_SEGMENT_KEYS: tuple[str, ...] = DEFAULT_SEGMENT_KEYS + EXTRA_SEGMENT_KEYS


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


def _fmt_tokens(n: float) -> str:
    """Format token count compactly: 1.2M, 45.2k, or 800."""

    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{n:.0f}"


def _setting_value(settings: Any, key: str, default: Any = None) -> Any:
    """Read one field from a live Settings object or a settings mapping."""

    if isinstance(settings, Mapping):
        return settings.get(key, default)
    return getattr(settings, key, default)


def compact_trigger_display_tokens(
    settings: Any,
    context_limit: float | int | None,
) -> int | None:
    """Resolve the auto-compact trigger (in tokens) for display surfaces.

    Mirrors ``core/agent_compaction.py::compact_trigger_tokens`` semantics:
    ``compact_threshold_mode="percentage"`` multiplies the context limit by
    ``compact_threshold``; ``"tokens"`` clamps ``compact_threshold_tokens``
    to the context limit. Accepts a live Settings object or a settings
    mapping snapshot. Returns ``None`` when the trigger cannot be resolved
    (missing settings, unusable values, or an unknown context limit).
    """

    if settings is None or not context_limit or context_limit <= 0:
        return None
    mode = _setting_value(settings, "compact_threshold_mode", "tokens")
    if mode == "percentage":
        try:
            threshold = float(_setting_value(settings, "compact_threshold", 0.0))
        except (TypeError, ValueError):
            return None
        if not 0 < threshold < 1:
            return None
        return max(1, int(context_limit * threshold))
    try:
        tokens = int(_setting_value(settings, "compact_threshold_tokens", 0))
    except (TypeError, ValueError):
        return None
    if tokens <= 0:
        return None
    return max(1, min(tokens, int(context_limit)))


def _trigger_fraction(trigger: int | None, limit: float | None) -> float | None:
    """Bar-scaling fraction when the trigger sits below the context limit."""

    if trigger is None or not limit or limit <= 0:
        return None
    if 0 < trigger < limit:
        return trigger / limit
    return None


def _percentage_mode_fraction(settings: Any | None) -> float | None:
    """Raw percentage threshold for bars rendered without a known limit."""

    if settings is None:
        return None
    if _setting_value(settings, "compact_threshold_mode", "tokens") != "percentage":
        return None
    try:
        threshold = float(_setting_value(settings, "compact_threshold", 0.0))
    except (TypeError, ValueError):
        return None
    return threshold if 0 < threshold < 1 else None


def _ctx_bar(
    percent: float,
    width: int = 10,
    *,
    compact_threshold: float | None = None,
) -> str:
    """Return a filled/empty block bar scaled to the compact threshold.

    When *compact_threshold* is set the bar represents 0 → threshold so a
    full bar means auto-compact is about to fire.  Without a threshold the
    bar spans 0–100 % of the raw context window.
    """

    if compact_threshold is not None and 0 < compact_threshold < 1:
        scaled = min(100.0, max(0.0, percent / (compact_threshold * 100) * 100))
    else:
        scaled = max(0.0, min(100.0, percent))
    filled = round((scaled / 100) * width)
    return "█" * filled + "░" * (width - filled)


def context_usage_label(
    state: CLIUIState,
    *,
    compact_settings: Any | None = None,
) -> str:
    """Return compact context usage with graphical bar when stats are available."""

    usage = select_context_usage(state)
    percent = usage.get("percent_used")
    used = _first_number(usage, "used_tokens", "total_tokens", "input_tokens")
    limit = _first_number(usage, "max_tokens", "context_limit", "context_window", "limit")

    if used is not None and limit and isinstance(percent, (int, float)):
        # Prefer the backend-resolved trigger (honors per-thread threshold
        # overrides; null means auto-compact is off). The local settings
        # derivation remains the fallback for payloads without the key.
        if "compact_trigger_tokens" in usage:
            raw_trigger = usage.get("compact_trigger_tokens")
            trigger = (
                int(raw_trigger)
                if isinstance(raw_trigger, (int, float))
                and not isinstance(raw_trigger, bool)
                and raw_trigger > 0
                else None
            )
        else:
            trigger = compact_trigger_display_tokens(compact_settings, limit)
        bar = _ctx_bar(percent, compact_threshold=_trigger_fraction(trigger, limit))
        cap = _bar_cap_label(limit, trigger)
        return f"ctx {_fmt_tokens(used)}/{cap} [{bar}] {percent:.0f}%"

    if isinstance(percent, (int, float)):
        bar = _ctx_bar(
            percent, compact_threshold=_percentage_mode_fraction(compact_settings)
        )
        return f"ctx [{bar}] {percent:.0f}%"

    if used is not None:
        return f"ctx {_fmt_tokens(used)}"
    return ""


def _bar_cap_label(limit: float, trigger: int | None) -> str:
    """Label for the bar's right edge: the compact point or the full limit."""

    if trigger is not None and 0 < trigger < limit:
        return _fmt_tokens(trigger)
    return _fmt_tokens(limit)


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
    *,
    separator: str = STATUS_SEPARATOR,
) -> list[StatusSegment]:
    """Drop low-priority segment records, then truncate survivors to fit one line.

    ``separator`` only affects the collapsed last-resort record (everything
    joined and truncated into one segment); the width math assumes the
    3-cell ``STATUS_SEPARATOR`` regardless, which every caller's separator
    matches.
    """

    render_width = coerce_width(width)
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

    text = separator.join(segment.text for segment in active)
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


# ----- turn-summary line (rendered into the transcript at turn end) ------- #

TURN_SUMMARY_GLYPH = "❋"
TURN_SUMMARY_GLYPH_ASCII = "*"
_TURN_SUMMARY_SEPARATOR = " · "
_TURN_SUMMARY_SEPARATOR_ASCII = " | "

# context_stats keys that describe the LAST RECORDED turn (stamped by the
# done event) rather than the thread. An errored turn never refreshes them,
# so the summary line must not attribute the previous turn's numbers to it.
_TURN_LEVEL_STATS_KEYS = ("tokens_per_second", "cost_usd_last")


def render_turn_summary_text(
    renderer: StatusBarRenderer,
    state: CLIUIState,
    *,
    capabilities: Any,
    context: StatusBarContext | None = None,
    width: int | None = None,
    now: float | None = None,
    ascii_only: bool = False,
) -> str:
    """Render the end-of-turn summary line: glyph + configured segments.

    Reuses the status-bar segment registry and width fitting; the renderer's
    layout holds the turn bar's refs. The join separator differs from the
    live bars (middle dot, quieter) but is cell-width-identical to
    ``STATUS_SEPARATOR``, so the fitted segments still fit. Returns "" when
    no segment produced text, in which case nothing should be printed.
    """
    glyph = TURN_SUMMARY_GLYPH_ASCII if ascii_only else TURN_SUMMARY_GLYPH
    separator = (
        _TURN_SUMMARY_SEPARATOR_ASCII if ascii_only else _TURN_SUMMARY_SEPARATOR
    )
    if state.last_turn_outcome == "error":
        # An errored turn produced no fresh turn-level stats: context_stats
        # still carries the PREVIOUS turn's rate and cost, and this line is
        # a permanent transcript record. Strip those keys so tps/cost go
        # silent instead of crediting the old turn's numbers to this one
        # (thread-level stats like context occupancy stay).
        stripped = {
            key: value
            for key, value in (state.context_stats or {}).items()
            if key not in _TURN_LEVEL_STATS_KEYS
        }
        state = replace(state, context_stats=stripped)
    render_width = coerce_width(
        width if width is not None else getattr(capabilities, "width", 80)
    )
    body_width = max(1, render_width - _cell_width(glyph) - 1)
    result = renderer.render(
        state,
        capabilities=capabilities,
        context=context,
        width=body_width,
        now=now,
        separator=separator,
    )
    if not result.segments:
        return ""
    return f"{glyph} {separator.join(result.segments)}"


def _context_style_class(percent: float | int | None) -> str:
    """Return style class for context usage based on fill level."""

    if not isinstance(percent, (int, float)):
        return "status"
    if percent >= 95:
        return "status.notice.error"
    if percent >= 80:
        return "status.notice.warning"
    return "status"


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


__all__ = [
    "ALL_SEGMENT_KEYS",
    "DEFAULT_NOTICE_TTL_SECONDS",
    "DEFAULT_SEGMENT_KEYS",
    "EXTRA_SEGMENT_KEYS",
    "NoticeLevel",
    "SCRIPT_REF_PREFIX",
    "STATUS_SEPARATOR",
    "ScriptSource",
    "TEXT_REF_PREFIX",
    "TURN_SUMMARY_GLYPH",
    "TURN_SUMMARY_GLYPH_ASCII",
    "StatusBarContext",
    "StatusBarRender",
    "StatusBarRenderer",
    "StatusNotice",
    "StatusSegment",
    "context_usage_label",
    "cwd_label",
    "fit_status_segments",
    "format_duration",
    "render_turn_summary_text",
    "select_status_notice",
]
