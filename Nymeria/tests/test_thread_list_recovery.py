"""Regression tests for recovered thread-list rows."""

from __future__ import annotations

from pathlib import Path

import pytest

from nymeria.core.accounts import AccountsRepo
from nymeria.core.chat_bindings import ChatBindingsRepo
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.core.todo_manager import TodoManager
from nymeria.core.todo_schedule_db import TodoScheduleDB
from nymeria.core.trigger_manager import TriggerManager


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


def _client(tmp_path: Path, api_client_builder) -> tuple[object, FakeAgent]:
    settings = api_client_builder.settings(tmp_path)
    api_client_builder.create_checkpoint_table(settings)
    agent = FakeAgent(tmp_path)
    return api_client_builder.client(agent, settings), agent


def test_threads_omits_recovered_metadata_owned_by_another_user(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
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


def test_threads_keeps_ownerless_metadata_recovery_rows(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("bob", "bob@example.com", "Aria")
    token = agent.accounts_repo.issue_token("bob")
    agent.thread_metadata_manager.upsert_thread("bob", "legacy-thread", title="Legacy")

    response = client.get("/threads", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    rows = {row["thread_id"]: row for row in response.json()["threads"]}
    assert rows["legacy-thread"]["recovered"] is True
    assert rows["legacy-thread"]["recovery_sources"] == ["metadata"]


@pytest.mark.parametrize(
    "provider",
    [
        "telegram",
        "slack",
        "whatsapp",
        "teams",
    ],
)
def test_threads_use_bound_platform_for_desktop_threads(
    provider: str,
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("bob", "bob@example.com", "Aria")
    token = agent.accounts_repo.issue_token("bob")
    thread_id = "desktop-thread"
    agent.accounts_repo.claim_thread(thread_id, "bob")
    agent.thread_metadata_manager.upsert_thread("bob", thread_id, title="Bound")
    agent.chat_bindings_repo.create_thread_binding(
        thread_id=thread_id,
        provider=provider,
        platform_chat_id=f"{provider}:chat-1",
        user_id="bob",
    )

    response = client.get("/threads", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    rows = {row["thread_id"]: row for row in response.json()["threads"]}
    assert rows[thread_id]["platform"] == provider
    assert rows[thread_id]["callable"] is False


@pytest.mark.parametrize(
    "provider",
    [
        "telegram",
        "slack",
        "whatsapp",
        "teams",
    ],
)
def test_threads_keep_bound_platform_for_callable_threads(
    provider: str,
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
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
        provider=provider,
        platform_chat_id=f"{provider}:chat-1",
        user_id="bob",
    )

    response = client.get("/threads", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    rows = {row["thread_id"]: row for row in response.json()["threads"]}
    assert rows[thread_id]["platform"] == provider
    assert rows[thread_id]["callable"] is True
    assert rows[thread_id]["title"] == "TelegramAgent"


def test_threads_keep_telegram_platform_for_native_callable_threads(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
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


def test_threads_revert_bound_desktop_platform_after_unbind(tmp_path: Path, api_client_builder):
    client, agent = _client(tmp_path, api_client_builder)
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
    tmp_path: Path, api_client_builder
):
    client, agent = _client(tmp_path, api_client_builder)
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
    tmp_path: Path, api_client_builder
):
    client, agent = _client(tmp_path, api_client_builder)
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
    tmp_path: Path, api_client_builder
):
    client, agent = _client(tmp_path, api_client_builder)
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


def _seed_checkpoint_rows(settings, thread_ids: list[str]) -> None:
    """Insert minimal rows into the checkpoints table for the orphan-visibility tests."""
    import sqlite3

    with sqlite3.connect(settings.db_path) as conn:
        conn.executemany(
            "INSERT INTO checkpoints (thread_id) VALUES (?)",
            [(tid,) for tid in thread_ids],
        )
        conn.commit()


def test_list_threads_owned_only_excludes_orphan_checkpoints_for_admin(
    tmp_path: Path, api_client_builder
):
    """
    Admin users see every orphan checkpoint thread when listing with default
    options — intentional, so operators can inspect them. owned_only=true
    must opt out of that behaviour so cleanup tooling, regression suites, and
    other automated callers never accidentally enumerate other users' threads
    just because their token has the admin role.
    """
    client, agent = _client(tmp_path, api_client_builder)
    settings = api_client_builder.settings(tmp_path)
    agent.accounts_repo.create_user(
        "admin-user", "admin@example.com", "Admin", role="admin"
    )
    token = agent.accounts_repo.issue_token("admin-user")

    # admin-user owns one thread, the database also has two orphan
    # checkpoint threads belonging to nobody recorded in thread_owners.
    agent.accounts_repo.claim_thread("mine", "admin-user")
    agent.thread_metadata_manager.upsert_thread("admin-user", "mine", title="Mine")
    _seed_checkpoint_rows(
        settings, ["mine", "orphan-checkpoint-a", "orphan-checkpoint-b"]
    )

    default_response = client.get(
        "/threads", headers={"Authorization": f"Bearer {token}"}
    )
    owned_response = client.get(
        "/threads?owned_only=true", headers={"Authorization": f"Bearer {token}"}
    )

    assert default_response.status_code == 200
    assert owned_response.status_code == 200

    default_ids = {row["thread_id"] for row in default_response.json()["threads"]}
    owned_ids = {row["thread_id"] for row in owned_response.json()["threads"]}

    # Default behaviour: admin sees the orphans (operator inspection).
    assert "orphan-checkpoint-a" in default_ids
    assert "orphan-checkpoint-b" in default_ids
    # owned_only behaviour: admin sees only their owned thread.
    assert owned_ids == {"mine"}


def test_list_threads_owned_only_for_regular_user_returns_owned_only(
    tmp_path: Path, api_client_builder
):
    """
    Regular users already don't see orphan checkpoints (no admin bypass), but
    owned_only=true still correctly returns only the threads they own — even
    when their thread_metadata store has lingering rows pointing at threads
    they no longer own (e.g. zombie metadata from a deleted callable thread).
    """
    client, agent = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("u", "u@example.com", "U")
    token = agent.accounts_repo.issue_token("u")

    agent.accounts_repo.claim_thread("owned", "u")
    agent.thread_metadata_manager.upsert_thread("u", "owned", title="Owned")
    # A lingering metadata row with no owner — would normally appear as a
    # recovered thread in the default response.
    agent.thread_metadata_manager.upsert_thread("u", "lingering", title="Lingering")

    owned_response = client.get(
        "/threads?owned_only=true", headers={"Authorization": f"Bearer {token}"}
    )

    assert owned_response.status_code == 200
    ids = {row["thread_id"] for row in owned_response.json()["threads"]}
    assert ids == {"owned"}
