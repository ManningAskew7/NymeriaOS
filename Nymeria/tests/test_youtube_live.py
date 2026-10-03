"""YouTube Live chat core (core/youtube_live.py): detection, reads, writes.

YouTube is faked at the HTTP layer (``request_with_policy``), so the module's
own transport, error classification, and egress path run for real; only the
vault lookup (``get_google_credentials`` and ``resolve_oauth_cache``) is
replaced, and the fake insists on each grant's literal scope so a scope mix-up
cannot pass.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

import httpx
import pytest

from nymeria.core import twitch_chatlog as chatlog_module
from nymeria.core import youtube_live as yt

NOW = datetime(2026, 10, 3, 9, 0, 0, tzinfo=timezone.utc)
BOT_CH = "UC" + "botchannel".ljust(22, "x")
STREAMER_CH = "UC" + "silkchannel".ljust(22, "y")
ALICE_CH = "UC" + "alice".ljust(22, "a")
MALLORY_CH = "UC" + "mallory".ljust(22, "m")
_SCOPES = {
    "google_youtube": ("https://www.googleapis.com/auth/youtube",),
    "google_youtube_readonly": ("https://www.googleapis.com/auth/youtube.readonly",),
}


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _text_item(mid: str, author_id: str, name: str, text: str, when: datetime, **flags: Any) -> dict:
    return {
        "id": mid,
        "snippet": {
            "type": "textMessageEvent",
            "publishedAt": _iso(when),
            "displayMessage": text,
            "textMessageDetails": {"messageText": text},
            "authorChannelId": author_id,
        },
        "authorDetails": {"channelId": author_id, "displayName": name, **flags},
    }


def _broadcast(video_id: str, chat_id: str, channel_id: str, title: str = "Live!") -> dict:
    return {"id": video_id, "snippet": {"liveChatId": chat_id, "channelId": channel_id, "title": title}}


class FakeYouTube:
    """A tiny YouTube Data API: routes (method, path) to canned state."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.broadcasts: dict[str, list[dict]] = {}  # bearer -> active broadcasts
        self.videos: dict[str, dict] = {}
        self.channels: dict[str, str] = {"tok-bot": BOT_CH}
        self.pages: list[Any] = []  # queued liveChat/messages results (dict or (status, reason))
        self.errors: dict[tuple[str, str], tuple[int, str]] = {}  # one-shot errors
        self.ban_seq = 0
        self.deleted_bans: set[str] = set()
        self.expired_bans: set[str] = set()
        self.transport_error: Optional[Exception] = None

    @staticmethod
    def _error(status: int, reason: str) -> httpx.Response:
        body = {"error": {"code": status, "message": f"{reason} happened", "errors": [{"reason": reason}]}}
        return httpx.Response(status, json=body)

    def __call__(self, client: Any, method: str, url: str, **kwargs: Any) -> httpx.Response:
        assert url.startswith(yt.API_BASE + "/"), url
        path = url[len(yt.API_BASE) + 1:]
        headers = kwargs.get("headers") or {}
        token = headers.get("Authorization", "").removeprefix("Bearer ")
        params = dict(kwargs.get("params") or {})
        self.calls.append({"method": method, "path": path, "params": params, "json": kwargs.get("json"), "token": token})
        if self.transport_error is not None:
            raise self.transport_error
        if (method, path) in self.errors:
            return self._error(*self.errors.pop((method, path)))
        if path == "channels":
            channel = self.channels.get(token)
            return httpx.Response(200, json={"items": [{"id": channel}] if channel else []})
        if path == "liveBroadcasts":
            return httpx.Response(200, json={"items": self.broadcasts.get(token, [])})
        if path == "videos":
            item = self.videos.get(str(params.get("id")))
            return httpx.Response(200, json={"items": [item] if item else []})
        if path == "liveChat/messages" and method == "GET":
            page = self.pages.pop(0) if self.pages else {"items": [], "nextPageToken": "tok-next"}
            if isinstance(page, tuple):
                return self._error(*page)
            return httpx.Response(200, json=page)
        if path == "liveChat/messages" and method == "POST":
            return httpx.Response(200, json={"id": f"sent-{len(self.calls)}"})
        if path == "liveChat/messages" and method == "DELETE":
            return httpx.Response(204)
        if path == "liveChat/bans" and method == "POST":
            self.ban_seq += 1
            return httpx.Response(200, json={"id": f"ban-{self.ban_seq}"})
        if path == "liveChat/bans" and method == "DELETE":
            ban_id = str(params.get("id"))
            if ban_id in self.expired_bans:
                return self._error(404, "liveChatBanNotFound")
            self.deleted_bans.add(ban_id)
            return httpx.Response(204)
        raise AssertionError(f"unexpected call {method} {path}")

    def of(self, path: str, method: Optional[str] = None) -> list[dict]:
        return [c for c in self.calls if c["path"] == path and (method is None or c["method"] == method)]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """FakeYouTube wired in; grants: alice holds the bot grant only by default."""
    from nymeria.tools import auth_cache_utils, service_integration_base

    api = FakeYouTube()
    monkeypatch.setattr(service_integration_base, "request_with_policy", api)
    grants: dict[tuple[str, str], str] = {("alice", yt.BOT_PROVIDER): "tok-bot"}
    connected: set[tuple[str, str]] = set()  # stored accounts that yield no token

    def fake_credentials(user_id, provider, scopes, account_id=None, **kwargs):
        assert tuple(scopes) == _SCOPES[provider], (provider, scopes)
        token = grants.get((user_id, provider))
        return SimpleNamespace(token=token) if token else None

    def fake_cache(user_id, provider, **kwargs):
        stored = (user_id, provider) in grants or (user_id, provider) in connected
        return SimpleNamespace(accounts={"acct": {}} if stored else {})

    monkeypatch.setattr(auth_cache_utils, "get_google_credentials", fake_credentials)
    monkeypatch.setattr(auth_cache_utils, "resolve_oauth_cache", fake_cache)
    monkeypatch.setattr(chatlog_module, "_STORES", {})
    yt._reset_for_tests()
    api.grants = grants  # type: ignore[attr-defined]
    api.connected = connected  # type: ignore[attr-defined]
    api.data_dir = tmp_path  # type: ignore[attr-defined]
    yield api
    yt._reset_for_tests()


