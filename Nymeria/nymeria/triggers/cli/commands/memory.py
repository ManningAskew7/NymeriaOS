"""Memory commands: /memory list, /memory search, /memory save, /memory forget."""

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


async def _handle_memory_root_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if args:
        return CommandResult.failed(
            "Usage: /memory list|search|save|forget",
            error_code="usage_error",
        )
    return await _handle_memory_list_context(context, [])


async def _handle_memory_list_context(
    context: CommandContext,
    _args: list[str],
) -> CommandResult:
    try:
        memories = await call_client_method(context, "list_memories", context.user_id)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/memory list", method_name=exc.method_name)

    entries = _memory_entries(memories)
    if not entries:
        return CommandResult.completed(CommandMessage("No memories saved.", level="warning"))
    return CommandResult.completed(
        CommandMessage("\n".join(_format_memories(entries, title="Memories")), title="Memories")
    )


async def _handle_memory_search_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    query = " ".join(args).strip()
    if not query:
        return CommandResult.failed("Usage: /memory search <query>", error_code="usage_error")

    try:
        memories = await call_client_method(
            context,
            "search_memories",
            context.user_id,
            query,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/memory search", method_name=exc.method_name)

    entries = _memory_entries(memories)
    if not entries:
        return CommandResult.completed(
            CommandMessage(f"No memories matched '{query}'.", level="warning")
        )
    return CommandResult.completed(
        CommandMessage(
            "\n".join(_format_memories(entries, title=f"Memory Search: {query}")),
            title="Memories",
        )
    )


async def _handle_memory_save_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if len(args) < 2:
        return CommandResult.failed(
            "Usage: /memory save <key> <value>",
            error_code="usage_error",
        )

    key = args[0]
    value = " ".join(args[1:]).strip()
    try:
        await call_client_method(context, "save_memory", context.user_id, key, value)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/memory save", method_name=exc.method_name)

    return CommandResult.completed(
        CommandMessage(f"Saved memory: {key}", level="success"),
        payload={"key": key},
    )


async def _handle_memory_forget_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if not args:
        return CommandResult.failed("Usage: /memory forget <key>", error_code="usage_error")

    key = args[0]
    try:
        await call_client_method(context, "forget_memory", context.user_id, key)
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/memory forget", method_name=exc.method_name)

    return CommandResult.completed(
        CommandMessage(f"Forgot: {key}", level="success"),
        payload={"key": key},
    )


def _memory_entries(value: Any) -> list[Mapping[str, Any]]:
    raw = mapping_get(value, "memories", value)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def _format_memories(
    memories: Sequence[Mapping[str, Any]],
    *,
    title: str,
) -> list[str]:
    lines = [title, "  Key                         Value                             Uses"]
    for memory in memories:
        key = str(memory.get("key") or memory.get("id") or "")
        value = str(memory.get("value") or memory.get("content") or "")
        uses = memory.get("access_count", memory.get("uses", ""))
        score = memory.get("score")
        suffix = f" score={score:.2f}" if isinstance(score, (int, float)) else ""
        lines.append(
            f"  {compact_id(key, width=27):<27} "
            f"{one_line(value, limit=32):<32} {uses}{suffix}"
        )
    return lines


def register(registry: CommandRegistry) -> None:
    """Register memory commands."""
    registry.register(Command(
        name="memory",
        aliases=[],
        description="Manage memories",
        usage="/memory list",
        handler=_handle_memory_root_context,
        category="Personal",
        subcommands={
            "list": Command(
                name="list",
                aliases=["profile"],
                description="List memories",
                usage="list",
                handler=_handle_memory_list_context,
                category="Personal",
            ),
            "search": Command(
                name="search",
                aliases=["find"],
                description="Search memories",
                usage="search <query>",
                handler=_handle_memory_search_context,
                category="Personal",
            ),
            "save": Command(
                name="save",
                aliases=["add"],
                description="Save memory",
                usage="save <key> <value>",
                handler=_handle_memory_save_context,
                category="Personal",
            ),
            "forget": Command(
                name="forget",
                aliases=["delete", "remove"],
                description="Forget memory",
                usage="forget <key>",
                handler=_handle_memory_forget_context,
                category="Personal",
            ),
        },
    ))
