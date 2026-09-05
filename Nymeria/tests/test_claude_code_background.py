"""Tests for the claude_code background watcher: per-end-turn reports, the
inline-or-detached decision, follow-up queueing, and the live registry.

The first report is claimed exactly once (inline or detached); every later
report is delivered detached. A run that ends several turns delivers each
one, tagged with the job id, the session id, its index and its kind.
"""

import importlib
import threading
import time
from types import SimpleNamespace

import pytest

from nymeria.core.thread_lock_manager import ThreadLockManager
from nymeria.tools import claude_code_background as bg
from nymeria.tools.claude_code_bridge import ClaudeCodeResult


def _agent(owner="u1", locks=None):
    return SimpleNamespace(
        _thread_locks=locks or ThreadLockManager(),
        accounts_repo=SimpleNamespace(get_thread_owner=lambda tid: owner),
    )


def _make_job(job_id="j1", thread_id="t1", user_id="u1"):
    return bg.ClaudeCodeJob(
        id=job_id,
        thread_id=thread_id,
        user_id=user_id,
        prompt="do work",
        cwd="/repo",
        mode="dontAsk",
        started_at=time.time(),
        detached_message="working in background",
    )


def _final_report(job, turns=None, total=None):
    turns = turns or []
    return bg.TurnReport(kind=bg.REPORT_FINAL, turns=turns, total=total if total is not None else len(turns))


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    monkeypatch.setattr(bg, "INLINE_GRACE_SECONDS", 0.3)
    monkeypatch.setattr(bg, "END_TURN_SETTLE_SECONDS", 0.3)
    bg.reset_jobs_for_tests()
    yield
    bg.reset_jobs_for_tests()


def _capture_deliveries(monkeypatch):
    delivered: list[tuple] = []
    event = threading.Event()

    def fake_submit(job, report):
        delivered.append((job, report))
        event.set()

    monkeypatch.setattr(bg, "_submit_completion_prompt", fake_submit)
    return delivered, event


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# --- inline-or-detached ------------------------------------------------------


def test_wait_inline_detaches_with_zero_budget():
    job = _make_job()
    assert job.wait_inline(0) == "detached"


def test_wait_inline_claims_inline_when_a_report_is_ready():
    job = _make_job()
    job.result = ClaudeCodeResult(ok=True, result_text="x")
    job.done.set()
    # The watcher publishes the report, then signals; the caller waits on the
    # report, not on completion (an interim turn can be the inline answer),
    # so a done run whose report is not published yet detaches.
    assert job.wait_inline(0.2) == "detached"
    job2 = _make_job("j2")
    job2.first_report = _final_report(job2)
    job2.report_ready.set()
    assert job2.wait_inline(5) == "inline"


def test_start_job_inline_does_not_notify(monkeypatch):
    delivered, _ = _capture_deliveries(monkeypatch)
    completed = []
    job = _make_job("inline-job")

    def producer():
        job.observer.feed_event({"type": "system", "subtype": "init", "session_id": "s1"})
        job.observer.feed_event({"type": "result", "subtype": "success", "result": "fast"})
        return ClaudeCodeResult(ok=True, result_text="fast", session_id="s1",
                                end_turns=job.observer.turns_after(0))

    bg.start_job(job, producer, on_complete=lambda r: completed.append(r))

    assert job.wait_inline(5) == "inline"
    report = job.first_report
    assert report is not None and report.final
    assert [t.text for t in report.turns] == ["fast"]
    assert job.result is not None and job.result.result_text == "fast"
    assert completed and completed[0].session_id == "s1"
    text = bg.format_report_for_agent(job, report)
    assert "job inline-job" in text and "session s1" in text and "FINAL" in text
    assert "fast" in text and "run summary" in text
    time.sleep(0.5)
    assert delivered == []


def test_start_job_detached_notifies_after_completion(monkeypatch):
    delivered, event = _capture_deliveries(monkeypatch)
    release = threading.Event()
    job = _make_job("detach-job")

    def producer():
        release.wait(5)
        job.observer.feed_event({"type": "result", "subtype": "success", "result": "late",
                                 "session_id": "s2"})
        return ClaudeCodeResult(ok=True, result_text="late", session_id="s2",
                                end_turns=job.observer.turns_after(0))

    bg.start_job(job, producer)
    assert job.wait_inline(0) == "detached"
    assert delivered == []

    release.set()
    assert event.wait(3), "completion was not delivered"
    time.sleep(0.3)
    assert len(delivered) == 1
    delivered_job, report = delivered[0]
    assert delivered_job is job and report.final
    assert [t.text for t in report.turns] == ["late"]
    assert job.result is not None and job.result.result_text == "late"


