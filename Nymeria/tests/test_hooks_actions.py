"""Unit tests for the ``inject_context`` canned action (core/hooks/actions.py).

Pure action logic: given a HookContext and params, the right outcome family (or
None) comes back, and templating renders from the context.
"""

from __future__ import annotations

from nymeria.core.hooks import HookContext, HookEvent
from nymeria.core.hooks.actions import (
    ACTION_PLANES,
    ACTIONS,
    block_if_matches,
    inject_context,
    rewrite_arg,
)
from nymeria.core.hooks.base import (
    DoneOutcome,
    HookProvenance,
    PostToolOutcome,
    PreToolOutcome,
    PromptOutcome,
)


def _ctx(event: HookEvent, **kw) -> HookContext:
    base = dict(event=event, thread_id="t1", user_id="u1", is_autonomous=False)
    base.update(kw)
    return HookContext(**base)


def test_action_table_exposes_inject_context():
    assert ACTIONS["inject_context"] is inject_context


def test_prompt_submit_returns_prompt_outcome():
    out = inject_context(_ctx(HookEvent.PROMPT_SUBMIT), {"text": "remember X"})
    assert isinstance(out, PromptOutcome)
    assert out.inject_context == "remember X"


def test_post_tool_use_returns_post_outcome_additional_context():
    out = inject_context(
        _ctx(HookEvent.POST_TOOL_USE, tool_name="Edit", tool_result_text="ok"),
        {"text": "run tests"},
    )
    assert isinstance(out, PostToolOutcome)
    assert out.additional_context == "run tests"
    # It appends, never rewrites the result.
    assert out.updated_result_text is None


def test_done_returns_continue_with_reason():
    out = inject_context(_ctx(HookEvent.DONE, final_text="done"), {"text": "check the build"})
    assert isinstance(out, DoneOutcome)
    assert out.continue_ is True
    assert out.reason == "check the build"


def test_done_is_one_shot_not_a_loop():
    # First DONE (not yet in a continuation) continues once...
    first = inject_context(
        _ctx(HookEvent.DONE, provenance=HookProvenance(done_continuation_active=False)),
        {"text": "run checks"},
    )
    assert isinstance(first, DoneOutcome)
    assert first.continue_ is True
    # ...but on the continuation turn it spawned, it must NOT continue again,
    # or the turn rides the hard cap (8x) instead of firing exactly once.
    again = inject_context(
        _ctx(
            HookEvent.DONE,
            provenance=HookProvenance(done_continuation_active=True, continuation_depth=1),
        ),
        {"text": "run checks"},
    )
    assert again is None


def test_pre_tool_use_is_not_an_injection_target():
    out = inject_context(_ctx(HookEvent.PRE_TOOL_USE, tool_name="Bash"), {"text": "x"})
    assert out is None


def test_empty_or_whitespace_text_returns_none():
    assert inject_context(_ctx(HookEvent.PROMPT_SUBMIT), {"text": ""}) is None
    assert inject_context(_ctx(HookEvent.PROMPT_SUBMIT), {"text": "   "}) is None
    assert inject_context(_ctx(HookEvent.PROMPT_SUBMIT), {}) is None
    assert inject_context(_ctx(HookEvent.PROMPT_SUBMIT), None) is None


def test_templating_interpolates_context_fields():
    out = inject_context(
        _ctx(
            HookEvent.POST_TOOL_USE,
            tool_name="Write",
            tool_status="success",
            tool_result_text="wrote file",
        ),
        {"text": "{tool_name} finished with {tool_status}"},
    )
    assert isinstance(out, PostToolOutcome)
    assert out.additional_context == "Write finished with success"


def test_unknown_placeholder_is_left_verbatim():
    out = inject_context(_ctx(HookEvent.PROMPT_SUBMIT), {"text": "hi {nonexistent}"})
    assert isinstance(out, PromptOutcome)
    assert out.inject_context == "hi {nonexistent}"


def test_static_text_passes_through_unchanged():
    out = inject_context(_ctx(HookEvent.PROMPT_SUBMIT), {"text": "no braces here"})
    assert out.inject_context == "no braces here"


