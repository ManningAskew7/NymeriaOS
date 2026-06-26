from __future__ import annotations

import asyncio

from nymeria.triggers.bot_helpers import SEEN_EVENT_TTL_SECONDS, SeenEventCache
from nymeria.triggers.matrix_bot import (
    MatrixCommand,
    MatrixReplyTarget,
    NymeriaMatrixBot,
    event_mentions_bot,
    make_platform_chat_id,
    make_thread_id,
    strip_matrix_mention,
)


class FakeMatrixClient:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.joined: list[str] = []

    async def authenticate(self):
        return "@bot:example.org"

    async def sync(self, *, since, timeout_ms):
        return {"next_batch": "s1", "rooms": {}}

    async def send_message(self, room_id, content):
        self.sent.append({"room_id": room_id, "content": dict(content)})
        return {"event_id": f"$bot{len(self.sent)}"}

    async def join_room(self, room_id_or_alias):
        self.joined.append(room_id_or_alias)
        return {"room_id": room_id_or_alias}

    async def close(self):
        return None


class FakeAPI:
    def __init__(self, *, resolved_user: str | None = "user-1") -> None:
        self.resolved_user = resolved_user
        self.chat_stream_calls: list[dict] = []
        self.claimed_links: list[dict] = []
        self.claimed_binds: list[dict] = []
        self.last_resolve: dict | None = None

    async def resolve_platform_user(self, platform: str, platform_user_id: str):
        self.last_resolve = {
            "platform": platform,
            "platform_user_id": platform_user_id,
        }
        return self.resolved_user

    async def list_chatapp_bindings(self, provider: str):
        return []

    async def chat_stream(self, message, thread_id, user_id):
        self.chat_stream_calls.append(
            {"message": message, "thread_id": thread_id, "user_id": user_id}
        )
        yield {"type": "response", "content": "matrix "}
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
        return {"unbound": True, "thread_id": "desktop-thread"}

    async def stop(self, thread_id: str, user_id: str | None = None):
        return {"stopped": True}

    async def health(self):
        return True

    async def close(self):
        return None


def make_bot(api: FakeAPI | None = None) -> tuple[NymeriaMatrixBot, FakeAPI, FakeMatrixClient]:
    fake_api = api or FakeAPI()
    matrix = FakeMatrixClient()
    bot = NymeriaMatrixBot(
        fake_api,  # type: ignore[arg-type]
        homeserver="https://matrix.example.org",
        access_token="tok",
        user_id="@bot:example.org",
        matrix_client=matrix,  # type: ignore[arg-type]
    )
    bot.user_id = "@bot:example.org"
    return bot, fake_api, matrix


def test_matrix_ids_and_mentions():
    assert make_platform_chat_id("!room:example.org") == "!room:example.org"
    assert (
        make_platform_chat_id("!room:example.org", "$root:example.org")
        == "!room:example.org:$root:example.org"
    )
    assert make_thread_id("!room:example.org") == "matrix_room-example.org"
    assert (
        make_thread_id("!room:example.org", "$root:example.org")
        == "matrix_room-example.org_thread_root-example.org"
    )
    assert strip_matrix_mention("@bot:example.org hello", "@bot:example.org") == "hello"
    assert event_mentions_bot(
        {"content": {"body": "hi", "m.mentions": {"user_ids": ["@bot:example.org"]}}},
        "@bot:example.org",
    )


def test_matrix_command_parser_handles_link_bind_and_exact_stop():
    assert MatrixCommand.parse("link abc").name == "link"
    assert MatrixCommand.parse("link_abc").arg == "abc"
    assert MatrixCommand.parse("@bot:example.org bind code-1", "@bot:example.org").arg == "code-1"
    assert MatrixCommand.parse("stop").name == "stop"
    assert MatrixCommand.parse("stop doing that") is None


def test_dm_like_room_streams_to_native_matrix_thread():
    bot, api, matrix = make_bot()
    bot._dm_like_rooms.add("!dm:example.org")

    asyncio.run(
        bot.handle_matrix_event(
            "!dm:example.org",
            {
                "type": "m.room.message",
                "event_id": "$event1",
                "sender": "@alice:example.org",
                "content": {"msgtype": "m.text", "body": "hello"},
            },
        )
    )

    assert api.last_resolve == {
        "platform": "matrix",
        "platform_user_id": "@alice:example.org",
    }
    assert api.chat_stream_calls == [
        {
            "message": "hello",
            "thread_id": "matrix_dm-example.org",
            "user_id": "user-1",
        }
    ]
    assert matrix.sent == [
        {
            "room_id": "!dm:example.org",
            "content": {
                "msgtype": "m.text",
                "body": "matrix reply",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$event1"}},
            },
        }
    ]


def test_mention_gated_room_ignores_unmentioned_message():
    bot, api, matrix = make_bot()

    asyncio.run(
        bot.handle_matrix_event(
            "!room:example.org",
            {
                "type": "m.room.message",
                "event_id": "$event2",
                "sender": "@alice:example.org",
                "content": {"msgtype": "m.text", "body": "hello"},
            },
        )
    )

    assert api.chat_stream_calls == []
    assert matrix.sent == []


