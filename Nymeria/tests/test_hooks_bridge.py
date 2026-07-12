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


def _defn(
    id_, event, text, *, action="inject_context", matcher=None, name=None, logic=None,
    fire_conditions=None, once=False,
):
    return SimpleNamespace(
        id=id_,
        name=name or id_,
        event=event,
        matcher=matcher,
        fire_conditions=fire_conditions,
        once=once,
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


# --- Pass 4: execution-recorder threading -------------------------------------

def test_build_registry_threads_definition_id_and_recorder():
    calls = []

    def recorder(reg, ctx, **kw):
        calls.append(reg)

    registry = build_registry(
        [_defn("abc12345", "done", "hi", name="my hook")], recorder=recorder
    )
    assert registry.recorder is recorder
    regs = registry.matching(HookEvent.DONE, _ctx(HookEvent.DONE))
    assert len(regs) == 1
    assert regs[0].definition_id == "abc12345"


def test_build_registry_defaults_to_no_recorder():
    registry = build_registry([_defn("a", "done", "hi")])
    assert registry.recorder is None


# --- Pass 5: run_command per-event plane + per-registration timeout ------------

from nymeria.core.hook_manager import RunCommandLogic  # noqa: E402


def _rc_defn(id_, event, *, command="echo hi", timeout=10.0):
    return SimpleNamespace(
        id=id_, name=id_, event=event, matcher=None,
        logic=RunCommandLogic(command=command, timeout_seconds=timeout),
    )


def test_run_command_lands_on_mutate_plane_for_in_band_events():
    reg = build_registry([
        _rc_defn("a", "prompt_submit"),
        _rc_defn("b", "pre_tool_use"),
    ])
    assert reg.has_mutating(HookEvent.PROMPT_SUBMIT)
    assert reg.has_mutating(HookEvent.PRE_TOOL_USE)
    assert not reg.has_observe(HookEvent.PROMPT_SUBMIT)


def test_run_command_lands_on_observe_plane_for_after_events():
    reg = build_registry([
        _rc_defn("a", "post_tool_use"),
        _rc_defn("b", "done"),
    ])
    assert reg.has_observe(HookEvent.POST_TOOL_USE)
    assert reg.has_observe(HookEvent.DONE)
    assert not reg.has_mutating(HookEvent.POST_TOOL_USE)


def test_run_command_sets_per_registration_timeout():
    reg = build_registry([_rc_defn("a", "done", timeout=30.0)])
    regs = reg.matching(HookEvent.DONE, _ctx(HookEvent.DONE), observe=True)
    assert len(regs) == 1
    # Dispatcher budget is the author timeout + 0.5s grace (so the action's own
    # subprocess kill fires first).
    assert regs[0].timeout == 30.5


def test_non_run_command_hook_has_no_timeout_override():
    reg = build_registry([_defn("a", "done", "hi")])
    regs = reg.matching(HookEvent.DONE, _ctx(HookEvent.DONE))
    assert regs[0].timeout is None


# --- Definition-level fire gate (fire_conditions + once) ----------------------

from nymeria.core.conditions import HookCondition  # noqa: E402
from nymeria.core.hooks.bridge import fire_condition_data  # noqa: E402


def _cond(field, op, value):
    return HookCondition(field=field, operator=op, value=value)


def test_fire_conditions_gate_blocks_and_allows():
    reg = build_registry([
        _defn("a", "prompt_submit", "warned",
              fire_conditions=[_cond("prompt", "contains", "deploy")]),
    ])
    fn = reg.matching(HookEvent.PROMPT_SUBMIT, _ctx(HookEvent.PROMPT_SUBMIT))[0].fn
    assert fn(_ctx(HookEvent.PROMPT_SUBMIT, prompt="ship it")) is None
    out = fn(_ctx(HookEvent.PROMPT_SUBMIT, prompt="deploy now"))
    assert isinstance(out, PromptOutcome)
    assert out.inject_context == "warned"


def test_fire_conditions_numeric_context_gate():
    conds = [_cond("context_pct_of_trigger", "gte", "85")]
    reg = build_registry([_defn("a", "prompt_submit", "wrap up", fire_conditions=conds)])
    fn = reg.matching(HookEvent.PROMPT_SUBMIT, _ctx(HookEvent.PROMPT_SUBMIT))[0].fn
    below = _ctx(
        HookEvent.PROMPT_SUBMIT, context_tokens=160_000, compact_trigger_tokens=200_000
    )
    above = _ctx(
        HookEvent.PROMPT_SUBMIT, context_tokens=172_000, compact_trigger_tokens=200_000
    )
    unknown = _ctx(HookEvent.PROMPT_SUBMIT)  # no signal -> numeric op is a non-match
    assert fn(below) is None
    assert fn(unknown) is None
    out = fn(above)
    assert isinstance(out, PromptOutcome)
    assert out.inject_context == "wrap up"


def test_fire_conditions_match_tool_args_under_args_namespace():
    conds = [_cond("args.command", "contains", "rm -rf")]
    reg = build_registry([_defn("a", "post_tool_use", "careful", fire_conditions=conds)])
    ctx_hit = _ctx(
        HookEvent.POST_TOOL_USE, tool_name="bash", tool_args={"command": "rm -rf /"}
    )
    ctx_miss = _ctx(HookEvent.POST_TOOL_USE, tool_name="bash", tool_args={"command": "ls"})
    fn = reg.matching(HookEvent.POST_TOOL_USE, ctx_hit)[0].fn
    assert fn(ctx_miss) is None
    assert fn(ctx_hit).additional_context == "careful"


def test_malformed_fire_conditions_make_hook_noop():
    reg = build_registry([
        _defn("a", "prompt_submit", "x", fire_conditions="garbage-not-a-list"),
    ])
    fn = reg.matching(HookEvent.PROMPT_SUBMIT, _ctx(HookEvent.PROMPT_SUBMIT))[0].fn
    assert fn(_ctx(HookEvent.PROMPT_SUBMIT, prompt="anything")) is None


def test_once_fires_once_then_stays_silent_and_rearms():
    conds = [_cond("context_pct_of_trigger", "gte", "85")]
    reg = build_registry([
        _defn("ck1", "prompt_submit", "advice", fire_conditions=conds, once=True),
    ])
    fn = reg.matching(HookEvent.PROMPT_SUBMIT, _ctx(HookEvent.PROMPT_SUBMIT))[0].fn

    def ctx(tokens, scratch=None):
        return _ctx(
            HookEvent.PROMPT_SUBMIT,
            context_tokens=tokens,
            compact_trigger_tokens=200_000,
            scratch=scratch or {},
        )

    # First crossing: fires and asks for the sentinel to be set.
    out = fn(ctx(172_000))
    assert out.inject_context == "advice"
    assert out.scratch_patch == {"hook_once:ck1": True}
    # Sentinel set (as the dispatcher would): silent while still above.
    fired = {"hook_once:ck1": True}
    assert fn(ctx(180_000, scratch=fired)) is None
    # Dropped below (compaction): a state-only outcome re-arms the sentinel.
    rearm = fn(ctx(100_000, scratch=fired))
    assert isinstance(rearm, PromptOutcome)
    assert not rearm.inject_context
    assert rearm.scratch_patch == {"hook_once:ck1": False}
    # Re-armed: the next crossing fires again.
    out2 = fn(ctx(190_000, scratch={"hook_once:ck1": False}))
    assert out2.inject_context == "advice"


def test_once_without_conditions_fires_once_per_thread():
    reg = build_registry([_defn("o1", "prompt_submit", "hello once", once=True)])
    fn = reg.matching(HookEvent.PROMPT_SUBMIT, _ctx(HookEvent.PROMPT_SUBMIT))[0].fn
    out = fn(_ctx(HookEvent.PROMPT_SUBMIT))
    assert out.inject_context == "hello once"
    assert out.scratch_patch == {"hook_once:o1": True}
    assert fn(_ctx(HookEvent.PROMPT_SUBMIT, scratch={"hook_once:o1": True})) is None


def test_once_stamps_sentinel_even_when_logic_noops():
    # The gate fired but the logic returned None (e.g. empty template): the
    # sentinel must still set via a no-op outcome, or the hook re-fires forever.
    logic = SimpleNamespace(
        action="inject_context", model_dump=lambda **kw: {"text": "{holder_kind}"}
    )
    reg = build_registry([_defn("n1", "prompt_submit", None, logic=logic, once=True)])
    fn = reg.matching(HookEvent.PROMPT_SUBMIT, _ctx(HookEvent.PROMPT_SUBMIT))[0].fn
    out = fn(_ctx(HookEvent.PROMPT_SUBMIT))  # holder_kind None -> renders empty -> None
    assert isinstance(out, PromptOutcome)
    assert not out.inject_context
    assert out.scratch_patch == {"hook_once:n1": True}


def test_fire_condition_data_shapes():
    ctx = _ctx(
        HookEvent.POST_TOOL_USE,
        tool_name="bash",
        tool_status="success",
        tool_args={"command": "ls"},
        context_tokens=150_000,
        context_limit=400_000,
        compact_trigger_tokens=200_000,
    )
    data = fire_condition_data(ctx)
    assert data["event"] == "post_tool_use"
    assert data["tool_name"] == "bash"
    assert data["args"] == {"command": "ls"}
    assert data["context_tokens"] == 150_000
    assert data["context_pct_of_trigger"] == 75.0
    assert data["context_pct_of_limit"] == 37.5
    # Unknown signal fields are ABSENT (numeric ops must not compare vs 0).
    bare = fire_condition_data(_ctx(HookEvent.PROMPT_SUBMIT))
    assert "context_tokens" not in bare
    assert "context_pct_of_trigger" not in bare


def test_gateless_definitions_take_the_ungated_path():
    # No fire_conditions and once=False: the registered fn is the bare action
    # closure (the hot path is unchanged).
    reg = build_registry([_defn("a", "prompt_submit", "hi")])
    fn = reg.matching(HookEvent.PROMPT_SUBMIT, _ctx(HookEvent.PROMPT_SUBMIT))[0].fn
    out = fn(_ctx(HookEvent.PROMPT_SUBMIT))
    assert out.inject_context == "hi"
    assert out.scratch_patch is None


def test_fire_conditions_holder_kind_gates_dream_turns():
    """Dream turns carry holder_kind="dream", so hooks can target or exclude them.

    Dream turns fire hooks normally (guardrails included); the fire gate is how
    an author opts a noisy hook out of dreams, or scopes one to dreams only.
    """
    dream_ctx = _ctx(
        HookEvent.PROMPT_SUBMIT,
        is_autonomous=True,
        holder_kind="dream",
        trigger_label='Dream("parent-1")',
    )
    user_ctx = _ctx(HookEvent.PROMPT_SUBMIT, holder_kind="user")

    only_dreams = [_cond("holder_kind", "equals", "dream")]
    reg = build_registry(
        [_defn("d1", "prompt_submit", "dream advice", fire_conditions=only_dreams)]
    )
    fn = reg.matching(HookEvent.PROMPT_SUBMIT, dream_ctx)[0].fn
    assert fn(user_ctx) is None
    assert fn(dream_ctx).inject_context == "dream advice"

    skip_dreams = [_cond("holder_kind", "not_equals", "dream")]
    reg2 = build_registry(
        [_defn("d2", "prompt_submit", "not for dreams", fire_conditions=skip_dreams)]
    )
    fn2 = reg2.matching(HookEvent.PROMPT_SUBMIT, dream_ctx)[0].fn
    assert fn2(dream_ctx) is None
    assert fn2(user_ctx).inject_context == "not for dreams"
