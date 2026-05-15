"""Tests for agent-authored Skill and Skill Kit configuration."""

from __future__ import annotations

import json
import asyncio
import importlib
import threading
from pathlib import Path
from types import SimpleNamespace

from langgraph.types import Command

from nymeria.core.agent import set_current_agent
from nymeria.core.thread_config import ThreadConfigManager
from nymeria.core.tool_reload import TOOL_RELOAD_QUEUED_KEY
from nymeria.core.user_profile import UserProfileManager
from nymeria.skills import SkillManager
from nymeria.tools import ALL_TOOLS, OPTIONAL_TOOLS
from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_all_tool_metadata
from nymeria.tools.skill_config import (
    SkillDraftStore,
    create_skill_draft,
    skill_config,
    skill_kit_create,
)


SELF_IMPROVE_MD = """---
name: self-improve
description: Build durable capabilities.
metadata:
  nymeria:
    required_tools:
      - hello_test
---

# Self Improve
"""


class _FakeRegistry:
    def get_tool(self, name: str):
        return None


class _FakeAgent:
    MAX_TOOL_RELOADS_PER_TURN = 1

    def __init__(self, root: Path, role: str = "admin"):
        bundled = root / "bundled"
        bundled.mkdir(parents=True, exist_ok=True)
        self.skill_manager = SkillManager(
            bundled_dir=bundled,
            data_skills_dir=root / "skills",
        )
        self.thread_config_manager = ThreadConfigManager(root / "threads")
        self.tool_registry = _FakeRegistry()
        self.accounts_repo = SimpleNamespace(
            get_user_by_id=lambda user_id: SimpleNamespace(role=role)
        )
        self._pending_tool_reload = {}
        self._turn_reload_count = {}
        self._graph_cache_lock = threading.RLock()
        self._user_graphs = {}
        self._async_user_graphs = {}
        self.invalidated: list[str] = []

    def invalidate_thread_config_cache(self, thread_id: str) -> None:
        self.invalidated.append(thread_id)


