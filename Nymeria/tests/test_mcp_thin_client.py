"""Tests for the Nymeria MCP thin-client helpers."""

from __future__ import annotations

import asyncio
from typing import cast

from nymeria.mcp_backend_client import (
    ChatTranscript,
    NymeriaBackendClient,
    collect_chat_transcript,
    message_steps_to_response_text,
    project_history_message_for_verbosity,
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


def test_chat_transcript_concise_redacts_tool_payloads_but_keeps_flow():
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
        transcript.add_event(event, keep_raw=True)

    result = transcript.as_dict(include_events=True, verbosity="concise")

    assert result["verbosity"] == "concise"
    assert result["model"] == "claude-test"
    assert result["context_stats"] == {"total_tokens": 42}
    assert result["final_response"] == "There are no active TODOs."
    assert result["steps"] == [
        {"type": "thinking", "content": "Need to inspect state."},
        {"type": "response", "content": "I will check the TODO list first."},
        {"type": "tool_call", "name": "todo_list", "status": "success"},
        {"type": "response", "content": "There are no active TODOs."},
    ]
    assert "filter_status" not in result["full_markdown"]
    assert "No active TODOs." not in result["full_markdown"]
    assert "events" not in result


def test_chat_transcript_chat_returns_only_final_conversation_text():
    transcript = ChatTranscript(thread_id="thread-1")
    for event in [
        {"type": "response", "content": "Preamble before a tool.", "thread_id": "thread-1"},
        {"type": "tool_call", "id": "call-1", "name": "search", "args": {"q": "secret"}},
        {"type": "tool_result", "id": "call-1", "name": "search", "result": "secret result"},
        {"type": "response", "content": "Final answer.", "thread_id": "thread-1"},
        {"type": "done", "thread_id": "thread-1"},
    ]:
        transcript.add_event(event, keep_raw=True)

    result = transcript.as_dict(include_events=True, verbosity="chat")

    assert result == {
        "thread_id": "thread-1",
        "final_response": "Final answer.",
        "errors": [],
        "done": True,
        "verbosity": "chat",
    }


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


def test_history_message_projection_redacts_concise_and_trims_chat():
    message = {
        "id": "assistant-1",
        "role": "assistant",
        "content": "Final",
        "timestamp": "2026-04-28T00:00:00Z",
        "steps": [
            {"type": "thinking", "content": "Think"},
            {"type": "response", "content": "Preamble"},
            {
                "type": "tool_call",
                "id": "call-1",
                "name": "search",
                "arguments": {"q": "secret"},
                "result": "secret result",
                "status": "success",
                "artifacts": [{"path": "/tmp/secret"}],
            },
            {"type": "response", "content": "Final"},
        ],
        "tool_calls": [{"name": "search", "arguments": {"q": "secret"}, "result": "secret result"}],
    }

    concise = project_history_message_for_verbosity(message, "concise")
    chat = project_history_message_for_verbosity(message, "chat")

    assert concise["steps"] == [
        {"type": "thinking", "content": "Think"},
        {"type": "response", "content": "Preamble"},
        {"type": "tool_call", "name": "search", "status": "success"},
        {"type": "response", "content": "Final"},
    ]
    assert concise["tool_calls"] == [{"type": "tool_call", "name": "search", "status": "success"}]
    assert "secret" not in concise["full_markdown"]
    assert chat == {
        "id": "assistant-1",
        "role": "assistant",
        "content": "Final",
        "timestamp": "2026-04-28T00:00:00Z",
        "final_response": "Final",
    }


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
            cast(NymeriaBackendClient, FakeChatClient()),
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
        yield {"type": "response", "content": "persisted final", "thread_id": "thread-3"}
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
            cast(NymeriaBackendClient, FakeChatClientWithHistory()),
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
            cast(NymeriaBackendClient, FakeChatClientWithStaleHistory()),
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


def test_get_client_falls_back_to_minted_token_file(tmp_path, monkeypatch):
    """In the full Docker stack the mcp container shares ``nymeria_data`` with
    the api and starts after it is healthy, so ``_get_client`` resolves the
    api-minted service token from disk when no operator token is set."""
    from types import SimpleNamespace

    import nymeria.config as config_module
    import nymeria.mcp_server as mcp_server
    from nymeria.core.service_bootstrap import SLIM_SERVICE_TOKEN_FILENAME

    (tmp_path / SLIM_SERVICE_TOKEN_FILENAME).write_text("nym_minted\n", encoding="utf-8")
    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(nymeria_service_token="", data_dir=tmp_path),
    )
    monkeypatch.setattr(mcp_server, "_service_token_override", None, raising=False)
    monkeypatch.setattr(mcp_server, "_backend_url_override", "http://api:8000", raising=False)
    monkeypatch.setattr(mcp_server, "_client", None, raising=False)

    client = mcp_server._get_client()

    assert client.service_token == "nym_minted"


# -- nymeria_command --------------------------------------------------------


