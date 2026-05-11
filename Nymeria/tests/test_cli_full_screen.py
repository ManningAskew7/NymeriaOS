from __future__ import annotations

import asyncio
import time

from cli_fixtures import FakeAgentClient, FakeTerminalCapabilities, simple_response_events

from nymeria.triggers.cli.commands import CommandRegistry
from nymeria.triggers.cli.commands import system as system_commands
from nymeria.triggers.cli.rendering.full_screen import (
    FullScreenPromptToolkitShell,
    FullScreenShellConfig,
    _style,
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


def test_full_screen_layout_keeps_status_above_framed_composer() -> None:
    shell = make_shell()

    app = shell.build_application()
    children = app.layout.container.children

    assert children[0] is shell.transcript_frame.container
    assert children[1] is shell.status_bar
    assert children[2] is shell.composer_frame.container
    assert shell.composer_frame.title == "Message"


def test_status_style_uses_default_background() -> None:
    color_attrs = _style(FakeTerminalCapabilities()).get_attrs_for_style_str(
        "class:status"
    )
    plain_attrs = _style(
        FakeTerminalCapabilities(supports_color=False)
    ).get_attrs_for_style_str("class:status")

    assert color_attrs.reverse is False
    assert color_attrs.bgcolor == ""
    assert plain_attrs.reverse is False
    assert plain_attrs.bgcolor == ""


def test_transcript_styles_keep_preamble_readable_and_thinking_lighter() -> None:
    style = _style(FakeTerminalCapabilities())
    preamble_attrs = style.get_attrs_for_style_str("class:transcript.preamble")
    final_attrs = style.get_attrs_for_style_str("class:transcript.final")
    thinking_attrs = style.get_attrs_for_style_str("class:transcript.thinking")

    assert preamble_attrs.color == final_attrs.color
    assert preamble_attrs.bgcolor == final_attrs.bgcolor
    assert thinking_attrs.color == "c0c0c0"


def test_full_screen_shell_streams_turn_into_transcript_and_ready_status() -> None:
    client = FakeAgentClient(default_stream=simple_response_events(("Hello", " there.")))
    shell = make_shell(client=client)

    result = run(shell.run_chat_turn("hello"))

    assert result is True
    assert client.chat_requests[0].message == "hello"
    assert client.chat_requests[0].thread_id == "thread-1"
    assert client.chat_requests[0].user_id == "alice"
    assert "──── You " in shell.transcript.text
    assert "\n  hello" in shell.transcript.text
    assert "──── Nymeria " in shell.transcript.text
    assert "\n  Hello there." in shell.transcript.text
    assert "Ready" in shell._status_text()


def test_full_screen_transcript_header_shows_live_activity_phase() -> None:
    shell = make_shell()
    now = time.monotonic()
    shell.state = start_turn(shell.state, "think", now=now)
    shell.state = reduce_stream_event(
        shell.state,
        {"type": "thinking", "content": "working"},
        now=now,
    )

    shell._refresh_transcript(now=now)

    assert "──── Nymeria · ⠋ Thinking... 0.0s " in shell.transcript.text
    assert "Thinking..." in shell._status_text()


def test_full_screen_transcript_header_updates_to_formulating() -> None:
    shell = make_shell()
    shell.state = start_turn(shell.state, "think", now=0.1)
    shell.state = reduce_stream_event(
        shell.state,
        {"type": "thinking", "content": "working"},
        now=1.0,
    )

    shell._refresh_transcript(now=1.5)
    thinking_text = shell.transcript.text
    shell._refresh_transcript(now=2.01)

    assert "Thinking... 1.4s " in thinking_text
    assert "Formulating... 1.9s " in shell.transcript.text


def test_full_screen_transcript_header_matches_waiting_detail() -> None:
    shell = make_shell()
    shell.state = start_turn(shell.state, "tool", now=0.1)
    shell.state = reduce_stream_event(
        shell.state,
        {
            "type": "tool_call",
            "id": "call-1",
            "name": "search_memory",
            "args": {"query": "private"},
        },
        now=1.1,
    )

    shell._refresh_transcript(now=1.2)
    header = next(line for line in shell.transcript.text.splitlines() if "Nymeria" in line)

    assert "Waiting... 1.1s search_memory " in header
    assert "private" not in header


def test_full_screen_shell_consumes_current_thread_autonomous_events() -> None:
    client = FakeAgentClient(
        autonomous_events=[
            {"type": "task_started", "thread_id": "other", "prompt": "Ignore me"},
            {
                "type": "task_started",
                "thread_id": "thread-1",
                "task_id": "todo-1",
                "todo_id": "todo-1",
                "prompt": "Work on TODO todo-1: Run a CLI smoke test",
            },
            {"type": "response", "thread_id": "thread-1", "content": "Smoke"},
            {
                "type": "task_completed",
                "thread_id": "thread-1",
                "task_id": "todo-1",
                "todo_id": "todo-1",
                "content": "Smoke test passed.",
            },
        ],
    )
    shell = make_shell(client=client)

    run(shell._consume_autonomous_stream())

    assert client.autonomous_requests[0]["user_id"] == "alice"
    assert "Nymeria · autonomous" in shell.transcript.text
    assert "Autonomous TODO started: Run a CLI smoke test" in shell.transcript.text
    assert "  Smoke test passed." in shell.transcript.text
    assert "Ignore me" not in shell.transcript.text


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
        "---- You -------------------------------------------------------------------------------------------",
        "  use a tool",
        "",
        "---- Nymeria ---------------------------------------------------------------------------------------",
        "  Thought: checking",
        "  - search_memory ok 0ms query=\"project status\" -> Found 2 matching notes.",
        "  I found the notes.",
    ]


def test_full_screen_verbose_command_only_changes_rendering() -> None:
    shell = make_shell()
    registry = CommandRegistry()
    system_commands.register(registry)
    shell.command_registry = registry
    shell.state = start_turn(shell.state, "use a tool", now=0.0)
    for event in [
        {"type": "thinking", "content": "private detail"},
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
        shell.state = reduce_stream_event(shell.state, event, now=1.0)
    shell._refresh_transcript()
    initial_state = shell.state

    status = run(shell._run_command("/verbose status"))
    on = run(shell._run_command("/verbose on"))
    verbose_text = shell.transcript.text
    off = run(shell._run_command("/verbose off"))

    assert status.ok is True
    assert on.ok is True
    assert off.ok is True
    assert "\n  Thought:\n    private detail" in verbose_text
    assert "  Thought: private detail" in shell.transcript.text
    assert shell.state == initial_state
    assert shell._transcript_verbose is False
