"""Unit tests for the definitions -> registry bridge (core/hooks/bridge.py)."""

from __future__ import annotations

from types import SimpleNamespace

from nymeria.core.hook_manager import (
    BlockIfMatchesLogic,
    InjectContextLogic,
    NotifyLogic,
    RewriteArgLogic,
    WebhookLogic,
)
from nymeria.core.hooks import HookContext, HookEvent
from nymeria.core.hooks.base import PostToolOutcome, PreToolOutcome, PromptOutcome
from nymeria.core.hooks.bridge import build_registry


def _logic(action, text):
    """A real logic variant (or an unknown-action fake with model_dump)."""
    if action == "inject_context":
        return InjectContextLogic(text=text)
    # Unknown action: expose ``action`` + ``model_dump`` like a real variant so
    # the bridge's skip path (unknown action) is exercised, not a dump crash.
    return SimpleNamespace(action=action, model_dump=lambda **kw: {"text": text})


def _defn(id_, event, text, *, action="inject_context", matcher=None, name=None, logic=None):
    return SimpleNamespace(
        id=id_,
        name=name or id_,
        event=event,
        matcher=matcher,
        logic=logic if logic is not None else _logic(action, text),
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


# --- Pass 3: PRE actions register on the mutate plane with full params -------

def test_block_if_matches_registers_mutate_and_passes_params():
    logic = BlockIfMatchesLogic(
        conditions=[{"field": "command", "operator": "contains", "value": "rm -rf"}],
        reason="denied",
    )
    reg = build_registry([_defn("g", "pre_tool_use", None, logic=logic, matcher="bash")])
    assert reg.has_mutating(HookEvent.PRE_TOOL_USE)
    ctx = _ctx(HookEvent.PRE_TOOL_USE, tool_name="bash", tool_args={"command": "rm -rf /"})
    reg_match = reg.matching(HookEvent.PRE_TOOL_USE, ctx)[0]
    out = reg_match.fn(ctx)
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "deny"
    assert out.reason == "denied"


def test_rewrite_arg_registers_mutate_and_passes_params():
    logic = RewriteArgLogic(updates={"command": "echo safe"})
    reg = build_registry([_defn("c", "pre_tool_use", None, logic=logic)])
    assert reg.has_mutating(HookEvent.PRE_TOOL_USE)
    ctx = _ctx(HookEvent.PRE_TOOL_USE, tool_name="bash", tool_args={"command": "whoami"})
    reg_match = reg.matching(HookEvent.PRE_TOOL_USE, ctx)[0]
    out = reg_match.fn(ctx)
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "modify"
    assert out.updated_args == {"command": "echo safe"}


def test_block_condition_not_met_allows():
    logic = BlockIfMatchesLogic(
        conditions=[{"field": "command", "operator": "contains", "value": "rm -rf"}]
    )
    reg = build_registry([_defn("g", "pre_tool_use", None, logic=logic)])
    ctx = _ctx(HookEvent.PRE_TOOL_USE, tool_name="bash", tool_args={"command": "ls"})
    out = reg.matching(HookEvent.PRE_TOOL_USE, ctx)[0].fn(ctx)
    assert out is None  # conditions not met -> allow


# --- Pass 3 slice B: observe-plane actions register observe=True -------------

def test_notify_registers_observe_plane():
    reg = build_registry([_defn("n", "done", None, logic=NotifyLogic(text="hi"))])
    assert reg.has_observe(HookEvent.DONE)
    assert not reg.has_mutating(HookEvent.DONE)
    # Only visible on the observe plane.
    assert reg.matching(HookEvent.DONE, _ctx(HookEvent.DONE), observe=False) == []
    assert len(reg.matching(HookEvent.DONE, _ctx(HookEvent.DONE), observe=True)) == 1


def test_webhook_registers_observe_and_passes_params():
    logic = WebhookLogic(url="https://x.test/h", text="body")
    reg = build_registry([_defn("w", "post_tool_use", None, logic=logic, matcher="Edit")])
    assert reg.has_observe(HookEvent.POST_TOOL_USE)
    regs = reg.matching(
        HookEvent.POST_TOOL_USE, _ctx(HookEvent.POST_TOOL_USE, tool_name="Edit"), observe=True
    )
    assert len(regs) == 1
