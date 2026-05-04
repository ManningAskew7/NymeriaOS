from __future__ import annotations

from pathlib import Path
from typing import Any

from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.tools import ALL_TOOLS
from nymeria.triggers import api as api_module


class FakeThreadLocks:
    def __init__(self):
        self.lock_info = None

    def get_lock_info(self, thread_id: str):
        return self.lock_info


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self._thread_locks = FakeThreadLocks()
        self.history_calls: list[dict[str, Any]] = []
        self.invalidated: list[str] = []
        self.synced_tools = 0

    def get_conversation_history(
        self,
        thread_id: str,
        *,
        include_internal: bool,
        show_autonomous_prompts: bool,
        show_prompt_metadata: bool,
    ):
        self.history_calls.append(
            {
                "thread_id": thread_id,
                "include_internal": include_internal,
                "show_autonomous_prompts": show_autonomous_prompts,
                "show_prompt_metadata": show_prompt_metadata,
            }
        )
        return [{"role": "user", "content": "hello"}]

    def get_context_stats(self, thread_id: str):
        return {"thread_id": thread_id, "tokens": 42}

    def invalidate_thread_config_cache(self, thread_id: str):
        self.invalidated.append(thread_id)

    def sync_agent_tools(self):
        self.synced_tools += 1


def _client(tmp_path: Path, api_client_builder) -> tuple[object, FakeAgent]:
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client = api_client_builder.client(agent, settings)
    agent.synced_tools = 0
    return client, agent


def _create_user(agent: FakeAgent, user_id: str, *, role: str = "user") -> str:
    agent.accounts_repo.create_user(
        user_id,
        f"{user_id}@example.com",
        user_id.title(),
        role=role,
    )
    return agent.accounts_repo.issue_token(user_id)


def test_thread_history_visibility_flags_and_context_processing_state(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    thread_id = "thread-history"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=thread_id,
            show_autonomous_prompts=True,
            show_prompt_metadata=True,
        )
    )
    agent._thread_locks.lock_info = {"owner": "test"}

    history = client.get(
        f"/threads/{thread_id}/history",
        headers=api_client_builder.auth(token),
    )
    internal_history = client.get(
        f"/threads/{thread_id}/history",
        headers=api_client_builder.auth(token),
        params={"include_internal": "true"},
    )
    context = client.get(
        f"/threads/{thread_id}/context",
        headers=api_client_builder.auth(token),
    )

    assert history.status_code == 200
    assert history.json() == {
        "thread_id": thread_id,
        "messages": [{"role": "user", "content": "hello"}],
    }
    assert internal_history.status_code == 200
    assert agent.history_calls == [
        {
            "thread_id": thread_id,
            "include_internal": False,
            "show_autonomous_prompts": True,
            "show_prompt_metadata": True,
        },
        {
            "thread_id": thread_id,
            "include_internal": True,
            "show_autonomous_prompts": False,
            "show_prompt_metadata": False,
        },
    ]
    assert context.status_code == 200
    assert context.json() == {
        "thread_id": thread_id,
        "tokens": 42,
        "processing": True,
    }


def test_thread_metadata_rename_callable_validates_conflicts_and_publishes_event(
    tmp_path: Path,
    monkeypatch,
    api_client_builder,
):
    events: list[dict[str, Any]] = []

    def capture_sync_event(**kwargs):
        events.append(kwargs)

    monkeypatch.setattr(api_module, "publish_sync_event", capture_sync_event)
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    target = "callable-target"
    existing = "callable-existing"
    agent.accounts_repo.claim_thread(target, "owner")
    agent.accounts_repo.claim_thread(existing, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=target, callable=True, callable_name="Helper")
    )
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=existing, callable=True, callable_name="ExistingHelper")
    )

    invalid = client.patch(
        f"/threads/{target}/metadata",
        headers=api_client_builder.auth(token),
        json={"title": "bad name"},
    )
    duplicate = client.patch(
        f"/threads/{target}/metadata",
        headers=api_client_builder.auth(token),
        json={"title": "ExistingHelper"},
    )
    core_conflict = client.patch(
        f"/threads/{target}/metadata",
        headers=api_client_builder.auth(token),
        json={"title": ALL_TOOLS[0].name},
    )
    renamed = client.patch(
        f"/threads/{target}/metadata",
        headers={
            **api_client_builder.auth(token),
            "X-Nymeria-Client-Id": "desktop-1",
        },
        json={"title": "RenamedHelper", "pinned": True},
    )

    assert invalid.status_code == 400
    assert "Invalid callable name" in invalid.json()["detail"]
    assert duplicate.status_code == 409
    assert "already used by thread callable-existing" in duplicate.json()["detail"]
    assert core_conflict.status_code == 400
    assert "conflicts with a core tool name" in core_conflict.json()["detail"]
    assert renamed.status_code == 200
    body = renamed.json()
    assert body["title"] == "RenamedHelper"
    assert body["title_source"] == "callable"
    assert body["pinned"] is True

    saved = agent.thread_config_manager.get_config(target)
    assert saved is not None
    assert saved.callable_name == "RenamedHelper"
    assert agent.invalidated == [target]
    assert agent.synced_tools == 1
    assert events == [
        {
            "event_type": "thread_updated",
            "thread_id": target,
            "user_id": "owner",
            "data": {
                "title": "RenamedHelper",
                "title_source": "callable",
                "pinned": True,
            },
            "origin_client_id": "desktop-1",
        }
    ]


def test_thread_claim_rejects_shared_and_hides_other_user_owner(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    owner_token = _create_user(agent, "owner")
    _create_user(agent, "other")
    admin_token = _create_user(agent, "admin", role="admin")
    agent.accounts_repo.claim_thread("other-thread", "other")

    fresh = client.post(
        "/threads/fresh-thread/claim",
        headers=api_client_builder.auth(owner_token),
    )
    shared = client.post(
        "/threads/telegram_-123/claim",
        headers=api_client_builder.auth(owner_token),
    )
    hidden = client.post(
        "/threads/other-thread/claim",
        headers=api_client_builder.auth(owner_token),
    )
    admin = client.post(
        "/threads/other-thread/claim",
        headers=api_client_builder.auth(admin_token),
    )

    assert fresh.status_code == 200
    assert fresh.json() == {"thread_id": "fresh-thread", "owner": "owner"}
    assert shared.status_code == 400
    assert shared.json()["detail"] == "Shared-channel threads cannot be claimed"
    assert hidden.status_code == 404
    assert hidden.json()["detail"] == "Not found"
    assert admin.status_code == 200
    assert admin.json() == {"thread_id": "other-thread", "owner": "other"}
