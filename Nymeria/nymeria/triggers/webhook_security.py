"""Shared webhook security helpers."""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable, Iterable
from typing import Any, Literal, Protocol

from fastapi import HTTPException

WEBHOOK_MAX_AGE_SECONDS = 10 * 60
WEBHOOK_MAX_FUTURE_SKEW_SECONDS = 5 * 60
TimestampUnit = Literal["seconds", "milliseconds"]


class _TimestampedMessage(Protocol):
    """Any inbound-message object exposing a provider event timestamp."""

    @property
    def timestamp(self) -> Any: ...


def require_configured_secret(value: str | None, setting_name: str) -> str:
    """Return a configured webhook secret, or raise 503 when it is unset.

    Used by webhook routers that gate signature verification on a
    server-configured secret (currently the WhatsApp router). The error detail
    names the missing setting verbatim so the per-router tests
    (e.g. ``WHATSAPP_APP_SECRET is required``) stay exact.
    """
    secret = (value or "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail=f"{setting_name} is required")
    return secret


def reject_stale_messages(
    payload: dict[str, Any],
    extract_messages: Callable[[dict[str, Any]], Iterable[_TimestampedMessage]],
    *,
    unit: TimestampUnit,
) -> None:
    """Raise 403 if any inbound message timestamp is outside the replay window.

    ``extract_messages`` is the platform's ``extract_inbound_messages`` and
    ``unit`` is the platform's timestamp unit (WhatsApp emits seconds). A
    payload with no inbound messages is a no-op, matching the prior
    per-router behavior.
    """
    for message in extract_messages(payload):
        if not webhook_timestamp_is_fresh(message.timestamp, unit=unit):
            raise HTTPException(status_code=403, detail="Stale webhook event")


def verify_meta_signature(
    raw_body: bytes,
    signature_header: str | None,
    app_secret: str | None,
) -> bool:
    """Verify Meta's X-Hub-Signature-256 header.

    Canonical HMAC-SHA256 verifier for Meta-style webhooks (currently the
    WhatsApp bot). Returns ``False`` (never raises) when the app secret or
    header is missing or malformed, and uses a constant-time comparison.
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
