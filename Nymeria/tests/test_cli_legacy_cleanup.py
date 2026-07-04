from __future__ import annotations

import asyncio
from pathlib import Path

from prompt_toolkit.formatted_text import to_formatted_text
from rich.console import Console

from cli_fixtures import (
    CapturedRenderOutput,
    DelayedEvent,
    FakeAgentClient,
    FakeTerminalCapabilities,
    run,
    simple_response_events,
    tool_call_result_events,
)

from nymeria.triggers.cli.app import CLIApp, CLIRuntimeConfig, _RichReplRuntime
from nymeria.triggers.cli.input import ComposerSubmission
from nymeria.triggers.cli.rendering.plain import PlainRenderer
from nymeria.triggers.cli.rendering.rich_repl import RichReplRenderer
from nymeria.triggers.cli.rendering.stream import StreamRenderer


class DummyAgent:
    pass


def make_app(client: FakeAgentClient) -> CLIApp:
    app = CLIApp(
        DummyAgent(),
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", transport="local"),
    )
    app._client = client
    app._maybe_auto_title = lambda _message: None
    return app


def test_repl_send_message_streams_through_agent_client_to_rich_renderer() -> None:
    client = FakeAgentClient(default_stream=simple_response_events(("Hello", " there.")))
    app = make_app(client)
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(supports_color=False),
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
    )

    app._send_message("hello", renderer)

    assert client.chat_requests[0].message == "hello"
    assert client.chat_requests[0].thread_id == "thread-1"
    assert client.chat_requests[0].user_id == "default"
    assert "Hello there." in output.stdout_text


def test_repl_submit_message_forwards_file_attachments_to_rich_transport(
    tmp_path: Path,
) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")
    client = FakeAgentClient(default_stream=simple_response_events(("Attached.",)))
    app = make_app(client)
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(supports_color=False),
        stdout=output.stdout,
        stderr=output.stderr,
        width=100,
    )

    app._submit_repl_message(f"summarize @{path}", renderer)

    assert client.chat_requests[0].message == "summarize"
    assert len(client.chat_requests[0].attachments) == 1
    assert client.chat_requests[0].attachments[0]["file_name"] == "notes.txt"
    assert "1 attachment" in output.stdout_text


