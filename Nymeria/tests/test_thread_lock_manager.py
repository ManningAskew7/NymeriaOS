"""Unit tests for the standalone ThreadLockManager primitive."""

from __future__ import annotations

import threading
import time

from nymeria.core.thread_lock_manager import ThreadLockManager


def test_get_lock_returns_same_lock_for_same_thread():
    mgr = ThreadLockManager()
    lock_a = mgr.get_lock("t1")
    lock_b = mgr.get_lock("t1")
    lock_c = mgr.get_lock("t2")
    assert lock_a is lock_b
    assert lock_a is not lock_c


def test_lock_info_roundtrip():
    mgr = ThreadLockManager()
    before = time.time()
    mgr.set_lock_info("t1", holder="astream", task_id="task-42")
    info = mgr.get_lock_info("t1")
    assert info is not None
    assert info["holder"] == "astream"
    assert info["task_id"] == "task-42"
    assert info["acquired_at"] >= before
    assert info["held_seconds"] >= 0

    mgr.clear_lock_info("t1")
    assert mgr.get_lock_info("t1") is None


def test_abort_event_signal_and_clear():
    mgr = ThreadLockManager()
    event_a = mgr.get_abort_event("t1")
    event_b = mgr.get_abort_event("t1")
    assert event_a is event_b
    assert not event_a.is_set()

    mgr.signal_abort("t1")
    assert event_a.is_set()

    mgr.clear_abort("t1")
    assert not event_a.is_set()

    # signal_abort on an unknown thread should create the event and set it
    mgr.signal_abort("new-thread")
    assert mgr.get_abort_event("new-thread").is_set()


def test_is_thread_busy_when_held_and_free():
    mgr = ThreadLockManager()
    assert mgr.is_thread_busy("t1") is False

    lock = mgr.get_lock("t1")
    held = threading.Event()
    release = threading.Event()

    def worker():
        with lock:
            held.set()
            release.wait(timeout=2.0)

    t = threading.Thread(target=worker)
    t.start()
    try:
        assert held.wait(timeout=2.0)
        assert mgr.is_thread_busy("t1") is True
    finally:
        release.set()
        t.join(timeout=2.0)

    assert mgr.is_thread_busy("t1") is False


def test_get_lock_info_returns_none_for_unknown_thread():
    mgr = ThreadLockManager()
    assert mgr.get_lock_info("nonexistent") is None
