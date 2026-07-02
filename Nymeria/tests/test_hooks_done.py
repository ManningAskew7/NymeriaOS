"""Tests for the DONE lifecycle-hook seam (observe plane in this pass).

The continue/re-drive plane and its loop guard land in a later step; this covers
the DONE context builder and the observe dispatch the normal-completion seam runs.
"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

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


# --------------------------------------------------------------------------- #
# Sync DONE continuation twin (slice D1): the no-loop chat path re-drives too.
# --------------------------------------------------------------------------- #

def test_maybe_done_continuation_sync_builds_prompt():
    dmod.reset()
    try:
        dmod.register(HookEvent.DONE, lambda c: DoneOutcome(continue_=True, reason="rerun tests"))
        agent = _bare_agent()
        prompt = agent._maybe_done_continuation_sync(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", final_text="done", continuation_depth=0,
        )
        assert prompt is not None
        assert prompt.message == "rerun tests"
        assert prompt.source == "hook_continuation"
        assert prompt.is_autonomous is True
        assert prompt.user_id == "u1"
    finally:
        dmod.reset()


def test_maybe_done_continuation_sync_none_when_no_hook():
    dmod.reset()
    try:
        agent = _bare_agent()
        assert agent._maybe_done_continuation_sync(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", final_text="done", continuation_depth=0,
        ) is None
    finally:
        dmod.reset()


def test_maybe_done_continuation_sync_respects_hard_cap():
    dmod.reset()
    try:
        dmod.register(HookEvent.DONE, lambda c: DoneOutcome(continue_=True, reason="again"))
        agent = _bare_agent()
        assert agent._maybe_done_continuation_sync(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", final_text="done",
            continuation_depth=NymeriaAgent.MAX_DONE_CONTINUATIONS,
        ) is None
    finally:
        dmod.reset()


def test_sync_continuation_loop_stops_at_hard_cap():
    """The sync twin mirrors the async loop guard: always-continue is bounded."""
    dmod.reset()
    try:
        dmod.register(HookEvent.DONE, lambda c: DoneOutcome(continue_=True, reason="again"))
        agent = _bare_agent()
        depth = redrives = 0
        while True:
            prompt = agent._maybe_done_continuation_sync(
                thread_id="t1", user_id="u1", is_autonomous=False,
                holder_kind="user", final_text="", continuation_depth=depth,
            )
            if prompt is None:
                break
            depth += 1
            redrives += 1
            if redrives > 100:  # test-side backstop; the guard should stop us first
                break
        assert redrives == NymeriaAgent.MAX_DONE_CONTINUATIONS
    finally:
        dmod.reset()


# --------------------------------------------------------------------------- #
# DoneOutcome.user_message delivery (slice D3).
# --------------------------------------------------------------------------- #

def test_done_user_message_delivered_with_continuation():
    dmod.reset()
    delivered = []
    try:
        dmod.register(
            HookEvent.DONE,
            lambda c: DoneOutcome(continue_=True, reason="go", user_message="ping"),
        )
        agent = _bare_agent()
        agent._deliver_hook_user_message = lambda uid, tid, txt: delivered.append((uid, tid, txt))
        prompt = asyncio.run(agent._maybe_done_continuation(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", final_text="", continuation_depth=0,
        ))
        assert prompt is not None and prompt.message == "go"
        assert delivered == [("u1", "t1", "ping")]
    finally:
        dmod.reset()


def test_done_user_message_delivered_without_continuation():
    dmod.reset()
    delivered = []
    try:
        dmod.register(
            HookEvent.DONE,
            lambda c: DoneOutcome(continue_=False, user_message="fyi"),
        )
        agent = _bare_agent()
        agent._deliver_hook_user_message = lambda uid, tid, txt: delivered.append((uid, tid, txt))
        prompt = asyncio.run(agent._maybe_done_continuation(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", final_text="", continuation_depth=0,
        ))
        assert prompt is None  # no continuation asked
        assert delivered == [("u1", "t1", "fyi")]  # but the message still reaches the user
    finally:
        dmod.reset()


def test_done_user_message_delivered_via_sync_twin():
    dmod.reset()
    delivered = []
    try:
        dmod.register(
            HookEvent.DONE,
            lambda c: DoneOutcome(continue_=False, user_message="sync fyi"),
        )
        agent = _bare_agent()
        agent._deliver_hook_user_message = lambda uid, tid, txt: delivered.append((uid, tid, txt))
        prompt = agent._maybe_done_continuation_sync(
            thread_id="t9", user_id="u9", is_autonomous=True,
            holder_kind="ticker", final_text="", continuation_depth=0,
        )
        assert prompt is None
        assert delivered == [("u9", "t9", "sync fyi")]
    finally:
        dmod.reset()


def test_deliver_hook_user_message_calls_notification(monkeypatch):
    import nymeria.core.notifications as notif
    calls = []
    monkeypatch.setattr(notif, "create_notification", lambda **kw: calls.append(kw))
    agent = _bare_agent()
    agent.settings = SimpleNamespace(fcm_enabled=False, data_dir="/tmp")
    agent._deliver_hook_user_message("u1", "t1", "hello there")
    assert len(calls) == 1
    assert calls[0]["user_id"] == "u1"
    assert calls[0]["thread_id"] == "t1"
    assert calls[0]["summary"] == "hello there"


def test_deliver_hook_user_message_empty_is_noop(monkeypatch):
    import nymeria.core.notifications as notif
    calls = []
    monkeypatch.setattr(notif, "create_notification", lambda **kw: calls.append(kw))
    agent = _bare_agent()
    agent.settings = SimpleNamespace(fcm_enabled=False, data_dir="/tmp")
    agent._deliver_hook_user_message("u1", "t1", "   ")
    assert calls == []


def test_deliver_hook_user_message_never_raises(monkeypatch):
    import nymeria.core.notifications as notif
    def boom(**kw):
        raise RuntimeError("notification store down")
    monkeypatch.setattr(notif, "create_notification", boom)
    agent = _bare_agent()
    agent.settings = SimpleNamespace(fcm_enabled=False, data_dir="/tmp")
    agent._deliver_hook_user_message("u1", "t1", "boom")  # swallowed, must not raise


# --------------------------------------------------------------------------- #
# Error-path DONE observe (slice D2): the observe helpers carry completed_normally
# so a `done` notify/webhook hook can react to a failed turn, not only a clean one.
# --------------------------------------------------------------------------- #

def test_fire_done_observe_sync_marks_error():
    dmod.reset()
    seen = {}
    try:
        dmod.register(HookEvent.DONE, lambda c: seen.update(normal=c.completed_normally), observe=True)
        agent = _bare_agent()
        agent._fire_done_observe_sync(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", completed_normally=False, final_text="",
        )
        dmod.drain_observe()  # the fire point schedules off-turn now
        assert seen["normal"] is False
    finally:
        dmod.reset()


def test_fire_done_observe_sync_normal_completion():
    dmod.reset()
    seen = {}
    try:
        dmod.register(HookEvent.DONE, lambda c: seen.update(normal=c.completed_normally), observe=True)
        agent = _bare_agent()
        agent._fire_done_observe_sync(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", completed_normally=True, final_text="ok",
        )
        dmod.drain_observe()  # the fire point schedules off-turn now
        assert seen["normal"] is True
    finally:
        dmod.reset()


def test_fire_done_observe_async_marks_error():
    dmod.reset()
    seen = {}
    try:
        async def obs(c):
            seen["normal"] = c.completed_normally
        dmod.register(HookEvent.DONE, obs, observe=True)
        agent = _bare_agent()

        async def _run():
            await agent._fire_done_observe(
                thread_id="t1", user_id="u1", is_autonomous=False,
                holder_kind="user", completed_normally=False, final_text="",
            )
            await dmod.adrain_observe()  # the fire point schedules a loop task now

        asyncio.run(_run())
        assert seen["normal"] is False
    finally:
        dmod.reset()


def test_fire_done_observe_sync_never_raises():
    dmod.reset()
    try:
        dmod.register(
            HookEvent.DONE,
            lambda c: (_ for _ in ()).throw(RuntimeError("boom")),
            observe=True,
        )
        agent = _bare_agent()
        # A faulting observe hook on the error path must not re-raise into cleanup.
        agent._fire_done_observe_sync(
            thread_id="t1", user_id="u1", is_autonomous=False,
            holder_kind="user", completed_normally=False, final_text="",
        )
        dmod.drain_observe()
    finally:
        dmod.reset()


def test_hard_cancel_fires_done_observe_via_deferred_finally():
    """Faithfully simulate astream's deferred-DONE contract (pass 5, slice B).

    astream sets ``done_observe_fired`` at both in-band fire points and its
    ``finally`` fires the deferred DONE observe (completed_normally=False) when
    neither ran, which is exactly the hard-cancel path: ``aclose()`` throws
    GeneratorExit past ``except Exception``. Scheduling never awaits, so the
    fire is legal inside the closing generator. Uses the real helper + real
    scheduling under a real ``aclose()``.
    """
    dmod.reset()
    seen = []
    try:
        dmod.register(
            HookEvent.DONE, lambda c: seen.append(c.completed_normally), observe=True
        )
        agent = _bare_agent()

        def skeleton():
            async def gen():
                done_observe_fired = False
                try:
                    yield "one"
                    yield "two"
                    agent._fire_done_observe_sync(
                        thread_id="t1", user_id="u1", is_autonomous=False,
                        holder_kind="user", completed_normally=True, final_text="done",
                    )
                    done_observe_fired = True
                finally:
                    if not done_observe_fired:
                        agent._fire_done_observe_sync(
                            thread_id="t1", user_id="u1", is_autonomous=False,
                            holder_kind="user", completed_normally=False, final_text="",
                        )
            return gen()

        async def _cancelled_run():
            gen = skeleton()
            assert await gen.__anext__() == "one"
            await gen.aclose()  # GeneratorExit into the suspended yield
            await dmod.adrain_observe()

        asyncio.run(_cancelled_run())
        dmod.drain_observe()
        assert seen == [False]

        seen.clear()

        async def _normal_run():
            async for _ in skeleton():
                pass
            await dmod.adrain_observe()

        asyncio.run(_normal_run())
        dmod.drain_observe()
        # Exactly once: the finally must not double-fire after the normal tail.
        assert seen == [True]
    finally:
        dmod.reset()
