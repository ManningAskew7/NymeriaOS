"""YouTube Live chat over the YouTube Data API: detection, reads, and writes.

The Twitch bot's YouTube half. A streamer who multistreams gets ONE agent
reading both chats: the Twitch bot (a thin client, ``triggers/twitch_bot.py``)
polls ``POST /youtube/live-chat/poll`` (``api/routers/youtube_live.py``),
which lands here, and the ``youtube_chat_*`` tools (``tools/youtube_live.py``)
call the same functions. Both run in the API process, so the live-chat
ATTACHMENT, the bot channel's own id, and the ban ledger are shared state:
whatever video the reader is attached to is the chat the tools act in.

Two vault grants, both on the account the bot relays as (Google OAuth through
``request_credential(kind="oauth")``, descriptors in
``config/oauth_providers.py``):

- ``google_youtube`` (scope ``youtube``): the BOT channel, a separate channel
  the streamer has made a moderator of their chat. Reads chat, posts,
  deletes, and bans. Required for everything.
- ``google_youtube_readonly`` (scope ``youtube.readonly``): the STREAMER's
  grant, used only to find their active broadcast (one 1-unit call). Optional:
  without it the reader falls back to the bot channel's own broadcast (a
  streamer who uses one channel, or a dry run on the bot channel) and to a
  video the bot pins (the ``!youtube <url>`` mod command).

Quota is the binding constraint (10,000 units/day per Google Cloud project,
reset at midnight Pacific): reads cost 1 unit (documented; budget 5 until
measured), every write costs 50. Hence the slow reader cadence (the bot polls
every ~30 s, not at YouTube's suggested 1 to 10 s) and the write tools'
cost notes. ``streamList`` exists but its REST form and cost are undocumented.

Chat text and display names are untrusted public input: the bot fences them
(``core/twitch_chatlog.fence_chat``) and display names lose their brackets
here so a name cannot carry a forged ``[yt:...]`` id onto someone else's line.

Egress: every request goes through ``policy_http_client`` +
``request_with_policy`` against the module-constant API host; no
response-supplied or credential-supplied value reaches a URL authority.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .storage_paths import quarantine_corrupt_file, safe_path_segment, write_text_atomic
from .twitch_chatlog import YOUTUBE_NAMESPACE, get_chat_log_store

logger = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"

BOT_PROVIDER = "google_youtube"
BROADCASTER_PROVIDER = "google_youtube_readonly"
# Checked as a SUBSET of the granted scopes by get_google_credentials, so only
# the scope each call needs is named (email/profile ride along at consent).
_BOT_SCOPES = ("https://www.googleapis.com/auth/youtube",)
_BROADCASTER_SCOPES = ("https://www.googleapis.com/auth/youtube.readonly",)
_LABELS = {
    BOT_PROVIDER: "YouTube bot channel",
    BROADCASTER_PROVIDER: "YouTube streamer (read-only)",
}

#: YouTube's own chat limits (Help Center): 200 chars per message.
MAX_MESSAGE_CHARS = 200
#: A first page (no page token) returns recent history; only lines this
#: fresh are delivered, so attaching mid-stream does not flood a pulse.
FIRST_PAGE_WINDOW_SECONDS = 120
#: The API's ceiling; one big page per slow poll is the quota-friendly shape.
LIST_MAX_RESULTS = 2000
#: Timeout bounds for youtube_chat_timeout (the API documents neither; the
#: UI offers 5 minutes, and a day is the longest "timeout" that is not a ban).
MIN_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 86400
#: Ban ledger entries older than this are pruned (a temporary ban is long
#: expired, and a permanent one this old is Studio territory).
BAN_LEDGER_RETENTION_DAYS = 30
#: The bot channel's own id is re-read at most this often (1 unit).
_SELF_ID_TTL_SECONDS = 3600

_HTTP_TIMEOUT = 20.0
_PACIFIC_ZONE = "America/Los_Angeles"
#: Pacific Standard Time, for a host with no IANA zone data (Windows without
#: the ``tzdata`` package): an hour early in summer, never late.
_PACIFIC_FALLBACK = timezone(timedelta(hours=-8), "PST")

CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9._=%-]{8,256}$")


# =============================================================================
# Errors
# =============================================================================


class YouTubeLiveError(RuntimeError):
    """A classified YouTube failure; ``state`` drives the reader and the tools.

    States: ``unauthorized`` (no or dead grant), ``quota_exhausted`` (with
    ``retry_after`` seconds until the Pacific-midnight reset), ``ended`` (the
    live chat closed), ``not_live`` (nothing to attach to), ``rate_limited``,
    ``forbidden`` (not a moderator, or the target is the owner/a moderator),
    ``not_found``, ``invalid`` (bad id, rejected text, stale page token), and
    ``error`` (anything else, transport failures included).
    """

    def __init__(
        self,
        state: str,
        message: str,
        *,
        reason: str = "",
        retry_after: Optional[int] = None,
    ):
        super().__init__(message)
        self.state = state
        self.reason = reason
        self.retry_after = retry_after


def _pacific() -> Any:
    try:
        return ZoneInfo(_PACIFIC_ZONE)
    except Exception:  # ZoneInfoNotFoundError: no zone data on this host
        return _PACIFIC_FALLBACK


def seconds_until_quota_reset(now: Optional[datetime] = None) -> int:
    """Seconds until the next midnight Pacific, when YouTube resets quota.

    The difference is taken in UTC: subtracting two times in one zone counts
    wall-clock time, an hour off on the days daylight saving starts or ends.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(_pacific())
    midnight = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    wait = midnight.astimezone(timezone.utc) - current.astimezone(timezone.utc)
    return max(60, int(wait.total_seconds()))


