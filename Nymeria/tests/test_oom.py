"""Tests for nymeria.oom: OOM-deprioritizing tool subprocesses.

The helper biases the Linux OOM killer toward tool subprocesses so a memory
spike evicts the offending tool, not the API server. The unit tests cover the
platform gating and the kwargs merge; one Linux-gated integration test spawns a
real child and confirms it reports the raised oom_score_adj.
"""

import os
import subprocess
import sys

import pytest

from nymeria import oom


# --- oom_score_preexec -------------------------------------------------------


def test_preexec_returns_callable_on_linux(monkeypatch):
    monkeypatch.setattr(oom.sys, "platform", "linux")
    fn = oom.oom_score_preexec()
    assert callable(fn)


def test_preexec_is_none_off_linux(monkeypatch):
    for platform in ("win32", "darwin"):
        monkeypatch.setattr(oom.sys, "platform", platform)
        assert oom.oom_score_preexec() is None


# --- with_tool_oom_score -----------------------------------------------------


def test_merge_adds_preexec_on_linux(monkeypatch):
    monkeypatch.setattr(oom.sys, "platform", "linux")
    kwargs: dict = {"shell": True}
    out = oom.with_tool_oom_score(kwargs)
    assert out is kwargs  # mutates in place, returns same dict
    assert callable(kwargs["preexec_fn"])


def test_merge_is_noop_off_linux(monkeypatch):
    monkeypatch.setattr(oom.sys, "platform", "darwin")
    kwargs: dict = {"shell": True}
    oom.with_tool_oom_score(kwargs)
    assert "preexec_fn" not in kwargs


def test_merge_never_clobbers_existing_preexec(monkeypatch):
    monkeypatch.setattr(oom.sys, "platform", "linux")
    sentinel = lambda: None  # noqa: E731 - test sentinel
    kwargs: dict = {"preexec_fn": sentinel}
    oom.with_tool_oom_score(kwargs)
    assert kwargs["preexec_fn"] is sentinel


# --- real spawn (Linux) ------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "linux" or not os.path.exists("/proc/self/oom_score_adj"),
    reason="oom_score_adj only adjustable on Linux with /proc",
)
def test_child_reports_raised_oom_score():
    """A child spawned with the preexec reports the raised score; a control
    child (no preexec) keeps the inherited default. Raising the score needs no
    capabilities, so this holds even in a cap-dropped container."""
    score = 654  # distinctive, mid-range value

    def _read_child(preexec):
        return subprocess.run(
            ["sh", "-c", "cat /proc/self/oom_score_adj"],
            capture_output=True,
            text=True,
            preexec_fn=preexec,
        ).stdout.strip()

    tagged = _read_child(oom.oom_score_preexec(score))
    control = _read_child(None)

    assert tagged == str(score)
    assert control != str(score) or int(control) >= score


# --- spawn-site wiring -------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX spawn kwargs")
def test_background_bash_kwargs_are_oom_tagged():
    from nymeria.tools.bash import _background_popen_kwargs

    kwargs = _background_popen_kwargs(
        "/tmp",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert kwargs.get("start_new_session") is True
    if sys.platform == "linux":
        assert callable(kwargs.get("preexec_fn"))


def test_run_local_blocking_passes_oom_preexec(monkeypatch, tmp_path):
    """The claude_code local spawn (the OOM incident's trigger) wires the OOM
    preexec into its Popen call, and starts the child in its own process group
    so Claude Code's whole node/ripgrep tree is group-killable on cancel."""
    from types import SimpleNamespace

    from nymeria.tools import claude_code_bridge as b

    captured: dict = {}

    class _FakeProc:
        returncode = 0

        def __init__(self, args, **kwargs):
            captured.update(kwargs)

        def communicate(self, input=None, timeout=None):
            return ('{"result": "ok", "subtype": "success"}', "")

        def poll(self):
            return 0

    monkeypatch.setattr(b.subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(b, "git_snapshot", lambda cwd: SimpleNamespace(head=None, dirty=set()))
    monkeypatch.setattr(b, "git_diff_summary", lambda before, cwd: ([], []))

    cfg = b.ClaudeCodeRunConfig(executable="claude")
    req = b.ClaudeCodeRequest(prompt="hi", cwd=str(tmp_path), permission_mode="dontAsk")
    result = b.run_local_blocking(req, cfg, timeout=5, env=b.build_subprocess_env(bare=False))

    assert result.result_text == "ok"
    # preexec_fn is always passed (None off Linux, the OOM callable on Linux).
    assert "preexec_fn" in captured
    if sys.platform != "win32":
        assert captured.get("start_new_session") is True
    if sys.platform == "linux":
        assert callable(captured["preexec_fn"])
