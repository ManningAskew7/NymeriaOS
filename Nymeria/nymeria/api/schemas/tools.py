"""Classic tool API schemas."""

from pydantic import BaseModel, Field


class DefaultToolsUpdateRequest(BaseModel):
    tool_names: list = Field(
        ...,
        description="Tool names to enable by default for new threads",
    )
