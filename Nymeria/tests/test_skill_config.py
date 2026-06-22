"""Tests for agent-authored Skill and Skill Kit configuration."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from langgraph.types import Command

from nymeria.core.agent import set_current_agent
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.core.tool_reload import TOOL_RELOAD_QUEUED_KEY
from nymeria.core.user_profile import DEFAULT_GLOBAL_SKILLS, UserProfileManager
from nymeria.skills import SkillManager
from nymeria.tools import SEED_TOOLS, CATALOG_TOOLS
from nymeria.tools.metadata import SecurityLevel, ToolCategory, get_all_tool_metadata
from nymeria.tools.skill_config import (
    skill_edit,
    skill_write,
)
from nymeria.tools.search_skills import skill_manage


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
        self._default_graph = object()
        self._default_async_graph = object()
        self.rebuild_calls = 0
        self.invalidated: list[str] = []

    def invalidate_thread_config_cache(self, thread_id: str) -> None:
        self.invalidated.append(thread_id)

    def _rebuild_default_graphs(self) -> None:
        # Mirror the real agent contract: clear the per-thread caches under
        # the lock, then swap in fresh default graphs.
        self.rebuild_calls += 1
        with self._graph_cache_lock:
            self._user_graphs.clear()
            self._async_user_graphs.clear()
        self._default_graph = object()
        self._default_async_graph = object()


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
    # The migration seeds the full default set: self-improve plus the focused
    # capability kits it routes to.
    assert profile.enabled_global_skills == DEFAULT_GLOBAL_SKILLS
    assert profile.global_skill_defaults_migrated is True

    # Only self-improve exists on disk in this test's bundled dir, so the kits
    # are enabled-but-not-installed and filtered out of the active set.
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


def test_skill_write_rejects_admin_only_required_tool_without_write(tmp_path: Path):
    agent = _FakeAgent(tmp_path, role="user")
    markdown = """---
name: admin-workflow
description: Should not publish for non-admin users.
---

# Admin Workflow

Use reload_all.
"""

    set_current_agent(agent)
    try:
        result = skill_write.func(
            markdown=markdown,
            tools=["reload_all"],
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


def test_skill_write_rejects_shadowing_bundled_skill(tmp_path: Path):
    bundled = tmp_path / "bundled"
    _write_skill(bundled, "self-improve", SELF_IMPROVE_MD)
    agent = _FakeAgent(tmp_path)
    agent.skill_manager = SkillManager(
        bundled_dir=bundled,
        data_skills_dir=tmp_path / "skills",
    )
    markdown = """---
name: self-improve
description: Attempt to shadow the bundled default skill.
---

# Replacement

This should not publish.
"""

    set_current_agent(agent)
    try:
        result = skill_write.func(
            markdown=markdown,
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


def test_skill_write_creates_skill_kit_with_script_and_activates_thread(tmp_path: Path):
    agent = _FakeAgent(tmp_path)
    markdown = """---
name: script-workflow
description: Use hello_test and a bundled helper script.
---

# Script Workflow

Run hello_test before using the helper script.
"""

    set_current_agent(agent)
    try:
        result = skill_write.func(
            markdown=markdown,
            tools=["hello_test"],
            scripts=[
                {
                    "path": "scripts/helper.py",
                    "content": "def main():\n    return 'ok'\n",
                    "executable": True,
                }
            ],
            tool_ttl="30m",
            tool_call_id="call-write",
            config={"configurable": {"user_id": "alice", "thread_id": "thread-a"}},
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, Command)
    payload = _json_prefix(result.update["messages"][0].content)
    assert payload["ok"] is True
    assert payload["skill"]["name"] == "script-workflow"
    assert payload["skill"]["required_tools"] == ["hello_test"]
    script_path = tmp_path / "skills" / "users" / "alice" / "script-workflow" / "scripts" / "helper.py"
    assert script_path.exists()
    assert script_path.stat().st_mode & 0o111
    skill = agent.skill_manager.get("script-workflow", user_id="alice")
    assert skill is not None
    assert skill.required_tools == ["hello_test"]
    assert "script-workflow" in agent.thread_config_manager.get_config("thread-a").enabled_skills
    assert result.update["messages"][0].additional_kwargs[TOOL_RELOAD_QUEUED_KEY] is True
    assert agent._pending_tool_reload["thread-a"]["source"] == "skill_write"


def test_skill_edit_rewrites_skill_md_and_preserves_scripts(tmp_path: Path):
    agent = _FakeAgent(tmp_path)
    markdown = """---
name: edit-workflow
description: Original description.
---

# Edit Workflow

