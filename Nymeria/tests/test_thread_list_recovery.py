"""Regression tests for recovered thread-list rows."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from nymeria.core.accounts import AccountsRepo
from nymeria.core.chat_bindings import ChatBindingsRepo
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.core.todo_manager import TodoManager
from nymeria.core.todo_schedule_db import TodoScheduleDB
from nymeria.core.trigger_manager import TriggerManager
from nymeria.triggers import api as api_module


@dataclass
class FakeSettings:
    data_dir: Path
    database_backend: str = "sqlite"
    postgres_uri: str | None = None
    redis_enabled: bool = False
    redis_url: str | None = None
    fcm_enabled: bool = False
    fcm_credentials_json: str | None = None
    cors_origins_list: list[str] | None = None

    def __post_init__(self):
        if self.cors_origins_list is None:
            self.cors_origins_list = ["*"]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nymeria.db"


class FakeAgent:
    def __init__(self, data_dir: Path):
        accounts_db = data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(accounts_db)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.todo_manager = TodoManager(data_dir)
        self._schedule_db = TodoScheduleDB(data_dir / "todo_schedule.db")
        self.trigger_manager = TriggerManager(data_dir)
        self.synced_tools = 0

    def sync_agent_tools(self):
        self.synced_tools += 1


def _client(tmp_path: Path, monkeypatch) -> tuple[TestClient, FakeAgent]:
    settings = FakeSettings(tmp_path)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("CREATE TABLE checkpoints (thread_id TEXT)")
        conn.commit()
    agent = FakeAgent(tmp_path)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    app = api_module.create_api_app(agent)
    return TestClient(app), agent


def test_threads_omits_recovered_metadata_owned_by_another_user(tmp_path: Path, monkeypatch):
    client, agent = _client(tmp_path, monkeypatch)
    agent.accounts_repo.create_user("bob", "bob@example.com", "Aria")
    agent.accounts_repo.create_user("other", "other@example.com", "Other")
    token = agent.accounts_repo.issue_token("bob")

    agent.accounts_repo.claim_thread("owned-thread", "bob")
    agent.accounts_repo.claim_thread("other-thread", "other")
    agent.thread_metadata_manager.upsert_thread("bob", "owned-thread", title="Owned")
    agent.thread_metadata_manager.upsert_thread("bob", "other-thread", title="Zombie")

    response = client.get("/threads", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    rows = {row["thread_id"]: row for row in response.json()["threads"]}
    assert "owned-thread" in rows
    assert "other-thread" not in rows


def test_threads_keeps_ownerless_metadata_recovery_rows(tmp_path: Path, monkeypatch):
    client, agent = _client(tmp_path, monkeypatch)
    agent.accounts_repo.create_user("bob", "bob@example.com", "Aria")
    token = agent.accounts_repo.issue_token("bob")
    agent.thread_metadata_manager.upsert_thread("bob", "legacy-thread", title="Legacy")

    response = client.get("/threads", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    rows = {row["thread_id"]: row for row in response.json()["threads"]}
    assert rows["legacy-thread"]["recovered"] is True
    assert rows["legacy-thread"]["recovery_sources"] == ["metadata"]


def test_threads_use_telegram_platform_for_bound_desktop_threads(tmp_path: Path, monkeypatch):
    client, agent = _client(tmp_path, monkeypatch)
    agent.accounts_repo.create_user("bob", "bob@example.com", "Aria")
    token = agent.accounts_repo.issue_token("bob")
    thread_id = "desktop-thread"
    agent.accounts_repo.claim_thread(thread_id, "bob")
    agent.thread_metadata_manager.upsert_thread("bob", thread_id, title="Bound")
    agent.chat_bindings_repo.create_thread_binding(
        thread_id=thread_id,
        provider="telegram",
        platform_chat_id="5551234567",
        user_id="bob",
    )

    response = client.get("/threads", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    rows = {row["thread_id"]: row for row in response.json()["threads"]}
    assert rows[thread_id]["platform"] == "telegram"
    assert rows[thread_id]["callable"] is False


def test_threads_keep_telegram_platform_for_bound_callable_threads(tmp_path: Path, monkeypatch):
    client, agent = _client(tmp_path, monkeypatch)
    agent.accounts_repo.create_user("bob", "bob@example.com", "Aria")
    token = agent.accounts_repo.issue_token("bob")
    thread_id = "desktop-thread"
    agent.accounts_repo.claim_thread(thread_id, "bob")
    agent.thread_metadata_manager.upsert_thread("bob", thread_id, title="Bound")
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=thread_id, callable=True, callable_name="TelegramAgent")
    )
    agent.chat_bindings_repo.create_thread_binding(
        thread_id=thread_id,
        provider="telegram",
        platform_chat_id="5551234567",
        user_id="bob",
    )

    response = client.get("/threads", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    rows = {row["thread_id"]: row for row in response.json()["threads"]}
    assert rows[thread_id]["platform"] == "telegram"
    assert rows[thread_id]["callable"] is True
    assert rows[thread_id]["title"] == "TelegramAgent"


def test_threads_keep_telegram_platform_for_native_callable_threads(tmp_path: Path, monkeypatch):
    client, agent = _client(tmp_path, monkeypatch)
    agent.accounts_repo.create_user("bob", "bob@example.com", "Aria")
    token = agent.accounts_repo.issue_token("bob")
    thread_id = "telegram_5551234567"
    agent.accounts_repo.claim_thread(thread_id, "bob")
    agent.thread_metadata_manager.upsert_thread(
        "bob",
        thread_id,
        title="Native Telegram",
        platform="telegram",
    )
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=thread_id, callable=True, callable_name="NativeTelegramAgent")
    )

    response = client.get("/threads", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    rows = {row["thread_id"]: row for row in response.json()["threads"]}
    assert rows[thread_id]["platform"] == "telegram"
    assert rows[thread_id]["callable"] is True
    assert rows[thread_id]["title"] == "NativeTelegramAgent"


def test_threads_revert_bound_desktop_platform_after_unbind(tmp_path: Path, monkeypatch):
    client, agent = _client(tmp_path, monkeypatch)
    agent.accounts_repo.create_user("bob", "bob@example.com", "Aria")
    token = agent.accounts_repo.issue_token("bob")
    thread_id = "desktop-thread"
    agent.accounts_repo.claim_thread(thread_id, "bob")
    agent.thread_metadata_manager.upsert_thread("bob", thread_id, title="Bound")
    binding = agent.chat_bindings_repo.create_thread_binding(
        thread_id=thread_id,
        provider="telegram",
        platform_chat_id="5551234567",
        user_id="bob",
    )
    agent.chat_bindings_repo.delete_thread_binding(binding.id, user_id="bob")

    response = client.get("/threads", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    rows = {row["thread_id"]: row for row in response.json()["threads"]}
    assert rows[thread_id]["platform"] == "desktop"


def test_admin_chatapp_switch_moves_binding_between_owned_threads(
    tmp_path: Path, monkeypatch
):
    client, agent = _client(tmp_path, monkeypatch)
    agent.accounts_repo.create_user("admin", "admin@example.com", "Admin", role="admin")
    agent.accounts_repo.create_user("bob", "bob@example.com", "Aria")
    admin_token = agent.accounts_repo.issue_token("admin")
    agent.accounts_repo.claim_thread("old-thread", "bob")
    agent.accounts_repo.claim_thread("new-thread", "bob")
    agent.chat_bindings_repo.create_thread_binding(
        thread_id="old-thread",
        provider="telegram",
        platform_chat_id="5551234567",
        user_id="bob",
    )

    response = client.post(
        "/admin/chatapp/bindings/switch",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "provider": "telegram",
            "platform_chat_id": "5551234567",
            "thread_id": "new-thread",
            "user_id": "bob",
        },
    )

    assert response.status_code == 200
    assert response.json()["thread_id"] == "new-thread"
    assert response.json()["previous_thread_id"] == "old-thread"
    assert (
        agent.chat_bindings_repo.lookup_thread_binding_by_chat(
            "telegram", "5551234567"
        ).thread_id
        == "new-thread"
    )
    assert agent.chat_bindings_repo.lookup_thread_binding_by_thread("telegram", "old-thread") is None


def test_admin_chatapp_switch_rejects_thread_already_bound_elsewhere(
    tmp_path: Path, monkeypatch
):
    client, agent = _client(tmp_path, monkeypatch)
    agent.accounts_repo.create_user("admin", "admin@example.com", "Admin", role="admin")
    agent.accounts_repo.create_user("bob", "bob@example.com", "Aria")
    admin_token = agent.accounts_repo.issue_token("admin")
    agent.accounts_repo.claim_thread("old-thread", "bob")
    agent.accounts_repo.claim_thread("new-thread", "bob")
    agent.chat_bindings_repo.create_thread_binding(
        thread_id="old-thread",
        provider="telegram",
        platform_chat_id="111",
        user_id="bob",
    )
    agent.chat_bindings_repo.create_thread_binding(
        thread_id="new-thread",
        provider="telegram",
        platform_chat_id="222",
        user_id="bob",
    )

    response = client.post(
        "/admin/chatapp/bindings/switch",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "provider": "telegram",
            "platform_chat_id": "111",
            "thread_id": "new-thread",
            "user_id": "bob",
        },
    )

    assert response.status_code == 409
    assert (
        agent.chat_bindings_repo.lookup_thread_binding_by_chat("telegram", "111").thread_id
        == "old-thread"
    )
    assert (
        agent.chat_bindings_repo.lookup_thread_binding_by_chat("telegram", "222").thread_id
        == "new-thread"
    )


def test_admin_chatapp_switch_rejects_native_platform_targets(
    tmp_path: Path, monkeypatch
):
    client, agent = _client(tmp_path, monkeypatch)
    agent.accounts_repo.create_user("admin", "admin@example.com", "Admin", role="admin")
    agent.accounts_repo.create_user("bob", "bob@example.com", "Aria")
    admin_token = agent.accounts_repo.issue_token("admin")
    agent.accounts_repo.claim_thread("telegram_5551234567", "bob")

    response = client.post(
        "/admin/chatapp/bindings/switch",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "provider": "telegram",
            "platform_chat_id": "5551234567",
            "thread_id": "telegram_5551234567",
            "user_id": "bob",
        },
    )

    assert response.status_code == 400