def authorize_hint(provider: str = BOT_PROVIDER) -> str:
    """How to (re)connect a grant, for tool errors and the reader's status."""
    if provider == BROADCASTER_PROVIDER:
        return (
            "The streamer's read-only YouTube grant is not connected: send them the "
            'link from request_credential(provider="google_youtube_readonly", kind="oauth").'
        )
    return (
        "YouTube is not authorized for this account: connect the bot channel with "
        'request_credential(provider="google_youtube", kind="oauth"). A grant also '
        "lapses when revoked, or after 7 days when the Google Cloud project's consent "
        "screen is still in Testing status (publish it In production)."
    )


def _error_reason(resp: Any) -> tuple[str, str]:
    """``(reason, message)`` from a Google error body; blanks when absent."""
    try:
        body = resp.json()
    except Exception:
        return "", ""
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return "", ""
    message = str(err.get("message") or "")
    errors = err.get("errors")
    reason = ""
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = str(errors[0].get("reason") or "")
    if not reason:
        # Newer error shapes carry the reason under details[].reason.
        for detail in err.get("details") or []:
            if isinstance(detail, dict) and detail.get("reason"):
                reason = str(detail["reason"])
                break
    return reason, message


_ENDED_REASONS = frozenset({"liveChatEnded", "liveChatNotFound", "liveChatDisabled"})
_NOT_FOUND_REASONS = frozenset(
    {"liveChatMessageNotFound", "liveChatBanNotFound", "liveChatUserNotFound", "videoNotFound"}
)
#: Poll failures that mean the attached chat cannot be read: the attachment
#: is dropped so the next poll re-detects (the bot's reader detaches on the
#: same states).
_DEAD_CHAT_STATES = frozenset({"ended", "forbidden", "not_found", "invalid"})


def _classify(resp: Any) -> YouTubeLiveError:
    """Map a non-2xx response to a readable, classified error."""
    reason, message = _error_reason(resp)
    status = getattr(resp, "status_code", 0)
    short = (message or f"HTTP {status}")[:200]
    if reason in {"quotaExceeded", "dailyLimitExceeded"}:
        wait = seconds_until_quota_reset()
        return YouTubeLiveError(
            "quota_exhausted",
            "The YouTube API daily quota is used up; it resets at midnight Pacific "
            f"(in about {wait // 3600}h {(wait % 3600) // 60}m).",
            reason=reason,
            retry_after=wait,
        )
    if reason in _ENDED_REASONS:
        return YouTubeLiveError("ended", "The YouTube live chat has ended or is turned off.", reason=reason)
    if reason == "rateLimitExceeded":
        return YouTubeLiveError("rate_limited", "YouTube says requests are too frequent; wait a bit.", reason=reason)
    if reason == "liveChatBanInsertionNotAllowed":
        return YouTubeLiveError(
            "forbidden",
            "YouTube refused: the channel owner and moderators cannot be banned or timed out.",
            reason=reason,
        )
    if reason == "modificationNotAllowed":
        return YouTubeLiveError(
            "forbidden",
            "YouTube refused: a message from the owner or a moderator cannot be deleted.",
            reason=reason,
        )
    if reason == "liveStreamingNotEnabled":
        return YouTubeLiveError(
            "forbidden", "YouTube: this channel is not enabled for live streaming.", reason=reason
        )
    if reason == "insufficientPermissions" or (status == 403 and reason == "forbidden"):
        return YouTubeLiveError(
            "forbidden",
            "YouTube refused: usually the bot channel is not a moderator of this live chat "
            "(the streamer must add it), or the grant lacks the youtube scope.",
            reason=reason,
        )
    if status == 401 or reason in {"authError", "unauthorized"}:
        return YouTubeLiveError("unauthorized", authorize_hint(), reason=reason)
    if reason in _NOT_FOUND_REASONS or status == 404:
        return YouTubeLiveError("not_found", f"YouTube: not found ({reason or short}).", reason=reason)
    if status == 403:
        return YouTubeLiveError("forbidden", f"YouTube refused the request: {short}", reason=reason)
    if reason == "messageTextInvalid":
        return YouTubeLiveError(
            "invalid",
            "YouTube rejected the message text (links, HTML, and some special characters "
            "are not allowed in live chat).",
            reason=reason,
        )
    if status == 400:
        return YouTubeLiveError("invalid", f"YouTube rejected the request: {short}", reason=reason)
    return YouTubeLiveError("error", f"YouTube API error {status}: {short}", reason=reason)


