"""Unit tests for the context-stats cluster extracted from NymeriaAgent."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from nymeria.core.agent_context_stats import (
    get_context_stats,
    rehydrate_token_usage,
)


def _fake_usage(
    *,
    total_input: int = 0,
    total_output: int = 0,
    last_input: int = 0,
    last_output: int = 0,
    compaction_count: int = 0,
    last_compaction_at: datetime | None = None,
    last_cost_usd: float | None = None,
    total_cost_usd: float = 0.0,
    cost_unavailable: bool = False,
    last_recorded_message_index: int = 0,
) -> Any:
    """SimpleNamespace stand-in for TokenUsage.

    SimpleNamespace lets us treat ``context_tokens`` and ``total_tokens``
    (which are computed properties on the real class) as plain settable
    attributes, which makes branch testing trivial.
    """
    return SimpleNamespace(
        total_input_tokens=total_input,
        total_output_tokens=total_output,
        last_input_tokens=last_input,
        last_output_tokens=last_output,
        compaction_count=compaction_count,
        last_compaction_at=last_compaction_at,
        context_tokens=last_input,  # mirrors the real property
        total_tokens=total_input + total_output,  # mirrors the real property
        last_cost_usd=last_cost_usd,
        total_cost_usd=total_cost_usd,
        cost_unavailable=cost_unavailable,
        last_recorded_message_index=last_recorded_message_index,
    )


def _fake_agent(
    *,
    messages: list | None = None,
    graph_raises: BaseException | None = None,
    usage: Any | None = None,
    second_usage: Any | None = None,
    rehydrate: Any | None = None,
    model: str = "gpt-4o",
    context_management: str = "auto_compact",
) -> Any:
    """Build a minimal agent stub for the context-stats functions.

    ``second_usage`` lets a test stage two different ``get_usage`` returns
    (the function calls ``get_usage`` twice when rehydration runs).
    """
    state = SimpleNamespace(values={"messages": messages or []})

    def _get_state(_config):
        if graph_raises is not None:
            raise graph_raises
        return state

    default_graph = SimpleNamespace(get_state=_get_state)

    usages = [usage if usage is not None else _fake_usage()]
    if second_usage is not None:
        usages.append(second_usage)

    record_calls: list[tuple[str, int, int]] = []
    get_usage_calls = 0

    def _get_usage(_tid):
        nonlocal get_usage_calls
        # Return usages[i] for the i-th call, sticking on the last entry.
        idx = min(len(record_calls) + get_usage_calls, len(usages) - 1)
        get_usage_calls += 1
        return usages[idx]

    def _record(tid, last_in, last_out):
        record_calls.append((tid, last_in, last_out))

    token_tracker = SimpleNamespace(
        get_usage=_get_usage,
        record_usage=_record,
    )

    llm_config = SimpleNamespace(model=model)
    settings = SimpleNamespace(context_management=context_management)

    agent = SimpleNamespace(
        _default_graph=default_graph,
        _token_tracker=token_tracker,
        _rehydrate_token_usage=rehydrate if rehydrate is not None else MagicMock(),
        _get_llm_config_for_thread=lambda _tid: llm_config,
        settings=settings,
    )
    agent._record_calls = record_calls  # type: ignore[attr-defined]
    return agent


# ----- rehydrate_token_usage --------------------------------------------------


def test_rehydrate_skips_when_no_ai_messages():
    agent = _fake_agent(messages=[HumanMessage(content="hi")])
    rehydrate_token_usage(cast(Any, agent), "t1")
    assert agent._record_calls == []


def test_rehydrate_records_cumulative_and_last_call(monkeypatch: pytest.MonkeyPatch):
    # Two AIMessages -> extract_from_message called twice -> cumulative (20, 40);
    # extract_last_from_messages returns the latest (10, 20).
    import nymeria.core.token_usage as tu

    monkeypatch.setattr(tu, "extract_from_message", lambda _msg: (10, 20))
    monkeypatch.setattr(tu, "extract_last_from_messages", lambda _msgs: (10, 20))

    # Module imports these names into agent_context_stats at import time, so
    # patching the source module isn't enough -- patch the bound names too.
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "extract_from_message", lambda _msg: (10, 20))
    monkeypatch.setattr(acs, "extract_last_from_messages", lambda _msgs: (10, 20))

    usage = _fake_usage()
    agent = _fake_agent(
        messages=[AIMessage(content="a"), AIMessage(content="b")],
        usage=usage,
    )

    rehydrate_token_usage(cast(Any, agent), "t1")

    assert agent._record_calls == [("t1", 10, 20)]
    # After record_usage, the function patches cumulative totals on the usage
    # object returned by the second get_usage call. With our stub, both calls
    # return the same SimpleNamespace, so we can read the patched values.
    assert usage.total_input_tokens == 20
    assert usage.total_output_tokens == 40


def test_rehydrate_silently_swallows_graph_exceptions():
    agent = _fake_agent(graph_raises=RuntimeError("graph blew up"))

    # Must not raise.
    rehydrate_token_usage(cast(Any, agent), "t1")

    assert agent._record_calls == []


# ----- get_context_stats ------------------------------------------------------


def test_get_stats_returns_dict_with_expected_keys(monkeypatch: pytest.MonkeyPatch):
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    compaction_at = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
    usage = _fake_usage(
        total_input=3_000,
        total_output=2_000,
        last_input=1_500,
        last_output=500,
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
        "input_tokens": 1_500,
        "output_tokens": 500,
        "cumulative_tokens": 5_000,
        "context_limit": 100_000,
        "usage_percentage": 1.5,
        "compaction_count": 2,
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


def test_get_stats_last_compaction_none_when_unset(monkeypatch: pytest.MonkeyPatch):
    import nymeria.core.agent_context_stats as acs

    monkeypatch.setattr(acs, "get_context_limit", lambda _model: 100_000)

    usage = _fake_usage(last_input=1)  # non-empty so rehydrate doesn't fire
    agent = _fake_agent(usage=usage)

    stats = get_context_stats(cast(Any, agent), "t1")

    assert stats["last_compaction"] is None
