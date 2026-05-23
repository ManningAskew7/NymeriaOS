"""Schemas for the ``POST /browser-commands/{command_id}/result`` endpoint."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class BrowserCommandResult(BaseModel):
    """Result body the Chrome extension POSTs back when a command finishes."""

    ok: bool
    status: Literal["success", "error", "aborted"]
    data: Optional[dict[str, Any]] = None
    error: Optional[str] = Field(default=None, max_length=4000)


class BrowserCommandAck(BaseModel):
    received: bool = True
    delivered: bool = Field(
        default=True,
        description=(
            "False if the command had already been resolved or swept by the "
            "time the result POST arrived — the agent has already moved on."
        ),
    )
