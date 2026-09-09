"""The thread request/reply ledger (core/thread_requests.py, backlog #357).

Written from the expected-behaviours list in ``tmp/request-reply-plan.md``,
not from the implementation: a request is recorded and receipted at once, the
callee's ONLY reply channel is ``reply_to_thread``, a final reply reaches the
caller exactly once (inline to a waiter, else a wake-up prompt through
completion_delivery), waits are bounded and chainable, statuses show tool
names and preamble text only, and the no-reply safety net is a turn-end
reminder for the callee plus a stale-request sweep for the caller.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from nymeria.core import completion_delivery as cd
from nymeria.core import thread_requests as tr
from nymeria.core.agent import set_current_agent
from nymeria.core.pending_prompt_queue import (
    get_pending_queue,
    notify_batch_absorbed,
    reset_pending_queue_for_tests,
)
from nymeria.core.thread_lock_manager import ThreadLockManager
from nymeria.tools.thread_requests import reply_to_thread, wait_for_reply

CALLER = "caller-thread"
CALLEE = "callee-thread"
OTHER = "other-thread"
_REAL_WAIT_CAP = tr.wait_cap


class _Repo:
    def __init__(self, owner: str | None = "owner"):
        self.owner = owner

    def get_thread_owner(self, thread_id: str):
        return self.owner


class _Agent:
    def __init__(self, *, messages: list | None = None):
        self._thread_locks = ThreadLockManager()
        self.accounts_repo = _Repo()
        self._default_graph = SimpleNamespace(
            get_state=lambda config: SimpleNamespace(values={"messages": list(messages or [])})
        )
        self.registered: list[tuple[str, str]] = []
        self.unregistered: list[tuple[str, str]] = []

    def register_callable_invocation(self, parent: str, child: str) -> None:
        self.registered.append((parent, child))

    def unregister_callable_invocation(self, parent: str, child: str) -> None:
        self.unregistered.append((parent, child))

    def hold(self, thread_id: str) -> threading.Lock:
        lock = self._thread_locks.get_lock(thread_id)
        lock.acquire()
        self._thread_locks.set_lock_info(thread_id, holder="test")
        return lock

    def release(self, thread_id: str, lock: threading.Lock) -> None:
        self._thread_locks.clear_lock_info(thread_id)
        lock.release()


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), "condition not met in time"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    tr.reset_for_tests()
    reset_pending_queue_for_tests()
    monkeypatch.setattr(tr, "wait_cap", lambda: 600.0)
    yield
    tr.reset_for_tests()
    reset_pending_queue_for_tests()
    set_current_agent(None)


def _capture_deliveries(monkeypatch) -> list[cd.CompletionDelivery]:
    seen: list[cd.CompletionDelivery] = []
    monkeypatch.setattr(cd, "fire_autonomous_turn", lambda agent, d: seen.append(d))
    return seen


def _open(*, dispatched: bool = True, **overrides) -> tr.ThreadRequest:
    base = dict(
        caller_thread_id=CALLER,
        caller_user_id="owner",
        caller_name="Coordinator",
        target_thread_id=CALLEE,
        callable_name="Helper",
        task="find the staff directory on the NAS",
        task_id="req-task-1",
    )
    base.update(overrides)
    req = tr.open_request(**base)
    if dispatched:
        # The prompt reached the callee's turn (its stream produced a chunk).
        req.progress.observe({"type": "response", "content": "working"})
    return req


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id, "user_id": "owner"}}


# --- 1, 17, 31: the request record, the callee's contract, the receipt -----------------


def test_the_callee_prompt_carries_the_contract_and_the_receipt_names_the_same_id():
    req = _open()
    prompt = tr.format_request_prompt(task="find the directory", req=req)
    receipt = tr.request_receipt(req)

    assert prompt.startswith("[Request Metadata]\n")
    assert f"request_id: {req.id}" in prompt
    assert f"source_thread_id: {CALLER}" in prompt
    assert "source_thread_name: Coordinator" in prompt
    assert f'reply_to_thread(request_id="{req.id}"' in prompt
    assert "your ordinary final message is not" in prompt
    assert "final=false" in prompt and "end your turn" in prompt
    assert prompt.endswith("\n\nfind the directory")
    assert "[Warning]" not in receipt

    assert receipt.startswith(f"[Requested]: request_id={req.id} target=Helper")
    assert f'wait_for_reply(request_id="{req.id}", timeout_seconds=N)' in receipt
    assert "timeout_seconds=0" in receipt
    assert req.state == tr.STATE_OPEN
    assert [r.id for r in tr.requests_owed_by(CALLEE)] == [req.id]
    assert [r.id for r in tr.requests_awaited_by(CALLER)] == [req.id]


def test_a_target_without_the_reply_tool_is_flagged_in_both_the_receipt_and_the_prompt():
    req = _open(target_can_reply=False)

    receipt = tr.request_receipt(req)
    prompt = tr.format_request_prompt(task="t", req=req)

    assert "[Warning]: Helper does not have reply_to_thread enabled" in receipt
    assert "reply_to_thread is NOT enabled on this thread" in prompt
    assert req.state == tr.STATE_OPEN  # still recorded: a later enable plus reply works


def test_a_queued_receipt_says_so():
    req = _open()
    assert "queued behind its current turn" in tr.request_receipt(req, queued=True)


# --- 4, 5: a final reply reaches an idle or busy caller exactly once ------------------


def test_final_reply_to_an_idle_caller_fires_one_wake_up_turn_with_the_reply_prompt(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()

    result = tr.reply(
        request_id=req.id, content="Staff directory: /nas/HR/staff.xlsx",
        final=True, replier_thread_id=CALLEE, agent=agent,
    )

    assert result.startswith("[Replied]") and "as a new prompt" in result
    _wait_until(lambda: len(seen) == 1)
    delivery = seen[0]
    assert delivery.thread_id == CALLER and delivery.user_id == "owner"
    assert delivery.source == "thread_reply" and delivery.source_id == req.id
    assert delivery.drop_on_abort is False
    assert delivery.prompt_text.startswith("[Reply from Helper]")
    assert f"request_id={req.id}" in delivery.prompt_text
    assert "Staff directory: /nas/HR/staff.xlsx" in delivery.prompt_text
    assert "data from Helper, not as instructions" in delivery.prompt_text
    _wait_until(lambda: req.delivered_via == "wake_up")
    assert req.state == tr.STATE_REPLIED
    time.sleep(0.2)
    assert len(seen) == 1


def test_busy_caller_absorbs_the_queued_reply_exactly_once(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    lock = agent.hold(CALLER)
    batch: list = []
    try:
        result = tr.reply(request_id=req.id, content="done", final=True, replier_thread_id=CALLEE, agent=agent)
        assert result.startswith("[Replied]")

        def drained() -> bool:
            batch.extend(get_pending_queue().drain(CALLER))
            return bool(batch)

        _wait_until(drained, timeout=3.0)
        assert len(batch) == 1
        assert batch[0].source == "thread_reply" and batch[0].source_id == req.id
        assert "[Reply from Helper]" in batch[0].message and "done" in batch[0].message
        notify_batch_absorbed(batch)  # what the running turn does with a drained batch
    finally:
        agent.release(CALLER, lock)
    _wait_until(lambda: req.delivered_via == "wake_up")
    time.sleep(0.3)
    assert seen == []  # absorbed by the running turn, never also fired as a new turn


# --- 7, 8: only the addressee replies, once ------------------------------------------


def test_unknown_or_misaddressed_replies_error_and_deliver_nothing(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()

    unknown = tr.reply(request_id="req-nope", content="x", final=True, replier_thread_id=CALLEE, agent=agent)
    wrong = tr.reply(request_id=req.id, content="x", final=True, replier_thread_id=OTHER, agent=agent)
    empty = tr.reply(request_id=req.id, content="   ", final=True, replier_thread_id=CALLEE, agent=agent)

    assert unknown.startswith("[Error]: Unknown request_id")
    assert wrong.startswith("[Error]") and "not addressed to this thread" in wrong
    assert empty.startswith("[Error]") and "empty" in empty
    time.sleep(0.2)
    assert seen == [] and req.state == tr.STATE_OPEN


def test_a_second_final_reply_is_refused_and_not_delivered(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    tr.reply(request_id=req.id, content="first", final=True, replier_thread_id=CALLEE, agent=agent)
    _wait_until(lambda: len(seen) == 1)

    second = tr.reply(request_id=req.id, content="second", final=True, replier_thread_id=CALLEE, agent=agent)

    assert second.startswith("[Error]") and "closed" in second
    time.sleep(0.2)
    assert len(seen) == 1 and req.final_reply == "first"


# --- 9: two callers, two replies, no crossing ----------------------------------------------


def test_two_callers_each_receive_only_their_own_reply(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    first = _open(caller_thread_id="caller-a", caller_name="A", task="task for A")
    second = _open(caller_thread_id="caller-b", caller_name="B", task="task for B")

    tr.reply(request_id=second.id, content="answer for B", final=True, replier_thread_id=CALLEE, agent=agent)
    tr.reply(request_id=first.id, content="answer for A", final=True, replier_thread_id=CALLEE, agent=agent)
    _wait_until(lambda: len(seen) == 2)

    by_thread = {d.thread_id: d for d in seen}
    assert set(by_thread) == {"caller-a", "caller-b"}
    assert "answer for A" in by_thread["caller-a"].prompt_text
    assert "answer for B" not in by_thread["caller-a"].prompt_text
    assert "answer for B" in by_thread["caller-b"].prompt_text
    assert by_thread["caller-a"].source_id == first.id
    assert by_thread["caller-b"].source_id == second.id


# --- 10: progress replies keep the request open and do not wake the caller ---------------------


def test_progress_reply_is_recorded_without_waking_the_caller(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()

    result = tr.reply(
        request_id=req.id, content="scanned 3 of 9 folders", final=False,
        replier_thread_id=CALLEE, agent=agent,
    )

    assert result.startswith("[Progress noted]")
    time.sleep(0.2)
    assert seen == [] and req.state == tr.STATE_OPEN
    status = tr.wait_for_reply(request_id=req.id, timeout=0, waiter_thread_id=CALLER, agent=agent)
    assert status.startswith("[Waiting]")
    assert "scanned 3 of 9 folders" in status


# --- 11, 12, 13: waits ---------------------------------------------------------------------


def test_reply_within_the_wait_returns_inline_and_is_not_also_delivered(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()

    def reply_soon():
        time.sleep(0.2)
        tr.reply(request_id=req.id, content="inline answer", final=True, replier_thread_id=CALLEE, agent=agent)

    threading.Thread(target=reply_soon, daemon=True).start()
    result = tr.wait_for_reply(request_id=req.id, timeout=5, waiter_thread_id=CALLER, agent=agent)

    assert result.startswith("[Reply from Helper]") and "inline answer" in result
    assert req.delivered_via == "inline"
    time.sleep(0.3)
    assert seen == []


def test_wait_timing_out_returns_a_status_and_a_later_reply_is_delivered_once(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()

    status = tr.wait_for_reply(request_id=req.id, timeout=1, waiter_thread_id=CALLER, agent=agent)

    assert status.startswith("[Waiting]") and "waited 1s" in status
    assert req.waiter is None  # the wait deregistered itself before returning
    tr.reply(request_id=req.id, content="late answer", final=True, replier_thread_id=CALLEE, agent=agent)
    _wait_until(lambda: len(seen) == 1)
    assert "late answer" in seen[0].prompt_text
    time.sleep(0.2)
    assert len(seen) == 1


def test_a_reply_between_waits_is_delivered_and_the_next_wait_reports_it_closed(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    tr.reply(request_id=req.id, content="landed in the gap", final=True, replier_thread_id=CALLEE, agent=agent)
    _wait_until(lambda: len(seen) == 1)

    result = tr.wait_for_reply(request_id=req.id, timeout=5, waiter_thread_id=CALLER, agent=agent)

    assert result.startswith("[Replied]") and "delivered to you as a prompt" in result
    assert "landed in the gap" in result
    time.sleep(0.2)
    assert len(seen) == 1


def test_a_waiter_registered_before_the_wait_catches_a_reply_that_lands_in_the_gap(monkeypatch):
    """begin_wait/finish_wait exist so a call that waits from the start can
    register before the callee is dispatched: a reply arriving between the two
    is inline, the callee is told so, and nothing is delivered as a prompt."""
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()

    waiter = tr.begin_wait(req, CALLER, agent)
    assert not isinstance(waiter, str)
    told = tr.reply(request_id=req.id, content="landed early", final=True, replier_thread_id=CALLEE, agent=agent)
    result = tr.finish_wait(req, waiter, seconds=5, agent=agent)

    assert "received it inline" in told
    assert result.startswith("[Reply from Helper]") and "landed early" in result
    assert req.delivered_via == "inline"
    assert isinstance(tr.begin_wait(req, CALLER, agent), str)  # closed: no second wait
    time.sleep(0.2)
    assert seen == []


def test_an_abandoned_wait_reroutes_a_landed_reply_as_a_wake_up(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    waiter = tr.begin_wait(req, CALLER, agent)
    assert not isinstance(waiter, str)
    tr.reply(request_id=req.id, content="orphaned", final=True, replier_thread_id=CALLEE, agent=agent)

    tr.abandon_wait(req, waiter, agent)

    _wait_until(lambda: len(seen) == 1)
    assert "orphaned" in seen[0].prompt_text and req.waiter is None


# --- 14: a status shows names and preamble text, never arguments or results ----------------------


def test_status_shows_tool_names_and_preamble_but_no_arguments_or_results():
    messages = [
        HumanMessage(content="[Request Metadata] ... find the directory"),
        AIMessage(
            content="Let me search the NAS first.",
            tool_calls=[{"name": "nas_search", "args": {"query": "SECRET-ARG-123"}, "id": "tc1", "type": "tool_call"}],
        ),
        ToolMessage(content="SECRET-RESULT-456 rows", tool_call_id="tc1", name="nas_search"),
        AIMessage(
            content="Now reading the file.",
            tool_calls=[
                {"name": "file_read", "args": {"path": "/nas/hr/SECRET-PATH"}, "id": "tc2", "type": "tool_call"},
                {"name": "memory_read", "args": {}, "id": "tc3", "type": "tool_call"},
            ],
        ),
        ToolMessage(content="SECRET-CONTENT-789", tool_call_id="tc2", name="file_read"),
    ]
    agent = _Agent(messages=messages)
    req = _open()

    status = tr.status_text(req, agent, steps=5)

    assert "Recent steps of Helper" in status
    assert '"Let me search the NAS first." -> nas_search' in status
    assert '"Now reading the file." -> file_read, memory_read' in status
    for secret in ("SECRET-ARG-123", "SECRET-RESULT-456", "SECRET-PATH", "SECRET-CONTENT-789"):
        assert secret not in status
    assert "Helper is idle" in status


def test_status_limits_steps_and_reports_a_busy_target():
    messages = [AIMessage(content=f"step {i}", tool_calls=[{"name": f"tool_{i}", "args": {}, "id": f"t{i}", "type": "tool_call"}]) for i in range(8)]
    agent = _Agent(messages=messages)
    req = _open()
    lock = agent.hold(CALLEE)
    try:
        status = tr.status_text(req, agent, steps=2)
    finally:
        agent.release(CALLEE, lock)
    assert "Helper is busy" in status
    assert "step 6" in status and "step 7" in status and "step 5" not in status
    assert "last 2" in status


# --- 15: only the requester waits; no mutual-wait deadlock -------------------------------------


def test_only_the_requesting_thread_may_wait_and_a_mutual_wait_returns_at_once():
    agent = _Agent()
    a_to_b = _open(caller_thread_id="A", target_thread_id="B", callable_name="B")
    b_to_a = _open(caller_thread_id="B", target_thread_id="A", callable_name="A")

    assert tr.wait_for_reply(request_id=a_to_b.id, timeout=1, waiter_thread_id=OTHER, agent=agent).startswith(
        "[Error]"
    )

    # A parks on its request to B; B then tries to wait on ITS request to A.
    parked = threading.Event()

    def a_waits():
        parked.set()
        tr.wait_for_reply(request_id=a_to_b.id, timeout=2, waiter_thread_id="A", agent=agent)

    thread = threading.Thread(target=a_waits, daemon=True)
    thread.start()
    parked.wait(2)
    _wait_until(lambda: a_to_b.waiter is not None)
    started = time.time()
    result = tr.wait_for_reply(request_id=b_to_a.id, timeout=5, waiter_thread_id="B", agent=agent)
    assert time.time() - started < 1.0
    assert result.startswith("[Waiting]") and "would deadlock" in result
    thread.join(5)


# --- 16: bounds ----------------------------------------------------------------------------------


def test_waits_are_clamped_and_the_kill_timeout_is_the_clamped_wait_plus_margin(monkeypatch):
    monkeypatch.setattr(tr, "wait_cap", lambda: 600.0)
    assert tr.clamp_wait(0) == 0.0 and tr.clamp_wait(-5) == 0.0 and tr.clamp_wait("junk") == 0.0
    assert tr.clamp_wait(120) == 120.0
    assert tr.clamp_wait(99999) == 600.0
    assert tr.wait_kill_timeout(0) is None
    assert tr.wait_kill_timeout(120) == 120.0 + tr.WAIT_KILL_MARGIN_SECONDS
    assert tr.wait_kill_timeout(99999) == 600.0 + tr.WAIT_KILL_MARGIN_SECONDS


def test_wait_cap_reads_the_setting(monkeypatch):
    monkeypatch.setattr(tr, "wait_cap", _REAL_WAIT_CAP)
    monkeypatch.setattr("nymeria.config.get_settings", lambda: SimpleNamespace(callable_wait_max_seconds=900))
    assert tr.wait_cap() == 900.0
    monkeypatch.setattr("nymeria.config.get_settings", lambda: SimpleNamespace())
    assert tr.wait_cap() == float(tr.DEFAULT_WAIT_MAX_SECONDS)


# --- 18: compaction seam --------------------------------------------------------------------


def test_compaction_block_lists_owed_and_awaited_requests_and_is_absent_otherwise():
    owed = _open(caller_thread_id="A", target_thread_id="B", callable_name="B", task="owed task")
    awaited = _open(caller_thread_id="B", target_thread_id="C", callable_name="C", task="awaited task")

    block = tr.compaction_block("B")

    assert block is not None and block.startswith("[Open requests]")
    assert f"You owe a reply to {owed.id}" in block and "owed task" in block
    assert f'reply_to_thread(request_id="{owed.id}"' in block
    assert f"You are waiting on {awaited.id} to C" in block and "awaited task" in block
    assert tr.compaction_block("nobody") is None


def test_the_resume_opener_carries_the_open_requests_block(monkeypatch):
    from nymeria.core import agent_memory_seed as seed

    _open(caller_thread_id="A", target_thread_id="B", callable_name="B", task="owed task")
    monkeypatch.setattr(seed, "read_global_memory", lambda uid, tid: "[empty]")
    monkeypatch.setattr(seed, "read_thread_memory", lambda uid, tid: "[empty]")
    monkeypatch.setattr(seed, "read_team_memory", lambda uid, tid: None)

    tail = seed.build_resume_compaction_tail(user_id="owner", thread_id="B", summary="the summary")
    opener = tail[0].content if isinstance(tail[0].content, str) else str(tail[0].content)

    assert "the summary" in opener and "[Open requests]" in opener and "owed task" in opener
    plain = seed.build_resume_compaction_tail(user_id="owner", thread_id="Z", summary="s")
    assert "[Open requests]" not in str(plain[0].content)


# --- 23, 24: the callee's turn-end reminder ----------------------------------------------------


def test_reminder_names_owed_requests_once_and_never_after_a_reply(monkeypatch):
    agent = _Agent()
    _capture_deliveries(monkeypatch)
    req = _open()

    first = tr.unreplied_request_reminder(CALLEE)
    second = tr.unreplied_request_reminder(CALLEE)

    assert first is not None and first.startswith("[Unreplied request]")
    assert req.id in first and "Coordinator" in first and "reply_to_thread(request_id=" in first
    assert "Nothing from this turn was delivered" in first
    assert second is None  # stamped: a later turn end does not re-drive
    assert req.reminded_at is not None

    other = _open(task="another")
    tr.reply(request_id=other.id, content="answered", final=True, replier_thread_id=CALLEE, agent=agent)
    assert tr.unreplied_request_reminder(CALLEE) is None
    assert tr.unreplied_request_reminder(OTHER) is None


def test_no_reminder_before_the_request_reached_the_callee_or_before_a_schedule_is_due():
    """A turn ending on the target between the record being opened and the
    prompt reaching the target (or before a scheduled TODO is due) is unrelated
    to the request: the single reminder must not spend itself on a preview."""
    pending = _open(dispatched=False)
    assert tr.unreplied_request_reminder(CALLEE) is None
    assert pending.reminded_at is None
    pending.progress.observe({"type": "queued"})  # enqueued behind the callee's running turn
    assert tr.unreplied_request_reminder(CALLEE) is not None

    due_at = time.time() + 3600
    scheduled = _open(dispatched=False, scheduled_for=due_at, task="later task")
    assert scheduled.scheduled is True and scheduled.opened_at == due_at
    assert scheduled not in tr.requests_owed_by(CALLEE)
    assert tr.unreplied_request_reminder(CALLEE) is None
    assert scheduled in tr.requests_owed_by(CALLEE, now=due_at + 1)
    assert not scheduled.reminder_eligible(due_at + 1)  # ticker lag grace
    assert scheduled.reminder_eligible(due_at + tr.SCHEDULED_REMINDER_GRACE_SECONDS + 1)
    block = tr.compaction_block(CALLER)
    assert block is not None and "scheduled, due in" in block and "later task" in block
    owed_block = tr.compaction_block(CALLEE) or ""
    assert "later task" not in owed_block  # not owed until due
    assert pending.task in owed_block  # the immediate one is


def test_a_callee_turn_that_dies_closes_the_request_and_tells_the_caller_now(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    tr.reply(request_id=req.id, content="got halfway", final=False, replier_thread_id=CALLEE, agent=agent)

    tr.fail_request(req, "Helper execution failed: boom", agent)

    assert req.state == tr.STATE_FAILED
    _wait_until(lambda: len(seen) == 1)
    notice = seen[0]
    assert notice.thread_id == CALLER and notice.drop_on_abort is False
    assert notice.prompt_text.startswith("[NoReply]") and "boom" in notice.prompt_text
    assert "got halfway" in notice.prompt_text and req.id in notice.prompt_text
    late = tr.reply(request_id=req.id, content="too late", final=True, replier_thread_id=CALLEE, agent=agent)
    assert late.startswith("[Error]") and "failed" in late
    status = tr.wait_for_reply(request_id=req.id, timeout=0, waiter_thread_id=CALLER, agent=agent)
    assert status.startswith("[NoReply]") and "boom" in status
    assert tr.unreplied_request_reminder(CALLEE) is None
    time.sleep(0.2)
    assert len(seen) == 1


def test_a_failure_while_the_caller_waits_is_returned_inline(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    waiter = tr.begin_wait(req, CALLER, agent)
    assert not isinstance(waiter, str)
    tr.fail_request(req, "boom", agent)
    result = tr.finish_wait(req, waiter, seconds=5, agent=agent)
    assert result.startswith("[NoReply]") and "boom" in result
    time.sleep(0.2)
    assert seen == []


def test_a_wait_notices_its_own_thread_being_stopped(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    waiter = tr.begin_wait(req, CALLER, agent)
    assert not isinstance(waiter, str)

    def stop_soon():
        time.sleep(0.3)
        agent._thread_locks.signal_abort(CALLER)

    threading.Thread(target=stop_soon, daemon=True).start()
    started = time.time()
    result = tr.finish_wait(req, waiter, seconds=10, agent=agent)

    assert time.time() - started < 3.0
    assert result.startswith("[Aborted]") and req.state == tr.STATE_OPEN and req.waiter is None
    # The reply still reaches the caller later, once, as a prompt.
    agent._thread_locks.clear_abort(CALLER)
    tr.reply(request_id=req.id, content="after the stop", final=True, replier_thread_id=CALLEE, agent=agent)
    _wait_until(lambda: len(seen) == 1)
    assert "after the stop" in seen[0].prompt_text


def test_a_reply_landing_on_a_stopped_caller_is_also_sent_as_a_prompt(monkeypatch):
    """A stopped turn discards its tool results, so a reply that lands inline
    during the stop must not die with them."""
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    waiter = tr.begin_wait(req, CALLER, agent)
    assert not isinstance(waiter, str)
    agent._thread_locks.signal_abort(CALLER)

    def reply_soon():
        time.sleep(0.2)
        tr.reply(request_id=req.id, content="landed on a stopped turn", final=True, replier_thread_id=CALLEE, agent=agent)

    threading.Thread(target=reply_soon, daemon=True).start()
    result = tr.finish_wait(req, waiter, seconds=10, agent=agent)
    agent._thread_locks.clear_abort(CALLER)

    assert "landed on a stopped turn" in result
    _wait_until(lambda: len(seen) == 1)
    assert "landed on a stopped turn" in seen[0].prompt_text


def test_sweep_clears_a_waiter_left_by_a_dead_thread(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    waiter = tr.begin_wait(req, CALLER, agent)
    assert not isinstance(waiter, str)
    waiter.started_at = time.time() - (tr.wait_cap() + tr.WAIT_KILL_MARGIN_SECONDS + tr.REQUEST_SWEEP_INTERVAL_SECONDS + 5)

    tr.sweep_stale_requests(agent)

    assert req.waiter is None and req.state == tr.STATE_OPEN
    tr.reply(request_id=req.id, content="to a fresh caller turn", final=True, replier_thread_id=CALLEE, agent=agent)
    _wait_until(lambda: len(seen) == 1)  # a wake-up, not a hand-off to the dead waiter


def test_a_reply_nobody_could_receive_is_recorded_as_dropped(monkeypatch):
    agent = _Agent()
    agent.accounts_repo = _Repo(owner="someone-else")  # the caller thread changed hands
    seen = _capture_deliveries(monkeypatch)
    req = _open()

    tr.reply(request_id=req.id, content="secret answer", final=True, replier_thread_id=CALLEE, agent=agent)

    _wait_until(lambda: req.delivered_via == "dropped")
    assert seen == []
    status = tr.wait_for_reply(request_id=req.id, timeout=0, waiter_thread_id=CALLER, agent=agent)
    assert "DROPPED" in status and "secret answer" in status


# --- 25, 26, 28: the caller-side sweep -----------------------------------------------------------


def test_sweep_nudges_the_caller_once_when_the_target_is_idle_past_the_window(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    later = req.opened_at + tr.REQUEST_NUDGE_SECONDS + 1

    assert tr.sweep_stale_requests(agent, now=req.opened_at + 60) == 0
    assert tr.sweep_stale_requests(agent, now=later) == 1
    _wait_until(lambda: len(seen) == 1)
    nudge = seen[0]
    assert nudge.thread_id == CALLER and nudge.source == "request_nudge"
    assert nudge.drop_on_abort is False
    assert nudge.prompt_text.startswith("[No reply yet]") and req.id in nudge.prompt_text
    assert f'wait_for_reply(request_id="{req.id}", timeout_seconds=0)' in nudge.prompt_text
    assert tr.sweep_stale_requests(agent, now=later + 600) == 0  # one nudge, ever
    time.sleep(0.2)
    assert len(seen) == 1 and req.state == tr.STATE_OPEN


def test_sweep_skips_the_nudge_while_the_target_is_busy(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    lock = agent.hold(CALLEE)
    try:
        assert tr.sweep_stale_requests(agent, now=req.opened_at + tr.REQUEST_NUDGE_SECONDS + 1) == 0
    finally:
        agent.release(CALLEE, lock)
    time.sleep(0.2)
    assert seen == [] and req.nudged_at is None


def test_sweep_expires_a_request_with_a_no_reply_notice_and_a_late_reply_is_refused(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    tr.reply(request_id=req.id, content="halfway", final=False, replier_thread_id=CALLEE, agent=agent)

    assert tr.sweep_stale_requests(agent, now=req.opened_at + tr.REQUEST_EXPIRY_SECONDS + 1) == 1
    _wait_until(lambda: len(seen) == 1)
    notice = seen[0]
    assert notice.prompt_text.startswith("[NoReply]") and req.id in notice.prompt_text
    assert "halfway" in notice.prompt_text
    assert req.state == tr.STATE_EXPIRED
    late = tr.reply(request_id=req.id, content="too late", final=True, replier_thread_id=CALLEE, agent=agent)
    assert late.startswith("[Error]") and "expired" in late
    time.sleep(0.2)
    assert len(seen) == 1


def test_a_reply_before_any_window_means_no_reminder_and_no_nudge(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    tr.reply(request_id=req.id, content="quick", final=True, replier_thread_id=CALLEE, agent=agent)
    _wait_until(lambda: len(seen) == 1)

    assert tr.unreplied_request_reminder(CALLEE) is None
    assert tr.sweep_stale_requests(agent, now=req.opened_at + tr.REQUEST_EXPIRY_SECONDS + 1) == 0
    time.sleep(0.2)
    assert len(seen) == 1


def test_sweep_evicts_closed_records_after_the_retention_window(monkeypatch):
    agent = _Agent()
    _capture_deliveries(monkeypatch)
    req = _open()
    tr.reply(request_id=req.id, content="done", final=True, replier_thread_id=CALLEE, agent=agent)
    assert tr.get_request(req.id) is not None
    tr.sweep_stale_requests(agent, now=time.time() + tr.CLOSED_RETENTION_SECONDS + 1)
    assert tr.get_request(req.id) is None


# --- 27: the sweep rides the API app in both shapes -----------------------------------------------


def test_the_stale_request_sweep_is_registered_on_the_api_app(monkeypatch):
    import inspect

    from nymeria.triggers import api as triggers_api

    registered: list[dict] = []
    monkeypatch.setattr(
        triggers_api, "_register_periodic_task", lambda app, **kwargs: registered.append(kwargs)
    )
    triggers_api._register_stale_request_sweep_lifecycle(SimpleNamespace(), agent_getter=lambda: None)

    assert len(registered) == 1
    assert registered[0]["state_prefix"] == "stale_request_sweep"
    assert registered[0]["interval_seconds"] == tr.REQUEST_SWEEP_INTERVAL_SECONDS
    # Wired unconditionally beside the approval sweeps (the worker's Ticker has
    # no agent): a live, unconditional call in the app factory body, not a
    # mention in a comment.
    import ast

    tree = ast.parse(inspect.getsource(triggers_api))
    live_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_register_stale_request_sweep_lifecycle"
    ]
    assert len(live_calls) == 1
    assert {kw.arg for kw in live_calls[0].keywords} == {"agent_getter"}
    enclosing = [
        fn
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(node is live_calls[0] for node in ast.walk(fn))
    ]
    innermost = min(enclosing, key=lambda fn: (fn.end_lineno or 0) - fn.lineno)
    assert any(
        isinstance(stmt, ast.Expr) and stmt.value is live_calls[0] for stmt in innermost.body
    ), "the sweep registration must be a top-level statement of the factory, not conditional"


# --- 29: durability ----------------------------------------------------------------------------


def test_open_requests_survive_a_process_restart_and_closed_ones_leave_the_store(tmp_path, monkeypatch):
    monkeypatch.setattr("nymeria.config.get_settings", lambda: SimpleNamespace(data_dir=tmp_path, callable_wait_max_seconds=600))
    tr.reset_for_tests(durable=True)
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    tr.reply(request_id=req.id, content="progress", final=False, replier_thread_id=CALLEE, agent=agent)
    record_path = tmp_path / "thread_requests" / f"{req.id}.json"
    assert record_path.exists()

    # "Restart": the in-memory ledger is gone; the record is reloaded on first use.
    tr.reset_for_tests(durable=True)
    tr._loaded = False
    reloaded = tr.get_request(req.id)
    assert reloaded is not None and reloaded.caller_thread_id == CALLER
    assert [r.get("content") for r in reloaded.replies] == ["progress"]

    result = tr.reply(request_id=req.id, content="final after restart", final=True, replier_thread_id=CALLEE, agent=agent)
    assert result.startswith("[Replied]")
    _wait_until(lambda: len(seen) == 1)
    assert "final after restart" in seen[0].prompt_text
    _wait_until(lambda: not record_path.exists())  # gone once the wake-up is out


def test_a_reply_undelivered_at_restart_is_delivered_by_the_sweep(tmp_path, monkeypatch):
    """The closed record stays on disk until its wake-up prompt is out: a
    reply queued behind a busy caller when the process died is re-delivered
    after the restart instead of being lost."""
    monkeypatch.setattr("nymeria.config.get_settings", lambda: SimpleNamespace(data_dir=tmp_path, callable_wait_max_seconds=600))
    tr.reset_for_tests(durable=True)
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    req = _open()
    record_path = tmp_path / "thread_requests" / f"{req.id}.json"
    # The delivery worker never runs (the process died right after the reply closed the record).
    monkeypatch.setattr(tr, "_start_delivery", lambda agent, req, *, kind: None)
    tr.reply(request_id=req.id, content="final before the crash", final=True, replier_thread_id=CALLEE, agent=agent)
    assert record_path.exists() and req.delivered_via is None
    monkeypatch.undo()
    monkeypatch.setattr("nymeria.config.get_settings", lambda: SimpleNamespace(data_dir=tmp_path, callable_wait_max_seconds=600))
    seen = _capture_deliveries(monkeypatch)

    tr.reset_for_tests(durable=True)
    tr._loaded = False
    reloaded = tr.get_request(req.id)
    assert reloaded is not None and reloaded.state == tr.STATE_REPLIED
    assert reloaded.final_reply == "final before the crash" and reloaded.delivered_via is None
    assert tr.unreplied_request_reminder(CALLEE) is None  # closed: nothing owed
    late = tr.reply(request_id=req.id, content="again", final=True, replier_thread_id=CALLEE, agent=agent)
    assert late.startswith("[Error]") and "closed" in late

    assert tr.sweep_stale_requests(agent) == 1
    _wait_until(lambda: len(seen) == 1)
    assert "final before the crash" in seen[0].prompt_text
    _wait_until(lambda: not record_path.exists())
    assert tr.sweep_stale_requests(agent) == 0  # delivered once


# --- 30, 32: seed membership and capability-loss warnings ------------------------------------------


def test_the_two_tools_are_seed_tools_in_a_fresh_profile():
    from nymeria.tools import fresh_default_thread_tool_names, seed_tool_names

    assert {"reply_to_thread", "wait_for_reply"} <= set(seed_tool_names())
    assert {"reply_to_thread", "wait_for_reply"} <= set(fresh_default_thread_tool_names())


def test_capability_loss_warning_names_what_stops_working():
    from nymeria.tools.metadata import capability_loss_warning

    assert capability_loss_warning(["bash_execute"]) is None
    assert capability_loss_warning([]) is None
    reply = capability_loss_warning(["reply_to_thread"])
    assert reply is not None and "cannot answer requests other threads make" in reply
    both = capability_loss_warning(["wait_for_reply", "reply_to_thread", "notify"])
    assert both is not None and "cannot wait inline" in both and "cannot answer requests" in both


# --- the tools themselves ------------------------------------------------------------------------


def test_reply_tool_resolves_the_replier_from_its_thread_context(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    set_current_agent(agent)
    req = _open()

    wrong = reply_to_thread.invoke({"request_id": req.id, "content": "x"}, config=_config(OTHER))
    right = reply_to_thread.invoke({"request_id": req.id, "content": "the answer"}, config=_config(CALLEE))

    assert wrong.startswith("[Error]") and "not addressed" in wrong
    assert right.startswith("[Replied]")
    _wait_until(lambda: len(seen) == 1)
    assert "the answer" in seen[0].prompt_text
    assert sorted(reply_to_thread.args.keys()) == ["content", "final", "request_id"]


def test_wait_tool_holds_the_abort_cascade_edge_only_while_waiting(monkeypatch):
    agent = _Agent()
    _capture_deliveries(monkeypatch)
    set_current_agent(agent)
    req = _open()

    status = wait_for_reply.invoke({"request_id": req.id, "timeout_seconds": 0}, config=_config(CALLER))
    assert status.startswith("[Waiting]")
    assert agent.registered == []  # a status check registers nothing

    result = wait_for_reply.invoke({"request_id": req.id, "timeout_seconds": 1}, config=_config(CALLER))
    assert result.startswith("[Waiting]")
    assert agent.registered == [(CALLER, CALLEE)]
    assert agent.unregistered == [(CALLER, CALLEE)]
    assert sorted(wait_for_reply.args.keys()) == ["request_id", "steps", "timeout_seconds"]
    assert wait_for_reply.metadata["inline_wait_timeout"]({"timeout_seconds": 100}) == 100.0 + tr.WAIT_KILL_MARGIN_SECONDS
    assert wait_for_reply.metadata["inline_wait_timeout"]({}) is None


def test_a_waited_call_returns_only_the_outcome_once_the_request_is_closed():
    """The receipt tells the caller how to wait or check progress; once the wait
    itself closed the request (reply landed, or it failed) those instructions are
    stale and would invite a redundant wait, so only the outcome is returned."""
    agent = _Agent()
    receipt = "[Requested]: request_id=req-x target=Helper. Helper is working on it."

    still_open = _open()
    text = tr.waited_result(still_open, receipt, "[Waiting]: no final reply yet")
    assert text.startswith(receipt) and text.rstrip().endswith("[Waiting]: no final reply yet")

    replied = _open()
    tr.reply(request_id=replied.id, content="42", final=True, replier_thread_id=CALLEE, agent=agent)
    text = tr.waited_result(replied, receipt, tr.format_reply_inline(replied))
    assert text.startswith("[Reply from Helper] (request_id=" + replied.id)
    assert "[Requested]" not in text and text.rstrip().endswith("42")

    failed = _open()
    tr.fail_request(failed, "boom", agent)
    text = tr.waited_result(failed, receipt, "[NoReply]: request failed: boom")
    assert text == "[NoReply]: request failed: boom"


# --- the wake-up turn belongs to the caller, never to the callee's stream --------------


class _StreamingAgent(_Agent):
    """An agent whose ``astream`` is the caller's wake-up turn: it drives a
    LangChain runnable (as the real graph would) and yields its own chunk."""

    def __init__(self):
        super().__init__()
        self.turns: list[dict] = []

    async def astream(self, **kwargs):
        from langchain_core.runnables import RunnableLambda

        self.turns.append(kwargs)
        kwargs["_on_turn_started"]()
        graph = RunnableLambda(lambda x: f"Delivered: {x}", name="caller-wake-up-graph")
        text = await graph.ainvoke("relayed")
        yield {"type": "response", "content": text}


def test_the_reply_wake_up_turn_streams_to_the_caller_and_never_into_the_callees_turn():
    """The callee replies from inside its reply_to_thread tool call, a LangChain
    run the callee's turn is streaming via astream_events. The caller is idle, so
    the reply wakes it with a fresh turn on a worker that copies the callee's
    context. That turn's output must land on the CALLER's thread only: the
    callee's run must see none of it (2026-09-09: the caller's "Delivered, ..."
    was token-spliced into the callee's Telegram message)."""
    import asyncio
    import json

    from langchain_core.runnables import RunnableLambda

    from nymeria.core.turn_stream_buffer import (
        get_turn_stream_registry,
        reset_turn_stream_registry,
    )

    reset_turn_stream_registry()
    agent = _StreamingAgent()
    req = _open()
    outcome: dict = {}

    def callee_tool_call(_input):
        outcome["result"] = tr.reply(
            request_id=req.id, content="done", final=True, replier_thread_id=CALLEE, agent=agent
        )
        _wait_until(lambda: agent.turns and not req.delivering)
        return "replied"

    callee_run = RunnableLambda(callee_tool_call, name="reply_to_thread")

    async def stream_callee_turn():
        return [event async for event in callee_run.astream_events("go", version="v2")]

    try:
        callee_events = asyncio.run(stream_callee_turn())

        assert outcome["result"].startswith("[Replied]") and "new prompt" in outcome["result"]
        # The wake-up turn ran on the caller's thread with the reply prompt...
        assert [turn["thread_id"] for turn in agent.turns] == [CALLER]
        assert "[Reply from Helper]" in agent.turns[0]["message"]
        # ...and its output landed on the caller's own turn stream...
        caller_stream = get_turn_stream_registry().get(CALLER)
        assert caller_stream is not None
        buffered = [json.loads(payload) for _, payload in caller_stream._entries]
        texts = [event.get("content") for event in buffered if event.get("type") == "response"]
        assert texts == ["Delivered: relayed"]
        assert all(event.get("thread_id") == CALLER for event in buffered)
        # ...while the callee's own run saw nothing of it.
        assert {event["name"] for event in callee_events} == {"reply_to_thread"}
    finally:
        reset_turn_stream_registry()
