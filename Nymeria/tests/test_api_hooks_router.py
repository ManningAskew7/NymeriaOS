"""Tests for the lifecycle-hooks REST router (/hooks)."""

from __future__ import annotations

from pathlib import Path

import pytest

from nymeria.api.routers import hooks as hooks_router_module
from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_config import ThreadConfigManager
from nymeria.core.thread_metadata import ThreadMetadataManager


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)

    def invalidate_thread_config_cache(self, thread_id: str):
        pass

    def sync_agent_tools(self):
        pass


@pytest.fixture
def client_env(tmp_path, api_client_builder, monkeypatch):
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent, settings, user_id="owner", email="owner@example.com"
    )
    # The hooks router resolves its store via its own module-level get_settings,
    # which create_api_app's monkeypatch does not cover; point it at tmp_path.
    monkeypatch.setattr(hooks_router_module, "get_settings", lambda: settings)
    headers = api_client_builder.auth(token)
    return client, agent, headers, api_client_builder


def _create(client, headers, **body):
    payload = {"name": "n", "event": "done", "text": "check", "scope": "global"}
    payload.update(body)
    return client.post("/hooks", headers=headers, json=payload)


def test_create_and_get(client_env):
    client, _agent, headers, _b = client_env
    resp = _create(client, headers, name="Finisher", text="run tests")
    assert resp.status_code == 201
    hook = resp.json()
    assert hook["name"] == "Finisher"
    assert hook["event"] == "done"
    assert hook["action"] == "inject_context"
    assert hook["text"] == "run tests"

    got = client.get(f"/hooks/{hook['id']}", headers=headers)
    assert got.status_code == 200
    assert got.json()["id"] == hook["id"]


def test_list_and_enabled_filter(client_env):
    client, _agent, headers, _b = client_env
    a = _create(client, headers, name="a").json()
    b = _create(client, headers, name="b").json()
    client.patch(f"/hooks/{b['id']}", headers=headers, json={"enabled": False})

    all_hooks = client.get("/hooks", headers=headers).json()
    assert {h["id"] for h in all_hooks} == {a["id"], b["id"]}

    enabled = client.get("/hooks?enabled_only=true", headers=headers).json()
    assert {h["id"] for h in enabled} == {a["id"]}


def test_create_rejects_bad_event(client_env):
    client, _agent, headers, _b = client_env
    resp = _create(client, headers, event="pre_tool_use")
    assert resp.status_code == 422  # schema Literal rejects it before the store


def test_create_thread_scoped_claims_thread(client_env):
    client, _agent, headers, _b = client_env
    resp = _create(
        client, headers, scope="thread", thread_id="thread-owned-by-owner", name="scoped"
    )
    assert resp.status_code == 201
    assert resp.json()["scope"] == "thread"
    assert resp.json()["thread_id"] == "thread-owned-by-owner"


def test_create_thread_scoped_without_thread_id_400(client_env):
    client, _agent, headers, _b = client_env
    resp = _create(client, headers, scope="thread", thread_id=None)
    assert resp.status_code == 400


def test_create_global_nulls_thread_id(client_env):
    client, _agent, headers, _b = client_env
    # A global hook must not carry a stray thread_id even if the client sends one.
    resp = _create(client, headers, scope="global", thread_id="some-thread")
    assert resp.status_code == 201
    assert resp.json()["thread_id"] == ""


def test_create_text_over_cap_422(client_env):
    client, _agent, headers, _b = client_env
    resp = _create(client, headers, text="x" * 10_001)
    assert resp.status_code == 422


def test_update_changes_text(client_env):
    client, _agent, headers, _b = client_env
    hook = _create(client, headers).json()
    resp = client.patch(f"/hooks/{hook['id']}", headers=headers, json={"text": "new text"})
    assert resp.status_code == 200
    assert resp.json()["text"] == "new text"


def test_update_empty_body_400(client_env):
    client, _agent, headers, _b = client_env
    hook = _create(client, headers).json()
    resp = client.patch(f"/hooks/{hook['id']}", headers=headers, json={})
    assert resp.status_code == 400


def test_delete(client_env):
    client, _agent, headers, _b = client_env
    hook = _create(client, headers).json()
    resp = client.delete(f"/hooks/{hook['id']}", headers=headers)
    assert resp.status_code == 204
    assert client.get(f"/hooks/{hook['id']}", headers=headers).status_code == 404


def test_get_missing_404(client_env):
    client, _agent, headers, _b = client_env
    assert client.get("/hooks/nope", headers=headers).status_code == 404


def test_test_endpoint_renders(client_env):
    client, _agent, headers, _b = client_env
    hook = _create(
        client, headers, event="post_tool_use", text="ran {tool_name}"
    ).json()
    resp = client.post(f"/hooks/{hook['id']}/test", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["rendered"] == "ran Edit"  # sample tool_name


def test_cross_user_isolation(client_env):
    client, agent, headers, builder = client_env
    hook = _create(client, headers, name="owners-hook").json()
    # A second authenticated user must not see or fetch the first user's hook.
    agent.accounts_repo.create_user("intruder", "intruder@example.com", "Intruder")
    other_headers = builder.auth(agent.accounts_repo.issue_token("intruder"))
    assert client.get("/hooks", headers=other_headers).json() == []
    assert client.get(f"/hooks/{hook['id']}", headers=other_headers).status_code == 404
