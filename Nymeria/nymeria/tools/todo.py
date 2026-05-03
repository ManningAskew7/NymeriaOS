"""TODO tools for Nymeria - manage autonomous task lists.

TODOs are the primary driver for autonomous operation. Active TODOs are
automatically injected into the system prompt, so Nymeria always knows
what tasks need attention.

Scheduling is integrated into TODOs via the `scheduled_for` field.
When a TODO has a scheduled time, Nymeria wakes up to work on it.
"""

import logging
import uuid as _uuid
from datetime import datetime
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.activity_log import ActivityType, log_activity
from ..core.time_utils import parse_scheduled_time, get_user_tz
from ..core.todo_constants import STATUS_ICONS, STATUS_ORDER, VALID_RECURRENCES
from ..core.todo_manager import TodoManager, TodoStatus
from .utils import get_user_id, get_thread_id

logger = logging.getLogger(__name__)

# Global TODO manager instance (initialized lazily)
_todo_manager: Optional[TodoManager] = None


def _get_todo_manager() -> TodoManager:
    """Get or create the global TODO manager."""
    global _todo_manager
    if _todo_manager is None:
        # Default to standard data directory
        from ..config import get_settings
        settings = get_settings()
        _todo_manager = TodoManager(settings.data_dir)
    return _todo_manager


# Use shared time parsing utilities
_parse_scheduled_for = parse_scheduled_time


def _get_schedule_db():
    """Get the schedule database from the current agent."""
    from ..core.agent import get_current_agent
    agent = get_current_agent()
    if agent is None:
        return None
    return getattr(agent, '_schedule_db', None)


def _format_todo_item(item, show_notes: bool = False) -> str:
    """Format a single TODO item for display."""
    icon = STATUS_ICONS.get(item.status, "[ ]")

    line = f"{icon} [{item.id}] {item.task}"

    if item.scheduled_for:
        display_time = item.scheduled_for.astimezone(get_user_tz())
        line += f" [scheduled: {display_time.strftime('%Y-%m-%d %H:%M')}]"

    if item.recurrence:
        line += f" [recurring: {item.recurrence}]"

    if show_notes and item.notes:
        line += f"\n    Notes: {item.notes}"

    return line


