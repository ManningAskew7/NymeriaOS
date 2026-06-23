"""Voice services for TTS and STT."""

import asyncio
import io
import logging
import os
from typing import Any, Optional, Protocol, Tuple, cast

import httpx

from ..config import Settings

logger = logging.getLogger(__name__)

# Timeout: 60s for TTS/STT (model inference can take a while)
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Containers Telegram/Discord-style voice messages accept (sendVoice takes
# OGG/Opus, MP3, M4A). A voice_note=True synthesis must land in this set.
_VOICE_NOTE_FORMATS = {"mp3", "opus"}


class VoiceServiceError(Exception):
    """Raised when a voice service call fails."""


class SupportsSynthesize(Protocol):
    """Common TTS contract: every provider returns (audio_bytes, content_type).

    ``voice_note=True`` asks for a container that chat platforms accept as a
    voice message (OGG/Opus or MP3); providers that already emit MP3 ignore it.
    """

    async def synthesize(self, text: str, *, voice_note: bool = False) -> Tuple[bytes, str]: ...


class SupportsTranscribe(Protocol):
    """Common STT contract: audio bytes in, transcript string out."""

    async def transcribe(self, audio_bytes: bytes, filename: str,
                         content_type: str = "audio/wav") -> str: ...


async def _post_audio(
    url: str,
    *,
    status_error_label: str,
    connection_error_label: str,
    **post_kwargs: Any,
) -> httpx.Response:
    """POST to an audio HTTP endpoint, returning the response or raising.

    Owns the one-shot ``AsyncClient``, the ``post`` + ``raise_for_status``, and
    the HTTP-status / transport error translation to :class:`VoiceServiceError`
    shared by the OpenAI-compatible, Cartesia, and ElevenLabs TTS paths and the
    STT path. Callers build the URL/headers/payload, pass them as keyword
    arguments, and read ``.content`` / ``.json()`` off the returned response (the
    body is fully read before this returns, so reading it after the client closes
    is safe for these non-streaming requests). ``status_error_label`` and
    ``connection_error_label`` are the per-provider message prefixes, passed in
    verbatim so each ``VoiceServiceError`` string is unchanged.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.post(url, **post_kwargs)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            detail = e.response.text[:500] if e.response else str(e)
            raise VoiceServiceError(f"{status_error_label} ({e.response.status_code}): {detail}")
        except httpx.RequestError as e:
            raise VoiceServiceError(f"{connection_error_label}: {e}")


class TTSService:
    """Text-to-Speech via OpenAI-compatible /audio/speech endpoint."""

    def __init__(self, base_url: str, api_key: str, model: str, voice: str,
                 output_format: str = "mp3", speed: float = 1.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.output_format = output_format
        self.speed = speed

    async def synthesize(self, text: str, *, voice_note: bool = False) -> Tuple[bytes, str]:
        """Convert text to audio. Returns (audio_bytes, content_type)."""
        url = f"{self.base_url}/audio/speech"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        output_format = self.output_format
        if voice_note and output_format not in _VOICE_NOTE_FORMATS:
            # wav/flac/pcm/aac are rejected as voice messages; mp3 is the safe
            # request because every OpenAI-compatible server supports it
            # (speaches, notably, supports mp3/wav but NOT opus).
            output_format = "mp3"
        payload = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": output_format,
            "speed": self.speed,
        }

        format_to_mime = {
            "mp3": "audio/mpeg",
            "wav": "audio/wav",
            "opus": "audio/ogg",
            "aac": "audio/aac",
            "flac": "audio/flac",
            "pcm": "audio/pcm",
        }
        content_type = format_to_mime.get(output_format, "audio/mpeg")

        resp = await _post_audio(
            url,
            json=payload,
            headers=headers,
            status_error_label="TTS request failed",
            connection_error_label="TTS connection error",
        )
        logger.info(f"TTS synthesized {len(resp.content)} bytes ({output_format})")
        return resp.content, content_type


def _audio_to_mp3(audio_bytes: bytes, mime_type: str) -> bytes:
    """Convert audio bytes to MP3 using pydub/ffmpeg."""
    from pydub import AudioSegment

    mt = mime_type.lower().split(";")[0].strip()
    audio: Any
    if mt in ("audio/l16", "audio/pcm"):
        # Raw 16-bit linear PCM — parse rate/channels from mime params
        rate, channels = 24000, 1
        for param in mime_type.split(";")[1:]:
            k, _, v = param.strip().partition("=")
            if k.strip().lower() == "rate":
                rate = int(v.strip())
            elif k.strip().lower() == "channels":
                channels = int(v.strip())
        audio = AudioSegment(
            data=audio_bytes,
            sample_width=2,
            frame_rate=rate,
            channels=channels,
        )
    elif mt in ("audio/wav", "audio/x-wav"):
        audio = AudioSegment.from_wav(io.BytesIO(audio_bytes))
    else:
        audio = AudioSegment.from_file(io.BytesIO(audio_bytes))

    mp3_io = io.BytesIO()
    cast(Any, audio).export(mp3_io, format="mp3", bitrate="128k")
    return mp3_io.getvalue()


class GeminiTTSService:
    """Text-to-Speech via Google Gemini TTS API."""

    def __init__(self, api_key: str, model: str, voice: str):
        self.api_key = api_key
        self.model = model
        self.voice = voice

    def _generate(self, text: str) -> Tuple[bytes, str]:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.api_key)

        response = client.models.generate_content(
            model=self.model,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=self.voice,
                        )
                    )
                ),
            ),
        )

        if not response.candidates:
            raise VoiceServiceError("Gemini TTS returned no candidates")
        candidate_content = response.candidates[0].content
        if candidate_content is None or not candidate_content.parts:
            raise VoiceServiceError("Gemini TTS returned no content")
        audio_part = candidate_content.parts[0]
        if not audio_part.inline_data or not audio_part.inline_data.data:
            raise VoiceServiceError("Gemini TTS returned empty audio data")
        mime = audio_part.inline_data.mime_type or "audio/L16"
        logger.debug(f"Gemini TTS returned {len(audio_part.inline_data.data)} bytes, mime={mime}")
        return audio_part.inline_data.data, mime

    async def synthesize(self, text: str, *, voice_note: bool = False) -> Tuple[bytes, str]:
        """Convert text to MP3 audio. Returns (audio_bytes, content_type).

        Always MP3, which is voice-note compatible, so ``voice_note`` is moot.
        """
        try:
            raw_bytes, mime_type = await asyncio.to_thread(self._generate, text)
        except VoiceServiceError:
            raise
        except Exception as e:
            raise VoiceServiceError(f"Gemini TTS failed: {e}")

        try:
            # pydub blocks on an ffmpeg subprocess; keep it off the event loop.
            mp3_bytes = await asyncio.to_thread(_audio_to_mp3, raw_bytes, mime_type)
        except Exception as e:
            raise VoiceServiceError(f"Gemini TTS audio conversion failed (is ffmpeg installed?): {e}")
        logger.info(f"TTS synthesized {len(mp3_bytes)} bytes (mp3 from Gemini {mime_type})")
        return mp3_bytes, "audio/mpeg"


class CartesiaTTSService:
    """Text-to-Speech via Cartesia Sonic REST API — returns MP3 directly."""

    _API_URL = "https://api.cartesia.ai/tts/bytes"
    _API_VERSION = "2025-04-16"

    def __init__(self, api_key: str, model: str, voice: str, speed: float = 1.0):
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.speed = max(0.6, min(1.5, speed))

    async def synthesize(self, text: str, *, voice_note: bool = False) -> Tuple[bytes, str]:
        """Convert text to MP3 audio. Returns (audio_bytes, content_type).

        Always MP3, which is voice-note compatible, so ``voice_note`` is moot.
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Cartesia-Version": self._API_VERSION,
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model_id": self.model,
            "transcript": text,
            "voice": {"mode": "id", "id": self.voice},
            "language": "en",
            "output_format": {
                "container": "mp3",
                "sample_rate": 44100,
                "bit_rate": 128000,
            },
        }
        if self.speed != 1.0:
            payload["generation_config"] = {"speed": self.speed}

        resp = await _post_audio(
            self._API_URL,
            json=payload,
            headers=headers,
            status_error_label="Cartesia TTS failed",
            connection_error_label="Cartesia TTS connection error",
        )
        logger.info(f"TTS synthesized {len(resp.content)} bytes (mp3 via Cartesia)")
        return resp.content, "audio/mpeg"