def _att(result: yt.PollResult) -> yt.Attachment:
    assert result.attachment is not None, result.detail
    return result.attachment


def _norm(item: dict, own: str = BOT_CH) -> dict:
    message = yt.normalize_message(item, own)
    assert message is not None
    return message


def _poll(fake: FakeYouTube, user: str = "alice", **kwargs: Any) -> yt.PollResult:
    kwargs.setdefault("now", NOW)
    return yt.poll(user, data_dir=fake.data_dir, retention_days=14, **kwargs)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Behavior 13: no grant, no calls, a connect hint
# ---------------------------------------------------------------------------


def test_poll_without_the_bot_grant_is_unauthorized_and_calls_nothing(fake):
    result = _poll(fake, user="bob")
    assert result.state == "unauthorized"
    assert 'request_credential(provider="google_youtube"' in result.detail
    assert fake.calls == []


def test_transport_failure_reads_as_error_without_leaking_the_token(fake):
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", BOT_CH)]
    fake.transport_error = httpx.ConnectError("connection refused")
    result = _poll(fake)
    assert result.state == "error" and "connection refused" in result.detail
    assert "tok-bot" not in result.detail


# ---------------------------------------------------------------------------
# Behavior 14: detection order
# ---------------------------------------------------------------------------


def test_detection_prefers_the_pin_then_the_streamer_then_the_bot_channel(fake):
    fake.grants[("alice", yt.BROADCASTER_PROVIDER)] = "tok-streamer"
    fake.broadcasts["tok-streamer"] = [_broadcast("silkvideo01", "chat-silk", STREAMER_CH, "Silk live")]
    fake.broadcasts["tok-bot"] = [_broadcast("botvideo001", "chat-bot", BOT_CH, "Bot test")]
    fake.videos["pinnedvid01"] = {
        "id": "pinnedvid01",
        "snippet": {"title": "Pinned", "channelId": STREAMER_CH, "channelTitle": "Silk"},
        "liveStreamingDetails": {"activeLiveChatId": "chat-pinned"},
    }

    pinned = _poll(fake, pinned_video_id="pinnedvid01")
    assert (pinned.state, _att(pinned).video_id, _att(pinned).source) == ("live", "pinnedvid01", "pinned")
    assert fake.of("liveChat/messages")[-1]["params"]["liveChatId"] == "chat-pinned"

    # Dropping the pin re-detects: the streamer's own broadcast wins.
    auto = _poll(fake)
    assert (_att(auto).video_id, _att(auto).source) == ("silkvideo01", "broadcaster")
    assert fake.of("liveChat/messages")[-1]["params"]["liveChatId"] == "chat-silk"

    # Without a live streamer broadcast, the bot channel's own (a dry run).
    yt.clear_attachment("alice")
    fake.broadcasts["tok-streamer"] = []
    fallback = _poll(fake)
    assert (_att(fallback).video_id, _att(fallback).source) == ("botvideo001", "bot")

    yt.clear_attachment("alice")
    fake.broadcasts["tok-bot"] = []
    nothing = _poll(fake)
    assert nothing.state == "not_live" and yt.current_attachment("alice") is None


