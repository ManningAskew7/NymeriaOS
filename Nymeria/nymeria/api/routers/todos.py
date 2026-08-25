"""TODO dashboard routes."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ...config import Settings
from ...core.accounts import AuthenticatedUser
from ...core.time_utils import ensure_aware_utc, parse_future_scheduled_time, utc_now
from ...core.todo_constants import (
    STATUS_ORDER,
    compute_recurrence_reschedule,
    validate_recurrence,
)
from ...core.todo_manager import TodoItem, TodoList, TodoManager, TodoStatus
from ...core.todo_schedule_db import TodoScheduleDB
from ..schemas.todos import (
    TodoCreateRequest,
    TodoDeliveryReportRequest,
    TodoDeliveryReportResponse,
    TodoItemResponse,
    TodoListResponse,
    TodoUpdateRequest,
)

logger = logging.getLogger(__name__)


def _parse_scheduled_for(scheduled_for: Optional[str]) -> Optional[datetime]:
    """
    Parse scheduled_for string to a timezone-aware UTC datetime.

    Supports:
    - Relative times: "45s", "17m", "2h", "1d", "1w"
    - Absolute local times: "2024-01-01 12:34", "2024-01-01T12:34"
    - ISO datetime with timezone: "2024-01-01T12:34:00Z"
    """
    if not scheduled_for:
        return None

    logger.debug(f"[API] Parsing scheduled_for: '{scheduled_for}'")
    try:
        result = parse_future_scheduled_time(scheduled_for)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.debug(
        f"[API] Parsed scheduled_for '{scheduled_for}' -> {result} (UTC), timestamp={result.timestamp()}"
    )
    return result


def _recurrence_anchor(item: TodoItem | None) -> datetime:
    if item and item.scheduled_for:
        return ensure_aware_utc(item.scheduled_for)
    return utc_now()


def _reschedule_recurring_done(
    todo_list: TodoList,
    item: TodoItem,
    todo_id: str,
) -> None:
    """Roll a just-completed recurring TODO forward to its next occurrence.

    ``item`` is the live (in-list) TODO that was marked done; when it carries a
    recurrence and a future slot exists, it is rescheduled to that slot as a
    PENDING item and the completion anchor is recorded in ``last_execution``.
    No-op when there is no recurrence or no future slot. ``update_item`` mutates
    the same object ``item`` references, so the caller's reference is updated in
    place.
    """
    if not item.recurrence:
        return
    if item.schedule_paused_at is not None:
        # Auto-paused by the recurring-failure policy (#154): "done" must
        # not silently resume the schedule (the reschedule below would set
        # scheduled_for, and update_item's resume-clear would erase the
        # pause marker and the failure streak). The TODO completes as done
        # with the pause intact; resume stays an explicit reschedule.
        return
    recurrence_anchor = _recurrence_anchor(item)
    next_execution, origin_to_persist = compute_recurrence_reschedule(
        item.recurrence, recurrence_anchor, item.recurrence_anchor
    )
    if next_execution is None:
        return
    todo_list.update_item(
        todo_id,
        scheduled_for=next_execution,
        status=TodoStatus.PENDING,
    )
    item.last_execution = recurrence_anchor
    if origin_to_persist is not None:
        item.recurrence_anchor = origin_to_persist
    logger.debug(
        f"[API] Auto-rescheduled recurring TODO {todo_id} "
        f"from anchor {recurrence_anchor} for {next_execution}"
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
        workflow_id=item.workflow_id,
        workflow_params=item.workflow_params,
        consecutive_failures=item.consecutive_failures,
        last_failure=item.last_failure,
        last_failure_at=item.last_failure_at,
        schedule_paused_at=item.schedule_paused_at,
        delivery_failures=item.delivery_failures,
        last_delivery_failure=item.last_delivery_failure,
        last_delivery_failure_at=item.last_delivery_failure_at,
    )


def _get_todo_schedule_db(settings: Settings) -> TodoScheduleDB:
    """Create the schedule DB handle used by TODO routes."""
    return TodoScheduleDB(settings.data_dir / "todo_schedule.db")


def _raise_if_todo_executing(
    schedule_db: TodoScheduleDB,
    todo_id: str,
    user_id: str,
    settings: Settings,
) -> None:
    """Reject user-facing TODO writes while a scheduled run owns the TODO."""
    stale_after_seconds = int(
        getattr(settings, "scheduler_active_execution_stale_minutes", 1440)
    ) * 60
    if schedule_db.is_execution_active(
        todo_id,
        user_id,
        stale_after_seconds=stale_after_seconds,
    ):
        raise HTTPException(
            status_code=409,
            detail=f"TODO '{todo_id}' is currently executing; try again after the run finishes.",
        )


def create_todos_router(
    verify_api_key: Callable[..., Any],
    require_admin_user: Callable[..., Any],
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
        user: AuthenticatedUser = Depends(require_admin_user),
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

        canonical_recurrence: Optional[str] = None
        if request.recurrence:
            try:
                canonical_recurrence = validate_recurrence(request.recurrence)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Parse scheduled_for
        scheduled_for = _parse_scheduled_for(request.scheduled_for)

        # Default thread_id from request, fallback to user-scoped default
        todo_thread_id = request.thread_id or f"default-{user_id}"

        workflow_id = (request.workflow_id or "").strip() or None
        if workflow_id:
            from ...core.workflows.tool_runtime import workflow_binding_error

            binding_error = workflow_binding_error(
                workflow_id, request.workflow_params or {}, allow_event=False
            )
            if binding_error:
                raise HTTPException(status_code=400, detail=binding_error)

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.add_item(
                task=request.task,
                notes=request.notes,
                scheduled_for=scheduled_for,
                thread_id=todo_thread_id,
                created_by="user",
                recurrence=canonical_recurrence,
                workflow_id=workflow_id,
                workflow_params=request.workflow_params if workflow_id else None,
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
            logger.debug(
                f"[API] TODO {created_item.id} has scheduled_for={created_item.scheduled_for}, syncing to schedule DB"
            )
            schedule_db = _get_todo_schedule_db(settings)
            todo_manager.sync_schedule_to_db(user_id, created_item.id, schedule_db)
            logger.debug(f"[API] Schedule synced for TODO {created_item.id}")

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

        recurrence: Optional[str] = None
        if request.recurrence and not request.clear_recurrence:
            try:
                recurrence = validate_recurrence(request.recurrence)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        _raise_if_todo_executing(schedule_db, todo_id, user_id, settings)

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found",
                )

            _raise_if_todo_executing(schedule_db, todo_id, user_id, settings)

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
                if updated:
                    _reschedule_recurring_done(todo_list, updated, todo_id)

            # Re-fetch the updated item (still in memory)
            updated_item = todo_list.get_item(todo_id)

        if updated_item is None:
            raise HTTPException(
                status_code=404,
                detail=f"TODO '{todo_id}' not found after update",
            )
        # Sync schedule AFTER atomic_update saves the file
        # (sync_schedule_to_db reads from disk, so file must be saved first)
        todo_manager.sync_schedule_to_db(user_id, updated_item.id, schedule_db)
        logger.debug(f"[API] Schedule synced for TODO {updated_item.id}")

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
        _raise_if_todo_executing(schedule_db, todo_id, user_id, settings)

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found",
                )

            _raise_if_todo_executing(schedule_db, todo_id, user_id, settings)

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
        _raise_if_todo_executing(schedule_db, todo_id, user_id, settings)

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found",
                )

            _raise_if_todo_executing(schedule_db, todo_id, user_id, settings)

            has_recurrence = item.recurrence
            success = todo_list.complete_item(todo_id)
            if not success:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found",
                )

            # Auto-reschedule recurring TODOs
            if has_recurrence:
                _reschedule_recurring_done(todo_list, item, todo_id)

            # Re-fetch the updated item
            item = todo_list.get_item(todo_id)
            if item is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found after completion",
                )

            # Sync schedule
            if has_recurrence and item.scheduled_for:
                todo_manager.sync_schedule_to_db(user_id, todo_id, schedule_db)
            else:
                schedule_db.remove_scheduled(todo_id)

            return _todo_to_response(item)

    @router.post(
        "/todos/{todo_id}/delivery-report",
        response_model=TodoDeliveryReportResponse,
    )
    async def report_todo_delivery(
        todo_id: str,
        request: TodoDeliveryReportRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings_fn),
    ):
        """Record a chat bot's delivery outcome for a TODO turn (#247).

        Admin-only: bots call with the service token, no Act-As. The TODO's
        owner is resolved server-side (the bot delivering an autonomous turn
        knows the chat, not necessarily the account), so a caller-supplied
        user id is never trusted for the accounting.

        The whole body runs off the event loop: owner resolution scans the
        per-user TODO files, and an alert-firing report enters the
        SYNCHRONOUS notification stack (30s httpx timeout per destination),
        whose likeliest destination is the very channel that just failed.
        Same treatment as ``POST /notifications/external``.
        """
        from ...core.delivery_accounting import record_delivery_report

        todo_manager = TodoManager(settings.data_dir)

        def _resolve_and_record():
            owner_id = todo_manager.find_owner(todo_id)
            if owner_id is None:
                return None
            return record_delivery_report(
                user_id=owner_id,
                todo_id=todo_id,
                outcome=request.outcome,
                platform=request.platform,
                target=request.target,
                error=request.error,
                thread_id=request.thread_id or "",
                todo_manager=todo_manager,
                schedule_db=_get_todo_schedule_db(settings),
                settings=settings,
            )

        result = await asyncio.to_thread(_resolve_and_record)
        if result is None:
            raise HTTPException(
                status_code=404, detail=f"TODO '{todo_id}' not found"
            )
        return result

    return router
