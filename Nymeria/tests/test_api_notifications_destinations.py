"""#263: deleting a seeded default destination sticks across later listings.

``GET /notifications/destinations`` auto-seeds defaults from the global env
config on every call, so before the tombstone a ``DELETE`` of
``telegram-default`` was undone by the next listing. These tests drive the
real router against a temp repo and settings that carry a Telegram config.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet

from nymeria.api.routers import notifications_config as notifications_config_module
from nymeria.core.accounts import AccountsRepo
from nymeria.core.chat_bindings import ChatBindingsRepo
from nymeria.core.notification_destinations import NotificationDestinationsRepo


class FakeAgent:
    def __init__(self, data_dir: Path):
        accounts_db = data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(accounts_db)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)

    def sync_agent_tools(self):
        pass


def _client(tmp_path, api_client_builder, monkeypatch):
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", Fernet.generate_key().decode())
    settings = api_client_builder.settings(tmp_path)
    # The auto-seed reads these with getattr, so a plain attribute is enough.
    settings.telegram_bot_token = "123:abc"
    settings.telegram_default_chat_id = "999"
    repo = NotificationDestinationsRepo(tmp_path / "accounts.db")
    monkeypatch.setattr(
        notifications_config_module, "get_destinations_repo", lambda: repo,
    )
    agent = FakeAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent, settings, user_id="owner",
    )
    return client, api_client_builder.auth(token)


def _names(client, headers):
    resp = client.get("/notifications/destinations", headers=headers)
    assert resp.status_code == 200, resp.text
    return {d["name"]: d["id"] for d in resp.json()["destinations"]}


def test_deleting_a_seeded_default_sticks_across_listings(
    tmp_path, api_client_builder, monkeypatch,
):
    client, headers = _client(tmp_path, api_client_builder, monkeypatch)

    first = _names(client, headers)
    assert "telegram-default" in first

    resp = client.delete(
        f"/notifications/destinations/{first['telegram-default']}", headers=headers,
    )
    assert resp.status_code == 200, resp.text

    assert "telegram-default" not in _names(client, headers)
    assert "telegram-default" not in _names(client, headers)

    resp = client.get("/notifications/profiles", headers=headers)
    assert resp.status_code == 200, resp.text
    default = next(p for p in resp.json()["profiles"] if p["name"] == "default")
    assert "telegram-default" not in default["destination_names"]
