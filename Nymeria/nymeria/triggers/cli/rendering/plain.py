"""Plain text renderer for non-interactive CLI output."""

from __future__ import annotations

import copy
import json
import re
import sys
from collections.abc import Iterable, Sequence
from typing import Any, TextIO

from ..state import (
    AssistantMessage,
    CLIUIState,
    SystemMessage,
    ToolCallStep,
    WorkspaceArtifact,
    create_initial_state,
    reduce_stream_event,
    select_last_assistant_message,
    select_response_content,
    start_turn,
)
from .indicator import activity_state_from_ui_state

ANSI_ESCAPE_PREFIX = "\x1b"
ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
DEFAULT_PREVIEW_LIMIT = 120


class PlainRenderer:
    """Render reducer-owned chat state as plain stdout/stderr text.

    The renderer intentionally avoids ANSI escape sequences and animation. It
    is suitable for pipes, dumb terminals, CI logs, and machine-adjacent use.
    """

    def __init__(
        self,
        *,
        state: CLIUIState | None = None,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        width: int | None = None,
    ) -> None:
        self.state = state or create_initial_state()
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.width = _positive_width(width)
        self._last_activity_text = ""
        self._rendered_tool_results: set[str] = set()
        self._rendered_system_ids: set[str] = set()
        self._rendered_error_count = len(self.state.errors)
        self._rendered_diagnostic_count = len(self.state.diagnostics)
        self._rendered_artifacts: set[str] = {
            _artifact_key(artifact) for artifact in self.state.artifacts
        }
        self._response_lengths: dict[str, int] = _assistant_response_lengths(
            self.state
        )
        self._stdout_line_open = False

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
        """Reduce one event and render only the newly visible output."""

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
        """Render a full plain transcript snapshot."""

        snapshot = state or self.state
        for message in snapshot.messages:
            if isinstance(message, AssistantMessage):
                self._render_assistant_snapshot(message)
            elif isinstance(message, SystemMessage):
                self._write_stderr(_system_message_text(message))
        for error in snapshot.errors:
            self._write_stderr(f"Error: {error.content or error.code}")
        self._ensure_stdout_newline()

    def finish(self) -> None:
        """Finish any open stdout line."""

        self._ensure_stdout_newline()

    def _render_transition(
        self,
        previous: CLIUIState,
        current: CLIUIState,
    ) -> None:
        if len(current.errors) > len(previous.errors):
            self._response_lengths = _assistant_response_lengths(current)
        else:
            self._render_response_delta(current)
        self._render_activity(current)
        self._render_tool_results(previous, current)
        self._render_new_artifacts(current)
        self._render_new_system_messages(current)
        self._render_new_errors(current)
        self._render_new_diagnostics(current)

        if current.turn_status in {"complete", "error"}:
            self.finish()

    def _render_response_delta(self, state: CLIUIState) -> None:
        message = select_last_assistant_message(state)
        if message is None:
            return

        content = select_response_content(message)
        previous_length = self._response_lengths.get(message.id, 0)
        if len(content) > previous_length:
            self.stdout.write(content[previous_length:])
            self.stdout.flush()
            self._stdout_line_open = bool(content and not content.endswith("\n"))
        self._response_lengths[message.id] = len(content)

    def _render_activity(self, state: CLIUIState) -> None:
        activity = activity_state_from_ui_state(state, now=state.updated_at)
        text = activity_status_text(activity, width=self.width)
        if not text or text == self._last_activity_text:
            return
        self._write_stderr(text)
        self._last_activity_text = text

    def _render_tool_results(
        self,
        _previous: CLIUIState,
        current: CLIUIState,
    ) -> None:
        for tool in _tool_steps(current):
            if tool.status == "running":
                continue
            if tool.id in self._rendered_tool_results:
                continue
            self._write_stderr(tool_result_summary(tool, width=self.width))
            self._rendered_tool_results.add(tool.id)

    def _render_new_artifacts(self, state: CLIUIState) -> None:
        for artifact in state.artifacts:
            key = _artifact_key(artifact)
            if key in self._rendered_artifacts:
                continue
            label = artifact.path or artifact.name or "workspace artifact"
            self._write_stderr(truncate_plain(f"Artifact: {label}", self.width))
            self._rendered_artifacts.add(key)

    def _render_new_system_messages(self, state: CLIUIState) -> None:
        for message in state.messages:
            if not isinstance(message, SystemMessage):
                continue
            if message.id in self._rendered_system_ids:
                continue
            self._write_stderr(_system_message_text(message))
            self._rendered_system_ids.add(message.id)

    def _render_new_errors(self, state: CLIUIState) -> None:
        for error in state.errors[self._rendered_error_count :]:
            self._write_stderr(f"Error: {error.content or error.code}")
        self._rendered_error_count = len(state.errors)

    def _render_new_diagnostics(self, state: CLIUIState) -> None:
        for diagnostic in state.diagnostics[self._rendered_diagnostic_count :]:
            text = diagnostic.message or diagnostic.source_type or "Unknown event"
            self._write_stderr(f"Diagnostic: {text}")
        self._rendered_diagnostic_count = len(state.diagnostics)

    def _render_assistant_snapshot(self, message: AssistantMessage) -> None:
        for step in message.steps:
            if isinstance(step, ToolCallStep):
                self._write_stderr(tool_result_summary(step, width=self.width))
        content = select_response_content(message)
        if content:
            self.stdout.write(content)
            self._stdout_line_open = not content.endswith("\n")

    def _write_stderr(self, text: str) -> None:
        self.stderr.write(f"{strip_ansi(text)}\n")
        self.stderr.flush()

    def _ensure_stdout_newline(self) -> None:
        if self._stdout_line_open:
            self.stdout.write("\n")
            self.stdout.flush()
            self._stdout_line_open = False