@tool
def nym_todo(
    todo_id: Optional[str] = None,
    task: Optional[str] = None,
    scheduled_for: Optional[str] = None,
    status: Optional[str] = None,
    notes: Optional[str] = None,
    recurrence: Optional[str] = None,
    clear_schedule: bool = False,
    clear_recurrence: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Create or update a TODO item. Omit todo_id to create, provide it to update.

    These are YOUR tasks. Scheduled TODOs auto-wake you to execute them.
    Use scheduled_for to prompt yourself at a specific time.

    Recurring TODOs auto-reschedule when marked done; use nym_todo_delete or
    clear_recurrence to stop them permanently.

    Args:
        todo_id: 8-char TODO ID (omit to create a new TODO)
        task: Task description (required for create, optional for update)
        scheduled_for: When to auto-wake: "30s", "5m", "1h", "1d" or "YYYY-MM-DD HH:MM" (required for create)
        status: "pending", "in_progress", or "done"
        notes: Additional notes (max 1000 chars)
        recurrence: "5min", "10min", "15min", "30min", "hourly", "daily", "weekly", "monthly"
        clear_schedule: Remove scheduled time
        clear_recurrence: Remove recurrence pattern
    """
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)
    if not thread_id:
        thread_id = f"todo-{str(_uuid.uuid4())[:8]}"
    manager = _get_todo_manager()
    schedule_db = _get_schedule_db()

    # --- CREATE mode (no todo_id) ---
    if todo_id is None:
        if not task:
            return "[Error]: 'task' is required when creating a new TODO."
        if not scheduled_for:
            return "[Error]: 'scheduled_for' is required when creating a new TODO. Every TODO needs a wake time."

        logger.info(f"nym_todo create: task={task[:50]}")

        # Parse scheduled_for
        todo_scheduled = _parse_scheduled_for(scheduled_for)
        if not todo_scheduled:
            return f"[Error]: Invalid scheduled_for format '{scheduled_for}'. Use '30s', '5m', '1h', '1d' or 'YYYY-MM-DD HH:MM'."

        # Validate recurrence
        todo_recurrence = None
        if recurrence:
            if recurrence.lower() not in VALID_RECURRENCES:
                return f"[Error]: Invalid recurrence '{recurrence}'. Use: {', '.join(VALID_RECURRENCES)}"
            todo_recurrence = recurrence.lower()

        # Use atomic update to prevent race conditions
        with manager.atomic_update(user_id) as todo_list:
            item = todo_list.add_item(
                task,
                scheduled_for=todo_scheduled,
                thread_id=thread_id,
                recurrence=todo_recurrence,
            )
            if item:
                logger.info(f"TODO added for user {user_id}: {item.id} - {task[:50]}")

                # Sync to schedule database if scheduled
                if todo_scheduled and schedule_db:
                    schedule_db.add_scheduled(
                        todo_id=item.id,
                        user_id=user_id,
                        scheduled_for=todo_scheduled,
                        task_preview=task[:100],
                        thread_id=thread_id,
                    )

                # Log activity
                metadata = {"todo_id": item.id}
                if scheduled_for:
                    metadata["scheduled_for"] = scheduled_for
                if todo_recurrence:
                    metadata["recurrence"] = todo_recurrence
                log_activity(
                    ActivityType.TODO_ADDED,
                    f"TODO added: {task[:80]}",
                    user_id=user_id,
                    metadata=metadata,
                )

                result = f"[Added]: TODO {item.id}: {task[:100]}"
                if todo_scheduled:
                    result += f" (scheduled for {scheduled_for})"
                if todo_recurrence:
                    result += f" (recurring: {todo_recurrence})"
                return result
            else:
                return f"[Error]: TODO limit reached ({todo_list.MAX_TODOS} active items). Complete or delete some tasks first."

    # --- UPDATE mode (todo_id provided) ---
    logger.info(f"nym_todo update: id={todo_id}")

    # Parse status
    todo_status = None
    if status:
        try:
            todo_status = TodoStatus(status.lower())
        except ValueError:
            return f"[Error]: Invalid status '{status}'. Use 'pending', 'in_progress', or 'done'."

    # Parse scheduled_for
    todo_scheduled = None
    if scheduled_for and not clear_schedule:
        todo_scheduled = _parse_scheduled_for(scheduled_for)
        if not todo_scheduled:
            return f"[Error]: Invalid scheduled_for format '{scheduled_for}'. Use '30s', '5m', '1h', '1d' or 'YYYY-MM-DD HH:MM'."

    # Validate recurrence
    todo_recurrence = None
    if recurrence and not clear_recurrence:
        if recurrence.lower() not in VALID_RECURRENCES:
            return f"[Error]: Invalid recurrence '{recurrence}'. Use: {', '.join(VALID_RECURRENCES)}"
        todo_recurrence = recurrence.lower()

    # Use atomic update to prevent race conditions
    rescheduled_time = None
    with manager.atomic_update(user_id) as todo_list:
        success = todo_list.update_item(
            todo_id,
            task=task,
            status=todo_status,
            notes=notes,
            scheduled_for=todo_scheduled,
            clear_schedule=clear_schedule,
            thread_id=thread_id if todo_scheduled else None,  # Only update thread on reschedule
            recurrence=todo_recurrence,
            clear_recurrence=clear_recurrence,
        )
        if success:
            item = todo_list.get_item(todo_id)
            logger.info(f"TODO updated for user {user_id}: {todo_id}")

            # Auto-reschedule recurring TODOs marked as done
            if todo_status == TodoStatus.DONE and item.recurrence:
                from ..core.todo_constants import RECURRENCE_DELTAS
                delta = RECURRENCE_DELTAS.get(item.recurrence)
                if delta:
                    rescheduled_time = datetime.utcnow() + delta
                    todo_list.update_item(
                        todo_id,
                        scheduled_for=rescheduled_time,
                        status=TodoStatus.PENDING,
                    )
                    item = todo_list.get_item(todo_id)
                    if item:
                        item.last_execution = datetime.utcnow()
                    logger.info(f"Auto-rescheduled recurring TODO {todo_id} for {rescheduled_time}")

            # Sync to schedule database
            if schedule_db:
                if rescheduled_time:
                    schedule_db.add_scheduled(
                        todo_id=item.id,
                        user_id=user_id,
                        scheduled_for=rescheduled_time,
                        task_preview=item.task[:100],
                        thread_id=item.thread_id,
                    )
                elif clear_schedule or todo_status == TodoStatus.DONE:
                    schedule_db.remove_scheduled(todo_id)
                elif todo_scheduled:
                    schedule_db.add_scheduled(
                        todo_id=item.id,
                        user_id=user_id,
                        scheduled_for=todo_scheduled,
                        task_preview=item.task[:100],
                        thread_id=thread_id,
                    )

            # Log activity
            metadata = {"todo_id": todo_id, "status": item.status.value}
            if scheduled_for:
                metadata["scheduled_for"] = scheduled_for
            if clear_schedule:
                metadata["schedule_cleared"] = True
            if todo_recurrence:
                metadata["recurrence"] = todo_recurrence
            if clear_recurrence:
                metadata["recurrence_cleared"] = True
            if rescheduled_time:
                metadata["rescheduled"] = True
            log_activity(
                ActivityType.TODO_UPDATED,
                f"TODO updated: {item.task[:60]} (status: {item.status.value})",
                user_id=user_id,
                metadata=metadata,
            )

            result = f"[Updated]: TODO {todo_id} - {item.task[:50]} (status: {item.status.value})"
            if rescheduled_time:
                result += f" (auto-rescheduled: recurring {item.recurrence})"
            elif item.scheduled_for:
                result += f" (scheduled)"
            elif clear_schedule:
                result += " (schedule cleared)"
            if item.recurrence and not rescheduled_time:
                result += f" (recurring: {item.recurrence})"
            elif clear_recurrence:
                result += " (recurrence cleared)"
            return result
        else:
            return f"[Error]: TODO '{todo_id}' not found. Use nym_todo_list to see available TODOs."


def _todo_complete_internal(
    todo_id: str,
    user_id: str,
) -> str:
    """
    Internal function to mark a TODO as completed.
    Used by MCP server. For tool usage, use nym_todo(todo_id=..., status="done").

    Args:
        todo_id: 8-char TODO ID
        user_id: User ID
    """
    logger.info(f"_todo_complete_internal called: id={todo_id}, user={user_id}")

    manager = _get_todo_manager()
    schedule_db = _get_schedule_db()

    # Use atomic update to prevent race conditions
    rescheduled_time = None
    with manager.atomic_update(user_id) as todo_list:
        item = todo_list.get_item(todo_id)
        if not item:
            return f"[Error]: TODO '{todo_id}' not found. Use nym_todo_list to see available TODOs."

        task_name = item.task
        has_recurrence = item.recurrence
        success = todo_list.complete_item(todo_id)
        if success:
            logger.info(f"TODO completed for user {user_id}: {todo_id}")

            # Auto-reschedule recurring TODOs
            if has_recurrence:
                from ..core.todo_constants import RECURRENCE_DELTAS
                delta = RECURRENCE_DELTAS.get(has_recurrence)
                if delta:
                    rescheduled_time = datetime.utcnow() + delta
                    todo_list.update_item(
                        todo_id,
                        scheduled_for=rescheduled_time,
                        status=TodoStatus.PENDING,
                    )
                    refreshed = todo_list.get_item(todo_id)
                    if refreshed:
                        refreshed.last_execution = datetime.utcnow()
                    logger.info(f"Auto-rescheduled recurring TODO {todo_id} for {rescheduled_time}")

            # Sync schedule database
            if schedule_db:
                if rescheduled_time:
                    schedule_db.add_scheduled(
                        todo_id=todo_id,
                        user_id=user_id,
                        scheduled_for=rescheduled_time,
                        task_preview=task_name[:100],
                        thread_id=item.thread_id,
                    )
                else:
                    schedule_db.remove_scheduled(todo_id)

            # Log activity
            log_activity(
                ActivityType.TODO_COMPLETED,
                f"TODO completed: {task_name[:80]}",
                user_id=user_id,
                metadata={"todo_id": todo_id},
            )
            if rescheduled_time:
                return f"[Completed]: {task_name[:100]} (auto-rescheduled: recurring {has_recurrence})"
            return f"[Completed]: {task_name[:100]}"
        else:
            return f"[Error]: Failed to complete TODO '{todo_id}'."


@tool
def nym_todo_delete(
    todo_id: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Delete a TODO permanently (no archive). Cancels any scheduled execution.

    Args:
        todo_id: 8-char TODO ID
    """
    logger.info(f"nym_todo_delete called: id={todo_id}")

    user_id = get_user_id(config)
    manager = _get_todo_manager()
    schedule_db = _get_schedule_db()

    # Use atomic update to prevent race conditions
    with manager.atomic_update(user_id) as todo_list:
        deleted = todo_list.delete_item(todo_id)
        if deleted:
            logger.info(f"TODO deleted for user {user_id}: {todo_id}")

            # Remove from schedule database
            if schedule_db:
                schedule_db.remove_scheduled(todo_id)

            # Log activity
            log_activity(
                ActivityType.TODO_DELETED,
                f"TODO deleted: {deleted.task[:80]}",
                user_id=user_id,
                metadata={"todo_id": todo_id},
            )
            return f"[Deleted]: {deleted.task[:100]}"
        else:
            return f"[Error]: TODO '{todo_id}' not found. Use nym_todo_list to see available TODOs."


@tool
def nym_todo_list(
    filter_status: Optional[str] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    List TODO items. Shows active (non-done) by default.

    Args:
        filter_status: "pending", "in_progress", "done", or "all"
    """
    logger.info(f"nym_todo_list called: filter={filter_status}")

    user_id = get_user_id(config)
    manager = _get_todo_manager()
    todo_list_obj = manager.get_todos(user_id)

    # Filter items
    if filter_status == "all":
        items = todo_list_obj.items
    elif filter_status:
        try:
            status = TodoStatus(filter_status.lower())
            items = [i for i in todo_list_obj.items if i.status == status]
        except ValueError:
            return f"[Error]: Invalid status filter '{filter_status}'. Use 'pending', 'in_progress', 'done', or 'all'."
    else:
        # Default: active (non-done) items
        items = todo_list_obj.get_active_todos()

    if not items:
        if filter_status:
            return f"[Info]: No TODOs with status '{filter_status}'."
        return "[Info]: No active TODOs. Use nym_todo to create tasks."

    # Sort: in_progress first, then pending, then done; then by scheduled_for, then created_at
    sorted_items = sorted(
        items,
        key=lambda i: (
            STATUS_ORDER.get(i.status, 3),
            (i.scheduled_for or datetime.max).timestamp() if i.scheduled_for else float('inf'),
            i.created_at,
        ),
    )

    # Format output
    lines = [f"TODO List ({len(items)} item{'s' if len(items) != 1 else ''}):"]
    lines.append("")

    for item in sorted_items:
        lines.append(_format_todo_item(item, show_notes=True))

    return "\n".join(lines)


# Export TODO tools
TODO_TOOLS = [
    nym_todo,
    nym_todo_delete,
    nym_todo_list,
]
