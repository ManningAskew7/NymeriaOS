"""Transcript rendering from reducer-owned CLI/TUI state."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

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
    collapse_inline,
    coerce_width,
    render_markdown_lines,
    truncate_cell_width,
    wrap_plain_text,
)
from .tool_rows import ToolRowRenderOptions, format_artifact_line, format_tool_row

DEFAULT_TRANSCRIPT_WIDTH = 80
INDENT = "  "
THINKING_MARKER_ASCII = "| "
THINKING_MARKER_UNICODE = "\u2502 "
VERBOSE_ARGS_LIMIT = 900
VERBOSE_RESULT_LIMIT = 900
VERBOSE_PAYLOAD_LINE_LIMIT = 24
EPOCH_TIMESTAMP_FLOOR = 1_000_000_000

TranscriptLineKind = Literal[
    "blank",
    "user_header",
    "user_text",
    "assistant_header",
    "autonomous_header",
    "thinking",
    "preamble",
    "assistant_divider",
    "tool",
    "tool_detail",
    "final",
    "system",
    "error",
    "artifact",
    "diagnostic",
]


@dataclass(frozen=True, slots=True)
class TranscriptLine:
    """One bounded transcript line plus its semantic style class."""

    text: str
    kind: TranscriptLineKind = "system"


@dataclass(frozen=True, slots=True)
class TranscriptRenderResult:
    """Styled transcript lines plus the plain text fallback."""

    lines: tuple[TranscriptLine, ...]
    text: str


@dataclass(frozen=True, slots=True)
class TranscriptRenderOptions:
    """Options for turning reducer state into terminal transcript text."""

    show_streaming_thinking: bool = False
    verbose: bool = False
    ascii_only: bool = True
    include_artifacts: bool = True
    assistant_activity_label: str = ""
    # API base URL used to build /workspace/download links for image artifacts.
    base_url: str = ""
    tool_row_options: ToolRowRenderOptions = field(
        default_factory=lambda: ToolRowRenderOptions(show_duration=True)
    )


class TranscriptRenderer:
    """Small cached renderer for full transcript snapshots."""

    def __init__(self, *, options: TranscriptRenderOptions | None = None) -> None:
        self.options = options or TranscriptRenderOptions()
        self._cache_width: int | None = None
        self._message_cache: dict[str, tuple[object, tuple[TranscriptLine, ...]]] = {}
        self.rendered_message_count = 0

    def render(
        self,
        state: CLIUIState,
        *,
        width: int | None = None,
        options: TranscriptRenderOptions | None = None,
    ) -> str:
        """Render a transcript and reuse unchanged message blocks."""

        return self.render_result(state, width=width, options=options).text

    def render_result(
        self,
        state: CLIUIState,
        *,
        width: int | None = None,
        options: TranscriptRenderOptions | None = None,
    ) -> TranscriptRenderResult:
        """Render a transcript as styled line records plus plain text."""

        selected_width = coerce_width(width, default=DEFAULT_TRANSCRIPT_WIDTH)
        selected_options = options or self.options
        if self._cache_width != selected_width or selected_options != self.options:
            self._message_cache.clear()
            self._cache_width = selected_width
            self.options = selected_options

        records: list[TranscriptLine] = []
        attached_artifacts: set[str] = set()
        seen_message_ids: set[str] = set()
        autonomous_label = ""
        for message in state.messages:
            seen_message_ids.add(message.id)
            assistant_label = (
                autonomous_label if isinstance(message, AssistantMessage) else ""
            )
            rendered = self._render_cached_message(
                message,
                width=selected_width,
                options=selected_options,
                assistant_label=assistant_label,
            )
            _append_message_records(records, rendered)
            attached_artifacts.update(_message_artifact_keys(message))

            if isinstance(message, SystemMessage) and message.kind == "autonomous":
                autonomous_label = _autonomous_label(message, selected_options)
            elif isinstance(message, AssistantMessage) and autonomous_label:
                autonomous_label = ""
            elif isinstance(message, UserMessage):
                autonomous_label = ""

        if selected_options.include_artifacts:
            orphan_artifacts = [
                artifact
                for artifact in state.artifacts
                if _artifact_key(artifact) not in attached_artifacts
            ]
            if orphan_artifacts:
                _append_message_records(
                    records,
                    tuple(
                        TranscriptLine(
                            format_artifact_line(
                                artifact,
                                width=selected_width,
                                base_url=selected_options.base_url,
                            ),
                            "artifact",
                        )
                        for artifact in orphan_artifacts
                    ),
                )

        for error in state.errors:
            _append_message_records(
                records,
                tuple(
                    TranscriptLine(line, "error")
                    for line in _plain_block(
                        f"Error: {error.content or error.code}",
                        width=selected_width,
                    )
                ),
            )
        for diagnostic in state.diagnostics:
            text = diagnostic.message or diagnostic.source_type or "Unknown event"
            _append_message_records(
                records,
                tuple(
                    TranscriptLine(line, "diagnostic")
                    for line in _plain_block(f"Diagnostic: {text}", width=selected_width)
                ),
            )

        stale = set(self._message_cache) - seen_message_ids
        for message_id in stale:
            self._message_cache.pop(message_id, None)

        bounded = _trim_trailing_blanks(_bounded_records(records, selected_width))
        return TranscriptRenderResult(
            lines=tuple(bounded),
            text="\n".join(line.text for line in bounded),
        )

    def _render_cached_message(
        self,
        message: TranscriptMessage,
        *,
        width: int,
        options: TranscriptRenderOptions,
        assistant_label: str = "",
    ) -> tuple[TranscriptLine, ...]:
        signature = _message_signature(message, options, assistant_label)
        cached = self._message_cache.get(message.id)
        if cached and cached[0] == signature:
            return cached[1]

        rendered = tuple(
            render_message_lines(
                message,
                width=width,
                options=options,
                assistant_label=assistant_label,
            )
        )
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


def render_transcript_lines(
    state: CLIUIState,
    *,
    width: int | None = None,
    options: TranscriptRenderOptions | None = None,
) -> tuple[TranscriptLine, ...]:
    """Render reducer state into styled transcript lines."""

    return TranscriptRenderer(options=options).render_result(state, width=width).lines


def render_message(
    message: TranscriptMessage,
    *,
    width: int | None = None,
    options: TranscriptRenderOptions | None = None,
) -> list[str]:
    """Render one transcript message into bounded terminal text lines."""

    return [
        line.text
        for line in render_message_lines(message, width=width, options=options)
    ]


def render_message_lines(
    message: TranscriptMessage,
    *,
    width: int | None = None,
    options: TranscriptRenderOptions | None = None,
    assistant_label: str = "",
) -> list[TranscriptLine]:
    """Render one transcript message into styled bounded terminal lines."""

    selected_width = coerce_width(width, default=DEFAULT_TRANSCRIPT_WIDTH)
    selected_options = options or TranscriptRenderOptions()
    if isinstance(message, UserMessage):
        return _user_lines(message, selected_width, selected_options)
    if isinstance(message, AssistantMessage):
        return _assistant_lines(
            message,
            selected_width,
            selected_options,
            assistant_label=assistant_label,
        )
    if isinstance(message, SystemMessage):
        return _system_lines(message, selected_width, selected_options)
    return []


def _user_lines(
    message: UserMessage,
    width: int,
    options: TranscriptRenderOptions,
) -> list[TranscriptLine]:
    records = [
        TranscriptLine(
            format_turn_separator(
                _header_text("You", message.timestamp),
                width=width,
                ascii_only=options.ascii_only,
            ),
            "user_header",
        )
    ]
    body_width = max(1, width - len(INDENT))
    for line in wrap_plain_text(message.content, width=body_width):
        records.append(_indented_record(line, "user_text", width))
    if message.attachments:
        label = "attachment" if len(message.attachments) == 1 else "attachments"
        records.append(
            _indented_record(
                f"{len(message.attachments)} {label}",
                "artifact",
                width,
            )
        )
    if message.context_summary and options.verbose:
        records.append(
            _indented_record(
                f"context: {message.context_summary}",
                "system",
                width,
            )
        )
    return records


def _assistant_lines(
    message: AssistantMessage,
    width: int,
    options: TranscriptRenderOptions,
    *,
    assistant_label: str = "",
) -> list[TranscriptLine]:
    label = _assistant_header_label(message, options, assistant_label)
    header_kind: TranscriptLineKind = (
        "autonomous_header" if assistant_label else "assistant_header"
    )
    records = [
        TranscriptLine(
            format_turn_separator(
                _header_text(label, message.timestamp),
                width=width,
                ascii_only=options.ascii_only,
            ),
            header_kind,
        )
    ]
    records.append(_assistant_divider_record(width, options))
    dispatch_text = _dispatch_reference_text(message)
    if dispatch_text:
        records.append(_indented_record(dispatch_text, "assistant_header", width))
    last_tool_index = _last_tool_index(message.steps)
    previous_block: Literal["thinking", "preamble", "tool", "final"] | None = None

    for index, step in enumerate(message.steps):
        if isinstance(step, ThinkingStep):
            block_kind: Literal["thinking", "preamble", "tool", "final"] = "thinking"
            step_records = _thinking_lines(step, width, options)
        elif isinstance(step, ToolCallStep):
            block_kind = "tool"
            step_records = _tool_lines(step, width, options)
        elif isinstance(step, ResponseStep):
            block_kind = "preamble" if index < last_tool_index else "final"
            step_records = _response_lines(
                step,
                width,
                options,
                kind=block_kind,
            )
        else:
            continue

        if not step_records:
            continue
        _append_assistant_block(
            records,
            step_records,
            block_kind=block_kind,
            previous_block=previous_block,
            width=width,
            options=options,
        )
        previous_block = block_kind

    if message.tool_reload_info is not None:
        tools = ", ".join(message.tool_reload_info.tools)
        text = f"Tools reloaded: {tools}" if tools else "Tools reloaded."
        records.extend(_indented_plain_block(text, kind="system", width=width))
    if message.status == "complete" and _has_assistant_body(records):
        _append_assistant_end_divider(records, width, options)
    return records


def _assistant_header_label(
    message: AssistantMessage,
    options: TranscriptRenderOptions,
    assistant_label: str,
) -> str:
    label = assistant_label or "Nymeria"
    activity = " ".join(str(options.assistant_activity_label or "").split())
    if assistant_label or message.status != "streaming" or not activity:
        return label
    separator = " - " if options.ascii_only else " \u00b7 "
    return f"{label}{separator}{activity}"


def _dispatch_reference_text(message: AssistantMessage) -> str:
    content = str(message.dispatch_info.get("content") or "").strip()
    if content:
        return content
    title = str(message.dispatch_info.get("title") or "").strip()
    thread_id = str(message.dispatch_info.get("thread_id") or "").strip()
    if title:
        return f"Response from {title}"
    if thread_id:
        return f"Response from {thread_id}"
    return ""


def _thinking_lines(
    step: ThinkingStep,
    width: int,
    options: TranscriptRenderOptions,
) -> list[TranscriptLine]:
    if not step.content:
        return [_thinking_record("...", width, options)]

    if not options.verbose:
        preview = collapse_inline(step.content)
        return [_thinking_record(preview, width, options)]

    prefix_width = cell_len(_thinking_marker(options))
    body_width = max(1, width - len(INDENT) - prefix_width)
    return [
        _thinking_record(line, width, options)
        for line in render_markdown_lines(
            step.content,
            width=body_width,
            ascii_only=options.ascii_only,
        )
    ]


def _thinking_record(
    text: str,
    width: int,
    options: TranscriptRenderOptions,
) -> TranscriptLine:
    return _indented_record(f"{_thinking_marker(options)}{text}", "thinking", width)


def _thinking_marker(options: TranscriptRenderOptions) -> str:
    return THINKING_MARKER_ASCII if options.ascii_only else THINKING_MARKER_UNICODE


def _tool_lines(
    step: ToolCallStep,
    width: int,
    options: TranscriptRenderOptions,
) -> list[TranscriptLine]:
    body_width = max(1, width - len(INDENT))
    tool_row_options = replace(
        options.tool_row_options,
        ascii_only=options.ascii_only,
    )
    row = format_tool_row(
        step,
        width=body_width,
        options=tool_row_options,
    )
    records = [_indented_record(row, "tool", width)]
    if not options.verbose:
        return records

    if step.arguments:
        records.extend(
            _verbose_payload_lines(
                "args",
                step.arguments,
                width=width,
                limit=VERBOSE_ARGS_LIMIT,
            )
        )
    if step.result not in (None, ""):
        records.extend(
            _verbose_payload_lines(
                "result",
                step.result,
                width=width,
                limit=VERBOSE_RESULT_LIMIT,
            )
        )
    if options.include_artifacts:
        records.extend(
            TranscriptLine(
                truncate_cell_width(
                    f"{INDENT}{format_artifact_line(item, width=body_width, base_url=options.base_url)}",
                    width,
                ),
                "artifact",
            )
            for item in step.artifacts
        )
    return records


def _response_lines(
    step: ResponseStep,
    width: int,
    options: TranscriptRenderOptions,
    *,
    kind: Literal["preamble", "final"],
) -> list[TranscriptLine]:
    content = step.content.strip()
    if not content:
        return []
    body_width = max(1, width - len(INDENT))
    rendered = render_markdown_lines(
        content,
        width=body_width,
        ascii_only=options.ascii_only,
    )
    return [_indented_record(line, kind, width) for line in rendered]


def _append_assistant_block(
    records: list[TranscriptLine],
    incoming: list[TranscriptLine],
    *,
    block_kind: Literal["thinking", "preamble", "tool", "final"],
    previous_block: Literal["thinking", "preamble", "tool", "final"] | None,
    width: int,
    options: TranscriptRenderOptions,
) -> None:
    if previous_block is not None and previous_block != block_kind:
        if records and records[-1].kind != "blank":
            records.append(TranscriptLine("", "blank"))
        if _needs_assistant_divider(
            block_kind,
            previous_block=previous_block,
        ):
            records.append(_assistant_divider_record(width, options))
            records.append(TranscriptLine("", "blank"))
    records.extend(incoming)


def _needs_assistant_divider(
    block_kind: Literal["thinking", "preamble", "tool", "final"],
    *,
    previous_block: Literal["thinking", "preamble", "tool", "final"],
) -> bool:
    return previous_block == "tool" and block_kind != "tool"


def _assistant_divider_record(
    width: int,
    options: TranscriptRenderOptions,
) -> TranscriptLine:
    body_width = max(1, width - len(INDENT))
    divider_width = min(body_width, 32)
    glyph = "." if options.ascii_only else "\u00b7"
    return TranscriptLine(
        truncate_cell_width(f"{INDENT}{glyph * divider_width}", width),
        "assistant_divider",
    )


def _append_assistant_end_divider(
    records: list[TranscriptLine],
    width: int,
    options: TranscriptRenderOptions,
) -> None:
    if records and records[-1].kind != "blank":
        records.append(TranscriptLine("", "blank"))
    if records and records[-1].kind == "blank":
        records.append(_assistant_divider_record(width, options))


def _has_assistant_body(records: list[TranscriptLine]) -> bool:
    return any(
        record.kind not in {
            "assistant_header",
            "autonomous_header",
            "blank",
            "assistant_divider",
        }
        for record in records
    )


def _system_lines(
    message: SystemMessage,
    width: int,
    options: TranscriptRenderOptions,
) -> list[TranscriptLine]:
    if message.kind == "autonomous":
        records = [
            TranscriptLine(
                format_turn_separator(
                    _header_text(_autonomous_label(message, options), message.timestamp),
                    width=width,
                    ascii_only=options.ascii_only,
                ),
                "autonomous_header",
            )
        ]
        if message.content:
            records.extend(
                _indented_plain_block(message.content, kind="system", width=width)
            )
        return records

    text = _system_text(message)
    kind: TranscriptLineKind = "error" if message.kind == "error" else "system"
    records = [
        TranscriptLine(
            format_turn_separator(
                _header_text("System", message.timestamp),
                width=width,
                ascii_only=options.ascii_only,
            ),
            kind,
        )
    ]
    records.extend(_indented_plain_block(text, kind=kind, width=width))
    return records


def _system_text(message: SystemMessage) -> str:
    if message.kind == "compaction_notice":
        text = "Context compacted."
        if message.context_summary:
            text = f"{text} {message.context_summary}"
        if message.messages_removed:
            text = f"{text} Removed {message.messages_removed} messages."
        return text
    if message.kind == "iteration_limit":
        return message.content or "Reached turn safety limit."
    if message.kind == "error":
        return f"Error: {message.content}"
    if message.kind == "tool_reload":
        tools = message.details.get("tools") if message.details else None
        if tools:
            return f"Tools reloaded: {', '.join(str(tool) for tool in tools)}"
        return "Tools reloaded."
    if message.kind == "context_attached":
        return message.content or message.context_summary or "Context attached."
    return message.content or message.kind.replace("_", " ").title()


def _verbose_payload_lines(
    label: str,
    value: Any,
    *,
    width: int,
    limit: int,
) -> list[TranscriptLine]:
    body_width = max(1, width - len(INDENT) * 2)
    text = _bounded_text(_json_payload(value), limit)
    first_prefix = f"{label}: "
    records: list[TranscriptLine] = []
    for index, line in enumerate(text.splitlines() or [""]):
        prefix = first_prefix if index == 0 else " " * len(first_prefix)
        for wrapped in wrap_plain_text(f"{prefix}{line}", width=body_width):
            records.append(
                TranscriptLine(
                    truncate_cell_width(f"{INDENT * 2}{wrapped}", width),
                    "tool_detail",
                )
            )
    if len(records) > VERBOSE_PAYLOAD_LINE_LIMIT:
        records = records[: VERBOSE_PAYLOAD_LINE_LIMIT - 1]
        records.append(
            TranscriptLine(
                truncate_cell_width(f"{INDENT * 2}... truncated ...", width),
                "tool_detail",
            )
        )
    return records


def _indented_plain_block(
    text: str,
    *,
    kind: TranscriptLineKind,
    width: int,
) -> list[TranscriptLine]:
    body_width = max(1, width - len(INDENT))
    return [
        _indented_record(line, kind, width)
        for line in wrap_plain_text(text, width=body_width)
    ]


def _indented_record(
    text: str,
    kind: TranscriptLineKind,
    width: int,
) -> TranscriptLine:
    if not text:
        return TranscriptLine("", "blank")
    return TranscriptLine(truncate_cell_width(f"{INDENT}{text}", width), kind)


def _plain_block(text: str, *, width: int) -> list[str]:
    return [truncate_cell_width(line, width) for line in wrap_plain_text(text, width=width)]


def _append_message_records(
    records: list[TranscriptLine],
    incoming: tuple[TranscriptLine, ...],
) -> None:
    if not incoming:
        return
    if records and records[-1].kind != "blank":
        records.append(TranscriptLine("", "blank"))
    records.extend(incoming)


def _bounded_records(
    records: list[TranscriptLine],
    width: int,
) -> list[TranscriptLine]:
    return [replace(record, text=truncate_cell_width(record.text, width)) for record in records]


def _trim_trailing_blanks(records: list[TranscriptLine]) -> list[TranscriptLine]:
    output = list(records)
    while output and output[-1].kind == "blank":
        output.pop()
    return output


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


def _last_tool_index(steps: tuple[MessageStep, ...]) -> int:
    for index in range(len(steps) - 1, -1, -1):
        if isinstance(steps[index], ToolCallStep):
            return index
    return -1


def _autonomous_label(
    message: SystemMessage,
    options: TranscriptRenderOptions,
) -> str:
    parts = ["Nymeria", "autonomous"]
    source = str(message.details.get("source") or "").strip()
    if source:
        parts.append(source)
    separator = " - " if options.ascii_only else " \u00b7 "
    return separator.join(parts)


def _header_text(label: str, timestamp: float) -> str:
    if timestamp >= EPOCH_TIMESTAMP_FLOOR:
        return f"{label}  {datetime.fromtimestamp(timestamp).strftime('%H:%M')}"
    return label


def format_turn_separator(
    label: str,
    *,
    width: int | None = None,
    ascii_only: bool = True,
) -> str:
    """Return a bounded horizontal rule with an embedded turn label."""

    selected_width = coerce_width(width, default=DEFAULT_TRANSCRIPT_WIDTH)
    rule = "-" if ascii_only else "\u2500"
    normalized_label = " ".join(str(label or "Message").split())
    max_label_width = max(1, selected_width - 7)
    bounded_label = truncate_cell_width(normalized_label, max_label_width)
    prefix = f"{rule * min(4, selected_width)} "
    separator = f"{prefix}{bounded_label} "
    remaining_width = selected_width - cell_len(separator)
    if remaining_width > 0:
        separator = f"{separator}{rule * remaining_width}"
    return truncate_cell_width(separator, selected_width)


def _message_signature(
    message: TranscriptMessage,
    options: TranscriptRenderOptions,
    assistant_label: str,
) -> object:
    if isinstance(message, UserMessage):
        return (
            "user",
            message.content,
            message.attachments,
            message.status,
            message.context_summary,
            options.verbose,
            options.ascii_only,
            assistant_label,
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
            options.ascii_only,
            assistant_label,
        )
    if isinstance(message, AssistantMessage):
        return (
            "assistant",
            message.status,
            message.activity_phase,
            _stable_repr(message.dispatch_info),
            tuple(_step_signature(step) for step in message.steps),
            _stable_repr(message.tool_reload_info),
            options,
            assistant_label,
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


def _json_payload(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True)
    except TypeError:
        return str(value)


def _bounded_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 24)].rstrip()}\n... truncated ..."


def _stable_repr(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True, default=repr)
    except TypeError:
        return repr(value)


def max_line_width(text: str) -> int:
    """Return the widest rendered line in terminal cells."""

    return max((cell_len(line) for line in text.splitlines()), default=0)


__all__ = [
    "TranscriptLine",
    "TranscriptLineKind",
    "TranscriptRenderOptions",
    "TranscriptRenderResult",
    "TranscriptRenderer",
    "format_turn_separator",
    "max_line_width",
    "render_message",
    "render_message_lines",
    "render_transcript",
    "render_transcript_lines",
]
