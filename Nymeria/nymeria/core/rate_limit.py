"""Small in-process rate limiting helpers."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Hashable


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: float = 0.0
    remaining: int = 0


class SlidingWindowRateLimiter:
    """Thread-safe sliding-window limiter keyed by caller-defined buckets."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
        prune_interval_seconds: float = 60.0,
    ):
        if max_keys <= 0:
            raise ValueError("max_keys must be positive")
        if prune_interval_seconds < 0:
            raise ValueError("prune_interval_seconds must be non-negative")

        self._clock = clock
        self._events: "OrderedDict[Hashable, Deque[float]]" = OrderedDict()
        self._windows: Dict[Hashable, float] = {}
        self._max_keys = max_keys
        self._prune_interval_seconds = prune_interval_seconds
        self._last_prune = 0.0
        self._lock = threading.Lock()

    def _prune_stale_locked(self, now: float) -> None:
        if (
            self._prune_interval_seconds
            and now - self._last_prune < self._prune_interval_seconds
        ):
            return

        for key, events in list(self._events.items()):
            window_seconds = self._windows.get(key)
            if not window_seconds:
                continue
            cutoff = now - window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if not events:
                self._events.pop(key, None)
                self._windows.pop(key, None)

        self._last_prune = now

    def _events_for_key_locked(self, key: Hashable) -> Deque[float]:
        events = self._events.get(key)
        if events is not None:
            self._events.move_to_end(key)
            return events

        while len(self._events) >= self._max_keys:
            evicted_key, _ = self._events.popitem(last=False)
            self._windows.pop(evicted_key, None)

        events = deque()
        self._events[key] = events
        return events

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
            self._prune_stale_locked(now)
            events = self._events_for_key_locked(key)
            self._windows[key] = window_seconds
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
            self._windows.clear()
            self._last_prune = 0.0
