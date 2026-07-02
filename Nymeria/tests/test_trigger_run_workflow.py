"""Phase 4 tests for the ``run_workflow`` trigger action.

``fire_action`` runs the published workflow through the turn executor's
``run_workflow`` seam (no LLM call): the raw event dict rides along as the
``event`` parameter only when the workflow's signature declares one, a
``needs_approval`` outcome counts as a successful fire, and an error envelope
raises into the standard trigger execution log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from nymeria.core.trigger_manager import (
    TriggerAction,
    TriggerDefinition,
    TriggerManager,
)
from nymeria.core.workflows import tool_runtime as tool_runtime_module


class _FakeWorkflowExecutor:
    """TurnExecutor stand-in: records run_workflow calls, returns a script."""

    is_remote = False

    def __init__(self, envelope: Optional[dict] = None) -> None:
        self.envelope = envelope or {"ok": True, "status": "ok"}
        self.calls: list[dict[str, Any]] = []

    async def run_workflow(
        self,
        workflow_id: str,
        params: Optional[dict] = None,
        *,
        user_id: str,
        thread_id: Optional[str] = None,
    ) -> dict:
        self.calls.append(
            {
                "workflow_id": workflow_id,
                "params": dict(params or {}),
                "user_id": user_id,
                "thread_id": thread_id,
            }
        )
        return dict(self.envelope)


def _workflow_trigger(config: Optional[dict] = None) -> TriggerDefinition:
    return TriggerDefinition(
        id="trig-wf",
        name="WF Trigger",
        source_type="webhook",
        source_config={},
        action=TriggerAction(
            type="run_workflow",
            config=config if config is not None else {"workflow_id": "wf_demo"},
        ),
        thread_id="wf-thread",
        enabled=True,
    )


@pytest.fixture
def manager(tmp_path: Path) -> TriggerManager:
    return TriggerManager(tmp_path)


def _executions(manager: TriggerManager):
    return manager.get_executions("owner")


def test_fire_passes_event_when_declared(manager, monkeypatch):
    monkeypatch.setattr(
        tool_runtime_module, "workflow_declares_event", lambda _wf: True
    )
    executor = _FakeWorkflowExecutor()
    event = {"body": "hello", "sender": "alice"}
    manager.fire_action(_workflow_trigger(), event, executor, "owner")

    assert len(executor.calls) == 1
    call = executor.calls[0]
    assert call["workflow_id"] == "wf_demo"
    assert call["params"]["event"] == event
    assert call["user_id"] == "owner"
    assert call["thread_id"] == "wf-thread"
    executions = _executions(manager)
    assert executions and executions[0]["status"] == "success"


def test_fire_omits_event_when_not_declared(manager, monkeypatch):
    monkeypatch.setattr(
        tool_runtime_module, "workflow_declares_event", lambda _wf: False
    )
    executor = _FakeWorkflowExecutor()
    trigger = _workflow_trigger(
        {"workflow_id": "wf_demo", "params": {"channel": "ops"}}
    )
    manager.fire_action(trigger, {"body": "hello"}, executor, "owner")

    call = executor.calls[0]
    assert "event" not in call["params"]
    assert call["params"] == {"channel": "ops"}


def test_configured_event_param_is_not_overwritten(manager, monkeypatch):
    monkeypatch.setattr(
        tool_runtime_module, "workflow_declares_event", lambda _wf: True
    )
    executor = _FakeWorkflowExecutor()
    trigger = _workflow_trigger(
        {"workflow_id": "wf_demo", "params": {"event": {"pinned": True}}}
    )
    manager.fire_action(trigger, {"body": "live"}, executor, "owner")
    assert executor.calls[0]["params"]["event"] == {"pinned": True}


def test_needs_approval_is_a_successful_fire(manager, monkeypatch):
    monkeypatch.setattr(
        tool_runtime_module, "workflow_declares_event", lambda _wf: False
    )
    executor = _FakeWorkflowExecutor(
        {"ok": False, "status": "needs_approval", "resume_token": "rec-1"}
    )
    manager.fire_action(_workflow_trigger(), {}, executor, "owner")
    executions = _executions(manager)
    assert executions and executions[0]["status"] == "success"


def test_error_envelope_logs_error_execution(manager, monkeypatch):
    monkeypatch.setattr(
        tool_runtime_module, "workflow_declares_event", lambda _wf: False
    )
    executor = _FakeWorkflowExecutor(
        {
            "ok": False,
            "status": "error",
            "error": {"kind": "budget_exceeded", "message": "too many calls"},
        }
    )
    manager.fire_action(_workflow_trigger(), {}, executor, "owner")
    executions = _executions(manager)
    assert executions and executions[0]["status"] == "error"
    assert "budget_exceeded" in (executions[0].get("error_message") or "")


def test_missing_workflow_id_logs_error(manager):
    executor = _FakeWorkflowExecutor()
    manager.fire_action(_workflow_trigger({}), {}, executor, "owner")
    assert executor.calls == []
    executions = _executions(manager)
    assert executions and executions[0]["status"] == "error"
    assert "workflow_id" in (executions[0].get("error_message") or "")


def test_default_thread_id_derives_from_trigger(manager, monkeypatch):
    monkeypatch.setattr(
        tool_runtime_module, "workflow_declares_event", lambda _wf: False
    )
    executor = _FakeWorkflowExecutor()
    trigger = _workflow_trigger()
    trigger.thread_id = ""
    manager.fire_action(trigger, {}, executor, "owner")
    assert executor.calls[0]["thread_id"] == "trigger-trig-wf"


def test_rest_binding_helper_validates_config_shape(monkeypatch):
    from nymeria.triggers.trigger_api import _run_workflow_binding_error

    assert "workflow_id" in _run_workflow_binding_error({})
    assert "workflow_id" in _run_workflow_binding_error({"workflow_id": "  "})
    assert "must be a dict" in _run_workflow_binding_error(
        {"workflow_id": "wf_demo", "params": "nope"}
    )
    seen: list[tuple] = []

    def fake_binding_error(workflow_id, params, *, allow_event=False):
        seen.append((workflow_id, dict(params), allow_event))
        return None

    monkeypatch.setattr(
        tool_runtime_module, "workflow_binding_error", fake_binding_error
    )
    assert (
        _run_workflow_binding_error({"workflow_id": "wf_demo", "params": {"a": 1}})
        is None
    )
    # Triggers always supply an event at fire time.
    assert seen == [("wf_demo", {"a": 1}, True)]
