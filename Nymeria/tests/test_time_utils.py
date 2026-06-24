"""Tests for shared time parsing helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nymeria.core.time_utils import (
    parse_future_scheduled_time,
    parse_scheduled_time,
    parse_tool_ttl,
    parse_usage_timestamp,
)
from nymeria.core.todo_constants import calculate_next_recurrence_time


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30m", ("30m", 30 * 60)),
        ("2h", ("2h", 2 * 3600)),
        ("7d", ("7d", 7 * 86400)),
        ("4w", ("4w", 4 * 7 * 86400)),
        ("never", ("never", None)),
        ("permanent", ("permanent", None)),
    ],
)
def test_parse_tool_ttl_accepts_supported_formats(raw: str, expected):
    assert parse_tool_ttl(raw) == expected


@pytest.mark.parametrize("raw", ["0h", "abc", "", "30s", "53w"])
def test_parse_tool_ttl_rejects_invalid_values(raw: str):
    with pytest.raises(ValueError):
        parse_tool_ttl(raw)


def test_parse_scheduled_time_accepts_arbitrary_relative_minutes_and_weeks():
    before = datetime.now(timezone.utc)

    scheduled = parse_scheduled_time("17m")
    week = parse_scheduled_time("1w")

    assert scheduled is not None
    assert timedelta(minutes=16, seconds=59) <= scheduled - before <= timedelta(minutes=17, seconds=2)
    assert week is not None
    assert timedelta(days=6, hours=23, minutes=59) <= week - before <= timedelta(days=7, seconds=2)


def test_parse_scheduled_time_accepts_iso_timezone():
    scheduled = parse_scheduled_time("2026-05-16T12:34:00-04:00")

    assert scheduled == datetime(2026, 5, 16, 16, 34, tzinfo=timezone.utc)


def test_parse_future_scheduled_time_rejects_past_absolute_time():
    with pytest.raises(ValueError, match="must be in the future"):
        parse_future_scheduled_time(
            "2026-05-15 09:00",
            tz=timezone.utc,
            now=datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc),
        )


# An east-of-UTC offset pushes 0001-01-01 below year 1 when the parsed absolute
# datetime is converted to UTC, which raises OverflowError out of astimezone; the
# relative cases overflow timedelta regardless of timezone.
_EAST_OF_UTC = timezone(timedelta(hours=10))


@pytest.mark.parametrize(
    "value, tz",
    [
        ("999999999999d", timezone.utc),
        ("9999999999999999w", timezone.utc),
        ("0001-01-01 00:00", _EAST_OF_UTC),
    ],
)
def test_parse_scheduled_time_treats_out_of_range_as_unparseable(value: str, tz):
    # A magnitude/date that cannot be represented must come back as None (the
    # "invalid" contract) rather than raising OverflowError out of the parser.
    assert parse_scheduled_time(value, tz=tz) is None


@pytest.mark.parametrize(
    "value, tz",
    [
        ("999999999999d", timezone.utc),
        ("0001-01-01 00:00", _EAST_OF_UTC),
    ],
)
def test_parse_future_scheduled_time_rejects_out_of_range_as_value_error(value: str, tz):
    # Out-of-range values surface through the existing invalid-format ValueError,
    # so every caller (HTTP routers, command backend, agent tool) renders a clean
    # error instead of a 500/OverflowError.
    with pytest.raises(ValueError, match="Invalid scheduled_for format"):
        parse_future_scheduled_time(value, tz=tz)


@pytest.mark.parametrize("value", [None, "", "not-a-timestamp", "2026-13-99"])
def test_parse_usage_timestamp_returns_none_for_blank_or_invalid(value):
    assert parse_usage_timestamp(value) is None


def test_parse_usage_timestamp_normalizes_to_aware_utc():
    # Offset-aware ISO is converted to UTC.
    assert parse_usage_timestamp("2026-05-16T12:34:00-04:00") == datetime(
        2026, 5, 16, 16, 34, tzinfo=timezone.utc
    )
    # Naive ISO is treated as UTC.
    assert parse_usage_timestamp("2026-05-16T12:34:00") == datetime(
        2026, 5, 16, 12, 34, tzinfo=timezone.utc
    )


def test_calculate_next_recurrence_time_preserves_schedule_anchor():
    anchor = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    completion_time = datetime(2026, 5, 15, 10, 3, tzinfo=timezone.utc)

    assert calculate_next_recurrence_time("hourly", anchor, now=completion_time) == datetime(
        2026,
        5,
        15,
        11,
        0,
        tzinfo=timezone.utc,
    )


def test_calculate_next_recurrence_time_skips_missed_intervals():
    anchor = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    now = datetime(2026, 5, 15, 12, 5, tzinfo=timezone.utc)

    assert calculate_next_recurrence_time("hourly", anchor, now=now) == datetime(
        2026,
        5,
        15,
        13,
        0,
        tzinfo=timezone.utc,
    )


def test_calculate_next_recurrence_time_arbitrary_interval_skips_missed():
    # Anchor 10:00 with 45m cadence fires at 10:45, 11:30, 12:15.
    # At "now = 11:30" the 11:30 slot is not strictly in the future, so the
    # next slot is 12:15.
    anchor = datetime(2026, 5, 15, 10, 0, tzinfo=timezone.utc)
    now = datetime(2026, 5, 15, 11, 30, tzinfo=timezone.utc)

    assert calculate_next_recurrence_time("45m", anchor, now=now) == datetime(
        2026, 5, 15, 12, 15, tzinfo=timezone.utc
    )
