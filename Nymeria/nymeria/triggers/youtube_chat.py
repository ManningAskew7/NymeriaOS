"""YouTube Live chat reader for the Twitch bot (multistreamers).

The bot process holds no Google credentials: this poller asks the API
(``POST /youtube/live-chat/poll``, ``core/youtube_live.py``) for the next page
of the streamer's live YouTube chat, acting as the bot's Nymeria account, and
hands new lines to the bot, which buffers them beside Twitch chat (one
buffer, one delivery cursor) and renders them as a separate prompt section.

Cadence is the quota lever (10,000 units/day per Google project): every
``poll_seconds`` (default 30) while attached, never faster than YouTube's
suggested interval; while nothing is live it re-checks every
``SEARCH_LIVE_SECONDS`` when the Twitch stream is known live (a multistreamer
goes live on both) and every ``SEARCH_IDLE_SECONDS`` otherwise. While the bot
is stopped (``!stop``) or YouTube is switched off (``!youtube off``) it makes
no calls at all, and it forgets its page token so resuming does not replay a
backlog (the API's first page only carries its last two minutes).

SDK-free and import-light on purpose (thin-client rule), so it unit-tests
without twitchio and without the agent harness.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Callable, Optional

from .bot_helpers import SeenEventCache

logger = logging.getLogger(__name__)

#: Re-detection cadence while not attached: the Twitch stream is live (the
#: YouTube one is likely starting too) vs not known to be.
SEARCH_LIVE_SECONDS = 60
SEARCH_IDLE_SECONDS = 300
#: Back-off after a transport/API error, and while the grant is missing (the
#: operator authorizing, then ``!youtube on``, wakes it early).
ERROR_RETRY_SECONDS = 60
UNAUTHORIZED_RETRY_SECONDS = 300
#: The loop's own tick while switched off or paused (no API calls are made).
IDLE_TICK_SECONDS = 30

#: Poll states that mean "not attached": the video id and page token go.
_DETACH_STATES = frozenset({"not_live", "ended", "not_found", "invalid", "forbidden"})


class YouTubeChatPoller:
    """Polls the API's YouTube reader on a cadence and forwards new lines.

    ``on_messages`` receives the API's normalized message dicts (oldest
    first), each delivered once (deduplicated by message id). ``is_paused``
    is the bot's kill switch; ``stream_live`` reports the Twitch stream's
    known liveness (None when unknown) for the search cadence.
    """

    def __init__(
        self,
        api: Any,
        *,
        user_id: str,
        on_messages: Callable[[list[dict[str, Any]]], Any],
        poll_seconds: int = 30,
        is_paused: Callable[[], bool] = lambda: False,
        stream_live: Callable[[], Optional[bool]] = lambda: None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._api = api
        self._user_id = user_id
        self._on_messages = on_messages
        self._poll_seconds = max(10, int(poll_seconds))
        self._is_paused = is_paused
        self._stream_live = stream_live
        self._clock = clock
        self.enabled = True
        self.state = "starting"
        self.detail = ""
        self.video_id: Optional[str] = None
        self.title: Optional[str] = None
        self.attached_via: Optional[str] = None
        self.pinned_video_id: Optional[str] = None
        self.retry_at: Optional[float] = None  # clock value when quota resets
        self.api_state: Optional[str] = None  # the last poll's state, as the API said it
        self._page_token: Optional[str] = None
        self._seen = SeenEventCache()
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # -- control -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def wake(self) -> None:
        """Poll now instead of waiting out the current delay."""
        self._wake.set()

    def pin(self, video_id: str) -> None:
        """Attach to this video (the ``!youtube <url>`` mod command)."""
        self.pinned_video_id = video_id
        self._detach()
        self.enabled = True
        self.wake()

    def unpin(self) -> None:
        """Back to auto-detection (reading on, like ``pin``)."""
        self.pinned_video_id = None
        self._detach()
        self.enabled = True
        self.wake()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if not enabled:
            self._detach()
            self.state = "off"
        self.wake()

    def _detach(self) -> None:
        self.video_id = None
        self.title = None
        self.attached_via = None
        self._page_token = None
        self.retry_at = None

    # -- status --------------------------------------------------------------

    def status_text(self) -> str:
        """One short phrase for ``!status``."""
        if not self.enabled or self.state == "off":
            return "off"
        if self.state == "paused":
            return "paused"
        if self.state == "live":
            title = (self.title or self.video_id or "").strip()
            if len(title) > 40:
                title = title[:39] + "..."
            pinned = ", pinned" if self.attached_via == "pinned" else ""
            return f"live ({title}{pinned})" if title else f"live{pinned}"
        if self.state == "quota":
            if self.retry_at is not None:
                left = max(0, int(self.retry_at - self._clock()))
                return f"quota used up (resets in ~{left // 3600}h {(left % 3600) // 60}m)"
            return "quota used up"
        if self.state == "unauthorized":
            return "not authorized"
        if self.state == "searching":
            if self.api_state in ("forbidden", "invalid"):
                return f"cannot read the chat ({self.api_state}), retrying"
            if self.pinned_video_id and self.api_state == "not_found":
                return f"pinned video {self.pinned_video_id} not found"
            return f"waiting for {self.pinned_video_id} to go live" if self.pinned_video_id else "not live"
        if self.state == "starting":
            return "starting"
        if self.state == "rate_limited":
            return "rate limited by YouTube, retrying"
        if self.state == "error":
            return f"error, retrying ({self.detail[:80]})" if self.detail else "error, retrying"
        return self.state

    def heartbeat_details(self) -> dict[str, Any]:
        return {
            "youtube": self.state if self.enabled else "off",
            "youtube_video": self.video_id,
            "youtube_pinned": self.pinned_video_id,
            "youtube_detail": self.detail[:200] or None,
        }

    # -- the loop ------------------------------------------------------------

    def _search_delay(self) -> float:
        return float(SEARCH_LIVE_SECONDS if self._stream_live() is True else SEARCH_IDLE_SECONDS)

    def _deliver(self, messages: list[dict[str, Any]]) -> None:
        fresh = [
            m for m in messages
            if isinstance(m, dict) and m.get("id") and not self._seen.mark_seen(str(m["id"]))
        ]
        if fresh:
            self._on_messages(fresh)

    async def poll_once(self) -> float:
        """One poll (or a deliberate non-poll); returns seconds until the next."""
        if not self.enabled:
            self.state = "off"
            self._detach()
            return float(IDLE_TICK_SECONDS)
        if self._is_paused():
            # Stopped: no calls, and no page token to replay a backlog from.
            self.state = "paused"
            self._page_token = None
            return float(IDLE_TICK_SECONDS)
        if self.retry_at is not None and self._clock() < self.retry_at:
            self.state = "quota"
            return max(1.0, self.retry_at - self._clock())
        self.retry_at = None
        try:
            result = await self._api.youtube_live_chat_poll(
                user_id=self._user_id,
                page_token=self._page_token,
                video_id=self.video_id,
                pinned_video_id=self.pinned_video_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state = "error"
            self.detail = f"reader request failed: {exc}"[:200]
            logger.warning("YouTube chat poll failed: %s", exc)
            return float(ERROR_RETRY_SECONDS)
        result = result if isinstance(result, dict) else {}
        state = str(result.get("state") or "error")
        self.api_state = state
        self.detail = str(result.get("detail") or "")

        if state in ("live", "ended"):
            video_id = result.get("video_id")
            if video_id and video_id != self.video_id:
                logger.info("YouTube chat: attached to %s (%s)", video_id, result.get("title"))
            self._deliver(list(result.get("messages") or []))
            if state == "live":
                self.state = "live"
                self.video_id = video_id
                self.title = result.get("title")
                self.attached_via = result.get("attached_via")
                self._page_token = result.get("next_page_token")
                suggested = float(result.get("poll_after_seconds") or 0.0)
                return max(float(self._poll_seconds), suggested)
            logger.info("YouTube chat: %s ended", self.video_id or video_id)
        if state in _DETACH_STATES:
            self._detach()
            self.state = "searching"
            return self._search_delay()
        if state == "quota_exhausted":
            wait = max(60, int(result.get("retry_after_seconds") or 3600))
            self.retry_at = self._clock() + wait
            self.state = "quota"
            logger.warning("YouTube API quota used up; reader idle for %ss", wait)
            return float(wait)
        if state == "unauthorized":
            self.state = "unauthorized"
            return float(UNAUTHORIZED_RETRY_SECONDS)
        if state == "rate_limited":
            self.state = "rate_limited"
            return float(self._poll_seconds * 2)
        self.state = "error"
        return float(ERROR_RETRY_SECONDS)

    async def _run(self) -> None:
        while True:
            self._wake.clear()  # a wake that lands during the poll still counts
            try:
                delay = await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("YouTube chat reader error", exc_info=True)
                delay = float(ERROR_RETRY_SECONDS)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)

