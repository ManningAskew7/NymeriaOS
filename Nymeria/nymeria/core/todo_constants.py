"""Shared TODO formatting constants for Nymeria.

This module provides a single source of truth for TODO status icons,
recurrence parsing, and sorting orders used across the codebase.
"""

import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from dateutil.relativedelta import relativedelta

from .time_utils import parse_duration, utc_now
from .todo_manager import TodoItem, TodoStatus


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
    "monthly": "1mo",
}

MIN_RECURRENCE_SECONDS = 60

RECURRENCE_FORMAT_HINT = (
    "Format: Nm, Nh, Nd, Nw, Nmo (or Ns for seconds, min 60s). "
    "Examples: 5m, 2h, 1d, 1w, 1mo. "
    "Legacy names also accepted: hourly, daily, weekly, monthly."
)

_MONTH_PATTERN = re.compile(r"^(\d+)mo$")

RecurrenceDelta = Union[timedelta, relativedelta]


def _canonicalize_recurrence(value: str) -> str:
    """Normalize a recurrence string (lowercase, trim, resolve legacy alias)."""
    cleaned = value.strip().lower()
    return LEGACY_RECURRENCE_ALIASES.get(cleaned, cleaned)


def _parse_month_interval(canonical: str) -> Optional[relativedelta]:
    """Return a calendar-aware monthly delta for "Nmo" inputs, else None."""
    match = _MONTH_PATTERN.match(canonical)
    if not match:
        return None
    months = int(match.group(1))
    if months <= 0:
        return None
    return relativedelta(months=months)


def parse_recurrence_interval(value: Optional[str]) -> Optional[RecurrenceDelta]:
    """Parse a recurrence string to a calendar- or duration-based delta.

    Accepts canonical durations ("5m", "2h", "1d", "1w", "30s"), calendar
    months ("1mo", "3mo"), and legacy preset names ("hourly", "daily",
    "weekly", "monthly", "5min" ... "30min"). Month intervals return a
    ``dateutil.relativedelta`` so calendar arithmetic stays correct across
    months of different lengths; everything else returns a ``timedelta``.
    Returns None when the input is empty or unparseable.
    """
    if not value:
        return None
    canonical = _canonicalize_recurrence(value)
    month_delta = _parse_month_interval(canonical)
    if month_delta is not None:
        return month_delta
    seconds = parse_duration(canonical)
    if seconds is None or seconds <= 0:
        return None
    return timedelta(seconds=seconds)


def is_calendar_month_recurrence(value: Optional[str]) -> bool:
    """Return True for calendar-month intervals ("Nmo" / legacy "monthly").

    These are the only intervals whose next slot must be derived from a stable
    origin (see ``calculate_next_recurrence_time``'s ``origin`` argument) so a
    month-end day (29-31) clamps to short months without drifting downward.
    """
    if not value:
        return False
    return _parse_month_interval(_canonicalize_recurrence(value)) is not None


def validate_recurrence(value: str) -> str:
    """Validate and return the canonical duration string.

    Raises ValueError for invalid format or sub-minute intervals. Legacy
    preset names are resolved (e.g. "5min" becomes "5m", "monthly" becomes
    "1mo"); other inputs are returned in their normalized form (lowercased,
    trimmed) without unit conversion ("300s" stays "300s"). Callers should
    store the returned value verbatim.
    """
    if not value or not value.strip():
        raise ValueError(f"recurrence is empty. {RECURRENCE_FORMAT_HINT}")
    canonical = _canonicalize_recurrence(value)
    if _parse_month_interval(canonical) is not None:
        # Month intervals are always above the 60s floor.
        return canonical
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

    Returns "Hourly" / "Daily" / "Weekly" / "Monthly" for the exact
    1h / 1d / 1w / 1mo slots and "Every Nm" / "Every Nmo" / etc. otherwise.
    Unparseable input falls back to the raw value so legacy data never
    renders as an empty string.
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
    if canonical == "1mo":
        return "Monthly"
    if _parse_month_interval(canonical) is not None:
        return f"Every {canonical}"
    if parse_duration(canonical) is None:
        return value
    return f"Every {canonical}"


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _months_between(start: datetime, end: datetime) -> int:
    """Whole calendar months from ``start`` to ``end`` (ignoring day/time)."""
    return (end.year - start.year) * 12 + (end.month - start.month)


def _next_month_slot_from_origin(
    origin: datetime,
    delta: relativedelta,
    anchor: datetime,
    baseline: datetime,
) -> datetime | None:
    """Next calendar-month slot derived from a stable ``origin``.

    Each slot is ``origin + relativedelta(months=k)`` for an increasing
    multiple ``k`` of the interval. Deriving from the origin (rather than from
    the previous, possibly clamped, slot) is what keeps a month-end anchor from
    drifting: Jan 31 -> Feb 28 -> Mar 31 -> Apr 30, never Feb 28 -> Mar 28.
    """
    interval = delta.years * 12 + delta.months
    if interval <= 0:
        return None
    # Slot index of the current anchor measured from the origin, advanced by
    # one interval to land on the next slot, then skipped forward past any
    # missed intervals so the result is always in the future.
    k = _months_between(origin, anchor) + interval
    next_time = origin + relativedelta(months=k)
    while next_time <= baseline:
        k += interval
        next_time = origin + relativedelta(months=k)
    return next_time


