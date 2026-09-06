"""Twitch per-chatter chat log routes: the bot pushes, clients read.

Twitch has no chat-history endpoint, so the API keeps its own record of
what the bot saw (``core/twitch_chatlog.py``). Everything is bound to the
authenticated caller: the bot posts as the account that runs it, and a
lookup only ever sees that account's channels.
"""

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Query

from ...core.accounts import AuthenticatedUser
from ...core.twitch_chatlog import MAX_QUERY_HOURS, MAX_QUERY_LIMIT, get_chat_log_store
from ..schemas.twitch_chatlog import (
    TwitchChatLogEntry,
    TwitchChatLogPushRequest,
    TwitchChatLogPushResponse,
    TwitchChatLogResponse,
)


def create_twitch_chatlog_router(
    verify_api_key: Callable[..., Any],
    authed_user_id: Callable[..., Any],
    get_settings_fn: Callable[[], Any],
) -> APIRouter:
    """Create the Twitch chat-log router with app dependencies injected."""
    router = APIRouter(tags=["Twitch"])

    def _store(user_id: str):
        settings = get_settings_fn()
        return get_chat_log_store(
            settings.data_dir, user_id, retention_days=settings.twitch_chatlog_retention_days
        )

    @router.post("/twitch/chat-log", response_model=TwitchChatLogPushResponse)
    async def push_chat_log(
        body: TwitchChatLogPushRequest,
        user_id: str = Depends(authed_user_id),
        _user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Store a batch of chat lines under the caller's account (the bot's push)."""
        stored, dropped = _store(user_id).append(body.channel, body.messages)
        return TwitchChatLogPushResponse(stored=stored, dropped=dropped)

    @router.get("/twitch/chat-log", response_model=TwitchChatLogResponse)
    async def get_chat_log(
        channel: str = Query(..., min_length=1, max_length=64),
        login: str = Query(..., min_length=1, max_length=64, description="Chatter login or display name"),
        limit: int = Query(default=50, ge=1, le=MAX_QUERY_LIMIT),
        hours: int = Query(default=24, ge=1, le=MAX_QUERY_HOURS),
        user_id: str = Depends(authed_user_id),
        _user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """That chatter's recent lines in the caller's log for ``channel``, newest first."""
        entries = _store(user_id).query(channel, login=login, limit=limit, hours=hours)
        return TwitchChatLogResponse(
            channel=channel.strip().lstrip("#").lower(),
            login=login.strip().lstrip("@").lower(),
            hours=hours,
            count=len(entries),
            entries=[TwitchChatLogEntry(**e) for e in entries],
        )

    return router
