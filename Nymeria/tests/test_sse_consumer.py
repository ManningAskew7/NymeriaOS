"""Tests for the shared SSE event consumer (triggers/sse_consumer.py)."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

from nymeria.triggers.sse_consumer import (
    SSEEventHandler,
    consume_sse_stream,
    dispatch_event,
    parse_attach_paths,
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


def test_dispatch_compacting_flushes_first():
    h = RecordingHandler()
    asyncio.run(dispatch_event({"type": "compacting", "message": "Working..."}, h, 0))
    assert h.calls[0] == ("flush_text", {"final": True})
    assert h.calls[1] == ("on_compacting", {"message": "Working..."})


def test_dispatch_compacting_default_message():
    h = RecordingHandler()
    asyncio.run(dispatch_event({"type": "compacting"}, h, 0))
    assert h.calls[1] == ("on_compacting", {"message": "Compacting context..."})


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