def test_not_live_without_the_streamer_grant_says_how_to_get_it(fake):
    result = _poll(fake)
    assert result.state == "not_live"
    assert "google_youtube_readonly" in result.detail


def test_a_refused_streamer_grant_falls_back_to_the_bot_channel_and_says_so(fake):
    fake.grants[("alice", yt.BROADCASTER_PROVIDER)] = "tok-streamer"
    fake.broadcasts["tok-bot"] = [_broadcast("botvideo001", "chat-bot", BOT_CH)]
    fake.errors[("GET", "liveBroadcasts")] = (401, "authError")  # the streamer's lookup runs first
    result = _poll(fake)
    assert result.state == "live" and _att(result).source == "bot"

    yt.clear_attachment("alice")
    fake.broadcasts["tok-bot"] = []
    fake.errors[("GET", "liveBroadcasts")] = (401, "authError")
    nothing = _poll(fake)
    assert nothing.state == "not_live" and "refused" in nothing.detail
    assert 'provider="google_youtube_readonly"' in nothing.detail
    assert 'provider="google_youtube"' not in nothing.detail  # not the bot's reconnect hint

    fake.errors[("GET", "liveBroadcasts")] = (403, "quotaExceeded")
    assert _poll(fake).state == "quota_exhausted"


def test_a_bot_channel_that_cannot_stream_is_not_live_rather_than_an_error(fake):
    fake.errors[("GET", "liveBroadcasts")] = (403, "liveStreamingNotEnabled")
    result = _poll(fake)
    assert result.state == "not_live" and "google_youtube_readonly" in result.detail


