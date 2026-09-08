"""The callable-thread tool as a REQUEST (backlog #357), at the tool boundary.

Written from ``tmp/request-reply-plan.md`` behaviours 1, 3, 6, 11, 12, 16 and
19-21: a call returns a ``[Requested]`` receipt at once and the callee's turn
starts on a worker carrying the ``[Request Metadata]`` block; ``wait_seconds``
returns the reply inline when it lands in time, else a ``[Waiting]`` status
with the request left open (the reply then wakes the caller once); a call
with no calling thread keeps the synchronous form; a thread cannot request
work from itself; a wait cannot be combined with a schedule; and the tool
declares its inline wait for the tool node's per-call kill.

The callee's turn is faked at ``thread_agent_executor._run_callable_stream``
(the worker's target), so what reaches it and what it may do (reply, or not)
is under test control.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from nymeria.agents.tool_factory import create_callable_thread_tool
from nymeria.core import completion_delivery as cd
from nymeria.core import thread_agent_executor as ex
from nymeria.core import thread_requests as tr
from nymeria.core.agent import set_current_agent
from nymeria.core.pending_prompt_queue import reset_pending_queue_for_tests
from nymeria.core.thread_config import ThreadConfig
from nymeria.core.thread_lock_manager import ThreadLockManager

CALLER = "caller-thread"
HELPER = "helper-thread"


class _Repo:
    def get_thread_owner(self, thread_id):
        return "owner"

    def get_user_by_id(self, user_id):
        return SimpleNamespace(role="admin")


class _Agent:
    def __init__(self):
        self._thread_locks = ThreadLockManager()
        self.accounts_repo = _Repo()
        self.thread_config_manager = SimpleNamespace(get_config=self._get_config)
        self.thread_metadata_manager = SimpleNamespace(
            get_thread=lambda uid, tid: SimpleNamespace(title="Coordinator")
        )
        self.registered: list[tuple[str, str]] = []
        self.unregistered: list[tuple[str, str]] = []

    @staticmethod
    def _get_config(thread_id):
        if thread_id == HELPER:
            return ThreadConfig(
                thread_id=HELPER,
                callable=True,
                callable_name="Helper",
                enabled_tools=["reply_to_thread"],
            )
        return None

    def register_callable_invocation(self, parent, child):
        self.registered.append((parent, child))

    def unregister_callable_invocation(self, parent, child):
        self.unregistered.append((parent, child))


def _request_id(text: str) -> str:
    """The request id named in a tool result (followed by a space in a receipt,
    by a comma in an inline reply block)."""
    return text.split("request_id=")[1].split(",")[0].split()[0]


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), "condition not met in time"


@pytest.fixture(autouse=True)
def _fresh():
    tr.reset_for_tests()
    reset_pending_queue_for_tests()
    yield
    tr.reset_for_tests()
    reset_pending_queue_for_tests()
    set_current_agent(None)


@pytest.fixture
def agent():
    a = _Agent()
    set_current_agent(a)
    return a


@pytest.fixture
def helper_tool():
    return create_callable_thread_tool(
        ThreadConfig(thread_id=HELPER, callable=True, callable_name="Helper")
    )


def _fake_callee(monkeypatch, *, reply_with=None, delay=0.1):
    """Stand in for the callee's turn on the worker: record what it was
    handed and, optionally, reply the way its reply_to_thread tool would."""
    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        if reply_with is not None:
            time.sleep(delay)
            request_id = kwargs["event_metadata"]["request_id"]
            tr.reply(
                request_id=request_id,
                content=reply_with,
                final=True,
                replier_thread_id=kwargs["thread_id"],
                agent=kwargs["agent"],
            )
        return ""

    monkeypatch.setattr(ex, "_run_callable_stream", fake)
    return calls


def _capture_deliveries(monkeypatch) -> list:
    seen: list = []
    monkeypatch.setattr(cd, "fire_autonomous_turn", lambda agent, d: seen.append(d))
    return seen


def _config(thread_id=CALLER):
    configurable = {"user_id": "owner"}
    if thread_id:
        configurable["thread_id"] = thread_id
    return {"configurable": configurable}


# --- 1, 3: receipt now, contract to the callee, work on a worker -------------------


def test_a_call_returns_a_receipt_at_once_and_the_callee_gets_the_request_block(agent, helper_tool, monkeypatch):
    calls = _fake_callee(monkeypatch)

    receipt = helper_tool.invoke({"task": "find the staff directory"}, config=_config())

    assert receipt.startswith("[Requested]: request_id=req-")
    assert "target=Helper" in receipt and "you may end your turn" in receipt
    req = tr.requests_awaited_by(CALLER)[0]
    assert req.id in receipt and req.target_thread_id == HELPER
    assert req.caller_name == "Coordinator" and req.task == "find the staff directory"
    assert req.target_can_reply is True and "[Warning]" not in receipt

    _wait_until(lambda: calls)
    handed = calls[0]
    assert handed["thread_id"] == HELPER and handed["callable_name"] == "Helper"
    assert handed["task"].startswith("[Request Metadata]\n")
    assert f"request_id: {req.id}" in handed["task"]
    assert f"source_thread_id: {CALLER}" in handed["task"]
    assert "source_thread_name: Coordinator" in handed["task"]
    assert handed["task"].endswith("\n\nfind the staff directory")
    assert handed["task_id"] == f"{req.id}-Helper" == req.task_id
    assert handed["event_metadata"]["source"] == "request"
    assert handed["event_metadata"]["request_id"] == req.id
    assert handed["is_self_invoke"] is True
    assert handed["progress_sink"] == req.progress.observe
    assert agent.registered == []  # nobody waited: no abort-cascade edge


def test_a_callee_without_reply_to_thread_is_flagged_on_the_receipt(agent, helper_tool, monkeypatch):
    _fake_callee(monkeypatch)
    monkeypatch.setattr(
        agent.thread_config_manager,
        "get_config",
        lambda tid: ThreadConfig(
            thread_id=HELPER, callable=True, callable_name="Helper",
            disabled_tools=["reply_to_thread"],
        ),
    )

    receipt = helper_tool.invoke({"task": "t"}, config=_config())

    assert "[Warning]: Helper does not have reply_to_thread enabled" in receipt
    assert tr.requests_awaited_by(CALLER)[0].target_can_reply is False


# --- 11, 12: the wait sugar --------------------------------------------------------


def test_wait_seconds_returns_the_reply_inline_when_it_lands_in_time(agent, helper_tool, monkeypatch):
    _fake_callee(monkeypatch, reply_with="Staff directory: /nas/HR/staff.xlsx")
    seen = _capture_deliveries(monkeypatch)

    result = helper_tool.invoke({"task": "find the directory", "wait_seconds": 5}, config=_config())

    # The reply block REPLACES the receipt: its "end your turn / wait again"
    # instructions are stale once the request is closed.
    assert result.startswith("[Reply from Helper] (request_id=req-")
    assert "[Requested]" not in result and "wait_for_reply" not in result
    assert result.rstrip().endswith("Staff directory: /nas/HR/staff.xlsx")
    req = tr.get_request(_request_id(result))
    assert req is not None and req.state == tr.STATE_REPLIED and req.delivered_via == "inline"
    assert agent.registered == [(CALLER, HELPER)] and agent.unregistered == [(CALLER, HELPER)]
    time.sleep(0.2)
    assert seen == []  # inline delivery is the only delivery


def test_wait_seconds_lapsing_leaves_the_request_open_and_the_reply_wakes_the_caller_once(agent, helper_tool, monkeypatch):
    calls = _fake_callee(monkeypatch)
    seen = _capture_deliveries(monkeypatch)

    result = helper_tool.invoke({"task": "slow task", "wait_seconds": 1}, config=_config())

    assert "[Requested]" in result and "[Waiting]" in result and "waited 1s" in result
    req = tr.requests_awaited_by(CALLER)[0]
    assert req.state == tr.STATE_OPEN and req.waiter is None
    assert agent.unregistered == [(CALLER, HELPER)]  # the edge went with the wait
    _wait_until(lambda: calls)
    tr.reply(request_id=req.id, content="late answer", final=True, replier_thread_id=HELPER, agent=agent)
    _wait_until(lambda: len(seen) == 1)
    assert seen[0].thread_id == CALLER and "late answer" in seen[0].prompt_text
    time.sleep(0.2)
    assert len(seen) == 1


def test_a_callee_whose_turn_dies_gives_the_caller_a_failure_notice_now(agent, helper_tool, monkeypatch):
    calls: list[dict] = []

    def dying_callee(**kwargs):
        calls.append(kwargs)
        return ex._build_error_result(code="thread_execution_failed", message="Helper execution failed: boom")

    monkeypatch.setattr(ex, "_run_callable_stream", dying_callee)
    seen = _capture_deliveries(monkeypatch)

    receipt = helper_tool.invoke({"task": "doomed"}, config=_config())

    assert receipt.startswith("[Requested]")
    _wait_until(lambda: len(seen) == 1)
    assert seen[0].thread_id == CALLER
    assert seen[0].prompt_text.startswith("[NoReply]") and "boom" in seen[0].prompt_text
    req = tr.get_request(receipt.split("request_id=")[1].split()[0])
    assert req is not None and req.state == tr.STATE_FAILED
    time.sleep(0.2)
    assert len(seen) == 1


def test_a_dispatch_that_fails_leaves_no_record_behind(agent, helper_tool, monkeypatch):
    _fake_callee(monkeypatch)
    import threading

    def broken_start(self):
        raise RuntimeError("no threads left")

    monkeypatch.setattr(threading.Thread, "start", broken_start)

    result = helper_tool.invoke({"task": "t", "wait_seconds": 5}, config=_config())

    assert result.startswith("[Error]") and "no threads left" in result
    assert tr.open_requests() == []  # nothing owed, nudged, or expired for a call that never ran


def test_the_callee_worker_runs_in_a_copy_of_the_callers_context(agent, helper_tool, monkeypatch):
    """The hook engine's re-entrance depth and LangChain's callback resets are
    ContextVars; a bare worker thread would read them back as defaults."""
    import contextvars

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("request_test_marker", default="unset")
    seen_values: list[str] = []

    def fake(**kwargs):
        seen_values.append(marker.get())
        return ""

    monkeypatch.setattr(ex, "_run_callable_stream", fake)
    marker.set("from-the-caller")

    helper_tool.invoke({"task": "t"}, config=_config())

    _wait_until(lambda: seen_values)
    assert seen_values == ["from-the-caller"]


def test_a_busy_callee_gets_a_queued_receipt(agent, helper_tool, monkeypatch):
    _fake_callee(monkeypatch)
    lock = agent._thread_locks.get_lock(HELPER)
    lock.acquire()
    try:
        receipt = helper_tool.invoke({"task": "t"}, config=_config())
    finally:
        lock.release()
    assert receipt.startswith("[Requested]") and "queued behind its current turn" in receipt


def test_a_callee_missing_the_reply_tool_in_its_defaults_is_flagged(agent, helper_tool, monkeypatch):
    """The advisory check reads the target's effective tool set: an explicit
    per-thread list, else the owner's default_thread_tools."""
    _fake_callee(monkeypatch)
    monkeypatch.setattr(
        agent.thread_config_manager,
        "get_config",
        lambda tid: ThreadConfig(thread_id=HELPER, callable=True, callable_name="Helper"),
    )
    agent.profile_manager = SimpleNamespace(
        get_profile=lambda uid: SimpleNamespace(
            tool_preferences=SimpleNamespace(default_thread_tools=["bash_execute", "file_read"])
        )
    )

    without = helper_tool.invoke({"task": "t"}, config=_config())
    agent.profile_manager = SimpleNamespace(
        get_profile=lambda uid: SimpleNamespace(
            tool_preferences=SimpleNamespace(default_thread_tools=["bash_execute", "reply_to_thread"])
        )
    )
    with_tool = helper_tool.invoke({"task": "t"}, config=_config())

    assert "[Warning]: Helper does not have reply_to_thread enabled" in without
    assert "[Warning]" not in with_tool


