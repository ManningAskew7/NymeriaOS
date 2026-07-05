from __future__ import annotations

import asyncio
from typing import Any

from nymeria.triggers.teams_bot import (
    NymeriaTeamsBot,
    TeamsCommand,
    TeamsReplyTarget,
    activity_from_payload,
    activity_mentions_bot,
    make_platform_chat_id,
    make_platform_user_id,
    make_thread_id,
    normalize_conversation_id,
    strip_teams_mentions,
)


class FakeTeamsClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send_message(self, target: TeamsReplyTarget, text: str) -> None:
        self.sent.append(
            {
                "conversation_id": target.conversation_id,
                "activity_id": target.activity_id,
                "platform_chat_id": target.platform_chat_id,
                "text": text,
            }
        )

    async def close(self) -> None:
        self.closed = True


class FakeTeamsAPI:
    def __init__(self, *, resolved_user: str | None = "user-1") -> None:
        self.resolved_user = resolved_user
        self.bindings: list[dict[str, Any]] = []
        self.resolve_calls: list[dict[str, str]] = []
        self.chat_stream_calls: list[dict[str, str]] = []
        self.claimed_links: list[dict[str, str]] = []
        self.claimed_binds: list[dict[str, str]] = []
        self.unbound: list[dict[str, str | None]] = []
        self.stopped: list[dict[str, str | None]] = []
        self.command_calls: list[dict[str, Any]] = []
        self.command_result: dict[str, Any] = {
            "success": True,
            "markdown": "**backend says hi**",
        }

    async def execute_command(
        self,
        command: str,
        *,
        thread_id: str | None = None,
        source: str = "user",
        actor: str | None = None,
        surface: str | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.command_calls.append(
            {
                "command": command,
                "thread_id": thread_id,
                "source": source,
                "actor": actor,
                "surface": surface,
                "user_id": user_id,
            }
        )
        return self.command_result

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
            yield {"type": "response", "content": "teams "}
            yield {"type": "response", "content": "reply"}
            yield {"type": "done"}

        return events()

    async def chat(self, message: str, thread_id: str, user_id: str) -> dict[str, Any]:
        return {"response": "fallback", "tool_call_count": 0}


def payload(
    *,
    activity_id: str = "activity-1",
    text: str = "hello",
    conversation_id: str = "19:direct",
    conversation_type: str = "personal",
    from_id: str = "29:user",
    aad_object_id: str = "aad-1",
    mentioned: bool = False,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "type": "message",
        "id": activity_id,
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/amer/",
        "conversation": {
            "id": conversation_id,
            "conversationType": conversation_type,
            "tenantId": "tenant-1",
        },
        "from": {
            "id": from_id,
            "name": "Alice",
            "aadObjectId": aad_object_id,
        },
        "recipient": {
            "id": "28:bot",
            "name": "Nymeria",
        },
        "text": text,
    }
    if mentioned:
        data["entities"] = [
            {
                "type": "mention",
                "mentioned": {"id": "28:bot", "name": "Nymeria"},
                "text": "<at>Nymeria</at>",
            }
        ]
    return data


def test_teams_ids_and_command_parser_are_stable() -> None:
    assert normalize_conversation_id("19:abc;messageid=root-1") == "19:abc"
    assert make_platform_user_id("tenant-1", aad_object_id="aad-1") == "tenant-1:aad-1"
    assert (
        make_platform_chat_id("tenant-1", "19:abc;messageid=root-1", "root-1")
        == "teams:tenant-1:19:abc:root-1"
    )
    assert (
        make_thread_id(
            "tenant-1",
            "19:abc",
            conversation_type="personal",
            user_id="aad/1",
        )
        == "teams_dm_tenant-1_aad-1"
    )
    assert (
        make_thread_id("tenant-1", "19:abc", thread_id="root/1")
        == "teams_tenant-1_19-abc_thread_root-1"
    )

    assert TeamsCommand.parse("link abc").name == "link"
    assert TeamsCommand.parse("link_abc").arg == "abc"
    assert TeamsCommand.parse("<at>Nymeria</at> bind code-1", bot_name="Nymeria").arg == "code-1"
    assert TeamsCommand.parse("/stop").name == "stop"
    assert TeamsCommand.parse("stop that") is None


def test_activity_payload_parsing_mentions_and_text_cleanup() -> None:
    activity = activity_from_payload(
        payload(text="<at>Nymeria</at> hello<br>world", mentioned=True)
    )

    assert activity is not None
    assert activity.text == "Nymeria hello\nworld"
    assert activity.platform_user_id == "tenant-1:aad-1"
    assert activity_mentions_bot(activity)
    assert (
        strip_teams_mentions(
            activity.raw_text,
            bot_id=activity.recipient_id,
            bot_name=activity.recipient_name,
        )
        == "hello\nworld"
    )


def test_direct_message_streams_to_native_teams_dm_thread() -> None:
    bot = NymeriaTeamsBot(api=FakeTeamsAPI(), teams_client=FakeTeamsClient())
    api = bot.api
    client = bot.teams
    assert isinstance(api, FakeTeamsAPI)
    assert isinstance(client, FakeTeamsClient)

    asyncio.run(bot.handle_payload(payload(text="hello")))

    assert api.resolve_calls == [
        {"provider": "teams", "provider_user_id": "tenant-1:aad-1"}
    ]
    assert api.chat_stream_calls == [
        {
            "message": "hello",
            "thread_id": "teams_dm_tenant-1_aad-1",
            "user_id": "user-1",
        }
    ]
    assert client.sent == [
        {
            "conversation_id": "19:direct",
            "activity_id": "activity-1",
            "platform_chat_id": "teams:tenant-1:19:direct",
            "text": "teams reply",
        }
    ]


def test_channel_mention_replies_in_thread_and_allows_followup() -> None:
    api = FakeTeamsAPI()
    client = FakeTeamsClient()
    bot = NymeriaTeamsBot(api=api, teams_client=client)

    async def run() -> None:
        await bot.handle_payload(
            payload(
                activity_id="activity-2",
                text="<at>Nymeria</at> hello",
                conversation_id="19:channel;messageid=root-1",
                conversation_type="channel",
                mentioned=True,
            )
        )
        await bot.handle_payload(
            payload(
                activity_id="activity-3",
                text="follow up",
                conversation_id="19:channel;messageid=root-1",
                conversation_type="channel",
            )
        )

    asyncio.run(run())

    assert len(api.chat_stream_calls) == 2
    assert api.chat_stream_calls[0] == {
        "message": "[Microsoft Teams Alice in channel]\nhello",
        "thread_id": "teams_tenant-1_19-channel_thread_root-1",
        "user_id": "user-1",
    }
    assert api.chat_stream_calls[1]["message"] == "[Microsoft Teams Alice in channel]\nfollow up"
    assert client.sent[0]["platform_chat_id"] == "teams:tenant-1:19:channel:root-1"
    assert client.sent[1]["platform_chat_id"] == "teams:tenant-1:19:channel:root-1"


def test_unmentioned_channel_message_is_ignored_by_default() -> None:
    api = FakeTeamsAPI()
    client = FakeTeamsClient()
    bot = NymeriaTeamsBot(api=api, teams_client=client)

    asyncio.run(
        bot.handle_payload(
            payload(
                text="hello",
                conversation_id="19:channel",
                conversation_type="channel",
            )
        )
    )

    assert api.chat_stream_calls == []
    assert client.sent == []


def test_link_command_claims_platform_code() -> None:
    api = FakeTeamsAPI(resolved_user=None)
    client = FakeTeamsClient()
    bot = NymeriaTeamsBot(api=api, teams_client=client)

    asyncio.run(
        bot.handle_payload(
            payload(
                text="<at>Nymeria</at> link code-123",
                conversation_id="19:channel",
                conversation_type="channel",
                mentioned=True,
            )
        )
    )

    assert api.claimed_links == [
        {
            "code": "code-123",
            "provider": "teams",
            "platform_user_id": "tenant-1:aad-1",
        }
    ]
    assert client.sent[0]["text"] == "Linked this Microsoft Teams account to Nymeria user `user-1`."


def test_unlinked_sender_is_rejected_without_agent_call() -> None:
    api = FakeTeamsAPI(resolved_user=None)
    client = FakeTeamsClient()
    bot = NymeriaTeamsBot(api=api, teams_client=client)

    asyncio.run(bot.handle_payload(payload(text="hello")))

    assert api.chat_stream_calls == []
    assert "not linked to a Nymeria user" in client.sent[0]["text"]


def test_dm_slash_command_is_forwarded_to_backend() -> None:
    api = FakeTeamsAPI()
    client = FakeTeamsClient()
    bot = NymeriaTeamsBot(api=api, teams_client=client)

    asyncio.run(bot.handle_payload(payload(text="/status")))

    assert api.command_calls == [
        {
            "command": "/status",
            "thread_id": "teams_dm_tenant-1_aad-1",
            "source": "user",
            "actor": "user",
            "surface": "teams",
            "user_id": "user-1",
        }
    ]
    # Handled by the backend registry, not the agent chat path.
    assert api.chat_stream_calls == []
    assert client.sent[0]["text"] == "**backend says hi**"


def test_channel_slash_command_forwards_clean_text_without_platform_prefix() -> None:
    api = FakeTeamsAPI()
    client = FakeTeamsClient()
    bot = NymeriaTeamsBot(api=api, teams_client=client)

    asyncio.run(
        bot.handle_payload(
            payload(
                text="<at>Nymeria</at> /todos list",
                conversation_id="19:channel;messageid=root-1",
                conversation_type="channel",
                mentioned=True,
            )
        )
    )

    assert len(api.command_calls) == 1
    # No "[Microsoft Teams ...]" chat prefix on the forwarded command.
    assert api.command_calls[0]["command"] == "/todos list"
    assert api.command_calls[0]["thread_id"] == "teams_tenant-1_19-channel_thread_root-1"
    assert api.chat_stream_calls == []


def test_chat_stream_kind_slash_command_falls_through_to_chat() -> None:
    api = FakeTeamsAPI()
    client = FakeTeamsClient()
    bot = NymeriaTeamsBot(api=api, teams_client=client)
    api.command_result = {
        "success": False,
        "markdown": "**Error:** `/skill` is handled outside the command service.",
        "data": {"execution_kind": "chat_stream"},
    }

    asyncio.run(bot.handle_payload(payload(text="/skill research")))

    # The refusal marker re-routes the raw text into the chat path.
    assert len(api.command_calls) == 1
    assert api.chat_stream_calls == [
        {
            "message": "/skill research",
            "thread_id": "teams_dm_tenant-1_aad-1",
            "user_id": "user-1",
        }
    ]
    assert client.sent[0]["text"] == "teams reply"


def test_local_stop_command_still_wins_over_passthrough() -> None:
    api = FakeTeamsAPI()
    client = FakeTeamsClient()
    bot = NymeriaTeamsBot(api=api, teams_client=client)

    asyncio.run(bot.handle_payload(payload(text="/stop")))

    assert api.stopped == [{"thread_id": "teams_dm_tenant-1_aad-1", "user_id": "user-1"}]
    assert api.command_calls == []


def test_unlinked_slash_command_is_rejected_without_backend_call() -> None:
    api = FakeTeamsAPI(resolved_user=None)
    client = FakeTeamsClient()
    bot = NymeriaTeamsBot(api=api, teams_client=client)

    asyncio.run(bot.handle_payload(payload(text="/status")))

    assert api.command_calls == []
    assert api.chat_stream_calls == []
    assert "not linked to a Nymeria user" in client.sent[0]["text"]


def test_active_chats_set_stays_bounded(monkeypatch) -> None:
    # F7: the active-conversation set must not grow without bound (the
    # _remember_active_thread cap-and-evict pattern).
    monkeypatch.setattr("nymeria.triggers.teams_bot.ACTIVE_CHAT_MAX", 10)
    bot = NymeriaTeamsBot(api=FakeTeamsAPI(), teams_client=FakeTeamsClient())

    for i in range(50):
        bot._remember_active_chat(f"chat-{i}")

    assert len(bot._active_chats) <= 10
    assert bot._active_chats  # eviction keeps the set non-empty, not cleared
