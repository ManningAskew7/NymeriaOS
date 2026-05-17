from __future__ import annotations

import asyncio
from typing import Any, Mapping

from nymeria.triggers.signal_bot import (
    NymeriaSignalBot,
    SignalCliRestClient,
    SignalCommand,
    SignalReplyTarget,
    inbound_message_from_payload,
    make_platform_chat_id,
    make_platform_user_id,
    make_thread_id,
    normalize_phone_number,
    render_signal_mentions,
    signal_mentions_bot,
)


class FakeSignalClient:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def check(self) -> bool:
        return True

    async def send_text(self, target: SignalReplyTarget, text: str) -> dict[str, Any]:
        self.sent.append(
            {
                "recipient": target.recipient,
                "group_id": target.group_id,
                "text": text,
            }
        )
        return {"timestamp": len(self.sent)}

    async def stream_events(self, account: str):  # pragma: no cover - start loop only.
        if False:
            yield {}

    async def close(self) -> None:
        return None


class FakeAPI:
    def __init__(self, *, resolved_user: str | None = "user-1") -> None:
        self.resolved_user = resolved_user
        self.chat_stream_calls: list[dict[str, str]] = []
        self.claimed_links: list[dict[str, str]] = []
        self.claimed_binds: list[dict[str, str]] = []
        self.unbound: list[dict[str, str | None]] = []
        self.stopped: list[dict[str, str | None]] = []
        self.last_resolve: dict[str, str] | None = None

    async def resolve_platform_user(self, provider: str, provider_user_id: str):
        self.last_resolve = {
            "provider": provider,
            "provider_user_id": provider_user_id,
        }
        return self.resolved_user

    async def list_chatapp_bindings(self, provider: str):
        self.last_bindings_provider = provider
        return []

    def chat_stream(self, message: str, thread_id: str, user_id: str):
        self.chat_stream_calls.append(
            {"message": message, "thread_id": thread_id, "user_id": user_id}
        )

        async def events():
            yield {"type": "response", "content": "signal "}
            yield {"type": "response", "content": "reply"}
            yield {"type": "done"}

        return events()

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
    *,
    respond_mode: str = "mention",
) -> tuple[NymeriaSignalBot, FakeAPI, FakeSignalClient]:
    fake_api = api or FakeAPI()
    signal = FakeSignalClient()
    bot = NymeriaSignalBot(
        fake_api,  # type: ignore[arg-type]
        signal,  # type: ignore[arg-type]
        account="+15551234567",
        respond_mode=respond_mode,
    )
    return bot, fake_api, signal


def payload(
    *,
    text: str = "hello",
    source_number: str = "+15550001111",
    source_uuid: str = "11111111-1111-1111-1111-111111111111",
    source_name: str = "Alice",
    timestamp: int = 1234,
    group_id: str | None = None,
    group_name: str | None = None,
    mentions: list[dict[str, Any]] | None = None,
) -> Mapping[str, Any]:
    data: dict[str, Any] = {
        "message": text,
        "mentions": mentions or [],
    }
    if group_id:
        data["groupInfo"] = {"groupId": group_id, "groupName": group_name or "Group"}
    return {
        "envelope": {
            "sourceNumber": source_number,
            "sourceUuid": source_uuid,
            "sourceName": source_name,
            "timestamp": timestamp,
            "dataMessage": data,
        }
    }


def test_signal_ids_mentions_and_commands_are_stable():
    assert normalize_phone_number("+1 (555) 000-1111") == "+15550001111"
    assert make_platform_user_id("+1 (555) 000-1111") == "+15550001111"
    assert (
        make_platform_user_id(None, "11111111-1111-1111-1111-111111111111")
        == "uuid:11111111-1111-1111-1111-111111111111"
    )
    assert make_platform_chat_id(sender_id="+15550001111") == "signal:dm:+15550001111"
    assert make_platform_chat_id(group_id="group/1") == "signal:group:group/1"
    assert make_thread_id(sender_id="+15550001111") == "signal_dm_15550001111"
    assert make_thread_id(group_id="group/1") == "signal_group_group-1"

    mentions = [{"number": "+15551234567", "start": 0, "length": 1}]
    rendered = render_signal_mentions("\uFFFC status?", mentions)
    assert rendered == "@+15551234567 status?"
    assert signal_mentions_bot(rendered, mentions, account="+15551234567")

    assert SignalCommand.parse("link abc", account="+15551234567").name == "link"
    assert SignalCommand.parse("bind_code123", account="+15551234567").arg == "code123"
    assert SignalCommand.parse("/stop", account="+15551234567").name == "stop"
    assert SignalCommand.parse("@+15551234567 stop", account="+15551234567").name == "stop"
    assert SignalCommand.parse("stop doing that", account="+15551234567") is None


def test_inbound_payload_parses_direct_group_mentions_and_ignores_sync_or_self():
    direct = inbound_message_from_payload(payload(), account="+15551234567")
    assert direct is not None
    assert direct.sender_id == "+15550001111"
    assert direct.text == "hello"
    assert direct.target.recipient == "+15550001111"

    uuid_only = inbound_message_from_payload(
        {
            "envelope": {
                "source": "11111111-1111-1111-1111-111111111111",
                "sourceName": "Alice",
                "timestamp": 123,
                "dataMessage": {"message": "hello"},
            }
        },
        account="+15551234567",
    )
    assert uuid_only is not None
    expected_uuid_sender = "uuid:11111111-1111-1111-1111-111111111111"
    assert uuid_only.sender_id == expected_uuid_sender
    assert uuid_only.target.recipient == expected_uuid_sender.removeprefix("uuid:")

    group = inbound_message_from_payload(
        payload(
            text="\uFFFC status?",
            group_id="group-1",
            mentions=[{"number": "+15551234567", "start": 0, "length": 1}],
        ),
        account="+15551234567",
    )
    assert group is not None
    assert group.group_id == "group-1"
    assert group.was_mentioned is True
    assert group.text == "status?"

    assert inbound_message_from_payload({"envelope": {"syncMessage": {}}}) is None
    assert (
        inbound_message_from_payload(
            payload(source_number="+15551234567"),
            account="+15551234567",
        )
        is None
    )


