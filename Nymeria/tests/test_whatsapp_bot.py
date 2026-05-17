from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import Any

from nymeria.triggers.whatsapp_bot import (
    NymeriaWhatsAppBot,
    WhatsAppCloudClient,
    WhatsAppCommand,
    WhatsAppInboundMessage,
    extract_inbound_messages,
    make_platform_chat_id,
    make_platform_user_id,
    make_thread_id,
    verify_meta_signature,
)


class FakeWhatsAppCloud:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_text(self, to: str, body: str, *, preview_url: bool = False) -> dict:
        self.sent.append((to, body))
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


def _payload(*messages: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "phone-1"},
                            "contacts": [
                                {"wa_id": "15551234567", "profile": {"name": "Alex"}}
                            ],
                            "messages": list(messages),
                        }
                    }
                ]
            }
        ]
    }


def test_signature_verification_uses_meta_hmac_header() -> None:
    body = b'{"entry":[]}'
    secret = "app-secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert verify_meta_signature(body, None, None) is False
    assert verify_meta_signature(body, f"sha256={digest}", secret) is True
    assert verify_meta_signature(body, "sha256=bad", secret) is False
    assert verify_meta_signature(body, None, secret) is False


def test_extract_inbound_messages_supports_text_buttons_and_interactive_replies() -> None:
    messages = extract_inbound_messages(
        _payload(
            {
                "id": "wamid.text",
                "from": "15551234567",
                "timestamp": "1",
                "type": "text",
                "text": {"body": "hello"},
            },
            {
                "id": "wamid.button",
                "from": "15551234567",
                "type": "button",
                "button": {"text": "Yes"},
            },
            {
                "id": "wamid.interactive",
                "from": "15551234567",
                "type": "interactive",
                "interactive": {"button_reply": {"id": "ok", "title": "OK"}},
            },
            {
                "id": "wamid.image",
                "from": "15551234567",
                "type": "image",
                "image": {"id": "media"},
            },
        )
    )

    assert [message.message_id for message in messages] == [
        "wamid.text",
        "wamid.button",
        "wamid.interactive",
    ]
    assert [message.text for message in messages] == ["hello", "Yes", "OK"]
    assert messages[0].contact_name == "Alex"


def test_ids_and_command_parser_are_stable() -> None:
    assert make_platform_user_id("+1 (555) 123-4567") == "15551234567"
    assert make_platform_chat_id("+1 (555) 123-4567") == "whatsapp:15551234567"
    assert make_thread_id("+1 (555) 123-4567") == "whatsapp_15551234567"

    assert WhatsAppCommand.parse("link abc").name == "link"
    assert WhatsAppCommand.parse("bind_code123").arg == "code123"
    assert WhatsAppCommand.parse("/stop").name == "stop"
    assert WhatsAppCommand.parse("hello") is None


def test_unlinked_sender_is_rejected_without_agent_call() -> None:
    async def run() -> None:
        api = FakeNymeriaAPI()
        cloud = FakeWhatsAppCloud()
        bot = NymeriaWhatsAppBot(api, cloud)

        await bot.handle_message(
            WhatsAppInboundMessage(
                message_id="wamid.1",
                sender_id="15551234567",
                text="hello",
                message_type="text",
            )
        )

        assert api.chat_stream_calls == []
        assert "not linked" in cloud.sent[0][1]

    asyncio.run(run())


def test_link_bind_unbind_and_stop_commands_use_chatapp_api() -> None:
    async def run() -> None:
        api = FakeNymeriaAPI()
        api.resolved[("whatsapp", "15551234567")] = "owner"
        cloud = FakeWhatsAppCloud()
        bot = NymeriaWhatsAppBot(api, cloud)
        base = {
            "message_id": "wamid.1",
            "sender_id": "15551234567",
            "message_type": "text",
        }

        await bot.handle_message(WhatsAppInboundMessage(text="link abc", **base))
        await bot.handle_message(WhatsAppInboundMessage(text="bind def", **base))
        await bot.handle_message(WhatsAppInboundMessage(text="stop", **base))
        await bot.handle_message(WhatsAppInboundMessage(text="unbind", **base))

        assert api.platform_claims == [
            {
                "code": "abc",
                "provider": "whatsapp",
                "platform_user_id": "15551234567",
            }
        ]
        assert api.thread_claims[0]["platform_chat_id"] == "whatsapp:15551234567"
        assert api.thread_claims[0]["expected_provider_user_id"] == "15551234567"
        assert api.unbinds[0]["user_id"] == "owner"
        assert api.stop_calls == [("desktop-thread", "owner")]

    asyncio.run(run())


def test_webhook_dedupes_messages_and_streams_to_bound_thread() -> None:
    async def run() -> None:
        api = FakeNymeriaAPI()
        api.resolved[("whatsapp", "15551234567")] = "owner"
        api.bindings = [
            {
                "provider": "whatsapp",
                "platform_chat_id": "whatsapp:15551234567",
                "thread_id": "desktop-thread",
                "user_id": "owner",
            }
        ]
        cloud = FakeWhatsAppCloud()
        bot = NymeriaWhatsAppBot(api, cloud)
        payload = _payload(
            {
                "id": "wamid.1",
                "from": "15551234567",
                "type": "text",
                "text": {"body": "hello"},
            }
        )

        first = await bot.handle_webhook(payload)
        second = await bot.handle_webhook(payload)

        assert first == {"processed": 1, "skipped": 0}
        assert second == {"processed": 0, "skipped": 1}
        assert api.chat_stream_calls == [("hello", "desktop-thread", "owner")]
        assert cloud.sent == [("15551234567", "Hello from Nymeria")]

    asyncio.run(run())


def test_cloud_client_sends_graph_text_payload() -> None:
    async def run() -> None:
        http = FakeHTTPClient()
        client = WhatsAppCloudClient(
            access_token="token",
            phone_number_id="phone-1",
            base_url="https://graph.facebook.com/v19.0",
            client=http,  # type: ignore[arg-type]
        )

        await client.send_text("+1 (555) 123-4567", "hello")
        await client.close()

        assert http.posts[0]["url"] == "https://graph.facebook.com/v19.0/phone-1/messages"
        assert http.posts[0]["headers"]["Authorization"] == "Bearer token"
        assert http.posts[0]["json"] == {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": "15551234567",
            "type": "text",
            "text": {"preview_url": False, "body": "hello"},
        }
        assert http.closed is False

    asyncio.run(run())
