"""Regression tests for context compaction triggers."""

from __future__ import annotations

from types import SimpleNamespace

from nymeria.core.agent import NymeriaAgent
from nymeria.core.agent_compaction import CompactionManager
from nymeria.core.token_tracker import TokenTracker


def _agent_with_compaction() -> NymeriaAgent:
    agent = object.__new__(NymeriaAgent)
    agent.settings = SimpleNamespace(
        context_management="auto_compact",
        compact_threshold=0.8,
    )
    agent._token_tracker = TokenTracker()
    agent._get_llm_config_for_thread = lambda thread_id: SimpleNamespace(model="gpt-5.5")
    agent._compaction = CompactionManager(agent)
    return agent


def test_auto_compact_uses_percentage_threshold_only_for_large_models():
    agent = _agent_with_compaction()

    assert agent._compact_trigger_tokens(1_050_000, 0.8) == 840_000

    agent._token_tracker.record_usage("thread-a", 134_000, 10)
    assert agent._should_auto_compact_now("thread-a", "user-a") is False

    agent._token_tracker.record_usage("thread-b", 840_000, 10)
    assert agent._should_auto_compact_now("thread-b", "user-a") is True
