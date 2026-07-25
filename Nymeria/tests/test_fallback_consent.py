"""Tests for user-consented LLM fallback switching + the refusal swap.

Covers the durable record store and rendezvous coordinator
(``core/fallback_approvals.py``), the decision gate built by
``make_fallback_decision_callback`` (mode matrix, source gate, bot-origin
skip, approve/decline/timeout/abort outcomes, hold plumbing), the vendored
consult helpers (``nodes.py``), the consent gating of the sync
``_invoke_llm_with_retries`` site, the node-level refusal swap on both agent
nodes, and the ``get_llm_config_for_thread`` field population.

Deliberate policy pinned here (do not "fix"): an unanswered prompt AUTO-SWAPS
(a fallback is a resilience action), the inverse of hook-approval timeouts;
and a refusal swap fires ONLY on the empty refusal shape.
"""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import MagicMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

import nymeria.core.fallback_approvals as fa
from nymeria.core.fallback_approvals import (
    create_pending_approval,
    get_fallback_approval_coordinator,
    list_pending,
    load_record,
    make_fallback_decision_callback,
    public_entry,
    sweep_stale_records,
)
from nymeria.core.thread_config import ThreadLLMConfig
from nymeria.vendor.react_agent import nodes as nodes_module
from nymeria.vendor.react_agent.config import LLMConfig, LLMFallbackConfig
from nymeria.vendor.react_agent.nodes import create_agent_node

from test_llm_config_resolution import _make_agent


