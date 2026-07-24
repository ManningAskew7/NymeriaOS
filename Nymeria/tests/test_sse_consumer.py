"""Tests for the shared SSE event consumer (triggers/sse_consumer.py)."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List


from nymeria.triggers.sse_consumer import (
    SSEEventHandler,
    consume_sse_stream,
    dispatch_event,
    format_auth_prompt_message,
    format_hook_approval_message,
    parse_attach_paths,
    parse_sse_data_line,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _aiter(events: List[Dict[str, Any]]):
    """Turn a list into an async iterable for consume_sse_stream."""
    for event in events:
        yield event


class RecordingHandler:
    """Captures every callback the dispatcher invokes."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.flush_count = 0

    async def flush_text(self, final: bool = False) -> None:
        self.flush_count += 1
        self.calls.append(("flush_text", {"final": final}))

    async def on_thinking(self) -> None:
        self.calls.append(("on_thinking", {}))

    async def on_response_chunk(self, content: str) -> None:
        self.calls.append(("on_response_chunk", {"content": content}))

    async def on_compacting(self, message: str) -> None:
        self.calls.append(("on_compacting", {"message": message}))

    async def on_compacted(
        self, summary: str, messages_removed: int, title: str
    ) -> None:
        self.calls.append(
            ("on_compacted", {
                "summary": summary,
                "messages_removed": messages_removed,
                "title": title,
            })
        )

    async def on_tool_call(
        self, name: str, args: Dict[str, Any], call_id: str, count: int
    ) -> None:
        self.calls.append(
            ("on_tool_call", {
                "name": name, "args": args, "call_id": call_id, "count": count,
            })
        )

    async def on_tool_result(
        self, call_id: str, result: str, attachments: List[str]
    ) -> None:
        self.calls.append(
            ("on_tool_result", {
                "call_id": call_id, "result": result, "attachments": attachments,
            })
        )

    async def on_tool_reload(self, tools: List[str], ttl: str) -> None:
        self.calls.append(("on_tool_reload", {"tools": tools, "ttl": ttl}))

    async def on_workspace_artifact(self, path: str) -> None:
        self.calls.append(("on_workspace_artifact", {"path": path}))

    async def on_error(self, content: str) -> None:
        self.calls.append(("on_error", {"content": content}))

    async def on_iteration_limit(self, content: str) -> None:
        self.calls.append(("on_iteration_limit", {"content": content}))

    async def on_turn_rewound(self, content: str) -> None:
        self.calls.append(("on_turn_rewound", {"content": content}))

    async def on_done(self, tool_call_count: int) -> None:
        self.calls.append(("on_done", {"tool_call_count": tool_call_count}))

    async def on_stream_end(self, tool_call_count: int) -> None:
        self.calls.append(("on_stream_end", {"tool_call_count": tool_call_count}))


# ---------------------------------------------------------------------------
# parse_attach_paths
# ---------------------------------------------------------------------------


def test_parse_attach_paths_extracts_paths():
    result = "Here is a file: [attach:/tmp/report.pdf] and [attach:/data/out.csv]"
    assert parse_attach_paths(result) == ["/tmp/report.pdf", "/data/out.csv"]


def test_parse_attach_paths_empty_string():
    assert parse_attach_paths("") == []


def test_parse_attach_paths_no_tags():
    assert parse_attach_paths("No attachments here.") == []


def test_parse_attach_paths_non_string():
    assert parse_attach_paths(42) == []
    assert parse_attach_paths(None) == []


# ---------------------------------------------------------------------------
# dispatch_event — event routing
# ---------------------------------------------------------------------------


def test_dispatch_thinking():
    h = RecordingHandler()
    count = asyncio.run(dispatch_event({"type": "thinking"}, h, 0))
    assert count == 0
    assert h.calls == [("on_thinking", {})]


def test_dispatch_tool_call_delta_maps_to_thinking():
    h = RecordingHandler()
    asyncio.run(dispatch_event({"type": "tool_call_delta"}, h, 0))
    assert h.calls == [("on_thinking", {})]


