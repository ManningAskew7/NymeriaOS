from __future__ import annotations

from pathlib import Path

from fastapi.routing import APIRoute

from nymeria.core.accounts import AccountsRepo


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.synced_tools = 0

    def sync_agent_tools(self):
        self.synced_tools += 1


def _client(tmp_path: Path, api_client_builder):
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    return api_client_builder.client(agent, settings), agent


def _create_user(agent: FakeAgent, user_id: str, *, role: str = "user") -> str:
    agent.accounts_repo.create_user(
        user_id,
        f"{user_id}@example.com",
        user_id.title(),
        role=role,
    )
    return agent.accounts_repo.issue_token(user_id)


def _route_tags(client, path: str, method: str) -> list[str]:
    for route in client.app.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method.upper() in route.methods
        ):
            return route.tags
    raise AssertionError(f"Route not found: {method} {path}")


def test_account_router_preserves_auth_and_admin_tags(
    tmp_path: Path,
    api_client_builder,
):
    client, _agent = _client(tmp_path, api_client_builder)

    assert _route_tags(client, "/me", "GET") == ["Auth"]
    assert _route_tags(client, "/me/tokens", "POST") == ["Auth"]
    assert _route_tags(client, "/platform/resolve", "GET") == ["Auth"]
    assert _route_tags(client, "/admin/users", "GET") == ["Admin"]
    assert _route_tags(client, "/admin/users/{user_id}/platforms", "POST") == [
        "Admin"
    ]


