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
    llm_max_tokens: Optional[int] = None,
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
        llm_max_tokens=llm_max_tokens,
    )
    agent._token_tracker = TokenTracker()
    # ``get_llm_config_for_thread`` merges the thread's max_tokens over the
    # global LLM_MAX_TOKENS; the stub hands the trigger sites that merged
    # value the way the real config layer does.
    agent._get_llm_config_for_thread = lambda thread_id: SimpleNamespace(
        model="gpt-5.5", max_tokens=llm_max_tokens
    )
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


def test_compact_trigger_tokens_token_mode_clamps_below_the_window():
    """#115: an oversized absolute setting clamps to the USABLE window, not the
    window edge. Without a known output cap the reserve is 20% of the window,
    so the trigger lands where percentage mode's default would (80%) and a
    reply still fits."""
    assert (
        CompactionManager.compact_trigger_tokens(
            128_000, mode="tokens", tokens=500_000
        )
        == 102_400
    )
    # A setting exactly on the window edge is oversized too.
    assert (
        CompactionManager.compact_trigger_tokens(64_000, mode="tokens", tokens=64_000)
        == 51_200
    )


def test_compact_trigger_tokens_reserves_the_configured_output_cap():
    """With an output cap known, the ceiling is window minus cap: a prompt
    above that cannot be sent with that cap at all. It binds even when the
    setting is below the window."""
    assert (
        CompactionManager.compact_trigger_tokens(
            200_000, mode="tokens", tokens=500_000, output_reserve=32_000
        )
        == 168_000
    )
    assert (
        CompactionManager.compact_trigger_tokens(
            200_000, mode="tokens", tokens=190_000, output_reserve=32_000
        )
        == 168_000
    )
    # Below the ceiling the setting is taken as-is.
    assert (
        CompactionManager.compact_trigger_tokens(
            200_000, mode="tokens", tokens=100_000, output_reserve=32_000
        )
        == 100_000
    )


def test_compact_trigger_tokens_reserve_never_takes_more_than_half_the_window():
    """A cap sized near a small window (or an unusable reserve) must not turn
    every turn into a compaction: the usable window floors at 50%."""
    assert (
        CompactionManager.compact_trigger_tokens(
            200_000, mode="tokens", tokens=500_000, output_reserve=150_000
        )
        == 100_000
    )
    assert (
        CompactionManager.compact_trigger_tokens(
            200_000, mode="tokens", tokens=500_000, output_reserve=250_000
        )
        == 100_000
    )
    # A zero or negative reserve means "unknown": the default fraction applies.
    assert (
        CompactionManager.compact_trigger_tokens(
            200_000, mode="tokens", tokens=500_000, output_reserve=0
        )
        == 160_000
    )


def test_usable_context_ceiling_matches_the_trigger_clamp():
    from nymeria.config.model_capabilities import usable_context_ceiling

    assert usable_context_ceiling(128_000) == 102_400
    assert usable_context_ceiling(200_000, 32_000) == 168_000
    assert usable_context_ceiling(200_000, 150_000) == 100_000
    assert usable_context_ceiling(1) == 1


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

    clamp_warnings = [
        r.getMessage() for r in caplog.records if "clamped" in r.getMessage()
    ]
    assert len(clamp_warnings) == 2  # one per distinct pair, repeat silent
    assert "compact_threshold_tokens=500000" in clamp_warnings[0]
    assert "128000" in clamp_warnings[0]
    # The warning says what the trigger became, not just that it moved.
    assert "102400" in clamp_warnings[0]
    assert "no output cap being known" in clamp_warnings[0]
    assert "51200" in clamp_warnings[1]


def test_compact_trigger_clamp_warns_in_the_band_below_the_window(
    monkeypatch, caplog
):
    """A setting under the window but over the usable ceiling used to be
    silently honoured (and could overflow); it now clamps and says so."""
    import logging

    from nymeria.core import agent_compaction

    monkeypatch.setattr(agent_compaction, "_CLAMP_WARNED_PAIRS", set())
    with caplog.at_level(logging.WARNING, logger="nymeria.core.agent_compaction"):
        trigger = CompactionManager.compact_trigger_tokens(
            200_000, mode="tokens", tokens=190_000, output_reserve=32_000
        )

    assert trigger == 168_000
    messages = [r.getMessage() for r in caplog.records if "clamped" in r.getMessage()]
    assert len(messages) == 1
    assert "190000" in messages[0] and "168000" in messages[0]
    assert "32000-token output cap" in messages[0]  # names the reserve it subtracted


