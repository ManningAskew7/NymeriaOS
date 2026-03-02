"""Command registry for the CLI."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..state import CLIState


@dataclass
class Command:
    """A registered CLI command."""

    name: str
    description: str
    handler: Callable[["CLIState", List[str]], None]
    aliases: List[str] = field(default_factory=list)
    usage: str = ""
    subcommands: Dict[str, "Command"] = field(default_factory=dict)
    hidden: bool = False


class CommandRegistry:
    """Stores and dispatches slash commands."""

    def __init__(self) -> None:
        self._commands: Dict[str, Command] = {}
        self._aliases: Dict[str, str] = {}  # alias -> canonical name

    def register(self, command: Command) -> None:
        self._commands[command.name] = command
        for alias in command.aliases:
            # Normalize: strip leading / so both "/t" and "t" resolve
            self._aliases[alias.lstrip("/")] = command.name

    def get(self, name: str) -> Optional[Command]:
        # Strip leading / so "/threads" resolves to "threads"
        lookup = name.lstrip("/")
        canonical = self._aliases.get(lookup, lookup)
        return self._commands.get(canonical)

    def dispatch(self, state: "CLIState", raw_input: str) -> bool:
        """
        Parse and dispatch a /command.

        Returns True if a command was handled, False if not recognized.
        """
        raw_input = raw_input.strip()
        if not raw_input.startswith("/"):
            return False

        try:
            parts = shlex.split(raw_input)
        except ValueError:
            parts = raw_input.split()

        cmd_name = parts[0]  # e.g., "/threads"
        args = parts[1:]

        command = self.get(cmd_name)
        if command is None:
            state.console.print(f"[red]Unknown command: {cmd_name}[/red]")
            state.console.print("[dim]Type /help for available commands.[/dim]")
            return True

        # Check for subcommand
        if args and command.subcommands:
            sub_name = args[0]
            sub = command.subcommands.get(sub_name)
            if sub:
                sub.handler(state, args[1:])
                return True

        # Call the main handler with remaining args
        command.handler(state, args)
        return True

    def get_all_commands(self) -> List[Command]:
        """Return all non-hidden commands."""
        return [c for c in self._commands.values() if not c.hidden]

    def get_completions(self) -> List[str]:
        """Return all command strings for autocomplete."""
        items: List[str] = []
        for cmd in self._commands.values():
            items.append(f"/{cmd.name}")
            for alias in cmd.aliases:
                items.append(alias)
            for sub_name in cmd.subcommands:
                items.append(f"/{cmd.name} {sub_name}")
        return sorted(set(items))
