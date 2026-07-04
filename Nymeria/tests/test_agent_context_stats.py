"""Unit tests for the context-stats cluster extracted from NymeriaAgent."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from nymeria.core.agent_compaction import CompactionManager
from nymeria.core.agent_context_stats import (
    get_context_stats,
    record_turn_usage,
    rehydrate_token_usage,
)


def _fake_usage(
    *,
    total_input: int = 0,
    total_output: int = 0,
    last_input: int = 0,
    turn_input: int = 0,
    turn_output: int = 0,
    turn_recorded: bool = False,
    turn_llm_seconds: float | None = None,
    compaction_count: int = 0,
    last_compaction_at: datetime | None = None,
    last_cost_usd: float | None = None,
    total_cost_usd: float = 0.0,
    cost_unavailable: bool = False,
    last_recorded_message_index: int = 0,
    context_model: str | None = None,
) -> Any:
    """SimpleNamespace stand-in for ThreadTokenUsage.

    SimpleNamespace lets us treat ``context_tokens`` and ``total_tokens``
    (which are computed properties on the real class) as plain settable
    attributes, which makes branch testing trivial.
    """
    return SimpleNamespace(
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        last_input_tokens=last_input,
        turn_input_tokens=turn_input,
        turn_output_tokens=turn_output,
        turn_recorded=turn_recorded,
        turn_llm_seconds=turn_llm_seconds,
        compaction_count=compaction_count,
        last_compaction_at=last_compaction_at,
        context_tokens=last_input,  # mirrors the real property
        total_tokens=total_input + total_output,  # mirrors the real property
        last_cost_usd=last_cost_usd,
        total_cost_usd=total_cost_usd,
        cost_unavailable=cost_unavailable,
        last_recorded_message_index=last_recorded_message_index,
        context_model=context_model,
    )


def _fake_agent(
    *,
    messages: list | None = None,
    graph_raises: BaseException | None = None,
    usage: Any | None = None,
    rehydrate: Any | None = None,
    model: str = "gpt-4o",
    context_management: str = "auto_compact",
    threshold_config: tuple[str, float, int] = ("tokens", 0.8, 200_000),
) -> Any:
    """Build a minimal agent stub for the context-stats functions."""
    state = SimpleNamespace(values={"messages": messages or []})

    def _get_state(_config):
        if graph_raises is not None:
            raise graph_raises
        return state

    default_graph = SimpleNamespace(get_state=_get_state)

    seed_calls: list[tuple] = []

    token_tracker = SimpleNamespace(
        get_usage=lambda _tid: usage if usage is not None else _fake_usage(),
        seed_rehydrated=lambda tid, **kw: seed_calls.append((tid, kw)),
    )

    llm_config = SimpleNamespace(model=model)
    settings = SimpleNamespace(context_management=context_management)
    compaction = SimpleNamespace(
        _resolve_threshold_config=lambda _tid: threshold_config,
    )

    agent = SimpleNamespace(
        _default_graph=default_graph,
        _token_tracker=token_tracker,
        _rehydrate_token_usage=rehydrate if rehydrate is not None else MagicMock(),
        _get_llm_config_for_thread=lambda _tid: llm_config,
        _compaction=compaction,
        # Real trigger math (the agent facade is a passthrough to this static).
        _compact_trigger_tokens=CompactionManager.compact_trigger_tokens,
        settings=settings,
    )
    agent._seed_calls = seed_calls  # type: ignore[attr-defined]
    return agent


# ----- rehydrate_token_usage --------------------------------------------------


def test_rehydrate_skips_when_no_ai_messages():
    agent = _fake_agent(messages=[HumanMessage(content="hi")])
    rehydrate_token_usage(cast(Any, agent), "t1")
    assert agent._seed_calls == []


def test_rehydrate_seeds_cumulative_occupancy_and_index(monkeypatch: pytest.MonkeyPatch):
    # Two AIMessages -> extract_from_message called twice -> cumulative (20, 40);
    # extract_last_from_messages returns the latest call's (10, 20), whose
    # input side becomes the occupancy estimate.
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "extract_from_message", lambda _msg: (10, 20))
    monkeypatch.setattr(acs, "extract_last_from_messages", lambda _msgs: (10, 20))

    agent = _fake_agent(
        messages=[AIMessage(content="a"), AIMessage(content="b")],
    )

    rehydrate_token_usage(cast(Any, agent), "t1")

    assert agent._seed_calls == [
        (
            "t1",
            {
                "total_input_tokens": 20,
                "total_output_tokens": 40,
                "context_tokens": 10,
                "message_index": 2,
                "context_model": "gpt-4o",
            },
        )
    ]


def test_rehydrate_silently_swallows_graph_exceptions():
    agent = _fake_agent(graph_raises=RuntimeError("graph blew up"))

    # Must not raise.
    rehydrate_token_usage(cast(Any, agent), "t1")

    assert agent._seed_calls == []


# ----- record_turn_usage ------------------------------------------------------


def _fake_record_agent(
    *,
    context_tokens=(0, 0),
    turn=(0, 0, None, False),
) -> Any:
    """Minimal agent stub exposing only the facades record_turn_usage calls.

    ``turn`` is the 4-tuple returned by ``_compute_turn_usage_and_cost``:
    (turn_input, turn_output, cost_usd, cost_unavailable).
    """
    record_turn_calls: list[tuple] = []
    record_cost_calls: list[tuple] = []

    token_tracker = SimpleNamespace(
        record_turn=lambda tid, **kw: record_turn_calls.append((tid, kw)),
    )
    agent = SimpleNamespace(
        _extract_tokens_from_response=lambda _msgs: context_tokens,
        _get_llm_config_for_thread=lambda _tid: SimpleNamespace(model="gpt-4o"),
        _compute_turn_usage_and_cost=lambda _tid, _msgs, _cfg: turn,
        _token_tracker=token_tracker,
        _record_turn_cost=lambda tid, uid, c, cu: record_cost_calls.append((tid, uid, c, cu)),
    )
    agent._record_turn_calls = record_turn_calls  # type: ignore[attr-defined]
    agent._record_cost_calls = record_cost_calls  # type: ignore[attr-defined]
    return agent


def test_record_turn_usage_records_summed_turn_and_occupancy():
    agent = _fake_record_agent(context_tokens=(90, 45), turn=(120, 45, 0.0021, False))

    result = record_turn_usage(cast(Any, agent), "t1", "u1", ["msg"])

    assert result == (120, 45, True)
    assert agent._record_turn_calls == [
        (
            "t1",
            {
                "turn_input_tokens": 120,
                "turn_output_tokens": 45,
                "context_tokens": 90,
                "cost_usd": 0.0021,
                "cost_unavailable": False,
                "turn_llm_seconds": None,
                "context_model": "gpt-4o",
            },
        )
    ]
    assert agent._record_cost_calls == [("t1", "u1", 0.0021, False)]


def test_record_turn_usage_empty_extraction_still_updates_tracker():
    """An empty turn records turn_recorded=False (via zero sums) and keeps
    the previous occupancy (context_tokens=None), rather than skipping the
    tracker entirely and leaving stale values look current."""
    agent = _fake_record_agent(context_tokens=(0, 0), turn=(0, 0, None, False))

    result = record_turn_usage(cast(Any, agent), "t1", "u1", ["msg"])

    assert result == (0, 0, False)
    assert agent._record_turn_calls == [
        (
            "t1",
            {
                "turn_input_tokens": 0,
                "turn_output_tokens": 0,
                "context_tokens": None,
                "cost_usd": None,
                "cost_unavailable": False,
                "turn_llm_seconds": None,
                "context_model": "gpt-4o",
            },
        )
    ]
    # _record_turn_cost self-guards on None cost; the call is still made.
    assert agent._record_cost_calls == [("t1", "u1", None, False)]


def test_record_turn_usage_cost_unavailable_does_not_force_token_zeros():
    """Defect #1: cost_unavailable used to force a (0,0) record that zeroed
    the context bar on subscription/local threads."""
    agent = _fake_record_agent(context_tokens=(0, 0), turn=(0, 0, None, True))

    result = record_turn_usage(cast(Any, agent), "t1", "u1", ["msg"])

    assert result == (0, 0, False)
    tid, kwargs = agent._record_turn_calls[0]
    assert kwargs["context_tokens"] is None  # occupancy untouched
    assert kwargs["cost_unavailable"] is True


def test_record_turn_usage_subscription_thread_keeps_turn_sums():
    """Subscription threads now get per-turn sums too (the slice runs for
    every provider class)."""
    agent = _fake_record_agent(context_tokens=(200, 80), turn=(350, 120, None, True))

    result = record_turn_usage(cast(Any, agent), "t1", "u1", ["msg"])

    assert result == (350, 120, True)
    tid, kwargs = agent._record_turn_calls[0]
    assert kwargs["turn_input_tokens"] == 350
    assert kwargs["turn_output_tokens"] == 120
    assert kwargs["context_tokens"] == 200
    assert kwargs["cost_unavailable"] is True


# ----- _rehydrate_cost_from_metadata (F10) ------------------------------------


def _fake_cost_agent(*, owner, thread_meta: dict, glob_files: list) -> Any:
    """Stub agent for _rehydrate_cost_from_metadata.

    thread_meta maps user_id -> total_cost_usd_micros for the target thread.
    glob_files lists the user_ids whose metadata files exist. A counter records
    whether the glob fallback scan ran (so the fast path can be asserted).
    """
    usage = SimpleNamespace(total_cost_usd=0.0)
    usage_store: dict = {}
    token_tracker = SimpleNamespace(get_usage=lambda _tid: usage, _usage=usage_store)
    calls = {"glob": 0}

    def _get_thread(user_id, _tid):
        micros = thread_meta.get(user_id)
        return SimpleNamespace(total_cost_usd_micros=micros) if micros is not None else None

    class _MetaDir:
        def glob(self, _pattern):
            calls["glob"] += 1
            return [SimpleNamespace(stem=u) for u in glob_files]

    manager = SimpleNamespace(metadata_dir=_MetaDir(), get_thread=_get_thread)
    accounts_repo = SimpleNamespace(get_thread_owner=lambda _tid: owner)

    agent = SimpleNamespace(
        thread_metadata_manager=manager,
        accounts_repo=accounts_repo,
        _token_tracker=token_tracker,
    )
    agent._usage_obj = usage  # type: ignore[attr-defined]
    agent._glob_calls = calls  # type: ignore[attr-defined]
    return agent


def test_rehydrate_cost_fast_path_reads_owner_only():
    from nymeria.core.agent_context_stats import _rehydrate_cost_from_metadata

    agent = _fake_cost_agent(
        owner="alice", thread_meta={"alice": 2_000_000}, glob_files=["alice", "bob"]
    )

    _rehydrate_cost_from_metadata(cast(Any, agent), "t1")

    assert agent._usage_obj.total_cost_usd == 2.0
    assert agent._glob_calls["glob"] == 0  # owner hit -> no full scan
    assert agent._token_tracker._usage.get("t1") is agent._usage_obj


def test_rehydrate_cost_falls_back_to_glob_when_owner_unknown():
    from nymeria.core.agent_context_stats import _rehydrate_cost_from_metadata

    # No registered owner; the cost lives under bob's metadata file.
    agent = _fake_cost_agent(
        owner=None, thread_meta={"bob": 3_500_000}, glob_files=["alice", "bob"]
    )

    _rehydrate_cost_from_metadata(cast(Any, agent), "t1")

    assert agent._glob_calls["glob"] == 1
    assert agent._usage_obj.total_cost_usd == 3.5


def test_rehydrate_cost_falls_back_when_owner_has_no_metadata_row():
    from nymeria.core.agent_context_stats import _rehydrate_cost_from_metadata

    # alice is the registered owner but the cost was persisted under bob
    # (e.g. a callable/shared thread) -> the owner read misses, glob finds it.
    agent = _fake_cost_agent(
        owner="alice", thread_meta={"bob": 1_000_000}, glob_files=["alice", "bob"]
    )

    _rehydrate_cost_from_metadata(cast(Any, agent), "t1")

    assert agent._glob_calls["glob"] == 1
    assert agent._usage_obj.total_cost_usd == 1.0


def test_rehydrate_cost_zero_owner_cost_falls_back_and_seeds_nothing():
    from nymeria.core.agent_context_stats import _rehydrate_cost_from_metadata

    # Owner row exists but is $0 -> fall back to the scan (which reproduces the
    # original first-row-wins short-circuit), seeding nothing.
    agent = _fake_cost_agent(
        owner="alice", thread_meta={"alice": 0}, glob_files=["alice"]
    )

    _rehydrate_cost_from_metadata(cast(Any, agent), "t1")

    assert agent._glob_calls["glob"] == 1
    assert agent._usage_obj.total_cost_usd == 0.0


# ----- get_context_stats ------------------------------------------------------


def test_get_stats_returns_dict_with_expected_keys(monkeypatch: pytest.MonkeyPatch):
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    compaction_at = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
    usage = _fake_usage(
        total_input=3_000,
        total_output=2_000,
        last_input=1_500,
        turn_input=700,
        turn_output=500,
        turn_recorded=True,
        turn_llm_seconds=7.125,
        compaction_count=2,
        last_compaction_at=compaction_at,
    )
    rehydrate_mock = MagicMock()
    agent = _fake_agent(usage=usage, rehydrate=rehydrate_mock, model="gpt-4o")

    stats = get_context_stats(cast(Any, agent), "thread-x")

    rehydrate_mock.assert_not_called()  # non-empty usage -> no rehydrate
    assert stats == {
        "thread_id": "thread-x",
        "model": "gpt-4o",
        "total_tokens": 1_500,
        "input_tokens": 700,
        "output_tokens": 500,
        "turn_recorded": True,
        "turn_llm_seconds": 7.125,
        "tokens_per_second": 70.2,
        "cumulative_tokens": 5_000,
        "context_limit": 100_000,
        "usage_percentage": 1.5,
        "compaction_count": 2,
        # tokens-mode 200k trigger clamped to the 100k model limit.
        "compact_trigger_tokens": 100_000,
        "last_compaction": compaction_at.isoformat(),
        "context_management": "auto_compact",
        "cost_usd_last": None,
        "cost_usd_cumulative": 0.0,
        "cost_unavailable": False,
    }


def test_get_stats_empty_triggers_rehydrate_via_facade(monkeypatch: pytest.MonkeyPatch):
    """Critical: get_context_stats must call agent._rehydrate_token_usage(...)
    (the facade), NOT the new module's free function directly. Tests that
    patch NymeriaAgent._rehydrate_token_usage rely on this.
    """
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    empty = _fake_usage()  # context_tokens=0, total_tokens=0
    rehydrate_mock = MagicMock()
    agent = _fake_agent(usage=empty, rehydrate=rehydrate_mock)

    get_context_stats(cast(Any, agent), "thread-empty")

    rehydrate_mock.assert_called_once_with("thread-empty")


def test_get_stats_non_empty_does_not_rehydrate(monkeypatch: pytest.MonkeyPatch):
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    usage = _fake_usage(last_input=42)  # context_tokens=42 (non-zero)
    rehydrate_mock = MagicMock()
    agent = _fake_agent(usage=usage, rehydrate=rehydrate_mock)

    get_context_stats(cast(Any, agent), "t1")

    rehydrate_mock.assert_not_called()


def test_get_stats_usage_percentage_zero_when_no_limit(monkeypatch: pytest.MonkeyPatch):
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 0)

    usage = _fake_usage(last_input=500)
    agent = _fake_agent(usage=usage)

    stats = get_context_stats(cast(Any, agent), "t1")

    # No ZeroDivisionError; percentage falls back to 0.
    assert stats["context_limit"] == 0
    assert stats["usage_percentage"] == 0


# ----- model-switch occupancy re-estimate (token-audit defect #11) -----------


def _wire_reestimate(
    agent: Any,
    usage: Any,
    *,
    estimate: int,
    raises: bool = False,
) -> list[tuple]:
    """Attach a recording set_context_estimate + estimator to a fake agent.

    ``set_context_estimate`` mutates the shared usage namespace exactly like
    the real tracker so the follow-up ``get_usage`` read sees fresh values.
    """
    set_calls: list[tuple] = []

    def _set_estimate(tid, tokens, context_model=None):
        set_calls.append((tid, tokens, context_model))
        usage.last_input_tokens = tokens
        usage.context_tokens = tokens
        usage.context_model = context_model

    agent._token_tracker.set_context_estimate = _set_estimate

    def _estimate(_messages, _model):
        if raises:
            raise RuntimeError("estimator boom")
        return estimate

    agent._compaction._estimate_messages_tokens = _estimate
    return set_calls


def test_get_stats_reestimates_occupancy_after_model_switch(
    monkeypatch: pytest.MonkeyPatch,
):
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    usage = _fake_usage(last_input=150_000, context_model="claude-old")
    agent = _fake_agent(usage=usage, model="gpt-new")
    set_calls = _wire_reestimate(agent, usage, estimate=40_000)

    stats = get_context_stats(cast(Any, agent), "t1")

    assert set_calls == [("t1", 40_000, "gpt-new")]
    assert stats["total_tokens"] == 40_000
    assert stats["usage_percentage"] == 40.0


def test_get_stats_skips_reestimate_when_model_matches(
    monkeypatch: pytest.MonkeyPatch,
):
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    usage = _fake_usage(last_input=50_000, context_model="gpt-4o")
    agent = _fake_agent(usage=usage, model="gpt-4o")
    set_calls = _wire_reestimate(agent, usage, estimate=40_000)

    stats = get_context_stats(cast(Any, agent), "t1")

    assert set_calls == []
    assert stats["total_tokens"] == 50_000


def test_get_stats_skips_reestimate_without_a_stamp(
    monkeypatch: pytest.MonkeyPatch,
):
    """Legacy rows (stamp None) keep the old forward-looking division."""
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    usage = _fake_usage(last_input=50_000, context_model=None)
    agent = _fake_agent(usage=usage, model="gpt-new")
    set_calls = _wire_reestimate(agent, usage, estimate=40_000)

    get_context_stats(cast(Any, agent), "t1")

    assert set_calls == []


def test_get_stats_reestimate_failure_keeps_stale_value(
    monkeypatch: pytest.MonkeyPatch,
):
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    usage = _fake_usage(last_input=150_000, context_model="claude-old")
    agent = _fake_agent(usage=usage, model="gpt-new")
    set_calls = _wire_reestimate(agent, usage, estimate=40_000, raises=True)

    stats = get_context_stats(cast(Any, agent), "t1")

    # Stale value keeps rendering; the stamp is untouched so a later poll
    # retries the re-estimate.
    assert set_calls == []
    assert stats["total_tokens"] == 150_000
    assert usage.context_model == "claude-old"


def test_get_stats_zero_estimate_does_not_zero_the_bar(
    monkeypatch: pytest.MonkeyPatch,
):
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    usage = _fake_usage(last_input=150_000, context_model="claude-old")
    agent = _fake_agent(usage=usage, model="gpt-new")
    set_calls = _wire_reestimate(agent, usage, estimate=0)

    stats = get_context_stats(cast(Any, agent), "t1")

    assert set_calls == []
    assert stats["total_tokens"] == 150_000


def test_tracker_stamps_context_model_only_with_fresh_occupancy():
    from nymeria.core.token_tracker import TokenTracker

    tracker = TokenTracker()
    tracker.record_turn(
        "t1",
        turn_input_tokens=100,
        turn_output_tokens=10,
        context_tokens=100,
        context_model="model-a",
    )
    assert tracker.get_usage("t1").context_model == "model-a"

    # A turn with no fresh occupancy keeps the previous stamp.
    tracker.record_turn(
        "t1",
        turn_input_tokens=50,
        turn_output_tokens=5,
        context_tokens=None,
        context_model="model-b",
    )
    assert tracker.get_usage("t1").context_model == "model-a"
    assert tracker.get_usage("t1").context_tokens == 100

    tracker.set_context_estimate("t1", 40, context_model="model-b")
    usage = tracker.get_usage("t1")
    assert (usage.context_tokens, usage.context_model) == (40, "model-b")

    tracker.reset_after_compact("t1", 10, context_model="model-c")
    usage = tracker.get_usage("t1")
    assert (usage.context_tokens, usage.context_model) == (10, "model-c")


def test_get_stats_compact_trigger_none_when_auto_compact_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    usage = _fake_usage(last_input=1)
    agent = _fake_agent(usage=usage, context_management="none")

    stats = get_context_stats(cast(Any, agent), "t1")

    assert stats["compact_trigger_tokens"] is None


def test_get_stats_compact_trigger_honors_thread_threshold_override(
    monkeypatch: pytest.MonkeyPatch,
):
    """Per-thread percentage-mode override flows into the resolved trigger."""
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    usage = _fake_usage(last_input=1)
    agent = _fake_agent(usage=usage, threshold_config=("percentage", 0.5, 0))

    stats = get_context_stats(cast(Any, agent), "t1")

    assert stats["compact_trigger_tokens"] == 50_000


def test_get_stats_last_compaction_none_when_unset(monkeypatch: pytest.MonkeyPatch):
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    usage = _fake_usage(last_input=1)  # non-empty so rehydrate doesn't fire
    agent = _fake_agent(usage=usage)

    stats = get_context_stats(cast(Any, agent), "t1")

    assert stats["last_compaction"] is None

def test_get_stats_tokens_per_second_none_without_timing(
    monkeypatch: pytest.MonkeyPatch,
):
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    usage = _fake_usage(
        last_input=1_000, turn_input=900, turn_output=300, turn_recorded=True,
        turn_llm_seconds=None,
    )
    stats = get_context_stats(cast(Any, _fake_agent(usage=usage)), "t1")

    assert stats["turn_llm_seconds"] is None
    assert stats["tokens_per_second"] is None


def test_get_stats_tokens_per_second_none_without_output_tokens(
    monkeypatch: pytest.MonkeyPatch,
):
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    usage = _fake_usage(
        last_input=1_000, turn_input=900, turn_output=0, turn_recorded=True,
        turn_llm_seconds=4.0,
    )
    stats = get_context_stats(cast(Any, _fake_agent(usage=usage)), "t1")

    assert stats["turn_llm_seconds"] == 4.0
    assert stats["tokens_per_second"] is None


def test_record_turn_usage_passes_llm_seconds_through():
    agent = _fake_record_agent(context_tokens=(90, 45), turn=(120, 45, None, True))

    record_turn_usage(cast(Any, agent), "t1", "u1", ["msg"], turn_llm_seconds=3.25)

    _tid, kwargs = agent._record_turn_calls[0]
    assert kwargs["turn_llm_seconds"] == 3.25
