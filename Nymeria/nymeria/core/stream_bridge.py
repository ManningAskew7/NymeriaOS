"""Sync bridges for consuming NymeriaAgent async streams from worker code."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger(__name__)


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
    chunk_count = 0
    started_at = time.monotonic()
    thread_id = kwargs.get("thread_id", "")
    user_id = kwargs.get("user_id", "")
    is_autonomous = bool(kwargs.get("_is_self_invoke"))
    if is_autonomous:
        logger.info(
            "[STREAM_BRIDGE] start thread=%s user=%s autonomous=True",
            thread_id,
            user_id,
        )
    try:
        try:
            previous_loop = asyncio.get_event_loop()
        except RuntimeError:
            had_previous_loop = False

        asyncio.set_event_loop(loop)
        agen = agent.astream(**kwargs).__aiter__()
        while True:
            try:
                chunk = loop.run_until_complete(agen.__anext__())
                chunk_count += 1
                if is_autonomous and chunk_count == 1:
                    logger.info(
                        "[STREAM_BRIDGE] first_chunk thread=%s type=%s after_ms=%d",
                        thread_id,
                        chunk.get("type"),
                        int((time.monotonic() - started_at) * 1000),
                    )
                yield chunk
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
        if is_autonomous:
            logger.info(
                "[STREAM_BRIDGE] end thread=%s chunks=%d elapsed_ms=%d",
                thread_id,
                chunk_count,
                int((time.monotonic() - started_at) * 1000),
            )
