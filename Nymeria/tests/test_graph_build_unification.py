"""Tests for AGENT-002: sync and async graph-build path unification.

Validates that _select_tools_for_graph is the single source of truth for
tool selection and that sync/async paths differ only in checkpointer config.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from nymeria.vendor.react_agent.config import CheckpointerConfig


def _method_tree(method):
    return ast.parse(textwrap.dedent(inspect.getsource(method)))


def _called_method_names(method) -> set[str]:
    names = set()
    for node in ast.walk(_method_tree(method)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
    return names


def _direct_graph_rebuild_ops(method) -> list[str]:
    ops = []
    for node in ast.walk(_method_tree(method)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            if (
                func.attr == "clear"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr in {"_user_graphs", "_async_user_graphs"}
            ):
                ops.append(f"{func.value.attr}.clear")
            if func.attr in {"_build_graph_with_prompt", "_build_async_graph_with_prompt"}:
                ops.append(func.attr)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in {"_default_graph", "_default_async_graph"}
                ):
                    ops.append(f"assign {target.attr}")
    return ops


def _make_agent():
    """Create a minimal NymeriaAgent mock with the real graph-build methods."""
    from nymeria.core.agent import NymeriaAgent

    with patch.object(NymeriaAgent, "__init__", lambda self: None):
        agent = NymeriaAgent()

    agent.settings = MagicMock()
    agent.settings.tool_timeout = 300
    agent.settings.tool_output_max_chars = 100000
    agent.settings.log_level = "INFO"
    agent.thread_config_manager = MagicMock()
    agent.thread_config_manager.get_config.return_value = None
    agent.profile_manager = MagicMock()
    agent.profile_manager.get_profile.return_value = MagicMock(
        tool_preferences=MagicMock(default_thread_tools=None),
    )
    agent.accounts_repo = MagicMock()
    agent.tool_registry = MagicMock()
    agent._callable_tool_thread_map = {}
    agent._on_tool_timeout = None
    agent.MAIN_AGENT_MAX_ITERATIONS = 500
    agent.CALLABLE_DEFAULT_MAX_ITERATIONS = 10
    agent.TURN_SAME_TOOL_RESULT_LIMIT = 5

    agent._checkpointer_config = CheckpointerConfig(backend="memory")
    agent._async_checkpointer_config = CheckpointerConfig(backend="sqlite_async", sqlite_path="/tmp/test.db")
    agent.skill_manager = None
    agent._get_llm_config_for_thread = MagicMock(return_value=MagicMock())

    return agent


def test_select_tools_shared_by_both_build_paths():
    """Both _build_graph_with_prompt and _build_async_graph_with_prompt
    call _select_tools_for_graph — the tool list is identical."""
    agent = _make_agent()

    calls = []
    original_select = agent._select_tools_for_graph.__func__

    def tracking_select(self, user_id, thread_id):
        result = original_select(self, user_id, thread_id)
        calls.append(("select", user_id, thread_id, [t.name for t in result[0]]))
        return result

    with patch.object(type(agent), "_select_tools_for_graph", tracking_select):
        with patch("nymeria.core.agent.create_graph", return_value=MagicMock()):
            agent._build_graph_with_prompt("prompt", user_id="u1", thread_id="t1")
            agent._build_async_graph_with_prompt("prompt", user_id="u1", thread_id="t1")

    assert len(calls) == 2
    assert calls[0][1:] == calls[1][1:]


def test_sync_async_differ_only_in_checkpointer():
    """Sync and async graph builds produce AgentConfigs that differ only
    in the checkpointer field."""
    agent = _make_agent()

    configs_seen = []

    def capture_create_graph(config=None, tools=None):
        configs_seen.append(config)
        return MagicMock()

    with patch("nymeria.core.agent.create_graph", side_effect=capture_create_graph):
        agent._build_graph_with_prompt("prompt", user_id="u1", thread_id="t1")
        agent._build_async_graph_with_prompt("prompt", user_id="u1", thread_id="t1")

    assert len(configs_seen) == 2
    sync_cfg, async_cfg = configs_seen

    assert sync_cfg.checkpointer.backend == "memory"
    assert async_cfg.checkpointer.backend == "sqlite_async"

    assert sync_cfg.system_prompt == async_cfg.system_prompt
    assert sync_cfg.max_iterations == async_cfg.max_iterations
    assert sync_cfg.tool_timeout == async_cfg.tool_timeout
    assert sync_cfg.tool_output_max_chars == async_cfg.tool_output_max_chars
    assert sync_cfg.verbose == async_cfg.verbose


def test_default_graph_excludes_capability_expansion_tools():
    agent = _make_agent()

    tools, _ = agent._select_tools_for_graph("u1", "t1")
    names = {tool.name for tool in tools}

    from nymeria.tools import (
        CAPABILITY_EXPANSION_TOOL_NAMES,
        LOCAL_SYSTEM_ACCESS_TOOL_NAMES,
        OPTIONAL_TOOLS,
    )

    assert {"web_search", "consult", "notify"}.issubset(names)
    assert names.isdisjoint(CAPABILITY_EXPANSION_TOOL_NAMES)
    assert names.isdisjoint(LOCAL_SYSTEM_ACCESS_TOOL_NAMES)
    assert LOCAL_SYSTEM_ACCESS_TOOL_NAMES.issubset(OPTIONAL_TOOLS)


def test_build_agent_config_uses_callable_iteration_limit():
    """Callable threads get a lower iteration limit."""
    agent = _make_agent()
    from nymeria.core.thread_config import ThreadConfig

    tc = ThreadConfig(thread_id="t1")
    tc.callable = True
    tc.callable_name = "helper"
    tc.callable_max_iterations = 7

    config = agent._build_agent_config(
        "prompt", agent._checkpointer_config, "t1", tc
    )
    assert config.max_iterations == 7

    config_main = agent._build_agent_config(
        "prompt", agent._checkpointer_config, "t1", None
    )
    assert config_main.max_iterations == 500


def test_get_graph_for_user_delegates_to_impl():
    """Both _get_graph_for_user and _get_async_graph_for_user go through
    _get_graph_for_user_impl."""
    agent = _make_agent()
    agent._user_graphs = {}
    agent._async_user_graphs = {}
    agent._graph_cache_lock = __import__("threading").Lock()
    agent._GRAPH_CACHE_MAX = 100

    calls = []
    def mock_impl(
        self,
        user_id,
        is_autonomous,
        thread_id,
        cache,
        build_fn,
        cache_key_fn=None,
    ):
        calls.append((cache is self._user_graphs, cache is self._async_user_graphs))
        return MagicMock()

    with patch.object(type(agent), "_get_graph_for_user_impl", mock_impl):
        agent._get_graph_for_user("u1", thread_id="t1")
        agent._get_async_graph_for_user("u1", thread_id="t1")

    assert calls[0] == (True, False)
    assert calls[1] == (False, True)


def test_async_graph_cache_is_scoped_to_running_event_loop():
    agent = _make_agent()
    agent._async_user_graphs = {}
    agent._graph_cache_lock = __import__("threading").Lock()
    agent._GRAPH_CACHE_MAX = 100
    agent._base_system_prompt = "base"
    agent._get_memory_hash = MagicMock(return_value="hash")
    agent._build_full_system_prompt = MagicMock(return_value="full")
    agent.profile_manager.get_profile.return_value = SimpleNamespace(
        memories={},
        personality_overrides={},
        tool_preferences=SimpleNamespace(default_thread_tools=[]),
    )
    todo_list = MagicMock()
    todo_list.get_active_todos_for_thread.return_value = []
    agent.todo_manager = MagicMock()
    agent.todo_manager.get_todos.return_value = todo_list

    first_graph = object()
    second_graph = object()
    agent._build_async_graph_with_prompt = MagicMock(
        side_effect=[first_graph, second_graph]
    )

    async def get_graph():
        return agent._get_async_graph_for_user("u1", thread_id="t1")

    loop_one = asyncio.new_event_loop()
    loop_two = asyncio.new_event_loop()
    try:
        graph_one = loop_one.run_until_complete(get_graph())
        graph_one_again = loop_one.run_until_complete(get_graph())
        graph_two = loop_two.run_until_complete(get_graph())
    finally:
        loop_one.close()
        loop_two.close()

    assert graph_one is first_graph
    assert graph_one_again is first_graph
    assert graph_two is second_graph
    assert agent._build_async_graph_with_prompt.call_count == 2


def test_toolset_mutators_delegate_default_graph_rebuild():
    """Tool-set mutation paths go through the shared cache rebuild helper."""
    from nymeria.core.agent import NymeriaAgent

    method_names = [
        "register_tool",
        "register_tools",
        "sync_agent_tools",
        "reload_mcp_server_tools",
        "reload_custom_tools",
        "reload_tools",
    ]

    for method_name in method_names:
        method = getattr(NymeriaAgent, method_name)
        assert "_rebuild_default_graphs" in _called_method_names(method)
        assert _direct_graph_rebuild_ops(method) == []