# --- multi-turn runs ---------------------------------------------------------


def test_every_end_turn_is_delivered_as_its_own_report(monkeypatch):
    """Tonight's failure shape: an early end-turn ("suite running, will pick
    up"), a long gap, then the real final. Both must reach the thread, the
    first as INTERIM while the run continues, the second as FINAL."""
    delivered, _ = _capture_deliveries(monkeypatch)
    second = threading.Event()
    job = _make_job("multi")

    def producer():
        job.observer.feed_event({"type": "system", "subtype": "init", "session_id": "s-multi"})
        job.observer.feed_event({"type": "result", "subtype": "success",
                                 "result": "suite running, will pick up"})
        second.wait(5)
        job.observer.feed_event({"type": "result", "subtype": "success",
                                 "result": "all green, committed abc123"})
        return ClaudeCodeResult(ok=True, result_text="all green, committed abc123",
                                session_id="s-multi", commits=["abc123 done"],
                                end_turns=job.observer.turns_after(0))

    bg.start_job(job, producer)
    assert job.wait_inline(0) == "detached"

    assert _wait_for(lambda: len(delivered) == 1), "interim end-turn was not delivered"
    _, interim = delivered[0]
    assert interim.kind == bg.REPORT_INTERIM
    assert [t.index for t in interim.turns] == [1]
    prompt = bg.build_completion_prompt(job, interim)
    assert "INTERIM end-turn 1" in prompt and "job multi" in prompt and "session s-multi" in prompt
    assert "suite running, will pick up" in prompt
    assert "do not treat the task as finished" in prompt
    assert 'resume="s-multi"' in prompt
    # Distinct task ids per interim delivery.
    delivery = bg.job_delivery(job, prompt, report=interim)
    assert delivery.task_id == "claude-code-multi-t1"
    assert delivery.source_id == "multi:t1"
    assert delivery.started_data["report"] == "interim"

    second.set()
    assert _wait_for(lambda: len(delivered) == 2), "final report was not delivered"
    _, final = delivered[1]
    assert final.final and [t.index for t in final.turns] == [2] and final.total == 2
    prompt = bg.build_completion_prompt(job, final)
    assert "FINAL: run finished" in prompt and "2 end-turn(s)" in prompt
    assert "all green, committed abc123" in prompt
    assert "commits: abc123 done" in prompt
    assert "suite running" not in prompt, "turn 1 was already delivered"
    assert bg.job_delivery(job, prompt, report=final).task_id == "claude-code-multi"


def test_an_end_turn_the_process_exits_on_merges_into_the_final_report(monkeypatch):
    """The common single-turn run: the result event lands and the process
    exits a moment later. One delivery, not an interim plus a final."""
    delivered, _ = _capture_deliveries(monkeypatch)
    job = _make_job("single")

    def producer():
        job.observer.feed_event({"type": "result", "subtype": "success", "result": "done",
                                 "session_id": "s-one"})
        time.sleep(0.05)  # well inside the settle window
        return ClaudeCodeResult(ok=True, result_text="done", session_id="s-one",
                                end_turns=job.observer.turns_after(0))

    bg.start_job(job, producer)
    assert job.wait_inline(0) == "detached"
    assert _wait_for(lambda: len(delivered) == 1)
    time.sleep(0.5)
    assert len(delivered) == 1
    _, report = delivered[0]
    assert report.final and [t.text for t in report.turns] == ["done"]


