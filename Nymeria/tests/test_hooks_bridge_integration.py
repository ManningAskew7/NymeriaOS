"""Integration tests: stored hook definitions -> per-turn registry -> dispatch.

Exercises the real seams between the persisted product surface
(``HookManager`` + ``ThreadConfig``) and the store-agnostic engine, via a light
stub that binds only the four ``NymeriaAgent`` methods involved (no full agent
construction). The headline case is the DONE-continuation gotcha regression:
the production ``default_registry`` is empty, so a per-turn DONE hook must fire
off the per-turn registry, not ``default_registry``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from nymeria.core.agent import NymeriaAgent
from nymeria.core.hook_manager import HookManager
from nymeria.core.hooks import (
    HookContext,
    HookEvent,
    default_registry,
    dispatch,
    dispatch_observe,
)
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager


class _StubAgent:
    """Binds just the hook-path methods; no checkpointer/graph/etc."""

    MAX_DONE_CONTINUATIONS = NymeriaAgent.MAX_DONE_CONTINUATIONS
    _done_context = NymeriaAgent._done_context
    # staticmethod on the real class; re-wrap so `self.` access does not bind self.
    _resolve_done_continuation = staticmethod(NymeriaAgent._resolve_done_continuation)
    _done_continuation_ctx = NymeriaAgent._done_continuation_ctx
    _continuation_prompt_from_outcome = NymeriaAgent._continuation_prompt_from_outcome
    _deliver_hook_user_message = NymeriaAgent._deliver_hook_user_message
    _maybe_done_continuation = NymeriaAgent._maybe_done_continuation
    _maybe_done_continuation_sync = NymeriaAgent._maybe_done_continuation_sync
    _hook_registry_for_turn = NymeriaAgent._hook_registry_for_turn

    def __init__(self, hook_manager, thread_config_manager, settings):
        self.hook_manager = hook_manager
        self.thread_config_manager = thread_config_manager
        self.settings = settings


@pytest.fixture
def env(tmp_path):
    hm = HookManager(tmp_path)
    tcm = ThreadConfigManager(tmp_path)
    settings = SimpleNamespace(hooks_enabled=True)
    return _StubAgent(hm, tcm, settings), hm, tcm


def _ctx(event, **kw):
    base = dict(event=event, thread_id="t1", user_id="u1", is_autonomous=False)
    base.update(kw)
    return HookContext(**base)


# --- scope + enable resolution ---------------------------------------------

def test_scope_resolution_thread_and_global(env):
    agent, hm, _tcm = env
    hm.add_hook("u1", name="g", event="prompt_submit", text="G", scope="global")
    hm.add_hook("u1", name="a", event="prompt_submit", text="A", scope="thread", thread_id="t1")
    hm.add_hook("u1", name="b", event="prompt_submit", text="B", scope="thread", thread_id="t2")

    reg_t1 = agent._hook_registry_for_turn("t1", "u1")
    reg_t2 = agent._hook_registry_for_turn("t2", "u1")
    # t1 sees global + its own; t2 sees global + its own.
    assert len(reg_t1.matching(HookEvent.PROMPT_SUBMIT, _ctx(HookEvent.PROMPT_SUBMIT))) == 2
    assert len(reg_t2.matching(HookEvent.PROMPT_SUBMIT, _ctx(HookEvent.PROMPT_SUBMIT))) == 2


def test_no_hooks_returns_none(env):
    agent, _hm, _tcm = env
    assert agent._hook_registry_for_turn("t1", "u1") is None


def test_disabled_hook_excluded(env):
    agent, hm, _tcm = env
    hm.add_hook("u1", name="off", event="done", text="x", scope="global", enabled=False)
    assert agent._hook_registry_for_turn("t1", "u1") is None


def test_master_kill_switch_disables_all(env):
    agent, hm, _tcm = env
    hm.add_hook("u1", name="on", event="done", text="x", scope="global")
    agent.settings = SimpleNamespace(hooks_enabled=False)
    assert agent._hook_registry_for_turn("t1", "u1") is None


def test_per_thread_override_disables_one_hook(env):
    agent, hm, tcm = env
    h = hm.add_hook("u1", name="on", event="done", text="x", scope="global")
    tcm.save_config(ThreadConfig(thread_id="t1", hook_overrides={h.id: False}))
    # Disabled on t1, still active on t2.
    assert agent._hook_registry_for_turn("t1", "u1") is None
    assert agent._hook_registry_for_turn("t2", "u1") is not None


# --- injection per event ----------------------------------------------------

def test_prompt_submit_injects(env):
    agent, hm, _tcm = env
    hm.add_hook("u1", name="p", event="prompt_submit", text="reminder", scope="global")
    reg = agent._hook_registry_for_turn("t1", "u1")
    outcome = dispatch(HookEvent.PROMPT_SUBMIT, _ctx(HookEvent.PROMPT_SUBMIT), registry=reg)
    assert outcome.inject_context == "reminder"


def test_post_tool_use_matcher_scoping(env):
    agent, hm, _tcm = env
    hm.add_hook(
        "u1", name="m", event="post_tool_use", text="note", scope="global", matcher="Edit|Write"
    )
    reg = agent._hook_registry_for_turn("t1", "u1")
    hit = dispatch(
        HookEvent.POST_TOOL_USE,
        _ctx(HookEvent.POST_TOOL_USE, tool_name="Edit"),
        registry=reg,
    )
    miss = dispatch(
        HookEvent.POST_TOOL_USE,
        _ctx(HookEvent.POST_TOOL_USE, tool_name="Bash"),
        registry=reg,
    )
    assert hit.additional_context == "note"
    assert miss is None


# --- PRE guardrail actions (block / rewrite) --------------------------------

def _pre_ctx(**kw):
    return _ctx(HookEvent.PRE_TOOL_USE, **kw)


def test_pre_block_denies_when_tool_and_args_match(env):
    agent, hm, _tcm = env
    hm.add_hook(
        "u1", name="guard", event="pre_tool_use", action="block_if_matches",
        params={"conditions": [{"field": "command", "operator": "contains", "value": "rm -rf"}],
                "reason": "no destructive commands"},
        matcher="bash", scope="global",
    )
    reg = agent._hook_registry_for_turn("t1", "u1")
    # Matcher (tool NAME) AND conditions (tool ARGS) must both pass to deny.
    denied = dispatch(
        HookEvent.PRE_TOOL_USE,
        _pre_ctx(tool_name="bash", tool_args={"command": "rm -rf /"}),
        registry=reg,
    )
    assert denied.decision == "deny"
    assert denied.reason == "no destructive commands"
    # Wrong tool name -> matcher excludes -> allow (no matching hook).
    assert dispatch(
        HookEvent.PRE_TOOL_USE,
        _pre_ctx(tool_name="Edit", tool_args={"command": "rm -rf /"}),
        registry=reg,
    ) is None
    # Right tool, args don't satisfy conditions -> hook returns None; as the sole
    # matching hook it reduces to None (the seam treats None as allow).
    assert dispatch(
        HookEvent.PRE_TOOL_USE,
        _pre_ctx(tool_name="bash", tool_args={"command": "ls"}),
        registry=reg,
    ) is None


def test_pre_rewrite_merges_args(env):
    agent, hm, _tcm = env
    hm.add_hook(
        "u1", name="clamp", event="pre_tool_use", action="rewrite_arg",
        params={"updates": {"command": "echo replaced"}}, matcher="bash", scope="global",
    )
    reg = agent._hook_registry_for_turn("t1", "u1")
    out = dispatch(
        HookEvent.PRE_TOOL_USE,
        _pre_ctx(tool_name="bash", tool_args={"command": "whoami", "timeout": "5"}),
        registry=reg,
    )
    assert out.decision == "modify"
    assert out.updated_args == {"command": "echo replaced"}  # only the named arg


# --- observe-plane actions via the real registry (Pass 3 slice B) -----------

def test_notify_on_done_fires_via_observe_dispatch(env):
    # A stored `notify` on `done` fires through the DONE observe path (the same
    # path agent.py uses) once the bridge registers it observe=True.
    agent, hm, _tcm = env
    hm.add_hook("u1", name="ping", event="done", action="notify",
                text="done: {final_text}", scope="global")
    reg = agent._hook_registry_for_turn("t1", "u1")
    assert reg.has_observe(HookEvent.DONE)
    ctx = _ctx(HookEvent.DONE, final_text="all good")
    with patch("nymeria.core.notifications.create_notification") as cn, \
         patch("nymeria.config.get_settings") as gs:
        gs.return_value = MagicMock(fcm_enabled=False)
        dispatch_observe(HookEvent.DONE, ctx, registry=reg)
    assert cn.call_args.kwargs["summary"] == "done: all good"


# --- DONE continuation: the gotcha regression -------------------------------

def test_done_continuation_fires_off_per_turn_registry(env):
    agent, hm, _tcm = env
    hm.add_hook("u1", name="finish", event="done", text="run the checks", scope="global")
    reg = agent._hook_registry_for_turn("t1", "u1")
    assert reg.has_mutating(HookEvent.DONE)

    prompt = asyncio.run(
        agent._maybe_done_continuation(
            thread_id="t1",
            user_id="u1",
            is_autonomous=False,
            holder_kind="interactive",
            final_text="all done",
            continuation_depth=0,
            registry=reg,
        )
    )
    assert prompt is not None
    assert prompt.message == "run the checks"
    assert prompt.source == "hook_continuation"


def test_done_gotcha_default_registry_is_empty_and_suppresses(env):
    # The production default_registry has no DONE mutating hook; without threading
    # the per-turn registry through, _maybe_done_continuation would gate to None.
    agent, hm, _tcm = env
    hm.add_hook("u1", name="finish", event="done", text="run the checks", scope="global")
    assert default_registry.has_mutating(HookEvent.DONE) is False

    # registry=None falls back to the empty default_registry -> no continuation.
    prompt = asyncio.run(
        agent._maybe_done_continuation(
            thread_id="t1",
            user_id="u1",
            is_autonomous=False,
            holder_kind="interactive",
            final_text="all done",
            continuation_depth=0,
            registry=None,
        )
    )
    assert prompt is None


def test_done_continuation_is_one_shot_across_redrive(env):
    # depth 0 (original turn) continues once; depth 1 (the continuation turn this
    # hook spawned) must NOT continue, so the turn fires exactly one follow-up
    # rather than riding the hard cap.
    agent, hm, _tcm = env
    hm.add_hook("u1", name="finish", event="done", text="run checks", scope="global")
    reg = agent._hook_registry_for_turn("t1", "u1")

    def _cont(depth):
        return asyncio.run(
            agent._maybe_done_continuation(
                thread_id="t1",
                user_id="u1",
                is_autonomous=False,
                holder_kind="interactive",
                final_text="x",
                continuation_depth=depth,
                registry=reg,
            )
        )

    assert _cont(0) is not None
    assert _cont(1) is None


def test_done_continuation_sync_is_one_shot_across_redrive(env):
    # slice D1: the sync twin re-drives through the real bridge registry exactly
    # as the async path does -- one follow-up, then the cooperative flag stops it.
    agent, hm, _tcm = env
    hm.add_hook("u1", name="finish", event="done", text="run checks", scope="global")
    reg = agent._hook_registry_for_turn("t1", "u1")

    def _cont(depth):
        return agent._maybe_done_continuation_sync(
            thread_id="t1",
            user_id="u1",
            is_autonomous=False,
            holder_kind="interactive",
            final_text="x",
            continuation_depth=depth,
            registry=reg,
        )

    p0 = _cont(0)
    assert p0 is not None
    assert p0.message == "run checks"
    assert _cont(1) is None


def test_done_continuation_respects_loop_guard_cap(env):
    agent, hm, _tcm = env
    hm.add_hook("u1", name="finish", event="done", text="again", scope="global")
    reg = agent._hook_registry_for_turn("t1", "u1")
    # At the hard cap, no further continuation is produced.
    prompt = asyncio.run(
        agent._maybe_done_continuation(
            thread_id="t1",
            user_id="u1",
            is_autonomous=False,
            holder_kind="interactive",
            final_text="x",
            continuation_depth=agent.MAX_DONE_CONTINUATIONS,
            registry=reg,
        )
    )
    assert prompt is None
