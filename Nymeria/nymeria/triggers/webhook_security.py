"""Shared webhook security helpers."""

from __future__ import annotations

import time
from typing import Any, Literal

WEBHOOK_MAX_AGE_SECONDS = 10 * 60
WEBHOOK_MAX_FUTURE_SKEW_SECONDS = 5 * 60
TimestampUnit = Literal["seconds", "milliseconds"]


def webhook_timestamp_is_fresh(
    value: Any,
    *,
    unit: TimestampUnit,
    now: float | None = None,
    max_age_seconds: int = WEBHOOK_MAX_AGE_SECONDS,
    max_future_skew_seconds: int = WEBHOOK_MAX_FUTURE_SKEW_SECONDS,
) -> bool:
    """Return whether a provider event timestamp is within the replay window."""
    timestamp = _parse_epoch_timestamp(value, unit=unit)
    if timestamp is None:
        return False
    current = time.time() if now is None else now
    if timestamp < current - max_age_seconds:
        return False
    return timestamp <= current + max_future_skew_seconds


def _parse_epoch_timestamp(value: Any, *, unit: TimestampUnit) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    if unit == "milliseconds":
        timestamp /= 1000.0
    return timestamp
