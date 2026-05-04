"""Shared helpers for chat-platform bot thin clients."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, Optional, Protocol

import httpx


USER_CACHE_TTL_SECONDS = 30 * 60


class PlatformResolverAPI(Protocol):
    async def resolve_platform_user(self, platform: str, platform_user_id: str) -> Optional[str]:
        """Resolve a platform user id to a Nymeria user id."""


def fmt_tokens(n: Optional[int]) -> str:
    """Format token counts for compact bot status messages."""
    if not n:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def context_bar(usage_pct: float, width: int = 20, *, code: bool = False) -> str:
    """Render a text progress bar, optionally wrapped as inline code."""
    filled = int(width * usage_pct / 100) if usage_pct else 0
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    if code:
        bar = f"`{bar}`"
    return f"{bar} {usage_pct}%"


def coerce_value(value_str: str) -> Any:
    """Auto-convert command string values to bool/None/int/float/str."""
    lower = value_str.lower()
    if lower in ("true", "false"):
        return lower == "true"
    if lower == "none":
        return None
    try:
        return int(value_str)
    except ValueError:
        pass
    try:
        return float(value_str)
    except ValueError:
        pass
    return value_str


def http_error_detail(exc: httpx.HTTPStatusError, *, text_limit: int = 200) -> str:
    """Extract a concise user-facing detail from an HTTP status error."""
    response = exc.response
    if response is not None:
        try:
            body = response.json()
            if isinstance(body, dict) and "detail" in body:
                return str(body["detail"])
        except Exception:  # noqa: BLE001
            pass
        text = (response.text or "").strip()
        if text:
            return text[:text_limit]
        return f"HTTP {response.status_code}"
    return str(exc)


class UserResolver:
    """Cached platform-user resolver shared by bot thin clients."""

    def __init__(
        self,
        api: PlatformResolverAPI,
        platform: str,
        *,
        ttl_seconds: int = USER_CACHE_TTL_SECONDS,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api = api
        self._platform = platform
        self._ttl_seconds = ttl_seconds
        self._logger = logger or logging.getLogger(__name__)
        self._clock = clock
        self._cache: dict[str, tuple[Optional[str], float]] = {}

    async def resolve(self, platform_user_id: int | str) -> Optional[str]:
        """Resolve and cache a platform user id, including confirmed misses."""
        key = str(platform_user_id)
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None:
            value, expires_at = cached
            if now < expires_at:
                return value
        try:
            user_id = await self._api.resolve_platform_user(self._platform, key)
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "resolve_platform_user(%s, %s) failed: %s",
                self._platform,
                key,
                exc,
            )
            return None
        self._cache[key] = (user_id, now + self._ttl_seconds)
        return user_id

    def invalidate(self, platform_user_id: int | str) -> None:
        """Drop one cached platform-user lookup."""
        self._cache.pop(str(platform_user_id), None)
