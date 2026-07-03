"""Tests for interactive hook approvals (core/hook_approvals.py + the
``require_approval`` action).

Covers the durable record store (mint/list/delete/cap/sweep), the rendezvous
coordinator (resolve wins once, abort_thread snaps holds), and the action's
outcome mapping (conditions gate, approve -> allow, deny -> deny with note,
timeout -> the hardened no-consent deny, abort -> deny, mint failure -> fail
closed), plus the bridge's coroutine-action wrapper.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from nymeria.core.hook_approvals import (
    clamp_window,
    create_pending_approval,
    get_hook_approval_coordinator,
    list_pending,
    load_record,
    public_entry,
    sweep_stale_records,
)
from nymeria.core.hooks import HookContext, HookEvent
from nymeria.core.hooks.actions import require_approval
from nymeria.core.hooks.base import PreToolOutcome


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    """Point the approval store at a temp dir and reset the coordinator."""
    settings = MagicMock(data_dir=tmp_path, fcm_enabled=False)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: settings)
    import nymeria.core.hook_approvals as ha

    monkeypatch.setattr(ha, "_coordinator", None)
    yield


def _ctx(**kw) -> HookContext:
    base = dict(
        event=HookEvent.PRE_TOOL_USE,
        thread_id="t1",
        user_id="u1",
        is_autonomous=False,
        tool_name="bash_execute",
        tool_call_id="call-1",
        tool_args={"command": "rm -rf /tmp/x"},
    )
    base.update(kw)
    return HookContext(**base)


def _mint(**kw):
    base = dict(
        user_id="u1",
        thread_id="t1",
        hook_id="h1",
        hook_name="guard",
        tool_name="bash_execute",
        tool_call_id="call-1",
        tool_args={"command": "ls"},
        prompt="Approve?",
        window_seconds=60.0,
    )
    base.update(kw)
    return create_pending_approval(**base)


# --- store ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_creates_durable_record_and_future():
    record, future = _mint()
    assert isinstance(future, asyncio.Future)
    stored = load_record(record["record_id"])
    assert stored is not None
    assert stored["tool_name"] == "bash_execute"
    assert stored["tool_call_id"] == "call-1"
    assert stored["prompt"] == "Approve?"
    entries = list_pending("u1")
    assert [e["record_id"] for e in entries] == [record["record_id"]]
    # Owner filter excludes other users.
    assert list_pending("someone-else") == []
    entry = public_entry(stored)
    assert entry["record_id"] == record["record_id"]
    assert entry["tool_args_preview"]


@pytest.mark.asyncio
async def test_mint_enforces_per_user_cap(monkeypatch):
    import nymeria.core.hook_approvals as ha

    monkeypatch.setattr(ha, "MAX_PENDING_PER_USER", 1)
    _mint(tool_call_id="call-1")
    with pytest.raises(ValueError):
        _mint(tool_call_id="call-2")
    # Another user is unaffected by u1's cap.
    _mint(user_id="u2", tool_call_id="call-3")


@pytest.mark.asyncio
async def test_sweep_removes_only_stale_records():
    fresh, _ = _mint(tool_call_id="fresh")
    stale, _ = _mint(tool_call_id="stale", window_seconds=60.0)
    # Age the stale record far past expiry + slack.
    rec = load_record(stale["record_id"])
    rec["expires_at"] = "2020-01-01T00:00:00+00:00"
    import nymeria.core.hook_approvals as ha

    ha._write_record(rec)
    removed = sweep_stale_records()
    assert removed == 1
    assert load_record(stale["record_id"]) is None
    assert load_record(fresh["record_id"]) is not None


# --- coordinator ------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_wakes_future_and_wins_only_once():
    record, future = _mint()
    coord = get_hook_approval_coordinator()
    assert coord.resolve(
        record["record_id"], approved=True, resolved_by="manning", note="ok"
    )
    result = await asyncio.wait_for(future, timeout=2)
    assert result == {
        "status": "resolved",
        "approved": True,
        "resolved_by": "manning",
        "note": "ok",
    }
    # The second resolve lost: nothing pending under that id.
    assert not coord.resolve(record["record_id"], approved=False, resolved_by="x")


@pytest.mark.asyncio
async def test_abort_thread_snaps_matching_holds_only():
    _, f1 = _mint(tool_call_id="a", thread_id="t1")
    _, f2 = _mint(tool_call_id="b", thread_id="t2")
    coord = get_hook_approval_coordinator()
    assert coord.abort_thread("t1") == 1
    assert (await asyncio.wait_for(f1, timeout=2)) == {"status": "aborted"}
    assert not f2.done()


# --- action -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_conditions_not_met_allows_without_asking():
    out = await require_approval(
        _ctx(),
        {"conditions": [{"field": "command", "operator": "contains", "value": "nope"}]},
    )
    assert out is None
    assert list_pending() == []


@pytest.mark.asyncio
async def test_action_malformed_conditions_are_a_noop():
    out = await require_approval(_ctx(), {"conditions": "not-a-list"})
    assert out is None


async def _run_action_with_resolution(*, approved: bool, note: str = ""):
    """Run the action and resolve its pending record from a side task."""

    async def _resolver():
        for _ in range(100):
            pending = list_pending("u1")
            if pending:
                get_hook_approval_coordinator().resolve(
                    pending[0]["record_id"],
                    approved=approved,
                    resolved_by="manning",
                    note=note,
                )
                return
            await asyncio.sleep(0.01)
        raise AssertionError("no pending record appeared")

    resolver = asyncio.ensure_future(_resolver())
    out = await require_approval(
        _ctx(), {"timeout_seconds": 30, "prompt": "Run {tool_name}?"}
    )
    await resolver
    return out


@pytest.mark.asyncio
async def test_action_approved_allows_with_note():
    out = await _run_action_with_resolution(approved=True)
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "allow"
    assert "approved by manning" in (out.note or "")
    # The waiter cleaned up its record.
    assert list_pending() == []


@pytest.mark.asyncio
async def test_action_denied_carries_resolver_note_and_no_retry_tail():
    out = await _run_action_with_resolution(approved=False, note="not on prod")
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "deny"
    assert "Denied by manning: not on prod" in (out.reason or "")
    assert "Do not retry" in (out.reason or "")
    assert list_pending() == []


@pytest.mark.asyncio
async def test_action_timeout_denies_with_no_consent_message(monkeypatch):
    import nymeria.core.hook_approvals as ha

    monkeypatch.setattr(ha, "MIN_APPROVAL_WINDOW_SECONDS", 0.05)
    out = await require_approval(_ctx(), {"timeout_seconds": 0.05})
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "deny"
    reason = out.reason or ""
    assert "did not approve" in reason
    assert "Do not retry" in reason
    assert "Silence is not consent" in reason
    assert list_pending() == []


@pytest.mark.asyncio
async def test_action_abort_denies_as_cancelled():
    async def _aborter():
        for _ in range(100):
            if list_pending("u1"):
                get_hook_approval_coordinator().abort_thread("t1")
                return
            await asyncio.sleep(0.01)
        raise AssertionError("no pending record appeared")

    aborter = asyncio.ensure_future(_aborter())
    out = await require_approval(_ctx(), {"timeout_seconds": 30})
    await aborter
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "deny"
    assert "cancelled" in (out.reason or "")
    assert list_pending() == []


@pytest.mark.asyncio
async def test_action_mint_failure_fails_closed(monkeypatch):
    import nymeria.core.hook_approvals as ha

    monkeypatch.setattr(ha, "MAX_PENDING_PER_USER", 0)
    out = await require_approval(_ctx(), {})
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "deny"
    assert "fail closed" in (out.reason or "")


@pytest.mark.asyncio
async def test_action_cancellation_cleans_up_record():
    task = asyncio.ensure_future(require_approval(_ctx(), {"timeout_seconds": 30}))
    for _ in range(100):
        if list_pending("u1"):
            break
        await asyncio.sleep(0.01)
    assert list_pending("u1")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list_pending() == []
    assert get_hook_approval_coordinator().pending_count() == 0


@pytest.mark.asyncio
async def test_action_publishes_pending_and_resolved_events(monkeypatch):
    events: list[tuple[str, dict]] = []

    def _capture(event_type, thread_id, user_id, task_id, data):
        events.append((event_type, data))

    monkeypatch.setattr(
        "nymeria.core.event_bus.publish_autonomous_event", _capture
    )
    out = await _run_action_with_resolution(approved=True)
    assert out.decision == "allow"
    types = [t for t, _ in events]
    assert "hook_approval" in types
    assert "hook_approval_resolved" in types
    pending_data = next(d for t, d in events if t == "hook_approval")
    assert pending_data["tool_call_id"] == "call-1"
    resolved_data = next(d for t, d in events if t == "hook_approval_resolved")
    assert resolved_data["outcome"] == "approved"


# --- window clamp + bridge wrapper -------------------------------------------


def test_clamp_window_bounds():
    assert clamp_window(None) == 180.0
    assert clamp_window("bogus") == 180.0
    assert clamp_window(1) == 10.0
    assert clamp_window(10_000) == 600.0
    assert clamp_window(240) == 240.0


def test_bridge_builds_coroutine_wrapper_for_async_action():
    import inspect

    from nymeria.core.hook_manager import HookDefinition, RequireApprovalLogic
    from nymeria.core.hooks.bridge import build_registry

    definition = HookDefinition(
        id="abc12345",
        name="approve bash",
        event="pre_tool_use",
        matcher="bash_execute",
        logic=RequireApprovalLogic(timeout_seconds=120.0),
        scope="global",
    )
    registry = build_registry([definition])
    regs = registry.matching(
        HookEvent.PRE_TOOL_USE, _ctx(tool_name="bash_execute"), observe=False
    )
    assert len(regs) == 1
    assert inspect.iscoroutinefunction(regs[0].fn)
    # The author's window drives the dispatcher budget (+0.5s slack).
    assert regs[0].timeout == pytest.approx(120.5)


def test_bridge_injects_definition_metadata_into_params():
    from nymeria.core.hook_manager import BlockIfMatchesLogic, HookDefinition
    from nymeria.core.hooks import bridge as bridge_mod

    captured: dict = {}

    def _spy(ctx, params):
        captured.update(params)
        return None

    definition = HookDefinition(
        id="def45678",
        name="spy hook",
        event="pre_tool_use",
        logic=BlockIfMatchesLogic(),
        scope="global",
    )
    with patch.dict(bridge_mod.ACTIONS, {"block_if_matches": _spy}):
        registry = bridge_mod.build_registry([definition])
        regs = registry.matching(HookEvent.PRE_TOOL_USE, _ctx(), observe=False)
        regs[0].fn(_ctx())
    assert captured["__definition_id"] == "def45678"
    assert captured["__definition_name"] == "spy hook"


# --- SafeToolNode integration -------------------------------------------------


@pytest.mark.asyncio
async def test_tool_node_holds_then_allows_on_approval():
    from langchain_core.tools import tool as lc_tool
    from langgraph.runtime import DEFAULT_RUNTIME
    from langgraph._internal._constants import CONFIG_KEY_RUNTIME

    import importlib

    dmod = importlib.import_module("nymeria.core.hooks.dispatch")
    from nymeria.vendor.react_agent.nodes import SafeToolNode

    @lc_tool
    def echo(text: str) -> str:
        """Echo the text back."""
        return f"echo:{text}"

    dmod.reset()
    try:

        async def _hook(ctx):
            return await require_approval(ctx, {"timeout_seconds": 30})

        dmod.register(HookEvent.PRE_TOOL_USE, _hook, name="approve echo")
        node = SafeToolNode([echo])
        config = {
            "configurable": {
                CONFIG_KEY_RUNTIME: DEFAULT_RUNTIME,
                "thread_id": "t1",
                "user_id": "u1",
            }
        }
        msg = {
            "messages": [
                __import__("langchain_core.messages", fromlist=["AIMessage"]).AIMessage(
                    content="",
                    tool_calls=[{"name": "echo", "args": {"text": "hi"}, "id": "c1"}],
                )
            ]
        }

        async def _approver():
            for _ in range(200):
                pending = list_pending("u1")
                if pending:
                    get_hook_approval_coordinator().resolve(
                        pending[0]["record_id"], approved=True, resolved_by="manning"
                    )
                    return pending[0]
                await asyncio.sleep(0.01)
            raise AssertionError("no pending record appeared")

        approver = asyncio.ensure_future(_approver())
        out = await node.ainvoke(msg, config)
        record = await approver
        assert record["tool_call_id"] == "c1"
        assert out["messages"][0].content == "echo:hi"
    finally:
        dmod.reset()


@pytest.mark.asyncio
async def test_tool_node_denies_on_timeout_with_no_consent_text(monkeypatch):
    from langchain_core.messages import AIMessage
    from langchain_core.tools import tool as lc_tool
    from langgraph.runtime import DEFAULT_RUNTIME
    from langgraph._internal._constants import CONFIG_KEY_RUNTIME

    import nymeria.core.hook_approvals as ha
    import importlib

    dmod = importlib.import_module("nymeria.core.hooks.dispatch")
    from nymeria.vendor.react_agent.nodes import SafeToolNode

    monkeypatch.setattr(ha, "MIN_APPROVAL_WINDOW_SECONDS", 0.05)

    @lc_tool
    def echo(text: str) -> str:
        """Echo the text back."""
        return f"echo:{text}"

    dmod.reset()
    try:

        async def _hook(ctx):
            return await require_approval(ctx, {"timeout_seconds": 0.05})

        dmod.register(HookEvent.PRE_TOOL_USE, _hook, name="approve echo")
        node = SafeToolNode([echo])
        config = {
            "configurable": {
                CONFIG_KEY_RUNTIME: DEFAULT_RUNTIME,
                "thread_id": "t1",
                "user_id": "u1",
            }
        }
        msg = {
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "echo", "args": {"text": "hi"}, "id": "c1"}],
                )
            ]
        }
        out = await node.ainvoke(msg, config)
        result = out["messages"][0]
        assert result.status == "error"
        assert "did not approve" in result.content
        assert "Silence is not consent" in result.content
        assert "echo:" not in result.content  # the tool never ran
    finally:
        dmod.reset()
