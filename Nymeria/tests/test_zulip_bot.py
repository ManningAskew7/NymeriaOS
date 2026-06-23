from __future__ import annotations

import asyncio
from typing import Any, Mapping

import httpx
import pytest

from nymeria.triggers.zulip_bot import (
    NymeriaZulipBot,
    ZulipCommand,
    ZulipReplyTarget,
    make_direct_chat_id,
    make_platform_user_id,
    make_realm_key,
    make_stream_chat_id,
    make_thread_id,
    message_from_event,
    message_mentions_bot,
    strip_bot_mention,
)


class FakeZulipClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def get_me(self):
        return {"user_id": 99, "email": "nymeria-bot@example.zulipchat.com", "full_name": "Nymeria"}

    async def register_queue(self):
        return {"queue_id": "q1", "last_event_id": 0}

    async def get_events(self, *, queue_id, last_event_id):  # pragma: no cover - start loop only.
        return {"events": []}

    async def send_message(self, *, message_type, to, content, topic=None):
        self.sent.append(
            {"message_type": message_type, "to": to, "content": content, "topic": topic}
        )
        return {"id": len(self.sent), "result": "success"}

    async def upload_file(self, *, filename, data, content_type):
        if not hasattr(self, "uploads"):
            self.uploads = []
        self.uploads.append({"filename": filename, "data": data, "content_type": content_type})
        return f"/user_uploads/abc/{filename}"

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
        yield {"type": "response", "content": "zulip "}
        yield {"type": "response", "content": "reply"}
        yield {"type": "done"}

    async def chat(self, *args, **kwargs):  # pragma: no cover - fallback only.
        return {"response": "fallback", "tool_call_count": 0}

    async def download_workspace_file(self, file_path, user_id=None):
        return getattr(self, "workspace_files", {}).get(file_path)

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


def make_bot(api: FakeAPI | None = None) -> tuple[NymeriaZulipBot, FakeAPI, FakeZulipClient]:
    fake_api = api or FakeAPI()
    client = FakeZulipClient()
    bot = NymeriaZulipBot(
        fake_api,  # type: ignore[arg-type]
        base_url="https://example.zulipchat.com",
        email="nymeria-bot@example.zulipchat.com",
        api_key="key",
        zulip_client=client,  # type: ignore[arg-type]
    )
    bot.bot_user_id = 99
    bot.bot_email = "nymeria-bot@example.zulipchat.com"
    bot.bot_full_name = "Nymeria"
    return bot, fake_api, client


def event(
    *,
    event_id: int,
    message_id: int,
    sender_id: int = 42,
    content: str,
    message_type: str = "direct",
    stream_id: int | None = None,
    stream_name: str | None = None,
    topic: str | None = None,
    flags: list[str] | None = None,
) -> Mapping[str, Any]:
    display_recipient: str | list[dict[str, Any]]
    if message_type == "stream":
        display_recipient = stream_name or "general"
    else:
        display_recipient = [
            {"id": sender_id, "email": "alice@example.com", "full_name": "Alice"},
            {"id": 99, "email": "nymeria-bot@example.zulipchat.com", "full_name": "Nymeria"},
        ]
    message = {
        "id": message_id,
        "sender_id": sender_id,
        "sender_email": "alice@example.com",
        "sender_full_name": "Alice",
        "type": message_type,
        "content": content,
        "display_recipient": display_recipient,
        "flags": flags or [],
    }
    if stream_id is not None:
        message["stream_id"] = stream_id
    if topic:
        message["topic"] = topic
    return {"id": event_id, "type": "message", "message": message}


