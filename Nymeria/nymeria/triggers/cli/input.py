"""prompt_toolkit integration: history, autocomplete, keybindings."""

from __future__ import annotations

import mimetypes
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory, ConditionalAutoSuggest
from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import (
    ConditionalProcessor,
    Processor,
    Transformation,
)
from prompt_toolkit.widgets import TextArea
from rich.cells import cell_len

from ..attachment_helpers import build_attachment
from .rendering.markdown import truncate_cell_width
from .rendering.slash_panel import slash_usage_hint
from .theme import CLITheme, DEFAULT_CLI_THEME

if TYPE_CHECKING:
    from prompt_toolkit.buffer import Buffer
    from prompt_toolkit.document import Document
    from prompt_toolkit.completion import CompleteEvent
    from .state import CLIState
    from .commands import Command, CommandRegistry


SubmitHandler = Callable[["ComposerSubmission"], bool | None]
ErrorHandler = Callable[[str], None]
StopHandler = Callable[[], bool | None]
StateGetter = Callable[[], bool]
CountGetter = Callable[[], int]
LabelGetter = Callable[[], str]
SlashPanelMoveHandler = Callable[[int], bool | None]
SlashPanelAcceptHandler = Callable[["Buffer"], bool | None]
FormMoveHandler = Callable[[int], bool | None]
FormTabHandler = Callable[[int], bool | None]
FormActionHandler = Callable[[], bool | None]

_SHIFT_ENTER_CSI = "\x1b[13;2u"
if _SHIFT_ENTER_CSI not in ANSI_SEQUENCES:
    ANSI_SEQUENCES[_SHIFT_ENTER_CSI] = Keys.ControlJ

_BRACKETED_PASTE_START = "\x1b[200~"
_BRACKETED_PASTE_END = "\x1b[201~"
_ATTACHMENT_TOKEN_RE = re.compile(r"(?<!\S)@(?P<path>\S+)")


@dataclass(frozen=True, slots=True)
class ComposerSubmission:
    """Parsed full-screen composer input."""

    message: str
    attachments: tuple[dict[str, Any], ...] = ()
    raw_text: str = ""
    attachment_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedAttachmentToken:
    """A file attachment token found in composer text."""

    token: str
    path: Path
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ComposerPromptState:
    """Small render-state object for the composer prompt."""

    busy: bool = False
    queued_count: int = 0
    attachment_errors: tuple[str, ...] = field(default_factory=tuple)


class CommandCompleter(Completer):
    """Tab-completes all registered /commands and subcommands."""

    def __init__(self, registry: "CommandRegistry") -> None:
        self._registry = registry
        self._metadata = _command_completion_metadata(registry)

    def get_completions(
        self, document: "Document", complete_event: "CompleteEvent"
    ):
        text = document.text_before_cursor.lstrip()
        if not text.startswith("/"):
            return

        candidates = self._registry.get_completions()

        # If the text already has a space, we're completing subcommands
        if " " in text:
            for candidate in candidates:
                if candidate.startswith(text) and candidate != text:
                    yield Completion(
                        candidate,
                        start_position=-len(text),
                        display_meta=self._metadata.get(candidate, ""),
                    )
        else:
            for candidate in candidates:
                if candidate.startswith(text) and candidate != text:
                    yield Completion(
                        candidate,
                        start_position=-len(text),
                        display_meta=self._metadata.get(candidate, ""),
                    )


class ComposerCompleter(Completer):
    """Complete slash commands and non-leading @file attachment paths."""

    def __init__(
        self,
        registry: "CommandRegistry | None" = None,
        *,
        cwd: Path | None = None,
    ) -> None:
        self.command_completer = CommandCompleter(registry) if registry else None
        self.cwd = cwd or Path.cwd()
        self.path_completer = PathCompleter(
            get_paths=lambda: [str(self.cwd)],
            expanduser=True,
        )

    def get_completions(
        self, document: "Document", complete_event: "CompleteEvent"
    ):
        text = document.text_before_cursor.lstrip()
        if text.startswith("/") and self.command_completer is not None:
            yield from self.command_completer.get_completions(document, complete_event)
            return

        token = _token_before_cursor(document.text_before_cursor)
        if not token.startswith("@"):
            return
        token_start = len(document.text_before_cursor) - len(token)
        mention_start = _leading_thread_mention_start(document.text_before_cursor)
        if mention_start is not None and token_start == mention_start:
            return

        from prompt_toolkit.document import Document

        path_fragment = token[1:]
        path_document = Document(path_fragment, cursor_position=len(path_fragment))
        for completion in self.path_completer.get_completions(
            path_document,
            complete_event,
        ):
            yield Completion(
                completion.text,
                start_position=completion.start_position,
                display=completion.display,
                display_meta="attach file",
            )


