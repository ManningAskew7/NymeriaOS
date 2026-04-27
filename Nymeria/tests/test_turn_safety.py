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
