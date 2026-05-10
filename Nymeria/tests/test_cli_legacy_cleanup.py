from __future__ import annotations

from pathlib import Path

from rich.console import Console

from cli_fixtures import (
    CapturedRenderOutput,
    FakeAgentClient,
    FakeTerminalCapabilities,
    simple_response_events,
    tool_call_result_events,
)

from nymeria.triggers.cli.app import CLIApp, CLIRuntimeConfig
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
