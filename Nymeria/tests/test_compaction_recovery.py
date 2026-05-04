"""Tests for pending-summary recovery after process restart.

Covers AGENT-004: the compaction marker in the checkpoint stores the
summary in additional_kwargs["summary"].  If the in-memory
_pending_summaries dict is lost (process restart), CompactionManager
should recover the summary from the checkpoint on the next turn.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage

from nymeria.core.agent import NymeriaAgent
from nymeria.core.agent_compaction import CompactionManager, create_compaction_marker
from nymeria.core.token_tracker import TokenTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeGraphState:
    """Simulates graph.get_state() return value."""

    def __init__(self, messages: List[Any]):
        self.values = {"messages": messages}


class FakeGraph:
    """Minimal graph stub that returns a canned state."""

    def __init__(self, messages: List[Any]):
        self._messages = messages

    def get_state(self, config: dict) -> FakeGraphState:
        return FakeGraphState(self._messages)


def _make_agent(messages: Optional[List[Any]] = None) -> NymeriaAgent:
    """Create a minimal NymeriaAgent stub with a fake default graph."""
    agent = object.__new__(NymeriaAgent)
    agent.settings = SimpleNamespace(
        context_management="auto_compact",
        compact_threshold=0.8,
    )
    agent._token_tracker = TokenTracker()
    agent._default_graph = FakeGraph(messages or [])
    agent._compaction = CompactionManager(agent)
    return agent


def _make_marker(
    summary: str = "Test summary of prior conversation.",
    auto_resumed: bool = False,
    messages_removed: int = 10,
) -> HumanMessage:
    return create_compaction_marker(
        summary=summary,
        messages_removed=messages_removed,
        auto_resumed=auto_resumed,
    )


# ---------------------------------------------------------------------------
# Tests: recovery from compaction marker
# ---------------------------------------------------------------------------

class TestPendingSummaryRecovery:
    """Recovery of pending summary from compaction marker after restart."""

    def test_normal_path_returns_in_memory_summary(self):
        """In-memory pending summary is returned without touching checkpoint."""
        agent = _make_agent(messages=[])
        agent._compaction._pending_summaries["t1"] = "in-memory summary"

        result = agent._compaction.get_pending_summary("t1")

        assert result == "in-memory summary"
        assert "t1" not in agent._compaction._pending_summaries

    def test_recovery_from_compaction_marker(self):
        """After restart, summary is recovered from the compaction marker."""
        marker = _make_marker(summary="Recovered context about the project.")
        agent = _make_agent(messages=[marker])

        result = agent._compaction.get_pending_summary("t1")

        assert result == "Recovered context about the project."

    def test_recovery_only_when_single_marker(self):
        """No recovery if checkpoint has more than one message (user already sent a follow-up)."""
        marker = _make_marker()
        user_msg = HumanMessage(content="Hello after compaction")
        agent = _make_agent(messages=[marker, user_msg])

        result = agent._compaction.get_pending_summary("t1")

        assert result is None

    def test_no_recovery_for_auto_resumed_marker(self):
        """auto_resumed=True markers were handled inline; no pending recovery needed."""
        marker = _make_marker(auto_resumed=True)
        agent = _make_agent(messages=[marker])

        result = agent._compaction.get_pending_summary("t1")

        assert result is None

    def test_no_recovery_for_empty_checkpoint(self):
        """Empty checkpoint means no compaction happened."""
        agent = _make_agent(messages=[])

        result = agent._compaction.get_pending_summary("t1")

        assert result is None

    def test_no_recovery_for_normal_message(self):
        """A single non-marker message should not trigger recovery."""
        agent = _make_agent(messages=[HumanMessage(content="Hello")])

        result = agent._compaction.get_pending_summary("t1")

        assert result is None

    def test_no_recovery_for_ai_message(self):
        """A single AI message should not trigger recovery."""
        agent = _make_agent(messages=[AIMessage(content="Response")])

        result = agent._compaction.get_pending_summary("t1")

        assert result is None

    def test_no_recovery_when_marker_has_no_summary(self):
        """Edge case: marker exists but summary field is empty."""
        marker = _make_marker(summary="")
        agent = _make_agent(messages=[marker])

        result = agent._compaction.get_pending_summary("t1")

        assert result is None

    def test_in_memory_takes_precedence_over_checkpoint(self):
        """If both in-memory and checkpoint have summaries, in-memory wins."""
        marker = _make_marker(summary="checkpoint summary")
        agent = _make_agent(messages=[marker])
        agent._compaction._pending_summaries["t1"] = "in-memory summary"

        result = agent._compaction.get_pending_summary("t1")

        assert result == "in-memory summary"

    def test_recovery_is_idempotent(self):
        """Calling get_pending_summary twice recovers from checkpoint both times."""
        marker = _make_marker(summary="Persistent summary")
        agent = _make_agent(messages=[marker])

        first = agent._compaction.get_pending_summary("t1")
        second = agent._compaction.get_pending_summary("t1")

        assert first == "Persistent summary"
        assert second == "Persistent summary"


# ---------------------------------------------------------------------------
# Tests: notepad recovery alongside summary
# ---------------------------------------------------------------------------

class TestNotepadRecoveryAlongsideSummary:
    """When summary is recovered from checkpoint, notepad is also re-read."""

    def test_notepad_recovered_from_disk_on_summary_recovery(self):
        """Notepad is re-read from disk when summary recovery triggers."""
        marker = _make_marker(summary="Recovered summary")
        agent = _make_agent(messages=[marker])

        with patch.object(
            CompactionManager, "_read_thread_notepad", return_value="My notepad content"
        ):
            summary = agent._compaction.get_pending_summary("t1")

        assert summary == "Recovered summary"
        assert agent._compaction._pending_notepads["t1"] == "My notepad content"

        notepad = agent._compaction.pop_pending_notepad("t1")
        assert notepad == "My notepad content"

    def test_no_notepad_when_none_exists(self):
        """If thread has no notepad, _pending_notepads stays empty."""
        marker = _make_marker(summary="Recovered summary")
        agent = _make_agent(messages=[marker])

        with patch.object(
            CompactionManager, "_read_thread_notepad", return_value=None
        ):
            summary = agent._compaction.get_pending_summary("t1")

        assert summary == "Recovered summary"
        assert "t1" not in agent._compaction._pending_notepads

    def test_notepad_not_recovered_for_normal_in_memory_path(self):
        """Normal in-memory path doesn't trigger notepad re-read."""
        agent = _make_agent(messages=[])
        agent._compaction._pending_summaries["t1"] = "in-memory"

        with patch.object(
            CompactionManager, "_read_thread_notepad"
        ) as mock_read:
            agent._compaction.get_pending_summary("t1")
            mock_read.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: checkpoint read failure resilience
# ---------------------------------------------------------------------------

class TestRecoveryResilience:
    """Recovery gracefully handles checkpoint read failures."""

    def test_exception_during_checkpoint_read_returns_none(self):
        """If graph.get_state() fails, recovery returns None (no crash)."""
        agent = _make_agent(messages=[])
        agent._default_graph = SimpleNamespace(
            get_state=lambda config: (_ for _ in ()).throw(
                RuntimeError("DB connection lost")
            )
        )

        result = agent._compaction.get_pending_summary("t1")

        assert result is None


# ---------------------------------------------------------------------------
# Tests: has_pending_summary does NOT trigger recovery
# ---------------------------------------------------------------------------

class TestHasPendingSummary:
    """has_pending_summary only checks in-memory state (cheap check)."""

    def test_has_pending_only_checks_memory(self):
        """has_pending_summary does not read the checkpoint."""
        marker = _make_marker(summary="Checkpoint summary")
        agent = _make_agent(messages=[marker])

        assert agent._compaction.has_pending_summary("t1") is False

    def test_has_pending_true_for_in_memory(self):
        agent = _make_agent(messages=[])
        agent._compaction._pending_summaries["t1"] = "summary"

        assert agent._compaction.has_pending_summary("t1") is True