def _write_skill(root: Path, name: str, content: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")


def _json_prefix(text: str) -> dict:
    return json.loads(text.split("\n\n[", 1)[0])


def test_self_improve_global_default_is_profile_setting_and_thread_disable_wins(tmp_path: Path):
    bundled = tmp_path / "bundled"
    _write_skill(bundled, "self-improve", SELF_IMPROVE_MD)
    manager = SkillManager(bundled_dir=bundled, data_skills_dir=tmp_path / "skills")

    active = manager.list_for_thread(
        user_id="alice",
        enabled_global_skills=[],
        thread_enabled_skills=[],
        thread_disabled_skills=[],
    )
    assert active == []

    profiles = UserProfileManager(tmp_path / "profiles")
    profile = profiles.get_profile("alice")
    assert profile.enabled_global_skills == ["self-improve"]
    assert profile.global_skill_defaults_migrated is True

    active = manager.list_for_thread(
        user_id="alice",
        enabled_global_skills=profile.enabled_global_skills,
        thread_enabled_skills=[],
        thread_disabled_skills=[],
    )
    assert [skill.name for skill in active] == ["self-improve"]

    active = manager.list_for_thread(
        user_id="alice",
        enabled_global_skills=profile.enabled_global_skills,
        thread_enabled_skills=[],
        thread_disabled_skills=["self-improve"],
    )
    assert active == []

    profile.enabled_global_skills = []
    profiles.save_profile(profile)
    profile = profiles.get_profile("alice")
    assert profile.enabled_global_skills == []
    assert profile.global_skill_defaults_migrated is True


def test_skill_config_publish_user_skill_activates_thread_and_queues_reload(tmp_path: Path, monkeypatch):
    skill_config_module = importlib.import_module("nymeria.tools.skill_config")

    monkeypatch.setattr(
        skill_config_module,
        "_draft_store",
        lambda: SkillDraftStore(tmp_path / "drafts"),
    )
    agent = _FakeAgent(tmp_path)
    set_current_agent(agent)
    try:
        result = skill_config.func(
            "publish",
            name="hello-workflow",
            description="Use hello_test for a durable greeting workflow.",
            body="# Hello Workflow\n\nUse hello_test, then summarize the result.",
            required_tools=["hello_test"],
            tool_call_id="call-1",
            config={"configurable": {"user_id": "alice", "thread_id": "thread-a"}},
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, Command)
    message = result.update["messages"][0]
    content = message.content
    payload = _json_prefix(content)
    assert payload["ok"] is True
    assert payload["skill"]["name"] == "hello-workflow"
    assert payload["skill"]["is_skill_kit"] is True
    assert payload["activated_current_thread"] is True
    assert agent.skill_manager.get("hello-workflow", user_id="alice") is not None
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert "hello-workflow" in tc.enabled_skills
    assert message.additional_kwargs[TOOL_RELOAD_QUEUED_KEY] is True
    assert agent._pending_tool_reload["thread-a"]["source"] == "skill_config"
    assert agent._pending_tool_reload["thread-a"]["skill_name"] == "hello-workflow"


def test_skill_kit_create_package_uses_facade_reload_source(tmp_path: Path, monkeypatch):
    skill_config_module = importlib.import_module("nymeria.tools.skill_config")

    monkeypatch.setattr(
        skill_config_module,
        "_draft_store",
        lambda: SkillDraftStore(tmp_path / "drafts"),
    )
    agent = _FakeAgent(tmp_path)
    set_current_agent(agent)
    try:
        result = asyncio.run(
            skill_kit_create.coroutine(
                "package",
                name="facade-workflow",
                description="Package an existing workflow through the facade.",
                body="# Facade Workflow\n\nUse bash_execute only if needed.",
                required_tools=["bash_execute"],
                tool_call_id="call-1",
                config={"configurable": {"user_id": "alice", "thread_id": "thread-a"}},
            )
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, Command)
    message = result.update["messages"][0]
    payload = _json_prefix(message.content)
    assert payload["ok"] is True
    assert payload["skill"]["name"] == "facade-workflow"
    assert agent._pending_tool_reload["thread-a"]["source"] == "skill_kit_create"
    assert agent._pending_tool_reload["thread-a"]["reason"] == "skill_kit_created"


def test_skill_config_rejects_admin_only_required_tool_without_write(tmp_path: Path, monkeypatch):
    skill_config_module = importlib.import_module("nymeria.tools.skill_config")

    monkeypatch.setattr(
        skill_config_module,
        "_draft_store",
        lambda: SkillDraftStore(tmp_path / "drafts"),
    )
    agent = _FakeAgent(tmp_path, role="user")
    set_current_agent(agent)
    try:
        result = skill_config.func(
            "publish",
            name="admin-workflow",
            description="Should not publish for non-admin users.",
            body="# Admin Workflow\n\nUse reload_all.",
            required_tools=["reload_all"],
            tool_call_id="call-1",
            config={"configurable": {"user_id": "alice", "thread_id": "thread-a"}},
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "Admin-only tools" in payload["error"]["message"]
    assert agent.skill_manager.get("admin-workflow", user_id="alice") is None
    assert agent.thread_config_manager.get_config("thread-a") is None


def test_skill_config_rejects_shadowing_bundled_skill(tmp_path: Path, monkeypatch):
    skill_config_module = importlib.import_module("nymeria.tools.skill_config")

    monkeypatch.setattr(
        skill_config_module,
        "_draft_store",
        lambda: SkillDraftStore(tmp_path / "drafts"),
    )
    bundled = tmp_path / "bundled"
    _write_skill(bundled, "self-improve", SELF_IMPROVE_MD)
    agent = _FakeAgent(tmp_path)
    agent.skill_manager = SkillManager(
        bundled_dir=bundled,
        data_skills_dir=tmp_path / "skills",
    )
    set_current_agent(agent)
    try:
        result = skill_config.func(
            "publish",
            name="self-improve",
            description="Attempt to shadow the bundled default skill.",
            body="# Replacement\n\nThis should not publish.",
            tool_call_id="call-1",
            config={"configurable": {"user_id": "alice", "thread_id": "thread-a"}},
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "would shadow an existing bundled skill" in payload["error"]["message"]
    assert not (tmp_path / "skills" / "users" / "alice" / "self-improve").exists()


def test_skill_config_is_optional_with_metadata():
    core_names = {tool.name for tool in ALL_TOOLS}

    assert "skill_config" not in core_names
    assert "skill_config" in OPTIONAL_TOOLS
    meta = get_all_tool_metadata("skill_config")
    assert meta is not None
    assert meta.category == ToolCategory.CUSTOM
    assert meta.security_level == SecurityLevel.MODERATE
    assert meta.default_enabled is False


def test_create_skill_draft_normalizes_and_validates_dependencies(tmp_path: Path):
    agent = _FakeAgent(tmp_path)
    set_current_agent(agent)
    try:
        draft = create_skill_draft(
            user_id="alice",
            name="hello-workflow",
            description="Use hello_test for a durable greeting workflow.",
            body="# Hello Workflow\n\nUse hello_test.",
            allowed_tools="Read, Write",
            required_tools=["hello_test", "hello_test"],
            tool_ttl="4w",
        )
    finally:
        set_current_agent(None)

    assert draft.name == "hello-workflow"
    assert draft.allowed_tools == ["Read", "Write"]
    assert draft.required_tools == ["hello_test"]
    assert draft.tool_ttl == "4w"
