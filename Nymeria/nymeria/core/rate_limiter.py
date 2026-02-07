"""Rate limiter for self-invokes to prevent runaway loops."""

import threading
import time
from collections import defaultdict
from typing import Dict, List


class RateLimiter:
    """
    Rate limiter for self-invokes to prevent runaway loops.

    Uses a sliding window to limit invokes per hour per user.
    """

    WINDOW_SECONDS = 3600  # 1 hour
    DEFAULT_MAX_PER_HOUR = 50

    def __init__(self, max_per_hour: int = DEFAULT_MAX_PER_HOUR):
        """
        Initialize the rate limiter.

        Args:
            max_per_hour: Maximum self_invoke calls per user per hour
        """
        self.max_per_hour = max_per_hour
        self._counts: Dict[str, List[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def check_and_record(self, user_id: str) -> tuple[bool, int]:
        """
        Check if user can make another self_invoke and record it.

        Args:
            user_id: User to check

        Returns:
            Tuple of (allowed, remaining_count)
        """
        now = time.time()
        cutoff = now - self.WINDOW_SECONDS

        with self._lock:
            # Prune old entries
            self._counts[user_id] = [t for t in self._counts[user_id] if t > cutoff]

            current = len(self._counts[user_id])
            remaining = self.max_per_hour - current

            if remaining <= 0:
                return False, 0

            # Record this invocation
            self._counts[user_id].append(now)
            return True, remaining - 1

    def get_remaining(self, user_id: str) -> int:
        """
        Get remaining invokes for this hour.

        Args:
            user_id: User to check

        Returns:
            Number of remaining invokes
        """
        now = time.time()
        cutoff = now - self.WINDOW_SECONDS

        with self._lock:
            count = len([t for t in self._counts[user_id] if t > cutoff])
            return max(0, self.max_per_hour - count)
