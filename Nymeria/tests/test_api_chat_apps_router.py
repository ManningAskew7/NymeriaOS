from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.routing import APIRoute

from nymeria.api.routers import chat_apps as chat_apps_router
from nymeria.core.accounts import AccountsRepo
from nymeria.core.chat_bindings import ChatBindingsRepo
from nymeria.core.thread_config import ThreadConfigManager
from nymeria.core.thread_metadata import ThreadMetadataManager


class FakeAgent:
    def __init__(self, data_dir: Path):
        accounts_db = data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(accounts_db)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.synced_tools = 0

    def sync_agent_tools(self):
        self.synced_tools += 1


class TelegramGetMeResponse:
    status_code = 200
    text = '{"ok": true, "result": {"username": "UserOwnedTestBot"}}'

    def json(self) -> dict[str, Any]:
        return {"ok": True, "result": {"username": "UserOwnedTestBot"}}


class TelegramAsyncClient:
    def __init__(self, *args: Any, **kwargs: Any):
        self.requests: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args: Any):
        return None

    async def get(self, url: str) -> TelegramGetMeResponse:
        self.requests.append(url)
        return TelegramGetMeResponse()


def _client(tmp_path: Path, api_client_builder, *, telegram_bot_username: str | None = None):
    settings = api_client_builder.settings(tmp_path)
    settings.telegram_bot_username = telegram_bot_username
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


