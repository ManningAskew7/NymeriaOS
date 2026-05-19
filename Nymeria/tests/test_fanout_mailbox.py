"""Cross-loop safety tests for FanoutMailbox.

The producer and consumer run on different event loops; this is the
core invariant that lets the sub-turn queue work in v1 (callable-thread
turns run on stream_bridge's loop while user-chat SSE runs on the
FastAPI loop). These tests pin down ordering, overflow behavior, and
the absence of any ``Future attached to a different loop`` errors.
"""

from __future__ import annotations

import asyncio
import threading


from nymeria.core.pending_prompt_queue import (
    FANOUT_MAILBOX_MAXSIZE,
    FanoutMailbox,
    _SENTINEL_PROMPT_ABSORBED,
)


def test_fifo_ordering_within_same_loop():
    async def _scenario():
        loop = asyncio.get_running_loop()
        mb = FanoutMailbox(loop)
        for i in range(5):
            mb.put({"type": "response", "content": str(i)})
        out = [await mb.get() for _ in range(5)]
        assert [e["content"] for e in out] == [str(i) for i in range(5)]

    asyncio.run(_scenario())


def test_cross_loop_producer_thread_does_not_corrupt_event():
    """Producer in a separate OS thread (no asyncio loop) wakes consumer."""

    async def _scenario():
        consumer_loop = asyncio.get_running_loop()
        mb = FanoutMailbox(consumer_loop)
        produced = []

        def producer():
            for i in range(20):
                event = {"type": "response", "content": str(i)}
                produced.append(event)
                mb.put(event)

        t = threading.Thread(target=producer)
        t.start()

        received = []
        while len(received) < 20:
            evt = await mb.get()
            # Drop fanout_dropped markers if the buffer overflows; size
            # is well under the bound here so we shouldn't see any.
            if evt.get("type") == "fanout_dropped":
                continue
            received.append(evt)
        t.join(timeout=2.0)
        assert received == produced

    asyncio.run(_scenario())


def test_cross_loop_producer_other_event_loop():
    """Producer running in a separate asyncio loop pokes consumer safely."""
    consumer_done = threading.Event()
    received: list[dict] = []

    async def consumer(mailbox: FanoutMailbox) -> None:
        for _ in range(10):
            evt = await mailbox.get()
            if evt.get("type") == "fanout_dropped":
                continue
            received.append(evt)
        consumer_done.set()

    def consumer_thread_main(out: dict) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        mailbox = FanoutMailbox(loop)
        out["mailbox"] = mailbox
        out["loop"] = loop
        loop.run_until_complete(consumer(mailbox))

    holder: dict = {}
    consumer_t = threading.Thread(target=consumer_thread_main, args=(holder,))
    consumer_t.start()

    # Spin until the consumer thread publishes the mailbox.
    for _ in range(200):
        if "mailbox" in holder:
            break
        threading.Event().wait(0.01)
    assert "mailbox" in holder, "consumer thread never published its mailbox"

    async def producer(mailbox: FanoutMailbox) -> None:
        for i in range(10):
            mailbox.put({"type": "response", "content": str(i)})
            await asyncio.sleep(0)  # let consumer drain a bit

    producer_loop = asyncio.new_event_loop()
    try:
        producer_loop.run_until_complete(producer(holder["mailbox"]))
    finally:
        producer_loop.close()

    assert consumer_done.wait(timeout=3.0), "consumer never finished"
    consumer_t.join(timeout=2.0)
    assert [e["content"] for e in received] == [str(i) for i in range(10)]


def test_overflow_drops_oldest_and_emits_marker():
    async def _scenario():
        loop = asyncio.get_running_loop()
        mb = FanoutMailbox(loop)
        # Fill past the bound. We want a deterministic overflow count.
        total = FANOUT_MAILBOX_MAXSIZE + 50
        for i in range(total):
            mb.put({"type": "response", "content": str(i)})

        # First read should surface the drop marker before any payload.
        first = await mb.get()
        assert first.get("type") == "fanout_dropped"
        assert first.get("dropped_count") == 50

        # The buffer should now contain the newest FANOUT_MAILBOX_MAXSIZE
        # items (since drop-oldest evicted the first 50).
        seen = []
        for _ in range(FANOUT_MAILBOX_MAXSIZE):
            seen.append(await mb.get())
        contents = [e["content"] for e in seen]
        expected = [str(i) for i in range(50, total)]
        assert contents == expected

    asyncio.run(_scenario())


def test_close_returns_sentinel_when_buffer_empty():
    async def _scenario():
        loop = asyncio.get_running_loop()
        mb = FanoutMailbox(loop)
        mb.close()
        evt = await mb.get()
        assert evt.get("type") == _SENTINEL_PROMPT_ABSORBED

    asyncio.run(_scenario())


def test_close_after_buffered_events_still_yields_them():
    async def _scenario():
        loop = asyncio.get_running_loop()
        mb = FanoutMailbox(loop)
        mb.put({"type": "response", "content": "a"})
        mb.put({"type": "response", "content": "b"})
        mb.close()
        assert (await mb.get())["content"] == "a"
        assert (await mb.get())["content"] == "b"
        assert (await mb.get())["type"] == _SENTINEL_PROMPT_ABSORBED

    asyncio.run(_scenario())


def test_put_after_close_is_noop():
    async def _scenario():
        loop = asyncio.get_running_loop()
        mb = FanoutMailbox(loop)
        mb.close()
        mb.put({"type": "response", "content": "ghost"})
        evt = await mb.get()
        assert evt.get("type") == _SENTINEL_PROMPT_ABSORBED

    asyncio.run(_scenario())