# --- 6, 19, 20, 21: guards --------------------------------------------------------------


def test_a_call_with_no_calling_thread_keeps_the_synchronous_form(agent, helper_tool, monkeypatch):
    calls = _fake_callee(monkeypatch)
    monkeypatch.setattr(ex, "invoke", lambda *args, **kwargs: "the callee's text")

    result = helper_tool.invoke({"task": "t"}, config=_config(thread_id=None))

    assert result == "the callee's text"
    assert tr.open_requests() == [] and calls == []
    # Without a thread there is nowhere for a scheduled reply to land, and a
    # busy check still honours if_busy.
    scheduled = helper_tool.invoke({"task": "t", "scheduled_for": "5m"}, config=_config(thread_id=None))
    assert scheduled.startswith("[Error]") and "scheduled_for needs a calling thread" in scheduled
    lock = agent._thread_locks.get_lock(HELPER)
    lock.acquire()
    try:
        busy = helper_tool.invoke({"task": "t", "if_busy": "error"}, config=_config(thread_id=None))
    finally:
        lock.release()
    assert busy.startswith("[Busy]")


def test_a_thread_cannot_request_work_from_itself(agent, helper_tool, monkeypatch):
    calls = _fake_callee(monkeypatch)

    result = helper_tool.invoke({"task": "t"}, config=_config(thread_id=HELPER))

    assert result.startswith("[Error]") and "cannot request work from itself" in result
    assert tr.open_requests() == [] and calls == []