class ElevenLabsTTSService:
    """Text-to-Speech via the ElevenLabs REST API.

    Emits Ogg/Opus for voice notes (native support) and MP3 otherwise.
    """

    _BASE_URL = "https://api.elevenlabs.io/v1"

    def __init__(self, api_key: str, model: str, voice: str, speed: float = 1.0):
        self.api_key = api_key
        self.model = model
        self.voice = voice
        # ElevenLabs voice_settings.speed accepts 0.7-1.2
        self.speed = max(0.7, min(1.2, speed))

    async def synthesize(self, text: str, *, voice_note: bool = False) -> Tuple[bytes, str]:
        """Convert text to audio. Returns (audio_bytes, content_type)."""
        url = f"{self._BASE_URL}/text-to-speech/{self.voice}"
        headers = {"xi-api-key": self.api_key, "Content-Type": "application/json"}
        params = {"output_format": "opus_48000_64" if voice_note else "mp3_44100_128"}
        payload: dict[str, Any] = {"text": text, "model_id": self.model}
        if self.speed != 1.0:
            payload["voice_settings"] = {"speed": self.speed}

        resp = await _post_audio(
            url,
            json=payload,
            params=params,
            headers=headers,
            status_error_label="ElevenLabs TTS failed",
            connection_error_label="ElevenLabs TTS connection error",
        )
        content_type = "audio/ogg" if voice_note else "audio/mpeg"
        logger.info(f"TTS synthesized {len(resp.content)} bytes ({params['output_format']} via ElevenLabs)")
        return resp.content, content_type


