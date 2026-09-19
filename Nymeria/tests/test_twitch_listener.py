"""Twitch stream listener core (triggers/twitch_listener.py).

Plan: tmp/twitch-chatter-listener-plan.md, behaviors L1-L4. The core is
SDK-free: the audio source and transcriber are fakes, the clocks and the
backoff sleep are injected, so every window, gate decision, and fault-policy
step is observable without streamlink, PyAV, or an STT provider.
"""

import asyncio
import io
import wave
from array import array
from datetime import datetime, timedelta, timezone

import pytest

from nymeria.triggers.twitch_listener import (
    BYTES_PER_SECOND,
    SOURCE_BACKOFF_INITIAL,
    SOURCE_BACKOFF_MAX,
    STT_FAILURE_LIMIT,
    STT_PAUSE_SECONDS,
    ListenerUnavailable,
    StreamListener,
    clean_transcript,
    pcm_to_wav,
    voiced_fraction,
)

T0 = datetime(2026, 9, 19, 10, 0, 0, tzinfo=timezone.utc)


def _silence(seconds):
    return bytes(int(seconds * BYTES_PER_SECOND))


def _speech(seconds):
    """Loud 200 Hz square wave: every frame clears the energy floor."""
    samples = array("h")
    n = int(seconds * BYTES_PER_SECOND) // 2
    for i in range(n):
        samples.append(12000 if (i // 40) % 2 == 0 else -12000)
    return samples.tobytes()


def _chunks(pcm, size):
    return [pcm[i : i + size] for i in range(0, len(pcm), size)]


class _Source:
    """Scripted audio source: each open() replays one script entry.

    An entry is a list of chunks (yielded in order, then the stream ends),
    an Exception (raised on open), or ``"forever"`` (yields silence until
    cancelled).
    """

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.opens = 0

    def open(self):
        self.opens += 1
        script = self.scripts.pop(0) if self.scripts else []
        return self._iterate(script)

    async def _iterate(self, script):
        if isinstance(script, BaseException):
            raise script
        if script == "forever":
            while True:
                await asyncio.sleep(0)
                yield _silence(0.5)
        for chunk in script:
            await asyncio.sleep(0)
            yield chunk


class _Transcriber:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def transcribe(self, audio_bytes, filename, content_type="audio/wav"):
        self.calls.append((audio_bytes, filename, content_type))
        result = self.results.pop(0) if self.results else "words"
        if isinstance(result, BaseException):
            raise result
        return result


class _Clock:
    """Wall clock that advances by ``step`` per read; monotonic settable."""

    def __init__(self, step_seconds=12):
        self.wall = T0
        self.step = timedelta(seconds=step_seconds)
        self.mono = 1000.0

    def now(self):
        current = self.wall
        self.wall = self.wall + self.step
        return current

    def monotonic(self):
        return self.mono


def _listener(source, transcriber, window_seconds=5, clock=None, sleeps=None):
    clock = clock or _Clock()
    delivered = []

    async def on_transcript(text, start):
        delivered.append((text, start))

    async def fake_sleep(seconds):
        if sleeps is not None:
            sleeps.append(seconds)
        await asyncio.sleep(0)

    listener = StreamListener(
        source,
        transcriber,
        on_transcript,
        window_seconds=window_seconds,
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=fake_sleep,
    )
    return listener, delivered


async def _run_until_idle(listener, spins=200):
    """Drive the listener until its source script is exhausted and it sits
    in backoff (the scripted source ends after its chunks)."""
    listener.start()
    for _ in range(spins):
        await asyncio.sleep(0)
        if listener.state == "backoff" or listener.state == "error":
            break
    await listener.stop()


# ---------------------------------------------------------------------------
# L1: windowing, the energy gate, and the WAV handed to the transcriber
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_windows_are_cut_exactly_silence_is_gated_and_wav_is_valid():
    # 5 s speech, 5 s silence, then a 1 s tail (dropped: under half a window),
    # fed in chunk sizes that never align with a window boundary.
    pcm = _speech(5) + _silence(5) + _speech(1)
    source = _Source(_chunks(pcm, 7777))
    transcriber = _Transcriber("hello")
    listener, delivered = _listener(source, transcriber, window_seconds=5)

    await _run_until_idle(listener)

    assert len(transcriber.calls) == 1, "one voiced window, one STT call"
    wav_bytes, filename, content_type = transcriber.calls[0]
    assert (filename, content_type) == ("window.wav", "audio/wav")
    with wave.open(io.BytesIO(wav_bytes)) as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()) == (1, 2, 16000)
        assert wav.getnframes() == 5 * 16000
    assert listener.windows_seen == 2 and listener.windows_silent == 1
    assert delivered == [("hello", T0)]


def test_voiced_fraction_measures_speech_share():
    assert voiced_fraction(_silence(1)) == 0.0
    assert voiced_fraction(_speech(1)) == 1.0
    half = _speech(0.5) + _silence(0.5)
    assert abs(voiced_fraction(half) - 0.5) < 0.05
    assert voiced_fraction(b"") == 0.0


def test_pcm_to_wav_round_trips_the_samples():
    pcm = _speech(0.1)
    with wave.open(io.BytesIO(pcm_to_wav(pcm))) as wav:
        assert wav.readframes(wav.getnframes()) == pcm


# ---------------------------------------------------------------------------
# L2: transcripts carry the window START time; junk is dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcripts_are_stamped_with_window_start_and_cleaned():
    pcm = _speech(25)  # five 5 s windows
    source = _Source(_chunks(pcm, BYTES_PER_SECOND))
    transcriber = _Transcriber("  hello \n  there ", "Thank you.", "", "[Music]", "real  words")
    listener, delivered = _listener(source, transcriber, window_seconds=5)

    await _run_until_idle(listener)

    assert len(transcriber.calls) == 5
    assert [text for text, _ in delivered] == ["hello there", "real words"]
    starts = [start for _, start in delivered]
    assert starts[0] == T0
    assert starts[1] > starts[0]
    assert listener.last_transcript_at == starts[1]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  a   b  ", "a b"),
        ("Thanks for watching!", ""),
        ("THANK YOU.", ""),
        ("(laughs)", ""),
        ("[BLANK_AUDIO]", ""),
        ("you", ""),
        ("thank you for the raid", "thank you for the raid"),
        (None, ""),
    ],
)
def test_clean_transcript_drops_hallucinations_and_collapses_space(raw, expected):
    assert clean_transcript(raw) == expected