def test_a_connected_grant_without_a_token_is_an_error_not_a_connect_prompt(fake, monkeypatch):
    from nymeria.tools import auth_cache_utils

    del fake.grants[("alice", yt.BOT_PROVIDER)]
    fake.connected.add(("alice", yt.BOT_PROVIDER))
    result = _poll(fake)
    assert result.state == "error" and "connected but no fresh token" in result.detail
    assert fake.calls == []

    def vault_down(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("vault locked")

    monkeypatch.setattr(auth_cache_utils, "get_google_credentials", vault_down)
    down = _poll(fake)
    assert down.state == "error" and "try again shortly" in down.detail
    assert "request_credential" not in down.detail and fake.calls == []


def test_a_pinned_video_that_is_not_live_is_not_live(fake):
    fake.videos["upcoming001"] = {"id": "upcoming001", "snippet": {}, "liveStreamingDetails": {}}
    result = _poll(fake, pinned_video_id="upcoming001")
    assert result.state == "not_live" and "upcoming001" in result.detail


# ---------------------------------------------------------------------------
# Behaviors 12 and 19: page tokens, first pages, ended chats
# ---------------------------------------------------------------------------


def test_first_page_delivers_only_the_last_two_minutes(fake):
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", BOT_CH)]
    fake.pages.append(
        {
            "items": [
                _text_item("old", ALICE_CH, "alice", "stale", NOW - timedelta(minutes=10)),
                _text_item("new", ALICE_CH, "alice", "fresh", NOW - timedelta(seconds=30)),
            ],
            "nextPageToken": "p1",
        }
    )
    result = _poll(fake)
    assert [m["id"] for m in result.messages] == ["new"]
    assert result.next_page_token == "p1"

    # A continued page delivers everything, however old.
    fake.pages.append({"items": [_text_item("late", ALICE_CH, "alice", "x", NOW - timedelta(minutes=10))]})
    nxt = _poll(fake, page_token="p1", video_id="vid00000001")
    assert [m["id"] for m in nxt.messages] == ["late"]
    assert fake.of("liveChat/messages")[-1]["params"]["pageToken"] == "p1"


def test_a_first_page_logs_its_history_though_it_delivers_only_the_last_two_minutes(fake):
    # Real-clock relative: the chat log rotates by the real date.
    now = datetime.now(timezone.utc)
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", STREAMER_CH)]
    fake.pages.append(
        {
            "items": [
                _text_item("old", MALLORY_CH, "mallory", "spam from before", now - timedelta(minutes=10)),
                _text_item("new", ALICE_CH, "alice", "fresh", now - timedelta(seconds=5)),
            ]
        }
    )
    result = _poll(fake, now=now)
    assert [m["id"] for m in result.messages] == ["new"]
    store = chatlog_module.get_chat_log_store(
        fake.data_dir, "alice", namespace=chatlog_module.YOUTUBE_NAMESPACE  # type: ignore[attr-defined]
    )
    logged = store.query(STREAMER_CH, login=MALLORY_CH, hours=48, now=now)
    assert [e["text"] for e in logged] == ["spam from before"]


def test_a_malformed_item_is_skipped_rather_than_failing_the_page(fake):
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", STREAMER_CH)]
    fake.pages.append({"items": [{"id": "bad", "snippet": "boom"}, _text_item("ok", ALICE_CH, "a", "fine", NOW)]})
    result = _poll(fake)
    assert result.state == "live" and [m["id"] for m in result.messages] == ["ok"]


@pytest.mark.parametrize(
    "status, reason, state",
    [(403, "forbidden", "forbidden"), (404, "notFound", "not_found"), (400, "badRequest", "invalid")],
)
def test_a_chat_that_cannot_be_read_drops_the_attachment_and_re_detects(fake, status, reason, state):
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", STREAMER_CH)]
    fake.pages.append((status, reason))
    failed = _poll(fake)
    assert failed.state == state and yt.current_attachment("alice") is None

    fake.broadcasts["tok-bot"] = [_broadcast("vid00000002", "chat-2", STREAMER_CH)]
    again = _poll(fake)
    assert again.state == "live" and _att(again).video_id == "vid00000002"
    assert fake.of("liveChat/messages")[-1]["params"]["liveChatId"] == "chat-2"


def test_a_page_token_from_another_video_is_never_sent(fake):
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000002", "chat-2", BOT_CH)]
    _poll(fake, page_token="from-old-chat", video_id="vid00000001")
    assert "pageToken" not in fake.of("liveChat/messages")[-1]["params"]


def test_a_stale_page_token_restarts_the_page(fake):
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", BOT_CH)]
    _poll(fake)
    fake.pages.append((400, "pageTokenInvalid"))
    fake.pages.append({"items": [_text_item("m1", ALICE_CH, "a", "hi", NOW)], "nextPageToken": "p9"})
    result = _poll(fake, page_token="expired", video_id="vid00000001")
    assert result.state == "live" and result.next_page_token == "p9"
    reads = fake.of("liveChat/messages")
    assert reads[-2]["params"].get("pageToken") == "expired"
    assert "pageToken" not in reads[-1]["params"]


def test_an_ended_chat_detaches_and_the_next_poll_re_detects(fake):
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", BOT_CH)]
    assert _poll(fake).state == "live"
    fake.pages.append((403, "liveChatEnded"))
    ended = _poll(fake, page_token="p1", video_id="vid00000001")
    assert ended.state == "ended" and yt.current_attachment("alice") is None

    # The new stream is a new video and a new chat; the old id is never reused.
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000002", "chat-2", BOT_CH)]
    detections = len(fake.of("liveBroadcasts"))
    again = _poll(fake, page_token="p1", video_id="vid00000001")
    assert _att(again).video_id == "vid00000002"
    assert len(fake.of("liveBroadcasts")) == detections + 1
    last = fake.of("liveChat/messages")[-1]["params"]
    assert last["liveChatId"] == "chat-2" and "pageToken" not in last


def test_offline_at_ends_the_attachment_but_delivers_the_last_page(fake):
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", BOT_CH)]
    fake.pages.append(
        {"items": [_text_item("bye", ALICE_CH, "a", "gg", NOW)], "offlineAt": _iso(NOW), "nextPageToken": "x"}
    )
    result = _poll(fake)
    assert result.state == "ended" and [m["id"] for m in result.messages] == ["bye"]
    assert result.next_page_token is None and yt.current_attachment("alice") is None


# ---------------------------------------------------------------------------
# Behavior 11: quota exhaustion
# ---------------------------------------------------------------------------


def test_quota_exhaustion_reports_the_wait_until_the_pacific_reset(fake, monkeypatch):
    monkeypatch.setattr(yt, "seconds_until_quota_reset", lambda now=None: 12345)
    fake.errors[("GET", "liveBroadcasts")] = (403, "quotaExceeded")
    result = _poll(fake)
    assert result.state == "quota_exhausted" and result.retry_after_seconds == 12345
    assert "midnight Pacific (in about 3h 25m)" in result.detail


def test_quota_reset_is_midnight_pacific():
    # 2026-10-03 09:00 UTC is 02:00 PDT; the reset is 22 h later.
    assert yt.seconds_until_quota_reset(NOW) == 22 * 3600
    # After US DST ends (PST, UTC-8): 2026-11-10 07:30 UTC is 23:30 PST.
    assert yt.seconds_until_quota_reset(datetime(2026, 11, 10, 7, 30, tzinfo=timezone.utc)) == 30 * 60
    # Real elapsed time on the switch days, not wall-clock: DST ends
    # 2026-11-01 (00:30 PDT to midnight PST is 24.5 h) and starts 2026-03-08
    # (01:00 PST to midnight PDT is 22 h).
    assert yt.seconds_until_quota_reset(datetime(2026, 11, 1, 7, 30, tzinfo=timezone.utc)) == 24 * 3600 + 1800
    assert yt.seconds_until_quota_reset(datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc)) == 22 * 3600


