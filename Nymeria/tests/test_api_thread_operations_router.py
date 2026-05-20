"""Tests for thread portability, attachment validation, and control routes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from nymeria.core.accounts import AccountsRepo
from nymeria.core.chat_bindings import ChatBindingsRepo
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager, ThreadLLMConfig
from nymeria.core.thread_metadata import ThreadMetadataManager


class FakeThreadLocks:
    def __init__(self):
        self.lock_info = None

    def get_lock_info(self, thread_id: str):
        return self.lock_info


class FakeToolRegistry:
    def list_tools(self):
        return [{"name": "custom_weather", "description": "Weather"}]


class FakeAgent:
    def __init__(self, data_dir: Path):
        accounts_db = data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(accounts_db)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.tool_registry = FakeToolRegistry()
        self.skill_manager = None
        self.settings = SimpleNamespace(llm_provider="openai", llm_model="gpt-5.5")
        self._callable_tool_thread_map = {}
        self._thread_locks = FakeThreadLocks()
        self.aborted_threads: list[str] = []
        self.compactions: list[tuple[str, str]] = []
        self.prunes: list[tuple[str, str]] = []
        self.synced_tools = 0

    def _get_llm_config_for_thread(self, thread_id: str) -> ThreadLLMConfig:
        config = self.thread_config_manager.get_config(thread_id)
        if config and config.llm_config:
            return config.llm_config
        return ThreadLLMConfig()

    async def compact_now(self, thread_id: str, user_id: str):
        self.compactions.append((thread_id, user_id))
        return {"status": "compacted", "thread_id": thread_id, "user_id": user_id}

    async def prune_now(self, thread_id: str, user_id: str):
        self.prunes.append((thread_id, user_id))
        return {"success": True, "pruned_count": 3, "chars_saved": 1234}

    def abort_with_cascade(self, thread_id: str):
        self.aborted_threads.append(thread_id)

    def invalidate_thread_config_cache(self, thread_id: str):
        pass

    def sync_agent_tools(self):
        self.synced_tools += 1


def _client(tmp_path: Path, api_client_builder) -> tuple[object, FakeAgent, str]:
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent,
        settings,
        user_id="owner",
        email="owner@example.com",
        display_name="Owner",
    )
    return client, agent, token


def test_attachment_validation_uses_effective_thread_model(tmp_path: Path, api_client_builder):
    client, agent, token = _client(tmp_path, api_client_builder)
    thread_id = "thread-attachments"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=thread_id,
            llm_config=ThreadLLMConfig(provider="openai", model="openai/gpt-5.5"),
        )
    )

    response = client.post(
        f"/threads/{thread_id}/attachments/validate",
        headers=api_client_builder.auth(token),
        json={
            "attachments": [
                {
                    "file_type": "image",
                    "data_url": "data:image/png;base64,abc",
                    "mime_type": "image/png",
                    "file_name": "image.png",
                }
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["compatible"] is True
    assert body["effective_provider"] == "openai"
    assert body["effective_model"] == "openai/gpt-5.5"
    assert body["required_modalities"] == ["image"]
    assert body["unsupported_modalities"] == []
    assert body["can_force_send"] is True


def test_attachment_validation_does_not_leak_other_users_thread_model(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, _token = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("other", "other@example.com", "Other")
    other_token = agent.accounts_repo.issue_token("other")
    thread_id = "thread-owned-by-owner"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=thread_id,
            llm_config=ThreadLLMConfig(provider="openai", model="openai/gpt-5.5"),
        )
    )

    response = client.post(
        f"/threads/{thread_id}/attachments/validate",
        headers=api_client_builder.auth(other_token),
        json={"attachments": []},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Not found"


def test_compact_route_runs_under_authenticated_user(tmp_path: Path, api_client_builder):
    client, agent, token = _client(tmp_path, api_client_builder)
    thread_id = "thread-compact"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    response = client.post(
        f"/threads/{thread_id}/compact",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "compacted",
        "thread_id": thread_id,
        "user_id": "owner",
    }
    assert agent.compactions == [(thread_id, "owner")]


def test_prune_route_runs_under_authenticated_user(tmp_path: Path, api_client_builder):
    client, agent, token = _client(tmp_path, api_client_builder)
    thread_id = "thread-prune"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    response = client.post(
        f"/threads/{thread_id}/prune",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "pruned_count": 3,
        "chars_saved": 1234,
    }
    assert agent.prunes == [(thread_id, "owner")]


def test_stop_route_aborts_only_when_thread_is_running(tmp_path: Path, api_client_builder):
    client, agent, token = _client(tmp_path, api_client_builder)
    thread_id = "thread-stop"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent._thread_locks.lock_info = {"holder": "chat", "held_seconds": 2.4}

    response = client.post(
        f"/threads/{thread_id}/stop",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "stopping"
    assert body["thread_id"] == thread_id
    assert "Thread was held by 'chat'" in body["message"]
    assert agent.aborted_threads == [thread_id]


def test_attachment_validation_returns_per_model_limits(tmp_path: Path, api_client_builder):
    client, agent, token = _client(tmp_path, api_client_builder)
    thread_id = "thread-claude"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=thread_id,
            llm_config=ThreadLLMConfig(provider="anthropic", model="claude-opus-4-7"),
        )
    )

    response = client.post(
        f"/threads/{thread_id}/attachments/validate",
        headers=api_client_builder.auth(token),
        json={"attachments": []},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["effective_provider"] == "anthropic"
    assert body["effective_model"] == "claude-opus-4-7"
    assert body["limits"]["max_images_per_request"] == 100
    assert body["limits"]["max_image_bytes"] == 5 * 1024 * 1024
    assert body["limits"]["max_pdf_pages"] == 100


def test_attachment_limits_endpoint_returns_per_model_caps(tmp_path: Path, api_client_builder):
    client, agent, token = _client(tmp_path, api_client_builder)
    thread_id = "thread-gpt"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=thread_id,
            llm_config=ThreadLLMConfig(provider="openai", model="openai/gpt-5.5"),
        )
    )

    response = client.get(
        f"/threads/{thread_id}/attachment_limits",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["effective_provider"] == "openai"
    assert body["effective_model"] == "openai/gpt-5.5"
    assert body["limits"]["max_images_per_request"] == 1500
    assert body["limits"]["max_total_bytes"] == 512 * 1024 * 1024


def test_attachment_limits_endpoint_falls_back_to_settings_model(
    tmp_path: Path, api_client_builder
):
    client, agent, token = _client(tmp_path, api_client_builder)
    thread_id = "thread-default"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    # No ThreadConfig saved; the route should fall back to agent.settings.

    response = client.get(
        f"/threads/{thread_id}/attachment_limits",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["effective_provider"] == "openai"
    assert body["effective_model"] == "gpt-5.5"
    assert body["limits"]["max_images_per_request"] == 1500


def test_attachment_limits_endpoint_denies_non_owner(tmp_path: Path, api_client_builder):
    client, agent, _token = _client(tmp_path, api_client_builder)
    agent.accounts_repo.create_user("other", "other@example.com", "Other")
    other_token = agent.accounts_repo.issue_token("other")
    thread_id = "thread-owned-by-owner"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    response = client.get(
        f"/threads/{thread_id}/attachment_limits",
        headers=api_client_builder.auth(other_token),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Not found"
