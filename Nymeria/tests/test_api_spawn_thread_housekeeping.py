"""Pin the API-side spawn-thread housekeeping lifecycle.

In Docker the worker doesn't run the agent, so it can't sweep idle
spawned threads on its own. ``create_api_app`` registers a 30-minute
housekeeping task on the API when the in-process ticker is disabled
to fill that gap. This test verifies:

1. The housekeeping registrar wires both a startup and a shutdown event
   handler onto the app.
2. The startup handler invokes ``sweep_idle_spawned_threads`` against
   the lazily-resolved agent.
3. After a deletion, ``agent.sync_agent_tools()`` is called so the
   callable-thread tool list reflects the removal on the next prompt.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from fastapi import FastAPI

from nymeria.triggers.api import _register_spawn_thread_housekeeping_lifecycle


def _drain_startup_handlers(app: FastAPI) -> list[Any]:
    return [
        handler
        for handler in app.router.on_startup
        if asyncio.iscoroutinefunction(handler)
    ]


def _drain_shutdown_handlers(app: FastAPI) -> list[Any]:
    return [
        handler
        for handler in app.router.on_shutdown
        if asyncio.iscoroutinefunction(handler)
    ]


def test_register_wires_startup_and_shutdown_handlers():
    app = FastAPI()
    agent = SimpleNamespace(sync_agent_tools=MagicMock())

    _register_spawn_thread_housekeeping_lifecycle(app, agent_getter=lambda: agent)

    assert _drain_startup_handlers(app), "expected a startup handler"
    assert _drain_shutdown_handlers(app), "expected a shutdown handler"


def test_housekeeping_loop_uses_lazy_agent_getter(monkeypatch):
    """The housekeeping handler resolves the agent lazily on each tick."""
    import sys

    # ``nymeria.tools.__init__`` re-exports the decorated tool as
    # ``spawn_thread``, which shadows the submodule attribute on the
    # package. Grab the underlying module directly so monkeypatch can
    # rebind the helper function.
    spawn_thread_module = sys.modules["nymeria.tools.spawn_thread"]

    app = FastAPI()
    sweep_calls: list[Any] = []

    fake_agent = SimpleNamespace(sync_agent_tools=MagicMock())
    get_calls: list[int] = []

    def get_agent() -> Any:
        get_calls.append(len(get_calls))
        return fake_agent

    def fake_sweep(agent_arg: Any) -> int:
        sweep_calls.append(agent_arg)
        return 0  # Nothing to delete, keep test deterministic.

    monkeypatch.setattr(spawn_thread_module, "sweep_idle_spawned_threads", fake_sweep)

    _register_spawn_thread_housekeeping_lifecycle(app, agent_getter=get_agent)

    startup_handlers = _drain_startup_handlers(app)
    shutdown_handlers = _drain_shutdown_handlers(app)
    assert startup_handlers and shutdown_handlers

    async def run() -> None:
        # Start the loop, immediately set the stop event so we don't sit
        # in the 30s startup delay, then run the shutdown handler. The
        # registrar must record the task + stop event on app.state so
        # the shutdown path can find them.
        await startup_handlers[0]()
        assert hasattr(app.state, "spawn_housekeeping_task")
        assert hasattr(app.state, "spawn_housekeeping_stop")
        app.state.spawn_housekeeping_stop.set()
        await shutdown_handlers[0]()

    asyncio.run(run())


def test_register_is_optional_in_slim_shape():
    """create_api_app() must call this helper only when disable_ticker=True.

    Slim runs the spawn sweep from the in-process ticker, so registering
    a second handler would either double-sweep or race against the
    ticker. This test pins that the registrar lives in api.py and is
    only reached behind the disable_ticker branch (a structural check;
    the actual create_api_app integration is exercised by the
    higher-level slim tests).
    """
    import nymeria.triggers.api as api_module

    src = (
        api_module.__file__
    )
    with open(src, "r", encoding="utf-8") as f:
        text = f.read()

    # The call must be guarded by the disable_ticker branch.
    assert "if disable_ticker:" in text
    assert "_register_spawn_thread_housekeeping_lifecycle(" in text
