"""Tests for the retained-turn compaction redesign (Phase 2a).

Compaction runs the real compact turn for its memory writes + summary, then
discards the turn and rebuilds the thread to a resume opener + authentic memory
read-back (no trailing assistant message, so a {"messages": []} re-drive
resumes the agent).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage

from nymeria.core import agent_memory_seed
from nymeria.core.agent_compaction import CompactionManager
from nymeria.core.agent_history import format_conversation_history


class _FakeCompactGraph:
    """Minimal async graph that simulates the add_messages reducer."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.checkpointer = SimpleNamespace(aget_tuple=self._aget_tuple)

    async def aget_state(self, config):
        return SimpleNamespace(
            values={"messages": list(self.messages)},
            config={"configurable": {"checkpoint_id": "cp1"}},
        )

    async def aupdate_state(self, config, payload):
        msgs = payload["messages"]
        remove_ids = {m.id for m in msgs if isinstance(m, RemoveMessage)}
        keep = [m for m in self.messages if m.id not in remove_ids]
        appended = [m for m in msgs if not isinstance(m, RemoveMessage)]
        self.messages = keep + appended

    async def _aget_tuple(self, config):
        return SimpleNamespace(checkpoint={"channel_versions": {}})


def _manager_with(messages, monkeypatch, *, summary="## Active Goal\nship"):
    graph = _FakeCompactGraph(messages)
    agent = SimpleNamespace(
        _default_async_graph=graph,
        _token_tracker=SimpleNamespace(reset_after_compact=lambda *a, **k: None),
    )
    manager = CompactionManager(agent)  # type: ignore[bad-argument-type]

    async def fake_summary(thread_id, user_id):
        return summary

    manager._generate_summary = fake_summary  # type: ignore[method-assign]
    monkeypatch.setattr(agent_memory_seed, "read_global_memory", lambda u, t: "GLOBAL-NOW")
    monkeypatch.setattr(agent_memory_seed, "read_thread_memory", lambda u, t: "THREAD-NOW")
    monkeypatch.setattr(
        "nymeria.core.agent_compaction.prune_checkpoints_before",
        lambda *a, **k: (0, 0, 0),
    )
    return manager, graph


def test_run_compact_turn_and_prune_builds_retained_tail(monkeypatch):
    pre = [HumanMessage(content=f"m{i}", id=f"m{i}") for i in range(6)]
    manager, graph = _manager_with(pre, monkeypatch, summary="## Active Goal\nship the thing")

    result = asyncio.run(
        manager._run_compact_turn_and_prune("t1", "u1", auto_resumed=True)
    )

    assert result["success"] is True
    # Counts are conversational (user turns + assistant replies), not raw
    # checkpoint objects: the 6 plain user messages count, the scaffolding-only
    # retained tail counts as 0.
    assert result["messages_before"] == 6
    assert result["messages_after"] == 0
    assert result["messages_removed"] == 6

    # State is exactly the retained tail: resume opener + read-back, no trailer.
    msgs = graph.messages
    assert len(msgs) == 4
    opener, ai_calls, tool_g, tool_t = msgs
    assert isinstance(opener, HumanMessage)
    assert opener.additional_kwargs["internal_type"] == "memory_seed_marker"
    assert opener.additional_kwargs["summary"] == "## Active Goal\nship the thing"
    assert opener.additional_kwargs["messages_removed"] == 6
    assert opener.additional_kwargs["auto_resumed"] is True
    assert "[Session resume]" in opener.content
    assert "ship the thing" in opener.content  # summary embedded inline

    assert isinstance(ai_calls, AIMessage) and ai_calls.tool_calls
    assert isinstance(tool_g, ToolMessage) and isinstance(tool_t, ToolMessage)
    assert {tool_g.content, tool_t.content} == {"GLOBAL-NOW", "THREAD-NOW"}
    # Ends on a ToolMessage (so a {"messages": []} re-drive resumes the agent).
    assert isinstance(msgs[-1], ToolMessage)
    # No dangling: tool_call ids are matched.
    assert {tc["id"] for tc in ai_calls.tool_calls} == {tool_g.tool_call_id, tool_t.tool_call_id}


