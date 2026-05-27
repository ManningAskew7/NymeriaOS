"""Tests for AGENT-012: sync/async compaction deduplication.

Covers the shared helpers extracted from the duplicated sync/async
_clear_and_reset and _generate_summary methods, plus the removal of
the unused _apatch_dangling_tool_calls async method.
"""

from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from nymeria.core.agent_compaction import CompactionManager
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

class TestVerifyRetained:
    def test_accepts_expected_tail_ending_on_tool_message(self):
        from langchain_core.messages import ToolMessage
        tail = [
            HumanMessage(content="resume opener"),
            AIMessage(content="", tool_calls=[
                {"id": "g", "name": "memory_read", "args": {"scope": "global"}, "type": "tool_call"},
            ]),
            ToolMessage(content="G", tool_call_id="g", name="memory_read"),
        ]
        state = SimpleNamespace(values={"messages": tail})
        assert CompactionManager._verify_retained("t1", state, len(tail)) is True

    def test_rejects_wrong_length(self):
        state = SimpleNamespace(values={"messages": [HumanMessage(content="a")]})
        assert CompactionManager._verify_retained("t1", state, 3) is False

    def test_rejects_tail_ending_on_open_tool_calls(self):
        msgs = [
            HumanMessage(content="a"),
            AIMessage(content="", tool_calls=[
                {"id": "x", "name": "memory_read", "args": {}, "type": "tool_call"},
            ]),
        ]
        state = SimpleNamespace(values={"messages": msgs})
        assert CompactionManager._verify_retained("t1", state, len(msgs)) is False


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

    def test_extract_summary_joins_content_block_list(self):
        """Anthropic-style content blocks must be joined to text, not repr'd."""
        msg = AIMessage(content=[
            {"type": "text", "text": "## Active Goal\nFirst block."},
            {"type": "text", "text": "## Progress\nSecond block."},
        ])
        result = CompactionManager.extract_summary(msg)
        assert result == "## Active Goal\nFirst block.\n## Progress\nSecond block."
        assert "[{" not in result

    def test_extract_summary_handles_block_without_type_key(self):
        msg = AIMessage(content=[{"text": "raw markdown summary"}])
        result = CompactionManager.extract_summary(msg)
        assert result == "raw markdown summary"

    def test_extract_summary_skips_non_text_blocks(self):
        msg = AIMessage(content=[
            {"type": "thinking", "thinking": "internal reasoning"},
            {"type": "text", "text": "visible summary"},
        ])
        result = CompactionManager.extract_summary(msg)
        assert result == "visible summary"
        assert "internal reasoning" not in result

    def test_extract_summary_passes_string_content_through(self):
        msg = AIMessage(content="plain string summary")
        assert CompactionManager.extract_summary(msg) == "plain string summary"


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
            "_prune_old_checkpoints",
            "_summary_input",
            "_extract_summary_from_result",
            "_run_compact_turn_and_prune",
            "_run_compact_turn_and_prune_sync",
            "_verify_retained",
        ):
            assert hasattr(CompactionManager, name), f"Missing helper: {name}"

    def test_retained_turn_clear_helpers_are_gone(self):
        """The old wipe-and-marker helpers were replaced by the retained tail."""
        for name in ("_build_clear_payload", "_verify_clear", "_clear_and_reset",
                     "_clear_and_reset_sync", "get_pending_summary"):
            assert not hasattr(CompactionManager, name), (
                f"{name} should have been removed by the retained-turn redesign"
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
