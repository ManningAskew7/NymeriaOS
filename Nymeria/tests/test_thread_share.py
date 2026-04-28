"""Tests for portable thread configuration sharing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from nymeria.core.accounts import AccountsRepo
from nymeria.core.thread_config import (
    TemporaryToolEntry,
    ThreadConfig,
    ThreadConfigManager,
    ThreadLLMConfig,
)
from nymeria.core.thread_metadata import ThreadMetadataManager
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


class FakeToolRegistry:
    def list_tools(self):
        return [{"name": "custom_weather", "description": "Weather"}]


class FakeSkill:
    def __init__(self, name: str):
        self.name = name


class FakeSkillManager:
    def list_installed(self, user_id: str):
        return [FakeSkill("email-style")]


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.tool_registry = FakeToolRegistry()
        self.skill_manager = FakeSkillManager()
        self._callable_tool_thread_map = {}
        self.invalidated: list[str] = []
        self.synced_tools = 0

    def invalidate_thread_config_cache(self, thread_id: str):
        self.invalidated.append(thread_id)

    def sync_agent_tools(self):
        self.synced_tools += 1


def _client(tmp_path: Path, monkeypatch) -> tuple[TestClient, FakeAgent, str]:
    settings = FakeSettings(tmp_path)
    agent = FakeAgent(tmp_path)
    monkeypatch.setattr(api_module, "get_settings", lambda: settings)
    app = api_module.create_api_app(agent)
    agent.accounts_repo.create_user("owner", "owner@example.com", "Owner")
    token = agent.accounts_repo.issue_token("owner")
    return TestClient(app), agent, token


def test_thread_export_omits_history_notepad_temporary_tools_and_api_key(tmp_path: Path, monkeypatch):
    client, agent, token = _client(tmp_path, monkeypatch)
    thread_id = "thread-export"
    agent.accounts_repo.claim_thread(thread_id, "owner")
    agent.thread_metadata_manager.upsert_thread("owner", thread_id, title="Export Me")
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id=thread_id,
            instructions="Do the thing",
            enabled_tools=["custom_weather"],
            temporary_tools={
                "hello_test": TemporaryToolEntry(
                    expires_at=datetime.utcnow()
                )
            },
            llm_config=ThreadLLMConfig(
                provider="openai",
                model="gpt-5.5",
                api_key="secret-key",
                base_url="http://example.test/v1",
            ),
        )
    )
    notes_dir = tmp_path / "thread_notes"
    notes_dir.mkdir()
    (notes_dir / f"{thread_id}.md").write_text("private notes", encoding="utf-8")

    response = client.get(
        f"/threads/{thread_id}/export",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "nymeria.thread.share"
    assert body["title"] == "Export Me"
    assert "notepad" in body["omitted"]
    assert "temporary_tools" in body["omitted"]
    assert "temporary_tools" not in body["config"]
    assert body["config"]["llm_config"]["model"] == "gpt-5.5"
    assert "api_key" not in body["config"]["llm_config"]


def test_thread_import_creates_empty_owned_thread_and_sanitizes_references(tmp_path: Path, monkeypatch):
    client, agent, token = _client(tmp_path, monkeypatch)
    existing_id = "existing-callable"
    agent.accounts_repo.claim_thread(existing_id, "owner")
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=existing_id, callable=True, callable_name="Helper")
    )

    document = {
        "kind": "nymeria.thread.share",
        "version": 1,
        "title": "Shared Helper",
        "config": {
            "instructions": "Imported instructions",
            "enabled_tools": [
                "custom_weather",
                "claude_code",
                "missing_tool",
                "Helper",
            ],
            "disabled_tools": ["file_read", "missing_disabled"],
            "enabled_skills": ["email-style", "missing-skill"],
            "llm_config": {
                "provider": "openai",
                "model": "gpt-5.5",
                "api_key": "do-not-import",
                "unknown": "ignored",
            },
            "callable": True,
            "callable_name": "Helper",
            "callable_description": "Imported helper",
        },
        "omitted": ["llm_config.api_key"],
    }

    response = client.post(
        "/threads/import",
        headers={"Authorization": f"Bearer {token}"},
        json=document,
    )

    assert response.status_code == 200
    body = response.json()
    imported_id = body["thread_id"]
    assert imported_id.startswith("imported-")
    assert agent.accounts_repo.get_thread_owner(imported_id) == "owner"
    assert agent.thread_metadata_manager.get_thread("owner", imported_id) is not None

    saved = agent.thread_config_manager.get_config(imported_id)
    assert saved is not None
    assert saved.instructions == "Imported instructions"
    assert saved.enabled_tools == ["custom_weather"]
    assert saved.disabled_tools == ["file_read"]
    assert saved.enabled_skills == ["email-style"]
    assert saved.llm_config is not None
    assert saved.llm_config.api_key is None
    assert saved.callable is True
    assert saved.callable_name != "Helper"
    assert saved.callable_name.startswith("Helper_")
    assert imported_id in agent.invalidated
    assert agent.synced_tools >= 1

    warnings = "\n".join(body["warnings"])
    assert "Dropped admin-only enabled tool: claude_code" in warnings
    assert "Dropped unavailable enabled tool: missing_tool" in warnings
    assert "Dropped unavailable enabled tool: Helper" in warnings
    assert "Dropped unavailable disabled tool: missing_disabled" in warnings
    assert "Dropped unavailable enabled_skill: missing-skill" in warnings
    assert "Ignored llm_config.api_key" in warnings


def test_thread_import_rejects_unknown_share_version(tmp_path: Path, monkeypatch):
    client, _agent, token = _client(tmp_path, monkeypatch)

    response = client.post(
        "/threads/import",
        headers={"Authorization": f"Bearer {token}"},
        json={"kind": "nymeria.thread.share", "version": 999, "config": {}},
    )

    assert response.status_code == 400
    assert "Unsupported import version" in response.json()["detail"]
