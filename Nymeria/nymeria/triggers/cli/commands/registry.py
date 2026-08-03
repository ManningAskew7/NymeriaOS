"""Slash-command registry for the CLI/TUI runtime."""

from __future__ import annotations

import inspect
import shlex
import textwrap
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

_PROTECTED_BUILTINS = {"help", "exit", "cls"}
_HELP_CATEGORY_ORDER = {
    "System": 0,
    "Threads": 10,
    "Model": 20,
    "Context": 30,
    "Tools": 40,
    "Skills": 50,
    "MCP": 60,
    "Personal": 70,
    "Automation": 75,
    "Conversation": 80,
    "Session": 90,
    "Other": 100,
}


class CommandParseError(ValueError):
    """Raised when slash-command input cannot be parsed."""


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
                builtin=True,
                category="System",
            )
        )
        self.register(
            Command(
                name="cls",
                description="Clear screen",
                usage="/cls",
                handler=_handle_clear,
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
                builtin=True,
                category="System",
            )
        )

    def register(self, command: Command) -> None:
        """Register a command and its aliases.

        Resolution rules when a command with the same name already exists:

        1. **Builtins win.** `_PROTECTED_BUILTINS` (help/cls/exit) cannot be
           replaced by non-builtin registrations; only their aliases merge in.
        2. **Backend wins over local.** When one side has
           ``metadata["backend_command"] = True`` and the other does not, the
           backend command takes the root (handler/description/aliases) and
           the local side's subcommands are merged in for any name the backend
           hasn't already claimed. This is order-independent.
        3. **Otherwise, replace.** Last registration wins for same-tier
           collisions (two locals or two backend entries).
        """

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
            existing_is_backend = bool(existing.metadata.get("backend_command"))
            incoming_is_backend = bool(command.metadata.get("backend_command"))
            if existing_is_backend and not incoming_is_backend:
                # Backend root already registered; merge in any local
                # subcommands the backend has not already claimed.
                for sub_name, sub in command.subcommands.items():
                    existing.subcommands.setdefault(sub_name, sub)
                return
            if incoming_is_backend and not existing_is_backend:
                # Backend overrides local root, but local subcommands survive
                # under it for any name the backend has not claimed.
                for sub_name, sub in existing.subcommands.items():
                    command.subcommands.setdefault(sub_name, sub)
                self._remove_aliases_for(existing.name)
                self._commands[normalized] = command
                for alias in command.aliases:
                    self._aliases[_normalize_token(alias)] = normalized
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

        args, json_requested = _strip_json_flag(invocation.args)
        if json_requested:
            invocation = replace(invocation, args=args)
            context = _with_json_output(context)

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
        """Return command-palette entries for visible commands.

        Walks the whole tree, not just roots and their children: a depth-3
        command (``/account tokens issue``) is a real spelling the palette
        must teach, and advertising only its parent is how the palette came to
        promise ``/account tokens`` while dispatching something else.
        """

        entries: list[CommandPaletteEntry] = []

        def walk(command: Command, path: tuple[str, ...]) -> None:
            if command.hidden and not include_hidden:
                return
            entries.append(
                _palette_entry(
                    command,
                    path,
                    parent=" ".join(path[:-1]) or None,
                )
            )
            for subcommand in command.subcommands.values():
                walk(subcommand, (*path, subcommand.name))

        for command in self._commands.values():
            walk(command, (command.name,))
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
        try:
            raw_result = command.handler(context, list(match.args))
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
        emit_result = getattr(context.output, "emit_result", None)
        if callable(emit_result):
            emit_result(result)
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
    entries = _dedupe_help_entries(entries)

    if not entries:
        message = f"No commands match '{' '.join(args)}'."
        return CommandResult.completed(CommandMessage(message, level="warning"))

    lines = _format_help_entries(
        entries,
        width=_help_output_width(context),
        query=" ".join(args).strip(),
    )
    return CommandResult.completed(CommandMessage("\n".join(lines), title="Commands"))


def _dedupe_help_entries(
    entries: Iterable[CommandPaletteEntry],
) -> list[CommandPaletteEntry]:
    entry_list = list(entries)
    deduped: dict[str, CommandPaletteEntry] = {}
    for entry in entry_list:
        if _is_default_subcommand_help_entry(entry, entry_list):
            continue
        label = _help_label(entry)
        if not label:
            continue
        key = " ".join(label.casefold().split())
        current = deduped.get(key)
        if current is None or _help_entry_rank(entry) > _help_entry_rank(current):
            deduped[key] = entry
    return sorted(deduped.values(), key=_help_sort_key)


def _is_default_subcommand_help_entry(
    entry: CommandPaletteEntry,
    entries: Iterable[CommandPaletteEntry],
) -> bool:
    if len(entry.command_path) != 1:
        return False
    root = entry.command_path[0]
    parts = _help_label(entry).lstrip("/").split()
    if len(parts) < 2 or parts[0].casefold() != root.casefold():
        return False
    default_subcommand = parts[1].casefold()
    return any(
        len(other.command_path) == 2
        and other.command_path[0].casefold() == root.casefold()
        and other.command_path[1].casefold() == default_subcommand
        for other in entries
    )


def _help_entry_rank(entry: CommandPaletteEntry) -> tuple[int, int, int]:
    return (
        len(entry.command_path),
        1 if entry.description else 0,
        len(entry.description or ""),
    )


def _help_sort_key(entry: CommandPaletteEntry) -> tuple[int, str, str]:
    category = _help_category(entry)
    return (
        _HELP_CATEGORY_ORDER.get(category, _HELP_CATEGORY_ORDER["Other"]),
        category.casefold(),
        _help_label(entry).casefold(),
    )


