"""Rich compatibility renderer backed by the CLI reducer state."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterable, Sequence
from typing import IO, Any, TextIO, cast

from rich.console import Console
from rich.cells import cell_len
from rich.text import Text

from ..state import (
    AssistantMessage,
    CLIUIState,
    ResponseStep,
    SystemMessage,
    ThinkingStep,
    ToolCallStep,
    UserMessage,
    create_initial_state,
    reduce_stream_event,
    select_last_assistant_message,
    select_response_content,
    start_turn,
)
from ..theme import CLITheme, DEFAULT_CLI_THEME, DEFAULT_TOOL_ICON, rich_style
from .markdown import (
    BLOCKQUOTE_RE,
    BULLET_RE,
    coerce_width,
    collapse_inline,
    FENCE_RE,
    HEADING_RE,
    HR_RE,
    NUMBERED_RE,
    render_inline_rich,
    render_markdown_lines,
    truncate_cell_width,
    wrap_plain_text,
    wrap_rich_lines,
)
from .rich_markdown import MarkdownBlock, MarkdownStreamBuffer, print_rich_markdown
from .shared_helpers import (
    _assistant_response_lengths,
    _dispatch_reference_text,
)
from .tool_rows import (
    ToolRowRenderOptions,
    ToolRowSegment,
    format_tool_row_segments,
)
from .transcript import (
    TranscriptLine,
    TranscriptRenderOptions,
    render_message_lines,
    render_transcript_lines,
)

THINKING_PREVIEW_MIN_CELLS = 12
_DENSE_MARKDOWN_BLOCK_KINDS = {"table", "code", "list", "blockquote", "hr"}
_RICH_NATIVE_LEADING_BLANK_KINDS = {"table", "list", "blockquote"}
_RICH_NATIVE_TRAILING_BLANK_KINDS = {"hr"}
_INLINE_MARKDOWN_MARKERS = ("**", "__", "~~", "](", "`")


class _TranscriptLineCounter:
    """Shared count of physical transcript lines written to the terminal."""

    __slots__ = ("count",)

    def __init__(self) -> None:
        self.count = 0


class _NewlineCountingWriter:
    """File proxy counting newlines written through a transcript console.

    Every renderer print is hard-wrapped to console width before writing, so
    newlines written == physical terminal rows scrolled. The live tool-row
    registry uses the shared counter to address still-visible rows for
    in-place rewrites.
    """

    def __init__(
        self,
        file: IO[str],
        counter: _TranscriptLineCounter,
        *,
        count: bool = True,
    ) -> None:
        self._file = file
        self._counter = counter
        # False for a console whose file does not scroll the terminal (a
        # redirected stderr): its newlines must not move the row arithmetic.
        self._count = count

    def write(self, text: str) -> int:
        if self._count:
            self._counter.count += text.count("\n")
        return self._file.write(text)

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        flush = getattr(self._file, "flush", None)
        if callable(flush):
            flush()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._file, name)


def _console_file_is_tty(console: Console) -> bool:
    """True when a console writes to the terminal itself (its lines scroll)."""

    try:
        return bool(console.file.isatty())
    except Exception:  # noqa: BLE001 - unknowable file: assume it is not the tty.
        return False


class RichReplRenderer:
    """Render reducer state for the legacy prompt_toolkit/Rich REPL path."""

    def __init__(
        self,
        *,
        state: CLIUIState | None = None,
        capabilities: Any | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        console: Console | None = None,
        error_console: Console | None = None,
        width: int | None = None,
        theme: CLITheme | None = None,
        stream_rich_response_lines: bool = False,
        download_base_url: str = "",
        tool_icon: str = DEFAULT_TOOL_ICON,
    ) -> None:
        self.state = state or create_initial_state()
        self.capabilities = capabilities
        self._download_base_url = download_base_url
        self.theme = theme or DEFAULT_CLI_THEME
        self.tool_icon = tool_icon or DEFAULT_TOOL_ICON
        self.width = coerce_width(width or getattr(capabilities, "width", 80))
        self.console = console or _make_console(
            capabilities,
            file=stdout or sys.stdout,
            width=self.width,
            stderr=False,
        )
        self.error_console = error_console or _make_console(
            capabilities,
            file=stderr or sys.stderr,
            width=self.width,
            stderr=True,
        )
        # Live tool rows (print-on-call + in-place completion flip). Dormant
        # unless the runtime wires ``live_row_context`` (pinned scroll-region
        # mode only); every other path keeps append-on-completion untouched.
        self._line_counter = _TranscriptLineCounter()
        supported = self._attach_line_counter(self.console)
        supported = (
            self._attach_line_counter(
                self.error_console,
                count=_console_file_is_tty(self.error_console),
            )
            and supported
        )
        self._live_rows_supported = supported
        self.live_row_context: Callable[[], tuple[int, int] | None] | None = None
        # tool_id -> (line counter after the running row printed, generation)
        self._live_tool_rows: dict[str, tuple[int, int]] = {}
        self._response_buffer = ""
        self._response_lengths = _assistant_response_lengths(self.state)
        self._thinking_lengths = _assistant_thinking_lengths(self.state)
        self._thinking_preview_rendered: set[tuple[str, int]] = set()
        self._rendered_tool_results: set[str] = set()
        self._rendered_system_ids: set[str] = set()
        self._rendered_dispatch_ids: set[str] = set()
        self._rendered_assistant_headers: set[str] = set()
        self._rendered_turn_end_ids: set[str] = set()
        self._rendered_error_count = len(self.state.errors)
        self._rendered_diagnostic_count = len(self.state.diagnostics)
        self._last_rendered_block: str | None = None
        self._last_markdown_block: MarkdownBlock | None = None
        self._response_stream_active = False
        self._markdown_stream = MarkdownStreamBuffer()
        self._stream_rich_response_lines = stream_rich_response_lines
        self._stream_line_buffer = ""
        self._turn_seen_tool = False
        self.transcript_verbose = False
        # Optional end-of-turn summary line source (state -> text), wired by
        # the Rich REPL runtime from the configured "turn" status-bar layout.
        # Empty/None output means no line; the bare separator blank remains.
        self.turn_summary_source: Callable[[CLIUIState], str] | None = None

    def _attach_line_counter(self, console: Console, *, count: bool = True) -> bool:
        """Route a console's writes through the shared newline counter."""

        try:
            file = console.file
            if (
                isinstance(file, _NewlineCountingWriter)
                and file._counter is self._line_counter
            ):
                return True
            # A foreign counter's wrapper is wrapped again: nested proxies
            # each count once for their own counter.
            console.file = cast(
                "IO[str]",
                _NewlineCountingWriter(file, self._line_counter, count=count),
            )
            return True
        except Exception:  # noqa: BLE001 - exotic console: live rows disable.
            return False

    def attach_transcript_console(self, console: Console) -> None:
        """Count transcript lines printed through an additional console.

        Every writer that prints into the transcript region while a turn is
        live (e.g. the app's slash-command/form/header console) must feed the
        shared counter, or in-place row flips would target the wrong terminal
        row. Attach failure disables live rows entirely (fail closed to the
        append-on-completion path).
        """

        if not self._attach_line_counter(console):
            self._live_rows_supported = False

    def transcript_line_count(self) -> int:
        """Physical transcript lines written so far (engine float anchor)."""

        return self._line_counter.count

    def set_theme(self, theme: CLITheme) -> None:
        """Update colors for future transcript output."""

        self.theme = theme

    def set_tool_icon(self, icon: str) -> None:
        """Update the tool-row ornament glyph for future transcript output."""

        self.tool_icon = icon or DEFAULT_TOOL_ICON

    def update_terminal_width(self, width: int) -> bool:
        """Update render width for future Rich transcript output."""

        new_width = coerce_width(width)
        if new_width == self.width:
            return False
        self.width = new_width
        self.console.width = new_width
        self.error_console.width = new_width
        return True

    def reset_state(self, state: CLIUIState | None = None) -> None:
        """Reset transcript state after a thread/user switch or clear."""

        self.state = state or create_initial_state()
        self._live_tool_rows = {}
        self._response_buffer = ""
        self._response_lengths = _assistant_response_lengths(self.state)
        self._thinking_lengths = _assistant_thinking_lengths(self.state)
        self._thinking_preview_rendered = set()
        self._rendered_tool_results = set()
        self._rendered_system_ids = set()
        self._rendered_dispatch_ids = set()
        self._rendered_assistant_headers = set()
        self._rendered_turn_end_ids = set()
        self._rendered_error_count = len(self.state.errors)
        self._rendered_diagnostic_count = len(self.state.diagnostics)
        self._last_rendered_block = None
        self._last_markdown_block = None
        self._response_stream_active = False
        self._markdown_stream.reset()
        self._stream_line_buffer = ""
        self._turn_seen_tool = False

    def start_turn(
        self,
        message: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
        attachments: Sequence[dict[str, Any]] | None = None,
        now: float | None = None,
    ) -> CLIUIState:
        """Append the submitted user turn to renderer state."""

        self.state = start_turn(
            self.state,
            message,
            thread_id=thread_id,
            user_id=user_id,
            attachments=list(attachments) if attachments is not None else None,
            now=now,
        )
        self._response_lengths = _assistant_response_lengths(self.state)
        self._thinking_lengths = _assistant_thinking_lengths(self.state)
        self._thinking_preview_rendered = {
            key
            for key in self._thinking_preview_rendered
            if key[0] != self.state.messages[-1].id
        }
        self._live_tool_rows = {}
        self._last_rendered_block = None
        self._last_markdown_block = None
        self._response_stream_active = False
        self._markdown_stream.reset()
        self._stream_line_buffer = ""
        self._turn_seen_tool = False
        if len(self.state.messages) >= 2 and isinstance(
            self.state.messages[-2],
            UserMessage,
        ):
            self._render_user_message(self.state.messages[-2])
            self.console.print()
        return self.state

    def render_event(self, event: Any, *, now: float | None = None) -> CLIUIState:
        """Reduce one stream event and render the resulting state delta."""

        previous = self.state
        self.state = reduce_stream_event(self.state, event, now=now)
        self._render_transition(previous, self.state)
        return self.state

    def render_events(
        self,
        events: Iterable[Any],
        *,
        now: float | None = None,
    ) -> CLIUIState:
        """Render a synchronous event stream."""

        for event in events:
            self.render_event(event, now=now)
        return self.state

    async def render_async_events(
        self,
        events: Any,
        *,
        now: float | None = None,
    ) -> CLIUIState:
        """Render an async event stream."""

        async for event in events:
            self.render_event(event, now=now)
        return self.state

    def render_state(self, state: CLIUIState | None = None) -> None:
        """Render a full transcript snapshot from reducer state."""

        self.flush_response()
        snapshot = state or self.state
        self.state = snapshot
        if not self._ascii_only():
            self._render_rich_state(snapshot)
            self._mark_state_rendered(snapshot)
            return
        for line in render_transcript_lines(
            snapshot,
            width=self.width,
            options=self._transcript_options(),
        ):
            self._render_transcript_line(line)
        self._mark_state_rendered(snapshot)

    def _render_rich_state(self, state: CLIUIState) -> None:
        """Replay stored transcript messages with Rich Markdown intact."""

        for message in state.messages:
            if isinstance(message, UserMessage):
                self._render_user_message(message)
            elif isinstance(message, AssistantMessage):
                self._render_assistant_message(message)
            elif isinstance(message, SystemMessage):
                for line in render_message_lines(
                    message,
                    width=self.width,
                    options=self._transcript_options(),
                ):
                    self._render_transcript_line(line)
            if isinstance(message, UserMessage):
                self.console.print()

        for error in state.errors:
            self._render_transcript_line(
                TranscriptLine(f"Error: {error.content or error.code}", "error")
            )
        for diagnostic in state.diagnostics:
            text = diagnostic.message or diagnostic.source_type or "Unknown event"
            self._render_transcript_line(
                TranscriptLine(f"Diagnostic: {text}", "diagnostic")
            )

    def flush_response(self) -> None:
        """Render buffered response markdown."""

        if not self._ascii_only():
            if self._stream_rich_response_lines:
                block_kind = "final" if self._turn_seen_tool else "preamble"
                self._flush_rich_scroll_region_stream(
                    block_kind=block_kind,
                    force=True,
                )
                return
            block_kind = "final" if self._turn_seen_tool else "preamble"
            self._flush_rich_markdown_blocks(block_kind=block_kind, force=True)
            return

        self._flush_stream_line(force=True)
        text = self._response_buffer.strip()
        if not text:
            self._response_buffer = ""
            return
        block_kind = "final" if self._turn_seen_tool else "preamble"
        self._begin_assistant_block(block_kind)
        body_width = max(1, self.width - 2)
        for line in render_markdown_lines(
            text,
            width=body_width,
            ascii_only=True,
        ):
            rendered = f"  {line}" if line else ""
            self.console.print(
                Text(
                    rendered,
                    style=_style_for_line_kind(block_kind, self.theme),
                )
            )
        self._response_buffer = ""

    def _render_transition(
        self,
        previous: CLIUIState,
        current: CLIUIState,
    ) -> None:
        self._render_dispatch_references(current)
        self._render_thinking_delta(current)
        self._flush_pending_thinking_previews(current)
        self._collect_response_delta(current)

        printed_non_response = False
        printed_non_response |= self._render_tool_results(previous, current)
        printed_non_response |= self._render_new_system_messages(current)
        printed_non_response |= self._render_new_errors(current)
        printed_non_response |= self._render_new_diagnostics(current)

        if printed_non_response:
            self.flush_response()
        if current.turn_status in {"complete", "error"}:
            self.flush_response()
            self._render_turn_end(current)

    def _render_dispatch_references(self, state: CLIUIState) -> None:
        for message in state.messages:
            if not isinstance(message, AssistantMessage):
                continue
            if message.id in self._rendered_dispatch_ids:
                continue
            text = _dispatch_reference_text(message)
            if not text:
                continue
            self._ensure_assistant_header(state, message)
            self.console.print(
                Text(
                    f"  {truncate_cell_width(text, max(1, self.width - 2))}",
                    style=_style_for_line_kind("assistant_header", self.theme),
                )
            )
            self._rendered_dispatch_ids.add(message.id)
            self._last_rendered_block = "dispatch"
            self._last_markdown_block = None

    def _collect_response_delta(self, state: CLIUIState) -> None:
        message = select_last_assistant_message(state)
        if message is None:
            return

        content = select_response_content(message)
        previous_length = self._response_lengths.get(message.id, 0)
        if len(content) > previous_length:
            self._ensure_assistant_header(state, message)
            self._stream_response_delta(content[previous_length:])
        self._response_lengths[message.id] = len(content)

    def _render_tool_results(
        self,
        _previous: CLIUIState,
        current: CLIUIState,
    ) -> bool:
        printed = False
        for tool in _tool_steps(current):
            if tool.id in self._rendered_tool_results:
                continue
            if tool.status == "running":
                printed |= self._maybe_print_running_tool_row(current, tool)
                continue
            if self._try_flip_live_tool_row(tool):
                # Rewrote the already-printed running row in place: nothing
                # was appended, so block/flush bookkeeping is untouched.
                self._rendered_tool_results.add(tool.id)
                continue
            self._print_tool_row(current, tool)
            self._rendered_tool_results.add(tool.id)
            printed = True
        return printed

    def _print_tool_row(self, state: CLIUIState, tool: ToolCallStep) -> None:
        """Append one tool row with the shared flush/header/block discipline."""

        self.flush_response()
        message = _message_for_tool(state, tool.id)
        if message is not None:
            self._ensure_assistant_header(state, message)
        self._begin_assistant_block("tool")
        self.console.print(
            render_tool_row(
                tool,
                width=self.width,
                ascii_only=self._ascii_only(),
                theme=self.theme,
                icon=self.tool_icon,
            )
        )
        self._turn_seen_tool = True

    def _live_context(self) -> tuple[int, int] | None:
        """Return ``(anchor_row, generation)`` when in-place rows are safe.

        ``anchor_row`` is the engine's estimate of the transcript write-cursor
        row (pinned scroll bottom, or the float-phase CPR snapshot estimate);
        it only gates the still-on-screen check, while the rewrite itself uses
        relative cursor moves.
        """

        if not self._live_rows_supported:
            return None
        source = self.live_row_context
        if source is None:
            return None
        try:
            return source()
        except Exception:  # noqa: BLE001 - engine fault: fall back to appends.
            return None

    def _maybe_print_running_tool_row(
        self,
        state: CLIUIState,
        tool: ToolCallStep,
    ) -> bool:
        """Print an in-flight tool row at call time when it can be flipped later.

        Verbose mode keeps completion-only appends (payload lines cannot be
        inserted in place), and without a live context the row would duplicate
        on completion, so both cases defer to the append path.
        """

        if tool.id in self._live_tool_rows:
            return False
        if tool.id not in state.active_tool_calls:
            # Not part of the live turn: e.g. an orphaned running step left
            # in history by a dead stream (turn_lost). Keep the old
            # completion-only invisibility instead of re-printing a stale
            # running row on every later turn.
            return False
        if self.transcript_verbose:
            return False
        context = self._live_context()
        if context is None:
            return False
        _anchor_row, generation = context
        self._print_tool_row(state, tool)
        self._live_tool_rows[tool.id] = (self._line_counter.count, generation)
        return True

    def _try_flip_live_tool_row(self, tool: ToolCallStep) -> bool:
        """Rewrite a registered running row in place with its final state."""

        slot = self._live_tool_rows.pop(tool.id, None)
        if slot is None:
            return False
        lines_after_print, generation = slot
        context = self._live_context()
        if context is None:
            return False
        anchor_row, current_generation = context
        if current_generation != generation:
            return False
        up_distance = self._line_counter.count - lines_after_print + 1
        if up_distance >= anchor_row:
            return False
        self._write_tool_row_above(up_distance, tool)
        return True

    def _write_tool_row_above(
        self,
        up_distance: int,
        tool: ToolCallStep,
        *,
        now: float | None = None,
    ) -> None:
        """Rewrite one still-visible tool row ``up_distance`` rows above.

        Runs inside the engine's render window with the cursor at the
        transcript write point. Addressing is fully relative (up, rewrite,
        back down), so the cursor lands exactly where it started in both the
        pinned and the floating-footer phases, and no newline is emitted, so
        the engine's cursor bookkeeping and the shared line counter are
        untouched. Callers must have validated ``up_distance`` against the
        anchor row first (the target must still be on screen).
        """

        file = self.console.file
        file.write(f"\x1b[{up_distance}A\r\x1b[2K")
        self.console.print(
            render_tool_row(
                tool,
                width=self.width,
                ascii_only=self._ascii_only(),
                theme=self.theme,
                icon=self.tool_icon,
                now=now,
            ),
            end="",
        )
        file.write(f"\r\x1b[{up_distance}B")
        self._flush_console_file()

    def has_live_tool_rows(self) -> bool:
        """True while any registered in-flight row may need a timer tick."""

        return bool(self._live_tool_rows)

    def render_running_tick(self, now: float | None = None) -> None:
        """Refresh elapsed timers on still-visible running tool rows."""

        if not self._live_tool_rows:
            return
        context = self._live_context()
        if context is None:
            # Unpinned or the geometry gate went false: the generation moved
            # on, so no slot can ever flip again. Clear them so the ticker
            # stops instead of spinning on dead rows.
            self._live_tool_rows.clear()
            return
        anchor_row, generation = context
        selected_now = time.monotonic() if now is None else now
        for tool_id, slot in list(self._live_tool_rows.items()):
            lines_after_print, slot_generation = slot
            if slot_generation != generation:
                self._live_tool_rows.pop(tool_id, None)
                continue
            step = self.state.active_tool_calls.get(tool_id)
            if step is None or step.status != "running":
                # Left the live-turn index (e.g. mid-turn compaction cleared
                # it): stop ticking; completion falls back to an append.
                self._live_tool_rows.pop(tool_id, None)
                continue
            up_distance = self._line_counter.count - lines_after_print + 1
            if up_distance >= anchor_row:
                # Scrolled out of the visible region: scrollback is
                # immutable, so stop ticking; completion appends a fresh row.
                self._live_tool_rows.pop(tool_id, None)
                continue
            self._write_tool_row_above(up_distance, step, now=selected_now)

    def _render_new_system_messages(self, state: CLIUIState) -> bool:
        printed = False
        for message in state.messages:
            if not isinstance(message, SystemMessage):
                continue
            if message.id in self._rendered_system_ids:
                continue
            self.flush_response()
            if self._last_rendered_block is not None:
                self.console.print()
            for line in render_message_lines(
                message,
                width=self.width,
                options=self._transcript_options(),
            ):
                self._render_transcript_line(line)
            self._rendered_system_ids.add(message.id)
            # Sentinel (not None) so the shared blank-line guards separate the
            # marker from whatever follows: the assistant response after an
            # autonomous marker, or a second consecutive system message. This
            # mirrors the transcript replay, which always inserts that blank.
            self._last_rendered_block = "system"
            self._last_markdown_block = None
            self._turn_seen_tool = False
            printed = True
        return printed

    def _render_new_errors(self, state: CLIUIState) -> bool:
        printed = False
        for error in state.errors[self._rendered_error_count :]:
            self.flush_response()
            self.error_console.print(
                Text(
                    f"Error: {error.content or error.code}",
                    style=_style_for_line_kind("error", self.theme),
                )
            )
            printed = True
        self._rendered_error_count = len(state.errors)
        return printed

    def _render_new_diagnostics(self, state: CLIUIState) -> bool:
        printed = False
        for diagnostic in state.diagnostics[self._rendered_diagnostic_count :]:
            text = diagnostic.message or diagnostic.source_type or "Unknown event"
            self.flush_response()
            self.error_console.print(
                Text(
                    f"Diagnostic: {text}",
                    style=_style_for_line_kind("diagnostic", self.theme),
                )
            )
            printed = True
        self._rendered_diagnostic_count = len(state.diagnostics)
        return printed

    def _render_user_message(self, message: UserMessage) -> None:
        """Echo the submitted prompt styled like the composer.

        The echo (rule, ``› prompt``, rule) doubles as the turn boundary:
        there is no "You"/"Nymeria" label chrome around turns.
        """

        ascii_only = self._ascii_only()
        border_style = rich_style(self.theme, "input_border")
        prompt_style = rich_style(self.theme, "prompt")
        user_style = _style_for_line_kind("user_text", self.theme)
        rule_char = "-" if ascii_only else "─"
        prompt_prefix = "> " if ascii_only else "› "
        border = Text(rule_char * max(1, self.width), style=border_style)

        self.console.print(border)
        body_width = max(1, self.width - len(prompt_prefix))
        first_line = True
        for raw_line in (message.content or "").splitlines() or [""]:
            wrapped_lines = wrap_plain_text(raw_line, width=body_width) or [""]
            for line in wrapped_lines:
                if first_line:
                    rendered = Text(prompt_prefix, style=prompt_style)
                    rendered.append(line, style=user_style)
                    first_line = False
                else:
                    rendered = Text(
                        f"{' ' * len(prompt_prefix)}{line}".rstrip(),
                        style=user_style,
                    )
                self.console.print(rendered)
        if message.attachments:
            label = "attachment" if len(message.attachments) == 1 else "attachments"
            self.console.print(
                Text(
                    f"{' ' * len(prompt_prefix)}{len(message.attachments)} {label}",
                    style=_style_for_line_kind("artifact", self.theme),
                )
            )
        self.console.print(border)

    def _render_thinking_delta(self, state: CLIUIState) -> None:
        for message in state.messages:
            if not isinstance(message, AssistantMessage):
                continue
            thinking_index = 0
            for step in message.steps:
                if not isinstance(step, ThinkingStep):
                    continue
                key = (message.id, thinking_index)
                previous_length = self._thinking_lengths.get(key, 0)
                if len(step.content) > previous_length:
                    delta = step.content[previous_length:]
                    self._ensure_assistant_header(state, message)
                    if self.transcript_verbose:
                        self._render_thinking_text(delta, preview=False)
                    elif key not in self._thinking_preview_rendered and (
                        _thinking_preview_is_ready(
                            step.content,
                            width=max(1, self.width - 4),
                        )
                    ):
                        self._render_thinking_text(step.content, preview=True)
                        self._thinking_preview_rendered.add(key)
                    self._thinking_lengths[key] = len(step.content)
                thinking_index += 1

    def _flush_pending_thinking_previews(self, state: CLIUIState) -> None:
        if self.transcript_verbose:
            return
        for message in state.messages:
            if not isinstance(message, AssistantMessage):
                continue
            thinking_index = 0
            for step_index, step in enumerate(message.steps):
                if not isinstance(step, ThinkingStep):
                    continue
                key = (message.id, thinking_index)
                thinking_index += 1
                if key in self._thinking_preview_rendered:
                    continue
                if not str(step.content or "").strip():
                    continue
                step_is_closed = step_index < len(message.steps) - 1
                if not step_is_closed and message.status not in {"complete", "error"}:
                    continue
                self._ensure_assistant_header(state, message)
                self._render_thinking_text(step.content, preview=True)
                self._thinking_preview_rendered.add(key)

    def _render_thinking_text(self, content: str, *, preview: bool) -> None:
        if not str(content or "").strip():
            text = "..."
        elif preview:
            text = _thinking_preview_text(
                content,
                width=max(1, self.width - 4),
            )
        else:
            text = str(content)
        if not str(text or "").strip():
            return
        self._begin_assistant_block("thinking")
        marker = "| " if self._ascii_only() else "\u2502 "
        if preview:
            self.console.print(
                Text(
                    f"  {marker}{text}",
                    style=_style_for_line_kind("thinking", self.theme),
                )
            )
            return
        thinking_style = _style_for_line_kind("thinking", self.theme)
        body_width = max(1, self.width - 4)
        if not self._ascii_only():
            for line in render_markdown_lines(
                text,
                width=body_width,
                ascii_only=False,
            ):
                if not line:
                    self.console.print()
                    continue
                styled = render_inline_rich(line, theme=self.theme)
                styled.stylize(thinking_style)
                padded = Text(f"  {marker}")
                padded.append_text(styled)
                self.console.print(padded)
        else:
            for line in render_markdown_lines(
                text,
                width=body_width,
                ascii_only=True,
            ):
                rendered = f"  {marker}{line}" if line else ""
                self.console.print(
                    Text(rendered, style=thinking_style)
                )

    def _stream_response_delta(self, delta: str) -> None:
        if not delta:
            return
        block_kind = "final" if self._turn_seen_tool else "preamble"
        text = str(delta or "").replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        if not self._ascii_only():
            if self._stream_rich_response_lines:
                self._begin_assistant_block(block_kind)
                self._stream_line_buffer += text
                self._response_stream_active = True
                self._flush_complete_rich_scroll_region_lines(block_kind)
                self._flush_rich_scroll_region_tail_if_ready(block_kind)
                return
            blocks = self._markdown_stream.append(text)
            self._response_stream_active = self._markdown_stream.has_pending
            if blocks:
                self._print_rich_markdown_blocks(blocks, block_kind=block_kind)
            return

        self._begin_assistant_block(block_kind)
        self._stream_line_buffer += text
        self._response_stream_active = True
        self._flush_complete_stream_lines(block_kind)
        self._flush_stream_line_if_ready(block_kind)

    def _flush_rich_markdown_blocks(
        self,
        *,
        block_kind: str,
        force: bool,
    ) -> None:
        if not force:
            self._response_stream_active = self._markdown_stream.has_pending
            return
        blocks = self._markdown_stream.flush()
        self._response_stream_active = False
        if blocks:
            self._print_rich_markdown_blocks(blocks, block_kind=block_kind)

    def _flush_complete_rich_scroll_region_lines(self, block_kind: str) -> None:
        while "\n" in self._stream_line_buffer:
            line, self._stream_line_buffer = self._stream_line_buffer.split("\n", 1)
            self._print_rich_scroll_region_line(line, block_kind)
        self._response_stream_active = (
            bool(self._stream_line_buffer) or self._markdown_stream.has_pending
        )

    def _flush_rich_scroll_region_tail_if_ready(self, block_kind: str) -> None:
        if not self._stream_line_buffer:
            self._response_stream_active = self._markdown_stream.has_pending
            return
        if self._markdown_stream.has_pending or _line_prefers_rich_markdown(
            self._stream_line_buffer
        ):
            blocks = self._markdown_stream.append(self._stream_line_buffer)
            self._stream_line_buffer = ""
            self._response_stream_active = self._markdown_stream.has_pending
            if blocks:
                self._print_rich_markdown_blocks(blocks, block_kind=block_kind)
            return
        self._flush_stream_line_if_ready(block_kind)

    def _flush_rich_scroll_region_stream(
        self,
        *,
        block_kind: str,
        force: bool,
    ) -> None:
        if not force:
            self._response_stream_active = (
                bool(self._stream_line_buffer) or self._markdown_stream.has_pending
            )
            return
        if self._stream_line_buffer:
            if self._markdown_stream.has_pending or _line_prefers_rich_markdown(
                self._stream_line_buffer
            ):
                blocks = self._markdown_stream.append(self._stream_line_buffer)
                self._stream_line_buffer = ""
                if blocks:
                    self._print_rich_markdown_blocks(blocks, block_kind=block_kind)
            else:
                self._flush_stream_line(force=True, block_kind=block_kind)
        if self._markdown_stream.has_pending:
            blocks = self._markdown_stream.flush()
            if blocks:
                self._print_rich_markdown_blocks(blocks, block_kind=block_kind)
        self._response_stream_active = False

    def _print_rich_scroll_region_line(self, line: str, block_kind: str) -> None:
        if self._markdown_stream.has_pending or _line_prefers_rich_markdown(line):
            blocks = self._markdown_stream.append(f"{line}\n")
            self._response_stream_active = self._markdown_stream.has_pending
            if blocks:
                self._print_rich_markdown_blocks(blocks, block_kind=block_kind)
            return
        self._print_stream_line(line, block_kind)

    def _print_rich_markdown_blocks(
        self,
        blocks: Sequence[MarkdownBlock | str],
        *,
        block_kind: str,
    ) -> None:
        rendered = False
        for block in blocks:
            markdown_block = _coerce_markdown_block(block)
            if not markdown_block.text.strip():
                continue
            if not rendered:
                self._begin_assistant_block(
                    block_kind,
                    flush_pending_markdown=False,
                )
                rendered = True
            if _should_print_markdown_separator(
                self._last_markdown_block,
                markdown_block,
            ):
                self.console.print()
            print_rich_markdown(
                self.console,
                markdown_block.text,
                theme=self.theme,
            )
            self._last_markdown_block = markdown_block
        if rendered:
            self._flush_console_file()

    def _render_assistant_message(self, message: AssistantMessage) -> None:
        self._turn_seen_tool = False
        rendered_body = False
        dispatch_text = _dispatch_reference_text(message)
        if dispatch_text:
            self.console.print(
                Text(
                    f"  {truncate_cell_width(dispatch_text, max(1, self.width - 2))}",
                    style=_style_for_line_kind("assistant_header", self.theme),
                )
            )
            self._last_rendered_block = "dispatch"
            self._last_markdown_block = None
            rendered_body = True

        for step in message.steps:
            if isinstance(step, ThinkingStep):
                self._render_thinking_text(step.content or "...", preview=True)
                rendered_body = True
            elif isinstance(step, ToolCallStep):
                self.flush_response()
                self._begin_assistant_block("tool")
                self.console.print(
                    render_tool_row(
                        step,
                        width=self.width,
                        ascii_only=self._ascii_only(),
                        theme=self.theme,
                        icon=self.tool_icon,
                    )
                )
                self._turn_seen_tool = True
                rendered_body = True
            elif isinstance(step, ResponseStep):
                if not step.content.strip():
                    continue
                if not self._ascii_only():
                    self._stream_response_delta(step.content)
                    self.flush_response()
                else:
                    block_kind = "final" if self._turn_seen_tool else "preamble"
                    self._begin_assistant_block(block_kind)
                    for line in render_markdown_lines(
                        step.content.strip(),
                        width=max(1, self.width - 2),
                        ascii_only=True,
                    ):
                        rendered = f"  {line}" if line else ""
                        self.console.print(
                            Text(
                                rendered,
                                style=_style_for_line_kind(block_kind, self.theme),
                            )
                        )
                rendered_body = True
        if message.status == "complete" and rendered_body:
            self.console.print()
            self._last_rendered_block = None
            self._last_markdown_block = None

    def _ensure_assistant_header(
        self,
        state: CLIUIState,
        message: AssistantMessage,
    ) -> None:
        """Reset per-message bookkeeping when an assistant message starts.

        There is no assistant header chrome anymore: user-initiated turns are
        bounded by the composer echo, and autonomous turns get their one-line
        marker from the preceding autonomous SystemMessage.
        """

        if message.id in self._rendered_assistant_headers:
            return
        if self._last_rendered_block is not None:
            self.console.print()
            self._last_rendered_block = None
            self._last_markdown_block = None
        self._rendered_assistant_headers.add(message.id)
        self._turn_seen_tool = False

    def _begin_assistant_block(
        self,
        block_kind: str,
        *,
        flush_pending_markdown: bool = True,
    ) -> None:
        if (
            flush_pending_markdown
            and not self._ascii_only()
            and self._markdown_stream.has_pending
            and self._last_rendered_block != block_kind
        ):
            previous_block = self._last_rendered_block or block_kind
            blocks = self._markdown_stream.flush()
            if blocks:
                self._last_rendered_block = previous_block
                self._print_rich_markdown_blocks(
                    blocks,
                    block_kind=previous_block,
                )
                self._response_stream_active = False
        if self._stream_line_buffer and self._last_rendered_block != block_kind:
            previous_block = self._last_rendered_block or block_kind
            if self._stream_rich_response_lines and not self._ascii_only():
                self._flush_rich_scroll_region_stream(
                    block_kind=previous_block,
                    force=True,
                )
            else:
                self._flush_stream_line(
                    force=True,
                    block_kind=previous_block,
                )
        if (
            self._last_rendered_block is not None
            and self._last_rendered_block != block_kind
        ):
            self._last_markdown_block = None
            self.console.print()
        self._last_rendered_block = block_kind

    def _flush_complete_stream_lines(self, block_kind: str) -> None:
        while "\n" in self._stream_line_buffer:
            line, self._stream_line_buffer = self._stream_line_buffer.split("\n", 1)
            self._print_stream_line(line, block_kind)
        self._response_stream_active = bool(self._stream_line_buffer)

    def _flush_stream_line_if_ready(self, block_kind: str) -> None:
        if not self._stream_line_buffer:
            return
        text = self._stream_line_buffer
        width = _stream_flush_width(self.width)
        if text.endswith((".", "!", "?", ":", ";")):
            self._flush_stream_line(force=True, block_kind=block_kind)
            return
        if len(text) < width:
            return
        split_at = text.rfind(" ", 0, width)
        if split_at > 12:
            chunk = text[:split_at].rstrip()
            self._stream_line_buffer = text[split_at:].lstrip()
            self._print_stream_line(chunk, block_kind)
            self._response_stream_active = bool(self._stream_line_buffer)
            return
        self._flush_stream_line(force=True, block_kind=block_kind)

    def _flush_stream_line(
        self,
        *,
        force: bool,
        block_kind: str | None = None,
    ) -> None:
        if not force or not self._stream_line_buffer:
            self._response_stream_active = bool(self._stream_line_buffer)
            return
        selected_block = block_kind or self._last_rendered_block or "preamble"
        self._print_stream_line(self._stream_line_buffer, selected_block)
        self._stream_line_buffer = ""
        self._response_stream_active = False

    def _print_stream_line(self, line: str, block_kind: str) -> None:
        if not line.strip():
            self.console.print()
            self._flush_console_file()
            return
        if not self._ascii_only():
            for wrapped in wrap_rich_lines(
                line,
                width=self.width,
                theme=self.theme,
            ):
                self.console.print(wrapped)
        else:
            style = _style_for_line_kind(block_kind, self.theme)
            body_width = max(1, self.width - 2)
            for rendered_line in render_markdown_lines(
                _clean_stream_delta(line),
                width=body_width,
                ascii_only=True,
            ):
                rendered = f"  {rendered_line}" if rendered_line else ""
                self.console.print(Text(rendered, style=style))
        self._flush_console_file()

    def _flush_console_file(self) -> None:
        flush = getattr(self.console.file, "flush", None)
        if callable(flush):
            flush()

    def _mark_state_rendered(self, state: CLIUIState) -> None:
        """Synchronize incremental render bookkeeping after a replay."""

        self._live_tool_rows = {}
        self._response_buffer = ""
        self._response_lengths = _assistant_response_lengths(state)
        self._thinking_lengths = _assistant_thinking_lengths(state)
        self._thinking_preview_rendered = set(_assistant_thinking_lengths(state))
        self._rendered_tool_results = {
            step.id
            for step in _tool_steps(state)
            if step.status in {"success", "error", "cancelled"}
        }
        self._rendered_system_ids = {
            message.id
            for message in state.messages
            if isinstance(message, SystemMessage)
        }
        self._rendered_dispatch_ids = {
            message.id
            for message in state.messages
            if isinstance(message, AssistantMessage) and message.dispatch_info
        }
        self._rendered_assistant_headers = {
            message.id
            for message in state.messages
            if isinstance(message, AssistantMessage)
            and _assistant_has_renderable_body(message)
        }
        self._rendered_turn_end_ids = {
            message.id
            for message in state.messages
            if isinstance(message, AssistantMessage)
            and message.status in {"complete", "error"}
            and _assistant_has_renderable_body(message)
        }
        self._rendered_error_count = len(state.errors)
        self._rendered_diagnostic_count = len(state.diagnostics)
        self._last_rendered_block = None
        self._last_markdown_block = None
        self._response_stream_active = False
        self._markdown_stream.reset()
        self._stream_line_buffer = ""
        self._turn_seen_tool = False

    def _render_turn_end(self, state: CLIUIState) -> None:
        message = select_last_assistant_message(state)
        if message is None or message.id in self._rendered_turn_end_ids:
            return
        if message.status not in {"complete", "error"}:
            return
        if not _assistant_has_renderable_body(message):
            return
        self.console.print()
        summary = self._turn_summary_text(state)
        if summary:
            self.console.print(
                Text(summary, style=_style_for_line_kind("diagnostic", self.theme))
            )
        self._rendered_turn_end_ids.add(message.id)
        self._last_rendered_block = None
        self._last_markdown_block = None

    def _turn_summary_text(self, state: CLIUIState) -> str:
        """End-of-turn summary from the wired source; "" disables the line.

        Live turn ends only: replay marks ``_rendered_turn_end_ids`` before
        ``_render_turn_end`` can fire, so history never grows summary lines.
        """
        source = self.turn_summary_source
        if source is None:
            return ""
        try:
            return str(source(state) or "").strip()
        except Exception:  # noqa: BLE001 - a summary fault must not kill the turn render.
            return ""

    def _render_transcript_line(self, line: TranscriptLine) -> None:
        self.console.print(
            Text(line.text, style=_style_for_line_kind(line.kind, self.theme))
        )

    def _transcript_options(self) -> TranscriptRenderOptions:
        return TranscriptRenderOptions(
            verbose=self.transcript_verbose,
            ascii_only=self._ascii_only(),
            base_url=self._download_base_url,
            tool_row_options=ToolRowRenderOptions(
                show_duration=True,
                ascii_only=self._ascii_only(),
                icon=self.tool_icon,
            ),
        )

    def _ascii_only(self) -> bool:
        return not bool(getattr(self.capabilities, "unicode_enabled", False))