def test_rich_runtime_consumes_current_thread_autonomous_events() -> None:
    client = FakeAgentClient(
        connection_label="api http://test",
        autonomous_events=[
            {"type": "task_started", "thread_id": "other", "prompt": "Ignore me"},
            {
                "type": "task_started",
                "thread_id": "thread-1",
                "task_id": "todo-1",
                "todo_id": "todo-1",
                "source": "scheduler",
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
    app = make_app(client)
    output = CapturedRenderOutput()
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(supports_color=False),
        stdout=output.stdout,
        stderr=output.stderr,
        width=110,
    )
    runtime = _RichReplRuntime(
        app=app,
        renderer=renderer,
        capabilities=FakeTerminalCapabilities(width=110, supports_color=False),
    )

    run(runtime._consume_autonomous_stream())

    assert client.autonomous_requests[0]["user_id"] == "default"
    assert client.autonomous_requests[0]["client_id"].startswith("cli-")
    assert "Nymeria · autonomous · scheduler" in output.stdout_text
    assert "Smoke test passed." in output.stdout_text
    assert "Ignore me" not in output.stdout_text


def test_rich_runtime_surfaces_threadless_autonomous_errors_in_status() -> None:
    client = FakeAgentClient(
        connection_label="api http://test",
        autonomous_events=[
            {
                "type": "error",
                "content": "stream dropped\nsecond line",
            },
        ],
    )
    app = make_app(client)
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(supports_color=False),
        stdout=CapturedRenderOutput().stdout,
        width=100,
    )
    runtime = _RichReplRuntime(
        app=app,
        renderer=renderer,
        capabilities=FakeTerminalCapabilities(width=120, supports_color=False),
    )

    run(runtime._consume_autonomous_stream())

    assert "Warning: stream dropped" in runtime.status_text()


def test_rich_runtime_prompt_fragments_keep_status_above_input() -> None:
    client = FakeAgentClient(connection_label="api http://test")
    app = make_app(client)
    renderer = RichReplRenderer(
        capabilities=FakeTerminalCapabilities(supports_color=False),
        stdout=CapturedRenderOutput().stdout,
        width=100,
    )
    runtime = _RichReplRuntime(
        app=app,
        renderer=renderer,
        capabilities=FakeTerminalCapabilities(width=120, supports_color=False),
    )

    fragments = runtime.prompt_fragments()
    newline_index = fragments.index(("", "\n"))

    assert isinstance(runtime.status_fragments(), list)
    assert to_formatted_text(runtime.status_fragments())
    assert fragments[0][0].startswith("class:status")
    assert "Nymeria" in "".join(text for _style, text in fragments[:newline_index])
    assert fragments[newline_index + 1] == ("class:prompt", "You: ")


def test_rich_async_submission_keeps_prompt_available_and_queues_next_turn() -> None:
    async def exercise() -> tuple[FakeAgentClient, _RichReplRuntime]:
        client = FakeAgentClient(
            streams={
                "first": [
                    DelayedEvent(
                        0.01,
                        {"type": "response", "content": "One", "thread_id": "thread-1"},
                    ),
                    {"type": "done", "thread_id": "thread-1", "tool_call_count": 0},
                ],
                "second": simple_response_events(("Two",), thread_id="thread-1"),
            },
            real_sleep=True,
        )
        app = make_app(client)
        renderer = RichReplRenderer(
            capabilities=FakeTerminalCapabilities(supports_color=False),
            stdout=CapturedRenderOutput().stdout,
            width=100,
        )
        runtime = _RichReplRuntime(
            app=app,
            renderer=renderer,
            capabilities=FakeTerminalCapabilities(width=120, supports_color=False),
        )

        await app._submit_rich_submission_async(
            ComposerSubmission("first"), renderer, runtime=runtime
        )
        await asyncio.sleep(0)
        assert runtime.current_turn_task is not None
        assert runtime.busy is True

        await app._submit_rich_submission_async(
            ComposerSubmission("second"), renderer, runtime=runtime
        )
        assert runtime.queued_count == 1

        await runtime.current_turn_task
        return client, runtime

    client, runtime = run(exercise())

    assert [request.message for request in client.chat_requests] == ["first", "second"]
    assert runtime.busy is False
    assert runtime.queued_count == 0


def test_repl_send_message_streams_through_agent_client_to_plain_renderer() -> None:
    client = FakeAgentClient(default_stream=tool_call_result_events())
    app = make_app(client)
    output = CapturedRenderOutput()
    renderer = PlainRenderer(stdout=output.stdout, stderr=output.stderr, width=100)

    app._send_message("use a tool", renderer)

    assert client.chat_requests[0].message == "use a tool"
    assert output.stdout_text == "I found the notes.\n"
    assert "search_memory" in output.stderr_text
    assert "\x1b" not in output.stdout_text
    assert "\x1b" not in output.stderr_text


def test_cli_app_no_longer_imports_raw_stream_bridge_or_stream_renderer() -> None:
    source = (
        Path(__file__).parents[1] / "nymeria/triggers/cli/app.py"
    ).read_text(encoding="utf-8")

    assert "iter_agent_astream" not in source
    assert "rendering.stream" not in source
    assert "StreamRenderer" not in source


def test_stream_renderer_import_is_reducer_backed_compatibility_shim() -> None:
    output = CapturedRenderOutput()
    state = type(
        "State",
        (),
        {
            "console": Console(
                file=output.stdout,
                force_terminal=False,
                no_color=True,
                width=100,
            )
        },
    )()
    renderer = StreamRenderer(state)

    renderer.render_stream(simple_response_events(("Shim", " response.")))

    assert "Shim response." in output.stdout_text
