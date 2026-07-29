"""Nymeria core module."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Import eagerly for type checkers and IDEs only. At runtime these resolve
    # through __getattr__ below, so nothing here costs import time.
    from .agent import NymeriaAgent, get_current_agent, set_current_agent
    from .user_profile import Memory, OptInSettings, UserProfile, UserProfileManager
    from .backup import BackupManager
    from .validator import CodeValidator
    from .self_agent import SELF_AGENT_TOOLS
    from .ticker import Ticker, get_ticker, set_ticker
    from .todo_manager import TodoManager, TodoList, TodoItem, TodoStatus
    from .todo_schedule_db import TodoScheduleDB, ScheduledTodoEntry

# Re-exports are resolved lazily (PEP 562), mirroring `nymeria/__init__.py`.
# This init used to import `.agent` eagerly, so reaching ANY core submodule
# (`core.storage_paths`, `core.checkpointer_config`, ...) built the whole agent
# runtime and pulled langchain -> transformers -> torch behind it: ~5.5s paid
# by callers that only wanted a path helper. Keep this table lazy.
_LAZY_EXPORTS = {
    "NymeriaAgent": ".agent",
    "get_current_agent": ".agent",
    "set_current_agent": ".agent",
    "Memory": ".user_profile",
    "OptInSettings": ".user_profile",
    "UserProfile": ".user_profile",
    "UserProfileManager": ".user_profile",
    "BackupManager": ".backup",
    "CodeValidator": ".validator",
    "SELF_AGENT_TOOLS": ".self_agent",
    "Ticker": ".ticker",
    "get_ticker": ".ticker",
    "set_ticker": ".ticker",
    "TodoManager": ".todo_manager",
    "TodoList": ".todo_manager",
    "TodoItem": ".todo_manager",
    "TodoStatus": ".todo_manager",
    "TodoScheduleDB": ".todo_schedule_db",
    "ScheduledTodoEntry": ".todo_schedule_db",
}


def __getattr__(name: str):
    """Resolve a re-exported symbol by importing only its defining submodule.

    Falls back to importing ``name`` as a submodule: the eager imports this
    replaced also bound every submodule they touched as a package attribute,
    so `nymeria.core.ticker` kept working without a direct import.
    """
    from importlib import import_module

    module = _LAZY_EXPORTS.get(name)
    if module is not None:
        return getattr(import_module(module, __name__), name)

    try:
        return import_module(f".{name}", __name__)
    except ModuleNotFoundError as exc:
        # Only translate "this submodule does not exist". A submodule that
        # exists but fails on a missing dependency must surface its own error,
        # not be masked as a missing attribute.
        if exc.name != f"{__name__}.{name}":
            raise
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None


def __dir__() -> list[str]:
    # Augment the default (`vars()`), never replace it: __getattr__ makes
    # submodules reachable, so hiding them here (along with __name__ and
    # already-imported submodules) would be a strict regression.
    return sorted(set(__all__) | set(globals()))


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
