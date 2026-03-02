"""Context and compaction commands: /context, /compact."""

from __future__ import annotations

import asyncio
from typing import List, TYPE_CHECKING

from rich.panel import Panel

from . import Command, CommandRegistry

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


def register(registry: CommandRegistry) -> None:
    """Register context commands."""
    registry.register(Command(
        name="context",
        aliases=[],
        description="Show context window stats",
        handler=_handle_context,
    ))
    registry.register(Command(
        name="compact",
        aliases=[],
        description="Compact conversation",
        handler=_handle_compact,
    ))
