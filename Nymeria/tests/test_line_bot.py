from __future__ import annotations

import asyncio
from typing import Any

from nymeria.triggers.line_bot import (
    LineCommand,
    LineReplyTarget,
    NymeriaLineBot,
    event_from_payload,
    event_mentions_bot,
    make_platform_chat_id,
    make_platform_user_id,
    make_thread_id,
    strip_line_mentions,
)


class FakeLineClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_message(self, target: LineReplyTarget, text: str) -> None:
        self.sent.append(
            {
                "to": target.to,
                "source_type": target.source_type,
                "platform_chat_id": target.platform_chat_id,
                "text": text,
            }
        )

    async def close(self) -> None:
        self.closed = True


class FakeLineAPI:
    def __init__(self, *, resolved_user: str | None = "user-1") -> None:
        self.resolved_user = resolved_user
        self.bindings: list[dict[str, Any]] = []
        self.resolve_calls: list[dict[str, str]] = []
        self.chat_stream_calls: list[dict[str, str]] = []
        self.claimed_links: list[dict[str, str]] = []
        self.claimed_binds: list[dict[str, str]] = []
        self.unbound: list[dict[str, str | None]] = []
        self.stopped: list[dict[str, str | None]] = []

    async def resolve_platform_user(self, provider: str, provider_user_id: str) -> str | None:
        self.resolve_calls.append(
            {
                "provider": provider,
                "provider_user_id": provider_user_id,
            }
        )
        return self.resolved_user

    async def list_chatapp_bindings(self, provider: str) -> list[dict[str, Any]]:
        return [entry for entry in self.bindings if entry["provider"] == provider]

    async def claim_platform_link_code(
        self,
        *,
        code: str,
        provider: str,
        platform_user_id: str,
    ) -> dict[str, Any]:
        self.claimed_links.append(
            {
                "code": code,
                "provider": provider,
                "platform_user_id": platform_user_id,
            }
        )
        return {"user_id": "user-1", "provider": provider}

    async def claim_thread_bind_code(
        self,
        *,
        code: str,
        provider: str,
        platform_chat_id: str,
        expected_provider_user_id: str,
    ) -> dict[str, Any]:
        self.claimed_binds.append(
            {
                "code": code,
                "provider": provider,
                "platform_chat_id": platform_chat_id,
                "expected_provider_user_id": expected_provider_user_id,
            }
        )
        return {"binding_id": 1, "thread_id": "desktop-thread", "user_id": "user-1"}

    async def unbind_chatapp_by_chat(
        self,
        *,
        provider: str,
        platform_chat_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.unbound.append(
            {
                "provider": provider,
                "platform_chat_id": platform_chat_id,
                "user_id": user_id,
            }
        )
        return {"unbound": True, "thread_id": "desktop-thread"}

    async def stop(self, thread_id: str, user_id: str | None = None) -> dict[str, Any]:
        self.stopped.append({"thread_id": thread_id, "user_id": user_id})
        return {"status": "stopping", "thread_id": thread_id}

    def chat_stream(self, message: str, thread_id: str, user_id: str):
        self.chat_stream_calls.append(
            {
                "message": message,
                "thread_id": thread_id,
                "user_id": user_id,
            }
        )

        async def events():
            yield {"type": "response", "content": "line "}
            yield {"type": "response", "content": "reply"}
            yield {"type": "done"}

        return events()

    async def chat(self, message: str, thread_id: str, user_id: str) -> dict[str, Any]:
        return {"response": "fallback", "tool_call_count": 0}


def payload(
    *,
    event_id: str = "event-1",
    text: str = "hello",
    source_type: str = "user",
    user_id: str = "U123",
    group_id: str = "Cgroup",
    room_id: str = "Rroom",
    message_id: str = "msg-1",
    destination: str = "Ubot",
    mentioned: bool = False,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "text",
        "id": message_id,
        "text": text,
    }
    if mentioned:
        message["mention"] = {
            "mentionees": [
                {
                    "index": 0,
                    "length": 8,
                    "userId": destination,
                    "type": "user",
                    "isSelf": True,
                }
            ]
        }
    source = {"type": source_type, "userId": user_id}
    if source_type == "group":
        source["groupId"] = group_id
    elif source_type == "room":
        source["roomId"] = room_id
    return {
        "destination": destination,
        "events": [
            {
                "type": "message",
                "mode": "active",
                "timestamp": 1778889600000,
                "source": source,
                "message": message,
                "webhookEventId": event_id,
                "replyToken": "reply-1",
            }
        ],
    }


def test_line_ids_and_command_parser_are_stable() -> None:
    assert make_platform_user_id("U123") == "U123"
    assert make_platform_chat_id("user", "U123") == "line:user:U123"
    assert make_platform_chat_id("group", "C123") == "line:group:C123"
    assert make_thread_id("user", "U123", user_id="U123") == "line_dm_U123"
    assert make_thread_id("group", "C123", user_id="U123") == "line_group_C123"
    assert make_thread_id("room", "R123", user_id="U123") == "line_room_R123"

    assert LineCommand.parse("link abc").name == "link"
    assert LineCommand.parse("link_abc").arg == "abc"
    assert LineCommand.parse("@Nymeria bind code-1", bot_name="Nymeria").arg == "code-1"
    assert LineCommand.parse("/stop").name == "stop"
    assert LineCommand.parse("stop that") is None


def test_line_payload_parsing_mentions_and_text_cleanup() -> None:
    event = event_from_payload(
        payload(text="@Nymeria hello", source_type="group", mentioned=True)["events"][0],
        destination="Ubot",
        bot_name="Nymeria",
    )

    assert event is not None
    assert event.text == "hello"
    assert event.platform_user_id == "U123"
    assert event_mentions_bot(event, bot_name="Nymeria", bot_user_id="Ubot")
    assert event.target_id == "Cgroup"
    assert (
        strip_line_mentions(event.raw_text, mention=event.mention, bot_name="Nymeria")
        == "hello"
    )


def test_direct_message_streams_to_native_line_dm_thread() -> None:
    bot = NymeriaLineBot(api=FakeLineAPI(), line_client=FakeLineClient())
    api = bot.api
    client = bot.line
    assert isinstance(api, FakeLineAPI)
    assert isinstance(client, FakeLineClient)

    asyncio.run(bot.handle_payload(payload(text="hello")))

    assert api.resolve_calls == [{"provider": "line", "provider_user_id": "U123"}]
    assert api.chat_stream_calls == [
        {
            "message": "hello",
            "thread_id": "line_dm_U123",
            "user_id": "user-1",
        }
    ]
    assert client.sent == [
        {
            "to": "U123",
            "source_type": "user",
            "platform_chat_id": "line:user:U123",
            "text": "line reply",
        }
    ]


def test_group_mention_replies_and_allows_followup() -> None:
    api = FakeLineAPI()
    client = FakeLineClient()
    bot = NymeriaLineBot(api=api, line_client=client)

    async def run() -> None:
        await bot.handle_payload(
            payload(
                event_id="event-2",
                text="@Nymeria hello",
                source_type="group",
                mentioned=True,
            )
        )
        await bot.handle_payload(
            payload(
                event_id="event-3",
                text="follow up",
                source_type="group",
                message_id="msg-2",
            )
        )

    asyncio.run(run())

    assert api.chat_stream_calls == [
        {
            "message": "[LINE U123 in Cgroup]\nhello",
            "thread_id": "line_group_Cgroup",
            "user_id": "user-1",
        },
        {
            "message": "[LINE U123 in Cgroup]\nfollow up",
            "thread_id": "line_group_Cgroup",
            "user_id": "user-1",
        },
    ]
    assert client.sent[0]["to"] == "Cgroup"
    assert client.sent[0]["platform_chat_id"] == "line:group:Cgroup"
    assert client.sent[1]["platform_chat_id"] == "line:group:Cgroup"


def test_unmentioned_group_message_is_ignored_by_default() -> None:
    api = FakeLineAPI()
    client = FakeLineClient()
    bot = NymeriaLineBot(api=api, line_client=client)

    asyncio.run(bot.handle_payload(payload(text="hello", source_type="group")))

    assert api.chat_stream_calls == []
    assert client.sent == []


def test_link_command_claims_platform_code() -> None:
    api = FakeLineAPI(resolved_user=None)
    client = FakeLineClient()
    bot = NymeriaLineBot(api=api, line_client=client)

    asyncio.run(
        bot.handle_payload(
            payload(
                text="@Nymeria link code-123",
                source_type="group",
                mentioned=True,
            )
        )
    )

    assert api.claimed_links == [
        {
            "code": "code-123",
            "provider": "line",
            "platform_user_id": "U123",
        }
    ]
    assert client.sent[0]["text"] == "Linked this LINE account to Nymeria user `user-1`."


def test_unlinked_sender_is_rejected_without_agent_call() -> None:
    api = FakeLineAPI(resolved_user=None)
    client = FakeLineClient()
    bot = NymeriaLineBot(api=api, line_client=client)

    asyncio.run(bot.handle_payload(payload(text="hello")))

    assert api.chat_stream_calls == []
    assert "not linked to a Nymeria user" in client.sent[0]["text"]
