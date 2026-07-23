"""Tests for the kit-composition family (backlog #41 + #26).

#41: nested required skills/kits + defer-aware activation on Skill().
#26: kit-declared thread templates (callable threads that spawn on first call).
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from langgraph.types import Command

from nymeria.agents import tool_factory
from nymeria.core.agent import set_current_agent
from nymeria.core.agent_graph import (
    build_template_thread_tools,
    compute_tool_superset,
    select_tools_for_graph,
    skills_fingerprint,
)
from nymeria.core.command_service import activate_skill_kit, deactivate_skill_kit
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.skills import (
    SkillManager,
    ThreadTemplate,
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

SIMPLE_BINDABLE_KIT_MD = """---
name: simple-bindable-kit
description: Kit binding a single required tool.
metadata:
  nymeria:
    required_tools:
      - hello_test
    tool_ttl: 30m
---

# Simple Bindable Kit
"""

BINDABLE_TEMPLATE_KIT_MD = """---
name: bindable-template-kit
description: Kit with a required tool AND a thread template.
metadata:
  nymeria:
    required_tools:
      - hello_test
    tool_ttl: 30m
    thread_templates:
      - name: helper-thread
        description: A helper thread.
---

# Bindable Template Kit
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


def test_activate_skill_kit_bind_failure_leaves_kit_inactive(tmp_path: Path, monkeypatch):
    """Strict-fail: a bind failure must NOT enable the kit (review fix, #41).

    The original order enabled the kit first, so a failed bind still left the
    kit in enabled_skills and its #26 template tools surfaced at the next graph
    build despite the reported failure. Bind runs first now.
    """
    manager = _manager_with(
        tmp_path, {"bindable-template-kit": BINDABLE_TEMPLATE_KIT_MD}
    )
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    # Fetch the real submodule from sys.modules: the "tool_search" NAME on the
    # nymeria.tools package is the tool object, which shadows the submodule for
    # both attribute access and `import ... as`.
    import importlib

    tool_search_mod = importlib.import_module("nymeria.tools.tool_search")
    monkeypatch.setattr(
        tool_search_mod,
        "bind_tools_for_thread",
        lambda *a, **k: SimpleNamespace(ok=False, text="tool 'hello_test' unavailable"),
    )
    set_current_agent(agent)
    try:
        ok, msg = activate_skill_kit(
            agent=agent,
            thread_id="thread-a",
            user_id="user-a",
            skill_name="bindable-template-kit",
        )
    finally:
        set_current_agent(None)

    assert ok is False
    assert "was NOT activated" in msg
    # The old message admitted a half-activated state; it must not resurface.
    assert "added to enabled_skills" not in msg
    # Nothing was written: the kit is not enabled, so its template tool cannot
    # surface at the next build.
    assert agent.thread_config_manager.get_config("thread-a") is None


def test_activate_skill_kit_enable_failure_rolls_back_binding(
    tmp_path: Path, monkeypatch
):
    """Bind succeeds but the enable write fails: the bound tools roll back so no
    partial activation survives (review fix, #41)."""
    manager = _manager_with(
        tmp_path, {"simple-bindable-kit": SIMPLE_BINDABLE_KIT_MD}
    )
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)

    def boom(agent_arg, thread_id, skill_name):
        raise RuntimeError("store write failed")

    monkeypatch.setattr("nymeria.tools.skill_config._activate_skill_on_thread", boom)
    set_current_agent(agent)
    try:
        ok, msg = activate_skill_kit(
            agent=agent,
            thread_id="thread-a",
            user_id="user-a",
            skill_name="simple-bindable-kit",
        )
    finally:
        set_current_agent(None)

    assert ok is False
    assert "Failed to add skill to thread" in msg
    tc = agent.thread_config_manager.get_config("thread-a")
    # The real bind wrote hello_test; rollback popped it and the kit was never
    # enabled, so nothing partial survives.
    assert tc is None or "hello_test" not in (tc.temporary_tools or {})
    assert tc is None or "simple-bindable-kit" not in (tc.enabled_skills or [])


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


# ---------------------------------------------------------------------------
# #26: thread templates - parsing and the ThreadTemplate model
# ---------------------------------------------------------------------------


