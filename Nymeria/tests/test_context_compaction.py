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
) -> Any:
    # A deliberately partial NymeriaAgent: real instance (so the real
    # threshold methods run) with only the attributes those methods touch
    # stubbed in. Typed Any because the stubs do not match the declared
    # attribute types.
    agent: Any = object.__new__(NymeriaAgent)
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

    assert agent._compact_trigger_tokens(1_050_000, 0.8, mode="percentage") == 840_000

    agent._token_tracker.record_turn("thread-a", turn_input_tokens=134_000, turn_output_tokens=10, context_tokens=134_000)
    assert agent._should_auto_compact_now("thread-a", "user-a") is False

    agent._token_tracker.record_turn("thread-b", turn_input_tokens=840_000, turn_output_tokens=10, context_tokens=840_000)
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


def test_compact_trigger_clamp_warns_once_per_pair(monkeypatch, caplog):
    """#115 interim visibility: a setting at or above the window warns the
    operator (the clamped trigger leaves no room for the response), once per
    distinct (setting, window) pair, not per turn."""
    import logging

    from nymeria.core import agent_compaction

    monkeypatch.setattr(agent_compaction, "_CLAMP_WARNED_PAIRS", set())
    with caplog.at_level(logging.WARNING, logger="nymeria.core.agent_compaction"):
        CompactionManager.compact_trigger_tokens(128_000, mode="tokens", tokens=500_000)
        CompactionManager.compact_trigger_tokens(128_000, mode="tokens", tokens=500_000)
        CompactionManager.compact_trigger_tokens(64_000, mode="tokens", tokens=64_000)

    overflow_warnings = [
        r.getMessage() for r in caplog.records if "OVERFLOW" in r.getMessage()
    ]
    assert len(overflow_warnings) == 2  # one per distinct pair, repeat silent
    assert "compact_threshold_tokens=500000" in overflow_warnings[0]
    assert "128000" in overflow_warnings[0]
    assert "#115" in overflow_warnings[0]


def test_compact_trigger_no_clamp_warning_below_window_or_percentage(
    monkeypatch, caplog
):
    import logging

    from nymeria.core import agent_compaction

    monkeypatch.setattr(agent_compaction, "_CLAMP_WARNED_PAIRS", set())
    with caplog.at_level(logging.WARNING, logger="nymeria.core.agent_compaction"):
        CompactionManager.compact_trigger_tokens(
            1_050_000, mode="tokens", tokens=100_000
        )
        CompactionManager.compact_trigger_tokens(128_000, 0.95, mode="percentage")

    assert not [r for r in caplog.records if "OVERFLOW" in r.getMessage()]


def test_auto_compact_token_mode_global_setting():
    agent = _agent_with_compaction(mode="tokens", tokens=100_000)

    agent._token_tracker.record_turn("thread-a", turn_input_tokens=95_000, turn_output_tokens=10, context_tokens=95_000)
    assert agent._should_auto_compact_now("thread-a", "user-a") is False

    agent._token_tracker.record_turn("thread-b", turn_input_tokens=105_000, turn_output_tokens=10, context_tokens=105_000)
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

    agent._token_tracker.record_turn("thread-override", turn_input_tokens=60_000, turn_output_tokens=10, context_tokens=60_000)
    assert agent._should_auto_compact_now("thread-override", "user-a") is True

    agent._token_tracker.record_turn("thread-default", turn_input_tokens=60_000, turn_output_tokens=10, context_tokens=60_000)
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

    agent._token_tracker.record_turn("thread-partial", turn_input_tokens=70_000, turn_output_tokens=10, context_tokens=70_000)
    assert agent._should_auto_compact_now("thread-partial", "user-a") is False

    agent._token_tracker.record_turn("thread-partial-2", turn_input_tokens=90_000, turn_output_tokens=10, context_tokens=90_000)
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

    agent._token_tracker.record_turn("thread-under", turn_input_tokens=95_000, turn_output_tokens=10, context_tokens=95_000)
    agent._token_tracker.record_turn("thread-over", turn_input_tokens=105_000, turn_output_tokens=10, context_tokens=105_000)

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
    agent._token_tracker.record_turn("thread-override", turn_input_tokens=50_000, turn_output_tokens=10, context_tokens=50_000)
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
    manager = CompactionManager(agent)  # type: ignore[bad-argument-type]
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


