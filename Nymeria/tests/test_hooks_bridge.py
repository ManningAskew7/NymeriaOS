"""Unit tests for the definitions -> registry bridge (core/hooks/bridge.py)."""

from __future__ import annotations

from types import SimpleNamespace

from nymeria.core.hooks import HookContext, HookEvent
from nymeria.core.hooks.base import PostToolOutcome, PromptOutcome
from nymeria.core.hooks.bridge import build_registry


def _defn(id_, event, text, *, action="inject_context", matcher=None, name=None):
    return SimpleNamespace(
        id=id_,
        name=name or id_,
        event=event,
        matcher=matcher,
        logic=SimpleNamespace(action=action, text=text),
    )


def _ctx(event, **kw):
    base = dict(event=event, thread_id="t1", user_id="u1", is_autonomous=False)
    base.update(kw)
    return HookContext(**base)


def test_build_registry_registers_one_hook_per_definition():
    reg = build_registry([
        _defn("a", "prompt_submit", "one"),
        _defn("b", "done", "two"),
    ])
    assert reg.has_mutating(HookEvent.PROMPT_SUBMIT)
    assert reg.has_mutating(HookEvent.DONE)
    assert not reg.has_mutating(HookEvent.PRE_TOOL_USE)


def test_registered_hook_produces_the_action_outcome():
    reg = build_registry([_defn("a", "prompt_submit", "hello")])
    regs = reg.matching(HookEvent.PROMPT_SUBMIT, _ctx(HookEvent.PROMPT_SUBMIT))
    assert len(regs) == 1
    out = regs[0].fn(_ctx(HookEvent.PROMPT_SUBMIT))
    assert isinstance(out, PromptOutcome)
    assert out.inject_context == "hello"


def test_registrations_are_mutate_plane():
    reg = build_registry([_defn("a", "done", "x")])
    # observe=False plane only
    assert reg.matching(HookEvent.DONE, _ctx(HookEvent.DONE), observe=True) == []
    assert len(reg.matching(HookEvent.DONE, _ctx(HookEvent.DONE), observe=False)) == 1


def test_matcher_scopes_post_tool_use():
    reg = build_registry([_defn("a", "post_tool_use", "note", matcher="Edit|Write")])
    matched = reg.matching(
        HookEvent.POST_TOOL_USE, _ctx(HookEvent.POST_TOOL_USE, tool_name="Edit")
    )
    unmatched = reg.matching(
        HookEvent.POST_TOOL_USE, _ctx(HookEvent.POST_TOOL_USE, tool_name="Bash")
    )
    assert len(matched) == 1
    assert unmatched == []


def test_per_definition_closures_do_not_late_bind():
    # Regression: each registered fn must bind its own definition, not the loop var.
    reg = build_registry([
        _defn("a", "post_tool_use", "FIRST"),
        _defn("b", "post_tool_use", "SECOND"),
    ])
    regs = reg.matching(
        HookEvent.POST_TOOL_USE, _ctx(HookEvent.POST_TOOL_USE, tool_name="Any")
    )
    texts = [r.fn(_ctx(HookEvent.POST_TOOL_USE, tool_name="Any")).additional_context for r in regs]
    assert texts == ["FIRST", "SECOND"]


def test_unknown_event_is_skipped():
    reg = build_registry([
        _defn("bad", "not_an_event", "x"),
        _defn("good", "done", "y"),
    ])
    assert reg.has_mutating(HookEvent.DONE)
    # Only the good one registered.
    assert len(reg.matching(HookEvent.DONE, _ctx(HookEvent.DONE))) == 1


def test_unknown_action_is_skipped():
    reg = build_registry([
        _defn("bad", "prompt_submit", "x", action="teleport"),
        _defn("good", "prompt_submit", "y"),
    ])
    regs = reg.matching(HookEvent.PROMPT_SUBMIT, _ctx(HookEvent.PROMPT_SUBMIT))
    assert len(regs) == 1
    assert regs[0].fn(_ctx(HookEvent.PROMPT_SUBMIT)).inject_context == "y"


def test_empty_definitions_build_inert_registry():
    reg = build_registry([])
    for event in HookEvent:
        assert not reg.has_mutating(event)


def test_post_outcome_from_registered_hook():
    reg = build_registry([_defn("a", "post_tool_use", "appended")])
    reg_match = reg.matching(
        HookEvent.POST_TOOL_USE, _ctx(HookEvent.POST_TOOL_USE, tool_name="X")
    )[0]
    out = reg_match.fn(_ctx(HookEvent.POST_TOOL_USE, tool_name="X"))
    assert isinstance(out, PostToolOutcome)
    assert out.additional_context == "appended"
