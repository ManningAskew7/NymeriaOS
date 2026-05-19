"""Unit tests for the in-memory pending-prompt queue backend."""

from __future__ import annotations

import asyncio
import threading


from nymeria.core.pending_prompt_queue import (
    FanoutMailbox,
    InMemoryPendingPromptQueue,
    PendingPrompt,
    PendingPromptQueueClosingError,
    PENDING_PROMPT_QUEUE_MAXSIZE,
    _SENTINEL_PROMPT_ABSORBED,
    create_pending_queue,
    get_pending_queue,
    make_pending_prompt,
    queued_prompt_header,
    reset_pending_queue_for_tests,
    set_pending_queue,
)


def _make_prompt(message: str, *, source: str = "user", autonomous: bool = False):
    return make_pending_prompt(
        message=message,
        source=source,
        source_id=None,
        source_label=f"{source}-label",
        user_id="user-1",
        is_autonomous=autonomous,
        fanout_mailbox=None,
        consumer_loop=None,
    )


def test_enqueue_and_drain_preserve_fifo_order():
    backend = InMemoryPendingPromptQueue()
    p1 = _make_prompt("first")
    p2 = _make_prompt("second")
    p3 = _make_prompt("third")
    assert backend.enqueue("t1", p1) == 1
    assert backend.enqueue("t1", p2) == 2
    assert backend.enqueue("t1", p3) == 3
    drained = backend.drain("t1")
    assert [p.message for p in drained] == ["first", "second", "third"]
    # Drain removes the queue; a second drain returns empty.
    assert backend.drain("t1") == []
    assert backend.size("t1") == 0
    assert backend.peek("t1") is False


def test_peek_and_size_per_thread():
    backend = InMemoryPendingPromptQueue()
    backend.enqueue("t1", _make_prompt("x"))
    backend.enqueue("t2", _make_prompt("y"))
    backend.enqueue("t2", _make_prompt("z"))
    assert backend.peek("t1") is True
    assert backend.size("t1") == 1
    assert backend.peek("t2") is True
    assert backend.size("t2") == 2
    assert backend.peek("nope") is False
    assert backend.size("nope") == 0


def test_halt_observation_consume_returns_zero_on_second_call():
    backend = InMemoryPendingPromptQueue()
    backend.mark_halt_observed("t1", 3)
    assert backend.consume_halt_observation("t1") == 3
    assert backend.consume_halt_observation("t1") == 0


def test_halt_observation_accumulates_multiple_marks():
    backend = InMemoryPendingPromptQueue()
    backend.mark_halt_observed("t1", 2)
    backend.mark_halt_observed("t1", 3)
    assert backend.consume_halt_observation("t1") == 5


def test_mark_halt_observed_ignores_non_positive_counts():
    backend = InMemoryPendingPromptQueue()
    backend.mark_halt_observed("t1", 0)
    backend.mark_halt_observed("t1", -5)
    assert backend.consume_halt_observation("t1") == 0


def test_clear_abandoned_sets_flag_and_wakes_notify_event():
    backend = InMemoryPendingPromptQueue()
    p1 = _make_prompt("x")
    p2 = _make_prompt("y")
    backend.enqueue("t1", p1)
    backend.enqueue("t1", p2)

    cleared = backend.clear("t1", abandoned=True)
    assert cleared == 2
    assert p1.abandoned is True
    assert p2.abandoned is True
    assert p1.notify_event.is_set()
    assert p2.notify_event.is_set()
    assert backend.size("t1") == 0


def test_clear_without_abandoned_does_not_set_flag():
    backend = InMemoryPendingPromptQueue()
    p1 = _make_prompt("x")
    backend.enqueue("t1", p1)
    cleared = backend.clear("t1", abandoned=False)
    assert cleared == 1
    assert p1.abandoned is False
    # notify_event still set so blocked waiters wake up.
    assert p1.notify_event.is_set()


def test_clear_also_drops_halt_observations():
    backend = InMemoryPendingPromptQueue()
    backend.mark_halt_observed("t1", 4)
    backend.enqueue("t1", _make_prompt("x"))
    backend.clear("t1", abandoned=True)
    assert backend.consume_halt_observation("t1") == 0


def test_clear_with_empty_queue_returns_zero():
    backend = InMemoryPendingPromptQueue()
    assert backend.clear("t1", abandoned=True) == 0


def test_release_window_rejects_new_enqueues_but_allows_drain():
    backend = InMemoryPendingPromptQueue()
    prompt = _make_prompt("already queued")
    backend.enqueue("t1", prompt)
    backend.begin_release("t1")
    assert backend.is_releasing("t1") is True
    assert backend.drain("t1") == [prompt]
    try:
        try:
            backend.enqueue("t1", _make_prompt("late"))
        except PendingPromptQueueClosingError:
            pass
        else:  # pragma: no cover - failure branch
            raise AssertionError("enqueue should reject while releasing")
    finally:
        backend.end_release("t1")
    assert backend.is_releasing("t1") is False
    assert backend.enqueue("t1", _make_prompt("next turn")) == 1


def test_enqueue_overflow_wakes_oldest_prompt():
    backend = InMemoryPendingPromptQueue()
    first = _make_prompt("first")
    backend.enqueue("t1", first)
    for i in range(PENDING_PROMPT_QUEUE_MAXSIZE):
        backend.enqueue("t1", _make_prompt(f"p{i}"))
    assert first.abandoned is True
    assert first.notify_event.is_set()
    drained = backend.drain("t1")
    assert len(drained) == PENDING_PROMPT_QUEUE_MAXSIZE
    assert drained[0].message == "p0"


def test_factory_returns_in_memory_implementation():
    backend = create_pending_queue(None)
    assert isinstance(backend, InMemoryPendingPromptQueue)


def test_singleton_get_set_reset():
    reset_pending_queue_for_tests()
    try:
        first = get_pending_queue()
        again = get_pending_queue()
        assert first is again
        replacement = create_pending_queue(None)
        set_pending_queue(replacement)
        assert get_pending_queue() is replacement
    finally:
        reset_pending_queue_for_tests()


def test_clear_pushes_error_into_attached_mailbox():
    backend = InMemoryPendingPromptQueue()

    async def _scenario():
        consumer_loop = asyncio.get_running_loop()
        mailbox = FanoutMailbox(consumer_loop)
        prompt = make_pending_prompt(
            message="hi",
            source="user",
            source_id=None,
            source_label="user-1",
            user_id="user-1",
            is_autonomous=False,
            fanout_mailbox=mailbox,
            consumer_loop=consumer_loop,
        )
        backend.enqueue("t1", prompt)

        # Clear from another thread so call_soon_threadsafe runs.
        await asyncio.to_thread(backend.clear, "t1", abandoned=True)

        first = await mailbox.get()
        assert first.get("type") == "error"
        assert first.get("code") == "aborted"

        sentinel = await mailbox.get()
        assert sentinel.get("type") == _SENTINEL_PROMPT_ABSORBED
        assert prompt.abandoned is True
        assert prompt.notify_event.is_set()

    asyncio.run(_scenario())


def test_queued_prompt_header_format():
    prompt = _make_prompt("hello")
    prompt.source = "trigger"
    prompt.source_label = "Daily Brief"
    header = queued_prompt_header(prompt)
    assert "[Queued prompt from trigger 'Daily Brief'" in header
    assert "received " in header
    assert "while you were mid-turn." in header


def test_pending_prompt_is_dataclass_with_defaults():
    prompt = _make_prompt("x")
    assert isinstance(prompt, PendingPrompt)
    assert prompt.abandoned is False
    assert isinstance(prompt.notify_event, threading.Event)
