"""Activity and notification commands."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from .system import (
    CommandClientMethodUnavailable,
    call_client_method,
    compact_id,
    mapping_get,
    one_line,
    unsupported_transport_result,
)


async def _handle_activity_root(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if args:
        return CommandResult.failed(
            "Usage: /activity recent [limit] [--type <type>] [--thread current|<id>]",
            error_code="usage_error",
        )
    return await _handle_activity_recent(context, [])


async def _handle_activity_recent(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    parsed = _parse_activity_args(args, context.thread_id)
    if isinstance(parsed, CommandResult):
        return parsed

    try:
        data = await call_client_method(
            context,
            "get_activity",
            context.user_id,
            limit=parsed["limit"],
            activity_type=parsed["activity_type"],
            thread_id=parsed["thread_id"],
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/activity recent", method_name=exc.method_name)

    entries = _activity_entries(data)
    if not entries:
        return CommandResult.completed(
            CommandMessage("No recent activity found.", level="warning")
        )
    return CommandResult.completed(
        CommandMessage("\n".join(_format_activity(entries)), title="Activity"),
        payload={"count": len(entries)},
    )


async def _handle_notifications(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    try:
        data = await call_client_method(context, "get_notifications", context.user_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/activity notifications", method_name=exc.method_name)

    notifications = _sequence_of_mappings(mapping_get(data, "notifications", []))
    unread = mapping_get(data, "unread_count", 0)
    if not notifications:
        return CommandResult.completed(
            CommandMessage("No notifications.", level="warning"),
            payload={"unread_count": unread},
        )

    lines = [f"Notifications ({unread} unread)", "  ID        Read  Summary"]
    for item in notifications:
        read = "yes" if item.get("read") else "no"
        lines.append(
            f"  {compact_id(item.get('id')):<8}  {read:<4}  "
            f"{one_line(item.get('summary'), limit=80)}"
        )
    return CommandResult.completed(
        CommandMessage("\n".join(lines), title="Notifications"),
        payload={"unread_count": unread},
    )


def _parse_activity_args(
    args: Sequence[str],
    current_thread_id: str | None,
) -> dict[str, Any] | CommandResult:
    parsed: dict[str, Any] = {"limit": 20, "activity_type": None, "thread_id": None}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg.isdigit():
            parsed["limit"] = max(1, int(arg))
        elif arg.startswith("--limit="):
            parsed["limit"] = _positive_int(arg.split("=", 1)[1], default=20)
        elif arg == "--type" and index + 1 < len(args):
            parsed["activity_type"] = args[index + 1]
            index += 1
        elif arg.startswith("--type="):
            parsed["activity_type"] = arg.split("=", 1)[1]
        elif arg == "--thread" and index + 1 < len(args):
            parsed["thread_id"] = (
                current_thread_id
                if args[index + 1].casefold() in {"current", "."}
                else args[index + 1]
            )
            index += 1
        elif arg in {"--current-thread", "--thread-current"}:
            parsed["thread_id"] = current_thread_id
        else:
            return CommandResult.failed(
                f"Unknown option: {arg}",
                error_code="usage_error",
            )
        index += 1
    return parsed


def _activity_entries(value: Any) -> list[Mapping[str, Any]]:
    return _sequence_of_mappings(mapping_get(value, "entries", value))


def _sequence_of_mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _format_activity(entries: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["Recent Activity", "  Time                  Type             Thread    Message"]
    for entry in entries:
        lines.append(
            f"  {one_line(entry.get('timestamp'), limit=20):<20}  "
            f"{compact_id(entry.get('type'), width=15):<15}  "
            f"{compact_id(entry.get('thread_id') or '', width=8):<8}  "
            f"{one_line(entry.get('message'), limit=80)}"
        )
    return lines


def _positive_int(value: str, *, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def register(registry: CommandRegistry) -> None:
    """Register activity commands."""
    registry.register(Command(
        name="activity",
        aliases=[],
        description="Inspect recent activity",
        usage="/activity recent",
        handler=_handle_activity_root,
        handler_mode="context",
        category="Personal",
        subcommands={
            "recent": Command(
                name="recent",
                aliases=["list"],
                description="Show recent activity",
                usage="recent [limit]",
                handler=_handle_activity_recent,
                handler_mode="context",
                category="Personal",
            ),
            "notifications": Command(
                name="notifications",
                aliases=["notice"],
                description="Show notifications",
                usage="notifications",
                handler=_handle_notifications,
                handler_mode="context",
                category="Personal",
            ),
        },
    ))
