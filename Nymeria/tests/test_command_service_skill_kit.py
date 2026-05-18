"""Tests for slash-command-driven skill kit activation helpers.

Covers activate_skill_kit and deactivate_skill_kit on
``nymeria.core.command_service``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nymeria.core.agent import set_current_agent
from nymeria.core.command_service import activate_skill_kit, deactivate_skill_kit
from nymeria.core.thread_config import ThreadConfigManager
from nymeria.skills import Skill


@pytest.fixture(autouse=True)
def _clear_current_agent():
    """Reset the agent global after each test to avoid cross-test pollution."""
    yield
    set_current_agent(None)


def _make_skill(name: str, *, required_tools: list[str] | None, tmp_path: Path) -> Skill:
    """Build an in-memory Skill object for testing without touching disk."""
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    metadata: dict = {}
    if required_tools is not None:
        metadata["nymeria"] = {"required_tools": list(required_tools), "tool_ttl": "30m"}
    return Skill(
        name=name,
        description=f"Test skill {name}",
        body="# {name}\n\nTest body.".format(name=name),
        path=skill_dir,
        scope="bundled",
        metadata=metadata,
    )


class _FakeSkillManager:
    def __init__(self, skill: Skill | None):
        self._skill = skill

    def get(self, name: str, user_id: str | None = None) -> Skill | None:
        if self._skill is None:
            return None
        if self._skill.name != name:
            return None
        return self._skill


class _FakeRegistry:
    def get_tool(self, name: str):
        return None


def _make_agent(
    tmp_path: Path,
    skill: Skill | None,
    *,
    role: str = "admin",
) -> SimpleNamespace:
    cfg_dir = tmp_path / "data"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    agent = SimpleNamespace(
        MAX_TOOL_RELOADS_PER_TURN=1,
        thread_config_manager=ThreadConfigManager(cfg_dir),
        tool_registry=_FakeRegistry(),
        skill_manager=_FakeSkillManager(skill),
        _pending_tool_reload={},
        _turn_reload_count={},
        accounts_repo=SimpleNamespace(
            get_user_by_id=lambda user_id: SimpleNamespace(role=role)
        ),
        profile_manager=SimpleNamespace(
            get_profile=lambda user_id: SimpleNamespace(
                tool_preferences=SimpleNamespace(default_thread_tools=None)
            )
        ),
        invalidated=[],
    )

    def _invalidate(thread_id: str) -> None:
        agent.invalidated.append(thread_id)

    agent.invalidate_thread_config_cache = _invalidate

    def _resolve_tt(tc):
        # No-op for tests; production path lazy-evicts expired TTLs.
        return None

    agent._resolve_temporary_tools = _resolve_tt
    set_current_agent(agent)
    return agent


class TestActivateSkillKit:
    def test_rejects_missing_thread(self, tmp_path):
        agent = _make_agent(tmp_path, None)
        ok, msg = activate_skill_kit(
            agent=agent, thread_id="", user_id="u1", skill_name="foo"
        )
        assert not ok
        assert "No active thread" in msg

    def test_rejects_unknown_skill(self, tmp_path):
        agent = _make_agent(tmp_path, None)
        ok, msg = activate_skill_kit(
            agent=agent, thread_id="t1", user_id="u1", skill_name="missing"
        )
        assert not ok
        assert "not found" in msg

    def test_activates_plain_skill_without_binding_tools(self, tmp_path):
        plain = _make_skill("plain", required_tools=None, tmp_path=tmp_path)
        agent = _make_agent(tmp_path, plain)
        ok, msg = activate_skill_kit(
            agent=agent, thread_id="t1", user_id="u1", skill_name="plain"
        )
        assert ok, msg
        tc = agent.thread_config_manager.get_config("t1")
        assert tc is not None
        assert "plain" in tc.enabled_skills
        assert tc.temporary_tools == {}
        assert "Skill 'plain' activated" in msg

    def test_activates_and_binds_tools(self, tmp_path):
        kit = _make_skill(
            "test-kit", required_tools=["bash_execute"], tmp_path=tmp_path
        )
        agent = _make_agent(tmp_path, kit)
        ok, msg = activate_skill_kit(
            agent=agent, thread_id="t1", user_id="u1", skill_name="test-kit"
        )
        assert ok, msg
        tc = agent.thread_config_manager.get_config("t1")
        assert tc is not None
        assert "test-kit" in tc.enabled_skills
        # bash_execute is a core tool so it ends up "already_default" rather
        # than written to temporary_tools — but the activation still succeeds.
        assert "Success" in msg


class TestDeactivateSkillKit:
    def test_noop_on_inactive_skill(self, tmp_path):
        kit = _make_skill(
            "test-kit", required_tools=["bash_execute"], tmp_path=tmp_path
        )
        agent = _make_agent(tmp_path, kit)
        ok, msg = deactivate_skill_kit(
            agent=agent, thread_id="t1", user_id="u1", skill_name="test-kit"
        )
        # With no thread config yet, returns an error since the config doesn't exist.
        assert not ok

    def test_removes_skill_from_enabled_skills(self, tmp_path):
        kit = _make_skill(
            "test-kit", required_tools=["bash_execute"], tmp_path=tmp_path
        )
        agent = _make_agent(tmp_path, kit)
        # Pre-seed: activate first.
        activate_skill_kit(
            agent=agent, thread_id="t1", user_id="u1", skill_name="test-kit"
        )
        tc_before = agent.thread_config_manager.get_config("t1")
        assert tc_before is not None
        assert "test-kit" in tc_before.enabled_skills

        ok, msg = deactivate_skill_kit(
            agent=agent, thread_id="t1", user_id="u1", skill_name="test-kit"
        )
        assert ok, msg
        tc_after = agent.thread_config_manager.get_config("t1")
        assert tc_after is not None
        assert "test-kit" not in tc_after.enabled_skills

    def test_reports_eviction_of_temporary_tools(self, tmp_path):
        # A skill kit whose required tool is NOT a default-bound core tool
        # will land in temporary_tools after activation, then deactivate
        # should evict it.
        kit = _make_skill(
            "ttl-kit",
            required_tools=["memory_clear_all"],
            tmp_path=tmp_path,
        )
        agent = _make_agent(tmp_path, kit)
        ok_act, _ = activate_skill_kit(
            agent=agent, thread_id="t1", user_id="u1", skill_name="ttl-kit"
        )
        assert ok_act
        tc_after_act = agent.thread_config_manager.get_config("t1")
        assert tc_after_act is not None
        assert "memory_clear_all" in tc_after_act.temporary_tools

        ok_deact, msg = deactivate_skill_kit(
            agent=agent, thread_id="t1", user_id="u1", skill_name="ttl-kit"
        )
        assert ok_deact, msg
        tc_after_deact = agent.thread_config_manager.get_config("t1")
        assert tc_after_deact is not None
        assert "memory_clear_all" not in tc_after_deact.temporary_tools
        assert "memory_clear_all" in msg
