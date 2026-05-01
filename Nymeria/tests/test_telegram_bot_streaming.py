"""Regression tests for Telegram streaming delivery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telegram.error import BadRequest

from nymeria.triggers.telegram_bot import NymeriaTelegramBot, TELEGRAM_TEXT_LIMIT


class _FakeMessage:
    def __init__(self, bot: "_FakeBot", text: str, reply_markup=None):
        self.bot = bot
        self.text = text
        self.reply_markup = reply_markup

    async def edit_text(self, text: str, **kwargs) -> None:
        self.bot._reject_if_too_long(text)
        self.text = text
        self.bot.edits.append(text)

    async def edit_reply_markup(self, reply_markup=None) -> None:
        self.reply_markup = reply_markup
        self.bot.reply_markup_edits += 1


class _FakeBot:
    def __init__(self):
        self.messages: list[_FakeMessage] = []
        self.edits: list[str] = []
        self.reply_markup_edits = 0

    def _reject_if_too_long(self, text: str) -> None:
        if len(text) > TELEGRAM_TEXT_LIMIT:
            raise BadRequest("Message is too long")

    async def send_message(self, chat_id: int, text: str, **kwargs) -> _FakeMessage:
        self._reject_if_too_long(text)
        msg = _FakeMessage(self, text, reply_markup=kwargs.get("reply_markup"))
        self.messages.append(msg)
        return msg

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        return None


class _FakeAPI:
    def __init__(self, text: str):
        self.text = text

    async def chat_stream(self, *args, **kwargs):
        yield {"type": "response", "content": self.text}
        yield {"type": "done"}


class _FakeEventAPI:
    def __init__(self, events: list[dict]):
        self.events = events

    async def chat_stream(self, *args, **kwargs):
        for event in self.events:
            yield event


def test_telegram_stream_splits_single_oversized_response_chunk():
    long_response = "A" * (TELEGRAM_TEXT_LIMIT * 2 + 211)
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(
        api=_FakeAPI(long_response),
        bot_token="test-token",
    )
    context = SimpleNamespace(bot=fake_bot)

    asyncio.run(
        bot._stream_to_chat(
            chat_id=123,
            message="prompt",
            thread_id="telegram_123",
            user_id="user-1",
            context=context,
        )
    )

    sent_text = "".join(msg.text for msg in fake_bot.messages)
    assert sent_text == long_response
    assert len(fake_bot.messages) >= 3
    assert all(len(msg.text) <= TELEGRAM_TEXT_LIMIT for msg in fake_bot.messages)
    assert fake_bot.messages[0].reply_markup is None
    assert fake_bot.reply_markup_edits == 1


def test_telegram_stream_surfaces_compaction_events():
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(
        api=_FakeEventAPI([
            {"type": "compacting", "message": "Compacting context..."},
            {
                "type": "compacted",
                "messages_removed": 7,
                "summary": "Prior task state and decisions.",
            },
            {"type": "response", "content": "Continuing now."},
            {"type": "done"},
        ]),
        bot_token="test-token",
    )
    context = SimpleNamespace(bot=fake_bot)

    asyncio.run(
        bot._stream_to_chat(
            chat_id=123,
            message="prompt",
            thread_id="telegram_123",
            user_id="user-1",
            context=context,
        )
    )

    texts = [msg.text for msg in fake_bot.messages]
    assert any("Compacting context" in text for text in texts)
    assert any("Context compacted" in text and "Prior task state" in text for text in texts)
    assert any("Continuing now." in text for text in texts)
