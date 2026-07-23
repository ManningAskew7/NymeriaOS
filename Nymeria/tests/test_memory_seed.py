"""Unit tests for the shared memory-seed builder and read helpers."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from nymeria.core import agent_memory_seed
from nymeria.core.agent_memory_seed import (
    MEMORY_INIT_OPENER,
    MEMORY_INIT_TRAILING,
    MEMORY_INIT_TYPE,
    MEMORY_SEED_MARKER_TYPE,
    build_init_seed_exchange,
    build_memory_exchange,
    build_resume_compaction_tail,
    read_global_memory,
    read_thread_memory,
)


class _FakeTool:
    """Stand-in for the memory_read BaseTool exposing .invoke()."""

    def __init__(self, *, result=None, exc=None):
        self._result = result
        self._exc = exc
        self.calls = []

    def invoke(self, args, config=None):
        self.calls.append((args, config))
        if self._exc is not None:
            raise self._exc
        return self._result


def test_build_memory_exchange_pairs_tool_calls_and_results():
    msgs = build_memory_exchange(
        opener_internal_type=MEMORY_INIT_TYPE,
        opener_text="opener",
        global_text="GLOBAL",
        thread_text="THREAD",
        trailing_text="done",
    )

    assert len(msgs) == 5
    opener, ai_calls, tool_global, tool_thread, trailing = msgs

    assert isinstance(opener, HumanMessage)
    assert opener.additional_kwargs == {"internal": True, "internal_type": MEMORY_INIT_TYPE}

    assert isinstance(ai_calls, AIMessage)
    assert ai_calls.content == ""  # no thinking blocks -> safe past Anthropic sanitizer
    call_ids = {tc["id"] for tc in ai_calls.tool_calls}
    scopes = {tc["args"]["scope"] for tc in ai_calls.tool_calls}
    assert scopes == {"global", "thread"}

    assert isinstance(tool_global, ToolMessage)
    assert isinstance(tool_thread, ToolMessage)
    result_ids = {tool_global.tool_call_id, tool_thread.tool_call_id}
    assert call_ids == result_ids  # every tool_call has a matching ToolMessage

    # content maps to the right scope
    by_id = {tc["id"]: tc["args"]["scope"] for tc in ai_calls.tool_calls}
    contents = {by_id[tool_global.tool_call_id]: tool_global.content,
                by_id[tool_thread.tool_call_id]: tool_thread.content}
    assert contents == {"global": "GLOBAL", "thread": "THREAD"}

    assert isinstance(trailing, AIMessage)
    assert trailing.content == "done"
    assert not trailing.tool_calls  # never end on an open tool-calls AIMessage


def test_build_memory_exchange_without_trailing_ends_on_tool_messages():
    msgs = build_memory_exchange(
        opener_internal_type=MEMORY_SEED_MARKER_TYPE,
        opener_text="opener",
        global_text="G",
        thread_text="T",
    )
    assert len(msgs) == 4
    assert isinstance(msgs[-1], ToolMessage)


def test_build_memory_exchange_unique_ids():
    msgs = build_memory_exchange(
        opener_internal_type=MEMORY_INIT_TYPE,
        opener_text="o",
        global_text="g",
        thread_text="t",
        trailing_text="x",
    )
    ids = [m.id for m in msgs]
    assert all(ids)
    assert len(set(ids)) == len(ids)


def test_read_helpers_return_tool_output(monkeypatch):
    fake = _FakeTool(result="memory listing")
    monkeypatch.setattr("nymeria.tools.memory.memory_read", fake)

    assert read_global_memory("u1", "t1") == "memory listing"
    assert read_thread_memory("u1", "t1") == "memory listing"

    # invoked with scope + config carrying user_id/thread_id
    (args, config), _ = fake.calls[0], None
    assert args == {"scope": "global"}
    assert config == {"configurable": {"thread_id": "t1", "user_id": "u1"}}
    assert fake.calls[1][0] == {"scope": "thread"}


def test_read_helpers_fall_back_on_error(monkeypatch):
    monkeypatch.setattr(
        "nymeria.tools.memory.memory_read",
        _FakeTool(exc=RuntimeError("boom")),
    )
    assert read_global_memory("u1", "t1") == "[empty]"
    assert read_thread_memory("u1", "t1") == "[empty]"


def test_build_init_seed_exchange_uses_real_content(monkeypatch):
    monkeypatch.setattr(agent_memory_seed, "read_global_memory", lambda u, t: "G-real")
    monkeypatch.setattr(agent_memory_seed, "read_thread_memory", lambda u, t: "T-real")

    msgs = build_init_seed_exchange("u1", "t1")

    assert msgs[0].content == MEMORY_INIT_OPENER
    assert msgs[0].additional_kwargs["internal_type"] == MEMORY_INIT_TYPE
    assert msgs[2].content == "G-real"
    assert msgs[3].content == "T-real"
    assert msgs[-1].content == MEMORY_INIT_TRAILING


def test_build_resume_compaction_tail_shape(monkeypatch):
    monkeypatch.setattr(agent_memory_seed, "read_global_memory", lambda u, t: "G-now")
    monkeypatch.setattr(agent_memory_seed, "read_thread_memory", lambda u, t: "T-now")

    tail = build_resume_compaction_tail(
        user_id="u1", thread_id="t1", summary="## Active Goal\nship it"
    )

    # No trailing assistant message: ends on the thread ToolMessage so a
    # {"messages": []} re-drive resumes the agent from the read-back.
    assert len(tail) == 4
    assert isinstance(tail[-1], ToolMessage)

    opener = tail[0]
    assert isinstance(opener, HumanMessage)
    assert opener.additional_kwargs["internal_type"] == MEMORY_SEED_MARKER_TYPE
    assert "[Session resume]" in opener.content
    assert "## Active Goal\nship it" in opener.content  # summary embedded inline

    # authentic post-edit memory content carried in the read-back
    assert tail[2].content == "G-now"
    assert tail[3].content == "T-now"
    # matched tool-call ids (no dangling)
    call_ids = {tc["id"] for tc in tail[1].tool_calls}
    assert call_ids == {tail[2].tool_call_id, tail[3].tool_call_id}


def test_build_memory_exchange_with_team_text_adds_third_pair():
    """Teamed threads carry a third scope="team" read (backlog #100 phase 3)."""
    msgs = build_memory_exchange(
        opener_internal_type=MEMORY_INIT_TYPE,
        opener_text="opener",
        global_text="G",
        thread_text="T",
        team_text="TEAM",
        trailing_text="done",
    )
    assert len(msgs) == 6
    ai_calls = msgs[1]
    scopes = {tc["args"]["scope"] for tc in ai_calls.tool_calls}
    assert scopes == {"global", "thread", "team"}
    call_ids = {tc["id"] for tc in ai_calls.tool_calls}
    tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
    assert call_ids == {m.tool_call_id for m in tool_msgs}
    by_id = {tc["id"]: tc["args"]["scope"] for tc in ai_calls.tool_calls}
    contents = {by_id[m.tool_call_id]: m.content for m in tool_msgs}
    assert contents == {"global": "G", "thread": "T", "team": "TEAM"}


def test_read_team_memory_only_when_teamed(monkeypatch):
    fake = _FakeTool(result="TEAM listing")
    monkeypatch.setattr("nymeria.tools.memory.memory_read", fake)

    monkeypatch.setattr("nymeria.tools.memory.acting_team_id", lambda config: None)
    assert agent_memory_seed.read_team_memory("u1", "t1") is None
    assert fake.calls == []  # unteamed: the read never fires

    monkeypatch.setattr(
        "nymeria.tools.memory.acting_team_id", lambda config: "team-1"
    )
    assert agent_memory_seed.read_team_memory("u1", "t1") == "TEAM listing"
    assert fake.calls[0][0] == {"scope": "team"}


def test_read_team_memory_probe_failure_degrades_to_none(monkeypatch):
    def boom(config):
        raise RuntimeError("probe down")

    monkeypatch.setattr("nymeria.tools.memory.acting_team_id", boom)
    assert agent_memory_seed.read_team_memory("u1", "t1") is None


def test_seed_and_resume_tail_include_team_read_iff_teamed(monkeypatch):
    monkeypatch.setattr(agent_memory_seed, "read_global_memory", lambda u, t: "G")
    monkeypatch.setattr(agent_memory_seed, "read_thread_memory", lambda u, t: "T")

    monkeypatch.setattr(
        agent_memory_seed, "read_team_memory", lambda u, t: "TEAM-real"
    )
    seed = build_init_seed_exchange("u1", "t1")
    assert len(seed) == 6
    assert seed[4].content == "TEAM-real"
    assert seed[-1].content == MEMORY_INIT_TRAILING
    tail = build_resume_compaction_tail(user_id="u1", thread_id="t1", summary="s")
    assert len(tail) == 5
    assert isinstance(tail[-1], ToolMessage)
    assert tail[-1].content == "TEAM-real"

    monkeypatch.setattr(agent_memory_seed, "read_team_memory", lambda u, t: None)
    assert len(build_init_seed_exchange("u1", "t1")) == 5
    assert len(build_resume_compaction_tail(user_id="u1", thread_id="t1", summary="s")) == 4
