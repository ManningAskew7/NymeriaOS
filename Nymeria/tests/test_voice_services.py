"""Focused tests for core/voice.py, core/voice_local.py, and the bot voice helpers.

Covers the provider factories (branching, per-provider defaults, key
resolution), the voice_note format negotiation, the in-process engine
guard rails, and the API-client voice methods. HTTP synthesis goes through a
monkeypatched httpx.AsyncClient: no network, and no optional voice packages
are required to run these.
"""

from __future__ import annotations

import asyncio
import io
import sys
import wave
from typing import Any

import pytest

import nymeria.core.voice as voice
from nymeria.config.settings import Settings
from nymeria.core.voice import (
    CartesiaTTSService,
    EdgeTTSService,
    ElevenLabsTTSService,
    GeminiTTSService,
    STTService,
    TTSService,
    VoiceServiceError,
    get_stt_service,
    get_tts_service,
)


def make_settings(**kwargs: Any) -> Settings:
    """Settings isolated from the repo .env AND ambient process env.

    Earlier tests in the suite export the repo .env into os.environ, and
    pydantic-settings reads os.environ regardless of _env_file, so every
    voice-relevant field gets an explicit override here.
    """
    for field in (
        "openai_api_key", "gemini_api_key", "groq_api_key",
        "tts_api_key", "tts_base_url", "tts_model", "tts_voice",
        "stt_api_key", "stt_base_url", "stt_model", "stt_language",
    ):
        kwargs.setdefault(field, None)
    kwargs.setdefault("tts_speed", 1.0)
    kwargs.setdefault("tts_output_format", "mp3")
    return Settings(_env_file=None, **kwargs)


# ── factory branching and defaults ──────────────────────────────────────────


def test_tts_none_raises():
    with pytest.raises(VoiceServiceError, match="TTS is not configured"):
        get_tts_service(make_settings(tts_provider="none"))


def test_stt_none_raises():
    with pytest.raises(VoiceServiceError, match="STT is not configured"):
        get_stt_service(make_settings(stt_provider="none"))


def test_openai_tts_defaults():
    tts = get_tts_service(make_settings(tts_provider="openai", openai_api_key="sk-x"))
    assert isinstance(tts, TTSService)
    assert tts.model == "gpt-4o-mini-tts"
    assert tts.voice == "nova"
    assert tts.base_url == "https://api.openai.com/v1"


def test_explicit_model_and_voice_override_defaults():
    tts = get_tts_service(make_settings(
        tts_provider="openai", openai_api_key="sk-x",
        tts_model="tts-1-hd", tts_voice="onyx",
    ))
    assert isinstance(tts, TTSService)
    assert (tts.model, tts.voice) == ("tts-1-hd", "onyx")


def test_kokoro_without_base_url_is_in_process():
    from nymeria.core.voice_local import LocalKokoroTTSService

    tts = get_tts_service(make_settings(tts_provider="kokoro"))
    assert isinstance(tts, LocalKokoroTTSService)
    assert tts.voice == "af_heart"
    assert tts.models_dir.name == "voice"


def test_kokoro_with_base_url_is_http_and_keyless():
    tts = get_tts_service(make_settings(
        tts_provider="kokoro", tts_base_url="http://speaches:8000/v1",
    ))
    assert isinstance(tts, TTSService)
    assert tts.model == "speaches-ai/Kokoro-82M-v1.0-ONNX"
    assert tts.api_key == "local"


def test_qwen3_never_forwards_openai_key_to_local_sidecar():
    tts = get_tts_service(make_settings(tts_provider="qwen3", openai_api_key="sk-real"))
    assert isinstance(tts, TTSService)
    assert tts.api_key == "local"


def test_gemini_requires_its_key_and_gets_voice_default():
    with pytest.raises(VoiceServiceError, match="GEMINI_API_KEY"):
        get_tts_service(make_settings(tts_provider="gemini"))
    tts = get_tts_service(make_settings(tts_provider="gemini", gemini_api_key="g-x"))
    assert isinstance(tts, GeminiTTSService)
    assert tts.model == "gemini-3.1-flash-tts-preview"
    assert tts.voice == "Kore"