# ---------------------------------------------------------------------------
# L3: STT failures drop the window; a streak pauses STT, then resumes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stt_failure_streak_pauses_transcription_then_resumes():
    clock = _Clock()
    windows = STT_FAILURE_LIMIT + 4
    pcm = _speech(5 * windows)
    source = _Source(_chunks(pcm, BYTES_PER_SECOND * 5))
    failures = [RuntimeError("provider down") for _ in range(STT_FAILURE_LIMIT)]
    transcriber = _Transcriber(*failures, "back", "again", "more", "last")
    listener, delivered = _listener(source, transcriber, window_seconds=5, clock=clock)

    # Freeze monotonic time: the pause never elapses during this run.
    listener.start()
    for _ in range(400):
        await asyncio.sleep(0)
        if listener.state == "backoff":
            break
    assert len(transcriber.calls) == STT_FAILURE_LIMIT, "paused: no calls after the streak"
    assert listener.state == "backoff"  # source ended after the script
    assert delivered == []
    assert listener.stt_failures_total == STT_FAILURE_LIMIT
    await listener.stop()

    # After the pause elapses, transcription resumes on the next window.
    clock.mono += STT_PAUSE_SECONDS + 1
    source.scripts.append(_chunks(_speech(5), BYTES_PER_SECOND))
    listener.start()
    for _ in range(200):
        await asyncio.sleep(0)
        if delivered:
            break
    await listener.stop()
    assert [t for t, _ in delivered] == ["back"]


@pytest.mark.asyncio
async def test_a_single_stt_failure_drops_only_its_window():
    pcm = _speech(10)
    source = _Source(_chunks(pcm, BYTES_PER_SECOND))
    transcriber = _Transcriber(RuntimeError("blip"), "second window")
    listener, delivered = _listener(source, transcriber, window_seconds=5)

    await _run_until_idle(listener)

    assert [t for t, _ in delivered] == ["second window"]
    assert listener.state != "stt_paused"


# ---------------------------------------------------------------------------
# L4: source failures restart with doubling backoff; stop() is prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_failures_back_off_doubling_to_the_cap():
    sleeps = []
    source = _Source(*[RuntimeError(f"hls {i}") for i in range(6)], "forever")
    transcriber = _Transcriber()
    listener, _ = _listener(source, transcriber, sleeps=sleeps)

    listener.start()
    for _ in range(300):
        await asyncio.sleep(0)
        if source.opens >= 7 and listener.state == "live":
            break
    await listener.stop()

    assert sleeps == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0]
    assert sleeps[0] == SOURCE_BACKOFF_INITIAL and max(sleeps) == SOURCE_BACKOFF_MAX
    assert listener.state == "off"


