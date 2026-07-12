"""Telegram emoji-reaction tests (backlog #45): inbound trigger (private-chat
scope, added-only, loop guard), outbound setMessageReaction execution with
standard-set validation, and reply suppression incl. the voice-reply skip."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from nymeria.core import bot_reactions
from nymeria.triggers.telegram_bot import NymeriaTelegramBot

BOT_ID = 999


@pytest.fixture(autouse=True)
def _clear_debounce():
    bot_reactions._recent_reaction_fires.clear()
    yield
    bot_reactions._recent_reaction_fires.clear()


def _reaction_bot(
    *, linked_user: Optional[str] = "u1"
) -> tuple[NymeriaTelegramBot, List[Dict[str, Any]]]:
    bot = NymeriaTelegramBot.__new__(NymeriaTelegramBot)
    bot._bindings = {}

    async def _resolve_user_id(telegram_user_id: int) -> Optional[str]:
        return linked_user

    dispatched: List[Dict[str, Any]] = []

    async def _stream_to_chat(**kwargs):
        dispatched.append(kwargs)

    bot.resolve_user_id = _resolve_user_id
    bot._stream_to_chat = _stream_to_chat
    return bot, dispatched


def _reaction_update(
    *,
    chat_type: str = "private",
    chat_id: int = 555,
    message_id: int = 42,
    user: Any = None,
    old: Optional[List[str]] = None,
    new: Optional[List[str]] = None,
) -> SimpleNamespace:
    if user is None:
        user = SimpleNamespace(id=5, is_bot=False, first_name="Alice")

    def _reactions(emojis: Optional[List[str]]):
        return [SimpleNamespace(emoji=e) for e in (emojis or [])]

    return SimpleNamespace(
        message_reaction=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type=chat_type),
            message_id=message_id,
            user=user,
            old_reaction=_reactions(old),
            new_reaction=_reactions(new if new is not None else ["👍"]),
        )
    )


def _context() -> SimpleNamespace:
    return SimpleNamespace(bot=SimpleNamespace(id=BOT_ID))


def _enable_toggle(monkeypatch, enabled: bool = True) -> None:
    monkeypatch.setattr(
        "nymeria.config.get_settings",
        lambda: SimpleNamespace(telegram_reaction_trigger_enabled=enabled),
    )


# ---------------------------------------------------------------------------
# Inbound trigger
# ---------------------------------------------------------------------------


def test_private_chat_reaction_fires_turn(monkeypatch):
    _enable_toggle(monkeypatch)
    bot, dispatched = _reaction_bot()

    asyncio.run(bot._on_message_reaction(_reaction_update(), _context()))

    assert len(dispatched) == 1
    call = dispatched[0]
    assert call["chat_id"] == 555
    assert call["thread_id"] == "telegram_555"
    assert call["user_id"] == "u1"
    assert call["is_self_invoke"] is True
    assert call["trigger_override"] == "reaction"
    assert call["source"] == "trigger"
    assert call["publish_autonomous_events"] is False
    assert call["platform_origin"] == {
        "platform": "telegram",
        "channel_id": "555",
        "message_id": "42",
        "kind": "reaction",
    }
    assert call["message"].startswith("[Reaction] Alice reacted with 👍")


def test_bound_chat_resolves_bound_thread(monkeypatch):
    _enable_toggle(monkeypatch)
    bot, dispatched = _reaction_bot()
    bot._bindings = {555: "desktop-thread-1"}

    asyncio.run(bot._on_message_reaction(_reaction_update(), _context()))
    assert dispatched[0]["thread_id"] == "desktop-thread-1"


def test_toggle_off_drops_reaction(monkeypatch):
    _enable_toggle(monkeypatch, enabled=False)
    bot, dispatched = _reaction_bot()

    asyncio.run(bot._on_message_reaction(_reaction_update(), _context()))
    assert dispatched == []


def test_group_chat_reaction_is_ignored(monkeypatch):
    _enable_toggle(monkeypatch)
    bot, dispatched = _reaction_bot()

    asyncio.run(
        bot._on_message_reaction(_reaction_update(chat_type="group"), _context())
    )
    assert dispatched == []


def test_loop_guard_drops_bot_reactors(monkeypatch):
    _enable_toggle(monkeypatch)
    bot, dispatched = _reaction_bot()

    self_user = SimpleNamespace(id=BOT_ID, is_bot=False, first_name="Nymeria")
    other_bot = SimpleNamespace(id=7, is_bot=True, first_name="OtherBot")
    asyncio.run(bot._on_message_reaction(_reaction_update(user=self_user), _context()))
    asyncio.run(bot._on_message_reaction(_reaction_update(user=other_bot), _context()))

    # An anonymous reaction (no user attached) also drops.
    anonymous = _reaction_update()
    anonymous.message_reaction.user = None
    asyncio.run(bot._on_message_reaction(anonymous, _context()))
    assert dispatched == []


def test_reaction_removal_does_not_fire(monkeypatch):
    _enable_toggle(monkeypatch)
    bot, dispatched = _reaction_bot()

    asyncio.run(
        bot._on_message_reaction(
            _reaction_update(old=["👍"], new=[]), _context()
        )
    )
    assert dispatched == []


def test_unlinked_reactor_dropped_silently(monkeypatch):
    _enable_toggle(monkeypatch)
    bot, dispatched = _reaction_bot(linked_user=None)

    asyncio.run(bot._on_message_reaction(_reaction_update(), _context()))
    assert dispatched == []


def test_repeat_reaction_is_debounced(monkeypatch):
    # Toggling 👍 off and on again within the TTL must not fire a second
    # full agent turn; a different emoji still fires.
    _enable_toggle(monkeypatch)
    bot, dispatched = _reaction_bot()

    asyncio.run(bot._on_message_reaction(_reaction_update(), _context()))
    asyncio.run(bot._on_message_reaction(_reaction_update(), _context()))
    assert len(dispatched) == 1

    asyncio.run(
        bot._on_message_reaction(_reaction_update(new=["❤"]), _context())
    )
    assert len(dispatched) == 2


def test_added_reaction_emojis_delta():
    added = NymeriaTelegramBot._added_reaction_emojis(
        SimpleNamespace(
            old_reaction=[SimpleNamespace(emoji="👍")],
            new_reaction=[SimpleNamespace(emoji="👍"), SimpleNamespace(emoji="❤")],
        )
    )
    assert added == ["❤"]

    custom = NymeriaTelegramBot._added_reaction_emojis(
        SimpleNamespace(
            old_reaction=[],
            new_reaction=[SimpleNamespace(emoji=None)],
        )
    )
    assert custom == ["a custom emoji"]


# ---------------------------------------------------------------------------
# Outbound reaction_request execution
# ---------------------------------------------------------------------------


class _ReactionRecorder:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    async def set_message_reaction(self, *, chat_id: int, message_id: int, reaction):
        self.calls.append(
            {"chat_id": chat_id, "message_id": message_id, "reaction": reaction}
        )


def _outbound_bot() -> tuple[NymeriaTelegramBot, _ReactionRecorder]:
    bot = NymeriaTelegramBot.__new__(NymeriaTelegramBot)
    recorder = _ReactionRecorder()
    bot._application = SimpleNamespace(bot=recorder)
    return bot, recorder


def test_reaction_request_sets_message_reaction():
    bot, recorder = _outbound_bot()

    asyncio.run(bot._on_reaction_request_event(555, {
        "platform": "telegram",
        "channel_id": "555",
        "message_id": "42",
        "emoji": "👍",
    }))
    assert recorder.calls == [
        {"chat_id": 555, "message_id": 42, "reaction": "👍"}
    ]


def test_reaction_request_rejects_nonstandard_emoji():
    bot, recorder = _outbound_bot()

    asyncio.run(bot._on_reaction_request_event(555, {
        "platform": "telegram",
        "channel_id": "555",
        "message_id": "42",
        "emoji": "x",
    }))
    assert recorder.calls == []


def test_reaction_request_routed_from_sse_event():
    bot, recorder = _outbound_bot()
    # _handle_sse_event needs the delivery cache attrs it touches en route.
    bot._bindings = {}
    bot._reverse_bindings = {}

    asyncio.run(bot._handle_sse_event({
        "type": "reaction_request",
        "thread_id": "telegram_555",
        "platform": "telegram",
        "channel_id": "555",
        "message_id": "42",
        "emoji": "🔥",
    }))
    assert recorder.calls and recorder.calls[0]["reaction"] == "🔥"

    # Other platforms fall through without touching the Telegram API.
    asyncio.run(bot._handle_sse_event({
        "type": "reaction_request",
        "thread_id": "telegram_555",
        "platform": "discord",
        "channel_id": "555",
        "message_id": "43",
        "emoji": "🔥",
    }))
    assert len(recorder.calls) == 1


# ---------------------------------------------------------------------------
# Reply suppression
# ---------------------------------------------------------------------------


class _HtmlSink:
    """Bot stub recording _send_html bubbles for the autonomous handler."""

    def __init__(self):
        self.sent: List[str] = []
        self._show_tool_calls: Dict[int, bool] = {}

    async def _send_html(self, chat_id: int, text: str, *args, **kwargs):
        self.sent.append(text)
        return SimpleNamespace(message_id=1)

    async def _send_file_attachment(self, chat_id: int, path: str, *args, **kwargs):
        return None


def test_autonomous_suppression_drops_reply_text():
    from nymeria.triggers.sse_consumer import dispatch_event

    sink = _HtmlSink()
    handler = NymeriaTelegramBot._AutonomousSSEHandler(sink, 555)

    async def _run():
        count = 0
        for event in [
            {"type": "tool_call", "id": "c1", "name": "react", "args": {}},
            {"type": "tool_result", "id": "c1", "result": "Reacted."},
            {"type": "reply_suppressed"},
            {"type": "response", "content": "Hidden reply."},
        ]:
            count = await dispatch_event(event, handler, count)
        await handler.flush_text(final=True)

    asyncio.run(_run())
    assert sink.sent == []
    assert handler._reply_suppressed is True


def test_interactive_suppression_skips_flush_and_voice():
    """_ChatSSEHandler drops buffered text and flags the voice-reply skip."""

    class _Bot:
        def __init__(self):
            self.sent: List[str] = []
            self._show_tool_calls: Dict[int, bool] = {}

        async def _send_html(self, chat_id, text, context=None, **kwargs):
            self.sent.append(text)
            return SimpleNamespace(message_id=1)

        async def _edit_html(self, message, text):
            return None

        def _create_stop_button_token(self, **kwargs):
            return "tok"

    async def _run():
        bot = _Bot()
        context = SimpleNamespace(
            bot=SimpleNamespace(
                send_chat_action=_noop_send_chat_action,
            )
        )
        handler = NymeriaTelegramBot._ChatSSEHandler(
            bot, 555, "telegram_555", "u1", 5, context
        )
        try:
            await handler.on_tool_call("react", {}, "c1", 1)
            await handler.on_reply_suppressed()
            await handler.on_response_chunk("Hidden reply.")
            await handler.on_done(1)
        finally:
            handler.cleanup()
        return bot, handler

    async def _noop_send_chat_action(**kwargs):
        return None

    bot, handler = asyncio.run(_run())
    assert bot.sent == []
    assert handler._reply_suppressed is True
    assert handler._text_buffer == ""
