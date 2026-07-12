"""Tests for the kit-composition family (backlog #41 + #26).

#41: nested required skills/kits + defer-aware activation on Skill().
#26: kit-declared thread templates (callable threads that spawn on first call).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from langgraph.types import Command

from nymeria.core.agent import set_current_agent
from nymeria.core.agent_graph import skills_fingerprint
from nymeria.core.command_service import activate_skill_kit, deactivate_skill_kit
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.skills import (
    SkillManager,
    expanded_required_tools,
    load_skill_directory,
    resolve_nested_skills,
)
from nymeria.skills.meta_tool import create_skill_meta_tool


OUTER_KIT_MD = """---
name: outer-kit
description: Outer kit that nests a kit and a plain skill.
metadata:
  nymeria:
    required_tools:
      - hello_test
    tool_ttl: 30m
    required_skills:
      - inner-kit
      - plain-skill
---

# Outer Kit

Use the outer workflow.
"""

INNER_KIT_MD = """---
name: inner-kit
description: Inner kit binding one tool and nesting deeper.
metadata:
  nymeria:
    required_tools:
      - memory_clear_all
    tool_ttl: 7d
    required_skills:
      - deep-skill
---

# Inner Kit

Use memory_clear_all wisely.
"""

PLAIN_MD = """---
name: plain-skill
description: Plain skill without dependencies.
---

# Plain Skill

No tools required.
"""

SKILLS_ONLY_KIT_MD = """---
name: skills-only-kit
description: Kit with nested skills but no tools of its own.
metadata:
  nymeria:
    required_skills:
      - plain-skill
---

# Skills Only Kit

Compose.
"""

MISSING_NESTED_KIT_MD = """---
name: broken-kit
description: Kit that requires a skill that is not installed.
metadata:
  nymeria:
    required_tools:
      - hello_test
    required_skills:
      - no-such-skill
---

# Broken Kit
"""


class _FakeRegistry:
    def get_tool(self, name: str):
        return None


class _FakeAgent:
    MAX_TOOL_RELOADS_PER_TURN = 1

    def __init__(self, data_dir: Path, role: str = "admin", skill_manager=None):
        from nymeria.core.agent import NymeriaAgent

        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.tool_registry = _FakeRegistry()
        self.skill_manager = skill_manager
        self._pending_tool_reload = {}
        self._turn_reload_count = {}
        self.accounts_repo = SimpleNamespace(
            get_user_by_id=lambda user_id: SimpleNamespace(role=role)
        )
        self.profile_manager = SimpleNamespace(
            get_profile=lambda user_id: SimpleNamespace(
                tool_preferences=SimpleNamespace(default_thread_tools=None),
                enabled_global_skills=[],
            )
        )
        self.invalidated: list[str] = []
        self._resolve_temp = NymeriaAgent._resolve_temporary_tools

    def invalidate_thread_config_cache(self, thread_id: str) -> None:
        self.invalidated.append(thread_id)

    def _resolve_temporary_tools(self, tc):
        return self._resolve_temp(self, tc)


def _write_skill(root: Path, name: str, content: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")
    return d


def _manager_with(tmp_path: Path, skills: dict[str, str]) -> SkillManager:
    bundled = tmp_path / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    for name, md in skills.items():
        _write_skill(bundled, name, md)
    manager = SkillManager(bundled_dir=bundled, data_skills_dir=tmp_path / "skills")
    manager.EXTERNAL_REFRESH_INTERVAL_SECONDS = 0.0
    return manager


_CONFIG = {"configurable": {"thread_id": "thread-a", "user_id": "user-a"}}


# ---------------------------------------------------------------------------
# #41: parsing + helpers
# ---------------------------------------------------------------------------


def test_required_skills_parse_and_kit_flag(tmp_path: Path):
    skill = load_skill_directory(
        _write_skill(tmp_path, "outer-kit", OUTER_KIT_MD), scope="bundled"
    )
    assert skill is not None
    assert skill.required_skills == ["inner-kit", "plain-skill"]
    assert skill.is_skill_kit is True

    skills_only = load_skill_directory(
        _write_skill(tmp_path, "skills-only-kit", SKILLS_ONLY_KIT_MD), scope="bundled"
    )
    assert skills_only is not None
    assert skills_only.required_tools == []
    # A skill nesting other skills is a kit even with no tools of its own.
    assert skills_only.is_skill_kit is True


def test_required_skills_accepts_string_and_dedupes(tmp_path: Path):
    md = """---
name: str-kit
description: String-valued required_skills.
metadata:
  nymeria:
    required_skills: plain-skill
---

# Body
"""
    skill = load_skill_directory(
        _write_skill(tmp_path, "str-kit", md), scope="bundled"
    )
    assert skill is not None
    assert skill.required_skills == ["plain-skill"]


def test_resolve_nested_skills_skips_self_and_dupes(tmp_path: Path):
    md = """---
