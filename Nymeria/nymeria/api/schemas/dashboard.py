"""Dashboard activity and notification API schemas."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class ActivityEntryResponse(BaseModel):
    """Response model for an activity entry."""

    id: str
    timestamp: datetime
    type: str
    message: str
    thread_id: Optional[str] = None
    metadata: Optional[dict] = None


class ActivityLogResponse(BaseModel):
    """Response model for activity log."""

    entries: List[ActivityEntryResponse]
    total: int


class NotificationResponse(BaseModel):
    """Response model for a single notification."""

    id: str
    summary: str
    thread_id: Optional[str] = None
    task_id: Optional[str] = None
    created_at: datetime
    read: bool


class NotificationsListResponse(BaseModel):
    """Response model for notifications list."""

    notifications: List[NotificationResponse]
    unread_count: int