TEMPLATE_KIT_MD = """---
name: template-kit
description: Kit declaring a callable-thread template.
metadata:
  nymeria:
    thread_templates:
      - name: research-helper
        description: Deep research helper thread.
        instructions: You are a focused research thread.
        tools:
          - memory_clear_all
        ttl_hours: 24
---

# Template Kit

Call research_helper for deep dives.
"""

SLOPPY_TEMPLATE_KIT_MD = """---
name: sloppy-kit
description: Kit with one invalid and one valid template.
metadata:
  nymeria:
    thread_templates:
      - name: no-description-here
      - name: good-helper
        description: The good one.
---

# Sloppy Kit
"""


def test_thread_template_parse_normalization_and_kit_flag(tmp_path: Path):
    skill = load_skill_directory(
        _write_skill(tmp_path, "template-kit", TEMPLATE_KIT_MD), scope="bundled"
    )
    assert skill is not None
    templates = skill.thread_templates
    # Kebab-case declared name normalizes to a tool-safe underscore name.
    assert [t.name for t in templates] == ["research_helper"]
    t = templates[0]
    assert t.thread_title == "Research Helper"
    assert t.tools == ["memory_clear_all"]
    assert t.ttl_hours == 24
    # A skill declaring templates is a kit even with no required tools.
    assert skill.is_skill_kit is True


def test_thread_templates_lenient_load_skips_invalid_entries(tmp_path: Path):
    skill = load_skill_directory(
        _write_skill(tmp_path, "sloppy-kit", SLOPPY_TEMPLATE_KIT_MD), scope="bundled"
    )
    assert skill is not None
    # The loader is lenient: the description-less entry is skipped with a
    # warning, the valid one survives.
    assert [t.name for t in skill.thread_templates] == ["good_helper"]


def test_thread_template_model_validation():
    with pytest.raises(Exception):
        ThreadTemplate(name="Bad Name!", description="x")
    with pytest.raises(Exception):
        ThreadTemplate(name="ok_name", description="x", ttl_hours=0)
    with pytest.raises(Exception):
        ThreadTemplate(name="ok_name", description="x", instructions="a" * 5001)
    with pytest.raises(Exception):
        # extra="forbid": a typo'd key must fail, not be silently ignored.
        ThreadTemplate(name="ok_name", description="x", bogus_key=True)
    t = ThreadTemplate(name="ok_name", description="x", tools="alpha, beta, alpha")
    assert t.tools == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# #26: template tool surfacing (graph build + dispatch superset)
# ---------------------------------------------------------------------------


def test_build_template_thread_tools_derivation_rules(tmp_path: Path):
    manager = _manager_with(tmp_path, {"template-kit": TEMPLATE_KIT_MD})
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)

    active_tc = ThreadConfig(thread_id="t1", enabled_skills=["template-kit"])
    tools = build_template_thread_tools(agent, "user-a", active_tc, set())
    assert [t.name for t in tools] == ["research_helper"]
    assert "Skill Kit 'template-kit'" in tools[0].description

    # Kit not active on the thread: nothing derived.
    idle_tc = ThreadConfig(thread_id="t2")
    assert build_template_thread_tools(agent, "user-a", idle_tc, set()) == []

    # disabled_tools stays authoritative over template surfacing.
    disabled_tc = ThreadConfig(
        thread_id="t3",
        enabled_skills=["template-kit"],
        disabled_tools=["research_helper"],
    )
    assert build_template_thread_tools(agent, "user-a", disabled_tc, set()) == []

    # An existing bound name (e.g. the materialized thread's ordinary callable
    # tool) shadows the template tool.
    assert (
        build_template_thread_tools(agent, "user-a", active_tc, {"research_helper"})
        == []
    )


def test_build_template_thread_tools_skips_own_callable_name(tmp_path: Path):
    """Self-invocation guard (review fix, #26): a callable thread whose own
    callable_name equals a template name (the declaring kit active on the
    materialized child via kit= or a global skill) must NOT be handed a tool
    that invokes itself; an ask would block on its own thread lock until tool
    timeout.
    """
    manager = _manager_with(tmp_path, {"template-kit": TEMPLATE_KIT_MD})
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)

    own_tc = ThreadConfig(
        thread_id="child-1",
        enabled_skills=["template-kit"],
        callable=True,
        callable_name="research_helper",
    )
    assert build_template_thread_tools(agent, "user-a", own_tc, set()) == []

    # A DIFFERENT callable name still gets the template (no self-loop to skip).
    other_tc = ThreadConfig(
        thread_id="child-2",
        enabled_skills=["template-kit"],
        callable=True,
        callable_name="something_else",
    )
    tools = build_template_thread_tools(agent, "user-a", other_tc, set())
    assert [t.name for t in tools] == ["research_helper"]


