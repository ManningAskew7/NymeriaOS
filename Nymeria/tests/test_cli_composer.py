from __future__ import annotations

import asyncio
from pathlib import Path

from cli_fixtures import (
    DelayedEvent,
    FakeAgentClient,
    FakeTerminalCapabilities,
    simple_response_events,
)
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import to_formatted_text

from nymeria.triggers.cli.commands import Command, CommandRegistry
from nymeria.triggers.cli.app import (
    CLIApp,
    _RichReplPromptToolkitShell,
    _RichReplRuntime,
)
from nymeria.triggers.cli.input import (
    CommandCompleter,
    ComposerCompleter,
    ComposerController,
    ComposerSubmission,
    create_session,
    get_prompt,
    parse_composer_submission,
    sanitize_composer_text,
)
from nymeria.triggers.cli.rendering.rich_repl import RichReplRenderer
from nymeria.triggers.cli.state import CLIState
from nymeria.triggers.cli.rendering.full_screen import (
    FullScreenPromptToolkitShell,
    FullScreenShellConfig,
)


def run(coro):
    return asyncio.run(coro)


def test_command_completions_include_descriptions() -> None:
    registry = CommandRegistry()
    registry.register(
        Command(
            name="help",
            aliases=["/h"],
            description="Show help",
            handler=lambda _state, _args: None,
        )
    )
    registry.register(
        Command(
            name="threads",
            aliases=["/t"],
            description="Manage threads",
            handler=lambda _state, _args: None,
            subcommands={
                "list": Command(
                    name="list",
                    description="List threads",
                    handler=lambda _state, _args: None,
                )
            },
        )
    )

    root_completions = list(
        CommandCompleter(registry).get_completions(Document("/he", 3), None)
    )
    subcommand_completions = list(
        CommandCompleter(registry).get_completions(Document("/threads l", 10), None)
    )

    assert root_completions[0].text == "/help"
    assert root_completions[0].display_meta_text == "Show help"
    assert subcommand_completions[0].text == "/threads list"
    assert subcommand_completions[0].display_meta_text == "List threads"


