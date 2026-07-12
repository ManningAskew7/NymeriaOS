"""SSE keepalive wrapper: silent gaps emit comment frames, items pass through.

Covers nymeria/api/sse.py and its wiring into the chat stream, which exists so
tunnel edges with idle timeouts (Cloudflare's proxy closes at ~100s) cannot
cut a chat turn that stays silent during a long tool call.
"""

import asyncio

from nymeria.api.sse import (
    SSE_KEEPALIVE_FRAME,
    SSE_KEEPALIVE_INTERVAL_SECONDS,
    with_sse_keepalive,
)


def test_keepalive_frame_is_an_sse_comment():
    # Comment lines are the one frame every consumer skips: desktop/mobile
    # chat parsers, the bot relays, and the MCP client all act only on
    # `data:`-prefixed lines.
    assert SSE_KEEPALIVE_FRAME.startswith(":")
    assert SSE_KEEPALIVE_FRAME.endswith("\n\n")


def test_default_interval_clears_cloudflare_idle_timeout():
    assert SSE_KEEPALIVE_INTERVAL_SECONDS < 100.0


def test_silent_gaps_emit_keepalives_without_delaying_items():
    async def slow_source():
        yield "data: first\n\n"
        await asyncio.sleep(0.25)
        yield "data: second\n\n"

    async def drive():
        out = []
        async for item in with_sse_keepalive(slow_source(), interval=0.05):
            out.append(item)
        return out

    out = asyncio.run(drive())
    assert out[0] == "data: first\n\n"
    assert out[-1] == "data: second\n\n"
    keepalives = [item for item in out if item == SSE_KEEPALIVE_FRAME]
    assert len(keepalives) >= 2  # the 0.25s gap spans several 0.05s intervals
    # Nothing else was injected or reordered.
    assert [i for i in out if i != SSE_KEEPALIVE_FRAME] == [
        "data: first\n\n",
        "data: second\n\n",
    ]


def test_fast_source_passes_through_untouched():
    async def fast_source():
        for n in range(5):
            yield f"data: {n}\n\n"

    async def drive():
        return [item async for item in with_sse_keepalive(fast_source(), interval=1.0)]

    assert asyncio.run(drive()) == [f"data: {n}\n\n" for n in range(5)]


def test_source_exception_propagates():
    async def broken_source():
        yield "data: ok\n\n"
        raise RuntimeError("stream broke")

    async def drive():
        out = []
        async for item in with_sse_keepalive(broken_source(), interval=1.0):
            out.append(item)
        return out

    try:
        asyncio.run(drive())
    except RuntimeError as exc:
        assert "stream broke" in str(exc)
    else:
        raise AssertionError("source exception was swallowed")


def test_consumer_close_runs_source_cleanup():
    # A client disconnect closes the wrapper mid-stream; the source's finally
    # (event-bus unsubscribes, abort handling in the real generators) must run.
    cleaned = []

    async def source_with_cleanup():
        try:
            yield "data: first\n\n"
            await asyncio.sleep(10)
            yield "data: never\n\n"
        finally:
            cleaned.append(True)

    async def drive():
        stream = with_sse_keepalive(source_with_cleanup(), interval=0.05)
        async for item in stream:
            if item == "data: first\n\n":
                break  # consumer goes away
        await stream.aclose()

    asyncio.run(drive())
    assert cleaned == [True]


def test_chat_stream_is_wrapped():
    # Wiring check: the long-lived chat generators go through the wrapper
    # (a silent regression here would resurface the tunnel-idle cutoffs).
    import inspect

    from nymeria.api.routers import chat as chat_module

    source = inspect.getsource(chat_module)
    assert source.count("with_sse_keepalive(") >= 2


def test_cancel_while_anext_task_owns_generator_still_cleans_up():
    # Cancelling the wrapper right after it scheduled an anext read (client
    # disconnect at stream start) used to crash its finally with
    # "aclose(): asynchronous generator is already running", because the
    # cancel of the pending anext task is only a request and the inner
    # generator frame is still owned by that task. The wrapper now tolerates
    # the busy frame; the inner generator's finally still runs when the
    # cancelled anext task unwinds.
    import pytest

    async def _scenario():
        started = asyncio.Event()
        cleaned = asyncio.Event()

        async def _inner():
            try:
                started.set()
                await asyncio.sleep(30)
                yield "never"
            finally:
                cleaned.set()

        gen = with_sse_keepalive(_inner())

        async def _consume():
            async for _item in gen:
                pass

        task = asyncio.create_task(_consume())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The inner generator's own finally runs via the anext task's unwind.
        await asyncio.wait_for(cleaned.wait(), 2)

    asyncio.run(_scenario())
