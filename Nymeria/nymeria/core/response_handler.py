"""Response handler for autonomous agent execution.

Simplified version that no longer parses structured output formats.
Visibility control is now handled by the mute_response tool.
"""

import logging
from typing import Literal, Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class NymeriaResponse(BaseModel):
    """Response from autonomous agent execution.

    Attributes:
        visibility: "activity" for background/silent, "full" for shown in thread
        notify: If True, send push notification to user
        summary: Optional summary for notifications (max 100 chars)
        content: The actual response content
    """

    visibility: Literal["activity", "full"] = "full"
    notify: bool = False
    summary: Optional[str] = None
    content: str


def create_response(
    content: str,
    visibility: Literal["activity", "full"] = "full",
    notify: bool = False,
    summary: Optional[str] = None,
) -> NymeriaResponse:
    """
    Create a NymeriaResponse with the given parameters.

    This is the primary way to create response objects now that we no longer
    parse structured output. Visibility is determined by the mute_response tool.

    Args:
        content: The response content
        visibility: "activity" for background/silent, "full" for chat
        notify: Whether to send a push notification
        summary: Optional notification summary (max 100 chars)

    Returns:
        NymeriaResponse with the given parameters
    """
    # Truncate summary if too long
    if summary and len(summary) > 100:
        summary = summary[:100]

    return NymeriaResponse(
        visibility=visibility,
        notify=notify,
        summary=summary,
        content=content,
    )