def test_cartesia_requires_key_and_voice():
    with pytest.raises(VoiceServiceError, match="TTS_API_KEY"):
        get_tts_service(make_settings(tts_provider="cartesia"))
    with pytest.raises(VoiceServiceError, match="TTS_VOICE"):
        get_tts_service(make_settings(tts_provider="cartesia", tts_api_key="c-x"))
    tts = get_tts_service(make_settings(
        tts_provider="cartesia", tts_api_key="c-x", tts_voice="some-uuid",
    ))
    assert isinstance(tts, CartesiaTTSService)
    assert tts.model == "sonic-3.5"


def test_elevenlabs_requires_key_and_gets_defaults():
    with pytest.raises(VoiceServiceError, match="ElevenLabs"):
        get_tts_service(make_settings(tts_provider="elevenlabs"))
    tts = get_tts_service(make_settings(tts_provider="elevenlabs", tts_api_key="xi-x"))
    assert isinstance(tts, ElevenLabsTTSService)
    assert tts.model == "eleven_flash_v2_5"
    assert tts.voice  # Rachel id


def test_edge_is_keyless_with_rate_mapping():
    tts = get_tts_service(make_settings(tts_provider="edge"))
    assert isinstance(tts, EdgeTTSService)
    assert tts.voice == "en-US-AriaNeural"
    assert tts._rate == "+0%"
    fast = get_tts_service(make_settings(tts_provider="edge", tts_speed=1.5))
    assert isinstance(fast, EdgeTTSService)
    assert fast._rate == "+50%"
    clamped = get_tts_service(make_settings(tts_provider="edge", tts_speed=4.0))
    assert isinstance(clamped, EdgeTTSService)
    assert clamped._rate == "+100%"  # edge speed clamps at 2.0


def test_openai_stt_defaults():
    stt = get_stt_service(make_settings(stt_provider="openai", openai_api_key="sk-x"))
    assert isinstance(stt, STTService)
    assert stt.model == "gpt-4o-mini-transcribe"


