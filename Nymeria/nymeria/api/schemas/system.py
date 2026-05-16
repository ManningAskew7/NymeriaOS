"""System endpoint schemas."""

from pydantic import BaseModel, Field

from ... import __version__


class HealthResponse(BaseModel):
    """Response model for health check."""

    status: str = "ok"
    version: str = __version__


class ReportRequest(BaseModel):
    """Request model for error report endpoint."""

    thread_id: str | None = None
    message_id: str = ""
    description: str = ""
    messages: list[dict] = Field(default_factory=list)
    timestamp: str = ""
    client_info: dict = Field(default_factory=dict)
