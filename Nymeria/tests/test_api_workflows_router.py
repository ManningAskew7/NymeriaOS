"""Workflow approval router tests: admin gating, pending/source/approve/
decline, and per-user run-record scoping."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nymeria.core.accounts import AccountsRepo
from nymeria.core.chat_bindings import ChatBindingsRepo
from nymeria.core.custom_tools import CustomToolLoader

tool_create_module = importlib.import_module("nymeria.tools.tool_create")

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
