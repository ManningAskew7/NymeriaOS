"""Regression tests for per-turn tool reload bookkeeping."""

from __future__ import annotations

from nymeria.core.agent import NymeriaAgent


def _bare_agent() -> NymeriaAgent:
    agent = object.__new__(NymeriaAgent)
    agent._pending_tool_reload = {}
    agent._turn_reload_count = {}
    return agent


def test_prepare_turn_discards_stale_pending_reload_for_same_thread():
    agent = _bare_agent()
    agent._pending_tool_reload = {
        "thread-a": {"new_tools": ["bash_execute"]},
        "thread-b": {"new_tools": ["calendar_list_events"]},
    }
    agent._turn_reload_count = {"thread-a": 2, "thread-b": 1}

    agent._prepare_tool_reload_state_for_turn("thread-a", "astream")

    assert "thread-a" not in agent._pending_tool_reload
    assert agent._pending_tool_reload == {
        "thread-b": {"new_tools": ["calendar_list_events"]}
    }
    assert agent._turn_reload_count["thread-a"] == 0
    assert agent._turn_reload_count["thread-b"] == 1


def test_sync_stream_cleanup_drops_unconsumed_pending_reload():
    agent = _bare_agent()
    agent._pending_tool_reload = {
        "thread-a": {"new_tools": ["bash_execute"]},
    }
    agent._turn_reload_count = {"thread-a": 1}

    agent._clear_tool_reload_state_after_stream("thread-a")

    assert agent._pending_tool_reload == {}
    assert "thread-a" not in agent._turn_reload_count
