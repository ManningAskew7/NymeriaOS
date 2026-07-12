"""Schemas for the ``POST /ui-prompts/{prompt_id}/result`` endpoint."""

from __future__ import annotations

import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Bounds the tool result the agent reads back (context cost) against a
# hostile or buggy client POST. Agent-authored forms are a few KB of values.
MAX_VALUES_BYTES = 64 * 1024


class UiPromptResult(BaseModel):
    """Result body the desktop app POSTs when the user answers a prompt."""

    status: Literal["submitted", "cancelled"]
    values: Optional[dict[str, Any]] = None

    @field_validator("values")
    @classmethod
    def _cap_values_size(cls, v: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if v is None:
            return v
        try:
            size = len(json.dumps(v, default=str).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ValueError("values must be JSON-serializable") from exc
        if size > MAX_VALUES_BYTES:
            raise ValueError(
                f"values too large ({size} bytes; limit {MAX_VALUES_BYTES})"
            )
        return v


class UiPromptAck(BaseModel):
    received: bool = True
    delivered: bool = Field(
        default=True,
        description=(
            "False if the prompt had already been resolved, timed out, or "
            "been swept by the time the result POST arrived; the agent has "
            "already moved on."
        ),
    )