class SlashUsageHintProcessor(Processor):
    """Append a muted usage suffix for exact slash-command paths."""

    def __init__(self, registry: "CommandRegistry") -> None:
        self.registry = registry

    def apply_transformation(self, transformation_input) -> Transformation:
        ti = transformation_input
        if ti.document.line_count != 1 or ti.lineno != 0:
            return Transformation(list(ti.fragments))
        if not ti.document.is_cursor_at_the_end:
            return Transformation(list(ti.fragments))

        hint = slash_usage_hint(ti.document.text_before_cursor, self.registry)
        if not hint:
            return Transformation(list(ti.fragments))

        fragments = _without_auto_suggestion(ti.fragments)
        used_width = sum(cell_len(text) for _style, text in fragments)
        available_width = max(0, int(ti.width or 0) - used_width)
        if available_width <= 0:
            return Transformation(cast(Any, fragments))

        fitted_hint = truncate_cell_width(hint, available_width)
        if not fitted_hint.strip():
            return Transformation(cast(Any, fragments))
        return Transformation(cast(Any, fragments + [("class:slash-hint", fitted_hint)]))


class PartialMaskProcessor(Processor):
    """Mask a secret value except its last few characters.

    A fully masked paste is unverifiable: 40 or 400 bullets look the same,
    and a wrong paste (the auth URL instead of the callback, a truncated
    key) is invisible until it fails server-side. Showing only the tail
    (the grok-TUI token-mask idiom) lets the user confirm WHAT landed while
    the value stays out of shoulder-surf view; short values mask fully,
    because a 6-char secret with 4 visible is no mask at all. History and
    form payloads never see the value regardless (form Enter bypasses
    history; secrets are never echoed into payloads).
    """

    def __init__(
        self,
        char: str = "•",
        *,
        visible_tail: int = 4,
        min_masked_length: int = 9,
    ) -> None:
        self.char = char
        self.visible_tail = visible_tail
        self.min_masked_length = min_masked_length

    def apply_transformation(self, transformation_input) -> Transformation:
        ti = transformation_input
        fragments = list(ti.fragments)
        total = sum(len(text) for _style, text, *_rest in fragments)
        # The processor runs once per LOGICAL LINE, so the tail reveal must
        # be scoped to the buffer's last line only: revealing "the last 4
        # chars" of every line of a multi-line paste leaks 4 chars per line.
        # The short-value gate is on the whole buffer for the same reason.
        is_last_line = ti.lineno == ti.document.line_count - 1
        visible_from = (
            total - self.visible_tail
            if is_last_line and len(ti.document.text) >= self.min_masked_length
            else total
        )
        masked: list[Any] = []
        position = 0
        for style, text, *rest in fragments:
            replaced = "".join(
                ch if position + index >= visible_from else self.char
                for index, ch in enumerate(text)
            )
            masked.append((style, replaced, *rest))
            position += len(text)
        return Transformation(cast(Any, masked))


