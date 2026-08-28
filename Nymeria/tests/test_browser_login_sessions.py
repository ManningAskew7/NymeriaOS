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
    LoginSessionConflictError,
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
        # One live session per user is registry-enforced, so u1's earlier
        # session ends before the live one starts.
        ended, _ = await _start(reg, tab_id=3, user_id="u1")
        reg.finish(ended.session_id, reason=REASON_COMPLETED)
        mine, _ = await _start(reg, tab_id=1, user_id="u1")
        await _start(reg, tab_id=2, user_id="u2")
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


def _capture_bus(published: list):
    """Context manager swapping the bus publisher for a recorder."""
    from contextlib import contextmanager

    from nymeria.core import event_bus as bus_mod

    @contextmanager
    def _cm():
        original = bus_mod.publish_autonomous_event
        bus_mod.publish_autonomous_event = lambda **kw: published.append(kw)  # type: ignore[assignment]
        try:
            yield
        finally:
            bus_mod.publish_autonomous_event = original  # type: ignore[assignment]

    return _cm()


def test_appending_a_frame_publishes_no_autonomous_event() -> None:
    """Frames ride their own endpoint, not the event bus: a fan-out would
    copy them to every subscriber (desktop, mobile, CLI, bots) and, worse,
    put them somewhere the agent's own stream can see."""
    published: list = []

    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg)
        session.append_frame(_frame())

    with _capture_bus(published):
        asyncio.run(run())
    assert published == []


def test_an_ending_announces_to_desktop_and_extension_exactly_once() -> None:
    """The end of a session is broadcast twice, deliberately: the desktop's
    ``browser_login_ended`` (a viewer that never attached, or a prompt chip,
    has no other signal) and a fire-and-forget ``login_session_stop`` command
    (a static page sends no frames, so the extension would otherwise never
    hear the ending and its screencast would outlive the session). A second
    ending publishes nothing: one session, one announce."""
    published: list = []

    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg)
        session.append_frame(_frame())
        assert session.mark_ended(REASON_COMPLETED) is True
        assert session.mark_ended(REASON_EXPIRED) is False

    with _capture_bus(published):
        asyncio.run(run())

    assert [event["event_type"] for event in published] == [
        "browser_login_ended",
        "browser_command",
    ]
    ended, stop = published
    assert ended["data"]["state"] == STATE_ENDED
    assert ended["data"]["end_reason"] == REASON_COMPLETED
    assert stop["data"]["command_type"] == "login_session_stop"
    assert stop["data"]["args"]["tab_id"] == 7
    assert stop["data"]["args"]["session_id"] == ended["data"]["session_id"]
    assert stop["data"]["command_id"].startswith("bcmd_")
    # Neither announce may carry what the screen showed.
    assert _SECRET_PIXELS not in json.dumps(published, default=str)


def test_every_ending_path_rides_the_same_announce() -> None:
    """finish(), the TTL sweep, and a thread abort all end through
    ``mark_ended``, so each announces exactly once; no path can end a
    session silently."""
    published: list = []

    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        finished, _ = await _start(reg, tab_id=1, user_id="u1", thread_id="t1")
        aborted, _ = await _start(reg, tab_id=2, user_id="u2", thread_id="t2")
        swept, _ = await _start(reg, tab_id=3, user_id="u3", thread_id="t3")
        reg.finish(finished.session_id, reason=REASON_COMPLETED)
        reg.abort_thread("t2")
        swept.created_at = time.monotonic() - (LOGIN_SESSION_TTL_SECONDS + 1)
        reg._sweep_once()

    with _capture_bus(published):
        asyncio.run(run())
    endings = [e for e in published if e["event_type"] == "browser_login_ended"]
    stops = [e for e in published if e["event_type"] == "browser_command"]
    assert {e["data"]["end_reason"] for e in endings} == {
        REASON_COMPLETED,
        REASON_ABORTED,
        REASON_EXPIRED,
    }
    assert len(endings) == 3 and len(stops) == 3


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
        spared, spared_future = await _start(
            reg, tab_id=2, user_id="u2", thread_id="t2"
        )
        assert reg.abort_thread("t1") == 1
        await asyncio.sleep(0)
        assert doomed_future.result()["status"] == REASON_ABORTED
        assert doomed.state == STATE_ENDED
        assert not spared_future.done()
        assert spared.state == STATE_ACTIVE
        assert reg.active_for_tab("u2", 2) is spared

    asyncio.run(run())


