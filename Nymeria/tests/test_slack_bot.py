from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nymeria.triggers.slack_bot import (
    NymeriaSlackBot,
    SlackCommand,
    make_platform_chat_id,
    make_platform_user_id,
    make_thread_id,
    slack_markdown,
    strip_bot_mention,
)


class FakeSlackClient:
    def __init__(self) -> None:
        self.posts: list[dict] = []

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": f"bot.{len(self.posts)}"}


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

    async def chat_stream(
        self,
        message,
        thread_id,
        user_id,
        attachments=None,
        force_unsupported_attachments=False,
    ):
        self.chat_stream_calls.append(
            {
                "message": message,
                "thread_id": thread_id,
                "user_id": user_id,
                "attachments": attachments,
                "force_unsupported_attachments": force_unsupported_attachments,
            }
        )
        yield {"type": "response", "content": "hello "}
        yield {"type": "response", "content": "from Nymeria"}
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


def make_bot(api: FakeAPI | None = None) -> tuple[NymeriaSlackBot, FakeAPI, FakeSlackClient]:
    fake_api = api or FakeAPI()
    client = FakeSlackClient()
    bot = NymeriaSlackBot(
        fake_api,  # type: ignore[arg-type]
        bot_token="xoxb-test",
        app_token="xapp-test",
    )
    bot._app = SimpleNamespace(client=client)
    bot._bot_user_id = "UBOT"
    bot._team_id = "T1"
    return bot, fake_api, client


def test_slack_ids_are_team_scoped_and_sanitized():
    assert make_platform_user_id("T 1", "U/1") == "T-1:U-1"
    assert make_platform_chat_id("T1", "C1", "171.123") == "T1:C1:171-123"
    assert make_thread_id("T1", "D1", user_id="U1", is_dm=True) == "slack_dm_T1_U1"
    assert (
        make_thread_id("T1", "C1", thread_ts="171.123")
        == "slack_T1_C1_thread_171-123"
    )


def test_slack_command_parser_handles_link_bind_and_exact_stop():
    assert SlackCommand.parse("link abc").name == "link"
    assert SlackCommand.parse("link_abc").arg == "abc"
    assert SlackCommand.parse("<@UBOT> bind code-1", "UBOT").arg == "code-1"
    assert SlackCommand.parse("stop").name == "stop"
    assert SlackCommand.parse("stop making noise") is None


def test_slack_markdown_escapes_control_chars_and_maps_bold():
    assert slack_markdown("**bold** <bad> & ok") == "*bold* &lt;bad&gt; &amp; ok"
    assert strip_bot_mention("<@UBOT> hello", "UBOT") == "hello"


def test_dm_message_streams_to_native_slack_dm_thread():
    bot, api, client = make_bot()

    asyncio.run(
        bot.handle_slack_event(
            {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "U1",
                "team": "T1",
                "ts": "171.100",
                "text": "hello",
            },
            source="message",
        )
    )

    assert api.last_resolve == {
        "platform": "slack",
        "platform_user_id": "T1:U1",
    }
    assert api.chat_stream_calls == [
        {
            "message": "hello",
            "thread_id": "slack_dm_T1_U1",
            "user_id": "user-1",
            "attachments": None,
            "force_unsupported_attachments": False,
        }
    ]
    assert client.posts == [
        {
            "channel": "D1",
            "text": "hello from Nymeria",
            "mrkdwn": True,
        }
    ]


def test_channel_app_mention_replies_in_slack_thread_and_allows_followup():
    bot, api, client = make_bot()

    async def run():
        await bot.handle_slack_event(
            {
                "type": "app_mention",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "team": "T1",
                "ts": "171.200",
                "text": "<@UBOT> hello",
            },
            source="app_mention",
        )
        await bot.handle_slack_event(
            {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "team": "T1",
                "ts": "171.201",
                "thread_ts": "171.200",
                "text": "follow up",
            },
            source="message",
        )

    asyncio.run(run())

    assert len(api.chat_stream_calls) == 2
    assert api.chat_stream_calls[0]["message"] == "[Slack <@U1> in C1]\nhello"
    assert api.chat_stream_calls[0]["thread_id"] == "slack_T1_C1_thread_171-200"
    assert api.chat_stream_calls[1]["message"] == "[Slack <@U1> in C1]\nfollow up"
    assert client.posts[0]["thread_ts"] == "171.200"
    assert client.posts[1]["thread_ts"] == "171.200"


def test_unlinked_user_gets_rejection_without_agent_call():
    bot, api, client = make_bot(FakeAPI(resolved_user=None))

    asyncio.run(
        bot.handle_slack_event(
            {
                "type": "app_mention",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "team": "T1",
                "ts": "171.300",
                "text": "<@UBOT> hello",
            },
            source="app_mention",
        )
    )

    assert api.chat_stream_calls == []
    assert "not linked" in client.posts[0]["text"]
    assert "T1:U1" in client.posts[0]["text"]


def test_link_and_bind_commands_claim_codes():
    bot, api, client = make_bot()

    async def run():
        await bot.handle_slack_event(
            {
                "type": "message",
                "channel": "D1",
                "channel_type": "im",
                "user": "U1",
                "team": "T1",
                "ts": "171.400",
                "text": "link abc123",
            },
            source="message",
        )
        await bot.handle_slack_event(
            {
                "type": "app_mention",
                "channel": "C1",
                "channel_type": "channel",
                "user": "U1",
                "team": "T1",
                "ts": "171.401",
                "text": "<@UBOT> bind bind123",
            },
            source="app_mention",
        )

    asyncio.run(run())

    assert api.claimed_links == [
        {
            "code": "abc123",
            "provider": "slack",
            "platform_user_id": "T1:U1",
        }
    ]
    assert api.claimed_binds == [
        {
            "code": "bind123",
            "provider": "slack",
            "platform_chat_id": "T1:C1:171-401",
            "expected_provider_user_id": "T1:U1",
        }
    ]
    assert "Linked this Slack account" in client.posts[0]["text"]
    assert "Bound this Slack conversation" in client.posts[1]["text"]
