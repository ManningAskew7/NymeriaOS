from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from cli_fixtures import (  # type: ignore[import-not-found]
    DelayedEvent,
    FakeAgentClient,
    FakeTerminalCapabilities,
    run,
    simple_response_events,
)
from prompt_toolkit.completion import CompleteEvent
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
    create_rich_repl_composer,
    create_session,
    get_prompt,
    parse_composer_submission,
    sanitize_composer_text,
)
from nymeria.triggers.cli.rendering.rich_repl import RichReplRenderer
from nymeria.triggers.cli.state import CLIState


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
            buffer_control=SimpleNamespace(),  # type: ignore[bad-argument-type]
            document=document,
            lineno=lineno,
            source_to_display=lambda position: position,
            fragments=fragments if fragments is not None else [("", text)],  # type: ignore[bad-argument-type]
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
        CommandCompleter(registry).get_completions(Document("/he", 3), CompleteEvent())
    )
    subcommand_completions = list(
        CommandCompleter(registry).get_completions(
            Document("/threads l", 10), CompleteEvent()
        )
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
            CompleteEvent(),
        )
    )

    assert completions
    assert completions[0].text == "tes.txt"
    assert completions[0].display_meta_text == "attach file"


def test_composer_does_not_complete_leading_thread_mention(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")

    completions = list(
        ComposerCompleter(cwd=tmp_path).get_completions(Document("@no", 3), CompleteEvent())
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


def test_prompt_glyph_signals_form_capture() -> None:
    """While a form holds the composer, the glyph next to the caret says so:
    a pencil + field label on a text step, the radio glyph on a select or
    filter step (typing there filters the option list). The caret cannot
    move to the panel, so this is the one honest capture signal."""

    text_step = ComposerController(
        form_is_active=lambda: True,
        form_has_navigable_list=lambda: False,
        active_field_label=lambda: "Redirect URL or code",
    )
    select_step = ComposerController(
        form_is_active=lambda: True,
        form_has_navigable_list=lambda: True,
    )
    busy_select = ComposerController(
        form_is_active=lambda: True,
        form_has_navigable_list=lambda: True,
        is_busy=lambda: True,
    )

    assert text_step.prompt_fragments() == [
        ("class:composer.form", "✎ Redirect URL or code › ")
    ]
    assert select_step.prompt_fragments() == [("class:composer.form", "◉ › ")]
    # Busy merges into the style, never hides the capture glyph.
    assert busy_select.prompt_fragments() == [("class:composer.busy", "◉ › ")]


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

    assert all("auto-suggestion" not in fragment[0] for fragment in fragments)
    assert fragments[-1][0] == "class:slash-hint"
    assert cell_len("".join(fragment[1] for fragment in fragments)) <= 16


def test_inline_slash_usage_hints_present_in_rich_composer(tmp_path: Path) -> None:
    registry = _usage_registry()

    rich = create_rich_repl_composer(
        command_registry=registry,
        history_path=tmp_path / "rich_history",
    )

    assert any(
        isinstance(processor, SlashUsageHintProcessor)
        for processor in (rich.text_area.control.input_processors or [])
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

    ready_text = "".join(
        fragment[1] for fragment in to_formatted_text(get_prompt(state))
    )
    busy_text = "".join(
        fragment[1] for fragment in to_formatted_text(get_prompt(state, busy=True))
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
    assert shell.composer_controller is not None
    children = app.layout.container.children

    assert app.full_screen is False
    footer_spacer = children[0]
    assert isinstance(footer_spacer, Window)
    assert footer_spacer.height.weight == 1  # type: ignore[missing-attribute]
    assert footer_spacer.height.preferred == 0  # type: ignore[missing-attribute]
    assert not footer_spacer.dont_extend_height()
    assert footer_spacer.char == " "

    transcript_gap = children[1]
    assert isinstance(transcript_gap, ConditionalContainer)
    assert isinstance(transcript_gap.content, Window)
    assert transcript_gap.content.height.min == 1  # type: ignore[missing-attribute]
    assert transcript_gap.content.height.max == 1  # type: ignore[missing-attribute]
    assert transcript_gap.content.dont_extend_height()
    assert transcript_gap.content.char == " "
    assert transcript_gap.filter.filters[1].filter is is_done  # type: ignore[missing-attribute]

    status_container = children[2]
    assert isinstance(status_container, ConditionalContainer)
    assert status_container.filter.filters[1].filter is is_done  # type: ignore[missing-attribute]

    status_bar = status_container.content
    assert isinstance(status_bar, Window)
    assert status_bar.height.min == 1  # type: ignore[missing-attribute]
    assert status_bar.height.max == 1  # type: ignore[missing-attribute]
    assert status_bar.height.preferred == 1  # type: ignore[missing-attribute]
    assert status_bar.dont_extend_height()
    assert not status_bar.wrap_lines()
    assert status_bar.char == " "
    assert status_bar.style == "class:status"
    assert status_bar.content.text() == runtime.status_fragments()  # type: ignore[missing-attribute]

    input_area = children[3]
    assert isinstance(input_area, HSplit)

    under_status_bar = children[4]
    assert isinstance(under_status_bar, ConditionalContainer)
    assert isinstance(under_status_bar.content, Window)
    assert under_status_bar.content.style == "class:status"
    # Hidden until a /statusbar layout configures under-prompt segments.
    assert not under_status_bar.filter()

    slash_panel = children[5]
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
    assert shell.composer_controller is not None
    buffer = shell.composer_controller.text_area.buffer
    buffer.text = "hello"
    assert shell.composer_controller.slash_panel_navigation_enabled() is False

    buffer.text = "/lo"
    assert shell.composer_controller.slash_panel_navigation_enabled() is True
    assert shell.composer_controller.move_slash_panel_selection(1) is True
    assert shell.composer_controller.accept_slash_panel_selection(buffer) is True

    assert buffer.text == "/loop"
    assert buffer.cursor_position == len("/loop")


def test_active_form_suppresses_slash_panel(tmp_path: Path) -> None:
    from nymeria.triggers.cli.commands.base import CommandResult
    from nymeria.triggers.cli.rendering.form_panel import (
        FormField,
        FormSpec,
        FormTab,
    )

    capabilities = FakeTerminalCapabilities(width=80, height=24, renderer="rich")
    cli_app = CLIApp(
        None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich"),
    )
    renderer = RichReplRenderer(capabilities=capabilities, width=80)
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
    assert shell.composer_controller is not None
    buffer = shell.composer_controller.text_area.buffer

    buffer.text = "/mo"
    assert runtime.slash_panel_visible() is True
    assert runtime.slash_panel_selectable() is True

    async def _confirm(_result) -> CommandResult:
        return CommandResult.completed()

    runtime.open_form(
        FormSpec(
            title="Pick",
            tabs=(FormTab(label="T", fields=(FormField(kind="radio", key="m"),)),),
            on_confirm=_confirm,
        )
    )
    # Even a slash-prefixed filter must not wake the slash panel while a form
    # owns the composer (otherwise Tab would overwrite the filter text).
    buffer.text = "/mo"
    assert runtime.form_is_active() is True
    assert runtime.slash_panel_visible() is False
    assert runtime.slash_panel_selectable() is False


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
    assert shell.composer_controller is not None
    children = app.layout.container.children
    runtime.terminal_width = lambda: 24  # type: ignore[method-assign]
    shell.composer_controller.text_area.buffer.text = "abcdefghij " * 6

    assert runtime.scroll_region_enabled() is True
    assert len(children) == 7
    assert isinstance(children[0], ConditionalContainer)
    assert isinstance(children[1], ConditionalContainer)
    assert isinstance(children[2], HSplit)
    assert isinstance(children[3], ConditionalContainer)  # under-prompt status bar
    assert isinstance(children[4], ConditionalContainer)  # slash panel, below input
    assert isinstance(children[5], ConditionalContainer)  # form panel, below slash panel
    assert isinstance(children[6], ConditionalContainer)  # queued panel, bottommost
    assert runtime.composer_input_height() > 1
    # Composer text does not start with "/", no form is open, and nothing is
    # queued, so all three panels are hidden and contribute zero footer height.
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




def test_rich_repl_queued_panel_tracks_queue_and_footer_height() -> None:
    capabilities = FakeTerminalCapabilities(width=80, height=24, renderer="rich")
    cli_app = CLIApp(
        None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich", rich_scroll_region=True),
    )
    renderer = RichReplRenderer(capabilities=capabilities, width=80)
    runtime = _RichReplRuntime(
        app=cli_app,
        renderer=renderer,
        capabilities=capabilities,
    )

    assert runtime.queued_panel_visible() is False
    assert runtime.queued_panel_height() == 0
    empty_footer = runtime.footer_height()

    runtime.queue_submission(ComposerSubmission("first queued"))
    runtime.queue_submission(ComposerSubmission("second queued"))

    assert runtime.queued_panel_visible() is True
    assert runtime.queued_panel_height() == 2
    assert runtime.footer_height() == empty_footer + 2
    rendered = "".join(text for _style, text in runtime.queued_panel_fragments())
    assert "queued 1: first queued" in rendered
    assert "queued 2: second queued" in rendered

    # Draining the queue hides the panel again and releases the footer rows.
    assert runtime.next_queued_submission().message == "first queued"
    assert runtime.queued_panel_height() == 1
    assert runtime.next_queued_submission().message == "second queued"
    assert runtime.queued_panel_visible() is False
    assert runtime.footer_height() == empty_footer


def test_secret_field_masks_the_value_but_never_the_prompt() -> None:
    """The field label must not be inside the maskable render chain.

    `TextArea(prompt=...)` becomes a BeforeInput processor, and TextArea places
    it AHEAD of caller-supplied input_processors, so the secret-field
    mask processor masked the prompt along with the value: a form's field
    label rendered as a run of bullets with no visible caret, which reads as
    text that cannot be deleted (reported from a real login: 23 bullets, the
    exact width of "Redirect URL or code > "). The prompt therefore renders via
    get_line_prefix, outside the processor chain.

    This asserts the composed render, not the fragments in isolation: the
    fragment-level tests could not see the ordering bug that caused it.
    """

    controller = ComposerController(
        form_is_active=lambda: True,
        form_has_navigable_list=lambda: False,
        active_field_label=lambda: "Redirect URL or code",
        active_field_is_secret=lambda: True,
    )
    controller.text_area.buffer.text = "4/0AXsecret"

    async def _render() -> str:
        # create_content applies the processor chain (and needs a running loop
        # for the buffer's history load).
        content = controller.text_area.control.create_content(80, 5)
        return "".join(text for _style, text in content.get_line(0))

    rendered = asyncio.run(_render())
    prefix = "".join(text for _style, text in controller._line_prefix(0, 0))

    # The value is masked in the processor-transformed content except its
    # last 4 characters (a fully masked paste is unverifiable), and ONLY the
    # value: an exact count also catches a prompt rendered twice (once
    # through each path), which would mask extra characters here.
    assert rendered.count("•") == len("4/0AXsecret") - 4
    assert rendered.startswith("•" * (len("4/0AXsecret") - 4) + "cret")
    assert "4/0AXsecret" not in rendered
    # ...and the label is readable, outside that content, where no mask
    # reaches (the pencil glyph marks the composer as the form's field).
    assert prefix == "✎ Redirect URL or code › "
    assert "Redirect" not in rendered
    assert "•" not in prefix


def test_short_secret_values_mask_fully() -> None:
    """A tail-reveal on a short secret would be no mask at all."""

    controller = ComposerController(
        form_is_active=lambda: True,
        form_has_navigable_list=lambda: False,
        active_field_label=lambda: "PIN",
        active_field_is_secret=lambda: True,
    )
    controller.text_area.buffer.text = "hunter2"

    async def _render() -> str:
        content = controller.text_area.control.create_content(80, 5)
        return "".join(text for _style, text in content.get_line(0))

    rendered = asyncio.run(_render())

    assert rendered.count("•") == len("hunter2")
    assert "hunter2" not in rendered
    assert "ter2" not in rendered


def test_multiline_secret_reveals_a_tail_only_on_the_last_line() -> None:
    """The tail reveal is per BUFFER, not per logical line.

    The mask processor runs once per line, so a per-line tail would reveal
    the last 4 characters of EVERY line of a multi-line paste (8 leaked
    chars on a 2-line value instead of 4).
    """

    controller = ComposerController(
        form_is_active=lambda: True,
        form_has_navigable_list=lambda: False,
        active_field_label=lambda: "Redirect URL or code",
        active_field_is_secret=lambda: True,
    )
    first, second = "4/0AXfirstline", "secondline"
    controller.text_area.buffer.text = f"{first}\n{second}"

    async def _render() -> list[str]:
        content = controller.text_area.control.create_content(80, 5)
        return [
            "".join(text for _style, text in content.get_line(lineno))
            for lineno in range(2)
        ]

    line0, line1 = asyncio.run(_render())

    # First line: fully masked, no tail.
    assert line0.count("•") == len(first)
    assert "line" not in line0
    # Last line: masked except the 4-char tail (rstrip: the composed render
    # pads the line with a trailing space cell).
    assert line1.count("•") == len(second) - 4
    assert line1.rstrip().endswith("line")
    assert second not in line1


def _busy_phase_runtime() -> _RichReplRuntime:
    capabilities = FakeTerminalCapabilities(width=80, height=24, renderer="rich")
    cli_app = CLIApp(None, thread_id="thread-1")
    renderer = RichReplRenderer(capabilities=capabilities, width=80)
    return _RichReplRuntime(
        app=cli_app,
        renderer=renderer,
        capabilities=capabilities,
    )


def _form_shell_runtime(tmp_path: Path) -> _RichReplRuntime:
    """A runtime with a REAL composer buffer (the draft stash reads and
    writes it), built the way the layout tests build theirs."""

    capabilities = FakeTerminalCapabilities(width=80, height=24, renderer="rich")
    cli_app = CLIApp(
        None,
        thread_id="thread-1",
        runtime_config=CLIRuntimeConfig(renderer="rich"),
    )
    renderer = RichReplRenderer(capabilities=capabilities, width=80)
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
    return runtime


def _radio_form(title: str, on_confirm):
    from nymeria.triggers.cli.rendering.form_panel import (
        FormField,
        FormOption,
        FormSpec,
        FormTab,
    )

    return FormSpec(
        title=title,
        tabs=(
            FormTab(
                label="T",
                fields=(
                    FormField(
                        kind="radio",
                        key="pick",
                        options=(FormOption(id="a", label="a"),),
                    ),
                ),
            ),
        ),
        on_confirm=on_confirm,
    )


def test_form_submit_keeps_the_panel_busy_until_the_result() -> None:
    """Submitting no longer drops the panel before the backend answers: it
    stays up as a frozen snapshot (busy footer), interaction no-ops, and the
    result closes it (the OAuth paste step awaits up to ~10s server-side)."""

    from nymeria.triggers.cli.commands.base import CommandResult
    from nymeria.triggers.cli.rendering import form_panel

    runtime = _busy_phase_runtime()
    observed: dict[str, object] = {}

    async def _confirm(_result) -> CommandResult:
        observed["busy"] = runtime.form_is_busy()
        observed["active"] = runtime.form_is_active()
        rendered = "".join(
            text
            for _style, text in form_panel.form_panel_fragments(
                runtime._active_form, runtime._form_state, width=60
            )
        )
        observed["busy_footer"] = "Working…" in rendered
        # Mid-flight interaction is inert. A second Enter returns True (the
        # Enter binding chains on return value, so True is what CONSUMES the
        # key; False would fall through and submit composer text as chat)
        # while scheduling nothing.
        observed["resubmit"] = runtime.schedule_form_submit()
        observed["move"] = runtime.move_form_selection(1)
        observed["toggle"] = runtime.toggle_form_option()
        return CommandResult.completed()

    runtime.open_form(_radio_form("Pick", _confirm))
    run(runtime._submit_form_async())

    assert observed == {
        "busy": True,
        "active": True,
        "busy_footer": True,
        "resubmit": True,
        "move": False,
        "toggle": False,
    }
    # The result arrived and nothing replaced the form: it closes.
    assert runtime.form_is_active() is False


def test_chain_step_result_replaces_the_busy_form_in_place() -> None:
    from nymeria.triggers.cli.commands.base import CommandResult

    runtime = _busy_phase_runtime()

    async def _next_confirm(_result) -> CommandResult:
        return CommandResult.completed()

    next_spec = _radio_form("Step 2", _next_confirm)

    async def _confirm(_result) -> CommandResult:
        # The backend's next chain step opens its form during the await.
        runtime.open_form(next_spec)
        return CommandResult.completed()

    runtime.open_form(_radio_form("Step 1", _confirm))
    run(runtime._submit_form_async())

    # The submitted form is NOT closed over the replacement: the next step
    # stays open, fresh (not busy), ready for input.
    assert runtime.form_is_active() is True
    assert runtime._active_form is next_spec
    assert runtime.form_is_busy() is False


def test_escape_dismisses_a_busy_form_without_killing_the_result() -> None:
    from nymeria.triggers.cli.commands.base import CommandResult

    runtime = _busy_phase_runtime()

    async def _confirm(_result) -> CommandResult:
        assert runtime.request_form_cancel() is True
        return CommandResult.completed()

    runtime.open_form(_radio_form("Pick", _confirm))
    run(runtime._submit_form_async())

    assert runtime.form_is_active() is False


def test_text_typed_during_the_busy_window_never_reaches_the_chat_composer(
    tmp_path: Path,
) -> None:
    """The composer stays live while the panel is busy (the OAuth confirm
    window is ~10s), so a late paste lands form-scoped, possibly secret.
    It must be wiped with the close, never left as a ready-to-send chat
    message: one Enter would ship it to the agent AND the plaintext
    history file, unmasked."""

    from nymeria.triggers.cli.commands.base import CommandResult

    runtime = _form_shell_runtime(tmp_path)

    async def _confirm(_result) -> CommandResult:
        # Typed or pasted while the step is in flight.
        runtime._set_composer_text("sk-secret-pasted-late")
        return CommandResult.completed()

    runtime.open_form(_radio_form("Pick", _confirm))
    run(runtime._submit_form_async())

    assert runtime.form_is_active() is False
    assert runtime._composer_text() == ""


def test_form_over_half_typed_chat_stashes_and_restores_the_draft(
    tmp_path: Path,
) -> None:
    """An event-opened form (hook approval, consent prompt) landing mid-typing
    must not eat the chat draft: the composer is the only text buffer, so the
    draft is stashed on open and restored, cursor included, when the form
    session ends."""

    from nymeria.triggers.cli.commands.base import CommandResult

    runtime = _form_shell_runtime(tmp_path)

    async def _confirm(_result) -> CommandResult:
        return CommandResult.completed()

    runtime._set_composer_text("half-typed chat message")
    runtime._set_composer_cursor(4)

    runtime.open_form(_radio_form("Approve?", _confirm))
    # The form owns an empty composer while open.
    assert runtime._composer_text() == ""

    run(runtime._submit_form_async())

    assert runtime.form_is_active() is False
    assert runtime._composer_text() == "half-typed chat message"
    assert runtime._composer_cursor() == 4


def test_form_cancel_restores_the_draft_over_form_typed_text(
    tmp_path: Path,
) -> None:
    from nymeria.triggers.cli.commands.base import CommandResult

    runtime = _form_shell_runtime(tmp_path)

    async def _confirm(_result) -> CommandResult:
        return CommandResult.completed()

    runtime._set_composer_text("draft for the agent")
    runtime.open_form(_radio_form("Pick", _confirm))
    runtime._set_composer_text("form filter text")

    # No running prompt_toolkit loop in the test: the dismissal note task
    # cannot be scheduled, and it is not what this test observes.
    runtime.application = None
    assert runtime.request_form_cancel() is True
    assert runtime._composer_text() == "draft for the agent"


def test_chain_replacement_preserves_the_draft_to_the_session_end(
    tmp_path: Path,
) -> None:
    from nymeria.triggers.cli.commands.base import CommandResult

    runtime = _form_shell_runtime(tmp_path)

    async def _next_confirm(_result) -> CommandResult:
        return CommandResult.completed()

    async def _confirm(_result) -> CommandResult:
        runtime.open_form(_radio_form("Step 2", _next_confirm))
        return CommandResult.completed()

    runtime._set_composer_text("keep me")
    runtime.open_form(_radio_form("Step 1", _confirm))
    run(runtime._submit_form_async())

    # Step 2 replaced the busy form: the draft is still stashed, not eaten.
    assert runtime.form_is_active() is True
    assert runtime._composer_text() == ""
    run(runtime._submit_form_async())
    assert runtime._composer_text() == "keep me"
