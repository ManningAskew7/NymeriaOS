"""Tests for the claude_code tool orchestration (transport + sessions + modes).

The detached/background path is exercised in test_claude_code_background.py; here
we drive the synchronous (no-thread-context) path so producers run inline and we
can assert routing, session persistence, and mode handling without threads.
"""

import importlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from nymeria.tools import claude_code_background as bg
from nymeria.tools.claude_code_bridge import RunObserver

claude_module = importlib.import_module("nymeria.tools.claude_code")
bridge = importlib.import_module("nymeria.tools.claude_code_bridge")


def _settings(tmp_path, **overrides):
    root = (tmp_path / "proj").resolve()
    root.mkdir(parents=True, exist_ok=True)
    base = dict(
        project_root=root,
        data_dir=tmp_path / "data",
        tool_timeout=300,
        nymeria_claude_code_url=None,
        nymeria_claude_code_token=None,
        nymeria_claude_code_roots=None,
        nymeria_claude_code_model=None,
        nymeria_claude_code_fallback_model=None,
        nymeria_claude_code_max_turns=None,
        nymeria_claude_code_max_budget_usd=None,
        nymeria_claude_code_disallowed_tools=None,
        nymeria_claude_code_bare=False,
        nymeria_claude_code_default_mode="dontAsk",
        nymeria_claude_code_block_seconds=None,
        nymeria_claude_code_allowed_models=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_local_sync_returns_result_and_persists_session(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        claude_module, "resolve_claude_executable", lambda: "/usr/bin/claude"
    )

    def fake_run_local(request, config, timeout, env=None, **kw):
        return bridge.ClaudeCodeResult(
            ok=True, result_text="implemented X", session_id="sess-9", subtype="success"
        )

    monkeypatch.setattr(claude_module, "run_local_blocking", fake_run_local)

    out = claude_module.claude_code.func("do X")
    assert "implemented X" in out
    assert "run summary" in out
    assert "sess-9" in out

    # Session persisted under the resolved default cwd (the project root).
    sessions = json.loads((settings.data_dir / "claude_code_sessions.json").read_text())
    assert "sess-9" in sessions.values()


def test_invalid_mode_returns_error(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    out = claude_module.claude_code.func("x", mode="explode-everything")
    assert "[Error]" in out
    assert "Valid modes" in out


def test_resume_passes_stored_session_id(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        claude_module, "resolve_claude_executable", lambda: "/usr/bin/claude"
    )

    # Pre-seed the store for the default cwd (project root).
    store = bridge.SessionStore(settings.data_dir / "claude_code_sessions.json")
    store.set("default", str(settings.project_root), "prev-sess")

    captured = {}

    def fake_run_local(request, config, timeout, env=None, **kw):
        captured["resume"] = request.resume_session_id
        return bridge.ClaudeCodeResult(ok=True, result_text="ok")

    monkeypatch.setattr(claude_module, "run_local_blocking", fake_run_local)

    claude_module.claude_code.func("continue", resume=True)
    assert captured["resume"] == "prev-sess"


def test_resume_false_ignores_stored_session(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        claude_module, "resolve_claude_executable", lambda: "/usr/bin/claude"
    )
    store = bridge.SessionStore(settings.data_dir / "claude_code_sessions.json")
    store.set("default", str(settings.project_root), "prev-sess")

    captured = {}

    def fake_run_local(request, config, timeout, env=None, **kw):
        captured["resume"] = request.resume_session_id
        return bridge.ClaudeCodeResult(ok=True, result_text="ok")

    monkeypatch.setattr(claude_module, "run_local_blocking", fake_run_local)
    claude_module.claude_code.func("fresh", resume=False)
    assert captured["resume"] is None


def test_remote_transport_completed(tmp_path, monkeypatch):
    settings = _settings(tmp_path, nymeria_claude_code_url="http://host:9000")
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)

    captured = {}

    class FakeClient:
        def __init__(self, base_url, token):
            captured["base_url"] = base_url
            captured["token"] = token

        def run(self, payload, timeout):
            captured["payload"] = payload
            return {
                "status": "completed",
                "job_id": "j1",
                "result": bridge.ClaudeCodeResult(
                    ok=True, result_text="remote done", session_id="r-sess"
                ).to_payload(),
            }

        def poll(self, job_id, timeout=30.0):  # pragma: no cover - not reached
            raise AssertionError("should not poll when run returns completed")

    monkeypatch.setattr(claude_module, "RemoteRunnerClient", FakeClient)

    out = claude_module.claude_code.func("do it remotely", mode="plan")
    assert "remote done" in out
    assert captured["base_url"] == "http://host:9000"
    assert captured["payload"]["mode"] == "plan"
    # Remote mode does not validate cwd locally (the runner owns the repo).
    assert captured["payload"]["working_dir"] is None


def test_remote_transport_polls_to_completion(tmp_path, monkeypatch):
    """The production runner returns {status: running, job_id}; the tool must poll
    /job until completed."""
    settings = _settings(tmp_path, nymeria_claude_code_url="http://host:9000")
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(claude_module, "REMOTE_POLL_INTERVAL", 0.01)

    class FakeClient:
        def __init__(self, base_url, token):
            self._polls = 0

        def run(self, payload, timeout):
            return {"status": "running", "job_id": "j7"}

        def poll(self, job_id, timeout=30.0):
            self._polls += 1
            if self._polls < 3:
                return {"status": "running"}
            return {
                "status": "completed",
                "result": bridge.ClaudeCodeResult(
                    ok=True, result_text="polled done", session_id="p-sess"
                ).to_payload(),
            }

    monkeypatch.setattr(claude_module, "RemoteRunnerClient", FakeClient)
    out = claude_module.claude_code.func("poll me")
    assert "polled done" in out


def test_remote_transport_tolerates_transient_poll_errors(tmp_path, monkeypatch):
    settings = _settings(tmp_path, nymeria_claude_code_url="http://host:9000")
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(claude_module, "REMOTE_POLL_INTERVAL", 0.01)

    class FlakyClient:
        def __init__(self, base_url, token):
            self._polls = 0

        def run(self, payload, timeout):
            return {"status": "running", "job_id": "j8"}

        def poll(self, job_id, timeout=30.0):
            self._polls += 1
            if self._polls <= 2:
                raise bridge.RemoteRunnerError("transient blip")
            return {
                "status": "completed",
                "result": bridge.ClaudeCodeResult(ok=True, result_text="survived").to_payload(),
            }

    monkeypatch.setattr(claude_module, "RemoteRunnerClient", FlakyClient)
    out = claude_module.claude_code.func("flaky")
    assert "survived" in out


def test_remote_runner_error_surfaces(tmp_path, monkeypatch):
    settings = _settings(tmp_path, nymeria_claude_code_url="http://host:9000")
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)

    class FakeClient:
        def __init__(self, base_url, token):
            pass

        def run(self, payload, timeout):
            raise bridge.RemoteRunnerError("connection refused")

    monkeypatch.setattr(claude_module, "RemoteRunnerClient", FakeClient)
    out = claude_module.claude_code.func("x")
    assert "connection refused" in out