def test_self_routes_use_effective_user_and_manage_own_tokens(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    user_token = _create_user(agent, "owner")
    admin_token = _create_user(agent, "admin", role="admin")
    headers = api_client_builder.auth(user_token)

    me = client.get("/me", headers=headers)
    patched = client.patch(
        "/me",
        headers=headers,
        json={"display_name": "Updated Owner"},
    )
    empty_name = client.patch(
        "/me",
        headers=headers,
        json={"display_name": "  "},
    )
    issued = client.post(
        "/me/tokens",
        headers=headers,
        json={"label": "desktop"},
    )
    tokens = client.get("/me/tokens", headers=headers)
    revoked = client.delete(
        f"/me/tokens/{issued.json()['metadata']['token_hash_prefix']}",
        headers=headers,
    )
    act_as_me = client.get(
        "/me",
        headers=api_client_builder.auth(
            admin_token,
            **{"X-Nymeria-Act-As": "owner"},
        ),
    )

    assert me.status_code == 200
    assert me.json() == {
        "id": "owner",
        "email": "owner@example.com",
        "display_name": "Owner",
        "role": "user",
    }
    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Updated Owner"
    assert empty_name.status_code == 400
    assert empty_name.json()["detail"] == "display_name cannot be empty"

    assert issued.status_code == 200
    assert issued.json()["raw_token"].startswith("nym_")
    assert issued.json()["metadata"]["label"] == "desktop"
    assert len(issued.json()["metadata"]["token_hash_prefix"]) == 8
    assert tokens.status_code == 200
    assert {token["label"] for token in tokens.json()} == {None, "desktop"}
    assert revoked.status_code == 200
    assert revoked.json() == {"revoked": True}
    assert act_as_me.status_code == 200
    assert act_as_me.json()["id"] == "owner"
    assert act_as_me.json()["role"] == "user"


def test_admin_user_token_platform_and_delete_contracts(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    admin_token = _create_user(agent, "admin", role="admin")
    _create_user(agent, "other")
    headers = api_client_builder.auth(admin_token)

    created = client.post(
        "/admin/users",
        headers=headers,
        json={
            "email": "New.User+Test@example.com",
            "display_name": "New User",
            "role": "user",
            "token_label": "initial-label",
        },
    )
    user_id = "newusertest"
    linked = client.post(
        f"/admin/users/{user_id}/platforms",
        headers=headers,
        json={"provider": "telegram", "provider_user_id": "123"},
    )
    platforms = client.get(f"/admin/users/{user_id}/platforms", headers=headers)
    resolved = client.get(
        "/platform/resolve",
        headers=headers,
        params={"provider": "telegram", "provider_user_id": "123"},
    )
    conflict = client.post(
        "/admin/users/other/platforms",
        headers=headers,
        json={"provider": "telegram", "provider_user_id": "123"},
    )
    my_platforms = client.get(
        "/me/platforms",
        headers=api_client_builder.auth(created.json()["raw_token"]),
    )
    issued = client.post(
        f"/admin/users/{user_id}/tokens",
        headers=headers,
        json={"label": "extra"},
    )
    rotated = client.post(
        f"/admin/users/{user_id}/tokens/rotate",
        headers=headers,
        json={"label": "rotated"},
    )
    detail = client.get(f"/admin/users/{user_id}", headers=headers)
    agent.accounts_repo.claim_thread("owned-thread", user_id)
    blocked_delete = client.delete(f"/admin/users/{user_id}", headers=headers)
    agent.accounts_repo.delete_thread_owner("owned-thread")
    deleted = client.delete(f"/admin/users/{user_id}", headers=headers)

    assert created.status_code == 200
    assert created.json()["metadata"]["label"] == "initial-label"

    assert linked.status_code == 200
    assert linked.json()["provider"] == "telegram"
    assert linked.json()["provider_user_id"] == "123"
    assert platforms.status_code == 200
    assert platforms.json() == [linked.json()]
    assert resolved.status_code == 200
    assert resolved.json() == {"user_id": user_id}
    assert conflict.status_code == 409
    assert "already linked" in conflict.json()["detail"]
    assert my_platforms.status_code == 200
    assert my_platforms.json() == [linked.json()]

    assert issued.status_code == 200
    assert issued.json()["metadata"]["label"] == "extra"
    assert rotated.status_code == 200
    assert rotated.json()["metadata"]["label"] == "rotated"
    assert rotated.json()["revoked_count"] == 2
    assert detail.status_code == 200
    assert detail.json()["token_count"] == 1
    assert detail.json()["thread_count"] == 0
    assert detail.json()["platform_count"] == 1

    assert blocked_delete.status_code == 409
    assert "still owns 1 thread" in blocked_delete.json()["detail"]
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    assert agent.accounts_repo.get_user_by_id(user_id) is None


def test_admin_account_guards_and_prefix_errors_remain_stable(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    admin_token = _create_user(agent, "admin", role="admin")
    _create_user(agent, "owner")
    headers = api_client_builder.auth(admin_token)

    demote_last_admin = client.patch(
        "/admin/users/admin",
        headers=headers,
        json={"role": "user"},
    )
    disable_last_admin = client.patch(
        "/admin/users/admin",
        headers=headers,
        json={"disabled": True},
    )
    missing_token = client.delete(
        "/admin/users/owner/tokens/deadbeef",
        headers=headers,
    )
    ambiguous_prefix = client.delete(
        "/admin/users/owner/tokens/abc",
        headers=headers,
    )
    missing_user_tokens = client.get("/admin/users/missing/tokens", headers=headers)
    bad_provider = client.delete(
        "/admin/users/owner/platforms/slack/123",
        headers=headers,
    )

    assert demote_last_admin.status_code == 409
    assert "zero enabled admins" in demote_last_admin.json()["detail"]
    assert disable_last_admin.status_code == 409
    assert "zero enabled admins" in disable_last_admin.json()["detail"]
    assert missing_token.status_code == 404
    assert missing_token.json()["detail"] == "Token not found"
    assert ambiguous_prefix.status_code == 400
    assert ambiguous_prefix.json()["detail"] == "Token prefix must be at least 4 chars"
    assert missing_user_tokens.status_code == 404
    assert missing_user_tokens.json()["detail"] == "User not found"
    assert bad_provider.status_code == 400
    assert bad_provider.json()["detail"] == "Unknown provider"
