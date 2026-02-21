"""Response handler for autonomous agent execution."""

import logging
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class NymeriaResponse(BaseModel):
    """Response from autonomous agent execution.

    Attributes:
        notify: If True, send push notification to user
        summary: Optional summary for notifications (max 100 chars)
        content: The actual response content
    """

    notify: bool = False
    summary: Optional[str] = None
    content: str


def create_response(
    content: str,
    notify: bool = False,
    summary: Optional[str] = None,
) -> NymeriaResponse:
    """
    Create a NymeriaResponse with the given parameters.

    Args:
        content: The response content
        notify: Whether to send a push notification
        summary: Optional notification summary (max 100 chars)

    Returns:
        NymeriaResponse with the given parameters
    """
    # Truncate summary if too long
    if summary and len(summary) > 100:
        summary = summary[:100]

    return NymeriaResponse(
        notify=notify,
        summary=summary,
        content=content,
    )
