"""Small in-process rate limiting helpers."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Hashable


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: float = 0.0
    remaining: int = 0


class SlidingWindowRateLimiter:
    """Thread-safe sliding-window limiter keyed by caller-defined buckets."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._events: Dict[Hashable, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(
        self,
        key: Hashable,
        *,
        limit: int,
        window_seconds: float,
    ) -> RateLimitResult:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")

        now = self._clock()
        cutoff = now - window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()

            if len(events) >= limit:
                retry_after = max(0.0, window_seconds - (now - events[0]))
                return RateLimitResult(
                    allowed=False,
                    retry_after=retry_after,
                    remaining=0,
                )

            events.append(now)
            return RateLimitResult(
                allowed=True,
                retry_after=0.0,
                remaining=max(0, limit - len(events)),
            )

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
