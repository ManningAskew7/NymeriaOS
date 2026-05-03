"""Tests for keyed lock helpers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from nymeria.core.keyed_locks import KeyedRLockMap


def test_keyed_rlock_map_returns_stable_lock_per_key():
    locks = KeyedRLockMap()

    assert locks.get("user-1") is locks.get("user-1")
    assert locks.get("user-1") is not locks.get("user-2")


def test_keyed_rlock_map_is_reentrant_for_same_thread():
    locks = KeyedRLockMap()

    with locks.locked("user-1"):
        lock = locks.get("user-1")
        assert lock.acquire(blocking=False) is True
        lock.release()


def test_keyed_rlock_map_concurrent_creation_returns_one_lock():
    locks = KeyedRLockMap()

    with ThreadPoolExecutor(max_workers=16) as executor:
        lock_ids = list(executor.map(lambda _: id(locks.get("user-1")), range(64)))

    assert len(set(lock_ids)) == 1
