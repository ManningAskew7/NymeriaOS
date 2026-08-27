"""Bot surfaces for require_approval hook holds (backlog #77).

Telegram and Discord receive the ``hook_approval`` autonomous event over the
firehose and render native Approve/Deny buttons; every resolution goes
through the REST endpoint under the clicker's identity, so owner-or-admin is
enforced server-side (404 = not yours, 409 = no longer pending). The
``hook_approval_resolved`` event is the single message-edit path for all
resolution surfaces (button, /hook command, desktop, timeout, abort).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Optional

import httpx

from nymeria.triggers.discord_bot import NymeriaDiscordBot
from nymeria.triggers.telegram_bot import NymeriaTelegramBot


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://test/hooks/approvals/r/resolve")
    return httpx.HTTPStatusError(
        f"{status}", request=request, response=httpx.Response(status, request=request)
    )


def _approval_event(**overrides) -> dict:
    event = {
        "type": "hook_approval",
        "thread_id": "telegram_123",
        "record_id": "rec-1",
        "user_id": "owner-user",
        "tool_name": "bash_execute",
        "tool_args_preview": '{"command": "rm -rf build"}',
        "prompt": "Approve tool call bash_execute?",
        "created_at": "2026-07-03T10:00:00+00:00",
        "expires_at": "2026-07-03T10:03:00+00:00",
    }
    event.update(overrides)
    return event


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class _FakeTelegramBotAPI:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.edits: list[dict] = []

    async def send_message(self, chat_id=None, text="", **kwargs) -> Any:
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(chat_id=chat_id, text=text, message_id=777)

    async def edit_message_text(self, **kwargs) -> None:
        self.edits.append(kwargs)


class _FakeAPI:
    """Recording stand-in for NymeriaAPIClient (approval slice only)."""

    def __init__(self, error: Optional[Exception] = None, mode: str = "off") -> None:
        self.error = error
        self.mode = mode
        self.resolved: list[tuple] = []

    async def get_thread_config(self, thread_id: str) -> dict:
        return {"telegram_autonomous_delivery": self.mode}

    async def resolve_hook_approval(self, record_id, approved, *, note=None, user_id=None):
        if self.error is not None:
            raise self.error
        self.resolved.append((record_id, approved, user_id))
        return {"status": "resolved"}


def _make_tg_bot(*, api: Optional[_FakeAPI] = None) -> tuple[NymeriaTelegramBot, _FakeTelegramBotAPI]:
    bot = NymeriaTelegramBot(api=api or _FakeAPI(), bot_token="test-token")
    fake = _FakeTelegramBotAPI()
    bot._application = SimpleNamespace(bot=fake)
    return bot, fake


class _FakeQuery:
    def __init__(self, data: str, chat_id: int = 123) -> None:
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id))
        self.answers: list[tuple] = []
        self.markup_cleared = False

    async def answer(self, text: str = "", show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))

    async def edit_message_reply_markup(self, reply_markup=None) -> None:
        self.markup_cleared = True


def _update_for(query: _FakeQuery, tg_user_id: int = 42) -> Any:
    return SimpleNamespace(
        callback_query=query,
        effective_chat=SimpleNamespace(id=query.message.chat.id),
        effective_user=SimpleNamespace(id=tg_user_id),
    )


def _link(bot: NymeriaTelegramBot, user_id: Optional[str]) -> None:
    async def resolve(
        _tg_id: int, *, guild_id: Optional[int] = None
    ) -> Optional[str]:
        return user_id

    bot.resolve_user_id = resolve  # type: ignore[method-assign]


def test_telegram_approval_sends_buttons_and_bypasses_delivery_gate():
    # delivery mode "off" would drop autonomous output; approvals must not be
    # droppable (silence guarantees a deny).
    bot, fake = _make_tg_bot(api=_FakeAPI(mode="off"))

    asyncio.run(bot._handle_sse_event(_approval_event()))

    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert "bash_execute" in sent["text"]
    assert "/hook approve rec-1" in sent["text"]
    markup = sent.get("reply_markup")
    assert markup is not None
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Approve" in lbl for lbl in labels)
    assert any("Deny" in lbl for lbl in labels)
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert all(d.startswith("hkap:") and len(d.encode()) <= 64 for d in data)
    # Token registered and indexed by record for the resolved-event edit.
    assert bot._hook_approval_by_record["rec-1"] in bot._hook_approval_tokens


def test_telegram_resolved_event_edits_message_and_clears_state():
    bot, fake = _make_tg_bot()
    asyncio.run(bot._handle_sse_event(_approval_event()))

    asyncio.run(bot._handle_sse_event({
        "type": "hook_approval_resolved",
        "thread_id": "telegram_123",
        "record_id": "rec-1",
        "tool_name": "bash_execute",
        "outcome": "approved",
        "resolved_by": "owner-user",
    }))

    assert len(fake.edits) == 1
    edit = fake.edits[0]
    assert edit["chat_id"] == 123 and edit["message_id"] == 777
    assert "Approved by owner-user" in edit["text"]
    assert bot._hook_approval_tokens == {}
    assert bot._hook_approval_by_record == {}


def test_telegram_timeout_outcome_renders_denial():
    bot, fake = _make_tg_bot()
    asyncio.run(bot._handle_sse_event(_approval_event()))
    asyncio.run(bot._handle_sse_event({
        "type": "hook_approval_resolved",
        "thread_id": "telegram_123",
        "record_id": "rec-1",
        "outcome": "timeout",
    }))
    assert "denied" in fake.edits[0]["text"]


def test_telegram_button_resolves_via_api_as_clicker():
    api = _FakeAPI()
    bot, _ = _make_tg_bot(api=api)
    _link(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_approval_event()))
    token = bot._hook_approval_by_record["rec-1"]

    query = _FakeQuery(f"hkap:a:{token}")
    asyncio.run(bot._on_hook_approval_button(_update_for(query), None))

    assert api.resolved == [("rec-1", True, "clicker-user")]
    assert query.answers == [("Approved.", False)]
    # Token stays until the resolved event pops it (single edit path).
    assert token in bot._hook_approval_tokens


def test_telegram_deny_button_resolves_false():
    api = _FakeAPI()
    bot, _ = _make_tg_bot(api=api)
    _link(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_approval_event()))
    token = bot._hook_approval_by_record["rec-1"]

    query = _FakeQuery(f"hkap:d:{token}")
    asyncio.run(bot._on_hook_approval_button(_update_for(query), None))

    assert api.resolved == [("rec-1", False, "clicker-user")]
    assert query.answers == [("Denied.", False)]


def test_telegram_button_rejects_unlinked_clicker():
    api = _FakeAPI()
    bot, _ = _make_tg_bot(api=api)
    _link(bot, None)
    asyncio.run(bot._handle_sse_event(_approval_event()))
    token = bot._hook_approval_by_record["rec-1"]

    query = _FakeQuery(f"hkap:a:{token}")
    asyncio.run(bot._on_hook_approval_button(_update_for(query), None))

    assert api.resolved == []
    assert query.answers and query.answers[0][1] is True  # alert shown


def test_telegram_button_404_means_not_authorized():
    api = _FakeAPI(error=_http_error(404))
    bot, _ = _make_tg_bot(api=api)
    _link(bot, "other-user")
    asyncio.run(bot._handle_sse_event(_approval_event()))
    token = bot._hook_approval_by_record["rec-1"]

    query = _FakeQuery(f"hkap:a:{token}")
    asyncio.run(bot._on_hook_approval_button(_update_for(query), None))

    assert query.answers == [("Only the requester or an admin can resolve this.", True)]
    assert token in bot._hook_approval_tokens  # still resolvable by the owner


def test_telegram_button_409_cleans_up_stale_prompt():
    api = _FakeAPI(error=_http_error(409))
    bot, _ = _make_tg_bot(api=api)
    _link(bot, "owner-user")
    asyncio.run(bot._handle_sse_event(_approval_event()))
    token = bot._hook_approval_by_record["rec-1"]

    query = _FakeQuery(f"hkap:a:{token}")
    asyncio.run(bot._on_hook_approval_button(_update_for(query), None))

    assert query.answers == [("No longer pending.", True)]
    assert token not in bot._hook_approval_tokens
    assert query.markup_cleared


def test_telegram_button_wrong_chat_rejected():
    api = _FakeAPI()
    bot, _ = _make_tg_bot(api=api)
    _link(bot, "owner-user")
    asyncio.run(bot._handle_sse_event(_approval_event()))
    token = bot._hook_approval_by_record["rec-1"]

    query = _FakeQuery(f"hkap:a:{token}", chat_id=999)
    asyncio.run(bot._on_hook_approval_button(_update_for(query), None))

    assert api.resolved == []
    assert query.answers and "another chat" in query.answers[0][0]


def test_telegram_expired_token_rejected():
    api = _FakeAPI()
    bot, _ = _make_tg_bot(api=api)
    _link(bot, "owner-user")

    query = _FakeQuery("hkap:a:unknown-token")
    asyncio.run(bot._on_hook_approval_button(_update_for(query), None))

    assert api.resolved == []
    assert query.answers and "expired" in query.answers[0][0]


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


class _FakeDiscordMessage:
    def __init__(self, content: str = "", view: Any = None) -> None:
        self.content = content
        self.view = view
        self.edits: list[dict] = []

    async def edit(self, **kwargs) -> None:
        self.edits.append(kwargs)
        if "content" in kwargs:
            self.content = kwargs["content"]


class _FakeDiscordChannel:
    def __init__(self, channel_id: int = 456) -> None:
        self.id = channel_id
        self.messages: list[_FakeDiscordMessage] = []

    async def send(self, content: str = "", **kwargs) -> _FakeDiscordMessage:
        msg = _FakeDiscordMessage(content=content, view=kwargs.get("view"))
        self.messages.append(msg)
        return msg


def _make_discord_bot(
    channel: _FakeDiscordChannel, *, api: Optional[_FakeAPI] = None
) -> NymeriaDiscordBot:
    bot = NymeriaDiscordBot.__new__(NymeriaDiscordBot)
    bot.api = api or _FakeAPI()
    bot._autonomous_state = {}
    bot._hook_approval_messages = {}
    bot.get_channel = lambda channel_id: channel if channel_id == channel.id else None

    async def _fetch_channel(channel_id: int):
        return channel if channel_id == channel.id else None

    bot.fetch_channel = _fetch_channel
    return bot


class _FakeInteractionResponse:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    def is_done(self) -> bool:
        return bool(self.sent)

    async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
        self.sent.append((content, ephemeral))


def _fake_interaction(user_id: int = 42, message: Any = None) -> Any:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=None,
        response=_FakeInteractionResponse(),
        message=message,
    )


def _discord_event(**overrides) -> dict:
    return _approval_event(thread_id="discord_123_456", **overrides)


def test_discord_approval_sends_view_and_tracks_record():
    channel = _FakeDiscordChannel()
    bot = _make_discord_bot(channel)

    asyncio.run(bot._handle_sse_event(_discord_event()))

    assert len(channel.messages) == 1
    msg = channel.messages[0]
    assert "bash_execute" in msg.content
    assert "/hook approve rec-1" in msg.content
    assert msg.view is not None
    labels = [child.label for child in msg.view.children]
    assert "Approve" in labels and "Deny" in labels
    assert "rec-1" in bot._hook_approval_messages


def test_discord_resolved_event_edits_message_and_clears_state():
    channel = _FakeDiscordChannel()
    bot = _make_discord_bot(channel)
    asyncio.run(bot._handle_sse_event(_discord_event()))

    asyncio.run(bot._handle_sse_event({
        "type": "hook_approval_resolved",
        "thread_id": "discord_123_456",
        "record_id": "rec-1",
        "tool_name": "bash_execute",
        "outcome": "denied",
        "resolved_by": "owner-user",
        "note": "not on prod",
    }))

    msg = channel.messages[0]
    assert msg.edits and msg.edits[0]["view"] is None
    assert "Denied by owner-user" in msg.content
    assert "not on prod" in msg.content
    assert bot._hook_approval_messages == {}


def test_discord_view_click_resolves_via_api_as_clicker():
    channel = _FakeDiscordChannel()
    api = _FakeAPI()
    bot = _make_discord_bot(channel, api=api)
    _link(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_discord_event()))
    view = channel.messages[0].view

    interaction = _fake_interaction()
    asyncio.run(view._resolve(interaction, True))

    assert api.resolved == [("rec-1", True, "clicker-user")]
    assert interaction.response.sent == [("Approved.", True)]
    assert view.resolved is True


def test_discord_view_double_click_guarded():
    channel = _FakeDiscordChannel()
    api = _FakeAPI()
    bot = _make_discord_bot(channel, api=api)
    _link(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_discord_event()))
    view = channel.messages[0].view

    asyncio.run(view._resolve(_fake_interaction(), True))
    second = _fake_interaction()
    asyncio.run(view._resolve(second, False))

    assert api.resolved == [("rec-1", True, "clicker-user")]  # only the first
    assert second.response.sent == [("Already resolved.", True)]


def test_discord_view_rejects_unlinked_clicker():
    channel = _FakeDiscordChannel()
    api = _FakeAPI()
    bot = _make_discord_bot(channel, api=api)
    _link(bot, None)
    asyncio.run(bot._handle_sse_event(_discord_event()))
    view = channel.messages[0].view

    interaction = _fake_interaction()
    asyncio.run(view._resolve(interaction, True))

    assert api.resolved == []
    assert "isn't linked" in interaction.response.sent[0][0]
    assert view.resolved is False


def test_discord_view_renders_infra_copy_when_resolution_unavailable():
    # Backlog #108: a resolver infra failure on the approval button must render
    # backend-unavailable copy, never link instructions, and must not resolve.
    from nymeria.triggers.bot_helpers import PlatformResolveUnavailableError

    channel = _FakeDiscordChannel()
    api = _FakeAPI()
    bot = _make_discord_bot(channel, api=api)

    async def _raise(_discord_user_id: int, *, guild_id: Optional[int] = None):
        raise PlatformResolveUnavailableError("discord", RuntimeError("backend down"))

    bot.resolve_user_id = _raise  # type: ignore[method-assign]
    asyncio.run(bot._handle_sse_event(_discord_event()))
    view = channel.messages[0].view

    interaction = _fake_interaction()
    asyncio.run(view._resolve(interaction, True))

    assert api.resolved == []
    assert interaction.response.sent, "infra copy must be sent"
    content = interaction.response.sent[0][0]
    assert "service token" in content
    assert "isn't linked" not in content
    assert view.resolved is False


def test_discord_view_404_means_not_authorized():
    channel = _FakeDiscordChannel()
    bot = _make_discord_bot(channel, api=_FakeAPI(error=_http_error(404)))
    _link(bot, "other-user")
    asyncio.run(bot._handle_sse_event(_discord_event()))
    view = channel.messages[0].view

    interaction = _fake_interaction()
    asyncio.run(view._resolve(interaction, True))

    assert "Only the requester or an admin" in interaction.response.sent[0][0]
    assert view.resolved is False


def test_discord_view_409_marks_resolved_and_drops_buttons():
    channel = _FakeDiscordChannel()
    bot = _make_discord_bot(channel, api=_FakeAPI(error=_http_error(409)))
    _link(bot, "owner-user")
    asyncio.run(bot._handle_sse_event(_discord_event()))
    msg = channel.messages[0]
    view = msg.view

    interaction = _fake_interaction(message=msg)
    asyncio.run(view._resolve(interaction, True))

    assert interaction.response.sent == [("No longer pending.", True)]
    assert view.resolved is True
    assert msg.edits and msg.edits[0]["view"] is None
