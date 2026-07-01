"""Per-thread scratch store: the PostToolUse -> Done cross-event channel.

A hook records a fact in one event (e.g. an edited-files list in POST_TOOL_USE)
that a later event on the same turn consumes (e.g. a DONE gate). The store is
in-memory and does not survive a restart.

The store is exposed to hook logic as a *data shape*, never a live handle: the
dispatcher reads a read-only snapshot into ``HookContext.scratch`` before running
hooks, and applies each outcome's ``scratch_patch`` afterwards. This keeps the
cross-event channel inside the primitives-only contract so it survives the future
move to an out-of-process sandbox. Access is ``threading.Lock``-guarded because
hooks run on both the event loop and threadpool workers.
"""

from __future__ import annotations

import copy
import logging
import threading
from collections.abc import Mapping
from types import MappingProxyType
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_EMPTY: Mapping[str, object] = MappingProxyType({})


def _deepcopy_guarded(src: Mapping) -> Dict[str, object]:
    """Per-value deep copy that skips (and logs) any value it cannot copy.

    A scratch read/write must never raise into a turn, so one un-deep-copyable
    value (a stateful ``__deepcopy__``, a ``RecursionError``-deep structure)
    cannot fail the whole snapshot/patch: it is dropped, the rest survive.
    """
    out: Dict[str, object] = {}
    for key, value in src.items():
        try:
            out[key] = copy.deepcopy(value)
        except Exception:  # noqa: BLE001 - a scratch copy must never crash a turn
            logger.warning("scratch value %r is not deep-copyable; dropping", key, exc_info=True)
    return out


class ScratchStore:
    """Bounded per-thread key/value store with snapshot-in, patch-out access."""

    def __init__(self, *, max_threads: int = 512, max_keys_per_thread: int = 256) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, object]] = {}
        self._order: List[str] = []  # insertion order, for thread eviction
        self._max_threads = max_threads
        self._max_keys = max_keys_per_thread

    def snapshot(self, thread_id: str) -> Mapping[str, object]:
        """Return a read-only, deep-copied view of a thread's scratch (empty if none).

        The copy is deep so a hook that mutates a nested value in its snapshot
        cannot corrupt the shared store (the snapshot is handed across the
        event loop AND threadpool workers). The ``MappingProxyType`` wrapper
        gives an immediate read-only signal at the top level.

        The (potentially O(n)) deep copy runs OUTSIDE the lock, over a top-level
        reference grabbed under it: the store never mutates a nested value in
        place (``apply_patch`` replaces whole keys with fresh copies), so the
        referenced values are stable to copy without holding the lock. Copying
        is per-value guarded, so a snapshot can never raise into a turn.
        """
        with self._lock:
            data = self._data.get(thread_id)
            if not data:
                return _EMPTY
            shallow = dict(data)  # top-level snapshot under the lock
        return MappingProxyType(_deepcopy_guarded(shallow))

    def apply_patch(self, thread_id: str, patch: Optional[Mapping]) -> None:
        """Merge ``patch`` into a thread's scratch, creating it if needed.

        Non-mapping patches are ignored (a malformed hook outcome must not
        corrupt the store). Values are deep-copied OUTSIDE the lock (so a value's
        ``__deepcopy__`` can neither re-enter the store and deadlock nor stall
        other threads under the lock) and per-value guarded (so an un-copyable
        value is dropped, never raised, and never escalates a hook decision).
        """
        if not patch or not isinstance(patch, Mapping):
            return
        copied = _deepcopy_guarded(patch)
        if not copied:
            return
        with self._lock:
            data = self._data.get(thread_id)
            if data is None:
                data = {}
                self._data[thread_id] = data
                self._order.append(thread_id)
                self._evict_threads_locked()
            data.update(copied)
            if len(data) > self._max_keys:
                # Drop oldest-inserted keys, keep the newest max_keys.
                for key in list(data.keys())[: len(data) - self._max_keys]:
                    del data[key]

    def clear(self, thread_id: Optional[str] = None) -> None:
        """Clear one thread's scratch, or all of it when ``thread_id`` is None."""
        with self._lock:
            if thread_id is None:
                self._data.clear()
                self._order.clear()
                return
            self._data.pop(thread_id, None)
            if thread_id in self._order:
                self._order.remove(thread_id)

    def _evict_threads_locked(self) -> None:
        """Evict oldest threads past the cap. Caller holds the lock."""
        while len(self._order) > self._max_threads:
            oldest = self._order.pop(0)
            self._data.pop(oldest, None)
