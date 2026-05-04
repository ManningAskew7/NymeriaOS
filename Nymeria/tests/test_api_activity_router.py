from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nymeria.core.accounts import AccountsRepo
from nymeria.core import activity_log as activity_module
from nymeria.core import notifications as notifications_module
from nymeria.core.activity_log import ActivityType
from nymeria import config as config_module


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.synced_tools = 0

    def sync_agent_tools(self):
        self.synced_tools += 1


def _client(tmp_path: Path, monkeypatch, api_client_builder):
    settings = api_client_builder.settings(tmp_path)
    core_settings = SimpleNamespace(
        data_dir=tmp_path,
        activity_retention_hours=12,
    )
    monkeypatch.setattr(config_module, "get_settings", lambda: core_settings)
    monkeypatch.setattr(activity_module, "_activity_log", None)
    monkeypatch.setattr(notifications_module, "_notification_store", None)

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


def test_activity_route_filters_by_type_thread_and_user(
    tmp_path: Path,
    monkeypatch,
    api_client_builder,
):
    client, agent = _client(tmp_path, monkeypatch, api_client_builder)
    alice_token = _create_user(agent, "alice")
    bob_token = _create_user(agent, "bob")

    activity_module.log_activity(
        ActivityType.USER_MESSAGE,
        "alice thread one",
        user_id="alice",
        thread_id="thread-1",
        metadata={"source": "test"},
    )
    activity_module.log_activity(
        ActivityType.TODO_ADDED,
        "alice thread two",
        user_id="alice",
        thread_id="thread-2",
    )
    activity_module.log_activity(
        ActivityType.USER_MESSAGE,
        "bob thread one",
        user_id="bob",
        thread_id="thread-1",
    )

    filtered = client.get(
        "/activity",
        headers=api_client_builder.auth(alice_token),
        params={"activity_type": "user_message", "thread_id": "thread-1"},
    )
    bob_activity = client.get(
        "/activity",
        headers=api_client_builder.auth(bob_token),
        params={"thread_id": "thread-1"},
    )
    invalid_type = client.get(
        "/activity",
        headers=api_client_builder.auth(alice_token),
        params={"activity_type": "not_a_type"},
    )

    assert filtered.status_code == 200
    filtered_body = filtered.json()
    entry = filtered_body["entries"][0]
    assert filtered_body["total"] == 1
    assert entry["type"] == "user_message"
    assert entry["message"] == "alice thread one"
    assert entry["thread_id"] == "thread-1"
    assert entry["metadata"] == {"source": "test"}
    assert bob_activity.status_code == 200
    assert [entry["message"] for entry in bob_activity.json()["entries"]] == [
        "bob thread one"
    ]
    assert invalid_type.status_code == 400
    assert invalid_type.json()["detail"] == "Invalid activity type 'not_a_type'"


def test_notification_routes_are_user_scoped_and_update_read_state(
    tmp_path: Path,
    monkeypatch,
    api_client_builder,
):
    client, agent = _client(tmp_path, monkeypatch, api_client_builder)
    alice_token = _create_user(agent, "alice")
    bob_token = _create_user(agent, "bob")

    store = notifications_module.get_notification_store()
    first = store.create("alice", "First", thread_id="thread-1", task_id="todo-1")
    second = store.create("alice", "Second", thread_id="thread-2")
    bob_notification = store.create("bob", "Bob only", thread_id="thread-1")

    listed = client.get("/notifications", headers=api_client_builder.auth(alice_token))
    cross_user_read = client.post(
        f"/notifications/{bob_notification.id}/read",
        headers=api_client_builder.auth(alice_token),
    )
    marked_one = client.post(
        f"/notifications/{first.id}/read",
        headers=api_client_builder.auth(alice_token),
    )
    after_one = client.get("/notifications", headers=api_client_builder.auth(alice_token))
    marked_all = client.post(
        "/notifications/read-all",
        headers=api_client_builder.auth(alice_token),
    )
    bob_after = client.get("/notifications", headers=api_client_builder.auth(bob_token))

    assert listed.status_code == 200
    listed_body = listed.json()
    assert listed_body["unread_count"] == 2
    assert {n["id"] for n in listed_body["notifications"]} == {first.id, second.id}

    assert cross_user_read.status_code == 404
    assert cross_user_read.json()["detail"] == (
        f"Notification '{bob_notification.id}' not found"
    )

    assert marked_one.status_code == 200
    assert marked_one.json() == {"status": "ok", "notification_id": first.id}

    first_after = next(
        n for n in after_one.json()["notifications"] if n["id"] == first.id
    )
    assert after_one.json()["unread_count"] == 1
    assert first_after["read"] is True

    assert marked_all.status_code == 200
    assert marked_all.json() == {"status": "ok", "marked_read": 1}
    assert bob_after.status_code == 200
    assert bob_after.json()["unread_count"] == 1
    assert [n["id"] for n in bob_after.json()["notifications"]] == [
        bob_notification.id
    ]
