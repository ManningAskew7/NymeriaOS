"""Small helpers for keyed in-process locks."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Dict, Iterator


class KeyedRLockMap:
    """Thread-safe factory for re-entrant locks keyed by an arbitrary string."""

    def __init__(self) -> None:
        self._locks: Dict[str, threading.RLock] = {}
        self._meta_lock = threading.Lock()

    def get(self, key: str) -> threading.RLock:
        """Return the stable lock for ``key``, creating it if needed."""
        with self._meta_lock:
            if key not in self._locks:
                self._locks[key] = threading.RLock()
            return self._locks[key]

    @contextmanager
    def locked(self, key: str) -> Iterator[None]:
        """Acquire the lock for ``key`` for the duration of the context."""
        lock = self.get(key)
        with lock:
            yield
