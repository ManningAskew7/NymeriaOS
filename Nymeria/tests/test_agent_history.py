"""Focused tests for agent history projection helpers."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from nymeria.core import agent_history as agent_history_module
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


def test_history_round_trip_uses_additional_kwargs_attachment_metadata():
    """Phase B+ attachment metadata on HumanMessage.additional_kwargs should
    drive history reconstruction, preserving original filename + byte size
    that the legacy content-block synthesis path would have lost."""
    user = HumanMessage(
        content=[
            {"type": "text", "text": "summarise the doc"},
        ],
        id="user-1",
        additional_kwargs={
            "attachments": [
                {
                    "id": "rec-uuid-1",
                    "type": "document",
                    "name": "Q3-financials.pdf",
                    "size": 184_232,
                    "mime_type": "application/pdf",
                    "sandbox_path": "/workspace/threads/t/attachments/Q3-financials.pdf",
                    "extracted_text_path": "/workspace/threads/t/attachments/Q3-financials.pdf.txt",
                    "pages": 12,
                    "sha256": "deadbeef",
                },
            ]
        },
    )

    history = format_conversation_history(
        [user],
        thread_id="t",
        timestamp_map={},
        clean_tool_result=lambda value: value,
        extract_workspace_artifacts=lambda _value: [],
    )

    assert len(history) == 1
    entry = history[0]
    assert entry["role"] == "user"
    assert entry["content"] == "summarise the doc"
    [attachment] = entry["attachments"]
    assert attachment["id"] == "rec-uuid-1"
    assert attachment["type"] == "document"
    assert attachment["name"] == "Q3-financials.pdf"
    assert attachment["size"] == 184_232
    assert attachment["mimeType"] == "application/pdf"
    # Sandbox-routed documents do not carry inline bytes; frontend fetches
    # via the owner-scoped download endpoint instead.
    assert attachment["dataUrl"] == ""


def test_history_round_trip_uses_metadata_for_image_with_inline_data():
    """Image metadata sits alongside an inline image_url content block; the
    rebuild should preserve the real filename and surface the data URL."""
    user = HumanMessage(
        content=[
            {"type": "text", "text": "describe"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,IMG"},
            },
        ],
        id="user-1",
        additional_kwargs={
            "attachments": [
                {
                    "type": "image",
                    "name": "screenshot.png",
                    "size": 4096,
                    "mime_type": "image/png",
                    "data_url": "data:image/png;base64,IMG",
                },
            ]
        },
    )

    history = format_conversation_history(
        [user],
        thread_id="t",
        timestamp_map={},
        clean_tool_result=lambda value: value,
        extract_workspace_artifacts=lambda _value: [],
    )

    [attachment] = history[0]["attachments"]
    assert attachment["type"] == "image"
    assert attachment["name"] == "screenshot.png"
    assert attachment["size"] == 4096
    assert attachment["mimeType"] == "image/png"
    assert attachment["dataUrl"] == "data:image/png;base64,IMG"


def test_history_round_trip_legacy_message_synthesises_attachment():
    """Messages without additional_kwargs attachments metadata fall back to
    the legacy synthesis path so older history still renders pills."""
    user = HumanMessage(
        content=[
            {"type": "text", "text": "describe"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64,LEGACY"},
            },
        ],
        id="user-1",
    )

    history = format_conversation_history(
        [user],
        thread_id="t",
        timestamp_map={},
        clean_tool_result=lambda value: value,
        extract_workspace_artifacts=lambda _value: [],
    )

    [attachment] = history[0]["attachments"]
    assert attachment["type"] == "image"
    assert attachment["mimeType"] == "image/jpeg"
    # Legacy path synthesizes a filename from the MIME extension.
    assert attachment["name"].endswith(".jpeg")


def test_format_conversation_history_uses_message_type_dispatch_table():
    source = inspect.getsource(format_conversation_history)

    assert "isinstance(msg, HumanMessage)" not in source
    assert "isinstance(msg, AIMessage)" not in source
    assert "isinstance(msg, SystemMessage)" not in source
    assert agent_history_module._HISTORY_MESSAGE_HANDLERS.keys() >= {
        HumanMessage,
        AIMessage,
        SystemMessage,
    }

    history = format_conversation_history(
        [
            SystemMessage(content="System note"),
            HumanMessage(content="Hello"),
            AIMessage(content="Hi"),
        ],
        thread_id="thread-a",
    )

    assert history == [
        {"id": "thread-a-1", "role": "system", "content": "System note"},
        {"id": "thread-a-2", "role": "user", "content": "Hello"},
        {"id": "thread-a-3", "role": "assistant", "content": "Hi"},
    ]


def test_compaction_marker_with_legacy_repr_summary_is_normalized():
    """Pre-fix compactions stored `str(content)` of an Anthropic content-block
    list; the history projection must parse those back to plain markdown."""
    legacy_summary = str([
        {"type": "text", "text": "## Active Goal\nFirst block."},
        {"type": "text", "text": "## Progress\nSecond block."},
    ])
    assert legacy_summary.startswith("[{")

    marker = HumanMessage(content="Context compacted", id="m1")
    marker.additional_kwargs = {
        "internal_type": "compaction_marker",
        "summary": legacy_summary,
        "messages_removed": 12,
        "auto_resumed": False,
        "timestamp": "2026-05-24T08:37:00+00:00",
    }

    history = format_conversation_history([marker], thread_id="thread-a")

    assert len(history) == 1
    entry = history[0]
    assert entry["kind"] == "compaction_notice"
    assert "[{" not in entry["context_summary"]
    assert "## Active Goal\nFirst block." in entry["context_summary"]
    assert "## Progress\nSecond block." in entry["context_summary"]


def test_compaction_marker_with_plain_string_summary_is_passed_through():
    marker = HumanMessage(content="Context compacted", id="m1")
    marker.additional_kwargs = {
        "internal_type": "compaction_marker",
        "summary": "## Active Goal\nPlain markdown summary.",
        "messages_removed": 5,
        "auto_resumed": True,
    }

    history = format_conversation_history([marker], thread_id="thread-b")

    assert history[0]["context_summary"] == "## Active Goal\nPlain markdown summary."
