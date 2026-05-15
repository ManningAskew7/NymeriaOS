"""Slash command API schemas."""

from typing import Any, Literal

from pydantic import BaseModel, Field


CommandSource = Literal["user", "agent", "cli"]
CommandActor = Literal["user", "agent", "system"]
CommandSurface = Literal[
    "desktop",
    "mobile",
    "cli",
    "discord",
    "telegram",
    "twitch",
    "api",
    "agent",
]
CommandScope = Literal["global", "surface_local"]
CommandDangerLevel = Literal["safe", "normal", "dangerous"]
CommandExecutionKind = Literal["command", "chat_stream", "surface_local"]
CommandResultLevel = Literal["info", "success", "warning", "error"]


class CommandExecuteRequest(BaseModel):
    command: str = Field(..., min_length=1, description="Raw slash command, with or without leading slash")
    thread_id: str | None = Field(default=None, description="Active thread ID for thread-scoped commands")
    source: CommandSource = Field(default="user", description="Compatibility caller source")
    actor: CommandActor | None = Field(default=None, description="Command actor")
    surface: CommandSurface | None = Field(default=None, description="Calling surface")


class CommandExecuteResponse(BaseModel):
    success: bool
    markdown: str
    command: str
    level: CommandResultLevel = "info"
    data: dict[str, Any] | None = None


class CommandInfoResponse(BaseModel):
    name: str
    description: str
    usage: str
    category: str
    subcommands: list[str]
    id: str
    path: list[str]
    aliases: list[str]
    scope: CommandScope
    surfaces: list[str]
    agent_allowed: bool
    requires_thread: bool
    requires_admin: bool
    mutates_state: bool
    danger_level: CommandDangerLevel
    execution_kind: CommandExecutionKind
    note: str | None = None
