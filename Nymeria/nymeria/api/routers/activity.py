"""Activity and notification routes."""

from collections.abc import Callable
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ...core.accounts import AuthenticatedUser
from ...core.activity_log import ActivityType, get_activity_log
from ...core.notifications import get_notification_store
from ..schemas.dashboard import (
    ActivityEntryResponse,
    ActivityLogResponse,
    NotificationResponse,
    NotificationsListResponse,
)


def create_activity_router(
    verify_api_key: Callable[..., Any],
    authed_user_id: Callable[..., Any],
    get_settings_fn: Callable[[], Any],
) -> APIRouter:
    """Create the activity/notification router with app dependencies injected."""
    router = APIRouter(tags=["Dashboard"])

    @router.get("/activity", response_model=ActivityLogResponse)
    async def get_activity(
        user_id: str = Depends(authed_user_id),
        limit: int = Query(default=50, le=100, description="Max entries to return"),
        activity_type: Optional[str] = Query(default=None, description="Filter by type"),
        thread_id: Optional[str] = Query(default=None, description="Filter by thread ID"),
        user: AuthenticatedUser = Depends(verify_api_key),
        _settings: Any = Depends(get_settings_fn),
    ):
        """
        Get activity log for a user.

        Returns recent activity entries, newest first.
        Optionally filter by thread_id.
        """
        activity_log = get_activity_log()

        type_filter = None
        if activity_type:
            try:
                type_filter = ActivityType(activity_type)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid activity type '{activity_type}'",
                ) from exc

        entries = activity_log.get_entries(
            user_id, limit=limit, activity_type=type_filter, thread_id=thread_id
        )

        return ActivityLogResponse(
            entries=[
                ActivityEntryResponse(
                    id=entry.id,
                    timestamp=entry.timestamp,
                    type=entry.type.value,
                    message=entry.message,
                    thread_id=entry.thread_id,
                    metadata=entry.metadata,
                )
                for entry in entries
            ],
            total=len(entries),
        )

    @router.get("/notifications", response_model=NotificationsListResponse)
    async def get_notifications(
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        _settings: Any = Depends(get_settings_fn),
        limit: int = Query(default=100, le=200, description="Max notifications to return"),
    ):
        """
        Get notifications for a user.

        Returns the most recent notifications (up to ``limit``) with the
        unread count for the bell badge. The in-app feed doubles as an audit
        log of every notify call so external-only sends still appear here.
        """
        store = get_notification_store()
        notifications = store.get_all(user_id, limit=limit)
        unread_count = store.get_unread_count(user_id)

        return NotificationsListResponse(
            notifications=[
                NotificationResponse(
                    id=n.id,
                    summary=n.summary,
                    thread_id=n.thread_id,
                    task_id=n.task_id,
                    created_at=n.created_at,
                    read=n.read,
                    profile=getattr(n, "profile", None),
                    attempted=list(getattr(n, "attempted", []) or []),
                    delivered_to=list(getattr(n, "delivered_to", []) or []),
                    errors=dict(getattr(n, "errors", {}) or {}),
                )
                for n in notifications
            ],
            unread_count=unread_count,
        )

    @router.post("/notifications/{notification_id}/read")
    async def mark_notification_read(
        notification_id: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        _settings: Any = Depends(get_settings_fn),
    ):
        """Mark a notification as read."""
        store = get_notification_store()
        success = store.mark_read(notification_id, user_id)

        if not success:
            raise HTTPException(
                status_code=404,
                detail=f"Notification '{notification_id}' not found",
            )

        return {"status": "ok", "notification_id": notification_id}

    @router.post("/notifications/read-all")
    async def mark_all_notifications_read(
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        _settings: Any = Depends(get_settings_fn),
    ):
        """Mark all notifications as read for a user."""
        store = get_notification_store()
        count = store.mark_all_read(user_id)

        return {"status": "ok", "marked_read": count}

    @router.delete("/notifications/{notification_id}")
    async def delete_notification(
        notification_id: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        _settings: Any = Depends(get_settings_fn),
    ):
        """Permanently delete a single notification from the in-app feed."""
        store = get_notification_store()
        if not store.delete(notification_id, user_id):
            raise HTTPException(
                status_code=404,
                detail=f"Notification '{notification_id}' not found",
            )
        return {"status": "ok", "notification_id": notification_id}

    @router.delete("/notifications")
    async def clear_notifications(
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        _settings: Any = Depends(get_settings_fn),
    ):
        """Permanently clear all notifications for the user."""
        store = get_notification_store()
        store.clear(user_id)
        return {"status": "ok"}

    return router
