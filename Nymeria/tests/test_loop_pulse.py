"""The LoopPulse doorbell: primitive contract and its event-bus wiring.

`core/loop_pulse.py` is the extracted wakeup doorbell behind the autonomous
SSE push-latency fix: `EventBus.publish` / `RedisEventBus._dispatch_local`
ring each subscriber's pulse on every successful enqueue, and the SSE
generator awaits a captured pulse instead of sleeping out its 1s poll
interval (measured: mean 871ms idle-bus delivery latency down to 0.8ms).

The contract has three legs, each with a specific race it closes:

* capture-before-check: a listener captures the event BEFORE checking for
  data, so a ring landing between the check and the await sets the
  already-captured event and can never be lost.
* replace-not-clear: `ring()` swaps in a fresh event rather than clearing
  the old one, so every captured listener wakes exactly once per ring and
  a post-ring capture blocks again (no busy loop).
* cross-thread marshal: publishers are worker threads (ticker, Redis
  subscriber loop, sync tool paths); a ring from off the bound loop must be
  marshalled with `call_soon_threadsafe` or the parked selector never wakes
  until its next timer.

Timing bounds here are deliberately loose (seconds of slack against
sub-millisecond expected latency) so they stay CI-safe under xdist load
while still going red when a mutation reintroduces poll-bound wakeups.
"""

from __future__ import annotations

import asyncio
import threading
import time

from nymeria.core.event_bus import AutonomousEvent, EventBus
from nymeria.core.loop_pulse import LoopPulse


def _event() -> AutonomousEvent:
    return AutonomousEvent(event_type="response", thread_id="t1", user_id="u1")


# -- primitive contract -------------------------------------------------------


def test_capture_before_check_closes_the_lost_wakeup_race():
    # The consumer discipline: capture, check (empty), await the capture.
    # A ring landing after the capture but before the await must still wake
    # the awaiter, because it sets the event the listener already holds.
    async def drive() -> None:
        pulse = LoopPulse()
        pulse.bind_running_loop()
        captured = pulse.listen()
        pulse.ring()  # lands "between the check and the await"
        await asyncio.wait_for(captured.wait(), timeout=1.0)

    asyncio.run(drive())


def test_ring_replaces_the_event_so_a_fresh_capture_blocks_again():
    # Two captures before a ring are the SAME event (both wake on one ring);
    # a capture AFTER the ring is a fresh unset event. The set-not-replace
    # mutation leaves the post-ring capture permanently set, which turns the
    # consumer's await into a busy loop.
    pulse = LoopPulse()
    first = pulse.listen()
    second = pulse.listen()
    assert first is second

    pulse.ring()
    assert first.is_set()

    fresh = pulse.listen()
    assert fresh is not first
    assert not fresh.is_set()


