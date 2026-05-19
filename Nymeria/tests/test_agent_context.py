from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage

from nymeria.core.agent_context import trim_context_window


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