def test_mentioned_room_message_streams_with_room_context():
    bot, api, matrix = make_bot()

    asyncio.run(
        bot.handle_matrix_event(
            "!room:example.org",
            {
                "type": "m.room.message",
                "event_id": "$event3",
                "sender": "@alice:example.org",
                "content": {
                    "msgtype": "m.text",
                    "body": "@bot:example.org status?",
                },
            },
        )
    )

    assert api.chat_stream_calls == [
        {
            "message": "[Matrix @alice:example.org in !room:example.org]\nstatus?",
            "thread_id": "matrix_room-example.org",
            "user_id": "user-1",
        }
    ]
    assert matrix.sent[0]["room_id"] == "!room:example.org"


def test_unlinked_matrix_user_gets_rejection_without_agent_call():
    bot, api, matrix = make_bot(FakeAPI(resolved_user=None))
    bot._dm_like_rooms.add("!dm:example.org")

    asyncio.run(
        bot.handle_matrix_event(
            "!dm:example.org",
            {
                "type": "m.room.message",
                "event_id": "$event4",
                "sender": "@alice:example.org",
                "content": {"msgtype": "m.text", "body": "hello"},
            },
        )
    )

    assert api.chat_stream_calls == []
    assert "not linked" in matrix.sent[0]["content"]["body"]
    assert "@alice:example.org" in matrix.sent[0]["content"]["body"]


def test_link_and_bind_commands_claim_codes():
    bot, api, matrix = make_bot()
    bot._dm_like_rooms.add("!dm:example.org")

    async def run():
        await bot.handle_matrix_event(
            "!dm:example.org",
            {
                "type": "m.room.message",
                "event_id": "$event5",
                "sender": "@alice:example.org",
                "content": {"msgtype": "m.text", "body": "link abc123"},
            },
        )
        await bot.handle_matrix_event(
            "!dm:example.org",
            {
                "type": "m.room.message",
                "event_id": "$event6",
                "sender": "@alice:example.org",
                "content": {"msgtype": "m.text", "body": "bind bind123"},
            },
        )

    asyncio.run(run())

    assert api.claimed_links == [
        {
            "code": "abc123",
            "provider": "matrix",
            "platform_user_id": "@alice:example.org",
        }
    ]
    assert api.claimed_binds == [
        {
            "code": "bind123",
            "provider": "matrix",
            "platform_chat_id": "!dm:example.org",
            "expected_provider_user_id": "@alice:example.org",
        }
    ]
    assert "Linked this Matrix account" in matrix.sent[0]["content"]["body"]
    assert "Bound this Matrix room" in matrix.sent[1]["content"]["body"]


def test_sync_response_updates_dm_summary_and_handles_events():
    bot, api, matrix = make_bot()
    data = {
        "rooms": {
            "join": {
                "!dm:example.org": {
                    "summary": {"m.joined_member_count": 2},
                    "timeline": {
                        "events": [
                            {
                                "type": "m.room.message",
                                "event_id": "$event7",
                                "sender": "@alice:example.org",
                                "content": {"msgtype": "m.text", "body": "hello"},
                            }
                        ]
                    },
                }
            }
        }
    }

    async def run():
        bot._update_room_state(data)
        await bot._handle_sync_response(data)

    asyncio.run(run())

    assert "!dm:example.org" in bot._dm_like_rooms
    assert api.chat_stream_calls[0]["thread_id"] == "matrix_dm-example.org"
    assert matrix.sent[0]["content"]["body"] == "matrix reply"


def test_matrix_mark_seen_uses_shared_ttl_cache():
    # Slice 22 F2 follow-up: matrix's former unbounded set[str] dedupe now rides
    # the shared SeenEventCache, gaining a TTL. The (already-str) event id dedupes
    # within the TTL window and is forgotten (treated as new) once it expires.
    bot, _api, _matrix = make_bot()
    clock = {"now": 1000.0}
    bot._seen = SeenEventCache(clock=lambda: clock["now"])

    assert bot._mark_seen("$evt-1") is False  # first sight: new
    assert bot._mark_seen("$evt-1") is True  # redelivery within TTL: deduped

    clock["now"] += SEEN_EVENT_TTL_SECONDS + 1  # let the entry expire
    assert bot._mark_seen("$evt-1") is False  # forgotten after TTL: new again


def test_send_text_adds_matrix_reply_relation():
    bot, _api, matrix = make_bot()

    asyncio.run(
        bot._send_text(
            MatrixReplyTarget(room_id="!room:example.org", reply_to_event_id="$source"),
            "hello",
        )
    )

    assert matrix.sent == [
        {
            "room_id": "!room:example.org",
            "content": {
                "msgtype": "m.text",
                "body": "hello",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$source"}},
            },
        }
    ]