def render_tool_row(
    tool: ToolCallStep,
    *,
    width: int | None = None,
    ascii_only: bool = False,
    theme: CLITheme | None = None,
    icon: str = DEFAULT_TOOL_ICON,
    now: float | None = None,
) -> Text:
    """Return a Rich compact tool row styled per segment.

    Desktop ToolCallCard header shape: bright icon + name, dim args,
    duration, and result preview; error turns the icon and name red.
    ``now`` renders a live elapsed duration on a running row (timer ticks).
    """

    selected_theme = theme or DEFAULT_CLI_THEME
    width = coerce_width(width)
    segments = format_tool_row_segments(
        tool,
        width=max(1, width - 2),
        options=ToolRowRenderOptions(
            show_duration=True,
            ascii_only=ascii_only,
            icon=icon,
        ),
        now=now,
    )
    row = Text("  ")
    for segment in segments:
        row.append(
            segment.text,
            style=_tool_segment_style(segment, tool.status, selected_theme),
        )
    row.truncate(width)
    return row


def _tool_segment_style(
    segment: ToolRowSegment,
    status: str,
    theme: CLITheme,
) -> str:
    is_error = status == "error"
    if segment.kind == "marker":
        if is_error:
            return rich_style(theme, "error")
        if status == "cancelled":
            return _style_for_line_kind("tool_detail", theme)
        return rich_style(theme, "tool_icon")
    if segment.kind == "name":
        if is_error:
            return rich_style(theme, "error", bold=True)
        return rich_style(theme, "tool", bold=True)
    if segment.kind == "status":
        if is_error:
            return rich_style(theme, "error")
        return _style_for_line_kind("tool_detail", theme)
    if segment.kind == "artifact":
        return _style_for_line_kind("artifact", theme)
    # args, duration, result: dim metadata around the bright head.
    return _style_for_line_kind("tool_detail", theme)


