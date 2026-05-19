"""Verify that NymeriaAgent.abort_with_cascade clears the
pending-prompt queue for the target thread and wakes any blocked
queuers with an aborted error.

This is the abort safety property: when a user calls
``POST /threads/{id}/stop``, queued prompts (which would otherwise
hang their queuers until lock_timeout) must be released immediately.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from nymeria.core.agent_callable_lifecycle import abort_with_cascade
from nymeria.core.pending_prompt_queue import (
    FanoutMailbox,
    _SENTINEL_PROMPT_ABSORBED,
    create_pending_queue,
    make_pending_prompt,
    reset_pending_queue_for_tests,
    set_pending_queue,
)
from nymeria.core.thread_lock_manager import ThreadLockManager


class _StubAgent:
    """Minimum surface required by abort_with_cascade."""

    def __init__(self) -> None:
        self._thread_locks = ThreadLockManager()
        self._active_callable_invocations: dict[str, set] = {}
        self._invocations_lock = threading.Lock()

    def abort_with_cascade(self, thread_id: str) -> None:
        # Re-enter the module function so cascading children works
        # the same as on the real agent.
        abort_with_cascade(self, thread_id)


@pytest.fixture
def isolated_queue():
    backend = create_pending_queue(None)
    set_pending_queue(backend)
    try:
        yield backend
    finally:
        reset_pending_queue_for_tests()


def test_abort_signals_lock_event_and_clears_queue(isolated_queue):
    agent = _StubAgent()
    p1 = make_pending_prompt(
        message="msg-1",
        source="user",
        source_id=None,
        source_label="u1",
        user_id="u1",
        is_autonomous=False,
        fanout_mailbox=None,
        consumer_loop=None,
    )
    p2 = make_pending_prompt(
        message="msg-2",
        source="trigger",
        source_id="trig-1",
        source_label="T",
        user_id="u1",
        is_autonomous=True,
        fanout_mailbox=None,
        consumer_loop=None,
    )
    isolated_queue.enqueue("t1", p1)
    isolated_queue.enqueue("t1", p2)

    abort_with_cascade(agent, "t1")

    # Lock-level abort event is signalled.
    assert agent._thread_locks.get_abort_event("t1").is_set()
    # Queue was drained.
    assert isolated_queue.size("t1") == 0
    # Each prompt is marked abandoned and its notify_event was set so
    # any blocked queuer wakes up.
    assert p1.abandoned is True
    assert p2.abandoned is True
    assert p1.notify_event.is_set()
    assert p2.notify_event.is_set()


def test_abort_pushes_error_into_attached_mailbox(isolated_queue):
    agent = _StubAgent()

    async def _scenario():
        consumer_loop = asyncio.get_running_loop()
        mailbox = FanoutMailbox(consumer_loop)
        prompt = make_pending_prompt(
            message="msg",
            source="user",
            source_id=None,
            source_label="u1",
            user_id="u1",
            is_autonomous=False,
            fanout_mailbox=mailbox,
            consumer_loop=consumer_loop,
        )
        isolated_queue.enqueue("t1", prompt)

        # Run abort from another thread so the mailbox's
        # call_soon_threadsafe path is exercised.
        await asyncio.to_thread(abort_with_cascade, agent, "t1")

        first = await mailbox.get()
        assert first.get("type") == "error"
        assert first.get("code") == "aborted"
        sentinel = await mailbox.get()
        assert sentinel.get("type") == _SENTINEL_PROMPT_ABSORBED

    asyncio.run(_scenario())


def test_abort_cascades_to_callable_children(isolated_queue):
    parent_agent = _StubAgent()
    parent_agent._active_callable_invocations["parent"] = {"child-a", "child-b"}

    # Pre-stage queued prompts on both children.
    for child in ("child-a", "child-b"):
        isolated_queue.enqueue(
            child,
            make_pending_prompt(
                message=f"task for {child}",
                source="callable",
                source_id="parent",
                source_label="parent-thread",
                user_id="u1",
                is_autonomous=False,
                fanout_mailbox=None,
                consumer_loop=None,
            ),
        )

    abort_with_cascade(parent_agent, "parent")

    # Parent and each child get the abort signal.
    assert parent_agent._thread_locks.get_abort_event("parent").is_set()
    assert parent_agent._thread_locks.get_abort_event("child-a").is_set()
    assert parent_agent._thread_locks.get_abort_event("child-b").is_set()
    # Child queues are cleared too.
    assert isolated_queue.size("child-a") == 0
    assert isolated_queue.size("child-b") == 0
