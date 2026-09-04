"""Tests for ask continuations (core/thread_agent_executor.py "Ask continuations").

A blocking callable ask that outruns its inline wait budget returns a
``[StillWorking]`` receipt instead of dying at the tool timeout; the callee
keeps running and its output wakes the caller thread later. Written from the
expected-behaviors list in the pass plan, not from the implementation.
"""

from __future__ import annotations

import contextvars
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from nymeria.agents.tool_factory import create_callable_thread_tool
from nymeria.core import completion_delivery as cd
from nymeria.core import thread_agent_executor as ex
from nymeria.core.agent import set_current_agent
from nymeria.core.pending_prompt_queue import (
    get_pending_queue,
    notify_batch_absorbed,
    reset_pending_queue_for_tests,
)
from nymeria.core.stream_bridge import StreamCollection
from nymeria.core.thread_config import ThreadConfig
from nymeria.core.thread_lock_manager import ThreadLockManager
from nymeria.core.todo_manager import TodoManager
from nymeria.core.todo_schedule_db import TodoScheduleDB

CALLER = "caller-thread"
CALLEE = "callee-thread"

# A stand-in for the hook engine's ContextVar depth counter: the worker must
# see the dispatching thread's value, not the ContextVar default.
_ctx_probe: contextvars.ContextVar[str] = contextvars.ContextVar("cont_probe", default="unset")


class _Repo:
    def __init__(self, owner: str | None = "owner"):
        self.owner = owner

    def get_thread_owner(self, thread_id: str):
        return self.owner

    def get_user_by_id(self, user_id: str):
        return SimpleNamespace(id=user_id, role="user")


class _Configs:
    def get_config(self, thread_id: str):
        if thread_id != CALLEE:
            return None
        return ThreadConfig(thread_id=thread_id, callable=True, callable_name="Helper")


class _Agent:
    def __init__(self, tmp_path: Path | None = None, owner: str | None = "owner"):
        self._thread_locks = ThreadLockManager()
        self.accounts_repo = _Repo(owner)
        self.thread_config_manager = _Configs()
        self.registered: list[tuple[str, str]] = []
        self.unregistered: list[tuple[str, str]] = []
        self._active_callable_invocations: dict[str, set[str]] = {}
        self._invocations_lock = threading.Lock()
        if tmp_path is not None:
            self.todo_manager = TodoManager(tmp_path)
            self._schedule_db = TodoScheduleDB(tmp_path / "todo_schedule.db")

    def register_callable_invocation(self, parent: str, child: str) -> None:
        self.registered.append((parent, child))
        self._active_callable_invocations.setdefault(parent, set()).add(child)

    def unregister_callable_invocation(self, parent: str, child: str) -> None:
        self.unregistered.append((parent, child))
        self._active_callable_invocations.get(parent, set()).discard(child)

    def is_thread_busy(self, thread_id: str) -> bool:
        return self._thread_locks.is_thread_busy(thread_id)

    def is_ancestor_invocation(self, child: str, target: str) -> bool:
        from nymeria.core.agent_callable_lifecycle import is_ancestor_invocation

        return is_ancestor_invocation(cast(Any, self), child, target)


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), "condition not met in time"


def _no_continuations() -> bool:
    with ex._continuations_lock:
        return not ex._continuations


@pytest.fixture(autouse=True)
def _fresh_state():
    ex.reset_continuations_for_tests()
    reset_pending_queue_for_tests()
    yield
    _wait_until(_no_continuations)
    reset_pending_queue_for_tests()
    set_current_agent(None)


def _capture_deliveries(monkeypatch) -> list[cd.CompletionDelivery]:
    seen: list[cd.CompletionDelivery] = []
    monkeypatch.setattr(cd, "fire_autonomous_turn", lambda agent, d: seen.append(d))
    return seen


def _blocking_run(release: threading.Event, *, text="final answer", chunks=()):
    def run(progress_sink):
        for chunk in chunks:
            progress_sink(chunk)
        release.wait(10)
        return text

    return run


def _kwargs(agent, **overrides):
    base = dict(
        agent=agent,
        callable_name="Helper",
        target_thread_id=CALLEE,
        caller_thread_id=CALLER,
        caller_user_id="owner",
        task="find the staff directory on the NAS",
        task_id="callable-Helper-abc",
        follow_up_tool="Helper",
        wait_budget=0.3,
    )
    base.update(overrides)
    return base