def test_inline_caller_takes_an_interim_turn_and_the_final_still_arrives(monkeypatch):
    """A run that outlives its first end-turn: the tool's inline wait returns
    that turn (tagged interim), and the final report is delivered detached
    later, saying the final message was that turn."""
    delivered, _ = _capture_deliveries(monkeypatch)
    finish = threading.Event()
    job = _make_job("inline-interim")

    def producer():
        job.observer.feed_event({"type": "result", "subtype": "success",
                                 "result": "plan written, subagents reviewing", "session_id": "s-ii"})
        finish.wait(5)
        return ClaudeCodeResult(ok=True, result_text="plan written, subagents reviewing",
                                session_id="s-ii", end_turns=job.observer.turns_after(0),
                                files_changed=["PLAN.md"])

    bg.start_job(job, producer)
    assert job.wait_inline(5) == "inline"
    report = job.first_report
    assert report is not None and report.kind == bg.REPORT_INTERIM
    text = bg.format_report_for_agent(job, report)
    assert "INTERIM end-turn 1" in text and "plan written" in text
    assert delivered == []

    finish.set()
    assert _wait_for(lambda: len(delivered) == 1)
    _, final = delivered[0]
    assert final.final and final.turns == [] and final.total == 1
    body = bg.report_body(job, final)
    assert "final message was end-turn 1, delivered earlier" in body
    assert "files_changed (1): PLAN.md" in body


def test_producer_exception_becomes_a_failed_final_report(monkeypatch):
    delivered, _ = _capture_deliveries(monkeypatch)
    job = _make_job("boom")

    def producer():
        raise RuntimeError("runner exploded")

    bg.start_job(job, producer)
    assert job.wait_inline(5) == "inline"
    report = job.first_report
    assert report is not None and report.final and report.turns == []
    text = bg.format_report_for_agent(job, report)
    assert "FINAL: run failed" in text and "runner exploded" in text


# --- follow-up queue ---------------------------------------------------------


def test_queued_followups_start_a_resumed_run_when_the_job_ends(monkeypatch):
    claude_module = importlib.import_module("nymeria.tools.claude_code")

    _capture_deliveries(monkeypatch)
    spawned: list[tuple] = []
    spawn_event = threading.Event()

    def fake_followup(previous, session_id, prompts):
        spawned.append((previous, session_id, list(prompts)))
        spawn_event.set()
        return SimpleNamespace(id="fu-1")

    monkeypatch.setattr(claude_module, "start_followup_run", fake_followup)
    release = threading.Event()
    job = _make_job("with-followups")

    def producer():
        job.observer.feed_event({"type": "system", "subtype": "init", "session_id": "s-f"})
        release.wait(5)
        return ClaudeCodeResult(ok=True, result_text="done", session_id="s-f")

    bg.start_job(job, producer)
    assert job.wait_inline(0) == "detached"
    assert job.queue_followup("also run ruff") == 1
    assert job.queue_followup("and push nothing") == 2
    assert spawned == []
    release.set()
    assert spawn_event.wait(3), "follow-up was not started after the run ended"
    previous, session_id, prompts = spawned[0]
    assert previous is job and session_id == "s-f"
    assert prompts == ["also run ruff", "and push nothing"]
    assert job.take_followups() == []


def test_followups_are_dropped_when_the_run_was_cancelled(monkeypatch):
    claude_module = importlib.import_module("nymeria.tools.claude_code")

    _capture_deliveries(monkeypatch)
    spawned: list = []
    monkeypatch.setattr(claude_module, "start_followup_run", lambda *a: spawned.append(a))
    job = _make_job("cancelled-followups")
    job.queue_followup("never")

    def producer():
        return ClaudeCodeResult(ok=False, is_error=True, subtype="cancelled",
                                error="aborted", session_id="s-x")

    bg.start_job(job, producer)
    assert job.wait_inline(5) == "inline"
    time.sleep(0.3)
    assert spawned == []


def test_cancel_jobs_for_thread_signals_only_running_jobs_on_that_thread():
    running = _make_job("run", thread_id="t1")
    other = _make_job("other", thread_id="t2")
    finished = _make_job("done", thread_id="t1")
    for job in (running, other, finished):
        job.cancel_event = threading.Event()
        bg.register(job)
    finished.done.set()
    assert bg.cancel_jobs_for_thread("t1") == [running]
    assert running.cancel_event.is_set()
    assert not other.cancel_event.is_set() and not finished.cancel_event.is_set()


