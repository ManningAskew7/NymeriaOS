"""Workspace artifact commands (client-side half).

The server-state listing (``/artifacts recent``: recent artifacts from thread
history) is a backend command (``core.registry_defaults`` +
``core.command_executor_context.ContextCommandsMixin``), so it works on every
frontend. The two genuinely client-side operations stay here: ``open`` shows an
artifact's path and metadata resolved against the terminal's own recent list,
and ``download`` writes bytes to the client's local disk via the CLI transport
method ``download_workspace_artifact`` (which the command-service backend
clients do not expose). The local root forwards to the backend so a bare
``/artifacts`` still lists recent when the backend catalog is registered.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ._shared import (
    CommandClientMethodUnavailable,
    call_client_method,
    call_client_user_scoped,
    unsupported_transport_result,
)
from ..rendering.details import collect_recent_artifacts, format_artifact_details
from ..state import CLIUIState, WorkspaceArtifact


async def _handle_artifacts_root(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    """Forward /artifacts to the backend registry (recent listing).

    The backend ``/artifacts`` root and ``/artifacts recent`` win over this
    local root when the backend catalog is registered, so this handler is only
    reached on a disconnected transport, where it degrades to the backend
    proxy's unsupported-transport message. The client-side ``open`` and
    ``download`` subcommands are merged under the backend root and keep running
    locally.
    """
    from .backend import _execute_backend_command

    return await _execute_backend_command(context, ("artifacts",), args)


async def _handle_artifacts_open(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    artifact = await _resolve_artifact_arg(context, args, command="/artifacts open")
    if isinstance(artifact, CommandResult):
        return artifact
    return CommandResult.completed(
        CommandMessage(format_artifact_details(artifact), title="Artifact"),
        payload={"path": artifact.path, "name": artifact.name},
    )


async def _handle_artifacts_download(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    artifact = await _resolve_artifact_arg(context, args, command="/artifacts download")
    if isinstance(artifact, CommandResult):
        return artifact

    try:
        downloaded = await call_client_user_scoped(
            context,
            "download_workspace_artifact",
            artifact.path,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result(
            "/artifacts download",
            method_name=exc.method_name,
        )

    if not downloaded:
        return CommandResult.failed(
            f"Could not download artifact: {artifact.path}",
            error_code="artifact_download_failed",
        )

    content, filename, content_type = downloaded
    size = len(content) if isinstance(content, (bytes, bytearray)) else 0
    return CommandResult.completed(
        CommandMessage(
            f"Downloaded artifact: {filename} ({_format_size(size)}, {content_type})",
            level="success",
        ),
        payload={
            "path": artifact.path,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": size,
        },
    )


async def _resolve_artifact_arg(
    context: CommandContext,
    args: Sequence[str],
    *,
    command: str,
) -> WorkspaceArtifact | CommandResult:
    if not args:
        return CommandResult.failed(f"Usage: {command} <index-or-path>", error_code="usage_error")

    ref = " ".join(args).strip()
    artifacts = _recent_artifacts_from_context(context, limit=100)
    if not artifacts and context.thread_id:
        artifacts = await _artifacts_from_history(context, limit=100)

    selected = _select_artifact(artifacts, ref)
    if selected is not None:
        return selected

    if ref.startswith("/"):
        return WorkspaceArtifact(path=ref, name=Path(ref).name)
    return CommandResult.failed(f"No artifact matching '{ref}'.")


def _recent_artifacts_from_context(
    context: CommandContext,
    *,
    limit: int,
) -> list[WorkspaceArtifact]:
    state = context.metadata.get("ui_state")
    return collect_recent_artifacts(
        state if isinstance(state, CLIUIState) else None,
        limit=limit,
    )


async def _artifacts_from_history(
    context: CommandContext,
    *,
    limit: int,
) -> list[WorkspaceArtifact]:
    if not context.thread_id:
        return []
    try:
        history = await call_client_method(
            context,
            "get_history",
            context.thread_id,
            user_id=context.user_id,
            include_internal=True,
        )
    except (CommandClientMethodUnavailable, TypeError):
        return []
    messages = history.get("messages", []) if isinstance(history, Mapping) else history
    artifacts: list[WorkspaceArtifact] = []
    seen: set[str] = set()
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        for artifact in _artifact_mappings(message):
            path = str(artifact.get("path") or "")
            if not path or path in seen:
                continue
            seen.add(path)
            artifacts.append(_workspace_artifact(artifact))
    return artifacts[-limit:] if limit > 0 else artifacts


def _artifact_mappings(message: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    direct = message.get("artifacts")
    if isinstance(direct, Sequence) and not isinstance(direct, (str, bytes)):
        found.extend(item for item in direct if isinstance(item, Mapping))
    tool_calls = message.get("toolCalls") or message.get("tool_calls") or []
    if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, (str, bytes)):
        for tool_call in tool_calls:
            if not isinstance(tool_call, Mapping):
                continue
            artifacts = tool_call.get("artifacts") or []
            if isinstance(artifacts, Sequence) and not isinstance(artifacts, (str, bytes)):
                found.extend(item for item in artifacts if isinstance(item, Mapping))
    return found


def _workspace_artifact(raw: Mapping[str, Any]) -> WorkspaceArtifact:
    path = str(raw.get("path") or "")
    name = str(raw.get("name") or Path(path).name)
    size = raw.get("size_bytes", raw.get("sizeBytes"))
    return WorkspaceArtifact(
        path=path,
        name=name,
        mime_type=str(raw.get("mime_type") or raw.get("mimeType") or ""),
        size_bytes=size if isinstance(size, int) else None,
        payload=dict(raw),
    )


def _select_artifact(
    artifacts: Sequence[WorkspaceArtifact],
    ref: str,
) -> WorkspaceArtifact | None:
    try:
        index = int(ref)
    except ValueError:
        index = None
    if index is not None:
        selected = index - 1 if index > 0 else len(artifacts) + index
        if 0 <= selected < len(artifacts):
            return artifacts[selected]
        return None

    lowered = ref.casefold()
    for artifact in reversed(artifacts):
        key = artifact.path or artifact.name
        if key.casefold().startswith(lowered) or lowered in key.casefold():
            return artifact
    return None


def _format_size(size: int) -> str:
    from ..rendering.tool_rows import format_size

    return format_size(size)


def register(registry: CommandRegistry) -> None:
    """Register the client-side workspace artifact commands."""
    registry.register(Command(
        name="artifacts",
        aliases=[],
        description="Inspect recent workspace artifacts",
        usage="/artifacts recent",
        handler=_handle_artifacts_root,
        category="Personal",
        subcommands={
            "open": Command(
                name="open",
                description="Show artifact path and metadata",
                usage="open <index-or-path>",
                handler=_handle_artifacts_open,
                category="Personal",
            ),
            "download": Command(
                name="download",
                aliases=["get"],
                description="Download a workspace artifact through the API",
                usage="download <index-or-path>",
                handler=_handle_artifacts_download,
                category="Personal",
            ),
        },
    ))