def test_dispatch_response_chunk():
    h = RecordingHandler()
    asyncio.run(dispatch_event({"type": "response", "content": "Hello"}, h, 0))
    assert h.calls == [("on_response_chunk", {"content": "Hello"})]


def test_dispatch_response_empty_content_ignored():
    h = RecordingHandler()
    asyncio.run(dispatch_event({"type": "response", "content": ""}, h, 0))
    assert h.calls == []


def test_format_auth_prompt_message_with_link():
    message = format_auth_prompt_message(
        {
            "type": "auth_prompt",
            "display_name": "GitHub",
            "connect_url": "https://nymeria.example.test/connect/credentials/p#tok",
            "expires_at": "2026-05-20T12:00:00+00:00",
        }
    )
    assert "Credential setup requested for GitHub" in message
    assert "https://nymeria.example.test/connect/credentials/p#tok" in message
    assert "Do not paste secrets into chat" in message


def test_format_auth_prompt_message_without_public_url():
    message = format_auth_prompt_message(
        {"type": "auth_prompt", "provider": "todoist"}
    )
    assert "NYMERIA_PUBLIC_URL" in message
    assert "Do not paste secrets into chat" in message


def test_format_auth_prompt_message_oauth_auth_code():
    message = format_auth_prompt_message(
        {
            "type": "auth_prompt",
            "mode": "oauth",
            "display_name": "Google Calendar",
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=x&state=y",
            "timeout_seconds": 180,
        }
    )
    assert "Sign in to Google Calendar" in message
    assert "https://accounts.google.com/o/oauth2/v2/auth?client_id=x&state=y" in message
    assert "expires in 3 minutes" in message
    assert "Do not share" in message


def test_format_auth_prompt_message_oauth_auth_code_missing_url():
    message = format_auth_prompt_message(
        {
            "type": "auth_prompt",
            "mode": "oauth",
            "display_name": "Google Calendar",
            "auth_url": "",
        }
    )
    assert "did not provide an authorization URL" in message


def test_format_auth_prompt_message_oauth_device_code():
    message = format_auth_prompt_message(
        {
            "type": "auth_prompt",
            "mode": "oauth_device",
            "display_name": "Microsoft Outlook",
            "user_code": "ABCD-1234",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
        }
    )
    assert "To connect Microsoft Outlook" in message
    assert "https://microsoft.com/devicelogin" in message
    assert "ABCD-1234" in message
    assert "expires in 15 minutes" in message


def test_format_auth_prompt_message_oauth_device_prefers_verification_uri_complete():
    message = format_auth_prompt_message(
        {
            "type": "auth_prompt",
            "mode": "oauth_device",
            "display_name": "Microsoft Outlook",
            "user_code": "ABCD-1234",
            "verification_uri": "https://microsoft.com/devicelogin",
            "verification_uri_complete": "https://microsoft.com/devicelogin?code=ABCD-1234",
            "expires_in": 600,
        }
    )
    assert "https://microsoft.com/devicelogin?code=ABCD-1234" in message
    assert "https://microsoft.com/devicelogin " not in message


def test_format_auth_prompt_message_oauth_device_missing_fields():
    message = format_auth_prompt_message(
        {
            "type": "auth_prompt",
            "mode": "oauth_device",
            "display_name": "Microsoft Outlook",
            "user_code": "",
            "verification_uri": "",
        }
    )
    assert "did not provide a user code" in message


def test_dispatch_auth_prompt_renders_default_message_and_flushes():
    h = RecordingHandler()
    asyncio.run(dispatch_event(
        {
            "type": "auth_prompt",
            "display_name": "GitHub",
            "connect_url": "https://nymeria.example.test/connect/credentials/p#tok",
        },
        h,
        0,
    ))
    assert h.calls[0] == ("flush_text", {"final": True})
    assert h.calls[1][0] == "on_response_chunk"
    assert "GitHub" in h.calls[1][1]["content"]
    assert h.calls[2] == ("flush_text", {"final": True})