def test_zulip_ids_are_realm_scoped_and_sanitized():
    assert make_realm_key("https://example.zulipchat.com") == "example.zulipchat.com"
    assert make_platform_user_id("example.zulipchat.com", "42/1") == "example.zulipchat.com:42-1"
    assert make_direct_chat_id("example.zulipchat.com", 42) == "example.zulipchat.com:direct:42"
    assert (
        make_stream_chat_id("example.zulipchat.com", 7, "Status / Now")
        == "example.zulipchat.com:stream:7:Status-Now"
    )
    assert (
        make_thread_id("example.zulipchat.com", sender_id=42, is_direct=True)
        == "zulip_dm_example.zulipchat.com_42"
    )
    assert (
        make_thread_id("example.zulipchat.com", stream_id=7, topic="Status / Now")
        == "zulip_example.zulipchat.com_7_topic_Status-Now"
    )


def test_zulip_event_parse_and_mentions():
    parsed = message_from_event(
        event(
            event_id=1,
            message_id=10,
            content="@**Nymeria** status?",
            message_type="stream",
            stream_id=7,
            stream_name="general",
            topic="Ops",
        )
    )
    assert parsed is not None
    assert parsed.id == 10
    assert parsed.stream_name == "general"
    assert message_mentions_bot(parsed, bot_full_name="Nymeria")
    assert strip_bot_mention("@**Nymeria** status?", bot_full_name="Nymeria") == "status?"


def test_zulip_command_parser_handles_link_bind_and_exact_stop():
    assert ZulipCommand.parse("link abc").name == "link"
    assert ZulipCommand.parse("link_abc").arg == "abc"
    assert ZulipCommand.parse("@**Nymeria** bind code-1", bot_full_name="Nymeria").arg == "code-1"
    assert ZulipCommand.parse("stop").name == "stop"
    assert ZulipCommand.parse("stop doing that") is None


def test_direct_message_streams_to_native_zulip_dm_thread():
    bot, api, client = make_bot()

    asyncio.run(
        bot.handle_zulip_event(
            event(event_id=1, message_id=10, content="hello")
        )
    )

    assert api.last_resolve == {
        "platform": "zulip",
        "platform_user_id": "example.zulipchat.com:42",
    }
    assert api.chat_stream_calls == [
        {
            "message": "hello",
            "thread_id": "zulip_dm_example.zulipchat.com_42",
            "user_id": "user-1",
        }
    ]
    assert client.sent == [
        {"message_type": "direct", "to": "[42]", "content": "zulip reply", "topic": None}
    ]


def test_stream_mention_routes_with_stream_context():
    bot, api, client = make_bot()

    asyncio.run(
        bot.handle_zulip_event(
            event(
                event_id=2,
                message_id=11,
                content="@**Nymeria** status?",
                message_type="stream",
                stream_id=7,
                stream_name="general",
                topic="Ops",
            )
        )
    )

    assert api.chat_stream_calls == [
        {
            "message": "[Zulip Alice in general#Ops]\nstatus?",
            "thread_id": "zulip_example.zulipchat.com_7_topic_Ops",
            "user_id": "user-1",
        }
    ]
    assert client.sent == [
        {"message_type": "stream", "to": "general", "content": "zulip reply", "topic": "Ops"}
    ]


def test_unmentioned_stream_message_is_ignored_by_default():
    bot, api, client = make_bot()

    asyncio.run(
        bot.handle_zulip_event(
            event(
                event_id=3,
                message_id=12,
                content="hello",
                message_type="stream",
                stream_id=7,
                stream_name="general",
                topic="Ops",
            )
        )
    )

    assert api.chat_stream_calls == []
    assert client.sent == []


def test_unlinked_user_gets_rejection_without_agent_call():
    bot, api, client = make_bot(FakeAPI(resolved_user=None))

    asyncio.run(
        bot.handle_zulip_event(
            event(event_id=4, message_id=13, content="hello")
        )
    )

    assert api.chat_stream_calls == []
    assert "not linked" in client.sent[0]["content"]
    assert "example.zulipchat.com:42" in client.sent[0]["content"]