def test_followups_dropped_without_a_session_are_named_on_the_final_report(monkeypatch):
    """The caller holds a [Queued] receipt; a drop is reported, never logged
    only."""
    claude_module = importlib.import_module("nymeria.tools.claude_code")
    delivered, event = _capture_deliveries(monkeypatch)
    spawned: list = []
    monkeypatch.setattr(claude_module, "start_followup_run", lambda *a: spawned.append(a))
    job = _make_job("no-session")
    job.queue_followup("then run the suite please")
    job.detach()
    bg.start_job(job, lambda: ClaudeCodeResult(ok=True, result_text="done"))
    assert event.wait(3)
    ((_, report),) = delivered
    assert report.final and spawned == []
    assert any(
        "DROPPED" in n and "without a session id" in n and "then run the suite" in n
        for n in report.notes
    )
    assert "DROPPED" in bg.report_body(job, report)


def test_a_followup_that_cannot_start_is_named_on_the_final_report(monkeypatch):
    claude_module = importlib.import_module("nymeria.tools.claude_code")
    delivered, event = _capture_deliveries(monkeypatch)

    def broken(*a):
        raise RuntimeError("runner gone")

    monkeypatch.setattr(claude_module, "start_followup_run", broken)
    job = _make_job("cannot-start")
    job.queue_followup("more")
    job.detach()
    bg.start_job(job, lambda: ClaudeCodeResult(ok=True, result_text="done", session_id="s-1"))
    assert event.wait(3)
    ((_, report),) = delivered
    assert any("could not start" in n and "runner gone" in n and "'more'" in n for n in report.notes)


def test_a_started_followup_is_named_on_the_final_report(monkeypatch):
    claude_module = importlib.import_module("nymeria.tools.claude_code")
    delivered, event = _capture_deliveries(monkeypatch)
    monkeypatch.setattr(
        claude_module, "start_followup_run", lambda *a: SimpleNamespace(id="fu-9")
    )
    job = _make_job("started-followup")
    job.queue_followup("more")
    job.detach()
    bg.start_job(job, lambda: ClaudeCodeResult(ok=True, result_text="done", session_id="s-1"))
    assert event.wait(3)
    ((_, report),) = delivered
    assert any("started as job fu-9" in n and "session s-1" in n for n in report.notes)
    assert "started as job fu-9" in bg.report_body(job, report)


def test_a_stop_that_lands_after_the_process_exits_still_drops_the_queue(monkeypatch):
    """The result reads success (the child was already gone), but the user
    stopped the session: nothing more runs on it, silently."""
    claude_module = importlib.import_module("nymeria.tools.claude_code")
    delivered, event = _capture_deliveries(monkeypatch)
    spawned: list = []
    monkeypatch.setattr(claude_module, "start_followup_run", lambda *a: spawned.append(a))
    job = _make_job("stopped-late")
    job.cancel_event = threading.Event()
    job.queue_followup("more")
    job.detach()
    release = threading.Event()

    def producer():
        release.wait(5)
        return ClaudeCodeResult(ok=True, result_text="done", session_id="s-1")

    bg.start_job(job, producer)
    assert bg.cancel_jobs_for_thread("t1") == [job]
    release.set()
    assert event.wait(3)
    ((_, report),) = delivered
    assert spawned == [] and report.notes == []


# --- live registry -----------------------------------------------------------


def test_find_job_by_id_session_and_latest():
    older = _make_job("old", thread_id="t1")
    older.observer.set_session_id("sess-old")
    older.done.set()
    newer = _make_job("new", thread_id="t1")
    newer.observer.set_session_id("sess-new")
    other = _make_job("other", thread_id="t2", user_id="u2")
    for job in (older, newer, other):
        bg.register(job)

    assert bg.find_job("old") is older
    assert bg.find_job("sess-new") is newer
    assert bg.find_job("latest", thread_id="t1") is newer
    assert bg.find_job("latest", thread_id="t2", user_id="u1") is None, "scoped to the user"
    assert bg.find_job("other", user_id="u1") is None
    assert bg.find_job("nope") is None
    assert bg.live_job_for_session("sess-new") is newer
    assert bg.live_job_for_session("sess-old") is None, "finished runs are not live"


def test_session_lookup_prefers_the_running_job_over_a_finished_one_on_the_same_session():
    finished = _make_job("first-run")
    finished.observer.set_session_id("s-shared")
    finished.done.set()
    running = _make_job("resumed-run")
    running.resumed_session_id = "s-shared"
    bg.register(finished)
    bg.register(running)
    assert bg.find_job("s-shared") is running
    assert bg.live_job_for_session("s-shared") is running