def test_abort_thread_with_no_sessions_is_a_no_op() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        await _start(reg, thread_id="t1")
        assert reg.abort_thread("t-other") == 0

    asyncio.run(run())


def test_a_thread_stop_cascades_into_live_login_sessions() -> None:
    """POST /threads/{id}/stop must not leave a human staring at a viewer
    whose agent has gone: the cancellation cascade ends the thread's login
    sessions. This pins the wiring in agent_callable_lifecycle, which is
    inside a try/except that would swallow a broken import silently."""
    import threading
    from types import SimpleNamespace

    from nymeria.core.agent_callable_lifecycle import abort_with_cascade

    class _Locks:
        def signal_abort(self, thread_id: str) -> None:
            pass

    agent = SimpleNamespace(
        _thread_locks=_Locks(),
        _invocations_lock=threading.Lock(),
        _active_callable_invocations={},
    )

    async def run() -> None:
        reg = get_browser_login_registry()
        session, future = reg.start(
            session_id=new_login_session_id(),
            user_id="u1",
            thread_id="t-stop",
            tab_id=7,
            url="https://example.com",
        )
        abort_with_cascade(agent, "t-stop")
        await asyncio.sleep(0)
        assert future.result()["status"] == REASON_ABORTED
        assert session.state == STATE_ENDED

    asyncio.run(run())


# ---------- outcomes outlive the popped record (the await tool's read) ----------


def test_an_outcome_survives_the_record_being_popped() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg)
        sid = session.session_id
        reg.finish(sid, reason=REASON_COMPLETED)
        await asyncio.sleep(0)
        # The record is gone (resolve pops)...
        assert reg.get(sid) is None
        # ...but the outcome is still readable by its owner, and only them.
        outcome = reg.outcome_for(sid, user_id="u1")
        assert outcome is not None and outcome["status"] == REASON_COMPLETED
        assert reg.outcome_for(sid, user_id="mallory") is None
        assert reg.outcome_for("blogin_never-existed", user_id="u1") is None

    asyncio.run(run())


def test_outcomes_age_out_and_are_capped(monkeypatch) -> None:
    import nymeria.core.browser_login_sessions as mod

    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        # Cap: with room for 2, the oldest of 3 outcomes is evicted.
        monkeypatch.setattr(mod, "_MAX_RECENT_OUTCOMES", 2)
        sids = []
        for tab in (1, 2, 3):
            session, _ = await _start(reg, tab_id=tab)
            sids.append(session.session_id)
            reg.finish(session.session_id, reason=REASON_COMPLETED)
            await asyncio.sleep(0)
        assert reg.outcome_for(sids[0], user_id="u1") is None
        assert reg.outcome_for(sids[2], user_id="u1") is not None
        # Age: past retention the survivor is gone too.
        monkeypatch.setattr(mod, "_OUTCOME_RETENTION_SECONDS", 0.0)
        assert reg.outcome_for(sids[2], user_id="u1") is None

    asyncio.run(run())


def test_await_outcome_returns_the_ending_now_or_later() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg)
        sid = session.session_id

        # Waiting while live: woken by the finish, not by polling.
        async def finish_soon() -> None:
            await asyncio.sleep(0.02)
            reg.finish(sid, reason=REASON_COMPLETED, detail="user clicked finish")

        waiter = asyncio.create_task(
            reg.await_outcome(sid, user_id="u1", timeout_seconds=5)
        )
        await finish_soon()
        status, outcome = await waiter
        assert status == "ended"
        assert outcome is not None
        assert outcome["ok"] is True and outcome["detail"] == "user clicked finish"

        # Awaiting again after the end reads the remembered outcome.
        status, again = await reg.await_outcome(sid, user_id="u1", timeout_seconds=1)
        assert status == "ended" and again is not None
        assert again["status"] == REASON_COMPLETED

    asyncio.run(run())


