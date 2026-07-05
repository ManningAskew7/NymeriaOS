from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from nymeria.core.agent_context import (
    RewindResult,
    RewindTargetNotFound,
    _mark_in_flight_tail,
    rewind_thread,
    rewind_thread_exchanges,
    trim_context_window,
)


class _FakeGraph:
    def __init__(self, messages: list) -> None:
        self.messages = messages
        self.update_calls: list[tuple[dict, dict]] = []

    def get_state(self, _config: dict) -> SimpleNamespace:
        return SimpleNamespace(values={"messages": self.messages})

    def update_state(self, config: dict, payload: dict) -> None:
        self.update_calls.append((config, payload))


class _FakeAgent:
    def __init__(self, messages: list) -> None:
        self.settings = SimpleNamespace(sliding_window_cycles=1)
        self._default_graph = _FakeGraph(messages)
        self.flush_calls: list[tuple[str, str, list]] = []

    def _flush_memories_before_trim(
        self,
        *,
        user_id: str,
        thread_id: str,
        messages_to_remove: list,
    ) -> None:
        self.flush_calls.append((user_id, thread_id, messages_to_remove))


def test_trim_context_window_uses_agent_flush_facade() -> None:
    messages = [
        HumanMessage(content="first", id="h1"),
        AIMessage(content="first response", id="a1"),
        HumanMessage(content="second", id="h2"),
        AIMessage(content="second response", id="a2"),
    ]
    agent = _FakeAgent(messages)

    removed = trim_context_window(
        cast(Any, agent),
        "thread-1",
        max_cycles=1,
        user_id="alice",
    )

    assert removed == 2
    assert agent.flush_calls == [("alice", "thread-1", messages[:2])]
    assert len(agent._default_graph.update_calls) == 1
    _, payload = agent._default_graph.update_calls[0]
    assert [command.id for command in payload["messages"]] == ["h1", "a1"]


def _three_cycle_messages() -> list:
    return [
        HumanMessage(content="first", id="h1"),
        AIMessage(content="first response", id="a1"),
        HumanMessage(content="second", id="h2"),
        AIMessage(content="second tool step", id="a2"),
        AIMessage(content="second final", id="a3"),
        HumanMessage(content="third", id="h3"),
        AIMessage(content="third response", id="a4"),
    ]


def test_rewind_removes_last_exchange_by_default() -> None:
    messages = _three_cycle_messages()
    agent = _FakeAgent(messages)

    removed = rewind_thread_exchanges(cast(Any, agent), "thread-1")

    assert removed == 2
    assert len(agent._default_graph.update_calls) == 1
    _, payload = agent._default_graph.update_calls[0]
    assert [command.id for command in payload["messages"]] == ["h3", "a4"]


def test_rewind_steps_removes_multiple_exchanges() -> None:
    messages = _three_cycle_messages()
    agent = _FakeAgent(messages)

    removed = rewind_thread_exchanges(cast(Any, agent), "thread-1", steps=2)

    assert removed == 5
    _, payload = agent._default_graph.update_calls[0]
    assert [command.id for command in payload["messages"]] == [
        "h2",
        "a2",
        "a3",
        "h3",
        "a4",
    ]


def test_rewind_clamps_to_available_cycles() -> None:
    messages = _three_cycle_messages()
    agent = _FakeAgent(messages)

    removed = rewind_thread_exchanges(cast(Any, agent), "thread-1", steps=99)

    assert removed == len(messages)
    _, payload = agent._default_graph.update_calls[0]
    assert [command.id for command in payload["messages"]] == [
        m.id for m in messages
    ]


def test_rewind_zero_or_negative_steps_is_noop() -> None:
    agent = _FakeAgent(_three_cycle_messages())

    assert rewind_thread_exchanges(cast(Any, agent), "thread-1", steps=0) == 0
    assert rewind_thread_exchanges(cast(Any, agent), "thread-1", steps=-1) == 0
    assert agent._default_graph.update_calls == []


def test_rewind_handles_empty_thread() -> None:
    agent = _FakeAgent([])

    assert rewind_thread_exchanges(cast(Any, agent), "thread-1") == 0
    assert agent._default_graph.update_calls == []


def test_rewind_noop_when_no_human_messages() -> None:
    agent = _FakeAgent([AIMessage(content="orphan", id="a1")])

    assert rewind_thread_exchanges(cast(Any, agent), "thread-1") == 0
    assert agent._default_graph.update_calls == []


