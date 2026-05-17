from __future__ import annotations

import asyncio
from typing import Any

from nymeria.triggers.google_chat_bot import (
    GoogleChatCommand,
    GoogleChatReplyTarget,
    NymeriaGoogleChatBot,
    event_from_payload,
    event_mentions_app,
    make_platform_chat_id,
    make_platform_user_id,
    make_thread_id,
    strip_googlechat_mentions,
)


class FakeGoogleChatClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_message(self, target: GoogleChatReplyTarget, text: str) -> None:
        self.sent.append(
            {
                "space_name": target.space_name,
                "thread_name": target.thread_name,
                "platform_chat_id": target.platform_chat_id,
                "text": text,
            }
        )

    async def close(self) -> None:
        self.closed = True


class FakeGoogleChatAPI:
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
            yield {"type": "response", "content": "google "}
            yield {"type": "response", "content": "reply"}
            yield {"type": "done"}

        return events()

    async def chat(self, message: str, thread_id: str, user_id: str) -> dict[str, Any]:
        return {"response": "fallback", "tool_call_count": 0}


def payload(
    *,
    event_id: str = "event-1",
    text: str = "hello",
    argument_text: str = "",
    space_name: str = "spaces/AAA",
    space_type: str = "DM",
    space_display_name: str = "",
    user_name: str = "users/123",
    user_email: str = "alice@example.com",
    user_type: str = "HUMAN",
    message_name: str = "spaces/AAA/messages/msg-1",
    thread_name: str | None = None,
    mentioned: bool = False,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "name": message_name,
        "sender": {
            "name": user_name,
            "displayName": "Alice",
            "email": user_email,
            "type": user_type,
        },
        "text": text,
    }
    if argument_text:
        message["argumentText"] = argument_text
    if thread_name:
        message["thread"] = {"name": thread_name}
    if mentioned:
        message["annotations"] = [
            {
                "type": "USER_MENTION",
                "userMention": {
                    "user": {
                        "name": "users/app",
                        "displayName": "Nymeria",
                        "type": "BOT",
                    }
                },
            }
        ]
    return {
        "type": "MESSAGE",
        "eventId": event_id,
        "eventTime": f"2026-05-16T00:00:{event_id[-1]}Z",
        "space": {
            "name": space_name,
            "type": space_type,
            "displayName": space_display_name,
        },
        "message": message,
    }


def test_googlechat_ids_and_command_parser_are_stable() -> None:
    assert make_platform_user_id("users/123", "alice@example.com") == "users/123"
    assert make_platform_user_id("", "Alice@Example.com") == "email:alice@example.com"
    assert (
        make_platform_chat_id("spaces/AAA", "spaces/AAA/threads/T1")
        == "googlechat:spaces/AAA:spaces/AAA/threads/T1"
    )
    assert (
        make_thread_id("spaces/AAA", space_type="DM", user_id="users/123")
        == "googlechat_dm_users-123"
    )
    assert (
        make_thread_id("spaces/AAA", thread_name="spaces/AAA/threads/T1")
        == "googlechat_AAA_thread_T1"
    )

    assert GoogleChatCommand.parse("link abc").name == "link"
    assert GoogleChatCommand.parse("link_abc").arg == "abc"
    assert GoogleChatCommand.parse("<users/app> bind code-1", bot_name="Nymeria").arg == "code-1"
    assert GoogleChatCommand.parse("/stop").name == "stop"
    assert GoogleChatCommand.parse("stop that") is None


def test_message_payload_parsing_mentions_and_text_cleanup() -> None:
    event = event_from_payload(
        payload(
            text="<users/app> hello",
            argument_text="hello",
            thread_name="spaces/AAA/threads/T1",
            mentioned=True,
        )
    )

    assert event is not None
    assert event.text == "hello"
    assert event.platform_user_id == "users/123"
    assert event_mentions_app(event)
    assert event.thread_name == "spaces/AAA/threads/T1"
    assert strip_googlechat_mentions(event.raw_text, bot_name="Nymeria") == "hello"


