"""Regression tests for context compaction triggers."""

from __future__ import annotations

from types import SimpleNamespace

from nymeria.core.agent import NymeriaAgent
from nymeria.core.token_tracker import TokenTracker


def _agent_with_compaction(*, soft_limit: int = 120000) -> NymeriaAgent:
    agent = object.__new__(NymeriaAgent)
    agent.settings = SimpleNamespace(
        context_management="auto_compact",
        compact_threshold=0.8,
        compact_soft_token_limit=soft_limit,
    )
    agent._token_tracker = TokenTracker()
    agent._get_llm_config_for_thread = lambda thread_id: SimpleNamespace(model="gpt-5.5")
    return agent


def test_auto_compact_uses_absolute_soft_token_limit_for_large_models():
    agent = _agent_with_compaction()

    assert agent._compact_trigger_tokens(1_050_000, 0.8) == 120_000

    agent._token_tracker.record_usage("thread-a", 119_999, 10)
    assert agent._should_auto_compact_now("thread-a", "user-a") is False

    agent._token_tracker.record_usage("thread-b", 120_000, 10)
    assert agent._should_auto_compact_now("thread-b", "user-a") is True


def test_auto_compact_can_disable_soft_limit():
    agent = _agent_with_compaction(soft_limit=0)

    assert agent._compact_trigger_tokens(1_050_000, 0.8) == 840_000

    agent._token_tracker.record_usage("thread-a", 130_000, 10)
    assert agent._should_auto_compact_now("thread-a", "user-a") is False
