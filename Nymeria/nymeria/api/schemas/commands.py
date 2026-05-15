"""Slash command API schemas."""

from typing import Literal

from pydantic import BaseModel, Field


CommandSource = Literal["user", "agent", "cli"]


class CommandExecuteRequest(BaseModel):
    command: str = Field(..., min_length=1, description="Raw slash command, with or without leading slash")
    thread_id: str | None = Field(default=None, description="Active thread ID for thread-scoped commands")
    source: CommandSource = Field(default="user", description="Caller surface")


class CommandExecuteResponse(BaseModel):
    success: bool
    markdown: str
    command: str


class CommandInfoResponse(BaseModel):
    name: str
    description: str
    usage: str
    category: str
    subcommands: list[str]
