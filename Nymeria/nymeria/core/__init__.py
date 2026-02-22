"""Nymeria core module."""

from .agent import NymeriaAgent, get_current_agent, set_current_agent
from .user_profile import Memory, OptInSettings, UserProfile, UserProfileManager
from .backup import BackupManager
from .validator import CodeValidator
from .self_agent import SELF_AGENT_TOOLS
from .ticker import Ticker, get_ticker, set_ticker
from .scheduler import DurableScheduler, ExecutionResult
from .rate_limiter import RateLimiter
from .todo_manager import TodoManager, TodoList, TodoItem, TodoStatus
from .todo_schedule_db import TodoScheduleDB, ScheduledTodoEntry
from .watchdog import Watchdog, get_watchdog, set_watchdog

# Deprecated imports - kept for backwards compatibility, will be removed
from ._deprecated.task_db import TaskDatabase, TaskStatus, DurableTask

# Backwards compatibility alias (deprecated)
Scheduler = DurableScheduler

__all__ = [
    # Agent
    "NymeriaAgent",
    "get_current_agent",
    "set_current_agent",
    # User Profile
    "Memory",
    "OptInSettings",
    "UserProfile",
    "UserProfileManager",
    # Utilities
    "BackupManager",
    "CodeValidator",
    "SELF_AGENT_TOOLS",
    # Rate Limiter
    "RateLimiter",
    # Task Database (DEPRECATED - use TodoScheduleDB, will be removed)
    "TaskDatabase",
    "TaskStatus",
    "DurableTask",
    # Ticker
    "Ticker",
    "get_ticker",
    "set_ticker",
    # Scheduler (DEPRECATED - use TODO scheduling, will be removed)
    "DurableScheduler",
    "Scheduler",  # Backwards compatibility alias
    "ExecutionResult",
    # TODO Manager
    "TodoManager",
    "TodoList",
    "TodoItem",
    "TodoStatus",
    # TODO Schedule Database
    "TodoScheduleDB",
    "ScheduledTodoEntry",
    # Watchdog
    "Watchdog",
    "get_watchdog",
    "set_watchdog",
]
