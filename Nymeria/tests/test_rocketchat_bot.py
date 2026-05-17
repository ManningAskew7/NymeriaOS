from __future__ import annotations

import asyncio
from typing import Any, Mapping

from nymeria.triggers.rocketchat_bot import (
    NymeriaRocketChatBot,
    RocketChatCommand,
    RocketChatReplyTarget,
    RocketChatRoom,
    make_platform_chat_id,
    make_platform_user_id,
    make_server_key,
    make_thread_id,
    message_from_stream_event,
    message_mentions_bot,
    strip_bot_mention,
)


class FakeRocketChatClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def get_me(self):
        return {"_id": "BOTID", "username": "nymeria", "name": "Nymeria"}

    async def get_subscriptions(self):
        return [
            RocketChatRoom(id="D1", name="alice", type="d"),
            RocketChatRoom(id="C1", name="general", type="c"),
        ]

    async def post_message(self, *, room_id, text, thread_id=None):
        self.sent.append({"room_id": room_id, "text": text, "thread_id": thread_id})
        return {"success": True}

    async def websocket_events(self, room_ids):  # pragma: no cover - start loop only.
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
        yield {"type": "response", "content": "rocket "}
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
) -> tuple[NymeriaRocketChatBot, FakeAPI, FakeRocketChatClient]:
    fake_api = api or FakeAPI()
    client = FakeRocketChatClient()
    bot = NymeriaRocketChatBot(
        fake_api,  # type: ignore[arg-type]
        base_url="https://chat.example.com",
        auth_token="token",
        user_id="BOTID",
        rocketchat_client=client,  # type: ignore[arg-type]
    )
    bot.bot_user_id = "BOTID"
    bot.bot_username = "nymeria"
    bot.bot_name = "Nymeria"
    bot._rooms = {
        "D1": RocketChatRoom(id="D1", name="alice", type="d"),
        "C1": RocketChatRoom(id="C1", name="general", type="c"),
    }
    return bot, fake_api, client


def event(
    *,
    message_id: str,
    room_id: str,
    user_id: str = "U1",
    text: str,
    thread_id: str = "",
    username: str = "alice",
    mentions: list[dict[str, Any]] | None = None,
) -> Mapping[str, Any]:
    raw = {
        "_id": message_id,
        "rid": room_id,
        "msg": text,
        "u": {"_id": user_id, "username": username, "name": "Alice"},
        "mentions": mentions or [],
    }
    if thread_id:
        raw["tmid"] = thread_id
    return {
        "msg": "changed",
        "collection": "stream-room-messages",
        "fields": {"eventName": room_id, "args": [raw]},
    }


def test_rocketchat_ids_are_server_scoped_and_sanitized():
    assert make_server_key("https://chat.example.com") == "chat.example.com"
    assert make_platform_user_id("chat.example.com", "U/1") == "chat.example.com:U-1"
    assert make_platform_chat_id("chat.example.com", "C1", "root/1") == "chat.example.com:C1:root-1"
    assert (
        make_thread_id("chat.example.com", "D1", user_id="U1", is_dm=True)
        == "rocketchat_dm_chat.example.com_U1"
    )
    assert (
        make_thread_id("chat.example.com", "C1", thread_id="root/1")
        == "rocketchat_chat.example.com_C1_thread_root-1"
    )


def test_rocketchat_event_parse_and_mentions():
    parsed = message_from_stream_event(
        event(
            message_id="m1",
            room_id="C1",
            text="@nymeria status?",
            mentions=[{"_id": "BOTID", "username": "nymeria"}],
        )
    )
    assert parsed is not None
    assert parsed.id == "m1"
    assert message_mentions_bot(parsed, bot_user_id="BOTID", bot_username="nymeria")
    assert strip_bot_mention("@nymeria status?", bot_username="nymeria") == "status?"


