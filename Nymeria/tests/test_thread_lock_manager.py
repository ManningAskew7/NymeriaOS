"""Unit tests for the standalone ThreadLockManager primitive."""

from __future__ import annotations

import asyncio
import threading
import time

from nymeria.core.thread_lock_manager import (
    ThreadLockManager,
    async_event_wait,
    async_lock_acquire,
)


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
    mgr.set_lock_info("t1", holder="autonomous", task_id="task-42")
    info = mgr.get_lock_info("t1")
    assert info is not None
    assert info["holder"] == "autonomous"
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


def test_active_locks_lists_held_locks_with_metadata_and_skips_free_ones():
    mgr = ThreadLockManager()
    held = mgr.get_lock("t-busy")
    mgr.get_lock("t-free")  # known but not held
    held.acquire()
    mgr.set_lock_info("t-busy", holder="autonomous", task_id="task-1")
    try:
        active = mgr.active_locks()
        assert [entry["thread_id"] for entry in active] == ["t-busy"]
        assert active[0]["holder"] == "autonomous"
        assert active[0]["held_seconds"] >= 0
    finally:
        held.release()
        mgr.clear_lock_info("t-busy")
    assert mgr.active_locks() == []


def test_active_locks_reports_held_lock_without_metadata_as_unknown():
    # The held lock IS the activity signal; missing metadata must not hide it.
    mgr = ThreadLockManager()
    lock = mgr.get_lock("t-anon")
    lock.acquire()
    try:
        active = mgr.active_locks()
        assert active == [
            {"thread_id": "t-anon", "holder": "unknown", "held_seconds": None}
        ]
    finally:
        lock.release()


def test_active_locks_ignores_stale_metadata_when_lock_is_released():
    # A holder that crashed past clear_lock_info leaves metadata behind;
    # phantom activity would wedge the deploy-sync idle gate forever.
    mgr = ThreadLockManager()
    mgr.get_lock("t-stale")
    mgr.set_lock_info("t-stale", holder="user")
    assert mgr.active_locks() == []


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


# --- async wait helpers (poll-based, cancellation-safe) ---


def test_async_lock_acquire_free_lock_is_immediate():
    lock = threading.Lock()

    async def _scenario():
        return await async_lock_acquire(lock, timeout=1.0)

    assert asyncio.run(_scenario()) is True
    # The helper took the lock; the caller owns the release.
    assert lock.locked()
    lock.release()


def test_async_lock_acquire_times_out_without_leaking():
    lock = threading.Lock()
    lock.acquire()

    async def _scenario():
        return await async_lock_acquire(lock, timeout=0.15, poll_interval=0.01)

    start = time.monotonic()
    assert asyncio.run(_scenario()) is False
    assert time.monotonic() - start < 2.0

    # The defining property vs the old thread-parking pattern: after the
    # holder releases, nothing orphaned swoops in and takes the lock.
    lock.release()
    time.sleep(0.1)
    assert lock.acquire(blocking=False) is True
    lock.release()


def test_async_lock_acquire_wins_when_holder_releases():
    lock = threading.Lock()
    lock.acquire()

    async def _scenario():
        async def _release_soon():
            await asyncio.sleep(0.1)
            lock.release()

        releaser = asyncio.create_task(_release_soon())
        try:
            return await async_lock_acquire(lock, timeout=2.0, poll_interval=0.01)
        finally:
            await releaser

    assert asyncio.run(_scenario()) is True
    assert lock.locked()
    lock.release()


def test_async_lock_acquire_cancellation_leaves_lock_untaken():
    lock = threading.Lock()
    lock.acquire()

    async def _scenario():
        waiter = asyncio.create_task(
            async_lock_acquire(lock, timeout=5.0, poll_interval=0.01)
        )
        await asyncio.sleep(0.05)
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass

    asyncio.run(_scenario())
    # Release and confirm no residue of the cancelled wait holds or later
    # takes the lock (the old pattern leaked it held here).
    lock.release()
    time.sleep(0.1)
    assert lock.acquire(blocking=False) is True
    lock.release()


def test_async_event_wait_set_before_and_during():
    event = threading.Event()
    event.set()

    async def _already_set():
        return await async_event_wait(event, timeout=1.0)

    assert asyncio.run(_already_set()) is True

    event.clear()

    async def _set_mid_wait():
        async def _set_soon():
            await asyncio.sleep(0.1)
            event.set()

        setter = asyncio.create_task(_set_soon())
        try:
            return await async_event_wait(event, timeout=2.0, poll_interval=0.01)
        finally:
            await setter

    assert asyncio.run(_set_mid_wait()) is True


def test_async_event_wait_times_out():
    event = threading.Event()

    async def _scenario():
        return await async_event_wait(event, timeout=0.15, poll_interval=0.01)

    start = time.monotonic()
    assert asyncio.run(_scenario()) is False
    assert time.monotonic() - start < 2.0