def test_compact_trigger_clamp_warning_is_honest_when_the_floor_binds(
    monkeypatch, caplog
):
    """With a 150k cap on a 200k window the arithmetic says 50k, the floor
    says 100k; the warning must name the 100k it actually reserved and say
    the cap was held at half the window, not print a subtraction that does
    not add up. The dedupe key includes the ceiling, so a second thread on
    the same model with a different cap warns again."""
    import logging

    from nymeria.core import agent_compaction

    monkeypatch.setattr(agent_compaction, "_CLAMP_WARNED_PAIRS", set())
    with caplog.at_level(logging.WARNING, logger="nymeria.core.agent_compaction"):
        CompactionManager.compact_trigger_tokens(
            200_000, mode="tokens", tokens=500_000, output_reserve=150_000
        )
        CompactionManager.compact_trigger_tokens(
            200_000, mode="tokens", tokens=500_000, output_reserve=32_000
        )

    messages = [r.getMessage() for r in caplog.records if "clamped" in r.getMessage()]
    assert len(messages) == 2
    assert "minus 100000 tokens" in messages[0]
    assert "150000-token output cap, held at half the window" in messages[0]
    assert "clamped to 100000" in messages[0]
    assert "clamped to 168000" in messages[1]


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

    assert not [r for r in caplog.records if "clamped" in r.getMessage()]


def test_auto_compact_token_mode_reserves_the_global_output_cap():
    """An oversized token setting with LLM_MAX_TOKENS set fires at window
    minus the cap (gpt-5.5 resolves to a 1,050,000 window)."""
    agent = _agent_with_compaction(mode="tokens", tokens=2_000_000, llm_max_tokens=50_000)

    agent._token_tracker.record_turn(
        "thread-a", turn_input_tokens=999_999, turn_output_tokens=10, context_tokens=999_999
    )
    assert agent._should_auto_compact_now("thread-a", "user-a") is False

    agent._token_tracker.record_turn(
        "thread-b", turn_input_tokens=1_000_000, turn_output_tokens=10, context_tokens=1_000_000
    )
    assert agent._should_auto_compact_now("thread-b", "user-a") is True


def test_auto_compact_token_mode_thread_max_tokens_overrides_the_reserve():
    """A per-thread max_tokens is that thread's output cap, so it is that
    thread's reserve; other threads keep the global one."""
    agent = _agent_with_compaction(mode="tokens", tokens=2_000_000, llm_max_tokens=50_000)
    agent._get_llm_config_for_thread = lambda thread_id: SimpleNamespace(
        model="gpt-5.5", max_tokens=100_000 if thread_id == "thread-t" else 50_000
    )

    agent._token_tracker.record_turn(
        "thread-t", turn_input_tokens=960_000, turn_output_tokens=10, context_tokens=960_000
    )
    assert agent._should_auto_compact_now("thread-t", "user-a") is True
    agent._token_tracker.record_turn(
        "thread-other", turn_input_tokens=960_000, turn_output_tokens=10, context_tokens=960_000
    )
    assert agent._should_auto_compact_now("thread-other", "user-a") is False


def test_auto_compact_reserves_the_discovered_output_ceiling_without_a_cap(monkeypatch):
    """No LLM_MAX_TOKENS anywhere: the provider factory still sends the
    model's discovered output ceiling, so the clamp reserves that, not the
    20% guess (Anthropic refuses input + max_tokens over the window)."""
    from nymeria.config import model_capabilities

    monkeypatch.setattr(model_capabilities, "get_max_output_tokens", lambda model: 64_000)
    agent = _agent_with_compaction(mode="tokens", tokens=2_000_000, llm_max_tokens=None)

    agent._token_tracker.record_turn(
        "thread-a", turn_input_tokens=985_999, turn_output_tokens=10, context_tokens=985_999
    )
    assert agent._should_auto_compact_now("thread-a", "user-a") is False
    agent._token_tracker.record_turn(
        "thread-b", turn_input_tokens=986_000, turn_output_tokens=10, context_tokens=986_000
    )
    assert agent._should_auto_compact_now("thread-b", "user-a") is True


def test_check_and_compact_sync_reserves_the_output_cap():
    """The sync path clamps with the same reserve as the async one."""
    agent = _agent_with_compaction(mode="tokens", tokens=2_000_000, llm_max_tokens=50_000)
    triggered: list[str] = []

    def fake_do_compact_sync(tid: str, _uid: str, **_kwargs: Any) -> dict[str, Any]:
        triggered.append(tid)
        return {"success": True, "thread_id": tid}

    agent._compaction._do_compact_sync = fake_do_compact_sync  # type: ignore[method-assign]
    agent._token_tracker.record_turn(
        "thread-under", turn_input_tokens=999_999, turn_output_tokens=10, context_tokens=999_999
    )
    assert agent._compaction.check_and_compact_sync("thread-under", "user-a") is None
    agent._token_tracker.record_turn(
        "thread-over", turn_input_tokens=1_000_000, turn_output_tokens=10, context_tokens=1_000_000
    )
    assert agent._compaction.check_and_compact_sync("thread-over", "user-a") == {
        "success": True,
        "thread_id": "thread-over",
    }
    assert triggered == ["thread-over"]


def test_hook_context_stats_reports_the_reserve_clamped_trigger():
    from nymeria.core.agent_compaction import hook_context_stats

    agent = _agent_with_compaction(mode="tokens", tokens=2_000_000, llm_max_tokens=50_000)
    agent._token_tracker.record_turn(
        "thread-a", turn_input_tokens=10, turn_output_tokens=10, context_tokens=10
    )

    stats = hook_context_stats(agent, "thread-a")

    assert stats["context_limit"] == 1_050_000
    assert stats["compact_trigger_tokens"] == 1_000_000


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
