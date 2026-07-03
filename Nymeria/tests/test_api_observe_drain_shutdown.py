"""Pin the API graceful-shutdown observe-hook drain (backlog #74B).

Observe hooks are scheduled off-turn (loop task or dispatch pool), so a
restart could drop side effects the turn already accepted. The API app
registers ``_drain_observe_hooks`` as a shutdown handler that runs both
drain barriers, each bounded so a hung hook cannot stall shutdown.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
import time

from nymeria.core.hooks import HookContext, HookEvent, HookRegistry
from nymeria.triggers.api import _drain_observe_hooks

dmod = importlib.import_module("nymeria.core.hooks.dispatch")


def _ctx() -> HookContext:
    return HookContext(
        event=HookEvent.DONE, thread_id="t1", user_id="u1", is_autonomous=False
    )


def test_shutdown_drain_flushes_pool_scheduled_observe():
    # No running loop at schedule time -> the observe lands on the dispatch
    # pool; the handler's drain_observe barrier must flush it.
    reg = HookRegistry()
    ran = threading.Event()
    reg.register(HookEvent.DONE, lambda c: ran.set(), observe=True)
    dmod.schedule_observe(HookEvent.DONE, _ctx(), registry=reg)

    asyncio.run(_drain_observe_hooks())
    assert ran.is_set()


def test_shutdown_drain_flushes_loop_scheduled_observe():
    # Scheduled with a running loop -> a loop task; the handler runs on that
    # same (shutdown) loop, so its adrain_observe barrier must flush it.
    reg = HookRegistry()
    seen = {}

    async def hook(c):
        seen["ran"] = True

    reg.register(HookEvent.DONE, hook, observe=True)

    async def _run():
        dmod.schedule_observe(HookEvent.DONE, _ctx(), registry=reg)
        assert not seen  # not started yet: this coroutine has not yielded
        await _drain_observe_hooks()

    asyncio.run(_run())
    assert seen.get("ran") is True


def test_shutdown_drain_is_bounded_by_hung_hook(monkeypatch):
    # A hook sleeping far past the bound must not stall shutdown: the handler
    # gives up at OBSERVE_DRAIN_TIMEOUT_SECONDS per barrier and swallows the
    # timeout (shutdown is best-effort).
    import nymeria.triggers.api as api_mod

    monkeypatch.setattr(api_mod, "OBSERVE_DRAIN_TIMEOUT_SECONDS", 0.2)
    reg = HookRegistry()
    release = threading.Event()
    reg.register(
        HookEvent.DONE, lambda c: release.wait(10.0), observe=True
    )
    # Give the hung hook the full author budget so the DISPATCH does not time
    # it out first; only the drain bound should cut the wait short.
    dmod.schedule_observe(HookEvent.DONE, _ctx(), registry=reg, timeout=30.0)

    started = time.monotonic()
    asyncio.run(_drain_observe_hooks())  # must not raise
    elapsed = time.monotonic() - started
    assert elapsed < 5.0
    # Unblock the worker so the pool is clean for later tests.
    release.set()
    dmod.drain_observe(timeout=5.0)
