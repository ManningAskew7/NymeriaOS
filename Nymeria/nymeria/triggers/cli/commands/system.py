"""System commands: /history, /redraw, /verbose.

The cross-cutting command toolkit (transport shim, confirmation, formatting,
and the scalar parser) lives in ``commands/_shared.py``. (/help, /cls, and
/exit are registered as renderer-agnostic builtins in
``registry.register_builtins``. /settings is a backend command since the
config-group migration; it forwards like any other backend proxy.)
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from ._shared import (
    CommandClientMethodUnavailable,
    call_client_method,
    one_line,
    unsupported_transport_result,
)


async def _handle_history_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    """Show conversation history through the active client."""

    thread_id = context.thread_id
    if not thread_id:
        return CommandResult.failed("No active thread is selected.")

    include_internal = False
    limit = 20
    remaining: list[str] = []
    for arg in args:
        normalized = arg.casefold()
        if normalized in {"--internal", "--include-internal"}:
            include_internal = True
        elif normalized.startswith("--limit="):
            limit = _parse_limit(normalized.split("=", 1)[1], default=limit)
        else:
            remaining.append(arg)
    if remaining:
        limit = _parse_limit(remaining[0], default=limit)

    try:
        history = await call_client_method(
            context,
            "get_history",
            thread_id,
            user_id=context.user_id,
            include_internal=include_internal,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/history", method_name=exc.method_name)

    messages = _history_messages(history)
    if not messages:
        return CommandResult.completed(CommandMessage("No conversation history.", level="warning"))

    selected = messages[-limit:] if limit > 0 else messages
    lines = [f"History ({len(selected)} of {len(messages)} messages)"]
    for message in selected:
        lines.extend(_format_history_message(message))
    return CommandResult.completed(CommandMessage("\n".join(lines), title="History"))


def _parse_limit(value: str, *, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def _history_messages(history: Any) -> list[Mapping[str, Any]]:
    if isinstance(history, Mapping):
        raw = history.get("messages", [])
    else:
        raw = history
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _format_history_message(message: Mapping[str, Any]) -> list[str]:
    role = str(message.get("role") or message.get("type") or "unknown")
    label = {
        "human": "You",
        "user": "You",
        "assistant": "Nymeria",
        "ai": "Nymeria",
        "system": "System",
        "tool": "Tool",
    }.get(role.casefold(), role.title())
    content = _message_content_text(message.get("content"))
    lines = [f"{label}: {one_line(content, limit=200)}" if content else f"{label}:"]

    tool_calls = message.get("toolCalls") or message.get("tool_calls") or []
    if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, (str, bytes)):
        for tool_call in tool_calls:
            if not isinstance(tool_call, Mapping):
                continue
            name = tool_call.get("name") or tool_call.get("function", {}).get("name")
            if name:
                lines.append(f"  > {name}")
    return lines


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            elif block is not None:
                parts.append(str(block))
        return " ".join(parts)
    return "" if content is None else str(content)


async def _handle_redraw_context(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    await context.dispatch({"type": "redraw"})
    return CommandResult.completed("Redrawn.")


async def _handle_verbose_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) > 1 or (args and args[0] not in {"on", "off", "status"}):
        return CommandResult.failed(
            "Usage: /verbose on|off|status",
            error_code="usage_error",
        )

    enabled = bool(context.metadata.get("transcript_verbose", False))
    mode = args[0] if args else "status"
    if mode == "status":
        return CommandResult.completed(
            f"Transcript verbosity is {'on' if enabled else 'off'}.",
            payload={"suppress_transcript": True},
        )

    next_enabled = mode == "on"
    await context.dispatch(
        {"type": "set_transcript_verbose", "enabled": next_enabled}
    )
    return CommandResult.completed(
        f"Transcript verbosity is {'on' if next_enabled else 'off'}.",
        payload={"suppress_transcript": True},
    )


def register(registry: CommandRegistry) -> None:
    """Register system commands.

    /help, /cls, and /exit are renderer-agnostic builtins registered by
    ``CommandRegistry.register_builtins``; system.py owns /history, /redraw,
    and /verbose.
    """
    registry.register(Command(
        name="history",
        aliases=[],
        description="Show conversation history",
        usage="/history [--internal] [limit]",
        handler=_handle_history_context,
        category="System",
    ))
    registry.register(Command(
        name="redraw",
        aliases=[],
        description="Redraw the CLI",
        usage="/redraw",
        handler=_handle_redraw_context,
        category="System",
    ))
    registry.register(Command(
        name="verbose",
        aliases=[],
        description="Toggle full-screen transcript verbosity",
        usage="/verbose on|off|status",
        handler=_handle_verbose_context,
        category="System",
    ))