def test_latest_prefers_a_running_job_and_finished_jobs_are_bounded(monkeypatch):
    monkeypatch.setattr(bg, "_RETAINED_FINISHED_JOBS", 2)
    running = _make_job("run")
    bg.register(running)
    for i in range(4):
        job = _make_job(f"fin{i}")
        job.done.set()
        bg.register(job)
    assert bg.find_job("latest", thread_id="t1") is running
    assert bg.find_job("fin0") is None and bg.find_job("fin1") is None
    assert bg.find_job("fin3") is not None


# --- rendering ---------------------------------------------------------------


def test_build_completion_prompt_includes_output():
    job = _make_job()
    job.observer.feed_event({"type": "result", "subtype": "success",
                             "result": "here is the plan", "session_id": "s"})
    job.result = ClaudeCodeResult(ok=True, result_text="here is the plan", session_id="s")
    prompt = bg.build_completion_prompt(job, job.next_report(bg.REPORT_FINAL))
    assert "here is the plan" in prompt
    assert job.id in prompt and "session s" in prompt
    assert "Relay the outcome" in prompt


def test_format_peek_renders_state_turns_and_tail():
    job = _make_job("peeked")
    job.observer.feed_event({"type": "system", "subtype": "init", "session_id": "s-peek"})
    job.observer.feed_event({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Reading the module first."},
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/repo/a.py"}}]}})
    job.observer.feed_event({"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "def a(): ...", "is_error": False}]}})
    job.observer.feed_event({"type": "result", "subtype": "success", "result": "first turn"})
    text = bg.format_peek(job, job.observer.snapshot(2), 2)
    assert "job peeked | session s-peek | RUNNING" in text
    assert "End-turns so far: 1" in text and "1: first turn" in text
    assert "last 2 of 4 entries" in text
    assert "tool_result: def a(): ..." in text and "end_turn: first turn" in text
    assert "Reading the module first" not in text, "outside the requested tail"
    assert 'resume="s-peek"' in text


# --- completion-delivery guards ----------------------------------------------


def test_cancelled_result_is_dropped():
    """A run cancelled by a thread abort stays silent (no autonomous turn)."""
    job = _make_job("cancelled-job")
    job.result = ClaudeCodeResult(
        ok=False, is_error=True, subtype="cancelled", error="aborted"
    )
    assert bg._should_drop_for_thread_state(job, _agent(owner="u1")) is True


def test_owner_mismatch_drops_completion():
    job = _make_job()  # user_id="u1"
    job.result = ClaudeCodeResult(ok=True, result_text="ok")
    assert bg._should_drop_for_thread_state(job, _agent(owner="someone-else")) is True


def test_owner_match_does_not_drop():
    job = _make_job()  # user_id="u1", thread_id="t1", no abort set
    job.result = ClaudeCodeResult(ok=True, result_text="ok")
    assert bg._should_drop_for_thread_state(job, _agent(owner="u1")) is False


def test_aborted_thread_drops_completion():
    job = _make_job()
    job.result = ClaudeCodeResult(ok=True, result_text="ok")
    agent = _agent(owner="u1")
    agent._thread_locks.signal_abort(job.thread_id)  # thread was stopped
    assert bg._should_drop_for_thread_state(job, agent) is True


def test_interim_report_is_not_silenced_by_a_later_cancel(monkeypatch):
    """Only the FINAL report of a cancelled run stays silent; an interim
    end-turn already produced is real output."""
    import nymeria.core.agent as agent_module
    import nymeria.core.completion_delivery as cd

    submitted: list = []
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: _agent(owner="u1"))
    monkeypatch.setattr(cd, "submit_completion", lambda agent, delivery, fire=None: submitted.append(delivery) or "fired")
    job = _make_job("interim-then-cancel")
    job.observer.feed_event({"type": "result", "subtype": "success", "result": "half way"})
    job.result = ClaudeCodeResult(ok=False, is_error=True, subtype="cancelled", error="aborted")
    bg._submit_completion_prompt(job, job.next_report(bg.REPORT_INTERIM))
    assert len(submitted) == 1 and submitted[0].source_id == "interim-then-cancel:t1"
    bg._submit_completion_prompt(job, job.next_report(bg.REPORT_FINAL))
    assert len(submitted) == 1, "the cancelled final stays silent"