def test_argument_validation(agent, helper_tool, monkeypatch):
    calls = _fake_callee(monkeypatch)

    combined = helper_tool.invoke({"task": "t", "wait_seconds": 5, "scheduled_for": "5m"}, config=_config())
    negative = helper_tool.invoke({"task": "t", "wait_seconds": -1}, config=_config())

    assert combined.startswith("[Error]") and "cannot be combined with scheduled_for" in combined
    assert negative.startswith("[Error]") and "wait_seconds" in negative
    # if_busy is a Literal in the schema: a bad value never reaches the body.
    with pytest.raises(Exception, match="if_busy"):
        helper_tool.invoke({"task": "t", "if_busy": "explode"}, config=_config())
    assert tr.open_requests() == [] and calls == []


def test_a_reply_landing_before_the_wait_starts_is_still_returned_inline(agent, helper_tool, monkeypatch):
    """The waiter is registered before the callee is dispatched, so even an
    instant reply (delay 0: the worker races the caller's wait) is inline and
    is not ALSO sent as a wake-up prompt."""
    _fake_callee(monkeypatch, reply_with="instant", delay=0.0)
    seen = _capture_deliveries(monkeypatch)

    result = helper_tool.invoke({"task": "quick one", "wait_seconds": 5}, config=_config())

    assert "[Reply from Helper]" in result and result.rstrip().endswith("instant")
    req = tr.requests_awaited_by(CALLER) or [tr.get_request(_request_id(result))]
    assert req[0] is not None and req[0].delivered_via == "inline"
    time.sleep(0.2)
    assert seen == []


