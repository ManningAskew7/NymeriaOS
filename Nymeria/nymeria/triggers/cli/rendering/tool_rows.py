"""Compact transcript rows for tool calls and artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from rich.cells import cell_len

from ..state import ToolCallStep, WorkspaceArtifact
from .markdown import collapse_inline, coerce_width, truncate_cell_width

DEFAULT_ARGS_LIMIT = 80
DEFAULT_RESULT_LIMIT = 120


@dataclass(frozen=True, slots=True)
class ToolRowRenderOptions:
    """Controls for compact tool row rendering."""

    show_duration: bool = False
    args_limit: int = DEFAULT_ARGS_LIMIT
    result_limit: int = DEFAULT_RESULT_LIMIT
    include_artifacts: bool = True


def format_tool_row(
    tool: ToolCallStep,
    *,
    width: int | None = None,
    options: ToolRowRenderOptions | None = None,
) -> str:
    """Return a single bounded row summarizing one tool call."""

    selected_width = coerce_width(width)
    selected_options = options or ToolRowRenderOptions()
    name = truncate_cell_width(tool.name or "tool", max(8, selected_width // 4))

    args_preview = format_args_preview(
        tool.arguments,
        limit=selected_options.args_limit,
    )

    result_preview = format_result_preview(
        tool.result,
        limit=selected_options.result_limit,
    )

    status_label = _status_label(tool)
    suffixes: list[str] = []
    if status_label:
        suffixes.append(status_label)

    if selected_options.show_duration and tool.status != "running":
        duration = format_duration(tool.started_at, tool.ended_at)
        if duration:
            suffixes.append(duration)

    if selected_options.include_artifacts:
        artifacts = format_artifacts_preview(tool.artifacts)
        if artifacts:
            suffixes.append(
                truncate_cell_width(artifacts, max(10, selected_width // 4))
            )

    prefix = f"> {name}"
    suffix = " ".join(suffixes)
    suffix_width = cell_len(suffix) + (1 if suffix else 0)
    if result_preview:
        fixed_width = cell_len(prefix) + len(" -> ") + suffix_width
        dynamic_width = max(8, selected_width - fixed_width)
        if args_preview:
            args_width = min(
                selected_options.args_limit,
                max(8, min(cell_len(args_preview), dynamic_width // 2)),
            )
            result_width = max(8, dynamic_width - args_width - 1)
            args_preview = truncate_cell_width(args_preview, args_width)
        else:
            result_width = dynamic_width
        result_preview = truncate_cell_width(result_preview, result_width)
        row = f"{prefix}"
        if args_preview:
            row = f"{row} {args_preview}"
        row = f"{row} -> {result_preview}"
    else:
        fixed_width = cell_len(prefix) + suffix_width + (1 if args_preview else 0)
        args_width = max(1, selected_width - fixed_width)
        row = prefix
        if args_preview:
            row = f"{row} {truncate_cell_width(args_preview, args_width)}"

    if suffix:
        row = f"{row} {suffix}"
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
    if len(args) == 1:
        value = next(iter(args.values()))
        return truncate_cell_width(_stringify(value), selected_limit)

    parts: list[str] = []
    for key, value in args.items():
        rendered_value = truncate_cell_width(_stringify(value), 40)
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


def format_artifact_line(
    artifact: WorkspaceArtifact,
    *,
    width: int | None = None,
) -> str:
    """Render one workspace artifact as a bounded transcript line."""

    selected_width = coerce_width(width)
    label = artifact.path or artifact.name or "workspace artifact"
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
    if tool.status in {"success", "pending"}:
        return ""
    return f"({tool.status})"


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return collapse_inline(value)
    try:
        return json.dumps(value, ensure_ascii=True, sort_keys=True)
    except TypeError:
        return collapse_inline(value)


__all__ = [
    "ToolRowRenderOptions",
    "format_args_preview",
    "format_artifact_line",
    "format_artifacts_preview",
    "format_duration",
    "format_result_preview",
    "format_size",
    "format_tool_row",
]
