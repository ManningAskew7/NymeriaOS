"""Tests for NymeriaAgent turn-safety facade dispatch."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from nymeria.core.agent_safety import (
    check_iteration_limit_hit,
    graph_run_config,
    turn_safety_event,
)
from nymeria.vendor.react_agent.nodes import (
    TURN_SAFETY_REASON_REPEATED_TOOL_RESULT,
    TurnSafetyResult,
)


class _AnalyzeFacadeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def _analyze_turn_safety(self, messages: list[Any], max_iterations: int) -> Any:
        self.calls.append(("analyze", messages, max_iterations))
        return SimpleNamespace(should_stop=True)


class _GraphConfigFacadeAgent:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def _max_iterations_for_thread(self, thread_id: str | None) -> int:
        self.calls.append(("max_iterations", thread_id))
        return 17

    def _recursion_limit_for_iterations(self, max_iterations: int) -> int:
        self.calls.append(("recursion_limit", max_iterations))
        return 177


class _EventFacadeAgent:
    TURN_SAME_TOOL_RESULT_LIMIT = 5

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def _turn_safety_content(self, safety: TurnSafetyResult) -> str:
        self.calls.append(("content", safety))
        return "facade content"


def test_check_iteration_limit_hit_uses_agent_analyze_facade():
    agent = _AnalyzeFacadeAgent()
    messages = [object()]

    assert check_iteration_limit_hit(cast(Any, agent), messages, 9) is True
    assert agent.calls == [("analyze", messages, 9)]


def test_graph_run_config_uses_agent_facades():
    agent = _GraphConfigFacadeAgent()
    callbacks: list[Any] = ["callback"]

    config = graph_run_config(cast(Any, agent), "thread-a", "user-b", callbacks)

    assert config == {
        "recursion_limit": 177,
        "configurable": {"thread_id": "thread-a", "user_id": "user-b"},
        "callbacks": callbacks,
    }
    assert agent.calls == [
        ("max_iterations", "thread-a"),
        ("recursion_limit", 17),
    ]


def test_turn_safety_event_uses_agent_content_facade():
    agent = _EventFacadeAgent()
    safety = TurnSafetyResult(
        should_stop=True,
        reason=TURN_SAFETY_REASON_REPEATED_TOOL_RESULT,
        tool_call_count=18,
        max_iterations=17,
        repeated_tool_name="lookup",
        repeated_count=5,
    )

    event = turn_safety_event(cast(Any, agent), safety, scope="callable_thread")

    assert event == {
        "type": "iteration_limit",
        "scope": "callable_thread",
        "reason": TURN_SAFETY_REASON_REPEATED_TOOL_RESULT,
        "content": "facade content",
        "max_iterations": 17,
        "tool_call_count": 18,
        "repeated_tool_name": "lookup",
        "repeated_count": 5,
    }
    assert agent.calls == [("content", safety)]
