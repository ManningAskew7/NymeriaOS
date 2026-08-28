"""Unit tests for the browser login-session registry.

Covers the plan's expected behaviors for the backend half of the human
login handoff: the frame buffer a viewer tails (B1), the structural
guarantee that no frame ever reaches the agent (B4/B7), the tab hold that
suspends the agent (B3), and the auto-teardown at the time limit (B6).
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from nymeria.core.browser_login_sessions import (
    LOGIN_SESSION_TTL_SECONDS,
    MAX_FRAMES_BUFFERED,
    REASON_ABORTED,
    REASON_COMPLETED,
    REASON_EXPIRED,
    STATE_ACTIVE,
    STATE_ENDED,
    BrowserLoginSessionRegistry,
    active_session_for_tab,
    get_browser_login_registry,
    new_login_session_id,
    reset_for_tests,
)

# What a screencast frame's base64 looks like to a test: a marker no other
# part of the payload could plausibly contain, so "did this leak" is a
# substring question with no false positives.
_SECRET_PIXELS = "PASSWORD-ON-SCREEN-JPEG-BYTES"


@pytest.fixture(autouse=True)
def fresh_registry():
    reset_for_tests()
    yield
    reset_for_tests()


def _frame(data: str = _SECRET_PIXELS) -> dict:
    return {"data": data, "metadata": {"deviceWidth": 1280, "deviceHeight": 720}}


async def _start(
    reg: BrowserLoginSessionRegistry,
    *,
    tab_id: int = 7,
    user_id: str = "u1",
    thread_id: str = "t1",
):
    return reg.start(
        session_id=new_login_session_id(),
        user_id=user_id,
        thread_id=thread_id,
        tab_id=tab_id,
        url="https://accounts.google.com/signin",
    )


# ---------- identity and lifecycle ----------


def test_new_login_session_id_is_prefixed_and_random() -> None:
    first, second = new_login_session_id(), new_login_session_id()
    assert first.startswith("blogin_")
    assert len(first) > len("blogin_") + 10
    assert first != second


def test_start_registers_an_active_session_with_a_deadline() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, future = await _start(reg)
        assert session.state == STATE_ACTIVE
        assert not future.done()
        # The countdown the viewer renders is real, not a placeholder.
        assert LOGIN_SESSION_TTL_SECONDS - 5 < session.seconds_remaining <= (
            LOGIN_SESSION_TTL_SECONDS
        )
        assert reg.get(session.session_id) is session

    asyncio.run(run())


def test_finish_completed_wakes_the_agent_with_a_success_outcome() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, future = await _start(reg)
        assert reg.finish(session.session_id, reason=REASON_COMPLETED) is True
        await asyncio.sleep(0)  # the wake is scheduled on the loop
        result = future.result()
        assert result["ok"] is True
        assert result["status"] == REASON_COMPLETED
        assert result["tab_id"] == 7
        assert session.state == STATE_ENDED
        assert session.end_reason == REASON_COMPLETED

    asyncio.run(run())


def test_second_finish_is_a_no_op_rather_than_a_second_result() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, future = await _start(reg)
        reg.finish(session.session_id, reason=REASON_COMPLETED)
        await asyncio.sleep(0)
        # A double "Done" click, or Done racing the expiry sweep.
        assert reg.finish(session.session_id, reason=REASON_EXPIRED) is False
        await asyncio.sleep(0)
        assert future.result()["status"] == REASON_COMPLETED

    asyncio.run(run())


def test_finish_carries_an_optional_detail_line() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, future = await _start(reg)
        reg.finish(
            session.session_id,
            reason="failed",
            detail="the extension could not start a screencast",
        )
        await asyncio.sleep(0)
        result = future.result()
        assert result["ok"] is False
        assert result["detail"] == "the extension could not start a screencast"

    asyncio.run(run())


# ---------- the tab hold that suspends the agent (B3) ----------


def test_active_for_tab_matches_only_the_holding_user_and_tab() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg, tab_id=7, user_id="u1")
        assert reg.active_for_tab("u1", 7) is session
        # Another user's tab 7 is not this session's tab.
        assert reg.active_for_tab("u2", 7) is None
        # Same user, a tab nobody is signing into.
        assert reg.active_for_tab("u1", 8) is None

    asyncio.run(run())


def test_a_finished_session_releases_its_tab() -> None:
    """The agent is un-suspended by the session ending, not by a timer."""

    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg, tab_id=7)
        assert reg.active_for_tab("u1", 7) is not None
        reg.finish(session.session_id, reason=REASON_COMPLETED)
        assert reg.active_for_tab("u1", 7) is None

    asyncio.run(run())


def test_module_shortcut_reads_the_process_singleton() -> None:
    async def run() -> None:
        reg = get_browser_login_registry()
        session, _ = await _start(reg, tab_id=3)
        assert active_session_for_tab("u1", 3) is session
        assert active_session_for_tab("u1", 4) is None

    asyncio.run(run())


def test_active_for_user_lists_only_that_users_live_sessions() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        mine, _ = await _start(reg, tab_id=1, user_id="u1")
        await _start(reg, tab_id=2, user_id="u2")
        ended, _ = await _start(reg, tab_id=3, user_id="u1")
        reg.finish(ended.session_id, reason=REASON_COMPLETED)
        assert [s.session_id for s in reg.active_for_user("u1")] == [mine.session_id]

    asyncio.run(run())


# ---------- frames never reach the agent (B4, B7) ----------


def test_the_agent_outcome_never_carries_frame_bytes() -> None:
    """The whole handoff exists so the password on screen stays out of the
    model's context. The outcome is the only thing the agent is handed, so
    nothing the screencast captured may appear anywhere inside it."""

    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, future = await _start(reg)
        for _ in range(3):
            session.append_frame(_frame())
        reg.finish(session.session_id, reason=REASON_COMPLETED)
        await asyncio.sleep(0)
        serialized = json.dumps(future.result())
        assert _SECRET_PIXELS not in serialized
        # The counters ARE reported: the agent may know frames flowed.
        assert future.result()["frames_received"] == 3

    asyncio.run(run())


def test_the_status_snapshot_never_carries_frame_bytes() -> None:
    """``snapshot`` is the shape shown in REST payloads and status lines, so
    it is the other surface a frame could leak through."""

    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg)
        session.append_frame(_frame())
        serialized = json.dumps(session.snapshot())
        assert _SECRET_PIXELS not in serialized
        assert json.loads(serialized)["has_frame"] is True

    asyncio.run(run())


def test_appending_a_frame_publishes_no_autonomous_event() -> None:
    """Frames ride their own endpoint, not the event bus: a fan-out would
    copy them to every subscriber (desktop, mobile, CLI, bots) and, worse,
    put them somewhere the agent's own stream can see."""
    from nymeria.core import event_bus as bus_mod

    published: list = []

    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg)
        session.append_frame(_frame())
        session.mark_ended(REASON_COMPLETED)

    original = bus_mod.publish_autonomous_event
    bus_mod.publish_autonomous_event = lambda **kw: published.append(kw)  # type: ignore[assignment]
    try:
        asyncio.run(run())
    finally:
        bus_mod.publish_autonomous_event = original  # type: ignore[assignment]
    assert published == []


