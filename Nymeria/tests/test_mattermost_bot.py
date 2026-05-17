from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

from nymeria.triggers.mattermost_bot import (
    MattermostCommand,
    MattermostReplyTarget,
    NymeriaMattermostBot,
    make_platform_chat_id,
    make_platform_user_id,
    make_server_key,
    make_thread_id,
    post_from_websocket_event,
    post_mentions_bot,
    strip_bot_mention,
)


class FakeMattermostClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def get_me(self):
        return {"id": "BOTID", "username": "nymeria"}

    async def create_post(self, *, channel_id, message, root_id=None):
        self.sent.append({"channel_id": channel_id, "message": message, "root_id": root_id})
        return {"id": f"bot-{len(self.sent)}"}

    async def websocket_events(self):  # pragma: no cover - start loop only.
        if False:
            yield {}

    async def close(self):
        return None


class FakeAPI:
    def __init__(self, *, resolved_user: str | None = "user-1") -> None:
        self.resolved_user = resolved_user
        self.chat_stream_calls: list[dict] = []
        self.claimed_links: list[dict] = []
        self.claimed_binds: list[dict] = []
        self.unbound: list[dict] = []
        self.stopped: list[dict] = []
        self.last_resolve: dict | None = None

    async def resolve_platform_user(self, platform: str, platform_user_id: str):
        self.last_resolve = {
            "platform": platform,
            "platform_user_id": platform_user_id,
        }
        return self.resolved_user

    async def list_chatapp_bindings(self, provider: str):
        self.last_bindings_provider = provider
        return []

    async def chat_stream(self, message, thread_id, user_id):
        self.chat_stream_calls.append(
            {"message": message, "thread_id": thread_id, "user_id": user_id}
        )
        yield {"type": "response", "content": "mattermost "}
        yield {"type": "response", "content": "reply"}
        yield {"type": "done"}

    async def chat(self, *args, **kwargs):  # pragma: no cover - fallback only.
        return {"response": "fallback", "tool_call_count": 0}

    async def claim_platform_link_code(self, *, code, provider, platform_user_id):
        self.claimed_links.append(
            {
                "code": code,
                "provider": provider,
                "platform_user_id": platform_user_id,
            }
        )
        return {"user_id": "user-1"}

    async def claim_thread_bind_code(
        self,
        *,
        code,
        provider,
        platform_chat_id,
        expected_provider_user_id,
    ):
        self.claimed_binds.append(
            {
                "code": code,
                "provider": provider,
                "platform_chat_id": platform_chat_id,
                "expected_provider_user_id": expected_provider_user_id,
            }
        )
        return {"thread_id": "desktop-thread", "user_id": "user-1"}

    async def unbind_chatapp_by_chat(self, **kwargs):
        self.unbound.append(kwargs)
        return {"unbound": True, "thread_id": "desktop-thread"}

    async def stop(self, thread_id: str, user_id: str | None = None):
        self.stopped.append({"thread_id": thread_id, "user_id": user_id})
        return {"stopped": True}

    async def health(self):
        return True

    async def close(self):
        return None


def make_bot(
    api: FakeAPI | None = None,
) -> tuple[NymeriaMattermostBot, FakeAPI, FakeMattermostClient]:
    fake_api = api or FakeAPI()
    client = FakeMattermostClient()
    bot = NymeriaMattermostBot(
        fake_api,  # type: ignore[arg-type]
        base_url="https://mattermost.example.com",
        access_token="token",
        mattermost_client=client,  # type: ignore[arg-type]
    )
    bot.bot_user_id = "BOTID"
    bot.bot_username = "nymeria"
    return bot, fake_api, client


def event(
    *,
    post_id: str,
    channel_id: str,
    user_id: str = "U1",
    message: str,
    channel_type: str = "O",
    root_id: str = "",
    sender_name: str = "alice",
) -> Mapping[str, Any]:
    post = {
        "id": post_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "message": message,
        "root_id": root_id,
    }
    return {
        "event": "posted",
        "data": {
            "post": json.dumps(post),
            "channel_type": channel_type,
            "sender_name": sender_name,
        },
    }


def test_mattermost_ids_are_server_scoped_and_sanitized():
    assert make_server_key("https://mattermost.example.com") == "mattermost.example.com"
    assert make_platform_user_id("mattermost.example.com", "U/1") == "mattermost.example.com:U-1"
    assert (
        make_platform_chat_id("mattermost.example.com", "C1", "root/1")
        == "mattermost.example.com:C1:root-1"
    )
    assert (
        make_thread_id("mattermost.example.com", "D1", user_id="U1", is_dm=True)
        == "mattermost_dm_mattermost.example.com_U1"
    )
    assert (
        make_thread_id("mattermost.example.com", "C1", root_id="root/1")
        == "mattermost_mattermost.example.com_C1_thread_root-1"
    )


