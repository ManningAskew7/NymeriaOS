"""Tests for ReAct turn safety limits."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from nymeria.vendor.react_agent.nodes import (
    TURN_SAFETY_REASON_MAX_ITERATIONS,
    TURN_SAFETY_REASON_REPEATED_TOOL_RESULT,
    analyze_turn_safety,
)


def _ai_call(call_id: str, name: str = "lookup", args: dict | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": name, "args": args or {"q": "same"}}],
    )


def _tool_result(call_id: str, content: str = "same result") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=call_id)


def _repeat_exchange(
    count: int,
    name: str = "lookup",
    args: dict | None = None,
    result: str = "same result",
):
    messages = []
    for index in range(count):
        call_id = f"call-{index}"
        messages.append(_ai_call(call_id, name=name, args=args))
        messages.append(_tool_result(call_id, result))
    return messages


def test_turn_safety_does_not_stop_below_hard_cap():
    messages = [HumanMessage(content="go")]
    messages.extend(_ai_call(f"call-{index}", args={"i": index}) for index in range(500))

    safety = analyze_turn_safety(messages, max_iterations=500)

    assert safety.should_stop is False
    assert safety.tool_call_count == 500


def test_turn_safety_stops_above_hard_cap():
    messages = [HumanMessage(content="go")]
    messages.extend(_ai_call(f"call-{index}", args={"i": index}) for index in range(501))

    safety = analyze_turn_safety(messages, max_iterations=500)

    assert safety.should_stop is True
    assert safety.reason == TURN_SAFETY_REASON_MAX_ITERATIONS
    assert safety.tool_call_count == 501


def test_turn_safety_stops_repeated_same_tool_args_and_result():
    messages = [HumanMessage(content="go")]
    messages.extend(_repeat_exchange(5))
    messages.append(_ai_call("call-next"))

    safety = analyze_turn_safety(messages, max_iterations=500, repeated_tool_result_limit=5)

    assert safety.should_stop is True
    assert safety.reason == TURN_SAFETY_REASON_REPEATED_TOOL_RESULT
    assert safety.repeated_tool_name == "lookup"
    assert safety.repeated_count == 5


def test_turn_safety_allows_same_args_with_different_results():
    messages = [HumanMessage(content="go")]
    for index in range(5):
        call_id = f"call-{index}"
        messages.append(_ai_call(call_id))
        messages.append(_tool_result(call_id, f"result {index}"))
    messages.append(_ai_call("call-next"))

    safety = analyze_turn_safety(messages, max_iterations=500, repeated_tool_result_limit=5)

    assert safety.should_stop is False


def test_turn_safety_allows_different_args_with_same_result():
    messages = [HumanMessage(content="go")]
    for index in range(5):
        call_id = f"call-{index}"
        messages.append(_ai_call(call_id, args={"q": index}))
        messages.append(_tool_result(call_id, "same result"))
    messages.append(_ai_call("call-next", args={"q": 5}))

    safety = analyze_turn_safety(messages, max_iterations=500, repeated_tool_result_limit=5)

    assert safety.should_stop is False


def test_turn_safety_ignores_previous_turn_repeats():
    messages = [HumanMessage(content="old")]
    messages.extend(_repeat_exchange(5))
    messages.append(HumanMessage(content="new"))
    messages.append(_ai_call("call-next"))

    safety = analyze_turn_safety(messages, max_iterations=500, repeated_tool_result_limit=5)

    assert safety.should_stop is False


# ---------------------------------------------------------------------------
# Graceful cap halt (backlog #27): the max-iterations cap fires at the
# sub-turn boundary (route_after_tools, ToolMessage-terminal tail) instead of
# pre-execution in should_continue, and a resume offset anchors the window.
# ---------------------------------------------------------------------------


def _exchanges(count: int) -> list:
    """``count`` completed, DISTINCT tool exchanges (no repeat-guard trips)."""
    messages = []
    for index in range(count):
        call_id = f"call-{index}"
        messages.append(_ai_call(call_id, args={"i": index}))
        messages.append(_tool_result(call_id, f"result {index}"))
    return messages


def test_turn_safety_cap_fires_on_tool_terminal_tail():
    # The graceful-halt shape: the crossing batch executed, results are in
    # history, the model call is pending.
    messages = [HumanMessage(content="go")] + _exchanges(6)

    safety = analyze_turn_safety(messages, max_iterations=5)

    assert safety.should_stop is True
    assert safety.reason == TURN_SAFETY_REASON_MAX_ITERATIONS
    assert safety.tool_call_count == 6


def test_turn_safety_no_stop_on_tool_terminal_tail_below_cap():
    messages = [HumanMessage(content="go")] + _exchanges(5)

    safety = analyze_turn_safety(messages, max_iterations=5)

    assert safety.should_stop is False


def test_turn_safety_no_stop_on_final_answer_even_above_cap():
    # A turn that ended with a normal final answer must never read as a cap
    # halt, whatever the count (guards the post-drive detection).
    messages = [HumanMessage(content="go")] + _exchanges(6)
    messages.append(AIMessage(content="done!"))

    safety = analyze_turn_safety(messages, max_iterations=5)

    assert safety.should_stop is False


def test_turn_safety_offset_anchors_window():
    messages = [HumanMessage(content="go")] + _exchanges(6)

    # Full anchor at the resume point: fresh window.
    anchored = analyze_turn_safety(messages, max_iterations=5, tool_call_offset=6)
    assert anchored.should_stop is False
    assert anchored.tool_call_count == 0

    # Partial offset: effective count = 5, not above the cap.
    partial = analyze_turn_safety(messages, max_iterations=5, tool_call_offset=1)
    assert partial.should_stop is False
    assert partial.tool_call_count == 5

    # No offset: the same tail is a cap halt.
    raw = analyze_turn_safety(messages, max_iterations=5)
    assert raw.should_stop is True


def test_should_continue_no_longer_enforces_cap():
    from nymeria.vendor.react_agent.nodes import create_should_continue

    should_continue = create_should_continue(
        max_iterations=5, repeated_tool_result_limit=5
    )
    messages = [HumanMessage(content="go")] + _exchanges(6)
    messages.append(_ai_call("call-pending", args={"i": "pending"}))

    # Above the cap with a pending batch: routes to tools (the cap now halts
    # in route_after_tools AFTER the batch executes).
    assert should_continue({"messages": messages}) == "tools"


def test_should_continue_still_ends_on_repeated_loop():
    from nymeria.vendor.react_agent.nodes import create_should_continue

    should_continue = create_should_continue(
        max_iterations=500, repeated_tool_result_limit=5
    )
    messages = [HumanMessage(content="go")]
    messages.extend(_repeat_exchange(5))
    messages.append(_ai_call("call-next"))

    assert should_continue({"messages": messages}) == "end"


def _route_state(count: int) -> dict:
    return {"messages": [HumanMessage(content="go")] + _exchanges(count)}


def test_route_after_tools_halts_on_cap_stamp(monkeypatch):
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: None)
    from nymeria.vendor.react_agent.nodes import route_after_tools

    config = {
        "configurable": {
            "thread_id": "t-cap-halt",
            "turn_safety_max_iterations": 5,
        }
    }
    assert route_after_tools(_route_state(6), config) == "end"
    assert route_after_tools(_route_state(5), config) == "agent"


def test_route_after_tools_offset_allows_resumed_window(monkeypatch):
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: None)
    from nymeria.vendor.react_agent.nodes import route_after_tools

    config = {
        "configurable": {
            "thread_id": "t-cap-resume",
            "turn_safety_max_iterations": 5,
            "turn_safety_tool_call_offset": 6,
        }
    }
    # 6 pre-resume calls anchored away: the continuation window is fresh.
    assert route_after_tools(_route_state(6), config) == "agent"
    # The continuation crossing its own window still halts (6 + 6 > 5 + 6).
    assert route_after_tools(_route_state(12), config) == "end"


def test_route_after_tools_agent_fallback_cap(monkeypatch):
    from types import SimpleNamespace

    fake_agent = SimpleNamespace(
        should_halt_for_subturn_compaction=lambda *a: False,
        _max_iterations_for_thread=lambda thread_id: 5,
    )
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: fake_agent)
    from nymeria.vendor.react_agent.nodes import route_after_tools

    config = {"configurable": {"thread_id": "t-cap-fallback"}}
    assert route_after_tools(_route_state(6), config) == "end"


def test_route_after_tools_no_cap_without_stamp_or_agent(monkeypatch):
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: None)
    from nymeria.vendor.react_agent.nodes import route_after_tools

    config = {"configurable": {"thread_id": "t-cap-none"}}
    # No stamp and no agent: the recursion limit stays the backstop.
    assert route_after_tools(_route_state(50), config) == "agent"
