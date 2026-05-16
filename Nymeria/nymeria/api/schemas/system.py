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


class ReportRequest(BaseModel):
    """Request model for error report endpoint."""

    thread_id: str | None = None
    message_id: str = ""
    description: str = ""
    messages: list[dict] = Field(default_factory=list)
    timestamp: str = ""
    client_info: dict = Field(default_factory=dict)
