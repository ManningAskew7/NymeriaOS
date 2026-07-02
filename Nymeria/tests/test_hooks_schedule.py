"""Tests for fire-and-forget observe scheduling (schedule_observe + drains).

Pass 5 makes the observe plane truly off-turn: fire points call
``schedule_observe``, which puts the dispatch on the running loop (task) or the
dedicated dispatch pool (no loop) and returns immediately. Per-hook semantics
(budgets, recording, fault swallowing) must be unchanged.
"""

from __future__ import annotations

import asyncio
import importlib
import threading
import time

from nymeria.core.hooks import HookContext, HookEvent, HookRegistry

dmod = importlib.import_module("nymeria.core.hooks.dispatch")


def ctx(event: HookEvent, **kw) -> HookContext:
    base = dict(event=event, thread_id="t1", user_id="u1", is_autonomous=False)
    base.update(kw)
    return HookContext(**base)


def _registry(recorder=None):
    reg = HookRegistry()
    reg.recorder = recorder
    return reg


def test_zero_hook_schedule_creates_nothing():
    reg = _registry()
    before_tasks = len(dmod._observe_tasks)
    before_futures = len(dmod._observe_futures)
    dmod.schedule_observe(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg)
    # The spine invariant: no task, no submit, nothing to clean up.
    assert len(dmod._observe_tasks) == before_tasks
    assert len(dmod._observe_futures) == before_futures


def test_schedule_runs_hook_without_a_loop():
    reg = _registry()
    ran = threading.Event()

    def hook(c):
        ran.set()

    reg.register(HookEvent.DONE, hook, observe=True)
    dmod.schedule_observe(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg)
    dmod.drain_observe()
    assert ran.is_set()


def test_schedule_runs_hook_on_the_running_loop():
    reg = _registry()
    seen = {}

    async def hook(c):
        seen["ran"] = True

    reg.register(HookEvent.DONE, hook, observe=True)

    async def _run():
        dmod.schedule_observe(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg)
        # Deterministically non-blocking: this coroutine has not yielded, so
        # the background task cannot have started yet.
        assert not seen
        await dmod.adrain_observe()

    asyncio.run(_run())
    assert seen.get("ran") is True
    assert not dmod._observe_tasks  # strong refs released after completion


def test_schedule_records_statuses_including_faults():
    calls = []

    def recorder(reg, c, *, status, detail, duration):
        calls.append(status)

    reg = _registry(recorder)

    def boom(c):
        raise ValueError("side effect failed")

    reg.register(HookEvent.DONE, boom, observe=True)
    dmod.schedule_observe(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg)
    dmod.drain_observe()
    assert calls == ["error"]


def test_scheduled_hook_still_bounded_by_timeout():
    statuses = []

    def recorder(reg, c, *, status, detail, duration):
        statuses.append(status)

    reg = _registry(recorder)

    def slow(c):
        time.sleep(0.5)

    reg.register(HookEvent.DONE, slow, name="slow", observe=True)
    dmod.schedule_observe(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg, timeout=0.05)
    dmod.drain_observe()
    assert statuses == ["timeout"]


def test_schedule_never_raises(monkeypatch):
    reg = _registry()
    monkeypatch.setattr(
        reg, "has_observe", lambda e: (_ for _ in ()).throw(RuntimeError("broken"))
    )
    # Must swallow and log, never raise into the fire point.
    dmod.schedule_observe(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg)
