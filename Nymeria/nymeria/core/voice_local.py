"""In-process local voice engines: Kokoro TTS and faster-whisper STT.

Used by ``core/voice.py`` when a local provider (``kokoro`` TTS,
``faster-whisper`` STT) is selected without a base URL: with a base URL the
provider is an OpenAI-compatible HTTP sidecar (e.g. speaches); without one the
engine runs inside the API process via the optional ``nymeriaos[voice-local]``
extra (``kokoro-onnx`` + ``faster-whisper``, both CPU-friendly).

Model weights download on first use into ``<data_dir>/voice/`` (Kokoro: ~325 MB
ONNX + ~28 MB voices from the kokoro-onnx GitHub release; whisper: from the
Hugging Face hub via faster-whisper). Loaded models are cached as process-wide
singletons behind a lock, so the memory cost (~500 MB for whisper ``small``
int8, ~350 MB for Kokoro) is paid once, on first request, not per call.

Verified on a 4 vCPU CPU-only host (2026-06-10): Kokoro synthesizes ~1s of
audio per second of wall clock; whisper ``small`` int8 transcribes a 4s clip in
~6s (``large-v3-turbo`` took 30s+ there, hence the small default).
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import wave
from pathlib import Path
from typing import Any, Optional, Tuple

from .voice import VoiceServiceError, _audio_to_mp3

logger = logging.getLogger(__name__)

KOKORO_MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
KOKORO_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)

# CPU-friendly defaults; both user-overridable (TTS_VOICE / STT_MODEL).
DEFAULT_KOKORO_VOICE = "af_heart"
DEFAULT_WHISPER_MODEL = "small"

INSTALL_HINT = (
    "Install the local voice extra: pip install 'nymeriaos[voice-local]' "
    "(or: pip install kokoro-onnx faster-whisper)."
)
_CONTAINER_INSTALL_HINT = (
    "This container image does not include the local voice engines. Run the "
    "speaches sidecar instead (docker compose --profile voice up -d) and set "
    "TTS_BASE_URL / STT_BASE_URL to http://speaches:8000/v1, or pick a hosted "
    "provider."
)


def _install_hint() -> str:
    """Container-aware install guidance: pip hints are dead ends in Docker."""
    if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists():
        return _CONTAINER_INSTALL_HINT
    return INSTALL_HINT

# One lock guards both caches: model loads are rare, heavy, and never overlap.
_load_lock = threading.Lock()
_kokoro_cache: dict[str, Any] = {}
_whisper_cache: dict[Tuple[str, str], Any] = {}


def local_tts_importable() -> bool:
    """True when the kokoro-onnx package is installed."""
    try:
        import kokoro_onnx  # noqa: F401  # pyrefly: ignore[missing-import]
        return True
    except ImportError:
        return False


def local_stt_importable() -> bool:
    """True when the faster-whisper package is installed."""
    try:
        import faster_whisper  # noqa: F401  # pyrefly: ignore[missing-import]
        return True
    except ImportError:
        return False


def _download_file(url: str, dest: Path) -> None:
    """Stream a model file to disk; atomic rename so a crash never half-installs."""
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    logger.info(f"Downloading {url} -> {dest} (one-time)")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(600.0, connect=30.0)) as resp:
            resp.raise_for_status()
            with open(part, "wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1024 * 1024):
                    fh.write(chunk)
        part.rename(dest)
    except Exception as e:
        part.unlink(missing_ok=True)
        raise VoiceServiceError(f"Failed to download Kokoro model file {url}: {e}")
    logger.info(f"Downloaded {dest.name} ({dest.stat().st_size // (1024 * 1024)} MB)")


def _samples_to_wav(samples: Any, sample_rate: int) -> bytes:
    """Encode float32 mono samples as 16-bit PCM WAV bytes (stdlib only)."""
    import numpy as np  # pyrefly: ignore[missing-import]

    pcm = np.clip(np.asarray(samples) * 32767.0, -32768, 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())
    return buf.getvalue()


def _wav_to_ogg_opus(wav_bytes: bytes) -> bytes:
    """Encode WAV bytes as Ogg/Opus via PyAV (bundled with faster-whisper).

    PyAV ships the ffmpeg libraries as a wheel, so this needs no system ffmpeg
    binary, unlike the pydub path in ``voice._audio_to_mp3``.
    """
    import av  # pyrefly: ignore[missing-import]

    out_buf = io.BytesIO()
    with av.open(io.BytesIO(wav_bytes)) as in_container:
        in_stream = in_container.streams.audio[0]
        with av.open(out_buf, "w", format="ogg") as out_container:
            out_stream = out_container.add_stream("libopus", rate=48000)
            out_stream.bit_rate = 64000
            resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
            for frame in in_container.decode(in_stream):
                for resampled in resampler.resample(frame):
                    for packet in out_stream.encode(resampled):
                        out_container.mux(packet)
            for resampled in resampler.resample(None):
                for packet in out_stream.encode(resampled):
                    out_container.mux(packet)
            for packet in out_stream.encode(None):
                out_container.mux(packet)
    return out_buf.getvalue()


class LocalKokoroTTSService:
    """Kokoro-82M TTS in-process via kokoro-onnx (CPU ONNX runtime).

    Returns WAV by default; ``voice_note=True`` re-encodes to Ogg/Opus (PyAV)
    or MP3 (pydub/ffmpeg fallback) because chat platforms reject WAV voice
    messages.
    """

    def __init__(self, voice: str, speed: float, models_dir: Path):
        self.voice = voice or DEFAULT_KOKORO_VOICE
        self.speed = max(0.5, min(2.0, speed))  # kokoro-onnx supported range
        self.models_dir = models_dir

    def _load(self) -> Any:
        try:
            from kokoro_onnx import Kokoro  # pyrefly: ignore[missing-import]
        except ImportError:
            raise VoiceServiceError(f"Kokoro TTS selected but kokoro-onnx is not installed. {_install_hint()}")

        key = str(self.models_dir)
        with _load_lock:
            instance = _kokoro_cache.get(key)
            if instance is not None:
                return instance
            model_path = self.models_dir / "kokoro-v1.0.onnx"
            voices_path = self.models_dir / "voices-v1.0.bin"
            if not model_path.is_file():
                _download_file(KOKORO_MODEL_URL, model_path)
            if not voices_path.is_file():
                _download_file(KOKORO_VOICES_URL, voices_path)
            try:
                instance = Kokoro(str(model_path), str(voices_path))
            except Exception as e:
                # A complete-but-corrupt download loads never, not once.
                raise VoiceServiceError(
                    f"Failed to load Kokoro model: {e}. If this persists, "
                    f"delete {self.models_dir} to force a fresh download."
                )
            # Single-slot cache: a changed models_dir must not strand the old
            # ~350 MB instance in memory.
            _kokoro_cache.clear()
            _kokoro_cache[key] = instance
            return instance

    def _synthesize_sync(self, text: str) -> Tuple[bytes, int]:
        kokoro = self._load()
        try:
            samples, sample_rate = kokoro.create(text, voice=self.voice, speed=self.speed, lang="en-us")
        except Exception as e:
            raise VoiceServiceError(f"Kokoro synthesis failed: {e}")
        if samples is None or len(samples) == 0:
            raise VoiceServiceError("Kokoro returned no audio")
        return _samples_to_wav(samples, sample_rate), sample_rate

    async def synthesize(self, text: str, *, voice_note: bool = False) -> Tuple[bytes, str]:
        """Convert text to audio. Returns (audio_bytes, content_type).

        WAV by default; ``voice_note=True`` re-encodes as Ogg/Opus via PyAV
        (no system ffmpeg needed), falling back to MP3 via pydub/ffmpeg.
        """
        wav_bytes, _ = await asyncio.to_thread(self._synthesize_sync, text)
        if voice_note:
            try:
                ogg = await asyncio.to_thread(_wav_to_ogg_opus, wav_bytes)
                logger.info(f"TTS synthesized {len(ogg)} bytes (ogg/opus via local Kokoro)")
                return ogg, "audio/ogg"
            except Exception as av_error:
                logger.debug(f"PyAV opus encode unavailable ({av_error}); trying pydub/ffmpeg")
            try:
                mp3 = await asyncio.to_thread(_audio_to_mp3, wav_bytes, "audio/wav")
            except Exception as e:
                raise VoiceServiceError(
                    "Kokoro produced audio but voice-note conversion failed. "
                    f"Install the av package or the ffmpeg system binary. ({e})"
                )
            logger.info(f"TTS synthesized {len(mp3)} bytes (mp3 via local Kokoro)")
            return mp3, "audio/mpeg"
        logger.info(f"TTS synthesized {len(wav_bytes)} bytes (wav via local Kokoro)")
        return wav_bytes, "audio/wav"


class LocalFasterWhisperSTTService:
    """Whisper STT in-process via faster-whisper (CTranslate2, int8 on CPU)."""

    def __init__(self, model: str, language: Optional[str], download_root: Path):
        self.model = model or DEFAULT_WHISPER_MODEL
        self.language = language
        self.download_root = download_root

    def _load(self) -> Any:
        try:
            from faster_whisper import WhisperModel  # pyrefly: ignore[missing-import]
        except ImportError:
            raise VoiceServiceError(
                f"faster-whisper STT selected but faster-whisper is not installed. {_install_hint()}"
            )

        key = (self.model, str(self.download_root))
        with _load_lock:
            instance = _whisper_cache.get(key)
            if instance is not None:
                return instance
            self.download_root.mkdir(parents=True, exist_ok=True)
            try:
                instance = WhisperModel(
                    self.model,
                    device="cpu",
                    compute_type="int8",
                    download_root=str(self.download_root),
                )
            except Exception as e:
                raise VoiceServiceError(f"Failed to load whisper model '{self.model}': {e}")
            # Single-slot cache: settings are hot-reloadable, and a model
            # switch (small -> medium) must release the old ~0.5 GB instance.
            _whisper_cache.clear()
            _whisper_cache[key] = instance
            return instance

    def _transcribe_sync(self, audio_bytes: bytes) -> str:
        model = self._load()
        try:
            # beam_size=1 halves latency on short voice notes; vad_filter drops
            # the silence segments whisper otherwise hallucinates text for.
            segments, _info = model.transcribe(
                io.BytesIO(audio_bytes),
                beam_size=1,
                language=self.language,
                vad_filter=True,
            )
            return " ".join(segment.text.strip() for segment in segments).strip()
        except VoiceServiceError:
            raise
        except Exception as e:
            raise VoiceServiceError(f"Local whisper transcription failed: {e}")

    async def transcribe(self, audio_bytes: bytes, filename: str,
                         content_type: str = "audio/wav") -> str:
        """Transcribe audio to text. Returns transcription string."""
        text = await asyncio.to_thread(self._transcribe_sync, audio_bytes)
        logger.info(f"STT transcribed {len(audio_bytes)} bytes -> {len(text)} chars (local whisper)")
        return text


__all__ = [
    "DEFAULT_KOKORO_VOICE",
    "DEFAULT_WHISPER_MODEL",
    "INSTALL_HINT",
    "LocalFasterWhisperSTTService",
    "LocalKokoroTTSService",
    "local_stt_importable",
    "local_tts_importable",
]