def test_manual_compact_marks_a_short_thread_as_declined_not_failed():
    """The manual path's benign no-op must carry the ``declined`` flag.

    The command renderers split on that flag: declined is an informational
    "Skipped", everything else is reported as a compaction FAILURE. ``compact_now``
    is the function both ``/compact`` and ``POST /threads/{id}/compact`` enter, so
    an unflagged short thread (the commonest benign outcome there is) reads to the
    user as though compaction had broken.

    This asserts the PRODUCER rather than the renderer on purpose. The renderer
    tests in ``test_command_service.py`` hand the fake API a result dict, so they
    pass whichever sites do or do not set the flag, and they did: the flag first
    landed on the overflow-recovery path, which no renderer reads, while this one
    went unmarked.
    """
    agent = SimpleNamespace(
        settings=SimpleNamespace(compact_keep_messages=3),
        _default_async_graph=_StubAsyncGraph([HumanMessage(content="one")]),
        _flush_memories_before_trim=lambda *_args: None,
    )
    manager = CompactionManager(agent)  # type: ignore[bad-argument-type]

    result = asyncio.run(manager.compact_now("thread-a", "user-a"))

    assert result["success"] is False
    assert result["declined"] is True


def test_compaction_failure_is_not_marked_declined():
    """The other half of the contract, so the flag cannot become a constant.

    A summary that could not be generated is a failure, and the renderers must be
    able to tell it apart from a thread that simply had nothing to compact.
    """
    agent = SimpleNamespace(
        settings=SimpleNamespace(compact_keep_messages=2),
        _default_async_graph=_StubAsyncGraph([
            HumanMessage(content="one"),
            HumanMessage(content="two"),
            HumanMessage(content="three"),
        ]),
        _flush_memories_before_trim=lambda *_args: None,
    )
    manager = CompactionManager(agent)  # type: ignore[bad-argument-type]

    async def failing_run(thread_id, user_id, *, auto_resumed, priority=None):
        return {"success": False, "reason": "Failed to generate summary"}

    manager._run_compact_turn_and_prune = failing_run  # type: ignore[method-assign]

    result = asyncio.run(manager.compact_now("thread-a", "user-a"))

    assert result["success"] is False
    assert result.get("declined") is not True


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
    manager = CompactionManager(agent)  # type: ignore[bad-argument-type]
    started: list[str] = []

    async def fake_run_compact_turn_and_prune(thread_id, user_id, *, auto_resumed, priority=None):
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


# ── Summary failures name their cause (#258) ───────────────────────────────────


class _SummaryGraph:
    """An agent graph whose summary invoke times out, raises, or answers empty."""

    def __init__(self, behavior: str) -> None:
        self.behavior = behavior

    async def ainvoke(self, _input: Any, config: dict[str, Any]) -> dict[str, Any]:
        if self.behavior == "timeout":
            await asyncio.sleep(5)
        if self.behavior == "error":
            raise RuntimeError("provider returned 400: image too large")
        return {"messages": [HumanMessage(content="prompt only, no AIMessage")]}


def _summary_agent(graph: _SummaryGraph) -> Any:
    return SimpleNamespace(
        _get_async_graph_for_user=lambda user_id, thread_id=None: graph,
        _compacting_threads=set(),
    )


