"""Bot surfaces for LLM fallback consent (llm-fallback-consent Phase 3).

Telegram and Discord receive the ``fallback_prompt`` autonomous event and
render native Swap/Don't-swap buttons; resolutions go through the REST
approval endpoint under the clicker's identity (owner-or-admin server-side,
404 = not yours, 409 = no longer pending), and ``fallback_prompt_resolved``
is the single message-edit path for every resolution surface. The pair
bypasses the autonomous delivery-mode gates (an unanswered prompt AUTO-SWAPS
the thread's model, so muting must not cost the user their say). Applied
swaps (``provider_fallback``) render a notice with an inline Revert button
that clears the thread's active fallback via the standard config PATCH.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Optional

import httpx

from nymeria.triggers.telegram_bot import NymeriaTelegramBot


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://test/llm/fallback-approvals/r/resolve")
    return httpx.HTTPStatusError(
        f"{status}", request=request, response=httpx.Response(status, request=request)
    )


def _prompt_event(**overrides) -> dict:
    event = {
        "type": "fallback_prompt",
        "thread_id": "telegram_123",
        "record_id": "fb-1",
        "user_id": "owner-user",
        "kind": "transport",
        "from_provider": "anthropic",
        "from_model": "claude-fable-5",
        "to_provider": "anthropic",
        "to_model": "claude-opus-4-8",
        "reason": "provider_server_error",
        "http_status": 529,
        "timeout_seconds": 180,
        "hold_options": [600, 3600, 7200, 28800],
        "allow_permanent": True,
        "default_hold_seconds": 7200,
        "created_at": "2026-07-25T10:00:00+00:00",
        "expires_at": "2026-07-25T10:03:00+00:00",
    }
    event.update(overrides)
    return event


def _swap_event(**overrides) -> dict:
    event = {
        "type": "provider_fallback",
        "thread_id": "telegram_123",
        "from_provider": "anthropic",
        "from_model": "claude-fable-5",
        "to_provider": "anthropic",
        "to_model": "claude-opus-4-8",
        "reason": "provider_server_error",
        "http_status": 529,
        "hold_seconds": 7200,
        "permanent": False,
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
    """Recording stand-in for NymeriaAPIClient (fallback-consent slice)."""

    def __init__(self, error: Optional[Exception] = None, mode: str = "off") -> None:
        self.error = error
        self.mode = mode
        self.resolved: list[tuple] = []
        self.config_patches: list[tuple] = []

    async def get_thread_config(self, thread_id: str) -> dict:
        return {"telegram_autonomous_delivery": self.mode}

    async def resolve_fallback_approval(
        self,
        record_id,
        approved,
        *,
        hold_seconds=None,
        hold_permanent=False,
        note=None,
        user_id=None,
    ):
        if self.error is not None:
            raise self.error
        self.resolved.append(
            (record_id, approved, hold_seconds, hold_permanent, user_id)
        )
        return {"status": "resolved"}

    async def update_thread_config(self, thread_id, *, user_id=None, **kwargs):
        if self.error is not None:
            raise self.error
        self.config_patches.append((thread_id, user_id, kwargs))
        return {"status": "ok"}


def _make_tg_bot(
    *, api: Optional[_FakeAPI] = None
) -> tuple[NymeriaTelegramBot, _FakeTelegramBotAPI]:
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
    async def resolve(_tg_id: int) -> Optional[str]:
        return user_id

    bot.resolve_user_id = resolve  # type: ignore[method-assign]


def _buttons(markup) -> list[tuple[str, str]]:
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def test_telegram_prompt_sends_buttons_and_bypasses_delivery_gate():
    # delivery mode "off" would drop autonomous output; consent prompts must
    # not be droppable (silence means an unconsented auto-swap).
    bot, fake = _make_tg_bot(api=_FakeAPI(mode="off"))

    asyncio.run(bot._handle_sse_event(_prompt_event()))

    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert "/fallback approve fb-1" in sent["text"]
    buttons = _buttons(sent["reply_markup"])
    labels = [text for text, _data in buttons]
    assert any("Swap (2h)" in lbl for lbl in labels)
    assert any("until reverted" in lbl for lbl in labels)
    assert any("Don't swap" in lbl for lbl in labels)
    assert all(
        data.startswith("fbap:") and len(data.encode()) <= 64
        for _text, data in buttons
    )
    assert bot._fallback_by_record["fb-1"] in bot._fallback_tokens


def test_telegram_prompt_without_permanent_option():
    bot, fake = _make_tg_bot()
    asyncio.run(bot._handle_sse_event(_prompt_event(allow_permanent=False)))
    labels = [text for text, _data in _buttons(fake.sent[0]["reply_markup"])]
    assert not any("until reverted" in lbl for lbl in labels)


def test_telegram_resolved_approved_edits_message_with_revert_button():
    bot, fake = _make_tg_bot()
    asyncio.run(bot._handle_sse_event(_prompt_event()))

    asyncio.run(bot._handle_sse_event({
        "type": "fallback_prompt_resolved",
        "thread_id": "telegram_123",
        "record_id": "fb-1",
        "kind": "transport",
        "outcome": "approved",
        "resolved_by": "owner-user",
        "hold_seconds": 7200,
        "hold_permanent": False,
    }))

    assert len(fake.edits) == 1
    edit = fake.edits[0]
    assert edit["chat_id"] == 123 and edit["message_id"] == 777
    assert "Swapped to the fallback for 2 hours" in edit["text"]
    assert "owner-user" in edit["text"]
    buttons = _buttons(edit["reply_markup"])
    assert len(buttons) == 1 and buttons[0][1].startswith("fbrv:")
    # Prompt token retired; only the freshly minted revert token remains.
    assert bot._fallback_by_record == {}
    assert len(bot._fallback_tokens) == 1


def test_telegram_resolved_declined_retracts_keyboard():
    bot, fake = _make_tg_bot()
    asyncio.run(bot._handle_sse_event(_prompt_event()))
    asyncio.run(bot._handle_sse_event({
        "type": "fallback_prompt_resolved",
        "thread_id": "telegram_123",
        "record_id": "fb-1",
        "outcome": "declined",
    }))
    edit = fake.edits[0]
    assert "Not swapped" in edit["text"]
    assert edit["reply_markup"] is None
    assert bot._fallback_tokens == {}


def test_telegram_timeout_outcome_keeps_revert_button():
    # Timeout = auto-swap applied, so the thread IS on the fallback.
    bot, fake = _make_tg_bot()
    asyncio.run(bot._handle_sse_event(_prompt_event()))
    asyncio.run(bot._handle_sse_event({
        "type": "fallback_prompt_resolved",
        "thread_id": "telegram_123",
        "record_id": "fb-1",
        "outcome": "timeout",
    }))
    edit = fake.edits[0]
    assert "auto-swapped" in edit["text"]
    assert edit["reply_markup"] is not None


def test_telegram_swap_button_applies_default_hold_as_clicker():
    api = _FakeAPI()
    bot, _ = _make_tg_bot(api=api)
    _link(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_prompt_event()))
    token = bot._fallback_by_record["fb-1"]

    query = _FakeQuery(f"fbap:a:{token}")
    asyncio.run(bot._on_fallback_prompt_button(_update_for(query), None))

    assert api.resolved == [("fb-1", True, 7200, False, "clicker-user")]
    assert query.answers == [("Swapping.", False)]
    # Token stays until the resolved event pops it (single edit path).
    assert token in bot._fallback_tokens


def test_telegram_permanent_button_sets_hold_permanent():
    api = _FakeAPI()
    bot, _ = _make_tg_bot(api=api)
    _link(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_prompt_event()))
    token = bot._fallback_by_record["fb-1"]

    query = _FakeQuery(f"fbap:p:{token}")
    asyncio.run(bot._on_fallback_prompt_button(_update_for(query), None))

    assert api.resolved == [("fb-1", True, None, True, "clicker-user")]


def test_telegram_deny_button_resolves_false():
    api = _FakeAPI()
    bot, _ = _make_tg_bot(api=api)
    _link(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_prompt_event()))
    token = bot._fallback_by_record["fb-1"]

    query = _FakeQuery(f"fbap:d:{token}")
    asyncio.run(bot._on_fallback_prompt_button(_update_for(query), None))

    assert api.resolved == [("fb-1", False, None, False, "clicker-user")]
    assert query.answers == [("Staying on the primary model.", False)]


def test_telegram_button_404_means_not_authorized():
    api = _FakeAPI(error=_http_error(404))
    bot, _ = _make_tg_bot(api=api)
    _link(bot, "other-user")
    asyncio.run(bot._handle_sse_event(_prompt_event()))
    token = bot._fallback_by_record["fb-1"]

    query = _FakeQuery(f"fbap:a:{token}")
    asyncio.run(bot._on_fallback_prompt_button(_update_for(query), None))

    assert query.answers == [("Only the requester or an admin can resolve this.", True)]
    assert token in bot._fallback_tokens  # still resolvable by the owner


def test_telegram_button_409_cleans_up_stale_prompt():
    api = _FakeAPI(error=_http_error(409))
    bot, _ = _make_tg_bot(api=api)
    _link(bot, "owner-user")
    asyncio.run(bot._handle_sse_event(_prompt_event()))
    token = bot._fallback_by_record["fb-1"]

    query = _FakeQuery(f"fbap:a:{token}")
    asyncio.run(bot._on_fallback_prompt_button(_update_for(query), None))

    assert query.answers == [("No longer pending.", True)]
    assert token not in bot._fallback_tokens
    assert query.markup_cleared


def test_telegram_button_wrong_chat_rejected():
    api = _FakeAPI()
    bot, _ = _make_tg_bot(api=api)
    _link(bot, "owner-user")
    asyncio.run(bot._handle_sse_event(_prompt_event()))
    token = bot._fallback_by_record["fb-1"]

    query = _FakeQuery(f"fbap:a:{token}", chat_id=999)
    asyncio.run(bot._on_fallback_prompt_button(_update_for(query), None))

    assert api.resolved == []
    assert query.answers and "another chat" in query.answers[0][0]


def test_telegram_revert_button_clears_active_fallback_as_clicker():
    api = _FakeAPI()
    bot, fake = _make_tg_bot(api=api)
    _link(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_prompt_event()))
    asyncio.run(bot._handle_sse_event({
        "type": "fallback_prompt_resolved",
        "thread_id": "telegram_123",
        "record_id": "fb-1",
        "outcome": "approved",
        "hold_seconds": 7200,
    }))
    revert_data = _buttons(fake.edits[0]["reply_markup"])[0][1]

    query = _FakeQuery(revert_data)
    asyncio.run(bot._on_fallback_revert_button(_update_for(query), None))

    assert api.config_patches == [
        ("telegram_123", "clicker-user", {"clear_active_fallback": True})
    ]
    assert query.answers == [("Reverted to the primary model.", False)]
    assert query.markup_cleared
    assert bot._fallback_tokens == {}


def test_telegram_revert_button_expired_token_points_at_command():
    bot, _ = _make_tg_bot()
    _link(bot, "owner-user")
    query = _FakeQuery("fbrv:unknown-token")
    asyncio.run(bot._on_fallback_revert_button(_update_for(query), None))
    assert query.answers and "/fallback revert" in query.answers[0][0]


def test_telegram_swap_notice_carries_revert_button_in_full_mode():
    bot, fake = _make_tg_bot(api=_FakeAPI(mode="full"))

    asyncio.run(bot._handle_sse_event(_swap_event()))

    assert len(fake.sent) == 1
    sent = fake.sent[0]
    assert "Switched to fallback claude-opus-4-8 for 2 hours" in sent["text"]
    assert "/fallback revert" in sent["text"]
    buttons = _buttons(sent["reply_markup"])
    assert len(buttons) == 1 and buttons[0][1].startswith("fbrv:")


def test_telegram_swap_notice_respects_delivery_gate():
    # Unlike the consent prompt, the applied-swap notice is ordinary
    # autonomous output: a muted thread stays quiet (/fallback status covers
    # the state).
    bot, fake = _make_tg_bot(api=_FakeAPI(mode="off"))
    asyncio.run(bot._handle_sse_event(_swap_event()))
    assert fake.sent == []


def test_telegram_zero_default_hold_passes_through():
    # llm_fallback_hold_seconds=0 is a legitimate operator choice ("swap for
    # this turn only"); it must not be conflated with an absent key and
    # silently upgraded to the 2h preset.
    api = _FakeAPI()
    bot, fake = _make_tg_bot(api=api)
    _link(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_prompt_event(default_hold_seconds=0)))

    labels = [text for text, _data in _buttons(fake.sent[0]["reply_markup"])]
    assert "✅ Swap" in labels  # no duration suffix
    token = bot._fallback_by_record["fb-1"]
    query = _FakeQuery(f"fbap:a:{token}")
    asyncio.run(bot._on_fallback_prompt_button(_update_for(query), None))
    assert api.resolved == [("fb-1", True, 0, False, "clicker-user")]


def test_telegram_resolved_without_explicit_hold_omits_phrase():
    # hold_seconds=None on the resolved event means "the configured default
    # applies"; rendering it as "for this turn" would contradict the
    # provider_fallback notice that follows with the ACTUAL hold.
    bot, fake = _make_tg_bot()
    asyncio.run(bot._handle_sse_event(_prompt_event()))
    asyncio.run(bot._handle_sse_event({
        "type": "fallback_prompt_resolved",
        "thread_id": "telegram_123",
        "record_id": "fb-1",
        "outcome": "approved",
        "resolved_by": "owner-user",
    }))
    text = fake.edits[0]["text"]
    assert "Swapped to the fallback (by owner-user)." in text
    assert "for this turn" not in text


def test_telegram_resolved_declined_copy_is_kind_aware():
    # Transport decline fails the turn with the original provider error;
    # refusal decline leaves the refusal standing (the P1 rewind path).
    bot, fake = _make_tg_bot()
    asyncio.run(bot._handle_sse_event(_prompt_event()))
    asyncio.run(bot._handle_sse_event({
        "type": "fallback_prompt_resolved",
        "thread_id": "telegram_123",
        "record_id": "fb-1",
        "kind": "transport",
        "outcome": "declined",
    }))
    assert "turn fails with the original provider error" in fake.edits[0]["text"]

    asyncio.run(bot._handle_sse_event(_prompt_event(record_id="fb-2", kind="refusal")))
    asyncio.run(bot._handle_sse_event({
        "type": "fallback_prompt_resolved",
        "thread_id": "telegram_123",
        "record_id": "fb-2",
        "kind": "refusal",
        "outcome": "declined",
    }))
    assert "refusal stands" in fake.edits[1]["text"]


def test_telegram_swap_notice_reverts_dispatch_target():
    # On a dispatched turn (@mention routing, /quick) the wire chunks carry
    # the ORIGINATING thread id while the hold lives on the dispatch target;
    # the Revert button must PATCH the target.
    api = _FakeAPI(mode="full")
    bot, fake = _make_tg_bot(api=api)
    _link(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_swap_event(
        dispatched_to={"thread_id": "target-thread", "title": "Target"},
    )))

    revert_data = _buttons(fake.sent[0]["reply_markup"])[0][1]
    query = _FakeQuery(revert_data)
    asyncio.run(bot._on_fallback_revert_button(_update_for(query), None))
    assert api.config_patches == [
        ("target-thread", "clicker-user", {"clear_active_fallback": True})
    ]


def test_telegram_interactive_handler_sends_swap_notice():
    # The interactive chat handler (the path a Telegram-origin turn actually
    # takes) must deliver the notice with a Revert button; the delivery-mode
    # gate does not apply on the interactive stream.
    bot, fake = _make_tg_bot(api=_FakeAPI(mode="off"))

    async def _run() -> None:
        handler = bot._ChatSSEHandler(
            bot, 123, "telegram_123", "owner-user", 42, SimpleNamespace(bot=fake)
        )
        try:
            await handler.on_provider_fallback(_swap_event())
        finally:
            handler.cleanup()

    asyncio.run(_run())
    assert len(fake.sent) == 1
    assert "Switched to fallback" in fake.sent[0]["text"]
    assert _buttons(fake.sent[0]["reply_markup"])[0][1].startswith("fbrv:")


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------

from nymeria.triggers.discord_bot import NymeriaDiscordBot  # noqa: E402


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
    bot._fallback_prompt_messages = {}
    bot.get_channel = lambda channel_id: channel if channel_id == channel.id else None

    async def _fetch_channel(channel_id: int):
        return channel if channel_id == channel.id else None

    bot.fetch_channel = _fetch_channel
    return bot


class _FakeInteractionResponse:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
        self.sent.append((content, ephemeral))


def _fake_interaction(user_id: int = 42, message: Any = None) -> Any:
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=_FakeInteractionResponse(),
        message=message,
    )


def _link_discord(bot: NymeriaDiscordBot, user_id: Optional[str]) -> None:
    async def resolve(_discord_id: int) -> Optional[str]:
        return user_id

    bot.resolve_user_id = resolve  # type: ignore[method-assign]


def _discord_prompt_event(**overrides) -> dict:
    return _prompt_event(thread_id="discord_123_456", **overrides)


def test_discord_prompt_sends_view_and_tracks_record():
    channel = _FakeDiscordChannel()
    bot = _make_discord_bot(channel)

    asyncio.run(bot._handle_sse_event(_discord_prompt_event()))

    assert len(channel.messages) == 1
    msg = channel.messages[0]
    assert "/fallback approve fb-1" in msg.content
    labels = [child.label for child in msg.view.children]
    assert labels == ["Swap (2h)", "Swap until reverted", "Don't swap"]
    assert "fb-1" in bot._fallback_prompt_messages


def test_discord_prompt_without_permanent_option():
    channel = _FakeDiscordChannel()
    bot = _make_discord_bot(channel)
    asyncio.run(
        bot._handle_sse_event(_discord_prompt_event(allow_permanent=False))
    )
    labels = [child.label for child in channel.messages[0].view.children]
    assert labels == ["Swap (2h)", "Don't swap"]


def test_discord_resolved_approved_edits_message_with_revert_view():
    channel = _FakeDiscordChannel()
    bot = _make_discord_bot(channel)
    asyncio.run(bot._handle_sse_event(_discord_prompt_event()))

    asyncio.run(bot._handle_sse_event({
        "type": "fallback_prompt_resolved",
        "thread_id": "discord_123_456",
        "record_id": "fb-1",
        "outcome": "approved",
        "resolved_by": "owner-user",
        "hold_seconds": 7200,
    }))

    msg = channel.messages[0]
    assert msg.edits
    assert "Swapped to the fallback for 2 hours" in msg.content
    assert "owner-user" in msg.content
    revert_view = msg.edits[0]["view"]
    assert revert_view is not None
    assert [child.label for child in revert_view.children] == ["Revert"]
    assert bot._fallback_prompt_messages == {}


def test_discord_resolved_declined_drops_buttons():
    channel = _FakeDiscordChannel()
    bot = _make_discord_bot(channel)
    asyncio.run(bot._handle_sse_event(_discord_prompt_event()))
    asyncio.run(bot._handle_sse_event({
        "type": "fallback_prompt_resolved",
        "thread_id": "discord_123_456",
        "record_id": "fb-1",
        "outcome": "declined",
    }))
    msg = channel.messages[0]
    assert "Not swapped" in msg.content
    assert msg.edits[0]["view"] is None


def test_discord_swap_button_applies_default_hold_as_clicker():
    channel = _FakeDiscordChannel()
    api = _FakeAPI()
    bot = _make_discord_bot(channel, api=api)
    _link_discord(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_discord_prompt_event()))
    view = channel.messages[0].view

    interaction = _fake_interaction()
    asyncio.run(view.swap.callback(interaction))

    assert api.resolved == [("fb-1", True, 7200, False, "clicker-user")]
    assert interaction.response.sent == [("Swapping.", True)]
    assert view.resolved is True


def test_discord_permanent_button_sets_hold_permanent():
    channel = _FakeDiscordChannel()
    api = _FakeAPI()
    bot = _make_discord_bot(channel, api=api)
    _link_discord(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_discord_prompt_event()))
    view = channel.messages[0].view

    asyncio.run(view.swap_permanent.callback(_fake_interaction()))

    assert api.resolved == [("fb-1", True, None, True, "clicker-user")]


def test_discord_deny_button_resolves_false():
    channel = _FakeDiscordChannel()
    api = _FakeAPI()
    bot = _make_discord_bot(channel, api=api)
    _link_discord(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_discord_prompt_event()))
    view = channel.messages[0].view

    interaction = _fake_interaction()
    asyncio.run(view.deny.callback(interaction))

    assert api.resolved == [("fb-1", False, None, False, "clicker-user")]
    assert interaction.response.sent == [
        ("Staying on the primary model.", True)
    ]


def test_discord_view_double_click_guarded():
    channel = _FakeDiscordChannel()
    api = _FakeAPI()
    bot = _make_discord_bot(channel, api=api)
    _link_discord(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_discord_prompt_event()))
    view = channel.messages[0].view

    asyncio.run(view.swap.callback(_fake_interaction()))
    second = _fake_interaction()
    asyncio.run(view.deny.callback(second))

    assert api.resolved == [("fb-1", True, 7200, False, "clicker-user")]
    assert second.response.sent == [("Already resolved.", True)]


def test_discord_view_404_means_not_authorized():
    channel = _FakeDiscordChannel()
    bot = _make_discord_bot(channel, api=_FakeAPI(error=_http_error(404)))
    _link_discord(bot, "other-user")
    asyncio.run(bot._handle_sse_event(_discord_prompt_event()))
    view = channel.messages[0].view

    interaction = _fake_interaction()
    asyncio.run(view.swap.callback(interaction))

    assert "Only the requester or an admin" in interaction.response.sent[0][0]
    assert view.resolved is False


def test_discord_view_409_marks_resolved_and_drops_buttons():
    channel = _FakeDiscordChannel()
    bot = _make_discord_bot(channel, api=_FakeAPI(error=_http_error(409)))
    _link_discord(bot, "owner-user")
    asyncio.run(bot._handle_sse_event(_discord_prompt_event()))
    msg = channel.messages[0]
    view = msg.view

    interaction = _fake_interaction(message=msg)
    asyncio.run(view.swap.callback(interaction))

    assert interaction.response.sent == [("No longer pending.", True)]
    assert view.resolved is True
    assert msg.edits and msg.edits[0]["view"] is None


def test_discord_revert_button_clears_active_fallback_as_clicker():
    channel = _FakeDiscordChannel()
    api = _FakeAPI()
    bot = _make_discord_bot(channel, api=api)
    _link_discord(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_discord_prompt_event()))
    asyncio.run(bot._handle_sse_event({
        "type": "fallback_prompt_resolved",
        "thread_id": "discord_123_456",
        "record_id": "fb-1",
        "outcome": "timeout",
    }))
    msg = channel.messages[0]
    revert_view = msg.edits[0]["view"]

    interaction = _fake_interaction(message=msg)
    asyncio.run(revert_view.revert.callback(interaction))

    assert api.config_patches == [
        ("discord_123_456", "clicker-user", {"clear_active_fallback": True})
    ]
    assert interaction.response.sent == [
        ("Reverted to the primary model.", True)
    ]


def test_discord_swap_notice_carries_revert_view():
    channel = _FakeDiscordChannel()
    bot = _make_discord_bot(channel)

    asyncio.run(bot._handle_sse_event(
        _swap_event(thread_id="discord_123_456")
    ))

    assert len(channel.messages) == 1
    msg = channel.messages[0]
    assert "Switched to fallback claude-opus-4-8 for 2 hours" in msg.content
    assert "/fallback revert" in msg.content
    assert [child.label for child in msg.view.children] == ["Revert"]


def test_discord_zero_default_hold_passes_through():
    # 0 = "swap for this turn only" must not be upgraded to the 2h preset.
    channel = _FakeDiscordChannel()
    api = _FakeAPI()
    bot = _make_discord_bot(channel, api=api)
    _link_discord(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(
        _discord_prompt_event(default_hold_seconds=0)
    ))
    view = channel.messages[0].view
    assert view.swap.label == "Swap"  # no duration suffix

    asyncio.run(view.swap.callback(_fake_interaction()))
    assert api.resolved == [("fb-1", True, 0, False, "clicker-user")]


def test_discord_resolved_without_explicit_hold_omits_phrase():
    channel = _FakeDiscordChannel()
    bot = _make_discord_bot(channel)
    asyncio.run(bot._handle_sse_event(_discord_prompt_event()))
    asyncio.run(bot._handle_sse_event({
        "type": "fallback_prompt_resolved",
        "thread_id": "discord_123_456",
        "record_id": "fb-1",
        "outcome": "approved",
        "resolved_by": "owner-user",
    }))
    msg = channel.messages[0]
    assert "Swapped to the fallback (by owner-user)." in msg.content
    assert "for this turn" not in msg.content


def test_discord_resolved_declined_copy_is_kind_aware():
    channel = _FakeDiscordChannel()
    bot = _make_discord_bot(channel)
    asyncio.run(bot._handle_sse_event(_discord_prompt_event(kind="refusal")))
    asyncio.run(bot._handle_sse_event({
        "type": "fallback_prompt_resolved",
        "thread_id": "discord_123_456",
        "record_id": "fb-1",
        "kind": "refusal",
        "outcome": "declined",
    }))
    assert "refusal stands" in channel.messages[0].content


def test_discord_swap_notice_reverts_dispatch_target():
    # Dispatched turns stream under the originating thread id while the hold
    # lives on the dispatch target; Revert must PATCH the target.
    channel = _FakeDiscordChannel()
    api = _FakeAPI()
    bot = _make_discord_bot(channel, api=api)
    _link_discord(bot, "clicker-user")
    asyncio.run(bot._handle_sse_event(_swap_event(
        thread_id="discord_123_456",
        dispatched_to={"thread_id": "target-thread", "title": "Target"},
    )))
    msg = channel.messages[0]

    interaction = _fake_interaction(message=msg)
    asyncio.run(msg.view.revert.callback(interaction))
    assert api.config_patches == [
        ("target-thread", "clicker-user", {"clear_active_fallback": True})
    ]


def test_discord_interactive_handler_sends_swap_notice():
    # The interactive chat handler (the path a Discord-origin turn actually
    # takes, with its bound thread_id as the 4th positional) must deliver the
    # notice with a Revert view.
    channel = _FakeDiscordChannel()
    api = _FakeAPI()
    bot = _make_discord_bot(channel, api=api)
    _link_discord(bot, "clicker-user")

    async def _first_send(content: str, **kwargs):
        return await channel.send(content, **kwargs)

    handler = bot._InteractiveChatHandler(bot, channel, _first_send, "discord_123_456")
    asyncio.run(handler.on_provider_fallback(_swap_event(thread_id="")))
    msg = channel.messages[0]
    assert "Switched to fallback" in msg.content
    assert [child.label for child in msg.view.children] == ["Revert"]

    # The revert targets the handler's bound thread.
    interaction = _fake_interaction(message=msg)
    asyncio.run(msg.view.revert.callback(interaction))
    assert api.config_patches == [
        ("discord_123_456", "clicker-user", {"clear_active_fallback": True})
    ]
