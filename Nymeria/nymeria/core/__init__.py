"""Nymeria core module."""

from .agent import NymeriaAgent, get_current_agent, set_current_agent
from .user_profile import Memory, OptInSettings, UserProfile, UserProfileManager
from .backup import BackupManager
from .validator import CodeValidator
from .self_agent import SELF_AGENT_TOOLS
from .ticker import Ticker, get_ticker, set_ticker
from .todo_manager import TodoManager, TodoList, TodoItem, TodoStatus
from .todo_schedule_db import TodoScheduleDB, ScheduledTodoEntry

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
    # Ticker
    "Ticker",
    "get_ticker",
    "set_ticker",
    # TODO Manager
    "TodoManager",
    "TodoList",
    "TodoItem",
    "TodoStatus",
    # TODO Schedule Database
    "TodoScheduleDB",
    "ScheduledTodoEntry",
]