def test_direct_message_streams_to_native_googlechat_dm_thread() -> None:
    bot = NymeriaGoogleChatBot(
        api=FakeGoogleChatAPI(),
        googlechat_client=FakeGoogleChatClient(),
    )
    api = bot.api
    client = bot.googlechat
    assert isinstance(api, FakeGoogleChatAPI)
    assert isinstance(client, FakeGoogleChatClient)

    asyncio.run(bot.handle_payload(payload(text="hello")))

    assert api.resolve_calls == [
        {"provider": "googlechat", "provider_user_id": "users/123"}
    ]
    assert api.chat_stream_calls == [
        {
            "message": "hello",
            "thread_id": "googlechat_dm_users-123",
            "user_id": "user-1",
        }
    ]
    assert client.sent == [
        {
            "space_name": "spaces/AAA",
            "thread_name": None,
            "platform_chat_id": "googlechat:spaces/AAA",
            "text": "google reply",
        }
    ]


def test_space_mention_replies_in_thread_and_allows_followup() -> None:
    api = FakeGoogleChatAPI()
    client = FakeGoogleChatClient()
    bot = NymeriaGoogleChatBot(api=api, googlechat_client=client)

    async def run() -> None:
        await bot.handle_payload(
            payload(
                event_id="event-2",
                text="<users/app> hello",
                argument_text="hello",
                space_type="ROOM",
                space_display_name="Ops",
                thread_name="spaces/AAA/threads/T1",
                mentioned=True,
            )
        )
        await bot.handle_payload(
            payload(
                event_id="event-3",
                text="follow up",
                space_type="ROOM",
                space_display_name="Ops",
                message_name="spaces/AAA/messages/msg-2",
                thread_name="spaces/AAA/threads/T1",
            )
        )

    asyncio.run(run())

    assert len(api.chat_stream_calls) == 2
    assert api.chat_stream_calls[0] == {
        "message": "[Google Chat Alice in Ops]\nhello",
        "thread_id": "googlechat_AAA_thread_T1",
        "user_id": "user-1",
    }
    assert api.chat_stream_calls[1]["message"] == "[Google Chat Alice in Ops]\nfollow up"
    assert client.sent[0]["platform_chat_id"] == "googlechat:spaces/AAA:spaces/AAA/threads/T1"
    assert client.sent[1]["platform_chat_id"] == "googlechat:spaces/AAA:spaces/AAA/threads/T1"


def test_unmentioned_space_message_is_ignored_by_default() -> None:
    api = FakeGoogleChatAPI()
    client = FakeGoogleChatClient()
    bot = NymeriaGoogleChatBot(api=api, googlechat_client=client)

    asyncio.run(
        bot.handle_payload(
            payload(
                text="hello",
                space_type="ROOM",
                space_display_name="Ops",
            )
        )
    )

    assert api.chat_stream_calls == []
    assert client.sent == []


def test_link_command_claims_platform_code() -> None:
    api = FakeGoogleChatAPI(resolved_user=None)
    client = FakeGoogleChatClient()
    bot = NymeriaGoogleChatBot(api=api, googlechat_client=client)

    asyncio.run(
        bot.handle_payload(
            payload(
                text="<users/app> link code-123",
                argument_text="link code-123",
                space_type="ROOM",
                space_display_name="Ops",
                mentioned=True,
            )
        )
    )

    assert api.claimed_links == [
        {
            "code": "code-123",
            "provider": "googlechat",
            "platform_user_id": "users/123",
        }
    ]
    assert client.sent[0]["text"] == "Linked this Google Chat account to Nymeria user `user-1`."


def test_unlinked_sender_is_rejected_without_agent_call() -> None:
    api = FakeGoogleChatAPI(resolved_user=None)
    client = FakeGoogleChatClient()
    bot = NymeriaGoogleChatBot(api=api, googlechat_client=client)

    asyncio.run(bot.handle_payload(payload(text="hello")))

    assert api.chat_stream_calls == []
    assert "not linked to a Nymeria user" in client.sent[0]["text"]
