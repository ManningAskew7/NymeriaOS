"""Shared TODO formatting constants for Nymeria.

This module provides a single source of truth for TODO status icons,
recurrence parsing, and sorting orders used across the codebase.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from .time_utils import parse_duration
from .todo_manager import TodoStatus


# Status icons for display
STATUS_ICONS = {
    TodoStatus.PENDING: "[ ]",
    TodoStatus.IN_PROGRESS: "[>]",
    TodoStatus.DONE: "[x]",
}

# Status ordering for sorting (lower is higher priority)
STATUS_ORDER = {
    TodoStatus.IN_PROGRESS: 0,
    TodoStatus.PENDING: 1,
    TodoStatus.DONE: 2,
}

# Legacy preset names mapped to canonical duration strings. Stored TODO data
# and the agent tool vocabulary still use these; the parser resolves them
# before delegating to parse_duration so old JSON keeps working.
LEGACY_RECURRENCE_ALIASES = {
    "5min": "5m",
    "10min": "10m",
    "15min": "15m",
    "30min": "30m",
    "hourly": "1h",
    "daily": "1d",
    "weekly": "1w",
    "monthly": "30d",
}

MIN_RECURRENCE_SECONDS = 60

RECURRENCE_FORMAT_HINT = (
    "Format: Nm, Nh, Nd, Nw (or Ns for seconds, min 60s). "
    "Examples: 5m, 2h, 1d, 1w. "
    "Legacy names also accepted: hourly, daily, weekly, monthly."
)


def _canonicalize_recurrence(value: str) -> str:
    """Normalize a recurrence string (lowercase, trim, resolve legacy alias)."""
    cleaned = value.strip().lower()
    return LEGACY_RECURRENCE_ALIASES.get(cleaned, cleaned)


def parse_recurrence_interval(value: Optional[str]) -> Optional[timedelta]:
    """Parse a recurrence string to a timedelta.

    Accepts canonical durations ("5m", "2h", "1d", "1w", "30s") and legacy
    preset names ("hourly", "daily", "weekly", "monthly", "5min" ... "30min").
    Returns None when the input is empty or unparseable.
    """
    if not value:
        return None
    canonical = _canonicalize_recurrence(value)
    seconds = parse_duration(canonical)
    if seconds is None or seconds <= 0:
        return None
    return timedelta(seconds=seconds)


def validate_recurrence(value: str) -> str:
    """Validate and return the canonical duration string.

    Raises ValueError for invalid format or intervals shorter than
    MIN_RECURRENCE_SECONDS. Legacy preset names are resolved (e.g. "5min"
    becomes "5m"); other inputs are returned in their normalized form
    (lowercased, trimmed) without unit conversion ("300s" stays "300s").
    Callers should store the returned value verbatim.
    """
    if not value or not value.strip():
        raise ValueError(f"recurrence is empty. {RECURRENCE_FORMAT_HINT}")
    canonical = _canonicalize_recurrence(value)
    seconds = parse_duration(canonical)
    if seconds is None or seconds <= 0:
        raise ValueError(
            f"Invalid recurrence {value!r}. {RECURRENCE_FORMAT_HINT}"
        )
    if seconds < MIN_RECURRENCE_SECONDS:
        raise ValueError(
            f"recurrence {value!r} is below the {MIN_RECURRENCE_SECONDS}s minimum. "
            f"{RECURRENCE_FORMAT_HINT}"
        )
    return canonical


def format_recurrence_for_display(value: Optional[str]) -> str:
    """Return a human-friendly label for a recurrence string.

    Returns "Hourly" / "Daily" / "Weekly" for the exact 1h / 1d / 1w slots
    and "Every Nm" / "Every Nh" / etc. otherwise. Unparseable input falls
    back to the raw value so legacy data never renders as an empty string.
    """
    if not value:
        return ""
    canonical = _canonicalize_recurrence(value)
    if canonical == "1h":
        return "Hourly"
    if canonical == "1d":
        return "Daily"
    if canonical == "1w":
        return "Weekly"
    if parse_duration(canonical) is None:
        return value
    return f"Every {canonical}"


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def calculate_next_recurrence_time(
    recurrence: str,
    anchor_time: datetime,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Return the next future recurrence time from the scheduled anchor.

    The anchor is the intended fire time, not the later completion time. If the
    system missed one or more intervals, this skips forward to the next future
    slot while preserving the cadence.
    """
    delta = parse_recurrence_interval(recurrence)
    if delta is None:
        return None

    anchor = _ensure_aware_utc(anchor_time)
    baseline = _ensure_aware_utc(now) if now is not None else datetime.now(timezone.utc)
    next_time = anchor + delta
    if next_time <= baseline:
        delta_seconds = delta.total_seconds()
        if delta_seconds <= 0:
            return None
        missed_intervals = int((baseline - next_time).total_seconds() // delta_seconds) + 1
        next_time = next_time + (delta * missed_intervals)
    return next_time
