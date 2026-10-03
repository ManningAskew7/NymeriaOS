"""YouTube Live chat reader route: the Twitch bot's YouTube half polls here.

The bot is a thin client with no Google credentials; this route reads the
caller's live chat with the caller's own vault grants (``core/youtube_live.py``),
so the attachment it finds is the one the ``youtube_chat_*`` tools act in.
Bound to the authenticated caller (the bot relays as its Nymeria account).
"""

from collections.abc import Callable
from typing import Any, cast

from fastapi import APIRouter, Depends

from ...core.accounts import AuthenticatedUser
from ..schemas.youtube_live import (
    YouTubeLiveChatMessage,
    YouTubeLiveChatPollRequest,
    YouTubeLiveChatPollResponse,
    YouTubePollState,
)


def create_youtube_live_router(
    verify_api_key: Callable[..., Any],
    authed_user_id: Callable[..., Any],
    get_settings_fn: Callable[[], Any],
) -> APIRouter:
    """Create the YouTube Live chat reader router with app dependencies injected."""
    router = APIRouter(tags=["YouTube"])

    # A plain def: the poll makes blocking Google calls (token refresh, Data
    # API), so FastAPI runs it on the threadpool rather than the event loop.
    @router.post("/youtube/live-chat/poll", response_model=YouTubeLiveChatPollResponse)
    def poll_live_chat(
        body: YouTubeLiveChatPollRequest,
        user_id: str = Depends(authed_user_id),
        _user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Attach to the caller's live YouTube chat (or the pinned video) and read one page."""
        from ...core import youtube_live

        settings = get_settings_fn()
        result = youtube_live.poll(
            user_id,
            data_dir=settings.data_dir,
            retention_days=settings.twitch_chatlog_retention_days,
            page_token=body.page_token,
            video_id=body.video_id,
            pinned_video_id=body.pinned_video_id,
        )
        attachment = result.attachment
        return YouTubeLiveChatPollResponse(
            state=cast(YouTubePollState, result.state),
            video_id=attachment.video_id if attachment else None,
            title=attachment.title if attachment else None,
            channel_id=attachment.channel_id if attachment else None,
            attached_via=attachment.source if attachment else None,
            messages=[YouTubeLiveChatMessage(**m) for m in result.messages],
            next_page_token=result.next_page_token,
            poll_after_seconds=result.poll_after_seconds,
            retry_after_seconds=result.retry_after_seconds,
            detail=result.detail,
        )

    return router