# --- 11: budget derivation -----------------------------------------------------


@pytest.mark.parametrize("tool_timeout,expected", [(300, 270.0), (900, 870.0), (30, 20.0)])
def test_wait_budget_stays_under_the_tool_timeout(tool_timeout, expected):
    assert ex.ask_wait_budget(tool_timeout) == expected


# --- 1: inline when the run finishes in time ---------------------------------


def test_run_finishing_within_budget_returns_inline_and_delivers_nothing(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)

    result = ex.run_with_continuation(**_kwargs(agent, run=lambda sink: "final answer"))

    assert result == "final answer"
    assert ex.STILL_WORKING_PREFIX not in result
    _wait_until(_no_continuations)
    time.sleep(0.2)
    assert seen == []
    assert get_pending_queue().drain(CALLER) == []


# --- 2: the receipt -------------------------------------------------------------


def test_run_outliving_budget_returns_receipt_with_progress(monkeypatch):
    agent = _Agent()
    _capture_deliveries(monkeypatch)
    release = threading.Event()
    chunks = [
        {"type": "tool_call", "name": "fetch_url_nymeria"},
        {"type": "tool_result", "name": "fetch_url_nymeria"},
        {"type": "tool_call", "name": "file_read"},
    ]
    started = time.time()
    try:
        result = ex.run_with_continuation(
            **_kwargs(agent, run=_blocking_run(release, chunks=chunks))
        )
    finally:
        release.set()

    assert time.time() - started < 3.0
    assert result.startswith(ex.STILL_WORKING_PREFIX)
    assert "Helper" in result
    assert "continuation_id=cont-" in result
    assert f"target_thread_id={CALLEE}" in result
    assert "2 tool call(s) so far, last file_read" in result
    assert 'mode="handoff"' in result
    assert "safely end your turn" in result


def test_receipt_reports_queued_behind_holder_when_nothing_ran_yet(monkeypatch):
    agent = _Agent()
    _capture_deliveries(monkeypatch)
    release = threading.Event()
    chunks = [{"type": "queued"}, {"type": "prompt_queued", "position": 1}]
    try:
        result = ex.run_with_continuation(
            **_kwargs(agent, run=_blocking_run(release, chunks=chunks))
        )
    finally:
        release.set()

    assert result.startswith(ex.STILL_WORKING_PREFIX)
    assert "still queued behind the thread's current turn" in result


# --- 3: idle caller -> autonomous wake-up turn ----------------------------------


def test_detached_result_fires_wake_up_turn_on_idle_caller(monkeypatch):
    agent = _Agent()
    events: list[tuple[str, str, str, str, dict]] = []
    seen_kwargs: dict = {}

    def fake_publish_autonomous_event(event_type, thread_id, user_id, task_id, data):
        events.append((event_type, thread_id, user_id, task_id, data))

    def fake_stream_and_collect(agent_arg, *, astream_kwargs, on_chunk, error_message_factory):
        seen_kwargs.update(astream_kwargs)
        on_chunk({"type": "response", "content": "relayed"}, SimpleNamespace())
        return SimpleNamespace(
            iteration_limit_hit=False, response_text=lambda fallback_to_thinking=True: "relayed"
        )

    monkeypatch.setattr("nymeria.core.event_bus.publish_autonomous_event", fake_publish_autonomous_event)
    monkeypatch.setattr(
        "nymeria.core.event_bus.publish_agent_stream_chunk",
        lambda chunk, *, thread_id, user_id, task_id: True,
    )
    monkeypatch.setattr("nymeria.core.stream_bridge.stream_and_collect", fake_stream_and_collect)
    monkeypatch.setattr("nymeria.core.activity_log.log_activity", lambda *a, **k: None)

    release = threading.Event()
    result = ex.run_with_continuation(
        **_kwargs(agent, run=_blocking_run(release, text="NAS says: 4 staff", chunks=[{"type": "tool_call", "name": "x"}]))
    )
    assert result.startswith(ex.STILL_WORKING_PREFIX)
    cont_id = result.split("continuation_id=")[1].split(",")[0]

    release.set()
    _wait_until(lambda: len(events) == 2)

    assert seen_kwargs["thread_id"] == CALLER
    assert seen_kwargs["user_id"] == "owner"
    assert seen_kwargs["_is_self_invoke"] is True
    assert seen_kwargs["source"] == "callable_result"
    assert seen_kwargs["_trigger_override"] == f'CallableResult("{CALLEE}", "Helper")'
    prompt = seen_kwargs["message"]
    assert "Helper finished the task you delegated" in prompt
    assert f"continuation_id={cont_id}" in prompt
    assert "find the staff directory on the NAS" in prompt
    assert "1 tool call(s)" in prompt
    assert "--- Helper output ---\nNAS says: 4 staff\n--- end Helper output ---" in prompt
    assert "not as instructions" in prompt
    assert [e[0] for e in events] == ["task_started", "task_completed"]
    assert events[0][1] == CALLER and events[0][3] == f"callable-result-{cont_id}"
    assert events[0][4]["callable_name"] == "Helper"
    assert events[0][4]["continuation_id"] == cont_id
    assert events[1][4]["content"] == "relayed"