def test_two_calls_are_two_requests(agent, helper_tool, monkeypatch):
    calls = _fake_callee(monkeypatch)

    first = helper_tool.invoke({"task": "first"}, config=_config())
    second = helper_tool.invoke({"task": "second"}, config=_config())

    ids = {r.id for r in tr.requests_awaited_by(CALLER)}
    assert len(ids) == 2
    assert all(rid in first or rid in second for rid in ids)
    _wait_until(lambda: len(calls) == 2)
    assert {c["task"].split("\n\n", 1)[1] for c in calls} == {"first", "second"}


# --- 16, 17: what the model is told, and what the tool node is told -----------------------------


def test_the_description_states_the_request_contract(helper_tool):
    text = helper_tool.description
    assert "[Requested]" in text and "request_id" in text
    assert "wait_seconds" in text and "wait_for_reply" in text
    assert "Nothing the thread says outside reply_to_thread is delivered to you" in text
    assert sorted(helper_tool.args) == ["if_busy", "scheduled_for", "task", "wait_seconds"]
    assert "mode" not in helper_tool.description


def test_the_tool_declares_its_inline_wait_for_the_tool_node(helper_tool):
    derive = helper_tool.metadata["inline_wait_timeout"]
    assert derive({"task": "t", "wait_seconds": 120}) == 120.0 + tr.WAIT_KILL_MARGIN_SECONDS
    assert derive({"task": "t"}) is None
    assert derive({"task": "t", "wait_seconds": 0}) is None