def test_missing_executable_local_errors(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(claude_module, "resolve_claude_executable", lambda: None)
    out = claude_module.claude_code.func("x")
    assert "[Error]" in out
    assert "executable not found" in out


def test_local_applies_thread_model_override(tmp_path, monkeypatch):
    """A per-thread claude_code_model override is applied to the local run config."""
    settings = _settings(tmp_path)
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        claude_module, "resolve_claude_executable", lambda: "/usr/bin/claude"
    )
    # No thread context in the sync path, so resolve the override directly via an
    # injected effective-model resolver (mirrors how the agent injects a manager).
    monkeypatch.setattr(
        claude_module,
        "get_effective_claude_code_model",
        lambda thread_id, default: "claude-opus-4-8",
    )

    captured = {}

    def fake_run_local(request, config, timeout, env=None, **kw):
        captured["model"] = config.model
        return bridge.ClaudeCodeResult(ok=True, result_text="ok")

    monkeypatch.setattr(claude_module, "run_local_blocking", fake_run_local)
    claude_module.claude_code.func("do X")
    assert captured["model"] == "claude-opus-4-8"


def test_per_call_mode_beats_thread_default_mode(tmp_path, monkeypatch):
    """An explicit per-call mode overrides the per-thread default mode."""
    settings = _settings(tmp_path)
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        claude_module, "resolve_claude_executable", lambda: "/usr/bin/claude"
    )
    # Thread default would be "plan", but the per-call mode is "bypass".
    monkeypatch.setattr(
        claude_module,
        "get_effective_claude_code_mode",
        lambda thread_id, default: "plan",
    )

    captured = {}

    def fake_run_local(request, config, timeout, env=None, **kw):
        captured["mode"] = request.permission_mode
        return bridge.ClaudeCodeResult(ok=True, result_text="ok")

    monkeypatch.setattr(claude_module, "run_local_blocking", fake_run_local)
    claude_module.claude_code.func("do X", mode="bypass")
    assert captured["mode"] == "bypassPermissions"


