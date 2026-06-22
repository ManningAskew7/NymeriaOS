"""Regression tests for EventBus per-subscriber counter pruning.

The enqueue/drop diagnostic counters are keyed ``f"{sub_id}:{event_type}"``.
Before the fix, ``unsubscribe`` only removed the subscriber's queue, so those
counter dicts grew without bound over the lifetime of a long-running API
process as short-lived SSE subscribers connected and disconnected.
"""

from __future__ import annotations

from nymeria.core.event_bus import AutonomousEvent, EventBus


def _event() -> AutonomousEvent:
    return AutonomousEvent(event_type="response", thread_id="t1", user_id="u1")


def test_unsubscribe_prunes_enqueue_counters_for_that_subscriber() -> None:
    bus = EventBus()
    bus.subscribe("sub_a")
    bus.subscribe("sub_b")
    for _ in range(3):
        bus.publish(_event())

    assert any(k.startswith("sub_a:") for k in bus._enqueue_counts)
    assert any(k.startswith("sub_b:") for k in bus._enqueue_counts)

    bus.unsubscribe("sub_a")

    # sub_a's counters are gone; sub_b is untouched.
    assert not any(k.startswith("sub_a:") for k in bus._enqueue_counts)
    assert any(k.startswith("sub_b:") for k in bus._enqueue_counts)


def test_unsubscribe_prunes_drop_counters_for_that_subscriber() -> None:
    bus = EventBus()
    bus.subscribe("sub_a")
    # Queue maxsize is 100; overflow forces Full -> per-subscriber drop counts.
    for _ in range(150):
        bus.publish(_event())

    assert any(k.startswith("sub_a:") for k in bus._drop_counts)

    bus.unsubscribe("sub_a")

    assert not any(k.startswith("sub_a:") for k in bus._drop_counts)
    assert not any(k.startswith("sub_a:") for k in bus._enqueue_counts)


def test_unsubscribe_leaves_event_type_publish_counters_intact() -> None:
    bus = EventBus()
    bus.subscribe("sub_a")
    bus.publish(_event())
    # publish counts are keyed by event type only (bounded), not per-subscriber.
    assert bus._publish_counts.get("response") == 1
    bus.unsubscribe("sub_a")
    assert bus._publish_counts.get("response") == 1


def test_unsubscribe_unknown_subscriber_is_safe() -> None:
    bus = EventBus()
    # No subscriber, no counters: pruning must not raise.
    bus.unsubscribe("never_subscribed")
    assert bus.get_subscriber_count() == 0