def test_composer_completes_attachment_paths(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")

    completions = list(
        ComposerCompleter(cwd=tmp_path).get_completions(Document("@no", 3), None)
    )

    assert completions
    assert completions[0].text == "tes.txt"
    assert completions[0].display_meta_text == "attach file"


def test_parse_composer_submission_builds_file_attachment(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("hello", encoding="utf-8")

    submission = parse_composer_submission(f"summarize @{path}", cwd=tmp_path)

    assert submission.message == "summarize"
    assert submission.attachment_errors == ()
    assert len(submission.attachments) == 1
    assert submission.attachments[0]["file_type"] == "document"
    assert submission.attachments[0]["file_name"] == "notes.txt"
    assert submission.attachments[0]["data_url"].startswith("data:text/plain;base64,")


def test_parse_composer_submission_uses_attachment_placeholder(
    tmp_path: Path,
) -> None:
    path = tmp_path / "notes.md"
    path.write_text("# hello", encoding="utf-8")

    submission = parse_composer_submission(f"@{path}", cwd=tmp_path)

    assert submission.message == "[attachment]"
    assert len(submission.attachments) == 1


def test_parse_composer_submission_reports_missing_attachment(tmp_path: Path) -> None:
    submission = parse_composer_submission("read @missing.txt", cwd=tmp_path)

    assert submission.attachments == ()
    assert submission.attachment_errors
    assert "Attachment not found" in submission.attachment_errors[0]


def test_slash_command_submission_does_not_parse_attachment_tokens(
    tmp_path: Path,
) -> None:
    submission = parse_composer_submission("/mcp add @server/package", cwd=tmp_path)

    assert submission.message == "/mcp add @server/package"
    assert submission.attachments == ()
    assert submission.attachment_errors == ()


def test_sanitize_composer_text_keeps_pasted_multiline_as_one_buffer() -> None:
    text = "\x1b[200~first\r\nsecond\x00\x1b[201~"

    assert sanitize_composer_text(text) == "first\nsecond"


def test_enter_submits_and_trailing_backslash_inserts_newline() -> None:
    submissions: list[ComposerSubmission] = []
    controller = ComposerController(on_submit=submissions.append)
    buffer = controller.text_area.buffer

    buffer.text = "first line \\"
    buffer.cursor_position = len(buffer.text)
    assert controller.handle_enter(buffer) is True
    assert buffer.text == "first line \n"
    assert submissions == []

    buffer.text = "first line\nsecond line"
    buffer.cursor_position = len(buffer.text)
    assert controller.handle_enter(buffer) is True
    assert submissions[0].message == "first line\nsecond line"
    assert buffer.text == ""


def test_ctrl_c_stops_busy_turn_without_clearing_draft() -> None:
    stopped: list[bool] = []
    controller = ComposerController(
        on_stop=lambda: stopped.append(True),
        is_busy=lambda: True,
    )
    buffer = controller.text_area.buffer
    buffer.text = "draft while busy"

    assert controller.stop_or_clear(buffer) is True
    assert stopped == [True]
    assert buffer.text == "draft while busy"


def test_full_screen_prompt_fragments_use_composer_labels() -> None:
    ready = ComposerController()
    busy = ComposerController(is_busy=lambda: True)
    queued = ComposerController(queued_count=lambda: 2)
    error = ComposerController()
    error.last_attachment_errors = ("Attachment not found",)

    prompts = [
        ready.prompt_fragments(),
        busy.prompt_fragments(),
        queued.prompt_fragments(),
        error.prompt_fragments(),
    ]

    assert ready.prompt_fragments() == [("class:composer", "You: ")]
    assert busy.prompt_fragments() == [("class:composer.busy", "Busy: ")]
    assert queued.prompt_fragments() == [("class:composer.queued", "Queued 2: ")]
    assert error.prompt_fragments() == [("class:composer.error", "Error: ")]
    assert all(">" not in text for fragments in prompts for _, text in fragments)


def test_rich_repl_prompt_uses_chat_label_without_command_chevron() -> None:
    state = CLIState(None, thread_id="thread-1")

    ready_text = "".join(text for _, text in to_formatted_text(get_prompt(state)))
    busy_text = "".join(
        text for _, text in to_formatted_text(get_prompt(state, busy=True))
    )

    assert ready_text == "You: "
    assert busy_text == "Busy: "
    assert ">" not in ready_text + busy_text


def test_rich_repl_session_can_erase_live_status_prompt(tmp_path: Path) -> None:
    session = create_session(
        tmp_path,
        CommandRegistry(),
        erase_when_done=True,
    )

    assert session.app.erase_when_done is True


def test_rich_repl_application_keeps_status_above_multiline_chat_input(
    tmp_path: Path,
) -> None:
    capabilities = FakeTerminalCapabilities(width=100)
    cli_app = CLIApp(None, thread_id="thread-1")
    renderer = RichReplRenderer(capabilities=capabilities, width=100)
    runtime = _RichReplRuntime(
        app=cli_app,
        renderer=renderer,
        capabilities=capabilities,
    )
    shell = _RichReplPromptToolkitShell(
        cli_app=cli_app,
        runtime=runtime,
        renderer=renderer,
        capabilities=capabilities,
        history_path=tmp_path / "cli_history",
    )

    app = shell.build_application()
    children = app.layout.container.children

    assert app.full_screen is False
    assert children[0].content.text() == runtime.status_fragments()
    assert children[1] is shell.composer_controller.text_area.window
    assert shell.composer_controller.text_area.buffer.multiline()
    assert shell.composer_controller.prompt_fragments() == [
        ("class:composer", "You: ")
    ]
    runtime.set_busy(True)
    assert shell.composer_controller.prompt_fragments() == [
        ("class:composer.busy", "Busy: ")
    ]
    assert all(
        ">" not in text
        for fragments in (
            shell.composer_controller.prompt_fragments(),
            runtime.status_fragments(),
        )
        for _, text in fragments
    )


def test_rich_repl_queued_submissions_run_in_order() -> None:
    async def exercise() -> FakeAgentClient:
        client = FakeAgentClient(
            streams={
                "first": [
                    DelayedEvent(
                        0.01,
                        {"type": "response", "content": "One", "thread_id": "thread-1"},
                    ),
                    {"type": "done", "thread_id": "thread-1", "tool_call_count": 0},
                ],
                "second": simple_response_events(("Two",)),
            },
            real_sleep=True,
        )
        capabilities = FakeTerminalCapabilities(width=100)
        cli_app = CLIApp(None, thread_id="thread-1", user_id="alice")
        cli_app._client = client
        renderer = RichReplRenderer(capabilities=capabilities, width=100)
        runtime = _RichReplRuntime(
            app=cli_app,
            renderer=renderer,
            capabilities=capabilities,
        )

        await cli_app._submit_rich_submission_async(
            ComposerSubmission("first"),
            renderer,
            runtime=runtime,
        )
        await asyncio.sleep(0)
        assert runtime.busy is True
        await cli_app._submit_rich_submission_async(
            ComposerSubmission("second"),
            renderer,
            runtime=runtime,
        )
        assert runtime.queued_count == 1

        assert runtime.current_turn_task is not None
        await runtime.current_turn_task
        return client

    client = run(exercise())

    assert [request.message for request in client.chat_requests] == ["first", "second"]


def make_shell(client: FakeAgentClient) -> FullScreenPromptToolkitShell:
    return FullScreenPromptToolkitShell(
        client=client,
        capabilities=FakeTerminalCapabilities(width=100),
        config=FullScreenShellConfig(
            thread_id="thread-1",
            user_id="alice",
            model="test-model",
            thread_label="Fixture thread",
        ),
    )


def test_full_screen_composer_queues_submissions_while_busy() -> None:
    async def exercise() -> FakeAgentClient:
        client = FakeAgentClient(
            streams={
                "first": [
                    DelayedEvent(
                        0.01,
                        {"type": "response", "content": "One", "thread_id": "thread-1"},
                    ),
                    {"type": "done", "thread_id": "thread-1", "tool_call_count": 0},
                ],
                "second": simple_response_events(("Two",)),
            },
            real_sleep=True,
        )
        shell = make_shell(client)

        assert shell._handle_composer_submission(ComposerSubmission("first")) is True
        await asyncio.sleep(0)
        assert shell._busy is True
        assert shell._handle_composer_submission(ComposerSubmission("second")) is True
        assert "queued 1" in shell._status_text()

        assert shell._current_turn_task is not None
        await shell._current_turn_task
        return client

    client = run(exercise())

    assert [request.message for request in client.chat_requests] == ["first", "second"]


def test_full_screen_stop_preserves_current_composer_input() -> None:
    client = FakeAgentClient()
    shell = make_shell(client)
    shell._busy = True
    shell.composer.buffer.text = "do not clear this"

    assert run(shell.stop_current_turn()) is True

    assert client.stop_count == 1
    assert shell.composer.buffer.text == "do not clear this"