class EdgeTTSService:
    """Text-to-Speech via Microsoft Edge's neural voices (edge-tts package).

    Free and keyless, but an unofficial endpoint: useful as a zero-config
    default, not something to depend on contractually. Always returns MP3.
    """

    def __init__(self, voice: str, speed: float = 1.0):
        self.voice = voice
        # edge-tts expresses speed as a signed percentage rate offset.
        clamped = max(0.5, min(2.0, speed))
        self._rate = f"{int(round((clamped - 1.0) * 100)):+d}%"

    async def synthesize(self, text: str, *, voice_note: bool = False) -> Tuple[bytes, str]:
        """Convert text to MP3 audio. Returns (audio_bytes, content_type)."""
        try:
            import edge_tts  # pyrefly: ignore[missing-import]
        except ImportError:
            raise VoiceServiceError("Edge TTS selected but the edge-tts package is not installed (pip install edge-tts).")

        buf = io.BytesIO()
        try:
            communicate = edge_tts.Communicate(text, self.voice, rate=self._rate)
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    buf.write(chunk["data"])
        except Exception as e:
            raise VoiceServiceError(f"Edge TTS failed: {e}")

        audio = buf.getvalue()
        if not audio:
            raise VoiceServiceError("Edge TTS returned no audio")
        logger.info(f"TTS synthesized {len(audio)} bytes (mp3 via Edge)")
        return audio, "audio/mpeg"


class STTService:
    """Speech-to-Text via OpenAI-compatible /audio/transcriptions endpoint."""

    def __init__(self, base_url: str, api_key: str, model: str,
                 language: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.language = language

    async def transcribe(self, audio_bytes: bytes, filename: str,
                         content_type: str = "audio/wav") -> str:
        """Transcribe audio to text. Returns transcription string."""
        url = f"{self.base_url}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        files = {"file": (filename, audio_bytes, content_type)}
        data = {"model": self.model}
        if self.language:
            data["language"] = self.language

        resp = await _post_audio(
            url,
            files=files,
            data=data,
            headers=headers,
            status_error_label="STT request failed",
            connection_error_label="STT connection error",
        )
        result = resp.json()
        text = result.get("text", "").strip()
        logger.info(f"STT transcribed {len(audio_bytes)} bytes -> {len(text)} chars")
        return text


def _resolve_api_key(provider_key: Optional[str], settings: Settings,
                     *, keyless_ok: bool = False) -> str:
    """Resolve API key: provider-specific key, then the OpenAI key.

    Local sidecars (speaches, qwen3) ignore the bearer token, so ``keyless_ok``
    providers get a placeholder when no provider key is set; never forward the
    real OpenAI key to a local sidecar.
    """
    if provider_key:
        return provider_key
    if keyless_ok:
        return "local"
    if settings.openai_api_key:
        return settings.openai_api_key
    raise VoiceServiceError("No API key configured for voice service. Set TTS_API_KEY/STT_API_KEY or OPENAI_API_KEY.")


def _resolve_base_url(provider: str, explicit_url: Optional[str], defaults: dict) -> str:
    """Resolve base URL: use explicit URL or provider default."""
    if explicit_url:
        return explicit_url
    url = defaults.get(provider)
    if not url:
        raise VoiceServiceError(f"No base URL configured for provider '{provider}'.")
    return url


_TTS_URL_DEFAULTS = {
    "openai": "https://api.openai.com/v1",
    "qwen3": "http://localhost:8880/v1",
}

_STT_URL_DEFAULTS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "faster-whisper": "http://localhost:8003/v1",
}

