"""Live-stream listener for the Twitch bot: broadcast audio to ``[STREAM]`` lines.

The bot (``twitch_bot.py``) only ever sees chat. This module lets it hear the
broadcast too: it pulls Twitch's ``audio_only`` HLS rendition through
streamlink, decodes it with PyAV to 16 kHz mono PCM, cuts the PCM into fixed
windows, drops the silent ones, and sends each voiced window to the
platform's STT service (``core/voice.py::get_stt_service``). Every transcript
comes back to the bot through a callback stamped with the window's start
time, and the bot buffers it as a ``[STREAM]`` line in the same ring buffer
its chat lines live in, so the agent reads chat and speech as one timeline.

Two halves, deliberately split so the core is testable without either
dependency:

- ``StreamListener`` is the SDK-free core (windowing, the energy gate, WAV
  packaging, the hallucination filter, the fault policy). Its audio source is
  anything with an ``open()`` returning an async iterator of PCM chunks and
  its transcriber is any ``SupportsTranscribe``.
- ``StreamlinkAudioSource`` is the production source. streamlink and av are
  imported lazily inside it, so the bot process never loads them unless
  listening is on, and a lean install reports ``ListenerUnavailable`` naming
  the extra instead of failing at import time.

Timestamps: a window is stamped with the wall-clock time its first byte
arrived, which is when those words reached this process; the broadcast
itself runs several seconds behind the streamer's microphone (Twitch's own
delay plus HLS segmenting), so a ``[STREAM]`` line reads a little later than
the moment it was said. Chat reacting to a line therefore usually follows it
in the buffer, which is the order the agent wants.

Trust: a transcript is untrusted input exactly like chat. Anything audible
on the broadcast (game audio, a clip the streamer is watching, anyone in the
room) lands in it, so the prompt composers fence it as data and the bot
never lets a transcribed "instruction" confer authority.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
import logging
import re
import sys
import threading
import time
import wave
from array import array
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Protocol, Union

logger = logging.getLogger(__name__)

#: PCM format every audio source must deliver: 16 kHz, mono, signed 16-bit.
SAMPLE_RATE = 16_000
SAMPLE_WIDTH = 2
CHANNELS = 1
BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS

#: Energy gate: a window is transcribed only when at least VOICED_FRACTION_MIN
#: of its FRAME_MS frames have an RMS above ENERGY_FLOOR_RMS (int16 units;
#: 300 is roughly -41 dBFS, well under quiet speech and well over mic hiss).
#: Dead air, "be right back" screens and ad gaps never reach the provider.
FRAME_MS = 100
ENERGY_FLOOR_RMS = 300
VOICED_FRACTION_MIN = 0.15

#: Window length bounds (settings enforce the same; these guard direct use).
WINDOW_SECONDS_MIN = 5
WINDOW_SECONDS_MAX = 30
DEFAULT_WINDOW_SECONDS = 12

#: Fault policy. A failed audio source restarts after SOURCE_BACKOFF_INITIAL
#: seconds, doubling per failure to SOURCE_BACKOFF_MAX; a completed window
#: resets it. STT failures drop their window; STT_FAILURE_LIMIT consecutive
#: failures pause transcription for STT_PAUSE_SECONDS (a dead provider must
#: not burn a request per window).
SOURCE_BACKOFF_INITIAL = 5.0
SOURCE_BACKOFF_MAX = 60.0
STT_FAILURE_LIMIT = 5
STT_PAUSE_SECONDS = 60.0

#: Whisper-family models emit these on silence or music; a window whose whole
#: transcript is one of them (case-insensitive, trailing punctuation ignored)
#: is dropped. Kept small on purpose: every entry is a phrase a streamer can
#: also really say, and a false drop costs one window.
HALLUCINATION_PHRASES = frozenset(
    {
        "thank you",
        "thank you very much",
        "thanks for watching",
        "thank you for watching",
        "thanks for listening",
        "subtitles by the amara.org community",
        "like and subscribe",
        "please subscribe",
        "you",
        "bye",
        "music",
        "blank_audio",
        "silence",
    }
)

_WHITESPACE = re.compile(r"\s+")
#: A transcript that is nothing but bracketed sound tags: "[Music]", "(laughs)".
_ONLY_BRACKETED = re.compile(r"^[\[\(][^\]\)]*[\]\)]$")

TranscriptCallback = Callable[[str, datetime], Union[None, Awaitable[None]]]


class ListenerUnavailable(RuntimeError):
    """The listener's optional dependencies are not installed."""


class SupportsTranscribe(Protocol):
    async def transcribe(
        self, audio_bytes: bytes, filename: str, content_type: str = "audio/wav"
    ) -> str: ...


