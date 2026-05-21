from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from cli_fixtures import (
    DelayedEvent,
    FakeAgentClient,
    FakeTerminalCapabilities,
    simple_response_events,
)
from prompt_toolkit.document import Document
from prompt_toolkit.filters import is_done
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.layout import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.processors import TransformationInput
from rich.cells import cell_len

from nymeria.triggers.cli.commands import Command, CommandRegistry
from nymeria.triggers.cli.app import (
    CLIApp,
    CLIRuntimeConfig,
    _RichReplPromptToolkitShell,
    _RichReplRuntime,
)
from nymeria.triggers.cli.input import (
    CommandCompleter,
    ComposerCompleter,
    ComposerController,
    ComposerSubmission,
    SlashUsageHintProcessor,
    create_full_screen_composer,
    create_rich_repl_composer,
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


def _slash_registry() -> CommandRegistry:
    registry = CommandRegistry()
    registry.register(
        Command(
            name="loop",
            description="Run a prompt on a recurring interval",
            handler=lambda _state, _args: None,
        )
    )
    registry.register(
        Command(
            name="login",
            description="Authenticate with the API",
            handler=lambda _state, _args: None,
        )
    )
    return registry


def _usage_registry() -> CommandRegistry:
    registry = CommandRegistry()
    registry.register(
        Command(
            name="color",
            description="Set session color",
            usage="/color [red|blue|green|yellow|default]",
            handler=lambda _state, _args: None,
        )
    )
    return registry


def _slash_hint_transform(
    registry: CommandRegistry,
    text: str,
    *,
    width: int = 80,
    cursor_position: int | None = None,
    fragments: list[tuple[str, str]] | None = None,
    lineno: int = 0,
):
    document = Document(
        text,
        len(text) if cursor_position is None else cursor_position,
    )
    return SlashUsageHintProcessor(registry).apply_transformation(
        TransformationInput(
            buffer_control=SimpleNamespace(),
            document=document,
            lineno=lineno,
            source_to_display=lambda position: position,
            fragments=fragments if fragments is not None else [("", text)],
            width=width,
            height=1,
        )
    ).fragments


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
        ComposerCompleter(cwd=tmp_path).get_completions(
            Document("summarize @no", 13),
            None,
        )
    )

    assert completions
    assert completions[0].text == "tes.txt"
    assert completions[0].display_meta_text == "attach file"