def _make_console(
    capabilities: Any | None,
    *,
    file: TextIO,
    width: int,
    stderr: bool,
) -> Console:
    color_enabled = bool(getattr(capabilities, "color_enabled", False))
    force_terminal = color_enabled and bool(
        getattr(capabilities, "stdout_isatty", True)
    )
    color_system = "auto" if color_enabled else None
    return Console(
        file=file,
        stderr=stderr,
        force_terminal=force_terminal,
        color_system=color_system,
        no_color=not color_enabled,
        width=width,
        highlight=False,
    )


def _assistant_thinking_lengths(state: CLIUIState) -> dict[tuple[str, int], int]:
    lengths: dict[tuple[str, int], int] = {}
    for message in state.messages:
        if not isinstance(message, AssistantMessage):
            continue
        thinking_index = 0
        for step in message.steps:
            if not isinstance(step, ThinkingStep):
                continue
            lengths[(message.id, thinking_index)] = len(step.content)
            thinking_index += 1
    return lengths


def _assistant_has_renderable_body(message: AssistantMessage) -> bool:
    return any(
        isinstance(step, ToolCallStep)
        or (isinstance(step, ThinkingStep) and bool(step.content))
        or (isinstance(step, ResponseStep) and bool(step.content.strip()))
        for step in message.steps
    )


