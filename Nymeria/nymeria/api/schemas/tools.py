"""Classic tool API schemas."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class DefaultToolsUpdateRequest(BaseModel):
    tool_names: list = Field(
        ...,
        description="Tool names to enable by default for new threads",
    )
    declined_core_tools: Optional[list[str]] = Field(
        default=None,
        description=(
            "Core seed tools absent from tool_names to record as declined, so "
            "they are no longer offered as new (backlog #164, the GUI's "
            "Dismiss). A seed tool the write removes is recorded on its own."
        ),
    )


class ToolSearchResultResponse(BaseModel):
    name: str
    description: str
    category: str
    security_level: str
    tool_type: str
    is_default: bool
    status: Optional[str] = None
    score: float
    enable_hint: str
    # Two-level integration grouping (integration tools only; None otherwise).
    group: Optional[str] = None
    group_label: Optional[str] = None
    service: Optional[str] = None
    service_label: Optional[str] = None
    # Credential axis (provider-mapped tools only; None = no credential
    # required): connected / pending / needs_setup / optional.
    auth_status: Optional[str] = None
    auth_provider: Optional[str] = None


class ToolSearchResponse(BaseModel):
    query: str
    mode: Literal["semantic", "bm25", "fuzzy", "substring"]
    warning: Optional[str] = None
    results: list[ToolSearchResultResponse]
