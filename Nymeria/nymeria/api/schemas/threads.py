"""Thread read/list/metadata/lifecycle API schemas."""

from typing import Any

from pydantic import BaseModel, Field


class ThreadHistoryResponse(BaseModel):
    """Response model for conversation history."""

    thread_id: str
    messages: list[Any]


class ThreadMetadataUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    pinned: bool | None = None


class ThreadMetadataMigrateRequest(BaseModel):
    threads: list[dict[str, Any]] = Field(default_factory=list)
