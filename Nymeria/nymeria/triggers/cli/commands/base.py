"""Shared models for the CLI command registry."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias

CommandLevel: TypeAlias = Literal["info", "success", "warning", "error"]
CommandStatus: TypeAlias = Literal["ok", "error", "exit", "clear", "unhandled"]
CommandHandlerMode: TypeAlias = Literal["legacy", "context"]

CommandReturn: TypeAlias = Any
CommandHandler: TypeAlias = Callable[[Any, list[str]], Any]
StateDispatcher: TypeAlias = Callable[[Any], Any | Awaitable[Any]]
ConfirmationHandler: TypeAlias = Callable[[str], bool | Awaitable[bool]]
PromptHandler: TypeAlias = Callable[[str], str | Awaitable[str]]


class CommandOutputSink(Protocol):
    """Receives structured command output from handlers."""

    def emit(self, message: "CommandMessage") -> None:
        """Emit one command message."""


@dataclass(frozen=True, slots=True)
class CommandMessage:
    """Structured user-facing command output."""

    content: str
    level: CommandLevel = "info"
    title: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Result returned by command dispatch."""

    handled: bool = True
    status: CommandStatus = "ok"
    messages: tuple[CommandMessage, ...] = ()
    command_path: tuple[str, ...] = ()
    error_code: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status not in {"error", "unhandled"}

    @classmethod
    def completed(
        cls,
        *messages: CommandMessage | str,
        command_path: tuple[str, ...] = (),
        payload: Mapping[str, Any] | None = None,
    ) -> "CommandResult":
        return cls(
            status="ok",
            messages=_coerce_messages(messages),
            command_path=command_path,
            payload=dict(payload or {}),
        )

    @classmethod
    def failed(
        cls,
        message: CommandMessage | str,
        *,
        command_path: tuple[str, ...] = (),
        error_code: str = "command_error",
        payload: Mapping[str, Any] | None = None,
    ) -> "CommandResult":
        return cls(
            status="error",
            messages=_coerce_messages((message,), default_level="error"),
            command_path=command_path,
            error_code=error_code,
            payload=dict(payload or {}),
        )

    @classmethod
    def exit(
        cls,
        *messages: CommandMessage | str,
        command_path: tuple[str, ...] = (),
    ) -> "CommandResult":
        return cls(
            status="exit",
            messages=_coerce_messages(messages),
            command_path=command_path,
        )

    @classmethod
    def clear(
        cls,
        *messages: CommandMessage | str,
        command_path: tuple[str, ...] = (),
    ) -> "CommandResult":
        return cls(
            status="clear",
            messages=_coerce_messages(messages),
            command_path=command_path,
        )

    @classmethod
    def unhandled(cls) -> "CommandResult":
        return cls(handled=False, status="unhandled")


@dataclass(slots=True)
class CommandContext:
    """Context object passed to v2 command handlers."""

    client: Any | None = None
    output: CommandOutputSink | None = None
    dispatch_state: StateDispatcher | None = None
    confirm_handler: ConfirmationHandler | None = None
    prompt_handler: PromptHandler | None = None
    secret_prompt_handler: PromptHandler | None = None
    thread_id: str | None = None
    user_id: str = "default"
    registry: Any | None = None
    legacy_state: Any | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def emit(
        self,
        content: str,
        *,
        level: CommandLevel = "info",
        title: str = "",
        details: Mapping[str, Any] | None = None,
    ) -> CommandMessage:
        """Emit one structured message through the configured output sink."""

        message = CommandMessage(
            content=content,
            level=level,
            title=title,
            details=dict(details or {}),
        )
        if self.output is not None:
            self.output.emit(message)
        return message

    async def dispatch(self, action: Any) -> Any:
        """Dispatch a state/UI action if the renderer supplied a dispatcher."""

        if self.dispatch_state is None:
            return None
        result = self.dispatch_state(action)
        if inspect.isawaitable(result):
            return await result
        return result

    async def confirm(self, prompt: str, *, default: bool = False) -> bool:
        """Ask for confirmation through the renderer, defaulting safely."""

        if self.confirm_handler is None:
            return default
        result = self.confirm_handler(prompt)
        if inspect.isawaitable(result):
            return bool(await result)
        return bool(result)

    async def prompt(self, prompt: str, *, secret: bool = False) -> str:
        """Ask for user input through the active renderer."""

        handler = self.secret_prompt_handler if secret else self.prompt_handler
        if handler is None:
            return ""
        result = handler(prompt)
        if inspect.isawaitable(result):
            return str(await result)
        return str(result)


@dataclass(slots=True)
class Command:
    """A registered slash command or subcommand."""

    name: str
    description: str
    handler: CommandHandler
    aliases: list[str] = field(default_factory=list)
    usage: str = ""
    subcommands: dict[str, "Command"] = field(default_factory=dict)
    hidden: bool = False
    handler_mode: CommandHandlerMode = "legacy"
    category: str = ""
    palette_title: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    builtin: bool = False


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    """Parsed slash-command input."""

    raw_input: str
    command_name: str
    args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandMatch:
    """Resolved command target and remaining arguments."""

    invocation: CommandInvocation
    root: Command
    command: Command
    path: tuple[str, ...]
    args: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommandCompletion:
    """Completion candidate with display metadata."""

    text: str
    description: str = ""
    command_path: tuple[str, ...] = ()
    hidden: bool = False
    alias: bool = False


@dataclass(frozen=True, slots=True)
class CommandPaletteEntry:
    """Command palette entry exposed by the registry."""

    text: str
    description: str = ""
    usage: str = ""
    category: str = ""
    command_path: tuple[str, ...] = ()


@dataclass(slots=True)
class ListCommandOutputSink:
    """In-memory output sink for tests and renderer adapters."""

    messages: list[CommandMessage] = field(default_factory=list)

    def emit(self, message: CommandMessage) -> None:
        self.messages.append(message)


def _coerce_messages(
    messages: tuple[CommandMessage | str, ...],
    *,
    default_level: CommandLevel = "info",
) -> tuple[CommandMessage, ...]:
    output: list[CommandMessage] = []
    for item in messages:
        if isinstance(item, CommandMessage):
            output.append(item)
        elif item is not None:
            output.append(CommandMessage(str(item), level=default_level))
    return tuple(output)


__all__ = [
    "Command",
    "CommandCompletion",
    "CommandContext",
    "CommandHandler",
    "CommandHandlerMode",
    "CommandInvocation",
    "CommandLevel",
    "CommandMatch",
    "CommandMessage",
    "CommandOutputSink",
    "CommandPaletteEntry",
    "CommandResult",
    "CommandReturn",
    "CommandStatus",
    "ConfirmationHandler",
    "ListCommandOutputSink",
    "PromptHandler",
    "StateDispatcher",
]