name: self-kit
description: Kit that names itself and repeats a dep.
metadata:
  nymeria:
    required_skills:
      - self-kit
      - plain-skill
      - plain-skill
---

# Body
"""
    manager = _manager_with(tmp_path, {"self-kit": md, "plain-skill": PLAIN_MD})
    skill = manager.get("self-kit")
    assert skill is not None
    nested, missing = resolve_nested_skills(skill, manager, "user-a")
    assert [s.name for s in nested] == ["plain-skill"]
    assert missing == []


def test_expanded_required_tools_union_preserves_order(tmp_path: Path):
    manager = _manager_with(
        tmp_path,
        {"outer-kit": OUTER_KIT_MD, "inner-kit": INNER_KIT_MD, "plain-skill": PLAIN_MD},
    )
    outer = manager.get("outer-kit")
    nested, missing = resolve_nested_skills(outer, manager, "user-a")
    assert missing == []
    assert expanded_required_tools(outer, nested) == [
        "hello_test",
        "memory_clear_all",
    ]


# ---------------------------------------------------------------------------
# #41: Skill() meta-tool activation
# ---------------------------------------------------------------------------


def test_skill_meta_tool_nested_kit_binds_union_and_inlines_bodies(tmp_path: Path):
    manager = _manager_with(
        tmp_path,
        {"outer-kit": OUTER_KIT_MD, "inner-kit": INNER_KIT_MD, "plain-skill": PLAIN_MD},
    )
    outer = manager.get("outer-kit")
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool(
            [outer], skill_manager=manager, user_id="user-a"
        )
        result = skill_tool.func("outer-kit", tool_call_id="call-1", config=_CONFIG)
    finally:
        set_current_agent(None)

    assert isinstance(result, Command)
    content = result.update["messages"][0].content
    # Outer body plus FULL nested bodies (one level deep).
    assert "Outer Kit" in content
    assert "[Nested skill: inner-kit (required by outer-kit)]" in content
    assert "Use memory_clear_all wisely." in content
    assert "[Nested skill: plain-skill (required by outer-kit)]" in content
    # The nested kit's own required_skills are LISTED, never expanded.
    assert "deep-skill" in content
    assert "one level deep" in content
    # One union bind: both the outer and nested kit tools are bound.
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert "hello_test" in tc.temporary_tools
    assert "memory_clear_all" in tc.temporary_tools
    assert sorted(agent._pending_tool_reload["thread-a"]["new_tools"]) == [
        "hello_test",
        "memory_clear_all",
    ]


def test_skill_meta_tool_missing_nested_skill_is_strict(tmp_path: Path):
    manager = _manager_with(tmp_path, {"broken-kit": MISSING_NESTED_KIT_MD})
    broken = manager.get("broken-kit")
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool(
            [broken], skill_manager=manager, user_id="user-a"
        )
        result = skill_tool.func("broken-kit", tool_call_id="call-1", config=_CONFIG)
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "[Skill Kit activation failed: broken-kit]" in result
    assert "no-such-skill" in result
    # No partial activation: nothing bound, no reload queued.
    assert agent.thread_config_manager.get_config("thread-a") is None
    assert agent._pending_tool_reload == {}


def test_skill_meta_tool_defer_lists_nested_names_only(tmp_path: Path):
    manager = _manager_with(
        tmp_path,
        {"outer-kit": OUTER_KIT_MD, "inner-kit": INNER_KIT_MD, "plain-skill": PLAIN_MD},
    )
    outer = manager.get("outer-kit")
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool(
            [outer], skill_manager=manager, user_id="user-a"
        )
        result = skill_tool.func(
            "outer-kit", defer=True, tool_call_id="call-1", config=_CONFIG
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "[Required skills (deferred)]" in result
    assert "inner-kit (kit):" in result
    assert "plain-skill (skill):" in result
    # Names + descriptions only: the nested bodies must NOT be inlined.
    assert "Use memory_clear_all wisely." not in result
    # Nothing bound.
    assert agent.thread_config_manager.get_config("thread-a") is None
    assert agent._pending_tool_reload == {}


def test_skill_meta_tool_defer_missing_nested_listed_not_fatal(tmp_path: Path):
    manager = _manager_with(tmp_path, {"broken-kit": MISSING_NESTED_KIT_MD})
    broken = manager.get("broken-kit")
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool(
            [broken], skill_manager=manager, user_id="user-a"
        )
        result = skill_tool.func(
            "broken-kit", defer=True, tool_call_id="call-1", config=_CONFIG
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "no-such-skill: (not installed" in result
    assert agent.thread_config_manager.get_config("thread-a") is None


def test_skill_meta_tool_ttl_override_governs_whole_union(tmp_path: Path):
    manager = _manager_with(
        tmp_path,
        {"outer-kit": OUTER_KIT_MD, "inner-kit": INNER_KIT_MD, "plain-skill": PLAIN_MD},
    )
    outer = manager.get("outer-kit")
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool(
            [outer], skill_manager=manager, user_id="user-a"
        )
        skill_tool.func("outer-kit", ttl="4w", tool_call_id="call-1", config=_CONFIG)
    finally:
        set_current_agent(None)

    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    week_out = datetime.now(timezone.utc) + timedelta(days=7)
    for name in ("hello_test", "memory_clear_all"):
        assert name in tc.temporary_tools
        # The single 4w override wins over BOTH kits' declared TTLs
        # (30m outer, 7d inner): one activation, one lifetime.
        assert tc.temporary_tools[name].expires_at > week_out


def test_skill_meta_tool_ttl_notice_for_skills_only_kit_with_no_tools(tmp_path: Path):
    manager = _manager_with(
        tmp_path, {"skills-only-kit": SKILLS_ONLY_KIT_MD, "plain-skill": PLAIN_MD}
    )
    kit = manager.get("skills-only-kit")
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool(
            [kit], skill_manager=manager, user_id="user-a"
        )
        result = skill_tool.func(
            "skills-only-kit", ttl="2h", tool_call_id="call-1", config=_CONFIG
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    # The union is empty, so the ttl no-effect notice applies.
    assert "no effect" in result
    assert "[Nested skill: plain-skill (required by skills-only-kit)]" in result
    assert agent.thread_config_manager.get_config("thread-a") is None


# ---------------------------------------------------------------------------
# #41: slash-path activation (activate_skill_kit / deactivate_skill_kit)
# ---------------------------------------------------------------------------


def test_activate_skill_kit_binds_expanded_union(tmp_path: Path):
    manager = _manager_with(
        tmp_path,
        {"outer-kit": OUTER_KIT_MD, "inner-kit": INNER_KIT_MD, "plain-skill": PLAIN_MD},
    )
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    set_current_agent(agent)
    try:
        ok, msg = activate_skill_kit(
            agent=agent,
            thread_id="thread-a",
            user_id="user-a",
            skill_name="outer-kit",
        )
    finally:
        set_current_agent(None)

    assert ok is True
    assert "Required skills pulled in (one level): inner-kit, plain-skill." in msg
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert "outer-kit" in tc.enabled_skills
    assert "hello_test" in tc.temporary_tools
    assert "memory_clear_all" in tc.temporary_tools


def test_activate_skill_kit_missing_nested_fails_before_mutation(tmp_path: Path):
    manager = _manager_with(tmp_path, {"broken-kit": MISSING_NESTED_KIT_MD})
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    set_current_agent(agent)
    try:
        ok, msg = activate_skill_kit(
            agent=agent,
            thread_id="thread-a",
            user_id="user-a",
            skill_name="broken-kit",
        )
    finally:
        set_current_agent(None)

    assert ok is False
    assert "no-such-skill" in msg
    # Strict: the kit was NOT enabled and nothing bound.
    assert agent.thread_config_manager.get_config("thread-a") is None


def test_activate_skill_kit_skills_only_kit_binds_nothing(tmp_path: Path):
    manager = _manager_with(
        tmp_path, {"skills-only-kit": SKILLS_ONLY_KIT_MD, "plain-skill": PLAIN_MD}
    )
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    set_current_agent(agent)
    try:
        ok, msg = activate_skill_kit(
            agent=agent,
            thread_id="thread-a",
            user_id="user-a",
            skill_name="skills-only-kit",
        )
    finally:
        set_current_agent(None)

    assert ok is True
    assert "no tools to bind" in msg
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert "skills-only-kit" in tc.enabled_skills
    assert tc.temporary_tools == {}


def test_deactivate_skill_kit_evicts_expanded_union(tmp_path: Path):
    manager = _manager_with(
        tmp_path,
        {"outer-kit": OUTER_KIT_MD, "inner-kit": INNER_KIT_MD, "plain-skill": PLAIN_MD},
    )
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    set_current_agent(agent)
    try:
        ok, _ = activate_skill_kit(
            agent=agent,
            thread_id="thread-a",
            user_id="user-a",
            skill_name="outer-kit",
        )
        assert ok
        ok, msg = deactivate_skill_kit(
            agent=agent,
            thread_id="thread-a",
            user_id="user-a",
            skill_name="outer-kit",
        )
    finally:
        set_current_agent(None)

    assert ok is True
    assert "hello_test" in msg and "memory_clear_all" in msg
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert "outer-kit" not in tc.enabled_skills
    assert "hello_test" not in tc.temporary_tools
    assert "memory_clear_all" not in tc.temporary_tools


# ---------------------------------------------------------------------------
# #41: graph-cache fingerprint folds nested deps
# ---------------------------------------------------------------------------


def _fingerprint_agent(tmp_path: Path, manager: SkillManager) -> _FakeAgent:
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="thread-a", enabled_skills=["outer-kit"])
    )
    return agent


def test_skills_fingerprint_folds_nested_dependency_changes(tmp_path: Path):
    manager = _manager_with(
        tmp_path,
        {"outer-kit": OUTER_KIT_MD, "inner-kit": INNER_KIT_MD, "plain-skill": PLAIN_MD},
    )
    agent = _fingerprint_agent(tmp_path, manager)
    before = skills_fingerprint(agent, "user-a", "thread-a")

    # Editing the NESTED kit's required_tools must change the fingerprint of a
    # thread whose ACTIVE kit merely nests it (defer=false binds the union).
    inner_dir = tmp_path / "bundled" / "inner-kit"
    (inner_dir / "SKILL.md").write_text(
        INNER_KIT_MD.replace("- memory_clear_all", "- bash_execute"),
        encoding="utf-8",
    )
    manager.refresh_if_stale(force=True)
    after = skills_fingerprint(agent, "user-a", "thread-a")
    assert before != after


def test_skills_fingerprint_folds_required_skills_list(tmp_path: Path):
    manager = _manager_with(
        tmp_path,
        {"outer-kit": OUTER_KIT_MD, "inner-kit": INNER_KIT_MD, "plain-skill": PLAIN_MD},
    )
    agent = _fingerprint_agent(tmp_path, manager)
    before = skills_fingerprint(agent, "user-a", "thread-a")

    outer_dir = tmp_path / "bundled" / "outer-kit"
    (outer_dir / "SKILL.md").write_text(
        OUTER_KIT_MD.replace("      - inner-kit\n", ""), encoding="utf-8"
    )
    manager.refresh_if_stale(force=True)
    after = skills_fingerprint(agent, "user-a", "thread-a")
    assert before != after


# ---------------------------------------------------------------------------
# #41: skill_write / skill_edit validation of required_skills
# ---------------------------------------------------------------------------


class _AuthoringAgent(_FakeAgent):
    def __init__(self, root: Path, role: str = "admin"):
        bundled = root / "bundled"
        bundled.mkdir(parents=True, exist_ok=True)
        _write_skill(bundled, "plain-skill", PLAIN_MD)
        manager = SkillManager(bundled_dir=bundled, data_skills_dir=root / "skills")
        manager.EXTERNAL_REFRESH_INTERVAL_SECONDS = 0.0
        super().__init__(root / "threads", role=role, skill_manager=manager)
        self._graph_cache_lock = threading.RLock()
        self._user_graphs = {}
        self._async_user_graphs = {}

    def _rebuild_default_graphs(self) -> None:
        return None


def _skill_write(markdown: str, agent) -> dict:
    from nymeria.tools.skill_config import skill_write

    set_current_agent(agent)
    try:
        result = skill_write.func(
            markdown=markdown,
            dry_run=True,
            tool_call_id="call-w",
            config=_CONFIG,
        )
    finally:
        set_current_agent(None)
    assert isinstance(result, str)
    return json.loads(result.split("\n\n[", 1)[0])


def test_skill_write_rejects_unknown_required_skill(tmp_path: Path):
    agent = _AuthoringAgent(tmp_path)
    md = """---
name: composed-kit
description: Kit nesting a skill that does not exist.
metadata:
  nymeria:
    required_skills:
      - ghost-skill
---

# Composed Kit
"""
    payload = _skill_write(md, agent)
    assert payload["ok"] is False
    assert "ghost-skill" in payload["error"]["message"]


def test_skill_write_rejects_self_referencing_kit(tmp_path: Path):
    agent = _AuthoringAgent(tmp_path)
    md = """---
name: narcissist-kit
description: Kit requiring itself.
metadata:
  nymeria:
    required_skills:
      - narcissist-kit
---

# Narcissist Kit
"""
    payload = _skill_write(md, agent)
    assert payload["ok"] is False
    assert "cannot require itself" in payload["error"]["message"]


def test_skill_write_accepts_installed_required_skill(tmp_path: Path):
    agent = _AuthoringAgent(tmp_path)
    md = """---
name: composed-kit
description: Kit nesting an installed plain skill.
metadata:
  nymeria:
    required_skills:
      - plain-skill
---

# Composed Kit
"""
    payload = _skill_write(md, agent)
    assert payload["ok"] is True
    assert payload["skill"]["required_skills"] == ["plain-skill"]
    assert payload["skill"]["is_skill_kit"] is True
