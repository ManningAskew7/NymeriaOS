"""Tests for Skill Kit tool binding."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from langgraph.types import Command

from nymeria.core.agent import NymeriaAgent, set_current_agent
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager, TemporaryToolEntry
from nymeria.core.tool_reload import TOOL_RELOAD_QUEUED_KEY
from nymeria.skills import load_skill_directory
from nymeria.skills.meta_tool import create_skill_meta_tool
from nymeria.tools.tool_search import bind_tools_for_thread, tool_manage, tool_search


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

MIXED_KIT_MD = """---
name: mixed-kit
description: Requires one existing tool and one newly bound tool.
metadata:
  nymeria:
    required_tools:
      - bash_execute
      - memory_clear_all
    tool_ttl: 30m
---

# Mixed Kit

Use bash_execute and memory_clear_all.
"""

SELF_IMPROVE_REQUIRED_TOOLS = [
    "tool_search",
    "tool_manage",
    "manage_mcp",
    "skill_manage",
    "api_discover",
    "http_request",
    "tool_create",
    "skill_write",
    "skill_edit",
]


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


def test_self_improve_required_tools_bind_for_non_admin(tmp_path: Path):
    agent = _FakeAgent(tmp_path, role="user")
    set_current_agent(agent)
    try:
        result = bind_tools_for_thread(
            SELF_IMPROVE_REQUIRED_TOOLS,
            "",
            "thread-a",
            "user-a",
            ttl="2h",
            strict=True,
            source="skill_kit",
            skill_name="self-improve",
        )
    finally:
        set_current_agent(None)

    assert result.ok is True
    assert result.reload_tools == sorted(SELF_IMPROVE_REQUIRED_TOOLS)
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert set(SELF_IMPROVE_REQUIRED_TOOLS).issubset(tc.temporary_tools)
    assert agent._pending_tool_reload["thread-a"]["source"] == "skill_kit"
    assert agent._pending_tool_reload["thread-a"]["skill_name"] == "self-improve"


def test_tool_search_is_search_only_and_tool_manage_manages_bindings(tmp_path: Path):
    agent = _FakeAgent(tmp_path)
    set_current_agent(agent)
    try:
        search_result = tool_search.func(
            query="browser",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
            top_k=5,
        )
        status_result = tool_manage.func(
            action="status",
            tool_call_id="call-status",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
        enable_result = tool_manage.func(
            action="enable",
            tools=["memory_clear_all"],
            ttl="4w",
            tool_call_id="call-enable",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
    finally:
        set_current_agent(None)

    assert "[Tool Search]" in search_result
    assert "browser_" in search_result
    assert "[Thread Tool Status]" in status_result
    assert isinstance(enable_result, Command)
    assert "memory_clear_all" in agent.thread_config_manager.get_config("thread-a").temporary_tools
    assert agent._pending_tool_reload["thread-a"]["source"] == "tool_manage"


def test_tool_manage_requires_ttl_and_accepts_flexible_ttl(tmp_path: Path):
    agent = _FakeAgent(tmp_path)
    set_current_agent(agent)
    try:
        enable_result = tool_manage.func(
            action="enable",
            tools=["hello_test"],
            tool_call_id="call-enable",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
        enable_with_ttl = tool_manage.func(
            action="enable",
            tools=["hello_test"],
            ttl="4w",
            tool_call_id="call-enable-ttl",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
    finally:
        set_current_agent(None)

    assert isinstance(enable_result, str)
    assert "ttl is required for enable" in enable_result
    assert isinstance(enable_with_ttl, Command)
    assert "hello_test" in agent.thread_config_manager.get_config("thread-a").temporary_tools
    assert agent._pending_tool_reload["thread-a"]["source"] == "tool_manage"


def test_tool_manage_prune_removes_clear_stale_thread_bindings(tmp_path: Path):
    agent = _FakeAgent(tmp_path)
    agent.thread_config_manager.save_config(
        ThreadConfig(
            thread_id="thread-a",
            enabled_tools=["bash_execute", "memory_clear_all", "missing_tool"],
            disabled_tools=["missing_disabled"],
            temporary_tools={
                "hello_test": TemporaryToolEntry(
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
                )
            },
        )
    )

    set_current_agent(agent)
    try:
        result = tool_manage.func(
            action="prune",
            tool_call_id="call-prune",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
    finally:
        set_current_agent(None)

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["changed"] is True
    assert payload["removed"]["enabled_redundant_default"] == ["bash_execute"]
    assert payload["removed"]["enabled_unavailable"] == ["missing_tool"]
    assert payload["removed"]["disabled_unavailable"] == ["missing_disabled"]
    assert payload["removed"]["temporary_expired"] == ["hello_test"]
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert tc.enabled_tools == ["memory_clear_all"]
    assert tc.disabled_tools == []
    assert tc.temporary_tools == {}


def test_tool_manage_prune_removes_recorded_stale_permanent_tool(tmp_path: Path, monkeypatch):
    from nymeria.core import capability_usage
    from nymeria.core.capability_usage import CapabilityUsageStore

    agent = _FakeAgent(tmp_path)
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id="thread-a", enabled_tools=["memory_clear_all"])
    )
    store = CapabilityUsageStore(tmp_path / "capability_usage.json")
    store.record(user_id="user-a", thread_id="thread-a", tools=["memory_clear_all"])
    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["user-a"]["thread-a"]["tools"]["memory_clear_all"]["last_used_at"] = (
        datetime.now(timezone.utc) - timedelta(days=45)
    ).isoformat()
    store.path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(capability_usage, "get_capability_usage_store", lambda: store)

    set_current_agent(agent)
    try:
        result = tool_manage.func(
            action="prune",
            stale_after_days=30,
            min_enabled_age_days=0,
            tool_call_id="call-prune",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
    finally:
        set_current_agent(None)

    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["removed"]["enabled_stale_used_before_cutoff"] == ["memory_clear_all"]
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert tc.enabled_tools == []


def test_mcp_install_binds_discovered_tools_and_queues_reload(tmp_path: Path, monkeypatch):
    from nymeria.core import mcp_auth_bridge, mcp_runtime, mcp_servers
    from nymeria.core.mcp_runtime import MCPInstallPlan
    from nymeria.tools.definitions.mcp_schema import MCPDiscoveredTool, MCPServerDefinition
    from nymeria.tools.metadata import clear_mcp_server_tool_metadata, register_mcp_server_tool_metadata
    from nymeria.tools.search_mcp import _install_mcp_server_impl

    clear_mcp_server_tool_metadata()

    defn = MCPServerDefinition(
        id="demo",
        name="Demo",
        transport="http",
        url="http://example.test/mcp",
    )
    plan = MCPInstallPlan(
        source_type="http",
        runtime_type="http",
        risk_level="low",
        confirmation_required=False,
        parsed_summary="Demo HTTP MCP server",
    )
    discovered = [MCPDiscoveredTool(name="lookup", description="Lookup data.")]

    class FakeMCPRegistry:
        def __init__(self):
            self.saved = []

        def get_server(self, server_id):
            return None

        def save_server(self, server_def):
            self.saved.append(server_def)

        def discover_tools(self, server_id):
            defn.discovered_tools = discovered
            return discovered

    registry = FakeMCPRegistry()
    live_tools = {}

    class FakeRegistry:
        def get_tool(self, name: str):
            return live_tools.get(name)

    agent = _FakeAgent(tmp_path)
    agent.tool_registry = FakeRegistry()

    def reload_mcp_server_tools():
        tool_name = "mcp__demo__lookup"
        register_mcp_server_tool_metadata(tool_name, "Lookup data.")
        live_tools[tool_name] = SimpleNamespace(name=tool_name, description="Lookup data.")
        return [tool_name]

    agent.reload_mcp_server_tools = reload_mcp_server_tools

    monkeypatch.setattr(mcp_runtime, "plan_text_source", lambda source, name=None: (defn, plan))
    monkeypatch.setattr(
        mcp_runtime,
        "prepare_runtime",
        lambda server_def, install_plan, **kwargs: (
            server_def,
            kwargs.get("log_sink") or [],
        ),
    )
    monkeypatch.setattr(
        mcp_auth_bridge,
        "apply_mcp_auth_presets",
        lambda server_def, user_id, log_sink: server_def,
    )
    monkeypatch.setattr(mcp_servers, "get_mcp_server_registry", lambda: registry)

    set_current_agent(agent)
    try:
        result = _install_mcp_server_impl(
            source="http://example.test/mcp",
            ttl="2h",
            tool_call_id="call-mcp",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
    finally:
        set_current_agent(None)
        clear_mcp_server_tool_metadata()

    assert isinstance(result, Command)
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert "mcp__demo__lookup" in tc.temporary_tools
    assert agent._pending_tool_reload["thread-a"]["source"] == "mcp_install"
    assert agent._pending_tool_reload["thread-a"]["new_tools"] == ["mcp__demo__lookup"]


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
    # bash_execute is part of the thread's default-bound set, so delegating to
    # bind_tools_for_thread classifies it as an already-bound no-op. The skip
    # decision comes from the REAL thread config, not the thread_tool_names
    # snapshot (which is the executor superset in dynamic mode).
    assert "Already bound (default set, no change): bash_execute" in result
    assert agent._pending_tool_reload == {}


def test_skill_meta_tool_reports_added_and_already_bound_required_tools(tmp_path: Path):
    skill_dir = _write_skill(tmp_path, "mixed-kit", MIXED_KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _FakeAgent(tmp_path / "data")
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool([skill], thread_tool_names=["bash_execute"])
        result = skill_tool.func(
            "mixed-kit",
            tool_call_id="call-1",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, Command)
    messages = result.update["messages"]
    content = messages[0].content
    # The full required set is delegated to bind_tools_for_thread, which binds
    # the genuinely-missing tool (memory_clear_all) and reports the already
    # default-bound one (bash_execute) as a no-op.
    assert "Newly loaded: memory_clear_all" in content
    assert "Already bound (default set, no change): bash_execute" in content
    assert agent._pending_tool_reload["thread-a"]["new_tools"] == ["memory_clear_all"]


def test_skill_meta_tool_binds_tool_present_only_in_superset_snapshot(tmp_path: Path):
    """Regression: in dynamic-binding mode the meta-tool's thread_tool_names is
    the executor superset (every registered tool), NOT the thread's bound set.
    A required tool that is absent from the thread must still be bound even when
    it appears in that snapshot — the snapshot must never gate binding.
    """
    skill_dir = _write_skill(tmp_path, "mixed-kit", MIXED_KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _FakeAgent(tmp_path / "data")
    set_current_agent(agent)
    try:
        # Simulate the superset: BOTH required tools appear in the snapshot,
        # even though memory_clear_all is not actually bound on the thread.
        skill_tool = create_skill_meta_tool(
            [skill], thread_tool_names=["bash_execute", "memory_clear_all"]
        )
        result = skill_tool.func(
            "mixed-kit",
            tool_call_id="call-1",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
    finally:
        set_current_agent(None)

    # Before the fix this returned a plain "already bound" string and bound
    # nothing. Now memory_clear_all is bound and a reload is queued.
    assert isinstance(result, Command)
    assert agent._pending_tool_reload["thread-a"]["new_tools"] == ["memory_clear_all"]
    tc = agent.thread_config_manager.get_config("thread-a")
    assert "memory_clear_all" in tc.temporary_tools


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
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
                )
            },
        )
    )

    agent._get_memory_hash("user-a", "thread-a")

    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert tc.temporary_tools == {}
