"""Shared helpers for chat-platform bot thin clients."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional, Protocol

import httpx


USER_CACHE_TTL_SECONDS = 30 * 60

# Defaults for the shared event-dedupe cache. A platform event id stays
# "seen" for this long, and the cache evicts down to half capacity once it
# exceeds the cap. Values match the per-bot constants the cache replaced.
SEEN_EVENT_TTL_SECONDS = 10 * 60
SEEN_EVENT_MAX = 5000


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
        pass  # not an integer, try float next
    try:
        return float(value_str)
    except ValueError:
        pass  # not a numeric value, return as string
    return value_str


def safe_id(value: Any) -> str:
    """Sanitize a value into a platform-safe id fragment.

    Collapses every run of characters outside ``[A-Za-z0-9_.-]`` to a single
    hyphen, trims leading/trailing hyphens, and falls back to ``"unknown"`` for
    empty or falsy input. This is the canonical builder for the native thread
    ids and platform-scoped keys every chat-platform bot generates, so the
    charset must stay stable. ``str(value or "")`` is the safe superset: any
    non-empty string is sanitized as-is, while ``None``/``0``/``""`` and other
    falsy inputs map to ``"unknown"`` rather than a literal ``"None"``.
    """
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-") or "unknown"


def normalize_base_url(base_url: str) -> str:
    """Strip a trailing slash from a self-hosted bot server base URL."""
    return base_url.rstrip("/")


def join_api_base(base_url: str, suffix: str) -> str:
    """Append an API ``suffix`` (e.g. ``"/api/v1"``) unless already present."""
    normalized = normalize_base_url(base_url)
    return normalized if normalized.endswith(suffix) else f"{normalized}{suffix}"


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


async def forward_backend_command(
    api: Any,
    raw_command: str,
    *,
    thread_id: str,
    user_id: str,
    surface: str,
    send: Callable[[str], Awaitable[None]],
    logger: logging.Logger | None = None,
) -> bool:
    """Forward a slash command to the backend command service and relay the result.

    Generic passthrough shared by the chat-platform bots: ``api`` needs only an
    ``execute_command`` method with the ``NymeriaAPIClient`` signature (the
    in-process webhook adapter mirrors it). The command result's markdown (or a
    terse fallback) is delivered through ``send``, which is expected to handle
    platform chunking/length limits (every bot's ``_send_text`` does).

    Returns True when the command was handled here (output or error relayed).
    Returns False when the caller should fall through to its normal chat path:
    commands with ``execution_kind == "chat_stream"`` (e.g. ``/skill``) are not
    executable by the command service and run as regular agent messages, which
    is exactly what the chat path already does with their raw text.
    """
    log = logger or logging.getLogger(__name__)
    try:
        result = await api.execute_command(
            raw_command,
            thread_id=thread_id,
            source="user",
            actor="user",
            surface=surface,
            user_id=user_id,
        )
    except httpx.HTTPStatusError as exc:
        log.error(
            "Backend command failed on %s: %s", surface, raw_command, exc_info=True
        )
        await send(f"Error: {http_error_detail(exc)}")
        return True
    except Exception as exc:  # noqa: BLE001
        log.error(
            "Backend command failed on %s: %s", surface, raw_command, exc_info=True
        )
        await send(f"Error: {exc}")
        return True

    if not isinstance(result, dict):
        log.error(
            "Backend command returned a non-dict result on %s: %s", surface, raw_command
        )
        await send("Command returned no output.")
        return True

    data = result.get("data")
    if (
        not result.get("success")
        and isinstance(data, dict)
        and data.get("execution_kind") == "chat_stream"
    ):
        return False

    text = str(result.get("markdown") or "").strip()
    if not text:
        text = "Done." if result.get("success") else "Command returned no output."
    await send(text)
    return True


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


class SeenEventCache:
    """TTL cache for deduping platform events by id, shared by bot clients.

    Used by the chat-platform bot thin clients and the API-hosted webhook
    routers to drop redelivered events. Keys expire after ``ttl_seconds`` and
    the cache evicts down to half ``max_items`` once it exceeds the cap.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = SEEN_EVENT_TTL_SECONDS,
        max_items: int = SEEN_EVENT_MAX,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_items = max_items
        self._clock = clock
        self._items: dict[str, float] = {}

    def mark_seen(self, key: str) -> bool:
        """Return True when *key* was already seen and still fresh."""
        now = self._clock()
        expires_at = self._items.get(key)
        if expires_at and expires_at > now:
            return True
        self._items[key] = now + self._ttl_seconds
        self._prune(now)
        return False

    def _prune(self, now: float) -> None:
        # Evict every expired key; when over the cap, also evict the oldest
        # insertion-ordered keys down to half capacity. Collect into a set (which
        # holds both the expired and the oldest-over-cap keys) so a key that is
        # both is listed, and popped, only once instead of twice.
        to_evict = {key for key, expiry in self._items.items() if expiry <= now}
        if len(self._items) > self._max_items:
            stale_count = len(self._items) - (self._max_items // 2)
            to_evict.update(list(self._items)[:stale_count])
        for key in to_evict:
            self._items.pop(key, None)