# ---------- the frame buffer a viewer tails (B1) ----------


def test_frames_reach_a_reader_in_order_with_stamped_seqs() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg)
        for index in range(3):
            assert session.append_frame(_frame(f"frame-{index}")) == index + 1
        session.mark_ended(REASON_COMPLETED)
        seen = [json.loads(payload) async for payload in session.stream_frames()]
        assert [entry["seq"] for entry in seen] == [1, 2, 3]
        assert [entry["data"] for entry in seen] == ["frame-0", "frame-1", "frame-2"]

    asyncio.run(run())


def test_a_reader_resumes_from_its_cursor() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg)
        for index in range(3):
            session.append_frame(_frame(f"frame-{index}"))
        session.mark_ended(REASON_COMPLETED)
        seen = [json.loads(p) async for p in session.stream_frames(from_seq=2)]
        assert [entry["seq"] for entry in seen] == [3]

    asyncio.run(run())


def test_a_slow_reader_skips_evicted_frames_instead_of_failing() -> None:
    """Video semantics, and the deliberate difference from the turn-stream
    buffer this pattern comes from: a skipped frame loses nothing the newer
    frame does not already show, so overflow is a drop, not an error."""

    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg)
        overflow = MAX_FRAMES_BUFFERED + 3
        for index in range(overflow):
            session.append_frame(_frame(f"frame-{index}"))
        session.mark_ended(REASON_COMPLETED)
        seen = [json.loads(p) async for p in session.stream_frames()]
        assert len(seen) == MAX_FRAMES_BUFFERED
        # The newest frames survived; the oldest three were dropped.
        assert seen[-1]["data"] == f"frame-{overflow - 1}"
        assert seen[0]["seq"] == overflow - MAX_FRAMES_BUFFERED + 1
        assert session.frames_dropped == 3
        assert session.frames_received == overflow

    asyncio.run(run())