class ComposerController:
    """Bottom composer widget plus input behavior for the full-screen shell."""

    def __init__(
        self,
        *,
        command_registry: "CommandRegistry | None" = None,
        history_path: Path | None = None,
        cwd: Path | None = None,
        on_submit: SubmitHandler | None = None,
        on_error: ErrorHandler | None = None,
        on_stop: StopHandler | None = None,
        is_busy: StateGetter | None = None,
        queued_count: CountGetter | None = None,
        slash_panel_is_active: StateGetter | None = None,
        on_slash_panel_move: SlashPanelMoveHandler | None = None,
        on_slash_panel_accept: SlashPanelAcceptHandler | None = None,
        form_is_active: StateGetter | None = None,
        form_tab_enabled: StateGetter | None = None,
        form_has_navigable_list: StateGetter | None = None,
        active_field_label: LabelGetter | None = None,
        active_field_is_checkbox: StateGetter | None = None,
        active_field_is_secret: StateGetter | None = None,
        on_form_move: FormMoveHandler | None = None,
        on_form_tab: FormTabHandler | None = None,
        on_form_toggle: FormActionHandler | None = None,
        on_form_submit: FormActionHandler | None = None,
        on_form_cancel: FormActionHandler | None = None,
        multiline: bool = False,
        show_queued_prompt: bool = True,
        show_slash_usage_hints: bool = False,
        max_height: int = 6,
    ) -> None:
        self.cwd = cwd or Path.cwd()
        self.on_submit = on_submit
        self.on_error = on_error
        self.on_stop = on_stop
        self.is_busy = is_busy or (lambda: False)
        self.queued_count = queued_count or (lambda: 0)
        self.slash_panel_is_active = slash_panel_is_active or (lambda: False)
        self.on_slash_panel_move = on_slash_panel_move
        self.on_slash_panel_accept = on_slash_panel_accept
        self.form_is_active = form_is_active or (lambda: False)
        self.form_tab_enabled = form_tab_enabled or (lambda: False)
        self.form_has_navigable_list = form_has_navigable_list or (lambda: False)
        self.active_field_label = active_field_label or (lambda: "")
        self.active_field_is_checkbox = active_field_is_checkbox or (lambda: False)
        self.active_field_is_secret = active_field_is_secret or (lambda: False)
        self.on_form_move = on_form_move
        self.on_form_tab = on_form_tab
        self.on_form_toggle = on_form_toggle
        self.on_form_submit = on_form_submit
        self.on_form_cancel = on_form_cancel
        self.show_queued_prompt = show_queued_prompt
        self.last_attachment_errors: tuple[str, ...] = ()
        self.key_bindings = self._build_key_bindings()
        input_height = (
            Dimension(min=1, max=max(1, max_height))
            if multiline
            else Dimension(min=1, max=max(1, max_height), preferred=1)
        )
        input_processors: list[Processor] = []
        if show_slash_usage_hints and command_registry is not None:
            input_processors.append(SlashUsageHintProcessor(command_registry))
        # Secret form fields (e.g. the /provider setup API-key step) mask the
        # composer's own echo, except the value's tail (see the processor
        # docstring: a fully masked paste is unverifiable). History is safe
        # structurally (form Enter routes to submit_form_selection, never
        # handle_enter / append_to_history), and history GHOSTS are
        # suppressed below so a prior prompt cannot render as gray
        # suggestion text mid-secret.
        input_processors.append(
            ConditionalProcessor(
                PartialMaskProcessor("•"),
                Condition(lambda: bool(self.active_field_is_secret())),
            )
        )
        self.text_area = TextArea(
            height=input_height,
            dont_extend_height=multiline,
            # NB: no prompt= here. TextArea turns that into a BeforeInput
            # processor and places it AHEAD of caller-supplied
            # input_processors, so the mask below would mask the prompt too.
            # The prompt renders through get_line_prefix instead; see
            # _line_prefix.
            multiline=multiline,
            wrap_lines=True,
            get_line_prefix=self._line_prefix,
            history=_history(history_path),
            auto_suggest=ConditionalAutoSuggest(
                AutoSuggestFromHistory(),
                Condition(lambda: not self.active_field_is_secret()),
            ),
            completer=ComposerCompleter(command_registry, cwd=self.cwd),
            complete_while_typing=False,
            accept_handler=self.handle_enter,
            input_processors=input_processors,
            name="nymeria-composer",
        )

    @property
    def prompt_state(self) -> ComposerPromptState:
        return ComposerPromptState(
            busy=self.is_busy(),
            queued_count=self.queued_count(),
            attachment_errors=self.last_attachment_errors,
        )

    def prompt_fragments(self):
        state = self.prompt_state
        if state.attachment_errors:
            return [("class:composer.error", "› ")]
        if self.show_queued_prompt and state.queued_count:
            return [("class:composer.queued", f"› {state.queued_count}: ")]
        # While a form step feeds off this buffer, say so HERE: the caret
        # cannot move to the panel (one focused widget), so the glyph next to
        # the caret is the only honest signal of where keystrokes land (the
        # hermes prompt-glyph idiom). A text step names the field it is
        # feeding (the composer is the ONLY surface that shows the value; the
        # panel reports its length); a select/filter step gets the radio
        # glyph, because typing there filters the option list. Busy is not
        # dropped for it, just merged: a chained step submits and waits for
        # the next one, so the label and the in-flight turn are both true at
        # once and the label carries the busy STYLE rather than losing to it.
        field_label = str(self.active_field_label() or "").strip()
        if field_label:
            style = "class:composer.busy" if state.busy else "class:composer.form"
            return [(style, f"✎ {field_label} › ")]
        if self.form_active():
            style = "class:composer.busy" if state.busy else "class:composer.form"
            return [(style, "◉ › ")]
        if state.busy:
            return [("class:composer.busy", "› ")]
        return [("class:composer", "› ")]

    def _line_prefix(self, line_number: int, wrap_count: int) -> Any:
        if wrap_count > 0 or line_number > 0:
            prompt_width = sum(len(text) for _, text in self.prompt_fragments())
            return [("", " " * prompt_width)]
        # The prompt renders HERE rather than through ``TextArea(prompt=...)``.
        # That argument becomes a BeforeInput processor which TextArea puts
        # BEFORE any caller-supplied input_processors, so the secret-field
        # mask processor masked the prompt along with the value: a form's
        # field label came out as a run of bullets with no caret, which reads as
        # text that cannot be deleted. get_line_prefix is applied at render
        # time, outside the processor chain, so a mask can never reach it.
        return self.prompt_fragments()

    def submit_buffer(self, buffer: "Buffer") -> bool:
        submission = parse_composer_submission(buffer.text, cwd=self.cwd)
        self.last_attachment_errors = submission.attachment_errors
        if submission.attachment_errors:
            self._emit_error(submission.attachment_errors[0])
            return False
        if not submission.message.strip() and not submission.attachments:
            buffer.reset()
            return True

        accepted = True
        if self.on_submit is not None:
            accepted = self.on_submit(submission) is not False
        if not accepted:
            return False

        buffer.append_to_history()
        buffer.reset()
        self.last_attachment_errors = ()
        return True

    def handle_enter(self, buffer: "Buffer") -> bool:
        """Submit on Enter or insert a fallback newline after a trailing backslash."""

        if _ends_with_unescaped_backslash(buffer.document.text_before_cursor):
            cursor = buffer.cursor_position
            before = buffer.text[:cursor]
            after = buffer.text[cursor:]
            slash_index = len(before.rstrip()) - 1
            replacement = f"{before[:slash_index]}\n"
            buffer.text = f"{replacement}{after}"
            buffer.cursor_position = len(replacement)
            return True
        return self.submit_buffer(buffer)

    def insert_newline(self, buffer: "Buffer") -> None:
        cursor = buffer.cursor_position
        buffer.text = f"{buffer.text[:cursor]}\n{buffer.text[cursor:]}"
        buffer.cursor_position = cursor + 1

    def stop_or_clear(self, buffer: "Buffer") -> bool:
        if self.is_busy():
            if self.on_stop is None:
                return False
            return self.on_stop() is not False
        buffer.reset()
        return True

    def open_external_editor(self, buffer: "Buffer") -> None:
        buffer.open_in_editor(validate_and_handle=False)

    def slash_panel_navigation_enabled(self) -> bool:
        return self.on_slash_panel_move is not None and self.slash_panel_is_active()

    def slash_panel_accept_enabled(self) -> bool:
        return self.on_slash_panel_accept is not None and self.slash_panel_is_active()

    def move_slash_panel_selection(self, delta: int) -> bool:
        if not self.slash_panel_navigation_enabled() or self.on_slash_panel_move is None:
            return False
        return self.on_slash_panel_move(delta) is not False

    def accept_slash_panel_selection(self, buffer: "Buffer") -> bool:
        if not self.slash_panel_accept_enabled() or self.on_slash_panel_accept is None:
            return False
        return self.on_slash_panel_accept(buffer) is not False

    def submit_slash_panel_selection(self, buffer: "Buffer") -> bool:
        if not self.accept_slash_panel_selection(buffer):
            return False
        return self.submit_buffer(buffer)

    def form_active(self) -> bool:
        return bool(self.form_is_active())

    def form_move_enabled(self) -> bool:
        """Up/Down drive the option list, and only when the active step HAS one.

        A text-only step (the OAuth paste rail) has no list, so without this gate
        the arrows are swallowed by a no-op handler. Such a step routes them to
        :meth:`form_caret_move_enabled` instead, NOT to the editor default.
        """

        return (
            self.on_form_move is not None
            and self.form_active()
            and bool(self.form_has_navigable_list())
        )

    def form_caret_move_enabled(self) -> bool:
        """Up/Down move the caret on a text step, and must never reach history.

        prompt_toolkit's default Up/Down are ``auto_up``/``auto_down``, which
        recall shell history whenever the cursor sits on the first LOGICAL line
        (wrapping does not save it: a 330-char value on one logical line still
        counts). The composer is backed by a real ``FileHistory``, so simply
        leaving these keys unbound on a text step replaces a pasted single-use
        OAuth code with a previous prompt: the same value destruction the
        per-step draft stash exists to prevent, arriving through a different
        door. Bind plain caret movement instead, which is a harmless no-op on a
        single-line value and still walks a multi-line one.
        """

        return self.form_active() and not bool(self.form_has_navigable_list())

    def form_tab_nav_enabled(self) -> bool:
        return self.on_form_tab is not None and bool(self.form_tab_enabled())

    # Left/Right have to serve two masters: navigating the step rail, and moving
    # the caret through a typed value (a pasted OAuth callback is ~330 chars).
    # They cross to the neighbouring step only FROM the respective boundary, so
    # editing wins wherever there is text under the caret and an empty field
    # still steps on the first press.

    def _composer_buffer(self) -> "Buffer | None":
        return getattr(getattr(self, "text_area", None), "buffer", None)

    def composer_cursor_at_start(self) -> bool:
        buffer = self._composer_buffer()
        if buffer is None:
            return True
        return int(getattr(buffer, "cursor_position", 0) or 0) <= 0

    def composer_cursor_at_end(self) -> bool:
        buffer = self._composer_buffer()
        if buffer is None:
            return True
        text = str(getattr(buffer, "text", "") or "")
        return int(getattr(buffer, "cursor_position", 0) or 0) >= len(text)

    def form_tab_prev_enabled(self) -> bool:
        return self.form_tab_nav_enabled() and self.composer_cursor_at_start()

    def form_tab_next_enabled(self) -> bool:
        return self.form_tab_nav_enabled() and self.composer_cursor_at_end()

    def form_toggle_enabled(self) -> bool:
        return (
            self.on_form_toggle is not None
            and self.form_active()
            and bool(self.active_field_is_checkbox())
        )

    def form_submit_enabled(self) -> bool:
        return self.on_form_submit is not None and self.form_active()

    def form_cancel_enabled(self) -> bool:
        return self.on_form_cancel is not None and self.form_active()

    def move_form_selection(self, delta: int) -> bool:
        if not self.form_move_enabled() or self.on_form_move is None:
            return False
        return self.on_form_move(delta) is not False

    def move_form_tab(self, delta: int) -> bool:
        if not self.form_tab_nav_enabled() or self.on_form_tab is None:
            return False
        return self.on_form_tab(delta) is not False

    def toggle_form_option(self) -> bool:
        if not self.form_toggle_enabled() or self.on_form_toggle is None:
            return False
        return self.on_form_toggle() is not False

    def submit_form_selection(self) -> bool:
        if not self.form_submit_enabled() or self.on_form_submit is None:
            return False
        return self.on_form_submit() is not False

    def cancel_form(self) -> bool:
        if not self.form_cancel_enabled() or self.on_form_cancel is None:
            return False
        return self.on_form_cancel() is not False

    def _build_key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()

        @bindings.add("enter", eager=True)
        def _submit(event):
            if self.submit_form_selection():
                return
            if self.submit_slash_panel_selection(event.current_buffer):
                return
            self.handle_enter(event.current_buffer)

        @bindings.add("c-j", eager=True)
        def _ctrl_enter_newline(event):
            self.insert_newline(event.current_buffer)

        @bindings.add(
            "escape",
            "enter",
            eager=True,
            filter=Condition(lambda: not self.form_active()),
        )
        def _modified_enter_newline(event):
            self.insert_newline(event.current_buffer)

        @bindings.add(
            "up",
            eager=True,
            filter=Condition(self.slash_panel_navigation_enabled),
        )
        def _slash_panel_up(event):
            self.move_slash_panel_selection(-1)

        @bindings.add(
            "down",
            eager=True,
            filter=Condition(self.slash_panel_navigation_enabled),
        )
        def _slash_panel_down(event):
            self.move_slash_panel_selection(1)

        @bindings.add(
            "tab",
            eager=True,
            filter=Condition(self.slash_panel_accept_enabled),
        )
        def _slash_panel_accept(event):
            self.accept_slash_panel_selection(event.current_buffer)

        @bindings.add(
            "up",
            eager=True,
            filter=Condition(self.form_move_enabled),
        )
        def _form_up(event):
            self.move_form_selection(-1)

        @bindings.add(
            "down",
            eager=True,
            filter=Condition(self.form_move_enabled),
        )
        def _form_down(event):
            self.move_form_selection(1)

        # A text step has no list, so these keep Up/Down away from the composer's
        # shell history. See form_caret_move_enabled: the default binding would
        # eat a pasted OAuth code.
        @bindings.add(
            "up",
            eager=True,
            filter=Condition(self.form_caret_move_enabled),
        )
        def _form_caret_up(event):
            event.current_buffer.cursor_up(count=event.arg)

        @bindings.add(
            "down",
            eager=True,
            filter=Condition(self.form_caret_move_enabled),
        )
        def _form_caret_down(event):
            event.current_buffer.cursor_down(count=event.arg)

        @bindings.add(
            "left",
            eager=True,
            filter=Condition(self.form_tab_prev_enabled),
        )
        def _form_tab_prev(event):
            self.move_form_tab(-1)

        @bindings.add(
            "right",
            eager=True,
            filter=Condition(self.form_tab_next_enabled),
        )
        def _form_tab_next(event):
            self.move_form_tab(1)

        # Tab/Shift-Tab always step, whatever the caret is doing, so rail
        # navigation stays reachable mid-value. Safe to bind unconditionally
        # against the slash panel: a form suppresses it (slash_panel_visible
        # returns False while a form is open), so the two filters never overlap.
        @bindings.add(
            "tab",
            eager=True,
            filter=Condition(self.form_tab_nav_enabled),
        )
        def _form_tab_next_explicit(event):
            self.move_form_tab(1)

        @bindings.add(
            "s-tab",
            eager=True,
            filter=Condition(self.form_tab_nav_enabled),
        )
        def _form_tab_prev_explicit(event):
            self.move_form_tab(-1)

        @bindings.add(
            " ",
            eager=True,
            filter=Condition(self.form_toggle_enabled),
        )
        def _form_toggle(event):
            self.toggle_form_option()

        # eager=False is load-bearing: a form is navigated with arrow keys, which
        # arrive as escape sequences (ESC [ A ...). An eager bare-escape binding
        # would consume the ESC prefix and break the arrows. Non-eager lets
        # prompt_toolkit match the longer arrow sequences first and only fire
        # this on a standalone Escape (c-g is the immediate cancel alternative).
        @bindings.add(
            "escape",
            eager=False,
            filter=Condition(self.form_cancel_enabled),
        )
        def _form_cancel_escape(event):
            self.cancel_form()

        @bindings.add(
            "c-g",
            eager=True,
            filter=Condition(self.form_cancel_enabled),
        )
        def _form_cancel_ctrl_g(event):
            self.cancel_form()

        @bindings.add("c-c", eager=True)
        def _stop_or_clear(event):
            self.stop_or_clear(event.current_buffer)

        @bindings.add("c-r")
        def _history_search(event):
            event.current_buffer.start_history_lines_completion()

        @bindings.add("c-x", "c-e")
        def _open_editor(event):
            self.open_external_editor(event.current_buffer)

        return bindings

    def _emit_error(self, message: str) -> None:
        if self.on_error is not None:
            self.on_error(message)


