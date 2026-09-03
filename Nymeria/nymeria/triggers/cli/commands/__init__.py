"""Command registry exports for the CLI."""

from __future__ import annotations

from .base import (
    Command,
    CommandCompletion,
    CommandContext,
    CommandHandler,
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
    ListCommandOutputSink,
    PromptHandler,
    StateDispatcher,
)
from .registry import (
    CommandParseError,
    CommandRegistry,
)
from .json_sink import JsonCommandOutputSink

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
    "CommandParseError",
    "CommandRegistry",
    "CommandResult",
    "CommandResultOutputSink",
    "CommandReturn",
    "CommandStatus",
    "ListCommandOutputSink",
    "PromptHandler",
    "JsonCommandOutputSink",
    "StateDispatcher",
]
