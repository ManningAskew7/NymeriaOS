"""Unit tests for the dynamic tool binding mode (Phase 1).

Covers the four contracts that gate correctness:
1. ``_tool_config_hash`` is stable across no-op reads and changes on mutation.
2. ``should_emit_reload_command`` returns True in rebuild mode, and False in
   dynamic mode because missing post-build tools are resolved at execution time.
3. ``create_dynamic_agent_node`` reuses its bound LLM when the resolver's
   hash is unchanged across consecutive calls, and rebinds when it changes.
4. ``tool_search._enable`` returns a plain string (not a Command) when
   running in dynamic mode, including for post-build tool names.
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from typing import List
from unittest.mock import patch

from langchain_core.tools import tool
from langgraph.types import Command

from nymeria.core import tool_reload
from nymeria.core.agent import NymeriaAgent
from nymeria.core.thread_config import TemporaryToolEntry, ThreadConfig
from nymeria.core.time_utils import utc_now


class _StubSettings:
    """Minimal stand-in for the pydantic Settings model in unit tests."""

    def __init__(self, dynamic_tool_binding: bool = False):
        self.dynamic_tool_binding = dynamic_tool_binding


def _stub_agent(*, dynamic: bool = False) -> NymeriaAgent:
    """Create a NymeriaAgent stub with just enough state for the helpers."""
    agent = object.__new__(NymeriaAgent)
    agent.settings = _StubSettings(dynamic_tool_binding=dynamic)
    agent._current_tool_superset_names = set()
    return agent


# ---------------------------------------------------------------------------
# _tool_config_hash
# ---------------------------------------------------------------------------


class ToolConfigHashTests(unittest.TestCase):
    def setUp(self):
        self.agent = _stub_agent()

    def test_empty_thread_config_returns_stable_hash(self):
        h1 = self.agent._tool_config_hash("u", "t", None)
        h2 = self.agent._tool_config_hash("u", "t", None)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)

    def test_hash_changes_when_enabled_tools_added(self):
        tc1 = ThreadConfig(thread_id="t", enabled_tools=[])
        tc2 = ThreadConfig(thread_id="t", enabled_tools=["calendar_list_events"])
        h1 = self.agent._tool_config_hash("u", "t", tc1)
        h2 = self.agent._tool_config_hash("u", "t", tc2)
        self.assertNotEqual(h1, h2)

    def test_hash_invariant_under_reorder(self):
        tc1 = ThreadConfig(thread_id="t", enabled_tools=["a", "b"])
        tc2 = ThreadConfig(thread_id="t", enabled_tools=["b", "a"])
        self.assertEqual(
            self.agent._tool_config_hash("u", "t", tc1),
            self.agent._tool_config_hash("u", "t", tc2),
        )

    def test_hash_excludes_expired_temporary_tools(self):
        now = utc_now()
        expired = TemporaryToolEntry(
            enabled_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
        live = TemporaryToolEntry(
            enabled_at=now,
            expires_at=now + timedelta(hours=1),
        )
        tc_with_expired = ThreadConfig(
            thread_id="t",
            temporary_tools={"a": expired, "b": live},
        )
        tc_only_live = ThreadConfig(
            thread_id="t",
            temporary_tools={"b": live},
        )
        self.assertEqual(
            self.agent._tool_config_hash("u", "t", tc_with_expired),
            self.agent._tool_config_hash("u", "t", tc_only_live),
        )

    def test_hash_distinguishes_skills(self):
        tc1 = ThreadConfig(thread_id="t", enabled_skills=[])
        tc2 = ThreadConfig(thread_id="t", enabled_skills=["hello-kit"])
        self.assertNotEqual(
            self.agent._tool_config_hash("u", "t", tc1),
            self.agent._tool_config_hash("u", "t", tc2),
        )


# ---------------------------------------------------------------------------
# should_emit_reload_command
# ---------------------------------------------------------------------------


class ShouldEmitReloadCommandTests(unittest.TestCase):
    def test_no_agent_falls_back_to_emit(self):
        with patch("nymeria.core.agent.get_current_agent", return_value=None):
            self.assertTrue(tool_reload.should_emit_reload_command(["x"]))

    def test_rebuild_mode_always_emits(self):
        agent = _stub_agent()
        agent.settings.dynamic_tool_binding = False
        agent._current_tool_superset_names = {"x"}
        with patch("nymeria.core.agent.get_current_agent", return_value=agent):
            self.assertTrue(tool_reload.should_emit_reload_command(["x"]))
            self.assertTrue(tool_reload.should_emit_reload_command([]))

    def test_dynamic_mode_skips_reload_for_existing_and_new_tool_names(self):
        agent = _stub_agent()
        agent.settings.dynamic_tool_binding = True
        agent._current_tool_superset_names = {"x", "y"}
        with patch("nymeria.core.agent.get_current_agent", return_value=agent):
            self.assertFalse(tool_reload.should_emit_reload_command(["x"]))
            self.assertFalse(tool_reload.should_emit_reload_command(["x", "y"]))
            self.assertFalse(
                tool_reload.should_emit_reload_command(
                    ["unknown_post_build_tool"],
                    thread_id="thread-a",
                )
            )
            self.assertFalse(tool_reload.should_emit_reload_command([]))


# ---------------------------------------------------------------------------
# create_dynamic_agent_node (rebind caching)
# ---------------------------------------------------------------------------


class DynamicAgentNodeRebindTests(unittest.TestCase):
    """Exercise the cache-on-hash behavior of create_dynamic_agent_node.

    The node should only re-call create_llm_with_tools when the resolver's
    hash changes. This is the prompt-cache-friendliness guarantee.
    """

    def _build_node_and_count(self, hashes: List[str]):
        from nymeria.vendor.react_agent import nodes as nodes_mod
        from nymeria.vendor.react_agent.config import LLMConfig

        @tool
        def fake_tool(query: str) -> str:
            """Stub tool — never executed in this test."""
            return query

        call_count = {"create_llm_with_tools": 0, "inner_agent_node": 0}

        class _FakeLLM:
            def __init__(self, tag):
                self.tag = tag

            def invoke(self, _state):
                return {"messages": []}

            async def ainvoke(self, _state):
                return {"messages": []}

        def fake_create_llm_with_tools(_llm_config, _tools):
            call_count["create_llm_with_tools"] += 1
            return _FakeLLM(tag=call_count["create_llm_with_tools"])

        # The inner agent node factory wraps the bound LLM. We stub it to
        # return a runnable that just returns an empty dict and increments
        # the counter on every invoke so we can detect rebuilds.
        def fake_create_agent_node(llm_with_tools, _system_prompt, _llm_config, _tools):
            class _Inner:
                def invoke(self, _state):
                    call_count["inner_agent_node"] += 1
                    return {"messages": []}

                async def ainvoke(self, _state):
                    call_count["inner_agent_node"] += 1
                    return {"messages": []}

            return _Inner()

        idx = {"i": 0}

        def resolver():
            i = idx["i"]
            idx["i"] = min(i + 1, len(hashes) - 1)
            return [fake_tool], hashes[i]

        with patch.object(nodes_mod, "create_llm_with_tools", fake_create_llm_with_tools), \
             patch.object(nodes_mod, "create_agent_node", fake_create_agent_node):
            node = nodes_mod.create_dynamic_agent_node(
                system_prompt="sys",
                llm_config=LLMConfig(),
                tool_resolver=resolver,
            )
            for _ in range(len(hashes)):
                node.invoke({"messages": []})
        return call_count

    def test_same_hash_across_steps_rebinds_once(self):
        counts = self._build_node_and_count(["h1", "h1", "h1"])
        self.assertEqual(counts["create_llm_with_tools"], 1)
        self.assertEqual(counts["inner_agent_node"], 3)

    def test_changed_hash_triggers_rebind(self):
        counts = self._build_node_and_count(["h1", "h2", "h2", "h3"])
        # Rebinds on h1 (first), h2 (change), h3 (change) = 3 binds.
        self.assertEqual(counts["create_llm_with_tools"], 3)


# ---------------------------------------------------------------------------
# tool_search._enable: dynamic-mode return shape
# ---------------------------------------------------------------------------


class ToolEnableDynamicReturnTests(unittest.TestCase):
    """In dynamic mode, _enable should NOT return Command(goto=END)."""

    def test_enable_in_dynamic_mode_returns_string_when_in_superset(self):
        import sys
        ts_mod = sys.modules["nymeria.tools.tool_search"]

        agent = _stub_agent()
        agent.settings.dynamic_tool_binding = True
        agent._current_tool_superset_names = {"calendar_list_events"}

        binding_result = ts_mod.ToolBindingResult(
            ok=True,
            text="[Success]: 1 tool enabled.",
            reload_tools=["calendar_list_events"],
            cap_hit=False,
        )

        with patch.object(ts_mod, "bind_tools_for_thread", return_value=binding_result), \
             patch("nymeria.core.agent.get_current_agent", return_value=agent):
            result = ts_mod._enable(
                tool_names=["calendar_list_events"],
                category="",
                thread_id="thread-a",
                user_id="u",
                ttl="2h",
                tool_call_id="call-1",
            )
        self.assertIsInstance(result, str)
        self.assertEqual(result, binding_result.text)

    def test_enable_in_rebuild_mode_returns_command_unchanged(self):
        import sys
        ts_mod = sys.modules["nymeria.tools.tool_search"]

        agent = _stub_agent()
        agent.settings.dynamic_tool_binding = False

        binding_result = ts_mod.ToolBindingResult(
            ok=True,
            text="[Success]: 1 tool enabled.",
            reload_tools=["calendar_list_events"],
            cap_hit=False,
        )

        with patch.object(ts_mod, "bind_tools_for_thread", return_value=binding_result), \
             patch("nymeria.core.agent.get_current_agent", return_value=agent):
            result = ts_mod._enable(
                tool_names=["calendar_list_events"],
                category="",
                thread_id="thread-a",
                user_id="u",
                ttl="2h",
                tool_call_id="call-1",
            )
        self.assertIsInstance(result, Command)

    def test_enable_in_dynamic_mode_returns_string_for_post_build_tool(self):
        import sys
        ts_mod = sys.modules["nymeria.tools.tool_search"]

        agent = _stub_agent()
        agent.settings.dynamic_tool_binding = True
        agent._current_tool_superset_names = {"tool_search"}

        binding_result = ts_mod.ToolBindingResult(
            ok=True,
            text="[Success]: 1 tool enabled.",
            reload_tools=["new_custom_tool"],
            cap_hit=False,
        )

        with patch.object(ts_mod, "bind_tools_for_thread", return_value=binding_result), \
             patch("nymeria.core.agent.get_current_agent", return_value=agent):
            result = ts_mod._enable(
                tool_names=["new_custom_tool"],
                category="",
                thread_id="thread-a",
                user_id="u",
                ttl="2h",
                tool_call_id="call-1",
            )
        self.assertIsInstance(result, str)
        self.assertEqual(result, binding_result.text)


if __name__ == "__main__":
    unittest.main()
