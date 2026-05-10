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
    CommandReturn,
    CommandStatus,
    ConfirmationHandler,
    ListCommandOutputSink,
    StateDispatcher,
)
from .registry import (
    CommandParseError,
    CommandRegistry,
    RichConsoleCommandOutputSink,
)

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
    "CommandReturn",
    "CommandStatus",
    "ConfirmationHandler",
    "ListCommandOutputSink",
    "RichConsoleCommandOutputSink",
    "StateDispatcher",
]
