from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from nymeria.core.accounts import AccountsRepo, TokenLimitExceeded


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


def test_account_tokens_expire_and_active_token_cap_is_enforced(tmp_path: Path):
    repo = AccountsRepo(
        tmp_path / "accounts.db",
        token_ttl_days=1,
        max_active_tokens_per_user=2,
    )
    repo.create_user("owner", "owner@example.com", "Owner")
    first = repo.issue_token("owner", label="first")
    second = repo.issue_token("owner", label="second")

    with pytest.raises(TokenLimitExceeded):
        repo.issue_token("owner", label="third")

    first_hash = repo.list_tokens_for_user("owner")[0].token_hash
    with sqlite3.connect(tmp_path / "accounts.db") as conn:
        conn.execute(
            "UPDATE user_tokens SET expires_at = ? WHERE token_hash = ?",
            ("2000-01-01T00:00:00+00:00", first_hash),
        )
        conn.commit()

    assert repo.verify_token(first) is None
    assert repo.verify_token(second) is not None
    third = repo.issue_token("owner", label="third")
    assert repo.verify_token(third) is not None


def test_bootstrap_token_file_is_deleted_after_successful_auth(tmp_path: Path):
    repo = AccountsRepo(tmp_path / "accounts.db")
    raw = repo.ensure_bootstrap_admin(tmp_path)
    token_path = tmp_path / "BOOTSTRAP_TOKEN.txt"

    assert raw is not None
    assert token_path.exists()
    assert repo.verify_token(raw) is not None
    assert not token_path.exists()


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
        "/admin/users/owner/platforms/not-a-provider/123",
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


# ---------------------------------------------------------------------------
# Identity ids are canonical storage segments (spec behaviors 1, 4, 6)
# ---------------------------------------------------------------------------


def _data_files(root: Path) -> list[str]:
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if not p.name.startswith("accounts.db")
    )


@pytest.mark.parametrize(
    ("user_id", "segment"),
    [
        ("alice.smith", "alicesmith"),
        ("a/b", "ab"),
        ("..", "default"),
        ("!!!", "default"),
        ("   ", "default"),
    ],
)
def test_admin_create_user_refuses_non_canonical_id(
    tmp_path: Path,
    api_client_builder,
    user_id: str,
    segment: str,
):
    client, agent = _client(tmp_path, api_client_builder)
    admin_token = _create_user(agent, "default", role="admin")
    users_before = [u.id for u in agent.accounts_repo.list_users()]
    files_before = _data_files(tmp_path)

    response = client.post(
        "/admin/users",
        headers=api_client_builder.auth(admin_token),
        json={"email": "alice@example.com", "id": user_id},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert f"stored as {segment!r}" in detail
    assert "letters, digits, '-' and '_'" in detail
    # No row, no token, no file: the request left nothing behind.
    assert [u.id for u in agent.accounts_repo.list_users()] == users_before
    assert agent.accounts_repo.get_user_by_email("alice@example.com") is None
    assert _data_files(tmp_path) == files_before
    # The owner's account (segment "default") is untouched by the fold.
    owner = agent.accounts_repo.get_user_by_id("default")
    assert (owner.email, owner.role) == ("default@example.com", "admin")
    assert len(agent.accounts_repo.list_tokens_for_user("default")) == 1


def test_admin_create_user_accepts_canonical_id_and_blank_id_falls_back(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(_create_user(agent, "admin", role="admin"))

    explicit = client.post(
        "/admin/users",
        headers=headers,
        json={"email": "alice.smith@example.com", "id": "alice-smith"},
    )
    blank = client.post(
        "/admin/users",
        headers=headers,
        json={"email": "bob.jones@example.com", "id": ""},
    )

    assert explicit.status_code == 200
    assert agent.accounts_repo.verify_token(explicit.json()["raw_token"]).id == "alice-smith"
    assert blank.status_code == 200
    # An empty id is "no id": the email-derived slug (already canonical) applies.
    assert agent.accounts_repo.verify_token(blank.json()["raw_token"]).id == "bobjones"


def test_admin_create_user_refuses_case_variant_of_existing_id(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(_create_user(agent, "admin", role="admin"))
    _create_user(agent, "alice")

    response = client.post(
        "/admin/users",
        headers=headers,
        json={"email": "alice.two@example.com", "id": "Alice"},
    )

    assert response.status_code == 400
    assert "'alice'" in response.json()["detail"]
    assert agent.accounts_repo.get_user_by_id("Alice") is None
    assert agent.accounts_repo.get_user_by_email("alice.two@example.com") is None
