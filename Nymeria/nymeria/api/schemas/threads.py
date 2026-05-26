"""Thread read/list/metadata/lifecycle API schemas."""

from typing import Any

from pydantic import BaseModel, Field


class ThreadHistoryResponse(BaseModel):
    """Response model for conversation history."""

    thread_id: str
    messages: list[Any]


class ThreadStatusResponse(BaseModel):
    """Lightweight thread status response for sync polling."""

    thread_id: str
    revision: str | None
    processing: bool


class ThreadOverviewResponse(BaseModel):
    """Resolved read model for one thread's header/dashboard status."""

    thread: dict[str, Any]
    status: dict[str, Any]
    context: dict[str, Any]
    config_summary: dict[str, Any]
    llm: dict[str, Any]
    callable: dict[str, Any]
    tools: dict[str, Any]
    mcp: dict[str, Any]
    skills: dict[str, Any]
    todos: dict[str, Any]
    triggers: dict[str, Any]
    chat_apps: dict[str, Any]
    user: dict[str, Any]
    section_errors: dict[str, str] = Field(default_factory=dict)


class ThreadBranchRequest(BaseModel):
    """Request model for creating a branch from an existing thread."""

    title: str | None = Field(default=None, max_length=200)
    from_message_index: int | None = Field(default=None, ge=1)


class ThreadBranchResponse(BaseModel):
    """Response model for a newly branched thread."""

    status: str
    source_thread_id: str
    thread_id: str
    title: str
    requested_title: str
    from_message_index: int | None = None
    source_checkpoint_id: str | None = None
    checkpoints: dict[str, int] = Field(default_factory=dict)
    config_cloned: bool = False
    callable_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ThreadMetadataUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    pinned: bool | None = None


class ThreadClaimRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    platform: str | None = Field(default=None, max_length=32)


class ThreadDreamRequest(BaseModel):
    """Manually trigger a dream (self-reflection) cycle for a thread.

    Bypasses the scheduler's gating fields. The configured ``dreaming.enabled``
    flag is still respected — if the thread has not opted in, the endpoint
    returns 409. Use ``force=True`` to override the opt-in check for one-off
    runs (admin/debug).
    """

    model: str | None = Field(default=None, max_length=120)
    force: bool = False


class ThreadDreamResponse(BaseModel):
    """Response from POST /threads/{id}/dream."""

    shadow_thread_id: str
    parent_thread_id: str
    started_at: str
    model: str
    enabled_optional_tools: list[str] = Field(default_factory=list)
    disabled_core_tools: list[str] = Field(default_factory=list)