def test_tool_args_rendered_as_json():
    out = inject_context(
        _ctx(HookEvent.POST_TOOL_USE, tool_name="Edit", tool_args={"file": "a.py"}),
        {"text": "args={tool_args}"},
    )
    assert out.additional_context == 'args={"file": "a.py"}'


def test_missing_field_renders_empty_not_placeholder():
    # final_text is unset on a PROMPT_SUBMIT context -> renders empty, not "{final_text}".
    out = inject_context(_ctx(HookEvent.PROMPT_SUBMIT), {"text": "final=[{final_text}]"})
    assert out.inject_context == "final=[]"


# --- block_if_matches (PRE guardrail) ---------------------------------------

def _pre(**kw):
    return _ctx(HookEvent.PRE_TOOL_USE, **kw)


def test_action_table_and_planes():
    assert ACTIONS["block_if_matches"] is block_if_matches
    assert ACTIONS["rewrite_arg"] is rewrite_arg
    assert ACTION_PLANES["block_if_matches"] == "mutate"
    assert ACTION_PLANES["rewrite_arg"] == "mutate"


def test_block_denies_when_conditions_met():
    out = block_if_matches(
        _pre(tool_name="bash", tool_args={"command": "rm -rf /"}),
        {"conditions": [{"field": "command", "operator": "contains", "value": "rm -rf"}],
         "reason": "no {tool_name}"},
    )
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "deny"
    assert out.reason == "no bash"  # reason is templated


def test_block_allows_when_conditions_not_met():
    out = block_if_matches(
        _pre(tool_name="bash", tool_args={"command": "ls"}),
        {"conditions": [{"field": "command", "operator": "contains", "value": "rm -rf"}]},
    )
    assert out is None


def test_block_empty_conditions_always_denies():
    out = block_if_matches(_pre(tool_name="bash", tool_args={"command": "ls"}), {"conditions": []})
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "deny"
    assert out.reason == "blocked by a lifecycle hook"  # default reason


def test_block_never_raises_on_bad_params():
    # A malformed condition must make the hook a no-op (allow), not raise (which
    # the dispatcher would treat as a fail-closed deny of every call).
    assert block_if_matches(_pre(tool_name="bash", tool_args={"command": "x"}),
                            {"conditions": [{"operator": "??"}]}) is None
    # A non-dict condition element (bad shape) also no-ops rather than raising.
    assert block_if_matches(_pre(tool_name="bash", tool_args={"command": "x"}),
                            {"conditions": ["not a dict"]}) is None
    assert block_if_matches(_pre(tool_name="bash", tool_args={"command": "x"}),
                            {"conditions": "not a list"}) is None
    assert block_if_matches(_pre(tool_name="bash"), None) is not None  # None params -> always deny


def test_rewrite_never_raises_on_bad_conditions():
    assert rewrite_arg(_pre(tool_name="bash", tool_args={"command": "x"}),
                       {"conditions": [42], "updates": {"command": "y"}}) is None


# --- rewrite_arg (PRE modify) -----------------------------------------------

def test_rewrite_modifies_when_conditions_met():
    out = rewrite_arg(
        _pre(tool_name="bash", tool_args={"command": "whoami"}),
        {"conditions": [], "updates": {"command": "echo {tool_name}"}},
    )
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "modify"
    assert out.updated_args == {"command": "echo bash"}


def test_rewrite_noop_when_conditions_unmet():
    out = rewrite_arg(
        _pre(tool_name="bash", tool_args={"command": "ls"}),
        {"conditions": [{"field": "command", "operator": "contains", "value": "rm"}],
         "updates": {"command": "echo safe"}},
    )
    assert out is None


def test_rewrite_empty_updates_returns_none():
    out = rewrite_arg(_pre(tool_name="bash", tool_args={"command": "ls"}), {"updates": {}})
    assert out is None


def test_rewrite_never_raises_on_bad_params():
    assert rewrite_arg(_pre(tool_name="bash"), {"updates": "not a dict"}) is None
    assert rewrite_arg(_pre(tool_name="bash"), None) is None
