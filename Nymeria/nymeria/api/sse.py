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
            await aclose()


__all__ = [
    "SSE_KEEPALIVE_FRAME",
    "SSE_KEEPALIVE_INTERVAL_SECONDS",
    "with_sse_keepalive",
]