def test_groq_stt_key_resolution(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(VoiceServiceError, match="GROQ_API_KEY"):
        get_stt_service(make_settings(stt_provider="groq"))

    stt = get_stt_service(make_settings(stt_provider="groq", groq_api_key="gsk-1"))
    assert isinstance(stt, STTService)
    assert stt.api_key == "gsk-1"
    assert stt.base_url == "https://api.groq.com/openai/v1"
    assert stt.model == "whisper-large-v3-turbo"

    # stt_api_key beats the shared groq key; the env var is the last resort.
    stt2 = get_stt_service(make_settings(
        stt_provider="groq", stt_api_key="gsk-2", groq_api_key="gsk-1",
    ))
    assert isinstance(stt2, STTService)
    assert stt2.api_key == "gsk-2"
    monkeypatch.setenv("GROQ_API_KEY", "gsk-env")
    stt3 = get_stt_service(make_settings(stt_provider="groq"))
    assert isinstance(stt3, STTService)
    assert stt3.api_key == "gsk-env"


def test_faster_whisper_local_vs_http():
    from nymeria.core.voice_local import LocalFasterWhisperSTTService

    local = get_stt_service(make_settings(stt_provider="faster-whisper"))
    assert isinstance(local, LocalFasterWhisperSTTService)
    assert local.model == "small"

    http = get_stt_service(make_settings(
        stt_provider="faster-whisper", stt_base_url="http://speaches:8000/v1",
    ))
    assert isinstance(http, STTService)
    assert http.model == "Systran/faster-whisper-small"
    assert http.api_key == "local"


# ── HTTP services: voice_note format negotiation ────────────────────────────


class _FakeResponse:
    def __init__(self, content: bytes = b"fake-audio"):
        self.content = content

    def raise_for_status(self) -> None:
        pass


class _FakeAsyncClient:
    captured: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.captured.append({"url": url, **kwargs})
        return _FakeResponse()


@pytest.fixture
def fake_http(monkeypatch):
    _FakeAsyncClient.captured = []
    monkeypatch.setattr(voice.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient.captured


def test_tts_voice_note_upgrades_wav_to_mp3(fake_http):
    tts = TTSService("http://x/v1", "k", "m", "v", output_format="wav")
    audio, content_type = asyncio.run(tts.synthesize("hi", voice_note=True))
    assert content_type == "audio/mpeg"
    assert fake_http[0]["json"]["response_format"] == "mp3"
    # without voice_note the configured format is honored
    _, ct2 = asyncio.run(tts.synthesize("hi"))
    assert ct2 == "audio/wav"
    assert fake_http[1]["json"]["response_format"] == "wav"


def test_tts_opus_is_voice_note_safe_and_ogg_typed(fake_http):
    tts = TTSService("http://x/v1", "k", "m", "v", output_format="opus")
    _, content_type = asyncio.run(tts.synthesize("hi", voice_note=True))
    assert content_type == "audio/ogg"
    assert fake_http[0]["json"]["response_format"] == "opus"


def test_elevenlabs_formats_and_speed(fake_http):
    tts = ElevenLabsTTSService("xi-k", "eleven_flash_v2_5", "voice-id", speed=1.1)
    _, ct = asyncio.run(tts.synthesize("hi", voice_note=True))
    assert ct == "audio/ogg"
    req = fake_http[0]
    assert req["params"]["output_format"] == "opus_48000_64"
    assert req["headers"]["xi-api-key"] == "xi-k"
    assert req["json"]["voice_settings"] == {"speed": 1.1}
    _, ct2 = asyncio.run(tts.synthesize("hi"))
    assert ct2 == "audio/mpeg"
    assert fake_http[1]["params"]["output_format"] == "mp3_44100_128"


def test_stt_service_passes_multipart_fields(fake_http):
    async def run() -> str:
        stt = STTService("http://x/v1", "k", "whisper-1", language="en")
        return await stt.transcribe(b"bytes", "note.ogg", "audio/ogg")

    class _JsonResponse(_FakeResponse):
        def json(self) -> dict[str, str]:
            return {"text": " hello "}

    async def post(self, url, **kwargs):
        _FakeAsyncClient.captured.append({"url": url, **kwargs})
        return _JsonResponse()

    _FakeAsyncClient.post = post  # type: ignore[method-assign]
    try:
        text = asyncio.run(run())
    finally:
        del _FakeAsyncClient.post
    assert text == "hello"
    req = _FakeAsyncClient.captured[0]
    assert req["files"]["file"] == ("note.ogg", b"bytes", "audio/ogg")
    assert req["data"] == {"model": "whisper-1", "language": "en"}


# ── in-process engines: guard rails without the optional packages ───────────


def test_local_kokoro_missing_package_raises_install_hint(monkeypatch, tmp_path):
    from nymeria.core.voice_local import LocalKokoroTTSService

    monkeypatch.setitem(sys.modules, "kokoro_onnx", None)
    service = LocalKokoroTTSService("af_heart", 1.0, tmp_path)
    with pytest.raises(VoiceServiceError, match="voice-local"):
        asyncio.run(service.synthesize("hi"))


def test_local_whisper_missing_package_raises_install_hint(monkeypatch, tmp_path):
    from nymeria.core.voice_local import LocalFasterWhisperSTTService

    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    service = LocalFasterWhisperSTTService("small", None, tmp_path)
    with pytest.raises(VoiceServiceError, match="voice-local"):
        asyncio.run(service.transcribe(b"bytes", "a.ogg"))


def test_samples_to_wav_roundtrip():
    from nymeria.core.voice_local import _samples_to_wav

    samples = [0.0, 0.5, -0.5, 1.0, -1.0]
    wav_bytes = _samples_to_wav(samples, 24000)
    with wave.open(io.BytesIO(wav_bytes)) as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 24000
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == len(samples)


def test_edge_missing_package_raises_clean_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "edge_tts", None)
    service = EdgeTTSService("en-US-AriaNeural")
    with pytest.raises(VoiceServiceError, match="edge-tts"):
        asyncio.run(service.synthesize("hi"))


# ── bot voice helpers ────────────────────────────────────────────────────────


def test_strip_markdown_for_speech():
    from nymeria.triggers.voice_helpers import strip_markdown_for_speech

    text = (
        "# Title\n\n"
        "Here is **bold** and a [link](https://example.com) and `code`.\n\n"
        "```python\nprint('hidden')\n```\n"
        "- bullet one\n"
        "> a quote\n"
    )
    out = strip_markdown_for_speech(text)
    assert "Title" in out and "bold" in out and "link" in out and "code" in out
    assert "https://example.com" not in out
    assert "print" not in out and "(code omitted)" in out
    assert "**" not in out and "#" not in out and "`" not in out
    assert "bullet one" in out and "a quote" in out


def test_strip_markdown_for_speech_caps_length():
    from nymeria.triggers.voice_helpers import strip_markdown_for_speech

    out = strip_markdown_for_speech("word " * 2000, max_chars=100)
    assert len(out) <= 100
    assert out.endswith("…")
    # Spaceless text (CJK, long tokens) still respects the cap exactly.
    out2 = strip_markdown_for_speech("x" * 5000, max_chars=100)
    assert len(out2) == 100 and out2.endswith("…")


def test_strip_markdown_handles_unterminated_fence():
    from nymeria.triggers.voice_helpers import strip_markdown_for_speech

    out = strip_markdown_for_speech("Here you go:\n```python\nsecret = do_thing(x)\n")
    assert "secret" not in out and "(code omitted)" in out


def test_strip_markdown_drops_table_separator_rows():
    from nymeria.triggers.voice_helpers import strip_markdown_for_speech

    out = strip_markdown_for_speech("| Name | Qty |\n|------|-----|\n| apples | 3 |")
    assert "---" not in out and "-----" not in out
    assert "Name" in out and "apples" in out


def test_strip_markdown_preserves_identifiers_and_math():
    from nymeria.triggers.voice_helpers import strip_markdown_for_speech

    out = strip_markdown_for_speech(
        "Set max_voice_bytes and compute 2*5 or 3 * 4 via https://x.dev/my_page_name; *this* is _emphasis_."
    )
    assert "max_voice_bytes" in out
    assert "2*5" in out and "3 * 4" in out
    assert "my_page_name" in out
    assert "*this*" not in out and "this is emphasis" in out


def test_is_voice_message_mime():
    from nymeria.triggers.voice_helpers import is_voice_message_mime

    assert is_voice_message_mime("audio/ogg")
    assert is_voice_message_mime("audio/mpeg; charset=binary")
    assert not is_voice_message_mime("audio/wav")
    assert not is_voice_message_mime("")


# ── API client voice methods ─────────────────────────────────────────────────


def test_api_client_voice_methods():
    from nymeria.triggers.api_client import NymeriaAPIClient

    client = NymeriaAPIClient("http://api:8000", "token")
    calls: list[dict[str, Any]] = []

    class _RawResp:
        content = b"audio-bytes"
        headers = {"content-type": "audio/ogg"}

    async def fake_request(method: str, path: str, **kwargs: Any) -> Any:
        calls.append({"method": method, "path": path, **kwargs})
        if kwargs.get("raw"):
            return _RawResp()
        return {"text": "transcribed"}

    client._request = fake_request  # type: ignore[method-assign]

    text = asyncio.run(client.transcribe_audio(b"x", "v.ogg", "audio/ogg", user_id="u1"))
    assert text == "transcribed"
    assert calls[0]["path"] == "/voice/stt"
    assert calls[0]["files"]["audio"] == ("v.ogg", b"x", "audio/ogg")
    assert calls[0]["act_as"] == "u1"

    audio, content_type = asyncio.run(client.synthesize_speech("hello", voice_note=True, user_id="u1"))
    assert (audio, content_type) == (b"audio-bytes", "audio/ogg")
    assert calls[1]["path"] == "/voice/tts"
    assert calls[1]["json_body"] == {"text": "hello", "voice_note": True}
