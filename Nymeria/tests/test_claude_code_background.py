"""Tests for the claude_code background watcher (inline vs detached delivery).

The inline-vs-detach decision must resolve exactly once: a run is never notified
twice and never silently dropped.
"""

import threading
import time

from nymeria.tools import claude_code_background as bg
from nymeria.tools.claude_code_bridge import ClaudeCodeResult


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
