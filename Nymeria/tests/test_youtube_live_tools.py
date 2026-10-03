"""youtube_chat_* tools (tools/youtube_live.py): plan behaviors 13, 15, 16,
17, 20. YouTube is faked at the HTTP layer (``FakeYouTube`` from
test_youtube_live), so every assert is about what reached YouTube and what
the agent reads back.

Edges skipped on purpose: concurrent sends (the tool is sequential per call
and YouTube rate limits are reported as errors, covered by the core tests).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nymeria.core import youtube_live as yt
from nymeria.tools import youtube_live as tools
from test_youtube_live import (  # noqa: F401  (fake is a fixture)
    ALICE_CH,
    MALLORY_CH,
    STREAMER_CH,
    _broadcast,
    _poll,
    _text_item,
    fake,
)


def _cfg(user: str = "alice") -> dict:
    return {"configurable": {"user_id": user, "thread_id": "twitch_silk"}}


@pytest.fixture
def live(fake, monkeypatch):  # noqa: F811
    """Alice's bot channel sees a live broadcast; settings point at tmp."""
    from types import SimpleNamespace

    import nymeria.config as config_mod

    fake.broadcasts["tok-bot"] = [_broadcast("vid00000001", "chat-1", STREAMER_CH, "Silk live")]
    settings = SimpleNamespace(data_dir=fake.data_dir, twitch_chatlog_retention_days=14)
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    return fake


def test_every_youtube_tool_receives_the_run_config():
    """Same pin as the Twitch family: the Optional spelling is never injected,
    which would collapse every per-user vault lookup to the default account."""
    from langchain_core.tools.base import _get_runnable_config_param

    names = [t.name for t in tools.YOUTUBE_LIVE_TOOLS]
    assert len(names) == 7
    for t in tools.YOUTUBE_LIVE_TOOLS:
        assert _get_runnable_config_param(t.func) == "config", t.name


def test_the_tools_never_take_the_youtube_api_key_credential():
    """The YouTube Data API key family (provider "youtube") is another
    credential: tool_search's setup status, the enable warning and the
    auth-failure guidance must not point these OAuth tools at it (found live:
    "youtube needs_setup"), and its key lookups must not claim the
    google_youtube OAuth provider name, whose token record is no API key."""
    from nymeria.tools import credential_registry as cr

    names = [t.name for t in tools.YOUTUBE_LIVE_TOOLS]
    assert all(name.startswith("youtube_chat_") for name in names)
    assert cr.auth_status_for_tools(names, "alice") == {}
    assert cr.get_provider_spec("google_youtube") is None
    key_spec = cr.spec_for_tool("youtube_search")  # the key family keeps its spec
    assert key_spec is not None and key_spec.provider == "youtube"


# ---------------------------------------------------------------------------
# Behavior 16: send splits at 200, at most 2 parts, refuses longer
# ---------------------------------------------------------------------------


def test_send_posts_one_message_as_the_bot_channel(live):
    result = tools.youtube_chat_send.func(message="gg chat", config=_cfg())
    assert result.startswith("Sent to the YouTube live chat") and "gg chat" in result
    posts = live.of("liveChat/messages", "POST")
    assert len(posts) == 1 and posts[0]["token"] == "tok-bot"
    assert posts[0]["json"]["snippet"] == {
        "liveChatId": "chat-1",
        "type": "textMessageEvent",
        "textMessageDetails": {"messageText": "gg chat"},
    }


def _posted(live) -> list[str]:
    return [c["json"]["snippet"]["textMessageDetails"]["messageText"] for c in live.of("liveChat/messages", "POST")]


_OPENER = "First sentence goes here and runs on for a while so its end lands inside the cut window. "


@pytest.mark.parametrize(
    "text, joiner, first_ends_sentence",
    [
        ("x" * 201, "", False),
        (("word " * 50).strip(), " ", False),
        # An early sentence end: a general splitter stops there and leaves a
        # third part (76 + 199 + 19).
        (" ".join(("Thanks for the raid everyone, that was a genuinely great way to kick it off. "
                   + "word " * 44).split()), " ", False),
        # A sentence end inside the window where both halves fit: cut there.
        (" ".join((_OPENER + "word " * 30).split()), " ", True),
        # 400 characters of words with no space where both halves fit.
        (" ".join(("abcdef " * 58).split())[:400], "", False),
        ("y" * 400, "", False),
    ],
)
def test_send_over_200_chars_goes_out_as_exactly_two_messages(live, text, joiner, first_ends_sentence):
    result = tools.youtube_chat_send.func(message=text, config=_cfg())
    assert "split into 2 messages" in result
    parts = _posted(live)
    assert len(parts) == 2 and all(0 < len(p) <= 200 for p in parts)
    assert joiner.join(parts) == text
    assert parts[0].endswith(".") is first_ends_sentence