class AudioSource(Protocol):
    """A (re)openable source of PCM chunks in the module's sample format."""

    def open(self) -> AsyncIterator[bytes]: ...


# =============================================================================
# Pure helpers (no I/O)
# =============================================================================


def voiced_fraction(pcm: bytes) -> float:
    """Share of FRAME_MS frames whose RMS clears the energy floor.

    Pure Python over ``array('h')`` on purpose: numpy is not a declared
    dependency of the bot image, and a 12 s window is 192k samples, a few
    tens of milliseconds of arithmetic.
    """
    samples = array("h")
    usable = len(pcm) - (len(pcm) % SAMPLE_WIDTH)
    if usable <= 0:
        return 0.0
    samples.frombytes(pcm[:usable])
    frame = SAMPLE_RATE * FRAME_MS // 1000
    frames = len(samples) // frame
    if frames == 0:
        return 0.0
    floor_sq = ENERGY_FLOOR_RMS * ENERGY_FLOOR_RMS * frame
    voiced = 0
    for i in range(frames):
        seg = samples[i * frame : (i + 1) * frame]
        energy = 0
        for x in seg:
            energy += x * x
        if energy > floor_sq:
            voiced += 1
    return voiced / frames


def is_voiced(pcm: bytes) -> bool:
    return voiced_fraction(pcm) >= VOICED_FRACTION_MIN


def pcm_to_wav(pcm: bytes) -> bytes:
    """Wrap raw PCM as a WAV file (the container every STT provider accepts)."""
    if sys.byteorder == "big":  # pragma: no cover - WAV is little-endian
        swapped = array("h")
        swapped.frombytes(pcm[: len(pcm) - len(pcm) % SAMPLE_WIDTH])
        swapped.byteswap()
        pcm = swapped.tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm)
    return buf.getvalue()


def clean_transcript(text: Optional[str]) -> str:
    """Collapse whitespace and drop empty or hallucinated transcripts."""
    collapsed = _WHITESPACE.sub(" ", text or "").strip()
    if not collapsed:
        return ""
    if _ONLY_BRACKETED.match(collapsed):
        return ""
    key = collapsed.lower().strip(" .!?,♪")
    if key in HALLUCINATION_PHRASES:
        return ""
    return collapsed


# =============================================================================
# Listener core
# =============================================================================


