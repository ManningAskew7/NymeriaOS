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


# --- execution log ------------------------------------------------------------

def test_executions_empty_and_route_not_shadowed(client_env):
    client, _agent, headers, _b = client_env
    # The literal /hooks/executions route must win over /hooks/{hook_id}.
    resp = client.get("/hooks/executions", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_executions_lists_and_filters(client_env, tmp_path):
    from nymeria.core.hook_manager import HookExecution, HookManager

    client, _agent, headers, _b = client_env
    hook = _create(client, headers, name="Finisher").json()
    # Log through a manager on the same data_dir the router reads.
    mgr = HookManager(tmp_path)
    mgr.log_execution("owner", HookExecution(
        hook_id=hook["id"], hook_name="Finisher", event="done", plane="mutate",
        status="ok", detail="continue",
    ))
    mgr.log_execution("owner", HookExecution(
        hook_id="deadbeef", hook_name="Other", event="post_tool_use", plane="observe",
        status="error", detail="side effect failed", tool_name="Edit",
    ))
    mgr.flush_execution_log("owner")

    entries = client.get("/hooks/executions", headers=headers).json()
    assert len(entries) == 2
    assert entries[0]["hook_id"] == "deadbeef"  # newest first
    assert entries[0]["status"] == "error"
    assert entries[1]["detail"] == "continue"

    filtered = client.get(
        f"/hooks/executions?hook_id={hook['id']}", headers=headers
    ).json()
    assert [e["hook_id"] for e in filtered] == [hook["id"]]

    limited = client.get("/hooks/executions?limit=1", headers=headers).json()
    assert len(limited) == 1


# --- taxonomy schema ------------------------------------------------------------

def test_schema_endpoint_shape(client_env):
    client, _agent, headers, _b = client_env
    resp = client.get("/hooks/schema", headers=headers)
    assert resp.status_code == 200
    schema = resp.json()
    assert set(schema["events"]) == {"prompt_submit", "pre_tool_use", "post_tool_use", "done"}
    assert schema["events"]["pre_tool_use"]["tool_event"] is True
    assert schema["events"]["done"]["tool_event"] is False
    assert "block_if_matches" in schema["events"]["pre_tool_use"]["actions"]
    wh = schema["actions"]["webhook"]
    assert wh["plane"] == "observe"
    assert wh["events"] == ["post_tool_use", "done"]
    assert "url" in wh["params_schema"]["properties"]
    assert "action" not in wh["params_schema"]["properties"]
    inj = schema["actions"]["inject_context"]
    assert inj["text_action"] is True
    assert "contains" in schema["operators"]
    assert schema["max_hooks"] == 50


def test_schema_exposes_plane_by_event_and_gated(client_env):
    client, _agent, headers, _b = client_env
    schema = client.get("/hooks/schema", headers=headers).json()
    rc = schema["actions"]["run_command"]
    # run_command is the only gated action and flips plane per event.
    assert rc["gated"] is True
    assert rc["plane_by_event"] == {
        "prompt_submit": "mutate",
        "pre_tool_use": "mutate",
        "post_tool_use": "observe",
        "done": "observe",
    }
    # Every non-gated action stays single-plane across its legal events.
    assert schema["actions"]["notify"]["gated"] is False
    assert set(schema["actions"]["notify"]["plane_by_event"].values()) == {"observe"}


# --- run_command authoring gate -------------------------------------------------

def _client_agent_for(builder, monkeypatch, tmp_path, *, role: str, flag: bool):
    """A client whose caller has ``role`` and whose deployment flag is ``flag``.

    Points both the router's own ``get_settings`` and the config-level one used
    by ``run_command_authoring_error`` at a settings object carrying the flag.
    Also returns the agent (for account mutations, e.g. demoting the caller)
    and the live settings object (mutable, for mid-test flag flips).
    """
    from nymeria import config as config_module

    settings = builder.settings(tmp_path, hooks_run_command_enabled=flag)
    agent = FakeAgent(tmp_path)
    client, token = builder.authenticated_client(
        agent, settings, user_id="owner", email="owner@example.com", role=role
    )
    monkeypatch.setattr(hooks_router_module, "get_settings", lambda: settings)
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    return client, builder.auth(token), agent, settings


def _client_for(builder, monkeypatch, tmp_path, *, role: str, flag: bool):
    client, headers, _agent, _settings = _client_agent_for(
        builder, monkeypatch, tmp_path, role=role, flag=flag
    )
    return client, headers


def _demote_owner(agent):
    """Demote ``owner`` to a plain user (a second admin dodges the last-admin guard)."""
    agent.accounts_repo.create_user(
        "admin2", "admin2@example.com", "Admin Two", role="admin"
    )
    agent.accounts_repo.update_user("owner", role="user")


def _run_command_body(**over):
    body = {
        "name": "rc",
        "event": "done",
        "action": "run_command",
        "command": "echo hi",
        "scope": "global",
    }
    body.update(over)
    return body


def test_create_run_command_403_when_not_admin(api_client_builder, monkeypatch, tmp_path):
    client, headers = _client_for(
        api_client_builder, monkeypatch, tmp_path, role="user", flag=True
    )
    resp = client.post("/hooks", headers=headers, json=_run_command_body())
    assert resp.status_code == 403
    assert "admin-only" in resp.json()["detail"]


def test_create_run_command_400_when_flag_off(api_client_builder, monkeypatch, tmp_path):
    client, headers = _client_for(
        api_client_builder, monkeypatch, tmp_path, role="admin", flag=False
    )
    resp = client.post("/hooks", headers=headers, json=_run_command_body())
    assert resp.status_code == 400
    assert "HOOKS_RUN_COMMAND_ENABLED" in resp.json()["detail"]


def test_create_run_command_400_for_non_admin_when_flag_off(
    api_client_builder, monkeypatch, tmp_path
):
    # Flag is checked first, so a flag-off deployment is a 400 (config) even for
    # a non-admin, with the flag-off message: status and message must agree.
    client, headers = _client_for(
        api_client_builder, monkeypatch, tmp_path, role="user", flag=False
    )
    resp = client.post("/hooks", headers=headers, json=_run_command_body())
    assert resp.status_code == 400
    assert "HOOKS_RUN_COMMAND_ENABLED" in resp.json()["detail"]


def test_create_run_command_succeeds_for_admin_with_flag_on(
    api_client_builder, monkeypatch, tmp_path
):
    client, headers = _client_for(
        api_client_builder, monkeypatch, tmp_path, role="admin", flag=True
    )
    resp = client.post("/hooks", headers=headers, json=_run_command_body(timeout_seconds=15))
    assert resp.status_code == 201
    hook = resp.json()
    assert hook["action"] == "run_command"
    assert hook["logic"]["command"] == "echo hi"
    assert hook["logic"]["timeout_seconds"] == 15


def test_update_to_run_command_rejected_when_flag_off(
    api_client_builder, monkeypatch, tmp_path
):
    client, headers = _client_for(
        api_client_builder, monkeypatch, tmp_path, role="admin", flag=False
    )
    created = client.post(
        "/hooks", headers=headers,
        json={"name": "n", "event": "done", "text": "check", "scope": "global"},
    ).json()
    resp = client.patch(
        f"/hooks/{created['id']}", headers=headers,
        json={"action": "run_command", "command": "echo hi"},
    )
    assert resp.status_code == 400
    assert "HOOKS_RUN_COMMAND_ENABLED" in resp.json()["detail"]


def test_update_run_command_command_403_for_demoted_owner(
    api_client_builder, monkeypatch, tmp_path
):
    # In-place behavior edits of a stored run_command hook re-gate on role:
    # authoring-time admin is not a permanent pass (e.g. the owner was demoted).
    client, headers, agent, _settings = _client_agent_for(
        api_client_builder, monkeypatch, tmp_path, role="admin", flag=True
    )
    created = client.post("/hooks", headers=headers, json=_run_command_body()).json()
    _demote_owner(agent)
    resp = client.patch(
        f"/hooks/{created['id']}", headers=headers, json={"command": "echo pwned"}
    )
    assert resp.status_code == 403
    assert "admin-only" in resp.json()["detail"]
    unchanged = client.get(f"/hooks/{created['id']}", headers=headers).json()
    assert unchanged["logic"]["command"] == "echo hi"


def test_update_run_command_command_400_when_flag_turned_off(
    api_client_builder, monkeypatch, tmp_path
):
    # Flipping the deployment flag off freezes behavior edits of stored
    # run_command hooks (flag precedence: 400, not 403, even for an admin).
    client, headers, _agent, settings = _client_agent_for(
        api_client_builder, monkeypatch, tmp_path, role="admin", flag=True
    )
    created = client.post("/hooks", headers=headers, json=_run_command_body()).json()
    settings.hooks_run_command_enabled = False
    resp = client.patch(
        f"/hooks/{created['id']}", headers=headers, json={"command": "echo bye"}
    )
    assert resp.status_code == 400
    assert "HOOKS_RUN_COMMAND_ENABLED" in resp.json()["detail"]


def test_update_run_command_enabled_toggle_allowed_for_non_admin(
    api_client_builder, monkeypatch, tmp_path
):
    # Enabled/name-only updates stay ungated: toggling never changes what the
    # hook executes, and the GUI enable switch must keep working for the owner.
    client, headers, agent, _settings = _client_agent_for(
        api_client_builder, monkeypatch, tmp_path, role="admin", flag=True
    )
    created = client.post("/hooks", headers=headers, json=_run_command_body()).json()
    _demote_owner(agent)
    resp = client.patch(
        f"/hooks/{created['id']}", headers=headers,
        json={"enabled": False, "name": "rc-off"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["name"] == "rc-off"
    assert body["logic"]["command"] == "echo hi"


# --- hook approvals (require_approval resolve surface) ------------------------


@pytest.fixture
def approvals_env(tmp_path, api_client_builder, monkeypatch):
    """Client + isolated approval store + reset coordinator.

    The approval store resolves its dir via ``nymeria.config.get_settings``
    (not the router module's), so both are pointed at tmp_path.
    """
    import nymeria.core.hook_approvals as ha

    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent, settings, user_id="owner", email="owner@example.com"
    )
    monkeypatch.setattr(hooks_router_module, "get_settings", lambda: settings)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: settings)
    monkeypatch.setattr(ha, "_coordinator", None)
    headers = api_client_builder.auth(token)
    return client, agent, headers, api_client_builder


@pytest.fixture
def waiter_loop():
    """A live event loop standing in for the held tool call's loop.

    ``asyncio.run`` would close the loop right after minting, and resolving a
    future on a closed loop is the waiter-gone (409) path, not the happy path.
    """
    import asyncio

    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _mint_pending(loop, user_id="owner", **kw):
    from nymeria.core.hook_approvals import create_pending_approval

    async def _mint():
        return create_pending_approval(
            user_id=user_id,
            thread_id=kw.get("thread_id", "t1"),
            hook_id="h1",
            hook_name="guard",
            tool_name=kw.get("tool_name", "bash_execute"),
            tool_call_id=kw.get("tool_call_id", "call-1"),
            tool_args={"command": "ls"},
            prompt="Approve?",
            window_seconds=60.0,
        )

    return loop.run_until_complete(_mint())


def _await_result(loop, future, timeout=2.0):
    import asyncio

    return loop.run_until_complete(asyncio.wait_for(future, timeout))


def test_approvals_list_scopes_to_owner(approvals_env, waiter_loop):
    client, agent, headers, builder = approvals_env
    record, _ = _mint_pending(waiter_loop, user_id="owner")
    _mint_pending(waiter_loop, user_id="somebody-else", tool_call_id="call-2")

    resp = client.get("/hooks/approvals", headers=headers)
    assert resp.status_code == 200
    entries = resp.json()["approvals"]
    assert [e["record_id"] for e in entries] == [record["record_id"]]
    assert entries[0]["tool_call_id"] == "call-1"

    # An admin sees every user's pending approvals.
    agent.accounts_repo.create_user("boss", "boss@example.com", "Boss", role="admin")
    admin_headers = builder.auth(agent.accounts_repo.issue_token("boss"))
    all_entries = client.get("/hooks/approvals", headers=admin_headers).json()["approvals"]
    assert len(all_entries) == 2


def test_resolve_approval_wakes_waiter(approvals_env, waiter_loop):
    client, _agent, headers, _b = approvals_env
    record, future = _mint_pending(waiter_loop)
    resp = client.post(
        f"/hooks/approvals/{record['record_id']}/resolve",
        headers=headers,
        json={"approved": True, "note": "go ahead"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["decision"] == "approved"
    result = _await_result(waiter_loop, future)
    assert result["approved"] is True
    assert result["resolved_by"] == "owner"
    assert result["note"] == "go ahead"


def test_resolve_approval_404_for_missing_and_foreign(approvals_env, waiter_loop):
    client, _agent, headers, _b = approvals_env
    assert (
        client.post(
            "/hooks/approvals/nope/resolve", headers=headers, json={"approved": True}
        ).status_code
        == 404
    )
    foreign, _ = _mint_pending(waiter_loop, user_id="somebody-else", tool_call_id="call-9")
    resp = client.post(
        f"/hooks/approvals/{foreign['record_id']}/resolve",
        headers=headers,
        json={"approved": True},
    )
    assert resp.status_code == 404  # existence not leaked


def test_resolve_approval_409_when_no_waiter_and_cleans_record(approvals_env, waiter_loop):
    from nymeria.core.hook_approvals import (
        get_hook_approval_coordinator,
        load_record,
    )

    client, _agent, headers, _b = approvals_env
    record, _future = _mint_pending(waiter_loop)
    # Simulate the waiter ending (timeout/abort) with the record left behind.
    get_hook_approval_coordinator().discard(record["record_id"])
    resp = client.post(
        f"/hooks/approvals/{record['record_id']}/resolve",
        headers=headers,
        json={"approved": True},
    )
    assert resp.status_code == 409
    assert load_record(record["record_id"]) is None


def test_resolve_approval_409_when_waiter_loop_died(approvals_env):
    """A future orphaned by a dead loop is waiter-gone: 409 + cleanup, not 500."""
    import asyncio

    from nymeria.core.hook_approvals import load_record

    client, _agent, headers, _b = approvals_env
    dead_loop = asyncio.new_event_loop()
    record, _future = _mint_pending(dead_loop)
    dead_loop.close()
    resp = client.post(
        f"/hooks/approvals/{record['record_id']}/resolve",
        headers=headers,
        json={"approved": True},
    )
    assert resp.status_code == 409
    assert load_record(record["record_id"]) is None


def test_admin_may_resolve_another_users_approval(approvals_env, waiter_loop):
    client, agent, _headers, builder = approvals_env
    record, future = _mint_pending(waiter_loop, user_id="somebody-else")
    agent.accounts_repo.create_user("boss", "boss@example.com", "Boss", role="admin")
    admin_headers = builder.auth(agent.accounts_repo.issue_token("boss"))
    resp = client.post(
        f"/hooks/approvals/{record['record_id']}/resolve",
        headers=admin_headers,
        json={"approved": False, "note": "not now"},
    )
    assert resp.status_code == 200
    result = _await_result(waiter_loop, future)
    assert result["approved"] is False
    assert result["resolved_by"] == "boss"


# --- Definition-level fire gate (fire_conditions + once) ----------------------

def test_create_with_fire_gate_roundtrips(client_env):
    client, _agent, headers, _b = client_env
    resp = _create(
        client, headers,
        name="advisory", event="post_tool_use",
        text="wrap up ({context_pct_of_trigger}%)",
        fire_conditions=[
            {"field": "context_pct_of_trigger", "operator": "gte", "value": "85"}
        ],
        once=True,
    )
    assert resp.status_code == 201
    hook = resp.json()
    assert hook["once"] is True
    assert hook["fire_conditions"][0]["operator"] == "gte"
    got = client.get(f"/hooks/{hook['id']}", headers=headers).json()
    assert got["once"] is True
    assert got["fire_conditions"][0]["field"] == "context_pct_of_trigger"


def test_patch_fire_gate(client_env):
    client, _agent, headers, _b = client_env
    hook = _create(client, headers).json()
    resp = client.patch(
        f"/hooks/{hook['id']}", headers=headers,
        json={
            "once": True,
            "fire_conditions": [
                {"field": "final_text", "operator": "contains", "value": "error"}
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["once"] is True
    assert body["fire_conditions"][0]["field"] == "final_text"


def test_create_rejects_bad_fire_gate_operator(client_env):
    client, _agent, headers, _b = client_env
    resp = _create(
        client, headers,
        fire_conditions=[{"field": "x", "operator": "sideways", "value": "1"}],
    )
    assert resp.status_code == 422  # pydantic rejects the unknown operator


def test_schema_exposes_fire_gate_and_numeric_operators(client_env):
    client, _agent, headers, _b = client_env
    schema = client.get("/hooks/schema", headers=headers).json()
    for op in ("gt", "gte", "lt", "lte"):
        assert op in schema["operators"]
    gate = schema["fire_gate"]
    assert gate["fields"] == ["fire_conditions", "once"]
    assert "context_pct_of_trigger" in gate["context_fields"]
    assert "tool_name" in gate["meta_fields"]
    assert gate["args_prefix"] == "args."


def test_templates_list_and_install(client_env):
    client, _agent, headers, _b = client_env
    listing = client.get("/hooks/templates", headers=headers)
    assert listing.status_code == 200
    ids = [t["id"] for t in listing.json()["templates"]]
    assert "context-checkpoint-advisory" in ids

    resp = client.post(
        "/hooks/templates/context-checkpoint-advisory/install", headers=headers, json={}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True
    assert body["hook"]["template"] == "context-checkpoint-advisory"
    assert body["hook"]["scope"] == "global"
    assert body["hook"]["once"] is True

    again = client.post(
        "/hooks/templates/context-checkpoint-advisory/install", headers=headers, json={}
    )
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["hook"]["id"] == body["hook"]["id"]


def test_template_install_thread_scope_requires_thread_id(client_env):
    client, _agent, headers, _b = client_env
    resp = client.post(
        "/hooks/templates/context-checkpoint-advisory/install",
        headers=headers,
        json={"scope": "thread"},
    )
    assert resp.status_code == 400
    assert "thread_id" in resp.json()["detail"]


def test_template_install_unknown_is_400(client_env):
    client, _agent, headers, _b = client_env
    resp = client.post("/hooks/templates/nope/install", headers=headers, json={})
    assert resp.status_code == 400
    assert "Unknown template" in resp.json()["detail"]


def test_create_single_use_and_schema_lifecycle(client_env):
    client, _agent, headers, _b = client_env
    resp = _create(client, headers, name="oneshot", single_use=True)
    assert resp.status_code == 201
    assert resp.json()["single_use"] is True

    patched = client.patch(
        f"/hooks/{resp.json()['id']}", headers=headers, json={"single_use": False}
    )
    assert patched.status_code == 200
    assert patched.json()["single_use"] is False

    schema = client.get("/hooks/schema", headers=headers).json()
    assert schema["lifecycle"]["fields"] == ["single_use"]


# --- run_workflow authoring ------------------------------------------------------

def _pass_workflow_binding(monkeypatch, error=None):
    """Stub the bind-time workflow validation (no published workflows in tests)."""
    monkeypatch.setattr(
        "nymeria.core.workflows.tool_runtime.workflow_binding_error",
        lambda workflow_id, params, allow_event=False: error,
    )


def _run_workflow_body(**over):
    body = {
        "name": "wf-guard",
        "event": "pre_tool_use",
        "action": "run_workflow",
        "matcher": "Bash",
        "workflow_id": "wf_guard",
        "workflow_params": {"mode": "strict"},
        "on_fault": "deny",
        "timeout_seconds": 45,
        "scope": "global",
    }
    body.update(over)
    return body


def test_create_run_workflow_roundtrip(client_env, monkeypatch):
    client, _agent, headers, _b = client_env
    _pass_workflow_binding(monkeypatch)
    resp = client.post("/hooks", headers=headers, json=_run_workflow_body())
    assert resp.status_code == 201
    hook = resp.json()
    assert hook["action"] == "run_workflow"
    assert hook["logic"]["workflow_id"] == "wf_guard"
    assert hook["logic"]["params"] == {"mode": "strict"}
    assert hook["logic"]["on_fault"] == "deny"
    assert hook["logic"]["timeout_seconds"] == 45
    assert hook["text"] == ""


def test_create_run_workflow_binding_rejected_400(client_env, monkeypatch):
    client, _agent, headers, _b = client_env
    _pass_workflow_binding(monkeypatch, "no published workflow tool named 'wf_guard'")
    resp = client.post("/hooks", headers=headers, json=_run_workflow_body())
    assert resp.status_code == 400
    assert "no published workflow tool named 'wf_guard'" in resp.json()["detail"]


def test_patch_run_workflow_partial_merge(client_env, monkeypatch):
    client, _agent, headers, _b = client_env
    _pass_workflow_binding(monkeypatch)
    created = client.post("/hooks", headers=headers, json=_run_workflow_body()).json()
    patched = client.patch(
        f"/hooks/{created['id']}", headers=headers,
        json={"workflow_params": {"mode": "lax"}, "on_fault": "allow"},
    )
    assert patched.status_code == 200
    logic = patched.json()["logic"]
    assert logic["params"] == {"mode": "lax"}
    assert logic["on_fault"] == "allow"
    assert logic["workflow_id"] == "wf_guard"  # untouched fields survive the merge


def test_patch_run_workflow_revalidates_binding(client_env, monkeypatch):
    client, _agent, headers, _b = client_env
    _pass_workflow_binding(monkeypatch)
    created = client.post("/hooks", headers=headers, json=_run_workflow_body()).json()
    _pass_workflow_binding(monkeypatch, "approval_required - revision not approved")
    patched = client.patch(
        f"/hooks/{created['id']}", headers=headers,
        json={"workflow_params": {"mode": "lax"}},
    )
    assert patched.status_code == 400
    assert "approval_required" in patched.json()["detail"]


def test_schema_exposes_run_workflow(client_env):
    client, _agent, headers, _b = client_env
    schema = client.get("/hooks/schema", headers=headers).json()
    rw = schema["actions"]["run_workflow"]
    assert rw["gated"] is False  # the workflow approval gate is the control
    assert rw["events"] == ["prompt_submit", "pre_tool_use", "post_tool_use", "done"]
    assert rw["plane_by_event"] == {
        "prompt_submit": "mutate",
        "pre_tool_use": "mutate",
        "post_tool_use": "observe",
        "done": "observe",
    }
    assert "workflow_id" in rw["params_schema"]["properties"]