def test_generate_summary_distinguishes_timeout_error_and_empty(monkeypatch):
    """One "Failed to generate summary" covered a timeout, any exception and an
    empty summarizer answer alike, so a caller could not tell a transient from a
    dead thread. Each cause now carries its own code and sentence."""
    from nymeria.core import agent_compaction

    monkeypatch.setattr(agent_compaction, "COMPACTION_TIMEOUT_SECONDS", 0.05)
    agents = {b: _summary_agent(_SummaryGraph(b)) for b in ("timeout", "error", "empty")}
    outcomes = {
        behavior: asyncio.run(
            CompactionManager(agent)  # type: ignore[bad-argument-type]
            ._generate_summary("thread-a", "user-a")
        )
        for behavior, agent in agents.items()
    }
    for outcome in outcomes.values():
        assert outcome.summary is None

    assert outcomes["timeout"].failure == "timeout"
    assert "timed out after 0.05 seconds" in (outcomes["timeout"].reason or "")
    assert outcomes["error"].failure == "error"
    assert "RuntimeError: provider returned 400: image too large" in (outcomes["error"].reason or "")
    assert outcomes["empty"].failure == "empty"
    assert outcomes["empty"].reason == "The summarizer returned no summary"

    results = {name: outcome.failure_result() for name, outcome in outcomes.items()}
    assert all(result["success"] is False and "declined" not in result for result in results.values())
    assert {result["failure"] for result in results.values()} == {"timeout", "error", "empty"}
    assert {result["reason"] for result in results.values()} == {
        outcome.reason for outcome in outcomes.values()
    }
    # And the thread is released from the compacting set on every path.
    assert all(agent._compacting_threads == set() for agent in agents.values())


def test_run_compact_turn_reports_the_summary_failure_it_got():
    """The compaction result carries the cause through to the renderers, and the
    generic sentence is gone from the producer."""
    from nymeria.core.agent_compaction import SummaryOutcome

    pre = [HumanMessage(content="one", id="m1"), HumanMessage(content="two", id="m2")]

    class _Graph(_StubAsyncGraph):
        async def aupdate_state(self, config, update):
            pass

    manager = CompactionManager(  # type: ignore[bad-argument-type]
        SimpleNamespace(_default_async_graph=_Graph(pre))
    )

    async def timed_out(thread_id, user_id, priority=None):
        return SummaryOutcome(failure="timeout", reason="Summary generation timed out after 900 seconds")

    manager._generate_summary = timed_out  # type: ignore[method-assign]
    result = asyncio.run(manager._run_compact_turn_and_prune("t1", "u1", auto_resumed=False))
    assert result == {
        "success": False,
        "failure": "timeout",
        "reason": "Summary generation timed out after 900 seconds",
    }


def test_sync_summary_path_reports_the_same_failure_codes(monkeypatch):
    """The sync sibling (auto-compaction, overflow recovery) shares the outcome
    contract: timeout / error / empty, mapped into the same result keys."""
    from langchain_core.messages import AIMessage

    from nymeria.core import agent_compaction

    monkeypatch.setattr(agent_compaction, "COMPACTION_TIMEOUT_SECONDS", 0.05)

    class _SyncGraph:
        def __init__(self, behavior: str) -> None:
            self.behavior = behavior

        def invoke(self, _input, config):
            if self.behavior == "timeout":
                import time

                time.sleep(0.5)
            if self.behavior == "error":
                raise ValueError("boom")
            if self.behavior == "ok":
                return {"messages": [AIMessage(content="## Active Goal\nsummary text")]}
            return {"messages": []}

    def manager_for(behavior: str) -> CompactionManager:
        agent = SimpleNamespace(
            _get_graph_for_user=lambda user_id, thread_id=None: _SyncGraph(behavior),
            _compacting_threads=set(),
        )
        return CompactionManager(agent)  # type: ignore[bad-argument-type]

    assert manager_for("timeout")._generate_summary_sync("t", "u").failure == "timeout"
    error = manager_for("error")._generate_summary_sync("t", "u")
    assert error.failure == "error" and "ValueError: boom" in (error.reason or "")
    assert manager_for("empty")._generate_summary_sync("t", "u").failure == "empty"
    ok = manager_for("ok")._generate_summary_sync("t", "u")
    assert ok.failure is None and ok.summary and "summary text" in ok.summary
