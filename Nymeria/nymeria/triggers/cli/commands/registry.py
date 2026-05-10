"""Slash-command registry for the CLI/TUI runtime."""

from __future__ import annotations

import asyncio
import inspect
import shlex
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from .base import (
    Command,
    CommandCompletion,
    CommandContext,
    CommandInvocation,
    CommandMatch,
    CommandMessage,
    CommandPaletteEntry,
    CommandResult,
)

_PROTECTED_BUILTINS = {"help", "exit", "clear"}


class CommandParseError(ValueError):
    """Raised when slash-command input cannot be parsed."""


class RichConsoleCommandOutputSink:
    """Rich console adapter for structured command output."""

    def __init__(self, console: Any) -> None:
        self.console = console

    def emit(self, message: CommandMessage) -> None:
        style = {
            "success": "green",
            "warning": "yellow",
            "error": "red",
        }.get(message.level, "")
        content = message.content
        if style:
            self.console.print(f"[{style}]{content}[/{style}]")
        else:
            self.console.print(content)


class CommandRegistry:
    """Stores, resolves, and dispatches CLI slash commands."""

    def __init__(self, *, include_builtins: bool = True) -> None:
        self._commands: dict[str, Command] = {}
        self._aliases: dict[str, str] = {}
        if include_builtins:
            self.register_builtins()

    def register_builtins(self) -> None:
        """Register renderer-agnostic command shell basics."""

        self.register(
            Command(
                name="help",
                aliases=["/h"],
                description="Show help",
                usage="/help [query]",
                handler=_handle_help,
                handler_mode="context",
                builtin=True,
                category="System",
            )
        )
        self.register(
            Command(
                name="clear",
                description="Clear screen",
                usage="/clear",
                handler=_handle_clear,
                handler_mode="context",
                builtin=True,
                category="System",
            )
        )
        self.register(
            Command(
                name="exit",
                aliases=["/quit", "/q"],
                description="Exit the CLI",
                usage="/exit",
                handler=_handle_exit,
                handler_mode="context",
                builtin=True,
                category="System",
            )
        )

    def register(self, command: Command) -> None:
        """Register a command and its aliases."""

        normalized = _normalize_token(command.name)
        if not normalized:
            raise ValueError("Command name cannot be empty")
        command.name = normalized
        _normalize_subcommands(command)

        existing = self._commands.get(normalized)
        if (
            existing is not None
            and existing.builtin
            and not command.builtin
            and normalized in _PROTECTED_BUILTINS
        ):
            self._merge_aliases(existing, command.aliases)
            return

        if existing is not None:
            self._remove_aliases_for(existing.name)

        self._commands[normalized] = command
        for alias in command.aliases:
            self._aliases[_normalize_token(alias)] = normalized

    def get(self, name: str) -> Command | None:
        """Return a command by canonical name or alias."""

        lookup = _normalize_token(name)
        canonical = self._aliases.get(lookup, lookup)
        return self._commands.get(canonical)

    def parse(self, raw_input: str) -> CommandInvocation | None:
        """Parse raw input into a command invocation."""

        raw_input = str(raw_input or "").strip()
        if not raw_input.startswith("/"):
            return None
        try:
            parts = shlex.split(raw_input)
        except ValueError as exc:
            raise CommandParseError(str(exc)) from exc
        if not parts:
            return None
        return CommandInvocation(
            raw_input=raw_input,
            command_name=_normalize_token(parts[0]),
            args=tuple(parts[1:]),
        )

    def resolve(self, raw_input: str | CommandInvocation) -> CommandMatch | None:
        """Resolve a parsed or raw slash command to a registered handler."""

        invocation = (
            self.parse(raw_input) if isinstance(raw_input, str) else raw_input
        )
        if invocation is None:
            return None

        root = self.get(invocation.command_name)
        if root is None:
            return None

        command = root
        path = [root.name]
        remaining = list(invocation.args)
        while remaining and command.subcommands:
            subcommand = _find_subcommand(command, remaining[0])
            if subcommand is None:
                break
            command = subcommand
            path.append(command.name)
            remaining.pop(0)

        return CommandMatch(
            invocation=invocation,
            root=root,
            command=command,
            path=tuple(path),
            args=tuple(remaining),
        )

    async def dispatch_async(
        self,
        context: CommandContext,
        raw_input: str,
    ) -> CommandResult:
        """Parse, resolve, and dispatch a slash command asynchronously."""

        context.registry = self
        try:
            invocation = self.parse(raw_input)
        except CommandParseError as exc:
            return self._emit_result(
                context,
                CommandResult.failed(
                    f"Could not parse command: {exc}",
                    error_code="command_parse_error",
                ),
            )
        if invocation is None:
            return CommandResult.unhandled()

        match = self.resolve(invocation)
        if match is None:
            return self._emit_result(
                context,
                _unknown_command_result(invocation.command_name),
            )

        result = await self._execute(context, match)
        if not result.command_path and result.handled:
            result = replace(result, command_path=match.path)
        return self._emit_result(context, result)

    def dispatch(self, state: Any, raw_input: str) -> bool:
        """
        Legacy synchronous dispatch entry point.

        Returns True when the input was a slash command and the registry
        handled the command attempt, including structured unknown-command
        errors. Returns False for non-command input.
        """

        context = CommandContext(
            output=RichConsoleCommandOutputSink(state.console),
            thread_id=getattr(state, "thread_id", None),
            user_id=getattr(state, "user_id", "default"),
            legacy_state=state,
            registry=self,
        )
        result = self.dispatch_sync(context, raw_input)
        return result.handled

    def dispatch_sync(
        self,
        context: CommandContext,
        raw_input: str,
    ) -> CommandResult:
        """Synchronous wrapper around ``dispatch_async``."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.dispatch_async(context, raw_input))
        raise RuntimeError("dispatch_sync cannot run inside an active event loop")

    def get_all_commands(self) -> list[Command]:
        """Return all non-hidden root commands."""

        return [
            command
            for command in self._commands.values()
            if not command.hidden
        ]

    def get_completions(self) -> list[str]:
        """Return command strings for prompt_toolkit autocomplete."""

        return sorted({item.text for item in self.get_completion_items()})

    def get_completion_items(
        self,
        *,
        include_hidden: bool = True,
    ) -> list[CommandCompletion]:
        """Return completion candidates with descriptions."""

        items: list[CommandCompletion] = []
        for command in self._commands.values():
            if command.hidden and not include_hidden:
                continue
            items.extend(_completion_items_for_command(command))
        return sorted(items, key=lambda item: item.text)

    def get_palette_entries(
        self,
        *,
        include_hidden: bool = False,
    ) -> list[CommandPaletteEntry]:
        """Return command-palette entries for visible commands."""

        entries: list[CommandPaletteEntry] = []
        for command in self._commands.values():
            if command.hidden and not include_hidden:
                continue
            entries.append(_palette_entry(command, (command.name,)))
            for subcommand in command.subcommands.values():
                if subcommand.hidden and not include_hidden:
                    continue
                entries.append(
                    _palette_entry(
                        subcommand,
                        (command.name, subcommand.name),
                        parent=command.name,
                    )
                )
        return sorted(entries, key=lambda item: item.text)

    def _merge_aliases(self, command: Command, aliases: Iterable[str]) -> None:
        for alias in aliases:
            normalized = _normalize_token(alias)
            if not normalized:
                continue
            if alias not in command.aliases:
                command.aliases.append(alias)
            self._aliases[normalized] = command.name

    def _remove_aliases_for(self, command_name: str) -> None:
        stale = [
            alias
            for alias, target in self._aliases.items()
            if target == command_name
        ]
        for alias in stale:
            self._aliases.pop(alias, None)

    async def _execute(
        self,
        context: CommandContext,
        match: CommandMatch,
    ) -> CommandResult:
        command = match.command
        if command.handler_mode == "legacy":
            if context.legacy_state is None:
                return CommandResult.failed(
                    f"/{' '.join(match.path)} is not available in this renderer yet.",
                    command_path=match.path,
                    error_code="legacy_command_unavailable",
                )
            target = context.legacy_state
        else:
            target = context

        try:
            raw_result = command.handler(target, list(match.args))
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
        except Exception as exc:  # noqa: BLE001 - command errors surface in UI.
            return CommandResult.failed(
                f"Command failed: {exc}",
                command_path=match.path,
                error_code="command_exception",
                payload={"error_type": exc.__class__.__name__},
            )

        return _coerce_result(raw_result, command_path=match.path)

    def _emit_result(
        self,
        context: CommandContext,
        result: CommandResult,
    ) -> CommandResult:
        if context.output is None:
            return result
        for message in result.messages:
            context.output.emit(message)
        return result


def _handle_help(context: CommandContext, args: list[str]) -> CommandResult:
    registry = context.registry
    if registry is None:
        return CommandResult.completed("No command registry is attached.")

    query = " ".join(args).strip().casefold()
    entries = registry.get_palette_entries(include_hidden=False)
    if query:
        entries = [
            entry
            for entry in entries
            if query in entry.text.casefold()
            or query in entry.description.casefold()
            or query in entry.category.casefold()
        ]

    if not entries:
        message = f"No commands match '{' '.join(args)}'."
        return CommandResult.completed(CommandMessage(message, level="warning"))

    lines = ["Commands"]
    for entry in entries:
        label = entry.usage or entry.text
        description = entry.description
        if description:
            lines.append(f"  {label:<28} {description}")
        else:
            lines.append(f"  {label}")
    return CommandResult.completed(CommandMessage("\n".join(lines), title="Commands"))


def _handle_exit(context: CommandContext, _args: list[str]) -> CommandResult:
    if context.legacy_state is not None:
        context.legacy_state.running = False
    return CommandResult.exit("Goodbye!")


def _handle_clear(context: CommandContext, _args: list[str]) -> CommandResult:
    if context.legacy_state is not None:
        context.legacy_state.console.clear()
        try:
            from ..rendering.welcome import render_welcome

            render_welcome(context.legacy_state)
        except Exception:  # noqa: BLE001 - clear should still succeed.
            pass
        return CommandResult.clear()
    return CommandResult.clear("Cleared.")


def _unknown_command_result(command_name: str) -> CommandResult:
    display = f"/{command_name}" if command_name else "/"
    return CommandResult.failed(
        f"Unknown command: {display}\nType /help for available commands.",
        error_code="unknown_command",
        payload={"command": command_name},
    )


def _coerce_result(raw_result: Any, *, command_path: tuple[str, ...]) -> CommandResult:
    if isinstance(raw_result, CommandResult):
        if not raw_result.command_path and raw_result.handled:
            return replace(raw_result, command_path=command_path)
        return raw_result
    if isinstance(raw_result, CommandMessage):
        return CommandResult.completed(raw_result, command_path=command_path)
    if raw_result is None:
        return CommandResult.completed(command_path=command_path)
    return CommandResult.completed(str(raw_result), command_path=command_path)


def _normalize_token(value: str) -> str:
    return str(value or "").strip().lstrip("/").casefold()


def _normalize_subcommands(command: Command) -> None:
    normalized: dict[str, Command] = {}
    for name, subcommand in command.subcommands.items():
        canonical = _normalize_token(subcommand.name or name)
        subcommand.name = canonical
        normalized[canonical] = subcommand
    command.subcommands = normalized


def _find_subcommand(command: Command, token: str) -> Command | None:
    lookup = _normalize_token(token)
    direct = command.subcommands.get(lookup)
    if direct is not None:
        return direct
    for subcommand in command.subcommands.values():
        aliases = {_normalize_token(alias) for alias in subcommand.aliases}
        if lookup in aliases:
            return subcommand
    return None


def _completion_items_for_command(command: Command) -> list[CommandCompletion]:
    items = [
        CommandCompletion(
            text=f"/{command.name}",
            description=command.description,
            command_path=(command.name,),
            hidden=command.hidden,
        )
    ]
    for alias in command.aliases:
        items.append(
            CommandCompletion(
                text=_display_alias(alias),
                description=command.description,
                command_path=(command.name,),
                hidden=command.hidden,
                alias=True,
            )
        )

    for subcommand in command.subcommands.values():
        items.append(
            CommandCompletion(
                text=f"/{command.name} {subcommand.name}",
                description=subcommand.description,
                command_path=(command.name, subcommand.name),
                hidden=subcommand.hidden,
            )
        )
        for alias in subcommand.aliases:
            items.append(
                CommandCompletion(
                    text=f"/{command.name} {_normalize_token(alias)}",
                    description=subcommand.description,
                    command_path=(command.name, subcommand.name),
                    hidden=subcommand.hidden,
                    alias=True,
                )
            )
    return items


def _palette_entry(
    command: Command,
    path: tuple[str, ...],
    *,
    parent: str | None = None,
) -> CommandPaletteEntry:
    text = f"/{' '.join(path)}"
    usage = command.usage
    if parent and usage and not usage.startswith(f"/{parent} "):
        usage = f"/{parent} {usage.lstrip('/')}"
    return CommandPaletteEntry(
        text=text,
        description=command.description,
        usage=usage or text,
        category=command.category,
        command_path=path,
    )


def _display_alias(alias: str) -> str:
    normalized = str(alias or "").strip()
    if not normalized:
        return ""
    return normalized if normalized.startswith("/") else f"/{normalized}"


__all__ = [
    "CommandParseError",
    "CommandRegistry",
    "RichConsoleCommandOutputSink",
]
