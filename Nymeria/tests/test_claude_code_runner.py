"""Tests for the host-side Claude Code runner service.

The runner is the policy boundary: it requires a bearer token, re-resolves the
working-directory allowlist, and runs Claude Code in a worker thread. The actual
``claude`` invocation is mocked so no real Claude Code is spawned.
"""

import importlib
import threading
import time
from types import SimpleNamespace

import pytest

runner_mod = importlib.import_module("nymeria.gateway.claude_code_runner")
bridge = importlib.import_module("nymeria.tools.claude_code_bridge")

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient

_AUTH = {"Authorization": "Bearer secret-token"}


def _settings(tmp_path, **overrides):
    root = (tmp_path / "proj").resolve()
    root.mkdir(parents=True, exist_ok=True)
    base = dict(
        project_root=root,
        data_dir=tmp_path / "data",
        nymeria_claude_code_token="secret-token",
        nymeria_claude_code_roots=None,
        nymeria_claude_code_model=None,
        nymeria_claude_code_fallback_model=None,
        nymeria_claude_code_max_turns=None,
        nymeria_claude_code_max_budget_usd=None,
        nymeria_claude_code_disallowed_tools=None,
        nymeria_claude_code_bare=False,
        nymeria_claude_code_default_mode="dontAsk",
        nymeria_claude_code_max_concurrency=2,
        nymeria_claude_code_allowed_models=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _client(tmp_path, monkeypatch, **overrides):
    settings = _settings(tmp_path, **overrides)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(runner_mod, "resolve_claude_executable", lambda: "/usr/bin/claude")
    app = runner_mod.create_app()
    return TestClient(app), settings


def _wait_completed(client, job_id, tries=100, delay=0.05):
    """Poll /job/{id} until the worker records a completed result."""
    for _ in range(tries):
        status = client.get(f"/job/{job_id}", headers=_AUTH).json()
        if status["status"] == "completed":
            return status["result"]
        time.sleep(delay)
    raise AssertionError(f"job {job_id} never completed")


def test_health_no_auth(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_run_requires_token(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.post("/run", json={"prompt": "hi"})
    assert resp.status_code == 401


def test_run_rejects_outside_allowlist(tmp_path, monkeypatch):
    client, settings = _client(tmp_path, monkeypatch)
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    resp = client.post(
        "/run",
        json={"prompt": "hi", "working_dir": str(outside)},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 400
    assert "allowed roots" in resp.json()["detail"]


def test_run_executes_and_reports_completion(tmp_path, monkeypatch):
    captured = {}

    def fake_run_local(request, config, timeout, env=None, *, cancel_check=None, **kw):
        captured["cwd"] = request.cwd
        captured["mode"] = request.permission_mode
        captured["has_cancel_check"] = callable(cancel_check)
        return bridge.ClaudeCodeResult(
            ok=True, result_text="runner did it", session_id="rs-1", subtype="success"
        )

    monkeypatch.setattr(runner_mod, "run_local_blocking", fake_run_local)
    client, settings = _client(tmp_path, monkeypatch)

    resp = client.post(
        "/run",
        json={"prompt": "build", "mode": "plan"},
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]
    assert resp.json()["status"] == "running"

    # Poll until the worker thread records the result.
    result = None
    for _ in range(50):
        status = client.get(
            f"/job/{job_id}", headers={"Authorization": "Bearer secret-token"}
        ).json()
        if status["status"] == "completed":
            result = status["result"]
            break
        time.sleep(0.05)

    assert result is not None, "job never completed"
    assert result["result_text"] == "runner did it"
    assert result["session_id"] == "rs-1"
    assert captured["mode"] == "plan"
    assert captured["cwd"] == str(settings.project_root)
    assert captured["has_cancel_check"] is True  # runner wires cancellation


def test_job_not_found(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.get("/job/nope", headers={"Authorization": "Bearer secret-token"})
    assert resp.status_code == 404


def test_cancel_running_job_group_kills_and_reports_cancelled(tmp_path, monkeypatch):
    started = threading.Event()

    def fake_run_local(request, config, timeout, env=None, *, cancel_check=None, **kw):
        # Emulate run_local_blocking honoring cancel_check on a long run.
        started.set()
        for _ in range(300):
            if cancel_check and cancel_check():
                return bridge.ClaudeCodeResult(
                    ok=False, is_error=True, subtype="cancelled", error="cancelled"
                )
            time.sleep(0.02)
        return bridge.ClaudeCodeResult(ok=True, result_text="done", subtype="success")

    monkeypatch.setattr(runner_mod, "run_local_blocking", fake_run_local)
    client, _ = _client(tmp_path, monkeypatch)

    job_id = client.post("/run", json={"prompt": "x"}, headers=_AUTH).json()["job_id"]
    assert started.wait(2.0), "run never started"

    resp = client.post(f"/cancel/{job_id}", headers=_AUTH)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelling"

    result = _wait_completed(client, job_id)
    assert result["subtype"] == "cancelled"


def test_cancel_unknown_job_returns_404(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.post("/cancel/nope", headers=_AUTH)
    assert resp.status_code == 404


def test_max_concurrency_caps_parallel_runs(tmp_path, monkeypatch):
    lock = threading.Lock()
    running = []
    peak = [0]
    release = threading.Event()

    def fake_run_local(request, config, timeout, env=None, *, cancel_check=None, **kw):
        with lock:
            running.append(1)
            peak[0] = max(peak[0], len(running))
        release.wait(3.0)
        with lock:
            running.pop()
        return bridge.ClaudeCodeResult(ok=True, result_text="ok", subtype="success")

    monkeypatch.setattr(runner_mod, "run_local_blocking", fake_run_local)
    client, _ = _client(tmp_path, monkeypatch, nymeria_claude_code_max_concurrency=1)

    j1 = client.post("/run", json={"prompt": "a"}, headers=_AUTH).json()["job_id"]
    j2 = client.post("/run", json={"prompt": "b"}, headers=_AUTH).json()["job_id"]

    time.sleep(0.4)  # let both worker threads reach the semaphore
    with lock:
        assert peak[0] == 1, "cap=1 should never run two host runs at once"

    release.set()
    _wait_completed(client, j1)
    _wait_completed(client, j2)


def test_select_runner_model_no_request_uses_default(tmp_path):
    settings = _settings(tmp_path, nymeria_claude_code_model="default-m")
    assert runner_mod._select_runner_model(settings, None) == "default-m"
    assert runner_mod._select_runner_model(settings, "  ") == "default-m"


def test_select_runner_model_open_allowlist_accepts_any(tmp_path):
    settings = _settings(tmp_path, nymeria_claude_code_model="default-m")
    # Unset allowlist = accept any requested model (budget caps remain the bound).
    assert runner_mod._select_runner_model(settings, "claude-opus-4-8") == "claude-opus-4-8"


def test_select_runner_model_allowlist_permits_listed(tmp_path):
    settings = _settings(
        tmp_path,
        nymeria_claude_code_model="default-m",
        nymeria_claude_code_allowed_models="claude-opus-4-8, claude-sonnet-4-6",
    )
    assert runner_mod._select_runner_model(settings, "claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_select_runner_model_allowlist_rejects_unlisted(tmp_path):
    settings = _settings(
        tmp_path,
        nymeria_claude_code_model="default-m",
        nymeria_claude_code_allowed_models="claude-opus-4-8",
    )
    # A requested model outside the allowlist falls back to the runner default.
    assert runner_mod._select_runner_model(settings, "some-other-model") == "default-m"


def test_run_passes_requested_model_through_allowlist(tmp_path, monkeypatch):
    captured = {}

    def fake_run_local(request, config, timeout, env=None, *, cancel_check=None, **kw):
        captured["model"] = config.model
        return bridge.ClaudeCodeResult(ok=True, result_text="ok", subtype="success")

    monkeypatch.setattr(runner_mod, "run_local_blocking", fake_run_local)
    client, _ = _client(
        tmp_path, monkeypatch, nymeria_claude_code_allowed_models="claude-opus-4-8"
    )

    job_id = client.post(
        "/run",
        json={"prompt": "x", "model": "claude-opus-4-8"},
        headers=_AUTH,
    ).json()["job_id"]
    _wait_completed(client, job_id)
    assert captured["model"] == "claude-opus-4-8"


def test_create_app_requires_token_unless_insecure(tmp_path, monkeypatch):
    settings = _settings(tmp_path, nymeria_claude_code_token=None)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: settings)
    with pytest.raises(RuntimeError):
        runner_mod.create_app()
    # insecure mode is allowed without a token
    app = runner_mod.create_app(allow_insecure=True)
    assert app is not None


# --- live status: session id, end-turns, peek --------------------------------


def _streaming_run(started, release, *, session_id="live-sess"):
    """A fake run that announces its session, ends one turn, then blocks
    until released, ending a second turn at exit."""

    def fake_run_local(request, config, timeout, env=None, *, cancel_check=None, observer=None, **kw):
        observer.feed_event({"type": "system", "subtype": "init", "session_id": session_id})
        observer.feed_event({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}}]}})
        observer.feed_event({"type": "result", "subtype": "success", "result": "interim note"})
        started.set()
        release.wait(5)
        observer.feed_event({"type": "result", "subtype": "success", "result": "all done"})
        return bridge.ClaudeCodeResult(
            ok=True, result_text="all done", session_id=session_id,
            end_turns=observer.turns_after(0),
        )

    return fake_run_local


def test_job_status_reports_session_and_end_turns_while_running(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(runner_mod, "run_local_blocking", _streaming_run(started, release))
    client, _ = _client(tmp_path, monkeypatch)
    job_id = client.post("/run", json={"prompt": "x"}, headers=_AUTH).json()["job_id"]
    assert started.wait(2.0)

    status = client.get(f"/job/{job_id}", headers=_AUTH).json()
    assert status["status"] == "running"
    assert status["session_id"] == "live-sess"
    assert [t["index"] for t in status["end_turns"]] == [1]
    assert status["end_turns"][0]["text"] == "interim note"
    assert "result" not in status

    release.set()
    result = _wait_completed(client, job_id)
    assert result["result_text"] == "all done"
    assert [t["text"] for t in result["end_turns"]] == ["interim note", "all done"]
    final = client.get(f"/job/{job_id}", headers=_AUTH).json()
    assert [t["index"] for t in final["end_turns"]] == [1, 2]


def test_peek_by_job_and_by_session_returns_the_transcript_tail(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(runner_mod, "run_local_blocking", _streaming_run(started, release))
    client, _ = _client(tmp_path, monkeypatch)
    job_id = client.post("/run", json={"prompt": "x"}, headers=_AUTH).json()["job_id"]
    assert started.wait(2.0)

    peek = client.get(f"/job/{job_id}/peek", params={"tail": 1}, headers=_AUTH).json()
    assert peek["job_id"] == job_id and peek["status"] == "running"
    assert peek["running"] is True and peek["session_id"] == "live-sess"
    assert peek["tail_total"] == 2
    assert [e["kind"] for e in peek["tail"]] == ["end_turn"]
    assert peek["tail"][0]["text"] == "interim note"
    assert len(peek["end_turns"]) == 1

    by_session = client.get("/sessions/live-sess/peek", headers=_AUTH).json()
    assert by_session["job_id"] == job_id
    assert [e["kind"] for e in by_session["tail"]] == ["tool_use", "end_turn"]
    assert by_session["tail"][0]["tool"] == "Read"

    assert client.get("/job/nope/peek", headers=_AUTH).status_code == 404
    assert client.get("/sessions/nope/peek", headers=_AUTH).status_code == 404
    assert client.get(f"/job/{job_id}/peek").status_code == 401, "peek needs the bearer token"

    release.set()
    _wait_completed(client, job_id)
    done = client.get(f"/job/{job_id}/peek", headers=_AUTH).json()
    assert done["status"] == "completed" and done["running"] is False


def test_session_peek_finds_the_newest_job_on_a_resumed_session(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(runner_mod, "run_local_blocking", _streaming_run(started, release, session_id="shared"))
    client, _ = _client(tmp_path, monkeypatch)
    first = client.post("/run", json={"prompt": "one"}, headers=_AUTH).json()["job_id"]
    assert started.wait(2.0)
    release.set()
    _wait_completed(client, first)
    started.clear()
    release.clear()
    second = client.post("/run", json={"prompt": "two", "resume_session_id": "shared"}, headers=_AUTH).json()["job_id"]
    assert started.wait(2.0)
    assert client.get("/sessions/shared/peek", headers=_AUTH).json()["job_id"] == second
    release.set()
    _wait_completed(client, second)


def test_run_refuses_a_flag_shaped_resume_id_before_spawning(tmp_path, monkeypatch):
    """The runner is the policy boundary: a resume id that would parse as a
    CLI flag (``--resume [value]`` is optional-argument) is a 400, whatever
    the tool side sent, and nothing is spawned."""
    spawned: list = []
    monkeypatch.setattr(
        runner_mod,
        "run_local_blocking",
        lambda *a, **k: spawned.append(a) or bridge.ClaudeCodeResult(ok=True),
    )
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.post(
        "/run",
        json={"prompt": "x", "resume_session_id": "--dangerously-skip-permissions"},
        headers=_AUTH,
    )
    assert resp.status_code == 400
    assert "session id" in resp.json()["detail"]
    time.sleep(0.05)
    assert spawned == []
