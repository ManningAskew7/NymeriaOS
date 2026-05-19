"""Tests for NymeriaAgent callable-lifecycle facade dispatch."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any, cast

from langchain_core.messages import AIMessage

from nymeria.core.agent_callable_lifecycle import (
    abort_with_cascade,
    on_tool_timeout,
    resolve_callable_timeout_thread_id,
)


class _OnTimeoutFacadeAgent:
    def __init__(self) -> None:
        self.resolve_calls: list[tuple[Any, ...]] = []
        self.abort_calls: list[str] = []

    def _resolve_callable_timeout_thread_id(
        self,
        tool_name: str | None,
        user_id: str | None,
        caller_thread_id: str | None,
    ) -> str | None:
        self.resolve_calls.append((tool_name, user_id, caller_thread_id))
        return "resolved-thread-id"

    def abort_with_cascade(self, thread_id: str) -> None:
        self.abort_calls.append(thread_id)


class _ResolveFacadeAgent:
    def __init__(self) -> None:
        self.scope_calls: list[tuple[Any, ...]] = []
        self._callable_tool_thread_map: dict[str, str] = {}

    def _callable_timeout_scope_user_id(
        self,
        user_id: str | None,
        caller_thread_id: str | None,
    ) -> str | None:
        self.scope_calls.append((user_id, caller_thread_id))
        return "scoped-user"

    def _get_team_scoped_callable_threads(
        self,
        user_id: str,
        caller_thread_id: str,
    ) -> list[Any]:
        return [SimpleNamespace(callable_name="my_tool", thread_id="callable-thread-id")]


class _RecursionFacadeAgent:
    def __init__(self) -> None:
        self.signal_aborts: list[str] = []
        self.recursion_calls: list[str] = []
        self._invocations_lock = threading.Lock()
        self._active_callable_invocations: dict[str, set[str]] = {
            "parent": {"child-a", "child-b"},
        }
        self._thread_locks = SimpleNamespace(signal_abort=self._record_signal_abort)

    def _record_signal_abort(self, thread_id: str) -> None:
        self.signal_aborts.append(thread_id)

    def abort_with_cascade(self, thread_id: str) -> None:
        self.recursion_calls.append(thread_id)


def test_on_tool_timeout_uses_agent_resolve_and_abort_facades():
    agent = _OnTimeoutFacadeAgent()
    ai_msg = AIMessage(
        content="",
        tool_calls=[{"id": "call-1", "name": "my_tool", "args": {}}],
    )
    input_dict = {"messages": [ai_msg]}
    config = {"configurable": {"user_id": "user-1", "thread_id": "thread-1"}}

    on_tool_timeout(cast(Any, agent), input_dict, config)

    assert agent.resolve_calls == [("my_tool", "user-1", "thread-1")]
    assert agent.abort_calls == ["resolved-thread-id"]


def test_resolve_callable_timeout_thread_id_uses_agent_scope_facade():
    agent = _ResolveFacadeAgent()

    result = resolve_callable_timeout_thread_id(
        cast(Any, agent),
        tool_name="my_tool",
        user_id="user-1",
        caller_thread_id="thread-1",
    )

    assert result == "callable-thread-id"
    assert agent.scope_calls == [("user-1", "thread-1")]


def test_abort_with_cascade_uses_agent_facade_for_recursion():
    agent = _RecursionFacadeAgent()

    abort_with_cascade(cast(Any, agent), "parent")

    assert agent.signal_aborts == ["parent"]
    assert sorted(agent.recursion_calls) == ["child-a", "child-b"]
