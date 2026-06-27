"""Tests for the host-side Claude Code runner service.

The runner is the policy boundary: it requires a bearer token, re-resolves the
working-directory allowlist, and runs Claude Code in a worker thread. The actual
``claude`` invocation is mocked so no real Claude Code is spawned.
"""

import importlib
import time
from types import SimpleNamespace

import pytest

runner_mod = importlib.import_module("nymeria.gateway.claude_code_runner")
bridge = importlib.import_module("nymeria.tools.claude_code_bridge")

fastapi_testclient = pytest.importorskip("fastapi.testclient")
TestClient = fastapi_testclient.TestClient


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
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _client(tmp_path, monkeypatch, **overrides):
    settings = _settings(tmp_path, **overrides)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(runner_mod, "resolve_claude_executable", lambda: "/usr/bin/claude")
    app = runner_mod.create_app()
    return TestClient(app), settings


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

    def fake_run_local(request, config, timeout, env=None):
        captured["cwd"] = request.cwd
        captured["mode"] = request.permission_mode
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


def test_job_not_found(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    resp = client.get("/job/nope", headers={"Authorization": "Bearer secret-token"})
    assert resp.status_code == 404


def test_create_app_requires_token_unless_insecure(tmp_path, monkeypatch):
    settings = _settings(tmp_path, nymeria_claude_code_token=None)
    monkeypatch.setattr(runner_mod, "get_settings", lambda: settings)
    with pytest.raises(RuntimeError):
        runner_mod.create_app()
    # insecure mode is allowed without a token
    app = runner_mod.create_app(allow_insecure=True)
    assert app is not None
