"""Tests for Skill Kit tool binding."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from langgraph.types import Command

from nymeria.core.agent import NymeriaAgent, set_current_agent
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager, TemporaryToolEntry
from nymeria.core.tool_reload import TOOL_RELOAD_QUEUED_KEY
from nymeria.skills import SkillManager, load_skill_directory
from nymeria.skills.meta_tool import (
    _SkillKitOutcome,
    _allowed_tools_advisory,
    _bind_skill_kit_tools,
    _resolve_active_skill,
    _resolve_effective_ttl,
    _skill_kit_reload_notice,
    create_skill_meta_tool,
)
from nymeria.tools.tool_search import (
    ToolBindingResult,
    bind_tools_for_thread,
    tool_manage,
    tool_search,
)

# The real submodule, used as the monkeypatch target: the package re-exports a
# `tool_search` StructuredTool that shadows the `nymeria.tools.tool_search`
# attribute, so only sys.modules resolves the module the helper imports from.
tool_search_module = sys.modules["nymeria.tools.tool_search"]


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

TOOL_MANAGEMENT_REQUIRED_TOOLS = [
    "tool_search",
    "tool_manage",
    "tool_create",
    "api_discover",
    "http_request",
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


def test_tool_management_required_tools_bind_for_non_admin(tmp_path: Path):
    agent = _FakeAgent(tmp_path, role="user")
    set_current_agent(agent)
    try:
        result = bind_tools_for_thread(
            TOOL_MANAGEMENT_REQUIRED_TOOLS,
            "",
            "thread-a",
            "user-a",
            ttl="2h",
            strict=True,
            source="skill_kit",
            skill_name="tool-management",
        )
    finally:
        set_current_agent(None)

    assert result.ok is True
    assert result.reload_tools == sorted(TOOL_MANAGEMENT_REQUIRED_TOOLS)
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert set(TOOL_MANAGEMENT_REQUIRED_TOOLS).issubset(tc.temporary_tools)
    assert agent._pending_tool_reload["thread-a"]["source"] == "skill_kit"
    assert agent._pending_tool_reload["thread-a"]["skill_name"] == "tool-management"


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


def test_mcp_install_agent_cannot_self_confirm_high_risk(tmp_path: Path, monkeypatch):
    """A prompt-injected admin thread must not self-approve a high-risk install.

    `confirmed`/`confirmed_risk_ids` are LLM-supplied tool args; for a
    confirmation-required plan the agent tool returns the plan without running
    it no matter what they are set to. A human confirms out of band via the
    REST/UI path instead.
    """
    from nymeria.core import mcp_runtime, mcp_servers
    from nymeria.core.mcp_runtime import MCPInstallPlan
    from nymeria.tools.definitions.mcp_schema import MCPServerDefinition
    from nymeria.tools.search_mcp import _install_mcp_server_impl

    defn = MCPServerDefinition(id="evil", name="Evil", server_command="npx")
    plan = MCPInstallPlan(
        source_type="git",
        runtime_type="git",
        risk_level="high",
        confirmation_required=True,
        parsed_summary="Git-cloned MCP server",
        risk_signals=[{"id": "git_source", "requires_confirmation": True}],
    )

    class FakeMCPRegistry:
        def __init__(self):
            self.saved = []
            self.discover_calls = []

        def get_server(self, server_id):
            return None

        def save_server(self, server_def):
            self.saved.append(server_def)

        def discover_tools(self, server_id):
            self.discover_calls.append(server_id)
            return []

    registry = FakeMCPRegistry()
    monkeypatch.setattr(mcp_runtime, "plan_text_source", lambda source, name=None: (defn, plan))
    monkeypatch.setattr(mcp_servers, "get_mcp_server_registry", lambda: registry)

    agent = _FakeAgent(tmp_path, role="admin")
    set_current_agent(agent)
    try:
        # Even with confirmed=True and every risk id acknowledged, the agent
        # cannot run a high-risk install.
        result = _install_mcp_server_impl(
            source="https://github.com/example/evil-mcp-server",
            confirmed=True,
            confirmed_risk_ids=["git_source"],
            tool_call_id="call-mcp",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["requires_confirmation"] is True
    assert payload["server_id"] == "evil"
    # Nothing was persisted and no subprocess was launched.
    assert registry.saved == []
    assert registry.discover_calls == []


def test_mcp_retry_agent_cannot_self_confirm_high_risk(tmp_path: Path, monkeypatch):
    """The retry action mirrors install: no agent self-approval of a high-risk plan.

    A high-risk draft persisted by a human's prior confirmed install must not be
    re-fired by a prompt-injected admin thread via `manage_mcp(action="retry",
    confirmed=True)`. The gate ignores the model-supplied confirmation.
    """
    from nymeria.core import mcp_servers
    from nymeria.tools.definitions.mcp_schema import MCPServerDefinition
    from nymeria.tools.search_mcp import _mcp_retry

    high_risk_plan = {
        "source_type": "git",
        "runtime_type": "git",
        "risk_level": "high",
        "confirmation_required": True,
        "parsed_summary": "Git-cloned MCP server",
        "risk_signals": [{"id": "git_source", "requires_confirmation": True}],
    }
    server = MCPServerDefinition(
        id="risky",
        name="Risky",
        server_command="npx",
        install_status="failed",
        install_plan=high_risk_plan,
    )

    prepared = []

    class FakeMCPRegistry:
        def get_server(self, server_id):
            return server if server_id == "risky" else None

        def get_all_servers(self):
            return [server]

        def save_server(self, server_def):
            prepared.append(server_def)

    monkeypatch.setattr(mcp_servers, "get_mcp_server_registry", lambda: FakeMCPRegistry())
    # prepare_runtime would launch the process; assert it is never reached.
    import nymeria.core.mcp_runtime as mcp_runtime

    def _boom(*args, **kwargs):
        raise AssertionError("prepare_runtime must not run for a high-risk retry")

    monkeypatch.setattr(mcp_runtime, "prepare_runtime", _boom)

    result = _mcp_retry(
        "risky",
        confirmed=True,
        confirmed_risk_ids=["git_source"],
        config_values={},
        user_id="admin-1",
    )
    payload = json.loads(result)
    assert payload["requires_confirmation"] is True
    assert payload["server_id"] == "risky"
    assert prepared == []  # never persisted "preparing" / launched


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


def test_skill_meta_tool_custom_ttl_overrides_kit_default(tmp_path: Path):
    """The agent can pass ttl to override a kit's declared tool_ttl, and the
    override applies to the bound tools' expiry."""
    skill_dir = _write_skill(tmp_path, "hello-kit", KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    assert skill.tool_ttl == "30m"  # kit default
    agent = _FakeAgent(tmp_path / "data")
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool([skill])
        skill_tool.func(
            "hello-kit",
            ttl="4w",
            tool_call_id="call-1",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
    finally:
        set_current_agent(None)

    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert "hello_test" in tc.temporary_tools
    expires_at = tc.temporary_tools["hello_test"].expires_at
    assert expires_at is not None
    # The 4w (~28d) override must win over the kit's 30m default.
    assert expires_at > datetime.now(timezone.utc) + timedelta(days=7)


def test_skill_meta_tool_invalid_ttl_falls_back_to_kit_default(tmp_path: Path):
    """An invalid agent-supplied ttl does not block activation: the kit's
    tools bind at the default TTL and the result notes the fallback."""
    skill_dir = _write_skill(tmp_path, "hello-kit", KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _FakeAgent(tmp_path / "data")
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool([skill])
        result = skill_tool.func(
            "hello-kit",
            ttl="banana",
            tool_call_id="call-1",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
    finally:
        set_current_agent(None)

    content = (
        result.update["messages"][0].content
        if isinstance(result, Command)
        else result
    )
    assert "Ignored ttl='banana'" in content
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert "hello_test" in tc.temporary_tools
    expires_at = tc.temporary_tools["hello_test"].expires_at
    assert expires_at is not None
    # Fell back to the kit's 30m default, so it expires within the hour.
    assert expires_at < datetime.now(timezone.utc) + timedelta(hours=1)


def test_skill_meta_tool_ttl_on_plain_skill_reports_no_effect(tmp_path: Path):
    """ttl on a skill that binds no tools is a no-op and is flagged as such;
    the body is still returned so the skill activates normally."""
    skill_dir = _write_skill(tmp_path, "plain-skill", PLAIN_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    skill_tool = create_skill_meta_tool([skill])

    result = skill_tool.func(
        "plain-skill",
        ttl="2h",
        tool_call_id="call-1",
        config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
    )

    assert isinstance(result, str)
    assert "Plain Skill" in result
    assert "no Skill Kit" in result
    assert "no effect" in result


def test_skill_meta_tool_defer_binds_nothing_and_lists_schemas(tmp_path: Path):
    """defer=true loads the kit body plus its tools' schemas without binding."""
    skill_dir = _write_skill(tmp_path, "hello-kit", KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _FakeAgent(tmp_path / "data")
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool([skill])
        result = skill_tool.func(
            "hello-kit",
            defer=True,
            tool_call_id="call-1",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "Hello Kit" in result
    assert "Skill Kit deferred" in result
    assert "tool_invoke" in result
    assert "hello_test args:" in result
    # Nothing bound: no config written, no reload queued.
    assert agent.thread_config_manager.get_config("thread-a") is None
    assert agent._pending_tool_reload == {}


def test_skill_meta_tool_defer_ignores_ttl(tmp_path: Path):
    skill_dir = _write_skill(tmp_path, "hello-kit", KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _FakeAgent(tmp_path / "data")
    set_current_agent(agent)
    try:
        skill_tool = create_skill_meta_tool([skill])
        result = skill_tool.func(
            "hello-kit",
            ttl="2h",
            defer=True,
            tool_call_id="call-1",
            config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        )
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "ttl was ignored" in result
    assert agent.thread_config_manager.get_config("thread-a") is None


def test_skill_meta_tool_defer_plain_skill_is_noop(tmp_path: Path):
    skill_dir = _write_skill(tmp_path, "plain-skill", PLAIN_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    skill_tool = create_skill_meta_tool([skill])

    result = skill_tool.func(
        "plain-skill",
        defer=True,
        tool_call_id="call-1",
        config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
    )

    assert isinstance(result, str)
    assert "Plain Skill" in result
    assert "binds no Skill Kit tools" in result


# ---------------------------------------------------------------------------
# Deferred executor guarantee (backlog #170): defer must never point the model
# at a tool_invoke the thread cannot call.
# ---------------------------------------------------------------------------

TEMPLATE_ONLY_KIT_MD = """---
name: template-kit
description: Declares a thread template and no required tools.
metadata:
  nymeria:
    thread_templates:
      - name: helper-thread
        description: A helper thread.
---

# Template Kit

Use the helper thread.
"""


def _defer_agent(
    tmp_path: Path,
    *,
    default_tools=None,
    dynamic=None,
    allow_unbound: bool = False,
) -> _FakeAgent:
    """Agent for defer tests: curated defaults and/or a settings namespace.

    ``default_tools=["bash_execute"]`` reproduces the #164 shape (a profile
    whose curated default_thread_tools predates tool_invoke). ``dynamic=None``
    leaves ``settings`` absent, which should_emit_reload_command treats as the
    legacy rebuild path.
    """
    agent = _FakeAgent(tmp_path / "data")
    if default_tools is not None:
        agent.profile_manager = SimpleNamespace(
            get_profile=lambda user_id: SimpleNamespace(
                tool_preferences=SimpleNamespace(default_thread_tools=default_tools)
            )
        )
    if dynamic is not None:
        agent.settings = SimpleNamespace(
            dynamic_tool_binding=dynamic, allow_unbound_tool_calls=allow_unbound
        )
    return agent


def _defer_call(skill_name: str, skill, ttl=None):
    skill_tool = create_skill_meta_tool([skill])
    kwargs = {"defer": True}
    if ttl is not None:
        kwargs["ttl"] = ttl
    return skill_tool.func(
        skill_name,
        tool_call_id="call-1",
        config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
        **kwargs,
    )


def test_defer_autobinds_tool_invoke_when_thread_lacks_it(tmp_path: Path):
    """#170 behavior 1: unreachable tool_invoke gets a 7-day TTL bind, announced."""
    skill_dir = _write_skill(tmp_path, "hello-kit", KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _defer_agent(tmp_path, default_tools=["bash_execute"], dynamic=True)
    set_current_agent(agent)
    try:
        result = _defer_call("hello-kit", skill)
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "hello_test args:" in result
    assert "tool_invoke auto-bound" in result
    assert "7d" in result
    assert "callable from your next step" in result
    # The header must not contradict the auto-bind note (review F4): the
    # universal truth is that none of the KIT's tools were bound, and the
    # cache-safe framing is dropped because the tools prefix did change.
    assert "None of this kit's tools were bound" in result
    assert "cache-safe" not in result
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None
    assert "tool_invoke" in tc.temporary_tools
    expires = tc.temporary_tools["tool_invoke"].expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    remaining = expires - datetime.now(timezone.utc)
    assert timedelta(days=6, hours=23) < remaining <= timedelta(days=7)
    # ONLY the executor was bound: the kit's own tools stayed deferred.
    assert "hello_test" not in (tc.enabled_tools or [])
    assert "hello_test" not in tc.temporary_tools
    # Dynamic mode: no legacy reload queued.
    assert agent._pending_tool_reload == {}
    # The repeat-use steer is the kit-level one, not the tool_manage detour.
    assert 'Skill(name="hello-kit", ttl=...)' in result
    assert 'tool_manage(action="enable")' not in result


def test_defer_no_bind_when_tool_invoke_has_live_ttl(tmp_path: Path):
    """#170 behavior 2: a live TTL entry counts as reachable; nothing changes."""
    skill_dir = _write_skill(tmp_path, "hello-kit", KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _defer_agent(tmp_path, default_tools=["bash_execute"], dynamic=True)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    tc = ThreadConfig(thread_id="thread-a")
    tc.temporary_tools = {"tool_invoke": TemporaryToolEntry(expires_at=future)}
    agent.thread_config_manager.save_config(tc)
    set_current_agent(agent)
    try:
        result = _defer_call("hello-kit", skill)
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "auto-bound" not in result
    entry = agent.thread_config_manager.get_config("thread-a").temporary_tools[
        "tool_invoke"
    ]
    expires = entry.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    assert abs((expires - future).total_seconds()) < 1


def test_defer_disabled_tool_invoke_stays_disabled(tmp_path: Path):
    """#170 behavior 3: an explicit disable is never silently reversed."""
    skill_dir = _write_skill(tmp_path, "hello-kit", KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _FakeAgent(tmp_path / "data")  # seed defaults carry tool_invoke
    tc = ThreadConfig(thread_id="thread-a")
    tc.disabled_tools = ["tool_invoke"]
    agent.thread_config_manager.save_config(tc)
    set_current_agent(agent)
    try:
        result = _defer_call("hello-kit", skill)
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "tool_invoke disabled on this thread" in result
    assert "NOT" in result
    assert 'Skill(name="hello-kit", ttl=...)' in result
    after = agent.thread_config_manager.get_config("thread-a")
    assert after.disabled_tools == ["tool_invoke"]
    assert "tool_invoke" not in (after.temporary_tools or {})
    assert "tool_invoke" not in (after.enabled_tools or [])


def test_defer_direct_mode_instructs_direct_calls_binds_nothing(tmp_path: Path):
    """#170 behavior 4: permissive dynamic mode strips tool_invoke, so the
    deferred instructions must point at direct calls and no bind may fire."""
    skill_dir = _write_skill(tmp_path, "hello-kit", KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _defer_agent(
        tmp_path, default_tools=["bash_execute"], dynamic=True, allow_unbound=True
    )
    set_current_agent(agent)
    try:
        result = _defer_call("hello-kit", skill)
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "DIRECTLY by name" in result
    assert "tool_invoke" not in result
    assert agent.thread_config_manager.get_config("thread-a") is None


def test_defer_templates_only_kit_autobinds_executor(tmp_path: Path):
    """#170 behavior 5: thread templates dispatch through tool_invoke too."""
    skill_dir = _write_skill(tmp_path, "template-kit", TEMPLATE_ONLY_KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    assert skill.thread_templates and not skill.required_tools
    agent = _defer_agent(tmp_path, default_tools=["bash_execute"], dynamic=True)
    set_current_agent(agent)
    try:
        result = _defer_call("template-kit", skill)
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "Thread templates (deferred)" in result
    assert "tool_invoke auto-bound" in result
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None and "tool_invoke" in tc.temporary_tools


def test_defer_plain_skill_no_autobind_without_tool_invoke(tmp_path: Path):
    """#170 behavior 6: nothing points at tool_invoke, so nothing binds."""
    skill_dir = _write_skill(tmp_path, "plain-skill", PLAIN_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _defer_agent(tmp_path, default_tools=["bash_execute"], dynamic=True)
    set_current_agent(agent)
    try:
        result = _defer_call("plain-skill", skill)
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "auto-bound" not in result
    assert agent.thread_config_manager.get_config("thread-a") is None


def test_defer_autobind_legacy_mode_emits_reload_command(tmp_path: Path):
    """#170 behavior 7: legacy rebuild mode wraps the body in the reload
    Command with the STOP notice; the bind is persisted and queued."""
    skill_dir = _write_skill(tmp_path, "hello-kit", KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _defer_agent(tmp_path, default_tools=["bash_execute"])  # no settings
    set_current_agent(agent)
    try:
        result = _defer_call("hello-kit", skill)
    finally:
        set_current_agent(None)

    assert isinstance(result, Command)
    message = result.update["messages"][0]
    assert message.additional_kwargs.get(TOOL_RELOAD_QUEUED_KEY) is True
    assert "tool_invoke auto-bound" in message.content
    assert "STOP NOW" in message.content
    assert "hello_test args:" in message.content
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None and "tool_invoke" in tc.temporary_tools
    pending = agent._pending_tool_reload["thread-a"]
    assert pending["new_tools"] == ["tool_invoke"]
    assert pending["ttl"] == "7d"
    assert pending["source"] == "skill_kit_defer"


def test_defer_autobind_failure_keeps_activation(tmp_path: Path, monkeypatch):
    """#170 behavior 8: a bind failure never fails the activation; the body
    still returns with an honest steer note."""
    skill_dir = _write_skill(tmp_path, "hello-kit", KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _defer_agent(tmp_path, default_tools=["bash_execute"], dynamic=True)
    monkeypatch.setattr(
        tool_search_module,
        "bind_tools_for_thread",
        lambda *a, **k: ToolBindingResult(ok=False, text="[Error]: nope"),
    )
    set_current_agent(agent)
    try:
        result = _defer_call("hello-kit", skill)
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "hello_test args:" in result
    assert "auto-binding" in result and "failed" in result
    assert "[Error]: nope" in result
    assert 'Skill(name="hello-kit", ttl=...)' in result
    assert agent.thread_config_manager.get_config("thread-a") is None


def test_defer_no_bind_when_tool_invoke_explicitly_enabled(tmp_path: Path):
    """#170 behavior 2, enabled arm: an enabled_tools entry counts as
    reachable even when the curated defaults lack the tool."""
    skill_dir = _write_skill(tmp_path, "hello-kit", KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _defer_agent(tmp_path, default_tools=["bash_execute"], dynamic=True)
    tc = ThreadConfig(thread_id="thread-a")
    tc.enabled_tools = ["tool_invoke"]
    agent.thread_config_manager.save_config(tc)
    set_current_agent(agent)
    try:
        result = _defer_call("hello-kit", skill)
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "auto-bound" not in result
    after = agent.thread_config_manager.get_config("thread-a")
    assert after.enabled_tools == ["tool_invoke"]
    assert "tool_invoke" not in (after.temporary_tools or {})


def test_defer_autobind_cap_hit_tells_the_truth(tmp_path: Path):
    """#170 behavior 7, cap-hit arm: the bind persists but the result is plain
    text saying the executor is callable only next user turn; no reload
    Command is emitted and nothing is queued (the cap guarantees no rebuild
    happens this turn)."""
    skill_dir = _write_skill(tmp_path, "hello-kit", KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _defer_agent(tmp_path, default_tools=["bash_execute"])  # legacy
    agent._turn_reload_count = {"thread-a": 1}  # cap (1) already consumed
    set_current_agent(agent)
    try:
        result = _defer_call("hello-kit", skill)
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "tool_invoke auto-bound" in result
    assert "next user turn" in result
    assert "STOP NOW" not in result
    tc = agent.thread_config_manager.get_config("thread-a")
    assert tc is not None and "tool_invoke" in tc.temporary_tools
    assert agent._pending_tool_reload == {}


def test_defer_direct_mode_templates_block_instructs_direct_calls(tmp_path: Path):
    """#170 behavior 4, templates arm: in permissive dynamic mode the
    thread-templates block also instructs direct calls, and nothing binds."""
    skill_dir = _write_skill(tmp_path, "template-kit", TEMPLATE_ONLY_KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _defer_agent(
        tmp_path, default_tools=["bash_execute"], dynamic=True, allow_unbound=True
    )
    set_current_agent(agent)
    try:
        result = _defer_call("template-kit", skill)
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "Thread templates (deferred)" in result
    assert "run one directly by name" in result
    assert "tool_invoke" not in result
    assert agent.thread_config_manager.get_config("thread-a") is None


def test_defer_autobind_after_expired_ttl_entry(tmp_path: Path):
    """#170 behavior 9: an expired TTL entry counts as unreachable."""
    skill_dir = _write_skill(tmp_path, "hello-kit", KIT_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    agent = _defer_agent(tmp_path, default_tools=["bash_execute"], dynamic=True)
    tc = ThreadConfig(thread_id="thread-a")
    tc.temporary_tools = {
        "tool_invoke": TemporaryToolEntry(
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=5)
        )
    }
    agent.thread_config_manager.save_config(tc)
    set_current_agent(agent)
    try:
        result = _defer_call("hello-kit", skill)
    finally:
        set_current_agent(None)

    assert isinstance(result, str)
    assert "tool_invoke auto-bound" in result
    entry = agent.thread_config_manager.get_config("thread-a").temporary_tools[
        "tool_invoke"
    ]
    expires = entry.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    assert expires - datetime.now(timezone.utc) > timedelta(days=6)


def test_skill_meta_tool_not_found_copy_points_to_install(tmp_path: Path):
    skill_dir = _write_skill(tmp_path, "plain-skill", PLAIN_MD)
    skill = load_skill_directory(skill_dir, scope="bundled")
    assert skill is not None
    skill_tool = create_skill_meta_tool([skill])

    result = skill_tool.func(
        "no-such-skill",
        tool_call_id="call-1",
        config={"configurable": {"thread_id": "thread-a", "user_id": "user-a"}},
    )

    assert isinstance(result, str)
    assert "No installed skill named" in result
    assert "install_skill" in result


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


def _bundled_skills_dir() -> Path:
    import nymeria

    return Path(nymeria.__file__).resolve().parent / "skills_bundled"


def test_bundled_capability_kits_expose_exact_required_tools():
    """The shipped capability kits must declare the exact tools they bind.

    Binding is strict, so a typo'd or missing name would fail kit activation at
    runtime. This pins the contract the self-improve split established.
    """
    expected = {
        "tool-management": [
            "tool_search", "tool_manage", "tool_create", "api_discover", "http_request",
        ],
        "skill-management": ["skill_manage", "skill_write", "skill_edit"],
        "mcp-management": ["manage_mcp"],
        "credential-management": [
            "auth_inspect", "auth_cleanup", "auth_bindings", "auth_test", "auth_write",
            "request_credential",
        ],
        "workflow-authoring": ["tool_create", "workflow_info"],
        "cli-customization": ["cli_statusbar_get", "cli_statusbar_set"],
    }
    bundled = _bundled_skills_dir()
    for name, tools in expected.items():
        skill = load_skill_directory(bundled / name, "bundled")
        assert skill is not None, f"missing bundled kit {name}"
        assert skill.is_skill_kit is True
        assert skill.required_tools == tools
        assert skill.tool_ttl == "2h"


def test_self_improve_is_text_only_guidance_skill():
    """After the split, self-improve binds no tools (it routes to the kits)."""
    skill = load_skill_directory(_bundled_skills_dir() / "self-improve", "bundled")
    assert skill is not None
    assert skill.required_tools == []
    assert skill.is_skill_kit is False


# ---------------------------------------------------------------------------
# Slice 27 F3: direct unit tests for the extracted skill_meta_tool helpers.
# The eight end-to-end test_skill_meta_tool_* tests above lock the integrated
# behavior; these pin the new module-level seams in isolation.
# ---------------------------------------------------------------------------

ADVISORY_MD = """---
name: advisory-skill
description: Skill with portable allowed-tools metadata.
allowed-tools:
  - bash_execute
  - memory_clear_all(args)
---

# Advisory Skill

Uses some tools.
"""


def _load(tmp_path: Path, name: str, md: str):
    skill = load_skill_directory(_write_skill(tmp_path, name, md), scope="bundled")
    assert skill is not None
    return skill


# ----- _resolve_effective_ttl -----

def test_resolve_effective_ttl_kit_default_when_no_override(tmp_path: Path):
    kit = _load(tmp_path, "hello-kit", KIT_MD)
    assert kit.tool_ttl == "30m"
    effective, notice = _resolve_effective_ttl(kit, None)
    assert effective == "30m"
    assert notice == ""


def test_resolve_effective_ttl_valid_override_wins(tmp_path: Path):
    kit = _load(tmp_path, "hello-kit", KIT_MD)
    effective, notice = _resolve_effective_ttl(kit, "4w")
    assert effective == "4w"
    assert notice == ""


def test_resolve_effective_ttl_no_tools_reports_no_effect(tmp_path: Path):
    plain = _load(tmp_path, "plain-skill", PLAIN_MD)
    effective, notice = _resolve_effective_ttl(plain, "2h")
    # No required_tools -> the kit default is kept and the model is told the
    # ttl had no effect.
    assert effective == plain.tool_ttl
    assert "no effect" in notice


def test_resolve_effective_ttl_invalid_falls_back_to_default(tmp_path: Path):
    kit = _load(tmp_path, "hello-kit", KIT_MD)
    effective, notice = _resolve_effective_ttl(kit, "banana")
    assert effective == "30m"
    assert "Ignored ttl='banana'" in notice


# ----- _allowed_tools_advisory -----

def test_allowed_tools_advisory_empty_without_thread_snapshot(tmp_path: Path):
    skill = _load(tmp_path, "advisory-skill", ADVISORY_MD)
    assert _allowed_tools_advisory(skill, set()) == ""


def test_allowed_tools_advisory_empty_without_allowed_tools(tmp_path: Path):
    plain = _load(tmp_path, "plain-skill", PLAIN_MD)
    assert plain.allowed_tools == []
    assert _allowed_tools_advisory(plain, {"bash_execute"}) == ""


def test_allowed_tools_advisory_empty_when_nothing_missing(tmp_path: Path, monkeypatch):
    skill = _load(tmp_path, "advisory-skill", ADVISORY_MD)
    # Declared tools are NOT known Nymeria tools -> no advisory (portable names
    # like Read/Write must not warn).
    monkeypatch.setattr(
        "nymeria.skills.meta_tool._known_nymeria_tool_names", lambda: set()
    )
    assert _allowed_tools_advisory(skill, {"bash_execute"}) == ""


def test_allowed_tools_advisory_warns_on_known_missing_tool(tmp_path: Path, monkeypatch):
    skill = _load(tmp_path, "advisory-skill", ADVISORY_MD)
    monkeypatch.setattr(
        "nymeria.skills.meta_tool._known_nymeria_tool_names",
        lambda: {"bash_execute", "memory_clear_all"},
    )
    # bash_execute is on the thread; memory_clear_all is a known Nymeria tool
    # declared by the skill but absent from the thread -> it must be flagged.
    notice = _allowed_tools_advisory(skill, {"bash_execute"})
    assert "NOT enabled on the current thread" in notice
    assert "memory_clear_all" in notice
    assert "bash_execute" not in notice
    assert notice.startswith("\n\n---\n")


# ----- _resolve_active_skill -----

def test_resolve_active_skill_snapshot_hit_without_manager(tmp_path: Path):
    skill = _load(tmp_path, "plain-skill", PLAIN_MD)
    resolved = _resolve_active_skill(
        "plain-skill", ["plain-skill"], {"plain-skill": skill}, None, None
    )
    assert resolved is skill


def test_resolve_active_skill_prefers_manager_over_snapshot(tmp_path: Path):
    snapshot_skill = _load(tmp_path, "plain-skill", PLAIN_MD)
    live_skill = _load(tmp_path, "plain-skill-live", PLAIN_MD)
    manager = cast(SkillManager, SimpleNamespace(get=lambda name, user_id=None: live_skill))
    resolved = _resolve_active_skill(
        "plain-skill", ["plain-skill"], {"plain-skill": snapshot_skill}, manager, "u"
    )
    assert resolved is live_skill


def test_resolve_active_skill_falls_back_to_snapshot_when_manager_misses(tmp_path: Path):
    snapshot_skill = _load(tmp_path, "plain-skill", PLAIN_MD)
    manager = cast(SkillManager, SimpleNamespace(get=lambda name, user_id=None: None))
    resolved = _resolve_active_skill(
        "plain-skill", ["plain-skill"], {"plain-skill": snapshot_skill}, manager, "u"
    )
    assert resolved is snapshot_skill


def test_resolve_active_skill_dynamic_post_reload(tmp_path: Path):
    # Name absent from the graph-build snapshot but present in the manager
    # (created/enabled since the graph was built).
    live_skill = _load(tmp_path, "fresh-skill", PLAIN_MD)
    manager = cast(SkillManager, SimpleNamespace(get=lambda name, user_id=None: live_skill))
    resolved = _resolve_active_skill("fresh-skill", [], {}, manager, "u")
    assert resolved is live_skill


def test_resolve_active_skill_unknown_returns_none(tmp_path: Path):
    assert _resolve_active_skill("nope", [], {}, None, None) is None


# ----- _skill_kit_reload_notice -----

def test_skill_kit_reload_notice_emits_command_when_legacy_reload(monkeypatch):
    binding = cast(ToolBindingResult, SimpleNamespace(reload_tools=["hello_test"]))
    monkeypatch.setattr(
        "nymeria.skills.meta_tool.should_emit_reload_command",
        lambda reload_tools, *, thread_id: True,
    )
    notice, emit_command = _skill_kit_reload_notice(
        binding, {"configurable": {"thread_id": "thread-a"}}
    )
    assert emit_command is True
    assert "[Skill Kit reload queued - STOP NOW]" in notice


def test_skill_kit_reload_notice_dynamic_when_no_legacy_reload(monkeypatch):
    binding = cast(ToolBindingResult, SimpleNamespace(reload_tools=["hello_test"]))
    monkeypatch.setattr(
        "nymeria.skills.meta_tool.should_emit_reload_command",
        lambda reload_tools, *, thread_id: False,
    )
    notice, emit_command = _skill_kit_reload_notice(
        binding, {"configurable": {"thread_id": "thread-a"}}
    )
    assert emit_command is False
    assert "callable on the next model step" in notice


# ----- _bind_skill_kit_tools -----

def test_bind_skill_kit_tools_no_required_tools_skips_bind(tmp_path: Path, monkeypatch):
    plain = _load(tmp_path, "plain-skill", PLAIN_MD)

    def _boom(*args, **kwargs):
        raise AssertionError("bind_tools_for_thread must not be called")

    monkeypatch.setattr(tool_search_module, "bind_tools_for_thread", _boom)
    outcome = _bind_skill_kit_tools(
        plain, "30m", {"configurable": {"thread_id": "thread-a", "user_id": "u"}}
    )
    assert outcome == _SkillKitOutcome(None, None, "", False, False)


def test_bind_skill_kit_tools_failure_carries_verbatim_text(tmp_path: Path, monkeypatch):
    kit = _load(tmp_path, "hello-kit", KIT_MD)
    fake_binding = SimpleNamespace(
        ok=False, text="boom: missing dep", reload_tools=[], cap_hit=False
    )
    monkeypatch.setattr(
        tool_search_module, "bind_tools_for_thread", lambda *a, **k: fake_binding
    )
    outcome = _bind_skill_kit_tools(
        kit, "30m", {"configurable": {"thread_id": "thread-a", "user_id": "u"}}
    )
    assert outcome.failure_text == (
        "[Skill Kit activation failed: hello-kit]\n"
        "boom: missing dep\n\n"
        "No required tools were bound. Do not follow this skill's "
        "instructions until the dependency problem is fixed."
    )
    assert outcome.result_suffix == ""
    assert outcome.reload_queued is False
    assert outcome.cap_hit is False


def test_bind_skill_kit_tools_success_builds_result_suffix(tmp_path: Path, monkeypatch):
    kit = _load(tmp_path, "hello-kit", KIT_MD)
    fake_binding = SimpleNamespace(
        ok=True, text="Newly loaded: hello_test", reload_tools=["hello_test"], cap_hit=False
    )
    monkeypatch.setattr(
        tool_search_module, "bind_tools_for_thread", lambda *a, **k: fake_binding
    )
    outcome = _bind_skill_kit_tools(
        kit, "30m", {"configurable": {"thread_id": "thread-a", "user_id": "u"}}
    )
    assert outcome.failure_text is None
    assert outcome.result_suffix == (
        "\n\n---\nSkill Kit binding result for hello-kit:\nNewly loaded: hello_test"
    )
    assert outcome.reload_queued is True
    assert outcome.cap_hit is False
