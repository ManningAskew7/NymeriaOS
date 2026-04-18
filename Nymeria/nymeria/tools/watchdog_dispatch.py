"""Watchdog-specific tools — cross-thread dispatch, notepad reading, and TODO overview.

These are optional tools designed exclusively for the Smart Watchdog thread.
They provide cross-thread visibility that normal threads don't need or want.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Annotated, List, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.activity_log import ActivityType, log_activity
from ..core.time_utils import parse_scheduled_time, get_user_tz
from ..core.todo_constants import STATUS_ICONS, STATUS_ORDER
from ..core.todo_manager import TodoManager, TodoStatus
from .utils import get_user_id, get_thread_id

logger = logging.getLogger(__name__)

_todo_manager: Optional[TodoManager] = None


def _get_todo_manager() -> TodoManager:
    global _todo_manager
    if _todo_manager is None:
        from ..config import get_settings
        _todo_manager = TodoManager(get_settings().data_dir)
    return _todo_manager


def _get_schedule_db():
    from ..core.agent import get_current_agent
    agent = get_current_agent()
    if agent is None:
        return None
    return getattr(agent, '_schedule_db', None)


@tool
def watchdog_dispatch(
    target_thread_id: str,
    task: str,
    scheduled_for: str = "now",
    notes: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Dispatch a TODO to a target thread. You CANNOT target your own thread.

    Use this to wake up other threads with specific work. The target thread
    will receive the TODO and execute it on its next tick.

    Args:
        target_thread_id: Thread ID to dispatch the TODO to (must be different from yours)
        task: Clear, specific description of what the target thread should do
        scheduled_for: When to fire: "now", "30s", "5m", "1h", "1d" or "YYYY-MM-DD HH:MM"
        notes: Supporting context for the target thread (e.g. what you observed)
    """
    caller_thread_id = get_thread_id(config)
    user_id = get_user_id(config)

    if target_thread_id == caller_thread_id:
        return "[Error]: Cannot dispatch to your own thread. Use nym_todo for self-scheduling."

    if not task:
        return "[Error]: 'task' is required."

    if not target_thread_id:
        return "[Error]: 'target_thread_id' is required."

    if scheduled_for == "now":
        scheduled_for = "30s"

    todo_scheduled = parse_scheduled_time(scheduled_for)
    if not todo_scheduled:
        return f"[Error]: Invalid scheduled_for '{scheduled_for}'. Use 'now', '30s', '5m', '1h', '1d' or 'YYYY-MM-DD HH:MM'."

    manager = _get_todo_manager()
    schedule_db = _get_schedule_db()

    with manager.atomic_update(user_id) as todo_list:
        item = todo_list.add_item(
            task,
            scheduled_for=todo_scheduled,
            thread_id=target_thread_id,
            notes=notes[:1000] if notes else None,
        )
        if not item:
            return f"[Error]: TODO limit reached. Complete or delete some tasks on thread '{target_thread_id}' first."

        if schedule_db:
            schedule_db.add_scheduled(
                todo_id=item.id,
                user_id=user_id,
                scheduled_for=todo_scheduled,
                task_preview=task[:100],
                thread_id=target_thread_id,
            )

    log_activity(
        ActivityType.WATCHDOG_NUDGE,
        f"Dispatched to {target_thread_id}: {task[:80]}",
        user_id=user_id,
        thread_id=caller_thread_id,
        metadata={
            "todo_id": item.id,
            "target_thread": target_thread_id,
            "scheduled_for": scheduled_for,
        },
    )

    logger.info(f"Watchdog dispatched TODO {item.id} to thread {target_thread_id}: {task[:50]}")
    return f"[Dispatched]: TODO {item.id} created on thread '{target_thread_id}': {task[:100]} (scheduled: {scheduled_for})"


@tool
def watchdog_read_notepad(
    target_thread_id: str,
) -> str:
    """
    Read another thread's notepad to understand what it's currently focused on.

    Args:
        target_thread_id: Thread ID whose notepad to read
    """
    if not target_thread_id:
        return "[Error]: 'target_thread_id' is required."

    from .thread_notes import read_notepad
    content = read_notepad(target_thread_id)
    if content:
        return content
    return f"[empty] — thread '{target_thread_id}' has no notepad content."


@tool
def watchdog_todo_overview(
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    List all active TODOs across ALL threads, showing which thread each belongs to.

    Use this before dispatching to avoid creating duplicate TODOs.
    """
    user_id = get_user_id(config)
    manager = _get_todo_manager()
    todo_list_obj = manager.get_todos(user_id)

    items = todo_list_obj.get_active_todos()
    if not items:
        return "[Info]: No active TODOs across any thread."

    sorted_items = sorted(
        items,
        key=lambda i: (
            i.thread_id or "",
            STATUS_ORDER.get(i.status, 3),
            (i.scheduled_for or datetime.max).timestamp() if i.scheduled_for else float('inf'),
        ),
    )

    # Group by thread
    current_thread = None
    lines: List[str] = [f"All Active TODOs ({len(items)} total):"]
    lines.append("")

    for item in sorted_items:
        tid = item.thread_id or "unassigned"
        if tid != current_thread:
            current_thread = tid
            lines.append(f"  Thread: {tid}")

        icon = STATUS_ICONS.get(item.status, "[ ]")
        line = f"    {icon} [{item.id}] {item.task}"

        if item.scheduled_for:
            display_time = item.scheduled_for.astimezone(get_user_tz())
            line += f" [scheduled: {display_time.strftime('%Y-%m-%d %H:%M')}]"

        if item.recurrence:
            line += f" [recurring: {item.recurrence}]"

        lines.append(line)

    return "\n".join(lines)


WATCHDOG_DISPATCH_TOOLS = [watchdog_dispatch, watchdog_read_notepad, watchdog_todo_overview]
