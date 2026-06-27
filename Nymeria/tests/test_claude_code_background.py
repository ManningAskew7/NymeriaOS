"""Tests for the claude_code background watcher (inline vs detached delivery).

The inline-vs-detach decision must resolve exactly once: a run is never notified
twice and never silently dropped.
"""

import threading
import time
from types import SimpleNamespace

from nymeria.core.thread_lock_manager import ThreadLockManager
from nymeria.tools import claude_code_background as bg
from nymeria.tools.claude_code_bridge import ClaudeCodeResult


def _agent(owner="u1", locks=None):
    return SimpleNamespace(
        _thread_locks=locks or ThreadLockManager(),
        accounts_repo=SimpleNamespace(get_thread_owner=lambda tid: owner),
    )


def _make_job(job_id="j1"):
    return bg.ClaudeCodeJob(
        id=job_id,
        thread_id="t1",
        user_id="u1",
        prompt="do work",
        cwd="/repo",
        mode="dontAsk",
        started_at=time.time(),
        detached_message="working in background",
    )


def test_wait_inline_detaches_with_zero_budget():
    job = _make_job()
    assert job.wait_inline(0) == "detached"


def test_wait_inline_claims_inline_when_done():
    job = _make_job()
    job.result = ClaudeCodeResult(ok=True, result_text="x")
    job.done.set()
    assert job.wait_inline(5) == "inline"


def test_start_job_inline_does_not_notify(monkeypatch):
    injected = []
    monkeypatch.setattr(bg, "_submit_completion_prompt", lambda job: injected.append(job))

    completed = []
    job = _make_job("inline-job")

    def producer():
        return ClaudeCodeResult(ok=True, result_text="fast", session_id="s1")

    bg.start_job(job, producer, on_complete=lambda r: completed.append(r))

    decision = job.wait_inline(5)
    assert decision == "inline"
    assert job.result is not None and job.result.result_text == "fast"
    # on_complete ran (session persistence hook).
    assert completed and completed[0].session_id == "s1"
    # Give the watcher time to (not) notify.
    time.sleep(0.3)
    assert injected == []


def test_start_job_detached_notifies_after_completion(monkeypatch):
    inject_event = threading.Event()
    injected = []

    def fake_submit(job):
        injected.append(job)
        inject_event.set()

    monkeypatch.setattr(bg, "_submit_completion_prompt", fake_submit)
    # Speed up the grace window for the test.
    monkeypatch.setattr(bg, "INLINE_GRACE_SECONDS", 0.5)

    release = threading.Event()
    job = _make_job("detach-job")

    def producer():
        release.wait(5)
        return ClaudeCodeResult(ok=True, result_text="late", session_id="s2")

    bg.start_job(job, producer)

    # Detach immediately (zero budget): producer is still blocked.
    assert job.wait_inline(0) == "detached"
    assert injected == []

    # Let the producer finish; the watcher must now notify exactly once.
    release.set()
    assert inject_event.wait(3), "completion was not delivered"
    assert len(injected) == 1
    assert injected[0] is job
    assert job.result is not None and job.result.result_text == "late"


def test_build_completion_prompt_includes_output():
    job = _make_job()
    job.result = ClaudeCodeResult(ok=True, result_text="here is the plan", session_id="s")
    prompt = bg.build_completion_prompt(job)
    assert "here is the plan" in prompt
    assert job.id in prompt
    assert "Relay the outcome" in prompt


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