def activity_status_text(activity: Any, *, width: int | None = None) -> str:
    """Return a spinner-free activity line for plain output."""

    if activity is None or not getattr(activity, "active", True):
        return ""
    phase = getattr(activity, "phase", "")
    if phase == "typing":
        return ""
    label = {
        "processing": "Processing...",
        "thinking": "Thinking...",
        "formulating": "Formulating...",
        "processing_results": "Processing results...",
        "waiting": "Waiting...",
    }.get(phase, "")
    if not label:
        return ""
    detail = " ".join(str(getattr(activity, "detail", "") or "").split())
    text = f"{label} {detail}".strip()
    return truncate_plain(text, _positive_width(width))


def tool_result_summary(
    tool: ToolCallStep,
    *,
    width: int | None = None,
) -> str:
    """Format one compact, bounded tool row."""

    parts = [">", tool.name or "tool"]
    args_preview = format_args_preview(tool.arguments)
    if args_preview:
        parts.append(args_preview)
    result_preview = result_preview_text(tool.result)
    if result_preview:
        parts.extend(["->", result_preview])
    if tool.status == "error":
        parts.append("(error)")
    elif tool.status == "cancelled":
        parts.append("(cancelled)")
    elif tool.status == "running":
        parts.append("(running)")
    return truncate_plain(" ".join(parts), _positive_width(width))


def format_args_preview(
    args: dict[str, Any],
    *,
    limit: int = 80,
) -> str:
    """Produce a compact one-line preview of tool arguments."""

    if not args:
        return ""
    if len(args) == 1:
        value = next(iter(args.values()))
        return truncate_plain(_stringify(value), limit)

    parts: list[str] = []
    for key, value in args.items():
        rendered_value = _stringify(value)
        if len(rendered_value) > 40:
            rendered_value = f"{rendered_value[:37].rstrip()}..."
        parts.append(f"{key}={rendered_value}")
    return truncate_plain(", ".join(parts), limit)


def result_preview_text(
    result: Any,
    *,
    limit: int = DEFAULT_PREVIEW_LIMIT,
) -> str:
    """Return a bounded one-line result preview."""

    if result in (None, ""):
        return ""
    return truncate_plain(_stringify(result), limit)


def truncate_plain(text: str, width: int | None) -> str:
    """Collapse and truncate plain text to a terminal width."""

    normalized = " ".join(str(text or "").split())
    selected_width = _positive_width(width)
    if len(normalized) <= selected_width:
        return normalized
    if selected_width <= 3:
        return normalized[:selected_width]
    return f"{normalized[: selected_width - 3].rstrip()}..."


def strip_ansi(text: str) -> str:
    """Defensively remove escape-prefixed text from plain output."""

    if ANSI_ESCAPE_PREFIX not in text:
        return text
    return ANSI_ESCAPE_RE.sub("", text)


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
        steps.extend(
            copy.deepcopy(step)
            for step in message.steps
            if isinstance(step, ToolCallStep)
        )
    return steps


def _artifact_key(artifact: WorkspaceArtifact) -> str:
    return artifact.path or artifact.name or repr(artifact.payload)


def _system_message_text(message: SystemMessage) -> str:
    if message.kind == "compaction_notice":
        suffix = f" {message.context_summary}" if message.context_summary else ""
        return truncate_plain(f"Context compacted.{suffix}", 120)
    if message.kind == "iteration_limit":
        return message.content or "Reached turn safety limit."
    if message.kind == "tool_reload":
        tools = message.details.get("tools") if message.details else None
        if tools:
            return f"Tools reloaded: {', '.join(str(tool) for tool in tools)}"
        return "Tools reloaded."
    if message.kind == "context_attached":
        return message.content or message.context_summary or "Context attached."
    if message.kind == "error":
        return f"Error: {message.content}"
    return message.content or message.kind.replace("_", " ").title()


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    except TypeError:
        return str(value)


def _positive_width(width: int | None) -> int:
    if width is None:
        return 80
    try:
        parsed = int(width)
    except (TypeError, ValueError):
        return 80
    return max(1, parsed)


__all__ = [
    "PlainRenderer",
    "activity_status_text",
    "format_args_preview",
    "result_preview_text",
    "strip_ansi",
    "tool_result_summary",
    "truncate_plain",
]
