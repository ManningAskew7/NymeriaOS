from __future__ import annotations

import sqlite3
from pathlib import Path

from nymeria.core.accounts import AccountsRepo
from nymeria.core.todo_manager import TodoList, TodoManager


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


def _scheduled_row_count(data_dir: Path, todo_id: str) -> int:
    with sqlite3.connect(data_dir / "todo_schedule.db") as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM scheduled_todos WHERE todo_id = ?",
            (todo_id,),
        ).fetchone()
    return row[0]


def test_todo_crud_routes_filter_reschedule_and_sync_schedule_db(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    headers = api_client_builder.auth(token)

    created = client.post(
        "/todos",
        headers=headers,
        json={
            "task": "Check inbox",
            "notes": "Use dashboard route",
            "scheduled_for": "30m",
            "recurrence": "daily",
            "thread_id": "thread-1",
        },
    )
    assert created.status_code == 200
    body = created.json()
    todo_id = body["id"]
    assert body["task"] == "Check inbox"
    assert body["status"] == "pending"
    assert body["created_by"] == "user"
    assert body["thread_id"] == "thread-1"
    assert body["scheduled_for"] is not None
    assert _scheduled_row_count(tmp_path, todo_id) == 1

    counts = client.get("/todos/thread-counts", headers=headers)
    filtered = client.get(
        "/todos",
        headers=headers,
        params={"thread_id": "thread-1"},
    )
    invalid_filter = client.get(
        "/todos",
        headers=headers,
        params={"filter_status": "bogus"},
    )

    assert counts.status_code == 200
    assert counts.json() == {"thread-1": 1}
    assert filtered.status_code == 200
    assert filtered.json()["user_id"] == "owner"
    assert [item["id"] for item in filtered.json()["items"]] == [todo_id]
    assert invalid_filter.status_code == 400
    assert invalid_filter.json()["detail"] == "Invalid status filter 'bogus'"

    completed = client.post(f"/todos/{todo_id}/complete", headers=headers)
    assert completed.status_code == 200
    completed_body = completed.json()
    assert completed_body["id"] == todo_id
    assert completed_body["status"] == "pending"
    assert completed_body["recurrence"] == "daily"
    assert completed_body["scheduled_for"] is not None
    assert completed_body["last_execution"] is not None
    assert _scheduled_row_count(tmp_path, todo_id) == 1

    deleted = client.delete(f"/todos/{todo_id}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "ok", "deleted_id": todo_id}
    assert _scheduled_row_count(tmp_path, todo_id) == 0


def test_todo_routes_are_effective_user_scoped_and_users_endpoint_remains(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    owner_token = _create_user(agent, "owner")
    other_token = _create_user(agent, "other")
    owner_headers = api_client_builder.auth(owner_token)
    other_headers = api_client_builder.auth(other_token)

    owner_todo = client.post(
        "/todos",
        headers=owner_headers,
        json={"task": "Owner task", "thread_id": "owner-thread"},
    )
    other_todo = client.post(
        "/todos",
        headers=other_headers,
        json={"task": "Other task", "thread_id": "other-thread"},
    )
    assert owner_todo.status_code == 200
    assert other_todo.status_code == 200
    manager = TodoManager(tmp_path)
    manager.save_todos(TodoList(user_id="empty-legacy"))

    owner_list_with_ignored_query = client.get(
        "/todos",
        headers=owner_headers,
        params={"user_id": "other", "filter_status": "all"},
    )
    users_with_todos = client.get("/todos/users", headers=owner_headers)

    assert owner_list_with_ignored_query.status_code == 200
    listed = owner_list_with_ignored_query.json()
    assert listed["user_id"] == "owner"
    assert [item["task"] for item in listed["items"]] == ["Owner task"]
    assert users_with_todos.status_code == 200
    assert set(users_with_todos.json()) == {"owner", "other"}

    assert manager.get_todos("other").get_item(other_todo.json()["id"]) is not None
