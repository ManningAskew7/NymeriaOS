"""TODO tools for Nymeria - manage autonomous task lists.

TODOs are the primary driver for autonomous operation. Active TODOs are
automatically injected into the system prompt, so Nymeria always knows
what tasks need attention.

Scheduling is now integrated into TODOs via the `scheduled_for` field.
When a TODO has a scheduled time, Nymeria wakes up to work on it.
"""

import logging
from datetime import datetime
from typing import Annotated, List, Optional, Union

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.activity_log import ActivityType, log_activity
from ..core.time_utils import parse_deadline, parse_scheduled_time, get_user_tz
from ..core.todo_constants import STATUS_ICONS, PRIORITY_MARKERS, STATUS_ORDER, PRIORITY_ORDER, PERMANENT_MARKER, PERMANENT_REJECT_MSG
from ..core.todo_manager import TodoManager, TodoPriority, TodoStatus
from ..core.watchdog import get_watchdog
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
_parse_deadline = parse_deadline
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
    priority = PRIORITY_MARKERS.get(item.priority, "") if item.priority else ""

    line = f"{icon} [{item.id}] {priority}{item.task}"

    if item.permanent:
        line += f" {PERMANENT_MARKER}"

    if item.scheduled_for:
        display_time = item.scheduled_for.astimezone(get_user_tz())
        line += f" [scheduled: {display_time.strftime('%Y-%m-%d %H:%M')}]"

    if item.deadline:
        line += f" (due: {item.deadline.strftime('%Y-%m-%d')})"

    if item.status == TodoStatus.BLOCKED and item.blocked_reason:
        line += f" - BLOCKED: {item.blocked_reason}"

    if show_notes and item.notes:
        line += f"\n    Notes: {item.notes}"

    return line


# Valid recurrence patterns
VALID_RECURRENCES = ['5min', '10min', '15min', '30min', 'hourly', 'daily', 'weekly', 'monthly']