@pytest.mark.asyncio
async def test_backoff_resets_after_a_completed_window():
    sleeps = []
    source = _Source(
        RuntimeError("first"),
        _chunks(_speech(5), BYTES_PER_SECOND),  # one full window, then the stream ends
        RuntimeError("third"),
        "forever",
    )
    listener, _ = _listener(source, _Transcriber(), sleeps=sleeps)

    listener.start()
    for _ in range(300):
        await asyncio.sleep(0)
        if source.opens >= 4 and listener.state == "live":
            break
    await listener.stop()

    # 5 (first failure), 5 (reset by the completed window, stream ended),
    # 10 (third failure doubles from the reset value).
    assert sleeps == [5.0, 5.0, 10.0]


class _SpeechForever:
    """Endless speech, so a running listener keeps producing transcripts."""

    def open(self):
        return self._iterate()

    async def _iterate(self):
        while True:
            await asyncio.sleep(0)
            yield _speech(1)


@pytest.mark.asyncio
async def test_stop_ends_the_loop_and_no_callback_fires_afterwards():
    transcriber = _Transcriber()
    listener, delivered = _listener(_SpeechForever(), transcriber)
    listener.start()
    assert listener.running and listener.state == "starting"  # honest before the first step
    for _ in range(200):
        await asyncio.sleep(0)
        if len(delivered) >= 2:
            break
    assert listener.running and listener.state == "live" and len(delivered) >= 2

    await listener.stop()
    assert not listener.running and listener.state == "off"
    before = (len(delivered), len(transcriber.calls))
    for _ in range(100):
        await asyncio.sleep(0)
    assert (len(delivered), len(transcriber.calls)) == before


@pytest.mark.asyncio
async def test_a_failing_transcript_callback_does_not_tear_the_source_down():
    pcm = _speech(10)
    source = _Source(_chunks(pcm, BYTES_PER_SECOND))
    transcriber = _Transcriber("one", "two")
    seen = []

    async def on_transcript(text, start):
        seen.append(text)
        raise RuntimeError("consumer bug")

    listener = StreamListener(source, transcriber, on_transcript, window_seconds=5,
                              now=_Clock().now, sleep=lambda s: asyncio.sleep(0))
    listener.start()
    for _ in range(200):
        await asyncio.sleep(0)
        if listener.state == "backoff":
            break
    await listener.stop()
    assert seen == ["one", "two"]  # both windows reached the consumer
    assert source.opens == 1  # the source was never reopened for the consumer's fault


@pytest.mark.asyncio
async def test_missing_dependencies_park_the_listener_in_error():
    source = _Source(ListenerUnavailable("needs nymeriaos[twitch]"))
    listener, _ = _listener(source, _Transcriber())

    listener.start()
    for _ in range(50):
        await asyncio.sleep(0)
    assert listener.state == "error" and "nymeriaos[twitch]" in (listener.error or "")
    assert not listener.running and source.opens == 1  # never retried
    await listener.stop()


# ---------------------------------------------------------------------------
# StreamlinkAudioSource: thread + queue hand-off, with the blocking parts stubbed
# ---------------------------------------------------------------------------

import threading  # noqa: E402

from nymeria.triggers.twitch_listener import StreamlinkAudioSource  # noqa: E402


class _FakeFd:
    def __init__(self):
        self.close_threads = []

    def close(self):
        self.close_threads.append(threading.current_thread().ident)


def _stub_source(monkeypatch, *, decode=None, open_error=None):
    monkeypatch.setattr("nymeria.triggers.twitch_listener._import_deps", lambda: (object(), object()))
    fd = _FakeFd()

    def fake_open(self, streamlink_mod):
        if open_error is not None:
            raise open_error
        return fd

    monkeypatch.setattr(StreamlinkAudioSource, "_open_stream", fake_open)
    if decode is not None:
        monkeypatch.setattr(StreamlinkAudioSource, "_decode", staticmethod(decode))
    return fd


@pytest.mark.asyncio
async def test_streamlink_source_relays_decoded_chunks_then_ends(monkeypatch):
    def decode(av, fd, stop, offer):
        for i in range(3):
            offer(bytes([i]) * 10)

    fd = _stub_source(monkeypatch, decode=decode)
    chunks = [c async for c in StreamlinkAudioSource("silk").open()]
    assert chunks == [b"\x00" * 10, b"\x01" * 10, b"\x02" * 10]
    for _ in range(20):
        await asyncio.sleep(0.01)
        if fd.close_threads:
            break
    assert fd.close_threads and threading.get_ident() not in fd.close_threads


@pytest.mark.asyncio
async def test_streamlink_source_surfaces_a_worker_failure_as_the_iterator_raising(monkeypatch):
    _stub_source(monkeypatch, open_error=RuntimeError("no streams for #silk (offline?)"))
    with pytest.raises(RuntimeError, match="no streams"):
        async for _ in StreamlinkAudioSource("silk").open():
            pass


