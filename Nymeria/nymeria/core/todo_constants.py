"""Shared TODO formatting constants for Nymeria.

This module provides a single source of truth for TODO status icons,
recurrence patterns, and sorting orders used across the codebase.
"""

from datetime import timedelta

from .todo_manager import TodoStatus


# Status icons for display
STATUS_ICONS = {
    TodoStatus.PENDING: "[ ]",
    TodoStatus.IN_PROGRESS: "[>]",
    TodoStatus.DONE: "[x]",
}

# Status ordering for sorting (lower is higher priority)
STATUS_ORDER = {
    TodoStatus.IN_PROGRESS: 0,
    TodoStatus.PENDING: 1,
    TodoStatus.DONE: 2,
}

# Valid recurrence patterns (single source of truth)
VALID_RECURRENCES = ['5min', '10min', '15min', '30min', 'hourly', 'daily', 'weekly', 'monthly']

# Recurrence deltas for calculating next execution time
RECURRENCE_DELTAS = {
    '5min': timedelta(minutes=5),
    '10min': timedelta(minutes=10),
    '15min': timedelta(minutes=15),
    '30min': timedelta(minutes=30),
    'hourly': timedelta(hours=1),
    'daily': timedelta(days=1),
    'weekly': timedelta(weeks=1),
    'monthly': timedelta(days=30),  # Approximate
}
