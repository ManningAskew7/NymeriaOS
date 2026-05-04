"""Sync bridges for consuming NymeriaAgent async streams from worker code."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, Mapping, Optional

logger = logging.getLogger(__name__)


@dataclass
class StreamCollection:
    """Collected state from a consumed agent stream."""

    response_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    chunk_count: int = 0
    tool_call_count: int = 0
    iteration_limit_hit: bool = False
    iteration_limit_event: Optional[Dict[str, Any]] = None

    def response_text(self, *, fallback_to_thinking: bool = True) -> str:
        """Return response text, optionally falling back to thinking chunks."""
        if self.response_parts:
            return "".join(self.response_parts)
        if fallback_to_thinking and self.thinking_parts:
            return "".join(self.thinking_parts)
        return ""


StreamChunkCallback = Callable[[Dict[str, Any], StreamCollection], None]
StreamErrorMessageFactory = Callable[[Dict[str, Any]], str]
StreamIterationLimitPredicate = Callable[[Dict[str, Any]], bool]


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


def stream_and_collect(
    agent: Any,
    *,
    astream_kwargs: Mapping[str, Any],
    on_chunk: Optional[StreamChunkCallback] = None,
    error_message_factory: Optional[StreamErrorMessageFactory] = None,
    should_mark_iteration_limit: Optional[StreamIterationLimitPredicate] = None,
) -> StreamCollection:
    """Consume ``agent.astream(...)`` and collect common stream state.

    Synchronous autonomous workers still own their caller-specific behavior
    (event-bus publishing, console output, logging, completion payloads), but
    they should not each reimplement the same stream iteration, response
    collection, error propagation, and iteration-limit bookkeeping.
    """
    collection = StreamCollection()

    for chunk in iter_agent_astream(agent, **dict(astream_kwargs)):
        chunk_type = chunk.get("type")
        collection.chunk_count += 1

        if chunk_type == "tool_call":
            collection.tool_call_count += 1
        elif chunk_type == "thinking":
            content = chunk.get("content", "")
            if content:
                collection.thinking_parts.append(content)
        elif chunk_type == "response":
            content = chunk.get("content", "")
            if content:
                collection.response_parts.append(content)
        elif chunk_type == "iteration_limit":
            should_mark = (
                True
                if should_mark_iteration_limit is None
                else should_mark_iteration_limit(chunk)
            )
            if should_mark:
                collection.iteration_limit_hit = True
                collection.iteration_limit_event = dict(chunk)

        if on_chunk is not None:
            on_chunk(chunk, collection)

        if chunk_type == "error":
            if error_message_factory is not None:
                message = error_message_factory(chunk)
            else:
                error_content = chunk.get("content", "")
                error_code = chunk.get("code", "unknown")
                message = error_content or f"Agent stream error (code={error_code})"
            raise RuntimeError(message)

    return collection


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
                pass  # event loop may already be closed
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
