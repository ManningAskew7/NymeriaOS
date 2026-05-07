"""Tests for thread configuration and callable-team routes."""

from __future__ import annotations

from pathlib import Path

from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager, ThreadLLMConfig
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.tools import ALL_TOOLS


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.invalidated: list[str] = []
        self.synced_tools = 0

    def invalidate_thread_config_cache(self, thread_id: str):
        self.invalidated.append(thread_id)

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
    agent.synced_tools = 0
    return client, agent, token


def test_thread_config_update_syncs_callable_metadata_and_partial_llm(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)
    thread_id = "thread-config"
    agent.accounts_repo.claim_thread(thread_id, "owner")

    response = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={
            "instructions": "Use the ops checklist.",
            "llm_config": {
                "provider": "openai",
                "model": "gpt-5.5",
                "temperature": 0.0,
                "max_tokens": 1,
            },
            "callable": True,
            "callable_name": "OpsHelper",
            "callable_description": "Help with ops.",
            "callable_max_iterations": 7,
            "enabled_skills": ["skill-a"],
            "disabled_skills": ["skill-b"],
            "inject_todos_in_prompt": True,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["thread_id"] == thread_id
    assert body["instructions"] == "Use the ops checklist."
    assert body["llm_config"]["provider"] == "openai"
    assert body["llm_config"]["model"] == "gpt-5.5"
    assert body["llm_config"]["temperature"] == 0.0
    assert body["llm_config"]["max_tokens"] == 1
    assert body["callable"] is True
    assert body["callable_name"] == "OpsHelper"
    assert body["callable_description"] == "Help with ops."
    assert body["callable_max_iterations"] == 7
    assert body["enabled_skills"] == ["skill-a"]
    assert body["disabled_skills"] == ["skill-b"]
    assert body["inject_todos_in_prompt"] is True
    assert body["has_customizations"] is True

    saved = agent.thread_config_manager.get_config(thread_id)
    assert saved is not None
    assert saved.llm_config is not None
    assert saved.llm_config.provider == "openai"
    assert saved.llm_config.model == "gpt-5.5"
    assert agent.thread_metadata_manager.get_thread("owner", thread_id).title == "OpsHelper"
    assert agent.invalidated == [thread_id]
    assert agent.synced_tools == 1

    clear_model = client.patch(
        f"/threads/{thread_id}/config",
        headers=headers,
        json={"llm_config": {"model": None}},
    )

    assert clear_model.status_code == 200
    saved = agent.thread_config_manager.get_config(thread_id)
    assert saved.llm_config is not None
    assert saved.llm_config.provider == "openai"
    assert saved.llm_config.model is None


def test_thread_config_rejects_invalid_core_and_duplicate_callable_names(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)
    agent.accounts_repo.claim_thread("existing", "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="existing", callable=True, callable_name="Helper")
    )
    agent.accounts_repo.claim_thread("target", "owner")

    missing_name = client.patch(
        "/threads/target/config",
        headers=headers,
        json={"callable": True},
    )
    invalid_name = client.patch(
        "/threads/target/config",
        headers=headers,
        json={"callable_name": "bad name"},
    )
    core_name = client.patch(
        "/threads/target/config",
        headers=headers,
        json={"callable_name": ALL_TOOLS[0].name},
    )
    duplicate = client.patch(
        "/threads/target/config",
        headers=headers,
        json={"callable_name": "Helper"},
    )

    assert missing_name.status_code == 400
    assert "callable_name is required" in missing_name.json()["detail"]
    assert invalid_name.status_code == 400
    assert "Invalid callable name" in invalid_name.json()["detail"]
    assert core_name.status_code == 400
    assert "conflicts with a built-in tool name" in core_name.json()["detail"]
    assert duplicate.status_code == 409
    assert "already used by thread existing" in duplicate.json()["detail"]


def test_thread_config_delete_removes_callable_config_and_syncs_tools(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _client(tmp_path, api_client_builder)
    thread_id = "callable-thread"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=thread_id,
            callable=True,
            callable_name="Helper",
            llm_config=ThreadLLMConfig(provider="openai"),
        )
    )

    response = client.delete(
        f"/threads/{thread_id}/config",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "thread_id": thread_id}
    assert agent.thread_config_manager.get_config(thread_id) is None
    assert agent.invalidated == [thread_id]
    assert agent.synced_tools == 1