def test_off_thread_ring_is_marshalled_onto_the_bound_loop():
    # With the loop parked in select() awaiting the captured event, a ring
    # from a worker thread must wake it promptly. A bare `event.set()` from
    # the thread only appends to the loop's ready list without waking the
    # selector, so the awaiter would sleep until its 5s timeout instead.
    async def drive() -> tuple[bool, float]:
        pulse = LoopPulse()
        pulse.bind_running_loop()
        captured = pulse.listen()

        parked = threading.Event()
        # call_soon runs after this task suspends, in the same loop
        # iteration, so by the time the thread wakes and finishes its 50ms
        # buffer the loop is parked in select().
        asyncio.get_running_loop().call_soon(parked.set)

        def ring_later() -> None:
            parked.wait(timeout=2.0)
            time.sleep(0.05)
            pulse.ring()

        thread = threading.Thread(target=ring_later)
        thread.start()
        start = time.monotonic()
        woke = True
        try:
            await asyncio.wait_for(captured.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            woke = False
        elapsed = time.monotonic() - start
        thread.join()
        return woke, elapsed

    woke, elapsed = asyncio.run(drive())
    assert woke, "off-thread ring never woke the bound-loop listener"
    assert elapsed < 2.0, f"wake took {elapsed:.3f}s: ring was not marshalled"


def test_unbound_pulse_still_rings_without_error():
    # A pulse that never bound a loop (sync-context subscriber) must not
    # raise on ring, and a same-thread captured event still gets set.
    pulse = LoopPulse()
    captured = pulse.listen()
    pulse.ring()
    assert captured.is_set()


def test_bind_running_loop_reports_whether_it_bound():
    pulse = LoopPulse()
    assert pulse.bind_running_loop() is False  # no loop here

    async def drive() -> bool:
        return pulse.bind_running_loop()

    assert asyncio.run(drive()) is True


# -- event-bus wiring ---------------------------------------------------------


def test_event_bus_doorbell_tracks_subscription_lifecycle():
    bus = EventBus()
    assert bus.doorbell("sub") is None
    bus.subscribe("sub")
    assert bus.doorbell("sub") is not None
    bus.unsubscribe("sub")
    assert bus.doorbell("sub") is None


def test_event_bus_publish_from_a_thread_rings_the_subscriber_doorbell():
    # The production shape: the SSE route subscribes on its loop, a worker
    # thread (ticker, tool path) publishes. The enqueue must ring the
    # doorbell so a captured listener wakes long before any poll timer.
    async def drive() -> tuple[bool, float, AutonomousEvent]:
        bus = EventBus()
        queue = bus.subscribe("sub")  # binds the doorbell to this loop
        doorbell = bus.doorbell("sub")
        assert doorbell is not None
        captured = doorbell.listen()

        parked = threading.Event()
        asyncio.get_running_loop().call_soon(parked.set)

        def publish_later() -> None:
            parked.wait(timeout=2.0)
            time.sleep(0.05)
            bus.publish(_event())

        thread = threading.Thread(target=publish_later)
        thread.start()
        start = time.monotonic()
        woke = True
        try:
            await asyncio.wait_for(captured.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            woke = False
        elapsed = time.monotonic() - start
        thread.join()
        return woke, elapsed, queue.get_nowait()

    woke, elapsed, delivered = asyncio.run(drive())
    assert woke, "publish never rang the subscriber's doorbell"
    assert elapsed < 2.0, f"wake took {elapsed:.3f}s: publish did not ring"
    assert delivered.event_type == "response"


def test_redis_dispatch_local_rings_doorbells_from_the_subscriber_thread(
    monkeypatch,
):
    # Docker production delivers cross-process events via the Redis
    # subscriber THREAD calling _dispatch_local; its enqueue must ring the
    # doorbell exactly like the in-memory publish path or the 1s poll
    # latency silently returns in the Docker shape only.
    import sys
    from unittest.mock import MagicMock

    fake_redis = MagicMock()
    fake_client = MagicMock()
    fake_client.ping.return_value = True
    fake_redis.from_url.return_value = fake_client
    monkeypatch.setitem(sys.modules, "redis", fake_redis)

    from nymeria.core.event_bus_redis import RedisEventBus

    async def drive() -> tuple[bool, float, AutonomousEvent]:
        bus = RedisEventBus("redis://localhost:6379", enable_subscriber=False)
        try:
            queue = bus.subscribe("sub")
            doorbell = bus.doorbell("sub")
            assert doorbell is not None
            captured = doorbell.listen()

            parked = threading.Event()
            asyncio.get_running_loop().call_soon(parked.set)

            def dispatch_later() -> None:
                parked.wait(timeout=2.0)
                time.sleep(0.05)
                bus._dispatch_local(_event())

            thread = threading.Thread(target=dispatch_later)
            thread.start()
            start = time.monotonic()
            woke = True
            try:
                await asyncio.wait_for(captured.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                woke = False
            elapsed = time.monotonic() - start
            thread.join()
            return woke, elapsed, queue.get_nowait()
        finally:
            bus.close()

    woke, elapsed, delivered = asyncio.run(drive())
    assert woke, "_dispatch_local never rang the subscriber's doorbell"
    assert elapsed < 2.0, f"wake took {elapsed:.3f}s: dispatch did not ring"
    assert delivered.event_type == "response"
