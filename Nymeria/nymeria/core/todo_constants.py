"""Shared TODO formatting constants for Nymeria.

This module provides a single source of truth for TODO status icons,
recurrence patterns, and sorting orders used across the codebase.
"""

from datetime import datetime, timedelta, timezone

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

# Valid recurrence patterns (single source of truth)
VALID_RECURRENCES = ['5min', '10min', '15min', '30min', 'hourly', 'daily', 'weekly', 'monthly']

# Recurrence deltas for calculating next execution time
RECURRENCE_DELTAS = {
    '5min': timedelta(minutes=5),
    '10min': timedelta(minutes=10),
    '15min': timedelta(minutes=15),
    '30min': timedelta(minutes=30),
    'hourly': timedelta(hours=1),
    'daily': timedelta(days=1),
    'weekly': timedelta(weeks=1),
    'monthly': timedelta(days=30),  # Approximate
}


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
    delta = RECURRENCE_DELTAS.get(recurrence)
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