def test_composer_does_not_complete_leading_thread_mention(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")

    completions = list(
        ComposerCompleter(cwd=tmp_path).get_completions(Document("@no", 3), None)
    )

    assert completions == []


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


def test_parse_composer_submission_preserves_leading_thread_mention(
    tmp_path: Path,
) -> None:
    path = tmp_path / "notes.md"
    path.write_text("# hello", encoding="utf-8")

    submission = parse_composer_submission(f"@{path}", cwd=tmp_path)

    assert submission.message == f"@{path}"
    assert submission.attachments == ()
    assert submission.attachment_errors == ()


def test_parse_composer_submission_preserves_quoted_thread_mention(
    tmp_path: Path,
) -> None:
    submission = parse_composer_submission('@"Research Notes" summarize', cwd=tmp_path)

    assert submission.message == '@"Research Notes" summarize'
    assert submission.attachments == ()
    assert submission.attachment_errors == ()


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

    assert ready.prompt_fragments() == [("class:composer", "› ")]
    assert busy.prompt_fragments() == [("class:composer.busy", "› ")]
    assert queued.prompt_fragments() == [("class:composer.queued", "› 2: ")]
    assert error.prompt_fragments() == [("class:composer.error", "› ")]
    assert all(">" not in text for fragments in prompts for _, text in fragments)


def test_slash_usage_hint_processor_appends_exact_command_hint() -> None:
    registry = _usage_registry()

    fragments = _slash_hint_transform(
        registry,
        "/color",
        fragments=[("class:text-area.prompt", "› "), ("", "/color")],
    )
    trailing_fragments = _slash_hint_transform(registry, "/color ")

    assert fragments[-1] == (
        "class:slash-hint",
        " [red|blue|green|yellow|default]",
    )
    assert trailing_fragments[-1] == (
        "class:slash-hint",
        "[red|blue|green|yellow|default]",
    )


def test_slash_usage_hint_processor_skips_ineligible_text() -> None:
    registry = _usage_registry()

    assert _slash_hint_transform(registry, "/co") == [("", "/co")]
    assert _slash_hint_transform(registry, "/color red") == [("", "/color red")]
    assert _slash_hint_transform(registry, " /color") == [("", " /color")]
    assert _slash_hint_transform(registry, "/color", cursor_position=3) == [
        ("", "/color")
    ]
    assert _slash_hint_transform(registry, "/color\n", lineno=1) == [
        ("", "/color\n")
    ]


def test_slash_usage_hint_processor_truncates_and_replaces_history_hint() -> None:
    registry = _usage_registry()

    fragments = _slash_hint_transform(
        registry,
        "/color",
        width=16,
        fragments=[("", "/color"), ("class:auto-suggestion", " red")],
    )

    assert all("auto-suggestion" not in style for style, _text in fragments)
    assert fragments[-1][0] == "class:slash-hint"
    assert cell_len("".join(text for _style, text in fragments)) <= 16


def test_inline_slash_usage_hints_are_rich_repl_only(tmp_path: Path) -> None:
    registry = _usage_registry()

    rich = create_rich_repl_composer(
        command_registry=registry,
        history_path=tmp_path / "rich_history",
    )
    full_screen = create_full_screen_composer(
        command_registry=registry,
        history_path=tmp_path / "full_history",
    )

    assert any(
        isinstance(processor, SlashUsageHintProcessor)
        for processor in rich.text_area.control.input_processors
    )
    assert not any(
        isinstance(processor, SlashUsageHintProcessor)
        for processor in full_screen.text_area.control.input_processors
    )


def test_composer_slash_panel_callbacks_are_gated() -> None:
    active = False
    moves: list[int] = []
    accepts: list[str] = []
    controller = ComposerController(
        slash_panel_is_active=lambda: active,
        on_slash_panel_move=moves.append,
        on_slash_panel_accept=lambda buffer: accepts.append(buffer.text),
    )
    buffer = controller.text_area.buffer
    buffer.text = "/lo"

    assert controller.slash_panel_navigation_enabled() is False
    assert controller.slash_panel_accept_enabled() is False
    assert controller.move_slash_panel_selection(1) is False
    assert controller.accept_slash_panel_selection(buffer) is False
    assert moves == []
    assert accepts == []

    active = True
    assert controller.slash_panel_navigation_enabled() is True
    assert controller.slash_panel_accept_enabled() is True
    assert controller.move_slash_panel_selection(1) is True
    assert controller.accept_slash_panel_selection(buffer) is True
    assert moves == [1]
    assert accepts == ["/lo"]


def test_enter_can_submit_slash_panel_selection() -> None:
    submissions: list[ComposerSubmission] = []

    def accept(buffer):
        buffer.text = "/loop"
        buffer.cursor_position = len(buffer.text)

    controller = ComposerController(
        on_submit=submissions.append,
        slash_panel_is_active=lambda: True,
        on_slash_panel_accept=accept,
    )
    buffer = controller.text_area.buffer
    buffer.text = "/lo"
    buffer.cursor_position = len(buffer.text)

    assert controller.submit_slash_panel_selection(buffer) is True

    assert [submission.message for submission in submissions] == ["/loop"]
    assert buffer.text == ""


def test_rich_repl_prompt_uses_chat_label_without_command_chevron() -> None:
    state = CLIState(None, thread_id="thread-1")

    ready_text = "".join(text for _, text in to_formatted_text(get_prompt(state)))
    busy_text = "".join(
        text for _, text in to_formatted_text(get_prompt(state, busy=True))
    )

    assert ready_text == "› "
    assert busy_text == "› "
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
    footer_spacer = children[0]
    assert isinstance(footer_spacer, Window)
    assert footer_spacer.height.weight == 1
    assert footer_spacer.height.preferred == 0
    assert not footer_spacer.dont_extend_height()
    assert footer_spacer.char == " "

    transcript_gap = children[1]
    assert isinstance(transcript_gap, ConditionalContainer)
    assert isinstance(transcript_gap.content, Window)
    assert transcript_gap.content.height.min == 1
    assert transcript_gap.content.height.max == 1
    assert transcript_gap.content.dont_extend_height()
    assert transcript_gap.content.char == " "
    assert transcript_gap.filter.filters[1].filter is is_done

    status_container = children[2]
    assert isinstance(status_container, ConditionalContainer)
    assert status_container.filter.filters[1].filter is is_done

    status_bar = status_container.content
    assert isinstance(status_bar, Window)
    assert status_bar.height.min == 1
    assert status_bar.height.max == 1
    assert status_bar.height.preferred == 1
    assert status_bar.dont_extend_height()
    assert not status_bar.wrap_lines()
    assert status_bar.char == " "
    assert status_bar.style == "class:status"
    assert status_bar.content.text() == runtime.status_fragments()

    input_area = children[3]
    assert isinstance(input_area, HSplit)

    slash_panel = children[4]
    assert isinstance(slash_panel, ConditionalContainer)
    assert isinstance(slash_panel.content, Window)
    assert slash_panel.content.style == "class:slash-panel"
    input_children = input_area.children
    assert len(input_children) == 3
    top_border = input_children[0]
    assert isinstance(top_border, ConditionalContainer)
    assert isinstance(top_border.content, Window)
    assert top_border.content.char == "─"
    assert input_children[1] is shell.composer_controller.text_area.window
    bottom_border = input_children[2]
    assert isinstance(bottom_border, ConditionalContainer)
    assert isinstance(bottom_border.content, Window)
    assert bottom_border.content.char == "─"
    assert input_area.style == "class:input-area"
    assert shell.composer_controller.text_area.window.dont_extend_height()
    assert not shell.composer_controller.text_area.window.height.preferred_specified
    assert shell.composer_controller.text_area.buffer.multiline()
    assert shell.composer_controller.prompt_fragments() == [
        ("class:composer", "› ")
    ]
    runtime.set_busy(True)
    assert shell.composer_controller.prompt_fragments() == [
        ("class:composer.busy", "› ")
    ]
    assert all(
        ">" not in text
        for fragments in (
            shell.composer_controller.prompt_fragments(),
            runtime.status_fragments(),
        )
        for _, text in fragments
    )


def test_rich_repl_slash_panel_navigation_fills_selected_command(
    tmp_path: Path,
) -> None:
    capabilities = FakeTerminalCapabilities(width=100)
    cli_app = CLIApp(None, thread_id="thread-1")
    cli_app.registry = _slash_registry()
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

    shell.build_application()
    buffer = shell.composer_controller.text_area.buffer
    buffer.text = "hello"
    assert shell.composer_controller.slash_panel_navigation_enabled() is False

    buffer.text = "/lo"
    assert shell.composer_controller.slash_panel_navigation_enabled() is True
    assert shell.composer_controller.move_slash_panel_selection(1) is True
    assert shell.composer_controller.accept_slash_panel_selection(buffer) is True

    assert buffer.text == "/loop"
    assert buffer.cursor_position == len("/loop")


def test_rich_repl_scroll_region_uses_footer_only_layout(tmp_path: Path) -> None:
    capabilities = FakeTerminalCapabilities(width=24, height=24, renderer="rich")
    cli_app = CLIApp(
        None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    renderer = RichReplRenderer(capabilities=capabilities, width=24)
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
    runtime.terminal_width = lambda: 24  # type: ignore[method-assign]
    shell.composer_controller.text_area.buffer.text = "abcdefghij " * 6

    assert runtime.scroll_region_enabled() is True
    assert len(children) == 4
    assert isinstance(children[0], ConditionalContainer)
    assert isinstance(children[1], ConditionalContainer)
    assert isinstance(children[2], HSplit)
    assert isinstance(children[3], ConditionalContainer)  # slash panel, below input
    assert runtime.composer_input_height() > 1
    # Composer text does not start with "/", so the panel is hidden
    assert runtime.footer_height() == runtime.composer_input_height() + 4
    assert shell.composer_controller.text_area.window.height().min == (
        runtime.composer_input_height()
    )


def test_rich_repl_status_fragments_refit_to_live_application_width() -> None:
    class FakeOutput:
        def __init__(self, columns: int) -> None:
            self.columns = columns

        def get_size(self) -> SimpleNamespace:
            return SimpleNamespace(columns=self.columns)

    capabilities = FakeTerminalCapabilities(width=100)
    cli_app = CLIApp(None, thread_id="thread-1")
    cli_app._repl_thread_label = "Resize status thread"
    cli_app._repl_model_label = "provider/model-with-extra-context"
    renderer = RichReplRenderer(capabilities=capabilities, width=100)
    runtime = _RichReplRuntime(
        app=cli_app,
        renderer=renderer,
        capabilities=capabilities,
    )
    fake_output = FakeOutput(columns=46)
    runtime.application = SimpleNamespace(output=fake_output)

    narrow_text = "".join(text for _style, text in runtime.status_fragments())
    fake_output.columns = 100
    wide_text = "".join(text for _style, text in runtime.status_fragments())

    assert cell_len(narrow_text) <= 46
    assert "provider/model-with-extra-context" not in narrow_text
    assert cell_len(wide_text) <= 100
    assert "provider/model-with-extra-context" in wide_text


def test_rich_repl_footer_height_gate_stays_visible_after_first_known_height() -> None:
    class FakeRenderer:
        height_is_known = False

    capabilities = FakeTerminalCapabilities(width=100)
    cli_app = CLIApp(None, thread_id="thread-1")
    renderer = RichReplRenderer(capabilities=capabilities, width=100)
    runtime = _RichReplRuntime(
        app=cli_app,
        renderer=renderer,
        capabilities=capabilities,
    )
    runtime.application = SimpleNamespace(renderer=FakeRenderer())

    assert runtime.footer_height_is_known() is False
    runtime.application.renderer.height_is_known = True
    assert runtime.footer_height_is_known() is True
    runtime.application.renderer.height_is_known = False
    assert runtime.footer_height_is_known() is True


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