# =============================================================================
# Transport and credentials
# =============================================================================


def _request(
    method: str,
    path: str,
    *,
    token: str,
    params: Optional[dict[str, Any]] = None,
    json_body: Any = None,
) -> Any:
    """One authenticated Data API call; returns parsed JSON (``{}`` on 204)."""
    from ..tools.service_integration_base import request_with_policy
    from .http_policy import policy_http_client

    try:
        with policy_http_client(timeout=_HTTP_TIMEOUT) as client:
            resp = request_with_policy(
                client,
                method,
                f"{API_BASE}/{path}",
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
    except Exception as exc:
        # Transport failures and the egress policy's RuntimeError. The message
        # never carries the bearer: httpx puts headers in no exception text.
        raise YouTubeLiveError("error", f"Could not reach YouTube: {exc}") from exc
    if resp.status_code >= 400:
        raise _classify(resp)
    if resp.status_code == 204 or not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError as exc:
        raise YouTubeLiveError("error", "YouTube returned a non-JSON response.") from exc


def _grant_connected(user_id: str, provider: str) -> bool:
    """Whether any account is stored for the grant (vault or legacy cache)."""
    from ..tools.auth_cache_utils import resolve_oauth_cache
    from ..tools.utils import ambient_thread_id

    try:
        return bool(resolve_oauth_cache(user_id, provider, thread_id=ambient_thread_id()).accounts)
    except Exception:
        logger.warning("Could not read the stored %s accounts for %s", provider, user_id, exc_info=True)
        return False


def _token(user_id: str, provider: str) -> Optional[str]:
    """A fresh access token for one of the two grants (refreshing as needed),
    or None when no such grant is connected.

    A grant that IS connected but yields no token (a vault or Google
    token-endpoint failure, or a revoked or narrowed grant) raises ``error``
    saying both, rather than reading as "not connected": a transient blip
    must not have the agent send the streamer a fresh consent link.
    """
    from ..tools.auth_cache_utils import OAuthAccountSelectionError, get_google_credentials

    scopes = _BOT_SCOPES if provider == BOT_PROVIDER else _BROADCASTER_SCOPES
    label = _LABELS[provider]
    try:
        creds = get_google_credentials(user_id, provider, scopes, provider_display_name=label)
    except OAuthAccountSelectionError as exc:
        raise YouTubeLiveError("unauthorized", f"{label}: {exc}") from exc
    except Exception as exc:
        logger.warning("Could not load the %s grant for %s", label, user_id, exc_info=True)
        raise YouTubeLiveError(
            "error", f"Could not load the {label} grant right now ({type(exc).__name__}); try again shortly."
        ) from exc
    token = getattr(creds, "token", None) if creds is not None else None
    if token:
        return str(token)
    if _grant_connected(user_id, provider):
        raise YouTubeLiveError(
            "error",
            f"The {label} grant is connected but no fresh token could be had: Google may be "
            "briefly unavailable, or the grant was revoked or lost its scope. If this "
            f'persists, reconnect it with request_credential(provider="{provider}", kind="oauth").',
        )
    return None


def _require_token(user_id: str, provider: str = BOT_PROVIDER) -> str:
    """``_token`` that raises ``unauthorized`` with the connect hint instead."""
    token = _token(user_id, provider)
    if not token:
        raise YouTubeLiveError("unauthorized", authorize_hint(provider))
    return token


_SELF_IDS: dict[str, tuple[str, float, str]] = {}  # user -> (channel id, expiry, token)
_SELF_LOCK = threading.Lock()


def bot_channel_id(user_id: str, token: str) -> str:
    """The bot channel's own id (1 unit per miss).

    Cached an hour and only for the token it was read with: a reconnect to
    another channel (a new token) re-reads it, so the bot's own lines are
    never taken for a chatter's.
    """
    now = time.monotonic()
    with _SELF_LOCK:
        cached = _SELF_IDS.get(user_id)
        if cached and cached[1] > now and cached[2] == token:
            return cached[0]
    data = _request("GET", "channels", token=token, params={"part": "id", "mine": "true"})
    items = data.get("items") or []
    channel_id = str(items[0].get("id") or "") if items else ""
    if not channel_id:
        raise YouTubeLiveError(
            "unauthorized",
            "The connected YouTube grant has no channel: reconnect it and pick the bot's "
            "channel on Google's channel picker.",
        )
    with _SELF_LOCK:
        _SELF_IDS[user_id] = (channel_id, now + _SELF_ID_TTL_SECONDS, token)
    return channel_id


# =============================================================================
# Attachment: which live chat the reader and the tools are on
# =============================================================================


@dataclass(frozen=True)
class Attachment:
    """The live chat the account is currently attached to."""

    video_id: str
    live_chat_id: str
    title: str
    channel_id: str  # the STREAMER's channel: the chat log's key
    channel_title: str
    source: str  # "pinned" | "broadcaster" | "bot"


_ATTACHMENTS: dict[str, Attachment] = {}
_ATTACH_LOCK = threading.Lock()


def current_attachment(user_id: str) -> Optional[Attachment]:
    with _ATTACH_LOCK:
        return _ATTACHMENTS.get(user_id)


def _set_attachment(user_id: str, attachment: Attachment) -> None:
    with _ATTACH_LOCK:
        previous = _ATTACHMENTS.get(user_id)
        _ATTACHMENTS[user_id] = attachment
    if previous is None or previous.video_id != attachment.video_id:
        logger.info(
            "YouTube: %s attached to %s (%s, via %s)",
            user_id,
            attachment.video_id,
            attachment.title,
            attachment.source,
        )


def clear_attachment(user_id: str, *, video_id: Optional[str] = None) -> None:
    """Forget the attachment (only if it is still ``video_id``, when given)."""
    with _ATTACH_LOCK:
        current = _ATTACHMENTS.get(user_id)
        if current is None or (video_id and current.video_id != video_id):
            return
        del _ATTACHMENTS[user_id]
    logger.info("YouTube: %s detached from %s", user_id, current.video_id)


def parse_video_id(value: str) -> Optional[str]:
    """A video id from a bare id or any common YouTube URL shape; else None."""
    text = (value or "").strip().strip("<>")
    if VIDEO_ID_RE.match(text):
        return text
    match = re.search(
        r"(?:youtube\.com/(?:watch\?(?:[^#\s]*&)?v=|live/|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])",
        text,
    )
    return match.group(1) if match else None


def _video_attachment(token: str, video_id: str, source: str) -> Attachment:
    data = _request(
        "GET", "videos", token=token, params={"part": "snippet,liveStreamingDetails", "id": video_id}
    )
    items = data.get("items") or []
    if not items:
        raise YouTubeLiveError("not_found", f"No YouTube video with id {video_id}.")
    item = items[0]
    details = item.get("liveStreamingDetails") or {}
    chat_id = details.get("activeLiveChatId")
    if not chat_id:
        raise YouTubeLiveError(
            "not_live", f"YouTube video {video_id} is not live right now (or its chat is off)."
        )
    snippet = item.get("snippet") or {}
    return Attachment(
        video_id=video_id,
        live_chat_id=str(chat_id),
        title=str(snippet.get("title") or ""),
        channel_id=str(snippet.get("channelId") or ""),
        channel_title=str(snippet.get("channelTitle") or ""),
        source=source,
    )


def _active_broadcast(token: str, source: str) -> Optional[Attachment]:
    """The grant owner's active broadcast with a chat, if any (1 unit)."""
    data = _request(
        "GET",
        "liveBroadcasts",
        token=token,
        params={
            "part": "snippet",
            "broadcastStatus": "active",
            "broadcastType": "all",
            "maxResults": 5,
        },
    )
    for item in data.get("items") or []:
        snippet = item.get("snippet") or {}
        chat_id = snippet.get("liveChatId")
        video_id = str(item.get("id") or "")
        if chat_id and video_id:
            return Attachment(
                video_id=video_id,
                live_chat_id=str(chat_id),
                title=str(snippet.get("title") or ""),
                channel_id=str(snippet.get("channelId") or ""),
                channel_title="",
                source=source,
            )
    return None


def _detect(user_id: str, bot_token: str) -> Attachment:
    """Broadcaster grant first (the streamer's own live broadcast), then the
    bot channel's own; ``not_live`` when neither is live.

    A failing streamer lookup (a revoked grant, a Google blip) falls through
    to the bot channel and is named in the ``not_live`` detail; a bot channel
    not enabled for live streaming (a fresh brand account) simply has no
    broadcast. Quota exhaustion always propagates.
    """
    attachment = None
    connected = False
    note = ""
    try:
        broadcaster_token = _token(user_id, BROADCASTER_PROVIDER)
        connected = broadcaster_token is not None
        if broadcaster_token:
            attachment = _active_broadcast(broadcaster_token, "broadcaster")
    except YouTubeLiveError as exc:
        if exc.state == "quota_exhausted":
            raise
        connected = True
        logger.warning("YouTube: the streamer grant lookup for %s failed: %s", user_id, exc)
        if exc.state == "unauthorized":
            note = (
                " The streamer's read-only grant was refused: reconnect it with "
                f'request_credential(provider="{BROADCASTER_PROVIDER}", kind="oauth").'
            )
        else:
            note = f" The streamer's read-only grant could not be checked: {exc}"
    if attachment is None:
        try:
            attachment = _active_broadcast(bot_token, "bot")
        except YouTubeLiveError as exc:
            if exc.reason != "liveStreamingNotEnabled":
                raise
    if attachment is None:
        hint = "" if connected else " " + authorize_hint(BROADCASTER_PROVIDER)
        raise YouTubeLiveError("not_live", "No live YouTube broadcast found." + hint + note)
    return attachment


def resolve_attachment(
    user_id: str, *, pinned_video_id: Optional[str] = None, bot_token: Optional[str] = None
) -> Attachment:
    """The reader's attachment: the pinned video when one is pinned, else the
    cached auto-detected one, else a fresh detection. Raises on failure.

    A pin replaces any attachment for another video; dropping the pin (the
    reader polls without one) re-detects rather than staying on the pinned
    video. A chat id is never carried across videos.
    """
    bot_token = bot_token or _require_token(user_id)
    current = current_attachment(user_id)
    if current is not None:
        if pinned_video_id and current.video_id == pinned_video_id:
            return current
        if not pinned_video_id and current.source != "pinned":
            return current
    try:
        if pinned_video_id:
            attachment = _video_attachment(bot_token, pinned_video_id, "pinned")
        else:
            attachment = _detect(user_id, bot_token)
    except YouTubeLiveError as exc:
        if exc.state in {"not_live", "not_found", "ended"}:
            clear_attachment(user_id)
        raise
    _set_attachment(user_id, attachment)
    return attachment


def attachment_for_tools(user_id: str) -> Attachment:
    """The tools' view: whatever the reader is attached to (pinned or not),
    else a fresh auto-detection (so the tools work without the bot running)."""
    current = current_attachment(user_id)
    if current is not None:
        return current
    return resolve_attachment(user_id)


# =============================================================================
# Message normalization
# =============================================================================


#: Brackets, parentheses, and their lookalikes: a line puts the badges in
#: parentheses BEFORE the name and the ids in brackets after it, so a name
#: carrying any of these could pose as a badge ("(broadcaster) Silk") or an id.
_NAME_BRACKETS = re.compile(r"[\[\](){}<>\uff08\uff09\uff3b\uff3d\uff5b\uff5d\u3010\u3011\u3014\u3015\u27e8\u27e9\ufe59\ufe5a\ufe5d\ufe5e]")


def clean_name(value: Any) -> str:
    """A display name safe to place before the ``[yt:...]`` id on a line.

    Brackets, parentheses, and their fullwidth lookalikes are dropped, so a
    name can pose as neither a role badge nor an id or tag, and whitespace
    (line breaks included) collapses to single spaces.
    """
    text = " ".join(_NAME_BRACKETS.sub(" ", str(value or "")).split())
    return text[:64] or "unknown"


def _badges(author: dict[str, Any]) -> list[str]:
    badges = []
    if author.get("isChatOwner"):
        badges.append("broadcaster")
    if author.get("isChatModerator"):
        badges.append("mod")
    if author.get("isChatSponsor"):
        badges.append("member")
    if author.get("isVerified"):
        badges.append("verified")
    return badges


def _event_text(kind: str, snippet: dict[str, Any], name: str, author_id: str) -> Optional[tuple[str, str]]:
    """``(tag, text)`` for the non-chat events worth a line; None to skip."""
    who = f"{name} [yt:{author_id}]" if author_id else name
    if kind == "superChatEvent":
        d = snippet.get("superChatDetails") or {}
        comment = " ".join(str(d.get("userComment") or "").split())
        amount = str(d.get("amountDisplayString") or "").strip()
        text = f"{who} sent a Super Chat" + (f" ({amount})" if amount else "")
        return "SUPERCHAT", text + (f": {comment}" if comment else "")
    if kind == "superStickerEvent":
        d = snippet.get("superStickerDetails") or {}
        amount = str(d.get("amountDisplayString") or "").strip()
        alt = str(((d.get("superStickerMetadata") or {}).get("altText")) or "").strip()
        text = f"{who} sent a Super Sticker" + (f" ({amount})" if amount else "")
        return "SUPERCHAT", text + (f": {alt}" if alt else "")
    if kind == "newSponsorEvent":
        level = str((snippet.get("newSponsorDetails") or {}).get("memberLevelName") or "").strip()
        return "MEMBER", f"{who} became a member" + (f" ({level})" if level else "")
    if kind == "memberMilestoneChatEvent":
        d = snippet.get("memberMilestoneChatDetails") or {}
        months = d.get("memberMonth")
        comment = " ".join(str(d.get("userComment") or "").split())
        text = f"{who} has been a member for {months} months" if months else f"{who} hit a membership milestone"
        return "MEMBER", text + (f": {comment}" if comment else "")
    if kind == "membershipGiftingEvent":
        count = (snippet.get("membershipGiftingDetails") or {}).get("giftMembershipsCount")
        return "MEMBER", f"{who} gifted {count or 'some'} memberships"
    if kind == "giftEvent":
        shown = " ".join(str(snippet.get("displayMessage") or "").split())
        return "GIFT", f"{who}: {shown}" if shown else f"{who} sent a gift"
    if kind == "userBannedEvent":
        d = snippet.get("userBannedDetails") or {}
        banned = d.get("bannedUserDetails") or {}
        target = clean_name(banned.get("displayName"))
        target_id = str(banned.get("channelId") or "")
        if target_id:
            target += f" [yt:{target_id}]"
        if d.get("banType") == "temporary":
            secs = d.get("banDurationSeconds")
            return "MOD", f"{name} timed out {target}" + (f" for {secs}s" if secs else "")
        return "MOD", f"{name} banned {target}"
    return None


def normalize_message(item: dict[str, Any], own_channel_id: str) -> Optional[dict[str, Any]]:
    """One API item as the reader's wire shape, or None when not worth a line.

    ``kind`` is ``chat`` (a chatter's text; ``is_self`` when the bot channel
    wrote it) or ``event`` (``tag`` names the line kind: SUPERCHAT, MEMBER,
    GIFT, MOD). Tombstones, polls, member-only mode flips, and per-recipient
    gift receipts are skipped.
    """
    snippet = item.get("snippet") or {}
    author = item.get("authorDetails") or {}
    kind = str(snippet.get("type") or "")
    message_id = str(item.get("id") or "")
    if not message_id:
        return None
    author_id = str(author.get("channelId") or snippet.get("authorChannelId") or "")
    name = clean_name(author.get("displayName"))
    base: dict[str, Any] = {
        "id": message_id,
        "published_at": str(snippet.get("publishedAt") or ""),
        "author_channel_id": author_id,
        "author_name": name,
        "badges": _badges(author),
        "is_self": bool(own_channel_id) and author_id == own_channel_id,
    }
    if kind == "textMessageEvent":
        text = (snippet.get("textMessageDetails") or {}).get("messageText")
        if text is None:
            text = snippet.get("displayMessage") or ""
        return {**base, "kind": "chat", "tag": None, "text": str(text)}
    event = _event_text(kind, snippet, name, author_id)
    if event is None:
        return None
    tag, text = event
    return {**base, "kind": "event", "tag": tag, "text": text, "is_self": False}


def _parse_ts(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# =============================================================================
# The reader's poll
# =============================================================================


@dataclass
class PollResult:
    state: str  # live | not_live | ended | unauthorized | quota_exhausted | rate_limited | error ...
    attachment: Optional[Attachment] = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    next_page_token: Optional[str] = None
    poll_after_seconds: float = 0.0
    retry_after_seconds: Optional[int] = None
    detail: str = ""


def _list_messages(token: str, live_chat_id: str, page_token: Optional[str]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "liveChatId": live_chat_id,
        "part": "snippet,authorDetails",
        "maxResults": LIST_MAX_RESULTS,
    }
    if page_token:
        params["pageToken"] = page_token
    return _request("GET", "liveChat/messages", token=token, params=params)


def poll(
    user_id: str,
    *,
    data_dir: Path,
    retention_days: int,
    page_token: Optional[str] = None,
    video_id: Optional[str] = None,
    pinned_video_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> PollResult:
    """One reader poll: attach (or re-detect), read a page, log, normalize.

    ``page_token`` is honored only when ``video_id`` names the current
    attachment: a token from an earlier chat starts the new chat fresh. A
    first page (no usable token) delivers only its last
    ``FIRST_PAGE_WINDOW_SECONDS`` of lines. A chat that ended (or a stream
    that went offline) clears the attachment so the next poll re-detects.
    Never raises: failures come back as a non-``live`` state with a detail.
    """
    attached: Optional[str] = None
    try:
        token = _require_token(user_id)
        attachment = resolve_attachment(user_id, pinned_video_id=pinned_video_id, bot_token=token)
        attached = attachment.video_id
        own_id = bot_channel_id(user_id, token)
        use_token = page_token if (page_token and video_id == attachment.video_id) else None
        try:
            data = _list_messages(token, attachment.live_chat_id, use_token)
        except YouTubeLiveError as exc:
            if exc.reason != "pageTokenInvalid" or not use_token:
                raise
            use_token = None  # stale token: restart the page (the bot dedupes by id)
            data = _list_messages(token, attachment.live_chat_id, None)
    except YouTubeLiveError as exc:
        if exc.state in _DEAD_CHAT_STATES:
            # The bot detaches on these too; keeping the attachment would
            # re-list an unreadable chat forever and block re-detection.
            clear_attachment(user_id, video_id=attached)
        return PollResult(state=exc.state, detail=str(exc), retry_after_seconds=exc.retry_after)

    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(seconds=FIRST_PAGE_WINDOW_SECONDS) if use_token is None else None
    fetched: list[dict[str, Any]] = []
    ended = bool(data.get("offlineAt"))
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        try:
            if (item.get("snippet") or {}).get("type") == "chatEndedEvent":
                ended = True
                continue
            message = normalize_message(item, own_id)
        except Exception:  # one malformed item must not cost the page
            logger.warning("YouTube: skipped an unreadable chat item", exc_info=True)
            continue
        if message is not None:
            fetched.append(message)

    # Every fetched line is logged (the chatter history covers a restart's
    # first page too); only the delivered lines are windowed.
    _log_chat_lines(user_id, attachment, fetched, data_dir=data_dir, retention_days=retention_days)
    messages = fetched
    if cutoff is not None:
        messages = [
            m for m in fetched if (when := _parse_ts(m["published_at"])) is not None and when >= cutoff
        ]
    if ended:
        clear_attachment(user_id, video_id=attachment.video_id)
    interval_ms = data.get("pollingIntervalMillis")
    return PollResult(
        state="ended" if ended else "live",
        attachment=attachment,
        messages=messages,
        next_page_token=None if ended else (data.get("nextPageToken") or None),
        poll_after_seconds=(float(interval_ms) / 1000.0) if isinstance(interval_ms, (int, float)) else 0.0,
        detail="The live chat ended." if ended else "",
    )


def _log_chat_lines(
    user_id: str,
    attachment: Attachment,
    messages: list[dict[str, Any]],
    *,
    data_dir: Path,
    retention_days: int,
) -> None:
    """Append chatters' lines (never the bot's own) to the YouTube chat log."""
    if not attachment.channel_id:
        return
    lines = []
    for m in messages:
        if m.get("is_self") or not m.get("author_channel_id"):
            continue
        if m["kind"] == "chat":
            text = m["text"]
        elif m.get("tag") == "SUPERCHAT":
            text = f"[{m['tag']}] {m['text']}"
        else:
            continue
        lines.append(
            {
                "message_id": m["id"],
                "user_login": m["author_channel_id"],
                "display_name": m["author_name"],
                "user_id": m["author_channel_id"],
                "text": text,
                "timestamp": m["published_at"],
                "badges": list(m["badges"]),
            }
        )
    if not lines:
        return
    try:
        store = get_chat_log_store(
            data_dir, user_id, retention_days=retention_days, namespace=YOUTUBE_NAMESPACE
        )
        store.append(attachment.channel_id, lines)
    except Exception:
        logger.warning("YouTube chat log append failed", exc_info=True)


# =============================================================================
# Writes (the tools)
# =============================================================================


def clean_channel_id(value: Any) -> str:
    """A chatter's channel id from what the agent copied (``yt:`` and brackets
    tolerated); raises ``invalid`` for anything that is not one."""
    text = str(value or "").strip().strip("[]").strip()
    if text.lower().startswith("yt:"):
        text = text[3:].strip()
    if not CHANNEL_ID_RE.match(text):
        raise YouTubeLiveError(
            "invalid",
            f"{value!r} is not a YouTube channel id: pass the UC... value from the "
            "chatter's [yt:...] tag in the YouTube chat section.",
        )
    return text


def clean_message_id(value: Any) -> str:
    text = str(value or "").strip().strip("[]").strip()
    if text.lower().startswith("msg:"):
        text = text[4:].strip()
    if not _MESSAGE_ID_RE.match(text):
        raise YouTubeLiveError(
            "invalid",
            f"{value!r} is not a YouTube message id: pass the value from the line's "
            "[msg:...] tag in the YouTube chat section.",
        )
    return text


def send_message(user_id: str, text: str) -> str:
    """Post one message (<= MAX_MESSAGE_CHARS) as the bot channel; returns its id."""
    attachment = attachment_for_tools(user_id)
    token = _require_token(user_id)
    body = {
        "snippet": {
            "liveChatId": attachment.live_chat_id,
            "type": "textMessageEvent",
            "textMessageDetails": {"messageText": text},
        }
    }
    try:
        data = _request("POST", "liveChat/messages", token=token, params={"part": "snippet"}, json_body=body)
    except YouTubeLiveError as exc:
        if exc.state == "ended":
            clear_attachment(user_id, video_id=attachment.video_id)
        raise
    return str(data.get("id") or "")


def delete_message(user_id: str, message_id: str) -> None:
    attachment = attachment_for_tools(user_id)
    token = _require_token(user_id)
    try:
        _request("DELETE", "liveChat/messages", token=token, params={"id": message_id})
    except YouTubeLiveError as exc:
        if exc.state == "ended":
            clear_attachment(user_id, video_id=attachment.video_id)
        raise


def ban(
    user_id: str,
    channel_id: str,
    *,
    seconds: Optional[int],
    data_dir: Path,
) -> str:
    """Ban (``seconds`` None) or time out a chatter in the attached chat;
    records the ban id so ``unban`` can lift it. Returns the ban id."""
    attachment = attachment_for_tools(user_id)
    token = _require_token(user_id)
    snippet: dict[str, Any] = {
        "liveChatId": attachment.live_chat_id,
        "type": "permanent" if seconds is None else "temporary",
        "bannedUserDetails": {"channelId": channel_id},
    }
    if seconds is not None:
        snippet["banDurationSeconds"] = int(seconds)
    try:
        data = _request("POST", "liveChat/bans", token=token, params={"part": "snippet"}, json_body={"snippet": snippet})
    except YouTubeLiveError as exc:
        if exc.state == "ended":
            clear_attachment(user_id, video_id=attachment.video_id)
        raise
    ban_id = str(data.get("id") or "")
    if ban_id:
        try:
            _record_ban(
                data_dir,
                user_id,
                channel_id,
                {
                    "ban_id": ban_id,
                    "type": snippet["type"],
                    "seconds": seconds,
                    "video_id": attachment.video_id,
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                },
            )
        except Exception:
            logger.warning("Could not record YouTube ban %s in the ledger", ban_id, exc_info=True)
    return ban_id


def unban(user_id: str, channel_id: str, *, data_dir: Path) -> str:
    """Lift the most recent ban the bot made on ``channel_id``.

    Returns ``lifted`` or ``expired`` (YouTube no longer has it: a timeout
    that ran out). Raises ``not_found`` when the ledger has no ban for the
    chatter: YouTube exposes no ban listing, so a ban made by someone else
    (or before the ledger existed) can only be lifted in YouTube Studio.
    """
    record = _ledger_load(data_dir, user_id).get(channel_id)
    if not record or not record.get("ban_id"):
        raise YouTubeLiveError(
            "not_found",
            "No ban by the bot is on record for that chatter. YouTube offers no way to "
            "look up other bans: lift it in YouTube Studio (or the chat's banned list).",
        )
    token = _require_token(user_id)
    try:
        _request("DELETE", "liveChat/bans", token=token, params={"id": record["ban_id"]})
        outcome = "lifted"
    except YouTubeLiveError as exc:
        if exc.state != "not_found":
            raise
        outcome = "expired"
    _ledger_pop(data_dir, user_id, channel_id)
    return outcome


def live_status(user_id: str) -> dict[str, Any]:
    """The attachment plus live viewer numbers (1 unit)."""
    attachment = attachment_for_tools(user_id)
    token = _require_token(user_id)
    data = _request(
        "GET",
        "videos",
        token=token,
        params={"part": "snippet,liveStreamingDetails", "id": attachment.video_id},
    )
    items = data.get("items") or []
    item = items[0] if items else {}
    details = item.get("liveStreamingDetails") or {}
    snippet = item.get("snippet") or {}
    return {
        "video_id": attachment.video_id,
        "url": f"https://www.youtube.com/watch?v={attachment.video_id}",
        "title": snippet.get("title") or attachment.title,
        "channel": snippet.get("channelTitle") or attachment.channel_title,
        "channel_id": attachment.channel_id,
        "attached_via": attachment.source,
        "live": bool(details.get("activeLiveChatId")) and not details.get("actualEndTime"),
        "concurrent_viewers": details.get("concurrentViewers"),
        "started_at": details.get("actualStartTime"),
    }


# =============================================================================
# Ban ledger (unban needs the id only our own ban call returns)
# =============================================================================

_LEDGER_LOCK = threading.Lock()


def _ledger_path(data_dir: Path, user_id: str) -> Path:
    return Path(data_dir) / "users" / safe_path_segment(user_id) / "youtube_live" / "bans.json"


def _ledger_load(data_dir: Path, user_id: str) -> dict[str, dict[str, Any]]:
    path = _ledger_path(data_dir, user_id)
    with _LEDGER_LOCK:
        return _ledger_read(path)


def _ledger_read(path: Path) -> dict[str, dict[str, Any]]:
    """Caller holds the lock. An unreadable file is quarantined, never reset."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        moved = quarantine_corrupt_file(path)
        logger.error("YouTube ban ledger %s is unreadable; quarantined to %s", path, moved)
        if moved is None:
            raise
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _ledger_write(path: Path, data: dict[str, dict[str, Any]]) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=BAN_LEDGER_RETENTION_DAYS)
    kept = {}
    for channel_id, record in data.items():
        when = _parse_ts(str(record.get("at") or ""))
        if when is not None and when >= cutoff:
            kept[channel_id] = record
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(kept, indent=2, sort_keys=True))


def _record_ban(data_dir: Path, user_id: str, channel_id: str, record: dict[str, Any]) -> None:
    path = _ledger_path(data_dir, user_id)
    with _LEDGER_LOCK:
        data = _ledger_read(path)
        data[channel_id] = record
        _ledger_write(path, data)


def _ledger_pop(data_dir: Path, user_id: str, channel_id: str) -> None:
    path = _ledger_path(data_dir, user_id)
    with _LEDGER_LOCK:
        data = _ledger_read(path)
        if data.pop(channel_id, None) is not None:
            _ledger_write(path, data)


def _reset_for_tests() -> None:
    """Drop the in-process caches (attachments, self ids)."""
    with _ATTACH_LOCK:
        _ATTACHMENTS.clear()
    with _SELF_LOCK:
        _SELF_IDS.clear()
