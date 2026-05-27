"""Tests for the sub-turn-boundary auto-compaction trigger (Phase 2b)."""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage

from nymeria.core import agent_compaction as ac
from nymeria.core.agent import NymeriaAgent
from nymeria.core.agent_compaction import CompactionManager
from nymeria.vendor.react_agent import nodes as vendor_nodes


def _ai_with_input_tokens(n: int) -> AIMessage:
    return AIMessage(
        content="x",
        usage_metadata={"input_tokens": n, "output_tokens": 5, "total_tokens": n + 5},
    )


def _manager(monkeypatch, *, mode_pct=0.8, limit=100_000, context_management="auto_compact"):
    agent = SimpleNamespace(
        settings=SimpleNamespace(context_management=context_management),
        _get_llm_config_for_thread=lambda tid: SimpleNamespace(model="x"),
    )
    mgr = object.__new__(CompactionManager)
    mgr._agent = agent
    monkeypatch.setattr(ac, "get_context_limit", lambda m: limit)
    monkeypatch.setattr(
        mgr, "_resolve_threshold_config", lambda tid: ("percentage", mode_pct, 100_000)
    )
    return mgr


# --- CompactionManager.should_subturn_compact ------------------------------

def test_should_subturn_compact_true_over_threshold(monkeypatch):
    mgr = _manager(monkeypatch)  # trigger = 0.8 * 100k = 80k
    msgs = [_ai_with_input_tokens(90_000), ToolMessage(content="r", tool_call_id="a")]
    assert mgr.should_subturn_compact("t1", msgs) is True


def test_should_subturn_compact_false_under_threshold(monkeypatch):
    mgr = _manager(monkeypatch)
    msgs = [_ai_with_input_tokens(50_000), ToolMessage(content="r", tool_call_id="a")]
    assert mgr.should_subturn_compact("t1", msgs) is False


def test_should_subturn_compact_false_when_not_auto_compact(monkeypatch):
    mgr = _manager(monkeypatch, context_management="none")
    msgs = [_ai_with_input_tokens(90_000)]
    assert mgr.should_subturn_compact("t1", msgs) is False


def test_should_subturn_compact_false_without_usage(monkeypatch):
    mgr = _manager(monkeypatch)
    # No AIMessage carrying usage metadata -> cannot decide -> False.
    assert mgr.should_subturn_compact("t1", [ToolMessage(content="r", tool_call_id="a")]) is False


# --- NymeriaAgent.should_halt_for_subturn_compaction -----------------------

def _agent_stub(*, decision: bool, compactions=0, context_management="auto_compact", compacting=()):
    return SimpleNamespace(
        settings=SimpleNamespace(context_management=context_management),
        MAX_COMPACTIONS_PER_TURN=NymeriaAgent.MAX_COMPACTIONS_PER_TURN,
        _compactions_this_turn={"t1": compactions} if compactions else {},
        _subturn_compact_requested=set(),
        _compacting_threads=set(compacting),
        _compaction=SimpleNamespace(should_subturn_compact=lambda tid, msgs: decision),
    )


def test_should_halt_flags_when_over_threshold():
    me = _agent_stub(decision=True)
    halted = NymeriaAgent.should_halt_for_subturn_compaction(me, "t1", [])
    assert halted is True
    assert "t1" in me._subturn_compact_requested


def test_should_halt_false_when_under_threshold():
    me = _agent_stub(decision=False)
    halted = NymeriaAgent.should_halt_for_subturn_compaction(me, "t1", [])
    assert halted is False
    assert "t1" not in me._subturn_compact_requested


def test_should_halt_respects_per_turn_cap():
    me = _agent_stub(decision=True, compactions=NymeriaAgent.MAX_COMPACTIONS_PER_TURN)
    halted = NymeriaAgent.should_halt_for_subturn_compaction(me, "t1", [])
    assert halted is False  # cap reached -> let the continuation finish
    assert "t1" not in me._subturn_compact_requested


def test_should_halt_false_when_not_auto_compact():
    me = _agent_stub(decision=True, context_management="none")
    assert NymeriaAgent.should_halt_for_subturn_compaction(me, "t1", []) is False


def test_should_halt_false_during_compaction_turn():
    # The compaction summary turn runs on oversized context and calls tools;
    # it must be exempt or compaction would re-enter compaction (and never
    # produce a summary).
    me = _agent_stub(decision=True, compacting=("t1",))
    assert NymeriaAgent.should_halt_for_subturn_compaction(me, "t1", []) is False
    assert "t1" not in me._subturn_compact_requested


# --- route_after_tools integration -----------------------------------------

def test_route_after_tools_halts_on_compaction_flag(monkeypatch):
    flagged = {}

    def fake_should_halt(thread_id, messages):
        flagged["called"] = (thread_id, len(messages))
        return True

    fake_agent = SimpleNamespace(should_halt_for_subturn_compaction=fake_should_halt)
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: fake_agent)

    state = {"messages": [
        AIMessage(content="", tool_calls=[
            {"id": "a", "name": "x", "args": {}, "type": "tool_call"},
        ]),
        ToolMessage(content="r", tool_call_id="a", name="x"),
    ]}
    config = {"configurable": {"thread_id": "t1"}}

    assert vendor_nodes.route_after_tools(state, config) == "end"
    assert flagged["called"] == ("t1", 2)


def test_route_after_tools_continues_when_not_flagged(monkeypatch):
    fake_agent = SimpleNamespace(should_halt_for_subturn_compaction=lambda *a: False)
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: fake_agent)

    state = {"messages": [
        AIMessage(content="", tool_calls=[
            {"id": "a", "name": "x", "args": {}, "type": "tool_call"},
        ]),
        ToolMessage(content="r", tool_call_id="a", name="x"),
    ]}
    config = {"configurable": {"thread_id": "t1"}}

    assert vendor_nodes.route_after_tools(state, config) == "agent"
