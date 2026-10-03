"""YouTube Live chat reader API schemas (the Twitch bot polls; see core/youtube_live.py)."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field

YouTubePollState = Literal[
    "live",
    "not_live",
    "ended",
    "unauthorized",
    "quota_exhausted",
    "rate_limited",
    "forbidden",
    "not_found",
    "invalid",
    "error",
]


class YouTubeLiveChatPollRequest(BaseModel):
    """One reader poll. The page token is honored only for the video it came from."""

    page_token: Optional[str] = Field(default=None, max_length=1024)
    video_id: Optional[str] = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{11}$",
        description="The attachment the page token belongs to (the previous response's video_id)",
    )
    pinned_video_id: Optional[str] = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_-]{11}$",
        description="Attach to this video instead of auto-detecting (the !youtube mod command)",
    )


class YouTubeLiveChatMessage(BaseModel):
    id: str
    kind: Literal["chat", "event"]
    tag: Optional[str] = Field(default=None, description="Event line kind: SUPERCHAT, MEMBER, GIFT, MOD")
    author_channel_id: str
    author_name: str
    badges: List[str]
    text: str
    published_at: str
    is_self: bool = Field(description="Written by the bot channel itself")


class YouTubeLiveChatPollResponse(BaseModel):
    state: YouTubePollState
    video_id: Optional[str] = None
    title: Optional[str] = None
    channel_id: Optional[str] = None
    attached_via: Optional[str] = Field(default=None, description="pinned, broadcaster, or bot")
    messages: List[YouTubeLiveChatMessage] = Field(default_factory=list, description="Oldest first")
    next_page_token: Optional[str] = None
    poll_after_seconds: float = Field(default=0.0, description="YouTube's suggested minimum wait")
    retry_after_seconds: Optional[int] = Field(default=None, description="Set on quota exhaustion")
    detail: str = ""
