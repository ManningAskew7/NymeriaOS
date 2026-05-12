"""Rich compatibility renderer backed by the CLI reducer state."""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from typing import Any, TextIO

from rich.console import Console
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
from ..theme import CLITheme, DEFAULT_CLI_THEME, rich_style
from .markdown import collapse_inline, render_markdown_lines, truncate_cell_width
from .plain import (
    truncate_plain,
)
from .tool_rows import ToolRowRenderOptions, format_tool_row
from .transcript import (
    TranscriptLine,
    TranscriptRenderOptions,
    format_turn_separator,
    render_message_lines,
    render_transcript_lines,
)


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
    ) -> None:
        self.state = state or create_initial_state()
        self.capabilities = capabilities
        self.theme = theme or DEFAULT_CLI_THEME
        self.width = _positive_width(width or getattr(capabilities, "width", 80))
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
        self._response_buffer = ""
        self._response_lengths = _assistant_response_lengths(self.state)
        self._thinking_lengths = _assistant_thinking_lengths(self.state)
        self._thinking_preview_rendered: set[str] = set()
        self._rendered_tool_results: set[str] = set()
        self._rendered_system_ids: set[str] = set()
        self._rendered_assistant_headers: set[str] = set()
        self._rendered_turn_end_ids: set[str] = set()
        self._rendered_error_count = len(self.state.errors)
        self._rendered_diagnostic_count = len(self.state.diagnostics)
        self._last_rendered_block: str | None = None
        self._response_stream_active = False
        self._stream_line_buffer = ""
        self._turn_seen_tool = False
        self.transcript_verbose = False

    def set_theme(self, theme: CLITheme) -> None:
        """Update colors for future transcript output."""

        self.theme = theme

    def reset_state(self, state: CLIUIState | None = None) -> None:
        """Reset transcript state after a thread/user switch or clear."""

        self.state = state or create_initial_state()
        self._response_buffer = ""
        self._response_lengths = _assistant_response_lengths(self.state)
        self._thinking_lengths = _assistant_thinking_lengths(self.state)
        self._thinking_preview_rendered = set()
        self._rendered_tool_results = set()
        self._rendered_system_ids = set()
        self._rendered_assistant_headers = set()
        self._rendered_turn_end_ids = set()
        self._rendered_error_count = len(self.state.errors)
        self._rendered_diagnostic_count = len(self.state.diagnostics)
        self._last_rendered_block = None
        self._response_stream_active = False
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
            attachments=attachments,
            now=now,
        )
        self._response_lengths = _assistant_response_lengths(self.state)
        self._thinking_lengths = _assistant_thinking_lengths(self.state)
        self._thinking_preview_rendered.discard(self.state.messages[-1].id)
        self._last_rendered_block = None
        self._response_stream_active = False
        self._stream_line_buffer = ""
        self._turn_seen_tool = False
        if len(self.state.messages) >= 2 and isinstance(
            self.state.messages[-2],
            UserMessage,
        ):
            self._render_user_message(self.state.messages[-2])
            self.console.print()
        if self.state.messages and isinstance(
            self.state.messages[-1],
            AssistantMessage,
        ):
            assistant = self.state.messages[-1]
            self._render_separator(
                "Nymeria",
                style=_style_for_line_kind("assistant_header", self.theme),
            )
            self._rendered_assistant_headers.add(assistant.id)
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
        for line in render_transcript_lines(
            snapshot,
            width=self.width,
            options=self._transcript_options(),
        ):
            self._render_transcript_line(line)
        self._mark_state_rendered(snapshot)

    def flush_response(self) -> None:
        """Render buffered response markdown."""

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
            ascii_only=self._ascii_only(),
        ):
            rendered = f"  {line}" if line else ""
            self.console.print(
                Text(rendered, style=_style_for_line_kind(block_kind, self.theme))
            )
        self._response_buffer = ""

    def _render_transition(
        self,
        previous: CLIUIState,
        current: CLIUIState,
    ) -> None:
        self._render_thinking_delta(current)
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
            if tool.status == "running":
                continue
            if tool.id in self._rendered_tool_results:
                continue
            self.flush_response()
            message = _message_for_tool(current, tool.id)
            if message is not None:
                self._ensure_assistant_header(current, message)
            self._begin_assistant_block("tool")
            self.console.print(
                render_tool_row(
                    tool,
                    width=self.width,
                    ascii_only=self._ascii_only(),
                    theme=self.theme,
                )
            )
            self._rendered_tool_results.add(tool.id)
            self._turn_seen_tool = True
            printed = True
        return printed

    def _render_new_system_messages(self, state: CLIUIState) -> bool:
        printed = False
        for message in state.messages:
            if not isinstance(message, SystemMessage):
                continue
            if message.id in self._rendered_system_ids:
                continue
            self.flush_response()
            for line in render_message_lines(
                message,
                width=self.width,
                options=self._transcript_options(),
            ):
                self._render_transcript_line(line)
            self._rendered_system_ids.add(message.id)
            self._last_rendered_block = None
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
        self._render_separator(
            "You",
            style=_style_for_line_kind("user_header", self.theme),
        )
        content = truncate_plain(message.content, max(1, self.width - 2))
        self.console.print(
            Text(f"  {content}", style=_style_for_line_kind("user_text", self.theme))
        )
        if message.attachments:
            label = "attachment" if len(message.attachments) == 1 else "attachments"
            self.console.print(
                Text(
                    f"  {len(message.attachments)} {label}",
                    style=_style_for_line_kind("artifact", self.theme),
                )
            )

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
                    elif message.id not in self._thinking_preview_rendered:
                        self._render_thinking_text(step.content, preview=True)
                        self._thinking_preview_rendered.add(message.id)
                    self._thinking_lengths[key] = len(step.content)
                thinking_index += 1

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
        body_width = max(1, self.width - 4)
        for line in render_markdown_lines(
            text,
            width=body_width,
            ascii_only=self._ascii_only(),
        ):
            rendered = f"  {marker}{line}" if line else ""
            self.console.print(
                Text(
                    rendered,
                    style=_style_for_line_kind("thinking", self.theme),
                )
            )

    def _stream_response_delta(self, delta: str) -> None:
        if not delta:
            return
        block_kind = "final" if self._turn_seen_tool else "preamble"
        self._begin_assistant_block(block_kind)
        text = str(delta or "").replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        self._stream_line_buffer += text
        self._response_stream_active = True
        self._flush_complete_stream_lines(block_kind)
        self._flush_stream_line_if_ready(block_kind)

    def _render_assistant_message(self, message: AssistantMessage) -> None:
        self._render_separator(
            "Nymeria",
            style=_style_for_line_kind("assistant_header", self.theme),
        )
        last_tool_index = _last_tool_index(message.steps)
        previous_block: str | None = None
        rendered_body = False
        for index, step in enumerate(message.steps):
            if isinstance(step, ThinkingStep):
                block_kind = "thinking"
            elif isinstance(step, ToolCallStep):
                block_kind = "tool"
            elif isinstance(step, ResponseStep) and step.content.strip():
                block_kind = "preamble" if index < last_tool_index else "final"
            else:
                continue

            if previous_block is not None and previous_block != block_kind:
                self.console.print()
                if _needs_assistant_divider(
                    block_kind,
                    previous_block=previous_block,
                ):
                    self._render_assistant_divider()
                    self.console.print()
            if isinstance(step, ThinkingStep):
                content = _thinking_preview_text(
                    step.content or "...",
                    width=max(1, self.width - 4),
                )
                marker = "| " if self._ascii_only() else "\u2502 "
                self.console.print(
                    Text(
                        f"  {marker}{content}",
                        style=_style_for_line_kind("thinking", self.theme),
                    )
                )
            elif isinstance(step, ToolCallStep):
                self.console.print(
                    render_tool_row(
                        step,
                        width=self.width,
                        ascii_only=self._ascii_only(),
                        theme=self.theme,
                    )
                )
            elif isinstance(step, ResponseStep):
                for line in render_markdown_lines(
                    step.content.strip(),
                    width=max(1, self.width - 2),
                    ascii_only=self._ascii_only(),
                ):
                    rendered = f"  {line}" if line else ""
                    self.console.print(
                        Text(
                            rendered,
                            style=_style_for_line_kind(block_kind, self.theme),
                        )
                    )
            previous_block = block_kind
            rendered_body = True
        if message.status == "complete" and rendered_body:
            self.console.print()
            self._render_assistant_divider()

    def _render_system_message(self, message: SystemMessage) -> None:
        self._render_separator(
            "System",
            style=_style_for_line_kind("diagnostic", self.theme),
        )
        if message.kind == "compaction_notice":
            text = "Context compacted."
            if message.context_summary:
                text = f"{text} {message.context_summary}"
            self.console.print(
                Text(
                    truncate_plain(text, self.width),
                    style=_style_for_line_kind("assistant_header", self.theme),
                )
            )
            return
        if message.kind == "iteration_limit":
            self.console.print(
                Text(
                    message.content or "Reached turn safety limit.",
                    style=_style_for_line_kind("tool", self.theme),
                )
            )
            return
        if message.kind == "error":
            self.error_console.print(
                Text(
                    f"Error: {message.content}",
                    style=_style_for_line_kind("error", self.theme),
                )
            )
            return
        self.console.print(
            Text(
                message.content or message.kind.replace("_", " ").title(),
                style=_style_for_line_kind("diagnostic", self.theme),
            )
        )

    def _render_separator(self, label: str, *, style: str) -> None:
        self.console.print(
            Text(
                format_turn_separator(
                    label,
                    width=self.width,
                    ascii_only=self._ascii_only(),
                ),
                style=style,
            )
        )

    def _ensure_assistant_header(
        self,
        state: CLIUIState,
        message: AssistantMessage,
    ) -> None:
        if message.id in self._rendered_assistant_headers:
            return
        label = _assistant_label_for_message(
            state,
            message,
            ascii_only=self._ascii_only(),
        )
        style = (
            _style_for_line_kind("autonomous_header", self.theme)
            if label != "Nymeria"
            else _style_for_line_kind("assistant_header", self.theme)
        )
        if self._last_rendered_block is not None:
            self.console.print()
        self._render_separator(label, style=style)
        self._rendered_assistant_headers.add(message.id)
        self._last_rendered_block = None
        self._turn_seen_tool = False

    def _render_assistant_divider(self) -> None:
        divider_width = min(max(1, self.width - 2), 32)
        glyph = "." if self._ascii_only() else "\u00b7"
        self.console.print(
            Text(
                f"  {glyph * divider_width}",
                style=_style_for_line_kind("assistant_divider", self.theme),
            )
        )

    def _begin_assistant_block(self, block_kind: str) -> None:
        if self._stream_line_buffer and self._last_rendered_block != block_kind:
            self._flush_stream_line(
                force=True,
                block_kind=self._last_rendered_block or block_kind,
            )
        if (
            self._last_rendered_block is not None
            and self._last_rendered_block != block_kind
        ):
            self.console.print()
            if _needs_assistant_divider(
                block_kind,
                previous_block=self._last_rendered_block,
            ):
                self._render_assistant_divider()
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
        style = _style_for_line_kind(block_kind, self.theme)
        if not line.strip():
            self.console.print()
            self._flush_console_file()
            return
        body_width = max(1, self.width - 2)
        for rendered_line in render_markdown_lines(
            _clean_stream_delta(line),
            width=body_width,
            ascii_only=self._ascii_only(),
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

        self._response_buffer = ""
        self._response_lengths = _assistant_response_lengths(state)
        self._thinking_lengths = _assistant_thinking_lengths(state)
        self._thinking_preview_rendered = {
            message.id
            for message in state.messages
            if isinstance(message, AssistantMessage)
            and any(
                isinstance(step, ThinkingStep) and bool(step.content)
                for step in message.steps
            )
        }
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
        self._response_stream_active = False
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
        self._render_assistant_divider()
        self._rendered_turn_end_ids.add(message.id)
        self._last_rendered_block = "assistant_divider"

    def _render_transcript_line(self, line: TranscriptLine) -> None:
        self.console.print(
            Text(line.text, style=_style_for_line_kind(line.kind, self.theme))
        )

    def _transcript_options(self) -> TranscriptRenderOptions:
        return TranscriptRenderOptions(
            verbose=self.transcript_verbose,
            ascii_only=self._ascii_only(),
        )

    def _ascii_only(self) -> bool:
        return not bool(getattr(self.capabilities, "unicode_enabled", False))


def render_tool_row(
    tool: ToolCallStep,
    *,
    width: int | None = None,
    ascii_only: bool = False,
    theme: CLITheme | None = None,
) -> Text:
    """Return a Rich compact tool row."""

    selected_theme = theme or DEFAULT_CLI_THEME
    width = _positive_width(width)
    row = format_tool_row(
        tool,
        width=max(1, width - 2),
        options=ToolRowRenderOptions(show_duration=True, ascii_only=ascii_only),
    )
    style = (
        _style_for_line_kind("error", selected_theme)
        if tool.status == "error"
        else _style_for_line_kind("tool", selected_theme)
    )
    return Text(f"  {row}", style=style)


def _last_tool_index(steps: Sequence[Any]) -> int:
    for index in range(len(steps) - 1, -1, -1):
        if isinstance(steps[index], ToolCallStep):
            return index
    return -1


def _needs_assistant_divider(
    block_kind: str,
    *,
    previous_block: str,
) -> bool:
    return previous_block == "tool" and block_kind != "tool"


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


def _assistant_response_lengths(state: CLIUIState) -> dict[str, int]:
    return {
        message.id: len(select_response_content(message))
        for message in state.messages
        if isinstance(message, AssistantMessage)
    }


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


def _assistant_label_for_message(
    state: CLIUIState,
    message: AssistantMessage,
    *,
    ascii_only: bool,
) -> str:
    messages = list(state.messages)
    try:
        index = messages.index(message)
    except ValueError:
        return "Nymeria"
    if index <= 0:
        return "Nymeria"
    previous = messages[index - 1]
    if not isinstance(previous, SystemMessage) or previous.kind != "autonomous":
        return "Nymeria"
    parts = ["Nymeria", "autonomous"]
    source = str(previous.details.get("source") or "").strip()
    if source:
        parts.append(source)
    separator = " - " if ascii_only else " \u00b7 "
    return separator.join(parts)


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


def _style_for_line_kind(kind: str, theme: CLITheme | None = None) -> str:
    selected_theme = theme or DEFAULT_CLI_THEME
    return {
        "user_header": rich_style(selected_theme, "user_header", bold=True),
        "user_text": rich_style(selected_theme, "user_text"),
        "assistant_header": rich_style(selected_theme, "assistant_header", bold=True),
        "autonomous_header": rich_style(selected_theme, "artifact", bold=True),
        "thinking": rich_style(selected_theme, "thinking", italic=True),
        "preamble": "",
        "assistant_divider": rich_style(selected_theme, "separator"),
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


def _positive_width(width: int | None) -> int:
    if width is None:
        return 80
    try:
        parsed = int(width)
    except (TypeError, ValueError):
        return 80
    return max(1, parsed)


def _stream_flush_width(width: int | None) -> int:
    return max(32, min(72, _positive_width(width) - 8))


__all__ = [
    "RichReplRenderer",
    "render_tool_row",
]