# Per-provider model/voice defaults, applied when TTS_MODEL / TTS_VOICE /
# STT_MODEL are unset. The kokoro and faster-whisper entries are the speaches
# sidecar ids (HTTP path); the in-process path has its own defaults in
# voice_local.py. Cartesia has no default voice on purpose: its voices are
# account-scoped UUIDs from play.cartesia.ai.
_TTS_MODEL_DEFAULTS = {
    "openai": "gpt-4o-mini-tts",
    "kokoro": "speaches-ai/Kokoro-82M-v1.0-ONNX",
    "qwen3": "Qwen3-TTS-0.6B",
    "gemini": "gemini-3.1-flash-tts-preview",
    "cartesia": "sonic-3.5",
    "elevenlabs": "eleven_flash_v2_5",
}
_TTS_VOICE_DEFAULTS = {
    "openai": "nova",
    "kokoro": "af_heart",
    "qwen3": "default",
    "gemini": "Kore",
    "elevenlabs": "21m00Tcm4TlvDq8ikWAM",  # Rachel
    "edge": "en-US-AriaNeural",
}
_STT_MODEL_DEFAULTS = {
    "openai": "gpt-4o-mini-transcribe",
    "groq": "whisper-large-v3-turbo",
    "faster-whisper": "Systran/faster-whisper-small",
}


def get_tts_service(settings: Settings) -> SupportsSynthesize:
    """Build a TTS service from current settings. Raises VoiceServiceError if not configured."""
    provider = settings.tts_provider
    if provider == "none":
        raise VoiceServiceError("TTS is not configured. Set TTS_PROVIDER in settings.")

    model = settings.tts_model or _TTS_MODEL_DEFAULTS.get(provider, "")
    voice = settings.tts_voice or _TTS_VOICE_DEFAULTS.get(provider, "")

    if provider == "gemini":
        if not settings.gemini_api_key:
            raise VoiceServiceError("GEMINI_API_KEY is required for Gemini TTS provider.")
        return GeminiTTSService(
            api_key=settings.gemini_api_key,
            model=model,
            voice=voice,
        )

    if provider == "cartesia":
        if not settings.tts_api_key:
            raise VoiceServiceError("TTS_API_KEY is required for Cartesia TTS provider (set to your Cartesia API key).")
        if not voice:
            raise VoiceServiceError("TTS_VOICE is required for Cartesia (a voice UUID from play.cartesia.ai).")
        return CartesiaTTSService(
            api_key=settings.tts_api_key,
            model=model,
            voice=voice,
            speed=settings.tts_speed,
        )

    if provider == "elevenlabs":
        if not settings.tts_api_key:
            raise VoiceServiceError("TTS_API_KEY is required for ElevenLabs TTS (set to your ElevenLabs API key).")
        return ElevenLabsTTSService(
            api_key=settings.tts_api_key,
            model=model,
            voice=voice,
            speed=settings.tts_speed,
        )

    if provider == "edge":
        return EdgeTTSService(voice=voice, speed=settings.tts_speed)

    if provider == "kokoro" and not settings.tts_base_url:
        # No sidecar URL: run Kokoro in-process (nymeriaos[voice-local] extra).
        from .voice_local import LocalKokoroTTSService

        return LocalKokoroTTSService(
            voice=voice,
            speed=settings.tts_speed,
            models_dir=settings.data_dir / "voice",
        )

    # openai / qwen3 / kokoro-with-base-url: OpenAI-compatible HTTP.
    base_url = _resolve_base_url(provider, settings.tts_base_url, _TTS_URL_DEFAULTS)
    api_key = _resolve_api_key(
        settings.tts_api_key, settings, keyless_ok=provider in ("qwen3", "kokoro")
    )

    return TTSService(
        base_url=base_url,
        api_key=api_key,
        model=model,
        voice=voice,
        output_format=settings.tts_output_format,
        speed=settings.tts_speed,
    )


def get_stt_service(settings: Settings) -> SupportsTranscribe:
    """Build an STT service from current settings. Raises VoiceServiceError if not configured."""
    provider = settings.stt_provider
    if provider == "none":
        raise VoiceServiceError("STT is not configured. Set STT_PROVIDER in settings.")

    if provider == "faster-whisper" and not settings.stt_base_url:
        # No sidecar URL: run whisper in-process (nymeriaos[voice-local] extra).
        from .voice_local import LocalFasterWhisperSTTService

        return LocalFasterWhisperSTTService(
            model=settings.stt_model or "",
            language=settings.stt_language,
            download_root=settings.data_dir / "voice" / "whisper",
        )

    base_url = _resolve_base_url(provider, settings.stt_base_url, _STT_URL_DEFAULTS)
    if provider == "groq":
        # Prefer the shared Groq key (also used by the LLM registry); the env
        # fallback covers shells where only GROQ_API_KEY is exported.
        api_key = settings.stt_api_key or settings.groq_api_key or os.environ.get("GROQ_API_KEY") or ""
        if not api_key:
            raise VoiceServiceError("STT_API_KEY or GROQ_API_KEY is required for Groq STT.")
    else:
        api_key = _resolve_api_key(
            settings.stt_api_key, settings, keyless_ok=provider == "faster-whisper"
        )

    return STTService(
        base_url=base_url,
        api_key=api_key,
        model=settings.stt_model or _STT_MODEL_DEFAULTS.get(provider, ""),
        language=settings.stt_language,
    )
