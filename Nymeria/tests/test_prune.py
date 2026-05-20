"""Tests for PruneManager - the /prune command's deterministic tool-result compression."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from nymeria.core.agent_prune import (
    PRUNED_INTERNAL_TYPE,
    SKIP_BELOW_CHARS,
    PruneManager,
    _detect_status,
    _format_marker,
)


class _FakeAsyncGraph:
    """Captures aget_state / aupdate_state for assertions."""

    def __init__(self, messages: List[Any]):
        self._messages = list(messages)
        self.updates: List[dict] = []

    async def aget_state(self, config):
        return SimpleNamespace(values={"messages": list(self._messages)})

    async def aupdate_state(self, config, payload):
        self.updates.append(payload)
        by_id = {getattr(m, "id", None): m for m in payload.get("messages", [])}
        self._messages = [by_id.get(m.id, m) for m in self._messages]


def _make_manager(messages: List[Any]) -> tuple[PruneManager, _FakeAsyncGraph]:
    graph = _FakeAsyncGraph(messages)
    agent = SimpleNamespace(_default_async_graph=graph)
    mgr = PruneManager(agent)
    return mgr, graph


def _big_content(n: int = 1000) -> str:
    return "x" * n


class TestFormatMarker:
    def test_includes_size_and_status(self):
        marker = _format_marker(4523, "success")
        assert "4523" in marker
        assert "success" in marker
        assert "/prune" in marker
        assert "Call this tool again" in marker

    def test_error_status(self):
        marker = _format_marker(87, "error")
        assert "87" in marker
        assert "error" in marker


class TestDetectStatus:
    def test_status_field_error(self):
        msg = ToolMessage(content="anything", tool_call_id="tc1", status="error")
        assert _detect_status(msg, "anything") == "error"

    def test_content_error_prefix(self):
        msg = ToolMessage(content="[Error]: boom", tool_call_id="tc1")
        assert _detect_status(msg, "[Error]: boom") == "error"

    def test_success_default(self):
        msg = ToolMessage(content="ok result", tool_call_id="tc1")
        assert _detect_status(msg, "ok result") == "success"


class TestPruneNow:
    @pytest.mark.asyncio
    async def test_rewrites_large_tool_message(self):
        big = _big_content(800)
        messages = [
            HumanMessage(content="hi", id="h1"),
            AIMessage(content="ok", id="a1"),
            ToolMessage(content=big, tool_call_id="tc1", name="gmail_search", id="t1"),
        ]
        mgr, graph = _make_manager(messages)
        result = await mgr.prune_now("thread-x")
        assert result["success"] is True
        assert result["pruned_count"] == 1
        assert result["chars_before"] == 800
        assert result["chars_after"] < 800
        assert result["chars_saved"] == 800 - result["chars_after"]

        replaced = [m for m in graph._messages if m.id == "t1"][0]
        assert "/prune" in replaced.content
        assert "800" in replaced.content
        assert replaced.tool_call_id == "tc1"
        assert replaced.name == "gmail_search"
        assert replaced.id == "t1"

    @pytest.mark.asyncio
    async def test_sets_internal_type_marker(self):
        messages = [
            ToolMessage(content=_big_content(500), tool_call_id="tc1", id="t1"),
        ]
        mgr, graph = _make_manager(messages)
        await mgr.prune_now("t")
        replaced = graph._messages[0]
        assert replaced.additional_kwargs["internal_type"] == PRUNED_INTERNAL_TYPE
        assert replaced.additional_kwargs["original_chars"] == 500
        assert replaced.additional_kwargs["original_status"] == "success"
        assert "pruned_at" in replaced.additional_kwargs

    @pytest.mark.asyncio
    async def test_skips_already_pruned(self):
        already = ToolMessage(
            content="[/prune placeholder ...]",
            tool_call_id="tc1",
            id="t1",
        )
        already.additional_kwargs["internal_type"] = PRUNED_INTERNAL_TYPE
        messages = [already]
        mgr, graph = _make_manager(messages)
        result = await mgr.prune_now("t")
        assert result["pruned_count"] == 0
        assert result["skipped_already_pruned"] == 1
        assert graph.updates == []

    @pytest.mark.asyncio
    async def test_skips_short_results(self):
        messages = [
            ToolMessage(content="x" * SKIP_BELOW_CHARS, tool_call_id="tc1", id="t1"),
            ToolMessage(content="x" * (SKIP_BELOW_CHARS + 1), tool_call_id="tc2", id="t2"),
        ]
        mgr, graph = _make_manager(messages)
        result = await mgr.prune_now("t")
        assert result["pruned_count"] == 1
        assert result["skipped_too_short"] == 1
        replaced_ids = [m.id for m in graph.updates[0]["messages"]]
        assert replaced_ids == ["t2"]

    @pytest.mark.asyncio
    async def test_detects_error_via_status_field(self):
        messages = [
            ToolMessage(
                content="x" * 500, tool_call_id="tc1", id="t1", status="error",
            ),
        ]
        mgr, graph = _make_manager(messages)
        await mgr.prune_now("t")
        replaced = graph._messages[0]
        assert replaced.additional_kwargs["original_status"] == "error"
        assert "error" in replaced.content

    @pytest.mark.asyncio
    async def test_detects_error_via_content_prefix(self):
        messages = [
            ToolMessage(content="[Error]: " + "x" * 500, tool_call_id="tc1", id="t1"),
        ]
        mgr, graph = _make_manager(messages)
        await mgr.prune_now("t")
        replaced = graph._messages[0]
        assert replaced.additional_kwargs["original_status"] == "error"

    @pytest.mark.asyncio
    async def test_empty_state(self):
        mgr, graph = _make_manager([])
        result = await mgr.prune_now("t")
        assert result["success"] is True
        assert result["pruned_count"] == 0
        assert graph.updates == []

    @pytest.mark.asyncio
    async def test_no_tool_messages(self):
        messages = [
            HumanMessage(content="hi", id="h1"),
            AIMessage(content="ok", id="a1"),
        ]
        mgr, graph = _make_manager(messages)
        result = await mgr.prune_now("t")
        assert result["success"] is True
        assert result["pruned_count"] == 0
        assert graph.updates == []

    @pytest.mark.asyncio
    async def test_idempotent_second_call(self):
        messages = [
            ToolMessage(content=_big_content(800), tool_call_id="tc1", id="t1"),
        ]
        mgr, graph = _make_manager(messages)
        first = await mgr.prune_now("t")
        assert first["pruned_count"] == 1
        second = await mgr.prune_now("t")
        assert second["pruned_count"] == 0
        assert second["skipped_already_pruned"] == 1
        assert len(graph.updates) == 1

    @pytest.mark.asyncio
    async def test_preserves_other_additional_kwargs(self):
        msg = ToolMessage(content=_big_content(500), tool_call_id="tc1", id="t1")
        msg.additional_kwargs["existing_key"] = "preserved"
        mgr, graph = _make_manager([msg])
        await mgr.prune_now("t")
        replaced = graph._messages[0]
        assert replaced.additional_kwargs["existing_key"] == "preserved"
        assert replaced.additional_kwargs["internal_type"] == PRUNED_INTERNAL_TYPE

    @pytest.mark.asyncio
    async def test_multiple_tool_messages_all_pruned(self):
        messages = [
            ToolMessage(content=_big_content(400), tool_call_id="tc1", id="t1"),
            AIMessage(content="thinking", id="a1"),
            ToolMessage(content=_big_content(900), tool_call_id="tc2", id="t2"),
            HumanMessage(content="more", id="h1"),
            ToolMessage(content=_big_content(700), tool_call_id="tc3", id="t3"),
        ]
        mgr, graph = _make_manager(messages)
        result = await mgr.prune_now("t")
        assert result["pruned_count"] == 3
        assert result["chars_before"] == 400 + 900 + 700
        replaced_ids = {m.id for m in graph.updates[0]["messages"]}
        assert replaced_ids == {"t1", "t2", "t3"}

    @pytest.mark.asyncio
    async def test_aget_state_failure_returns_error(self):
        class _BrokenGraph:
            async def aget_state(self, config):
                raise RuntimeError("db down")

        agent = SimpleNamespace(_default_async_graph=_BrokenGraph())
        mgr = PruneManager(agent)
        result = await mgr.prune_now("t")
        assert result["success"] is False
        assert "db down" in result["reason"]

    @pytest.mark.asyncio
    async def test_aupdate_state_failure_returns_error(self):
        class _BrokenGraph:
            async def aget_state(self, config):
                return SimpleNamespace(values={"messages": [
                    ToolMessage(content=_big_content(500), tool_call_id="tc1", id="t1"),
                ]})

            async def aupdate_state(self, config, payload):
                raise RuntimeError("write failed")

        agent = SimpleNamespace(_default_async_graph=_BrokenGraph())
        mgr = PruneManager(agent)
        result = await mgr.prune_now("t")
        assert result["success"] is False
        assert "write failed" in result["reason"]
