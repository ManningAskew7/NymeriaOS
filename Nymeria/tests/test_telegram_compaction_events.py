"""Regression tests for Telegram compaction stream events."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nymeria.triggers.telegram_bot import NymeriaTelegramBot


class _FakeMessage:
    def __init__(self, bot: "_FakeBot", text: str, reply_markup=None):
        self.bot = bot
        self.text = text
        self.reply_markup = reply_markup

    async def edit_text(self, text: str, **kwargs) -> None:
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

    async def send_message(self, chat_id: int, text: str, **kwargs) -> _FakeMessage:
        msg = _FakeMessage(self, text, reply_markup=kwargs.get("reply_markup"))
        self.messages.append(msg)
        return msg

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        return None


class _FakeAPI:
    def __init__(self, events: list[dict]):
        self.events = events

    async def chat_stream(self, *args, **kwargs):
        for event in self.events:
            yield event


def test_telegram_stream_surfaces_compaction_events():
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(
        api=_FakeAPI([
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
