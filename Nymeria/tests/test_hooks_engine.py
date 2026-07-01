"""Unit tests for the lifecycle-hooks engine spine (contract, registry, scratch,
dispatch). Pure engine: no agent, no live seams. Fixtures are plain callables.

Async dispatch is exercised via ``asyncio.run`` so these tests do not depend on
pytest-asyncio configuration.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from nymeria.core.hooks import (
    DoneOutcome,
    HookContext,
    HookEvent,
    HookRegistry,
    PostToolOutcome,
    PreToolOutcome,
    PromptOutcome,
    ScratchStore,
)
import importlib

dmod = importlib.import_module("nymeria.core.hooks.dispatch")


def ctx(event: HookEvent, **kw) -> HookContext:
    base = dict(event=event, thread_id="t1", user_id="u1", is_autonomous=False)
    base.update(kw)
    return HookContext(**base)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def reg():
    return HookRegistry()


@pytest.fixture
def scratch():
    return ScratchStore()


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def test_registry_matching_registration_order(reg):
    order = []
    for i in range(3):
        reg.register(HookEvent.DONE, lambda c, i=i: order.append(i))
    got = reg.matching(HookEvent.DONE, ctx(HookEvent.DONE))
    assert [r.id for r in got] == sorted(r.id for r in got)
    assert len(got) == 3


def test_registry_event_filter(reg):
    reg.register(HookEvent.DONE, lambda c: None)
    reg.register(HookEvent.PROMPT_SUBMIT, lambda c: None)
    assert len(reg.matching(HookEvent.DONE, ctx(HookEvent.DONE))) == 1
    assert len(reg.matching(HookEvent.PROMPT_SUBMIT, ctx(HookEvent.PROMPT_SUBMIT))) == 1


def test_registry_matcher_pipe_list(reg):
    reg.register(HookEvent.PRE_TOOL_USE, lambda c: None, matcher="Edit|Write")
    assert len(reg.matching(HookEvent.PRE_TOOL_USE, ctx(HookEvent.PRE_TOOL_USE, tool_name="Edit"))) == 1
    assert len(reg.matching(HookEvent.PRE_TOOL_USE, ctx(HookEvent.PRE_TOOL_USE, tool_name="Read"))) == 0
    # None tool_name never matches a set matcher
    assert len(reg.matching(HookEvent.PRE_TOOL_USE, ctx(HookEvent.PRE_TOOL_USE))) == 0


def test_registry_observe_filter(reg):
    reg.register(HookEvent.DONE, lambda c: None, observe=False)
    reg.register(HookEvent.DONE, lambda c: None, observe=True)
    assert len(reg.matching(HookEvent.DONE, ctx(HookEvent.DONE), observe=False)) == 1
    assert len(reg.matching(HookEvent.DONE, ctx(HookEvent.DONE), observe=True)) == 1
    assert len(reg.matching(HookEvent.DONE, ctx(HookEvent.DONE))) == 2


def test_registry_has_mutating(reg):
    assert not reg.has_mutating(HookEvent.PRE_TOOL_USE)
    reg.register(HookEvent.PRE_TOOL_USE, lambda c: None, observe=True)
    assert not reg.has_mutating(HookEvent.PRE_TOOL_USE)
    reg.register(HookEvent.PRE_TOOL_USE, lambda c: None, observe=False)
    assert reg.has_mutating(HookEvent.PRE_TOOL_USE)


def test_registry_has_observe(reg):
    assert not reg.has_observe(HookEvent.POST_TOOL_USE)
    reg.register(HookEvent.POST_TOOL_USE, lambda c: None, observe=False)
    assert not reg.has_observe(HookEvent.POST_TOOL_USE)  # mutate-only doesn't count
    reg.register(HookEvent.POST_TOOL_USE, lambda c: None, observe=True)
    assert reg.has_observe(HookEvent.POST_TOOL_USE)


def test_tool_hooks_active_includes_observe(reg):
    from nymeria.core.hooks import tool_hooks_active
    assert not tool_hooks_active(reg)
    # An observe-only POST tool hook must activate the seam (else it never fires).
    reg.register(HookEvent.POST_TOOL_USE, lambda c: None, observe=True)
    assert tool_hooks_active(reg)


def test_registry_unregister(reg):
    h = reg.register(HookEvent.DONE, lambda c: None)
    assert reg.unregister(h) is True
    assert reg.unregister(h) is False
    assert reg.matching(HookEvent.DONE, ctx(HookEvent.DONE)) == []


# --------------------------------------------------------------------------- #
# Scratch
# --------------------------------------------------------------------------- #

def test_scratch_snapshot_is_readonly_and_isolated(scratch):
    scratch.apply_patch("t1", {"a": 1})
    snap = scratch.snapshot("t1")
    assert snap["a"] == 1
    with pytest.raises(TypeError):
        snap["b"] = 2  # MappingProxyType is read-only
    # thread isolation
    assert scratch.snapshot("t2") == {}


def test_scratch_patch_merges(scratch):
    scratch.apply_patch("t1", {"a": 1})
    scratch.apply_patch("t1", {"b": 2})
    snap = scratch.snapshot("t1")
    assert dict(snap) == {"a": 1, "b": 2}


def test_scratch_snapshot_deep_isolated(scratch):
    # Mutating a nested value in a snapshot must not corrupt the shared store
    # (snapshots cross the event loop AND threadpool workers).
    scratch.apply_patch("t1", {"edited": ["a.py"]})
    snap = scratch.snapshot("t1")
    snap["edited"].append("b.py")
    assert scratch.snapshot("t1")["edited"] == ["a.py"]


def test_scratch_apply_patch_no_alias(scratch):
    # The store must not alias a mutable value owned by a hook's outcome.
    payload = {"edited": ["a.py"]}
    scratch.apply_patch("t1", payload)
    payload["edited"].append("b.py")
    assert scratch.snapshot("t1")["edited"] == ["a.py"]


class _Uncopyable:
    """A value whose deep copy always fails, e.g. a live handle a hook stashed."""

    def __deepcopy__(self, memo):
        raise RuntimeError("not copyable")


def test_scratch_uncopyable_value_dropped_not_raised(scratch):
    # An un-deep-copyable value must be dropped on write (not raise), and a later
    # snapshot must not raise either. The copyable sibling key survives.
    scratch.apply_patch("t1", {"bad": _Uncopyable(), "ok": 1})
    snap = scratch.snapshot("t1")  # must not raise
    assert "bad" not in snap
    assert snap["ok"] == 1


def test_scratch_thread_eviction():
    s = ScratchStore(max_threads=2)
    s.apply_patch("t1", {"x": 1})
    s.apply_patch("t2", {"x": 1})
    s.apply_patch("t3", {"x": 1})  # evicts t1
    assert s.snapshot("t1") == {}
    assert s.snapshot("t3")["x"] == 1


def test_scratch_key_bound():
    s = ScratchStore(max_keys_per_thread=2)
    s.apply_patch("t1", {"a": 1, "b": 2, "c": 3})
    snap = s.snapshot("t1")
    assert len(snap) == 2
    assert "c" in snap  # newest kept


# --------------------------------------------------------------------------- #
# Dispatch: reduction
# --------------------------------------------------------------------------- #

def test_prompt_submit_concatenates(reg, scratch):
    reg.register(HookEvent.PROMPT_SUBMIT, lambda c: PromptOutcome(inject_context="one"))
    reg.register(HookEvent.PROMPT_SUBMIT, lambda c: PromptOutcome(inject_context="two"))
    reg.register(HookEvent.PROMPT_SUBMIT, lambda c: None)
    out = run(dmod.adispatch(HookEvent.PROMPT_SUBMIT, ctx(HookEvent.PROMPT_SUBMIT),
                             registry=reg, scratch=scratch))
    assert out.inject_context == "one\ntwo"


def test_pre_tool_deny_wins_and_reasons_join(reg, scratch):
    reg.register(HookEvent.PRE_TOOL_USE, lambda c: PreToolOutcome(decision="modify", updated_args={"x": 1}))
    reg.register(HookEvent.PRE_TOOL_USE, lambda c: PreToolOutcome(decision="deny", reason="r1"))
    reg.register(HookEvent.PRE_TOOL_USE, lambda c: PreToolOutcome(decision="deny", reason="r2"))
    out = run(dmod.adispatch(HookEvent.PRE_TOOL_USE, ctx(HookEvent.PRE_TOOL_USE, tool_name="Edit"),
                             registry=reg, scratch=scratch))
    assert out.decision == "deny"
    assert out.reason == "r1\nr2"


def test_pre_tool_modify_merges(reg, scratch):
    reg.register(HookEvent.PRE_TOOL_USE, lambda c: PreToolOutcome(decision="modify", updated_args={"a": 1}))
    reg.register(HookEvent.PRE_TOOL_USE, lambda c: PreToolOutcome(decision="modify", updated_args={"b": 2, "a": 9}))
    out = run(dmod.adispatch(HookEvent.PRE_TOOL_USE, ctx(HookEvent.PRE_TOOL_USE, tool_name="Edit"),
                             registry=reg, scratch=scratch))
    assert out.decision == "modify"
    assert out.updated_args == {"a": 9, "b": 2}  # later overrides earlier


def test_pre_tool_all_allow(reg, scratch):
    reg.register(HookEvent.PRE_TOOL_USE, lambda c: PreToolOutcome(decision="allow"))
    out = run(dmod.adispatch(HookEvent.PRE_TOOL_USE, ctx(HookEvent.PRE_TOOL_USE, tool_name="Edit"),
                             registry=reg, scratch=scratch))
    assert out.decision == "allow"


def test_post_tool_updated_last_wins_notes_concat(reg, scratch):
    reg.register(HookEvent.POST_TOOL_USE, lambda c: PostToolOutcome(updated_result_text="first", additional_context="n1"))
    reg.register(HookEvent.POST_TOOL_USE, lambda c: PostToolOutcome(updated_result_text="second", additional_context="n2"))
    out = run(dmod.adispatch(HookEvent.POST_TOOL_USE, ctx(HookEvent.POST_TOOL_USE, tool_name="Edit"),
                             registry=reg, scratch=scratch))
    assert out.updated_result_text == "second"
    assert out.additional_context == "n1\nn2"


def test_done_continue_or_and_reasons_concat(reg, scratch):
    reg.register(HookEvent.DONE, lambda c: DoneOutcome(continue_=True, reason="c1"))
    reg.register(HookEvent.DONE, lambda c: DoneOutcome(continue_=False, user_message="hi"))
    reg.register(HookEvent.DONE, lambda c: DoneOutcome(continue_=True, reason="c2"))
    out = run(dmod.adispatch(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg, scratch=scratch))
    assert out.continue_ is True
    assert out.reason == "c1\nc2"
    assert out.user_message == "hi"


def test_no_hooks_returns_none(reg, scratch):
    out = run(dmod.adispatch(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg, scratch=scratch))
    assert out is None


# --------------------------------------------------------------------------- #
# Dispatch: fault policy
# --------------------------------------------------------------------------- #

def test_pre_tool_raise_fails_closed(reg, scratch):
    def boom(c):
        raise RuntimeError("nope")
    reg.register(HookEvent.PRE_TOOL_USE, boom, name="boom")
    out = run(dmod.adispatch(HookEvent.PRE_TOOL_USE, ctx(HookEvent.PRE_TOOL_USE, tool_name="Edit"),
                             registry=reg, scratch=scratch))
    assert out.decision == "deny"
    assert "boom" in out.reason


def test_post_tool_raise_fails_open(reg, scratch):
    def boom(c):
        raise RuntimeError("nope")
    reg.register(HookEvent.POST_TOOL_USE, boom)
    reg.register(HookEvent.POST_TOOL_USE, lambda c: PostToolOutcome(additional_context="ok"))
    out = run(dmod.adispatch(HookEvent.POST_TOOL_USE, ctx(HookEvent.POST_TOOL_USE, tool_name="Edit"),
                             registry=reg, scratch=scratch))
    assert out.additional_context == "ok"  # surviving hook's note wins; no crash


def test_prompt_raise_fails_open(reg, scratch):
    reg.register(HookEvent.PROMPT_SUBMIT, lambda c: (_ for _ in ()).throw(RuntimeError("x")))
    reg.register(HookEvent.PROMPT_SUBMIT, lambda c: PromptOutcome(inject_context="ok"))
    out = run(dmod.adispatch(HookEvent.PROMPT_SUBMIT, ctx(HookEvent.PROMPT_SUBMIT),
                             registry=reg, scratch=scratch))
    assert out.inject_context == "ok"


def test_illegal_outcome_dropped(reg, scratch):
    # A PROMPT_SUBMIT hook returning a PreToolOutcome is illegal for the event.
    reg.register(HookEvent.PROMPT_SUBMIT, lambda c: PreToolOutcome(decision="deny"))
    reg.register(HookEvent.PROMPT_SUBMIT, lambda c: PromptOutcome(inject_context="ok"))
    out = run(dmod.adispatch(HookEvent.PROMPT_SUBMIT, ctx(HookEvent.PROMPT_SUBMIT),
                             registry=reg, scratch=scratch))
    assert out.inject_context == "ok"


def test_malformed_scratch_patch_does_not_crash(reg, scratch):
    # A non-mapping scratch_patch must be ignored, not raise out of dispatch.
    reg.register(
        HookEvent.POST_TOOL_USE,
        lambda c: PostToolOutcome(additional_context="ok", scratch_patch=[("a", 1, 2)]),
    )
    out = run(dmod.adispatch(HookEvent.POST_TOOL_USE, ctx(HookEvent.POST_TOOL_USE, tool_name="Edit"),
                             registry=reg, scratch=scratch))
    assert out.additional_context == "ok"  # outcome still applied; bad patch dropped


def test_malformed_pre_updated_args_does_not_crash(reg, scratch):
    # A modify outcome with non-mapping updated_args is ignored (-> allow), no crash.
    reg.register(
        HookEvent.PRE_TOOL_USE,
        lambda c: PreToolOutcome(decision="modify", updated_args=[("a", 1, 2)]),
    )
    out = run(dmod.adispatch(HookEvent.PRE_TOOL_USE, ctx(HookEvent.PRE_TOOL_USE, tool_name="Edit"),
                             registry=reg, scratch=scratch))
    assert out.decision == "allow"


def test_malformed_scratch_patch_sync_parity(reg, scratch):
    # Same isolation on the sync bridge path.
    reg.register(
        HookEvent.POST_TOOL_USE,
        lambda c: PostToolOutcome(additional_context="ok", scratch_patch="not-a-dict"),
    )
    out = dmod.dispatch(HookEvent.POST_TOOL_USE, ctx(HookEvent.POST_TOOL_USE, tool_name="Edit"),
                        registry=reg, scratch=scratch)
    assert out.additional_context == "ok"


def test_pre_allow_with_uncopyable_scratch_stays_allow(reg, scratch):
    # A mapping scratch_patch carrying an un-copyable value must not raise and
    # must not escalate an allow to a deny: the bad value is simply dropped.
    reg.register(
        HookEvent.PRE_TOOL_USE,
        lambda c: PreToolOutcome(decision="allow", scratch_patch={"h": _Uncopyable()}),
    )
    out = run(dmod.adispatch(HookEvent.PRE_TOOL_USE, ctx(HookEvent.PRE_TOOL_USE, tool_name="Edit"),
                             registry=reg, scratch=scratch))
    assert out.decision == "allow"


def test_uncopyable_scratch_does_not_crash_next_event(reg, scratch):
    # End-to-end: a POST hook stashes a bad value; the DONE snapshot must not
    # raise, and the copyable key is visible next event.
    reg.register(
        HookEvent.POST_TOOL_USE,
        lambda c: PostToolOutcome(scratch_patch={"bad": _Uncopyable(), "ok": 1}),
    )
    reg.register(HookEvent.DONE, lambda c: DoneOutcome(user_message=str(c.scratch.get("ok"))))
    run(dmod.adispatch(HookEvent.POST_TOOL_USE, ctx(HookEvent.POST_TOOL_USE, tool_name="Edit"),
                       registry=reg, scratch=scratch))
    out = run(dmod.adispatch(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg, scratch=scratch))
    assert out.user_message == "1"


# --------------------------------------------------------------------------- #
# Dispatch: scratch flow + timeout + sync parity
# --------------------------------------------------------------------------- #

def test_scratch_patch_applied_and_visible_next_event(reg, scratch):
    seen = {}

    def writer(c):
        return PostToolOutcome(scratch_patch={"edited": ["a.py"]})

    def reader(c):
        seen["edited"] = c.scratch.get("edited")
        return None

    reg.register(HookEvent.POST_TOOL_USE, writer)
    reg.register(HookEvent.DONE, reader)
    run(dmod.adispatch(HookEvent.POST_TOOL_USE, ctx(HookEvent.POST_TOOL_USE, tool_name="Edit"),
                       registry=reg, scratch=scratch))
    run(dmod.adispatch(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg, scratch=scratch))
    assert seen["edited"] == ["a.py"]


def test_hook_timeout_bounded(reg, scratch):
    def slow(c):
        time.sleep(2)
        return PromptOutcome(inject_context="late")
    reg.register(HookEvent.PROMPT_SUBMIT, slow)

    async def _timed():
        start = time.monotonic()
        out = await dmod.adispatch(HookEvent.PROMPT_SUBMIT, ctx(HookEvent.PROMPT_SUBMIT),
                                   registry=reg, scratch=scratch, timeout=0.1)
        return time.monotonic() - start, out

    elapsed, out = run(_timed())
    assert elapsed < 1.5  # not the full 2s sleep
    assert out is None  # timed-out prompt hook failed open -> skipped


def test_async_hook_supported(reg, scratch):
    async def ahook(c):
        await asyncio.sleep(0)
        return PromptOutcome(inject_context="async")
    reg.register(HookEvent.PROMPT_SUBMIT, ahook)
    out = run(dmod.adispatch(HookEvent.PROMPT_SUBMIT, ctx(HookEvent.PROMPT_SUBMIT),
                             registry=reg, scratch=scratch))
    assert out.inject_context == "async"


def test_sync_dispatch_parity(reg, scratch):
    reg.register(HookEvent.PROMPT_SUBMIT, lambda c: PromptOutcome(inject_context="s1"))
    reg.register(HookEvent.PROMPT_SUBMIT, lambda c: PromptOutcome(inject_context="s2"))
    out = dmod.dispatch(HookEvent.PROMPT_SUBMIT, ctx(HookEvent.PROMPT_SUBMIT),
                        registry=reg, scratch=scratch)
    assert out.inject_context == "s1\ns2"


def test_sync_dispatch_runs_async_hook(reg, scratch):
    async def ahook(c):
        return PromptOutcome(inject_context="bridged")
    reg.register(HookEvent.PROMPT_SUBMIT, ahook)
    out = dmod.dispatch(HookEvent.PROMPT_SUBMIT, ctx(HookEvent.PROMPT_SUBMIT),
                        registry=reg, scratch=scratch)
    assert out.inject_context == "bridged"


# --------------------------------------------------------------------------- #
# Observe plane
# --------------------------------------------------------------------------- #

def test_observe_runs_and_swallows_faults(reg, scratch):
    calls = []
    reg.register(HookEvent.DONE, lambda c: calls.append("ok"), observe=True)
    reg.register(HookEvent.DONE, lambda c: (_ for _ in ()).throw(RuntimeError("x")), observe=True)
    # Should not raise despite the faulting observe hook.
    run(dmod.adispatch_observe(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg, scratch=scratch))
    assert calls == ["ok"]


def test_observe_not_run_by_mutate_dispatch(reg, scratch):
    calls = []
    reg.register(HookEvent.DONE, lambda c: calls.append("observe"), observe=True)
    out = run(dmod.adispatch(HookEvent.DONE, ctx(HookEvent.DONE), registry=reg, scratch=scratch))
    assert out is None  # observe hook not treated as a mutate outcome
    assert calls == []  # mutate dispatch skips observe-plane hooks


def test_default_register_and_reset():
    handle = dmod.register(HookEvent.DONE, lambda c: DoneOutcome(user_message="hi"))
    assert isinstance(handle, int)
    out = run(dmod.adispatch(HookEvent.DONE, ctx(HookEvent.DONE)))
    assert out.user_message == "hi"
    dmod.reset()
    assert run(dmod.adispatch(HookEvent.DONE, ctx(HookEvent.DONE))) is None