def _hook_approval_event(**overrides) -> Dict[str, Any]:
    event = {
        "type": "hook_approval",
        "record_id": "rec-abc123",
        "user_id": "u1",
        "thread_id": "t1",
        "tool_name": "bash_execute",
        "tool_args_preview": '{"command": "rm -rf build"}',
        "prompt": "Approve tool call bash_execute?",
        "created_at": "2026-07-03T10:00:00+00:00",
        "expires_at": "2026-07-03T10:03:00+00:00",
    }
    event.update(overrides)
    return event


def test_format_hook_approval_message_full():
    message = format_hook_approval_message(_hook_approval_event())
    assert "bash_execute" in message
    assert "rm -rf build" in message
    assert "/hook approve rec-abc123" in message
    assert "/hook deny rec-abc123" in message
    assert "within 180 seconds" in message
    assert "denied" in message  # silence-is-denial is stated up front


def test_format_hook_approval_message_caps_args_preview():
    message = format_hook_approval_message(
        _hook_approval_event(tool_args_preview="x" * 2000)
    )
    assert "x" * 301 not in message
    assert "..." in message


def test_format_hook_approval_message_tolerates_missing_fields():
    message = format_hook_approval_message({"type": "hook_approval"})
    assert "a tool" in message
    assert "/hook approve" in message
    assert "within" not in message  # no stamps, no window claim


def test_dispatch_hook_approval_renders_default_message_and_flushes():
    h = RecordingHandler()
    asyncio.run(dispatch_event(_hook_approval_event(), h, 0))
    assert h.calls[0] == ("flush_text", {"final": True})
    assert h.calls[1][0] == "on_response_chunk"
    assert "bash_execute" in h.calls[1][1]["content"]
    assert h.calls[2] == ("flush_text", {"final": True})


def test_dispatch_hook_approval_prefers_handler_callback():
    h = RecordingHandler()
    seen = {}

    async def on_hook_approval(event):
        seen["event"] = event

    h.on_hook_approval = on_hook_approval
    asyncio.run(dispatch_event(_hook_approval_event(), h, 0))
    assert seen["event"]["record_id"] == "rec-abc123"
    # Default text must NOT also render when the surface has its own UI.
    assert all(name != "on_response_chunk" for name, _ in h.calls)


def test_dispatch_hook_approval_resolved_is_silent_by_default():
    h = RecordingHandler()
    asyncio.run(
        dispatch_event(
            {"type": "hook_approval_resolved", "record_id": "r", "outcome": "approved"},
            h,
            0,
        )
    )
    assert h.calls == []


def test_dispatch_hook_approval_resolved_calls_handler_callback():
    h = RecordingHandler()
    seen = {}

    async def on_hook_approval_resolved(event):
        seen["outcome"] = event.get("outcome")

    h.on_hook_approval_resolved = on_hook_approval_resolved
    asyncio.run(
        dispatch_event(
            {"type": "hook_approval_resolved", "record_id": "r", "outcome": "timeout"},
            h,
            0,
        )
    )
    assert seen["outcome"] == "timeout"


def test_dispatch_dispatched_emits_response_reference_line():
    h = RecordingHandler()
    asyncio.run(
        dispatch_event(
            {
                "type": "dispatched",
                "title": "Research",
                "dispatched_to": {"thread_id": "thread-2", "title": "Research"},
            },
            h,
            0,
        )
    )
    assert h.calls == [
        ("on_response_chunk", {"content": "[Response from Research]\n\n"})
    ]


def test_dispatch_compacting_flushes_first():
    h = RecordingHandler()
    asyncio.run(dispatch_event({"type": "compacting", "message": "Working..."}, h, 0))
    assert h.calls[0] == ("flush_text", {"final": True})
    assert h.calls[1] == ("on_compacting", {"message": "Working..."})


