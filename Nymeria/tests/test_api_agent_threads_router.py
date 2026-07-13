from __future__ import annotations

from pathlib import Path
from typing import Any

from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.tools import SEED_TOOLS
from nymeria.triggers import api as api_module


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.synced_tools = 0

    def sync_agent_tools(self):
        self.synced_tools += 1


def _client(tmp_path: Path, api_client_builder) -> tuple[object, FakeAgent]:
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


def _save_callable(agent: FakeAgent, user_id: str, thread_id: str, name: str) -> None:
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=thread_id, callable=True, callable_name=name)
    )
    agent.accounts_repo.claim_thread(thread_id, user_id)


def test_agent_threads_list_is_scoped_to_effective_user(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    owner_token = _create_user(agent, "owner")
    _create_user(agent, "other")
    admin_token = _create_user(agent, "admin", role="admin")
    _save_callable(agent, "owner", "owner-helper", "OwnerHelper")
    _save_callable(agent, "other", "other-helper", "OtherHelper")
    _save_callable(agent, "admin", "admin-helper", "AdminHelper")

    owner_response = client.get(
        "/agents/threads",
        headers=api_client_builder.auth(owner_token),
    )
    admin_response = client.get(
        "/agents/threads",
        headers=api_client_builder.auth(admin_token),
    )
    act_as_response = client.get(
        "/agents/threads",
        headers={
            **api_client_builder.auth(admin_token),
            "X-Nymeria-Act-As": "owner",
        },
    )

    assert owner_response.status_code == 200
    assert admin_response.status_code == 200
    assert act_as_response.status_code == 200
    assert [t["callable_name"] for t in owner_response.json()["threads"]] == [
        "OwnerHelper"
    ]
    assert [t["callable_name"] for t in admin_response.json()["threads"]] == [
        "AdminHelper"
    ]
    assert [t["callable_name"] for t in act_as_response.json()["threads"]] == [
        "OwnerHelper"
    ]


def test_create_agent_thread_claims_metadata_syncs_tools_and_publishes_event(
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

    response = client.post(
        "/agents/threads",
        headers={
            **api_client_builder.auth(token),
            "X-Nymeria-Client-Id": "desktop-1",
        },
        json={
            "callable_name": "Helper",
            "callable_description": "",
            "system_prompt": "Do helper work.",
            "llm_provider": "openai",
            "llm_model": "test-model",
            "llm_temperature": 0.2,
            "llm_max_tokens": 123,
        },
    )

    assert response.status_code == 200
    body = response.json()
    thread_id = body["thread_id"]
    assert thread_id.startswith("agent-helper-")
    assert body["callable"] is True
    assert body["callable_name"] == "Helper"
    assert body["callable_description"] == "Invoke the Helper callable thread"
    assert body["system_prompt"] == "Do helper work."
    assert body["llm_config"] == {
        "provider": "openai",
        "model": "test-model",
        "temperature": 0.2,
        "max_tokens": 123,
        "extended_thinking": None,
        "reasoning_effort": None,
        "use_model_defaults": None,
        "openai_api_mode": None,
        "provider_route": None,
        "base_url": None,
        "context_length": None,
        "ollama_num_ctx": None,
        "api_key": None,
        "compact_threshold_mode": None,
        "compact_threshold": None,
        "compact_threshold_tokens": None,
        "compact_proactive_enabled": None,
        "compact_proactive_idle_seconds": None,
        "compact_proactive_min_pct": None,
        "fallback_switch_mode": None,
    }
    assert body["has_customizations"] is True

    saved = agent.thread_config_manager.get_config(thread_id)
    assert saved is not None
    assert saved.callable_name == "Helper"
    assert agent.accounts_repo.get_thread_owner(thread_id) == "owner"

    metadata = agent.thread_metadata_manager.get_thread("owner", thread_id)
    assert metadata is not None
    assert metadata.title == "Helper"
    assert metadata.title_source == "callable"
    assert metadata.platform == "callable"

    assert agent.synced_tools == 2
    assert events == [
        {
            "event_type": "thread_created",
            "thread_id": thread_id,
            "user_id": "owner",
            "data": {
                "title": "Helper",
                "title_source": "callable",
                "platform": "callable",
            },
            "origin_client_id": "desktop-1",
        }
    ]


def test_create_agent_thread_rejects_owner_duplicate_and_core_tool_conflict(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    owner_token = _create_user(agent, "owner")
    _create_user(agent, "other")
    _save_callable(agent, "other", "other-helper", "Helper")

    cross_user_allowed = client.post(
        "/agents/threads",
        headers=api_client_builder.auth(owner_token),
        json={"callable_name": "Helper"},
    )
    duplicate = client.post(
        "/agents/threads",
        headers=api_client_builder.auth(owner_token),
        json={"callable_name": "Helper"},
    )
    core_conflict = client.post(
        "/agents/threads",
        headers=api_client_builder.auth(owner_token),
        json={"callable_name": SEED_TOOLS[0].name},
    )

    assert cross_user_allowed.status_code == 200
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]
    assert core_conflict.status_code == 400
    assert "conflicts with a core tool name" in core_conflict.json()["detail"]
