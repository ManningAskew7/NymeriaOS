"""Classic tool API schemas."""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class DefaultToolsUpdateRequest(BaseModel):
    tool_names: list = Field(
        ...,
        description="Tool names to enable by default for new threads",
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


class ToolSearchResponse(BaseModel):
    query: str
    mode: Literal["semantic", "bm25", "fuzzy", "substring"]
    warning: Optional[str] = None
    results: list[ToolSearchResultResponse]
