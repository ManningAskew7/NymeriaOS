"""TODO dashboard API schemas."""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class TodoItemResponse(BaseModel):
    """Response model for a single TODO item."""

    id: str
    task: str
    status: str
    created_at: datetime
    updated_at: datetime
    notes: Optional[str] = None
    scheduled_for: Optional[datetime] = None
    thread_id: Optional[str] = None
    last_execution: Optional[datetime] = None
    created_by: str = "agent"
    recurrence: Optional[str] = None
    workflow_id: Optional[str] = None
    workflow_params: Optional[dict] = None
    # Recurring-failure policy state (#154): consecutive failed occurrences
    # (any success resets), the most recent failure, and the auto-pause
    # marker (recurrence kept; a new scheduled_for resumes and clears).
    consecutive_failures: int = 0
    last_failure: Optional[str] = None
    last_failure_at: Optional[datetime] = None
    schedule_paused_at: Optional[datetime] = None
    # Delivery-failure state (#247): consecutive occurrences whose output a
    # chat bot could not deliver (bot delivery reports; a delivered report
    # resets). Parallel to the #154 trio, same pause marker.
    delivery_failures: int = 0
    last_delivery_failure: Optional[str] = None
    last_delivery_failure_at: Optional[datetime] = None


class TodoCreateRequest(BaseModel):
    """Request model for creating a new TODO."""

    task: str = Field(..., min_length=1, max_length=500, description="Task description")
    notes: Optional[str] = Field(
        default=None, max_length=1000, description="Additional notes"
    )
    scheduled_for: Optional[str] = Field(
        default=None,
        description="When to execute: relative ('45s', '17m', '2h', '1w') or absolute/ISO datetime",
    )
    recurrence: Optional[str] = Field(
        default=None,
        description=(
            "Recurrence interval as a duration string (Nm, Nh, Nd, Nw, Nmo; "
            "or Ns with a 60s minimum). Examples: '5m', '2h', '1d', '1mo'. "
            "Calendar months (Nmo) advance by calendar arithmetic so a TODO "
            "anchored on the 31st clamps to the last day of shorter months. "
            "Legacy names also accepted: hourly, daily, weekly, monthly. "
            "Stored in canonical form."
        ),
    )
    thread_id: Optional[str] = Field(
        default=None, description="Thread ID for scheduled execution output"
    )
    workflow_id: Optional[str] = Field(
        default=None,
        description=(
            "Published workflow tool the ticker runs headlessly at the "
            "scheduled time instead of an agent turn (create-only)"
        ),
    )
    workflow_params: Optional[dict] = Field(
        default=None, description="Parameters bound to the scheduled workflow run"
    )


class TodoUpdateRequest(BaseModel):
    """Request model for updating a TODO."""

    task: Optional[str] = Field(default=None, max_length=500, description="Task description")
    status: Optional[str] = Field(
        default=None, description="Status: pending, in_progress, done"
    )
    notes: Optional[str] = Field(
        default=None, max_length=1000, description="Additional notes"
    )
    scheduled_for: Optional[str] = Field(
        default=None,
        description="When to execute: relative ('45s', '17m', '2h', '1w') or absolute/ISO datetime",
    )
    recurrence: Optional[str] = Field(
        default=None,
        description=(
            "Recurrence interval as a duration string (Nm, Nh, Nd, Nw, Nmo; "
            "or Ns with a 60s minimum). Examples: '5m', '2h', '1d', '1mo'. "
            "Calendar months (Nmo) advance by calendar arithmetic so a TODO "
            "anchored on the 31st clamps to the last day of shorter months. "
            "Legacy names also accepted: hourly, daily, weekly, monthly. "
            "Stored in canonical form."
        ),
    )
    thread_id: Optional[str] = Field(
        default=None, description="Thread ID for scheduled execution output"
    )
    clear_schedule: bool = Field(default=False, description="Clear the schedule")
    clear_recurrence: bool = Field(default=False, description="Clear the recurrence")


class TodoDeliveryReportRequest(BaseModel):
    """A chat bot's delivery outcome for one scheduled TODO turn (#247).

    Filed by the Telegram/Discord bots after they finish (or fail) sending
    an autonomous TODO turn's output; drives the delivery-failure
    accounting in ``core/delivery_accounting.py``.
    """

    outcome: Literal["delivered", "partial", "failed"] = Field(
        ...,
        description=(
            "delivered: all sends landed; partial: some sends failed; "
            "failed: nothing reached the chat"
        ),
    )
    platform: str = Field(
        ..., min_length=1, max_length=40, description="e.g. 'telegram'"
    )
    target: str = Field(
        default="",
        max_length=120,
        description="Human-readable destination, e.g. 'chat 5551234567'",
    )
    error: Optional[str] = Field(
        default=None, max_length=500, description="First send error observed"
    )
    thread_id: Optional[str] = Field(
        default=None, description="Thread the turn executed in"
    )


class TodoDeliveryReportResponse(BaseModel):
    """What the delivery accounting did with one report."""

    outcome: str
    delivery_failures: int
    alerted: bool
    paused: bool


class TodoListResponse(BaseModel):
    """Response model for TODO list."""

    user_id: str
    items: List[TodoItemResponse]
    total: int
