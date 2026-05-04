"""TODO dashboard routes."""

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ...config import Settings
from ...core.accounts import AuthenticatedUser
from ...core.todo_constants import RECURRENCE_DELTAS, STATUS_ORDER, VALID_RECURRENCES
from ...core.todo_manager import TodoItem, TodoManager, TodoStatus
from ...core.todo_schedule_db import TodoScheduleDB
from ..schemas.todos import TodoCreateRequest, TodoItemResponse, TodoListResponse, TodoUpdateRequest

logger = logging.getLogger(__name__)


def _parse_scheduled_for(scheduled_for: Optional[str]) -> Optional[datetime]:
    """
    Parse scheduled_for string to a timezone-aware UTC datetime.

    Supports:
    - Relative times: "30s", "30m", "2h", "1d", "1w"
    - ISO datetime with Z suffix (e.g., "2024-01-01T12:00:00.000Z") - parsed as UTC
    - ISO datetime without Z (e.g., "2024-01-01T12:00") - interpreted as LOCAL time
    """
    if not scheduled_for:
        return None

    scheduled_for = scheduled_for.strip()
    logger.info(f"[API] Parsing scheduled_for: '{scheduled_for}'")

    # Try relative time parsing first: 30s, 5m, 1h, 1d, 1w
    match = re.match(r"^(\d+)(s|m|h|d|w)$", scheduled_for.lower())
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
        seconds = value * multipliers[unit]
        # Use timezone-aware UTC datetime to avoid timestamp() interpretation issues
        result = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        logger.info(
            f"[API] Parsed relative time '{scheduled_for}' -> {result} (UTC), timestamp={result.timestamp()}"
        )
        return result

    # Handle ISO strings with Z suffix (UTC) - from frontend toISOString()
    if scheduled_for.endswith("Z"):
        utc_formats = [
            "%Y-%m-%dT%H:%M:%S.%fZ",  # With milliseconds: 2024-01-01T12:00:00.000Z
            "%Y-%m-%dT%H:%M:%SZ",  # Without milliseconds: 2024-01-01T12:00:00Z
        ]
        for fmt in utc_formats:
            try:
                result = datetime.strptime(scheduled_for, fmt).replace(tzinfo=timezone.utc)
                logger.info(
                    f"[API] Parsed UTC time '{scheduled_for}' -> {result} (UTC), timestamp={result.timestamp()}"
                )
                return result
            except ValueError:
                continue

    # Try absolute formats - these are interpreted as LOCAL time, then converted to UTC
    formats = [
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d %H:%M:%S",
    ]

    for fmt in formats:
        try:
            # Parse as naive datetime (assumed local time from user's browser)
            local_dt = datetime.strptime(scheduled_for, fmt)
            # Convert to UTC by assuming it's in the system's local timezone
            local_dt = local_dt.astimezone()  # Add local timezone info
            result = local_dt.astimezone(timezone.utc)  # Convert to UTC
            logger.info(
                f"[API] Parsed absolute time '{scheduled_for}' -> local={local_dt}, UTC={result}, timestamp={result.timestamp()}"
            )
            return result
        except ValueError:
            continue

    raise HTTPException(
        status_code=400,
        detail=(
            f"Invalid scheduled_for format: '{scheduled_for}'. Use relative "
            "(e.g., '30m', '2h') or datetime (e.g., '2024-03-15 14:00')."
        ),
    )


def _todo_to_response(item: TodoItem) -> TodoItemResponse:
    """Convert a TodoItem to TodoItemResponse."""
    return TodoItemResponse(
        id=item.id,
        task=item.task,
        status=item.status.value,
        created_at=item.created_at,
        updated_at=item.updated_at,
        notes=item.notes,
        scheduled_for=item.scheduled_for,
        thread_id=item.thread_id,
        last_execution=item.last_execution,
        created_by=item.created_by,
        recurrence=item.recurrence,
    )


def _get_todo_schedule_db(settings: Settings) -> TodoScheduleDB:
    """Create the schedule DB handle used by TODO routes."""
    return TodoScheduleDB(settings.data_dir / "todo_schedule.db")


def _raise_if_todo_executing(
    schedule_db: TodoScheduleDB, todo_id: str, user_id: str
) -> None:
    """Reject user-facing TODO writes while a scheduled run owns the TODO."""
    if schedule_db.is_execution_active(todo_id, user_id):
        raise HTTPException(
            status_code=409,
            detail=f"TODO '{todo_id}' is currently executing; try again after the run finishes.",
        )


