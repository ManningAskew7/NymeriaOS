"""Concurrency tests for ``BrowserThread`` (tools/browser.py).

These exercise the slice 17 / F7 step-1 fix without launching Playwright by
replacing the worker loop (``_run``) with a zero-arg fake worker that speaks the
same ``(command, args, reply)`` protocol, and patching the module-level
``_check_playwright_available`` so ``start()`` proceeds. The fixes under test:

* ``start()`` is synchronized so concurrent first-callers cannot spawn duplicate
  workers.
* each ``execute()`` carries its own reply queue so concurrent callers can never
  receive each other's results, and a timed-out call's late result never leaks
  to the next caller.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

import pytest

from nymeria.tools import browser as browser_mod
from nymeria.tools.browser import BrowserThread


def _install_fake_worker(
    bt: BrowserThread,
    handler: Callable[[str, dict], object],
    *,
    started_counter: Optional[list] = None,
    counter_lock: Optional[threading.Lock] = None,
) -> None:
    """Replace ``bt._run`` with a zero-arg fake worker using the real protocol.

    ``handler(command, args)`` computes the per-command result. A raised
    exception is translated to a ``(False, str(exc))`` reply, mirroring the real
    loop. If ``started_counter`` is given, the worker bumps it once on entry so a
    test can assert how many worker threads were actually created.
    """

    def fake_run() -> None:
        if started_counter is not None and counter_lock is not None:
            with counter_lock:
                started_counter[0] += 1
        bt._ready.set()
        while bt._running:
            try:
                command, args, reply = bt._command_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if command == "STOP":
                break
            try:
                result = handler(command, args)
                if reply is not None:
                    reply.put((True, result))
            except Exception as exc:  # broad, mirrors the real worker
                if reply is not None:
                    reply.put((False, str(exc)))

    # Instance attribute shadows the bound method; threading.Thread(target=...)
    # picks it up at start() time. It is a zero-arg closure (NOT a self-taking
    # function), which is required because Thread calls target() with no args.
    bt._run = fake_run  # type: ignore[method-assign]


@pytest.fixture
def fake_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``start()`` believe Playwright is installed."""
    monkeypatch.setattr(
        browser_mod, "_check_playwright_available", lambda: (True, "ok")
    )


def test_basic_dispatch_and_stop(fake_playwright: None) -> None:
    bt = BrowserThread()
    _install_fake_worker(bt, lambda command, args: {"echo": args})
    try:
        ok, result = bt.execute("echo", {"v": 1})
        assert ok is True
        assert result == {"echo": {"v": 1}}
    finally:
        bt.stop()
    # stop() drains the worker via the STOP sentinel without raising.
    assert bt._thread is not None
    assert not bt._thread.is_alive()


def test_handler_error_surfaces_as_failure(fake_playwright: None) -> None:
    bt = BrowserThread()

    def boom(command: str, args: dict) -> object:
        raise RuntimeError("kaboom")

    _install_fake_worker(bt, boom)
    try:
        ok, result = bt.execute("anything", {})
        assert ok is False
        assert "kaboom" in result
    finally:
        bt.stop()


def test_no_cross_talk_under_concurrency(fake_playwright: None) -> None:
    """Concurrent callers must each receive their OWN result.

    All N callers are held in flight (each blocked on its own reply queue) until
    the test releases the worker, so their results are produced and consumed in a
    racing window. With the per-call reply queue each caller gets back exactly the
    id it submitted. A single shared result queue would, with N=10 callers,
    almost certainly mis-deliver at least one result (the chance every blocked
    getter happens to wake for its own result is ~1/N!), so this is a strong
    regression guard; the deterministic guard is
    ``test_timed_out_result_does_not_leak_to_next_caller``.
    """
    n_callers = 10
    bt = BrowserThread()
    release = threading.Event()

    def handler(command: str, args: dict) -> object:
        # Hold each command until the test releases, so all callers are
        # concurrently blocked on their reply queues before any result flows.
        if not release.wait(timeout=5):
            raise AssertionError("release was never set")
        return {"id": args["id"]}

    _install_fake_worker(bt, handler)
    # Warm up the single worker so every concurrent call reuses it.
    assert bt.start()[0] is True

    results: dict[str, tuple] = {}
    results_lock = threading.Lock()
    start_together = threading.Barrier(n_callers)

    def call(caller_id: str) -> None:
        start_together.wait(timeout=5)
        ok, result = bt.execute("work", {"id": caller_id})
        with results_lock:
            results[caller_id] = (ok, result)

    ids = [f"caller-{i}" for i in range(n_callers)]
    threads = [threading.Thread(target=call, args=(cid,)) for cid in ids]
    try:
        for t in threads:
            t.start()
        # Give every thread a beat to enqueue its command, then release.
        time.sleep(0.2)
        release.set()
        for t in threads:
            t.join(timeout=5)

        assert set(results) == set(ids)
        for cid in ids:
            assert results[cid] == (True, {"id": cid})
    finally:
        bt.stop()


def test_timed_out_result_does_not_leak_to_next_caller(fake_playwright: None) -> None:
    """A slow command that times out must not deliver its late result to the
    next caller."""
    bt = BrowserThread()

    def handler(command: str, args: dict) -> object:
        if command == "slow":
            time.sleep(0.4)
            return {"id": "slow"}
        return {"id": args["id"]}

    _install_fake_worker(bt, handler)
    try:
        # This call times out well before the worker finishes the slow command.
        ok, result = bt.execute("slow", {}, timeout=0.1)
        assert ok is False
        assert "timed out" in result

        # The next call must get its OWN result, never the orphaned slow one.
        ok2, result2 = bt.execute("fast", {"id": "fast"}, timeout=5)
        assert ok2 is True
        assert result2 == {"id": "fast"}
    finally:
        bt.stop()


def test_concurrent_start_creates_single_worker(fake_playwright: None) -> None:
    """Concurrent first-callers must not spawn duplicate worker threads."""
    bt = BrowserThread()
    started_counter = [0]
    counter_lock = threading.Lock()
    _install_fake_worker(
        bt,
        lambda command, args: args,
        started_counter=started_counter,
        counter_lock=counter_lock,
    )

    barrier = threading.Barrier(8)
    oks: list = []
    oks_lock = threading.Lock()

    def starter() -> None:
        barrier.wait(timeout=5)
        ok, _ = bt.start()
        with oks_lock:
            oks.append(ok)

    threads = [threading.Thread(target=starter) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    try:
        assert all(oks)
        assert len(oks) == 8
        # Exactly one worker thread was ever created despite 8 concurrent starts.
        assert started_counter[0] == 1
    finally:
        bt.stop()
