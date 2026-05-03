"""Tests for Skill Kit tool binding."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from langgraph.types import Command

from nymeria.core.agent import NymeriaAgent, set_current_agent
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager, TemporaryToolEntry
from nymeria.core.tool_reload import TOOL_RELOAD_QUEUED_KEY
from nymeria.skills import load_skill_directory
from nymeria.skills.meta_tool import create_skill_meta_tool
from nymeria.tools.tool_search import bind_tools_for_thread


KIT_MD = """---
name: hello-kit
description: Bind the hello test tool.
metadata:
  nymeria:
    required_tools:
      - hello_test
    tool_ttl: 30m
---

# Hello Kit

Use hello_test.
"""

PLAIN_MD = """---
name: plain-skill
description: Plain skill without tool dependencies.
---

# Plain Skill

No tools required.
"""

BASH_KIT_MD = """---
name: bash-kit
description: Requires an already-bound core tool.
metadata:
  nymeria:
    required_tools:
      - bash_execute
---

# Bash Kit

Use bash_execute only if needed.
"""


class _FakeRegistry:
    def get_tool(self, name: str):
        return None


class _FakeAgent:
    MAX_TOOL_RELOADS_PER_TURN = 1

    def __init__(self, data_dir: Path, role: str = "admin"):
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.tool_registry = _FakeRegistry()
        self._pending_tool_reload = {}
        self._turn_reload_count = {}
        self.accounts_repo = SimpleNamespace(
            get_user_by_id=lambda user_id: SimpleNamespace(role=role)
        )
        self.profile_manager = SimpleNamespace(
            get_profile=lambda user_id: SimpleNamespace(
                tool_preferences=SimpleNamespace(default_thread_tools=None)
            )
        )
        self.invalidated: list[str] = []

    def invalidate_thread_config_cache(self, thread_id: str) -> None:
        self.invalidated.append(thread_id)

    def _resolve_temporary_tools(self, tc):
        return NymeriaAgent._resolve_temporary_tools(self, tc)


def _write_skill(root: Path, name: str, content: str) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(content)
    return d


def test_skill_kit_binding_strict_failure_does_not_mutate_config(tmp_path: Path):
    agent = _FakeAgent(tmp_path)
    set_current_agent(agent)
    try:
        result = bind_tools_for_thread(
            ["hello_test", "missing_tool"],
            "",
            "thread-a",
            "user-a",
            strict=True,
            source="skill_kit",
            skill_name="bad-kit",
        )
    finally:
        set_current_agent(None)

    assert result.ok is False
    assert "no tools were bound" in result.text
    assert agent.thread_config_manager.get_config("thread-a") is None
    assert agent._pending_tool_reload == {}


def test_skill_kit_binding_writes_ttl_and_source_reload_metadata(tmp_path: Path):
    agent = _FakeAgent(tmp_path)
    set_current_agent(agent)
    try:
        result = bind_tools_for_thread(
            ["hello_test"],
            "",
            "thread-a",
            "user-a",
            ttl="30m",
            strict=True,
            source="skill_kit",
            skill_name="hello-kit",
        )
    finally:
        set_current_agent(None)

    assert result.ok is True
    assert result.reload_tools == ["hello_test"]
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert "hello_test" in tc.temporary_tools
    assert agent._pending_tool_reload["thread-a"]["source"] == "skill_kit"
    assert agent._pending_tool_reload["thread-a"]["skill_name"] == "hello-kit"


def test_skill_kit_binding_un_disables_required_tool(tmp_path: Path):
    agent = _FakeAgent(tmp_path)
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="thread-a", disabled_tools=["hello_test"])
    )
    set_current_agent(agent)
    try:
        result = bind_tools_for_thread(
            ["hello_test"],
            "",
            "thread-a",
            "user-a",
            ttl="2h",
            strict=True,
            source="skill_kit",
            skill_name="hello-kit",
        )
    finally:
        set_current_agent(None)

    assert result.ok is True
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert "hello_test" not in tc.disabled_tools
    assert "hello_test" in tc.temporary_tools
    assert result.reload_tools == ["hello_test"]


def test_skill_kit_binding_admin_blocked_is_strict(tmp_path: Path):
    agent = _FakeAgent(tmp_path, role="user")
    set_current_agent(agent)
    try:
        result = bind_tools_for_thread(
            ["reload_all"],
            "",
            "thread-a",
            "user-a",
            strict=True,
            source="skill_kit",
            skill_name="admin-kit",
        )
    finally:
        set_current_agent(None)

    assert result.ok is False
    assert "Admin-only tools" in result.text
    assert agent.thread_config_manager.get_config("thread-a") is None
    assert agent._pending_tool_reload == {}


def test_skill_kit_binding_developer_only_blocked_for_non_admin(tmp_path: Path):
    agent = _FakeAgent(tmp_path, role="user")
    set_current_agent(agent)
    try:
        result = bind_tools_for_thread(
            ["hello_test"],
            "",
            "thread-a",
            "user-a",
            strict=True,
            source="skill_kit",
            skill_name="hello-kit",
        )
    finally:
        set_current_agent(None)

    assert result.ok is False
    assert "Developer-only diagnostic tools" in result.text
    assert agent.thread_config_manager.get_config("thread-a") is None
    assert agent._pending_tool_reload == {}


def test_skill_meta_tool_returns_command_when_skill_kit_queues_reload(tmp_path: Path):
    skill_dir = _write_skill(tmp_path, "hello-kit", KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _FakeAgent(tmp_path / "data")
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool([skill])
        result = skill_tool.func(
            "hello-kit",
            tool_call_id="call-1",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, Command)
    messages = result.update["messages"]
    assert "Hello Kit" in messages[0].content
    assert "Skill Kit reload queued" in messages[0].content
    assert messages[0].additional_kwargs[TOOL_RELOAD_QUEUED_KEY] is True
    assert agent._pending_tool_reload["thread-a"]["source"] == "skill_kit"


def test_skill_meta_tool_plain_skill_returns_body_without_reload(tmp_path: Path):
    skill_dir = _write_skill(tmp_path, "plain-skill", PLAIN_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    skill_tool = create_skill_meta_tool([skill])

    result = skill_tool.func(
        "plain-skill",
        tool_call_id="call-1",
        config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
    )

    assert isinstance(result, str)
    assert "Plain Skill" in result


def test_skill_meta_tool_required_tools_already_bound_returns_body(tmp_path: Path):
    skill_dir = _write_skill(tmp_path, "bash-kit", BASH_KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _FakeAgent(tmp_path / "data")
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool([skill], thread_tool_names=["bash_execute"])
        result = skill_tool.func(
            "bash-kit",
            tool_call_id="call-1",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "Bash Kit" in result
    assert "No binding changes" in result
    assert agent._pending_tool_reload == {}


def test_memory_hash_evicts_expired_temporary_tools(tmp_path: Path):
    agent = object.__new__(NymeriaAgent)
    agent.thread_config_manager = ThreadConfigManager(tmp_path)
    agent.profile_manager = SimpleNamespace(
        get_profile=lambda user_id: SimpleNamespace(
            memories=[],
            personality_overrides={},
            tool_preferences=SimpleNamespace(default_thread_tools=None),
        )
    )
    agent.todo_manager = SimpleNamespace(
        get_todos=lambda user_id: SimpleNamespace(
            get_active_todos_for_thread=lambda thread_id: [],
            get_active_todos=lambda: [],
        )
    )
    agent.skill_manager = None
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="thread-a",
            temporary_tools={
                "hello_test": TemporaryToolEntry(
                    expires_at=datetime.utcnow() - timedelta(seconds=1)
                )
            },
        )
    )

    agent._get_memory_hash("user-a", "thread-a")

    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert tc.temporary_tools == {}
