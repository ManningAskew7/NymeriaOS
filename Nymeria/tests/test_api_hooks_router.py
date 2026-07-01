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
    resp = _create(client, headers, event="not_an_event")
    assert resp.status_code == 422  # schema Literal rejects it before the store


def test_create_rejects_illegal_action_for_event(client_env):
    # inject_context is not legal on pre_tool_use -> manager raises -> 400.
    client, _agent, headers, _b = client_env
    resp = _create(client, headers, event="pre_tool_use", action="inject_context", text="x")
    assert resp.status_code == 400


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


def test_create_block_if_matches(client_env):
    client, _agent, headers, _b = client_env
    resp = _create(
        client, headers, name="guard", event="pre_tool_use", action="block_if_matches",
        text=None, matcher="bash",
        conditions=[{"field": "command", "operator": "contains", "value": "rm -rf"}],
        reason="no destructive commands",
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["action"] == "block_if_matches"
    assert body["matcher"] == "bash"
    assert body["logic"]["reason"] == "no destructive commands"
    assert body["logic"]["conditions"][0]["field"] == "command"
    assert body["text"] == ""  # non-text action


def test_create_rewrite_arg(client_env):
    client, _agent, headers, _b = client_env
    resp = _create(
        client, headers, name="clamp", event="pre_tool_use", action="rewrite_arg",
        text=None, updates={"command": "echo blocked"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["logic"]["updates"] == {"command": "echo blocked"}


def test_test_endpoint_describes_block(client_env):
    client, _agent, headers, _b = client_env
    hook = _create(
        client, headers, name="guard", event="pre_tool_use", action="block_if_matches",
        text=None, matcher="bash",
        conditions=[{"field": "command", "operator": "contains", "value": "rm"}],
    ).json()
    resp = client.post(f"/hooks/{hook['id']}/test", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["action"] == "block_if_matches"
    assert "rm" in resp.json()["rendered"]


def test_update_block_conditions(client_env):
    client, _agent, headers, _b = client_env
    hook = _create(
        client, headers, name="guard", event="pre_tool_use", action="block_if_matches",
        text=None, conditions=[{"field": "command", "operator": "contains", "value": "a"}],
    ).json()
    resp = client.patch(
        f"/hooks/{hook['id']}", headers=headers,
        json={"conditions": [], "reason": "always deny"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["logic"]["reason"] == "always deny"
    assert resp.json()["logic"]["conditions"] == []


def test_partial_patch_preserves_sibling_subfields(client_env):
    # A partial PATCH to a multi-field action must not wipe the field it omits.
    client, _agent, headers, _b = client_env
    hook = _create(
        client, headers, name="guard", event="pre_tool_use", action="block_if_matches",
        text=None, reason="Dangerous!",
        conditions=[{"field": "command", "operator": "contains", "value": "rm -rf"}],
    ).json()
    # Tighten only the condition; the reason must survive.
    resp = client.patch(
        f"/hooks/{hook['id']}", headers=headers,
        json={"conditions": [{"field": "command", "operator": "contains", "value": "rm -rf /"}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["logic"]["reason"] == "Dangerous!"  # preserved
    assert resp.json()["logic"]["conditions"][0]["value"] == "rm -rf /"
    # Change only the reason; the condition must survive.
    resp2 = client.patch(f"/hooks/{hook['id']}", headers=headers, json={"reason": "Nope"})
    assert resp2.status_code == 200
    assert resp2.json()["logic"]["reason"] == "Nope"
    assert resp2.json()["logic"]["conditions"][0]["value"] == "rm -rf /"  # preserved


def test_patch_switch_action_uses_fresh_params(client_env):
    # Switching action replaces the logic wholesale with the new action's params.
    client, _agent, headers, _b = client_env
    hook = _create(
        client, headers, name="g", event="pre_tool_use", action="block_if_matches",
        text=None, reason="x",
        conditions=[{"field": "command", "operator": "contains", "value": "a"}],
    ).json()
    resp = client.patch(
        f"/hooks/{hook['id']}", headers=headers,
        json={"action": "rewrite_arg", "updates": {"command": "echo hi"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "rewrite_arg"
    assert resp.json()["logic"]["updates"] == {"command": "echo hi"}


def test_create_notify(client_env):
    client, _agent, headers, _b = client_env
    resp = _create(
        client, headers, name="ping", event="done", action="notify",
        text="finished: {final_text}",
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["action"] == "notify"
    assert resp.json()["text"] == "finished: {final_text}"


def test_create_webhook(client_env):
    client, _agent, headers, _b = client_env
    resp = _create(
        client, headers, name="wh", event="done", action="webhook",
        text="body", url="https://example.com/hook",
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["logic"]["url"] == "https://example.com/hook"
    assert resp.json()["logic"]["text"] == "body"


def test_create_notify_illegal_on_prompt_submit(client_env):
    client, _agent, headers, _b = client_env
    resp = _create(client, headers, event="prompt_submit", action="notify", text="x")
    assert resp.status_code == 400  # legality rejected by the manager


def test_webhook_test_endpoint_describes(client_env):
    client, _agent, headers, _b = client_env
    hook = _create(
        client, headers, name="wh", event="done", action="webhook",
        text="ran {tool_name}", url="https://example.com/{thread_id}",
    ).json()
    resp = client.post(f"/hooks/{hook['id']}/test", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["action"] == "webhook"
    assert "example.com" in resp.json()["rendered"]


def test_update_webhook_preserves_url_on_text_only_patch(client_env):
    client, _agent, headers, _b = client_env
    hook = _create(
        client, headers, name="wh", event="done", action="webhook",
        text="old", url="https://example.com/keep",
    ).json()
    resp = client.patch(f"/hooks/{hook['id']}", headers=headers, json={"text": "new"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["logic"]["text"] == "new"
    assert resp.json()["logic"]["url"] == "https://example.com/keep"  # preserved


def test_cross_user_isolation(client_env):
    client, agent, headers, builder = client_env
    hook = _create(client, headers, name="owners-hook").json()
    # A second authenticated user must not see or fetch the first user's hook.
    agent.accounts_repo.create_user("intruder", "intruder@example.com", "Intruder")
    other_headers = builder.auth(agent.accounts_repo.issue_token("intruder"))
    assert client.get("/hooks", headers=other_headers).json() == []
    assert client.get(f"/hooks/{hook['id']}", headers=other_headers).status_code == 404
