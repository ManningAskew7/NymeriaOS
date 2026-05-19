"""Tests for the sub-turn halt path -- route_after_tools observing
a non-empty pending-prompt queue and the backend's halt-observation
accounting that drives the post-drive drain loop in NymeriaAgent.astream.
"""

from __future__ import annotations


import pytest

from langchain_core.messages import AIMessage

from nymeria.core.pending_prompt_queue import (
    create_pending_queue,
    make_pending_prompt,
    reset_pending_queue_for_tests,
    set_pending_queue,
)
from nymeria.vendor.react_agent.nodes import route_after_tools


@pytest.fixture
def isolated_queue():
    """Install a fresh backend for the test and tear it down after."""
    backend = create_pending_queue(None)
    set_pending_queue(backend)
    try:
        yield backend
    finally:
        reset_pending_queue_for_tests()


def _state_without_reload() -> dict:
    """A state whose last AIMessage has no queued-reload sentinel."""
    return {
        "messages": [
            AIMessage(content="hello"),
        ]
    }


def _make_prompt(message: str, *, autonomous: bool = False):
    return make_pending_prompt(
        message=message,
        source="user",
        source_id=None,
        source_label="user-1",
        user_id="user-1",
        is_autonomous=autonomous,
        fanout_mailbox=None,
        consumer_loop=None,
    )


def test_route_after_tools_continues_when_queue_empty(isolated_queue):
    config = {"configurable": {"thread_id": "t1"}}
    assert route_after_tools(_state_without_reload(), config) == "agent"
    assert isolated_queue.consume_halt_observation("t1") == 0


def test_route_after_tools_halts_when_queue_nonempty(isolated_queue):
    isolated_queue.enqueue("t1", _make_prompt("queued msg"))
    config = {"configurable": {"thread_id": "t1"}}
    assert route_after_tools(_state_without_reload(), config) == "end"
    # The router marked the halt-observation atomically for the drain
    # loop to consume.
    assert isolated_queue.consume_halt_observation("t1") == 1


def test_route_after_tools_without_config_falls_back_to_agent(isolated_queue):
    # Even with a non-empty queue, no config means no thread_id is
    # available, so we can't check -- default behavior is "agent".
    isolated_queue.enqueue("t1", _make_prompt("queued msg"))
    assert route_after_tools(_state_without_reload(), None) == "agent"
    # Halt observation should NOT have fired since we never looked.
    assert isolated_queue.consume_halt_observation("t1") == 0


def test_route_after_tools_without_thread_id_in_config(isolated_queue):
    isolated_queue.enqueue("t1", _make_prompt("queued msg"))
    config = {"configurable": {}}
    assert route_after_tools(_state_without_reload(), config) == "agent"
    assert isolated_queue.consume_halt_observation("t1") == 0


def test_route_after_tools_records_full_count_per_observation(isolated_queue):
    isolated_queue.enqueue("t1", _make_prompt("a"))
    isolated_queue.enqueue("t1", _make_prompt("b"))
    isolated_queue.enqueue("t1", _make_prompt("c"))
    config = {"configurable": {"thread_id": "t1"}}
    assert route_after_tools(_state_without_reload(), config) == "end"
    # The router records the FULL queue size at observation time so
    # the drain loop can yield turn_halted with the right count.
    assert isolated_queue.consume_halt_observation("t1") == 3


def test_route_after_tools_signature_accepts_config_keyword():
    """LangGraph's RunnableCallable only auto-injects config when the
    routing function's signature accepts it. Guard the contract."""
    import inspect
    sig = inspect.signature(route_after_tools)
    params = sig.parameters
    assert "config" in params, "route_after_tools must accept a config kwarg"
    # And it must have a default, so legacy ``route_after_tools(state)``
    # call sites stay valid.
    assert params["config"].default is None
