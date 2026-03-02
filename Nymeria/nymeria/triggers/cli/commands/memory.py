"""Memory commands: /memory list, /memory save, /memory forget."""

from __future__ import annotations

from typing import List, TYPE_CHECKING

from . import Command, CommandRegistry
from ..rendering.tables import render_memory_table

if TYPE_CHECKING:
    from ..state import CLIState


def _handle_memory(state: "CLIState", args: List[str]) -> None:
    """Default: show usage hint."""
    state.console.print("[dim]Usage: /memory list | save <key> <value> | forget <key>[/dim]")


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
    profile.add_memory(key, value)
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


def register(registry: CommandRegistry) -> None:
    """Register memory commands."""
    registry.register(Command(
        name="memory",
        aliases=[],
        description="Manage memories",
        handler=_handle_memory,
        subcommands={
            "list": Command(name="list", aliases=[], description="List memories", handler=_handle_memory_list),
            "save": Command(name="save", aliases=[], description="Save memory", handler=_handle_memory_save),
            "forget": Command(name="forget", aliases=[], description="Forget memory", handler=_handle_memory_forget),
        },
    ))