def test_a_live_reader_is_woken_by_a_new_frame() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg)
        stream = session.stream_frames()

        async def push() -> None:
            await asyncio.sleep(0.02)
            session.append_frame(_frame("live"))

        asyncio.create_task(push())
        payload = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
        assert json.loads(payload)["data"] == "live"
        await stream.aclose()

    asyncio.run(run())


def test_a_live_reader_ends_when_the_session_ends() -> None:
    """The viewer's stream closes on Done rather than hanging on its poll."""

    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg)

        async def stop() -> None:
            await asyncio.sleep(0.02)
            reg.finish(session.session_id, reason=REASON_COMPLETED)

        asyncio.create_task(stop())
        seen = []

        async def drain() -> None:
            async for payload in session.stream_frames():
                seen.append(payload)

        await asyncio.wait_for(drain(), timeout=2.0)
        assert seen == []

    asyncio.run(run())


def test_a_frame_arriving_after_the_end_is_refused() -> None:
    """The extension learns a backend-side ending this way: its next frame
    POST is rejected, which is its cue to stop the screencast."""

    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg)
        assert session.append_frame(_frame()) == 1
        reg.finish(session.session_id, reason=REASON_EXPIRED)
        assert session.append_frame(_frame()) == 0
        assert session.frames_received == 1

    asyncio.run(run())


# ---------- auto-teardown at the time limit (B6) ----------


def test_the_time_limit_ends_the_session_and_tells_the_agent() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, future = await _start(reg)
        # No operator action at all: only the clock moves.
        session.created_at = time.monotonic() - LOGIN_SESSION_TTL_SECONDS - 1
        reg._sweep_once()
        await asyncio.sleep(0)
        result = future.result()
        assert result["ok"] is False
        assert result["status"] == REASON_EXPIRED
        assert session.state == STATE_ENDED
        # And the tab is handed back to the agent.
        assert reg.active_for_tab("u1", 7) is None

    asyncio.run(run())


def test_a_session_inside_its_limit_survives_the_sweep() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, future = await _start(reg)
        reg._sweep_once()
        assert not future.done()
        assert session.state == STATE_ACTIVE
        assert reg.active_for_tab("u1", 7) is session

    asyncio.run(run())


def test_expiry_wakes_a_viewer_stream_too() -> None:
    """A viewer left open past the limit is disconnected, not left tailing a
    session the backend has already torn down."""

    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg)
        session.created_at = time.monotonic() - LOGIN_SESSION_TTL_SECONDS - 1

        async def sweep() -> None:
            await asyncio.sleep(0.02)
            reg._sweep_once()

        asyncio.create_task(sweep())

        async def drain() -> None:
            async for _ in session.stream_frames():
                pass

        await asyncio.wait_for(drain(), timeout=2.0)

    asyncio.run(run())


# ---------- thread abort ----------


def test_abort_thread_ends_only_its_own_threads_sessions() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        doomed, doomed_future = await _start(reg, tab_id=1, thread_id="t1")
        spared, spared_future = await _start(reg, tab_id=2, thread_id="t2")
        assert reg.abort_thread("t1") == 1
        await asyncio.sleep(0)
        assert doomed_future.result()["status"] == REASON_ABORTED
        assert doomed.state == STATE_ENDED
        assert not spared_future.done()
        assert spared.state == STATE_ACTIVE
        assert reg.active_for_tab("u1", 2) is spared

    asyncio.run(run())


def test_abort_thread_with_no_sessions_is_a_no_op() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        await _start(reg, thread_id="t1")
        assert reg.abort_thread("t-other") == 0

    asyncio.run(run())