def create_todos_router(
    verify_api_key: Callable[..., Any],
    authed_user_id: Callable[..., Any],
    get_settings_fn: Callable[[], Any],
) -> APIRouter:
    """Create the TODO dashboard router with app dependencies injected."""
    router = APIRouter(tags=["Dashboard"])

    @router.get("/todos/thread-counts")
    async def get_thread_task_counts(
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Get active task count per thread for badge display."""
        todo_manager = TodoManager(settings.data_dir)
        todo_list = todo_manager.get_todos(user_id)
        return todo_list.get_thread_task_counts()

    @router.get("/todos/users")
    async def list_users_with_todos(
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """
        List all user IDs that have TODO lists.

        Used by the watchdog worker to discover users to scan for stale TODOs
        without having to crawl the data directory itself.
        """
        todo_manager = TodoManager(settings.data_dir)
        return todo_manager.get_all_users_with_todos()

    @router.get("/todos", response_model=TodoListResponse)
    async def get_todos(
        user_id: str = Depends(authed_user_id),
        filter_status: Optional[str] = Query(
            default=None, description="Filter by status"
        ),
        thread_id: Optional[str] = Query(default=None, description="Filter by thread ID"),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """
        Get TODO items for a user.

        Returns all active TODOs by default. Use filter_status to filter by
        specific status. Optionally filter by thread_id.
        """
        todo_manager = TodoManager(settings.data_dir)
        todo_list = todo_manager.get_todos(user_id)

        # Filter items
        if filter_status == "all":
            items = todo_list.items
        elif filter_status:
            try:
                status = TodoStatus(filter_status.lower())
                items = [i for i in todo_list.items if i.status == status]
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid status filter '{filter_status}'",
                ) from exc
        else:
            # Default: active (non-done) items
            items = todo_list.get_active_todos()

        # Filter by thread_id if provided
        if thread_id:
            items = [i for i in items if i.thread_id == thread_id]

        # Sort: in_progress first, then pending, then done; then by created_at
        sorted_items = sorted(
            items,
            key=lambda i: (STATUS_ORDER.get(i.status, 3), i.created_at),
        )

        return TodoListResponse(
            user_id=user_id,
            items=[_todo_to_response(item) for item in sorted_items],
            total=len(sorted_items),
        )

    @router.post("/todos", response_model=TodoItemResponse)
    async def create_todo(
        request: TodoCreateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """
        Create a new TODO item.

        The TODO is created by the user (created_by='user').
        """
        todo_manager = TodoManager(settings.data_dir)

        # Validate recurrence
        if request.recurrence and request.recurrence.lower() not in VALID_RECURRENCES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid recurrence: '{request.recurrence}'. "
                    f"Use: {', '.join(VALID_RECURRENCES)}"
                ),
            )

        # Parse scheduled_for
        scheduled_for = _parse_scheduled_for(request.scheduled_for)

        # Default thread_id from request, fallback to user-scoped default
        todo_thread_id = request.thread_id or f"default-{user_id}"

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.add_item(
                task=request.task,
                notes=request.notes,
                scheduled_for=scheduled_for,
                thread_id=todo_thread_id,
                created_by="user",
                recurrence=request.recurrence.lower() if request.recurrence else None,
            )

            if item is None:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot create TODO: maximum limit reached",
                )

            # Store item data for after context exits
            created_item = item

        # Sync schedule AFTER atomic_update saves the file
        # (sync_schedule_to_db reads from disk, so file must be saved first)
        if created_item.scheduled_for:
            logger.info(
                f"[API] TODO {created_item.id} has scheduled_for={created_item.scheduled_for}, syncing to schedule DB"
            )
            schedule_db = _get_todo_schedule_db(settings)
            todo_manager.sync_schedule_to_db(user_id, created_item.id, schedule_db)
            logger.info(f"[API] Schedule synced for TODO {created_item.id}")
        else:
            logger.info(f"[API] TODO {created_item.id} has no schedule, skipping sync")

        return _todo_to_response(created_item)

    @router.patch("/todos/{todo_id}", response_model=TodoItemResponse)
    async def update_todo(
        todo_id: str,
        request: TodoUpdateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Update an existing TODO item."""
        todo_manager = TodoManager(settings.data_dir)
        schedule_db = _get_todo_schedule_db(settings)

        # Parse status
        status = None
        if request.status:
            try:
                status = TodoStatus(request.status.lower())
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid status: '{request.status}'. "
                        "Use: pending, in_progress, done"
                    ),
                ) from exc

        # Validate recurrence
        recurrence = None
        if request.recurrence and not request.clear_recurrence:
            if request.recurrence.lower() not in VALID_RECURRENCES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid recurrence: '{request.recurrence}'. "
                        f"Use: {', '.join(VALID_RECURRENCES)}"
                    ),
                )
            recurrence = request.recurrence.lower()

        # Parse scheduled_for
        scheduled_for = None
        if request.scheduled_for and not request.clear_schedule:
            scheduled_for = _parse_scheduled_for(request.scheduled_for)

        existing = todo_manager.get_todos(user_id).get_item(todo_id)
        if not existing:
            raise HTTPException(
                status_code=404,
                detail=f"TODO '{todo_id}' not found",
            )
        _raise_if_todo_executing(schedule_db, todo_id, user_id)

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found",
                )

            _raise_if_todo_executing(schedule_db, todo_id, user_id)

            success = todo_list.update_item(
                todo_id=todo_id,
                task=request.task,
                status=status,
                notes=request.notes,
                scheduled_for=scheduled_for,
                clear_schedule=request.clear_schedule,
                thread_id=request.thread_id,
                recurrence=recurrence,
                clear_recurrence=request.clear_recurrence,
            )

            if not success:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found",
                )

            # Auto-reschedule recurring TODOs marked as done
            if status == TodoStatus.DONE:
                updated = todo_list.get_item(todo_id)
                if updated and updated.recurrence:
                    delta = RECURRENCE_DELTAS.get(updated.recurrence)
                    if delta:
                        next_execution = datetime.now(timezone.utc) + delta
                        todo_list.update_item(
                            todo_id,
                            scheduled_for=next_execution,
                            status=TodoStatus.PENDING,
                        )
                        updated.last_execution = datetime.now(timezone.utc)
                        logger.info(
                            f"[API] Auto-rescheduled recurring TODO {todo_id} for {next_execution}"
                        )

            # Re-fetch the updated item (still in memory)
            updated_item = todo_list.get_item(todo_id)

        # Sync schedule AFTER atomic_update saves the file
        # (sync_schedule_to_db reads from disk, so file must be saved first)
        todo_manager.sync_schedule_to_db(user_id, updated_item.id, schedule_db)
        logger.info(f"[API] Schedule synced for TODO {updated_item.id}")

        return _todo_to_response(updated_item)

    @router.delete("/todos/{todo_id}")
    async def delete_todo(
        todo_id: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Delete a TODO item."""
        todo_manager = TodoManager(settings.data_dir)
        schedule_db = _get_todo_schedule_db(settings)

        existing = todo_manager.get_todos(user_id).get_item(todo_id)
        if not existing:
            raise HTTPException(
                status_code=404,
                detail=f"TODO '{todo_id}' not found",
            )
        _raise_if_todo_executing(schedule_db, todo_id, user_id)

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found",
                )

            _raise_if_todo_executing(schedule_db, todo_id, user_id)

            deleted = todo_list.delete_item(todo_id)
            if not deleted:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found",
                )

            # Remove from schedule if it was scheduled
            schedule_db.remove_scheduled(todo_id)

            return {"status": "ok", "deleted_id": todo_id}

    @router.post("/todos/{todo_id}/complete", response_model=TodoItemResponse)
    async def complete_todo(
        todo_id: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Mark a TODO item as done."""
        todo_manager = TodoManager(settings.data_dir)
        schedule_db = _get_todo_schedule_db(settings)

        existing = todo_manager.get_todos(user_id).get_item(todo_id)
        if not existing:
            raise HTTPException(
                status_code=404,
                detail=f"TODO '{todo_id}' not found",
            )
        _raise_if_todo_executing(schedule_db, todo_id, user_id)

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found",
                )

            _raise_if_todo_executing(schedule_db, todo_id, user_id)

            has_recurrence = item.recurrence
            success = todo_list.complete_item(todo_id)
            if not success:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found",
                )

            # Auto-reschedule recurring TODOs
            if has_recurrence:
                delta = RECURRENCE_DELTAS.get(has_recurrence)
                if delta:
                    next_execution = datetime.now(timezone.utc) + delta
                    todo_list.update_item(
                        todo_id,
                        scheduled_for=next_execution,
                        status=TodoStatus.PENDING,
                    )
                    refreshed = todo_list.get_item(todo_id)
                    if refreshed:
                        refreshed.last_execution = datetime.now(timezone.utc)
                    logger.info(
                        f"[API] Auto-rescheduled recurring TODO {todo_id} for {next_execution}"
                    )

            # Re-fetch the updated item
            item = todo_list.get_item(todo_id)

            # Sync schedule
            if has_recurrence and item.scheduled_for:
                todo_manager.sync_schedule_to_db(user_id, todo_id, schedule_db)
            else:
                schedule_db.remove_scheduled(todo_id)

            return _todo_to_response(item)

    return router
