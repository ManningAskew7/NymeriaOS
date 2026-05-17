from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import Any

from nymeria.triggers.webex_bot import (
    NymeriaWebexBot,
    WebexCommand,
    WebexMessage,
    WebexMessagingClient,
    extract_webhook_events,
    make_platform_chat_id,
    make_platform_user_id,
    make_thread_id,
    message_from_details,
    strip_webex_mention,
    verify_webex_signature,
)


class FakeWebex:
    def __init__(self) -> None:
        self.messages: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self.me_calls = 0

    async def get_me(self) -> dict[str, Any]:
        self.me_calls += 1
        return {"id": "bot-person", "emails": ["nymeria@webex.bot"]}

    async def get_message(self, message_id: str) -> dict[str, Any]:
        return self.messages[message_id]

    async def send_message(
        self,
        *,
        room_id: str,
        markdown: str,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        self.sent.append({"room_id": room_id, "markdown": markdown, "parent_id": parent_id})
        return {"id": f"sent-{len(self.sent)}"}


class FakeNymeriaAPI:
    def __init__(self) -> None:
        self.resolved: dict[tuple[str, str], str | None] = {}
        self.bindings: list[dict[str, Any]] = []
        self.platform_claims: list[dict[str, str]] = []
        self.thread_claims: list[dict[str, str]] = []
        self.unbinds: list[dict[str, str | None]] = []
        self.stop_calls: list[tuple[str, str | None]] = []
        self.chat_stream_calls: list[tuple[str, str, str]] = []
        self.chat_calls: list[tuple[str, str, str]] = []

    async def resolve_platform_user(self, provider: str, provider_user_id: str) -> str | None:
        return self.resolved.get((provider, provider_user_id))

    async def list_chatapp_bindings(self, provider: str) -> list[dict[str, Any]]:
        return [entry for entry in self.bindings if entry["provider"] == provider]

    async def claim_platform_link_code(
        self,
        *,
        code: str,
        provider: str,
        platform_user_id: str,
    ) -> dict[str, Any]:
        self.platform_claims.append(
            {
                "code": code,
                "provider": provider,
                "platform_user_id": platform_user_id,
            }
        )
        return {"user_id": "owner", "provider": provider}

    async def claim_thread_bind_code(
        self,
        *,
        code: str,
        provider: str,
        platform_chat_id: str,
        expected_provider_user_id: str,
    ) -> dict[str, Any]:
        self.thread_claims.append(
            {
                "code": code,
                "provider": provider,
                "platform_chat_id": platform_chat_id,
                "expected_provider_user_id": expected_provider_user_id,
            }
        )
        return {"binding_id": 1, "thread_id": "desktop-thread", "user_id": "owner"}

    async def unbind_chatapp_by_chat(
        self,
        *,
        provider: str,
        platform_chat_id: str,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.unbinds.append(
            {
                "provider": provider,
                "platform_chat_id": platform_chat_id,
                "user_id": user_id,
            }
        )
        return {"unbound": True, "thread_id": "desktop-thread"}

    async def stop(self, thread_id: str, user_id: str | None = None) -> dict[str, Any]:
        self.stop_calls.append((thread_id, user_id))
        return {"status": "stopping", "thread_id": thread_id}

    def chat_stream(self, message: str, thread_id: str, user_id: str):
        self.chat_stream_calls.append((message, thread_id, user_id))

        async def events():
            yield {"type": "response", "content": "Hello "}
            yield {"type": "response", "content": "from Nymeria"}
            yield {"type": "done"}

        return events()

    async def chat(self, message: str, thread_id: str, user_id: str) -> dict[str, Any]:
        self.chat_calls.append((message, thread_id, user_id))
        return {"response": "fallback", "tool_call_count": 0}


class FakeHTTPResponse:
    status_code = 200
    content = b'{"id":"sent-1"}'

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = data or {"id": "sent-1"}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._data


class FakeHTTPClient:
    def __init__(self) -> None:
        self.gets: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []
        self.closed = False

    async def get(self, url: str, **kwargs: Any) -> FakeHTTPResponse:
        self.gets.append({"url": url, **kwargs})
        if url.endswith("/people/me"):
            return FakeHTTPResponse({"id": "bot-person", "emails": ["bot@webex.bot"]})
        return FakeHTTPResponse({"id": "msg-1", "roomId": "room-1", "personId": "person-1", "text": "hello"})

    async def post(self, url: str, **kwargs: Any) -> FakeHTTPResponse:
        self.posts.append({"url": url, **kwargs})
        return FakeHTTPResponse({"id": "sent-1", "markdown": kwargs.get("json", {}).get("markdown")})

    async def aclose(self) -> None:
        self.closed = True


def _payload(message_id: str = "msg-1", person_id: str = "person-1") -> dict[str, Any]:
    return {
        "id": "webhook-1",
        "resource": "messages",
        "event": "created",
        "data": {
            "id": message_id,
            "roomId": "room-1",
            "personId": person_id,
            "personEmail": "user@example.com",
            "created": "2026-05-16T00:00:00Z",
        },
    }


def test_signature_verification_uses_webex_hmac_header() -> None:
    body = b'{"resource":"messages"}'
    secret = "webhook-secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()

    assert verify_webex_signature(body, None, None) is True
    assert verify_webex_signature(body, digest, secret) is True
    assert verify_webex_signature(body, f"sha1={digest}", secret) is True
    assert verify_webex_signature(body, "bad", secret) is False
    assert verify_webex_signature(body, None, secret) is False


def test_extract_webhook_event_and_message_details() -> None:
    events = extract_webhook_events(_payload())
    message = message_from_details(
        {
            "id": "msg-1",
            "roomId": "room-1",
            "personId": "person-1",
            "text": "hello",
            "roomType": "direct",
            "mentionedPeople": ["bot-person"],
        },
        fallback=events[0],
    )

    assert events[0].message_id == "msg-1"
    assert events[0].room_id == "room-1"
    assert message is not None
    assert message.display_text == "hello"
    assert message.mentioned_people == ("bot-person",)


def test_ids_and_command_parser_are_stable() -> None:
    assert make_platform_user_id("person-1") == "person-1"
    assert make_platform_chat_id("room-1") == "webex:room-1"
    assert make_platform_chat_id("room-1", "parent-1") == "webex:room-1:parent-1"
    assert make_thread_id("room-1", room_type="direct", person_id="person-1") == "webex_dm_person-1"
    assert make_thread_id("room-1") == "webex_room-1"
    assert strip_webex_mention("<@personId:bot-person|Nymeria> link abc", bot_person_id="bot-person") == "link abc"

    assert WebexCommand.parse("link abc").name == "link"
    assert WebexCommand.parse("bind_code123").arg == "code123"
    assert WebexCommand.parse("/stop").name == "stop"
    assert WebexCommand.parse("hello") is None


def test_unlinked_sender_is_rejected_without_agent_call() -> None:
    async def run() -> None:
        api = FakeNymeriaAPI()
        webex = FakeWebex()
        bot = NymeriaWebexBot(api, webex, bot_person_id="bot-person")

        await bot.handle_message(
            WebexMessage(
                message_id="msg-1",
                room_id="room-1",
                person_id="person-1",
                text="hello",
                room_type="direct",
            )
        )

        assert api.chat_stream_calls == []
        assert "not linked" in webex.sent[0]["markdown"]

    asyncio.run(run())


def test_link_bind_unbind_and_stop_commands_use_chatapp_api() -> None:
    async def run() -> None:
        api = FakeNymeriaAPI()
        api.resolved[("webex", "person-1")] = "owner"
        webex = FakeWebex()
        bot = NymeriaWebexBot(api, webex, bot_person_id="bot-person")
        base = {
            "message_id": "msg-1",
            "room_id": "room-1",
            "person_id": "person-1",
            "room_type": "direct",
        }

        await bot.handle_message(WebexMessage(text="link abc", **base))
        await bot.handle_message(WebexMessage(text="bind def", **base))
        await bot.handle_message(WebexMessage(text="stop", **base))
        await bot.handle_message(WebexMessage(text="unbind", **base))

        assert api.platform_claims == [
            {
                "code": "abc",
                "provider": "webex",
                "platform_user_id": "person-1",
            }
        ]
        assert api.thread_claims[0]["platform_chat_id"] == "webex:room-1"
        assert api.thread_claims[0]["expected_provider_user_id"] == "person-1"
        assert api.unbinds[0]["user_id"] == "owner"
        assert api.stop_calls == [("desktop-thread", "owner")]

    asyncio.run(run())


def test_webhook_dedupes_fetches_message_and_streams_to_bound_thread() -> None:
    async def run() -> None:
        api = FakeNymeriaAPI()
        api.resolved[("webex", "person-1")] = "owner"
        api.bindings = [
            {
                "provider": "webex",
                "platform_chat_id": "webex:room-1",
                "thread_id": "desktop-thread",
                "user_id": "owner",
            }
        ]
        webex = FakeWebex()
        webex.messages["msg-1"] = {
            "id": "msg-1",
            "roomId": "room-1",
            "personId": "person-1",
            "text": "hello",
            "roomType": "direct",
        }
        bot = NymeriaWebexBot(api, webex, bot_person_id="bot-person")
        payload = _payload()

        first = await bot.handle_webhook(payload)
        second = await bot.handle_webhook(payload)

        assert first == {"processed": 1, "skipped": 0}
        assert second == {"processed": 0, "skipped": 1}
        assert api.chat_stream_calls == [("hello", "desktop-thread", "owner")]
        assert webex.sent == [{"room_id": "room-1", "markdown": "Hello from Nymeria", "parent_id": None}]

    asyncio.run(run())


def test_webhook_skips_self_messages() -> None:
    async def run() -> None:
        api = FakeNymeriaAPI()
        webex = FakeWebex()
        webex.messages["msg-1"] = {
            "id": "msg-1",
            "roomId": "room-1",
            "personId": "bot-person",
            "text": "self",
            "roomType": "direct",
        }
        bot = NymeriaWebexBot(api, webex, bot_person_id="bot-person")

        result = await bot.handle_webhook(_payload(person_id="bot-person"))

        assert result == {"processed": 0, "skipped": 1}
        assert api.chat_stream_calls == []

    asyncio.run(run())


def test_webex_client_uses_messages_api_payload() -> None:
    async def run() -> None:
        http = FakeHTTPClient()
        client = WebexMessagingClient(
            access_token="token",
            base_url="https://webex.example/v1",
            client=http,  # type: ignore[arg-type]
        )

        await client.get_me()
        await client.get_message("msg/1")
        await client.send_message(room_id="room-1", parent_id="parent-1", markdown="hello")
        await client.close()

        assert http.gets[0]["url"] == "https://webex.example/v1/people/me"
        assert http.gets[1]["url"] == "https://webex.example/v1/messages/msg%2F1"
        assert http.posts[0]["url"] == "https://webex.example/v1/messages"
        assert http.posts[0]["headers"]["Authorization"] == "Bearer token"
        assert http.posts[0]["json"] == {
            "roomId": "room-1",
            "markdown": "hello",
            "parentId": "parent-1",
        }
        assert http.closed is False

    asyncio.run(run())
