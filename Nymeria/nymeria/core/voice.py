"""Voice services for TTS and STT via OpenAI-compatible APIs."""

import logging
from typing import Optional, Tuple

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


def get_tts_service(settings: Settings) -> TTSService:
    """Build a TTSService from current settings. Raises VoiceServiceError if not configured."""
    if settings.tts_provider == "none":
        raise VoiceServiceError("TTS is not configured. Set TTS_PROVIDER in settings.")

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
