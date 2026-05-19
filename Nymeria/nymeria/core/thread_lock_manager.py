"""Per-thread locking primitive shared by NymeriaAgent.

Extracted from ``NymeriaAgent``. The class is instantiated by the
agent's ``__init__`` (``self._thread_locks = ThreadLockManager()``)
and accessed by every external caller through ``agent._thread_locks``,
so the public interface to consumers is unchanged.

Uses ``threading.Lock`` (works across sync and async paths since
LangGraph releases the GIL during I/O). Tracks lock-holder metadata
for richer "queued" event info, and per-thread abort events for
cooperative cancellation.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional


class ThreadLockManager:
    """Per-thread locking to prevent concurrent access to the same conversation.

    Uses threading.Lock (works across both sync and async paths since
    LangGraph's graph invocations release the GIL during I/O).

    Also tracks lock holder metadata for richer "queued" event info.
    """

    def __init__(self):
        self._locks: Dict[str, threading.Lock] = {}
        self._lock_info: Dict[str, Dict[str, Any]] = {}
        self._abort_events: Dict[str, threading.Event] = {}
        self._meta_lock = threading.Lock()

    def get_lock(self, thread_id: str) -> threading.Lock:
        """Get or create a lock for a specific thread_id."""
        with self._meta_lock:
            if thread_id not in self._locks:
                self._locks[thread_id] = threading.Lock()
            return self._locks[thread_id]

    def set_lock_info(self, thread_id: str, holder: str, task_id: Optional[str] = None):
        """Record who holds the lock and when it was acquired."""
        with self._meta_lock:
            self._lock_info[thread_id] = {
                "holder": holder,
                "task_id": task_id,
                "acquired_at": time.time(),
            }

    def clear_lock_info(self, thread_id: str):
        """Clear lock holder metadata."""
        with self._meta_lock:
            self._lock_info.pop(thread_id, None)

    def get_abort_event(self, thread_id: str) -> threading.Event:
        """Get or create an abort event for a specific thread_id."""
        with self._meta_lock:
            if thread_id not in self._abort_events:
                self._abort_events[thread_id] = threading.Event()
            return self._abort_events[thread_id]

    def signal_abort(self, thread_id: str):
        """Signal the abort event for a thread, requesting cancellation."""
        with self._meta_lock:
            if thread_id not in self._abort_events:
                self._abort_events[thread_id] = threading.Event()
            self._abort_events[thread_id].set()

    def clear_abort(self, thread_id: str):
        """Clear the abort event so a new operation can start cleanly."""
        with self._meta_lock:
            if thread_id in self._abort_events:
                self._abort_events[thread_id].clear()

    def get_lock_info(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Get current lock holder info including held_seconds."""
        with self._meta_lock:
            info = self._lock_info.get(thread_id)
            if info:
                return {
                    **info,
                    "held_seconds": round(time.time() - info["acquired_at"], 1),
                }
            return None

    def is_thread_busy(self, thread_id: str) -> bool:
        """Non-blocking check whether a thread's lock is currently held."""
        lock = self.get_lock(thread_id)
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
            return False
        return True