def test_quota_reset_without_zone_data_falls_back_to_pacific_standard_time(monkeypatch):
    from zoneinfo import ZoneInfoNotFoundError

    def no_zone_data(name: str) -> Any:
        raise ZoneInfoNotFoundError(name)

    monkeypatch.setattr(yt, "ZoneInfo", no_zone_data)
    # 09:00 UTC is 01:00 PST: midnight PST (08:00 UTC) is 23 h away.
    assert yt.seconds_until_quota_reset(NOW) == 23 * 3600


# ---------------------------------------------------------------------------
# Behaviors 6, 7, 8: normalization
# ---------------------------------------------------------------------------


def test_normalization_maps_badges_flags_self_and_cleans_names(fake):
    owner = _norm(
        _text_item("m1", STREAMER_CH, "Silk", "hi", NOW, isChatOwner=True, isVerified=True)
    )
    assert owner["badges"] == ["broadcaster", "verified"] and owner["kind"] == "chat"
    member_mod = _norm(
        _text_item("m2", ALICE_CH, "alice", "x", NOW, isChatModerator=True, isChatSponsor=True)
    )
    assert member_mod["badges"] == ["mod", "member"]
    own = _norm(_text_item("m3", BOT_CH, "SilkGPT", "beep", NOW))
    assert own["is_self"] is True
    forged = _norm(
        _text_item("m4", MALLORY_CH, f"Bob [yt:{ALICE_CH}]\nfake", "x", NOW)
    )
    assert "[" not in forged["author_name"] and "\n" not in forged["author_name"]
    assert forged["author_channel_id"] == MALLORY_CH
    # A name cannot pose as a role badge either (badges sit in parentheses
    # before the name), fullwidth lookalikes included.
    posing = _norm(_text_item("m5", MALLORY_CH, "(broadcaster) Silk", "x", NOW))
    assert posing["author_name"] == "broadcaster Silk"
    wide = _norm(_text_item("m6", MALLORY_CH, "Silk\uff08mod\uff09\uff3byt:x\uff3d", "x", NOW))
    assert wide["author_name"] == "Silk mod yt:x"


