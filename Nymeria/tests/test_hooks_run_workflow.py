"""Tests for the ``run_workflow`` hook action (backlog #80).

Three layers:

- Action unit tests with a stubbed ``run_workflow_by_id``: per-event outcome
  mapping, the ``on_fault`` policy, needs_approval semantics per plane, the
  ``event`` payload contract, and the wall-clock cap forwarding.
- ``tool_runtime.run_workflow_by_id`` wall-clock-cap unit tests.
- One integration test driving the REAL out-of-process engine from the hook
  action (a source returning a deny decision vetoes the tool call).

Authoring-surface coverage (manager validation chokepoint, flat-field
mapping) lives here too, next to the logic it guards.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from nymeria.core.hook_manager import (
    HookManager,
    build_update_kwargs,
    params_from_fields,
)
from nymeria.core.hooks import HookContext, HookEvent
from nymeria.core.hooks.actions import (
    ACTION_PLANES,
    ACTIONS,
    _WORKFLOW_EVENT_TEXT_CAP,
    hook_event_payload,
    run_workflow,
)
from nymeria.core.hooks.base import PreToolOutcome, PromptOutcome
from nymeria.core.hook_spec import plane_for
from nymeria.core.workflows import tool_runtime as tool_runtime_module
from nymeria.core.workflows.budget import WorkflowBudget
from nymeria.core.workflows.envelope import (
    STATUS_NEEDS_APPROVAL,
    WorkflowEnvelope,
    WorkflowError,
    ok_envelope,
)


def _ctx(event: HookEvent, **kw) -> HookContext:
    base = dict(event=event, thread_id="t1", user_id="u1", is_autonomous=False)
    base.update(kw)
    return HookContext(**base)


def _params(**kw) -> dict:
    base: dict = {"workflow_id": "wf_demo", "params": {}, "timeout_seconds": 42.0}
    base.update(kw)
    return base


def _ok(output: Any) -> WorkflowEnvelope:
    return ok_envelope(output, {"calls_used": 0, "ai_calls_used": 0, "wall_seconds": 0.1})


def _err(status: str = "error", message: str = "boom") -> WorkflowEnvelope:
    return WorkflowEnvelope(
        ok=False,
        status=status,
        error=WorkflowError(kind="author_error", message=message),
    )


def _suspended() -> WorkflowEnvelope:
    return WorkflowEnvelope(
        ok=False, status=STATUS_NEEDS_APPROVAL, output={"record_id": "r1"}
    )


def _stub_run(
    monkeypatch,
    *,
    envelope: Optional[WorkflowEnvelope] = None,
    refusal: Optional[str] = None,
    declares_event: bool = False,
    raises: Optional[BaseException] = None,
) -> list:
    """Stub the by-id execution seam; returns the recorded call list."""
    calls: list = []

    async def fake_run(loader, tool_id, params, *, user_id, thread_id, depth=0,
                       wall_clock_cap=None):
        calls.append({
            "tool_id": tool_id, "params": dict(params), "user_id": user_id,
            "thread_id": thread_id, "wall_clock_cap": wall_clock_cap,
        })
        if raises is not None:
            raise raises
        if refusal is not None:
            return refusal, None
        return None, SimpleNamespace(envelope=envelope or _ok(None))

    monkeypatch.setattr(tool_runtime_module, "run_workflow_by_id", fake_run)
    monkeypatch.setattr(
        tool_runtime_module, "workflow_declares_event", lambda _wf: declares_event
    )
    monkeypatch.setattr(
        "nymeria.core.custom_tools.get_custom_tool_loader", lambda: object()
    )
    return calls


def _fire(event: HookEvent, params: Optional[dict] = None, **ctx_kw):
    return asyncio.run(run_workflow(_ctx(event, **ctx_kw), params or _params()))


# --- taxonomy ------------------------------------------------------------------


def test_action_table_and_planes():
    assert ACTIONS["run_workflow"] is run_workflow
    # Async (the require_approval pattern): the bridge preserves coroutine-ness
    # so a long in-band run never occupies a mutate-pool worker.
    assert inspect.iscoroutinefunction(run_workflow)
    assert ACTION_PLANES["run_workflow"] == "mutate"
    assert plane_for("run_workflow", "prompt_submit") == "mutate"
    assert plane_for("run_workflow", "pre_tool_use") == "mutate"
    assert plane_for("run_workflow", "post_tool_use") == "observe"
    assert plane_for("run_workflow", "done") == "observe"


# --- prompt_submit (mutate injector) --------------------------------------------


def test_prompt_submit_injects_dict_result(monkeypatch):
    _stub_run(monkeypatch, envelope=_ok({"inject_context": "remember X"}))
    out = _fire(HookEvent.PROMPT_SUBMIT)
    assert isinstance(out, PromptOutcome)
    assert out.inject_context == "remember X"


def test_prompt_submit_injects_plain_string_result(monkeypatch):
    _stub_run(monkeypatch, envelope=_ok("  house rules  "))
    out = _fire(HookEvent.PROMPT_SUBMIT)
    assert isinstance(out, PromptOutcome)
    assert out.inject_context == "house rules"


def test_prompt_submit_empty_output_is_noop(monkeypatch):
    _stub_run(monkeypatch, envelope=_ok(None))
    assert _fire(HookEvent.PROMPT_SUBMIT) is None


def test_prompt_submit_failure_raises_for_honest_error_status(monkeypatch):
    # The dispatcher fails PROMPT open on a raise and records ``error``;
    # returning None here would log an indistinguishable ``no_op`` (#74C).
    _stub_run(monkeypatch, envelope=_err())
    with pytest.raises(RuntimeError, match="boom"):
        _fire(HookEvent.PROMPT_SUBMIT)


def test_prompt_submit_suspension_raises(monkeypatch):
    _stub_run(monkeypatch, envelope=_suspended())
    with pytest.raises(RuntimeError, match="suspended"):
        _fire(HookEvent.PROMPT_SUBMIT)


# --- pre_tool_use (mutate guardrail; never raises) -------------------------------


def test_pre_ok_empty_output_allows_as_noop(monkeypatch):
    _stub_run(monkeypatch, envelope=_ok(None))
    assert _fire(HookEvent.PRE_TOOL_USE, tool_name="bash") is None


def test_pre_deny_decision(monkeypatch):
    _stub_run(
        monkeypatch,
        envelope=_ok({"decision": "deny", "reason": "policy says no"}),
    )
    out = _fire(HookEvent.PRE_TOOL_USE, tool_name="bash")
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "deny"
    assert out.reason == "policy says no"


def test_pre_modify_decision(monkeypatch):
    _stub_run(
        monkeypatch,
        envelope=_ok({"decision": "modify", "updated_args": {"command": "ls"}}),
    )
    out = _fire(HookEvent.PRE_TOOL_USE, tool_name="bash")
    assert out.decision == "modify"
    assert out.updated_args == {"command": "ls"}


def test_pre_allow_with_note_and_scratch_patch(monkeypatch):
    _stub_run(
        monkeypatch,
        envelope=_ok({
            "decision": "allow",
            "note": "checked and fine",
            "scratch_patch": {"seen": True},
        }),
    )
    out = _fire(HookEvent.PRE_TOOL_USE, tool_name="bash")
    assert out.decision == "allow"
    assert out.note == "checked and fine"
    assert out.scratch_patch == {"seen": True}


def test_pre_fault_default_allows_with_note(monkeypatch):
    _stub_run(monkeypatch, envelope=_err())
    out = _fire(HookEvent.PRE_TOOL_USE, tool_name="bash")
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "allow"
    assert "boom" in (out.note or "")


def test_pre_fault_deny_fails_closed(monkeypatch):
    _stub_run(monkeypatch, envelope=_err())
    out = _fire(
        HookEvent.PRE_TOOL_USE, _params(on_fault="deny"), tool_name="bash"
    )
    assert out.decision == "deny"
    assert "boom" in (out.reason or "")


def test_pre_refusal_maps_to_on_fault(monkeypatch):
    _stub_run(monkeypatch, refusal="approval_required - revision not approved")
    out = _fire(
        HookEvent.PRE_TOOL_USE, _params(on_fault="deny"), tool_name="bash"
    )
    assert out.decision == "deny"
    assert "approval_required" in (out.reason or "")


def test_pre_suspension_cannot_hold_and_maps_to_on_fault(monkeypatch):
    _stub_run(monkeypatch, envelope=_suspended())
    out = _fire(HookEvent.PRE_TOOL_USE, tool_name="bash")
    assert out.decision == "allow"
    assert "suspended" in (out.note or "")


def test_pre_non_decision_result_is_a_visible_author_bug(monkeypatch):
    _stub_run(monkeypatch, envelope=_ok("yes"))
    out = _fire(HookEvent.PRE_TOOL_USE, tool_name="bash")
    assert out.decision == "allow"
    assert "non-decision" in (out.note or "")


def test_pre_exception_never_raises(monkeypatch):
    _stub_run(monkeypatch, raises=RuntimeError("infra down"))
    out = _fire(HookEvent.PRE_TOOL_USE, tool_name="bash")
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "allow"
    assert "infra down" in (out.note or "")


def test_pre_cancellation_propagates(monkeypatch):
    # Turn abort: the executor killed the child and re-raised; the action must
    # not swallow it into an allow.
    _stub_run(monkeypatch, raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        _fire(HookEvent.PRE_TOOL_USE, tool_name="bash")


def test_pre_missing_workflow_id_maps_to_on_fault(monkeypatch):
    _stub_run(monkeypatch, envelope=_ok(None))
    out = _fire(HookEvent.PRE_TOOL_USE, {"on_fault": "deny"}, tool_name="bash")
    assert out.decision == "deny"


# --- observe plane (post_tool_use / done) ----------------------------------------


def test_done_ok_returns_none(monkeypatch):
    _stub_run(monkeypatch, envelope=_ok({"anything": 1}))
    assert _fire(HookEvent.DONE, final_text="bye") is None


def test_done_suspension_counts_as_successful_fire(monkeypatch):
    _stub_run(monkeypatch, envelope=_suspended())
    assert _fire(HookEvent.DONE, final_text="bye") is None


def test_observe_failure_raises_for_error_status(monkeypatch):
    _stub_run(monkeypatch, envelope=_err(status="timeout", message="too slow"))
    with pytest.raises(RuntimeError, match="too slow"):
        _fire(HookEvent.POST_TOOL_USE, tool_name="bash", tool_status="success")


def test_done_missing_workflow_id_is_silent_noop(monkeypatch):
    _stub_run(monkeypatch, envelope=_ok(None))
    assert _fire(HookEvent.DONE, {"on_fault": "allow"}) is None


# --- call plumbing ----------------------------------------------------------------


def test_wall_clock_cap_and_identity_forwarded(monkeypatch):
    calls = _stub_run(monkeypatch, envelope=_ok(None))
    _fire(HookEvent.DONE)
    assert calls[0]["tool_id"] == "wf_demo"
    assert calls[0]["wall_clock_cap"] == 42.0
    assert calls[0]["user_id"] == "u1"
    assert calls[0]["thread_id"] == "t1"


def test_event_payload_added_when_declared(monkeypatch):
    calls = _stub_run(monkeypatch, envelope=_ok(None), declares_event=True)
    _fire(
        HookEvent.PRE_TOOL_USE,
        tool_name="bash",
        tool_call_id="c1",
        tool_args={"command": "ls"},
    )
    event = calls[0]["params"]["event"]
    assert event["event"] == "pre_tool_use"
    assert event["tool_name"] == "bash"
    assert event["tool_args"] == {"command": "ls"}
    assert event["thread_id"] == "t1" and event["user_id"] == "u1"


def test_event_payload_omitted_when_not_declared(monkeypatch):
    calls = _stub_run(monkeypatch, envelope=_ok(None), declares_event=False)
    _fire(HookEvent.DONE)
    assert "event" not in calls[0]["params"]


def test_bound_event_param_is_not_overwritten(monkeypatch):
    calls = _stub_run(monkeypatch, envelope=_ok(None), declares_event=True)
    _fire(HookEvent.DONE, _params(params={"event": {"pinned": True}}))
    assert calls[0]["params"]["event"] == {"pinned": True}


def test_hook_event_payload_caps_long_text_fields():
    payload = hook_event_payload(
        _ctx(HookEvent.PROMPT_SUBMIT, prompt="x" * (2 * _WORKFLOW_EVENT_TEXT_CAP))
    )
    assert len(payload["prompt"]) == _WORKFLOW_EVENT_TEXT_CAP


def test_hook_event_payload_is_json_safe():
    import json as _json

    payload = hook_event_payload(
        _ctx(
            HookEvent.POST_TOOL_USE,
            tool_name="bash",
            tool_args={"path": object()},  # non-JSON value stringified
            scratch={"k": 1},
        )
    )
    _json.dumps(payload)  # must not raise
    assert isinstance(payload["tool_args"]["path"], str)


# --- run_workflow_by_id wall-clock cap --------------------------------------------


def _by_id_env(monkeypatch, captured: dict):
    loader = SimpleNamespace(
        get_definition=lambda tid: SimpleNamespace(
            workflow_config=SimpleNamespace(
                source_code="def run():\n    return None\n",
                entrypoint="run",
                continuations=[],
            ),
            parameters={},
        )
    )
    monkeypatch.setattr(tool_runtime_module, "workflow_execution_gate", lambda *a: None)
    monkeypatch.setattr(
        tool_runtime_module, "budget_from_config", lambda cfg: WorkflowBudget()
    )
    monkeypatch.setattr(tool_runtime_module, "config_revision_hash", lambda *a: "h")

    async def fake_execute(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(envelope=_ok(None))

    monkeypatch.setattr("nymeria.core.workflows.executor.execute_workflow", fake_execute)
    return loader


def _run_by_id(loader, **kw):
    return asyncio.run(
        tool_runtime_module.run_workflow_by_id(
            loader, "wf_demo", {}, user_id="u1", thread_id="t1", **kw
        )
    )


def test_by_id_cap_lowers_wall_clock(monkeypatch):
    captured: dict = {}
    loader = _by_id_env(monkeypatch, captured)
    refusal, run = _run_by_id(loader, wall_clock_cap=42.0)
    assert refusal is None and run is not None
    assert captured["budget"].wall_clock_seconds == 42.0


def test_by_id_cap_never_raises_wall_clock(monkeypatch):
    captured: dict = {}
    loader = _by_id_env(monkeypatch, captured)
    _run_by_id(loader, wall_clock_cap=100_000.0)
    assert captured["budget"].wall_clock_seconds == WorkflowBudget().wall_clock_seconds


def test_by_id_no_cap_keeps_configured_wall_clock(monkeypatch):
    captured: dict = {}
    loader = _by_id_env(monkeypatch, captured)
    _run_by_id(loader)
    assert captured["budget"].wall_clock_seconds == WorkflowBudget().wall_clock_seconds


# --- authoring chokepoint (manager) ----------------------------------------------


def _binding(monkeypatch, error: Optional[str]):
    monkeypatch.setattr(
        "nymeria.core.workflows.tool_runtime.workflow_binding_error",
        lambda wf, params, allow_event=False: error,
    )


def test_add_hook_validates_binding(tmp_path, monkeypatch):
    _binding(monkeypatch, None)
    manager = HookManager(tmp_path)
    hook = manager.add_hook(
        "u1",
        name="wf gate",
        event="pre_tool_use",
        action="run_workflow",
        params={"workflow_id": "wf_demo"},
        scope="global",
    )
    assert hook is not None
    assert hook.logic.action == "run_workflow"
    assert hook.logic.timeout_seconds == 60.0
    assert hook.logic.on_fault == "allow"


def test_add_hook_rejects_unsound_binding(tmp_path, monkeypatch):
    _binding(monkeypatch, "no published workflow tool named 'wf_demo'")
    manager = HookManager(tmp_path)
    with pytest.raises(ValueError, match="no published workflow"):
        manager.add_hook(
            "u1",
            name="wf gate",
            event="pre_tool_use",
            action="run_workflow",
            params={"workflow_id": "wf_demo"},
            scope="global",
        )


def test_logic_model_requires_workflow_id():
    from nymeria.core.hook_manager import build_logic

    # build_logic (pydantic) rejects before any store or binding I/O.
    with pytest.raises(ValueError):
        build_logic("run_workflow", {})


def test_update_hook_revalidates_binding(tmp_path, monkeypatch):
    _binding(monkeypatch, None)
    manager = HookManager(tmp_path)
    hook = manager.add_hook(
        "u1",
        name="wf gate",
        event="pre_tool_use",
        action="run_workflow",
        params={"workflow_id": "wf_demo"},
        scope="global",
    )
    _binding(monkeypatch, "unknown parameter(s) for workflow 'wf_demo': nope")
    with pytest.raises(ValueError, match="unknown parameter"):
        manager.update_hook(
            "u1", hook.id,
            params={"workflow_id": "wf_demo", "params": {"nope": 1}},
        )


def test_params_from_fields_run_workflow_branch():
    params = params_from_fields(
        "run_workflow",
        workflow_id="wf_demo",
        workflow_params={"channel": "ops"},
        timeout_seconds=90.0,
        on_fault="deny",
    )
    assert params == {
        "workflow_id": "wf_demo",
        "params": {"channel": "ops"},
        "timeout_seconds": 90.0,
        "on_fault": "deny",
    }


def test_build_update_kwargs_merges_onto_stored_params(tmp_path, monkeypatch):
    _binding(monkeypatch, None)
    manager = HookManager(tmp_path)
    hook = manager.add_hook(
        "u1",
        name="wf gate",
        event="pre_tool_use",
        action="run_workflow",
        params={"workflow_id": "wf_demo", "timeout_seconds": 90.0},
        scope="global",
    )
    kwargs = build_update_kwargs(hook, on_fault="deny", scalars={})
    # A partial edit keeps the stored siblings (workflow_id, timeout).
    assert kwargs["params"]["workflow_id"] == "wf_demo"
    assert kwargs["params"]["timeout_seconds"] == 90.0
    assert kwargs["params"]["on_fault"] == "deny"


# --- integration: the real engine vetoes a tool call ------------------------------


def test_pre_hook_runs_real_engine_and_denies(tmp_path, monkeypatch):
    """End to end minus the loader: hook action -> by-id path -> real child
    process -> deny decision mapped to a PreToolOutcome."""
    fake_settings = SimpleNamespace(data_dir=tmp_path, custom_tools_dir=tmp_path)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: fake_settings)
    monkeypatch.setattr("nymeria.config.settings.get_settings", lambda: fake_settings)

    source = (
        "def run(event: dict = None):\n"
        "    tool = (event or {}).get('tool_name', '?')\n"
        "    return {'decision': 'deny', 'reason': 'engine blocks ' + tool}\n"
    )
    loader = SimpleNamespace(
        get_definition=lambda tid: SimpleNamespace(
            workflow_config=SimpleNamespace(
                source_code=source, entrypoint="run", continuations=[]
            ),
            parameters={"event": SimpleNamespace(required=False)},
        )
    )
    monkeypatch.setattr("nymeria.core.custom_tools.get_custom_tool_loader", lambda: loader)
    monkeypatch.setattr(tool_runtime_module, "workflow_execution_gate", lambda *a: None)
    monkeypatch.setattr(
        tool_runtime_module,
        "budget_from_config",
        lambda cfg: WorkflowBudget(wall_clock_seconds=30.0),
    )
    monkeypatch.setattr(tool_runtime_module, "config_revision_hash", lambda *a: "h")
    monkeypatch.setattr(
        tool_runtime_module, "workflow_declares_event", lambda _wf: True
    )

    out = asyncio.run(
        run_workflow(
            _ctx(HookEvent.PRE_TOOL_USE, tool_name="bash", tool_args={"command": "rm"}),
            {"workflow_id": "wf_demo", "timeout_seconds": 30.0},
        )
    )
    assert isinstance(out, PreToolOutcome)
    assert out.decision == "deny"
    assert out.reason == "engine blocks bash"