def _graph_agent(tmp_path: Path, manager: SkillManager):
    """MagicMock-pattern NymeriaAgent (as in test_graph_build_unification) with
    a real ThreadConfigManager and real SkillManager for template tests."""
    from nymeria.core.agent import NymeriaAgent

    with patch.object(NymeriaAgent, "__init__", lambda self: None):
        agent = NymeriaAgent()

    agent.settings = MagicMock()
    agent.settings.dynamic_tool_binding = False
    agent.settings.allow_unbound_tool_calls = False
    agent.thread_config_manager = ThreadConfigManager(tmp_path / "graph-data")
    agent.profile_manager = MagicMock()
    agent.profile_manager.get_profile.return_value = SimpleNamespace(
        tool_preferences=SimpleNamespace(default_thread_tools=None),
        enabled_global_skills=[],
    )
    agent.accounts_repo = MagicMock()
    agent.accounts_repo.list_threads_for_user.return_value = []
    agent.accounts_repo.get_user_by_id.return_value = SimpleNamespace(role="admin")
    agent.tool_registry = MagicMock()
    agent.tool_registry.get_all_tools.return_value = []
    agent.tool_registry.get_tool.return_value = None
    agent._callable_tool_thread_map = {}
    agent._get_team_scoped_callable_threads = MagicMock(return_value=[])
    agent.skill_manager = manager
    return agent


def test_select_tools_for_graph_binds_active_templates(tmp_path: Path):
    manager = _manager_with(tmp_path, {"template-kit": TEMPLATE_KIT_MD})
    agent = _graph_agent(tmp_path, manager)

    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="thread-a", enabled_skills=["template-kit"])
    )
    tools, _ = select_tools_for_graph(agent, "user-a", "thread-a")
    assert "research_helper" in {t.name for t in tools}

    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="thread-b",
            enabled_skills=["template-kit"],
            disabled_tools=["research_helper"],
        )
    )
    tools, _ = select_tools_for_graph(agent, "user-a", "thread-b")
    assert "research_helper" not in {t.name for t in tools}

    # Thread without the kit active: no template tool bound.
    tools, _ = select_tools_for_graph(agent, "user-a", "thread-c")
    assert "research_helper" not in {t.name for t in tools}


def test_compute_tool_superset_includes_installed_templates(tmp_path: Path):
    manager = _manager_with(tmp_path, {"template-kit": TEMPLATE_KIT_MD})
    agent = _graph_agent(tmp_path, manager)

    # The kit is enabled on NO thread: the dispatch superset still carries the
    # template so the deferred path (tool_invoke) can run it, mirroring the
    # "visibility is not reachability" loadability rule.
    tools, names = compute_tool_superset(agent, "user-a", "thread-z")
    assert "research_helper" in names
    template_tool = next(t for t in tools if t.name == "research_helper")
    assert "Skill Kit 'template-kit'" in template_tool.description


def test_skills_fingerprint_folds_template_edits(tmp_path: Path):
    manager = _manager_with(tmp_path, {"template-kit": TEMPLATE_KIT_MD})
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="thread-a", enabled_skills=["template-kit"])
    )
    before = skills_fingerprint(agent, "user-a", "thread-a")

    kit_dir = tmp_path / "bundled" / "template-kit"
    (kit_dir / "SKILL.md").write_text(
        TEMPLATE_KIT_MD.replace("ttl_hours: 24", "ttl_hours: 48"), encoding="utf-8"
    )
    manager.refresh_if_stale(force=True)
    after = skills_fingerprint(agent, "user-a", "thread-a")
    assert before != after


# ---------------------------------------------------------------------------
# #26: Skill() meta-tool surfacing (defer listing + defer=false registration)
# ---------------------------------------------------------------------------