def calculate_next_recurrence_time(
    recurrence: str,
    anchor_time: datetime,
    *,
    now: datetime | None = None,
    origin: datetime | None = None,
) -> datetime | None:
    """Return the next future recurrence time from the scheduled anchor.

    The anchor is the intended fire time, not the later completion time. If the
    system missed one or more intervals, this skips forward to the next future
    slot while preserving the cadence. Month-based intervals use calendar
    arithmetic via ``relativedelta``, so a TODO anchored on the 31st clamps to
    the last day of shorter months (Feb 28/29) the same way Google Calendar
    handles "monthly on the 31st".

    ``origin`` is the stable first-fire time of a calendar-month series. When
    provided for a month interval, each slot is derived from the origin
    (``origin + N months``) rather than from the previous (possibly clamped)
    anchor, so a month-end day does not drift downward over successive fires.
    It is ignored for fixed-duration intervals and when ``None`` (which
    preserves the pre-origin anchor-relative behavior).
    """
    delta = parse_recurrence_interval(recurrence)
    if delta is None:
        return None

    anchor = _ensure_aware_utc(anchor_time)
    baseline = _ensure_aware_utc(now) if now is not None else utc_now()

    if isinstance(delta, relativedelta) and origin is not None:
        return _next_month_slot_from_origin(
            _ensure_aware_utc(origin), delta, anchor, baseline
        )

    next_time = anchor + delta
    if next_time <= baseline:
        if isinstance(delta, timedelta):
            delta_seconds = delta.total_seconds()
            if delta_seconds <= 0:
                return None
            missed_intervals = int((baseline - next_time).total_seconds() // delta_seconds) + 1
            next_time = next_time + (delta * missed_intervals)
        else:
            # relativedelta has no fixed length; step forward calendar by
            # calendar. Bounded by missed-month count, so at most a few dozen
            # iterations for realistic downtime.
            while next_time <= baseline:
                next_time = next_time + delta
    return next_time


def resolve_done_recurrence_anchor(item: Optional[TodoItem]) -> datetime:
    """Return the OCCURRENCE a "done" transition should advance the series from.

    Every done-path (the ``nym_todo`` tool and the MCP completion, the REST
    complete / PATCH-to-done endpoints, and the ``/todos complete`` command)
    resolves its anchor here so the four cannot drift apart. Order:
    ``last_execution``, then ``scheduled_for``, then now.

    ``last_execution`` comes first because the schedule row and
    ``scheduled_for`` have TWO writers for one occurrence. The ticker re-arms at
    the end of every successful run (``Ticker._handle_recurrence``, anchored on
    its own poll-time snapshot), and the human confirming late makes the agent
    mark the same occurrence done afterwards. Anchoring on the row the ticker
    just advanced turned day N+1 into day N+2 and consumed day N+1 without
    dispatching it: five medication reminders were silently skipped in
    production before 2026-08-26. ``last_execution`` names the slot that
    actually ran, so it does not move under the second writer.

    Both orderings then land the same slot, which is what a double-writer
    design needs. Post-finalize: ``last_execution`` is day N, one interval is
    day N+1, already the armed slot, so a late done is a no-op. Pre-finalize
    (mid-turn): ``last_execution`` is still day N-1, one interval lands on day
    N which is already past, and ``calculate_next_recurrence_time``'s
    skip-forward normalizes it to day N+1. Idempotent by construction rather
    than by luck.

    Two anchor sources it deliberately tolerates:

    - ``todo_manager.clear_todo_schedule`` writes ``last_execution=utc_now()``
      (a completion time, not a slot), but no recurring TODO with a usable
      recurrence can reach it. See the proof comment at that writer.
    - A resumed TODO (#154 / #247 auto-pause, or any explicit reschedule)
      carries a ``last_execution`` older than its new ``scheduled_for``,
      because the resume writes only the schedule. Advancing from the stale
      slot is then more than one interval behind, so skip-forward still lands
      the next FUTURE slot, keeping the original cadence rather than
      re-anchoring on the operator's new time. It never skips an occurrence.
    """
    if item is not None:
        if item.last_execution is not None:
            return _ensure_aware_utc(item.last_execution)
        if item.scheduled_for is not None:
            return _ensure_aware_utc(item.scheduled_for)
    return utc_now()


def compute_recurrence_reschedule(
    recurrence: str,
    anchor: datetime,
    existing_origin: datetime | None,
    *,
    now: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Resolve a recurring TODO's next slot plus the origin to persist.

    Single source of truth for the stable-origin handling shared by every
    reschedule path (ticker success + retry give-up, the ``/done`` tool, the
    REST complete / PATCH-to-done endpoints, and the slash-command completion)
    so they cannot drift apart.

    For calendar-month intervals the next slot is derived from a stable origin
    (``existing_origin`` when already stored, otherwise the current ``anchor``,
    adopted lazily) so a month-end day (29-31) clamps to short months without
    creeping downward over successive fires. Fixed-duration intervals ignore
    the origin entirely.

    Returns ``(next_time, origin_to_persist)``: ``next_time`` is None when the
    recurrence has no further slot; ``origin_to_persist`` is the value the
    caller should write to ``TodoItem.recurrence_anchor`` (set only the first
    time a month series adopts its origin, None otherwise so a stored origin is
    never overwritten).
    """
    is_monthly = is_calendar_month_recurrence(recurrence)
    origin = (existing_origin or anchor) if is_monthly else None
    next_time = calculate_next_recurrence_time(
        recurrence, anchor, now=now, origin=origin
    )
    origin_to_persist = origin if (is_monthly and existing_origin is None) else None
    return next_time, origin_to_persist