def test_count_conversational_messages_excludes_tool_traffic_and_scaffolding():
    # A realistic checkpoint: user turns, an assistant tool-call step + its
    # result, assistant text replies, an autonomous wake-up + a prior
    # compaction's resume opener (both internal scaffolding), plus a
    # thinking-only assistant step. Only the 5 conversation messages count.
    msgs = [
        HumanMessage(content="hello", id="h1"),                                  # +1 user
        AIMessage(content="", id="a1", tool_calls=[                              # tool-call only
            {"id": "x", "name": "web_search", "args": {}, "type": "tool_call"},
        ]),
        ToolMessage(content="results...", tool_call_id="x", name="web_search", id="t1"),  # tool result
        AIMessage(content="Here are the results.", id="a2"),                     # +1 reply
        HumanMessage(content="thanks", id="h2"),                                 # +1 user
        HumanMessage(                                                           # autonomous wake-up
            content="[Trigger: ticker]", id="auto",
            additional_kwargs={"internal": True, "internal_type": "autonomous_wakeup"},
        ),
        AIMessage(content="Checked, nothing new.", id="a3"),                     # +1 reply
        HumanMessage(                                                           # prior resume opener
            content="[Session resume] ...", id="seed",
            additional_kwargs={"internal": True, "internal_type": "memory_seed_marker"},
        ),
        AIMessage(content=[                                                      # +1 reply (text block)
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Done."},
        ], id="a4"),
        AIMessage(content=[                                                      # thinking only
            {"type": "thinking", "thinking": "still pondering"},
        ], id="a5"),
    ]

    assert CompactionManager._count_conversational_messages(msgs) == 5
    # The raw object count (what the buggy notice used to report) is far higher.
    assert len(msgs) == 10


def test_run_compact_turn_and_prune_counts_only_conversation(monkeypatch):
    # Mixed pre-compaction state: 4 conversation messages buried among tool
    # traffic and internal scaffolding (7 raw objects). The notice must report 4.
    pre = [
        HumanMessage(content="hello", id="h1"),
        AIMessage(content="", id="a1", tool_calls=[
            {"id": "x", "name": "web_search", "args": {}, "type": "tool_call"},
        ]),
        ToolMessage(content="results...", tool_call_id="x", name="web_search", id="t1"),
        AIMessage(content="Here are the results.", id="a2"),
        HumanMessage(content="thanks", id="h2"),
        HumanMessage(
            content="[Trigger: ticker]", id="auto",
            additional_kwargs={"internal": True, "internal_type": "autonomous_wakeup"},
        ),
        AIMessage(content="Checked, nothing new.", id="a3"),
    ]
    manager, _graph = _manager_with(pre, monkeypatch)

    result = asyncio.run(
        manager._run_compact_turn_and_prune("t1", "u1", auto_resumed=False)
    )

    assert result["success"] is True
    assert result["messages_before"] == 4
    assert result["messages_after"] == 0
    assert result["messages_removed"] == 4


def test_run_compact_turn_and_prune_failure_discards_delta(monkeypatch):
    # Pre-turn messages, then the compaction turn "added" a couple messages that
    # must be dropped when summary generation fails.
    pre = [HumanMessage(content=f"m{i}", id=f"m{i}") for i in range(4)]
    manager, graph = _manager_with(pre, monkeypatch, summary="")

    # Simulate the compaction turn having appended a prompt + partial message.
    async def fake_summary_with_delta(thread_id, user_id):
        graph.messages = pre + [
            HumanMessage(content="compact prompt", id="cp"),
            AIMessage(content="partial", id="partial"),
        ]
        return None  # failed to produce a summary

    manager._generate_summary = fake_summary_with_delta  # type: ignore[method-assign]

    result = asyncio.run(
        manager._run_compact_turn_and_prune("t1", "u1", auto_resumed=False)
    )

    assert result["success"] is False
    # The delta (cp + partial) is removed; the original messages remain intact.
    assert [m.id for m in graph.messages] == ["m0", "m1", "m2", "m3"]


def test_memory_seed_marker_renders_as_compaction_notice():
    opener = HumanMessage(content="[Session resume] ... <summary> ...", id="seed-0")
    opener.additional_kwargs = {
        "internal": True,
        "internal_type": "memory_seed_marker",
        "summary": "## Active Goal\nship",
        "messages_removed": 9,
        "auto_resumed": True,
    }
    ai = AIMessage(content="", id="seed-1", tool_calls=[
        {"id": "g", "name": "memory_read", "args": {"scope": "global"}, "type": "tool_call"},
    ])
    tool = ToolMessage(content="GLOBAL", tool_call_id="g", name="memory_read", id="seed-2")
    user = HumanMessage(content="next question", id="u1")

    history = format_conversation_history([opener, ai, tool, user], thread_id="t-x")

    # The resume opener renders as the compaction notice; the read-back AI/Tool
    # are suppressed from the user view; the real user turn shows.
    notices = [e for e in history if e.get("kind") == "compaction_notice"]
    assert len(notices) == 1
    assert notices[0]["context_summary"] == "## Active Goal\nship"
    assert notices[0]["messages_removed"] == 9
    assert not any(e["role"] == "assistant" for e in history)
    assert [e for e in history if e["role"] == "user"][0]["content"] == "next question"
