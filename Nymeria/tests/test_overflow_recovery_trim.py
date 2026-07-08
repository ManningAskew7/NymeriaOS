"""Regression tests for context-overflow recovery's direct-trim head validity.

The overflow-recovery "direct trim" fallback removes the oldest messages when no
rewind checkpoint exists. It must only cut on a HumanMessage boundary so the
retained head starts a clean user turn: any other head (an orphaned tool_result,
or a first-message assistant turn) is a provider 400, and the trim is committed
to the durable checkpoint BEFORE the summary LLM call runs, so an invalid head
would permanently brick the thread. These tests pin that invariant on the pure
boundary helper and on the sync trim caller.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)

from nymeria.core.agent_compaction import CompactionManager


# --- message builders ------------------------------------------------------


def _human(i: int, *, internal: bool = False) -> HumanMessage:
    kwargs = {"internal": True} if internal else {}
    return HumanMessage(content=f"user {i}", id=f"h{i}", additional_kwargs=kwargs)


def _ai_tool_call(i: int, *, n: int = 1) -> AIMessage:
    calls = [
        {"name": "do_thing", "args": {}, "id": f"call{i}_{k}"} for k in range(n)
    ]
    return AIMessage(content="", tool_calls=calls, id=f"a{i}")


def _tool(i: int, call_id: str) -> ToolMessage:
    return ToolMessage(content="ok", tool_call_id=call_id, id=f"t{call_id}")


def _ai_text(i: int) -> AIMessage:
    return AIMessage(content=f"reply {i}", id=f"r{i}")


def _autonomous_loop(turns: int = 7) -> List[Any]:
    """One (internal) wake-up followed by a long tool loop, no interior Human."""
    msgs: List[Any] = [_human(0, internal=True)]
    for i in range(1, turns + 1):
        msgs.append(_ai_tool_call(i))
        msgs.append(_tool(i, f"call{i}_0"))
    return msgs


def _interactive_thread(turns: int = 6) -> List[Any]:
    """turns user turns, each: Human -> AI(tool_call) -> Tool -> AI(text)."""
    msgs: List[Any] = []
    for i in range(turns):
        msgs.append(_human(i))
        msgs.append(_ai_tool_call(i))
        msgs.append(_tool(i, f"call{i}_0"))
        msgs.append(_ai_text(i))
    return msgs


# --- harness ---------------------------------------------------------------


def _manager(*, compact_keep_messages: int = 4) -> CompactionManager:
    agent: Any = SimpleNamespace(
        settings=SimpleNamespace(compact_keep_messages=compact_keep_messages)
    )
    return CompactionManager(agent)


def _stub_tokens(cm: CompactionManager, *, target: int, per_msg: int = 100) -> None:
    """Make token estimation deterministic: per_msg tokens per message."""
    cm._recovery_target_tokens = lambda thread_id: target  # type: ignore[method-assign]
    cm._model_for = lambda thread_id: ""  # type: ignore[method-assign]
    cm._estimate_messages_tokens = (  # type: ignore[method-assign]
        lambda messages, model="": len(messages) * per_msg
    )


class _FakeGraph:
    """Minimal graph stub for the sync direct-trim caller."""

    def __init__(self, messages: List[Any]) -> None:
        self._messages = messages
        self.updates: List[Any] = []

    def get_state(self, config: Any) -> Any:
        return SimpleNamespace(values={"messages": self._messages})

    def update_state(self, config: Any, payload: Any) -> None:
        self.updates.append(payload)


_CONFIG = {"configurable": {"thread_id": "t", "user_id": "u"}}


# --- _recovery_prefix_remove_count -----------------------------------------


def test_autonomous_loop_no_interior_human_declines() -> None:
    # A single wake-up then a long tool loop: the only Human is at index 0, so
    # there is no valid cut. Recovery must decline (return 0) rather than strand
    # a ToolMessage/AIMessage head.
    cm = _manager()
    msgs = _autonomous_loop(turns=7)  # 15 messages, Human only at index 0
    assert cm._recovery_prefix_remove_count("t", msgs) == 0


def test_interior_human_cut_lands_on_human_head_under_target() -> None:
    cm = _manager()
    msgs = _interactive_thread(turns=6)  # 24 msgs; Human at 0,4,8,12,16,20
    # Already under target -> smallest cut (first interior boundary).
    _stub_tokens(cm, target=10**9)
    idx = cm._recovery_prefix_remove_count("t", msgs)
    assert idx == 4
    assert isinstance(msgs[idx], HumanMessage)


def test_interior_human_cut_honors_token_target() -> None:
    cm = _manager()
    msgs = _interactive_thread(turns=6)  # 24 msgs; boundaries [4,8,12,16,20]
    # per_msg=100 -> only the last boundary's 4-message tail (400) fits target.
    _stub_tokens(cm, target=500, per_msg=100)
    idx = cm._recovery_prefix_remove_count("t", msgs)
    assert idx == 20
    assert isinstance(msgs[idx], HumanMessage)


def test_parallel_tool_batch_head_stays_human() -> None:
    # A turn whose AIMessage has two tool_calls with two results. The only cut
    # offered is the interior Human, never an index inside the parallel batch.
    cm = _manager()
    msgs: List[Any] = [
        _human(0),
        _ai_tool_call(0, n=2),
        _tool(0, "call0_0"),
        _tool(0, "call0_1"),
        _ai_text(0),
        _human(1),
        _ai_tool_call(1, n=2),
        _tool(1, "call1_0"),
        _tool(1, "call1_1"),
        _ai_text(1),
    ]  # 10 msgs; Human at 0 and 5; max_prefix = 6 -> only boundary is 5
    _stub_tokens(cm, target=100, per_msg=100)
    idx = cm._recovery_prefix_remove_count("t", msgs)
    assert idx == 5
    assert isinstance(msgs[idx], HumanMessage)
    # Never lands on a ToolMessage or a mid-batch AIMessage.
    assert not isinstance(msgs[idx], (ToolMessage, AIMessage))


def test_short_thread_returns_zero() -> None:
    cm = _manager(compact_keep_messages=4)
    msgs = [_human(0), _ai_text(0)]  # <= compact_keep_messages
    assert cm._recovery_prefix_remove_count("t", msgs) == 0


# --- _direct_trim_for_recovery_sync ----------------------------------------


def test_direct_trim_commits_nothing_when_declined() -> None:
    cm = _manager()
    graph = _FakeGraph(_autonomous_loop(turns=7))
    result = cm._direct_trim_for_recovery_sync(graph, _CONFIG, "t")
    assert result is False
    assert graph.updates == []


def test_direct_trim_removes_prefix_up_to_human_head() -> None:
    cm = _manager()
    _stub_tokens(cm, target=10**9)  # under target -> boundaries[0] == 4
    msgs = _interactive_thread(turns=6)
    graph = _FakeGraph(msgs)
    result = cm._direct_trim_for_recovery_sync(graph, _CONFIG, "t")
    assert result is True
    assert len(graph.updates) == 1
    removed = graph.updates[0]["messages"]
    assert len(removed) == 4
    assert all(isinstance(cmd, RemoveMessage) for cmd in removed)
    # The retained head is a clean user turn.
    assert isinstance(msgs[4], HumanMessage)