def _format_help_entries(
    entries: list[CommandPaletteEntry],
    *,
    width: int,
    query: str,
) -> list[str]:
    title = "Commands"
    if query:
        title = f"Commands matching '{query}'"

    lines = [title, "  Tip: /help <query> filters this list."]
    label_width = _help_label_width(entries, width=width)
    current_category = ""
    for entry in entries:
        category = _help_category(entry)
        if category != current_category:
            lines.append("")
            lines.append(category)
            current_category = category
        lines.extend(
            _format_help_row(
                _help_label(entry),
                entry.description,
                label_width=label_width,
                width=width,
            )
        )
    return lines


def _format_help_row(
    label: str,
    description: str,
    *,
    label_width: int,
    width: int,
) -> list[str]:
    indent = "  "
    if not description:
        return [f"{indent}{label}"]

    description_prefix = f"{indent}{'':<{label_width}}  "
    description_width = max(20, width - len(description_prefix))
    wrapped_description = textwrap.wrap(
        description,
        width=description_width,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [description]

    if len(label) > label_width:
        lines = [f"{indent}{label}"]
        lines.extend(f"{description_prefix}{line}" for line in wrapped_description)
        return lines

    first, *rest = wrapped_description
    lines = [f"{indent}{label:<{label_width}}  {first}"]
    lines.extend(f"{description_prefix}{line}" for line in rest)
    return lines


def _help_label_width(
    entries: Iterable[CommandPaletteEntry],
    *,
    width: int,
) -> int:
    max_label_width = max((len(_help_label(entry)) for entry in entries), default=0)
    terminal_budget = max(24, width - 38)
    return max(24, min(max_label_width, 64, terminal_budget))


def _help_output_width(context: CommandContext) -> int:
    capabilities = context.metadata.get("capabilities")
    width = getattr(capabilities, "width", None)
    if not (isinstance(width, int) and width > 0):
        width = 88
    # Reserve the 2-column transcript gutter the Rich command sink adds
    # (rendering/command_output.py): rows built to the FULL terminal
    # width overflowed by exactly the gutter and wrapped to column 0
    # (measured: 42 of 140 catalog rows at width 80).
    return max(40, width - 2)


def _help_label(entry: CommandPaletteEntry) -> str:
    return (entry.usage or entry.text or "").strip()


def _help_category(entry: CommandPaletteEntry) -> str:
    return (entry.category or "Other").strip() or "Other"


def _handle_exit(context: CommandContext, _args: list[str]) -> CommandResult:
    return CommandResult.exit("Goodbye!")


def _handle_clear(context: CommandContext, _args: list[str]) -> CommandResult:
    return CommandResult.clear("Cleared.")


def _unknown_command_result(command_name: str) -> CommandResult:
    display = f"/{command_name}" if command_name else "/"
    return CommandResult.failed(
        f"Unknown command: {display}\nType /help for available commands.",
        error_code="unknown_command",
        payload={"command": command_name},
    )


def _strip_json_flag(args: tuple[str, ...]) -> tuple[tuple[str, ...], bool]:
    remaining = tuple(arg for arg in args if str(arg).casefold() != "--json")
    return remaining, len(remaining) != len(args)


def _with_json_output(context: CommandContext) -> CommandContext:
    from .json_sink import JsonCommandOutputSink

    metadata = dict(context.metadata)
    metadata["json_output"] = True
    return replace(context, output=JsonCommandOutputSink(), metadata=metadata)


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
    """Completion candidates for a command and every descendant.

    Recursive for the same reason the palette is: depth-3 leaves are typeable
    spellings, and a completion list that stops at depth 2 teaches the parent
    as if it were the whole family.
    """
    items: list[CommandCompletion] = []

    def walk(node: Command, path: tuple[str, ...]) -> None:
        prefix = " ".join(path)
        items.append(
            CommandCompletion(
                text=f"/{prefix}",
                description=node.description,
                command_path=path,
                hidden=node.hidden,
            )
        )
        for alias in node.aliases:
            # A root alias is a whole command spelling; a subcommand alias
            # replaces the leaf under the same parent.
            text = (
                _display_alias(alias)
                if len(path) == 1
                else _subcommand_alias_text(alias, path[:-1])
            )
            items.append(
                CommandCompletion(
                    text=text,
                    description=node.description,
                    command_path=path,
                    hidden=node.hidden,
                    alias=True,
                )
            )
        for subcommand in node.subcommands.values():
            walk(subcommand, (*path, subcommand.name))

    walk(command, (command.name,))
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


def _subcommand_alias_text(alias: str, parent_path: tuple[str, ...]) -> str:
    """Completion text for a SUBCOMMAND alias, parent prefix counted once.

    Backend subcommand aliases arrive as WHOLE paths ("provider passback"),
    because that is how the backend registers them, so joining the parent onto
    one verbatim produced "/provider provider passback": a suggestion that
    resolves to nothing on either side.
    """
    normalized = _normalize_token(alias)
    prefix = " ".join(parent_path)
    if prefix and normalized.startswith(f"{prefix} "):
        normalized = normalized[len(prefix) + 1 :]
    return f"/{prefix} {normalized}".strip() if prefix else f"/{normalized}"


def _display_alias(alias: str) -> str:
    normalized = str(alias or "").strip()
    if not normalized:
        return ""
    return normalized if normalized.startswith("/") else f"/{normalized}"


__all__ = [
    "CommandParseError",
    "CommandRegistry",
]
