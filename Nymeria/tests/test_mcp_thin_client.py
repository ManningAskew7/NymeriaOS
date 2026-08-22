"""Tests for the Nymeria MCP thin-client helpers."""

from __future__ import annotations

import asyncio
from typing import cast

from nymeria.mcp_backend_client import (
    ChatTranscript,
    NymeriaAPIError,
    NymeriaBackendClient,
    message_steps_to_response_text,
    transcript_from_events,
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
    EVENTS = [
        {"type": "thinking", "content": "Think", "thread_id": "thread-2"},
        {"type": "response", "content": "Answer", "thread_id": "thread-2"},
        {"type": "done", "thread_id": "thread-2"},
    ]

    async def get(self, *_args, **_kwargs):
        return {"messages": []}


def test_rendered_transcript_can_include_raw_events():
    async def _run():
        return await transcript_from_events(
            cast(NymeriaBackendClient, FakeChatClient()),
            FakeChatClient.EVENTS,
            thread_id="thread-2",
            user_id="default",
            message="hello",
            include_events=True,
        )

    result = asyncio.run(_run())

    assert result["thread_id"] == "thread-2"
    assert result["final_response"] == "Answer"
    assert [event["type"] for event in result["events"]] == ["thinking", "response", "done"]


class FakeChatClientWithHistory:
    EVENTS = [
        {"type": "tool_call", "id": "call-b", "name": "second_tool", "args": {}, "thread_id": "thread-3"},
        {"type": "tool_call", "id": "call-a", "name": "first_tool", "args": {}, "thread_id": "thread-3"},
        {"type": "response", "content": "persisted final", "thread_id": "thread-3"},
        {"type": "done", "thread_id": "thread-3"},
    ]

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


def test_finished_transcript_prefers_persisted_step_order_for_copy_output():
    async def _run():
        return await transcript_from_events(
            cast(NymeriaBackendClient, FakeChatClientWithHistory()),
            FakeChatClientWithHistory.EVENTS,
            thread_id="thread-3",
            user_id="default",
            message="hello",
            include_events=True,
        )

    result = asyncio.run(_run())

    assert result["history_message_id"] == "assistant-1"
    assert [step["name"] for step in result["steps"][:2]] == ["first_tool", "second_tool"]
    assert result["final_response"] == "persisted final"
    assert [event["name"] for event in result["events"][:2]] == ["second_tool", "first_tool"]


class FakeChatClientWithStaleHistory:
    EVENTS = [
        {"type": "response", "content": "fresh final", "thread_id": "thread-4"},
        {"type": "done", "thread_id": "thread-4"},
    ]

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


def test_rendered_transcript_ignores_stale_persisted_history():
    async def _run():
        return await transcript_from_events(
            cast(NymeriaBackendClient, FakeChatClientWithStaleHistory()),
            FakeChatClientWithStaleHistory.EVENTS,
            thread_id="thread-4",
            user_id="default",
            message="new prompt",
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


def _projected(transcript, mode):
    """Mirror the shipped nymeria_chat path: as_dict(verbose) then the
    per-mode projection (the renderer's tail). Testing as_dict
    alone previously passed while the shipped path recomputed the fields."""
    from nymeria.mcp_backend_client import project_chat_payload_for_verbosity

    return project_chat_payload_for_verbosity(
        transcript.as_dict(verbosity="verbose"), mode
    )


def test_failed_turn_renders_failure_line_not_empty_message():
    """A turn with zero response steps and an error surfaces the error as
    final_response instead of the "(empty message)" placeholder (which buried
    the real signal for MCP callers; live incident 2026-08-10), in every
    verbosity mode of the shipped projection."""
    transcript = ChatTranscript(thread_id="thread-1")
    transcript.add_event(
        {
            "type": "error",
            "content": "An error occurred: Error code: 401 - Invalid API key",
            "code": "agent_runtime_error",
            "thread_id": "thread-1",
        }
    )
    transcript.add_event({"type": "done", "thread_id": "thread-1"})

    for mode in ("verbose", "concise", "chat"):
        result = _projected(transcript, mode)
        assert result["final_response"] == (
            "[turn failed] An error occurred: Error code: 401 - Invalid API key"
        ), mode
        if "full_markdown" in result:
            assert result["full_markdown"] == result["final_response"], mode
        assert result["errors"][0]["code"] == "agent_runtime_error", mode


def test_failed_turn_with_tool_steps_appends_failure_to_markdown():
    """The tool-steps case: the projection recomputes final_response from
    steps, which previously reinstated "(empty message)" over the failure
    line (review finding, 2026-08-10)."""
    transcript = ChatTranscript(thread_id="thread-1")
    transcript.add_event({"type": "tool_call", "id": "c1", "name": "search", "args": {}})
    transcript.add_event({"type": "tool_result", "id": "c1", "name": "search", "result": "r"})
    transcript.add_event({"type": "error", "content": "boom", "code": "agent_runtime_error"})

    for mode in ("verbose", "concise"):
        result = _projected(transcript, mode)
        assert result["final_response"] == "[turn failed] boom", mode
        assert result["full_markdown"].startswith("### Tool: search"), mode
        assert result["full_markdown"].endswith("[turn failed] boom"), mode
    # chat mode redacts tool steps entirely; the failure line still lands.
    chat = _projected(transcript, "chat")
    assert chat["final_response"] == "[turn failed] boom"


def test_successful_turn_with_errors_keeps_response_text():
    """A mid-turn recovered error must not clobber real response text."""
    transcript = ChatTranscript(thread_id="thread-1")
    transcript.add_event({"type": "error", "content": "transient", "code": "x"})
    transcript.add_event({"type": "response", "content": "Recovered fine."})

    for mode in ("verbose", "concise", "chat"):
        result = _projected(transcript, mode)
        assert result["final_response"] == "Recovered fine.", mode
        assert "[turn failed]" not in str(result.get("full_markdown", "")), mode


# ---------------------------------------------------------------------------
# Collected-events rendering (background dispatch parity with blocking chat)
# ---------------------------------------------------------------------------

_DISPATCH_EVENTS = [
    {"type": "thinking", "content": "Planning", "thread_id": "thread-9"},
    {
        "type": "tool_call",
        "id": "call-1",
        "name": "chrome_screenshot",
        "args": {"tab_id": 7},
        "thread_id": "thread-9",
    },
    {
        "type": "tool_result",
        "tool_call_id": "call-1",
        "name": "chrome_screenshot",
        "result": "captured 1280x720",
        "thread_id": "thread-9",
    },
    {"type": "response", "content": "Shot taken.", "thread_id": "thread-9"},
]


class _CountingHistoryClient:
    """Backend stub that records whether the persisted-steps overlay ran."""

    def __init__(self):
        self.get_calls = 0

    async def stream_chat(self, **_kwargs):
        for event in _DISPATCH_EVENTS:
            yield event
        yield {"type": "done", "thread_id": "thread-9"}

    async def get(self, *_args, **_kwargs):
        self.get_calls += 1
        return {
            "messages": [
                {"id": "user-9", "role": "user", "content": "shoot it"},
                {
                    "id": "assistant-9",
                    "role": "assistant",
                    "steps": [
                        {
                            "type": "tool_call",
                            "id": "call-1",
                            "name": "chrome_screenshot",
                            "arguments": {"tab_id": 7},
                        },
                        {"type": "response", "content": "Shot taken."},
                    ],
                },
            ]
        }


def test_running_turn_renders_without_consulting_persisted_history():
    """Mid-turn there is no persisted assistant message worth preferring.

    Consulting history anyway is not merely wasteful: the newest assistant
    message belongs to the PREVIOUS turn, so an overlay could graft stale steps
    onto a live partial.
    """

    async def _run():
        client = _CountingHistoryClient()
        partial = await transcript_from_events(
            cast(NymeriaBackendClient, client),
            _DISPATCH_EVENTS,  # deliberately no "done"
            thread_id="thread-9",
            user_id="default",
            message="shoot it",
        )
        return client, partial

    client, partial = asyncio.run(_run())

    assert client.get_calls == 0
    assert "history_message_id" not in partial
    assert partial["done"] is False
    # The live stream order survives, rather than being replaced by history.
    assert [step["type"] for step in partial["steps"]] == [
        "thinking",
        "tool_call",
        "response",
    ]


def test_finished_turn_prefers_persisted_step_order():
    """Once done, a collected transcript gets the same overlay chat does."""

    async def _run():
        client = _CountingHistoryClient()
        final = await transcript_from_events(
            cast(NymeriaBackendClient, client),
            _DISPATCH_EVENTS + [{"type": "done", "thread_id": "thread-9"}],
            thread_id="thread-9",
            user_id="default",
            message="shoot it",
        )
        return client, final

    client, final = asyncio.run(_run())

    assert client.get_calls == 1
    assert final["history_message_id"] == "assistant-9"


def test_partial_transcript_honors_verbosity_like_a_finished_one():
    """Verbosity is a property of the rendering, not of completion state."""

    async def _run():
        return {
            mode: await transcript_from_events(
                None,
                _DISPATCH_EVENTS,
                thread_id="thread-9",
                verbosity=mode,
            )
            for mode in ("verbose", "concise", "chat")
        }

    rendered = asyncio.run(_run())

    verbose_call = next(
        step for step in rendered["verbose"]["steps"] if step["type"] == "tool_call"
    )
    assert verbose_call["arguments"] == {"tab_id": 7}
    assert verbose_call["result"] == "captured 1280x720"

    concise_call = next(
        step for step in rendered["concise"]["steps"] if step["type"] == "tool_call"
    )
    assert concise_call["name"] == "chrome_screenshot"
    assert "arguments" not in concise_call
    assert "result" not in concise_call

    # "chat" is the callable-thread semantic: the answer, none of the working.
    assert "steps" not in rendered["chat"]
    assert rendered["chat"]["final_response"] == "Shot taken."


# ---------------------------------------------------------------------------
# The bounded-wait chat contract (mode / wait_seconds / if_busy)
# ---------------------------------------------------------------------------


class _SlowChatClient:
    """Streams a couple of events, then stalls until released."""

    def __init__(self):
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def stream_chat(self, **_kwargs):
        yield {"type": "thinking", "content": "Working", "thread_id": "thread-slow"}
        yield {"type": "response", "content": "partial so far", "thread_id": "thread-slow"}
        self.started.set()
        await self.release.wait()
        yield {"type": "response", "content": " and the rest", "thread_id": "thread-slow"}
        yield {"type": "done", "thread_id": "thread-slow"}

    async def get(self, *_args, **_kwargs):
        return {"messages": []}


def _chat(monkeypatch, client, **kwargs):
    import nymeria.mcp_server as mcp_server

    monkeypatch.setattr(mcp_server, "_get_client", lambda: client)
    monkeypatch.setattr(mcp_server, "effective_act_as", lambda uid: uid)
    return asyncio.run(mcp_server.nymeria_chat(**kwargs))


def test_ask_that_outlives_its_budget_returns_a_partial_and_a_resume_token(monkeypatch):
    """The budget expiring must not cancel the turn or lose the transcript."""
    import nymeria.mcp_server as mcp_server

    client = _SlowChatClient()

    async def _run():
        monkeypatch.setattr(mcp_server, "_get_client", lambda: client)
        monkeypatch.setattr(mcp_server, "effective_act_as", lambda uid: uid)
        running = await mcp_server.nymeria_chat(
            message="drive the browser",
            thread_id="thread-slow",
            wait_seconds=1,
        )
        # The turn is still live: releasing it must still complete normally,
        # and the resume token must reach the same turn rather than a new one.
        client.release.set()
        finished = await mcp_server.nymeria_chat_collect(
            dispatch_id=running["resume"], timeout_seconds=5
        )
        return running, finished

    running, finished = asyncio.run(_run())

    assert running["status"] == "running"
    assert running["final_response"] == "partial so far"
    assert "[Running]" in running["running"]
    assert running["resume"]

    assert finished["status"] == "done"
    assert finished["final_response"] == "partial so far and the rest"
    assert finished["thread_id"] == "thread-slow"


def test_ask_within_budget_returns_done_with_the_flat_transcript(monkeypatch):
    """The existing flat shape is the contract; regression specs read it."""
    result = _chat(
        monkeypatch,
        _CountingHistoryClient(),
        message="shoot it",
        thread_id="thread-9",
        wait_seconds=5,
    )

    assert result["status"] == "done"
    assert result["final_response"] == "Shot taken."
    assert result["thread_id"] == "thread-9"
    assert "resume" not in result
    assert "running" not in result


def test_handoff_returns_a_receipt_without_waiting(monkeypatch):
    """A handoff must return before the turn does, and say so."""
    client = _SlowChatClient()
    result = _chat(
        monkeypatch,
        client,
        message="go do the long thing",
        thread_id="thread-slow",
        mode="handoff",
    )

    assert result["status"] == "dispatched"
    assert "[HandedOff]" in result["handoff"]
    assert "no final response will be returned here" in result["handoff"]
    assert result["resume"]
    # It genuinely did not wait for the turn's own output.
    assert "final_response" not in result


def test_wait_seconds_with_handoff_is_refused_not_ignored(monkeypatch):
    """A parameter that cannot apply must teach, not silently do nothing."""
    result = _chat(
        monkeypatch,
        _SlowChatClient(),
        message="hi",
        thread_id="thread-slow",
        mode="handoff",
        wait_seconds=30,
    )

    assert "wait_seconds is only supported when mode='ask'" in result["error"]
    assert "status" not in result


def test_out_of_range_wait_names_both_numbers_and_the_alternative(monkeypatch):
    result = _chat(
        monkeypatch,
        _SlowChatClient(),
        message="hi",
        thread_id="thread-slow",
        wait_seconds=99_999,
    )

    assert "99999" in result["error"]
    assert "3600" in result["error"]
    assert "handoff" in result["error"]


def test_if_busy_error_refuses_a_busy_thread_and_names_the_retry(monkeypatch):
    """Refusing is only useful if it says how to proceed instead."""
    import nymeria.mcp_server as mcp_server

    async def _busy(_thread_id, _user_id):
        return True

    monkeypatch.setattr(mcp_server, "_thread_is_busy", _busy)
    result = _chat(
        monkeypatch,
        _SlowChatClient(),
        message="hi",
        thread_id="thread-slow",
        if_busy="error",
    )

    assert result["status"] == "busy"
    assert "[Busy]" in result["error"]
    assert "if_busy='queue'" in result["error"]


def test_a_resume_pins_the_turn_not_just_the_thread(monkeypatch):
    """A thread that has moved on to a NEW turn must 404, not silently hand
    back a different turn wearing the same thread id."""
    import nymeria.mcp_server as mcp_server

    client = _ReplayClient()
    token = mcp_server._encode_resume("gone", "thread-r", 0, "default", "turn-7")
    _collect(monkeypatch, client, resume=token, timeout_seconds=5)

    assert client.replay_calls[0]["turn_id"] == "turn-7"


# ---------------------------------------------------------------------------
# Resume durability: server replay when the in-memory dispatch is gone
# ---------------------------------------------------------------------------


class _ReplayClient:
    """Backend stub whose turn/turn-replay endpoint returns a fixed turn."""

    def __init__(self, events=None, raises=None):
        self._events = events or [
            {"type": "response", "content": "recovered answer", "thread_id": "thread-r"},
            {"type": "done", "thread_id": "thread-r"},
        ]
        self._raises = raises
        self.replay_calls = []

    async def stream_turn_replay(self, *, thread_id, user_id, turn_id=None, from_seq=0):
        self.replay_calls.append(
            {
                "thread_id": thread_id,
                "user_id": user_id,
                "from_seq": from_seq,
                "turn_id": turn_id,
            }
        )
        if self._raises:
            raise self._raises
        for event in self._events:
            yield event

    async def get(self, *_args, **_kwargs):
        return {"messages": []}


def _collect(monkeypatch, client, **kwargs):
    import nymeria.mcp_server as mcp_server

    monkeypatch.setattr(mcp_server, "_get_client", lambda: client)
    monkeypatch.setattr(mcp_server, "effective_act_as", lambda uid: uid)
    return asyncio.run(mcp_server.nymeria_chat_collect(**kwargs))


def test_resume_token_round_trips_thread_and_cursor():
    import nymeria.mcp_server as mcp_server

    token = mcp_server._encode_resume("abc123", "thread-r", 41, "someone", "turn-7")
    assert token != "abc123"  # opaque, not just the id

    decoded = mcp_server._decode_resume(token)
    assert decoded == {
        "dispatch_id": "abc123",
        "thread_id": "thread-r",
        "cursor": 41,
        "user_id": "someone",
        "turn_id": "turn-7",
    }


def test_a_bare_dispatch_id_still_decodes_for_older_callers():
    """The regression specs pass raw ids; they must keep working."""
    import nymeria.mcp_server as mcp_server

    assert mcp_server._decode_resume("deadbeef0001") == {
        "dispatch_id": "deadbeef0001",
        "thread_id": None,
        "cursor": 0,
        "user_id": None,
        "turn_id": None,
    }


def test_evicted_dispatch_recovers_from_the_servers_own_copy(monkeypatch):
    """Ten minutes of distraction must not destroy a finished turn."""
    import nymeria.mcp_server as mcp_server

    client = _ReplayClient()
    token = mcp_server._encode_resume("gone-forever", "thread-r", 0, "default")
    result = _collect(monkeypatch, client, resume=token, timeout_seconds=5)

    assert result["status"] == "done"
    assert result["recovered_from"] == "server_replay"
    assert result["final_response"] == "recovered answer"
    assert client.replay_calls[0]["thread_id"] == "thread-r"


def test_thread_id_alone_attaches_to_the_most_recent_turn(monkeypatch):
    """The lost-handle path: no token at all, just a thread."""
    client = _ReplayClient()
    result = _collect(monkeypatch, client, thread_id="thread-r", timeout_seconds=5)

    assert result["status"] == "done"
    assert result["final_response"] == "recovered answer"


def test_expired_buffer_says_so_and_points_at_history(monkeypatch):
    from nymeria.mcp_backend_client import NymeriaAPIError

    client = _ReplayClient(raises=NymeriaAPIError(404, "turn_not_found"))
    result = _collect(monkeypatch, client, thread_id="thread-r", timeout_seconds=5)

    assert "nymeria_get_thread_history" in result["error"]
    assert "expired" in result["error"]


def test_replay_gap_is_reported_rather_than_passed_off_as_complete(monkeypatch):
    """A record with a hole must never read as the whole turn."""
    from nymeria.mcp_backend_client import NymeriaAPIError

    client = _ReplayClient(raises=NymeriaAPIError(410, "turn_replay_gap"))
    result = _collect(monkeypatch, client, thread_id="thread-r", timeout_seconds=5)

    assert "gap" in result["error"]
    assert "missing" in result["error"]
    assert "do not treat" in result["error"].lower()


def test_no_dispatch_and_no_thread_explains_how_to_recover(monkeypatch):
    result = _collect(
        monkeypatch, _ReplayClient(), dispatch_id="nope", timeout_seconds=5
    )

    assert "not found" in result["error"]
    assert "thread_id=" in result["error"]


def test_resuming_returns_only_new_work_but_the_whole_answer(monkeypatch):
    """Incremental steps bound the context; the answer stays undivided."""
    import nymeria.mcp_server as mcp_server

    client = _SlowChatClient()

    async def _run():
        monkeypatch.setattr(mcp_server, "_get_client", lambda: client)
        monkeypatch.setattr(mcp_server, "effective_act_as", lambda uid: uid)
        first = await mcp_server.nymeria_chat(
            message="drive", thread_id="thread-slow", wait_seconds=1
        )
        client.release.set()
        second = await mcp_server.nymeria_chat_collect(
            resume=first["resume"], timeout_seconds=5
        )
        return first, second

    first, second = asyncio.run(_run())

    # Two events (thinking + first response) landed before the stall, so the
    # continuation starts at index 2 rather than replaying from zero.
    assert second["continued_from"] == 2
    # The working is only the NEW part: the early thinking step is not repeated.
    assert all(step.get("content") != "Working" for step in second["steps"])
    # The answer is the WHOLE answer, not the tail fragment.
    assert second["final_response"] == "partial so far and the rest"


# ---------------------------------------------------------------------------
# Halt honesty: a stopped turn must never read as a finished one
# ---------------------------------------------------------------------------


def _halted_transcript(*, with_text: bool, resumable: bool = False):
    transcript = ChatTranscript(thread_id="thread-h")
    if with_text:
        transcript.add_event(
            {"type": "response", "content": "I got partway through.", "thread_id": "thread-h"}
        )
    transcript.add_event(
        {
            "type": "iteration_limit",
            "content": "Agent reached the maximum number of steps.",
            "reason": "max_iterations",
            "max_iterations": 300,
            "resumable": resumable,
            "thread_id": "thread-h",
        }
    )
    transcript.add_event({"type": "done", "thread_id": "thread-h"})
    return transcript


def test_a_halted_turn_flags_incompleteness_in_every_verbosity():
    """The note must survive projection, or "chat" mode hides the halt."""
    transcript = _halted_transcript(with_text=True)

    for mode in ("verbose", "concise", "chat"):
        result = transcript.as_dict(verbosity=mode)
        assert result["final_response"].startswith("I got partway through."), mode
        assert "may be incomplete" in result["final_response"], mode
        assert result["halted"]["reason"] == "max_iterations", mode
        assert result["halted"]["max_iterations"] == 300, mode


def test_the_halt_notice_is_not_passed_off_as_the_assistants_own_words():
    """The runtime's limit message must not become a response step."""
    transcript = _halted_transcript(with_text=True)

    contents = [
        str(step.get("content") or "")
        for step in transcript.steps
        if step.get("type") == "response"
    ]
    assert contents == ["I got partway through."]
    assert not any("maximum number of steps" in c for c in contents)


def test_a_halt_with_no_output_says_so_instead_of_empty_message():
    result = _halted_transcript(with_text=False).as_dict(verbosity="chat")

    assert result["final_response"].startswith("[Halted:")
    assert "(empty message)" not in result["final_response"]
    assert "manual follow-up" in result["final_response"]


def test_a_resumable_halt_names_resume_rather_than_manual_follow_up():
    """Resumable and terminal halts need different next steps."""
    result = _halted_transcript(with_text=True, resumable=True).as_dict()

    assert "/resume" in result["final_response"]
    assert "manual follow-up" not in result["final_response"]
    assert result["halted"]["resumable"] is True


def test_an_unhalted_turn_carries_no_halt_block_or_note():
    transcript = ChatTranscript(thread_id="thread-h")
    transcript.add_event({"type": "response", "content": "All done.", "thread_id": "thread-h"})
    transcript.add_event({"type": "done", "thread_id": "thread-h"})

    for mode in ("verbose", "concise", "chat"):
        result = transcript.as_dict(verbosity=mode)
        assert result["final_response"] == "All done.", mode
        assert "incomplete" not in result["final_response"], mode
        assert result.get("halted") is None, mode


# ---------------------------------------------------------------------------
# Review fixes: shipped-path halt rendering, token states, busy source, replay
# ---------------------------------------------------------------------------

_HALT_EVENTS = [
    {"type": "response", "content": "Partway through.", "thread_id": "thread-h"},
    {
        "type": "iteration_limit",
        "content": "Agent reached the maximum number of steps.",
        "thread_id": "thread-h",
    },
    {"type": "done", "thread_id": "thread-h"},
]


def test_halt_note_appears_exactly_once_through_the_shipped_renderer():
    """The projection runs twice on the real path; the note must not double.

    The halt tests that render via ChatTranscript.as_dict alone exercise ONE
    projection and so cannot catch this.
    """

    async def _run():
        return {
            mode: await transcript_from_events(
                None, _HALT_EVENTS, thread_id="thread-h", verbosity=mode
            )
            for mode in ("verbose", "concise", "chat")
        }

    rendered = asyncio.run(_run())

    for mode, result in rendered.items():
        assert result["final_response"].count("may be incomplete") == 1, mode
        assert result["final_response"].startswith("Partway through."), mode


def test_halt_note_reaches_full_markdown_not_only_final_response():
    """A verbose consumer reads full_markdown; a halt must be visible there."""

    async def _run():
        return await transcript_from_events(
            None, _HALT_EVENTS, thread_id="thread-h", verbosity="verbose"
        )

    result = asyncio.run(_run())

    assert "may be incomplete" in result["full_markdown"]
    assert result["full_markdown"].count("may be incomplete") == 1


def test_a_thread_only_resume_token_survives_the_round_trip():
    """The server-replay token carries no dispatch id; it must still decode.

    Gating the decoder on a truthy dispatch id sent this token down the
    bare-id branch, which lost the thread and made the recovery path
    enterable but not continuable.
    """
    import nymeria.mcp_server as mcp_server

    decoded = mcp_server._decode_resume(
        mcp_server._encode_resume("", "thread-r", 0, "default")
    )
    assert decoded["thread_id"] == "thread-r"
    assert decoded["dispatch_id"] == ""
    assert decoded["user_id"] == "default"


def test_busy_check_reads_the_per_thread_endpoint_not_the_admin_aggregate(monkeypatch):
    """/status/turns hides busy_threads from non-admins, so it can never say
    True for them; the per-thread status endpoint is role-independent."""
    import nymeria.mcp_server as mcp_server

    seen = {}

    async def _fake(_method, path, user_id=None, **_kw):
        seen["path"] = path
        return {"thread_id": "thread-b", "processing": True}

    monkeypatch.setattr(mcp_server, "_json_call", _fake)
    verdict = asyncio.run(mcp_server._thread_is_busy("thread-b", "someone"))

    assert verdict is True
    assert seen["path"] == "/threads/thread-b/status"
    assert "status/turns" not in seen["path"]


def test_busy_check_reports_unknowable_when_the_endpoint_errors(monkeypatch):
    import nymeria.mcp_server as mcp_server

    async def _fake(_method, _path, user_id=None, **_kw):
        return {"error": "boom"}

    monkeypatch.setattr(mcp_server, "_json_call", _fake)
    assert asyncio.run(mcp_server._thread_is_busy("thread-b", "someone")) is None


class _AbortedReplayClient(_ReplayClient):
    """Replays a turn whose writer died: attach state says so, no terminal."""

    async def stream_turn_replay(self, *, thread_id, user_id, turn_id=None, from_seq=0):
        self.replay_calls.append({"thread_id": thread_id, "user_id": user_id})
        yield {"type": "turn_attach", "state": "aborted", "turn_id": "t1"}
        yield {"type": "response", "content": "got this far", "thread_id": thread_id}


def test_an_aborted_turn_is_reported_finished_not_still_running(monkeypatch):
    """An aborted turn emits no terminal event. Without reading turn_attach we
    would call it 'running' and hand back a token for a dead turn."""
    result = _collect(
        monkeypatch, _AbortedReplayClient(), thread_id="thread-r", timeout_seconds=5
    )

    assert result["status"] == "done"
    assert result["turn_state"] == "aborted"
    assert "aborted" in result["error"]
    assert "resume" not in result


class _MidStreamGapClient(_ReplayClient):
    """Gap discovered mid-replay: in-band frame, then the stream just ends."""

    async def stream_turn_replay(self, *, thread_id, user_id, turn_id=None, from_seq=0):
        self.replay_calls.append({"thread_id": thread_id, "user_id": user_id})
        yield {"type": "turn_attach", "state": "live", "turn_id": "t1"}
        yield {"type": "response", "content": "first part", "thread_id": thread_id}
        yield {"type": "turn_replay_gap", "thread_id": thread_id}


def test_a_mid_stream_gap_is_reported_like_an_attach_time_one(monkeypatch):
    """The attach-time 410 only covers gaps that already existed."""
    result = _collect(
        monkeypatch, _MidStreamGapClient(), thread_id="thread-r", timeout_seconds=5
    )

    assert "gap" in result["error"]
    assert "do not treat" in result["error"].lower()


# ---------------------------------------------------------------------------
# Cold-review fixes: error fidelity, overlay scope, identity, retention
# ---------------------------------------------------------------------------


class _RefusingChatClient:
    """Backend that refuses the turn outright (e.g. admission control 429)."""

    async def stream_chat(self, **_kwargs):
        raise NymeriaAPIError(
            429,
            "Too many concurrent interactive turns.",
            {"detail": {"code": "interactive_busy", "retry_after": 12}},
        )
        yield  # pragma: no cover - makes this an async generator

    async def get(self, *_args, **_kwargs):
        return {"messages": []}


def test_a_refused_turn_keeps_its_status_code_and_payload(monkeypatch):
    """A turn that never started is a failed CALL, not a completed empty one.

    Dropping to a flattened string loses retry_after on a 429 and the auth
    detail on a 401, which is what a caller needs to decide what to do next.
    """
    result = _chat(
        monkeypatch,
        _RefusingChatClient(),
        message="hi",
        thread_id="thread-x",
        wait_seconds=5,
    )

    assert result["status_code"] == 429
    assert result["payload"]["detail"]["retry_after"] == 12
    assert result.get("status") != "done"


class _HistoryOverlayClient(_CountingHistoryClient):
    """Finished turn whose persisted history covers the WHOLE turn."""

    async def stream_chat(self, **_kwargs):
        yield {"type": "thinking", "content": "Planning", "thread_id": "thread-9"}
        yield {"type": "response", "content": "Shot taken.", "thread_id": "thread-9"}
        yield {"type": "done", "thread_id": "thread-9"}


def test_a_sliced_collect_does_not_get_the_whole_turn_via_the_overlay(monkeypatch):
    """The overlay replaces steps with the full assistant message, so taking it
    on a slice returns everything and defeats the cursor on the last poll."""
    import nymeria.mcp_server as mcp_server

    client = _HistoryOverlayClient()

    async def _run():
        monkeypatch.setattr(mcp_server, "_get_client", lambda: client)
        monkeypatch.setattr(mcp_server, "effective_act_as", lambda uid: uid)
        ctx = mcp_server._start_background_chat(
            message="shoot it", thread_id="thread-9", user_id="default"
        )
        await mcp_server._await_dispatch(ctx, 5)
        token = mcp_server._encode_resume(
            ctx["dispatch_id"], "thread-9", 1, "default"
        )
        return await mcp_server.nymeria_chat_collect(resume=token, timeout_seconds=5)

    result = asyncio.run(_run())

    assert result["continued_from"] == 1
    assert "history_message_id" not in result
    # "Planning" was delivered before the cursor and must not come back.
    assert all(step.get("content") != "Planning" for step in result["steps"])


def test_a_halted_turn_still_gets_its_persisted_step_order(monkeypatch):
    """Projecting before the overlay made the halt note fail the same-turn
    comparison, silently costing halted turns their canonical ordering."""

    class _HaltedHistoryClient:
        async def get(self, *_args, **_kwargs):
            return {
                "messages": [
                    {"id": "u", "role": "user", "content": "hi"},
                    {
                        "id": "msg-h",
                        "role": "assistant",
                        "steps": [{"type": "response", "content": "Partway through."}],
                    },
                ]
            }

    async def _run():
        return await transcript_from_events(
            cast(NymeriaBackendClient, _HaltedHistoryClient()),
            _HALT_EVENTS,
            thread_id="thread-h",
            user_id="default",
            message="hi",
        )

    result = asyncio.run(_run())

    assert result["history_message_id"] == "msg-h"
    assert "may be incomplete" in result["final_response"]


def test_a_dispatch_is_not_readable_by_another_identity(monkeypatch):
    """A 48-bit id alone must not surface another user's transcript."""
    import nymeria.mcp_server as mcp_server

    client = _CountingHistoryClient()

    async def _run():
        monkeypatch.setattr(mcp_server, "_get_client", lambda: client)
        monkeypatch.setattr(mcp_server, "effective_act_as", lambda uid: uid)
        ctx = mcp_server._start_background_chat(
            message="alice's prompt", thread_id="alice-thread", user_id="alice"
        )
        await mcp_server._await_dispatch(ctx, 5)
        return await mcp_server.nymeria_chat_collect(
            dispatch_id=ctx["dispatch_id"], user_id="bob", timeout_seconds=1
        )

    result = asyncio.run(_run())

    # Bob does not get Alice's rendered turn out of process memory. He is sent
    # to the server path, which the backend gates on thread access.
    assert result.get("final_response") != "Shot taken."
    assert result.get("recovered_from") == "server_replay" or result.get("error")


def test_collect_rejects_a_wait_beyond_the_ceiling(monkeypatch):
    result = _collect(
        monkeypatch, _ReplayClient(), thread_id="thread-r", timeout_seconds=99_999
    )

    assert "99999" in result["error"]
    assert "3600" in result["error"]


def test_an_over_long_turn_stops_retaining_and_says_so(monkeypatch):
    """Retention is bounded; a truncated record must not read as complete."""
    import nymeria.mcp_server as mcp_server

    monkeypatch.setattr(mcp_server, "_DISPATCH_MAX_EVENTS", 2)

    class _ChattyClient:
        async def stream_chat(self, **_kwargs):
            for i in range(6):
                yield {"type": "response", "content": f"chunk{i}", "thread_id": "thread-c"}
            yield {"type": "done", "thread_id": "thread-c"}

        async def get(self, *_args, **_kwargs):
            return {"messages": []}

    result = _chat(
        monkeypatch,
        _ChattyClient(),
        message="go",
        thread_id="thread-c",
        wait_seconds=5,
        include_events=True,
    )

    assert len(result["events"]) <= 3
    assert "[Truncated]" in result["truncated"]
    assert "nymeria_get_thread_history" in result["truncated"]


# ---------------------------------------------------------------------------
# Behaviors the conformance pass found unguarded
# ---------------------------------------------------------------------------


def test_the_handoff_receipt_names_the_thread_and_how_to_collect(monkeypatch):
    """A receipt that does not say where the work went, or how to get it back,
    leaves the caller with nothing actionable."""
    result = _chat(
        monkeypatch,
        _SlowChatClient(),
        message="go",
        thread_id="thread-slow",
        mode="handoff",
    )

    assert "thread-slow" in result["handoff"]
    assert "nymeria_chat_collect" in result["handoff"]
    assert result["thread_id"] == "thread-slow"


def test_if_busy_queue_still_joins_a_busy_thread(monkeypatch):
    """The default must keep queueing; only if_busy='error' refuses."""
    import nymeria.mcp_server as mcp_server

    async def _busy(_thread_id, _user_id):
        return True

    monkeypatch.setattr(mcp_server, "_thread_is_busy", _busy)
    result = _chat(
        monkeypatch,
        _CountingHistoryClient(),
        message="shoot it",
        thread_id="thread-9",
        wait_seconds=5,
    )

    assert result["status"] == "done"
    assert result["final_response"] == "Shot taken."


def test_turn_status_asks_per_thread_when_given_one(monkeypatch):
    """Per-thread status works for any identity; the aggregate hides its
    useful half from non-admins."""
    import nymeria.mcp_server as mcp_server

    seen = []

    async def _fake(_method, path, user_id=None, **_kw):
        seen.append(path)
        return {"processing": True, "turn": {"state": "live"}}

    monkeypatch.setattr(mcp_server, "_json_call", _fake)
    scoped = asyncio.run(mcp_server.nymeria_turn_status(thread_id="thread-q"))
    asyncio.run(mcp_server.nymeria_turn_status())

    assert scoped["turn"]["state"] == "live"
    assert seen == ["/threads/thread-q/status", "/status/turns"]


def test_attachments_reach_the_wire_on_a_dispatched_chat(monkeypatch):
    """#234: the Docker agent cannot read host paths, so a brief has to be
    pushed rather than referenced."""
    sent = {}

    class _Recorder:
        async def stream_chat(self, **kwargs):
            sent.update(kwargs)
            yield {"type": "response", "content": "ok", "thread_id": "thread-a"}
            yield {"type": "done", "thread_id": "thread-a"}

        async def get(self, *_a, **_k):
            return {"messages": []}

    attachments = [{"type": "text", "name": "brief.md", "content": "do this"}]
    _chat(
        monkeypatch,
        _Recorder(),
        message="here",
        thread_id="thread-a",
        wait_seconds=5,
        attachments=attachments,
    )

    assert sent["attachments"] == attachments


def test_every_chat_tool_description_names_its_situation_and_the_alternative():
    """F4: the reason the wrong tool got used all session is that the right
    one did not describe itself. Keep each description self-locating."""
    import nymeria.mcp_server as mcp_server

    chat = mcp_server.nymeria_chat.__doc__ or ""
    assert 'mode="ask"' in chat or "ask (default)" in chat
    assert "handoff" in chat
    assert "wait_seconds" in chat
    assert "transport timeout" in chat

    background = mcp_server.nymeria_chat_background.__doc__ or ""
    assert "REGRESSION-TEST INSTRUMENT" in background
    assert "nymeria_chat" in background

    collect = mcp_server.nymeria_chat_collect.__doc__ or ""
    for handle in ("resume", "dispatch_id", "thread_id"):
        assert handle in collect

    status = mcp_server.nymeria_turn_status.__doc__ or ""
    assert "thread_id" in status and "admin" in status


def test_projecting_a_halted_payload_twice_adds_one_note():
    """Idempotency guard: the pipeline projects once today, but this is the
    invariant that made a second projection safe when it did not."""
    from nymeria.mcp_backend_client import project_chat_payload_for_verbosity

    transcript = _halted_transcript(with_text=True)
    payload = transcript.unprojected(verbosity="verbose")

    once = project_chat_payload_for_verbosity(payload, "verbose")
    twice = project_chat_payload_for_verbosity(once, "verbose")

    assert twice["final_response"].count("may be incomplete") == 1
    assert twice["full_markdown"].count("may be incomplete") == 1


def test_a_turn_that_only_reasons_says_so_instead_of_empty_message():
    """Not the same as inheriting the in-process thinking fallback, which
    hands the caller raw reasoning as though it were the answer."""
    transcript = ChatTranscript(thread_id="thread-r")
    transcript.add_event(
        {"type": "thinking", "content": "Long deliberation.", "thread_id": "thread-r"}
    )
    transcript.add_event({"type": "done", "thread_id": "thread-r"})

    for mode in ("verbose", "concise", "chat"):
        result = transcript.as_dict(verbosity=mode)
        assert result["final_response"].startswith("[No answer:"), mode
        assert "(empty message)" not in result["final_response"], mode
        # The reasoning is NOT passed off as the answer.
        assert "Long deliberation." not in result["final_response"], mode


def test_the_default_wait_derives_from_tool_timeout(monkeypatch):
    """"As long as an in-process callable ask" has to be derived, not merely
    a matching hardcoded number, or raising TOOL_TIMEOUT silently desyncs it."""
    import nymeria.config
    import nymeria.mcp_server as mcp_server

    class _Settings:
        nymeria_mcp_chat_wait_seconds = None
        tool_timeout = 900

    monkeypatch.setattr(nymeria.config, "get_settings", lambda: _Settings())
    assert mcp_server._resolve_chat_wait_seconds(None) == 900

    _Settings.nymeria_mcp_chat_wait_seconds = 120
    assert mcp_server._resolve_chat_wait_seconds(None) == 120

    # An explicit argument still wins over both.
    assert mcp_server._resolve_chat_wait_seconds(45) == 45


def test_a_misconfigured_default_clamps_but_a_bad_argument_refuses(monkeypatch):
    """An operator typo must not brick every call; a caller asking for the
    impossible must be told rather than silently given something else."""
    import nymeria.config
    import nymeria.mcp_server as mcp_server

    class _Settings:
        nymeria_mcp_chat_wait_seconds = 99_999
        tool_timeout = 300

    monkeypatch.setattr(nymeria.config, "get_settings", lambda: _Settings())
    assert mcp_server._resolve_chat_wait_seconds(None) == 3600

    try:
        mcp_server._resolve_chat_wait_seconds(99_999)
    except ValueError as exc:
        assert "3600" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("an out-of-range argument must raise")


def test_a_running_turns_token_carries_the_turn_id_off_the_wire(monkeypatch):
    """The id only ever arrives as a stream event, so it has to be learned
    there; a token without it resumes onto whatever turn is current later."""
    import nymeria.mcp_server as mcp_server

    class _TurnIdClient:
        def __init__(self):
            self.release = asyncio.Event()

        async def stream_chat(self, **_kwargs):
            yield {"type": "turn_started", "turn_id": "turn-42", "thread_id": "thread-t"}
            yield {"type": "response", "content": "working", "thread_id": "thread-t"}
            await self.release.wait()
            yield {"type": "done", "thread_id": "thread-t"}

        async def get(self, *_a, **_k):
            return {"messages": []}

    client = _TurnIdClient()

    async def _run():
        monkeypatch.setattr(mcp_server, "_get_client", lambda: client)
        monkeypatch.setattr(mcp_server, "effective_act_as", lambda uid: uid)
        running = await mcp_server.nymeria_chat(
            message="go", thread_id="thread-t", wait_seconds=1
        )
        client.release.set()
        return running

    running = asyncio.run(_run())

    assert running["status"] == "running"
    assert mcp_server._decode_resume(running["resume"])["turn_id"] == "turn-42"


# ---- #199: a blocking wait proves it is alive, and teaches polling ----


def test_a_long_wait_reports_progress_while_it_waits(monkeypatch):
    """The wait was one silent wait_for, so a client idle timeout aborted
    collects over healthy turns. Losing the notifications is the regression."""
    import nymeria.mcp_server as mcp_server

    calls: list[tuple[float, float]] = []

    class _Reporter:
        async def report_progress(self, progress, total=None, message=None):
            calls.append((progress, total))

    monkeypatch.setattr(mcp_server.mcp, "get_context", lambda: _Reporter())
    monkeypatch.setattr(mcp_server, "_PROGRESS_INTERVAL_S", 0.02)

    async def _run():
        loop = asyncio.get_running_loop()
        ctx = {"done_event": asyncio.Event()}
        started = loop.time()
        done = await mcp_server._await_dispatch(ctx, 0.2)
        return done, loop.time() - started

    done, elapsed = asyncio.run(_run())

    assert done is False
    assert len(calls) >= 2, "a wait that outlives the interval must notify"
    assert all(total == 0.2 for _, total in calls)
    progresses = [p for p, _ in calls]
    assert progresses == sorted(progresses), "progress must not run backwards"
    # The slicing must not stretch the budget: a regression that recomputed
    # the deadline per slice would wait forever and still notify happily.
    assert elapsed < 0.2 * 1.5


def test_the_replay_path_reports_progress_too(monkeypatch):
    """The thread_id-only collect is the advertised recovery path, so it must
    prove it is alive the same way the dispatch wait does (review catch)."""
    import nymeria.mcp_server as mcp_server

    calls: list[tuple[float, float]] = []

    class _Reporter:
        async def report_progress(self, progress, total=None, message=None):
            calls.append((progress, total))

    class _HangingReplayClient:
        async def stream_turn_replay(self, **_kwargs):
            yield {"type": "turn_attach", "state": "running"}
            yield {"type": "response", "content": "partial"}
            await asyncio.sleep(30)

    monkeypatch.setattr(mcp_server.mcp, "get_context", lambda: _Reporter())
    monkeypatch.setattr(mcp_server, "_PROGRESS_INTERVAL_S", 0.02)
    monkeypatch.setattr(mcp_server, "_get_client", lambda: _HangingReplayClient())

    events, error, state = asyncio.run(
        mcp_server._replay_turn_events("thread-x", "u1", budget=0.2)
    )

    assert error is None
    assert state == "running"
    assert [e["type"] for e in events] == ["response"], "the partial drain is kept"
    assert len(calls) >= 2, "the recovery path must notify while it waits"


def test_a_broken_progress_channel_never_breaks_the_wait(monkeypatch):
    """Progress is best-effort by contract: most clients send no
    progressToken, and a raise from the channel must not fail the collect."""
    import nymeria.mcp_server as mcp_server

    attempts = []

    class _Broken:
        async def report_progress(self, *args, **kwargs):
            attempts.append(args)
            raise RuntimeError("context is not available outside of a request")

    monkeypatch.setattr(mcp_server.mcp, "get_context", lambda: _Broken())
    monkeypatch.setattr(mcp_server, "_PROGRESS_INTERVAL_S", 0.02)

    async def _run():
        ctx = {"done_event": asyncio.Event()}

        async def release_soon():
            await asyncio.sleep(0.08)
            ctx["done_event"].set()

        releaser = asyncio.create_task(release_soon())
        done = await mcp_server._await_dispatch(ctx, 5)
        await releaser
        return done

    assert asyncio.run(_run()) is True
    assert len(attempts) == 1, "the channel is dropped on its first failure"


def test_an_unavailable_context_downgrades_to_a_plain_wait(monkeypatch):
    import nymeria.mcp_server as mcp_server

    def _boom():
        raise RuntimeError("no request context")

    monkeypatch.setattr(mcp_server.mcp, "get_context", _boom)
    monkeypatch.setattr(mcp_server, "_PROGRESS_INTERVAL_S", 0.02)

    async def _run():
        ctx = {"done_event": asyncio.Event()}
        ctx["done_event"].set()
        return await mcp_server._await_dispatch(ctx, 1)

    assert asyncio.run(_run()) is True


def test_collect_docstring_teaches_the_polling_shape():
    """Docstring-only fact: the polling pattern binds every client, including
    the ones progress notifications never reach."""
    import nymeria.mcp_server as mcp_server

    collect = " ".join((mcp_server.nymeria_chat_collect.__doc__ or "").split())
    assert "POLLING IS THE EXPECTED SHAPE" in collect
    assert "resume" in collect
    assert "idle ceiling" in collect or "idle timeout" in collect
    chat = " ".join((mcp_server.nymeria_chat.__doc__ or "").split())
    assert "progressToken" in chat
    assert "nymeria_chat_collect" in chat
