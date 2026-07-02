"""Phase 4 tests: the ``nym.approve`` suspend/resume path.

Covers the durable pending-record store (create/claim/expiry semantics), the
end-to-end suspension of a real subprocess run, the executor's forged-finish
refusal, and the resume path (approve/decline continuation runs, the
resume_invalid refusals, the expiry sweep). Engine runs use real subprocesses
like the phase 1 tests; the record store is redirected to tmp_path.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from nymeria.core.workflows import WorkflowBudget, execute_workflow
from nymeria.core.workflows import approvals as approvals_module
from nymeria.core.workflows.approvals import (
    MAX_PENDING_PER_WORKFLOW,
    claim_approval,
    create_pending_approval,
    expired_approvals,
    finish_claim,
    list_pending_approvals,
    load_approval,
    pending_dir,
    public_approval_entry,
    resolve_approval_record,
    sweep_expired_approvals,
)
from nymeria.core.workflows.authoring import (
    approve_revision,
    config_revision_hash,
    validate_workflow_static,
)
from nymeria.core.workflows.executor import _finish_to_envelope
from nymeria.core.workflows.registry import ApprovalRuntime
from nymeria.tools.definitions.custom_tool_schema import WorkflowToolConfig

pytestmark = pytest.mark.asyncio

WALL = 30.0

SUSPEND_SOURCE = (
    "def run(x: str = 'one'):\n"
    "    nym.approve(prompt='Ship it?', state={'x': x}, resume='cont')\n"
    "    return {'unreachable': True}\n"
    "\n"
    "def cont(state, decision):\n"
    "    return {\n"
    "        'approved': decision['approved'],\n"
    "        'note': decision['note'],\n"
    "        'x': state['x'],\n"
    "    }\n"
)


@pytest.fixture(autouse=True)
def _pending_store(tmp_path, monkeypatch):
    """Redirect the approval record store (and silence announcements).

    The global settings patch also redirects trace.py's run-record store
    (the resume_invalid path persists a stepless record); the engine reads
    only ``data_dir`` from global settings.
    """
    monkeypatch.setattr(
        approvals_module,
        "get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path),
    )
    monkeypatch.setattr(
        "nymeria.config.get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path),
    )
    notifications: list[dict] = []

    def _record_notification(**kwargs):
        notifications.append(kwargs)

    monkeypatch.setattr(
        "nymeria.core.notifications.create_notification", _record_notification
    )
    yield notifications


def _mint(record_id: str = "run-1", workflow_id: str = "wf_demo", **overrides):
    kwargs = dict(
        run_id=record_id,
        workflow_id=workflow_id,
        origin="tool",
        owner_user_id="owner",
        user_id="owner",
        thread_id="t1",
        revision_hash="h" * 64,
        resume_entrypoint="cont",
        state={"x": "one"},
        prompt="Ship it?",
    )
    kwargs.update(overrides)
    return create_pending_approval(**kwargs)


def _approval_runtime(**overrides) -> ApprovalRuntime:
    kwargs = dict(
        origin="tool",
        revision_hash="h" * 64,
        continuations=("cont",),
        owner_user_id="owner",
    )
    kwargs.update(overrides)
    return ApprovalRuntime(**kwargs)


async def _run_suspendable(
    source: str,
    *,
    approval: ApprovalRuntime | None,
    budget: WorkflowBudget | None = None,
    params: dict | None = None,
):
    return await execute_workflow(
        source=source,
        entrypoint="run",
        params=params or {},
        user_id="owner",
        thread_id="t1",
        workflow_id="wf_demo",
        budget=budget or WorkflowBudget(wall_clock_seconds=WALL),
        approval=approval,
        persist_record=False,
    )


# --- record store -------------------------------------------------------------


async def test_create_load_and_public_entry():
    record = _mint()
    assert record["record_id"] == "run-1"
    assert record["resume_token"] and record["resume_token"] != record["record_id"]
    loaded = load_approval("run-1")
    assert loaded is not None
    assert loaded["state"] == {"x": "one"}
    assert loaded["expires_at"] > loaded["created_at"]
    entry = public_approval_entry(loaded)
    assert entry["record_id"] == "run-1"
    assert entry["workflow_id"] == "wf_demo"
    assert "resume_token" not in entry and "state" not in entry


async def test_list_filters_by_user():
    _mint("run-a", user_id="alice")
    _mint("run-b", user_id="bob")
    assert {r["record_id"] for r in list_pending_approvals()} == {"run-a", "run-b"}
    assert [r["record_id"] for r in list_pending_approvals(user_id="bob")] == ["run-b"]


async def test_pending_cap_per_workflow():
    for i in range(MAX_PENDING_PER_WORKFLOW):
        _mint(f"run-{i}")
    with pytest.raises(ValueError, match="unresolved"):
        _mint("run-overflow")
    # A different workflow is not affected by the full one.
    _mint("run-other", workflow_id="wf_other")


async def test_claim_is_mutually_exclusive():
    _mint("run-1")
    first = claim_approval("run-1")
    assert first is not None and first["record_id"] == "run-1"
    assert claim_approval("run-1") is None
    assert load_approval("run-1") is None  # no longer pending
    finish_claim("run-1")
    assert not list(pending_dir().glob("*"))


async def test_expired_and_stale_claim_purge():
    fresh = _mint("run-fresh")
    expired = _mint("run-old")
    path = approvals_module._record_path("run-old")
    expired["expires_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(expired), encoding="utf-8")
    stale_claim = pending_dir() / "run-dead.json.claimed"
    stale_claim.write_text("{}", encoding="utf-8")
    os.utime(stale_claim, (0, 0))
    hits = expired_approvals()
    assert [r["record_id"] for r in hits] == ["run-old"]
    assert not stale_claim.exists()
    assert load_approval("run-fresh") == fresh


# --- suspension (real subprocess) ----------------------------------------------


async def test_suspend_end_to_end():
    approval = _approval_runtime()
    result = await _run_suspendable(
        SUSPEND_SOURCE, approval=approval, params={"x": "two"}
    )
    env = result.envelope
    assert env.ok is False and env.status == "needs_approval"
    record_id = env.resume_token
    assert record_id and env.output["record_id"] == record_id
    assert env.output["prompt"] == "Ship it?"
    assert env.output["expires_at"]
    record = load_approval(record_id)
    assert record is not None
    assert record["state"] == {"x": "two"}
    assert record["resume_entrypoint"] == "cont"
    # The internal token stays out of the envelope.
    assert record["resume_token"] != record_id
    assert env.resume_token != record["resume_token"]
    # ... and out of the step trace (persisted run records are served over
    # GET /workflows/runs); the raw state is redacted there too.
    approve_steps = [s for s in result.trace.steps if s.verb == "approve"]
    assert approve_steps and approve_steps[0].status == "ok"
    assert record["resume_token"] not in approve_steps[0].result_summary
    assert "[redacted]" in approve_steps[0].args_summary
    assert "two" not in approve_steps[0].args_summary


async def test_author_swallowing_suspend_discards_record():
    source = (
        "def run():\n"
        "    try:\n"
        "        nym.approve(prompt='Ship it?', state={}, resume='cont')\n"
        "    except BaseException:\n"
        "        pass\n"
        "    return {'done': True}\n"
        "\n"
        "def cont(state, decision):\n"
        "    return {}\n"
    )
    result = await _run_suspendable(source, approval=_approval_runtime())
    assert result.envelope.status == "ok"
    assert list_pending_approvals() == []


async def test_second_approve_is_verb_error_and_cleans_up():
    source = (
        "def run():\n"
        "    try:\n"
        "        nym.approve(prompt='first', state={}, resume='cont')\n"
        "    except BaseException:\n"
        "        pass\n"
        "    nym.approve(prompt='second', state={}, resume='cont')\n"
        "\n"
        "def cont(state, decision):\n"
        "    return {}\n"
    )
    result = await _run_suspendable(source, approval=_approval_runtime())
    env = result.envelope
    assert env.status == "error" and env.error.kind == "verb_error"
    assert "one suspension per run" in env.error.message
    assert list_pending_approvals() == []


async def test_adhoc_run_cannot_suspend():
    result = await _run_suspendable(SUSPEND_SOURCE, approval=None)
    env = result.envelope
    assert env.status == "error" and env.error.kind == "verb_error"
    assert "saved workflow" in env.error.message
    assert list_pending_approvals() == []


async def test_undeclared_continuation_is_rejected():
    source = (
        "def run():\n"
        "    nym.approve(prompt='Ship it?', state={}, resume='nope')\n"
    )
    result = await _run_suspendable(source, approval=_approval_runtime())
    env = result.envelope
    assert env.status == "error" and env.error.kind == "verb_error"
    assert "declared continuation" in env.error.message


async def test_state_over_cap_is_state_too_large():
    source = (
        "def run():\n"
        "    nym.approve(prompt='Ship it?', state={'blob': 'x' * 500}, resume='cont')\n"
        "\n"
        "def cont(state, decision):\n"
        "    return {}\n"
    )
    budget = WorkflowBudget(wall_clock_seconds=WALL, state_cap_bytes=64)
    result = await _run_suspendable(source, approval=_approval_runtime(), budget=budget)
    env = result.envelope
    assert env.status == "error" and env.error.kind == "state_too_large"
    assert list_pending_approvals() == []


async def test_forged_suspension_is_refused():
    forged = {"status": "needs_approval", "resume_token": "forged-token"}
    # No approval runtime at all (adhoc run).
    env = _finish_to_envelope(forged, {}, 0, "", approval=None)
    assert env.status == "error" and env.error.kind == "runner_error"
    assert "did not mint" in env.error.message
    # Approval runtime present but the token does not match the minted one.
    approval = _approval_runtime()
    approval.record_id = "run-1"
    approval.token = "real-token"
    env = _finish_to_envelope(forged, {}, 0, "", approval=approval)
    assert env.status == "error" and env.error.kind == "runner_error"


# --- resume --------------------------------------------------------------------


def _saved_workflow(monkeypatch, source: str = SUSPEND_SOURCE):
    """A gate-approved published workflow served by a fake loader."""
    config = WorkflowToolConfig(
        source_code=source, entrypoint="run", continuations=["cont"]
    )
    params, errors = validate_workflow_static(config=config)
    assert errors == [], errors
    config = approve_revision(config, params, approved_by="admin")
    definition = SimpleNamespace(workflow_config=config, parameters=params)
    loader = SimpleNamespace(
        get_definition=lambda workflow_id: (
            definition if workflow_id == "wf_demo" else None
        )
    )
    monkeypatch.setattr(
        "nymeria.core.custom_tools.get_custom_tool_loader", lambda: loader
    )
    return config, params


async def test_resolve_approved_runs_continuation(monkeypatch):
    config, params = _saved_workflow(monkeypatch)
    _mint(revision_hash=config_revision_hash(config, params), state={"x": "resumed"})
    claimed = claim_approval("run-1")
    result = await resolve_approval_record(
        claimed, approved=True, resolved_by="admin", note="go"
    )
    assert result["decision"] == "approved"
    envelope = result["envelope"]
    assert envelope["status"] == "ok"
    assert envelope["output"] == {"approved": True, "note": "go", "x": "resumed"}
    assert not list(pending_dir().glob("*"))  # claim finished


async def test_resolve_declined_still_runs_continuation(monkeypatch):
    config, params = _saved_workflow(monkeypatch)
    _mint(revision_hash=config_revision_hash(config, params))
    claimed = claim_approval("run-1")
    result = await resolve_approval_record(
        claimed, approved=False, resolved_by="owner", note="not now"
    )
    assert result["decision"] == "declined"
    envelope = result["envelope"]
    assert envelope["status"] == "ok"
    assert envelope["output"]["approved"] is False
    assert envelope["output"]["note"] == "not now"


async def test_resume_refused_when_content_changed(monkeypatch, tmp_path):
    _saved_workflow(monkeypatch)
    _mint(revision_hash="0" * 64)
    claimed = claim_approval("run-1")
    result = await resolve_approval_record(claimed, approved=True, resolved_by="admin")
    envelope = result["envelope"]
    assert envelope["status"] == "error"
    assert envelope["error"]["kind"] == "resume_invalid"
    assert "changed since" in envelope["error"]["message"]
    assert not list(pending_dir().glob("*"))
    # The ack's run_id must not dangle: a refused resume persists a stepless
    # run record with the resume_invalid envelope.
    record_path = (
        tmp_path / "workflows" / "runs" / "wf_demo" / f"{result['run_id']}.json"
    )
    assert record_path.is_file()
    import json as _json

    record = _json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "error"
    assert record["envelope"]["error"]["kind"] == "resume_invalid"
    assert record["trace"]["steps"] == []
    assert record["user_id"] == "owner"


async def test_resume_refused_when_workflow_gone(monkeypatch):
    _saved_workflow(monkeypatch)
    _mint(workflow_id="wf_deleted", revision_hash="0" * 64)
    claimed = claim_approval("run-1")
    result = await resolve_approval_record(claimed, approved=True, resolved_by="admin")
    assert result["envelope"]["error"]["kind"] == "resume_invalid"
    assert "no longer exists" in result["envelope"]["error"]["message"]


async def test_resume_refused_when_gate_revoked(monkeypatch):
    config = WorkflowToolConfig(
        source_code=SUSPEND_SOURCE, entrypoint="run", continuations=["cont"]
    )
    params, errors = validate_workflow_static(config=config)
    assert errors == [], errors
    # Never approved: the gate refuses, even though the hash matches.
    definition = SimpleNamespace(workflow_config=config, parameters=params)
    monkeypatch.setattr(
        "nymeria.core.custom_tools.get_custom_tool_loader",
        lambda: SimpleNamespace(get_definition=lambda _wf: definition),
    )
    _mint(revision_hash=config_revision_hash(config, params))
    claimed = claim_approval("run-1")
    result = await resolve_approval_record(claimed, approved=True, resolved_by="admin")
    assert result["envelope"]["error"]["kind"] == "resume_invalid"
    assert "no longer approved" in result["envelope"]["error"]["message"]


async def test_resume_refused_when_continuation_undeclared(monkeypatch):
    config, params = _saved_workflow(monkeypatch)
    _mint(
        revision_hash=config_revision_hash(config, params),
        resume_entrypoint="vanished",
    )
    claimed = claim_approval("run-1")
    result = await resolve_approval_record(claimed, approved=True, resolved_by="admin")
    assert result["envelope"]["error"]["kind"] == "resume_invalid"
    assert "no longer declared" in result["envelope"]["error"]["message"]


async def test_sweep_resolves_expired_as_declined(monkeypatch, _pending_store):
    config, params = _saved_workflow(monkeypatch)
    record = _mint(revision_hash=config_revision_hash(config, params))
    path = approvals_module._record_path("run-1")
    record["expires_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(record), encoding="utf-8")
    _mint("run-live", revision_hash=config_revision_hash(config, params))

    resolved = await sweep_expired_approvals()
    assert resolved == 1
    assert [r["record_id"] for r in list_pending_approvals()] == ["run-live"]
    # The owner heard about the declined resume (announce + resume summary).
    assert any("declined" in n.get("summary", "") for n in _pending_store)