class _RecordingBackend:
    """Fake backend client capturing POSTs so request shape is assertable."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    async def post(self, path, json_body=None, params=None, act_as=None):
        self.calls.append(
            {
                "path": path,
                "json_body": json_body,
                "params": params,
                "act_as": act_as,
            }
        )
        return self.response


def _run_command_tool(monkeypatch, response, **kwargs):
    import nymeria.mcp_server as mcp_server

    backend = _RecordingBackend(response)
    monkeypatch.setattr(mcp_server, "_get_client", lambda: backend)
    result = asyncio.run(mcp_server.nymeria_command(**kwargs))
    return backend, result


def test_nymeria_command_forwards_user_shaped_request_and_passes_result_through(monkeypatch):
    """The tool must emulate a formless user surface, not an agent caller."""
    backend, result = _run_command_tool(
        monkeypatch,
        {
            "success": True,
            "markdown": "**Model set** to `claude-fable-5`.",
            "command": "model",
            "level": "success",
            "data": None,
        },
        command="/model claude-fable-5",
        thread_id="thread-9",
    )

    assert backend.calls == [
        {
            "path": "/commands/execute",
            "json_body": {
                "command": "/model claude-fable-5",
                "thread_id": "thread-9",
                "source": "user",
                "actor": "user",
                "surface": "api",
                "supports_forms": False,
            },
            "params": None,
            "act_as": "default",
        }
    ]
    assert result["success"] is True
    assert result["level"] == "success"
    assert result["markdown"] == "**Model set** to `claude-fable-5`."
    # A plain command result must not grow a chat redirect hint.
    assert "hint" not in result


def test_nymeria_command_surface_and_act_as_reach_the_wire(monkeypatch):
    """Admin inbound identity honors an explicit acted-as user, and the
    chosen surface rides the request so per-surface blocking is testable."""
    import nymeria.mcp_auth as mcp_auth

    token = mcp_auth.set_identity({"user_id": "manning", "role": "admin"})
    try:
        backend, _ = _run_command_tool(
            monkeypatch,
            {"success": True, "markdown": "ok", "command": "help", "level": "info"},
            command="/help",
            user_id="claude-test",
            surface="telegram",
        )
    finally:
        mcp_auth.reset_identity(token)

    call = backend.calls[0]
    assert call["act_as"] == "claude-test"
    assert call["json_body"]["surface"] == "telegram"


def test_nymeria_command_chat_stream_result_gains_chat_hint(monkeypatch):
    """chat_stream commands are detected structurally (data.execution_kind),
    and the hint routes the caller to nymeria_chat."""
    backend, result = _run_command_tool(
        monkeypatch,
        {
            "success": False,
            "markdown": "`/goal` is handled outside the command service.",
            "command": "goal",
            "level": "error",
            "data": {"execution_kind": "chat_stream"},
        },
        command="/goal ship the beta",
        thread_id="thread-7",
    )

    assert backend.calls, "chat_stream detection must come from the response, not pre-parsing"
    # The backend only emits execution_kind data past the requires-thread
    # gate, so the emulated request must carry the thread.
    assert backend.calls[0]["json_body"]["thread_id"] == "thread-7"
    assert "nymeria_chat" in result["hint"]
    # The backend's own markdown stays intact alongside the hint.
    assert result["markdown"] == "`/goal` is handled outside the command service."


def test_nymeria_command_empty_command_errors_without_backend_call(monkeypatch):
    backend, result = _run_command_tool(
        monkeypatch,
        {"success": True, "markdown": "never", "command": "x", "level": "info"},
        command="   ",
    )

    assert result == {"error": "command is required"}
    assert backend.calls == []


def test_nymeria_command_successful_result_with_data_gets_no_hint(monkeypatch):
    """Successful commands routinely carry dict data (e.g. /model returns
    data.state); that must never trip the chat_stream redirect."""
    _, result = _run_command_tool(
        monkeypatch,
        {
            "success": True,
            "markdown": "**Model set** to `claude-fable-5`.",
            "command": "model",
            "level": "success",
            "data": {"state": {"model": "claude-fable-5"}},
        },
        command="/model claude-fable-5",
        thread_id="thread-9",
    )

    assert "hint" not in result


def test_nymeria_command_non_chat_stream_kind_gets_no_hint(monkeypatch):
    """surface_local commands also come back non-executable with an
    execution_kind; the redirect must key on chat_stream specifically."""
    _, result = _run_command_tool(
        monkeypatch,
        {
            "success": False,
            "markdown": "`/theme` is handled outside the command service.",
            "command": "theme",
            "level": "error",
            "data": {"execution_kind": "surface_local"},
        },
        command="/theme dark",
        thread_id="thread-9",
    )

    assert "hint" not in result


def test_nymeria_command_docstring_lists_every_command_surface():
    """Ratchet: the docstring's surface list must track CommandSurface."""
    from typing import get_args

    from nymeria.api.schemas.commands import CommandSurface
    from nymeria.mcp_server import nymeria_command

    doc = nymeria_command.__doc__ or ""
    missing = [s for s in get_args(CommandSurface) if s not in doc]
    assert missing == []
