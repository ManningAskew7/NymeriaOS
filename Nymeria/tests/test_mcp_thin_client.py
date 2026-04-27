"""Tests for the Nymeria MCP thin-client helpers."""

from __future__ import annotations

import asyncio

from nymeria.mcp_backend_client import (
    ChatTranscript,
    NymeriaBackendClient,
    collect_chat_transcript,
    message_steps_to_response_text,
)
from nymeria.mcp_server import _collection_result


def test_chat_transcript_preserves_thinking_preamble_tool_result_and_final_response():
    transcript = ChatTranscript(thread_id="thread-1")
    for event in [
        {"type": "thinking", "content": "Need to inspect state.", "thread_id": "thread-1"},
        {"type": "response", "content": "I will check the TODO list first.", "thread_id": "thread-1"},
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "todo_list",
            "args": {"filter_status": "all"},
            "thread_id": "thread-1",
        },
        {
            "type": "tool_result",
            "id": "call-1",
            "name": "todo_list",
            "result": "No active TODOs.",
            "thread_id": "thread-1",
        },
        {"type": "response", "content": "There are no active TODOs.", "thread_id": "thread-1"},
        {"type": "done", "model": "claude-test", "context_stats": {"total_tokens": 42}, "thread_id": "thread-1"},
    ]:
        transcript.add_event(event)

    result = transcript.as_dict()

    assert result["thread_id"] == "thread-1"
    assert result["final_response"] == "There are no active TODOs."
    assert result["model"] == "claude-test"
    assert result["context_stats"] == {"total_tokens": 42}
    assert "> *Thinking:*" in result["full_markdown"]
    assert "I will check the TODO list first." in result["full_markdown"]
    assert "### Tool: todo_list" in result["full_markdown"]
    assert '"filter_status": "all"' in result["full_markdown"]
    assert "No active TODOs." in result["full_markdown"]


def test_workspace_artifacts_attach_to_matching_tool_call():
    transcript = ChatTranscript()
    transcript.add_event({"type": "tool_call", "id": "call-1", "name": "write_file", "args": {"path": "x.txt"}})
    transcript.add_event(
        {
            "type": "workspace_artifact",
            "tool_call_id": "call-1",
            "tool_name": "write_file",
            "path": "/workspace/x.txt",
            "name": "x.txt",
            "mime_type": "text/plain",
        }
    )

    assert transcript.steps[0]["artifacts"] == [
        {"path": "/workspace/x.txt", "name": "x.txt", "mime_type": "text/plain"}
    ]


def test_response_text_matches_desktop_trailing_response_rule():
    steps = [
        {"type": "response", "content": "Preamble"},
        {"type": "tool_call", "name": "search", "arguments": {}},
        {"type": "response", "content": "Final"},
    ]

    assert message_steps_to_response_text(steps) == "Final"


class FakeChatClient:
    async def stream_chat(self, **_kwargs):
        yield {"type": "thinking", "content": "Think", "thread_id": "thread-2"}
        yield {"type": "response", "content": "Answer", "thread_id": "thread-2"}
        yield {"type": "done", "thread_id": "thread-2"}

    async def get(self, *_args, **_kwargs):
        return {"messages": []}


def test_collect_chat_transcript_can_include_raw_events():
    async def _run():
        return await collect_chat_transcript(
            FakeChatClient(),
            message="hello",
            user_id="default",
            include_events=True,
        )

    result = asyncio.run(_run())

    assert result["thread_id"] == "thread-2"
    assert result["final_response"] == "Answer"
    assert [event["type"] for event in result["events"]] == ["thinking", "response", "done"]


class FakeChatClientWithHistory:
    async def stream_chat(self, **_kwargs):
        yield {"type": "tool_call", "id": "call-b", "name": "second_tool", "args": {}, "thread_id": "thread-3"}
        yield {"type": "tool_call", "id": "call-a", "name": "first_tool", "args": {}, "thread_id": "thread-3"}
        yield {"type": "response", "content": "stream final", "thread_id": "thread-3"}
        yield {"type": "done", "thread_id": "thread-3"}

    async def get(self, *_args, **_kwargs):
        return {
            "messages": [
                {"id": "user-1", "role": "user", "content": "hello"},
                {
                    "id": "assistant-1",
                    "role": "assistant",
                    "steps": [
                        {"type": "tool_call", "id": "call-a", "name": "first_tool", "arguments": {}},
                        {"type": "tool_call", "id": "call-b", "name": "second_tool", "arguments": {}},
                        {"type": "response", "content": "persisted final"},
                    ],
                }
            ]
        }


def test_collect_chat_transcript_prefers_persisted_step_order_for_copy_output():
    async def _run():
        return await collect_chat_transcript(
            FakeChatClientWithHistory(),
            message="hello",
            user_id="default",
            include_events=True,
        )

    result = asyncio.run(_run())

    assert result["history_message_id"] == "assistant-1"
    assert [step["name"] for step in result["steps"][:2]] == ["first_tool", "second_tool"]
    assert result["final_response"] == "persisted final"
    assert [event["name"] for event in result["events"][:2]] == ["second_tool", "first_tool"]


class FakeChatClientWithStaleHistory:
    async def stream_chat(self, **_kwargs):
        yield {"type": "response", "content": "fresh final", "thread_id": "thread-4"}
        yield {"type": "done", "thread_id": "thread-4"}

    async def get(self, *_args, **_kwargs):
        return {
            "messages": [
                {"id": "old-user", "role": "user", "content": "old prompt"},
                {
                    "id": "old-assistant",
                    "role": "assistant",
                    "steps": [{"type": "response", "content": "stale final"}],
                },
            ]
        }


def test_collect_chat_transcript_ignores_stale_persisted_history():
    async def _run():
        return await collect_chat_transcript(
            FakeChatClientWithStaleHistory(),
            message="new prompt",
            user_id="default",
        )

    result = asyncio.run(_run())

    assert result["final_response"] == "fresh final"
    assert "history_message_id" not in result


def test_backend_client_headers_include_service_token_and_act_as():
    client = NymeriaBackendClient("http://api:8000", "nym_service")

    headers = client._headers(act_as="default", accept="text/event-stream")

    assert headers["Authorization"] == "Bearer nym_service"
    assert headers["X-Nymeria-Act-As"] == "default"
    assert headers["Accept"] == "text/event-stream"
    assert headers["X-Nymeria-Client-Id"] == "nymeria-mcp"


def test_collection_result_wraps_empty_and_non_empty_lists():
    assert _collection_result("triggers", [], thread_id="thread-1") == {
        "triggers": [],
        "total": 0,
        "thread_id": "thread-1",
    }

    result = _collection_result("executions", [{"id": "exec-1"}])

    assert result == {"executions": [{"id": "exec-1"}], "total": 1}