def _message_for_tool(state: CLIUIState, tool_id: str) -> AssistantMessage | None:
    for message in state.messages:
        if not isinstance(message, AssistantMessage):
            continue
        for step in message.steps:
            if isinstance(step, ToolCallStep) and step.id == tool_id:
                return message
    return None


def _clean_stream_delta(delta: str) -> str:
    """Strip common inline markdown markers while preserving streamed text."""

    return (
        str(delta or "")
        .replace("**", "")
        .replace("__", "")
        .replace("`", "")
    )


def _thinking_preview_text(content: str, *, width: int) -> str:
    """Return a one-line thinking preview with an explicit cutoff marker."""

    text = collapse_inline(content) if content else "..."
    if not text:
        return ""
    if text.endswith("..."):
        return truncate_cell_width(text, width)
    return truncate_cell_width(f"{text}...", width)


def _thinking_preview_is_ready(content: str, *, width: int) -> bool:
    text = collapse_inline(content)
    if not text:
        return False
    target_width = max(THINKING_PREVIEW_MIN_CELLS, width - 3)
    return cell_len(text) >= target_width


def _style_for_line_kind(kind: str, theme: CLITheme | None = None) -> str:
    selected_theme = theme or DEFAULT_CLI_THEME
    return {
        "user_frame": rich_style(selected_theme, "input_border"),
        "user_text": rich_style(selected_theme, "user_text"),
        "assistant_header": rich_style(selected_theme, "assistant_header", bold=True),
        "autonomous_header": rich_style(selected_theme, "artifact", bold=True),
        "thinking": rich_style(selected_theme, "thinking", italic=True),
        "preamble": "",
        "tool": rich_style(selected_theme, "tool"),
        "tool_detail": rich_style(selected_theme, "diagnostic"),
        "final": "",
        "system": rich_style(selected_theme, "diagnostic"),
        "error": rich_style(selected_theme, "error"),
        "artifact": rich_style(selected_theme, "artifact"),
        "diagnostic": rich_style(selected_theme, "diagnostic"),
    }.get(kind, "")