def test_thread_default_mode_used_when_no_per_call_mode(tmp_path, monkeypatch):
    """With no per-call mode, the per-thread default mode is used."""
    settings = _settings(tmp_path)
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        claude_module, "resolve_claude_executable", lambda: "/usr/bin/claude"
    )
    monkeypatch.setattr(
        claude_module,
        "get_effective_claude_code_mode",
        lambda thread_id, default: "plan",
    )

    captured = {}

    def fake_run_local(request, config, timeout, env=None, **kw):
        captured["mode"] = request.permission_mode
        return bridge.ClaudeCodeResult(ok=True, result_text="ok")

    monkeypatch.setattr(claude_module, "run_local_blocking", fake_run_local)
    claude_module.claude_code.func("do X")
    assert captured["mode"] == "plan"


def test_remote_payload_includes_model_override(tmp_path, monkeypatch):
    """The per-thread model override travels in the remote /run payload."""
    settings = _settings(tmp_path, nymeria_claude_code_url="http://host:9000")
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        claude_module,
        "get_effective_claude_code_model",
        lambda thread_id, default: "claude-opus-4-8",
    )

    captured = {}

    class FakeClient:
        def __init__(self, base_url, token):
            pass

        def run(self, payload, timeout):
            captured["payload"] = payload
            return {
                "status": "completed",
                "job_id": "j1",
                "result": bridge.ClaudeCodeResult(ok=True, result_text="done").to_payload(),
            }

        def poll(self, job_id, timeout=30.0):  # pragma: no cover - completed inline
            raise AssertionError("should not poll")

    monkeypatch.setattr(claude_module, "RemoteRunnerClient", FakeClient)
    claude_module.claude_code.func("remote work")
    assert captured["payload"]["model"] == "claude-opus-4-8"


def test_remote_producer_cancels_on_abort(tmp_path, monkeypatch):
    """When the thread's abort event is set, the remote producer calls the
    runner's /cancel and returns a cancelled result instead of polling forever."""
    settings = _settings(tmp_path, nymeria_claude_code_url="http://host:9000")
    monkeypatch.setattr(claude_module, "REMOTE_POLL_INTERVAL", 0.01)

    cancelled = {}

    class FakeClient:
        def __init__(self, base_url, token):
            pass

        def run(self, payload, timeout):
            return {"status": "running", "job_id": "jc"}

        def poll(self, job_id, timeout=30.0):  # pragma: no cover - aborted first
            return {"status": "running"}

        def cancel(self, job_id, timeout=10.0):
            cancelled["job_id"] = job_id
            return True

    monkeypatch.setattr(claude_module, "RemoteRunnerClient", FakeClient)

    abort = threading.Event()
    abort.set()  # thread already aborted before the producer runs
    producer = claude_module._make_remote_producer(
        settings,
        "http://host:9000",
        prompt="x",
        working_dir=None,
        cli_mode="dontAsk",
        resume_session_id=None,
        abort_event=abort,
    )
    result = producer()
    assert result.subtype == "cancelled"
    assert result.is_error is True
    assert cancelled.get("job_id") == "jc"  # the runner job was cancelled


# --- session addressing, follow-up queue, peek, prompt framing ---------------
#
# These need a thread context (the job path), so the run is faked at
# ``run_local_blocking`` and the watcher's timing constants are shortened.

THREAD_CFG = {"configurable": {"thread_id": "thread-A", "user_id": "owner"}}


