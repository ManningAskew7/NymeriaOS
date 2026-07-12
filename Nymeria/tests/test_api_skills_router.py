"""Regression tests for the extracted Skills API router."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from threading import Lock

from nymeria.core.accounts import AccountsRepo


class FakeSkill:
    def __init__(self, root: Path):
        self.name = "kit-skill"
        self.description = "Use a required tool as part of a workflow."
        self.scope = "user"
        self.allowed_tools = ["nym_todo"]
        self.required_tools = ["web_search"]
        self.required_skills = ["plain-skill"]
        self.tool_ttl = "30m"
        self.is_skill_kit = True
        self.has_scripts = True
        self.has_references = True
        self.has_assets = False
        self.body = "Follow the Skill Kit workflow."
        self.path = root / "skills" / "users" / "owner" / "kit-skill"
        self.license = "MIT"

    def list_scripts(self) -> list[str]:
        return ["scripts/run.py"]

    def list_references(self) -> list[str]:
        return ["references/guide.md"]


class FakeSkillManager:
    def __init__(self, root: Path):
        self.skill = FakeSkill(root)
        self.last_thread_resolution: dict | None = None

    def list_installed(self, user_id: str):
        return [self.skill] if user_id == "owner" else []

    def get(self, name: str, user_id: str):
        if name == self.skill.name and user_id == "owner":
            return self.skill
        return None

    def list_for_thread(
        self,
        *,
        user_id: str,
        enabled_global_skills: list[str],
        thread_enabled_skills: list[str],
        thread_disabled_skills: list[str],
    ):
        self.last_thread_resolution = {
            "user_id": user_id,
            "enabled_global_skills": enabled_global_skills,
            "thread_enabled_skills": thread_enabled_skills,
            "thread_disabled_skills": thread_disabled_skills,
        }
        return [self.skill]


class FakeProfileManager:
    def __init__(self):
        self.profile = SimpleNamespace(enabled_global_skills=["global-skill"])
        self.saved_profile = None

    def get_profile(self, user_id: str):
        assert user_id == "owner"
        return self.profile

    def save_profile(self, profile):
        self.saved_profile = profile


class FakeThreadConfigManager:
    def get_config(self, thread_id: str):
        assert thread_id == "thread-1"
        return SimpleNamespace(
            enabled_skills=["thread-skill"],
            disabled_skills=["disabled-skill"],
        )


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.profile_manager = FakeProfileManager()
        self.thread_config_manager = FakeThreadConfigManager()
        self.skill_manager = FakeSkillManager(data_dir)
        self._graph_cache_lock = Lock()
        self._user_graphs = {"owner": object()}
        self._async_user_graphs = {"owner": object()}
        self._default_graph = object()
        self._default_async_graph = object()
        self.synced_tools = 0
        self.rebuild_calls = 0

    def sync_agent_tools(self):
        self.synced_tools += 1

    def _rebuild_default_graphs(self) -> None:
        # Mirror the real agent contract: clear the per-thread caches under
        # the lock, then swap in fresh default graphs.
        self.rebuild_calls += 1
        with self._graph_cache_lock:
            self._user_graphs.clear()
            self._async_user_graphs.clear()
        self._default_graph = object()
        self._default_async_graph = object()


def _client(tmp_path: Path, api_client_builder) -> tuple[object, FakeAgent, str]:
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent,
        settings,
        user_id="owner",
    )
    return client, agent, token


def test_skills_router_preserves_skill_kit_metadata(tmp_path: Path, api_client_builder):
    client, _agent, token = _client(tmp_path, api_client_builder)
    headers = api_client_builder.auth(token)

    listed = client.get("/skills", headers=headers)
    detail = client.get("/skills/kit-skill", headers=headers)

    assert listed.status_code == 200
    assert listed.json()["skills"] == [
        {
            "name": "kit-skill",
            "description": "Use a required tool as part of a workflow.",
            "scope": "user",
            "allowed_tools": ["nym_todo"],
            "required_tools": ["web_search"],
            "required_skills": ["plain-skill"],
            "tool_ttl": "30m",
            "is_skill_kit": True,
            "default_active": False,
            "has_scripts": True,
            "has_references": True,
            "has_assets": False,
        }
    ]
    assert detail.status_code == 200
    assert detail.json()["body"] == "Follow the Skill Kit workflow."
    assert detail.json()["scripts"] == ["scripts/run.py"]
    assert detail.json()["references"] == ["references/guide.md"]


def test_thread_skills_route_uses_existing_resolution_inputs(
    tmp_path: Path,
    api_client_builder,
):
    client, agent, token = _client(tmp_path, api_client_builder)

    response = client.get(
        "/threads/thread-1/skills",
        headers=api_client_builder.auth(token),
    )

    assert response.status_code == 200
    assert response.json()["enabled_global"] == ["global-skill"]
    assert response.json()["thread_enabled"] == ["thread-skill"]
    assert response.json()["thread_disabled"] == ["disabled-skill"]
    assert agent.skill_manager.last_thread_resolution == {
        "user_id": "owner",
        "enabled_global_skills": ["global-skill"],
        "thread_enabled_skills": ["thread-skill"],
        "thread_disabled_skills": ["disabled-skill"],
    }


def test_global_skills_update_clears_graph_caches(tmp_path: Path, api_client_builder):
    client, agent, token = _client(tmp_path, api_client_builder)

    response = client.put(
        "/settings/global-skills",
        headers=api_client_builder.auth(token),
        json={"skill_names": ["kit-skill"]},
    )

    assert response.status_code == 200
    assert response.json() == {"enabled_global_skills": ["kit-skill"]}
    assert agent.profile_manager.saved_profile is agent.profile_manager.profile
    assert agent.profile_manager.profile.enabled_global_skills == ["kit-skill"]
    # The router delegates to the canonical agent rebuild rather than reaching
    # into private cache internals; the rebuild both clears the per-thread
    # caches and recompiles the defaults.
    assert agent.rebuild_calls == 1
    assert agent._user_graphs == {}
    assert agent._async_user_graphs == {}
