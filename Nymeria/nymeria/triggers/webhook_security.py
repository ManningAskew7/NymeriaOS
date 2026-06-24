"""Shared webhook security helpers."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Literal

WEBHOOK_MAX_AGE_SECONDS = 10 * 60
WEBHOOK_MAX_FUTURE_SKEW_SECONDS = 5 * 60
TimestampUnit = Literal["seconds", "milliseconds"]


def verify_meta_signature(
    raw_body: bytes,
    signature_header: str | None,
    app_secret: str | None,
) -> bool:
    """Verify Meta's X-Hub-Signature-256 header.

    Canonical HMAC-SHA256 verifier shared by the Messenger, Instagram, and
    WhatsApp webhook bots. Returns ``False`` (never raises) when the app secret
    or header is missing or malformed, and uses a constant-time comparison.
    """
    if not app_secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


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