def test_dispatch_compacting_default_message():
    h = RecordingHandler()
    asyncio.run(dispatch_event({"type": "compacting"}, h, 0))
    assert h.calls[1] == ("on_compacting", {"message": "Compacting thread context..."})


def test_dispatch_compacted_flushes_first():
    h = RecordingHandler()
    asyncio.run(dispatch_event(
        {"type": "compacted", "summary": "S", "messages_removed": 5}, h, 0
    ))
    assert h.calls[0] == ("flush_text", {"final": True})
    assert h.calls[1] == ("on_compacted", {
        "summary": "S", "messages_removed": 5, "title": "Context compacted",
    })


def test_dispatch_context_attached_flushes_and_uses_title():
    h = RecordingHandler()
    asyncio.run(dispatch_event(
        {"type": "context_attached", "summary": "Prior work"}, h, 0
    ))
    assert h.calls[0] == ("flush_text", {"final": True})
    assert h.calls[1] == ("on_compacted", {
        "summary": "Prior work",
        "messages_removed": 0,
        "title": "Context summary attached",
    })


def test_dispatch_tool_call_increments_count():
    h = RecordingHandler()
    count = asyncio.run(dispatch_event(
        {"type": "tool_call", "name": "search", "args": {"q": "x"}, "id": "tc1"},
        h, 0,
    ))
    assert count == 1
    assert h.calls == [("on_tool_call", {
        "name": "search", "args": {"q": "x"}, "call_id": "tc1", "count": 1,
    })]


def test_dispatch_tool_call_count_threads_through():
    h = RecordingHandler()
    count = asyncio.run(dispatch_event(
        {"type": "tool_call", "name": "a", "args": {}, "id": "tc2"}, h, 3,
    ))
    assert count == 4
    assert h.calls[0][1]["count"] == 4


def test_dispatch_tool_result_extracts_attachments():
    h = RecordingHandler()
    asyncio.run(dispatch_event(
        {"type": "tool_result", "id": "tc1", "result": "Done [attach:/f.txt]"},
        h, 0,
    ))
    assert h.calls == [("on_tool_result", {
        "call_id": "tc1",
        "result": "Done [attach:/f.txt]",
        "attachments": ["/f.txt"],
    })]


def test_dispatch_tool_result_no_attachments():
    h = RecordingHandler()
    asyncio.run(dispatch_event(
        {"type": "tool_result", "id": "tc2", "result": "ok"}, h, 0,
    ))
    assert h.calls[0][1]["attachments"] == []


def test_dispatch_tool_reload_flushes_first():
    h = RecordingHandler()
    asyncio.run(dispatch_event(
        {"type": "tool_reload", "tools": ["calc"], "ttl": "turn"}, h, 0,
    ))
    assert h.calls[0] == ("flush_text", {"final": True})
    assert h.calls[1] == ("on_tool_reload", {"tools": ["calc"], "ttl": "turn"})


def test_dispatch_workspace_artifact():
    h = RecordingHandler()
    asyncio.run(dispatch_event(
        {"type": "workspace_artifact", "path": "/out/file.csv"}, h, 0,
    ))
    assert h.calls == [("on_workspace_artifact", {"path": "/out/file.csv"})]


def test_dispatch_workspace_artifact_empty_path_ignored():
    h = RecordingHandler()
    asyncio.run(dispatch_event(
        {"type": "workspace_artifact", "path": ""}, h, 0,
    ))
    assert h.calls == []


def test_dispatch_workspace_artifact_non_string_path_ignored():
    h = RecordingHandler()
    asyncio.run(dispatch_event(
        {"type": "workspace_artifact", "path": None}, h, 0,
    ))
    assert h.calls == []


def test_dispatch_error_flushes_first():
    h = RecordingHandler()
    asyncio.run(dispatch_event(
        {"type": "error", "content": "boom"}, h, 0,
    ))
    assert h.calls[0] == ("flush_text", {"final": True})
    assert h.calls[1] == ("on_error", {"content": "boom"})


