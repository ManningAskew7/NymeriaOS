from __future__ import annotations

import asyncio
from typing import Any

from nymeria.triggers.messenger_bot import (
    MessengerCommand,
    MessengerGraphClient,
    MessengerInboundMessage,
    NymeriaMessengerBot,
    extract_inbound_messages,
    make_platform_chat_id,
    make_platform_user_id,
    make_thread_id,
)


class FakeMessengerGraph:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None]] = []

    async def send_text(
        self,
        psid: str,
        body: str,
        *,
        page_id: str | None = None,
        messaging_type: str = "RESPONSE",
    ) -> dict:
        self.sent.append((psid, body, page_id))
        return {"ok": True}


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
    content = b'{"ok":true}'

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"ok": True}


class FakeHTTPClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.closed = False

    async def post(self, url: str, **kwargs: Any) -> FakeHTTPResponse:
        self.posts.append({"url": url, **kwargs})
        return FakeHTTPResponse()

    async def aclose(self) -> None:
        self.closed = True


def _payload(*events: dict[str, Any]) -> dict[str, Any]:
    return {"object": "page", "entry": [{"id": "page-1", "messaging": list(events)}]}


def _message_event(
    *,
    mid: str = "mid.1",
    sender: str = "psid-1",
    page: str = "page-1",
    text: str = "hello",
) -> dict[str, Any]:
    return {
        "sender": {"id": sender},
        "recipient": {"id": page},
        "timestamp": 1,
        "message": {"mid": mid, "text": text},
    }


def test_extract_inbound_messages_supports_text_quick_replies_and_postbacks() -> None:
    messages = extract_inbound_messages(
        _payload(
            _message_event(mid="mid.text", text="hello"),
            {
                "sender": {"id": "psid-1"},
                "recipient": {"id": "page-1"},
                "timestamp": 2,
                "message": {
                    "mid": "mid.quick",
                    "quick_reply": {"payload": "quick payload"},
                },
            },
            {
                "sender": {"id": "psid-1"},
                "recipient": {"id": "page-1"},
                "timestamp": 3,
                "postback": {"title": "Start", "payload": "GET_STARTED"},
            },
            {
                "sender": {"id": "page-1"},
                "recipient": {"id": "psid-1"},
                "timestamp": 4,
                "message": {"mid": "mid.echo", "text": "ignore", "is_echo": True},
            },
            {
                "sender": {"id": "psid-1"},
                "recipient": {"id": "page-1"},
                "delivery": {"mids": ["mid.text"]},
            },
        )
    )

    assert [message.message_id for message in messages] == [
        "mid.text",
        "mid.quick",
        "postback:page-1:psid-1:3:Start",
    ]
    assert [message.text for message in messages] == ["hello", "quick payload", "Start"]
    assert [message.message_type for message in messages] == ["message", "message", "postback"]


def test_ids_and_command_parser_are_stable() -> None:
    assert make_platform_user_id("psid:123") == "psid-123"
    assert make_platform_chat_id("psid:123", "page:456") == "messenger:page-456:psid-123"
    assert make_platform_chat_id("psid:123") == "messenger:psid-123"
    assert make_thread_id("psid:123", "page:456") == "messenger_page-456_psid-123"
    assert make_thread_id("psid:123") == "messenger_psid-123"

    assert MessengerCommand.parse("link abc").name == "link"
    assert MessengerCommand.parse("bind_code123").arg == "code123"
    assert MessengerCommand.parse("/stop").name == "stop"
    assert MessengerCommand.parse("hello") is None


def test_unlinked_sender_is_rejected_without_agent_call() -> None:
    async def run() -> None:
        api = FakeNymeriaAPI()
        graph = FakeMessengerGraph()
        bot = NymeriaMessengerBot(api, graph)  # type: ignore[arg-type]

        await bot.handle_message(
            MessengerInboundMessage(
                message_id="mid.1",
                sender_id="psid-1",
                page_id="page-1",
                text="hello",
                message_type="message",
            )
        )

        assert api.chat_stream_calls == []
        assert "not linked" in graph.sent[0][1]

    asyncio.run(run())


def test_link_bind_unbind_and_stop_commands_use_chatapp_api() -> None:
    async def run() -> None:
        api = FakeNymeriaAPI()
        api.resolved[("messenger", "psid-1")] = "owner"
        graph = FakeMessengerGraph()
        bot = NymeriaMessengerBot(api, graph)  # type: ignore[arg-type]
        base = {
            "message_id": "mid.1",
            "sender_id": "psid-1",
            "page_id": "page-1",
            "message_type": "message",
        }

        await bot.handle_message(MessengerInboundMessage(text="link abc", **base))
        await bot.handle_message(MessengerInboundMessage(text="bind def", **base))
        await bot.handle_message(MessengerInboundMessage(text="stop", **base))
        await bot.handle_message(MessengerInboundMessage(text="unbind", **base))

        assert api.platform_claims == [
            {
                "code": "abc",
                "provider": "messenger",
                "platform_user_id": "psid-1",
            }
        ]
        assert api.thread_claims[0]["platform_chat_id"] == "messenger:page-1:psid-1"
        assert api.thread_claims[0]["expected_provider_user_id"] == "psid-1"
        assert api.unbinds[0]["user_id"] == "owner"
        assert api.stop_calls == [("desktop-thread", "owner")]

    asyncio.run(run())


def test_webhook_dedupes_messages_and_streams_to_bound_thread() -> None:
    async def run() -> None:
        api = FakeNymeriaAPI()
        api.resolved[("messenger", "psid-1")] = "owner"
        api.bindings = [
            {
                "provider": "messenger",
                "platform_chat_id": "messenger:page-1:psid-1",
                "thread_id": "desktop-thread",
                "user_id": "owner",
            }
        ]
        graph = FakeMessengerGraph()
        bot = NymeriaMessengerBot(api, graph)  # type: ignore[arg-type]
        payload = _payload(_message_event(mid="mid.1"))

        first = await bot.handle_webhook(payload)
        second = await bot.handle_webhook(payload)

        assert first == {"processed": 1, "skipped": 0}
        assert second == {"processed": 0, "skipped": 1}
        assert api.chat_stream_calls == [("hello", "desktop-thread", "owner")]
        assert graph.sent == [("psid-1", "Hello from Nymeria", "page-1")]

    asyncio.run(run())


def test_graph_client_sends_messenger_text_payload() -> None:
    async def run() -> None:
        http = FakeHTTPClient()
        client = MessengerGraphClient(
            page_access_token="token",
            page_id="page-1",
            base_url="https://graph.facebook.com/v23.0",
            client=http,  # type: ignore[arg-type]
        )

        await client.send_text("psid-1", "hello")
        await client.close()

        assert http.posts[0]["url"] == "https://graph.facebook.com/v23.0/page-1/messages"
        assert http.posts[0]["headers"]["Authorization"] == "Bearer token"
        assert http.posts[0]["json"] == {
            "recipient": {"id": "psid-1"},
            "messaging_type": "RESPONSE",
            "message": {"text": "hello"},
        }
        assert http.closed is False

    asyncio.run(run())