def test_send_takes_200_chars_in_one_message_and_refuses_over_400(live):
    tools.youtube_chat_send.func(message="z" * 200, config=_cfg())
    assert _posted(live) == ["z" * 200]

    before = len(live.calls)
    refused = tools.youtube_chat_send.func(message="x" * 401, config=_cfg())
    assert refused.startswith("[Error]") and "Shorten it to 400" in refused
    assert len(live.calls) == before  # nothing reached YouTube


@pytest.mark.parametrize("empty", ["", "   ", "\n\t"])
def test_send_rejects_empty_text_without_a_call(live, empty):
    result = tools.youtube_chat_send.func(message=empty, config=_cfg())
    assert result == "[Error]: message is empty."
    assert live.calls == []


def test_send_failure_carries_the_error_prefix_the_bot_counts_on(live):
    live.errors[("POST", "liveChat/messages")] = (403, "forbidden")
    result = tools.youtube_chat_send.func(message="hi", config=_cfg())
    assert result.startswith("[Error]") and "not a moderator" in result


def test_a_failed_second_part_reports_the_first_went_out(live, monkeypatch):
    from nymeria.tools import service_integration_base

    def fail_second_insert(client, method, url, **kwargs):
        if method == "POST" and url.endswith("liveChat/messages") and live.of("liveChat/messages", "POST"):
            return live._error(403, "rateLimitExceeded")
        return live(client, method, url, **kwargs)

    monkeypatch.setattr(service_integration_base, "request_with_policy", fail_second_insert)
    result = tools.youtube_chat_send.func(message=("word " * 50).strip(), config=_cfg())
    assert result.startswith("[Error]") and "(1 of 2 parts sent first)" in result
    assert len(live.of("liveChat/messages", "POST")) == 1


# ---------------------------------------------------------------------------
# Behaviors 13 and 17: no grant / not live means no write
# ---------------------------------------------------------------------------


def test_every_write_refuses_without_a_grant_and_names_the_provider(fake):  # noqa: F811
    for result in (
        tools.youtube_chat_send.func(message="hi", config=_cfg("bob")),
        tools.youtube_chat_timeout.func(channel_id=ALICE_CH, config=_cfg("bob")),
        tools.youtube_chat_ban.func(channel_id=ALICE_CH, config=_cfg("bob")),
        tools.youtube_chat_delete_message.func(message_id="LCC.abcdefghij", config=_cfg("bob")),
    ):
        assert result.startswith("[Error]") and 'provider="google_youtube"' in result
    assert fake.calls == []


def test_writes_refuse_when_nothing_is_live_without_spending_a_write(fake):  # noqa: F811
    result = tools.youtube_chat_send.func(message="hi", config=_cfg())
    assert result.startswith("[Error]") and "No live YouTube broadcast" in result
    assert not [c for c in fake.calls if c["method"] in ("POST", "DELETE")]
    status = tools.youtube_chat_status.func(config=_cfg())
    assert status.startswith("Not live on YouTube")


def test_tools_act_in_the_chat_the_reader_is_pinned_to(live):
    live.videos["pinnedvid01"] = {
        "id": "pinnedvid01",
        "snippet": {"title": "Pinned", "channelId": STREAMER_CH},
        "liveStreamingDetails": {"activeLiveChatId": "chat-pinned"},
    }
    _poll(live, pinned_video_id="pinnedvid01")
    tools.youtube_chat_send.func(message="hi", config=_cfg())
    assert live.of("liveChat/messages", "POST")[-1]["json"]["snippet"]["liveChatId"] == "chat-pinned"


# ---------------------------------------------------------------------------
# Moderation: ids, bounds, ban kinds (behavior 15)
# ---------------------------------------------------------------------------


def test_timeout_takes_the_copied_yt_tag_and_bounds_the_seconds(live):
    ok = tools.youtube_chat_timeout.func(channel_id=f"[yt:{MALLORY_CH}]", seconds=120, config=_cfg())
    assert ok == f'Timed out {MALLORY_CH} for 120s in the YouTube live chat of "Silk live".'
    snippet = live.of("liveChat/bans", "POST")[-1]["json"]["snippet"]
    assert (snippet["type"], snippet["banDurationSeconds"]) == ("temporary", 120)

    before = len(live.calls)
    for bad in (9, 86401, 0, -5):
        result = tools.youtube_chat_timeout.func(channel_id=MALLORY_CH, seconds=bad, config=_cfg())
        assert result.startswith("[Error]: seconds must be 10 to 86400")
    by_name = tools.youtube_chat_timeout.func(channel_id="mallory", config=_cfg())
    assert by_name.startswith("[Error]") and "not a YouTube channel id" in by_name
    assert len(live.calls) == before

    edge = tools.youtube_chat_timeout.func(channel_id=MALLORY_CH, seconds=86400, config=_cfg())
    assert edge.startswith("Timed out")


