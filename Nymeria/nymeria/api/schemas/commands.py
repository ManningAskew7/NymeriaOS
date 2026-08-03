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
    "slack",
    "whatsapp",
    "teams",
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
    supports_forms: bool = Field(
        default=False,
        description=(
            "Capability flag: this caller renders declarative form payloads "
            "(data.form). When false, form payloads are stripped from the "
            "response (the markdown fallback carries the same content) and "
            "bare commands with missing required arguments return the usage "
            "error instead of a generated picker."
        ),
    )


class CommandExecuteResponse(BaseModel):
    success: bool
    markdown: str
    command: str
    level: CommandResultLevel = "info"
    data: dict[str, Any] | None = None


class CommandOptionResponse(BaseModel):
    """One live option of a ``choices_ref`` value set (GET /commands/options).

    The shape matches the form contract's option dict, so a client can feed
    these straight into a picker or an autocomplete list.
    """

    id: str
    label: str
    meta: str = ""
    description: str = ""
    current: bool = False


class CommandParamModel(BaseModel):
    """One declared argument of a schema'd command (backlog #129).

    ``choices`` is the statically enforced set; ``choices_ref`` names a
    dynamic value set consumers may resolve live (autocomplete, forms).
    """

    name: str
    kind: Literal["positional", "option", "flag", "rest", "scope"]
    type: Literal["str", "int", "bool"] = "str"
    required: bool = False
    choices: list[str] = []
    choices_ref: str | None = None
    default: str | int | bool | None = None
    repeatable: bool = False
    aliases: list[str] = []
    description: str = ""
    no_echo: bool = False
    label: str | None = None


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
    blocked_surfaces: list[str] = []
    blocked_reason: str | None = None
    agent_allowed: bool
    requires_thread: bool
    requires_admin: bool
    mutates_state: bool
    danger_level: CommandDangerLevel
    execution_kind: CommandExecutionKind
    note: str | None = None
    examples: list[str] = []
    # None = the command has no declared schema; [] = schema'd, zero args.
    params: list[CommandParamModel] | None = None
