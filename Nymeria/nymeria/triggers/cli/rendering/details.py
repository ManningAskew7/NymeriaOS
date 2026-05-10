"""Details rendering for hidden CLI transcript data."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any, TypeVar

from ..state import (
    AssistantMessage,
    CLIUIState,
    ThinkingStep,
    ToolCallStep,
    WorkspaceArtifact,
)
from .markdown import collapse_inline
from .tool_rows import format_size

DEFAULT_DETAILS_LIMIT = 4000
T = TypeVar("T")


def render_details(
    state: CLIUIState | None,
    *,
    target: str = "recent",
    ref: str = "",
    limit: int = DEFAULT_DETAILS_LIMIT,
) -> str:
    """Render bounded details for recent hidden transcript data."""

    if state is None:
        return "No live transcript state is available."

    normalized = (target or "recent").casefold()
    if normalized in {"recent", "last"}:
        item = _last_detail_item(state)
    elif normalized in {"tool", "tools", "tool_call"}:
        item = _find_tool(state, ref)
    elif normalized in {"thinking", "thought"}:
        item = _find_thinking(state, ref)
    elif normalized in {"artifact", "artifacts"}:
        item = _find_artifact(state, ref)
    elif normalized in {"error", "errors"}:
        item = _find_error(state, ref)
    else:
        return "Usage: /details [tool|thinking|artifact|error] [id-or-index] [--full]"

    if item is None:
        return f"No {normalized} details found."

    heading, body = item
    bounded = _bounded(body, limit=limit)
    return f"{heading}\n{bounded}" if bounded else heading


def collect_recent_artifacts(
    state: CLIUIState | None,
    *,
    limit: int = 10,
) -> list[WorkspaceArtifact]:
    """Return recent unique artifacts from reducer state."""

    if state is None:
        return []

    seen: set[str] = set()
    artifacts: list[WorkspaceArtifact] = []
    for artifact in reversed(list(state.artifacts)):
        key = artifact.path or artifact.name
        if not key or key in seen:
            continue
        seen.add(key)
        artifacts.append(artifact)
    for message in reversed(state.messages):
        if not isinstance(message, AssistantMessage):
            continue
        for step in reversed(message.steps):
            if not isinstance(step, ToolCallStep):
                continue
            for artifact in reversed(step.artifacts):
                key = artifact.path or artifact.name
                if not key or key in seen:
                    continue
                seen.add(key)
                artifacts.append(artifact)
    artifacts.reverse()
    return artifacts[-limit:] if limit > 0 else artifacts


def format_artifact_details(artifact: WorkspaceArtifact) -> str:
    """Render one artifact's metadata."""

    rows = [
        ("Name", artifact.name),
        ("Path", artifact.path),
        ("MIME", artifact.mime_type),
        (
            "Size",
            format_size(artifact.size_bytes) if artifact.size_bytes is not None else "",
        ),
    ]
    payload = _json_payload(artifact.payload)
    if payload and payload != "{}":
        rows.append(("Payload", payload))
    return "\n".join(_aligned_rows(rows, title="Artifact Details"))


def _last_detail_item(state: CLIUIState) -> tuple[str, str] | None:
    if state.errors:
        error = state.errors[-1]
        return ("Error Details", _json_payload(error))

    artifacts = collect_recent_artifacts(state, limit=1)
    if artifacts:
        return ("Artifact Details", format_artifact_details(artifacts[-1]))

    for message in reversed(state.messages):
        if not isinstance(message, AssistantMessage):
            continue
        for step in reversed(message.steps):
            if isinstance(step, ToolCallStep):
                return (_tool_heading(step), _tool_body(step))
            if isinstance(step, ThinkingStep):
                return ("Thinking Details", step.content or "(empty)")
    return None


def _find_tool(state: CLIUIState, ref: str) -> tuple[str, str] | None:
    tools = list(_iter_tools(state))
    if not tools:
        return None
    tool = _select_by_ref(tools, ref, lambda item: item.id or item.name)
    return (_tool_heading(tool), _tool_body(tool)) if tool else None


def _find_thinking(state: CLIUIState, ref: str) -> tuple[str, str] | None:
    thinking = list(_iter_thinking(state))
    if not thinking:
        return None
    step = _select_by_ref(
        thinking,
        ref,
        lambda item: collapse_inline(item.content)[:40],
    )
    return ("Thinking Details", step.content or "(empty)") if step else None


def _find_artifact(state: CLIUIState, ref: str) -> tuple[str, str] | None:
    artifacts = collect_recent_artifacts(state, limit=100)
    artifact = _select_by_ref(artifacts, ref, lambda item: item.path or item.name)
    return ("Artifact Details", format_artifact_details(artifact)) if artifact else None


def _find_error(state: CLIUIState, ref: str) -> tuple[str, str] | None:
    errors = list(state.errors)
    error = _select_by_ref(errors, ref, lambda item: item.code or item.content)
    return ("Error Details", _json_payload(error)) if error else None


def _iter_tools(state: CLIUIState) -> Iterable[ToolCallStep]:
    for message in state.messages:
        if isinstance(message, AssistantMessage):
            for step in message.steps:
                if isinstance(step, ToolCallStep):
                    yield step


def _iter_thinking(state: CLIUIState) -> Iterable[ThinkingStep]:
    for message in state.messages:
        if isinstance(message, AssistantMessage):
            for step in message.steps:
                if isinstance(step, ThinkingStep):
                    yield step


def _select_by_ref(
    items: Sequence[T],
    ref: str,
    key_fn,
) -> T | None:
    if not items:
        return None
    if not ref:
        return items[-1]
    try:
        index = int(ref)
    except ValueError:
        index = None
    if index is not None:
        if index == 0:
            return None
        selected = index - 1 if index > 0 else len(items) + index
        if 0 <= selected < len(items):
            return items[selected]
        return None
    lowered = ref.casefold()
    for item in reversed(items):
        key = str(key_fn(item) or "")
        if key.casefold().startswith(lowered) or lowered in key.casefold():
            return item
    return None


def _tool_heading(tool: ToolCallStep) -> str:
    name = tool.name or "tool"
    suffix = f" ({tool.status})" if tool.status else ""
    return f"Tool Details: {name}{suffix}"


def _tool_body(tool: ToolCallStep) -> str:
    rows = [
        ("ID", tool.id),
        ("Name", tool.name),
        ("Status", tool.status),
        ("Arguments", _json_payload(tool.arguments)),
        ("Result", _json_payload(tool.result)),
    ]
    if tool.artifacts:
        rows.append(
            (
                "Artifacts",
                ", ".join(artifact.path or artifact.name for artifact in tool.artifacts),
            )
        )
    return "\n".join(_aligned_rows(rows))


def _json_payload(value: Any) -> str:
    if is_dataclass(value):
        value = asdict(value)
    try:
        return json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True)
    except TypeError:
        return str(value)


def _aligned_rows(
    rows: Sequence[tuple[str, Any]],
    *,
    title: str | None = None,
) -> list[str]:
    width = max((len(label) for label, _value in rows), default=0)
    lines = [title] if title else []
    for label, value in rows:
        text = "" if value is None else str(value)
        lines.append(f"  {label:<{width}}  {text}")
    return lines


def _bounded(value: str, *, limit: int) -> str:
    if limit <= 0 or len(value) <= limit:
        return value
    return f"{value[: max(0, limit - 24)].rstrip()}\n... truncated ..."


__all__ = [
    "collect_recent_artifacts",
    "format_artifact_details",
    "render_details",
]