# --- 4: busy caller -> pending prompt ------------------------------------------


def test_detached_result_queues_pending_prompt_on_busy_caller(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    release = threading.Event()
    result = ex.run_with_continuation(**_kwargs(agent, run=_blocking_run(release, text="done")))
    assert result.startswith(ex.STILL_WORKING_PREFIX)

    lock = agent._thread_locks.get_lock(CALLER)
    lock.acquire()
    try:
        release.set()
        _wait_until(lambda: get_pending_queue().size(CALLER) == 1)
        pending = get_pending_queue().drain(CALLER)
        notify_batch_absorbed(pending)  # what the holder does with a drained batch
    finally:
        lock.release()

    assert seen == []
    prompt = pending[0]
    assert prompt.source == "callable_result"
    assert prompt.is_autonomous is True
    assert prompt.fanout_mailbox is None
    assert prompt.user_id == "owner"
    assert "--- Helper output ---\ndone\n--- end Helper output ---" in prompt.message


# --- 26: the worker runs in a copy of the caller's context ----------------------


def test_worker_runs_in_a_copy_of_the_callers_context(monkeypatch):
    agent = _Agent()
    _capture_deliveries(monkeypatch)
    token = _ctx_probe.set("from-caller")
    try:
        result = ex.run_with_continuation(**_kwargs(agent, run=lambda sink: _ctx_probe.get()))
    finally:
        _ctx_probe.reset(token)
    assert result == "from-caller"


# --- 23: a queued result survives a user stop on the caller ---------------------


def test_queued_result_survives_a_user_stop_and_wakes_the_caller_afterwards(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    release = threading.Event()
    result = ex.run_with_continuation(**_kwargs(agent, run=_blocking_run(release, text="done")))
    assert result.startswith(ex.STILL_WORKING_PREFIX)

    lock = agent._thread_locks.get_lock(CALLER)
    lock.acquire()
    try:
        release.set()
        _wait_until(lambda: get_pending_queue().size(CALLER) == 1)
        # A user /stop on the caller: user prompts are restored, the rest cleared.
        get_pending_queue().clear_with_restore(CALLER)
        time.sleep(0.05)
        assert seen == []
    finally:
        lock.release()

    _wait_until(lambda: len(seen) == 1)
    assert seen[0].thread_id == CALLER
    assert "--- Helper output ---\ndone\n--- end Helper output ---" in seen[0].prompt_text
    assert get_pending_queue().size(CALLER) == 0


# --- 5: stopped caller still delivered; foreign owner dropped --------------------


def test_stopped_caller_still_receives_the_result(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    release = threading.Event()
    result = ex.run_with_continuation(**_kwargs(agent, run=_blocking_run(release)))
    assert result.startswith(ex.STILL_WORKING_PREFIX)

    agent._thread_locks.signal_abort(CALLER)  # the user stopped the caller meanwhile
    release.set()
    _wait_until(lambda: len(seen) == 1)

    assert seen[0].thread_id == CALLER
    assert seen[0].drop_on_abort is False


def test_foreign_owned_caller_is_dropped(monkeypatch, caplog):
    agent = _Agent(owner="someone-else")
    seen = _capture_deliveries(monkeypatch)
    release = threading.Event()
    result = ex.run_with_continuation(**_kwargs(agent, run=_blocking_run(release)))
    assert result.startswith(ex.STILL_WORKING_PREFIX)

    release.set()
    _wait_until(_no_continuations)
    time.sleep(0.2)

    assert seen == []
    assert get_pending_queue().drain(CALLER) == []


# --- 6: exactly-once resolution ------------------------------------------------


def test_run_finishing_just_after_budget_is_delivered_exactly_once(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)

    def run(progress_sink):
        time.sleep(0.6)
        return "late"

    result = ex.run_with_continuation(**_kwargs(agent, run=run, wait_budget=0.2))

    assert result.startswith(ex.STILL_WORKING_PREFIX)
    _wait_until(lambda: len(seen) == 1)
    _wait_until(_no_continuations)
    time.sleep(0.3)
    assert len(seen) == 1
    assert "--- Helper output ---\nlate\n" in seen[0].prompt_text


# --- 12: a raising run still reaches the caller ---------------------------------


def test_raising_run_delivers_its_error_marker(monkeypatch):
    agent = _Agent()
    seen = _capture_deliveries(monkeypatch)
    release = threading.Event()

    def run(progress_sink):
        release.wait(10)
        raise RuntimeError("provider exploded")

    result = ex.run_with_continuation(**_kwargs(agent, run=run))
    assert result.startswith(ex.STILL_WORKING_PREFIX)

    release.set()
    _wait_until(lambda: len(seen) == 1)
    assert "Helper execution failed: provider exploded" in seen[0].prompt_text
    assert ex.ERROR_MARKER_PREFIX in seen[0].prompt_text


def test_raising_run_within_budget_returns_error_inline():
    agent = _Agent()

    def run(progress_sink):
        raise RuntimeError("boom")

    result = ex.run_with_continuation(**_kwargs(agent, run=run))

    assert result.startswith(ex.ERROR_MARKER_PREFIX)
    assert "Helper execution failed: boom" in result


# --- 8: interim annotation on the callee's own answer ---------------------------


def _fake_stream(monkeypatch, text: str):
    def fake_stream_and_collect(agent_arg, *, astream_kwargs, on_chunk, error_message_factory):
        collection = StreamCollection()
        collection.response_parts.append(text)
        on_chunk({"type": "response", "content": text}, collection)
        return collection

    monkeypatch.setattr(ex, "stream_and_collect", fake_stream_and_collect)
    monkeypatch.setattr(ex, "publish_autonomous_event", lambda **kwargs: None)
    monkeypatch.setattr(ex, "publish_agent_stream_chunk", lambda chunk, **kwargs: None)


def test_callee_answer_carries_interim_note_when_its_own_asks_are_outstanding(monkeypatch):
    agent = _Agent()
    _fake_stream(monkeypatch, "here is what I found")
    # The callee (CALLEE) itself asked two sub-threads that are still running.
    for name, target in (("WebResearch", "t-web"), ("NASLibrarian", "t-nas")):
        ex._register_continuation(
            ex.AskContinuation(
                id=f"cont-{name}",
                callable_name=name,
                target_thread_id=target,
                caller_thread_id=CALLEE,
                caller_user_id="owner",
                task="sub task",
                task_id="x",
            )
        )
    try:
        text = ex._run_callable_stream(
            agent=agent,
            thread_id=CALLEE,
            task="t",
            caller_user_id="owner",
            callable_name="Helper",
            task_id="callable-Helper-1",
            trigger_override=None,
            is_self_invoke=False,
        )
    finally:
        ex.reset_continuations_for_tests()

    assert text.startswith("here is what I found")
    assert "[Note: Helper ended this turn with 2 callable sub-task(s) still running (NASLibrarian, WebResearch)" in text
    assert "their results will wake Helper, not you" in text


def test_callee_answer_has_no_note_without_outstanding_asks(monkeypatch):
    agent = _Agent()
    _fake_stream(monkeypatch, "clean answer")

    text = ex._run_callable_stream(
        agent=agent,
        thread_id=CALLEE,
        task="t",
        caller_user_id="owner",
        callable_name="Helper",
        task_id="callable-Helper-1",
        trigger_override=None,
        is_self_invoke=False,
    )

    assert text == "clean answer"


def test_finished_continuations_do_not_count_as_outstanding():
    cont = ex.AskContinuation(
        id="cont-done",
        callable_name="X",
        target_thread_id="t-x",
        caller_thread_id=CALLEE,
        caller_user_id="owner",
        task="t",
        task_id="x",
    )
    ex._register_continuation(cont)
    try:
        assert [c.id for c in ex.outstanding_for_caller(CALLEE)] == ["cont-done"]
        cont.done.set()
        assert ex.outstanding_for_caller(CALLEE) == []
        assert ex.interim_note("Helper", []) is None
    finally:
        ex.reset_continuations_for_tests()


# --- 9: handoff metadata names an existing continuation ------------------------


def _owed_continuation() -> ex.AskContinuation:
    return ex.AskContinuation(
        id="cont-owed",
        callable_name="Helper",
        target_thread_id=CALLEE,
        caller_thread_id=CALLER,
        caller_user_id="owner",
        task="t",
        task_id="x",
    )


def _capture_handoff_run(monkeypatch):
    captured: dict = {}
    ran = threading.Event()

    def fake_run(**kwargs):
        captured.update(kwargs)
        captured["ctx_probe"] = _ctx_probe.get()
        ran.set()

    monkeypatch.setattr(ex, "_run_callable_stream", fake_run)
    return captured, ran


def test_immediate_handoff_to_a_thread_already_owed_a_result_says_so(monkeypatch):
    agent = _Agent()
    ex._register_continuation(_owed_continuation())
    set_current_agent(cast(Any, agent))
    captured, ran = _capture_handoff_run(monkeypatch)
    token = _ctx_probe.set("from-caller")
    try:
        result = ex.handoff(
            CALLEE, "also check the org chart", "owner", "Helper",
            caller_thread_id=CALLER, caller_name="Coordinator",
        )
        assert ran.wait(5)
    finally:
        _ctx_probe.reset(token)
        ex.reset_continuations_for_tests()

    assert result.startswith("[HandedOff]:")
    assert captured["ctx_probe"] == "from-caller"  # the handoff worker inherits context too
    task = captured["task"]
    assert "continuation cont-owed" in task
    # Conditional wording: absorbed into the ask's turn vs a fresh turn.
    assert "absorbed into the turn that ask started" in task
    assert "no callback is needed" in task
    assert "started a fresh turn instead" in task


def test_scheduled_handoff_never_carries_the_continuation_note(tmp_path: Path):
    agent = _Agent(tmp_path)
    ex._register_continuation(_owed_continuation())
    set_current_agent(cast(Any, agent))
    try:
        result = ex.handoff(
            CALLEE, "also check the org chart", "owner", "Helper",
            caller_thread_id=CALLER, caller_name="Coordinator", scheduled_for="30s",
        )
    finally:
        ex.reset_continuations_for_tests()

    assert result.startswith("[HandedOff]:")
    notes = agent.todo_manager.get_todos("owner").items[0].notes or ""
    assert "continuation" not in notes
    assert "NOT returned to the source thread automatically" in notes


def test_handoff_without_an_owed_result_carries_no_continuation_note(monkeypatch):
    agent = _Agent()
    set_current_agent(cast(Any, agent))
    captured, ran = _capture_handoff_run(monkeypatch)
    result = ex.handoff(
        CALLEE, "also check the org chart", "owner", "Helper",
        caller_thread_id=CALLER, caller_name="Coordinator",
    )
    assert ran.wait(5)

    assert result.startswith("[HandedOff]:")
    task = captured["task"]
    assert "continuation" not in task
    assert "NOT returned to the source thread automatically" in task


# --- 7 + 10: the callable tool routes asks and drops the edge on detach --------


def _tool():
    return create_callable_thread_tool(
        ThreadConfig(thread_id=CALLEE, callable=True, callable_name="Helper")
    )


def _config(thread_id: str | None):
    configurable: dict = {"user_id": "owner"}
    if thread_id:
        configurable["thread_id"] = thread_id
    return {"configurable": configurable}


def test_tool_ask_with_a_caller_thread_uses_the_continuation_and_drops_the_edge(monkeypatch):
    agent = _Agent()
    set_current_agent(cast(Any, agent))
    calls: list[dict] = []

    def fake_invoke_with_continuation(thread_id, task, user_id, name, **kwargs):
        calls.append({"thread_id": thread_id, "task": task, "user_id": user_id, "name": name, **kwargs})
        # The edge must be registered while the ask is inline...
        assert agent._active_callable_invocations.get(CALLER) == {CALLEE}
        return f"{ex.STILL_WORKING_PREFIX}: Helper has been working"

    monkeypatch.setattr(ex, "invoke_with_continuation", fake_invoke_with_continuation)
    monkeypatch.setattr(ex, "invoke", lambda *a, **k: pytest.fail("plain invoke must not run"))

    result = cast(Any, _tool()).func(task="do it", config=_config(CALLER))

    assert result.startswith(ex.STILL_WORKING_PREFIX)
    assert calls[0]["caller_thread_id"] == CALLER
    assert calls[0]["thread_id"] == CALLEE
    # ...and gone once the receipt is returned: the callee is a handoff target
    # now (no cascade-abort from the caller, no circular block on a callback).
    assert agent.unregistered == [(CALLER, CALLEE)]
    assert agent._active_callable_invocations.get(CALLER, set()) == set()
    assert agent.is_ancestor_invocation(CALLEE, CALLER) is False


def test_tool_ask_without_a_caller_thread_stays_plainly_blocking(monkeypatch):
    agent = _Agent()
    set_current_agent(cast(Any, agent))
    monkeypatch.setattr(
        ex, "invoke_with_continuation",
        lambda *a, **k: pytest.fail("no caller thread to wake: must not detach"),
    )
    monkeypatch.setattr(ex, "invoke", lambda thread_id, task, user_id, name, **k: "plain answer")

    result = cast(Any, _tool()).func(task="do it", config=_config(None))

    assert result == "plain answer"
    assert agent.registered == []
    assert _no_continuations()


def test_tool_description_states_the_still_working_contract():
    description = _tool().description
    assert "[StillWorking]" in description
    assert "delivered to you later" in description
    assert "handoff" in description


# --- 13: spawn_thread's initial prompt gets the same continuation ---------------


def _spawn_outliving_budget(monkeypatch, *, callable_tool_name):
    from nymeria.tools.spawn_thread import _invoke_spawned

    agent = _Agent()
    agent.thread_metadata_manager = SimpleNamespace(
        get_thread=lambda uid, tid: SimpleNamespace(title="Parent")
    )
    seen = _capture_deliveries(monkeypatch)
    monkeypatch.setattr(ex, "ask_wait_budget", lambda tool_timeout=None: 0.3)
    monkeypatch.setattr("nymeria.core.event_bus.publish_autonomous_event", lambda **k: None)
    monkeypatch.setattr(
        "nymeria.core.event_bus.publish_agent_stream_chunk",
        lambda chunk, *, thread_id, user_id, task_id: True,
    )
    release = threading.Event()

    def fake_stream_and_collect(agent_arg, *, astream_kwargs, on_chunk, error_message_factory):
        on_chunk({"type": "tool_call", "name": "web_search"}, SimpleNamespace())
        release.wait(10)
        return SimpleNamespace(
            iteration_limit_hit=False, response_text=lambda **k: "child finished"
        )

    monkeypatch.setattr("nymeria.core.stream_bridge.stream_and_collect", fake_stream_and_collect)

    result = _invoke_spawned(
        cast(Any, agent),
        child_thread_id="child-1",
        parent_thread_id=CALLER,
        title="Researcher",
        task="research the thing",
        user_id="owner",
        callable_tool_name=callable_tool_name,
    )
    return agent, seen, release, result


def test_spawned_first_turn_outliving_budget_returns_receipt_and_wakes_parent(monkeypatch):
    agent, seen, release, result = _spawn_outliving_budget(
        monkeypatch, callable_tool_name="spawned_researcher_ab12cd34"
    )
    assert result.startswith(ex.STILL_WORKING_PREFIX)
    assert "Researcher has been working" in result
    assert "1 tool call(s) so far, last web_search" in result
    # The follow-up instruction names the REGISTERED tool, not the title.
    assert 'call spawned_researcher_ab12cd34 with mode="handoff"' in result
    assert 'call Researcher with mode="handoff"' not in result
    assert agent.unregistered == [(CALLER, "child-1")]

    release.set()
    _wait_until(lambda: len(seen) == 1)
    delivery = seen[0]
    assert delivery.thread_id == CALLER
    assert delivery.source == "callable_result"
    assert "--- Researcher output ---\nchild finished\n" in delivery.prompt_text


def test_spawned_non_callable_child_receipt_offers_no_handoff(monkeypatch):
    agent, seen, release, result = _spawn_outliving_budget(monkeypatch, callable_tool_name=None)
    try:
        assert result.startswith(ex.STILL_WORKING_PREFIX)
        assert "has no callable tool" in result
        assert 'mode="handoff"' not in result
    finally:
        release.set()
    _wait_until(lambda: len(seen) == 1)
