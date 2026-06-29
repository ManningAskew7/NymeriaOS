"""Tests for ordered (sequential) same-turn tool execution in SafeToolNode.

Covers the Tier 1 control-tool path (``run_tools_in_order``): the predicate,
the ordered loop, per-call timeout (outer batch timeout bypassed), per-step
abort, ``stop_on_error``, the synthesized marker ack/nudge, the
``analyze_turn_safety`` exclusion, signature parity, and a coupling guard
against the pinned langgraph-prebuilt internals.

SafeToolNode.invoke/ainvoke cannot be called bare (RunnableCallable treats
``runtime`` as a required injected kwarg). We inject ``DEFAULT_RUNTIME`` through
``config[configurable][__pregel_runtime]`` so the node runs exactly as the graph
would drive it, without standing up a full compiled StateGraph.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import threading

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.runtime import DEFAULT_RUNTIME
from langgraph._internal._constants import CONFIG_KEY_RUNTIME

from nymeria.vendor.react_agent.nodes import (
    SEQUENTIAL_ORDER_TOOL_NAME,
    SafeToolNode,
    analyze_turn_safety,
)
from nymeria.tools.tool_order import run_tools_in_order


def _config(thread_id: str = "t-seq", *, sequential: bool = False):
    configurable = {
        CONFIG_KEY_RUNTIME: DEFAULT_RUNTIME,
        "thread_id": thread_id,
        "user_id": "u-seq",
    }
    if sequential:
        # Tier 2 (#67): the deterministic per-thread/global flag, resolved into
        # config by agent_safety.graph_run_config before the graph runs.
        configurable["sequential_tools"] = True
    return {"configurable": configurable}


def _msg(tool_calls):
    return {"messages": [AIMessage(content="", tool_calls=tool_calls)]}


def _marker_call(call_id="m", **args):
    return {"name": SEQUENTIAL_ORDER_TOOL_NAME, "args": args, "id": call_id}


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------

def test_predicate_true_only_with_marker():
    node = SafeToolNode([run_tools_in_order])
    with_marker = _msg([_marker_call(), {"name": "x", "args": {}, "id": "a"}])
    without = _msg([{"name": "x", "args": {}, "id": "a"}])
    assert node._should_run_sequentially(with_marker) is True
    assert node._should_run_sequentially(without) is False


def test_predicate_shape_robustness_no_crash():
    node = SafeToolNode([run_tools_in_order])
    # Send-API single-call payloads and unknown shapes -> concurrent, never crash.
    assert node._should_run_sequentially({"__type": "tool_call_with_context"}) is False
    assert node._should_run_sequentially(
        [{"type": "tool_call", "name": SEQUENTIAL_ORDER_TOOL_NAME, "args": {}, "id": "z"}]
    ) is False
    assert node._should_run_sequentially({}) is False
    assert node._should_run_sequentially({"messages": []}) is False
    # A plain list of messages ending in the marker AIMessage is a valid shape.
    seq_list = [HumanMessage(content="hi"), AIMessage(content="", tool_calls=[_marker_call()])]
    assert node._should_run_sequentially(seq_list) is True


# ---------------------------------------------------------------------------
# Tier 2: deterministic per-thread/global "sequential tool execution" setting,
# resolved into config["configurable"]["sequential_tools"] by graph_run_config.
# ---------------------------------------------------------------------------

def test_predicate_true_from_config_flag_without_marker():
    node = SafeToolNode([run_tools_in_order])
    batch = _msg([{"name": "x", "args": {}, "id": "a"}, {"name": "y", "args": {}, "id": "b"}])
    # No marker: flag in config flips it sequential; absent/False stays concurrent.
    assert node._should_run_sequentially(batch, _config(sequential=True)) is True
    assert node._should_run_sequentially(batch, _config(sequential=False)) is False
    assert node._should_run_sequentially(batch, None) is False


def test_tier2_setting_orders_side_effects_without_marker():
    order: list[str] = []
    slow, fast = _ordering_tools(order)
    node = SafeToolNode([slow, fast, run_tools_in_order])
    calls = [
        {"name": "slow_first", "args": {}, "id": "a"},
        {"name": "fast_second", "args": {}, "id": "b"},
    ]
    # No marker in the batch; ordering comes purely from the Tier 2 config flag.
    result = asyncio.run(node.ainvoke(_msg(calls), _config(sequential=True)))
    assert order == ["slow", "fast"]  # emitted order despite the slow tool first
    names = [m.name for m in result["messages"]]
    assert names == ["slow_first", "fast_second"]


def test_tier2_setting_off_stays_concurrent():
    order: list[str] = []
    slow, fast = _ordering_tools(order)
    node = SafeToolNode([slow, fast, run_tools_in_order])
    calls = [
        {"name": "slow_first", "args": {}, "id": "a"},
        {"name": "fast_second", "args": {}, "id": "b"},
    ]
    result = asyncio.run(node.ainvoke(_msg(calls), _config(sequential=False)))
    assert order == ["fast", "slow"]  # concurrent: fast finishes first
    assert len(result["messages"]) == 2


def test_tier2_setting_and_marker_both_sequential():
    order: list[str] = []
    slow, fast = _ordering_tools(order)
    node = SafeToolNode([slow, fast, run_tools_in_order])
    calls = [
        _marker_call(),
        {"name": "slow_first", "args": {}, "id": "a"},
        {"name": "fast_second", "args": {}, "id": "b"},
    ]
    result = asyncio.run(node.ainvoke(_msg(calls), _config(sequential=True)))
    assert order == ["slow", "fast"]
    # The marker is still ack'd even though the setting (not the marker) is what
    # would have forced sequencing on its own.
    names = [m.name for m in result["messages"]]
    assert names == [SEQUENTIAL_ORDER_TOOL_NAME, "slow_first", "fast_second"]


# ---------------------------------------------------------------------------
# Ordering (async + sync)
# ---------------------------------------------------------------------------

def _ordering_tools(order):
    @tool
    async def slow_first(x: str = "") -> str:
        """Slow tool."""
        await asyncio.sleep(0.20)
        order.append("slow")
        return "slow-done"

    @tool
    async def fast_second(x: str = "") -> str:
        """Fast tool."""
        await asyncio.sleep(0.01)
        order.append("fast")
        return "fast-done"

    return slow_first, fast_second


def test_default_concurrent_fast_finishes_first():
    order: list[str] = []
    slow, fast = _ordering_tools(order)
    node = SafeToolNode([slow, fast, run_tools_in_order])
    calls = [
        {"name": "slow_first", "args": {}, "id": "a"},
        {"name": "fast_second", "args": {}, "id": "b"},
    ]
    result = asyncio.run(node.ainvoke(_msg(calls), _config()))
    assert order == ["fast", "slow"]
    assert len(result["messages"]) == 2


def test_sequential_orders_side_effects_and_messages():
    order: list[str] = []
    slow, fast = _ordering_tools(order)
    node = SafeToolNode([slow, fast, run_tools_in_order])
    calls = [
        _marker_call(),
        {"name": "slow_first", "args": {}, "id": "a"},
        {"name": "fast_second", "args": {}, "id": "b"},
    ]
    result = asyncio.run(node.ainvoke(_msg(calls), _config()))
    # Side effects ordered despite the slow tool being first.
    assert order == ["slow", "fast"]
    # Result messages in emitted order: marker ack, slow, fast.
    names = [m.name for m in result["messages"]]
    assert names == [SEQUENTIAL_ORDER_TOOL_NAME, "slow_first", "fast_second"]


def test_sequential_sync_path_orders_side_effects():
    order: list[str] = []

    @tool
    def a_tool(x: str = "") -> str:
        """A."""
        order.append("a")
        return "a-done"

    @tool
    def b_tool(x: str = "") -> str:
        """B."""
        order.append("b")
        return "b-done"

    node = SafeToolNode([a_tool, b_tool, run_tools_in_order])
    calls = [
        _marker_call(),
        {"name": "b_tool", "args": {}, "id": "b"},
        {"name": "a_tool", "args": {}, "id": "a"},
    ]
    result = node.invoke(_msg(calls), _config())
    assert order == ["b", "a"]  # emitted order, not alphabetical
    names = [m.name for m in result["messages"]]
    assert names == [SEQUENTIAL_ORDER_TOOL_NAME, "b_tool", "a_tool"]


# ---------------------------------------------------------------------------
# Marker ack / nudge
# ---------------------------------------------------------------------------

def test_marker_ack_counts_siblings():
    node = SafeToolNode([run_tools_in_order])

    @tool
    def noop(x: str = "") -> str:
        """Noop."""
        return "ok"

    node = SafeToolNode([noop, run_tools_in_order])
    calls = [
        _marker_call(),
        {"name": "noop", "args": {}, "id": "a"},
        {"name": "noop", "args": {}, "id": "b"},
    ]
    result = asyncio.run(node.ainvoke(_msg(calls), _config()))
    ack = next(m for m in result["messages"] if m.name == SEQUENTIAL_ORDER_TOOL_NAME)
    assert "Ordered 2 tool call(s)" in ack.content


def test_marker_alone_returns_nudge():
    node = SafeToolNode([run_tools_in_order])
    result = asyncio.run(node.ainvoke(_msg([_marker_call()]), _config()))
    ack = result["messages"][0]
    assert ack.name == SEQUENTIAL_ORDER_TOOL_NAME
    assert "nothing was ordered" in ack.content


# ---------------------------------------------------------------------------
# Live-stream visibility: the inert marker is never dispatched, so on_tool_start/
# on_tool_end never fire for it. The ordered loop emits synthetic tool_call/
# tool_result custom events so the card shows during streaming, not only on
# reload. The events match the LIVE event shape (key ``args``, no ``status``).
# ---------------------------------------------------------------------------

def _capture_custom_events(monkeypatch):
    captured: list[tuple[str, dict]] = []

    async def _afake(name, payload, config=None):
        captured.append((name, payload))

    def _fake(name, payload, config=None):
        captured.append((name, payload))

    monkeypatch.setattr("nymeria.vendor.react_agent.nodes.adispatch_custom_event", _afake)
    monkeypatch.setattr("nymeria.vendor.react_agent.nodes.dispatch_custom_event", _fake)
    return captured


def test_marker_emits_live_frames_async(monkeypatch):
    captured = _capture_custom_events(monkeypatch)

    @tool
    def noop(x: str = "") -> str:
        """Noop."""
        return "ok"

    node = SafeToolNode([noop, run_tools_in_order])
    calls = [
        _marker_call(stop_on_error=True),
        {"name": "noop", "args": {}, "id": "a"},
        {"name": "noop", "args": {}, "id": "b"},
    ]
    result = asyncio.run(node.ainvoke(_msg(calls), _config()))

    # Exactly one tool_call + one tool_result frame, in that order, for the marker.
    assert [name for name, _ in captured] == ["tool_call", "tool_result"]
    call_frame = captured[0][1]
    assert call_frame == {
        "id": "m",
        "name": SEQUENTIAL_ORDER_TOOL_NAME,
        "args": {"stop_on_error": True},  # marker args echoed through
    }
    result_frame = captured[1][1]
    assert result_frame["id"] == "m"
    assert result_frame["name"] == SEQUENTIAL_ORDER_TOOL_NAME
    # The result mirrors the ack content the loop appended to graph state.
    ack = next(m for m in result["messages"] if m.tool_call_id == "m")
    assert result_frame["result"] == ack.content
    assert "Ordered 2 tool call(s)" in result_frame["result"]


def test_marker_emits_live_frames_sync(monkeypatch):
    captured = _capture_custom_events(monkeypatch)

    @tool
    def a_tool(x: str = "") -> str:
        """A."""
        return "a-done"

    node = SafeToolNode([a_tool, run_tools_in_order])
    calls = [_marker_call(), {"name": "a_tool", "args": {}, "id": "a"}]
    result = node.invoke(_msg(calls), _config())

    assert [name for name, _ in captured] == ["tool_call", "tool_result"]
    assert captured[0][1] == {"id": "m", "name": SEQUENTIAL_ORDER_TOOL_NAME, "args": {}}
    ack = next(m for m in result["messages"] if m.tool_call_id == "m")
    assert captured[1][1]["result"] == ack.content


def test_concurrent_batch_emits_no_marker_frames(monkeypatch):
    # No marker -> concurrent path -> the emit helpers are never invoked. (Real
    # tool events on that path come from LangChain callbacks, not custom events.)
    captured = _capture_custom_events(monkeypatch)

    @tool
    async def noop(x: str = "") -> str:
        """Noop."""
        return "ok"

    node = SafeToolNode([noop, run_tools_in_order])
    asyncio.run(node.ainvoke(_msg([{"name": "noop", "args": {}, "id": "a"}]), _config()))
    assert captured == []


def test_marker_frame_dispatch_failure_does_not_break_turn(monkeypatch):
    # A streaming hiccup (dispatch raises) must be swallowed; tool outputs still
    # complete so the turn is unaffected.
    async def _boom_a(name, payload, config=None):
        raise ValueError("dispatch down")

    def _boom(name, payload, config=None):
        raise ValueError("dispatch down")

    monkeypatch.setattr("nymeria.vendor.react_agent.nodes.adispatch_custom_event", _boom_a)
    monkeypatch.setattr("nymeria.vendor.react_agent.nodes.dispatch_custom_event", _boom)

    @tool
    async def noop(x: str = "") -> str:
        """Noop."""
        return "ok"

    node = SafeToolNode([noop, run_tools_in_order])
    calls = [_marker_call(), {"name": "noop", "args": {}, "id": "a"}]
    result = asyncio.run(node.ainvoke(_msg(calls), _config()))
    names = [m.name for m in result["messages"]]
    assert names == [SEQUENTIAL_ORDER_TOOL_NAME, "noop"]
    by_id = {m.tool_call_id: m for m in result["messages"]}
    assert by_id["a"].content == "ok"


# ---------------------------------------------------------------------------
# Per-call timeout: outer batch timeout bypassed, fast sibling survives
# ---------------------------------------------------------------------------

def test_per_call_timeout_fires_and_sibling_survives():
    order: list[str] = []

    @tool
    async def hangs(x: str = "") -> str:
        """Hangs well past the per-call timeout."""
        await asyncio.sleep(5.0)
        order.append("hangs")
        return "should-not-finish"

    @tool
    async def quick(x: str = "") -> str:
        """Quick tool that runs after the slow one times out."""
        await asyncio.sleep(0.01)
        order.append("quick")
        return "quick-done"

    # tool_timeout=1: the slow call is bounded to ~1s by its PER-CALL timeout, then
    # the loop continues. A non-bypassed outer wrapper would instead fire at 1s and
    # mark the whole batch timed out, so 'quick' could never return a real result.
    node = SafeToolNode([hangs, quick, run_tools_in_order], tool_timeout=1)
    calls = [
        _marker_call(),
        {"name": "hangs", "args": {}, "id": "a"},
        {"name": "quick", "args": {}, "id": "b"},
    ]
    result = asyncio.run(node.ainvoke(_msg(calls), _config()))
    by_id = {m.tool_call_id: m for m in result["messages"]}
    assert "[Error]" in by_id["a"].content and "timed out" in by_id["a"].content
    assert by_id["b"].content == "quick-done"  # completed sibling survived
    assert order == ["quick"]


def test_outer_batch_timeout_bypassed_for_long_ordered_batch():
    # Two healthy calls, each comfortably under the per-call timeout, whose SUMMED
    # runtime exceeds it. No call times out; this isolates the outer-bypass: a
    # non-bypassed whole-batch wrapper would kill the second call mid-run and
    # discard everything. tool_timeout=2, each call ~1.3s, sum ~2.6s.
    order: list[str] = []

    @tool
    async def step_a(x: str = "") -> str:
        """First healthy step."""
        await asyncio.sleep(1.3)
        order.append("a")
        return "a-done"

    @tool
    async def step_b(x: str = "") -> str:
        """Second healthy step."""
        await asyncio.sleep(1.3)
        order.append("b")
        return "b-done"

    node = SafeToolNode([step_a, step_b, run_tools_in_order], tool_timeout=2)
    calls = [
        _marker_call(),
        {"name": "step_a", "args": {}, "id": "a"},
        {"name": "step_b", "args": {}, "id": "b"},
    ]
    result = asyncio.run(node.ainvoke(_msg(calls), _config()))
    by_id = {m.tool_call_id: m for m in result["messages"]}
    assert by_id["a"].content == "a-done"
    assert by_id["b"].content == "b-done"
    assert order == ["a", "b"]


def test_notify_single_call_timeout_builds_one_call_input():
    # Deterministic unit test of the parity helper: it must hand on_timeout a
    # synthetic input carrying ONLY the timed-out call (so on_tool_timeout
    # cascade-aborts just that callable sub-thread), plus the real config.
    notified: list[dict] = []
    node = SafeToolNode(
        [run_tools_in_order],
        on_timeout=lambda input_dict, config=None: notified.append(
            {"input": input_dict, "config": config}
        ),
    )
    cfg = _config()
    node._notify_single_call_timeout({"name": "hangs", "args": {}, "id": "a"}, cfg)

    assert len(notified) == 1
    tcs = notified[0]["input"]["messages"][-1].tool_calls
    assert [tc["name"] for tc in tcs] == ["hangs"]
    assert notified[0]["config"] is cfg


def test_per_call_timeout_notifies_only_timed_out_call():
    # Parity with the concurrent path: a per-call timeout must fire on_timeout so a
    # hung callable sub-thread gets cascade-aborted. The completed sibling must NOT
    # be notified (it would wrongly abort an unrelated callable thread by name).
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
        [hangs, quick, run_tools_in_order],
        tool_timeout=1,
        on_timeout=lambda input_dict, config=None: notified.append(
            {"input": input_dict, "config": config}
        ),
    )
    calls = [
        _marker_call(),
        {"name": "hangs", "args": {}, "id": "a"},
        {"name": "quick", "args": {}, "id": "b"},
    ]
    asyncio.run(node.ainvoke(_msg(calls), _config()))

    assert len(notified) == 1
    tcs = notified[0]["input"]["messages"][-1].tool_calls
    assert [tc["name"] for tc in tcs] == ["hangs"]
    assert notified[0]["config"] is not None


def test_graph_bubble_up_propagates_not_converted():
    # GraphInterrupt (a GraphBubbleUp subclass) raised inside a tool must propagate
    # out of the ordered loop, never be converted into an error ToolMessage.
    from langgraph.errors import GraphBubbleUp

    @tool
    async def interrupts(x: str = "") -> str:
        """Raises a bubble-up exception."""
        raise GraphBubbleUp()

    node = SafeToolNode([interrupts, run_tools_in_order])
    calls = [_marker_call(), {"name": "interrupts", "args": {}, "id": "a"}]
    with pytest.raises(GraphBubbleUp):
        asyncio.run(node.ainvoke(_msg(calls), _config()))


def test_graph_bubble_up_propagates_sync_path():
    from langgraph.errors import GraphBubbleUp

    @tool
    def interrupts(x: str = "") -> str:
        """Raises a bubble-up exception (sync path)."""
        raise GraphBubbleUp()

    node = SafeToolNode([interrupts, run_tools_in_order])
    calls = [_marker_call(), {"name": "interrupts", "args": {}, "id": "a"}]
    with pytest.raises(GraphBubbleUp):
        node.invoke(_msg(calls), _config())


# ---------------------------------------------------------------------------
# stop_on_error
# ---------------------------------------------------------------------------

def test_stop_on_error_skips_remaining_calls():
    order: list[str] = []

    @tool
    async def failing(x: str = "") -> str:
        """Returns the [Error]: string convention rather than raising."""
        order.append("failing")
        return "[Error]: boom"

    @tool
    async def after(x: str = "") -> str:
        """Should be skipped when stop_on_error is set."""
        order.append("after")
        return "after-done"

    node = SafeToolNode([failing, after, run_tools_in_order])
    calls = [
        _marker_call(stop_on_error=True),
        {"name": "failing", "args": {}, "id": "a"},
        {"name": "after", "args": {}, "id": "b"},
    ]
    result = asyncio.run(node.ainvoke(_msg(calls), _config()))
    by_id = {m.tool_call_id: m for m in result["messages"]}
    assert by_id["a"].content == "[Error]: boom"
    assert "Skipped" in by_id["b"].content
    assert order == ["failing"]  # 'after' never ran


def test_stop_on_error_false_runs_all_despite_failure():
    order: list[str] = []

    @tool
    async def failing(x: str = "") -> str:
        """Fails via the [Error]: convention."""
        order.append("failing")
        return "[Error]: boom"

    @tool
    async def after(x: str = "") -> str:
        """Runs regardless under the default stop_on_error=False."""
        order.append("after")
        return "after-done"

    node = SafeToolNode([failing, after, run_tools_in_order])
    calls = [
        _marker_call(),  # stop_on_error defaults False
        {"name": "failing", "args": {}, "id": "a"},
        {"name": "after", "args": {}, "id": "b"},
    ]
    result = asyncio.run(node.ainvoke(_msg(calls), _config()))
    by_id = {m.tool_call_id: m for m in result["messages"]}
    assert by_id["b"].content == "after-done"
    assert order == ["failing", "after"]


# ---------------------------------------------------------------------------
# Per-step abort
# ---------------------------------------------------------------------------

def test_abort_mid_sequence_cancels_remainder(monkeypatch):
    order: list[str] = []
    abort_event = threading.Event()

    @tool
    async def first(x: str = "") -> str:
        """Sets the abort event after running."""
        order.append("first")
        abort_event.set()
        return "first-done"

    @tool
    async def second(x: str = "") -> str:
        """Should be cancelled because abort fired before it."""
        order.append("second")
        return "second-done"

    class _Locks:
        def get_abort_event(self, _tid):
            return abort_event

    class _Agent:
        _thread_locks = _Locks()

    import nymeria.core.agent as agent_mod

    monkeypatch.setattr(agent_mod, "get_current_agent", lambda: _Agent())

    node = SafeToolNode([first, second, run_tools_in_order])
    calls = [
        _marker_call(),
        {"name": "first", "args": {}, "id": "a"},
        {"name": "second", "args": {}, "id": "b"},
    ]
    result = asyncio.run(node.ainvoke(_msg(calls), _config()))
    by_id = {m.tool_call_id: m for m in result["messages"]}
    assert by_id["a"].content == "first-done"
    assert "cancelled" in by_id["b"].content
    assert order == ["first"]  # second never ran
    # Every tool_call still has a matching ToolMessage.
    assert {m.tool_call_id for m in result["messages"]} == {"m", "a", "b"}


# ---------------------------------------------------------------------------
# analyze_turn_safety exclusion
# ---------------------------------------------------------------------------

def test_marker_excluded_from_repeated_result_guard():
    # Five completed marker exchanges + a sixth marker request: must NOT stop.
    messages: list = [HumanMessage(content="go")]
    for i in range(5):
        messages.append(AIMessage(content="", tool_calls=[_marker_call(call_id=f"m{i}")]))
        messages.append(ToolMessage(content="Ordered 0 tool call(s)", tool_call_id=f"m{i}"))
    messages.append(AIMessage(content="", tool_calls=[_marker_call(call_id="m-final")]))

    safety = analyze_turn_safety(messages, max_iterations=100, repeated_tool_result_limit=5)
    assert safety.should_stop is False


def test_non_marker_tool_still_trips_guard():
    # Sanity: a real tool repeating identically still trips the guard (we didn't
    # weaken runaway protection for the actual work).
    messages: list = [HumanMessage(content="go")]
    for i in range(5):
        messages.append(
            AIMessage(content="", tool_calls=[{"name": "loop", "args": {"q": 1}, "id": f"c{i}"}])
        )
        messages.append(ToolMessage(content="same", tool_call_id=f"c{i}"))
    messages.append(AIMessage(content="", tool_calls=[{"name": "loop", "args": {"q": 1}, "id": "c-final"}]))

    safety = analyze_turn_safety(messages, max_iterations=100, repeated_tool_result_limit=5)
    assert safety.should_stop is True


# ---------------------------------------------------------------------------
# Coupling guards (fail loudly on a langgraph upgrade)
# ---------------------------------------------------------------------------

def test_marker_name_matches_tool_name():
    assert run_tools_in_order.name == SEQUENTIAL_ORDER_TOOL_NAME


def test_func_afunc_signature_parity():
    params_func = list(inspect.signature(SafeToolNode._func).parameters)
    params_afunc = list(inspect.signature(SafeToolNode._afunc).parameters)
    assert params_func == params_afunc == ["self", "input", "config", "runtime"]


def test_tool_runtime_field_set_unchanged():
    from langgraph.prebuilt.tool_node import ToolRuntime

    field_names = {f.name for f in dataclasses.fields(ToolRuntime)}
    required = {
        "state",
        "tool_call_id",
        "config",
        "context",
        "store",
        "stream_writer",
        "tools",
        "execution_info",
        "server_info",
    }
    missing = required - field_names
    assert not missing, f"ToolRuntime lost fields the ordered loop constructs: {missing}"


def test_extract_state_signature_unchanged():
    # The ordered loop calls self._extract_state(input, config); pin the arity.
    params = list(inspect.signature(SafeToolNode._extract_state).parameters)
    assert params == ["self", "input", "config"]


def test_reused_helpers_present():
    for name in ("_run_one", "_arun_one", "_combine_tool_outputs", "_parse_input"):
        assert callable(getattr(SafeToolNode, name)), f"missing reused helper {name}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
