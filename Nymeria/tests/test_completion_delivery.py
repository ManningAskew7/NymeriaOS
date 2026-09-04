"""Tests for the shared detach-and-deliver helper (core/completion_delivery.py).

The choreography background bash, detached Claude Code runs, and callable-ask
continuations share: route to a pending prompt when the origin thread is
busy, else fire an autonomous turn; drop on owner mismatch always and on the
abort flag only when the producer asks; bookend the turn.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from nymeria.core import completion_delivery as cd
from nymeria.core.activity_log import ActivityType
from nymeria.core.pending_prompt_queue import (
    notify_batch_absorbed,
    notify_batch_error,
    PendingPromptQueueClosingError,
    get_pending_queue,
    reset_pending_queue_for_tests,
)
from nymeria.core.thread_lock_manager import ThreadLockManager


class _Repo:
    def __init__(self, owner, *, raise_on_lookup: bool = False):
        self.owner = owner
        self.raise_on_lookup = raise_on_lookup

    def get_thread_owner(self, thread_id: str):
        if self.raise_on_lookup:
            raise RuntimeError("db down")
        return self.owner


def _agent(locks=None, owner="u1", *, raise_on_lookup=False):
    return SimpleNamespace(
        _thread_locks=locks or ThreadLockManager(),
        accounts_repo=_Repo(owner, raise_on_lookup=raise_on_lookup),
    )


def _delivery(**overrides) -> cd.CompletionDelivery:
    base = dict(
        thread_id="t1",
        user_id="u1",
        prompt_text="[Result] the thing finished",
        source="callable_result",
        source_id="cont-1",
        source_label="Helper result",
        task_id="callable-result-cont-1",
        label="Callable result",
        drop_on_abort=False,
    )
    base.update(overrides)
    return cd.CompletionDelivery(**base)


@pytest.fixture(autouse=True)
def _fresh_queue():
    reset_pending_queue_for_tests()
    yield
    reset_pending_queue_for_tests()


# --- routing -----------------------------------------------------------------


def test_busy_thread_enqueues_pending_prompt_with_producer_identity():
    locks = ThreadLockManager()
    agent = _agent(locks)
    fired: list[str] = []
    lock = locks.get_lock("t1")
    lock.acquire()
    try:
        # drop_on_abort producers hand off at enqueue and return at once.
        outcome = cd.submit_completion(
            agent, _delivery(drop_on_abort=True), fire=lambda: fired.append("x")
        )
        pending = get_pending_queue().drain("t1")
    finally:
        lock.release()

    assert outcome == cd.RESULT_QUEUED
    assert fired == []
    assert len(pending) == 1
    prompt = pending[0]
    assert prompt.message == "[Result] the thing finished"
    assert prompt.source == "callable_result"
    assert prompt.source_id == "cont-1"
    assert prompt.source_label == "Helper result"
    assert prompt.user_id == "u1"
    assert prompt.is_autonomous is True
    assert prompt.fanout_mailbox is None


def test_idle_thread_fires_the_producer_callback_and_queues_nothing():
    agent = _agent()
    fired: list[str] = []

    outcome = cd.submit_completion(agent, _delivery(), fire=lambda: fired.append("x"))

    assert outcome == cd.RESULT_FIRED
    assert fired == ["x"]
    assert get_pending_queue().drain("t1") == []


def test_idle_thread_defaults_to_fire_autonomous_turn(monkeypatch):
    agent = _agent()
    seen: list[cd.CompletionDelivery] = []
    monkeypatch.setattr(cd, "fire_autonomous_turn", lambda a, d: seen.append(d))

    outcome = cd.submit_completion(agent, _delivery())

    assert outcome == cd.RESULT_FIRED
    assert [d.source_id for d in seen] == ["cont-1"]


def test_closing_queue_falls_through_to_fire(monkeypatch):
    locks = ThreadLockManager()
    agent = _agent(locks)

    class ClosingQueue:
        def enqueue(self, thread_id, pending):
            raise PendingPromptQueueClosingError("closing")

    monkeypatch.setattr(
        "nymeria.core.pending_prompt_queue.get_pending_queue", lambda: ClosingQueue()
    )
    fired: list[str] = []
    lock = locks.get_lock("t1")
    lock.acquire()
    try:
        outcome = cd.submit_completion(agent, _delivery(), fire=lambda: fired.append("x"))
    finally:
        lock.release()

    assert outcome == cd.RESULT_FIRED
    assert fired == ["x"]


# --- drop guards -------------------------------------------------------------


def test_abort_flag_drops_only_when_the_producer_asks():
    locks = ThreadLockManager()
    locks.signal_abort("t1")
    agent = _agent(locks)

    assert cd.drop_reason(agent, _delivery(drop_on_abort=True)) == "aborted"
    assert cd.drop_reason(agent, _delivery(drop_on_abort=False)) is None

    fired: list[str] = []
    assert (
        cd.submit_completion(
            agent, _delivery(drop_on_abort=True), fire=lambda: fired.append("x")
        )
        == cd.RESULT_DROPPED
    )
    assert fired == []
    assert (
        cd.submit_completion(
            agent, _delivery(drop_on_abort=False), fire=lambda: fired.append("y")
        )
        == cd.RESULT_FIRED
    )
    assert fired == ["y"]


@pytest.mark.parametrize("drop_on_abort", [True, False])
def test_owner_mismatch_drops_in_both_modes(drop_on_abort):
    agent = _agent(owner="someone-else")
    fired: list[str] = []

    outcome = cd.submit_completion(
        agent, _delivery(drop_on_abort=drop_on_abort), fire=lambda: fired.append("x")
    )

    assert outcome == cd.RESULT_DROPPED
    assert cd.drop_reason(agent, _delivery(drop_on_abort=drop_on_abort)) == "owner_mismatch"
    assert fired == []
    assert get_pending_queue().drain("t1") == []


def test_owner_lookup_failure_fails_closed():
    agent = _agent(raise_on_lookup=True)
    fired: list[str] = []

    outcome = cd.submit_completion(agent, _delivery(), fire=lambda: fired.append("x"))

    assert outcome == cd.RESULT_DROPPED
    assert cd.drop_reason(agent, _delivery()) == "owner_lookup_failed"
    assert fired == []


def test_busy_then_closing_rechecks_guards_before_firing(monkeypatch):
    # The queue closes (holder releasing) and, in that window, the owner check
    # fails: the fall-through must re-run the guards, not fire blindly.
    locks = ThreadLockManager()
    repo_state = {"owner": "u1"}

    class FlippingRepo:
        def get_thread_owner(self, thread_id):
            return repo_state["owner"]

    agent = SimpleNamespace(_thread_locks=locks, accounts_repo=FlippingRepo())

    class ClosingQueue:
        def enqueue(self, thread_id, pending):
            repo_state["owner"] = "stranger"
            raise PendingPromptQueueClosingError("closing")

    monkeypatch.setattr(
        "nymeria.core.pending_prompt_queue.get_pending_queue", lambda: ClosingQueue()
    )
    fired: list[str] = []
    lock = locks.get_lock("t1")
    lock.acquire()
    try:
        outcome = cd.submit_completion(agent, _delivery(), fire=lambda: fired.append("x"))
    finally:
        lock.release()

    assert outcome == cd.RESULT_DROPPED
    assert fired == []


# --- the autonomous turn ------------------------------------------------------


class _Captured:
    def __init__(self):
        self.events: list[tuple[str, str, str, str, dict]] = []
        self.chunks: list[dict] = []
        self.activities: list[tuple[object, str, str, str, dict]] = []
        self.astream_kwargs: dict = {}


def _patch_turn(monkeypatch, *, stream_behavior) -> _Captured:
    cap = _Captured()

    def fake_publish_autonomous_event(event_type, thread_id, user_id, task_id, data):
        cap.events.append((event_type, thread_id, user_id, task_id, data))

    def fake_publish_agent_stream_chunk(chunk, *, thread_id, user_id, task_id):
        cap.chunks.append(chunk)
        return True

    def fake_stream_and_collect(agent_arg, *, astream_kwargs, on_chunk, error_message_factory):
        cap.astream_kwargs.update(astream_kwargs)
        return stream_behavior(on_chunk)

    def fake_log_activity(activity_type, message, user_id, thread_id, metadata):
        cap.activities.append((activity_type, message, user_id, thread_id, metadata))

    monkeypatch.setattr(
        "nymeria.core.event_bus.publish_autonomous_event", fake_publish_autonomous_event
    )
    monkeypatch.setattr(
        "nymeria.core.event_bus.publish_agent_stream_chunk", fake_publish_agent_stream_chunk
    )
    monkeypatch.setattr("nymeria.core.stream_bridge.stream_and_collect", fake_stream_and_collect)
    monkeypatch.setattr("nymeria.core.activity_log.log_activity", fake_log_activity)
    return cap


class _Result:
    def __init__(self, text="ack", *, iteration_limit_hit=False):
        self.text = text
        self.iteration_limit_hit = iteration_limit_hit

    def response_text(self, *, fallback_to_thinking: bool = True) -> str:
        return self.text


def test_fire_publishes_bookends_payloads_activity_and_trigger_override(monkeypatch):
    def behavior(on_chunk):
        on_chunk({"type": "response", "content": "ack"}, SimpleNamespace(chunk_count=1))
        return _Result("ack")

    cap = _patch_turn(monkeypatch, stream_behavior=behavior)
    delivery = _delivery(
        trigger_override='CallableResult("t-callee", "Helper")',
        started_data={"continuation_id": "cont-1"},
        completed_data={"continuation_id": "cont-1", "callable_name": "Helper"},
        activity_message="Helper finished a detached ask",
        activity_metadata={"continuation_id": "cont-1"},
        activity_type=ActivityType.TASK_COMPLETED,
    )

    cd.fire_autonomous_turn(_agent(), delivery)

    assert cap.astream_kwargs["message"] == "[Result] the thing finished"
    assert cap.astream_kwargs["thread_id"] == "t1"
    assert cap.astream_kwargs["_is_self_invoke"] is True
    assert cap.astream_kwargs["source"] == "callable_result"
    assert cap.astream_kwargs["source_id"] == "cont-1"
    assert cap.astream_kwargs["_trigger_override"] == 'CallableResult("t-callee", "Helper")'
    assert [e[0] for e in cap.events] == ["task_started", "task_completed"]
    started, completed = cap.events
    assert (started[1], started[2], started[3]) == ("t1", "u1", "callable-result-cont-1")
    assert started[4] == {
        "prompt": "[Result] the thing finished",
        "source": "callable_result",
        "continuation_id": "cont-1",
    }
    assert completed[4] == {
        "content": "ack",
        "source": "callable_result",
        "continuation_id": "cont-1",
        "callable_name": "Helper",
    }
    assert cap.chunks == [{"type": "response", "content": "ack"}]
    assert cap.activities == [
        (
            ActivityType.TASK_COMPLETED,
            "Helper finished a detached ask",
            "u1",
            "t1",
            {"continuation_id": "cont-1", "partial": False},
        )
    ]


def test_fire_bookends_even_when_the_stream_yields_no_eligible_chunk(monkeypatch):
    # Only queue-meta chunks (never real work): task_started must still precede
    # task_completed so consumers see a well-formed lifecycle.
    def behavior(on_chunk):
        on_chunk({"type": "prompt_queued", "position": 1}, SimpleNamespace())
        return _Result("")

    cap = _patch_turn(monkeypatch, stream_behavior=behavior)

    cd.fire_autonomous_turn(_agent(), _delivery())

    assert [e[0] for e in cap.events] == ["task_started", "task_completed"]


def test_fire_marks_partial_on_iteration_limit(monkeypatch):
    cap = _patch_turn(
        monkeypatch, stream_behavior=lambda on_chunk: _Result("half", iteration_limit_hit=True)
    )

    cd.fire_autonomous_turn(_agent(), _delivery())

    completed = cap.events[-1][4]
    assert completed["content"] == "half"
    assert completed["partial"] is True
    assert cap.activities[0][4]["partial"] is True


def test_fire_error_path_publishes_error_bookend_and_failed_activity(monkeypatch):
    def behavior(on_chunk):
        raise RuntimeError("provider down")

    cap = _patch_turn(monkeypatch, stream_behavior=behavior)

    cd.fire_autonomous_turn(_agent(), _delivery(completed_data={"continuation_id": "cont-1"}))

    assert [e[0] for e in cap.events] == ["task_started", "task_completed"]
    err = cap.events[-1][4]
    assert err["status"] == "error"
    assert err["error"] == "provider down"
    assert err["error_message"] == "provider down"
    assert err["continuation_id"] == "cont-1"
    assert "notification failed: provider down" in err["content"]
    assert cap.activities[0][0] is ActivityType.TASK_FAILED
    assert cap.activities[0][4]["error"] == "provider down"


def test_fire_respects_drop_guard(monkeypatch):
    cap = _patch_turn(monkeypatch, stream_behavior=lambda on_chunk: _Result("ack"))

    cd.fire_autonomous_turn(_agent(owner="stranger"), _delivery())

    assert cap.events == []
    assert cap.astream_kwargs == {}


# --- queued must-deliver results follow through (behavior 23) -----------------


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), "condition not met in time"


def _submit_in_thread(agent, delivery, fired: list[str]):
    outcome: list[str] = []
    worker = threading.Thread(
        target=lambda: outcome.append(
            cd.submit_completion(agent, delivery, fire=lambda: fired.append("x"))
        ),
        daemon=True,
    )
    worker.start()
    return worker, outcome


@pytest.mark.parametrize("wake", ["user_stop", "abort_cascade", "inject_failed"])
def test_queued_must_deliver_result_is_redelivered_when_cleared_before_absorption(wake):
    locks = ThreadLockManager()
    agent = _agent(locks)
    fired: list[str] = []
    lock = locks.get_lock("t1")
    lock.acquire()
    try:
        worker, outcome = _submit_in_thread(agent, _delivery(drop_on_abort=False), fired)
        _wait_until(lambda: get_pending_queue().size("t1") == 1)
        queue = get_pending_queue()
        if wake == "user_stop":
            restored, discarded = queue.clear_with_restore("t1")
            assert (restored, discarded) == ([], 1)
        elif wake == "abort_cascade":
            queue.clear("t1", abandoned=True)
        else:
            notify_batch_error(queue.drain("t1"), code="inject_failed", content="boom")
        time.sleep(0.05)
        assert fired == []  # the thread is still busy: nothing fires yet
    finally:
        lock.release()

    worker.join(10)
    assert outcome == [cd.RESULT_FIRED]
    assert fired == ["x"]
    assert get_pending_queue().size("t1") == 0


def test_queued_must_deliver_result_absorbed_by_the_holder_fires_nothing():
    locks = ThreadLockManager()
    agent = _agent(locks)
    fired: list[str] = []
    lock = locks.get_lock("t1")
    lock.acquire()
    try:
        worker, outcome = _submit_in_thread(agent, _delivery(drop_on_abort=False), fired)
        _wait_until(lambda: get_pending_queue().size("t1") == 1)
        batch = get_pending_queue().drain("t1")
        notify_batch_absorbed(batch)
    finally:
        lock.release()

    worker.join(10)
    assert outcome == [cd.RESULT_QUEUED]
    assert fired == []


def test_queued_must_deliver_result_gives_up_after_the_redelivery_cap(monkeypatch):
    monkeypatch.setattr(cd, "QUEUED_REDELIVERY_ATTEMPTS", 1)
    locks = ThreadLockManager()
    agent = _agent(locks)
    fired: list[str] = []
    lock = locks.get_lock("t1")
    lock.acquire()
    try:
        worker, outcome = _submit_in_thread(agent, _delivery(drop_on_abort=False), fired)
        for _ in range(2):
            _wait_until(lambda: get_pending_queue().size("t1") == 1)
            get_pending_queue().clear_with_restore("t1")
        worker.join(10)
    finally:
        lock.release()

    assert outcome == [cd.RESULT_DROPPED]
    assert fired == []


# --- InlineLatch (the shared inline-or-detached decision) ----------------------


def test_inline_latch_claims_inline_only_when_done_within_budget():
    latch = cd.InlineLatch()
    latch.done.set()
    assert latch.wait_inline(0.05) == "inline"
    assert latch.settle(0.0) == "inline"

    late = cd.InlineLatch()
    assert late.wait_inline(0.05) == "detached"
    late.done.set()
    assert late.settle(0.0) == "detached"


def test_inline_latch_settles_detached_when_no_caller_claims_within_grace():
    latch = cd.InlineLatch()
    latch.done.set()
    assert latch.settle(0.05) == "detached"
    # One-way: a caller arriving after the producer settled cannot flip it.
    assert latch.wait_inline(0) == "detached"


def test_inline_latch_settle_waits_out_the_grace_for_a_late_inline_claim():
    latch = cd.InlineLatch()
    latch.done.set()
    threading.Timer(0.05, lambda: latch.wait_inline(0)).start()
    assert latch.settle(2.0) == "inline"


# --------------------------------------------------------------------------- #
# hold_thread: the model-free holder's lock choreography
# --------------------------------------------------------------------------- #


def _user_prompt(text: str):
    from nymeria.core.pending_prompt_queue import make_pending_prompt

    return make_pending_prompt(
        message=text,
        source="user",
        source_id="u",
        source_label="user",
        user_id="u1",
        is_autonomous=False,
        fanout_mailbox=None,
        consumer_loop=None,
    )


def test_hold_thread_looks_like_a_holder_turn_and_closes_the_queue_window():
    reset_pending_queue_for_tests()
    locks = ThreadLockManager()
    agent = _agent(locks)
    locks.signal_abort("t1")
    queue = get_pending_queue()
    # A prompt that snuck into the queue before the hold is handed back the
    # way a stop does, never stranded behind a holder with no boundary.
    early = _user_prompt("early")
    queue.enqueue("t1", early)

    with cd.hold_thread(agent, "t1", holder="claude_code", task_id="claude-code-1", timeout=1) as held:
        assert held is True
        assert locks.get_lock("t1").locked() is True
        assert locks.get_lock_info("t1")["holder"] == "claude_code"
        assert locks.get_abort_event("t1").is_set() is False
        assert early.restored is True
        # A contender arriving now must block on the lock (astream's
        # is_releasing arm), not enqueue behind us.
        with pytest.raises(PendingPromptQueueClosingError):
            queue.enqueue("t1", _user_prompt("late"))

    assert locks.get_lock("t1").locked() is False
    assert locks.get_lock_info("t1") is None
    assert queue.is_releasing("t1") is False
    queue.enqueue("t1", _user_prompt("next turn"))  # window open again


def test_hold_thread_yields_false_and_touches_nothing_when_a_turn_keeps_the_lock():
    reset_pending_queue_for_tests()
    locks = ThreadLockManager()
    agent = _agent(locks)
    lock = locks.get_lock("t1")
    assert lock.acquire(blocking=False)
    try:
        with cd.hold_thread(agent, "t1", holder="claude_code", task_id="x", timeout=0.05) as held:
            assert held is False
            assert locks.get_lock_info("t1") is None
            assert get_pending_queue().is_releasing("t1") is False
    finally:
        lock.release()


def test_hold_thread_releases_on_a_body_exception():
    reset_pending_queue_for_tests()
    locks = ThreadLockManager()
    agent = _agent(locks)
    with pytest.raises(RuntimeError):
        with cd.hold_thread(agent, "t1", holder="h", task_id="x", timeout=1):
            raise RuntimeError("boom")
    assert locks.get_lock("t1").locked() is False
    assert locks.get_lock_info("t1") is None
    assert get_pending_queue().is_releasing("t1") is False


def test_deliver_without_turn_respects_the_guard_and_reports_busy(monkeypatch):
    reset_pending_queue_for_tests()
    cap = _patch_turn(monkeypatch, stream_behavior=lambda on_chunk: _Result())
    delivery = _delivery(drop_on_abort=False)

    mismatched = _agent(owner="someone-else")
    assert cd.deliver_without_turn(mismatched, delivery, "text", timeout=1) == cd.RESULT_DROPPED
    assert cap.events == []

    locks = ThreadLockManager()
    agent = _agent(locks)
    lock = locks.get_lock("t1")
    assert lock.acquire(blocking=False)
    try:
        assert cd.deliver_without_turn(agent, delivery, "text", timeout=0.05) == cd.RESULT_BUSY
    finally:
        lock.release()
    assert cap.events == []
    assert cap.activities == []
