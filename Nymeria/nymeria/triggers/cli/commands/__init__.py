"""Command registry exports for the CLI."""

from __future__ import annotations

from .base import (
    Command,
    CommandCompletion,
    CommandContext,
    CommandHandler,
    CommandHandlerMode,
    CommandInvocation,
    CommandLevel,
    CommandMatch,
    CommandMessage,
    CommandOutputSink,
    CommandPaletteEntry,
    CommandResult,
    CommandResultOutputSink,
    CommandReturn,
    CommandStatus,
    ConfirmationHandler,
    ListCommandOutputSink,
    PromptHandler,
    StateDispatcher,
)
from .registry import (
    CommandParseError,
    CommandRegistry,
    RichConsoleCommandOutputSink,
)
from .json_sink import JsonCommandOutputSink

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
    "CommandParseError",
    "CommandRegistry",
    "CommandResult",
    "CommandResultOutputSink",
    "CommandReturn",
    "CommandStatus",
    "ConfirmationHandler",
    "ListCommandOutputSink",
    "PromptHandler",
    "JsonCommandOutputSink",
    "RichConsoleCommandOutputSink",
    "StateDispatcher",
]
