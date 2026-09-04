"""Per-call timeouts on SafeToolNode's CONCURRENT (default) dispatch path.

The ordered path (``test_tool_node_sequential.py``) already bounded each call
on its own. The concurrent path used one whole-batch timeout that, on
firing, replaced EVERY call's result with a timeout error (discarding
completed siblings) and cascade-aborted every callable in the batch by name.
These tests pin the new contract: a hung call fails alone, on both the async
and the sync path.

Same harness as the sequential tests: ``DEFAULT_RUNTIME`` injected through
``config[configurable][__pregel_runtime]`` so the node runs as the graph
drives it, without a compiled StateGraph.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph._internal._constants import CONFIG_KEY_RUNTIME
from langgraph.errors import GraphBubbleUp
from langgraph.runtime import DEFAULT_RUNTIME

from nymeria.vendor.react_agent.nodes import SafeToolNode


def _config(thread_id: str = "t-conc"):
    return {
        "configurable": {
            CONFIG_KEY_RUNTIME: DEFAULT_RUNTIME,
            "thread_id": thread_id,
            "user_id": "u-conc",
        }
    }


def _msg(tool_calls):
    return {"messages": [AIMessage(content="", tool_calls=tool_calls)]}


def _by_id(result):
    return {m.tool_call_id: m for m in result["messages"]}


# --- async path ---------------------------------------------------------------


def test_async_hung_call_times_out_alone_and_sibling_keeps_its_result():
    @tool
    async def hangs(x: str = "") -> str:
        """Hangs well past the per-call timeout."""
        await asyncio.sleep(5.0)
        return "should-not-finish"

    @tool
    async def quick(x: str = "") -> str:
        """Finishes at once (a Skill() activation next to a slow callable ask)."""
        await asyncio.sleep(0.01)
        return "quick-done"

    node = SafeToolNode([hangs, quick], tool_timeout=1)
    calls = [
        {"name": "hangs", "args": {}, "id": "a"},
        {"name": "quick", "args": {}, "id": "b"},
    ]
    started = time.monotonic()
    result = asyncio.run(node.ainvoke(_msg(calls), _config()))
    elapsed = time.monotonic() - started

    by_id = _by_id(result)
    assert "[Error]" in by_id["a"].content and "timed out" in by_id["a"].content
    assert by_id["a"].status == "error"
    assert by_id["b"].content == "quick-done"
    assert elapsed < 3.0, f"batch waited past the per-call timeout: {elapsed:.1f}s"


def test_async_only_the_hung_call_is_notified():
    notified: list[dict] = []

    @tool
    async def hangs(x: str = "") -> str:
        """Hangs past the per-call timeout."""
        await asyncio.sleep(5.0)
        return "should-not-finish"

    @tool
    async def quick(x: str = "") -> str:
        """Completes immediately."""
        return "quick-done"

    node = SafeToolNode(
        [hangs, quick],
        tool_timeout=1,
        on_timeout=lambda input_dict, config=None: notified.append(
            {"input": input_dict, "config": config}
        ),
    )
    calls = [
        {"name": "quick", "args": {}, "id": "b"},
        {"name": "hangs", "args": {}, "id": "a"},
    ]
    asyncio.run(node.ainvoke(_msg(calls), _config()))

    assert len(notified) == 1
    tcs = notified[0]["input"]["messages"][-1].tool_calls
    assert [tc["name"] for tc in tcs] == ["hangs"]
    assert notified[0]["config"] is not None


def test_async_healthy_batch_is_untouched_and_never_notified():
    notified: list[dict] = []

    @tool
    async def a(x: str = "") -> str:
        """Healthy."""
        return "a-done"

    @tool
    async def b(x: str = "") -> str:
        """Healthy."""
        await asyncio.sleep(0.05)
        return "b-done"

    node = SafeToolNode(
        [a, b], tool_timeout=2, on_timeout=lambda *args, **kwargs: notified.append({})
    )
    calls = [{"name": "a", "args": {}, "id": "1"}, {"name": "b", "args": {}, "id": "2"}]
    result = asyncio.run(node.ainvoke(_msg(calls), _config()))

    by_id = _by_id(result)
    assert by_id["1"].content == "a-done"
    assert by_id["2"].content == "b-done"
    assert notified == []


def test_async_graph_bubble_up_propagates_from_concurrent_path():
    @tool
    async def interrupts(x: str = "") -> str:
        """Raises a bubble-up exception."""
        raise GraphBubbleUp("pause")

    node = SafeToolNode([interrupts], tool_timeout=2)
    with pytest.raises(GraphBubbleUp):
        asyncio.run(
            node.ainvoke(_msg([{"name": "interrupts", "args": {}, "id": "i"}]), _config())
        )


# --- sync path ----------------------------------------------------------------


def test_sync_hung_call_times_out_alone_and_sibling_keeps_its_result():
    release = threading.Event()

    @tool
    def hangs(x: str = "") -> str:
        """Blocks until released (well past the per-call timeout)."""
        release.wait(5.0)
        return "should-not-finish"

    @tool
    def quick(x: str = "") -> str:
        """Finishes at once."""
        return "quick-done"

    node = SafeToolNode([hangs, quick], tool_timeout=1)
    calls = [
        {"name": "hangs", "args": {}, "id": "a"},
        {"name": "quick", "args": {}, "id": "b"},
    ]
    started = time.monotonic()
    try:
        result = node.invoke(_msg(calls), _config())
    finally:
        release.set()
    elapsed = time.monotonic() - started

    by_id = _by_id(result)
    assert "[Error]" in by_id["a"].content and "timed out" in by_id["a"].content
    assert by_id["b"].content == "quick-done"
    assert elapsed < 3.0, f"batch waited past the per-call timeout: {elapsed:.1f}s"


def test_sync_only_the_hung_call_is_notified():
    notified: list[dict] = []
    release = threading.Event()

    @tool
    def hangs(x: str = "") -> str:
        """Blocks until released."""
        release.wait(5.0)
        return "should-not-finish"

    @tool
    def quick(x: str = "") -> str:
        """Completes immediately."""
        return "quick-done"

    node = SafeToolNode(
        [hangs, quick],
        tool_timeout=1,
        on_timeout=lambda input_dict, config=None: notified.append(
            {"input": input_dict, "config": config}
        ),
    )
    calls = [
        {"name": "quick", "args": {}, "id": "b"},
        {"name": "hangs", "args": {}, "id": "a"},
    ]
    try:
        node.invoke(_msg(calls), _config())
    finally:
        release.set()

    assert len(notified) == 1
    tcs = notified[0]["input"]["messages"][-1].tool_calls
    assert [tc["name"] for tc in tcs] == ["hangs"]


def test_sync_graph_bubble_up_propagates_from_concurrent_path():
    @tool
    def interrupts(x: str = "") -> str:
        """Raises a bubble-up exception."""
        raise GraphBubbleUp("pause")

    node = SafeToolNode([interrupts], tool_timeout=2)
    with pytest.raises(GraphBubbleUp):
        node.invoke(_msg([{"name": "interrupts", "args": {}, "id": "i"}]), _config())


def test_sync_calls_run_concurrently_not_serially():
    # Two calls that each take ~0.4s must finish together, not back to back:
    # the per-call bound must not have serialized the default path.
    barrier = threading.Barrier(2, timeout=2.0)

    @tool
    def step_a(x: str = "") -> str:
        """Waits for its sibling at a barrier."""
        barrier.wait()
        return "a-done"

    @tool
    def step_b(x: str = "") -> str:
        """Waits for its sibling at a barrier."""
        barrier.wait()
        return "b-done"

    node = SafeToolNode([step_a, step_b], tool_timeout=3)
    calls = [{"name": "step_a", "args": {}, "id": "a"}, {"name": "step_b", "args": {}, "id": "b"}]
    result = node.invoke(_msg(calls), _config())

    by_id = _by_id(result)
    assert by_id["a"].content == "a-done"
    assert by_id["b"].content == "b-done"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_sync_pool_queued_call_is_timed_from_its_own_start():
    """With one worker the second call waits 1.3s in the pool queue, then runs
    1.3s: past a batch-wide 2s deadline, well within its own 2s."""

    @tool
    def slow_a(x: str) -> str:
        """Slow a."""
        time.sleep(1.3)
        return "a done"

    @tool
    def slow_b(x: str) -> str:
        """Slow b."""
        time.sleep(1.3)
        return "b done"

    notified: list = []
    node = SafeToolNode(
        [slow_a, slow_b],
        tool_timeout=2,
        on_timeout=lambda *args, **kwargs: notified.append(args),
    )
    config = _config()
    config["max_concurrency"] = 1
    result = node.invoke(
        _msg(
            [
                {"name": "slow_a", "args": {"x": "1"}, "id": "a", "type": "tool_call"},
                {"name": "slow_b", "args": {"x": "1"}, "id": "b", "type": "tool_call"},
            ]
        ),
        config=config,
    )
    by_id = _by_id(result)
    assert by_id["a"].content == "a done"
    assert by_id["b"].content == "b done"
    assert notified == []