def test_rocketchat_command_parser_handles_link_bind_and_exact_stop():
    assert RocketChatCommand.parse("link abc").name == "link"
    assert RocketChatCommand.parse("link_abc").arg == "abc"
    assert RocketChatCommand.parse("@nymeria bind code-1", bot_username="nymeria").arg == "code-1"
    assert RocketChatCommand.parse("stop").name == "stop"
    assert RocketChatCommand.parse("stop doing that") is None


def test_dm_message_streams_to_native_rocketchat_dm_thread():
    bot, api, client = make_bot()

    asyncio.run(
        bot.handle_websocket_event(
            event(message_id="m1", room_id="D1", text="hello")
        )
    )

    assert api.last_resolve == {
        "platform": "rocketchat",
        "platform_user_id": "chat.example.com:U1",
    }
    assert api.chat_stream_calls == [
        {
            "message": "hello",
            "thread_id": "rocketchat_dm_chat.example.com_U1",
            "user_id": "user-1",
        }
    ]
    assert client.sent == [{"room_id": "D1", "text": "rocket reply", "thread_id": None}]


def test_channel_mention_replies_in_thread_and_allows_followup():
    bot, api, client = make_bot()

    async def run():
        await bot.handle_websocket_event(
            event(message_id="m2", room_id="C1", text="@nymeria hello")
        )
        await bot.handle_websocket_event(
            event(message_id="m3", room_id="C1", text="follow up", thread_id="m2")
        )

    asyncio.run(run())

    assert len(api.chat_stream_calls) == 2
    assert api.chat_stream_calls[0]["message"] == "[Rocket.Chat @alice in general]\nhello"
    assert api.chat_stream_calls[0]["thread_id"] == "rocketchat_chat.example.com_C1_thread_m2"
    assert api.chat_stream_calls[1]["message"] == "[Rocket.Chat @alice in general]\nfollow up"
    assert client.sent[0]["thread_id"] == "m2"
    assert client.sent[1]["thread_id"] == "m2"


def test_unmentioned_channel_message_is_ignored_by_default():
    bot, api, client = make_bot()

    asyncio.run(
        bot.handle_websocket_event(
            event(message_id="m4", room_id="C1", text="hello")
        )
    )

    assert api.chat_stream_calls == []
    assert client.sent == []


def test_unlinked_user_gets_rejection_without_agent_call():
    bot, api, client = make_bot(FakeAPI(resolved_user=None))

    asyncio.run(
        bot.handle_websocket_event(
            event(message_id="m5", room_id="D1", text="hello")
        )
    )

    assert api.chat_stream_calls == []
    assert "not linked" in client.sent[0]["text"]
    assert "chat.example.com:U1" in client.sent[0]["text"]


def test_link_and_bind_commands_claim_codes():
    bot, api, client = make_bot()

    async def run():
        await bot.handle_websocket_event(event(message_id="m6", room_id="D1", text="link abc123"))
        await bot.handle_websocket_event(event(message_id="m7", room_id="C1", text="@nymeria bind bind123"))

    asyncio.run(run())

    assert api.claimed_links == [
        {
            "code": "abc123",
            "provider": "rocketchat",
            "platform_user_id": "chat.example.com:U1",
        }
    ]
    assert api.claimed_binds == [
        {
            "code": "bind123",
            "provider": "rocketchat",
            "platform_chat_id": "chat.example.com:C1:m7",
            "expected_provider_user_id": "chat.example.com:U1",
        }
    ]
    assert "Linked this Rocket.Chat account" in client.sent[0]["text"]
    assert "Bound this Rocket.Chat conversation" in client.sent[1]["text"]


def test_send_text_splits_large_rocketchat_messages():
    bot, _api, client = make_bot()

    asyncio.run(
        bot._send_text(RocketChatReplyTarget(room_id="C1", thread_id="m1"), "x" * 4001)
    )

    assert len(client.sent) == 2
    assert all(len(item["text"]) <= 4000 for item in client.sent)