def test_link_and_bind_commands_claim_codes():
    bot, api, client = make_bot()

    async def run():
        await bot.handle_zulip_event(event(event_id=5, message_id=14, content="link abc123"))
        await bot.handle_zulip_event(
            event(
                event_id=6,
                message_id=15,
                content="@**Nymeria** bind bind123",
                message_type="stream",
                stream_id=7,
                stream_name="general",
                topic="Ops",
            )
        )

    asyncio.run(run())

    assert api.claimed_links == [
        {
            "code": "abc123",
            "provider": "zulip",
            "platform_user_id": "example.zulipchat.com:42",
        }
    ]
    assert api.claimed_binds == [
        {
            "code": "bind123",
            "provider": "zulip",
            "platform_chat_id": "example.zulipchat.com:stream:7:Ops",
            "expected_provider_user_id": "example.zulipchat.com:42",
        }
    ]
    assert "Linked this Zulip account" in client.sent[0]["content"]
    assert "Bound this Zulip conversation" in client.sent[1]["content"]


def test_send_text_splits_large_zulip_messages():
    bot, _api, client = make_bot()

    asyncio.run(
        bot._send_text(ZulipReplyTarget(message_type="stream", to="general", topic="Ops"), "x" * 4001)
    )

    assert len(client.sent) == 2
    assert all(len(item["content"]) <= 4000 for item in client.sent)


def test_zulip_workspace_attachment_uploads_and_links():
    bot, api, client = make_bot()
    api.workspace_files = {"/workspace/u/img.png": (b"img-bytes", "img.png", "image/png")}
    target = ZulipReplyTarget(message_type="stream", to="general", topic="Ops")

    asyncio.run(bot._send_workspace_attachment(target, "/workspace/u/img.png"))

    assert getattr(client, "uploads", []) and client.uploads[0]["filename"] == "img.png"
    assert any("/user_uploads/abc/img.png" in item["content"] for item in client.sent)


def test_zulip_workspace_attachment_falls_back_to_text_when_missing():
    bot, api, client = make_bot()
    api.workspace_files = {}
    target = ZulipReplyTarget(message_type="stream", to="general", topic="Ops")

    asyncio.run(bot._send_workspace_attachment(target, "/workspace/u/missing.png"))

    assert not getattr(client, "uploads", [])
    assert any("Workspace artifact" in item["content"] for item in client.sent)


def _http_status_error(status_code: int, detail: str) -> httpx.HTTPStatusError:
    request = httpx.Request("DELETE", "https://api.test/binding")
    response = httpx.Response(status_code, json={"detail": detail}, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


class RaisingAPI(FakeAPI):
    def __init__(self, *, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    async def unbind_chatapp_by_chat(self, **kwargs):
        raise self._exc

    async def stop(self, thread_id: str, user_id: str | None = None):
        raise self._exc


def test_unbind_http_error_shows_server_detail():
    # F11: a 4xx from unbind renders the server-provided detail, matching
    # _cmd_link/_cmd_bind, instead of a raw exception repr.
    bot, _api, client = make_bot(
        RaisingAPI(exc=_http_status_error(409, "binding owned by another user"))
    )

    asyncio.run(bot.handle_zulip_event(event(event_id=20, message_id=30, content="unbind")))

    assert client.sent[0]["content"] == "Couldn't unbind: binding owned by another user"


def test_stop_http_error_shows_server_detail():
    bot, _api, client = make_bot(RaisingAPI(exc=_http_status_error(404, "thread not found")))

    asyncio.run(bot.handle_zulip_event(event(event_id=21, message_id=31, content="stop")))

    assert client.sent[0]["content"] == "Couldn't stop the current run: thread not found"


def test_unbind_non_http_error_propagates():
    # F11: the narrowed catch no longer swallows unexpected (non-HTTP) errors;
    # they propagate to the loop-level handler that logs with exc_info.
    bot, _api, _client = make_bot(RaisingAPI(exc=RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(bot.handle_zulip_event(event(event_id=22, message_id=32, content="unbind")))