def test_skill_meta_tool_defer_lists_template_schemas(tmp_path: Path):
    manager = _manager_with(tmp_path, {"template-kit": TEMPLATE_KIT_MD})
    kit = manager.get("template-kit")
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool(
            [kit], skill_manager=manager, user_id="user-a"
        )
        result = skill_tool.func(
            "template-kit", defer=True, tool_call_id="call-1", config=_CONFIG
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "[Thread templates (deferred)]" in result
    assert "research_helper: Deep research helper thread." in result
    # The compact args schema is rendered for tool_invoke use.
    assert "task" in result
    # Nothing registered: the thread config was never created.
    assert agent.thread_config_manager.get_config("thread-a") is None
    assert agent._pending_tool_reload == {}


def test_skill_meta_tool_registers_templates_on_activation(tmp_path: Path):
    manager = _manager_with(tmp_path, {"template-kit": TEMPLATE_KIT_MD})
    kit = manager.get("template-kit")
    agent = _FakeAgent(tmp_path / "data", skill_manager=manager)
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool(
            [kit], skill_manager=manager, user_id="user-a"
        )
        result = skill_tool.func("template-kit", tool_call_id="call-1", config=_CONFIG)
        again = skill_tool.func("template-kit", tool_call_id="call-2", config=_CONFIG)
    finally:
        set_current_agent(None)

    # Legacy rebuild mode: registration enabled the kit (a skill-only change),
    # so the invocation queues a reload and stops the turn.
    assert isinstance(result, Command)
    content = result.update["messages"][0].content
    assert "[Thread templates registered]: research_helper" in content
    assert "STOP NOW" in content
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert "template-kit" in tc.enabled_skills
    assert "thread-a" in agent._pending_tool_reload

    # Re-activation is idempotent: no second reload command, plain text.
    assert isinstance(again, str)
    assert "[Thread templates registered]" in again


# ---------------------------------------------------------------------------
# #26: lazy materialization on first call
# ---------------------------------------------------------------------------


class _TemplateAgent(_FakeAgent):
    """_FakeAgent extended with the surfaces template materialization touches."""

    def __init__(self, data_dir: Path, skill_manager=None):
        super().__init__(data_dir, skill_manager=skill_manager)
        self.owned_threads: list[str] = []
        self.accounts_repo = SimpleNamespace(
            get_user_by_id=lambda user_id: SimpleNamespace(role="admin"),
            list_threads_for_user=lambda user_id: list(self.owned_threads),
        )
        self.meta_upserts: list = []
        self.thread_metadata_manager = SimpleNamespace(
            get_thread=lambda user_id, thread_id: None,
            upsert_thread=lambda user_id, thread_id, **kw: self.meta_upserts.append(
                (thread_id, kw)
            ),
        )
        self.synced = 0

    def sync_agent_tools(self):
        self.synced += 1


def _template_fixture(tmp_path: Path):
    manager = _manager_with(tmp_path, {"template-kit": TEMPLATE_KIT_MD})
    agent = _TemplateAgent(tmp_path / "data", skill_manager=manager)
    kit = manager.get("template-kit")
    assert kit is not None
    template = kit.thread_templates[0]
    tool = tool_factory.create_template_thread_tool("template-kit", template)
    return manager, agent, tool


def _fake_spawn_factory(agent: _TemplateAgent, spawn_calls: list, *, delay: float = 0.0):
    """Fake for the module-level _spawn_template_thread seam: registers a
    fresh callable thread on the fake agent and returns a spawn receipt."""

    def fake_spawn(agent_arg, template, config):
        spawn_calls.append(template.name)
        if delay:
            time.sleep(delay)
        thread_id = f"spawned-{template.name}-{len(spawn_calls)}"
        agent.thread_config_manager.save_config(
            ThreadConfig(
                thread_id=thread_id,
                callable=True,
                callable_name=f"spawned_{template.name}_x",
                callable_description="fresh spawn",
            )
        )
        agent.owned_threads.append(thread_id)
        return (
            f"[Spawned]: thread_id={thread_id}\n"
            "Callable as: spawned_research_helper_x(task=...)"
        )

    return fake_spawn


def test_template_first_call_materializes_then_routes(tmp_path: Path, monkeypatch):
    _, agent, tool = _template_fixture(tmp_path)
    spawn_calls: list[str] = []
    invoke_calls: list[tuple] = []
    monkeypatch.setattr(
        tool_factory, "_spawn_template_thread", _fake_spawn_factory(agent, spawn_calls)
    )

    def fake_invoke(child_tc, task, mode, config):
        invoke_calls.append((child_tc.thread_id, task, mode))
        return "child answer"

    monkeypatch.setattr(tool_factory, "_invoke_materialized", fake_invoke)

    set_current_agent(agent)
    try:
        first = tool.func(task="dig into X", config=_CONFIG)
        second = tool.func(task="follow up", mode="handoff", config=_CONFIG)
    finally:
        set_current_agent(None)

    # Exactly one spawn; the first call carries the materialization receipt.
    assert spawn_calls == ["research_helper"]
    assert first.startswith("[Materialized]: thread_id=spawned-research_helper-1")
    assert first.endswith("child answer")
    assert "[Materialized]" not in second

    # Finalize renamed the spawn to the template tool name (the routing key).
    tc = agent.thread_config_manager.get_config("spawned-research_helper-1")
    assert tc is not None
    assert tc.callable_name == "research_helper"
    assert tc.callable_description == "Deep research helper thread."

    # Provenance stamp (best-effort platform_meta).
    assert agent.meta_upserts
    thread_id, kwargs = agent.meta_upserts[0]
    assert thread_id == "spawned-research_helper-1"
    assert kwargs["platform_meta"] == {
        "template_skill": "template-kit",
        "template_name": "research_helper",
    }
    assert agent.synced >= 1

    # Both calls routed to the SAME thread through the callable seam.
    assert invoke_calls == [
        ("spawned-research_helper-1", "dig into X", "ask"),
        ("spawned-research_helper-1", "follow up", "handoff"),
    ]


def test_template_materialization_drives_real_spawn_thread_func(
    tmp_path: Path, monkeypatch
):
    """End-to-end (review fix, #26): the first call runs the REAL
    _spawn_template_thread -> spawn_thread.func keyword wiring (only
    _invoke_materialized, an LLM turn, is stubbed). A spawn_thread signature
    drift now fails loudly here instead of passing silently under a mock.
    """
    _, agent, tool = _template_fixture(tmp_path)
    # spawn_thread claims ownership through accounts_repo; record it so the
    # materialized thread is owned, mirroring production. (No agent.settings is
    # needed: the template declares no model, so the tier-alias branch that
    # reads settings is skipped.)
    agent.accounts_repo.claim_thread = lambda tid, uid: agent.owned_threads.append(tid)

    invoked: list = []

    def fake_invoke(child_tc, task, mode, config):
        invoked.append((child_tc.thread_id, child_tc.callable_name, task, mode))
        return "child answer"

    monkeypatch.setattr(tool_factory, "_invoke_materialized", fake_invoke)

    import importlib

    spawn_mod = importlib.import_module("nymeria.tools.spawn_thread")
    with spawn_mod._spawn_rate_lock:
        spawn_mod._spawn_counts.clear()
    monkeypatch.setattr(
        "nymeria.core.event_bus.publish_sync_event", lambda *a, **k: None
    )

    set_current_agent(agent)
    try:
        result = tool.func(task="dig in", config=_CONFIG)
    finally:
        set_current_agent(None)

    # Exactly one real spawn happened; the call carries the materialization
    # receipt and the child's answer.
    assert result.startswith("[Materialized]: thread_id=spawned-")
    assert result.endswith("child answer")
    spawned_ids = [t for t in agent.owned_threads if t.startswith("spawned-")]
    assert len(spawned_ids) == 1

    child = agent.thread_config_manager.get_config(spawned_ids[0])
    assert child is not None
    # Finalize renamed the spawn's callable_name to the template tool name.
    assert child.callable is True
    assert child.callable_name == "research_helper"
    # The template.tools list survived the REAL spawn tool-resolution pass.
    assert "memory_clear_all" in (child.enabled_tools or [])
    # The route went through the renamed child via the real keyword wiring.
    assert invoked == [(spawned_ids[0], "research_helper", "dig in", "ask")]


def test_template_materialization_inherits_caller_team(tmp_path: Path, monkeypatch):
    """Backlog #97 (supersedes kit decision #15's accepted edge): a template
    thread materialized from a TEAMED caller inherits the caller's callable
    team through the real spawn machinery, so the teamed caller can invoke
    what it just materialized under full team isolation."""
    _, agent, tool = _template_fixture(tmp_path)
    agent.accounts_repo.claim_thread = lambda tid, uid: agent.owned_threads.append(tid)
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="thread-a",
            callable_team_id="team-a",
            callable_team_name="Ops",
        )
    )

    monkeypatch.setattr(
        tool_factory, "_invoke_materialized", lambda *a, **k: "child answer"
    )

    import importlib

    spawn_mod = importlib.import_module("nymeria.tools.spawn_thread")
    with spawn_mod._spawn_rate_lock:
        spawn_mod._spawn_counts.clear()
    monkeypatch.setattr(
        "nymeria.core.event_bus.publish_sync_event", lambda *a, **k: None
    )

    set_current_agent(agent)
    try:
        result = tool.func(task="dig in", config=_CONFIG)
    finally:
        set_current_agent(None)

    assert result.startswith("[Materialized]: thread_id=spawned-")
    spawned_ids = [t for t in agent.owned_threads if t.startswith("spawned-")]
    assert len(spawned_ids) == 1
    child = agent.thread_config_manager.get_config(spawned_ids[0])
    assert child is not None
    assert child.callable_team_id == "team-a"
    assert child.callable_team_name == "Ops"