def test_events_become_tagged_lines_and_noise_is_skipped():
    def item(kind: str, **snippet: Any) -> dict:
        return {
            "id": f"e-{kind}",
            "snippet": {"type": kind, "publishedAt": _iso(NOW), **snippet},
            "authorDetails": {"channelId": ALICE_CH, "displayName": "alice"},
        }

    sc = _norm(
        item("superChatEvent", superChatDetails={"amountDisplayString": "A$5.00", "userComment": "gg"})
    )
    assert (sc["kind"], sc["tag"]) == ("event", "SUPERCHAT")
    assert sc["text"] == f"alice [yt:{ALICE_CH}] sent a Super Chat (A$5.00): gg"
    member = _norm(item("newSponsorEvent", newSponsorDetails={"memberLevelName": "Gold"}))
    assert member["tag"] == "MEMBER" and member["text"].endswith("became a member (Gold)")
    ban = _norm(
        item(
            "userBannedEvent",
            userBannedDetails={
                "bannedUserDetails": {"channelId": MALLORY_CH, "displayName": "mallory"},
                "banType": "temporary",
                "banDurationSeconds": 300,
            },
        )
    )
    assert ban["tag"] == "MOD" and ban["text"] == f"alice timed out mallory [yt:{MALLORY_CH}] for 300s"
    for kind in ("tombstone", "pollEvent", "sponsorOnlyModeStartedEvent", "giftMembershipReceivedEvent"):
        assert yt.normalize_message(item(kind), BOT_CH) is None


def test_the_bot_channels_own_lines_are_flagged_from_its_channel_id(fake):
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", BOT_CH)]
    fake.pages.append(
        {"items": [_text_item("me", BOT_CH, "SilkGPT", "hello", NOW), _text_item("you", ALICE_CH, "a", "yo", NOW)]}
    )
    result = _poll(fake)
    assert [(m["id"], m["is_self"]) for m in result.messages] == [("me", True), ("you", False)]


def test_the_bot_channel_is_re_read_when_the_grant_changes(fake):
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", STREAMER_CH)]
    fake.pages.append({"items": [_text_item("a", BOT_CH, "first bot", "x", NOW)]})
    assert [m["is_self"] for m in _poll(fake).messages] == [True]

    # Reconnected to another channel: the old one is a chatter now.
    new_bot = "UC" + "newbot".ljust(22, "n")
    fake.grants[("alice", yt.BOT_PROVIDER)] = "tok-bot2"
    fake.channels["tok-bot2"] = new_bot
    fake.pages.append(
        {"items": [_text_item("b", BOT_CH, "first bot", "y", NOW), _text_item("c", new_bot, "new", "z", NOW)]}
    )
    result = _poll(fake)
    assert [(m["id"], m["is_self"]) for m in result.messages] == [("b", False), ("c", True)]
    assert len(fake.of("channels")) == 2