def test_rewind_to_message_id_cuts_mid_thread() -> None:
    messages = _three_cycle_messages()
    agent = _FakeAgent(messages)

    result = rewind_thread(cast(Any, agent), "thread-1", to_message_id="h2")

    assert result == RewindResult(removed=5, exchanges=2)
    assert len(agent._default_graph.update_calls) == 1
    _, payload = agent._default_graph.update_calls[0]
    assert [command.id for command in payload["messages"]] == [
        "h2",
        "a2",
        "a3",
        "h3",
        "a4",
    ]


def test_rewind_to_message_id_wins_over_steps() -> None:
    agent = _FakeAgent(_three_cycle_messages())

    result = rewind_thread(
        cast(Any, agent), "thread-1", steps=99, to_message_id="h3"
    )

    assert result == RewindResult(removed=2, exchanges=1)
    _, payload = agent._default_graph.update_calls[0]
    assert [command.id for command in payload["messages"]] == ["h3", "a4"]


def test_rewind_to_unknown_message_id_raises() -> None:
    agent = _FakeAgent(_three_cycle_messages())

    with pytest.raises(RewindTargetNotFound):
        rewind_thread(cast(Any, agent), "thread-1", to_message_id="missing")
    assert agent._default_graph.update_calls == []


def test_rewind_to_non_user_message_id_raises() -> None:
    agent = _FakeAgent(_three_cycle_messages())

    with pytest.raises(RewindTargetNotFound):
        rewind_thread(cast(Any, agent), "thread-1", to_message_id="a2")
    assert agent._default_graph.update_calls == []


def test_rewind_to_message_id_on_empty_thread_raises() -> None:
    agent = _FakeAgent([])

    with pytest.raises(RewindTargetNotFound):
        rewind_thread(cast(Any, agent), "thread-1", to_message_id="h1")
    assert agent._default_graph.update_calls == []


def test_rewind_thread_steps_mode_reports_exchanges() -> None:
    agent = _FakeAgent(_three_cycle_messages())

    result = rewind_thread(cast(Any, agent), "thread-1", steps=2)

    assert result == RewindResult(removed=5, exchanges=2)


class _FakeLocks:
    def __init__(self, locked: bool) -> None:
        self._locked = locked

    def get_lock_info(self, _thread_id: str):
        return object() if self._locked else None


class _ProcessingAgent:
    def __init__(self, locked: bool) -> None:
        self._thread_locks = _FakeLocks(locked)


def _formatted(role: str = "assistant") -> list[dict]:
    return [
        {"role": "user", "content": "hi"},
        {"role": role, "content": "partial reply"},
    ]


def test_mark_in_flight_tail_marks_processing_assistant_tail() -> None:
    # Mid-turn switch: thread is processing and the raw tail is the in-flight
    # turn's assistant output -> the displayed tail must continue streaming.
    formatted = _formatted()
    raw = [HumanMessage(content="hi", id="h1"), AIMessage(content="partial", id="a1")]

    _mark_in_flight_tail(cast(Any, _ProcessingAgent(True)), "t1", raw, formatted)

    assert formatted[-1]["processing"] is True


def test_mark_in_flight_tail_marks_when_raw_tail_is_tool_message() -> None:
    # A tool just returned and the model is about to continue the same turn.
    formatted = _formatted()
    raw = [
        AIMessage(content="", id="a1"),
        ToolMessage(content="result", tool_call_id="tc1", id="t-msg"),
    ]

    _mark_in_flight_tail(cast(Any, _ProcessingAgent(True)), "t1", raw, formatted)

    assert formatted[-1]["processing"] is True


def test_mark_in_flight_tail_skips_handoff_with_queued_human_input() -> None:
    # Back-to-back handoff: a new turn is queued (raw tail is a wake-up
    # HumanMessage) but has not produced assistant output yet. The displayed
    # tail is the *prior* reply and must NOT be reused, or the new turn would
    # graft onto it (the regression fixed in 08f92b87).
    formatted = _formatted()
    raw = [
        AIMessage(content="prior reply", id="a1"),
        HumanMessage(content="autonomous wake-up", id="h2"),
    ]

    _mark_in_flight_tail(cast(Any, _ProcessingAgent(True)), "t1", raw, formatted)

    assert "processing" not in formatted[-1]


def test_mark_in_flight_tail_skips_when_not_processing() -> None:
    formatted = _formatted()
    raw = [HumanMessage(content="hi", id="h1"), AIMessage(content="done", id="a1")]

    _mark_in_flight_tail(cast(Any, _ProcessingAgent(False)), "t1", raw, formatted)

    assert "processing" not in formatted[-1]


def test_mark_in_flight_tail_skips_when_displayed_tail_not_assistant() -> None:
    formatted = _formatted(role="user")
    raw = [AIMessage(content="x", id="a1")]

    _mark_in_flight_tail(cast(Any, _ProcessingAgent(True)), "t1", raw, formatted)

    assert "processing" not in formatted[-1]
