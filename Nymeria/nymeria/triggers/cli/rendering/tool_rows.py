"""Compact transcript rows for tool calls and artifacts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from rich.cells import cell_len

from ..state import ToolCallStep, WorkspaceArtifact
from ..theme import DEFAULT_TOOL_ICON
from .markdown import collapse_inline, coerce_width, truncate_cell_width

DEFAULT_ARGS_LIMIT = 80
DEFAULT_RESULT_LIMIT = 120

# C0 controls (minus the whitespace collapse_inline already folds), DEL, and
# the C1 CSI byte. A tool result carrying escape sequences must not be able
# to move the terminal cursor from inside a one-line row preview (that would
# also desync the live tool-row line accounting).
_TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x9b]")


@dataclass(frozen=True, slots=True)
class ToolRowRenderOptions:
    """Controls for compact tool row rendering."""

    show_duration: bool = True
    args_limit: int = DEFAULT_ARGS_LIMIT
    result_limit: int = DEFAULT_RESULT_LIMIT
    include_artifacts: bool = True
    ascii_only: bool = True
    icon: str = DEFAULT_TOOL_ICON


@dataclass(frozen=True, slots=True)
class ToolRowSegment:
    """One styled slice of a compact tool row.

    ``kind`` is one of: marker, name, args, status, duration, result,
    artifact. Segment texts carry their own separators, so joining the
    texts in order reproduces the plain row exactly.
    """

    kind: str
    text: str


def format_tool_row_segments(
    tool: ToolCallStep,
    *,
    width: int | None = None,
    options: ToolRowRenderOptions | None = None,
    now: float | None = None,
) -> tuple[ToolRowSegment, ...]:
    """Return one tool row as styleable segments.

    Desktop ToolCallCard header shape: ``icon name(args) duration -> result``.
    Success carries no status word (the marker + duration say it); error,
    cancelled, pending, and running keep theirs.

    The args/result split targets ``width`` but does not hard-guarantee it: the
    per-part floors can overshoot at very small widths. Callers render the
    joined text through ``truncate_cell_width`` / ``Text.truncate`` (see
    ``format_tool_row`` and ``render_tool_row``), which is the authoritative
    width clamp.
    """

    selected_width = coerce_width(width)
    selected_options = options or ToolRowRenderOptions()
    name = truncate_cell_width(tool.name or "tool", max(8, selected_width // 4))
    marker = _status_marker(tool, options=selected_options)

    args_preview = format_args_preview(
        tool.arguments,
        limit=selected_options.args_limit,
    )

    result_preview = format_result_preview(
        tool.result,
        limit=selected_options.result_limit,
    )

    status_label = _status_label(tool)
    duration = ""
    if selected_options.show_duration and tool.status != "running":
        # Prefer the server-measured execution time over the event-arrival diff.
        if tool.duration_ms is not None:
            duration = format_duration_from_ms(tool.duration_ms)
        else:
            duration = format_duration(tool.started_at, tool.ended_at)
    elif selected_options.show_duration and now is not None:
        # Live elapsed time for an in-flight row (the transcript ticker
        # passes ``now``); snapshot renders pass nothing and show no timer.
        duration = format_duration(tool.started_at, now)

    artifacts = ""
    if selected_options.include_artifacts:
        artifacts = format_artifacts_preview(tool.artifacts)
        if artifacts:
            artifacts = truncate_cell_width(artifacts, max(10, selected_width // 4))

    head = f"{marker} {name}"
    status_part = f" {status_label}" if status_label else ""
    duration_part = f" {duration}" if duration else ""
    artifact_part = f" {artifacts}" if artifacts else ""
    paren_overhead = 2 if args_preview else 0
    arrow_overhead = len(" -> ") if result_preview else 0

    fixed_width = (
        cell_len(head)
        + paren_overhead
        + cell_len(status_part)
        + cell_len(duration_part)
        + cell_len(artifact_part)
        + arrow_overhead
    )
    dynamic_width = max(8, selected_width - fixed_width)
    if result_preview and args_preview:
        args_need = cell_len(args_preview)
        result_need = cell_len(result_preview)
        if args_need + result_need > dynamic_width:
            # Split the row budget, letting the shorter side donate its slack.
            half = dynamic_width // 2
            if args_need <= half:
                args_width = args_need
            elif result_need <= half:
                args_width = dynamic_width - result_need
            else:
                args_width = half
            args_width = min(args_width, selected_options.args_limit)
            result_width = max(8, dynamic_width - args_width)
            args_preview = truncate_cell_width(args_preview, max(8, args_width))
            result_preview = truncate_cell_width(result_preview, result_width)
    elif result_preview:
        result_preview = truncate_cell_width(result_preview, dynamic_width)
    elif args_preview:
        args_preview = truncate_cell_width(args_preview, dynamic_width)

    segments = [
        ToolRowSegment("marker", marker),
        ToolRowSegment("name", f" {name}"),
    ]
    if args_preview:
        segments.append(ToolRowSegment("args", f"({args_preview})"))
    if status_part:
        segments.append(ToolRowSegment("status", status_part))
    if duration_part:
        segments.append(ToolRowSegment("duration", duration_part))
    if result_preview:
        segments.append(ToolRowSegment("result", f" -> {result_preview}"))
    if artifact_part:
        segments.append(ToolRowSegment("artifact", artifact_part))
    return tuple(segments)


def format_tool_row(
    tool: ToolCallStep,
    *,
    width: int | None = None,
    options: ToolRowRenderOptions | None = None,
    now: float | None = None,
) -> str:
    """Return a single bounded row summarizing one tool call."""

    selected_width = coerce_width(width)
    row = "".join(
        segment.text
        for segment in format_tool_row_segments(
            tool,
            width=width,
            options=options,
            now=now,
        )
    )
    return truncate_cell_width(row, selected_width)


def format_args_preview(
    args: dict[str, Any],
    *,
    limit: int = DEFAULT_ARGS_LIMIT,
) -> str:
    """Render tool arguments as a compact one-line preview."""

    if not args:
        return ""

    selected_limit = coerce_width(limit)
    parts: list[str] = []
    for key, value in args.items():
        rendered_value = truncate_cell_width(_stringify_argument(value), 40)
        parts.append(f"{key}={rendered_value}")
    return truncate_cell_width(", ".join(parts), selected_limit)


def format_result_preview(
    result: Any,
    *,
    limit: int = DEFAULT_RESULT_LIMIT,
) -> str:
    """Render a bounded one-line result preview."""

    if result in (None, ""):
        return ""
    return truncate_cell_width(_stringify(result), coerce_width(limit))


def workspace_download_url(base_url: str, path: str) -> str:
    """Build the authenticated /workspace/download URL for a workspace path."""

    return f"{base_url.rstrip('/')}/workspace/download?path={quote(path, safe='')}"


def format_artifact_line(
    artifact: WorkspaceArtifact,
    *,
    width: int | None = None,
    base_url: str = "",
) -> str:
    """Render one workspace artifact as a bounded transcript line.

    The CLI is a remote terminal that cannot read the backend filesystem, so for
    image artifacts (when an API base_url is known) the label is the clickable
    /workspace/download URL instead of the raw workspace path.
    """

    selected_width = coerce_width(width)
    label = artifact.path or artifact.name or "workspace artifact"
    if base_url and artifact.path and (artifact.mime_type or "").startswith("image/"):
        label = workspace_download_url(base_url, artifact.path)
    details: list[str] = []
    if artifact.size_bytes is not None:
        details.append(format_size(artifact.size_bytes))
    if artifact.mime_type:
        details.append(artifact.mime_type)
    suffix = f" ({', '.join(details)})" if details else ""
    return truncate_cell_width(f"  artifact: {label}{suffix}", selected_width)


def format_artifacts_preview(artifacts: tuple[WorkspaceArtifact, ...]) -> str:
    """Return a short artifact count/name preview for a tool row."""

    if not artifacts:
        return ""
    first = artifacts[0]
    label = first.name or first.path or "artifact"
    if len(artifacts) == 1:
        return f"[artifact: {collapse_inline(label)}]"
    return f"[artifacts: {collapse_inline(label)} +{len(artifacts) - 1}]"


def format_duration(started_at: float, ended_at: float | None) -> str:
    """Format a tool duration from reducer timestamps."""

    if ended_at is None or started_at <= 0 or ended_at < started_at:
        return ""
    seconds = ended_at - started_at
    if seconds < 1:
        return f"{round(seconds * 1000):.0f}ms"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{round(seconds):.0f}s"


def format_duration_from_ms(duration_ms: int) -> str:
    """Format a server-measured tool duration (tool_result.duration_ms)."""

    if duration_ms < 0:
        return ""
    if duration_ms < 1000:
        return f"{duration_ms}ms"
    seconds = duration_ms / 1000
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{round(seconds):.0f}s"


def format_size(size_bytes: int) -> str:
    """Format a byte count for compact artifact metadata."""

    size = float(max(0, size_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{size:.0f}B"
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _status_label(tool: ToolCallStep) -> str:
    # Success carries no word: the marker + duration already say it.
    labels = {
        "success": "",
        "pending": "pending",
        "running": "running",
        "error": "error",
        "cancelled": "cancelled",
    }
    return labels.get(tool.status, tool.status or "")


def _status_marker(tool: ToolCallStep, *, options: ToolRowRenderOptions) -> str:
    if options.ascii_only:
        markers = {
            "success": "-",
            "pending": "-",
            "running": "-",
            "error": "x",
            "cancelled": "!",
        }
        return markers.get(tool.status, "-")
    if tool.status == "cancelled":
        return "!"
    return options.icon


def _stringify_argument(value: Any) -> str:
    if isinstance(value, str):
        collapsed = collapse_inline(value)
        if re.fullmatch(r"[A-Za-z0-9_./:-]+", collapsed):
            return collapsed
        return json.dumps(collapsed, ensure_ascii=True)
    return _stringify(value)


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return _TERMINAL_CONTROL_RE.sub("", collapse_inline(value))
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    except TypeError:
        return _TERMINAL_CONTROL_RE.sub("", collapse_inline(value))


__all__ = [
    "ToolRowRenderOptions",
    "ToolRowSegment",
    "format_args_preview",
    "format_artifact_line",
    "format_artifacts_preview",
    "format_duration",
    "format_result_preview",
    "format_size",
    "format_tool_row",
    "format_tool_row_segments",
]
