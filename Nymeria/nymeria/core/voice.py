"""Voice services for TTS and STT."""

import asyncio
import io
import logging
from typing import Any, Optional, Tuple, Union, cast

import httpx

from ..config import Settings

logger = logging.getLogger(__name__)

# Timeout: 60s for TTS/STT (model inference can take a while)
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class VoiceServiceError(Exception):
    """Raised when a voice service call fails."""


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

    async def synthesize(self, text: str) -> Tuple[bytes, str]:
        """Convert text to audio. Returns (audio_bytes, content_type)."""
        url = f"{self.base_url}/audio/speech"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {
            "model": self.model,
            "input": text,
            "voice": self.voice,
            "response_format": self.output_format,
            "speed": self.speed,
        }

        format_to_mime = {
            "mp3": "audio/mpeg",
            "wav": "audio/wav",
            "opus": "audio/opus",
            "aac": "audio/aac",
            "flac": "audio/flac",
            "pcm": "audio/pcm",
        }
        content_type = format_to_mime.get(self.output_format, "audio/mpeg")

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            try:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                logger.info(f"TTS synthesized {len(resp.content)} bytes ({self.output_format})")
                return resp.content, content_type
            except httpx.HTTPStatusError as e:
                detail = e.response.text[:500] if e.response else str(e)
                raise VoiceServiceError(f"TTS request failed ({e.response.status_code}): {detail}")
            except httpx.RequestError as e:
                raise VoiceServiceError(f"TTS connection error: {e}")


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

    async def synthesize(self, text: str) -> Tuple[bytes, str]:
        """Convert text to MP3 audio. Returns (audio_bytes, content_type)."""
        try:
            raw_bytes, mime_type = await asyncio.to_thread(self._generate, text)
        except VoiceServiceError:
            raise
        except Exception as e:
            raise VoiceServiceError(f"Gemini TTS failed: {e}")

        mp3_bytes = _audio_to_mp3(raw_bytes, mime_type)
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

    async def synthesize(self, text: str) -> Tuple[bytes, str]:
        """Convert text to MP3 audio. Returns (audio_bytes, content_type)."""
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

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            try:
                resp = await client.post(self._API_URL, json=payload, headers=headers)
                resp.raise_for_status()
                logger.info(f"TTS synthesized {len(resp.content)} bytes (mp3 via Cartesia)")
                return resp.content, "audio/mpeg"
            except httpx.HTTPStatusError as e:
                detail = e.response.text[:500] if e.response else str(e)
                raise VoiceServiceError(f"Cartesia TTS failed ({e.response.status_code}): {detail}")
            except httpx.RequestError as e:
                raise VoiceServiceError(f"Cartesia TTS connection error: {e}")


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

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            try:
                resp = await client.post(url, files=files, data=data, headers=headers)
                resp.raise_for_status()
                result = resp.json()
                text = result.get("text", "").strip()
                logger.info(f"STT transcribed {len(audio_bytes)} bytes -> {len(text)} chars")
                return text
            except httpx.HTTPStatusError as e:
                detail = e.response.text[:500] if e.response else str(e)
                raise VoiceServiceError(f"STT request failed ({e.response.status_code}): {detail}")
            except httpx.RequestError as e:
                raise VoiceServiceError(f"STT connection error: {e}")


def _resolve_api_key(provider_key: Optional[str], settings: Settings) -> str:
    """Resolve API key: use provider-specific key, fall back to OpenAI key."""
    if provider_key:
        return provider_key
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
    "faster-whisper": "http://localhost:8003/v1",
}


def get_tts_service(settings: Settings) -> Union[TTSService, GeminiTTSService, CartesiaTTSService]:
    """Build a TTS service from current settings. Raises VoiceServiceError if not configured."""
    if settings.tts_provider == "none":
        raise VoiceServiceError("TTS is not configured. Set TTS_PROVIDER in settings.")

    if settings.tts_provider == "gemini":
        if not settings.gemini_api_key:
            raise VoiceServiceError("GEMINI_API_KEY is required for Gemini TTS provider.")
        return GeminiTTSService(
            api_key=settings.gemini_api_key,
            model=settings.tts_model,
            voice=settings.tts_voice,
        )

    if settings.tts_provider == "cartesia":
        if not settings.tts_api_key:
            raise VoiceServiceError("TTS_API_KEY is required for Cartesia TTS provider (set to your Cartesia API key).")
        return CartesiaTTSService(
            api_key=settings.tts_api_key,
            model=settings.tts_model,
            voice=settings.tts_voice,
            speed=settings.tts_speed,
        )

    base_url = _resolve_base_url(settings.tts_provider, settings.tts_base_url, _TTS_URL_DEFAULTS)
    api_key = _resolve_api_key(settings.tts_api_key, settings)

    return TTSService(
        base_url=base_url,
        api_key=api_key,
        model=settings.tts_model,
        voice=settings.tts_voice,
        output_format=settings.tts_output_format,
        speed=settings.tts_speed,
    )


def get_stt_service(settings: Settings) -> STTService:
    """Build an STTService from current settings. Raises VoiceServiceError if not configured."""
    if settings.stt_provider == "none":
        raise VoiceServiceError("STT is not configured. Set STT_PROVIDER in settings.")

    base_url = _resolve_base_url(settings.stt_provider, settings.stt_base_url, _STT_URL_DEFAULTS)
    api_key = _resolve_api_key(settings.stt_api_key, settings)

    return STTService(
        base_url=base_url,
        api_key=api_key,
        model=settings.stt_model,
        language=settings.stt_language,
    )
