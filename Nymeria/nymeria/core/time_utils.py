"""Shared time parsing utilities for Nymeria."""

import re
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

TOOL_TTL_MAX_SECONDS = 365 * 24 * 60 * 60
TOOL_TTL_FORMAT_HINT = (
    "Format: Nm, Nh, Nd, Nw, or 'never'. "
    "Examples: 30m, 2h, 7d, 4w, never."
)
SCHEDULED_TIME_FORMAT_HINT = (
    "Use a future relative duration like '45s', '17m', '3h', '2d', or '1w', "
    "or an absolute time like 'YYYY-MM-DD HH:MM', 'YYYY-MM-DDTHH:MM', "
    "or an ISO datetime with timezone."
)


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

    Supports formats like "30s", "5m", "1h", "1d", "1w".

    Args:
        duration_str: Duration string (e.g., "30s", "5m", "1h", "1d", "1w")

    Returns:
        Seconds as int, or None if invalid format
    """
    if not duration_str:
        return None

    match = re.match(r"^(\d+)(s|m|h|d|w)$", duration_str.lower().strip())
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
    return value * multipliers[unit]


def parse_tool_ttl(raw: str) -> Tuple[str, Optional[int]]:
    """Parse a tool-binding TTL into a normalized key and seconds.

    Tool TTLs intentionally support coarser units than general scheduled
    durations: minutes, hours, days, weeks, or no expiry via "never" /
    "permanent".

    Raises:
        ValueError: If the value is blank, malformed, zero, or over one year.
    """
    value = ("" if raw is None else str(raw)).strip().lower()
    if value in {"never", "permanent"}:
        return value, None

    match = re.match(r"^(\d+)(m|h|d|w)$", value)
    if not match:
        raise ValueError(f"Invalid tool TTL {raw!r}. {TOOL_TTL_FORMAT_HINT}")

    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError(
            f"Invalid tool TTL {raw!r}: value must be greater than zero. "
            f"{TOOL_TTL_FORMAT_HINT}"
        )

    unit = match.group(2)
    multipliers = {"m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
    seconds = amount * multipliers[unit]
    if seconds > TOOL_TTL_MAX_SECONDS:
        raise ValueError(
            f"Invalid tool TTL {raw!r}: maximum is 365d or 52w. "
            f"{TOOL_TTL_FORMAT_HINT}"
        )

    return f"{amount}{unit}", seconds


def parse_scheduled_time(time_str: str, tz: Optional[tzinfo] = None) -> Optional[datetime]:
    """
    Parse a scheduled time string into a timezone-aware UTC datetime.

    Supports:
    - Relative: "30s", "5m", "1h", "1d", "1w"
    - Absolute: "2024-03-15 14:00", "2024-03-15T14:00" (interpreted in user timezone)
    - ISO with timezone: "2024-03-15T14:00:00Z", "2024-03-15T14:00:00-04:00"

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

    # Try relative format first: 30s, 5m, 1h, 1d, 1w (timezone-agnostic)
    seconds = parse_duration(time_str)
    if seconds is not None:
        return datetime.now(timezone.utc) + timedelta(seconds=seconds)

    # Resolve timezone for absolute time parsing
    if tz is None:
        tz = get_user_tz()

    # Python's ISO parser handles timezone offsets and fractional seconds.
    if "T" in time_str or " " in time_str:
        try:
            iso_value = time_str[:-1] + "+00:00" if time_str.endswith("Z") else time_str
            parsed = datetime.fromisoformat(iso_value)
            if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
                parsed = parsed.replace(tzinfo=tz)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass  # Not an ISO datetime; try configured absolute formats below.

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


def parse_future_scheduled_time(
    time_str: str,
    tz: Optional[tzinfo] = None,
    now: Optional[datetime] = None,
) -> datetime:
    """Parse a scheduled time and require it to be in the future."""
    scheduled = parse_scheduled_time(time_str, tz=tz)
    if scheduled is None:
        raise ValueError(
            f"Invalid scheduled_for format {time_str!r}. {SCHEDULED_TIME_FORMAT_HINT}"
        )

    scheduled = ensure_aware_utc(scheduled)
    baseline = ensure_aware_utc(now) if now is not None else utc_now()
    if scheduled <= baseline:
        raise ValueError(
            f"scheduled_for must be in the future; got {time_str!r}. "
            f"{SCHEDULED_TIME_FORMAT_HINT}"
        )
    return scheduled


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
