"""Context and compaction commands: /context, /compact."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import List, TYPE_CHECKING

from rich.panel import Panel

from . import Command, CommandContext, CommandMessage, CommandRegistry, CommandResult
from .system import (
    call_client_method,
    confirmation_granted,
    confirmation_required_result,
    mapping_get,
    strip_confirmation_flags,
    unsupported_transport_result,
    CommandClientMethodUnavailable,
)

if TYPE_CHECKING:
    from ..state import CLIState


def _handle_context(state: "CLIState", args: List[str]) -> None:
    """Show context window usage statistics."""
    stats = state.agent.get_context_stats(state.thread_id)

    usage_pct = stats.get("usage_percentage", 0)
    if usage_pct >= 80:
        pct_style = "red"
    elif usage_pct >= 50:
        pct_style = "yellow"
    else:
        pct_style = "green"

    body = (
        f"  Thread          {stats['thread_id']}\n"
        f"  Total tokens    {stats['total_tokens']:,} / {stats['context_limit']:,} "
        f"[{pct_style}]({usage_pct}%)[/{pct_style}]\n"
        f"    Input         {stats['input_tokens']:,}\n"
        f"    Output        {stats['output_tokens']:,}\n"
        f"  Context mgmt    {stats['context_management']}\n"
        f"  Compactions     {stats['compaction_count']}\n"
        f"  Last compacted  {stats['last_compaction'] or 'Never'}"
    )

    state.console.print(Panel(body, title="Context", border_style="dim", padding=(0, 1)))


def _handle_compact(state: "CLIState", args: List[str]) -> None:
    """Trigger manual compaction with spinner."""
    with state.console.status("[dim]Compacting...[/dim]", spinner="dots"):
        try:
            result = asyncio.run(
                state.agent.compact_now(state.thread_id, user_id=state.user_id)
            )
        except Exception as e:
            state.console.print(f"[red]Error: {e}[/red]")
            return

    if result.get("success"):
        before = result.get("messages_before", "?")
        after = result.get("messages_after", "?")
        removed = result.get("messages_removed", 0)
        state.console.print(
            f"[green]Compacted! Removed {removed} messages ({before} -> {after}).[/green]"
        )
    else:
        reason = result.get("reason", "Unknown reason")
        state.console.print(f"[yellow]Skipped: {reason}[/yellow]")


async def _handle_context_command(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    """Show context stats through the active client."""

    if context.legacy_state is not None:
        _handle_context(context.legacy_state, args)
        return CommandResult.completed()
    if args:
        return CommandResult.failed("Usage: /context", error_code="usage_error")
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    try:
        stats = await call_client_method(
            context,
            "get_context_stats",
            context.thread_id,
            user_id=context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/context", method_name=exc.method_name)

    if not isinstance(stats, Mapping):
        return CommandResult.failed("Context response was not a mapping.")
    return CommandResult.completed(
        CommandMessage(_format_context_stats(stats), title="Context")
    )


async def _handle_compact_command(
    context: CommandContext,
    args: list[str],
) -> CommandResult:
    """Trigger manual compaction through the active client."""

    if context.legacy_state is not None:
        _handle_compact(context.legacy_state, args)
        return CommandResult.completed()
    if not context.thread_id:
        return CommandResult.failed("No active thread is selected.")

    args, explicit_confirmation = strip_confirmation_flags(args)
    if args:
        return CommandResult.failed("Usage: /compact [--yes]", error_code="usage_error")

    confirmed = await confirmation_granted(
        context,
        f"Compact thread {context.thread_id}?",
        explicitly_confirmed=explicit_confirmation,
    )
    if not confirmed:
        return confirmation_required_result("/compact")

    try:
        result = await call_client_method(
            context,
            "compact",
            context.thread_id,
            context.user_id,
        )
    except CommandClientMethodUnavailable as exc:
        return unsupported_transport_result("/compact", method_name=exc.method_name)

    return _format_compaction_result(result)


def _format_context_stats(stats: Mapping[str, object]) -> str:
    usage_pct = stats.get("usage_percentage", "")
    rows = [
        ("Thread", stats.get("thread_id", "")),
        ("Total tokens", _token_ratio(stats)),
        ("Input", stats.get("input_tokens", "")),
        ("Output", stats.get("output_tokens", "")),
        ("Context mgmt", stats.get("context_management", "")),
        ("Compactions", stats.get("compaction_count", "")),
        ("Last compacted", stats.get("last_compaction") or "Never"),
    ]
    if usage_pct != "":
        rows.insert(2, ("Usage", f"{usage_pct}%"))
    width = max((len(label) for label, _value in rows), default=0)
    lines = ["Context"]
    for label, value in rows:
        lines.append(f"  {label:<{width}}  {value}")
    return "\n".join(lines)


def _token_ratio(stats: Mapping[str, object]) -> str:
    total = stats.get("total_tokens", stats.get("used_tokens", ""))
    limit = stats.get("context_limit", stats.get("max_tokens", ""))
    if total == "" and limit == "":
        return ""
    if isinstance(total, int):
        total_text = f"{total:,}"
    else:
        total_text = str(total)
    if limit == "":
        return total_text
    limit_text = f"{limit:,}" if isinstance(limit, int) else str(limit)
    return f"{total_text} / {limit_text}"


def _format_compaction_result(result: object) -> CommandResult:
    if isinstance(result, Mapping) and result.get("success") is False:
        reason = result.get("reason", "Unknown reason")
        return CommandResult.completed(CommandMessage(f"Skipped: {reason}", level="warning"))
    removed = mapping_get(result, "messages_removed", None)
    before = mapping_get(result, "messages_before", None)
    after = mapping_get(result, "messages_after", None)
    if removed is None:
        return CommandResult.completed(
            CommandMessage("Compaction requested.", level="success")
        )
    suffix = f"Removed {removed} messages"
    if before is not None and after is not None:
        suffix += f" ({before} -> {after})"
    return CommandResult.completed(
        CommandMessage(f"Compacted. {suffix}.", level="success")
    )


def register(registry: CommandRegistry) -> None:
    """Register context commands."""
    registry.register(Command(
        name="context",
        aliases=[],
        description="Show context window stats",
        usage="/context",
        handler=_handle_context_command,
        handler_mode="context",
        category="Context",
    ))
    registry.register(Command(
        name="compact",
        aliases=[],
        description="Compact conversation",
        usage="/compact [--yes]",
        handler=_handle_compact_command,
        handler_mode="context",
        category="Context",
    ))