def test_await_outcome_timeout_reports_active_and_never_ends_the_login() -> None:
    """The wait's own timeout (or a cancelled awaiting tool) must never end
    or wound the session: the shield is the difference between "the agent
    stopped waiting" and "the user's login window died under them"."""

    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, future = await _start(reg)
        sid = session.session_id
        status, snapshot = await reg.await_outcome(
            sid, user_id="u1", timeout_seconds=0.01
        )
        assert status == "active"
        assert snapshot is not None and snapshot["state"] == STATE_ACTIVE
        assert snapshot["seconds_remaining"] > 0
        assert session.state == STATE_ACTIVE
        assert not future.cancelled()
        # The session is fully alive: the user finishing still lands.
        reg.finish(sid, reason=REASON_COMPLETED)
        status, outcome = await reg.await_outcome(sid, user_id="u1", timeout_seconds=1)
        assert status == "ended"
        assert outcome is not None and outcome["ok"] is True

    asyncio.run(run())


def test_await_outcome_is_scoped_to_the_owner() -> None:
    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        session, _ = await _start(reg, user_id="u1")
        status, payload = await reg.await_outcome(
            session.session_id, user_id="mallory", timeout_seconds=0.01
        )
        assert (status, payload) == ("unknown", None)
        status, _ = await reg.await_outcome(
            "blogin_nope", user_id="u1", timeout_seconds=0.01
        )
        assert status == "unknown"

    asyncio.run(run())


# ---------- one live session per user is registry-atomic ----------


def test_start_atomically_refuses_a_second_live_session_per_user() -> None:
    """The claim and the insert share one lock acquisition: a caller-side
    pre-check races the caller's own awaits (tab creation), so the registry
    is where one-per-user is actually enforced. The refusal names the
    winner; a different user is unaffected; an ended session frees the
    slot."""

    async def run() -> None:
        reg = BrowserLoginSessionRegistry()
        winner, _ = await _start(reg, tab_id=1, user_id="u1")
        with pytest.raises(LoginSessionConflictError) as caught:
            await _start(reg, tab_id=2, user_id="u1")
        assert caught.value.existing is winner
        # The loser registered nothing.
        assert [s.session_id for s in reg.active_for_user("u1")] == [
            winner.session_id
        ]
        # Another user's claim is independent.
        await _start(reg, tab_id=3, user_id="u2")
        # An ended session releases the slot.
        reg.finish(winner.session_id, reason=REASON_COMPLETED)
        again, _ = await _start(reg, tab_id=4, user_id="u1")
        assert reg.active_for_user("u1")[0] is again

    asyncio.run(run())


def test_an_abort_or_sweep_outcome_is_readable_the_moment_it_announces() -> None:
    """abort_thread and the TTL sweep pop the record BEFORE announcing, and
    the announce publishes synchronously (seconds, on a stalled Redis). The
    outcome must land in the retention store first, or a chrome_await_login
    racing the ending reads "unknown" about a login that just ended."""
    outcomes_at_announce: dict[str, object] = {}

    async def run() -> None:
        from nymeria.core import event_bus as bus_mod

        reg = BrowserLoginSessionRegistry()
        aborted, _ = await _start(reg, tab_id=1, user_id="u1", thread_id="t1")
        swept, _ = await _start(reg, tab_id=2, user_id="u2", thread_id="t2")

        original = bus_mod.publish_autonomous_event

        def probing_publish(**kw):
            if kw.get("event_type") == "browser_login_ended":
                sid = kw["data"]["session_id"]
                outcomes_at_announce[sid] = reg.outcome_for(
                    sid, user_id=kw["user_id"]
                )

        bus_mod.publish_autonomous_event = probing_publish  # type: ignore[assignment]
        try:
            reg.abort_thread("t1")
            swept.created_at = time.monotonic() - (LOGIN_SESSION_TTL_SECONDS + 1)
            reg._sweep_once()
        finally:
            bus_mod.publish_autonomous_event = original  # type: ignore[assignment]

        aborted_seen = outcomes_at_announce.get(aborted.session_id)
        swept_seen = outcomes_at_announce.get(swept.session_id)
        assert isinstance(aborted_seen, dict) and aborted_seen["status"] == REASON_ABORTED
        assert isinstance(swept_seen, dict) and swept_seen["status"] == REASON_EXPIRED

    asyncio.run(run())
