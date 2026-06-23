"""Pin the API-side dream scheduler lifecycle.

In Docker the worker doesn't run the agent, and ``invoke_dream`` spawns the
dream turn in-process, so automatic dream scheduling must run on the API.
``create_api_app`` registers a heartbeat task on the API when the in-process
ticker is disabled to fill that gap. This test verifies:

1. The dream registrar wires both a startup and a shutdown event handler.
2. The startup handler records the task + stop event on ``app.state`` under
   the expected attribute names.
3. The shutdown path finds them and stops cleanly without sitting in the
   startup delay (the stop event short-circuits it).

The per-pass body is not exercised here: the registrar's startup delay is
60s, so the loop body never runs in a fast unit test. The generic engine's
per-pass execution and logging are covered in
``test_api_periodic_task_lifecycle.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI

from nymeria.triggers.api import _register_dream_scheduler_lifecycle


def _fake_agent_getter() -> Any:
    # Typed -> Any so it satisfies the registrar's Callable[[], NymeriaAgent]
    # parameter under static checking; the loop body is never run here.
    return SimpleNamespace()


def _startup_handlers(app: FastAPI) -> list[Any]:
    return [h for h in app.router.on_startup if asyncio.iscoroutinefunction(h)]


def _shutdown_handlers(app: FastAPI) -> list[Any]:
    return [h for h in app.router.on_shutdown if asyncio.iscoroutinefunction(h)]


def test_register_wires_startup_and_shutdown_handlers():
    app = FastAPI()

    _register_dream_scheduler_lifecycle(app, agent_getter=_fake_agent_getter)

    assert _startup_handlers(app), "expected a startup handler"
    assert _shutdown_handlers(app), "expected a shutdown handler"


def test_dream_lifecycle_records_state_and_stops_cleanly():
    """Startup records task + stop on app.state; shutdown stops cleanly."""
    app = FastAPI()

    _register_dream_scheduler_lifecycle(app, agent_getter=_fake_agent_getter)

    startup_handlers = _startup_handlers(app)
    shutdown_handlers = _shutdown_handlers(app)
    assert startup_handlers and shutdown_handlers

    async def run() -> None:
        await startup_handlers[0]()
        assert hasattr(app.state, "dream_scheduler_task")
        assert hasattr(app.state, "dream_scheduler_stop")
        # Dream scheduling uses a stop event only (no wake event).
        assert not hasattr(app.state, "dream_scheduler_wake")
        app.state.dream_scheduler_stop.set()
        await shutdown_handlers[0]()

    asyncio.run(run())