def test_template_concurrent_first_calls_spawn_once(tmp_path: Path, monkeypatch):
    _, agent, tool = _template_fixture(tmp_path)
    spawn_calls: list[str] = []
    monkeypatch.setattr(
        tool_factory,
        "_spawn_template_thread",
        _fake_spawn_factory(agent, spawn_calls, delay=0.2),
    )
    monkeypatch.setattr(
        tool_factory, "_invoke_materialized", lambda tc, task, mode, config: "answer"
    )

    results: list = [None, None]

    def call(i: int):
        results[i] = tool.func(task=f"task {i}", config=_CONFIG)

    set_current_agent(agent)
    try:
        workers = [threading.Thread(target=call, args=(i,)) for i in range(2)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=10)
    finally:
        set_current_agent(None)

    # The in-process lock serializes the two first calls: one spawn, the
    # loser routes to the winner's freshly materialized thread.
    assert spawn_calls == ["research_helper"]
    assert all(r is not None and r.endswith("answer") for r in results)
    assert sum("[Materialized]" in r for r in results) == 1


def test_template_call_fails_closed_when_kit_uninstalled(tmp_path: Path, monkeypatch):
    manager = _manager_with(tmp_path, {"template-kit": TEMPLATE_KIT_MD})
    kit = manager.get("template-kit")
    template = kit.thread_templates[0]
    tool = tool_factory.create_template_thread_tool("template-kit", template)

    # The agent's CURRENT store no longer has the kit: a stale graph still
    # carrying the tool must fail closed, never spawn.
    empty_manager = _manager_with(tmp_path / "other", {})
    agent = _TemplateAgent(tmp_path / "data", skill_manager=empty_manager)
    spawned = []
    monkeypatch.setattr(
        tool_factory,
        "_spawn_template_thread",
        lambda *a, **k: spawned.append(1) or "[Spawned]: thread_id=nope",
    )

    set_current_agent(agent)
    try:
        result = tool.func(task="x", config=_CONFIG)
    finally:
        set_current_agent(None)

    assert "no longer declares thread template 'research_helper'" in result
    assert "stale" in result
    assert spawned == []


