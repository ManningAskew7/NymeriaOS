"""System endpoint schemas."""

from typing import Literal

from pydantic import BaseModel, Field

from ... import __version__


class HealthResponse(BaseModel):
    """Response model for health check."""

    status: str = "ok"
    version: str = __version__


class DependencyReadiness(BaseModel):
    """Readiness state for one runtime dependency."""

    status: Literal["ok", "skipped", "error"]
    detail: str | None = None


class ReadinessResponse(BaseModel):
    """Response model for readiness checks."""

    status: Literal["ok", "error"]
    version: str = __version__
    checks: dict[str, DependencyReadiness]


class BusyThread(BaseModel):
    """One currently held thread lock (an in-flight turn)."""

    thread_id: str
    holder: str
    held_seconds: float | None = None


class TurnActivityResponse(BaseModel):
    """Live activity; all-zero counts are the restart-safe signal."""

    active_turns: int
    interactive_active: int
    # Detached background bash jobs hold no thread lock on purpose, so a
    # restart would kill them while active_turns reads 0.
    background_jobs: int = 0
    # Populated only for admin callers: thread ids and holder labels are
    # cross-user metadata; the counts alone carry the idle predicate.
    busy_threads: list[BusyThread] = Field(default_factory=list)


class ReportRequest(BaseModel):
    """Request model for error report endpoint."""

    thread_id: str | None = None
    message_id: str = ""
    description: str = ""
    messages: list[dict] = Field(default_factory=list)
    timestamp: str = ""
    client_info: dict = Field(default_factory=dict)
