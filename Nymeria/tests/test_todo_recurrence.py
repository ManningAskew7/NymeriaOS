"""Tests for arbitrary-interval TODO recurrence parsing and validation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from dateutil.relativedelta import relativedelta

from nymeria.core.todo_constants import (
    MIN_RECURRENCE_SECONDS,
    calculate_next_recurrence_time,
    compute_recurrence_reschedule,
    format_recurrence_for_display,
    is_calendar_month_recurrence,
    parse_recurrence_interval,
    validate_recurrence,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5m", timedelta(minutes=5)),
        ("2h", timedelta(hours=2)),
        ("3d", timedelta(days=3)),
        ("1w", timedelta(weeks=1)),
        ("90s", timedelta(seconds=90)),
    ],
)
def test_parse_recurrence_canonical_durations(raw: str, expected: timedelta):
    assert parse_recurrence_interval(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("hourly", timedelta(hours=1)),
        ("daily", timedelta(days=1)),
        ("weekly", timedelta(weeks=1)),
        ("5min", timedelta(minutes=5)),
        ("10min", timedelta(minutes=10)),
        ("15min", timedelta(minutes=15)),
        ("30min", timedelta(minutes=30)),
    ],
)
def test_parse_recurrence_legacy_aliases(raw: str, expected: timedelta):
    assert parse_recurrence_interval(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected_months"),
    [
        ("1mo", 1),
        ("2mo", 2),
        ("12mo", 12),
        ("monthly", 1),
    ],
)
def test_parse_recurrence_months_returns_relativedelta(raw: str, expected_months: int):
    delta = parse_recurrence_interval(raw)
    assert isinstance(delta, relativedelta)
    assert delta == relativedelta(months=expected_months)


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("hourly", "1h"),
        ("daily", "1d"),
        ("weekly", "1w"),
        ("monthly", "1mo"),
        ("5min", "5m"),
        ("30min", "30m"),
        ("2H", "2h"),
        ("  45m  ", "45m"),
        ("1mo", "1mo"),
        ("3mo", "3mo"),
        ("  2MO ", "2mo"),
    ],
)
def test_validate_recurrence_returns_canonical_form(raw: str, canonical: str):
    assert validate_recurrence(raw) == canonical


@pytest.mark.parametrize(
    "raw",
    ["30s", "0m", "59s"],
)
def test_validate_recurrence_rejects_below_minimum(raw: str):
    with pytest.raises(ValueError, match=str(MIN_RECURRENCE_SECONDS)):
        validate_recurrence(raw)


@pytest.mark.parametrize(
    "raw",
    ["five minutes", "", " ", "5x", "1month", "h", "abc", "0mo", "mo", "1.5mo"],
)
def test_validate_recurrence_rejects_garbage(raw: str):
    with pytest.raises(ValueError):
        validate_recurrence(raw)


def test_validate_recurrence_rejects_none_via_empty_guard():
    # validate_recurrence is documented for non-None input but should fail
    # cleanly if a caller forwards a falsy value.
    with pytest.raises(ValueError):
        validate_recurrence("")  # explicit empty
    assert parse_recurrence_interval(None) is None


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        ("1h", "Hourly"),
        ("hourly", "Hourly"),
        ("1d", "Daily"),
        ("daily", "Daily"),
        ("1w", "Weekly"),
        ("weekly", "Weekly"),
        ("1mo", "Monthly"),
        ("monthly", "Monthly"),
        ("45m", "Every 45m"),
        ("2h", "Every 2h"),
        ("30d", "Every 30d"),
        ("3mo", "Every 3mo"),
    ],
)
def test_format_recurrence_for_display(raw: str, label: str):
    assert format_recurrence_for_display(raw) == label


def test_format_recurrence_for_display_unparseable_falls_back_to_raw():
    assert format_recurrence_for_display("garbage") == "garbage"
    assert format_recurrence_for_display(None) == ""


# --- calculate_next_recurrence_time: month-aware behaviour ---


def _utc(*args, **kwargs) -> datetime:
    return datetime(*args, **kwargs, tzinfo=timezone.utc)


def test_monthly_advances_one_calendar_month_not_thirty_days():
    anchor = _utc(2026, 1, 15, 11, 0)
    # baseline just after the anchor so we get the next slot, not a skip
    now = _utc(2026, 1, 15, 11, 0, 1)
    next_time = calculate_next_recurrence_time("1mo", anchor, now=now)
    assert next_time == _utc(2026, 2, 15, 11, 0)


def test_monthly_legacy_alias_uses_calendar_month():
    anchor = _utc(2026, 1, 15, 11, 0)
    now = _utc(2026, 1, 15, 11, 0, 1)
    next_time = calculate_next_recurrence_time("monthly", anchor, now=now)
    # Was: 2026-02-14 11:00 (30d drift). Now: 2026-02-15 11:00.
    assert next_time == _utc(2026, 2, 15, 11, 0)


def test_monthly_jan31_clamps_to_feb28_non_leap():
    anchor = _utc(2026, 1, 31, 11, 0)
    now = _utc(2026, 1, 31, 11, 0, 1)
    next_time = calculate_next_recurrence_time("1mo", anchor, now=now)
    assert next_time == _utc(2026, 2, 28, 11, 0)


def test_monthly_jan31_clamps_to_feb29_in_leap_year():
    anchor = _utc(2028, 1, 31, 11, 0)
    now = _utc(2028, 1, 31, 11, 0, 1)
    next_time = calculate_next_recurrence_time("1mo", anchor, now=now)
    assert next_time == _utc(2028, 2, 29, 11, 0)


def test_monthly_skips_missed_intervals_on_long_downtime():
    # Anchor in January, but the system was down through April: should land
    # on the next future slot (May 15), not on each missed month.
    anchor = _utc(2026, 1, 15, 11, 0)
    now = _utc(2026, 4, 20, 9, 0)
    next_time = calculate_next_recurrence_time("1mo", anchor, now=now)
    assert next_time == _utc(2026, 5, 15, 11, 0)


def test_three_month_interval_uses_calendar_arithmetic():
    anchor = _utc(2026, 1, 15, 11, 0)
    now = _utc(2026, 1, 15, 11, 0, 1)
    next_time = calculate_next_recurrence_time("3mo", anchor, now=now)
    assert next_time == _utc(2026, 4, 15, 11, 0)


def test_daily_skip_forward_uses_seconds_path_unchanged():
    # Regression guard: daily/weekly/etc. should keep using the timedelta
    # skip-forward path, not the relativedelta loop.
    anchor = _utc(2026, 1, 15, 11, 0)
    now = _utc(2026, 1, 20, 11, 0, 30)  # 5 days + 30s late
    next_time = calculate_next_recurrence_time("1d", anchor, now=now)
    assert next_time == _utc(2026, 1, 21, 11, 0)


# --- calculate_next_recurrence_time: stable-origin month-end anchoring ---


def test_monthly_origin_prevents_month_end_drift():
    # The drift regression: deriving each slot from the previous *clamped* slot
    # sends Jan 31 -> Feb 28 -> Mar 28 -> ... . With a stable origin the series
    # recovers the 31st in longer months.
    origin = _utc(2026, 1, 31, 11, 0)
    slot = calculate_next_recurrence_time(
        "1mo", origin, now=_utc(2026, 1, 31, 11, 0, 1), origin=origin
    )
    assert slot == _utc(2026, 2, 28, 11, 0)
    slot = calculate_next_recurrence_time(
        "1mo", slot, now=_utc(2026, 2, 28, 11, 0, 1), origin=origin
    )
    assert slot == _utc(2026, 3, 31, 11, 0)  # NOT Mar 28
    slot = calculate_next_recurrence_time(
        "1mo", slot, now=_utc(2026, 3, 31, 11, 0, 1), origin=origin
    )
    assert slot == _utc(2026, 4, 30, 11, 0)
    slot = calculate_next_recurrence_time(
        "1mo", slot, now=_utc(2026, 4, 30, 11, 0, 1), origin=origin
    )
    assert slot == _utc(2026, 5, 31, 11, 0)  # recovers the 31st


def test_monthly_origin_skips_missed_intervals_to_future_month_end():
    origin = _utc(2026, 1, 31, 11, 0)
    # Current slot is the clamped Feb 28; the box was down until Apr 20. The
    # origin series is Jan31, Feb28, Mar31, Apr30, ...; the first slot after
    # Apr 20 is Apr 30 (clamped from the Jan-31 origin, drift-free), not Mar 31.
    slot = calculate_next_recurrence_time(
        "1mo", _utc(2026, 2, 28, 11, 0), now=_utc(2026, 4, 20, 9, 0), origin=origin
    )
    assert slot == _utc(2026, 4, 30, 11, 0)


def test_three_month_origin_series_clamps_without_drift():
    origin = _utc(2026, 1, 31, 11, 0)
    slot = calculate_next_recurrence_time(
        "3mo", origin, now=_utc(2026, 1, 31, 11, 0, 1), origin=origin
    )
    assert slot == _utc(2026, 4, 30, 11, 0)
    slot = calculate_next_recurrence_time(
        "3mo", slot, now=_utc(2026, 4, 30, 11, 0, 1), origin=origin
    )
    assert slot == _utc(2026, 7, 31, 11, 0)  # NOT Jul 30


def test_origin_ignored_for_fixed_duration_intervals():
    # A daily interval keeps its timedelta anchor-relative behavior even when an
    # origin is (harmlessly) supplied.
    anchor = _utc(2026, 1, 15, 11, 0)
    slot = calculate_next_recurrence_time(
        "1d", anchor, now=_utc(2026, 1, 15, 11, 0, 30), origin=_utc(2026, 1, 1, 11, 0)
    )
    assert slot == _utc(2026, 1, 16, 11, 0)


def test_origin_none_preserves_anchor_relative_behavior():
    # Back-compat guard: without an origin, monthly stays anchor-relative (the
    # pre-fix behavior), so legacy callers are unaffected.
    anchor = _utc(2026, 2, 28, 11, 0)
    slot = calculate_next_recurrence_time("1mo", anchor, now=_utc(2026, 2, 28, 11, 0, 1))
    assert slot == _utc(2026, 3, 28, 11, 0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1mo", True),
        ("3mo", True),
        ("monthly", True),
        ("1d", False),
        ("2h", False),
        ("1w", False),
        ("90s", False),
        (None, False),
        ("", False),
    ],
)
def test_is_calendar_month_recurrence(value, expected):
    assert is_calendar_month_recurrence(value) is expected


# --- compute_recurrence_reschedule: the shared reschedule contract ---


def test_compute_recurrence_reschedule_adopts_then_pins_month_origin():
    origin_slot = _utc(2026, 1, 31, 11, 0)
    # First reschedule: no stored origin -> adopt the current slot and tell the
    # caller to persist it.
    nxt, persist = compute_recurrence_reschedule(
        "1mo", origin_slot, None, now=_utc(2026, 1, 31, 11, 0, 1)
    )
    assert nxt == _utc(2026, 2, 28, 11, 0)
    assert persist == origin_slot
    # Second: a stored origin is present -> derive from it (drift-free) and do
    # NOT ask to overwrite it.
    nxt2, persist2 = compute_recurrence_reschedule(
        "1mo", nxt, origin_slot, now=_utc(2026, 2, 28, 11, 0, 1)
    )
    assert nxt2 == _utc(2026, 3, 31, 11, 0)
    assert persist2 is None


def test_compute_recurrence_reschedule_ignores_origin_for_fixed_duration():
    nxt, persist = compute_recurrence_reschedule(
        "1d", _utc(2026, 1, 15, 11, 0), None, now=_utc(2026, 1, 15, 11, 0, 30)
    )
    assert nxt == _utc(2026, 1, 16, 11, 0)
    assert persist is None  # fixed-duration series never adopt an origin


def test_compute_recurrence_reschedule_returns_none_for_unparseable():
    nxt, persist = compute_recurrence_reschedule(
        "garbage", _utc(2026, 1, 15, 11, 0), None
    )
    assert nxt is None
    assert persist is None
