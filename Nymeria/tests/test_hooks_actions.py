"""Unit tests for the ``inject_context`` canned action (core/hooks/actions.py).

Pure action logic: given a HookContext and params, the right outcome family (or
None) comes back, and templating renders from the context.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from nymeria.core.hooks import HookContext, HookEvent
from nymeria.core.hooks.actions import (
    ACTION_PLANES,
    ACTIONS,
    block_if_matches,
    create_todo,
    inject_context,
    notify,
    rewrite_arg,
    webhook,
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


# --- observe-plane actions (notify / create_todo / webhook) -----------------

def _done(**kw):
    return _ctx(HookEvent.DONE, **kw)


def test_observe_planes_registered():
    assert ACTIONS["notify"] is notify
    assert ACTIONS["create_todo"] is create_todo
    assert ACTIONS["webhook"] is webhook
    for a in ("notify", "create_todo", "webhook"):
        assert ACTION_PLANES[a] == "observe"


def test_notify_delivers_templated_and_returns_none():
    ctx = _done(final_text="all good")
    with patch("nymeria.core.notifications.create_notification") as cn, \
         patch("nymeria.config.get_settings") as gs:
        gs.return_value = MagicMock(fcm_enabled=False)
        out = notify(ctx, {"text": "finished: {final_text}"})
    assert out is None  # observe plane
    assert cn.call_args.kwargs["summary"] == "finished: all good"
    assert cn.call_args.kwargs["user_id"] == "u1"


def test_notify_sends_push_when_fcm_enabled():
    ctx = _done(final_text="ok")
    with patch("nymeria.core.notifications.create_notification"), \
         patch("nymeria.config.get_settings") as gs, \
         patch("nymeria.core.fcm.send_to_all_devices") as fcm:
        gs.return_value = MagicMock(fcm_enabled=True, data_dir="/tmp/x")
        notify(ctx, {"text": "hi"})
    assert fcm.called
    assert fcm.call_args.kwargs["text"] == "hi"


def test_notify_empty_text_is_noop():
    with patch("nymeria.core.notifications.create_notification") as cn:
        assert notify(_done(), {"text": "  "}) is None
        assert notify(_done(), {}) is None
    assert not cn.called


def test_notify_never_raises_on_infra_failure():
    with patch("nymeria.core.notifications.create_notification", side_effect=RuntimeError("boom")), \
         patch("nymeria.config.get_settings") as gs:
        gs.return_value = MagicMock(fcm_enabled=False)
        assert notify(_done(), {"text": "hi"}) is None  # swallowed, not raised


def test_create_todo_adds_templated_task():
    ctx = _done(final_text="thing")
    with patch("nymeria.tools.todo._get_todo_manager") as gm:
        todo_list = MagicMock()
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=todo_list)
        cm.__exit__ = MagicMock(return_value=False)
        gm.return_value.atomic_update.return_value = cm
        out = create_todo(ctx, {"text": "follow up on {final_text}"})
    assert out is None
    assert todo_list.add_item.call_args.kwargs == {
        "task": "follow up on thing", "created_by": "hook", "thread_id": "t1"
    }


def test_create_todo_never_raises():
    with patch("nymeria.tools.todo._get_todo_manager", side_effect=RuntimeError("x")):
        assert create_todo(_done(), {"text": "t"}) is None


def test_webhook_posts_via_egress_policy():
    ctx = _ctx(HookEvent.POST_TOOL_USE, tool_name="Edit")
    with patch("nymeria.core.http_policy.httpx_request_with_policy") as hp:
        hp.return_value = (MagicMock(), [], MagicMock())
        out = webhook(ctx, {"url": "https://example.com/{tool_name}", "text": "ran {tool_name}"})
    assert out is None
    args, kwargs = hp.call_args.args, hp.call_args.kwargs
    assert args[0] == "POST"
    assert args[1] == "https://example.com/Edit"  # url templated
    assert kwargs["json"]["text"] == "ran Edit"


def test_webhook_empty_url_is_noop():
    with patch("nymeria.core.http_policy.httpx_request_with_policy") as hp:
        assert webhook(_done(), {"url": "", "text": "x"}) is None
        assert webhook(_done(), {"text": "x"}) is None
    assert not hp.called


def test_webhook_never_raises_on_failure():
    # A blocked/private URL raises HTTPPolicyViolation; any request failure must
    # be swallowed (observe never raises into a turn).
    with patch("nymeria.core.http_policy.httpx_request_with_policy",
               side_effect=RuntimeError("blocked by egress policy")):
        assert webhook(_done(), {"url": "http://169.254.169.254/", "text": "x"}) is None


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


# --- run_command (subprocess pathfinder) ------------------------------------

import contextlib  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402

from nymeria.core.hooks.actions import run_command  # noqa: E402


def _prompt(**kw):
    return _ctx(HookEvent.PROMPT_SUBMIT, **kw)


def _post(**kw):
    return _ctx(HookEvent.POST_TOOL_USE, **kw)


def _run_settings(enabled=True):
    return MagicMock(hooks_run_command_enabled=enabled, data_dir="/tmp")


@contextlib.contextmanager
def _patch_run_settings(enabled=True, *, owner_is_admin=True):
    # run_command imports get_settings twice (gate + cwd resolution) and
    # is_admin once (owner re-check). Patch all at their import sources. The
    # owner defaults to admin so the existing run_command tests exercise
    # execution; the gate-symmetry tests pass owner_is_admin=False.
    with patch("nymeria.config.get_settings", return_value=_run_settings(enabled)), \
            patch("nymeria.tools.utils.is_admin", return_value=owner_is_admin):
        yield


def test_run_command_gate_off_returns_none():
    with patch("nymeria.config.get_settings",
               return_value=MagicMock(hooks_run_command_enabled=False)):
        assert run_command(_prompt(), {"command": "echo hi"}) is None


def test_run_command_non_admin_owner_neutered():
    # Gate-symmetry: the admin authoring gate is bypassable by a direct write to
    # data/hooks/<user_id>.json, so run_command re-checks the owner's admin role
    # at fire time. A non-admin owner neuters the hook (no-op), even with the
    # deployment flag on.
    with _patch_run_settings(owner_is_admin=False):
        assert run_command(_prompt(), {"command": "printf 'hello'"}) is None


def test_run_command_non_admin_owner_pre_tool_allows_not_denies():
    # The neuter must ALLOW on pre_tool_use (return None), never deny: a
    # non-admin's planted guardrail must not be able to block tool calls.
    with _patch_run_settings(owner_is_admin=False):
        out = run_command(
            _pre(tool_name="bash"),
            {"command": "echo 'blocked' 1>&2; exit 2"},
        )
    assert out is None


def test_run_command_admin_owner_runs():
    # Positive control: an admin owner still executes.
    with _patch_run_settings(owner_is_admin=True):
        out = run_command(_prompt(), {"command": "printf 'ok'"})
    assert isinstance(out, PromptOutcome)
    assert out.inject_context == "ok"


def test_run_command_empty_command_is_noop():
    with _patch_run_settings():
        assert run_command(_prompt(), {"command": "  "}) is None


def test_run_command_prompt_submit_injects_stdout():
    with _patch_run_settings():
        out = run_command(_prompt(), {"command": "printf 'hello world'"})
    assert isinstance(out, PromptOutcome)
    assert out.inject_context == "hello world"


def test_run_command_prompt_submit_fails_open_on_nonzero():
    with _patch_run_settings():
        out = run_command(_prompt(), {"command": "echo nope; exit 1"})
    assert out is None  # injection is an enhancement, so failure is silent


def test_run_command_receives_context_json_on_stdin():
    # The command reads stdin and echoes back a field, proving the payload wiring.
    script = f"{sys.executable} -c \"import sys,json; print(json.load(sys.stdin)['thread_id'])\""
    with _patch_run_settings():
        out = run_command(_prompt(thread_id="thread-xyz"), {"command": script})
    assert isinstance(out, PromptOutcome)
    assert out.inject_context == "thread-xyz"


def test_run_command_env_is_minimal_no_secret_leak():
    # A secret in the parent env must NOT reach the child (minimal env only).
    import os
    os.environ["NYMERIA_SECRET_PROBE"] = "leaked"
    try:
        script = (
            f"{sys.executable} -c \"import os; "
            "print(os.environ.get('NYMERIA_SECRET_PROBE','ABSENT'))\""
        )
        with _patch_run_settings():
            out = run_command(_prompt(), {"command": script})
        assert isinstance(out, PromptOutcome)
        assert out.inject_context == "ABSENT"
    finally:
        del os.environ["NYMERIA_SECRET_PROBE"]


def test_run_command_pre_allows_on_exit0_empty():
    with _patch_run_settings():
        out = run_command(_pre(tool_name="bash"), {"command": "true"})
    assert out is None  # allow


def test_run_command_pre_denies_on_exit2():
    with _patch_run_settings():
        out = run_command(
            _pre(tool_name="bash"),
            {"command": "echo 'no rm allowed' 1>&2; exit 2"},
        )
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "deny"
    assert "no rm allowed" in out.reason


def test_run_command_pre_json_deny():
    payload = json.dumps({"decision": "deny", "reason": "policy says no"})
    with _patch_run_settings():
        out = run_command(_pre(tool_name="bash"), {"command": f"printf %s '{payload}'"})
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "deny"
    assert out.reason == "policy says no"


def test_run_command_pre_json_modify():
    payload = json.dumps({"decision": "modify", "updated_args": {"command": "ls -la"}})
    with _patch_run_settings():
        out = run_command(_pre(tool_name="bash"), {"command": f"printf %s '{payload}'"})
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "modify"
    assert out.updated_args == {"command": "ls -la"}


def test_run_command_pre_other_nonzero_is_nonblocking():
    with _patch_run_settings():
        out = run_command(_pre(tool_name="bash"), {"command": "exit 1"})
    assert out is None  # a script bug is non-blocking (mirrors Claude Code)


def test_run_command_pre_timeout_fails_closed():
    with _patch_run_settings():
        out = run_command(
            _pre(tool_name="bash"),
            {"command": "sleep 5", "timeout_seconds": 1},
        )
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "deny"
    assert "timed out" in out.reason


def test_run_command_observe_returns_none():
    with _patch_run_settings():
        assert run_command(_post(tool_name="bash"), {"command": "echo side effect"}) is None