@pytest.mark.asyncio
async def test_closing_the_iterator_stops_the_worker_and_closes_off_loop(monkeypatch):
    stopped = threading.Event()

    def decode(av, fd, stop, offer):
        while not stop.is_set():
            offer(b"\x00" * 10)
            stop.wait(0.005)
        stopped.set()

    fd = _stub_source(monkeypatch, decode=decode)
    iterator = StreamlinkAudioSource("silk").open()
    first = await iterator.__anext__()
    assert first == b"\x00" * 10
    await iterator.aclose()

    assert stopped.wait(2), "the worker never saw the stop"
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(fd.close_threads) >= 1:
            break
    # Closed from the worker and/or the closer thread, never the loop thread.
    assert fd.close_threads and threading.get_ident() not in fd.close_threads


# ---------------------------------------------------------------------------
# Install guidance, the PyAV file-object adapter, and the decode loop
# ---------------------------------------------------------------------------

import sys  # noqa: E402

import nymeria.triggers.twitch_listener as listener_module  # noqa: E402
from nymeria.triggers.twitch_listener import (  # noqa: E402
    WINDOW_SECONDS_MAX,
    WINDOW_SECONDS_MIN,
    _ReadableStream,
    check_available,
)


def test_missing_dependency_names_the_extra_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "av", None)  # `import av` now raises ImportError
    with pytest.raises(ListenerUnavailable, match=r"nymeriaos\[twitch\]"):
        check_available()


def test_window_seconds_are_clamped_to_the_documented_bounds():
    args = (_Source(), _Transcriber(), lambda text, start: None)
    assert StreamListener(*args, window_seconds=99)._window_seconds == WINDOW_SECONDS_MAX == 30
    assert StreamListener(*args, window_seconds=1)._window_seconds == WINDOW_SECONDS_MIN == 5
    assert StreamListener(*args, window_seconds=12)._window_seconds == 12


class _StreamIOLike:
    """streamlink's reader: has read() but io.IOBase's readable() (False)."""

    def __init__(self):
        self.reads = []
        self.closed = False

    def read(self, size=-1):
        self.reads.append(size)
        return b"ts"

    def readable(self):
        return False

    def close(self):
        self.closed = True
        raise OSError("already torn down")


def test_readable_stream_gives_pyav_the_face_it_checks():
    raw = _StreamIOLike()
    wrapped = _ReadableStream(raw)
    assert wrapped.readable() is True and wrapped.seekable() is False and wrapped.writable() is False
    assert wrapped.read(4) == b"ts" and raw.reads == [4]
    wrapped.close()  # the reader's own close error never escapes
    assert raw.closed


def test_decode_wraps_the_reader_slices_plane_padding_and_chunks(monkeypatch):
    """PyAV must see a readable() object; each resampled frame contributes
    exactly samples*2 bytes (planes carry alignment padding); PCM is
    offered in CHUNK_SECONDS pieces with the tail flushed at the end."""
    monkeypatch.setattr(listener_module, "CHUNK_SECONDS", 12 / listener_module.BYTES_PER_SECOND)  # 12-byte chunks
    seen = {}

    class Plane:
        def __init__(self, data):
            self._data = data

        def __bytes__(self):
            return self._data

    class OutFrame:
        def __init__(self, samples, payload):
            self.samples = samples
            self.planes = [Plane(payload)]

    class Resampler:
        def __init__(self, **kwargs):
            seen["resampler"] = kwargs

        def resample(self, frame):
            return [OutFrame(4, b"abcdefgh" + b"PADDING!")]  # 8 real bytes + padding

    class Container:
        def __init__(self, fileobj):
            seen["readable"] = fileobj.readable()
            seen["read"] = fileobj.read(2)
            self.closed = False

        def decode(self, audio=0):
            for _ in range(4):
                yield object()

        def close(self):
            self.closed = True

    class FakeAv:
        AudioResampler = Resampler

        @staticmethod
        def open(fileobj, mode="r"):
            seen["container"] = Container(fileobj)
            return seen["container"]

    offered = []
    StreamlinkAudioSource._decode(FakeAv, _StreamIOLike(), threading.Event(), offered.append)

    assert seen["readable"] is True and seen["read"] == b"ts"
    assert seen["resampler"] == {"format": "s16", "layout": "mono", "rate": 16000}
    # 4 frames x 8 real bytes = 32 bytes: two 12-byte chunks and an 8-byte tail.
    assert offered == [b"abcdefghabcd", b"efghabcdefgh", b"abcdefgh"]
    assert b"PADDING" not in b"".join(offered)
    assert seen["container"].closed