def test_template_call_fails_closed_on_ownership_lookup_error(
    tmp_path: Path, monkeypatch
):
    """Fail-closed materialization (review fix, #26): if the owned-thread lookup
    itself ERRORS, the tool must not treat that as "not materialized" and spawn
    a duplicate (which would be renamed onto the same callable_name, making name
    routing arbitrary forever). It returns a retryable error and spawns nothing.
    """
    _, agent, tool = _template_fixture(tmp_path)
    spawned: list = []
    monkeypatch.setattr(
        tool_factory,
        "_spawn_template_thread",
        lambda *a, **k: spawned.append(1) or "[Spawned]: thread_id=nope",
    )

    # First lookup point: list_threads_for_user raises.
    def boom_list(user_id):
        raise RuntimeError("db down")

    agent.accounts_repo.list_threads_for_user = boom_list
    set_current_agent(agent)
    try:
        result = tool.func(task="x", config=_CONFIG)
    finally:
        set_current_agent(None)

    assert "ownership lookup failed" in result
    assert "Retry" in result
    assert spawned == []

    # Second lookup point: the by-name callable lookup raises (owned is
    # non-empty so we reach it).
    agent.accounts_repo.list_threads_for_user = lambda user_id: ["some-thread"]

    def boom_by_name(*a, **k):
        raise RuntimeError("index corrupt")

    monkeypatch.setattr(
        agent.thread_config_manager, "get_callable_thread_by_name", boom_by_name
    )
    set_current_agent(agent)
    try:
        result2 = tool.func(task="y", config=_CONFIG)
    finally:
        set_current_agent(None)

    assert "ownership lookup failed" in result2
    assert spawned == []