# ---------------------------------------------------------------------------
# Behavior 20: chatter history is logged under the streamer's channel
# ---------------------------------------------------------------------------


def test_chatters_lines_are_logged_and_the_bots_own_are_not(fake):
    # Real-clock relative: the store rotates day files by the real date.
    now = datetime.now(timezone.utc)
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", STREAMER_CH)]
    fake.pages.append(
        {
            "items": [
                _text_item("m1", ALICE_CH, "alice", "first", now - timedelta(seconds=3)),
                _text_item("m2", BOT_CH, "SilkGPT", "bot says", now - timedelta(seconds=2)),
                _text_item("m3", ALICE_CH, "alice", "second", now - timedelta(seconds=1)),
            ]
        }
    )
    _poll(fake, now=now)
    store = chatlog_module.get_chat_log_store(
        fake.data_dir, "alice", namespace=chatlog_module.YOUTUBE_NAMESPACE  # type: ignore[attr-defined]
    )
    entries = store.query(STREAMER_CH, login=ALICE_CH, hours=48, now=now)
    assert [e["text"] for e in entries] == ["second", "first"]
    assert store.query(STREAMER_CH, login=BOT_CH, hours=48, now=now) == []
    # Stored apart from the Twitch log.
    assert (fake.data_dir / "users" / "alice" / "youtube_chatlog").is_dir()  # type: ignore[attr-defined]
    assert not (fake.data_dir / "users" / "alice" / "twitch_chatlog").exists()  # type: ignore[attr-defined]


def test_the_youtube_log_never_keeps_more_than_30_days_whatever_the_setting(tmp_path):
    """YouTube's API policy (and the published privacy policy) cap stored
    chat at 30 days; the shared retention setting goes to 365 for Twitch."""
    now = datetime.now(timezone.utc)

    def seed(store: Any, channel: str) -> Any:
        day_dir = tmp_path / "users" / "alice" / store_ns(store) / channel.lower()
        day_dir.mkdir(parents=True, exist_ok=True)
        for age in (29, 31):
            (day_dir / f"{(now - timedelta(days=age)).date().isoformat()}.jsonl").write_text("{}\n")
        return day_dir

    def store_ns(store: Any) -> str:
        return store._root.name

    youtube = chatlog_module.get_chat_log_store(
        tmp_path, "alice", retention_days=365, namespace=chatlog_module.YOUTUBE_NAMESPACE
    )
    yt_dir = seed(youtube, STREAMER_CH)
    youtube.rotate(STREAMER_CH, now=now)
    kept = sorted(p.stem for p in yt_dir.glob("*.jsonl"))
    assert kept == [(now - timedelta(days=29)).date().isoformat()]
    # A settings change on a later call cannot lift the cap either.
    again = chatlog_module.get_chat_log_store(
        tmp_path, "alice", retention_days=90, namespace=chatlog_module.YOUTUBE_NAMESPACE
    )
    assert again.retention_days == 30

    twitch = chatlog_module.get_chat_log_store(tmp_path, "alice", retention_days=365)
    tw_dir = seed(twitch, "silk")
    twitch.rotate("silk", now=now)
    assert len(list(tw_dir.glob("*.jsonl"))) == 2  # Twitch keeps its own setting


# ---------------------------------------------------------------------------
# Behavior 15: bans are recorded so unban works, even after a restart
# ---------------------------------------------------------------------------


