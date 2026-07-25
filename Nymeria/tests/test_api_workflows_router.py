"""Workflow approval router tests: admin gating, pending/source/approve/
decline, and per-user run-record scoping."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nymeria
from nymeria.core.accounts import AccountsRepo
from nymeria.core.chat_bindings import ChatBindingsRepo
from nymeria.core.custom_tools import CustomToolLoader

tool_create_module = importlib.import_module("nymeria.tools.tool_create")

BUNDLED_WORKFLOWS_DIR = Path(nymeria.__file__).parent / "workflows_bundled"

WF_SOURCE = "def run(name: str):\n    return name\n"


class FakeAgent:
    def __init__(self, data_dir: Path):
        accounts_db = data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(accounts_db)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)

    def reload_tools(self):
        pass

    def sync_agent_tools(self):
        pass


def _sandbox_tool_create(monkeypatch, tmp_path: Path):
    store = tool_create_module.ToolDraftStore(tmp_path / "tool_drafts")
    loader = CustomToolLoader(tmp_path / "custom_tools")
    fake_settings = SimpleNamespace(
        data_dir=tmp_path, custom_tools_dir=tmp_path / "custom_tools"
    )
    monkeypatch.setattr(tool_create_module, "_draft_store", lambda: store)
    monkeypatch.setattr(tool_create_module, "get_custom_tool_loader", lambda: loader)
    monkeypatch.setattr(tool_create_module, "get_settings", lambda: fake_settings)
    monkeypatch.setattr("nymeria.config.settings.get_settings", lambda: fake_settings)
    return store


def _seed_pending_draft(store, tool_id: str = "wf_router_a") -> None:
    draft = tool_create_module.create_draft_definition(
        user_id="author",
        tool_id=tool_id,
        name="WF Router",
        description="router test workflow",
        parameters=None,
        http_config=None,
        implementation_type="workflow",
        python_code=WF_SOURCE,
    )
    store.save("author", draft)


def _client(tmp_path, api_client_builder, *, role: str) -> tuple[Any, str]:
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent, settings, user_id="caller", role=role
    )
    return client, token


def test_pending_routes_are_admin_only(tmp_path, api_client_builder, monkeypatch):
    _sandbox_tool_create(monkeypatch, tmp_path)
    client, token = _client(tmp_path, api_client_builder, role="user")
    assert (
        client.get("/workflows/pending", headers=api_client_builder.auth(token)).status_code
        == 403
    )
    assert (
        client.post(
            "/workflows/approve",
            json={"kind": "draft", "id": "x", "owner_user_id": "author"},
            headers=api_client_builder.auth(token),
        ).status_code
        == 403
    )


def test_pending_source_approve_flow(tmp_path, api_client_builder, monkeypatch):
    store = _sandbox_tool_create(monkeypatch, tmp_path)
    _seed_pending_draft(store)
    monkeypatch.setattr(
        "nymeria.core.notifications.create_notification", lambda **kw: None
    )
    client, token = _client(tmp_path, api_client_builder, role="admin")
    headers = api_client_builder.auth(token)

    pending = client.get("/workflows/pending", headers=headers).json()
    assert pending["total"] == 1
    entry = pending["pending"][0]
    assert entry["tool_id"] == "wf_router_a" and entry["kind"] == "draft"

    source = client.get(
        "/workflows/source",
        params={"kind": "draft", "id": "wf_router_a", "owner_user_id": "author"},
        headers=headers,
    ).json()
    assert "def run(" in source["source_code"]
    assert source["approval"] == "pending"

    ack = client.post(
        "/workflows/approve",
        json={"kind": "draft", "id": "wf_router_a", "owner_user_id": "author"},
        headers=headers,
    )
    assert ack.status_code == 200
    assert ack.json() == {
        "ok": True,
        "decision": "approved",
        "kind": "draft",
        "id": "wf_router_a",
    }
    saved = store.get("author", "wf_router_a")
    assert saved.workflow_config.approved_by == "caller"
    assert client.get("/workflows/pending", headers=headers).json()["total"] == 0


def test_decline_with_note_and_missing_target(tmp_path, api_client_builder, monkeypatch):
    store = _sandbox_tool_create(monkeypatch, tmp_path)
    _seed_pending_draft(store, tool_id="wf_router_b")
    monkeypatch.setattr(
        "nymeria.core.notifications.create_notification", lambda **kw: None
    )
    client, token = _client(tmp_path, api_client_builder, role="admin")
    headers = api_client_builder.auth(token)

    ack = client.post(
        "/workflows/decline",
        json={
            "kind": "draft",
            "id": "wf_router_b",
            "owner_user_id": "author",
            "note": "needs a delivery target",
        },
        headers=headers,
    )
    assert ack.status_code == 200 and ack.json()["decision"] == "declined"
    saved = store.get("author", "wf_router_b")
    assert saved.workflow_config.decline_note == "needs a delivery target"

    missing = client.post(
        "/workflows/approve",
        json={"kind": "draft", "id": "ghost", "owner_user_id": "author"},
        headers=headers,
    )
    assert missing.status_code == 404

    bad_kind = client.post(
        "/workflows/approve",
        json={"kind": "nope", "id": "x", "owner_user_id": "author"},
        headers=headers,
    )
    assert bad_kind.status_code == 400


def test_approve_with_revision_pin(tmp_path, api_client_builder, monkeypatch):
    store = _sandbox_tool_create(monkeypatch, tmp_path)
    _seed_pending_draft(store, tool_id="wf_router_c")
    monkeypatch.setattr(
        "nymeria.core.notifications.create_notification", lambda **kw: None
    )
    client, token = _client(tmp_path, api_client_builder, role="admin")
    headers = api_client_builder.auth(token)

    entry = client.get("/workflows/pending", headers=headers).json()["pending"][0]

    stale = client.post(
        "/workflows/approve",
        json={
            "kind": "draft",
            "id": "wf_router_c",
            "owner_user_id": "author",
            "revision": "0" * 64,
        },
        headers=headers,
    )
    assert stale.status_code == 409
    assert "changed since" in stale.json()["detail"]
    saved = store.get("author", "wf_router_c")
    assert saved.workflow_config.approved_revision is None

    pinned = client.post(
        "/workflows/approve",
        json={
            "kind": "draft",
            "id": "wf_router_c",
            "owner_user_id": "author",
            "revision": entry["revision"],
        },
        headers=headers,
    )
    assert pinned.status_code == 200 and pinned.json()["decision"] == "approved"


def test_runs_are_user_scoped(tmp_path, api_client_builder, monkeypatch):
    _sandbox_tool_create(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "nymeria.config.get_settings", lambda: SimpleNamespace(data_dir=tmp_path)
    )
    from nymeria.core.workflows.trace import StepTrace, persist_run_record

    for run_id, user in (("r1", "caller"), ("r2", "other")):
        persist_run_record(
            StepTrace(run_id=run_id, workflow_id="wf_router_runs"),
            {"ok": True, "status": "ok", "output": "x", "budget": {}},
            user_id=user,
            thread_id="t",
        )

    client, token = _client(tmp_path, api_client_builder, role="user")
    body = client.get(
        "/workflows/runs/wf_router_runs", headers=api_client_builder.auth(token)
    ).json()
    assert body["total"] == 1
    assert body["runs"][0]["user_id"] == "caller"


def test_admin_sees_all_runs(tmp_path, api_client_builder, monkeypatch):
    _sandbox_tool_create(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "nymeria.config.get_settings", lambda: SimpleNamespace(data_dir=tmp_path)
    )
    from nymeria.core.workflows.trace import StepTrace, persist_run_record

    for run_id, user in (("r1", "caller"), ("r2", "other")):
        persist_run_record(
            StepTrace(run_id=run_id, workflow_id="wf_router_runs"),
            {"ok": True, "status": "ok", "output": "x", "budget": {}},
            user_id=user,
            thread_id="t",
        )

    client, token = _client(tmp_path, api_client_builder, role="admin")
    body = client.get(
        "/workflows/runs/wf_router_runs", headers=api_client_builder.auth(token)
    ).json()
    assert body["total"] == 2


def test_aggregate_runs_across_workflows_user_scoped(
    tmp_path, api_client_builder, monkeypatch
):
    _sandbox_tool_create(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "nymeria.config.get_settings", lambda: SimpleNamespace(data_dir=tmp_path)
    )
    from nymeria.core.workflows.trace import StepTrace, persist_run_record

    for run_id, workflow_id, user in (
        ("r1", "wf_alpha", "caller"),
        ("r2", "wf_beta", "caller"),
        ("r3", "wf_beta", "other"),
    ):
        persist_run_record(
            StepTrace(run_id=run_id, workflow_id=workflow_id),
            {"ok": True, "status": "ok", "output": "x", "budget": {}},
            user_id=user,
            thread_id="t",
        )

    client, token = _client(tmp_path, api_client_builder, role="user")
    body = client.get(
        "/workflows/runs", headers=api_client_builder.auth(token)
    ).json()
    assert body["total"] == 2
    assert {r["workflow_id"] for r in body["runs"]} == {"wf_alpha", "wf_beta"}
    assert all(r["user_id"] == "caller" for r in body["runs"])

    limited = client.get(
        "/workflows/runs?limit=1", headers=api_client_builder.auth(token)
    ).json()
    assert limited["total"] == 1


# --- runtime approvals (nym.approve suspensions, phase 4) -----------------------


def _approvals_store(monkeypatch, tmp_path):
    # The shared ApprovalRecordStore resolves settings lazily via
    # nymeria.config.get_settings, so that is the redirect seam.
    monkeypatch.setattr(
        "nymeria.config.get_settings",
        lambda: SimpleNamespace(data_dir=tmp_path),
    )


def _mint_runtime_approval(record_id: str, user_id: str):
    from nymeria.core.workflows.approvals import create_pending_approval

    return create_pending_approval(
        run_id=record_id,
        workflow_id="wf_pending",
        origin="tool",
        owner_user_id=user_id,
        user_id=user_id,
        thread_id="t1",
        revision_hash="h" * 64,
        resume_entrypoint="cont",
        state={"secret": "payload"},
        prompt="Ship it?",
    )


def test_runtime_approvals_list_is_owner_scoped(
    tmp_path, api_client_builder, monkeypatch
):
    _sandbox_tool_create(monkeypatch, tmp_path)
    _approvals_store(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "nymeria.core.notifications.create_notification", lambda **kw: None
    )
    _mint_runtime_approval("rec-mine", "caller")
    _mint_runtime_approval("rec-other", "other")

    client, token = _client(tmp_path, api_client_builder, role="user")
    body = client.get(
        "/workflows/approvals", headers=api_client_builder.auth(token)
    ).json()
    assert body["total"] == 1
    entry = body["approvals"][0]
    assert entry["record_id"] == "rec-mine"
    # Public entries never expose the resume token or the raw state.
    assert "resume_token" not in entry and "state" not in entry

    # A second client needs its own accounts DB (same email would collide);
    # the approvals store stays the patched shared one.
    admin_dir = tmp_path / "admin-host"
    admin_dir.mkdir()
    admin_settings = api_client_builder.settings(admin_dir)
    admin_client, admin_token = api_client_builder.authenticated_client(
        FakeAgent(admin_dir), admin_settings, user_id="boss", role="admin"
    )
    body = admin_client.get(
        "/workflows/approvals", headers=api_client_builder.auth(admin_token)
    ).json()
    assert body["total"] == 2


def test_resolve_runtime_approval_owner_ack_and_claim(
    tmp_path, api_client_builder, monkeypatch
):
    _sandbox_tool_create(monkeypatch, tmp_path)
    _approvals_store(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "nymeria.core.notifications.create_notification", lambda **kw: None
    )
    resolved: list[dict] = []

    async def fake_resolve(record, **kwargs):
        resolved.append({"record": record, **kwargs})
        return {"record_id": record["record_id"]}

    monkeypatch.setattr(
        "nymeria.core.workflows.approvals.resolve_approval_record", fake_resolve
    )
    _mint_runtime_approval("rec-1", "caller")

    client, token = _client(tmp_path, api_client_builder, role="user")
    ack = client.post(
        "/workflows/approvals/rec-1/resolve",
        json={"approved": True, "note": "go"},
        headers=api_client_builder.auth(token),
    )
    assert ack.status_code == 200
    body = ack.json()
    assert body["ok"] is True
    assert body["record_id"] == "rec-1"
    assert body["decision"] == "approved"
    assert body["workflow_id"] == "wf_pending"
    assert body["run_id"]
    # The claim happened synchronously: the record is no longer pending.
    from nymeria.core.workflows.approvals import load_approval

    assert load_approval("rec-1") is None
    assert (tmp_path / "workflows" / "pending" / "rec-1.json.claimed").exists()


def test_resolve_runtime_approval_hides_other_users_records(
    tmp_path, api_client_builder, monkeypatch
):
    _sandbox_tool_create(monkeypatch, tmp_path)
    _approvals_store(monkeypatch, tmp_path)
    _mint_runtime_approval("rec-other", "other")

    client, token = _client(tmp_path, api_client_builder, role="user")
    headers = api_client_builder.auth(token)
    # Not yours and missing look identical: 404, no existence leak.
    not_yours = client.post(
        "/workflows/approvals/rec-other/resolve",
        json={"approved": False},
        headers=headers,
    )
    missing = client.post(
        "/workflows/approvals/ghost/resolve",
        json={"approved": True},
        headers=headers,
    )
    assert not_yours.status_code == 404
    assert missing.status_code == 404
    # The record was never claimed.
    from nymeria.core.workflows.approvals import load_approval

    assert load_approval("rec-other") is not None


def test_resolve_runtime_approval_conflict_when_already_claimed(
    tmp_path, api_client_builder, monkeypatch
):
    _sandbox_tool_create(monkeypatch, tmp_path)
    _approvals_store(monkeypatch, tmp_path)
    _mint_runtime_approval("rec-1", "caller")
    # Simulate losing the claim race: the record loads but the claim fails.
    monkeypatch.setattr(
        "nymeria.core.workflows.approvals.claim_approval", lambda rid: None
    )

    client, token = _client(tmp_path, api_client_builder, role="user")
    conflict = client.post(
        "/workflows/approvals/rec-1/resolve",
        json={"approved": True},
        headers=api_client_builder.auth(token),
    )
    assert conflict.status_code == 409


# --- POST /workflows/{id}/execute ----------------------------------------------


def _patch_execute(monkeypatch, *, refusal=None, output=None):
    calls: list[dict] = []

    async def fake_run(loader, workflow_id, params, *, user_id, thread_id, depth=0):
        calls.append(
            {
                "workflow_id": workflow_id,
                "params": params,
                "user_id": user_id,
                "thread_id": thread_id,
            }
        )
        if refusal is not None:
            return refusal, None
        run = SimpleNamespace(
            trace=SimpleNamespace(run_id="run-exec-1"),
            envelope=SimpleNamespace(
                to_dict=lambda: {"ok": True, "status": "ok", "output": output}
            ),
        )
        return None, run

    monkeypatch.setattr(
        "nymeria.core.workflows.tool_runtime.run_workflow_by_id", fake_run
    )
    monkeypatch.setattr(
        "nymeria.core.custom_tools.get_custom_tool_loader", lambda: None
    )
    return calls


def test_execute_endpoint_returns_envelope(tmp_path, api_client_builder, monkeypatch):
    _sandbox_tool_create(monkeypatch, tmp_path)
    calls = _patch_execute(monkeypatch, output={"echo": 1})

    client, token = _client(tmp_path, api_client_builder, role="user")
    response = client.post(
        "/workflows/wf_exec/execute",
        json={"params": {"name": "x"}, "thread_id": "t9"},
        headers=api_client_builder.auth(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["workflow_id"] == "wf_exec"
    assert body["run_id"] == "run-exec-1"
    assert body["envelope"]["output"] == {"echo": 1}
    assert calls == [
        {
            "workflow_id": "wf_exec",
            "params": {"name": "x"},
            "user_id": "caller",
            "thread_id": "t9",
        }
    ]


def test_execute_endpoint_maps_refusals(tmp_path, api_client_builder, monkeypatch):
    _sandbox_tool_create(monkeypatch, tmp_path)
    client, token = _client(tmp_path, api_client_builder, role="user")
    headers = api_client_builder.auth(token)

    _patch_execute(monkeypatch, refusal="workflow tool 'wf_ghost' was not found")
    missing = client.post(
        "/workflows/wf_ghost/execute", json={"params": {}}, headers=headers
    )
    assert missing.status_code == 404

    _patch_execute(
        monkeypatch, refusal="workflow execution requires an admin-approved revision"
    )
    ungated = client.post(
        "/workflows/wf_exec/execute", json={"params": {}}, headers=headers
    )
    assert ungated.status_code == 403


# --- bundled workflow templates ------------------------------------------------


def _sandbox_with_templates(monkeypatch, tmp_path: Path):
    """Sandbox tool_create AND expose the real bundled-workflow catalog."""
    loader = CustomToolLoader(tmp_path / "custom_tools")
    fake_settings = SimpleNamespace(
        data_dir=tmp_path,
        custom_tools_dir=tmp_path / "custom_tools",
        bundled_workflows_dir=BUNDLED_WORKFLOWS_DIR,
    )
    monkeypatch.setattr(tool_create_module, "get_custom_tool_loader", lambda: loader)
    monkeypatch.setattr(tool_create_module, "get_settings", lambda: fake_settings)
    monkeypatch.setattr("nymeria.config.settings.get_settings", lambda: fake_settings)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: fake_settings)
    # Keep install off any ambient agent (publish-only, loader.load_all path).
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: None)
    return loader


def test_templates_list_available_to_any_user(tmp_path, api_client_builder, monkeypatch):
    _sandbox_with_templates(monkeypatch, tmp_path)
    client, token = _client(tmp_path, api_client_builder, role="user")
    resp = client.get("/workflows/templates", headers=api_client_builder.auth(token))
    assert resp.status_code == 200
    body = resp.json()
    ids = {t["id"] for t in body["templates"]}
    assert {"two_thread_conversation", "url_watcher"} <= ids
    watcher = next(t for t in body["templates"] if t["id"] == "url_watcher")
    assert watcher["parameters"] == ["alert_after_failures", "url"]


def test_template_install_admin_then_idempotent(tmp_path, api_client_builder, monkeypatch):
    loader = _sandbox_with_templates(monkeypatch, tmp_path)
    client, token = _client(tmp_path, api_client_builder, role="admin")
    headers = api_client_builder.auth(token)

    first = client.post("/workflows/templates/url_watcher/install", json={}, headers=headers)
    assert first.status_code == 200
    assert first.json()["created"] is True
    assert first.json()["approval"] == "approved"
    assert loader.get_definition("url_watcher") is not None

    again = client.post("/workflows/templates/url_watcher/install", json={}, headers=headers)
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["already_installed"] is True


def test_template_install_works_with_no_body(tmp_path, api_client_builder, monkeypatch):
    """A body-less POST must install, not 422.

    The request model is deliberately field-less (reserved for future options),
    so a mandatory body would reject a plain `curl -X POST .../install` for a
    field nobody can supply. This endpoint has no GUI or slash surface, so
    curl/scripts ARE the callers.
    """
    loader = _sandbox_with_templates(monkeypatch, tmp_path)
    client, token = _client(tmp_path, api_client_builder, role="admin")

    resp = client.post(
        "/workflows/templates/url_watcher/install",
        headers=api_client_builder.auth(token),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] is True
    assert loader.get_definition("url_watcher") is not None


def test_template_install_forbidden_for_non_admin(tmp_path, api_client_builder, monkeypatch):
    _sandbox_with_templates(monkeypatch, tmp_path)
    client, token = _client(tmp_path, api_client_builder, role="user")
    resp = client.post(
        "/workflows/templates/url_watcher/install",
        json={},
        headers=api_client_builder.auth(token),
    )
    assert resp.status_code == 403


def test_template_install_unknown_id_400(tmp_path, api_client_builder, monkeypatch):
    _sandbox_with_templates(monkeypatch, tmp_path)
    client, token = _client(tmp_path, api_client_builder, role="admin")
    resp = client.post(
        "/workflows/templates/nope/install",
        json={},
        headers=api_client_builder.auth(token),
    )
    assert resp.status_code == 400
    assert "Unknown workflow template" in resp.json()["detail"]
