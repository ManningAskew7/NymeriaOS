"""Pin the generic ``_register_periodic_task`` engine.

The spawn-housekeeping, dream-scheduler, and tool-index-warm lifecycles are
thin wrappers over this one engine, so the engine is where per-pass
execution, startup/shutdown wiring, app.state recording, the start-log line,
the error label, and the wake-event variant are exercised directly. Each
test runs exactly one pass by having ``run_pass`` set the stop event, which
makes the engine's ``if stop.is_set(): break`` end the loop deterministically
without relying on real timeouts.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI

from nymeria.triggers.api import _register_periodic_task


def test_engine_runs_pass_records_state_and_logs_start(caplog):
    app = FastAPI()
    calls: list[int] = []

    async def run_pass() -> None:
        calls.append(1)
        # End the loop after the first pass.
        app.state.demo_stop.set()

    n_start = len(app.router.on_startup)
    n_stop = len(app.router.on_shutdown)

    _register_periodic_task(
        app,
        state_prefix="demo",
        run_pass=run_pass,
        interval_seconds=0.01,
        startup_delay_seconds=0.01,
        start_log="Demo task started",
        error_label="Demo task",
    )

    # Exactly one startup and one shutdown handler added.
    assert len(app.router.on_startup) == n_start + 1
    assert len(app.router.on_shutdown) == n_stop + 1

    async def run() -> None:
        with caplog.at_level(logging.INFO, logger="nymeria.triggers.api"):
            await app.router.on_startup[-1]()
            assert hasattr(app.state, "demo_task")
            assert hasattr(app.state, "demo_stop")
            # No wake event unless use_wake_event=True.
            assert not hasattr(app.state, "demo_wake")
            await app.state.demo_task  # loop ends after the first pass
            await app.router.on_shutdown[-1]()

    asyncio.run(run())

    assert calls == [1]
    assert "Demo task started" in caplog.text


def test_engine_logs_error_label_when_pass_raises(caplog):
    app = FastAPI()

    async def run_pass() -> None:
        # Stop first so the loop exits after this (failing) pass.
        app.state.boom_stop.set()
        raise RuntimeError("kaboom")

    _register_periodic_task(
        app,
        state_prefix="boom",
        run_pass=run_pass,
        interval_seconds=0.01,
        startup_delay_seconds=0.01,
        start_log="Boom task started",
        error_label="Boom task",
    )

    async def run() -> None:
        with caplog.at_level(logging.ERROR, logger="nymeria.triggers.api"):
            await app.router.on_startup[-1]()
            # The engine swallows the per-pass exception, so awaiting the task
            # must not raise.
            await app.state.boom_task
            await app.router.on_shutdown[-1]()

    asyncio.run(run())

    assert "Boom task pass failed" in caplog.text


def test_engine_wake_variant_wires_wake_event_and_loop_start_hook():
    """The wake variant stores a wake event on app.state and passes that same
    object to ``on_loop_start`` before the startup delay (the wiring tool-index
    warm relies on so ``mark_dirty`` can set the loop's wake)."""
    app = FastAPI()
    seen: dict[str, object] = {}

    async def on_loop_start(stop: asyncio.Event, wake) -> None:
        seen["wake_is_app_state_wake"] = wake is app.state.warm_wake
        seen["ran_before_pass"] = "pass_ran" not in seen

    async def run_pass() -> None:
        seen["pass_ran"] = True
        app.state.warm_stop.set()

    _register_periodic_task(
        app,
        state_prefix="warm",
        run_pass=run_pass,
        interval_seconds=0.01,
        startup_delay_seconds=0.01,
        start_log="Warm task started",
        error_label="Warm task",
        use_wake_event=True,
        on_loop_start=on_loop_start,
    )

    async def run() -> None:
        await app.router.on_startup[-1]()
        assert hasattr(app.state, "warm_wake")
        assert app.state.warm_wake is not None
        await app.state.warm_task
        await app.router.on_shutdown[-1]()

    asyncio.run(run())

    assert seen.get("wake_is_app_state_wake") is True
    assert seen.get("ran_before_pass") is True
    assert seen.get("pass_ran") is True


async def _wait_until(predicate, *, attempts: int = 200, delay: float = 0.01) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(delay)
    raise AssertionError("condition not met in time")


def test_engine_wake_retriggers_next_pass_before_interval():
    """A wake set() returns the heartbeat wait early so the next pass runs
    without waiting out the (long) interval. Exercises the wake-driven
    re-trigger branch the tool-index warm loop depends on."""
    app = FastAPI()
    passes: list[int] = []

    async def run_pass() -> None:
        passes.append(len(passes))
        if len(passes) >= 2:
            app.state.retrig_stop.set()

    _register_periodic_task(
        app,
        state_prefix="retrig",
        run_pass=run_pass,
        # Long interval: the test would time out if it actually waited it, so
        # reaching pass 2 quickly proves the wake short-circuited the heartbeat.
        interval_seconds=30.0,
        startup_delay_seconds=0.01,
        start_log="Retrig task started",
        error_label="Retrig task",
        use_wake_event=True,
    )

    async def run() -> None:
        await app.router.on_startup[-1]()
        # Wait for pass 1 (after which the loop parks on the wake wait), then
        # wake it. Setting the wake only after pass 1 ensures iteration 1's
        # wake.clear() has already happened.
        await _wait_until(lambda: len(passes) >= 1)
        app.state.retrig_wake.set()
        await asyncio.wait_for(app.state.retrig_task, timeout=5.0)
        await app.router.on_shutdown[-1]()

    asyncio.run(run())

    assert passes == [0, 1]


def test_engine_survives_pass_exception_without_busy_spin(caplog):
    """A pass that raises without setting stop is logged, the loop survives,
    and it parks on the heartbeat (no busy-spin) until the next wake."""
    app = FastAPI()
    passes: list[int] = []

    async def run_pass() -> None:
        passes.append(len(passes))
        if len(passes) == 1:
            raise RuntimeError("boom")  # first pass fails, does NOT set stop
        app.state.surv_stop.set()  # second pass exits cleanly

    _register_periodic_task(
        app,
        state_prefix="surv",
        run_pass=run_pass,
        interval_seconds=30.0,
        startup_delay_seconds=0.01,
        start_log="Surv task started",
        error_label="Surv task",
        use_wake_event=True,
    )

    async def run() -> None:
        with caplog.at_level(logging.ERROR, logger="nymeria.triggers.api"):
            await app.router.on_startup[-1]()
            await _wait_until(lambda: len(passes) >= 1)
            # The loop is parked on the 30s wake wait after the failed pass.
            # If it busy-spun, more passes would accrue during this window.
            await asyncio.sleep(0.05)
            assert passes == [0]
            app.state.surv_wake.set()  # wake -> second pass runs and stops
            await asyncio.wait_for(app.state.surv_task, timeout=5.0)
            await app.router.on_shutdown[-1]()

    asyncio.run(run())

    assert passes == [0, 1]
    assert "Surv task pass failed" in caplog.text
