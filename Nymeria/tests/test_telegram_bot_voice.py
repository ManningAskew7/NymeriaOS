"""Telegram voice-in/voice-out: inbound transcription and voice-note replies.

The bot is a thin client, so these tests fake the NymeriaAPIClient voice
methods and Telegram's bot object: no network, no STT/TTS provider needed.
Voice replies are best-effort by design: a missing TTS provider (503) or a
send failure must never degrade the already-delivered text reply.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from nymeria.triggers.telegram_bot import NymeriaTelegramBot


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://api/voice")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(f"HTTP {status_code}", request=request, response=response)


class _FakeSentMessage:
    def __init__(self, bot: "_FakeBot", text: str):
        self._bot = bot
        self.text = text

    async def edit_text(self, text: str = "", **kwargs) -> None:
        self.text = text
        self._bot.edits.append(text)

    async def edit_reply_markup(self, reply_markup=None) -> None:
        return None


class _FakeBot:
    def __init__(self, *, fail_send_voice: bool = False):
        self.id = 999
        self.sent_messages: list[str] = []
        self.edits: list[str] = []
        self.chat_actions: list[str] = []
        self.voice_sends: list[bytes] = []
        self.audio_sends: list[bytes] = []
        self.fail_send_voice = fail_send_voice
        self.file_bytes = b"ogg-opus-data"

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent_messages.append(text)
        return _FakeSentMessage(self, text)

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.chat_actions.append(str(action))

    async def send_voice(self, chat_id: int, voice: bytes, **kwargs) -> None:
        if self.fail_send_voice:
            raise RuntimeError("VOICE_MESSAGES_FORBIDDEN")
        self.voice_sends.append(voice)

    async def send_audio(self, chat_id: int, audio: bytes, **kwargs) -> None:
        self.audio_sends.append(audio)

    async def get_file(self, file_id: str):
        data = self.file_bytes

        class _File:
            async def download_as_bytearray(self) -> bytearray:
                return bytearray(data)

        return _File()


class _VoiceAPI:
    """chat_stream + voice methods, scripted."""

    def __init__(
        self,
        response_text: str = "Hello there.",
        *,
        synth_error: Exception | None = None,
        transcript: str = "what is the weather",
        transcribe_error: Exception | None = None,
        synth_content_type: str = "audio/ogg",
        extra_events: list[dict] | None = None,
    ):
        self.response_text = response_text
        self.synth_error = synth_error
        self.transcript = transcript
        self.transcribe_error = transcribe_error
        self.synth_content_type = synth_content_type
        self.extra_events = extra_events or []
        self.synth_calls: list[dict] = []
        self.transcribe_calls: list[dict] = []
        self.stream_kwargs: dict = {}

    async def chat_stream(self, *args, **kwargs):
        self.stream_kwargs = kwargs
        yield {"type": "response", "content": self.response_text}
        for event in self.extra_events:
            yield event
        yield {"type": "done"}

    async def synthesize_speech(self, text: str, voice_note: bool = False, user_id=None):
        self.synth_calls.append({"text": text, "voice_note": voice_note, "user_id": user_id})
        if self.synth_error is not None:
            raise self.synth_error
        return b"tts-bytes", self.synth_content_type

    async def transcribe_audio(self, audio_bytes, filename, content_type, user_id=None):
        self.transcribe_calls.append({
            "filename": filename, "content_type": content_type, "user_id": user_id,
        })
        if self.transcribe_error is not None:
            raise self.transcribe_error
        return self.transcript


def _run_stream(bot: NymeriaTelegramBot, fake_bot: _FakeBot, *, voice_reply: bool) -> None:
    asyncio.run(
        bot._stream_to_chat(
            chat_id=123,
            message="prompt",
            thread_id="telegram_123",
            user_id="user-1",
            context=SimpleNamespace(bot=fake_bot),
            voice_reply=voice_reply,
        )
    )


def test_voice_reply_sends_voice_note_and_trigger_override():
    api = _VoiceAPI("**Sunny** today, 22 degrees.")
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(api=api, bot_token="t")

    _run_stream(bot, fake_bot, voice_reply=True)

    assert fake_bot.voice_sends == [b"tts-bytes"]
    assert api.synth_calls[0]["voice_note"] is True
    assert api.synth_calls[0]["user_id"] == "user-1"
    # markdown is flattened before synthesis
    assert "**" not in api.synth_calls[0]["text"]
    assert "Sunny" in api.synth_calls[0]["text"]
    # the agent is told the reply will be spoken
    assert "voice" in (api.stream_kwargs.get("trigger_override") or "")
    # the text reply went out exactly once (progressive-edit path, no
    # plain-chunk fallback double-send)
    assert len(fake_bot.sent_messages) == 1


def test_error_event_suppresses_voice_reply():
    api = _VoiceAPI(
        "Let me check the weather for",
        extra_events=[{"type": "error", "content": "LLM provider returned 500"}],
    )
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(api=api, bot_token="t")

    _run_stream(bot, fake_bot, voice_reply=True)

    # A truncated non-answer must not be spoken after an error notice.
    assert api.synth_calls == []
    assert fake_bot.voice_sends == []
    assert any("error" in m.lower() for m in fake_bot.sent_messages)


def test_auth_prompt_is_never_spoken():
    api = _VoiceAPI(
        "Working on it.",
        extra_events=[{
            "type": "auth_prompt",
            "display_name": "GitHub",
            "connect_url": "https://nymeria.example/connect/abc123XYZ",
        }],
    )
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(api=api, bot_token="t")

    _run_stream(bot, fake_bot, voice_reply=True)

    # The one-time link goes out as its own text message...
    assert any("abc123XYZ" in m for m in fake_bot.sent_messages)
    # ...and never reaches the synthesized speech.
    assert len(api.synth_calls) == 1
    assert "abc123XYZ" not in api.synth_calls[0]["text"]
    assert "Working on it" in api.synth_calls[0]["text"]


def test_text_messages_never_trigger_synthesis():
    api = _VoiceAPI()
    bot = NymeriaTelegramBot(api=api, bot_token="t")

    _run_stream(bot, _FakeBot(), voice_reply=False)

    assert api.synth_calls == []
    assert api.stream_kwargs.get("trigger_override") is None


def test_voice_reply_silently_skips_when_tts_unconfigured():
    api = _VoiceAPI(synth_error=_http_status_error(503))
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(api=api, bot_token="t")

    _run_stream(bot, fake_bot, voice_reply=True)  # must not raise

    assert fake_bot.voice_sends == []
    assert fake_bot.audio_sends == []
    # the text reply still went out
    assert any("Hello there" in m for m in fake_bot.sent_messages)


def test_voice_send_falls_back_to_audio_file():
    api = _VoiceAPI()
    fake_bot = _FakeBot(fail_send_voice=True)
    bot = NymeriaTelegramBot(api=api, bot_token="t")

    _run_stream(bot, fake_bot, voice_reply=True)

    assert fake_bot.voice_sends == []
    assert fake_bot.audio_sends == [b"tts-bytes"]


def test_non_voice_mime_goes_straight_to_audio_file():
    api = _VoiceAPI(synth_content_type="audio/wav")
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(api=api, bot_token="t")

    _run_stream(bot, fake_bot, voice_reply=True)

    assert fake_bot.voice_sends == []
    assert fake_bot.audio_sends == [b"tts-bytes"]


# ── inbound transcription ───────────────────────────────────────────────────


def _voice_update(*, file_size: int = 2048, kind: str = "voice"):
    chat = SimpleNamespace(id=123, type="private")
    voice = audio = document = None
    if kind == "voice":
        voice = SimpleNamespace(file_id="f1", file_size=file_size, mime_type="audio/ogg")
    elif kind == "audio":
        audio = SimpleNamespace(
            file_id="f1", file_size=file_size, mime_type="audio/mpeg", file_name="memo.mp3",
        )
    elif kind == "document":
        document = SimpleNamespace(
            file_id="f1", file_size=file_size, mime_type="audio/x-m4a", file_name="memo.m4a",
        )
    message = SimpleNamespace(voice=voice, audio=audio, document=document, chat=chat)
    return SimpleNamespace(message=message)


def test_inbound_voice_note_is_transcribed():
    api = _VoiceAPI(transcript="remind me at five")
    fake_bot = _FakeBot()
    bot = NymeriaTelegramBot(api=api, bot_token="t")

    transcript, errors = asyncio.run(
        bot._transcribe_inbound_audio(
            _voice_update(), SimpleNamespace(bot=fake_bot), user_id="user-1",
        )
    )

    assert transcript == "remind me at five"
    assert errors == []
    assert api.transcribe_calls[0]["filename"] == "voice.ogg"
    assert api.transcribe_calls[0]["content_type"] == "audio/ogg"
    assert api.transcribe_calls[0]["user_id"] == "user-1"


def test_inbound_audio_file_uses_its_name_and_mime():
    api = _VoiceAPI(transcript="a song?")
    bot = NymeriaTelegramBot(api=api, bot_token="t")

    transcript, errors = asyncio.run(
        bot._transcribe_inbound_audio(
            _voice_update(kind="audio"), SimpleNamespace(bot=_FakeBot()), user_id="u",
        )
    )

    assert transcript == "a song?"
    assert api.transcribe_calls[0]["filename"] == "memo.mp3"
    assert api.transcribe_calls[0]["content_type"] == "audio/mpeg"


def test_inbound_audio_document_is_transcribed():
    """Forwarded/shared audio often arrives as a Document, not Audio."""
    api = _VoiceAPI(transcript="meeting notes")
    bot = NymeriaTelegramBot(api=api, bot_token="t")

    transcript, errors = asyncio.run(
        bot._transcribe_inbound_audio(
            _voice_update(kind="document"), SimpleNamespace(bot=_FakeBot()), user_id="u",
        )
    )

    assert transcript == "meeting notes"
    assert errors == []
    assert api.transcribe_calls[0]["filename"] == "memo.m4a"
    assert api.transcribe_calls[0]["content_type"] == "audio/x-m4a"


def test_inbound_voice_503_yields_friendly_setup_message():
    api = _VoiceAPI(transcribe_error=_http_status_error(503))
    bot = NymeriaTelegramBot(api=api, bot_token="t")

    transcript, errors = asyncio.run(
        bot._transcribe_inbound_audio(
            _voice_update(), SimpleNamespace(bot=_FakeBot()), user_id="u",
        )
    )

    assert transcript is None
    assert len(errors) == 1 and "speech-to-text" in errors[0]


def test_oversized_voice_note_is_rejected_before_download():
    api = _VoiceAPI()
    bot = NymeriaTelegramBot(api=api, bot_token="t")

    transcript, errors = asyncio.run(
        bot._transcribe_inbound_audio(
            _voice_update(file_size=bot.MAX_VOICE_BYTES + 1),
            SimpleNamespace(bot=_FakeBot()),
            user_id="u",
        )
    )

    assert transcript is None
    assert len(errors) == 1 and "too large" in errors[0]
    assert api.transcribe_calls == []


def test_empty_transcript_reports_no_speech():
    api = _VoiceAPI(transcript="   ")
    bot = NymeriaTelegramBot(api=api, bot_token="t")

    transcript, errors = asyncio.run(
        bot._transcribe_inbound_audio(
            _voice_update(), SimpleNamespace(bot=_FakeBot()), user_id="u",
        )
    )

    assert transcript is None
    assert len(errors) == 1 and "make out" in errors[0]


def test_text_only_message_returns_nothing():
    api = _VoiceAPI()
    bot = NymeriaTelegramBot(api=api, bot_token="t")
    update = SimpleNamespace(
        message=SimpleNamespace(
            voice=None, audio=None, document=None,
            chat=SimpleNamespace(id=123, type="private"),
        )
    )

    transcript, errors = asyncio.run(
        bot._transcribe_inbound_audio(update, SimpleNamespace(bot=_FakeBot()), user_id="u")
    )

    assert transcript is None and errors == []
    assert api.transcribe_calls == []


def test_non_audio_document_is_not_routed_to_stt():
    api = _VoiceAPI()
    bot = NymeriaTelegramBot(api=api, bot_token="t")
    update = SimpleNamespace(
        message=SimpleNamespace(
            voice=None, audio=None,
            document=SimpleNamespace(
                file_id="f1", file_size=2048,
                mime_type="application/pdf", file_name="doc.pdf",
            ),
            chat=SimpleNamespace(id=123, type="private"),
        )
    )

    transcript, errors = asyncio.run(
        bot._transcribe_inbound_audio(update, SimpleNamespace(bot=_FakeBot()), user_id="u")
    )

    assert transcript is None and errors == []
    assert api.transcribe_calls == []
