"""Shared time parsing utilities for Nymeria."""

import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo


def get_user_tz() -> ZoneInfo:
    """Get the user's configured timezone as a ZoneInfo object."""
    from ..config import get_settings
    return ZoneInfo(get_settings().user_timezone)


def utc_now() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def ensure_aware_utc(value: datetime) -> datetime:
    """Normalize a datetime to timezone-aware UTC.

    Legacy JSON data may contain naive UTC timestamps. Treat those as UTC so
    comparisons keep working after new timestamps become timezone-aware.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_duration(duration_str: str) -> Optional[int]:
    """
    Parse a duration string to seconds.

    Supports formats like "30s", "5m", "1h", "1d".

    Args:
        duration_str: Duration string (e.g., "30s", "5m", "1h", "1d")

    Returns:
        Seconds as int, or None if invalid format
    """
    if not duration_str:
        return None

    match = re.match(r"^(\d+)(s|m|h|d)$", duration_str.lower().strip())
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]


def parse_scheduled_time(time_str: str, tz: Optional[tzinfo] = None) -> Optional[datetime]:
    """
    Parse a scheduled time string into a timezone-aware UTC datetime.

    Supports:
    - Relative: "30s", "5m", "1h", "1d"
    - Absolute: "2024-03-15 14:00", "2024-03-15T14:00" (interpreted in user timezone)

    Args:
        time_str: Time string to parse
        tz: Timezone for interpreting absolute times. If None, uses the
            user's configured timezone from settings.

    Returns:
        Timezone-aware datetime in UTC, or None if invalid format
    """
    if not time_str:
        return None

    time_str = time_str.strip()

    # Try relative format first: 30s, 5m, 1h, 1d (timezone-agnostic)
    seconds = parse_duration(time_str)
    if seconds is not None:
        return datetime.now(timezone.utc) + timedelta(seconds=seconds)

    # Resolve timezone for absolute time parsing
    if tz is None:
        tz = get_user_tz()

    # Try absolute formats - interpreted in the user's timezone
    formats = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]

    for fmt in formats:
        try:
            naive_dt = datetime.strptime(time_str, fmt)
            # Localize to user's timezone, then convert to UTC
            local_dt = naive_dt.replace(tzinfo=tz)
            return local_dt.astimezone(timezone.utc)
        except ValueError:
            continue

    return None


def parse_deadline(deadline_str: str) -> Optional[datetime]:
    """
    Parse a deadline string into a datetime.

    Args:
        deadline_str: Deadline string in various date formats

    Returns:
        datetime or None if invalid format
    """
    if not deadline_str:
        return None

    # Try common formats
    formats = [
        "%Y-%m-%d",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%m/%d/%Y",
        "%d/%m/%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(deadline_str, fmt)
        except ValueError:
            continue

    return None
