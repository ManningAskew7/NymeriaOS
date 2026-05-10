from __future__ import annotations

import asyncio

from cli_fixtures import FakeAgentClient, FakeTerminalCapabilities, simple_response_events

from nymeria.triggers.cli.rendering.full_screen import (
    FullScreenPromptToolkitShell,
    FullScreenShellConfig,
    render_transcript,
)
from nymeria.triggers.cli.state import create_initial_state, reduce_stream_event, start_turn


def run(coro):
    return asyncio.run(coro)


def make_shell(
    *,
    capabilities: FakeTerminalCapabilities | None = None,
    client: FakeAgentClient | None = None,
) -> FullScreenPromptToolkitShell:
    return FullScreenPromptToolkitShell(
        client=client or FakeAgentClient(),
        capabilities=capabilities or FakeTerminalCapabilities(width=100),
        config=FullScreenShellConfig(
            thread_id="thread-1",
            user_id="alice",
            model="test-model",
            thread_label="Fixture thread",
        ),
    )


def test_full_screen_application_uses_prompt_toolkit_full_screen_when_allowed() -> None:
    shell = make_shell()

    app = shell.build_application()

    assert app.full_screen is True
    assert app.mouse_support() is True


def test_full_screen_application_respects_no_alt_screen_capability() -> None:
    shell = make_shell(
        capabilities=FakeTerminalCapabilities(
            supports_alt_screen=False,
            supports_mouse=False,
        )
    )

    app = shell.build_application()

    assert app.full_screen is False
    assert app.mouse_support() is False


def test_full_screen_shell_streams_turn_into_transcript_and_ready_status() -> None:
    client = FakeAgentClient(default_stream=simple_response_events(("Hello", " there.")))
    shell = make_shell(client=client)

    result = run(shell.run_chat_turn("hello"))

    assert result is True
    assert client.chat_requests[0].message == "hello"
    assert client.chat_requests[0].thread_id == "thread-1"
    assert client.chat_requests[0].user_id == "alice"
    assert "You: hello" in shell.transcript.text
    assert "Nymeria: Hello there." in shell.transcript.text
    assert "Ready" in shell._status_text()


def test_full_screen_transcript_renders_tool_rows_in_event_order() -> None:
    state = create_initial_state(thread_id="thread-1", user_id="alice", now=0.0)
    state = start_turn(state, "use a tool", now=0.1)
    for event in [
        {"type": "thinking", "content": "checking"},
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "search_memory",
            "args": {"query": "project status"},
        },
        {
            "type": "tool_result",
            "id": "call-1",
            "name": "search_memory",
            "result": "Found 2 matching notes.",
        },
        {"type": "response", "content": "I found the notes."},
        {"type": "done", "tool_call_count": 1},
    ]:
        state = reduce_stream_event(state, event, now=1.0)

    text = render_transcript(state, width=100)

    assert text.splitlines() == [
        "You: use a tool",
        "Thought",
        "> search_memory project status -> Found 2 matching notes.",
        "Nymeria: I found the notes.",
    ]