def create_session(
    data_dir: Path,
    registry: "CommandRegistry",
    *,
    erase_when_done: bool = False,
) -> PromptSession:
    """Create a PromptSession with history, autocomplete, and keybindings."""
    history_path = data_dir / "cli_history"
    history_path.parent.mkdir(parents=True, exist_ok=True)

    bindings = KeyBindings()

    @bindings.add("c-c")
    def _clear_input(event):
        """Ctrl+C clears the current input instead of exiting."""
        event.current_buffer.reset()

    return PromptSession(
        history=FileHistory(str(history_path)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=ComposerCompleter(registry, cwd=Path.cwd()),
        key_bindings=bindings,
        complete_while_typing=False,
        erase_when_done=erase_when_done,
    )


def create_rich_repl_composer(
    *,
    command_registry: "CommandRegistry | None" = None,
    history_path: Path | None = None,
    cwd: Path | None = None,
    on_submit: SubmitHandler | None = None,
    on_error: ErrorHandler | None = None,
    on_stop: StopHandler | None = None,
    is_busy: StateGetter | None = None,
    queued_count: CountGetter | None = None,
    slash_panel_is_active: StateGetter | None = None,
    on_slash_panel_move: SlashPanelMoveHandler | None = None,
    on_slash_panel_accept: SlashPanelAcceptHandler | None = None,
    form_is_active: StateGetter | None = None,
    form_tab_enabled: StateGetter | None = None,
    form_has_navigable_list: StateGetter | None = None,
    active_field_label: LabelGetter | None = None,
    active_field_is_checkbox: StateGetter | None = None,
    active_field_is_secret: StateGetter | None = None,
    on_form_move: FormMoveHandler | None = None,
    on_form_tab: FormTabHandler | None = None,
    on_form_toggle: FormActionHandler | None = None,
    on_form_submit: FormActionHandler | None = None,
    on_form_cancel: FormActionHandler | None = None,
) -> ComposerController:
    """Create the scrollback-native Rich REPL composer controller."""

    return ComposerController(
        command_registry=command_registry,
        history_path=history_path,
        cwd=cwd,
        on_submit=on_submit,
        on_error=on_error,
        on_stop=on_stop,
        is_busy=is_busy,
        queued_count=queued_count,
        slash_panel_is_active=slash_panel_is_active,
        on_slash_panel_move=on_slash_panel_move,
        on_slash_panel_accept=on_slash_panel_accept,
        form_is_active=form_is_active,
        form_tab_enabled=form_tab_enabled,
        form_has_navigable_list=form_has_navigable_list,
        active_field_label=active_field_label,
        active_field_is_checkbox=active_field_is_checkbox,
        active_field_is_secret=active_field_is_secret,
        on_form_move=on_form_move,
        on_form_tab=on_form_tab,
        on_form_toggle=on_form_toggle,
        on_form_submit=on_form_submit,
        on_form_cancel=on_form_cancel,
        multiline=True,
        show_queued_prompt=False,
        show_slash_usage_hints=True,
        max_height=6,
    )


def get_prompt(
    state: "CLIState",
    *,
    busy: bool = False,
    theme: CLITheme | None = None,
) -> HTML:
    """Build the dynamic prompt string."""
    selected_theme = theme or DEFAULT_CLI_THEME
    color = selected_theme.color("prompt_busy" if busy else "prompt")
    return HTML(
        f"<style fg='{color}' bg=''><b>›</b></style> "
    )


def sanitize_composer_text(text: str) -> str:
    """Normalize pasted composer text before parsing or sending."""

    return (
        str(text or "")
        .replace(_BRACKETED_PASTE_START, "")
        .replace(_BRACKETED_PASTE_END, "")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\x00", "")
    )


def parse_composer_submission(text: str, *, cwd: Path | None = None) -> ComposerSubmission:
    """Parse composer text into message text and optional @file attachments.

    A leading @ token is preserved for backend thread dispatch.
    """

    raw_text = sanitize_composer_text(text)
    cleaned_text = raw_text.strip()
    if cleaned_text.startswith("/"):
        return ComposerSubmission(message=cleaned_text, raw_text=raw_text)

    root = cwd or Path.cwd()
    mention_start = _leading_thread_mention_start(raw_text)
    tokens = _attachment_tokens(raw_text, root)
    if mention_start is not None:
        tokens = [token for token in tokens if token.start != mention_start]
    if not tokens:
        return ComposerSubmission(message=cleaned_text, raw_text=raw_text)

    attachments: list[dict[str, Any]] = []
    errors: list[str] = []
    for token in tokens:
        attachment, error = _build_path_attachment(token.path)
        if error:
            errors.append(error)
            continue
        if attachment is not None:
            attachments.append(attachment)

    message = _remove_attachment_tokens(raw_text, tokens).strip()
    if not message and attachments:
        message = "[attachment]" if len(attachments) == 1 else "[attachments]"

    return ComposerSubmission(
        message=message,
        attachments=tuple(attachments),
        raw_text=raw_text,
        attachment_errors=tuple(errors),
    )


def _history(history_path: Path | None) -> History:
    if history_path is None:
        return InMemoryHistory()
    history_path.parent.mkdir(parents=True, exist_ok=True)
    return FileHistory(str(history_path))


def _command_completion_metadata(registry: "CommandRegistry") -> dict[str, str]:
    metadata: dict[str, str] = {}
    for command in registry.get_all_commands():
        _add_command_metadata(metadata, command, f"/{command.name}")
        for alias in command.aliases:
            _add_command_metadata(metadata, command, alias)
        for sub_name, subcommand in command.subcommands.items():
            _add_command_metadata(metadata, subcommand, f"/{command.name} {sub_name}")
            for alias in subcommand.aliases:
                _add_command_metadata(metadata, subcommand, f"/{command.name} {alias}")
    return metadata


def _add_command_metadata(
    metadata: dict[str, str],
    command: "Command",
    candidate: str,
) -> None:
    description = command.description
    if description:
        metadata[candidate] = description


def _without_auto_suggestion(fragments) -> list[tuple[str, str]]:
    return [
        (str(style), str(text))
        for style, text in fragments
        if "auto-suggestion" not in str(style)
    ]


def _token_before_cursor(text: str) -> str:
    if not text:
        return ""
    token_start = max(text.rfind(" "), text.rfind("\n"), text.rfind("\t")) + 1
    return text[token_start:]


def _ends_with_unescaped_backslash(text: str) -> bool:
    stripped = text.rstrip()
    if not stripped.endswith("\\"):
        return False
    slash_count = 0
    for char in reversed(stripped):
        if char != "\\":
            break
        slash_count += 1
    return slash_count % 2 == 1


def _leading_thread_mention_start(text: str) -> int | None:
    stripped = text.lstrip()
    if not stripped.startswith("@"):
        return None

    if not stripped[1:]:
        return None
    return len(text) - len(stripped)


def _attachment_tokens(text: str, cwd: Path) -> list[ParsedAttachmentToken]:
    tokens: list[ParsedAttachmentToken] = []
    for match in _ATTACHMENT_TOKEN_RE.finditer(text):
        path_text = match.group("path").strip()
        if not path_text:
            continue
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = cwd / path
        tokens.append(
            ParsedAttachmentToken(
                token=match.group(0),
                path=path,
                start=match.start(),
                end=match.end(),
            )
        )
    return tokens


def _remove_attachment_tokens(
    text: str,
    tokens: Sequence[ParsedAttachmentToken],
) -> str:
    if not tokens:
        return text

    parts: list[str] = []
    cursor = 0
    for token in tokens:
        parts.append(text[cursor:token.start])
        cursor = token.end
    parts.append(text[cursor:])
    return re.sub(r"[ \t]{2,}", " ", "".join(parts))


def _build_path_attachment(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, f"Attachment not found: {path}"
    if not path.is_file():
        return None, f"Attachment is not a file: {path}"

    mime_type, _encoding = mimetypes.guess_type(path.name)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"Could not read attachment {path}: {exc}"
    return build_attachment(raw, mime_type, path.name)


__all__ = [
    "CommandCompleter",
    "ComposerCompleter",
    "ComposerController",
    "ComposerPromptState",
    "ComposerSubmission",
    "SlashUsageHintProcessor",
    "create_rich_repl_composer",
    "create_session",
    "get_prompt",
    "parse_composer_submission",
    "sanitize_composer_text",
]