class _Transient(RuntimeError):
    status_code = 500


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Point the approval store at a temp dir and reset the coordinator."""
    settings = MagicMock(data_dir=tmp_path, fcm_enabled=False)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: settings)
    monkeypatch.setattr(fa, "_coordinator", None)
    yield


@pytest.fixture()
def events(monkeypatch):
    """Capture provider events dispatched from both node paths."""
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        nodes_module,
        "dispatch_custom_event",
        lambda name, payload, config=None: captured.append((name, payload)),
    )

    async def _acapture(name, payload, config=None):
        captured.append((name, payload))

    monkeypatch.setattr(nodes_module, "adispatch_custom_event", _acapture)
    return captured


def _mint(**kw):
    base = dict(
        kind="transport",
        user_id="u1",
        thread_id="t1",
        payload={
            "from_provider": "anthropic",
            "from_model": "claude-fable-5",
            "to_provider": "anthropic",
            "to_model": "claude-opus-4-8",
            "reason": "server_error",
            "http_status": 500,
        },
        window_seconds=60.0,
        default_hold_seconds=7200,
    )
    base.update(kw)
    return create_pending_approval(**base)


# --- store + coordinator ----------------------------------------------------


@pytest.mark.asyncio
async def test_mint_creates_durable_record_and_future():
    record, future = _mint()
    assert isinstance(future, asyncio.Future)
    stored = load_record(record["record_id"])
    assert stored is not None
    assert stored["kind"] == "transport"
    assert stored["from_model"] == "claude-fable-5"
    assert stored["to_model"] == "claude-opus-4-8"
    assert stored["hold_options"] == list(fa.HOLD_PRESET_SECONDS)
    assert stored["allow_permanent"] is True
    assert [e["record_id"] for e in list_pending("u1")] == [record["record_id"]]
    assert list_pending("someone-else") == []
    entry = public_entry(stored)
    assert entry["record_id"] == record["record_id"]
    assert entry["default_hold_seconds"] == 7200


@pytest.mark.asyncio
async def test_mint_enforces_per_user_cap(monkeypatch):
    monkeypatch.setattr(fa, "MAX_PENDING_PER_USER", 1)
    _mint()
    with pytest.raises(ValueError):
        _mint()
    _mint(user_id="u2")  # another user is unaffected


@pytest.mark.asyncio
async def test_sweep_removes_only_stale_records():
    fresh, _ = _mint()
    stale, _ = _mint()
    rec = load_record(stale["record_id"])
    rec["expires_at"] = "2020-01-01T00:00:00+00:00"
    fa._write_record(rec)
    assert sweep_stale_records() == 1
    assert load_record(stale["record_id"]) is None
    assert load_record(fresh["record_id"]) is not None


@pytest.mark.asyncio
async def test_resolve_carries_hold_and_wins_once():
    record, future = _mint()
    coordinator = get_fallback_approval_coordinator()
    assert coordinator.resolve(
        record["record_id"],
        approved=True,
        resolved_by="u1",
        hold_seconds=600,
    )
    result = await future
    assert result["approved"] is True
    assert result["hold_seconds"] == 600
    # The second resolve lost the race.
    assert not coordinator.resolve(
        record["record_id"], approved=False, resolved_by="u2"
    )


@pytest.mark.asyncio
async def test_abort_thread_snaps_parked_prompts():
    _, future_t1 = _mint(thread_id="t1")
    _, future_t2 = _mint(thread_id="t2")
    aborted = get_fallback_approval_coordinator().abort_thread("t1")
    assert aborted == 1
    assert (await future_t1)["status"] == "aborted"
    assert not future_t2.done()


# --- decision gate ----------------------------------------------------------


def _gate(**kw):
    base = dict(
        thread_id="t1",
        user_id="u1",
        switch_mode="ask",
        refusal_mode="ask",
        prompt_timeout_seconds=60,
        default_hold_seconds=7200,
    )
    base.update(kw)
    return make_fallback_decision_callback(**base)


def _context(
    kind="transport", is_autonomous=False, holder_kind="user", sync_surface=False
):
    return {
        "kind": kind,
        "is_autonomous": is_autonomous,
        "holder_kind": holder_kind,
        "sync_surface": sync_surface,
        "from_provider": "anthropic",
        "from_model": "claude-fable-5",
        "to_provider": "anthropic",
        "to_model": "claude-opus-4-8",
        "reason": "refusal" if kind == "refusal" else "server_error",
    }


@pytest.mark.asyncio
async def test_gate_transport_auto_mode_never_parks():
    decide = _gate(switch_mode="auto")
    assert await decide(_context()) == {"action": "auto"}
    assert list_pending() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("is_autonomous", [True, None])
async def test_gate_ask_mode_skips_non_consent_capable_turns(is_autonomous):
    """Autonomous turns and source-unknown turns never park."""
    decide = _gate()
    result = await decide(_context(is_autonomous=is_autonomous))
    assert result == {"action": "auto"}
    assert list_pending() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("holder_kind", ["callable", "handoff", None])
async def test_gate_ask_mode_skips_child_turn_holders(holder_kind):
    """A callable/handoff child's "user" is a waiting parent tool call, and a
    missing holder stamp means the surface is unknown: neither may park."""
    decide = _gate()
    result = await decide(_context(holder_kind=holder_kind))
    assert result == {"action": "auto"}
    result = await decide(_context(kind="refusal", holder_kind=holder_kind))
    assert result == {"action": "swap"}
    assert list_pending() == []


@pytest.mark.asyncio
async def test_gate_ask_mode_skips_sync_surface_turns():
    """/chat/sync serves bots and programmatic callers with no prompt UI."""
    decide = _gate()
    assert await decide(_context(sync_surface=True)) == {"action": "auto"}
    assert list_pending() == []


@pytest.mark.asyncio
async def test_gate_ask_mode_skips_bot_origin_threads(monkeypatch):
    import nymeria.core.bot_reactions as bot_reactions

    monkeypatch.setattr(
        bot_reactions, "get_turn_origin", lambda thread_id: {"platform": "telegram"}
    )
    decide = _gate()
    assert await decide(_context()) == {"action": "auto"}
    assert list_pending() == []


@pytest.mark.asyncio
async def test_gate_approve_returns_swap_with_chosen_hold():
    decide = _gate()
    task = asyncio.create_task(decide(_context()))
    record_id = await _wait_for_record()
    assert get_fallback_approval_coordinator().resolve(
        record_id, approved=True, resolved_by="u1", hold_permanent=True
    )
    result = await task
    assert result == {"action": "swap", "hold_permanent": True}
    # The waiter deleted its durable record on exit.
    assert list_pending() == []


@pytest.mark.asyncio
async def test_gate_decline_returns_fail():
    decide = _gate()
    task = asyncio.create_task(decide(_context()))
    record_id = await _wait_for_record()
    get_fallback_approval_coordinator().resolve(
        record_id, approved=False, resolved_by="u1"
    )
    assert await task == {"action": "fail"}


@pytest.mark.asyncio
async def test_gate_timeout_auto_swaps(monkeypatch):
    """Locked policy: silence keeps the turn working (swap), never fail."""
    monkeypatch.setattr(fa, "clamp_window", lambda value: 0.05)
    decide = _gate()
    result = await decide(_context())
    assert result == {"action": "swap"}
    assert list_pending() == []


@pytest.mark.asyncio
async def test_gate_abort_returns_fail():
    decide = _gate()
    task = asyncio.create_task(decide(_context()))
    await _wait_for_record()
    get_fallback_approval_coordinator().abort_thread("t1")
    assert await task == {"action": "fail"}


@pytest.mark.asyncio
async def test_gate_refusal_modes():
    assert await _gate(refusal_mode="off")(_context(kind="refusal")) == {
        "action": "fail"
    }
    assert await _gate(refusal_mode=None)(_context(kind="refusal")) == {
        "action": "fail"
    }
    assert await _gate(refusal_mode="auto")(_context(kind="refusal")) == {
        "action": "swap"
    }
    # ask + autonomous: swap immediately, no park.
    result = await _gate(refusal_mode="ask")(
        _context(kind="refusal", is_autonomous=True)
    )
    assert result == {"action": "swap"}
    assert list_pending() == []


@pytest.mark.asyncio
async def test_gate_park_fault_degrades_to_resilience_default(monkeypatch):
    """A broken park machinery must never fail the turn."""

    def boom(_record):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(fa, "persist_pending_record", boom)
    assert await _gate()(_context()) == {"action": "auto"}
    assert await _gate()(_context(kind="refusal")) == {"action": "swap"}
    # The registered-then-failed future was discarded, not leaked.
    assert get_fallback_approval_coordinator().pending_count() == 0


@pytest.mark.asyncio
async def test_gate_cancelled_park_cleans_up():
    """A turn task dying under the park leaks nothing and retracts the card."""
    decide = _gate()
    task = asyncio.create_task(decide(_context()))
    record_id = await _wait_for_record()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list_pending() == []
    assert get_fallback_approval_coordinator().pending_count() == 0
    assert load_record(record_id) is None


async def _wait_for_record() -> str:
    for _ in range(200):
        records = list_pending()
        if records:
            return str(records[0]["record_id"])
        await asyncio.sleep(0.005)
    raise AssertionError("no pending record appeared")


# --- vendored consult helpers ----------------------------------------------


def _refusal_message() -> AIMessage:
    return AIMessage(
        content=[{"type": "thinking", "thinking": "hmm", "signature": "x" * 64}],
        response_metadata={"stop_reason": "refusal", "model_name": "claude-fable-5"},
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )


class TestEmptyRefusalShape:
    def test_empty_anthropic_refusal(self):
        assert nodes_module._is_empty_refusal_response(_refusal_message())

    def test_empty_openai_content_filter(self):
        msg = AIMessage(content="", response_metadata={"finish_reason": "content_filter"})
        assert nodes_module._is_empty_refusal_response(msg)

    def test_partial_output_refusal_is_not_empty(self):
        msg = AIMessage(
            content=[{"type": "text", "text": "partial answer"}],
            response_metadata={"stop_reason": "refusal"},
        )
        assert not nodes_module._is_empty_refusal_response(msg)

    def test_tool_call_refusal_is_not_empty(self):
        msg = AIMessage(
            content="",
            tool_calls=[{"name": "t", "args": {}, "id": "1"}],
            response_metadata={"stop_reason": "refusal"},
        )
        assert not nodes_module._is_empty_refusal_response(msg)

    def test_normal_response_is_not_refusal(self):
        msg = AIMessage(content="ok", response_metadata={"stop_reason": "end_turn"})
        assert not nodes_module._is_empty_refusal_response(msg)


async def _swap_callback(context):
    return {"action": "swap"}


def _consent_config(**kwargs) -> LLMConfig:
    base = dict(
        provider="custom",
        model="primary-model",
        stream_max_retries=0,
        stream_retry_initial_delay=0.0,
        stream_retry_max_delay=0.0,
        fallbacks=[LLMFallbackConfig(provider="custom", model="fallback-model")],
        fallback_decision_callback=_swap_callback,
    )
    base.update(kwargs)
    return LLMConfig(**base)


class TestVendoredHelpers:
    def test_refusal_swap_next_index_gates(self):
        # The off/ask/auto MODE is host policy inside the decision callback
        # (an "off" host answers "fail"); the vendored gate checks only
        # consent wiring and candidate supply.
        assert (
            nodes_module._refusal_swap_next_index(
                _consent_config(fallback_decision_callback=None), 0
            )
            is None
        )
        assert (
            nodes_module._refusal_swap_next_index(_consent_config(fallbacks=[]), 0)
            is None
        )
        assert nodes_module._refusal_swap_next_index(_consent_config(), 0) == 1
        # Last candidate: nothing left to swap to.
        assert nodes_module._refusal_swap_next_index(_consent_config(), 1) is None

    def test_turn_is_autonomous_reads_stamp(self):
        assert nodes_module._turn_is_autonomous(None) is None
        assert nodes_module._turn_is_autonomous({"configurable": {}}) is None
        assert (
            nodes_module._turn_is_autonomous(
                {"configurable": {"hook_is_autonomous": False}}
            )
            is False
        )
        assert (
            nodes_module._turn_is_autonomous(
                {"configurable": {"hook_is_autonomous": True}}
            )
            is True
        )

    def test_payload_with_hold_merges_choice(self):
        payload = {"to_model": "m"}
        assert nodes_module._payload_with_hold(payload, {"action": "swap"}) == payload
        assert nodes_module._payload_with_hold(
            payload, {"action": "swap", "hold_seconds": 600}
        ) == {"to_model": "m", "hold_seconds": 600}
        assert nodes_module._payload_with_hold(
            payload, {"action": "swap", "hold_permanent": True, "hold_seconds": 600}
        ) == {"to_model": "m", "hold_permanent": True}

    def test_sync_bridge_runs_async_callback(self):
        decisions = []

        async def decide(context):
            decisions.append(context["kind"])
            return {"action": "fail"}

        cfg = _consent_config(fallback_decision_callback=decide)
        result = nodes_module._consult_fallback_decision_sync(
            cfg, {"to_model": "m"}, kind="transport", is_autonomous=False
        )
        assert result == {"action": "fail"}
        assert decisions == ["transport"]

    @pytest.mark.asyncio
    async def test_sync_bridge_degrades_to_auto_on_running_loop(self):
        cfg = _consent_config()
        result = nodes_module._consult_fallback_decision_sync(
            cfg, {}, kind="transport", is_autonomous=False
        )
        assert result == {"action": "auto"}

    @pytest.mark.asyncio
    async def test_consult_maps_bad_callback_output_to_auto(self):
        async def weird(context):
            return {"action": "explode"}

        cfg = _consent_config(fallback_decision_callback=weird)
        result = await nodes_module._aconsult_fallback_decision(
            cfg, {}, kind="transport", is_autonomous=False
        )
        assert result == {"action": "auto"}


# --- sync transport site gating ---------------------------------------------


def test_sync_site_decline_raises_original_error(events):
    async def decline(context):
        assert context["kind"] == "transport"
        return {"action": "fail"}

    activations = []
    cfg = _consent_config(
        fallback_decision_callback=decline,
        fallback_activation_callback=lambda payload: activations.append(payload),
    )

    def invoke(_candidate):
        raise _Transient("boom")

    with pytest.raises(_Transient):
        nodes_module._invoke_llm_with_retries(
            invoke, cfg, cast(BaseChatModel, object()), [], None
        )
    assert activations == []
    assert cfg.active_fallback_candidate_index == 0
    assert [name for name, _ in events if name == "provider_fallback"] == []


def test_sync_site_swap_carries_chosen_hold(events, monkeypatch):
    async def approve(context):
        return {"action": "swap", "hold_seconds": 600}

    activations = []
    cfg = _consent_config(
        fallback_decision_callback=approve,
        fallback_activation_callback=lambda payload: activations.append(payload),
    )
    fallback_llm = object()
    monkeypatch.setattr(
        nodes_module, "create_llm_with_tools", lambda config, tools: fallback_llm
    )

    def invoke(candidate):
        if candidate is not fallback_llm:
            raise _Transient("boom")
        return AIMessage(content="from-fallback")

    result = nodes_module._invoke_llm_with_retries(
        invoke, cfg, cast(BaseChatModel, object()), [], None
    )
    assert result.content == "from-fallback"
    assert activations[0]["hold_seconds"] == 600
    assert cfg.active_fallback_candidate_index == 1


# --- node-level refusal swap ------------------------------------------------


class _FakeLLM:
    def __init__(self, response: AIMessage):
        self._response = response

    def invoke(self, _messages, **kwargs):
        return self._response


class _FakeStreamLLM:
    def __init__(self, response: AIMessage):
        self._response = response

    async def astream(self, _messages, **kwargs):
        yield AIMessageChunk(
            content=self._response.content,
            response_metadata=self._response.response_metadata,
            usage_metadata=self._response.usage_metadata,
            tool_calls=self._response.tool_calls,
        )


@pytest.fixture()
def _quiet_streaming_helpers(monkeypatch):
    async def _noop_warm(llm_config, candidate_index):
        return None

    monkeypatch.setattr(nodes_module, "_warm_max_output_ceiling", _noop_warm)
    monkeypatch.setattr(
        nodes_module,
        "_streaming_max_tokens_kwargs",
        lambda candidate, llm_config, candidate_index=0, cache=None: {},
    )


def test_sync_node_swaps_empty_refusal_to_fallback(events, monkeypatch):
    activations = []
    cfg = _consent_config(
        fallback_activation_callback=lambda payload: activations.append(payload),
    )
    monkeypatch.setattr(
        nodes_module,
        "create_llm_with_tools",
        lambda config, tools: _FakeLLM(AIMessage(content="rescued")),
    )
    node = create_agent_node(_FakeLLM(_refusal_message()), "system prompt", cfg)
    result = node.invoke(
        {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
    )
    message = result["messages"][0]
    assert message.content == "rescued"
    assert "empty_turn_refusal" not in (message.additional_kwargs or {})
    fallback_events = [p for n, p in events if n == "provider_fallback"]
    assert fallback_events and fallback_events[0]["reason"] == "refusal"
    assert activations and activations[0]["to_model"] == "fallback-model"
    assert cfg.active_fallback_candidate_index == 1
    # The discarded refusal stays visible in telemetry, marked as swapped.
    refused = [p for n, p in events if n == "response_refused"]
    assert refused and refused[0]["swapped"] is True
    assert refused[0]["produced_output"] is False
    assert refused[0]["model"] == "primary-model"


def test_sync_node_declined_refusal_keeps_p1_path(events):
    async def decline(context):
        return {"action": "fail"}

    cfg = _consent_config(fallback_decision_callback=decline)
    node = create_agent_node(_FakeLLM(_refusal_message()), "system prompt", cfg)
    result = node.invoke(
        {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
    )
    message = result["messages"][0]
    # The shipped P1 behavior: marker + visible notice, no swap.
    assert message.additional_kwargs.get("empty_turn_refusal") is True
    assert [n for n, _ in events if n == "provider_fallback"] == []
    refused = [p for n, p in events if n == "response_refused"]
    assert refused and refused[0]["produced_output"] is False


def test_sync_node_partial_refusal_never_consults(events):
    async def explode(context):  # pragma: no cover - must not be called
        raise AssertionError("partial-output refusal must not consult the gate")

    cfg = _consent_config(fallback_decision_callback=explode)
    partial = AIMessage(
        content=[{"type": "text", "text": "some answer"}],
        response_metadata={"stop_reason": "refusal"},
    )
    node = create_agent_node(_FakeLLM(partial), "system prompt", cfg)
    result = node.invoke(
        {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
    )
    assert result["messages"][0].content == [
        {"type": "text", "text": "some answer"}
    ]
    refused = [p for n, p in events if n == "response_refused"]
    assert refused and refused[0]["produced_output"] is True


def test_sync_node_second_refusal_falls_through_to_p1(events, monkeypatch):
    """The fallback also refused: one swap attempt only, then P1 handles it."""
    cfg = _consent_config()
    monkeypatch.setattr(
        nodes_module,
        "create_llm_with_tools",
        lambda config, tools: _FakeLLM(_refusal_message()),
    )
    node = create_agent_node(_FakeLLM(_refusal_message()), "system prompt", cfg)
    result = node.invoke(
        {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
    )
    message = result["messages"][0]
    assert message.additional_kwargs.get("empty_turn_refusal") is True
    # Exactly one swap was attempted.
    assert [n for n, _ in events if n == "provider_fallback"] == ["provider_fallback"]


@pytest.mark.asyncio
async def test_async_node_swaps_empty_refusal_to_fallback(
    events, monkeypatch, _quiet_streaming_helpers
):
    activations = []
    cfg = _consent_config(
        fallback_activation_callback=lambda payload: activations.append(payload),
    )
    monkeypatch.setattr(
        nodes_module,
        "create_llm_with_tools",
        lambda config, tools: _FakeStreamLLM(AIMessage(content="rescued")),
    )
    node = create_agent_node(
        _FakeStreamLLM(_refusal_message()), "system prompt", cfg
    )
    result = await node.ainvoke(
        {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
    )
    message = result["messages"][0]
    assert nodes_module._visible_text_of(message.content) == "rescued"
    fallback_events = [p for n, p in events if n == "provider_fallback"]
    assert fallback_events and fallback_events[0]["reason"] == "refusal"
    assert activations and activations[0]["to_model"] == "fallback-model"
    assert cfg.active_fallback_candidate_index == 1
    refused = [p for n, p in events if n == "response_refused"]
    assert refused and refused[0]["swapped"] is True


async def _off_mode_callback(context):
    # What make_fallback_decision_callback answers when refusal mode is "off".
    return {"action": "fail"}


@pytest.mark.asyncio
@pytest.mark.parametrize("callback", [None, _off_mode_callback])
async def test_async_node_off_mode_keeps_p1_path(
    events, _quiet_streaming_helpers, callback
):
    """No consent wiring, or an off-mode host answering "fail": P1 handles it."""
    cfg = _consent_config(fallback_decision_callback=callback)
    node = create_agent_node(
        _FakeStreamLLM(_refusal_message()), "system prompt", cfg
    )
    result = await node.ainvoke(
        {"messages": [HumanMessage(content="hi")]}, {"configurable": {}}
    )
    message = result["messages"][0]
    assert message.additional_kwargs.get("empty_turn_refusal") is True
    assert [n for n, _ in events if n == "provider_fallback"] == []


# --- /fallback command surface ----------------------------------------------


def _command_ctx(actor="user"):
    from nymeria.core.command_service import CommandContext

    return CommandContext(
        user_id="u1",
        thread_id="t1",
        actor=actor,
        surface="cli",
        is_admin=False,
    )


@pytest.mark.parametrize("sub", ["approvals", "approve x", "deny x", "revert"])
def test_fallback_resolve_commands_are_human_only(sub):
    from cli_fixtures import run
    from nymeria.core.command_service import CommandService
    from test_command_service import FakeCommandApi

    service = CommandService()
    result = run(
        service.execute(_command_ctx(actor="agent"), f"/fallback {sub}", api=FakeCommandApi())
    )
    assert result.success is False
    assert "not available to the agent" in result.markdown


def _mint_on_loop():
    """Mint on a dedicated waiter loop (the command tests run sync)."""
    loop = asyncio.new_event_loop()

    async def _mint_coro():
        return _mint()

    record, future = loop.run_until_complete(_mint_coro())
    return loop, record, future


def _resolved(loop, future):
    return loop.run_until_complete(asyncio.wait_for(future, 2.0))


def test_fallback_approve_command_resolves_with_minutes(monkeypatch):
    import nymeria.tools.utils as tools_utils
    from cli_fixtures import run
    from nymeria.core.command_service import CommandService
    from test_command_service import FakeCommandApi

    monkeypatch.setattr(tools_utils, "is_admin", lambda user_id, agent=None: False)
    loop, record, future = _mint_on_loop()
    try:
        service = CommandService()
        result = run(
            service.execute(
                _command_ctx(),
                f"/fallback approve {record['record_id']} 10",
                api=FakeCommandApi(),
            )
        )
        assert result.success is True
        resolved = _resolved(loop, future)
        assert resolved["hold_seconds"] == 600
        assert resolved["approved"] is True
    finally:
        loop.close()


def test_fallback_deny_command_declines(monkeypatch):
    import nymeria.tools.utils as tools_utils
    from cli_fixtures import run
    from nymeria.core.command_service import CommandService
    from test_command_service import FakeCommandApi

    monkeypatch.setattr(tools_utils, "is_admin", lambda user_id, agent=None: False)
    loop, record, future = _mint_on_loop()
    try:
        service = CommandService()
        result = run(
            service.execute(
                _command_ctx(),
                f"/fallback deny {record['record_id']}",
                api=FakeCommandApi(),
            )
        )
        assert result.success is True
        assert _resolved(loop, future)["approved"] is False
    finally:
        loop.close()


def test_fallback_revert_requires_thread_ownership(monkeypatch):
    """The executor context carries an unvalidated caller-supplied thread_id;
    revert must refuse (non-leaking) when the thread is not in the caller's
    visible list."""
    import nymeria.tools.utils as tools_utils
    from cli_fixtures import run
    from nymeria.core.command_service import CommandService
    from test_command_service import FakeCommandApi

    monkeypatch.setattr(tools_utils, "is_admin", lambda user_id, agent=None: False)
    service = CommandService()
    # _command_ctx targets thread "t1", which FakeCommandApi does not list.
    result = run(
        service.execute(_command_ctx(), "/fallback revert", api=FakeCommandApi())
    )
    assert result.success is False
    assert "No thread matching" in result.markdown


def test_fallback_status_hides_foreign_thread_section(monkeypatch):
    import nymeria.tools.utils as tools_utils
    from cli_fixtures import run
    from nymeria.core.command_service import CommandService
    from test_command_service import FakeCommandApi

    monkeypatch.setattr(tools_utils, "is_admin", lambda user_id, agent=None: False)
    service = CommandService()
    result = run(
        service.execute(_command_ctx(), "/fallback status", api=FakeCommandApi())
    )
    assert result.success is True
    assert "Switch mode:" in result.markdown
    assert "This thread" not in result.markdown


# --- LLMConfig resolution ---------------------------------------------------


def test_llm_config_resolution_wires_consent_callback_only():
    """The policy crosses the vendored boundary ONLY inside the callback
    closure; LLMConfig carries no mode fields (they'd be dead payload)."""
    agent = _make_agent()
    agent.settings.llm_fallback_switch_mode = "ask"
    agent.settings.llm_refusal_swap_mode = "auto"
    agent.settings.llm_fallback_prompt_timeout_seconds = 120

    config = agent.get_llm_config_for_thread("thread-1", "u1")
    assert config.fallback_decision_callback is not None
    assert not hasattr(config, "fallback_switch_mode")
    assert not hasattr(config, "refusal_swap_mode")
    assert not hasattr(config, "fallback_prompt_timeout")
    # The closure carries the resolved refusal mode: auto = silent swap.
    result = asyncio.run(
        config.fallback_decision_callback(
            {"kind": "refusal", "is_autonomous": True}
        )
    )
    assert result == {"action": "swap"}


def test_llm_config_resolution_thread_overrides_consent_modes():
    agent = _make_agent(ThreadLLMConfig(refusal_swap_mode="ask"))
    agent.settings.llm_refusal_swap_mode = "off"

    config = agent.get_llm_config_for_thread("thread-1", "u1")
    # Global "off" would answer "fail"; the thread's "ask" answers "swap" for
    # a non-consent-capable turn, proving the override reached the closure.
    result = asyncio.run(
        config.fallback_decision_callback(
            {"kind": "refusal", "is_autonomous": True}
        )
    )
    assert result == {"action": "swap"}


def test_llm_config_resolution_no_thread_has_no_decision_callback():
    agent = _make_agent()
    config = agent.get_llm_config_for_thread("")
    assert config.fallback_decision_callback is None