def test_dispatch_error_default_message():
    h = RecordingHandler()
    asyncio.run(dispatch_event({"type": "error"}, h, 0))
    assert h.calls[1] == ("on_error", {"content": "Unknown error"})


def test_dispatch_iteration_limit():
    h = RecordingHandler()
    asyncio.run(dispatch_event(
        {"type": "iteration_limit", "content": "Too many"}, h, 0,
    ))
    assert h.calls == [("on_iteration_limit", {"content": "Too many"})]


def test_dispatch_iteration_limit_empty_content_ignored():
    h = RecordingHandler()
    asyncio.run(dispatch_event(
        {"type": "iteration_limit", "content": ""}, h, 0,
    ))
    assert h.calls == []


def test_dispatch_turn_rewound_flushes_then_delivers_content():
    """A refusal rewind (backlog #105) delivers its explanation to the bot."""
    h = RecordingHandler()
    asyncio.run(dispatch_event(
        {
            "type": "turn_rewound",
            "reason": "refusal",
            "content": "The classifier declined this turn.",
            "prompt": "the refused prompt",
        },
        h,
        0,
    ))
    assert h.calls == [
        ("flush_text", {"final": True}),
        ("on_turn_rewound", {"content": "The classifier declined this turn."}),
    ]


def test_dispatch_turn_rewound_empty_content_ignored():
    h = RecordingHandler()
    asyncio.run(dispatch_event({"type": "turn_rewound", "content": ""}, h, 0))
    assert h.calls == [("flush_text", {"final": True})]


def test_dispatch_turn_rewound_falls_back_without_handler_method():
    """An out-of-tree handler predating on_turn_rewound still delivers it."""

    class LegacyHandler:
        def __init__(self) -> None:
            self.calls: List[tuple] = []

        async def flush_text(self, final: bool = False) -> None:
            self.calls.append(("flush_text", {"final": final}))

        async def on_iteration_limit(self, content: str) -> None:
            self.calls.append(("on_iteration_limit", {"content": content}))

    h = LegacyHandler()
    asyncio.run(dispatch_event(
        {"type": "turn_rewound", "content": "rewound"}, h, 0,
    ))
    assert h.calls == [
        ("flush_text", {"final": True}),
        ("on_iteration_limit", {"content": "rewound"}),
    ]


def test_dispatch_done():
    h = RecordingHandler()
    asyncio.run(dispatch_event({"type": "done"}, h, 5))
    assert h.calls == [("on_done", {"tool_call_count": 5})]


def test_dispatch_unknown_event_ignored():
    h = RecordingHandler()
    count = asyncio.run(dispatch_event({"type": "queued"}, h, 2))
    assert count == 2
    assert h.calls == []


# ---------------------------------------------------------------------------
# consume_sse_stream — end-to-end integration
# ---------------------------------------------------------------------------


def test_consume_full_stream():
    h = RecordingHandler()
    events = [
        {"type": "thinking"},
        {"type": "response", "content": "Hi "},
        {"type": "tool_call", "name": "calc", "args": {"x": 1}, "id": "t1"},
        {"type": "tool_result", "id": "t1", "result": "42"},
        {"type": "response", "content": "answer is 42"},
        {"type": "done"},
    ]
    asyncio.run(consume_sse_stream(_aiter(events), h))

    names = [c[0] for c in h.calls]
    assert "on_thinking" in names
    assert "on_response_chunk" in names
    assert "on_tool_call" in names
    assert "on_tool_result" in names
    assert "on_done" in names
    assert names[-1] == "on_stream_end"


