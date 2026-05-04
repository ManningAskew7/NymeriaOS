"""Tests for AGENT-012: sync/async compaction deduplication.

Covers the shared helpers extracted from the duplicated sync/async
_clear_and_reset and _generate_summary methods, plus the removal of
the unused _apatch_dangling_tool_calls async method.
"""

from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from nymeria.core.agent_compaction import CompactionManager, create_compaction_marker
from nymeria.core.token_tracker import TokenTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager() -> CompactionManager:
    """Create a CompactionManager with a minimal agent stub."""
    agent = SimpleNamespace(
        settings=SimpleNamespace(
            context_management="auto_compact",
            compact_threshold=0.8,
        ),
        _token_tracker=TokenTracker(),
    )
    mgr = object.__new__(CompactionManager)
    mgr._agent = agent
    mgr._pending_summaries = {}
    mgr._pending_notepads = {}
    return mgr


# ---------------------------------------------------------------------------
# _build_clear_payload
# ---------------------------------------------------------------------------

class TestBuildClearPayload:
    def test_returns_remove_commands_and_marker(self):
        m1 = HumanMessage(content="hello", id="m1")
        m2 = AIMessage(content="hi", id="m2")
        payload = CompactionManager._build_clear_payload(
            [m1, m2], msg_count_before=5, summary="test summary", auto_resumed=False,
        )
        msgs = payload["messages"]
        assert len(msgs) == 3
        assert msgs[0].id == "m1"
        assert msgs[1].id == "m2"
        marker = msgs[2]
        assert marker.additional_kwargs["summary"] == "test summary"
        assert marker.additional_kwargs["messages_removed"] == 5
        assert marker.additional_kwargs["auto_resumed"] is False

    def test_auto_resumed_flag(self):
        m = HumanMessage(content="x", id="a")
        payload = CompactionManager._build_clear_payload(
            [m], msg_count_before=1, summary="s", auto_resumed=True,
        )
        marker = payload["messages"][-1]
        assert marker.additional_kwargs["auto_resumed"] is True

    def test_marker_gets_unique_id(self):
        m = HumanMessage(content="x", id="a")
        p1 = CompactionManager._build_clear_payload([m], 1, "", False)
        p2 = CompactionManager._build_clear_payload([m], 1, "", False)
        assert p1["messages"][-1].id != p2["messages"][-1].id


# ---------------------------------------------------------------------------
# _verify_clear
# ---------------------------------------------------------------------------

class TestVerifyClear:
    def test_accepts_single_message(self):
        state = SimpleNamespace(values={"messages": [HumanMessage(content="marker")]})
        assert CompactionManager._verify_clear("t1", state) is True

    def test_rejects_zero_messages(self):
        state = SimpleNamespace(values={"messages": []})
        assert CompactionManager._verify_clear("t1", state) is False

    def test_rejects_multiple_messages(self):
        state = SimpleNamespace(values={"messages": [
            HumanMessage(content="a"),
            AIMessage(content="b"),
        ]})
        assert CompactionManager._verify_clear("t1", state) is False


# ---------------------------------------------------------------------------
# _prune_old_checkpoints
# ---------------------------------------------------------------------------

class TestPruneOldCheckpoints:
    def test_delegates_to_prune_checkpoints_before(self):
        verify_state = SimpleNamespace(
            config={"configurable": {"checkpoint_id": "cp-1"}},
        )
        cp_tuple = SimpleNamespace(
            checkpoint={"channel_versions": {"msgs": "v3"}},
        )
        with patch(
            "nymeria.core.agent_compaction.prune_checkpoints_before",
            return_value=(2, 3, 1),
        ) as mock_prune:
            CompactionManager._prune_old_checkpoints("t1", verify_state, cp_tuple)
            mock_prune.assert_called_once_with(
                "t1", "cp-1", {"msgs": "v3"},
            )

    def test_handles_none_cp_tuple(self):
        verify_state = SimpleNamespace(
            config={"configurable": {"checkpoint_id": "cp-1"}},
        )
        with patch(
            "nymeria.core.agent_compaction.prune_checkpoints_before",
            return_value=(0, 0, 0),
        ) as mock_prune:
            CompactionManager._prune_old_checkpoints("t1", verify_state, None)
            mock_prune.assert_called_once_with("t1", "cp-1", {})

    def test_no_checkpoint_id_is_noop(self):
        verify_state = SimpleNamespace(config={"configurable": {}})
        with patch(
            "nymeria.core.agent_compaction.prune_checkpoints_before",
        ) as mock_prune:
            CompactionManager._prune_old_checkpoints("t1", verify_state, None)
            mock_prune.assert_not_called()


