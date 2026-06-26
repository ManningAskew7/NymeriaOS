"""Tests for consume_autonomous_firehose (the shared bot SSE firehose loop).

These exercise the reconnect/parse loop that discord_bot and telegram_bot share
(slice 21 F7). The bots' own delegation is locked separately by the
inspect.getsource guards in test_discord_sse_migration.py /
test_telegram_sse_migration.py; here we drive the helper directly against a fake
httpx client so the loop's behavior (parse routing, status backoff, stop
semantics, reconnect logging) is pinned.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from nymeria.triggers import sse_consumer
from nymeria.triggers.sse_consumer import consume_autonomous_firehose


class _FakeResponse:
    def __init__(self, status_code: int, lines: list[str]):
        self.status_code = status_code
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _RaisingStream:
    """A stream context manager whose entry raises (ConnectError/ReadTimeout)."""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *exc):
        return False


class _StreamCM:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    def __init__(self, connections: list):
        self._connections = connections
        self.requests: list[tuple] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None):
        self.requests.append((method, url, headers))
        conn = self._connections.pop(0)
        if isinstance(conn, Exception):
            return _RaisingStream(conn)
        return _StreamCM(conn)


def _install_fake_httpx(monkeypatch, connections: list) -> dict:
    """Point sse_consumer.httpx.AsyncClient at a fake replaying `connections`.

    Each while-loop iteration constructs a fresh AsyncClient, so the scripted
    connections are shared across the per-iteration clients via one list.
    """
    shared = list(connections)
    captured: dict = {"clients": []}

    def _factory(*args, **kwargs):
        client = _FakeAsyncClient(shared)
        captured["clients"].append(client)
        return client

    monkeypatch.setattr(sse_consumer.httpx, "AsyncClient", _factory)
    return captured


def _stop_on_sleep(monkeypatch) -> dict:
    """Replace asyncio.sleep with a no-op that records and flips a stop flag."""
    state = {"flag": False, "sleeps": []}

    async def _sleep(delay):
        state["sleeps"].append(delay)
        state["flag"] = True

    monkeypatch.setattr(sse_consumer.asyncio, "sleep", _sleep)
    return state


def _data(obj) -> str:
    return f"data: {json.dumps(obj)}"


def test_firehose_dispatches_events_and_skips_noise(monkeypatch, caplog):
    seen: list = []

    async def on_event(event):
        seen.append(event)

    lines = [
        _data({"type": "x", "n": 1}),
        ": keepalive comment frame",
        "",
        "data: :comment payload",
        _data({"type": "y"}),
    ]
    _install_fake_httpx(monkeypatch, [_FakeResponse(200, lines)])
    sleep_state = _stop_on_sleep(monkeypatch)

    with caplog.at_level(logging.INFO):
        asyncio.run(
            consume_autonomous_firehose(
                base_url="http://api.test",
                api_key="key",
                on_event=on_event,
                log_label="API",
                logger=logging.getLogger("test.firehose.api"),
                should_stop=lambda: sleep_state["flag"],
            )
        )

    # Only the two real data events are dispatched; comment/blank frames are
    # skipped via parse_sse_data_line.
    assert seen == [{"type": "x", "n": 1}, {"type": "y"}]
    assert "API SSE listener connecting to http://api.test/autonomous/stream" in caplog.text
    assert "API SSE connected, listening for events" in caplog.text
    assert "API SSE listener stopped" in caplog.text


def test_firehose_uses_admin_firehose_request(monkeypatch):
    async def on_event(event):
        pass

    captured = _install_fake_httpx(monkeypatch, [_FakeResponse(200, [])])
    sleep_state = _stop_on_sleep(monkeypatch)

    asyncio.run(
        consume_autonomous_firehose(
            base_url="http://api.test",
            api_key="secret-key",
            on_event=on_event,
            log_label="API",
            logger=logging.getLogger("test.firehose.req"),
            should_stop=lambda: sleep_state["flag"],
        )
    )

    method, url, headers = captured["clients"][0].requests[0]
    assert method == "GET"
    assert url == "http://api.test/autonomous/stream"
    assert headers == {"Authorization": "Bearer secret-key", "X-Nymeria-Act-As": "*"}


def test_firehose_backs_off_on_non_200(monkeypatch, caplog):
    calls: list = []

    async def on_event(event):
        calls.append(event)

    _install_fake_httpx(monkeypatch, [_FakeResponse(503, [])])
    sleep_state = _stop_on_sleep(monkeypatch)

    with caplog.at_level(logging.ERROR):
        asyncio.run(
            consume_autonomous_firehose(
                base_url="http://api.test",
                api_key="key",
                on_event=on_event,
                log_label="API",
                logger=logging.getLogger("test.firehose.status"),
                should_stop=lambda: sleep_state["flag"],
            )
        )

    assert calls == []
    assert "SSE connection failed: 503" in caplog.text
    assert sleep_state["sleeps"] == [3]  # one backoff sleep at the initial delay


def test_firehose_stop_mid_stream_skips_remaining_events(monkeypatch):
    seen: list = []

    async def on_event(event):
        seen.append(event)

    lines = [_data({"type": "first"}), _data({"type": "second"})]
    _install_fake_httpx(monkeypatch, [_FakeResponse(200, lines)])
    _stop_on_sleep(monkeypatch)

    # should_stop flips True once the first event has been dispatched, so the
    # mid-stream check returns before the second event.
    asyncio.run(
        consume_autonomous_firehose(
            base_url="http://api.test",
            api_key="key",
            on_event=on_event,
            log_label="API",
            logger=logging.getLogger("test.firehose.mid"),
            should_stop=lambda: len(seen) >= 1,
        )
    )

    assert seen == [{"type": "first"}]


def test_firehose_connect_error_backs_off_then_reconnects(monkeypatch, caplog):
    seen: list = []
    state = {"stop": False}

    async def on_event(event):
        seen.append(event)
        state["stop"] = True  # stop once the post-reconnect event arrives

    # Two failed connections, then a successful one delivering an event. Proves
    # the loop reconnects after errors and the 3->6 exponential backoff ramp.
    _install_fake_httpx(
        monkeypatch,
        [
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused again"),
            _FakeResponse(200, [_data({"type": "after_reconnect"})]),
        ],
    )
    sleeps: list = []

    async def _sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(sse_consumer.asyncio, "sleep", _sleep)

    with caplog.at_level(logging.WARNING):
        asyncio.run(
            consume_autonomous_firehose(
                base_url="http://api.test",
                api_key="key",
                on_event=on_event,
                log_label="API",
                logger=logging.getLogger("test.firehose.connect"),
                should_stop=lambda: state["stop"],
            )
        )

    # The event only arrives if the loop reconnected past both ConnectErrors.
    assert seen == [{"type": "after_reconnect"}]
    # Exponential backoff ramp across the two failures.
    assert sleeps == [3, 6]
    # The warning names the bare base_url, not the full /autonomous/stream URL.
    assert "Cannot reach API at http://api.test, retrying in 3s" in caplog.text
    assert "autonomous/stream, retrying" not in caplog.text


class _CancellingResponse(_FakeResponse):
    async def aiter_lines(self):
        yield f"data: {json.dumps({'type': 't'})}"
        raise asyncio.CancelledError()


def test_firehose_without_should_stop_loops_until_cancelled(monkeypatch, caplog):
    seen: list = []

    async def on_event(event):
        seen.append(event)

    # Telegram shape: should_stop=None -> while True (stopped() always False).
    # CancelledError raised inside the stream is caught by the loop's
    # `except asyncio.CancelledError: return`, the clean exit telegram relies on.
    _install_fake_httpx(monkeypatch, [_CancellingResponse(200, [])])

    async def _sleep(delay):
        raise AssertionError("trailing sleep must not run; cancelled mid-stream")

    monkeypatch.setattr(sse_consumer.asyncio, "sleep", _sleep)

    with caplog.at_level(logging.INFO):
        asyncio.run(
            consume_autonomous_firehose(
                base_url="http://api.test",
                api_key="key",
                on_event=on_event,
                log_label="Telegram",
                logger=logging.getLogger("test.firehose.tg"),
                should_stop=None,
            )
        )

    assert seen == [{"type": "t"}]
    # No terminal "stopped" log: the while-True loop never falls through.
    assert "Telegram SSE listener stopped" not in caplog.text
