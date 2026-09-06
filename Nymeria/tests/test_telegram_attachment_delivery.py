"""Outbound file delivery on the Telegram bot: a failed send is surfaced.

Background: two of five agent-authored notes on a production instance never
reached the user (Telegram ``TimedOut`` on ``send_document``) while the
agent's reply said "attached above". ``_send_file_attachment`` returned
False and every caller discarded it, so nobody was told. These tests pin
the contract: a failed send logs AND posts a one-line notice naming the
file into the chat; a refused download posts a notice WITHOUT the resend
hint (a resend fails identically); a successful send posts no notice.

Deliberately not covered: the ``send_photo`` branch (same try/except as
``send_document``, only the Telegram method differs) and the
no-bot-at-all ``RuntimeError`` branch (unreachable once the application
has started).
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest
from telegram.error import RetryAfter, TimedOut

from nymeria.triggers.telegram_bot import NymeriaTelegramBot


class _TelegramRecorder:
    """Stands in for ``application.bot``: records sends, fails on demand."""

    def __init__(self, document_error: Optional[Exception] = None):
        self.document_error = document_error
        self.documents: List[Dict[str, Any]] = []
        self.messages: List[Dict[str, Any]] = []
        self.message_attempts = 0

    async def send_document(self, *, chat_id: int, document, caption: str):
        if self.document_error is not None:
            raise self.document_error
        self.documents.append({"chat_id": chat_id, "caption": caption})

    async def send_photo(self, *, chat_id: int, photo, caption: str):
        self.documents.append({"chat_id": chat_id, "caption": caption})

    async def send_message(self, *, chat_id: int, text: str, **kwargs):
        self.message_attempts += 1
        self.messages.append({"chat_id": chat_id, "text": text})


def _bot(recorder: Optional[_TelegramRecorder], download) -> NymeriaTelegramBot:
    bot = NymeriaTelegramBot.__new__(NymeriaTelegramBot)
    bot._application = SimpleNamespace(bot=recorder) if recorder is not None else None
    bot.api = SimpleNamespace(download_workspace_file=download)
    return bot


async def _download_ok(path: str):
    # The API may name the file from content-disposition, not the path.
    return b"# note", "Note_to_Solicitor.md", "text/markdown"


async def _download_missing(path: str):
    return None


def test_timed_out_send_posts_failure_notice_naming_the_file():
    recorder = _TelegramRecorder(document_error=TimedOut())
    bot = _bot(recorder, _download_ok)

    ok = asyncio.run(bot._send_file_attachment(555, "/workspace/on-disk-name.md"))

    assert ok is False
    assert recorder.documents == []
    assert len(recorder.messages) == 1
    notice = recorder.messages[0]
    assert notice["chat_id"] == 555
    assert "Note_to_Solicitor.md" in notice["text"], "names the file as the send would have"
    assert "on-disk-name" not in notice["text"]
    assert "resend" in notice["text"]
    assert "\n" not in notice["text"].strip(), "notice must be a single line"


def test_rate_limited_send_sleeps_then_posts_notice(monkeypatch):
    slept: List[float] = []

    async def _fake_sleep(secs: float) -> None:
        slept.append(secs)

    monkeypatch.setattr("nymeria.triggers.telegram_bot.asyncio.sleep", _fake_sleep)
    recorder = _TelegramRecorder(document_error=RetryAfter(7))
    bot = _bot(recorder, _download_ok)

    ok = asyncio.run(bot._send_file_attachment(555, "/workspace/x.md"))

    assert ok is False
    assert slept == [7.0], "honours Telegram's retry_after before the notice"
    assert [m["chat_id"] for m in recorder.messages] == [555]
    assert "Note_to_Solicitor.md" in recorder.messages[0]["text"]


def test_download_refusal_posts_notice_without_resend_hint():
    recorder = _TelegramRecorder()
    bot = _bot(recorder, _download_missing)

    ok = asyncio.run(bot._send_file_attachment(555, "/workspace/huge.pdf"))

    assert ok is False
    assert recorder.documents == []
    assert [m["chat_id"] for m in recorder.messages] == [555]
    text = recorder.messages[0]["text"]
    assert "huge.pdf" in text
    assert "/workspace" not in text
    assert "resend" not in text, "a refused download fails the same way on resend"


def test_successful_send_posts_no_notice():
    recorder = _TelegramRecorder()
    bot = _bot(recorder, _download_ok)

    ok = asyncio.run(bot._send_file_attachment(555, "/workspace/Note_to_Solicitor.md"))

    assert ok is True
    assert [d["caption"] for d in recorder.documents] == ["Note_to_Solicitor.md"]
    assert recorder.messages == []


def test_interactive_context_bot_is_used_for_the_notice():
    """The interactive stream handler passes ``context``; the notice must go
    through that bot, not the (absent) application bot."""
    recorder = _TelegramRecorder(document_error=TimedOut())
    bot = _bot(None, _download_ok)

    ok = asyncio.run(
        bot._send_file_attachment(777, "/workspace/x.md", context=SimpleNamespace(bot=recorder))
    )

    assert ok is False
    assert [m["chat_id"] for m in recorder.messages] == [777]


def test_notice_send_failure_is_attempted_logged_and_swallowed(caplog):
    """The notice is best-effort: a chat that rejects it must not blow up
    the caller that already lost the attachment, but it must have been
    attempted and the swallow must be logged."""

    class _DeadChat(_TelegramRecorder):
        async def send_message(self, **kwargs):
            self.message_attempts += 1
            raise TimedOut()

    recorder = _DeadChat(document_error=TimedOut())
    bot = _bot(recorder, _download_ok)

    with caplog.at_level(logging.WARNING, logger="nymeria.triggers.telegram_bot"):
        ok = asyncio.run(bot._send_file_attachment(555, "/workspace/x.md"))

    assert ok is False
    assert recorder.message_attempts == 1
    assert any("attachment-failure notice" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("path", ["/workspace/a.md", "/workspace/sub/b.pdf"])
def test_notice_never_leaks_the_workspace_path(path):
    recorder = _TelegramRecorder(document_error=TimedOut())
    bot = _bot(recorder, _download_ok)

    asyncio.run(bot._send_file_attachment(1, path))

    assert "/workspace" not in recorder.messages[0]["text"]
