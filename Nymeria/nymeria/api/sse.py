"""SSE streaming helpers shared by the event-stream routes.

`with_sse_keepalive` inserts SSE comment frames (`: keepalive`) into an event
stream whenever the source stays silent longer than the interval. Reverse
proxies and tunnel edges close connections they consider idle (Cloudflare's
proxy at roughly 100 seconds), and a chat turn can legitimately emit nothing
for minutes while a long tool call runs, so without these frames a remotely
tunneled chat stream dies mid-turn. Comment lines are part of the SSE spec
and every Nymeria consumer (desktop/mobile chat parsers, the bot relays, the
CLI transport, the MCP backend client) already skips non-`data:` lines.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterable, AsyncIterator

# Well under the ~100s Cloudflare idle timeout, and aligned with common
# proxy read-timeout floors; off the :00/:30-style round numbers on purpose.
SSE_KEEPALIVE_INTERVAL_SECONDS = 25.0

# An SSE comment frame: ignored by EventSource and by every line parser that
# only handles `data:`-prefixed lines.
SSE_KEEPALIVE_FRAME = ": keepalive\n\n"

# Response headers every chat/event StreamingResponse sets: disable client and
# proxy caching, keep the connection open, and turn off nginx response buffering
# (`X-Accel-Buffering`) so frames flush to the client immediately. Starlette
# copies these into the response without mutating the dict, so the shared
# module-level constant can be passed directly to each StreamingResponse. The
# health-stream in `system.py` deliberately omits `Connection: keep-alive` and
# keeps its own dict.
SSE_RESPONSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


async def with_sse_keepalive(
    source: AsyncIterable[str],
    *,
    interval: float = SSE_KEEPALIVE_INTERVAL_SECONDS,
) -> AsyncIterator[str]:
    """Yield everything from `source`, plus a comment frame per silent gap.

    The source's items are never delayed: a pending read is raced against the
    interval, and only the timeout branch emits a keepalive. Cancellation and
    generator close are forwarded to the source (its `finally` cleanup, e.g.
    event-bus unsubscribes and abort handling, still runs).
    """
    iterator = source.__aiter__()
    pending: asyncio.Task | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(iterator))
            try:
                item = await asyncio.wait_for(asyncio.shield(pending), timeout=interval)
            except asyncio.TimeoutError:
                yield SSE_KEEPALIVE_FRAME
                continue
            except StopAsyncIteration:
                pending = None
                return
            pending = None
            yield item
    finally:
        if pending is not None:
            pending.cancel()
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except RuntimeError:
                # "asynchronous generator is already running": the generator
                # frame is still owned by the just-cancelled anext task (the
                # cancel above is only a request; the task unwinds on a later
                # loop tick). That unwind throws CancelledError into the
                # generator, so its try/finally cleanup still runs; closing
                # here would be redundant. Seen when a response task is
                # cancelled mid-read (client disconnect at stream start).
                pass


__all__ = [
    "SSE_KEEPALIVE_FRAME",
    "SSE_KEEPALIVE_INTERVAL_SECONDS",
    "SSE_RESPONSE_HEADERS",
    "with_sse_keepalive",
]
