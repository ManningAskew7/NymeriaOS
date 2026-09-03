"""Shared models for the CLI command registry."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeAlias

CommandLevel: TypeAlias = Literal["info", "success", "warning", "error"]
CommandStatus: TypeAlias = Literal["ok", "error", "exit", "clear", "unhandled"]

CommandReturn: TypeAlias = Any
CommandHandler: TypeAlias = Callable[[Any, list[str]], Any]
StateDispatcher: TypeAlias = Callable[[Any], Any | Awaitable[Any]]
PromptHandler: TypeAlias = Callable[[str], str | Awaitable[str]]

_JSON_UNSET = object()


class CommandOutputSink(Protocol):
    """Receives structured command output from handlers."""

    def emit(self, message: "CommandMessage") -> None:
        """Emit one command message."""


class CommandResultOutputSink(CommandOutputSink, Protocol):
    """Receives a complete command result for machine-readable renderers."""

    def emit_result(self, result: "CommandResult") -> None:
        """Emit one complete command result."""


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
    json_payload: Any = field(default=_JSON_UNSET, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.status not in {"error", "unhandled"}

    @property
    def has_json_payload(self) -> bool:
        return self.json_payload is not _JSON_UNSET

    @classmethod
    def completed(
        cls,
        *messages: CommandMessage | str,
        command_path: tuple[str, ...] = (),
        payload: Mapping[str, Any] | None = None,
        json_payload: Any = _JSON_UNSET,
    ) -> "CommandResult":
        return cls(
            status="ok",
            messages=_coerce_messages(messages),
            command_path=command_path,
            payload=dict(payload or {}),
            json_payload=json_payload,
        )

    @classmethod
    def failed(
        cls,
        message: CommandMessage | str,
        *,
        command_path: tuple[str, ...] = (),
        error_code: str = "command_error",
        payload: Mapping[str, Any] | None = None,
        json_payload: Any = _JSON_UNSET,
    ) -> "CommandResult":
        return cls(
            status="error",
            messages=_coerce_messages((message,), default_level="error"),
            command_path=command_path,
            error_code=error_code,
            payload=dict(payload or {}),
            json_payload=json_payload,
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
    prompt_handler: PromptHandler | None = None
    secret_prompt_handler: PromptHandler | None = None
    thread_id: str | None = None
    user_id: str = "default"
    registry: Any | None = None
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

    async def prompt(self, prompt: str, *, secret: bool = False) -> str:
        """Ask for user input through the active renderer."""

        handler = self.secret_prompt_handler if secret else self.prompt_handler
        if handler is None:
            return ""
        result = handler(prompt)
        if inspect.isawaitable(result):
            return str(await result)
        return str(result)

    def supports_forms(self) -> bool:
        """True when the active renderer can show an interactive form.

        Only the Rich REPL renders the inline form panel; the plain renderer
        and any non-interactive caller fall back to text/argument behavior.
        """

        capabilities = self.metadata.get("capabilities")
        return (
            getattr(capabilities, "renderer", "") == "rich"
            and self.dispatch_state is not None
        )


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
    "CommandInvocation",
    "CommandLevel",
    "CommandMatch",
    "CommandMessage",
    "CommandOutputSink",
    "CommandPaletteEntry",
    "CommandResult",
    "CommandResultOutputSink",
    "CommandReturn",
    "CommandStatus",
    "ListCommandOutputSink",
    "PromptHandler",
    "StateDispatcher",
]
