"""Shared TODO formatting constants for Nymeria.

This module provides a single source of truth for TODO status icons
and priority markers used across the codebase.
"""

from .todo_manager import TodoPriority, TodoStatus


# Status icons for display
STATUS_ICONS = {
    TodoStatus.PENDING: "[ ]",
    TodoStatus.IN_PROGRESS: "[>]",
    TodoStatus.DONE: "[x]",
    TodoStatus.BLOCKED: "[!]",
}

# Priority markers for display
PRIORITY_MARKERS = {
    TodoPriority.HIGH: "!!",
    TodoPriority.MEDIUM: "!",
    TodoPriority.LOW: "",
}

# Permanent TODO markers
PERMANENT_MARKER = "[P]"
PERMANENT_REJECT_MSG = (
    "This task is recurring and permanent. It will continue activating at the "
    "scheduled interval. If you think this is a mistake, ask the user to cancel "
    "or delete it."
)

# Status ordering for sorting (lower is higher priority)
STATUS_ORDER = {
    TodoStatus.IN_PROGRESS: 0,
    TodoStatus.BLOCKED: 1,
    TodoStatus.PENDING: 2,
    TodoStatus.DONE: 3,
}

# Priority ordering for sorting (lower is higher priority)
PRIORITY_ORDER = {
    TodoPriority.HIGH: 0,
    TodoPriority.MEDIUM: 1,
    TodoPriority.LOW: 2,
    None: 3,
}
