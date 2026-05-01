"""Sync bridges for consuming NymeriaAgent async streams from worker code."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterator, Optional


def iter_agent_astream(agent: Any, **kwargs: Any) -> Iterator[Dict[str, Any]]:
    """Yield ``agent.astream(...)`` chunks from synchronous worker contexts.

    Autonomous workers, callable tools, and spawned-thread dispatch are sync
    call sites, but regular chat uses ``NymeriaAgent.astream()`` so async-only
    tools can run. This bridge preserves live streaming while letting those sync
    callers use the same async agent path.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        yield from _iter_in_local_loop(agent, **kwargs)
        return

    raise RuntimeError("iter_agent_astream() is only supported from synchronous code")


def _iter_in_local_loop(agent: Any, **kwargs: Any) -> Iterator[Dict[str, Any]]:
    loop = asyncio.new_event_loop()
    agen: Optional[Any] = None
    previous_loop = None
    had_previous_loop = True
    try:
        try:
            previous_loop = asyncio.get_event_loop()
        except RuntimeError:
            had_previous_loop = False

        asyncio.set_event_loop(loop)
        agen = agent.astream(**kwargs).__aiter__()
        while True:
            try:
                yield loop.run_until_complete(agen.__anext__())
            except StopAsyncIteration:
                break
    finally:
        if agen is not None:
            try:
                loop.run_until_complete(agen.aclose())
            except RuntimeError:
                pass
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()
        if had_previous_loop:
            asyncio.set_event_loop(previous_loop)
        else:
            asyncio.set_event_loop(None)