def test_mattermost_event_parse_and_mentions():
    parsed = post_from_websocket_event(
        event(post_id="p1", channel_id="C1", message="@nymeria status?")
    )
    assert parsed is not None
    assert parsed.id == "p1"
    assert parsed.channel_type == "O"
    assert post_mentions_bot(parsed, bot_username="nymeria")
    assert strip_bot_mention("@nymeria status?", bot_username="nymeria") == "status?"


def test_mattermost_command_parser_handles_link_bind_and_exact_stop():
    assert MattermostCommand.parse("link abc").name == "link"
    assert MattermostCommand.parse("link_abc").arg == "abc"
    assert MattermostCommand.parse("@nymeria bind code-1", bot_username="nymeria").arg == "code-1"
    assert MattermostCommand.parse("stop").name == "stop"
    assert MattermostCommand.parse("stop doing that") is None


def test_dm_message_streams_to_native_mattermost_dm_thread():
    bot, api, client = make_bot()

    asyncio.run(
        bot.handle_websocket_event(
            event(post_id="p1", channel_id="D1", message="hello", channel_type="D")
        )
    )

    assert api.last_resolve == {
        "platform": "mattermost",
        "platform_user_id": "mattermost.example.com:U1",
    }
    assert api.chat_stream_calls == [
        {
            "message": "hello",
            "thread_id": "mattermost_dm_mattermost.example.com_U1",
            "user_id": "user-1",
        }
    ]
    assert client.sent == [
        {"channel_id": "D1", "message": "mattermost reply", "root_id": None}
    ]


def test_channel_mention_replies_in_thread_and_allows_followup():
    bot, api, client = make_bot()

    async def run():
        await bot.handle_websocket_event(
            event(post_id="p2", channel_id="C1", message="@nymeria hello")
        )
        await bot.handle_websocket_event(
            event(post_id="p3", channel_id="C1", message="follow up", root_id="p2")
        )

    asyncio.run(run())

    assert len(api.chat_stream_calls) == 2
    assert api.chat_stream_calls[0]["message"] == "[Mattermost @alice in C1]\nhello"
    assert api.chat_stream_calls[0]["thread_id"] == "mattermost_mattermost.example.com_C1_thread_p2"
    assert api.chat_stream_calls[1]["message"] == "[Mattermost @alice in C1]\nfollow up"
    assert client.sent[0]["root_id"] == "p2"
    assert client.sent[1]["root_id"] == "p2"


def test_unmentioned_channel_message_is_ignored_by_default():
    bot, api, client = make_bot()

    asyncio.run(
        bot.handle_websocket_event(
            event(post_id="p4", channel_id="C1", message="hello")
        )
    )

    assert api.chat_stream_calls == []
    assert client.sent == []


def test_unlinked_user_gets_rejection_without_agent_call():
    bot, api, client = make_bot(FakeAPI(resolved_user=None))

    asyncio.run(
        bot.handle_websocket_event(
            event(post_id="p5", channel_id="D1", message="hello", channel_type="D")
        )
    )

    assert api.chat_stream_calls == []
    assert "not linked" in client.sent[0]["message"]
    assert "mattermost.example.com:U1" in client.sent[0]["message"]


def test_link_and_bind_commands_claim_codes():
    bot, api, client = make_bot()

    async def run():
        await bot.handle_websocket_event(
            event(post_id="p6", channel_id="D1", message="link abc123", channel_type="D")
        )
        await bot.handle_websocket_event(
            event(post_id="p7", channel_id="C1", message="@nymeria bind bind123")
        )

    asyncio.run(run())

    assert api.claimed_links == [
        {
            "code": "abc123",
            "provider": "mattermost",
            "platform_user_id": "mattermost.example.com:U1",
        }
    ]
    assert api.claimed_binds == [
        {
            "code": "bind123",
            "provider": "mattermost",
            "platform_chat_id": "mattermost.example.com:C1:p7",
            "expected_provider_user_id": "mattermost.example.com:U1",
        }
    ]
    assert "Linked this Mattermost account" in client.sent[0]["message"]
    assert "Bound this Mattermost conversation" in client.sent[1]["message"]


def test_send_text_splits_large_mattermost_messages():
    bot, _api, client = make_bot()

    asyncio.run(
        bot._send_text(MattermostReplyTarget(channel_id="C1", root_id="p1"), "x" * 4001)
    )

    assert len(client.sent) == 2
    assert all(len(item["message"]) <= 4000 for item in client.sent)
