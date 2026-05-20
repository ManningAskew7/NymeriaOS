"""Dashboard activity and notification API schemas."""

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel, Field


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
    """Response model for a single notification.

    Includes audit-log fields (``profile``, ``attempted``, ``delivered_to``,
    ``errors``) so the desktop sidebar can render badges showing every
    external channel each notification reached.
    """

    id: str
    summary: str
    thread_id: Optional[str] = None
    task_id: Optional[str] = None
    created_at: datetime
    read: bool
    profile: Optional[str] = None
    attempted: List[str] = Field(default_factory=list)
    delivered_to: List[str] = Field(default_factory=list)
    errors: Dict[str, str] = Field(default_factory=dict)


class NotificationsListResponse(BaseModel):
    """Response model for notifications list."""

    notifications: List[NotificationResponse]
    unread_count: int