def test_chat_app_routes_keep_tags_and_deep_link_payloads(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(
        tmp_path,
        api_client_builder,
        telegram_bot_username="@NymeriaTestBot",
    )
    token = _create_user(agent, "owner")
    agent.accounts_repo.claim_thread("desktop-thread", "owner")
    headers = api_client_builder.auth(token)

    platform_code = client.post(
        "/me/platform-link-codes",
        headers=headers,
        json={"provider": "telegram"},
    )
    thread_code = client.post(
        "/threads/desktop-thread/chatapp/bind-code",
        headers=headers,
        json={"provider": "telegram"},
    )
    bindings = client.get(
        "/threads/desktop-thread/chatapp/bindings",
        headers=headers,
    )

    assert _route_tags(client, "/admin/chatapp/bindings", "GET") == ["Admin"]
    assert _route_tags(client, "/me/platform-link-codes", "POST") == ["Auth"]
    assert _route_tags(client, "/threads/{thread_id}/chatapp/bind-code", "POST") == [
        "Threads"
    ]
    assert platform_code.status_code == 200
    assert platform_code.json()["bot_username"] == "NymeriaTestBot"
    assert platform_code.json()["deep_link"].startswith(
        "https://t.me/NymeriaTestBot?start=link_"
    )
    assert thread_code.status_code == 200
    assert thread_code.json()["deep_link"].startswith(
        "https://t.me/NymeriaTestBot?start=bind_"
    )
    assert bindings.status_code == 200
    assert bindings.json() == []


def test_chat_app_code_routes_accept_shared_bot_providers(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    agent.accounts_repo.claim_thread("desktop-thread", "owner")
    headers = api_client_builder.auth(token)

    for provider in (
        "slack",
        "matrix",
        "whatsapp",
        "messenger",
        "webex",
        "mattermost",
        "zulip",
        "rocketchat",
        "teams",
        "googlechat",
        "line",
        "signal",
    ):
        platform_code = client.post(
            "/me/platform-link-codes",
            headers=headers,
            json={"provider": provider},
        )
        thread_code = client.post(
            "/threads/desktop-thread/chatapp/bind-code",
            headers=headers,
            json={"provider": provider},
        )

        assert platform_code.status_code == 200
        assert platform_code.json()["bot_username"] is None
        assert platform_code.json()["deep_link"] is None
        assert thread_code.status_code == 200
        assert thread_code.json()["deep_link"] is None


def test_admin_claim_rejects_mismatched_identity_without_consuming_code(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    admin_token = _create_user(agent, "admin", role="admin")
    _create_user(agent, "owner")
    agent.accounts_repo.claim_thread("desktop-thread", "owner")
    code = agent.chat_bindings_repo.issue_bind_code(
        kind="thread_bind",
        provider="telegram",
        user_id="owner",
        thread_id="desktop-thread",
        ttl_seconds=600,
    )

    response = client.post(
        "/admin/chatapp/bindings/claim",
        headers=api_client_builder.auth(admin_token),
        json={
            "code": code,
            "provider": "telegram",
            "platform_chat_id": "chat-1",
            "expected_provider_user_id": "wrong-platform-user",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Code was issued by a different Nymeria account"
    claim = agent.chat_bindings_repo.inspect_bind_code(
        code, kind="thread_bind", provider="telegram"
    )
    assert claim.user_id == "owner"
    assert claim.thread_id == "desktop-thread"


def test_admin_platform_link_claim_links_and_consumes_code(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    admin_token = _create_user(agent, "admin", role="admin")
    _create_user(agent, "owner")
    code = agent.chat_bindings_repo.issue_bind_code(
        kind="platform_link",
        provider="telegram",
        user_id="owner",
        ttl_seconds=600,
    )
    payload = {
        "code": code,
        "provider": "telegram",
        "platform_user_id": "telegram-user-1",
    }

    linked = client.post(
        "/admin/platform/link-codes/claim",
        headers=api_client_builder.auth(admin_token),
        json=payload,
    )
    second_claim = client.post(
        "/admin/platform/link-codes/claim",
        headers=api_client_builder.auth(admin_token),
        json=payload,
    )

    assert linked.status_code == 200
    assert linked.json()["user_id"] == "owner"
    assert linked.json()["provider_user_id"] == "telegram-user-1"
    assert agent.accounts_repo.resolve_platform("telegram", "telegram-user-1") == "owner"
    assert second_claim.status_code == 400
    assert second_claim.json()["detail"] == "Invalid code: already used"


def test_admin_unbind_by_chat_can_be_scoped_to_binding_owner(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    admin_token = _create_user(agent, "admin", role="admin")
    _create_user(agent, "owner")
    _create_user(agent, "other")
    agent.accounts_repo.claim_thread("desktop-thread", "owner")
    agent.chat_bindings_repo.create_thread_binding(
        thread_id="desktop-thread",
        provider="telegram",
        platform_chat_id="chat-1",
        user_id="owner",
    )

    rejected = client.delete(
        "/admin/chatapp/bindings/by-chat",
        headers=api_client_builder.auth(admin_token),
        params={
            "provider": "telegram",
            "platform_chat_id": "chat-1",
            "user_id": "other",
        },
    )
    allowed = client.delete(
        "/admin/chatapp/bindings/by-chat",
        headers=api_client_builder.auth(admin_token),
        params={
            "provider": "telegram",
            "platform_chat_id": "chat-1",
            "user_id": "owner",
        },
    )

    assert rejected.status_code == 403
    assert rejected.json()["detail"] == "Binding belongs to a different user"
    assert allowed.status_code == 200
    assert allowed.json() == {"unbound": True, "thread_id": "desktop-thread"}


def test_user_owned_telegram_bot_registration_idempotency_and_delete(
    tmp_path: Path,
    monkeypatch,
    api_client_builder,
):
    monkeypatch.setattr(chat_apps_router.nymeria_secrets, "has_secrets_key", lambda: True)
    monkeypatch.setattr(
        chat_apps_router.nymeria_secrets,
        "encrypt",
        lambda token: f"cipher:{token}",
    )
    monkeypatch.setattr(
        chat_apps_router.nymeria_secrets,
        "decrypt",
        lambda ciphertext: ciphertext.removeprefix("cipher:"),
    )
    monkeypatch.setattr(chat_apps_router.httpx, "AsyncClient", TelegramAsyncClient)

    client, agent = _client(tmp_path, api_client_builder)
    owner_token = _create_user(agent, "owner")
    other_token = _create_user(agent, "other")
    admin_token = _create_user(agent, "admin", role="admin")
    agent.accounts_repo.claim_thread("desktop-thread", "owner")
    payload = {"bot_token": "123:bot-token"}

    created = client.post(
        "/me/telegram-bots",
        headers=api_client_builder.auth(owner_token),
        json=payload,
    )
    repeated = client.post(
        "/me/telegram-bots",
        headers=api_client_builder.auth(owner_token),
        json=payload,
    )
    cross_owner = client.post(
        "/me/telegram-bots",
        headers=api_client_builder.auth(other_token),
        json=payload,
    )
    listed = client.get(
        "/me/telegram-bots",
        headers=api_client_builder.auth(owner_token),
    )
    admin_listed = client.get(
        "/admin/telegram-bots",
        headers=api_client_builder.auth(admin_token),
    )
    bot_id = created.json()["id"]
    agent.chat_bindings_repo.create_thread_binding(
        thread_id="desktop-thread",
        provider="telegram",
        platform_chat_id="chat-1",
        user_id="owner",
        user_telegram_bot_id=bot_id,
    )
    deleted = client.delete(
        f"/me/telegram-bots/{bot_id}",
        headers=api_client_builder.auth(owner_token),
    )

    assert created.status_code == 200
    assert created.json()["bot_username"] == "UserOwnedTestBot"
    assert repeated.status_code == 200
    assert repeated.json()["id"] == created.json()["id"]
    assert cross_owner.status_code == 409
    assert "different account" in cross_owner.json()["detail"]
    assert listed.status_code == 200
    assert listed.json() == [created.json()]
    assert admin_listed.status_code == 200
    assert admin_listed.json()[0]["bot_token"] == "123:bot-token"
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    assert agent.chat_bindings_repo.lookup_thread_binding_by_chat(
        "telegram", "chat-1"
    ) is None
