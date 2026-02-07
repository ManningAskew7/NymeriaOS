"""Migration utilities for Nymeria.

Contains one-time migration functions for upgrading between versions.
This module can be removed once migration period is complete.
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._deprecated.task_db import TaskDatabase
    from .todo_manager import TodoManager
    from .todo_schedule_db import TodoScheduleDB

logger = logging.getLogger(__name__)


def migrate_old_scheduled_tasks(
    task_db: "TaskDatabase",
    todo_manager: "TodoManager",
    schedule_db: "TodoScheduleDB",
) -> int:
    """
    Migrate old self_invoke tasks to scheduled TODOs.

    This is a one-time migration for users upgrading from the old
    self_invoke system to the new TODO-based scheduling.

    Args:
        task_db: Old task database
        todo_manager: New TODO manager
        schedule_db: New schedule database

    Returns:
        Number of tasks migrated
    """
    from ._deprecated.task_db import TaskStatus

    try:
        # Get all pending tasks from old system
        pending_tasks = task_db.get_due_tasks(before_timestamp=float("inf"))
        pending_tasks = [t for t in pending_tasks if t.status == TaskStatus.PENDING]

        if not pending_tasks:
            return 0

        migrated = 0
        for task in pending_tasks:
            # Create a TODO from the task
            with todo_manager.atomic_update(task.user_id) as todo_list:
                scheduled_for = datetime.fromtimestamp(task.execute_at)
                item = todo_list.add_item(
                    task=task.prompt[:500],
                    scheduled_for=scheduled_for,
                    thread_id=task.thread_id,
                )

                if item:
                    # Add to schedule DB
                    schedule_db.add_scheduled(
                        todo_id=item.id,
                        user_id=task.user_id,
                        scheduled_for=scheduled_for,
                        task_preview=task.prompt[:100],
                        thread_id=task.thread_id,
                    )

                    # Mark old task as migrated (cancelled)
                    task_db.update_status(
                        task.id,
                        TaskStatus.CANCELLED,
                        result="Migrated to TODO system",
                    )
                    migrated += 1
                    logger.info(f"Migrated task {task.id} to TODO {item.id}")

        if migrated > 0:
            logger.info(f"Migrated {migrated} scheduled task(s) to TODO system")

        return migrated

    except Exception as e:
        logger.error(f"Error during task migration: {e}")
        return 0
