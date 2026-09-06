"""Twitch chat log API schemas (the bot pushes, clients and the tool read)."""

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from ...core.twitch_chatlog import CHATLOG_BATCH_MAX


class TwitchChatLogPushRequest(BaseModel):
    """A batch of chat lines from the bot. Stored under the authenticated user."""

    channel: str = Field(..., min_length=1, max_length=64, description="Channel login (with or without #)")
    messages: List[Dict[str, Any]] = Field(
        ..., max_length=CHATLOG_BATCH_MAX, description="Lines: message_id, user_login, display_name, user_id, text, timestamp, badges"
    )


class TwitchChatLogPushResponse(BaseModel):
    stored: int
    dropped: int


class TwitchChatLogEntry(BaseModel):
    message_id: str
    user_login: str
    display_name: str
    user_id: str
    text: str
    timestamp: str
    badges: List[str]


class TwitchChatLogResponse(BaseModel):
    channel: str
    login: str
    hours: int
    count: int
    entries: List[TwitchChatLogEntry] = Field(..., description="Newest first")
