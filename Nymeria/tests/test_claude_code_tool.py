"""Tests for the claude_code tool orchestration (transport + sessions + modes).

The detached/background path is exercised in test_claude_code_background.py; here
we drive the synchronous (no-thread-context) path so producers run inline and we
can assert routing, session persistence, and mode handling without threads.
"""

import importlib
import json
from types import SimpleNamespace


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
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_local_sync_returns_result_and_persists_session(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(claude_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        claude_module, "resolve_claude_executable", lambda: "/usr/bin/claude"
    )

    def fake_run_local(request, config, timeout, env=None):
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

    def fake_run_local(request, config, timeout, env=None):
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

    def fake_run_local(request, config, timeout, env=None):
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