def test_direct_message_streams_to_native_signal_thread():
    bot, api, signal = make_bot()
    message = inbound_message_from_payload(payload(text="hello"), account="+15551234567")
    assert message is not None

    asyncio.run(bot.handle_message(message))

    assert api.last_resolve == {
        "provider": "signal",
        "provider_user_id": "+15550001111",
    }
    assert api.chat_stream_calls == [
        {
            "message": "hello",
            "thread_id": "signal_dm_15550001111",
            "user_id": "user-1",
        }
    ]
    assert signal.sent == [
        {"recipient": "+15550001111", "group_id": None, "text": "signal reply"}
    ]


def test_group_message_is_mention_gated_and_uses_group_thread():
    bot, api, signal = make_bot()
    unmentioned = inbound_message_from_payload(
        payload(text="hello", group_id="group-1"),
        account="+15551234567",
    )
    assert unmentioned is not None
    asyncio.run(bot.handle_message(unmentioned))
    assert api.chat_stream_calls == []
    assert signal.sent == []

    mentioned = inbound_message_from_payload(
        payload(
            text="\uFFFC status?",
            group_id="group-1",
            group_name="Ops",
            mentions=[{"number": "+15551234567", "start": 0, "length": 1}],
        ),
        account="+15551234567",
    )
    assert mentioned is not None
    asyncio.run(bot.handle_message(mentioned))

    assert api.chat_stream_calls == [
        {
            "message": "[Signal Alice in Ops]\nstatus?",
            "thread_id": "signal_group_group-1",
            "user_id": "user-1",
        }
    ]
    assert signal.sent == [
        {"recipient": None, "group_id": "group-1", "text": "signal reply"}
    ]


def test_unlinked_signal_sender_gets_rejection_without_agent_call():
    bot, api, signal = make_bot(FakeAPI(resolved_user=None))
    message = inbound_message_from_payload(payload(text="hello"), account="+15551234567")
    assert message is not None

    asyncio.run(bot.handle_message(message))

    assert api.chat_stream_calls == []
    assert "not linked" in signal.sent[0]["text"]
    assert "+15550001111" in signal.sent[0]["text"]


def test_link_bind_unbind_and_stop_commands_use_chatapp_api():
    bot, api, signal = make_bot()

    async def run() -> None:
        for text in ("link abc123", "bind bind123", "unbind", "stop"):
            message = inbound_message_from_payload(
                payload(text=text, timestamp=hash(text) % 100000),
                account="+15551234567",
            )
            assert message is not None
            await bot.handle_message(message)

    asyncio.run(run())

    assert api.claimed_links == [
        {
            "code": "abc123",
            "provider": "signal",
            "platform_user_id": "+15550001111",
        }
    ]
    assert api.claimed_binds == [
        {
            "code": "bind123",
            "provider": "signal",
            "platform_chat_id": "signal:dm:+15550001111",
            "expected_provider_user_id": "+15550001111",
        }
    ]
    assert api.unbound == [
        {
            "provider": "signal",
            "platform_chat_id": "signal:dm:+15550001111",
            "user_id": "user-1",
        }
    ]
    assert api.stopped == [{"thread_id": "signal_dm_15550001111", "user_id": "user-1"}]
    assert "Linked this Signal account" in signal.sent[0]["text"]
    assert "Bound this Signal conversation" in signal.sent[1]["text"]
    assert "Unbound" in signal.sent[2]["text"]
    assert "Stopped" in signal.sent[3]["text"]


class FakeHTTPResponse:
    status_code = 200
    content = b'{"result":{"timestamp":123}}'

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {"result": {"timestamp": 123}}


class FakeHTTPClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.closed = False

    async def get(self, url: str, **kwargs: Any) -> FakeHTTPResponse:
        return FakeHTTPResponse()

    async def post(self, url: str, **kwargs: Any) -> FakeHTTPResponse:
        self.posts.append({"url": url, **kwargs})
        return FakeHTTPResponse()

    async def aclose(self) -> None:
        self.closed = True


def test_signal_cli_client_sends_json_rpc_payloads():
    async def run() -> FakeHTTPClient:
        http = FakeHTTPClient()
        client = SignalCliRestClient(
            "localhost:8080",
            account="+15551234567",
            http_client=http,  # type: ignore[arg-type]
        )
        result = await client.send_text(SignalReplyTarget(recipient="+15550001111"), "hi")
        assert result == {"timestamp": 123}
        return http

    http = asyncio.run(run())
    assert http.posts[0]["url"] == "http://localhost:8080/api/v1/rpc"
    payload = http.posts[0]["json"]
    assert payload["jsonrpc"] == "2.0"
    assert payload["method"] == "send"
    assert payload["params"] == {
        "account": "+15551234567",
        "message": "hi",
        "recipient": ["+15550001111"],
    }
