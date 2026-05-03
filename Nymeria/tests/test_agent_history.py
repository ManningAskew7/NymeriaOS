"""Focused tests for agent history projection helpers."""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from nymeria.core.agent_history import (
    TIMESTAMP_SCAN_LIMIT,
    build_message_timestamp_map,
    extract_timestamp,
    format_conversation_history,
)


class FakeHistoryGraph:
    def __init__(self, states):
        self.states = states
        self.last_config = None
        self.last_limit = None

    def get_state_history(self, config, limit=None):
        self.last_config = config
        self.last_limit = limit
        return iter(self.states)


def _state(created_at: str, messages):
    return SimpleNamespace(created_at=created_at, values={"messages": messages})


def test_extract_timestamp_from_time_context_prefix():
    text = (
        "[Time: Sunday, May 03, 2026 at 04:05 PM (UTC)]\n"
        "[Trigger: manual]\n\nHello"
    )

    assert extract_timestamp(text) == "2026-05-03T16:05:00+00:00"


def test_build_message_timestamp_map_uses_earliest_checkpoint_in_scan_window():
    user = HumanMessage(content="User", id="user-1")
    assistant = AIMessage(content="Assistant", id="assistant-1")
    graph = FakeHistoryGraph([
        _state("2026-05-03T10:02:00Z", [user, assistant]),
        _state("2026-05-03T10:01:00Z", [user]),
        _state("2026-05-03T10:00:00Z", []),
    ])

    timestamps = build_message_timestamp_map(
        graph,
        "thread-a",
        {"user-1", "assistant-1"},
    )

    assert graph.last_config == {"configurable": {"thread_id": "thread-a"}}
    assert graph.last_limit == TIMESTAMP_SCAN_LIMIT
    assert timestamps == {
        "user-1": "2026-05-03T10:01:00Z",
        "assistant-1": "2026-05-03T10:02:00Z",
    }


def test_format_conversation_history_attaches_tool_results_and_artifacts():
    messages = [
        HumanMessage(
            content=(
                "[Current Time: Sunday, May 03, 2026 at 04:05 PM (UTC)]\n"
                "[Trigger: manual]\n\nFind the file"
            ),
            id="user-1",
        ),
        AIMessage(
            content="",
            tool_calls=[{
                "id": "call-1",
                "name": "workspace_lookup",
                "args": {"path": "report.txt"},
            }],
            id="assistant-1",
        ),
        ToolMessage(
            content="Found report [attach:/workspace/report.txt]",
            tool_call_id="call-1",
        ),
        AIMessage(content="Done", id="assistant-2"),
    ]

    history = format_conversation_history(
        messages,
        thread_id="thread-a",
        timestamp_map={"user-1": "2026-05-03T16:05:00+00:00"},
        clean_tool_result=lambda value: value.replace(
            "[attach:/workspace/report.txt]", ""
        ).strip(),
        extract_workspace_artifacts=lambda value: [
            {
                "path": "/workspace/report.txt",
                "name": "report.txt",
                "mime_type": "text/plain",
                "size_bytes": 12,
            }
        ] if "[attach:/workspace/report.txt]" in value else [],
    )

    assert history[0] == {
        "id": "thread-a-1",
        "role": "user",
        "content": "Find the file",
        "timestamp": "2026-05-03T16:05:00+00:00",
    }

    assistant = history[1]
    assert assistant["content"] == "Done"
    assert assistant["steps"] == [
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "workspace_lookup",
            "arguments": {"path": "report.txt"},
            "status": "success",
            "result": "Found report",
            "artifacts": [{
                "path": "/workspace/report.txt",
                "name": "report.txt",
                "mime_type": "text/plain",
                "size_bytes": 12,
            }],
        },
        {"type": "response", "content": "Done"},
    ]