def test_template_spawn_refusal_passes_through_verbatim(tmp_path: Path, monkeypatch):
    _, agent, tool = _template_fixture(tmp_path)
    refusal = "[Error]: Spawn rate limit reached (5 per hour). Try again later."
    monkeypatch.setattr(
        tool_factory, "_spawn_template_thread", lambda *a, **k: refusal
    )
    invoked: list = []
    monkeypatch.setattr(
        tool_factory,
        "_invoke_materialized",
        lambda *a, **k: invoked.append(1) or "never",
    )

    set_current_agent(agent)
    try:
        result = tool.func(task="x", config=_CONFIG)
    finally:
        set_current_agent(None)

    # Spawn gates (depth/rate caps, role gates, bad kit) surface verbatim;
    # nothing was created and nothing invoked.
    assert result == refusal
    assert invoked == []
    assert agent.owned_threads == []


def test_template_tool_rejects_bad_mode(tmp_path: Path):
    _, agent, tool = _template_fixture(tmp_path)
    set_current_agent(agent)
    try:
        result = tool.func(task="x", mode="broadcast", config=_CONFIG)
    finally:
        set_current_agent(None)
    assert result == "[Error]: mode must be 'ask' or 'handoff'."


# ---------------------------------------------------------------------------
# #26: skill_write / skill_edit validation of thread_templates
# ---------------------------------------------------------------------------


def test_skill_write_rejects_invalid_thread_templates(tmp_path: Path):
    agent = _AuthoringAgent(tmp_path)
    md = """---
name: bad-template-kit
description: Kit with invalid templates.
metadata:
  nymeria:
    thread_templates:
      - name: "helper!"
        description: Bad tool name.
      - name: ghost-runner
        description: Unknown tool.
        tools:
          - no_such_tool_xyz
      - name: ghost-runner
        description: Duplicate name.
---

# Bad Kit
"""
    payload = _skill_write(md, agent)
    assert payload["ok"] is False
    msg = payload["error"]["message"]
    assert "no skill was written" in msg
    assert "thread_templates[0]" in msg
    assert "no_such_tool_xyz" in msg
    assert "duplicate template name" in msg


def test_skill_write_rejects_template_collisions_and_unknown_kit(tmp_path: Path):
    agent = _AuthoringAgent(tmp_path)
    md = """---
name: colliding-template-kit
description: Kit whose template collides and names a missing kit.
metadata:
  nymeria:
    thread_templates:
      - name: bash_execute
        description: Collides with a real tool.
      - name: kit-user
        description: Uses a kit that is not installed.
        kit: not-a-kit
---

# Colliding Kit
"""
    payload = _skill_write(md, agent)
    assert payload["ok"] is False
    msg = payload["error"]["message"]
    assert "collides with an existing tool" in msg
    assert "'not-a-kit' is not installed" in msg


def test_skill_write_template_tools_respect_role_gates(tmp_path: Path):
    agent = _AuthoringAgent(tmp_path, role="user")
    md = """---
name: gated-template-kit
description: Non-admin author declaring an admin-only template tool.
metadata:
  nymeria:
    thread_templates:
      - name: code-helper
        description: Wants claude_code.
        tools:
          - claude_code
---

# Gated Kit
"""
    payload = _skill_write(md, agent)
    assert payload["ok"] is False
    assert "admin-only" in payload["error"]["message"]


def test_skill_write_accepts_valid_thread_templates(tmp_path: Path):
    agent = _AuthoringAgent(tmp_path)
    md = """---
name: authored-template-kit
description: Authored kit with a valid thread template.
metadata:
  nymeria:
    thread_templates:
      - name: docs-helper
        description: Documentation helper thread.
        tools:
          - memory_clear_all
---

# Authored Kit
"""
    payload = _skill_write(md, agent)
    assert payload["ok"] is True
    assert payload["skill"]["thread_templates"] == [
        {"name": "docs_helper", "description": "Documentation helper thread."}
    ]
    assert payload["skill"]["is_skill_kit"] is True
