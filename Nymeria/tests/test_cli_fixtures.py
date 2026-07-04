"""Self-tests for reusable CLI/TUI fixture helpers."""

from __future__ import annotations

import asyncio

import pytest

from cli_fixtures import (
    CapturedRenderOutput,
    FakeAgentClient,
    FakeClock,
    FakeTerminalCapabilities,
    async_event_stream,
    event_sequence,
    strip_delays,
    sync_event_stream,
)


async def _collect_stream(items, *, clock: FakeClock | None = None):
    return [event async for event in async_event_stream(items, clock=clock)]


def test_named_event_sequences_are_available_and_isolated() -> None:
    first = event_sequence("tool_call_result")
    second = event_sequence("tool_call_result")

    first[0]["name"] = "mutated"

    assert strip_delays(second)[0]["name"] == "search_memory"
    assert [event["type"] for event in strip_delays(second)] == [
        "tool_call",
        "tool_result",
        "response",
        "done",
    ]


def test_delayed_async_stream_advances_fake_clock_without_real_sleep() -> None:
    clock = FakeClock()
    script = event_sequence("thinking_silence", delay_seconds=1.5)

    events = asyncio.run(_collect_stream(script, clock=clock))

    assert [event["type"] for event in events] == ["thinking", "response", "done"]
    assert clock.sleep_calls == [1.5]
    assert clock.monotonic() == pytest.approx(1.5)


def test_sync_stream_advances_fake_clock_for_legacy_renderers() -> None:
    clock = FakeClock()
    script = event_sequence("thinking_silence", delay_seconds=0.75)

    events = list(sync_event_stream(script, clock=clock))

    assert [event["type"] for event in events] == ["thinking", "response", "done"]
    assert clock.monotonic() == pytest.approx(0.75)


def test_fake_agent_client_records_requests_and_serves_message_streams() -> None:
    client = FakeAgentClient(
        streams={
            "use tool": event_sequence("tool_call_result"),
        },
        context_stats={"owner:thread-a": {"used_tokens": 10, "max_tokens": 100}},
    )

    async def run():
        events = [
            event
            async for event in client.stream_chat(
                "use tool",
                thread_id="thread-a",
                user_id="owner",
                attachments=[{"file_name": "note.txt"}],
                force=True,
            )
        ]
        stop_result = await client.stop("thread-a", user_id="owner")
        stats = await client.get_context_stats("thread-a", user_id="owner")
        history = await client.get_history("thread-a", user_id="owner")
        threads = await client.list_threads("owner")
        return events, stop_result, stats, history, threads

    events, stop_result, stats, history, threads = asyncio.run(run())

    assert [event["type"] for event in events] == [
        "tool_call",
        "tool_result",
        "response",
        "done",
    ]
    assert client.chat_requests[0].message == "use tool"
    assert client.chat_requests[0].attachments == ({"file_name": "note.txt"},)
    assert client.chat_requests[0].options == {"force": True}
    assert client.stop_count == 1
    assert client.stop_requests[0].user_id == "owner"
    assert stop_result == {"ok": True, "thread_id": "thread-a"}
    assert stats == {"used_tokens": 10, "max_tokens": 100}
    assert history == {"messages": []}
    assert threads == [
        {"id": "thread-1", "title": "Fixture thread", "user_id": "owner"}
    ]


def test_fake_terminal_capabilities_capture_common_fallbacks() -> None:
    default = FakeTerminalCapabilities()
    dumb = default.with_overrides(term="dumb")
    forced = default.with_overrides(no_color=True, force_color=True)

    assert default.is_interactive is True
    assert default.prefers_plain_renderer is False
    assert dumb.is_interactive is False
    assert dumb.prefers_plain_renderer is True
    assert forced.color_enabled is True
    assert default.with_overrides(no_color=True).color_enabled is False
    assert default.with_overrides(stdin_isatty=False).animation_enabled is False


def test_captured_render_output_keeps_stdout_and_stderr_separate() -> None:
    output = CapturedRenderOutput()

    output.write_stdout("answer", end="\n")
    output.write_stderr("status", end="\n")

    assert output.stdout_text == "answer\n"
    assert output.stderr_text == "status\n"

    output.clear()

    assert output.stdout_text == ""
    assert output.stderr_text == ""


def test_unknown_event_sequence_reports_known_names() -> None:
    with pytest.raises(ValueError, match="simple_response"):
        event_sequence("missing")