@pytest.fixture(autouse=True)
def _job_registry(monkeypatch):
    monkeypatch.setattr(bg, "INLINE_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(bg, "END_TURN_SETTLE_SECONDS", 0.2)
    bg.reset_jobs_for_tests()
    yield
    bg.reset_jobs_for_tests()


def _local(tmp_path, monkeypatch, **overrides):
    settings = _settings(tmp_path, **overrides)
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(claude_module, "resolve_claude_executable", lambda: "/usr/bin/claude")
    return settings


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_resume_accepts_an_explicit_session_id(tmp_path, monkeypatch):
    settings = _local(tmp_path, monkeypatch)
    store = bridge.SessionStore(settings.data_dir / "claude_code_sessions.json")
    store.set("default", str(settings.project_root), "stored-sess")
    captured = {}

    def fake_run_local(request, config, timeout, env=None, **kw):
        captured["resume"] = request.resume_session_id
        return bridge.ClaudeCodeResult(ok=True, result_text="ok", session_id="explicit-sess")

    monkeypatch.setattr(claude_module, "run_local_blocking", fake_run_local)
    claude_module.claude_code.func("continue that one", resume="explicit-sess")
    assert captured["resume"] == "explicit-sess", "the named session wins over the stored one"
    # The store now points at the session just used.
    assert store.get("default", str(settings.project_root)) == "explicit-sess"


@pytest.mark.parametrize(
    "token,expected", [("true", True), ("Yes", True), ("false", False), ("new", False), ("  ", True)]
)
def test_resume_strings_that_mean_a_bool_are_coerced(token, expected):
    assert claude_module._coerce_resume(token) is expected
    assert claude_module._coerce_resume("abc-123") == "abc-123"


def test_prompt_is_framed_with_the_originating_thread(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    captured = {}

    def fake_run_local(request, config, timeout, env=None, observer=None, **kw):
        captured["prompt"] = request.prompt
        return bridge.ClaudeCodeResult(ok=True, result_text="ok", session_id="s-frame")

    monkeypatch.setattr(claude_module, "run_local_blocking", fake_run_local)
    out = claude_module.claude_code.func("fix the bug", config=THREAD_CFG)
    sent = captured["prompt"]
    assert sent.startswith(bridge.BRIDGE_CONTEXT_OPEN)
    assert "Nymeria thread thread-A" in sent
    assert 'nymeria_chat` with thread_id="thread-A"' in sent
    assert "POST /threads/thread-A/chat" in sent
    job_id = out.split("job ")[1].split(" ")[0]
    assert f"bridge job {job_id}" in sent and f'"[Claude Code job {job_id}]"' in sent
    assert sent.endswith(f"{bridge.BRIDGE_CONTEXT_CLOSE}\n\nfix the bug")
    # The report the agent reads is tagged the same way.
    assert f"[Claude Code job {job_id} | session s-frame | FINAL" in out


def test_threadless_prompt_goes_out_bare(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    captured = {}

    def fake_run_local(request, config, timeout, env=None, **kw):
        captured["prompt"] = request.prompt
        return bridge.ClaudeCodeResult(ok=True, result_text="ok")

    monkeypatch.setattr(claude_module, "run_local_blocking", fake_run_local)
    claude_module.claude_code.func("plain")
    assert captured["prompt"] == "plain"


def test_resumed_run_names_the_session_in_the_frame(tmp_path, monkeypatch):
    settings = _local(tmp_path, monkeypatch)
    store = bridge.SessionStore(settings.data_dir / "claude_code_sessions.json")
    store.set("thread-A", str(settings.project_root), "prev")
    captured = {}

    def fake_run_local(request, config, timeout, env=None, **kw):
        captured["prompt"] = request.prompt
        return bridge.ClaudeCodeResult(ok=True, result_text="ok", session_id="prev")

    monkeypatch.setattr(claude_module, "run_local_blocking", fake_run_local)
    claude_module.claude_code.func("go on", config=THREAD_CFG)
    assert "resuming Claude Code session prev" in captured["prompt"]


def test_prompt_for_a_running_session_is_queued_and_started_afterwards(tmp_path, monkeypatch):
    """Tonight's wrong-session shape: a follow-up while the run is in flight.
    It must not resume anything concurrently; it queues on the live job and
    runs as a resumed job of THAT session when the run ends."""
    _local(tmp_path, monkeypatch)
    release = threading.Event()
    spawned: list[tuple] = []

    def fake_run_local(request, config, timeout, env=None, observer=None, **kw):
        observer.feed_event({"type": "system", "subtype": "init", "session_id": "live-sess"})
        release.wait(5)
        return bridge.ClaudeCodeResult(ok=True, result_text="first done", session_id="live-sess")

    monkeypatch.setattr(claude_module, "run_local_blocking", fake_run_local)
    monkeypatch.setattr(bg, "_submit_completion_prompt", lambda job, report: None)
    monkeypatch.setattr(
        claude_module, "start_followup_run", lambda prev, sid, prompts: spawned.append((prev.id, sid, prompts))
    )

    first = claude_module.claude_code.func("long task", detach=True, config=THREAD_CFG)
    job_id = first.split("job ")[1].split(" ")[0]
    assert _wait_for(lambda: bg.find_job(job_id).session_id == "live-sess")

    by_job = claude_module.claude_code.func("also do X", resume=job_id, config=THREAD_CFG)
    assert by_job.startswith("[Queued]") and "session live-sess" in by_job and f"job {job_id}" in by_job
    by_session = claude_module.claude_code.func("and Y", resume="live-sess", config=THREAD_CFG)
    assert by_session.startswith("[Queued]") and "position 2" in by_session
    # Bare resume=True: the stored session IS the live one (persisted on
    # first sight, not at the end), so it queues too instead of forking it.
    bare = claude_module.claude_code.func("and Z", resume=True, config=THREAD_CFG)
    assert bare.startswith("[Queued]") and "position 3" in bare
    assert spawned == []
    # A fresh session is still available while the other runs.
    monkeypatch.setattr(
        claude_module, "run_local_blocking",
        lambda request, config, timeout, env=None, **kw: bridge.ClaudeCodeResult(
            ok=True, result_text="independent", session_id="other-sess"),
    )
    fresh = claude_module.claude_code.func("unrelated", resume=False, config=THREAD_CFG)
    assert "independent" in fresh and "session other-sess" in fresh

    release.set()
    assert _wait_for(lambda: bool(spawned))
    assert spawned[0] == (job_id, "live-sess", ["also do X", "and Y", "and Z"])


def test_resume_of_an_unknown_reference_is_sent_as_a_session_id(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    captured = {}

    def fake_run_local(request, config, timeout, env=None, **kw):
        captured["resume"] = request.resume_session_id
        return bridge.ClaudeCodeResult(ok=True, result_text="ok", session_id="host-only")

    monkeypatch.setattr(claude_module, "run_local_blocking", fake_run_local)
    claude_module.claude_code.func("pick up", resume="host-only", config=THREAD_CFG)
    assert captured["resume"] == "host-only"


def test_peek_shows_a_running_sessions_live_tail(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    release = threading.Event()

    def fake_run_local(request, config, timeout, env=None, observer=None, **kw):
        observer.feed_event({"type": "system", "subtype": "init", "session_id": "peek-sess"})
        observer.feed_event({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}})
        observer.feed_event({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "12 passed"}]}})
        release.wait(5)
        return bridge.ClaudeCodeResult(ok=True, result_text="done", session_id="peek-sess")

    monkeypatch.setattr(claude_module, "run_local_blocking", fake_run_local)
    monkeypatch.setattr(bg, "_submit_completion_prompt", lambda job, report: None)
    started = claude_module.claude_code.func("run tests", detach=True, config=THREAD_CFG)
    job_id = started.split("job ")[1].split(" ")[0]
    assert _wait_for(lambda: bg.find_job(job_id).session_id == "peek-sess")

    out = claude_module.claude_code.func(peek="latest", tail=1, config=THREAD_CFG)
    assert f"[Claude Code job {job_id} | session peek-sess | RUNNING" in out
    assert "End-turns so far: none" in out
    assert "tool_result: 12 passed" in out and "tool_use Bash" not in out, "tail=1"
    assert "read-only look" in out
    # The same by session id, wider tail; the prompt is not required.
    out = claude_module.claude_code.func(peek="peek-sess", tail=5, config=THREAD_CFG)
    assert "tool_use Bash" in out
    release.set()
    assert _wait_for(lambda: not bg.find_job(job_id).running)
    out = claude_module.claude_code.func(peek=job_id, config=THREAD_CFG)
    assert "| finished" in out and "End-turns so far" in out


def test_peek_of_an_unknown_reference_and_a_missing_prompt_are_errors(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    assert "[Error]" in claude_module.claude_code.func(peek="nope", config=THREAD_CFG)
    assert "prompt is required" in claude_module.claude_code.func(config=THREAD_CFG)


def test_start_followup_run_resumes_the_session_with_the_queued_prompts(tmp_path, monkeypatch):
    _local(tmp_path, monkeypatch)
    calls: list[dict] = []
    delivered: list[tuple] = []
    delivered_event = threading.Event()

    def fake_prepare_run(settings_arg, **kwargs):
        calls.append(kwargs)
        observer = RunObserver()

        def producer():
            observer.feed_event({"type": "result", "subtype": "success", "result": "followed up",
                                 "session_id": "s-prev"})
            return bridge.ClaudeCodeResult(ok=True, result_text="followed up", session_id="s-prev",
                                           end_turns=observer.turns_after(0))

        return claude_module.PreparedRun(
            producer=producer, persist=lambda r: None, run_cwd="/repo",
            resume_session_id="s-prev", observer=observer,
        )

    monkeypatch.setattr(claude_module, "prepare_run", fake_prepare_run)

    def fake_deliver(job, report):
        delivered.append((job, report))
        delivered_event.set()

    previous = bg.ClaudeCodeJob(
        id="prev", thread_id="thread-A", user_id="owner", prompt="first", cwd="/repo",
        mode="bypassPermissions", started_at=time.time(), detached_message="",
        deliver=fake_deliver,
    )
    previous.observer.set_session_id("s-prev")
    job = claude_module.start_followup_run(previous, "s-prev", ["also X", "then Y"])
    assert calls[0]["resume"] == "s-prev" and calls[0]["cli_mode"] == "bypassPermissions"
    assert calls[0]["thread_id"] == "thread-A" and calls[0]["working_dir"] == "/repo"
    assert "[Queued follow-up 1 of 2]\nalso X" in calls[0]["prompt"]
    assert "[Queued follow-up 2 of 2]\nthen Y" in calls[0]["prompt"]
    assert job.resumed_session_id == "s-prev" and job.deliver is fake_deliver
    assert bg.find_job(job.id) is job
    assert delivered_event.wait(3)
    assert delivered[0][0] is job and delivered[0][1].final
    assert "followed up" in delivered[0][1].turns[0].text


def test_remote_polls_feed_the_observer_and_persist_the_session_early(tmp_path, monkeypatch):
    """The runner reports the session id and end-turns before completion; the
    tool learns them per poll (session store written on first sight) and the
    end-turns reach the observer as they appear."""
    settings = _settings(tmp_path, nymeria_claude_code_url="http://host:9000")
    monkeypatch.setattr(claude_module, "REMOTE_POLL_INTERVAL", 0.01)
    seen: list[tuple[int, int]] = []

    class FakeClient:
        polls = 0

        def __init__(self, base_url, token):
            pass

        def run(self, payload, timeout):
            return {"status": "running", "job_id": "rj"}

        def poll(self, job_id, timeout=30.0):
            FakeClient.polls += 1
            if FakeClient.polls == 1:
                return {"status": "running", "session_id": "r-sess", "end_turns": []}
            if FakeClient.polls == 2:
                return {"status": "running", "session_id": "r-sess", "end_turns": [
                    {"index": 1, "text": "interim", "subtype": "success"}]}
            return {"status": "completed", "session_id": "r-sess", "end_turns": [
                {"index": 1, "text": "interim", "subtype": "success"},
                {"index": 2, "text": "final", "subtype": "success"}],
                "result": bridge.ClaudeCodeResult(ok=True, result_text="final", session_id="r-sess",
                                                  end_turns=[bridge.EndTurn(1, "interim"), bridge.EndTurn(2, "final")]).to_payload()}

    observer = RunObserver()
    sessions: list[tuple[str, int]] = []
    observer.on_session = lambda sid: sessions.append((sid, FakeClient.polls))
    observer.on_end_turn = lambda t: seen.append((t.index, FakeClient.polls))
    producer = claude_module._make_remote_producer(
        settings, "http://host:9000", prompt="x", working_dir=None, cli_mode="dontAsk",
        resume_session_id=None, observer=observer, client=FakeClient("http://host:9000", None),
    )
    result = producer()
    assert sessions == [("r-sess", 1)], "session known at the first poll, not at completion"
    assert seen == [(1, 2), (2, 3)]
    assert observer.remote_job_id == "rj"
    assert result.result_text == "final" and [t.index for t in result.end_turns] == [1, 2]
    assert observer.legacy_runner is False


def test_remote_result_from_a_runner_without_end_turns_counts_as_one_turn(tmp_path, monkeypatch):
    settings = _settings(tmp_path, nymeria_claude_code_url="http://host:9000")

    class OldClient:
        def __init__(self, base_url, token):
            pass

        def run(self, payload, timeout):
            return {"status": "completed", "job_id": "old", "result": {
                "ok": True, "result_text": "legacy", "session_id": "old-sess"}}

    observer = RunObserver()
    producer = claude_module._make_remote_producer(
        settings, "http://host:9000", prompt="x", working_dir=None, cli_mode="dontAsk",
        resume_session_id=None, observer=observer, client=OldClient("u", None),
    )
    result = producer()
    assert observer.session_id == "old-sess"
    assert [t.text for t in result.end_turns] == ["legacy"]
    assert observer.turn_count == 1
    assert observer.legacy_runner is True


def test_remote_peek_uses_the_runner_job_id(tmp_path, monkeypatch):
    settings = _settings(tmp_path, nymeria_claude_code_url="http://host:9000")
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(claude_module, "REMOTE_POLL_INTERVAL", 0.01)
    release = threading.Event()
    peeks: list[tuple] = []

    class FakeClient:
        def __init__(self, base_url, token):
            pass

        def run(self, payload, timeout):
            return {"status": "running", "job_id": "runner-77"}

        def poll(self, job_id, timeout=30.0):
            if not release.is_set():
                return {"status": "running", "session_id": "rp-sess", "end_turns": []}
            return {"status": "completed", "result": bridge.ClaudeCodeResult(
                ok=True, result_text="done", session_id="rp-sess").to_payload()}

        def peek(self, job_id, tail=12, timeout=15.0):
            peeks.append((job_id, tail))
            return {"session_id": "rp-sess", "running": True, "elapsed": 3.0, "end_turns": [],
                    "tail": [{"at": 0, "kind": "tool_use", "tool": "Grep", "text": "{}"}], "tail_total": 1}

        def cancel(self, job_id, timeout=10.0):
            return True

    monkeypatch.setattr(claude_module, "RemoteRunnerClient", FakeClient)
    monkeypatch.setattr(bg, "_submit_completion_prompt", lambda job, report: None)
    started = claude_module.claude_code.func("remote", detach=True, config=THREAD_CFG)
    job_id = started.split("job ")[1].split(" ")[0]
    assert _wait_for(lambda: bg.find_job(job_id).session_id == "rp-sess")
    out = claude_module.claude_code.func(peek=job_id, tail=3, config=THREAD_CFG)
    assert peeks == [("runner-77", 3)]
    assert "tool_use Grep" in out and "RUNNING" in out
    release.set()
    assert _wait_for(lambda: not bg.find_job(job_id).running)


def _remote_detached_run(monkeypatch, client_cls, tmp_path):
    """Start a detached remote run against ``client_cls``; return the job and
    the list the watcher's deliveries land in."""
    settings = _settings(tmp_path, nymeria_claude_code_url="http://host:9000")
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(claude_module, "REMOTE_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(claude_module, "RemoteRunnerClient", client_cls)
    delivered: list[tuple] = []
    monkeypatch.setattr(bg, "_submit_completion_prompt", lambda job, report: delivered.append((job, report)))
    started = claude_module.claude_code.func("multi-turn", detach=True, config=THREAD_CFG)
    job = bg.find_job(started.split("job ")[1].split(" ")[0])
    assert job is not None
    return job, delivered


def test_legacy_runner_run_is_flagged_in_the_final_report_and_peek(tmp_path, monkeypatch):
    """Job d1b0ff78 (2026-09-04): a runner service still on pre-end-turn code
    reports nothing until the process exits, so a two-turn run reached the
    thread as ONE delivery, labelled a normal single-turn FINAL, carrying
    turn 2's text with turn 1 lost, and peek quoted only the runner's own
    job id. The report must say the runner is legacy, and peek must explain
    itself without a round trip, naming both ids."""
    peeks: list = []

    class LegacyClient:
        polls = 0

        def __init__(self, base_url, token):
            pass

        def run(self, payload, timeout):
            return {"status": "running", "job_id": "cd5a4bbc58d11bba"}

        def poll(self, job_id, timeout=30.0):
            LegacyClient.polls += 1
            if LegacyClient.polls < 3:
                return {"status": "running"}  # no session_id, no end_turns
            return {"status": "completed", "result": {
                "ok": True, "result_text": "Turn 2: the subagent reported back",
                "session_id": "925728ae", "subtype": "success", "num_turns": 1}}

        def peek(self, job_id, tail=12, timeout=15.0):
            peeks.append(job_id)
            raise AssertionError("a legacy runner has no peek route to ask")

        def cancel(self, job_id, timeout=10.0):
            return True

    job, delivered = _remote_detached_run(monkeypatch, LegacyClient, tmp_path)
    assert _wait_for(lambda: len(delivered) == 1 and not job.running)
    _, report = delivered[0]
    assert report.final and [t.text for t in report.turns] == ["Turn 2: the subagent reported back"]
    prompt = bg.build_completion_prompt(job, report)
    assert "FINAL: run finished" in prompt
    assert "only the terminal end-turn captured (legacy runner" in prompt
    assert "1 end-turn(s)" not in prompt, "a truncated run must not pass as a normal single-turn one"
    assert "[Runner note]: the runner service predates per-end-turn reporting" in prompt
    assert "restart it at a quiet moment" in prompt

    out = claude_module.claude_code.func(peek=job.id, config=THREAD_CFG)
    assert peeks == [], "legacy detected from the polls: no peek round trip"
    assert "End-turns so far: 1" in out
    assert "No transcript tail: the runner service predates per-end-turn reporting" in out
    assert f"Bridge job {job.id} is runner job cd5a4bbc58d11bba" in out


def test_current_runner_end_turns_deliver_interim_then_final_through_the_remote_poll(tmp_path, monkeypatch):
    """The same run against a runner that reports end-turns per poll: turn 1
    goes out INTERIM once the settle window passes with the process alive,
    turn 2 arrives as the FINAL with the cumulative count, no legacy note."""
    release = threading.Event()

    class CurrentClient:
        def __init__(self, base_url, token):
            pass

        def run(self, payload, timeout):
            return {"status": "running", "job_id": "rj-2"}

        def poll(self, job_id, timeout=30.0):
            turn1 = {"index": 1, "text": "turn 1: subagent outstanding", "subtype": "success"}
            if not release.is_set():
                return {"status": "running", "session_id": "s-2", "end_turns": [turn1]}
            turn2 = {"index": 2, "text": "turn 2: done", "subtype": "success"}
            return {"status": "completed", "session_id": "s-2", "end_turns": [turn1, turn2],
                    "result": bridge.ClaudeCodeResult(
                        ok=True, result_text="turn 2: done", session_id="s-2", subtype="success",
                        end_turns=[bridge.EndTurn(1, "turn 1: subagent outstanding"), bridge.EndTurn(2, "turn 2: done")],
                    ).to_payload()}

        def cancel(self, job_id, timeout=10.0):
            return True

    job, delivered = _remote_detached_run(monkeypatch, CurrentClient, tmp_path)
    assert _wait_for(lambda: len(delivered) == 1), "turn 1 was not delivered while the run went on"
    _, interim = delivered[0]
    assert interim.kind == bg.REPORT_INTERIM and [t.index for t in interim.turns] == [1]
    assert job.running
    assert "INTERIM end-turn 1" in bg.build_completion_prompt(job, interim)

    release.set()
    assert _wait_for(lambda: len(delivered) == 2)
    _, final = delivered[1]
    assert final.final and [t.index for t in final.turns] == [2] and final.total == 2
    prompt = bg.build_completion_prompt(job, final)
    assert "FINAL: run finished" in prompt and "2 end-turn(s)" in prompt
    assert "legacy runner" not in prompt and "[Runner note]" not in prompt