Original body.
"""

    set_current_agent(agent)
    try:
        write_result = skill_write.func(
            markdown=markdown,
            tools=["hello_test"],
            scripts=[
                {
                    "path": "scripts/helper.py",
                    "content": "def main():\n    return 'ok'\n",
                }
            ],
            activate_current_thread=False,
            tool_call_id="call-write",
            config={"configurable": {"user_id": "alice", "thread_id": "thread-a"}},
        )
        edit_result = skill_edit.func(
            name="edit-workflow",
            description="Updated description.",
            body="# Edit Workflow\n\nUpdated body.",
            set_tools="bash_execute",
            tool_ttl="7d",
            activate_current_thread=False,
            tool_call_id="call-edit",
            config={"configurable": {"user_id": "alice", "thread_id": "thread-a"}},
        )
    finally:
        set_current_agent(None)

    assert isinstance(write_result, str)
    assert json.loads(write_result)["ok"] is True
    assert isinstance(edit_result, str)
    payload = json.loads(edit_result)
    assert payload["ok"] is True
    skill = agent.skill_manager.get("edit-workflow", user_id="alice")
    assert skill is not None
    assert skill.description == "Updated description."
    assert skill.required_tools == ["bash_execute"]
    assert skill.tool_ttl == "7d"
    assert "Updated body." in skill.body
    script_path = tmp_path / "skills" / "users" / "alice" / "edit-workflow" / "scripts" / "helper.py"
    assert script_path.exists()


def test_skill_manage_prune_removes_missing_and_noop_thread_entries(tmp_path: Path):
    agent = _FakeAgent(tmp_path)
    _write_skill(
        tmp_path / "skills" / "users" / "alice",
        "kept-skill",
        """---
name: kept-skill
description: Installed skill should remain.
---

# Kept Skill
""",
    )
    agent.skill_manager.reload()
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="thread-a",
            enabled_skills=["kept-skill", "missing-skill"],
            disabled_skills=["old-noop"],
        )
    )

    set_current_agent(agent)
    try:
        result = skill_manage.func(
            action="prune",
            tool_call_id="call-prune",
            config={"configurable": {"user_id": "alice", "thread_id": "thread-a"}},
        )
    finally:
        set_current_agent(None)

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["changed"] is True
    assert payload["removed"]["enabled_missing"] == ["missing-skill"]
    assert payload["removed"]["disabled_missing"] == ["old-noop"]
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert tc.enabled_skills == ["kept-skill"]
    assert tc.disabled_skills == []


def test_skill_manage_prune_removes_recorded_stale_thread_skill(tmp_path: Path, monkeypatch):
    from datetime import timedelta

    from nymeria.core import capability_usage
    from nymeria.core.capability_usage import CapabilityUsageStore
    from nymeria.core.time_utils import utc_now

    agent = _FakeAgent(tmp_path)
    _write_skill(
        tmp_path / "skills" / "users" / "alice",
        "stale-skill",
        """---
name: stale-skill
description: Skill with old recorded usage.
---

# Stale Skill
""",
    )
    agent.skill_manager.reload()
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="thread-a", enabled_skills=["stale-skill"])
    )
    store = CapabilityUsageStore(tmp_path / "capability_usage.json")
    store.record(user_id="alice", thread_id="thread-a", skills=["stale-skill"])
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["alice"]["thread-a"]["skills"]["stale-skill"]["last_used_at"] = (
        utc_now() - timedelta(days=45)
    ).isoformat()
    store.path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(capability_usage, "get_capability_usage_store", lambda: store)

    set_current_agent(agent)
    try:
        result = skill_manage.func(
            action="prune",
            stale_after_days=30,
            min_enabled_age_days=0,
            tool_call_id="call-prune",
            config={"configurable": {"user_id": "alice", "thread_id": "thread-a"}},
        )
    finally:
        set_current_agent(None)

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["removed"]["enabled_stale_used_before_cutoff"] == ["stale-skill"]
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert tc.enabled_skills == []


def test_skill_write_and_edit_are_optional_with_metadata():
    core_names = {tool.name for tool in SEED_TOOLS}

    assert "skill_config" not in core_names
    assert "skill_kit_create" not in core_names
    assert "skill_config" not in CATALOG_TOOLS
    assert "skill_kit_create" not in CATALOG_TOOLS
    assert get_all_tool_metadata("skill_config") is None
    assert get_all_tool_metadata("skill_kit_create") is None
    for name in ("skill_write", "skill_edit"):
        assert name not in core_names
        assert name in CATALOG_TOOLS
        meta = get_all_tool_metadata(name)
        assert meta is not None
        assert meta.category == ToolCategory.CUSTOM
        assert meta.security_level == SecurityLevel.MODERATE
        assert meta.default_enabled is False