def _tool_steps(state: CLIUIState) -> list[ToolCallStep]:
    steps: list[ToolCallStep] = []
    for message in state.messages:
        if not isinstance(message, AssistantMessage):
            continue
        steps.extend(step for step in message.steps if isinstance(step, ToolCallStep))
    return steps


def _stream_flush_width(width: int | None) -> int:
    return max(32, min(72, coerce_width(width) - 8))


def _line_prefers_rich_markdown(line: str) -> bool:
    stripped = str(line or "").strip()
    if not stripped:
        return False
    if (
        FENCE_RE.match(line)
        or HEADING_RE.match(line)
        or HR_RE.match(line)
        or BLOCKQUOTE_RE.match(line)
        or BULLET_RE.match(line)
        or NUMBERED_RE.match(line)
    ):
        return True
    if _looks_like_pipe_table_row(stripped):
        return True
    return any(marker in stripped for marker in _INLINE_MARKDOWN_MARKERS)


def _looks_like_pipe_table_row(line: str) -> bool:
    text = str(line or "").strip()
    if "|" not in text:
        return False
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells = [cell.strip() for cell in text.split("|")]
    return len(cells) >= 2 and any(cells)


def _coerce_markdown_block(block: MarkdownBlock | str) -> MarkdownBlock:
    if isinstance(block, MarkdownBlock):
        return block
    return MarkdownBlock.from_text(block)


def _should_print_markdown_separator(
    previous: MarkdownBlock | None,
    current: MarkdownBlock,
) -> bool:
    if previous is None:
        return False
    if previous.kind == "heading" and current.kind == "heading":
        return False
    if previous.kind in _RICH_NATIVE_TRAILING_BLANK_KINDS:
        return False
    if current.kind in _RICH_NATIVE_LEADING_BLANK_KINDS:
        return False
    if previous.trailing_blank_lines > 0 or current.leading_blank_lines > 0:
        return True
    return previous.kind in _DENSE_MARKDOWN_BLOCK_KINDS


__all__ = [
    "RichReplRenderer",
    "render_tool_row",
]