def test_consume_stream_counts_tools():
    h = RecordingHandler()
    events = [
        {"type": "tool_call", "name": "a", "args": {}, "id": "1"},
        {"type": "tool_result", "id": "1", "result": ""},
        {"type": "tool_call", "name": "b", "args": {}, "id": "2"},
        {"type": "tool_result", "id": "2", "result": ""},
        {"type": "done"},
    ]
    asyncio.run(consume_sse_stream(_aiter(events), h))

    tool_calls = [c for c in h.calls if c[0] == "on_tool_call"]
    assert tool_calls[0][1]["count"] == 1
    assert tool_calls[1][1]["count"] == 2

    done = [c for c in h.calls if c[0] == "on_done"][0]
    assert done[1]["tool_call_count"] == 2

    stream_end = [c for c in h.calls if c[0] == "on_stream_end"][0]
    assert stream_end[1]["tool_call_count"] == 2


def test_consume_stream_calls_stream_end_on_empty():
    h = RecordingHandler()
    asyncio.run(consume_sse_stream(_aiter([]), h))
    assert h.calls == [("on_stream_end", {"tool_call_count": 0})]


def test_consume_stream_flush_before_compacting():
    """Verify the dispatcher flushes before every compacting event."""
    h = RecordingHandler()
    events = [
        {"type": "response", "content": "text"},
        {"type": "compacting", "message": "Compacting..."},
        {"type": "compacted", "summary": "S", "messages_removed": 1},
        {"type": "done"},
    ]
    asyncio.run(consume_sse_stream(_aiter(events), h))

    # There should be a flush_text before on_compacting and on_compacted
    assert h.flush_count >= 2
    names = [c[0] for c in h.calls]
    compacting_idx = names.index("on_compacting")
    assert names[compacting_idx - 1] == "flush_text"
    compacted_idx = names.index("on_compacted")
    assert names[compacted_idx - 1] == "flush_text"


def test_consume_stream_flush_before_error():
    h = RecordingHandler()
    events = [
        {"type": "response", "content": "partial"},
        {"type": "error", "content": "oops"},
    ]
    asyncio.run(consume_sse_stream(_aiter(events), h))

    names = [c[0] for c in h.calls]
    error_idx = names.index("on_error")
    assert names[error_idx - 1] == "flush_text"


def test_consume_stream_flush_before_tool_reload():
    h = RecordingHandler()
    events = [
        {"type": "response", "content": "text"},
        {"type": "tool_reload", "tools": ["x"], "ttl": "session"},
    ]
    asyncio.run(consume_sse_stream(_aiter(events), h))

    names = [c[0] for c in h.calls]
    reload_idx = names.index("on_tool_reload")
    assert names[reload_idx - 1] == "flush_text"


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_recording_handler_satisfies_protocol():
    assert isinstance(RecordingHandler(), SSEEventHandler)


# ---------------------------------------------------------------------------
# SSE line parsing (parse_sse_data_line): the shared parser that
# api_client/trigger_api previously hand-rolled inline.
# ---------------------------------------------------------------------------


def test_parse_sse_data_line_decodes_data_payload():
    assert parse_sse_data_line('data: {"type": "done"}') == {"type": "done"}
    assert parse_sse_data_line('data: {"type": "response", "content": "hi"}') == {
        "type": "response",
        "content": "hi",
    }


def test_parse_sse_data_line_skips_non_data_lines():
    # Blank line, bare keepalive comment frame, and a non-data field line.
    assert parse_sse_data_line("") is None
    assert parse_sse_data_line(": keepalive") is None
    assert parse_sse_data_line("event: ping") is None


def test_parse_sse_data_line_skips_data_comment_and_bad_json():
    # ``data: :...`` comment payload and undecodable JSON both skip.
    assert parse_sse_data_line("data: :still-a-comment") is None
    assert parse_sse_data_line("data: not-json") is None


def test_parse_sse_data_line_skips_bare_null_payload():
    # Real events are always JSON objects; a bare ``data: null`` decodes to
    # None and is therefore treated as a skip (documented inert edge).
    assert parse_sse_data_line("data: null") is None


def test_parse_sse_data_line_preserves_falsy_non_null_payloads():
    # Falsy-but-not-None decoded values are returned, not skipped.
    assert parse_sse_data_line("data: false") is False
    assert parse_sse_data_line("data: 0") == 0
    assert parse_sse_data_line('data: ""') == ""
