"""Memory commands: /memory list, /memory search, /memory save, /memory forget."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, List, TYPE_CHECKING

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from .system import (
    CommandClientMethodUnavailable,
    call_client_method,
    compact_id,
    mapping_get,
    one_line,
    unsupported_transport_result,
)
from ..rendering.tables import render_memory_table

if TYPE_CHECKING:
    from ..state import CLIState


def _handle_memory(state: "CLIState", args: List[str]) -> None:
    """Default: show usage hint."""
    state.console.print(
        "[dim]Usage: /memory list | search <query> | save <key> <value> | forget <key>[/dim]"
    )


def _handle_memory_list(state: "CLIState", args: List[str]) -> None:
    """List all user memories."""
    profile = state.profile_manager.get_profile(state.user_id)
    render_memory_table(state.console, profile.memories)


def _handle_memory_save(state: "CLIState", args: List[str]) -> None:
    """Save a memory: /memory save <key> <value>."""
    if len(args) < 2:
        state.console.print("[red]Usage: /memory save <key> <value>[/red]")
        return

    key = args[0]
    value = " ".join(args[1:])

    profile = state.profile_manager.get_profile(state.user_id)
    from ....core.memory_limits import (
        get_global_memory_char_limit,
        get_memory_max_entries,
        get_memory_value_max_chars,
        memory_entries_full_error,
        validate_profile_memory_write,
    )

    max_entries = get_memory_max_entries(state.settings)
    value_cap = get_memory_value_max_chars(state.settings)
    limit_error = validate_profile_memory_write(
        profile,
        key=key,
        value=value,
        limit=get_global_memory_char_limit(state.settings),
        max_entries=max_entries,
        max_value_chars=value_cap,
    )
    if limit_error:
        state.console.print(f"[red]{limit_error}[/red]")
        return
    if not profile.add_memory(
        key, value, max_entries=max_entries, max_value_chars=value_cap
    ):
        state.console.print(f"[red]{memory_entries_full_error(max_entries)}[/red]")
        return
    state.profile_manager.save_profile(profile)
    state.console.print(f"[green]Saved memory: {key}[/green]")


def _handle_memory_forget(state: "CLIState", args: List[str]) -> None:
    """Remove a memory by key."""
    if not args:
        state.console.print("[red]Usage: /memory forget <key>[/red]")
        return

    key = args[0]
    profile = state.profile_manager.get_profile(state.user_id)
    if profile.remove_memory(key):
        state.profile_manager.save_profile(profile)
        state.console.print(f"[green]Forgot: {key}[/green]")
    else:
        state.console.print(f"[red]No memory with key '{key}'.[/red]")


async def _handle_memory_root_context(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    if context.legacy_state is not None:
        _handle_memory(context.legacy_state, args)
        return CommandResult.completed()
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
    if context.legacy_state is not None:
        _handle_memory_list(context.legacy_state, [])
        return CommandResult.completed()

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
    if context.legacy_state is not None:
        return CommandResult.failed(
            "/memory search is only available in the new command layer.",
            error_code="legacy_command_unavailable",
        )
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
    if context.legacy_state is not None:
        _handle_memory_save(context.legacy_state, args)
        return CommandResult.completed()
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
    if context.legacy_state is not None:
        _handle_memory_forget(context.legacy_state, args)
        return CommandResult.completed()
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
        handler_mode="context",
        category="Personal",
        subcommands={
            "list": Command(
                name="list",
                aliases=["profile"],
                description="List memories",
                usage="list",
                handler=_handle_memory_list_context,
                handler_mode="context",
                category="Personal",
            ),
            "search": Command(
                name="search",
                aliases=["find"],
                description="Search memories",
                usage="search <query>",
                handler=_handle_memory_search_context,
                handler_mode="context",
                category="Personal",
            ),
            "save": Command(
                name="save",
                aliases=["add"],
                description="Save memory",
                usage="save <key> <value>",
                handler=_handle_memory_save_context,
                handler_mode="context",
                category="Personal",
            ),
            "forget": Command(
                name="forget",
                aliases=["delete", "remove"],
                description="Forget memory",
                usage="forget <key>",
                handler=_handle_memory_forget_context,
                handler_mode="context",
                category="Personal",
            ),
        },
    ))
