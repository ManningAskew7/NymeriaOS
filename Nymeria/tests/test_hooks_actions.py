"""Unit tests for the ``inject_context`` canned action (core/hooks/actions.py).

Pure action logic: given a HookContext and params, the right outcome family (or
None) comes back, and templating renders from the context.
"""

from __future__ import annotations

from nymeria.core.hooks import HookContext, HookEvent
from nymeria.core.hooks.actions import ACTIONS, inject_context
from nymeria.core.hooks.base import (
    DoneOutcome,
    HookProvenance,
    PostToolOutcome,
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
