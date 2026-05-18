"""Integration tests for dynamic tool binding (Phase 1).

These tests exercise the wiring between agent.py, nodes.py, and graph.py
in dynamic mode — verifying that:

- ``_build_graph_with_prompt`` dispatches to ``_build_dynamic_graph_with_prompt``
  when the flag is on, and through the legacy path when off.
- The dynamic resolver is constructed and produces ``(tools, hash)`` tuples
  whose hash reflects ThreadConfig changes.
- ``_compute_tool_superset`` populates ``_current_tool_superset_names`` and
  includes core tools.
- ``create_graph`` is called with the right kwargs in dynamic mode.

Tests use the same MagicMock pattern as ``test_graph_build_unification.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nymeria.vendor.react_agent.config import CheckpointerConfig


def _make_agent(*, dynamic: bool = False):
    """Mirror of test_graph_build_unification._make_agent with the
    dynamic-binding flag and superset-name set initialized."""
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
    agent.tool_registry.all_tools.return_value = []
    agent.tool_registry.get_tool.return_value = None
    agent._callable_tool_thread_map = {}
    agent._on_tool_timeout = None
    agent.MAIN_AGENT_MAX_ITERATIONS = 500
    agent.CALLABLE_DEFAULT_MAX_ITERATIONS = 10
    agent.TURN_SAME_TOOL_RESULT_LIMIT = 5
    agent._checkpointer_config = CheckpointerConfig(backend="memory")
    agent._async_checkpointer_config = CheckpointerConfig(
        backend="sqlite_async", sqlite_path="/tmp/test.db"
    )
    agent.skill_manager = None
    agent._get_llm_config_for_thread = MagicMock(return_value=MagicMock())

    # The new dynamic-mode state: read live from settings.
    agent.settings.dynamic_tool_binding = dynamic
    agent._current_tool_superset_names = set()

    return agent


def test_flag_off_uses_legacy_build_path():
    """When dynamic_tool_binding is False, the legacy create_graph call
    receives no dynamic_tool_resolver kwarg."""
    agent = _make_agent(dynamic=False)
    captured = {}

    def capture(config=None, tools=None, checkpointer=None, **kwargs):
        captured["kwargs"] = kwargs
        return MagicMock()

    with patch("nymeria.core.agent_graph.create_graph", side_effect=capture):
        agent._build_graph_with_prompt("prompt", user_id="u1", thread_id="t1")

    assert "dynamic_tool_resolver" not in captured["kwargs"]
    assert "superset_tools" not in captured["kwargs"]


def test_flag_on_passes_resolver_and_superset_to_create_graph():
    agent = _make_agent(dynamic=True)
    captured = {}

    def capture(config=None, tools=None, checkpointer=None, **kwargs):
        captured["kwargs"] = kwargs
        captured["tools_len"] = len(tools) if tools else 0
        return MagicMock()

    with patch("nymeria.core.agent_graph.create_graph", side_effect=capture):
        agent._build_graph_with_prompt("prompt", user_id="u1", thread_id="t1")

    assert "dynamic_tool_resolver" in captured["kwargs"]
    assert "superset_tools" in captured["kwargs"]
    assert callable(captured["kwargs"]["dynamic_tool_resolver"])
    # Superset should be non-empty (core ALL_TOOLS at minimum).
    assert captured["tools_len"] > 0
    assert len(captured["kwargs"]["superset_tools"]) > 0


def test_flag_on_populates_superset_name_set_on_agent():
    """should_emit_reload_command reads _current_tool_superset_names; it
    must be populated by every dynamic graph build."""
    agent = _make_agent(dynamic=True)
    with patch("nymeria.core.agent_graph.create_graph", return_value=MagicMock()):
        agent._build_graph_with_prompt("prompt", user_id="u1", thread_id="t1")

    assert agent._current_tool_superset_names
    # ALL_TOOLS core names must be there — bash_execute, file_read, etc.
    assert "bash_execute" in agent._current_tool_superset_names
    assert "tool_search" in agent._current_tool_superset_names


def test_resolver_returns_consistent_hash_for_unchanged_config():
    agent = _make_agent(dynamic=True)
    resolver = agent._make_dynamic_tool_resolver("u1", "t1")
    _, h1 = resolver()
    _, h2 = resolver()
    assert h1 == h2


def test_resolver_hash_changes_when_thread_config_mutates():
    """Mutating the thread_config_manager's response between resolver calls
    should change the returned hash."""
    from nymeria.core.thread_config import ThreadConfig

    agent = _make_agent(dynamic=True)
    resolver = agent._make_dynamic_tool_resolver("u1", "t1")

    agent.thread_config_manager.get_config.return_value = ThreadConfig(thread_id="t1")
    _, h1 = resolver()

    agent.thread_config_manager.get_config.return_value = ThreadConfig(
        thread_id="t1", enabled_tools=["calendar_list_events"]
    )
    _, h2 = resolver()

    assert h1 != h2


def test_async_build_path_also_branches_on_flag():
    agent = _make_agent(dynamic=True)
    captured = {}

    def capture(config=None, tools=None, checkpointer=None, **kwargs):
        captured["backend"] = config.checkpointer.backend if config else None
        captured["has_resolver"] = "dynamic_tool_resolver" in kwargs
        return MagicMock()

    with patch("nymeria.core.agent_graph.create_graph", side_effect=capture):
        agent._build_async_graph_with_prompt("prompt", user_id="u1", thread_id="t1")

    assert captured["has_resolver"] is True
    assert captured["backend"] == "sqlite_async"


def test_compute_superset_dedupes_across_sources():
    """Core + optional + registry should be merged with no duplicates by name."""
    agent = _make_agent(dynamic=True)
    tools, names = agent._compute_tool_superset("u1", "t1")
    tool_names = [t.name for t in tools]
    assert len(tool_names) == len(set(tool_names))
    assert set(tool_names) == names


def test_legacy_reload_path_unchanged_when_flag_off():
    """The legacy build path doesn't touch _current_tool_superset_names —
    ensuring backwards-compat: should_emit_reload_command's defensive check
    falls back to True when the set is empty."""
    agent = _make_agent(dynamic=False)
    with patch("nymeria.core.agent_graph.create_graph", return_value=MagicMock()):
        agent._build_graph_with_prompt("prompt", user_id="u1", thread_id="t1")

    # Flag off → no superset population.
    assert agent._current_tool_superset_names == set()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
