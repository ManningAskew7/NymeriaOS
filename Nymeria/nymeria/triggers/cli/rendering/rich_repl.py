"""Rich compatibility renderer backed by the CLI reducer state."""

from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from typing import Any, TextIO

from rich.console import Console
from rich.markdown import Markdown
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
from .plain import (
    truncate_plain,
)
from .tool_rows import ToolRowRenderOptions, format_tool_row
from .transcript import format_turn_separator


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
    ) -> None:
        self.state = state or create_initial_state()
        self.capabilities = capabilities
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
        self._rendered_tool_results: set[str] = set()
        self._rendered_system_ids: set[str] = set()
        self._rendered_error_count = len(self.state.errors)
        self._rendered_diagnostic_count = len(self.state.diagnostics)

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
        for message in snapshot.messages:
            if isinstance(message, UserMessage):
                self._render_user_message(message)
            elif isinstance(message, AssistantMessage):
                self._render_assistant_message(message)
            elif isinstance(message, SystemMessage):
                self._render_system_message(message)

        for error in snapshot.errors:
            self.error_console.print(Text(f"Error: {error.content}", style="red"))
        for diagnostic in snapshot.diagnostics:
            text = diagnostic.message or diagnostic.source_type or "Unknown event"
            self.error_console.print(Text(f"Diagnostic: {text}", style="dim"))

    def flush_response(self) -> None:
        """Render buffered response markdown."""

        text = self._response_buffer.strip()
        if not text:
            self._response_buffer = ""
            return
        self.console.print(Markdown(text))
        self._response_buffer = ""

    def _render_transition(
        self,
        previous: CLIUIState,
        current: CLIUIState,
    ) -> None:
        if len(current.errors) > len(previous.errors):
            self._response_lengths = _assistant_response_lengths(current)
        else:
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

    def _collect_response_delta(self, state: CLIUIState) -> None:
        message = select_last_assistant_message(state)
        if message is None:
            return

        content = select_response_content(message)
        previous_length = self._response_lengths.get(message.id, 0)
        if len(content) > previous_length:
            self._response_buffer += content[previous_length:]
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
            self.console.print(
                render_tool_row(
                    tool,
                    width=self.width,
                    ascii_only=self._ascii_only(),
                )
            )
            self._rendered_tool_results.add(tool.id)
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
            self._render_system_message(message)
            self._rendered_system_ids.add(message.id)
            printed = True
        return printed

    def _render_new_errors(self, state: CLIUIState) -> bool:
        printed = False
        for error in state.errors[self._rendered_error_count :]:
            self.flush_response()
            self.error_console.print(
                Text(f"Error: {error.content or error.code}", style="red")
            )
            printed = True
        self._rendered_error_count = len(state.errors)
        return printed

    def _render_new_diagnostics(self, state: CLIUIState) -> bool:
        printed = False
        for diagnostic in state.diagnostics[self._rendered_diagnostic_count :]:
            text = diagnostic.message or diagnostic.source_type or "Unknown event"
            self.flush_response()
            self.error_console.print(Text(f"Diagnostic: {text}", style="dim"))
            printed = True
        self._rendered_diagnostic_count = len(state.diagnostics)
        return printed

    def _render_user_message(self, message: UserMessage) -> None:
        self._render_separator("You", style="bold cyan")
        content = truncate_plain(message.content, max(1, self.width - 2))
        self.console.print(Text(f"  {content}", style="cyan"))

    def _render_assistant_message(self, message: AssistantMessage) -> None:
        self._render_separator("Nymeria", style="bold green")
        for step in message.steps:
            if isinstance(step, ThinkingStep):
                self.console.print(Text("  Thinking...", style="dim"))
            elif isinstance(step, ToolCallStep):
                self.console.print(
                    render_tool_row(
                        step,
                        width=self.width,
                        ascii_only=self._ascii_only(),
                    )
                )
            elif isinstance(step, ResponseStep) and step.content.strip():
                self.console.print(Markdown(step.content.strip()))

    def _render_system_message(self, message: SystemMessage) -> None:
        self._render_separator("System", style="dim")
        if message.kind == "compaction_notice":
            text = "Context compacted."
            if message.context_summary:
                text = f"{text} {message.context_summary}"
            self.console.print(Text(truncate_plain(text, self.width), style="green"))
            return
        if message.kind == "iteration_limit":
            self.console.print(
                Text(message.content or "Reached turn safety limit.", style="yellow")
            )
            return
        if message.kind == "error":
            self.error_console.print(Text(f"Error: {message.content}", style="red"))
            return
        self.console.print(
            Text(
                message.content or message.kind.replace("_", " ").title(),
                style="dim",
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

    def _ascii_only(self) -> bool:
        return not bool(getattr(self.capabilities, "unicode_enabled", False))


def render_tool_row(
    tool: ToolCallStep,
    *,
    width: int | None = None,
    ascii_only: bool = False,
) -> Text:
    """Return a Rich compact tool row."""

    width = _positive_width(width)
    row = format_tool_row(
        tool,
        width=max(1, width - 2),
        options=ToolRowRenderOptions(show_duration=True, ascii_only=ascii_only),
    )
    style = "red" if tool.status == "error" else "yellow"
    return Text(f"  {row}", style=style)


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


__all__ = [
    "RichReplRenderer",
    "render_tool_row",
]