def test_ban_then_unban_round_trip_and_unknown_bans_point_to_studio(live):
    assert tools.youtube_chat_ban.func(channel_id=MALLORY_CH, config=_cfg()).startswith("Banned")
    assert live.of("liveChat/bans", "POST")[-1]["json"]["snippet"]["type"] == "permanent"
    assert tools.youtube_chat_unban.func(channel_id=MALLORY_CH, config=_cfg()) == f"Lifted the bot's ban on {MALLORY_CH}."
    assert live.deleted_bans == {"ban-1"}
    unknown = tools.youtube_chat_unban.func(channel_id=ALICE_CH, config=_cfg())
    assert unknown.startswith("[Error]") and "YouTube Studio" in unknown


def test_delete_takes_the_copied_msg_tag(live):
    result = tools.youtube_chat_delete_message.func(message_id="[msg:LCC.EhwKGkNKUzRq]", config=_cfg())
    assert result == 'Deleted message LCC.EhwKGkNKUzRq from the YouTube live chat of "Silk live".'
    assert live.of("liveChat/messages", "DELETE")[-1]["params"] == {"id": "LCC.EhwKGkNKUzRq"}

    live.errors[("DELETE", "liveChat/messages")] = (403, "modificationNotAllowed")
    refused = tools.youtube_chat_delete_message.func(message_id="LCC.EhwKGkNKUzRq", config=_cfg())
    assert refused.startswith("[Error]") and "owner or a moderator" in refused


# ---------------------------------------------------------------------------
# Behavior 20: chatter history, scoped to the caller's account
# ---------------------------------------------------------------------------


def test_chatter_log_returns_that_chatters_lines_for_the_caller_only(live):
    # Stamped relative to the real clock: the store's retention and query
    # windows run on it (a fixed date would age out of the log).
    now = datetime.now(timezone.utc)
    live.pages.append(
        {
            "items": [
                _text_item("m1", ALICE_CH, "alice", "first <b>", now - timedelta(seconds=40)),
                _text_item("m2", MALLORY_CH, "mallory", "spam", now - timedelta(seconds=30)),
                _text_item("m3", ALICE_CH, "alice", "second", now - timedelta(seconds=20)),
            ]
        }
    )
    _poll(live, now=now)
    log = tools.youtube_chat_get_chatter_log.func(channel_id=f"yt:{ALICE_CH}", hours=48, config=_cfg())
    assert "Latest 2 message(s)" in log
    assert log.index("first") < log.index("second") and "spam" not in log
    assert "[msg:m1]" in log and "<untrusted_chat_messages>" in log

    # After the chat ends the log is still found (keyed by the streamer).
    yt.clear_attachment("alice")
    assert "Latest 2 message(s)" in tools.youtube_chat_get_chatter_log.func(
        channel_id=ALICE_CH, hours=48, config=_cfg()
    )
    # Another account sees nothing of alice's log.
    other = tools.youtube_chat_get_chatter_log.func(channel_id=ALICE_CH, hours=48, config=_cfg("bob"))
    assert other.startswith("[Error]: no YouTube chat has been read")


def test_chatter_log_matches_the_channel_id_never_a_display_name(live):
    """A YouTube display name is free text: an impostor naming themselves
    after alice's channel id must not land in alice's history."""
    now = datetime.now(timezone.utc)
    live.pages.append(
        {
            "items": [
                _text_item("m1", ALICE_CH, "alice", "hello", now - timedelta(seconds=30)),
                _text_item("m2", MALLORY_CH, ALICE_CH, "impostor spam", now - timedelta(seconds=20)),
            ]
        }
    )
    _poll(live, now=now)
    log = tools.youtube_chat_get_chatter_log.func(channel_id=ALICE_CH, hours=48, config=_cfg())
    assert "Latest 1 message(s)" in log and "hello" in log and "impostor" not in log


def test_status_reports_the_attached_stream(live):
    live.videos["vid00000001"] = {
        "id": "vid00000001",
        "snippet": {"title": "Silk live", "channelTitle": "Silk", "channelId": STREAMER_CH},
        "liveStreamingDetails": {"activeLiveChatId": "chat-1", "concurrentViewers": "321"},
    }
    status = tools.youtube_chat_status.func(config=_cfg())
    assert '"concurrent_viewers": "321"' in status and '"live": true' in status
    assert '"url": "https://www.youtube.com/watch?v=vid00000001"' in status