class StreamListener:
    """Windows an audio source, gates silence, transcribes, and calls back.

    ``start()`` runs the loop as a background task; ``stop()`` cancels it and
    waits, after which no callback fires. ``state`` is one of ``off`` (not
    running), ``starting`` (opening the source), ``live`` (audio flowing),
    ``backoff`` (source failed, waiting to reopen), ``stt_paused`` (audio
    flowing but transcription paused after repeated provider failures), or
    ``error`` (the source is unavailable for good, dependencies missing).

    ``now``, ``monotonic`` and ``sleep`` are injectable for tests.
    """

    def __init__(
        self,
        audio_source: AudioSource,
        transcriber: SupportsTranscribe,
        on_transcript: TranscriptCallback,
        *,
        window_seconds: int = DEFAULT_WINDOW_SECONDS,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._source = audio_source
        self._transcriber = transcriber
        self._on_transcript = on_transcript
        self._window_seconds = max(WINDOW_SECONDS_MIN, min(WINDOW_SECONDS_MAX, int(window_seconds)))
        self._window_bytes = self._window_seconds * BYTES_PER_SECOND
        self._now = now
        self._monotonic = monotonic
        self._sleep = sleep

        self.state = "off"
        self.error: Optional[str] = None
        self.windows_seen = 0
        self.windows_transcribed = 0
        self.windows_silent = 0
        self.stt_failures_total = 0
        self.last_transcript_at: Optional[datetime] = None

        self._task: Optional[asyncio.Task] = None
        self._pending = bytearray()
        self._window_start: Optional[datetime] = None
        self._stt_failure_streak = 0
        self._stt_paused_until: Optional[float] = None

    # --- lifecycle ---------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self.error = None
        self._task = asyncio.create_task(self._run(), name="twitch-stream-listener")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # cancellation on stop is the expected outcome
            except Exception:
                logger.debug("Stream listener task ended with an error on stop", exc_info=True)
        self.state = "off"
        self._pending.clear()
        self._window_start = None

    async def _run(self) -> None:
        backoff = SOURCE_BACKOFF_INITIAL
        while True:
            self.state = "starting"
            failure: Optional[BaseException] = None
            try:
                async for chunk in self._source.open():
                    if self.state != "live" and self._stt_paused_until is None:
                        self.state = "live"
                    completed = await self._ingest(chunk)
                    if completed:
                        backoff = SOURCE_BACKOFF_INITIAL
                await self._flush_partial()
                logger.info("Stream audio ended; reopening in %.0fs", backoff)
            except asyncio.CancelledError:
                raise
            except ListenerUnavailable as e:
                self.state = "error"
                self.error = str(e)
                logger.error("Stream listener unavailable: %s", e)
                return
            except Exception as e:  # source or decode failure: reopen with backoff
                failure = e
                logger.warning("Stream audio source failed (%s); reopening in %.0fs", e, backoff)
            self._pending.clear()
            self._window_start = None
            self.state = "backoff"
            self.error = str(failure) if failure else None
            await self._sleep(backoff)
            backoff = min(backoff * 2, SOURCE_BACKOFF_MAX)

    # --- windowing ---------------------------------------------------------

    async def _ingest(self, chunk: bytes) -> bool:
        """Append a chunk; process every full window it completes.

        Returns True when at least one window completed (the source is
        healthy enough to reset its backoff).
        """
        if not chunk:
            return False
        if self._window_start is None:
            self._window_start = self._now()
        self._pending += chunk
        completed = False
        while len(self._pending) >= self._window_bytes:
            pcm = bytes(self._pending[: self._window_bytes])
            del self._pending[: self._window_bytes]
            start = self._window_start
            assert start is not None
            self._window_start = self._now() if self._pending else None
            await self._process_window(pcm, start)
            completed = True
        return completed

    async def _flush_partial(self) -> None:
        """Transcribe a partial window at stream end when it is long enough
        to carry words (half a window); a shorter tail is dropped."""
        if len(self._pending) >= self._window_bytes // 2 and self._window_start is not None:
            pcm = bytes(self._pending)
            start = self._window_start
            self._pending.clear()
            self._window_start = None
            await self._process_window(pcm, start)

    async def _process_window(self, pcm: bytes, start: datetime) -> None:
        self.windows_seen += 1
        if self._stt_paused_until is not None:
            if self._monotonic() < self._stt_paused_until:
                return
            self._stt_paused_until = None
            self.state = "live"
            logger.info("Stream transcription resumed after the pause")
        if not is_voiced(pcm):
            self.windows_silent += 1
            return
        try:
            raw = await self._transcriber.transcribe(pcm_to_wav(pcm), "window.wav", "audio/wav")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._note_stt_failure(e)
            return
        self._stt_failure_streak = 0
        text = clean_transcript(raw)
        if not text:
            return
        self.windows_transcribed += 1
        self.last_transcript_at = start
        try:
            result = self._on_transcript(text, start)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:
            # The consumer's problem, not the audio source's: never tear the
            # HLS session down for it.
            logger.error("Stream transcript callback failed", exc_info=True)

    def _note_stt_failure(self, error: BaseException) -> None:
        self._stt_failure_streak += 1
        self.stt_failures_total += 1
        logger.warning(
            "Stream transcription failed (%d in a row): %s", self._stt_failure_streak, error
        )
        if self._stt_failure_streak >= STT_FAILURE_LIMIT:
            self._stt_failure_streak = 0
            self._stt_paused_until = self._monotonic() + STT_PAUSE_SECONDS
            self.state = "stt_paused"
            self.error = f"transcription paused {STT_PAUSE_SECONDS:.0f}s after repeated failures: {error}"
            logger.error(
                "Stream transcription paused for %.0fs after %d consecutive failures",
                STT_PAUSE_SECONDS,
                STT_FAILURE_LIMIT,
            )


# =============================================================================
# Production audio source: streamlink + PyAV
# =============================================================================


def _import_deps() -> tuple[Any, Any]:
    """Import streamlink and av, or raise ListenerUnavailable naming the extra."""
    try:
        import av
        import streamlink
    except ImportError as e:
        raise ListenerUnavailable(
            "stream listening needs streamlink and av: pip install 'nymeriaos[twitch]'"
        ) from e
    return streamlink, av


def check_available() -> None:
    """Raise ListenerUnavailable when the production source cannot run."""
    _import_deps()


#: streamlink stream name for Twitch's audio-only rendition, plus the fallback
#: order when a channel does not offer it (rare; the lowest video rendition
#: still carries the audio track and PyAV decodes only that).
_PREFERRED_STREAMS = ("audio_only", "160p", "worst")

#: PCM handed to the loop in this many seconds per chunk; the queue holds
#: QUEUE_SECONDS of them before new audio is dropped (a stalled consumer must
#: not back the HLS reader up into a reconnect).
CHUNK_SECONDS = 0.5
QUEUE_SECONDS = 60


class _ReadableStream:
    """The file-object face PyAV wants over streamlink's stream reader.

    streamlink's ``StreamIO`` has ``read()`` but inherits ``io.IOBase``'s
    ``readable()`` (False), and PyAV refuses a file object whose
    ``readable()`` is False (measured 2026-09-19 against streamlink 8.6.1
    and av 18.1.0: "File object has no read() method, or readable()
    returned False"). Sequential, non-seekable, so PyAV probes the format
    from the byte stream itself.
    """

    def __init__(self, fd: Any):
        self._fd = fd

    def read(self, size: int = -1) -> bytes:
        return self._fd.read(size)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def writable(self) -> bool:
        return False

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._fd.close()


class StreamlinkAudioSource:
    """Twitch ``audio_only`` HLS through streamlink, decoded by PyAV.

    A worker thread owns the blocking pieces (streamlink's segment reader
    and the decoder) and hands PCM chunks to the event loop through a
    bounded queue; ``open()`` returns the async iterator that drains it.
    Closing the iterator stops the thread. streamlink's Twitch plugin is
    asked to filter ad segments (``disable-ads``), so an ad break shows up
    as a gap, never as an ad read in the transcript.
    """

    def __init__(self, channel: str):
        self._channel = channel.lower()
        self.dropped_chunks = 0

    @property
    def url(self) -> str:
        return f"https://www.twitch.tv/{self._channel}"

    def open(self) -> AsyncIterator[bytes]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        streamlink_mod, av = _import_deps()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=int(QUEUE_SECONDS / CHUNK_SECONDS))
        done = object()
        stop = threading.Event()
        holder: dict[str, Any] = {}

        def _offer(item: Any) -> None:
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                self.dropped_chunks += 1
                if self.dropped_chunks == 1 or self.dropped_chunks % 100 == 0:
                    logger.warning(
                        "Stream audio consumer is behind: %d chunk(s) dropped", self.dropped_chunks
                    )

        def _relay(item: Any) -> None:
            # The loop may already be closed at process shutdown.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_offer, item)

        def worker() -> None:
            try:
                fd = self._open_stream(streamlink_mod)
                holder["fd"] = fd
                try:
                    if not stop.is_set():
                        self._decode(av, fd, stop, _relay)
                finally:
                    with contextlib.suppress(Exception):
                        fd.close()
                _relay(done)
            except BaseException as e:  # noqa: BLE001 - relayed to the loop as the failure
                _relay(e)

        thread = threading.Thread(target=worker, name="twitch-listener-audio", daemon=True)
        thread.start()
        try:
            while True:
                item = await queue.get()
                if item is done:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            stop.set()
            fd = holder.get("fd")
            if fd is not None:
                # NEVER close on the loop thread: streamlink's close() joins
                # its segment threads (up to the 60 s stream timeout during a
                # stall), which would freeze EventSub keepalives and chat.
                threading.Thread(
                    target=self._close_quietly, args=(fd,), name="twitch-listener-close", daemon=True
                ).start()

    @staticmethod
    def _close_quietly(fd: Any) -> None:
        with contextlib.suppress(Exception):
            fd.close()

    def _open_stream(self, streamlink_mod: Any) -> Any:
        session = streamlink_mod.Streamlink()
        session.set_option("stream-timeout", 60)
        session.set_option("hls-live-edge", 2)
        _name, plugin_class, resolved_url = session.resolve_url(self.url)
        plugin = plugin_class(session, resolved_url, {"disable-ads": True, "low-latency": False})
        streams = plugin.streams()
        if not streams:
            raise RuntimeError(f"no streams for #{self._channel} (offline?)")
        for name in _PREFERRED_STREAMS:
            stream = streams.get(name)
            if stream is not None:
                logger.info("Stream audio source opened: #%s (%s)", self._channel, name)
                return stream.open()
        raise RuntimeError(
            f"no usable rendition for #{self._channel}: {', '.join(sorted(streams))}"
        )

    @staticmethod
    def _decode(av: Any, fd: Any, stop: threading.Event, offer: Callable[[bytes], None]) -> None:
        """Decode the MPEG-TS byte stream to 16 kHz mono s16 and offer chunks."""
        chunk_bytes = int(CHUNK_SECONDS * BYTES_PER_SECOND)
        pending = bytearray()
        container = av.open(_ReadableStream(fd), mode="r")
        try:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            for frame in container.decode(audio=0):
                if stop.is_set():
                    return
                for out in resampler.resample(frame):
                    # planes[0] may carry alignment padding past the samples.
                    pending += bytes(out.planes[0])[: out.samples * SAMPLE_WIDTH]
                while len(pending) >= chunk_bytes:
                    offer(bytes(pending[:chunk_bytes]))
                    del pending[:chunk_bytes]
            if pending:
                offer(bytes(pending))
        finally:
            with contextlib.suppress(Exception):
                container.close()
