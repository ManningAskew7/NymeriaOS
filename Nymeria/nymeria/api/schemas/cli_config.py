"""Schemas for the ``POST /cli-config/{command_id}/result`` endpoint."""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class CLIConfigResult(BaseModel):
    """Result body a CLI client POSTs back after applying a cli_config event."""

    ok: bool
    status: Literal["success", "error", "aborted"]
    data: Optional[dict[str, Any]] = None
    error: Optional[str] = Field(default=None, max_length=4000)


class CLIConfigAck(BaseModel):
    received: bool = True
    delivered: bool = Field(
        default=True,
        description=(
            "False if the command had already been resolved (an earlier CLI "
            "answered first) or swept by the time this result POST arrived."
        ),
    )
