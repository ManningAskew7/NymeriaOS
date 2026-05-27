"""Regression tests for context compaction triggers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, Optional

from langchain_core.messages import HumanMessage

from nymeria.core.agent import NymeriaAgent
from nymeria.core.agent_compaction import CompactionManager
from nymeria.core.token_tracker import TokenTracker


class _StubThreadConfigManager:
    """Minimal stub for ThreadConfigManager.get_config used by threshold resolution."""

    def __init__(self) -> None:
        self._configs: Dict[str, Any] = {}

    def set_llm_config(self, thread_id: str, llm_config: Any) -> None:
        self._configs[thread_id] = SimpleNamespace(llm_config=llm_config)

    def get_config(self, thread_id: str) -> Optional[Any]:
        return self._configs.get(thread_id)


def _agent_with_compaction(
    *,
    mode: str = "percentage",
    pct: float = 0.8,
    tokens: int = 100_000,
) -> NymeriaAgent:
    agent = object.__new__(NymeriaAgent)
    agent.settings = SimpleNamespace(
        context_management="auto_compact",
        compact_threshold=pct,
        compact_threshold_mode=mode,
        compact_threshold_tokens=tokens,
    )
    agent._token_tracker = TokenTracker()
    agent._get_llm_config_for_thread = lambda thread_id: SimpleNamespace(model="gpt-5.5")
    agent.thread_config_manager = _StubThreadConfigManager()
    agent._compaction = CompactionManager(agent)
    return agent


def test_auto_compact_uses_percentage_threshold_only_for_large_models():
    agent = _agent_with_compaction()

    assert agent._compact_trigger_tokens(1_050_000, 0.8) == 840_000

    agent._token_tracker.record_usage("thread-a", 134_000, 10)
    assert agent._should_auto_compact_now("thread-a", "user-a") is False

    agent._token_tracker.record_usage("thread-b", 840_000, 10)
    assert agent._should_auto_compact_now("thread-b", "user-a") is True


def test_compact_trigger_tokens_token_mode_returns_absolute_value():
    assert (
        CompactionManager.compact_trigger_tokens(
            1_050_000, mode="tokens", tokens=100_000
        )
        == 100_000
    )


def test_compact_trigger_tokens_token_mode_clamped_to_model_limit():
    """Oversized absolute token settings clamp to model_limit so compaction still fires."""
    assert (
        CompactionManager.compact_trigger_tokens(
            128_000, mode="tokens", tokens=500_000
        )
        == 128_000
    )


def test_auto_compact_token_mode_global_setting():
    agent = _agent_with_compaction(mode="tokens", tokens=100_000)

    agent._token_tracker.record_usage("thread-a", 95_000, 10)
    assert agent._should_auto_compact_now("thread-a", "user-a") is False

    agent._token_tracker.record_usage("thread-b", 105_000, 10)
    assert agent._should_auto_compact_now("thread-b", "user-a") is True


def test_auto_compact_per_thread_override_wins_over_global():
    """A thread setting compact_threshold_mode='tokens' compacts earlier than the global percentage default."""
    agent = _agent_with_compaction(mode="percentage", pct=0.8)

    agent.thread_config_manager.set_llm_config(
        "thread-override",
        SimpleNamespace(
            compact_threshold_mode="tokens",
            compact_threshold=None,
            compact_threshold_tokens=50_000,
        ),
    )

    agent._token_tracker.record_usage("thread-override", 60_000, 10)
    assert agent._should_auto_compact_now("thread-override", "user-a") is True

    agent._token_tracker.record_usage("thread-default", 60_000, 10)
    assert agent._should_auto_compact_now("thread-default", "user-a") is False


def test_auto_compact_thread_override_partial_falls_back_to_global_tokens():
    """Thread sets only mode='tokens'; missing tokens value should fall back to global without crashing."""
    agent = _agent_with_compaction(mode="percentage", pct=0.8, tokens=80_000)

    agent.thread_config_manager.set_llm_config(
        "thread-partial",
        SimpleNamespace(
            compact_threshold_mode="tokens",
            compact_threshold=None,
            compact_threshold_tokens=None,
        ),
    )

    agent._token_tracker.record_usage("thread-partial", 70_000, 10)
    assert agent._should_auto_compact_now("thread-partial", "user-a") is False

    agent._token_tracker.record_usage("thread-partial-2", 90_000, 10)
    agent.thread_config_manager.set_llm_config(
        "thread-partial-2",
        SimpleNamespace(
            compact_threshold_mode="tokens",
            compact_threshold=None,
            compact_threshold_tokens=None,
        ),
    )
    assert agent._should_auto_compact_now("thread-partial-2", "user-a") is True


def test_check_and_compact_sync_honors_token_mode_and_thread_override():
    """The sync compaction path must apply the same mode + per-thread resolution as the async path."""
    agent = _agent_with_compaction(mode="tokens", tokens=100_000)

    agent._token_tracker.record_usage("thread-under", 95_000, 10)
    agent._token_tracker.record_usage("thread-over", 105_000, 10)

    assert agent._compaction.check_and_compact_sync("thread-under", "user-a") is None

    triggered: list[str] = []
    def fake_do_compact_sync(tid: str, _uid: str, **_kwargs: Any) -> dict[str, Any]:
        triggered.append(tid)
        return {
            "success": True,
            "thread_id": tid,
        }

    agent._compaction._do_compact_sync = fake_do_compact_sync  # type: ignore[method-assign]
    result = agent._compaction.check_and_compact_sync("thread-over", "user-a")
    assert result == {"success": True, "thread_id": "thread-over"}
    assert triggered == ["thread-over"]

    agent.thread_config_manager.set_llm_config(
        "thread-override",
        SimpleNamespace(
            compact_threshold_mode="tokens",
            compact_threshold=None,
            compact_threshold_tokens=40_000,
        ),
    )
    agent._token_tracker.record_usage("thread-override", 50_000, 10)
    triggered.clear()
    result = agent._compaction.check_and_compact_sync("thread-override", "user-a")
    assert result == {"success": True, "thread_id": "thread-override"}
    assert triggered == ["thread-override"]


class _StubAsyncGraph:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages

    async def aget_state(self, config: dict[str, Any]) -> Any:
        return SimpleNamespace(values={"messages": self.messages})


def test_manual_compact_start_callback_waits_for_message_count_check():
    agent = SimpleNamespace(
        settings=SimpleNamespace(compact_keep_messages=3),
        _default_async_graph=_StubAsyncGraph([
            HumanMessage(content="one"),
            HumanMessage(content="two"),
        ]),
        _flush_memories_before_trim=lambda *_args: None,
    )
    manager = CompactionManager(agent)
    started: list[str] = []

    result = asyncio.run(
        manager.compact_now(
            "thread-a",
            "user-a",
            on_started=lambda: started.append("started"),
        )
    )

    assert result["success"] is False
    assert "Not enough messages" in result["reason"]
    assert started == []


def test_manual_compact_start_callback_runs_when_compaction_starts():
    agent = SimpleNamespace(
        settings=SimpleNamespace(compact_keep_messages=3),
        _default_async_graph=_StubAsyncGraph([
            HumanMessage(content="one"),
            HumanMessage(content="two"),
            HumanMessage(content="three"),
        ]),
        _flush_memories_before_trim=lambda *_args: None,
    )
    manager = CompactionManager(agent)
    started: list[str] = []

    async def fake_run_compact_turn_and_prune(thread_id, user_id, *, auto_resumed):
        return {
            "success": True,
            "messages_before": 3,
            "messages_after": 4,
            "messages_removed": 3,
            "auto_resumed": auto_resumed,
            "summary": "summary",
        }

    manager._run_compact_turn_and_prune = fake_run_compact_turn_and_prune  # type: ignore[method-assign]

    result = asyncio.run(
        manager.compact_now(
            "thread-a",
            "user-a",
            on_started=lambda: started.append("started"),
        )
    )

    assert result["success"] is True
    assert started == ["started"]
