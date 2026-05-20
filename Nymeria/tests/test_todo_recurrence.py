"""Tests for arbitrary-interval TODO recurrence parsing and validation."""

from __future__ import annotations

from datetime import timedelta

import pytest

from nymeria.core.todo_constants import (
    MIN_RECURRENCE_SECONDS,
    format_recurrence_for_display,
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
        ("monthly", timedelta(days=30)),
        ("5min", timedelta(minutes=5)),
        ("10min", timedelta(minutes=10)),
        ("15min", timedelta(minutes=15)),
        ("30min", timedelta(minutes=30)),
    ],
)
def test_parse_recurrence_legacy_aliases(raw: str, expected: timedelta):
    assert parse_recurrence_interval(raw) == expected


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("hourly", "1h"),
        ("daily", "1d"),
        ("weekly", "1w"),
        ("monthly", "30d"),
        ("5min", "5m"),
        ("30min", "30m"),
        ("2H", "2h"),
        ("  45m  ", "45m"),
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
    ["five minutes", "", " ", "5x", "1month", "h", "abc"],
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
        ("45m", "Every 45m"),
        ("2h", "Every 2h"),
        ("30d", "Every 30d"),
        ("monthly", "Every 30d"),
    ],
)
def test_format_recurrence_for_display(raw: str, label: str):
    assert format_recurrence_for_display(raw) == label


def test_format_recurrence_for_display_unparseable_falls_back_to_raw():
    assert format_recurrence_for_display("garbage") == "garbage"
    assert format_recurrence_for_display(None) == ""
