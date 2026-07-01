"""Tests for the DONE lifecycle-hook seam (observe plane in this pass).

The continue/re-drive plane and its loop guard land in a later step; this covers
the DONE context builder and the observe dispatch the normal-completion seam runs.
"""

from __future__ import annotations

import asyncio
import importlib

from nymeria.core.agent import NymeriaAgent
from nymeria.core.hooks import DoneOutcome, HookEvent

dmod = importlib.import_module("nymeria.core.hooks.dispatch")


def _bare_agent() -> NymeriaAgent:
    return NymeriaAgent.__new__(NymeriaAgent)


def test_done_context_fields():
    agent = _bare_agent()
    ctx = agent._done_context(
        thread_id="t1", user_id="u1", is_autonomous=True,
        holder_kind="ticker", completed_normally=True, final_text="all done",
    )
    assert ctx.event is HookEvent.DONE
    assert ctx.completed_normally is True
    assert ctx.final_text == "all done"
    assert ctx.holder_kind == "ticker"
    assert ctx.is_autonomous is True
    assert ctx.provenance.continuation_depth == 0
    assert ctx.provenance.done_continuation_active is False


def test_done_observe_sync_runs_hook():
    dmod.reset()
    seen = {}
    try:
        dmod.register(HookEvent.DONE, lambda c: seen.update(final=c.final_text), observe=True)
        agent = _bare_agent()
        ctx = agent._done_context(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", completed_normally=True, final_text="hi there",
        )
        dmod.dispatch_observe(HookEvent.DONE, ctx)
        assert seen["final"] == "hi there"
    finally:
        dmod.reset()


def test_done_observe_async_runs_hook():
    dmod.reset()
    seen = {}
    try:
        async def obs(c):
            seen["ran"] = True
        dmod.register(HookEvent.DONE, obs, observe=True)
        agent = _bare_agent()
        ctx = agent._done_context(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", completed_normally=True, final_text="x",
        )
        asyncio.run(dmod.adispatch_observe(HookEvent.DONE, ctx))
        assert seen.get("ran") is True
    finally:
        dmod.reset()


def test_done_observe_fault_isolated():
    dmod.reset()
    try:
        dmod.register(
            HookEvent.DONE,
            lambda c: (_ for _ in ()).throw(RuntimeError("x")),
            observe=True,
        )
        agent = _bare_agent()
        ctx = agent._done_context(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", completed_normally=True, final_text="x",
        )
        dmod.dispatch_observe(HookEvent.DONE, ctx)  # must not raise
    finally:
        dmod.reset()


# --------------------------------------------------------------------------- #
# DONE continue: loop-guard resolver + continuation-prompt builder
# --------------------------------------------------------------------------- #

def test_resolve_done_continuation_guard():
    agent = _bare_agent()
    r = agent._resolve_done_continuation
    assert r(DoneOutcome(continue_=True, reason="go"), 0, 8) == "go"
    assert r(DoneOutcome(continue_=True, reason="go"), 8, 8) is None   # at cap
    assert r(DoneOutcome(continue_=False, reason="go"), 0, 8) is None  # not continuing
    assert r(DoneOutcome(continue_=True, reason=None), 0, 8) is None   # no steering reason
    assert r(None, 0, 8) is None


def test_maybe_done_continuation_builds_prompt():
    dmod.reset()
    try:
        dmod.register(HookEvent.DONE, lambda c: DoneOutcome(continue_=True, reason="lint failed, fix it"))
        agent = _bare_agent()
        prompt = asyncio.run(agent._maybe_done_continuation(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", final_text="done", continuation_depth=0,
        ))
        assert prompt is not None
        assert prompt.message == "lint failed, fix it"
        assert prompt.source == "hook_continuation"
        assert prompt.is_autonomous is True
        assert prompt.user_id == "u1"
    finally:
        dmod.reset()


def test_maybe_done_continuation_none_when_no_hook():
    dmod.reset()
    try:
        agent = _bare_agent()
        prompt = asyncio.run(agent._maybe_done_continuation(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", final_text="done", continuation_depth=0,
        ))
        assert prompt is None
    finally:
        dmod.reset()


def test_maybe_done_continuation_respects_hard_cap():
    dmod.reset()
    try:
        dmod.register(HookEvent.DONE, lambda c: DoneOutcome(continue_=True, reason="again"))
        agent = _bare_agent()
        prompt = asyncio.run(agent._maybe_done_continuation(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", final_text="done",
            continuation_depth=NymeriaAgent.MAX_DONE_CONTINUATIONS,
        ))
        assert prompt is None
    finally:
        dmod.reset()


def test_maybe_done_continuation_cooperative_flag():
    """A cooperative hook sees done_continuation_active and can self-limit."""
    dmod.reset()
    seen = {}
    try:
        def hook(c):
            seen["active"] = c.provenance.done_continuation_active
            seen["depth"] = c.provenance.continuation_depth
            if c.provenance.continuation_depth == 0:
                return DoneOutcome(continue_=True, reason="one more")
            return None

        dmod.register(HookEvent.DONE, hook)
        agent = _bare_agent()
        p0 = asyncio.run(agent._maybe_done_continuation(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", final_text="", continuation_depth=0))
        assert p0 is not None
        assert seen["active"] is False

        p1 = asyncio.run(agent._maybe_done_continuation(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", final_text="", continuation_depth=1))
        assert p1 is None
        assert seen["active"] is True
        assert seen["depth"] == 1
    finally:
        dmod.reset()


def _drive_continuation_loop(agent) -> int:
    """Faithfully simulate the astream drain-loop continuation loop.

    Mirrors the wiring: call ``_maybe_done_continuation`` with the current depth,
    stop when it returns None, otherwise increment depth and go again. Returns the
    number of continuations (re-drives) that would have occurred.
    """
    depth = 0
    redrives = 0
    while True:
        prompt = asyncio.run(agent._maybe_done_continuation(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", final_text="", continuation_depth=depth,
        ))
        if prompt is None:
            break
        depth += 1
        redrives += 1
        if redrives > 100:  # test-side backstop; the guard should stop us first
            break
    return redrives


def test_continuation_loop_stops_at_hard_cap():
    """An always-continue hook is bounded by MAX_DONE_CONTINUATIONS."""
    dmod.reset()
    try:
        dmod.register(HookEvent.DONE, lambda c: DoneOutcome(continue_=True, reason="again"))
        redrives = _drive_continuation_loop(_bare_agent())
        assert redrives == NymeriaAgent.MAX_DONE_CONTINUATIONS
    finally:
        dmod.reset()


def test_continuation_loop_stops_when_hook_stops():
    """The loop ends as soon as a hook stops asking to continue."""
    dmod.reset()
    try:
        def hook(c):
            if c.provenance.continuation_depth < 3:
                return DoneOutcome(continue_=True, reason="keep going")
            return DoneOutcome(continue_=False)
        dmod.register(HookEvent.DONE, hook)
        redrives = _drive_continuation_loop(_bare_agent())
        assert redrives == 3
    finally:
        dmod.reset()