def test_timeout_records_the_ban_and_unban_lifts_it_after_a_restart(fake):
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", STREAMER_CH)]
    ban_id = yt.ban("alice", MALLORY_CH, seconds=600, data_dir=fake.data_dir)  # type: ignore[attr-defined]
    body = fake.of("liveChat/bans", "POST")[-1]["json"]["snippet"]
    assert body == {
        "liveChatId": "chat-1",
        "type": "temporary",
        "bannedUserDetails": {"channelId": MALLORY_CH},
        "banDurationSeconds": 600,
    }
    ledger = json.loads(
        (fake.data_dir / "users" / "alice" / "youtube_live" / "bans.json").read_text()  # type: ignore[attr-defined]
    )
    assert ledger[MALLORY_CH]["ban_id"] == ban_id

    yt._reset_for_tests()  # an API restart: in-memory state gone, the ledger stays
    assert yt.unban("alice", MALLORY_CH, data_dir=fake.data_dir) == "lifted"  # type: ignore[attr-defined]
    assert ban_id in fake.deleted_bans

    with pytest.raises(yt.YouTubeLiveError) as missing:
        yt.unban("alice", MALLORY_CH, data_dir=fake.data_dir)  # type: ignore[attr-defined]
    assert missing.value.state == "not_found" and "YouTube Studio" in str(missing.value)


def test_unbanning_a_timeout_that_already_ran_out_says_so(fake):
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", STREAMER_CH)]
    ban_id = yt.ban("alice", MALLORY_CH, seconds=60, data_dir=fake.data_dir)  # type: ignore[attr-defined]
    fake.expired_bans.add(ban_id)
    assert yt.unban("alice", MALLORY_CH, data_dir=fake.data_dir) == "expired"  # type: ignore[attr-defined]


def test_a_permanent_ban_is_permanent_and_a_refused_ban_is_explained(fake):
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", STREAMER_CH)]
    yt.ban("alice", MALLORY_CH, seconds=None, data_dir=fake.data_dir)  # type: ignore[attr-defined]
    snippet = fake.of("liveChat/bans", "POST")[-1]["json"]["snippet"]
    assert snippet["type"] == "permanent" and "banDurationSeconds" not in snippet

    fake.errors[("POST", "liveChat/bans")] = (403, "liveChatBanInsertionNotAllowed")
    with pytest.raises(yt.YouTubeLiveError) as refused:
        yt.ban("alice", STREAMER_CH, seconds=300, data_dir=fake.data_dir)  # type: ignore[attr-defined]
    assert refused.value.state == "forbidden" and "moderators" in str(refused.value)


def test_a_corrupt_ban_ledger_is_quarantined_not_silently_reset(fake):
    path = fake.data_dir / "users" / "alice" / "youtube_live" / "bans.json"  # type: ignore[attr-defined]
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", STREAMER_CH)]
    yt.ban("alice", MALLORY_CH, seconds=60, data_dir=fake.data_dir)  # type: ignore[attr-defined]
    quarantined = list((path.parent / "quarantine").iterdir())
    assert len(quarantined) == 1 and quarantined[0].read_text() == "{not json"
    assert MALLORY_CH in json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Small parsers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ&t=5", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("<https://youtube.com/live/dQw4w9WgXcQ>", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/@silk/live", None),
        ("dQw4w9WgXcQextra", None),
        ("", None),
    ],
)
def test_parse_video_id(value, expected):
    assert yt.parse_video_id(value) == expected


def test_channel_and_message_ids_tolerate_copied_tags_and_reject_names():
    assert yt.clean_channel_id(f"[yt:{ALICE_CH}]") == ALICE_CH
    assert yt.clean_channel_id(f" yt:{ALICE_CH} ") == ALICE_CH
    for bad in ("alice", "@alice", ALICE_CH + "z", ""):
        with pytest.raises(yt.YouTubeLiveError):
            yt.clean_channel_id(bad)
    assert yt.clean_message_id("[msg:LCC.EhwKGkNKUzRqN2Fw]") == "LCC.EhwKGkNKUzRqN2Fw"
    with pytest.raises(yt.YouTubeLiveError):
        yt.clean_message_id("drop table; --")
