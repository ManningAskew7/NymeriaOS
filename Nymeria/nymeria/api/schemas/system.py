"""System endpoint schemas."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Response model for health check."""

    status: str = "ok"
    version: str = "1.0.0"
