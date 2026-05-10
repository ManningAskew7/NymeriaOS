"""Transcript rendering from reducer-owned CLI/TUI state."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from rich.cells import cell_len

from ..state import (
    AssistantMessage,
    CLIUIState,
    MessageStep,
    ResponseStep,
    SystemMessage,
    ThinkingStep,
    ToolCallStep,
    TranscriptMessage,
    UserMessage,
    WorkspaceArtifact,
)
from .markdown import (
    coerce_width,
    render_markdown_lines,
    truncate_cell_width,
    wrap_plain_text,
)
from .tool_rows import ToolRowRenderOptions, format_artifact_line, format_tool_row

DEFAULT_TRANSCRIPT_WIDTH = 80


@dataclass(frozen=True, slots=True)
class TranscriptRenderOptions:
    """Options for turning reducer state into terminal transcript text."""

    show_streaming_thinking: bool = False
    ascii_only: bool = True
    include_artifacts: bool = True
    tool_row_options: ToolRowRenderOptions = field(
        default_factory=ToolRowRenderOptions
    )


class TranscriptRenderer:
    """Small cached renderer for full transcript snapshots."""

    def __init__(self, *, options: TranscriptRenderOptions | None = None) -> None:
        self.options = options or TranscriptRenderOptions()
        self._cache_width: int | None = None
        self._message_cache: dict[str, tuple[object, tuple[str, ...]]] = {}
        self.rendered_message_count = 0

    def render(
        self,
        state: CLIUIState,
        *,
        width: int | None = None,
        options: TranscriptRenderOptions | None = None,
    ) -> str:
        """Render a transcript and reuse unchanged message blocks."""

        selected_width = coerce_width(width, default=DEFAULT_TRANSCRIPT_WIDTH)
        selected_options = options or self.options
        if self._cache_width != selected_width or selected_options != self.options:
            self._message_cache.clear()
            self._cache_width = selected_width
            self.options = selected_options

        lines: list[str] = []
        attached_artifacts: set[str] = set()
        seen_message_ids: set[str] = set()
        for message in state.messages:
            seen_message_ids.add(message.id)
            rendered = self._render_cached_message(
                message,
                width=selected_width,
                options=selected_options,
            )
            lines.extend(rendered)
            attached_artifacts.update(_message_artifact_keys(message))

        if selected_options.include_artifacts:
            for artifact in state.artifacts:
                if _artifact_key(artifact) in attached_artifacts:
                    continue
                lines.append(format_artifact_line(artifact, width=selected_width))

        for error in state.errors:
            lines.extend(
                _plain_block(
                    f"Error: {error.content or error.code}",
                    width=selected_width,
                )
            )
        for diagnostic in state.diagnostics:
            text = diagnostic.message or diagnostic.source_type or "Unknown event"
            lines.extend(_plain_block(f"Diagnostic: {text}", width=selected_width))

        stale = set(self._message_cache) - seen_message_ids
        for message_id in stale:
            self._message_cache.pop(message_id, None)

        return "\n".join(_bounded_lines(lines, selected_width)).rstrip()

    def _render_cached_message(
        self,
        message: TranscriptMessage,
        *,
        width: int,
        options: TranscriptRenderOptions,
    ) -> tuple[str, ...]:
        signature = _message_signature(message, options)
        cached = self._message_cache.get(message.id)
        if cached and cached[0] == signature:
            return cached[1]

        rendered = tuple(render_message(message, width=width, options=options))
        self._message_cache[message.id] = (signature, rendered)
        self.rendered_message_count += 1
        return rendered


def render_transcript(
    state: CLIUIState,
    *,
    width: int | None = None,
    options: TranscriptRenderOptions | None = None,
) -> str:
    """Render reducer state into a terminal transcript snapshot."""

    return TranscriptRenderer(options=options).render(state, width=width)


def render_message(
    message: TranscriptMessage,
    *,
    width: int | None = None,
    options: TranscriptRenderOptions | None = None,
) -> list[str]:
    """Render one transcript message into bounded terminal lines."""

    selected_width = coerce_width(width, default=DEFAULT_TRANSCRIPT_WIDTH)
    selected_options = options or TranscriptRenderOptions()
    if isinstance(message, UserMessage):
        return _prefixed_plain_block("You", message.content, selected_width)
    if isinstance(message, AssistantMessage):
        return _assistant_lines(message, selected_width, selected_options)
    if isinstance(message, SystemMessage):
        return _system_lines(message, selected_width)
    return []


def _assistant_lines(
    message: AssistantMessage,
    width: int,
    options: TranscriptRenderOptions,
) -> list[str]:
    lines: list[str] = []
    for step in message.steps:
        if isinstance(step, ThinkingStep):
            lines.extend(_thinking_lines(step, message, width, options))
        elif isinstance(step, ToolCallStep):
            lines.append(
                format_tool_row(
                    step,
                    width=width,
                    options=options.tool_row_options,
                )
            )
            if options.include_artifacts:
                lines.extend(
                    format_artifact_line(item, width=width)
                    for item in step.artifacts
                )
        elif isinstance(step, ResponseStep):
            lines.extend(_response_lines(step, width, options))

    if message.tool_reload_info is not None:
        tools = ", ".join(message.tool_reload_info.tools)
        text = f"Tools reloaded: {tools}" if tools else "Tools reloaded."
        lines.extend(_plain_block(text, width=width))

    if not lines and message.status == "streaming":
        lines.append(truncate_cell_width("Nymeria: ", width))
    return lines


def _thinking_lines(
    step: ThinkingStep,
    message: AssistantMessage,
    width: int,
    options: TranscriptRenderOptions,
) -> list[str]:
    if message.status != "streaming":
        return [truncate_cell_width("Thought", width)]
    lines = [truncate_cell_width("Thinking...", width)]
    if options.show_streaming_thinking and step.content:
        content_width = max(1, width - 2)
        for line in render_markdown_lines(
            step.content,
            width=content_width,
            ascii_only=options.ascii_only,
        ):
            lines.append(truncate_cell_width(f"  {line}", width))
    return lines


def _response_lines(
    step: ResponseStep,
    width: int,
    options: TranscriptRenderOptions,
) -> list[str]:
    content = step.content.strip()
    if not content:
        return []
    rendered = render_markdown_lines(
        content,
        width=max(1, width - len("Nymeria: ")),
        ascii_only=options.ascii_only,
    )
    return _prefixed_lines("Nymeria", rendered, width)


def _system_lines(message: SystemMessage, width: int) -> list[str]:
    if message.kind == "compaction_notice":
        text = "Context compacted."
        if message.context_summary:
            text = f"{text} {message.context_summary}"
        if message.messages_removed:
            text = f"{text} Removed {message.messages_removed} messages."
        return _plain_block(text, width=width)
    if message.kind == "iteration_limit":
        return _plain_block(
            message.content or "Reached turn safety limit.",
            width=width,
        )
    if message.kind == "error":
        return _plain_block(f"Error: {message.content}", width=width)
    if message.kind == "tool_reload":
        tools = message.details.get("tools") if message.details else None
        if tools:
            text = f"Tools reloaded: {', '.join(str(tool) for tool in tools)}"
        else:
            text = "Tools reloaded."
        return _plain_block(text, width=width)
    if message.kind == "context_attached":
        return _plain_block(
            message.content or message.context_summary or "Context attached.",
            width=width,
        )
    return _plain_block(
        message.content or message.kind.replace("_", " ").title(),
        width=width,
    )


def _prefixed_plain_block(prefix: str, content: str, width: int) -> list[str]:
    body_width = max(1, width - len(prefix) - 2)
    return _prefixed_lines(
        prefix,
        wrap_plain_text(content, width=body_width),
        width,
    )


def _prefixed_lines(prefix: str, lines: list[str], width: int) -> list[str]:
    first_prefix = f"{prefix}: "
    continuation_prefix = " " * len(first_prefix)
    if not lines:
        return [truncate_cell_width(first_prefix, width)]
    output: list[str] = []
    for index, line in enumerate(lines):
        if not line:
            output.append("")
            continue
        label = first_prefix if index == 0 else continuation_prefix
        output.append(truncate_cell_width(f"{label}{line}", width))
    return output


def _plain_block(text: str, *, width: int) -> list[str]:
    return [truncate_cell_width(line, width) for line in wrap_plain_text(text, width=width)]


def _bounded_lines(lines: list[str], width: int) -> list[str]:
    return [truncate_cell_width(line, width) for line in lines]


def _message_artifact_keys(message: TranscriptMessage) -> set[str]:
    if not isinstance(message, AssistantMessage):
        return set()
    keys: set[str] = set()
    for step in message.steps:
        if isinstance(step, ToolCallStep):
            keys.update(_artifact_key(item) for item in step.artifacts)
    return keys


def _artifact_key(artifact: WorkspaceArtifact) -> str:
    return artifact.path or artifact.name or _stable_repr(artifact.payload)


def _message_signature(
    message: TranscriptMessage,
    options: TranscriptRenderOptions,
) -> object:
    if isinstance(message, UserMessage):
        return (
            "user",
            message.content,
            message.attachments,
            message.status,
            message.context_summary,
        )
    if isinstance(message, SystemMessage):
        return (
            "system",
            message.kind,
            message.content,
            message.context_summary,
            message.messages_removed,
            message.auto_resumed,
            _stable_repr(message.details),
        )
    if isinstance(message, AssistantMessage):
        return (
            "assistant",
            message.status,
            tuple(_step_signature(step) for step in message.steps),
            _stable_repr(message.tool_reload_info),
            options,
        )
    return repr(message)


def _step_signature(step: MessageStep) -> object:
    if isinstance(step, ThinkingStep):
        return ("thinking", step.content, step.started_at, step.updated_at)
    if isinstance(step, ResponseStep):
        return ("response", step.content, step.started_at, step.updated_at)
    if isinstance(step, ToolCallStep):
        return (
            "tool",
            step.id,
            step.name,
            _stable_repr(step.arguments),
            _stable_repr(step.result),
            tuple(_artifact_key(item) for item in step.artifacts),
            step.status,
            step.started_at,
            step.updated_at,
            step.ended_at,
        )
    return repr(step)


def _stable_repr(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, default=repr)
    except TypeError:
        return repr(value)


def max_line_width(text: str) -> int:
    """Return the widest rendered line in terminal cells."""

    return max((cell_len(line) for line in text.splitlines()), default=0)


__all__ = [
    "TranscriptRenderOptions",
    "TranscriptRenderer",
    "max_line_width",
    "render_message",
    "render_transcript",
]