# ---------------------------------------------------------------------------
# _summary_input / _extract_summary_from_result
# ---------------------------------------------------------------------------

class TestSummaryHelpers:
    def test_summary_input_contains_compact_prompt(self):
        mgr = _make_manager()
        inp = mgr._summary_input()
        msgs = inp["messages"]
        assert len(msgs) == 1
        assert "compaction" in msgs[0].content.lower() or "summary" in msgs[0].content.lower()
        assert msgs[0].additional_kwargs.get("internal_type") == "compact_prompt"

    def test_extract_summary_from_result_finds_last_ai_message(self):
        messages: List[Any] = [
            HumanMessage(content="hello"),
            AIMessage(content="first response"),
            HumanMessage(content="continue"),
            AIMessage(content="final summary"),
        ]
        result = CompactionManager._extract_summary_from_result(messages)
        assert result == "final summary"

    def test_extract_summary_skips_empty_ai_messages(self):
        messages: List[Any] = [
            AIMessage(content="good content"),
            AIMessage(content=""),
        ]
        result = CompactionManager._extract_summary_from_result(messages)
        assert result == "good content"

    def test_extract_summary_returns_none_for_no_ai_messages(self):
        messages: List[Any] = [HumanMessage(content="only human")]
        assert CompactionManager._extract_summary_from_result(messages) is None

    def test_extract_summary_returns_none_for_empty_list(self):
        assert CompactionManager._extract_summary_from_result([]) is None


# ---------------------------------------------------------------------------
# Static regression: _apatch_dangling_tool_calls must stay deleted
# ---------------------------------------------------------------------------

class TestDeadCodeRemoval:
    def test_apatch_dangling_tool_calls_not_in_agent(self):
        """The unused async patch method must not come back."""
        from nymeria.core import agent as agent_mod
        assert not hasattr(agent_mod.NymeriaAgent, "_apatch_dangling_tool_calls"), (
            "_apatch_dangling_tool_calls was dead code and should not be reintroduced"
        )

    def test_sync_patch_dangling_still_exists(self):
        """The sync patch method is actively used and must remain."""
        from nymeria.core import agent as agent_mod
        assert hasattr(agent_mod.NymeriaAgent, "_patch_dangling_tool_calls")

    def test_shared_helpers_exist_on_compaction_manager(self):
        """Verify the extracted shared helpers are present."""
        for name in (
            "_build_clear_payload",
            "_verify_clear",
            "_prune_old_checkpoints",
            "_summary_input",
            "_extract_summary_from_result",
        ):
            assert hasattr(CompactionManager, name), f"Missing helper: {name}"

    def test_sync_async_clear_and_reset_both_use_build_clear_payload(self):
        """Both clear_and_reset methods must delegate to _build_clear_payload."""
        src = inspect.getsource(CompactionManager)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in ("_clear_and_reset", "_clear_and_reset_sync"):
                    body_src = ast.get_source_segment(src, node)
                    assert "_build_clear_payload" in body_src, (
                        f"{node.name} must call _build_clear_payload"
                    )

    def test_sync_async_generate_summary_both_use_summary_input(self):
        """Both summary methods must delegate to _summary_input."""
        src = inspect.getsource(CompactionManager)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in ("_generate_summary", "_generate_summary_sync"):
                    body_src = ast.get_source_segment(src, node)
                    assert "_summary_input" in body_src, (
                        f"{node.name} must call _summary_input"
                    )