@tool
def todo_add(
    task: Union[str, List[dict]],
    priority: Optional[str] = None,
    deadline: Optional[str] = None,
    scheduled_for: Optional[str] = None,
    recurrence: Optional[str] = None,
    permanent: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Add TODO item(s). Use scheduled_for to auto-wake at that time.

    Args:
        task: Task string, or list of dicts with task/priority/deadline/scheduled_for/recurrence/permanent
        priority: "high", "medium", or "low"
        deadline: Due date (YYYY-MM-DD)
        scheduled_for: "30s", "5m", "1h", "1d" or "YYYY-MM-DD HH:MM"
        recurrence: "5min", "10min", "15min", "30min", "hourly", "daily", "weekly", "monthly"
        permanent: If True, task cannot be completed (requires recurrence)
    """
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)
    manager = _get_todo_manager()
    schedule_db = _get_schedule_db()

    # Handle batch mode
    if isinstance(task, list):
        return _todo_add_batch(task, user_id, thread_id, manager, schedule_db)

    # Single task mode
    logger.info(f"todo_add called: task={task[:50]}")

    # Parse priority
    todo_priority = None
    if priority:
        try:
            todo_priority = TodoPriority(priority.lower())
        except ValueError:
            return f"[Error]: Invalid priority '{priority}'. Use 'high', 'medium', or 'low'."

    # Parse deadline
    todo_deadline = None
    if deadline:
        todo_deadline = _parse_deadline(deadline)
        if not todo_deadline:
            return f"[Error]: Invalid deadline format '{deadline}'. Use YYYY-MM-DD format."

    # Parse scheduled_for
    todo_scheduled = None
    if scheduled_for:
        todo_scheduled = _parse_scheduled_for(scheduled_for)
        if not todo_scheduled:
            return f"[Error]: Invalid scheduled_for format '{scheduled_for}'. Use '30s', '5m', '1h', '1d' or 'YYYY-MM-DD HH:MM'."

    # Validate recurrence
    todo_recurrence = None
    if recurrence:
        if recurrence.lower() not in VALID_RECURRENCES:
            return f"[Error]: Invalid recurrence '{recurrence}'. Use: {', '.join(VALID_RECURRENCES)}"
        todo_recurrence = recurrence.lower()

    # Validate permanent requires recurrence
    if permanent and not todo_recurrence:
        return "[Error]: permanent=True requires a recurrence pattern to be set."

    # Use atomic update to prevent race conditions
    with manager.atomic_update(user_id) as todo_list:
        item = todo_list.add_item(
            task,
            priority=todo_priority,
            deadline=todo_deadline,
            scheduled_for=todo_scheduled,
            thread_id=thread_id if todo_scheduled else None,
            recurrence=todo_recurrence,
            permanent=permanent,
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
            metadata = {"todo_id": item.id, "priority": priority}
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
            if permanent:
                result += " (permanent)"
            return result
        else:
            return f"[Error]: TODO limit reached ({todo_list.MAX_TODOS} active items). Complete or delete some tasks first."


def _todo_add_batch(
    tasks: List[dict],
    user_id: str,
    thread_id: str,
    manager: TodoManager,
    schedule_db,
) -> str:
    """Handle batch TODO creation."""
    if not tasks:
        return "[Error]: Empty task list provided."

    if len(tasks) > 20:
        return "[Error]: Maximum 20 TODOs per batch."

    results = []
    errors = []

    with manager.atomic_update(user_id) as todo_list:
        for i, spec in enumerate(tasks):
            if not isinstance(spec, dict):
                errors.append(f"Item {i+1}: Must be a dictionary")
                continue

            task_text = spec.get("task")
            if not task_text:
                errors.append(f"Item {i+1}: Missing 'task' field")
                continue

            # Parse priority
            todo_priority = None
            if spec.get("priority"):
                try:
                    todo_priority = TodoPriority(spec["priority"].lower())
                except ValueError:
                    errors.append(f"Item {i+1}: Invalid priority '{spec['priority']}'")
                    continue

            # Parse deadline
            todo_deadline = None
            if spec.get("deadline"):
                todo_deadline = _parse_deadline(spec["deadline"])
                if not todo_deadline:
                    errors.append(f"Item {i+1}: Invalid deadline '{spec['deadline']}'")
                    continue

            # Parse scheduled_for
            todo_scheduled = None
            if spec.get("scheduled_for"):
                todo_scheduled = _parse_scheduled_for(spec["scheduled_for"])
                if not todo_scheduled:
                    errors.append(f"Item {i+1}: Invalid scheduled_for '{spec['scheduled_for']}'")
                    continue

            # Parse recurrence
            todo_recurrence = None
            if spec.get("recurrence"):
                if spec["recurrence"].lower() not in VALID_RECURRENCES:
                    errors.append(f"Item {i+1}: Invalid recurrence '{spec['recurrence']}'")
                    continue
                todo_recurrence = spec["recurrence"].lower()

            # Parse permanent
            todo_permanent = bool(spec.get("permanent", False))
            if todo_permanent and not todo_recurrence:
                errors.append(f"Item {i+1}: permanent=True requires recurrence")
                continue

            # Add the item
            item = todo_list.add_item(
                task_text,
                priority=todo_priority,
                deadline=todo_deadline,
                scheduled_for=todo_scheduled,
                thread_id=thread_id if todo_scheduled else None,
                recurrence=todo_recurrence,
                permanent=todo_permanent,
            )

            if item:
                results.append(item)

                # Sync to schedule database if scheduled
                if todo_scheduled and schedule_db:
                    schedule_db.add_scheduled(
                        todo_id=item.id,
                        user_id=user_id,
                        scheduled_for=todo_scheduled,
                        task_preview=task_text[:100],
                        thread_id=thread_id,
                    )

                # Log activity
                log_activity(
                    ActivityType.TODO_ADDED,
                    f"TODO added: {task_text[:60]}",
                    user_id=user_id,
                    metadata={"todo_id": item.id, "batch": True},
                )
            else:
                errors.append(f"Item {i+1}: TODO limit reached")
                break

    # Build response
    if results:
        lines = [f"[Added]: {len(results)} TODO(s) created:"]
        for item in results:
            scheduled_info = ""
            if item.scheduled_for:
                scheduled_info = f" (scheduled)"
            lines.append(f"  - {item.id}: {item.task[:50]}{scheduled_info}")

        if errors:
            lines.append(f"\n[Errors]: {len(errors)} failed:")
            for err in errors[:5]:
                lines.append(f"  - {err}")

        return "\n".join(lines)
    else:
        return f"[Error]: No TODOs created. Errors: {'; '.join(errors[:5])}"


@tool
def todo_update(
    todo_id: str,
    task: Optional[str] = None,
    status: Optional[str] = None,
    notes: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    priority: Optional[str] = None,
    scheduled_for: Optional[str] = None,
    clear_schedule: bool = False,
    recurrence: Optional[str] = None,
    clear_recurrence: bool = False,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Update a TODO item.

    Args:
        todo_id: 8-char TODO ID
        task: New task description
        status: "pending", "in_progress", "done", or "blocked"
        notes: Add notes (max 1000 chars)
        blocked_reason: Why blocked (auto-sets status to blocked)
        priority: "high", "medium", or "low"
        scheduled_for: "30s", "5m", "1h", "1d" or "YYYY-MM-DD HH:MM"
        clear_schedule: Remove scheduled time
        recurrence: "5min", "10min", "15min", "30min", "hourly", "daily", "weekly", "monthly"
        clear_recurrence: Remove recurrence pattern
    """
    logger.info(f"todo_update called: id={todo_id}")

    user_id = get_user_id(config)
    thread_id = get_thread_id(config)
    manager = _get_todo_manager()
    schedule_db = _get_schedule_db()

    # Parse status
    todo_status = None
    if status:
        try:
            todo_status = TodoStatus(status.lower())
        except ValueError:
            return f"[Error]: Invalid status '{status}'. Use 'pending', 'in_progress', 'done', or 'blocked'."

    # Parse priority
    todo_priority = None
    if priority:
        try:
            todo_priority = TodoPriority(priority.lower())
        except ValueError:
            return f"[Error]: Invalid priority '{priority}'. Use 'high', 'medium', or 'low'."

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
    with manager.atomic_update(user_id) as todo_list:
        # Check permanent guard before update
        item_check = todo_list.get_item(todo_id)
        if item_check and todo_status == TodoStatus.DONE and item_check.permanent:
            return f"[Error]: {PERMANENT_REJECT_MSG}"

        success = todo_list.update_item(
            todo_id,
            task=task,
            status=todo_status,
            notes=notes,
            blocked_reason=blocked_reason,
            priority=todo_priority,
            scheduled_for=todo_scheduled,
            clear_schedule=clear_schedule,
            thread_id=thread_id if todo_scheduled else None,
            recurrence=todo_recurrence,
            clear_recurrence=clear_recurrence,
        )
        if success:
            item = todo_list.get_item(todo_id)
            logger.info(f"TODO updated for user {user_id}: {todo_id}")

            # Clear watchdog nudge tracking on status change to done
            if todo_status == TodoStatus.DONE:
                watchdog = get_watchdog()
                if watchdog:
                    watchdog.clear_nudge_tracking(user_id, todo_id)

            # Sync to schedule database
            if schedule_db:
                if clear_schedule or todo_status == TodoStatus.DONE:
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
            log_activity(
                ActivityType.TODO_UPDATED,
                f"TODO updated: {item.task[:60]} (status: {item.status.value})",
                user_id=user_id,
                metadata=metadata,
            )

            result = f"[Updated]: TODO {todo_id} - {item.task[:50]} (status: {item.status.value})"
            if item.scheduled_for:
                result += f" (scheduled)"
            elif clear_schedule:
                result += " (schedule cleared)"
            if item.recurrence:
                result += f" (recurring: {item.recurrence})"
            elif clear_recurrence:
                result += " (recurrence cleared)"
            return result
        else:
            return f"[Error]: TODO '{todo_id}' not found. Use todo_list to see available TODOs."


def _todo_complete_internal(
    todo_id: str,
    user_id: str,
) -> str:
    """
    Internal function to mark a TODO as completed.
    Used by MCP server. For tool usage, use todo_update(status="done").

    Args:
        todo_id: 8-char TODO ID
        user_id: User ID
    """
    logger.info(f"_todo_complete_internal called: id={todo_id}, user={user_id}")

    manager = _get_todo_manager()
    schedule_db = _get_schedule_db()

    # Use atomic update to prevent race conditions
    with manager.atomic_update(user_id) as todo_list:
        item = todo_list.get_item(todo_id)
        if not item:
            return f"[Error]: TODO '{todo_id}' not found. Use todo_list to see available TODOs."

        # Check permanent guard
        if item.permanent:
            return f"[Error]: {PERMANENT_REJECT_MSG}"

        task_name = item.task
        success = todo_list.complete_item(todo_id)
        if success:
            logger.info(f"TODO completed for user {user_id}: {todo_id}")

            # Clear watchdog nudge tracking
            watchdog = get_watchdog()
            if watchdog:
                watchdog.clear_nudge_tracking(user_id, todo_id)

            # Remove from schedule database
            if schedule_db:
                schedule_db.remove_scheduled(todo_id)

            # Log activity
            log_activity(
                ActivityType.TODO_COMPLETED,
                f"TODO completed: {task_name[:80]}",
                user_id=user_id,
                metadata={"todo_id": todo_id},
            )
            return f"[Completed]: {task_name[:100]}"
        else:
            return f"[Error]: Failed to complete TODO '{todo_id}'."


@tool
def todo_delete(
    todo_id: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    Delete a TODO permanently (no archive). Cancels any scheduled execution.

    Args:
        todo_id: 8-char TODO ID
    """
    logger.info(f"todo_delete called: id={todo_id}")

    user_id = get_user_id(config)
    manager = _get_todo_manager()
    schedule_db = _get_schedule_db()

    # Use atomic update to prevent race conditions
    with manager.atomic_update(user_id) as todo_list:
        deleted = todo_list.delete_item(todo_id)
        if deleted:
            logger.info(f"TODO deleted for user {user_id}: {todo_id}")

            # Clear watchdog nudge tracking
            watchdog = get_watchdog()
            if watchdog:
                watchdog.clear_nudge_tracking(user_id, todo_id)

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
            return f"[Error]: TODO '{todo_id}' not found. Use todo_list to see available TODOs."


@tool
def todo_list(
    filter_status: Optional[str] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """
    List TODO items. Shows active (non-done) by default.

    Args:
        filter_status: "pending", "in_progress", "blocked", "done", or "all"
    """
    logger.info(f"todo_list called: filter={filter_status}")

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
            return f"[Error]: Invalid status filter '{filter_status}'. Use 'pending', 'in_progress', 'blocked', 'done', or 'all'."
    else:
        # Default: active (non-done) items
        items = todo_list_obj.get_active_todos()

    if not items:
        if filter_status:
            return f"[Info]: No TODOs with status '{filter_status}'."
        return "[Info]: No active TODOs. Use todo_add to create tasks."

    # Sort: in_progress first, then by priority (high > medium > low > none), then by created_at
    sorted_items = sorted(
        items,
        key=lambda i: (STATUS_ORDER.get(i.status, 4), PRIORITY_ORDER.get(i.priority, 3), i.created_at),
    )

    # Format output
    lines = [f"TODO List ({len(items)} item{'s' if len(items) != 1 else ''}):"]
    lines.append("")

    for item in sorted_items:
        lines.append(_format_todo_item(item, show_notes=True))

    return "\n".join(lines)


# Export TODO tools
# Note: todo_complete removed - use todo_update(status="done") instead
TODO_TOOLS = [
    todo_add,
    todo_update,
    todo_delete,
    todo_list,
]
