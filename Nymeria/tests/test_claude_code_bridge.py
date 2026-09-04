"""Tests for the Claude Code bridge engine (pure logic).

Covers the transport-agnostic pieces shared by the claude_code tool and the host
runner: permission-mode mapping, working-directory allowlisting, CLI-argument
assembly, JSON result parsing, and the session store.
"""

import io
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest

from nymeria.tools import claude_code_bridge as b


# --- map_mode ----------------------------------------------------------------


@pytest.mark.parametrize(
    "token,expected",
    [
        (None, "dontAsk"),
        ("", "dontAsk"),
        ("dont_ask", "dontAsk"),
        ("safe", "dontAsk"),
        ("plan", "plan"),
        ("accept_edits", "acceptEdits"),
        ("auto", "auto"),
        ("bypass", "bypassPermissions"),
        ("yolo", "bypassPermissions"),
        ("default", "default"),
        ("BYPASS", "bypassPermissions"),
    ],
)
def test_map_mode_aliases(token, expected):
    assert b.map_mode(token) == expected


def test_map_mode_invalid_raises():
    with pytest.raises(b.ClaudeCodeError):
        b.map_mode("nuke-everything")


# --- parse_roots / resolve_cwd_against_roots ---------------------------------


def test_parse_roots_default_to_project_root(tmp_path):
    roots = b.parse_roots(None, tmp_path)
    assert roots == [tmp_path.resolve()]


def test_parse_roots_comma_and_pathsep(tmp_path):
    a = tmp_path / "a"
    c = tmp_path / "c"
    raw = f"{a},{c}"
    roots = b.parse_roots(raw, tmp_path)
    assert a.resolve() in roots and c.resolve() in roots


def test_resolve_cwd_default_is_root(tmp_path):
    assert b.resolve_cwd_against_roots(None, [tmp_path], tmp_path) == tmp_path.resolve()


def test_resolve_cwd_relative_resolves_under_root(tmp_path):
    (tmp_path / "pkg").mkdir()
    out = b.resolve_cwd_against_roots("pkg", [tmp_path], tmp_path)
    assert out == (tmp_path / "pkg").resolve()


def test_resolve_cwd_outside_allowlist_raises(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(b.ClaudeCodeError) as exc:
        b.resolve_cwd_against_roots(str(outside), [root], root)
    assert "outside the allowed roots" in str(exc.value)


def test_resolve_cwd_missing_raises(tmp_path):
    with pytest.raises(b.ClaudeCodeError):
        b.resolve_cwd_against_roots("does-not-exist", [tmp_path], tmp_path)


# --- build_cli_args ----------------------------------------------------------


def test_build_cli_args_core_flags():
    cfg = b.ClaudeCodeRunConfig(executable="claude", model="opus")
    req = b.ClaudeCodeRequest(prompt="hi", cwd="/tmp", permission_mode="dontAsk")
    args = b.build_cli_args(req, cfg)
    assert args[0] == "claude"
    assert "-p" in args
    # stream-json (one event per line, one ``result`` per end-turn) is the
    # default, and the CLI requires --verbose with it under -p.
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in args
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--model") + 1] == "opus"
    # The hard deny list is always present.
    assert "--disallowedTools" in args
    # No prompt in argv (it is fed on stdin).
    assert "hi" not in args


def test_build_cli_args_optional_flags():
    cfg = b.ClaudeCodeRunConfig(
        executable="claude",
        max_turns=20,
        max_budget_usd=1.5,
        bare=True,
        fallback_model="sonnet",
    )
    req = b.ClaudeCodeRequest(
        prompt="x", cwd="/tmp", permission_mode="plan", resume_session_id="sess-1"
    )
    args = b.build_cli_args(req, cfg)
    assert args[args.index("--max-turns") + 1] == "20"
    assert args[args.index("--max-budget-usd") + 1] == "1.5"
    assert args[args.index("--fallback-model") + 1] == "sonnet"
    assert "--bare" in args
    assert args[args.index("--resume") + 1] == "sess-1"


def test_build_cli_args_json_format_has_no_verbose():
    cfg = b.ClaudeCodeRunConfig(executable="claude", output_format="json")
    req = b.ClaudeCodeRequest(prompt="x", cwd="/tmp")
    args = b.build_cli_args(req, cfg)
    assert args[args.index("--output-format") + 1] == "json"
    assert "--verbose" not in args


# --- parse_cli_result --------------------------------------------------------


def test_parse_cli_result_success():
    obj = {
        "result": "all done",
        "session_id": "abc",
        "subtype": "success",
        "is_error": False,
        "num_turns": 3,
        "duration_ms": 1234,
        "total_cost_usd": 0.02,
        "usage": {"input_tokens": 10},
    }
    res = b.parse_cli_result(json.dumps(obj), "", 0)
    assert res.ok and not res.is_error
    assert res.result_text == "all done"
    assert res.session_id == "abc"
    assert res.num_turns == 3
    assert res.total_cost_usd == 0.02


def test_parse_cli_result_error_subtype():
    obj = {"result": "", "subtype": "error_max_turns", "is_error": True, "session_id": "z"}
    res = b.parse_cli_result(json.dumps(obj), "", 0)
    assert res.is_error and not res.ok
    assert res.subtype == "error_max_turns"


def test_parse_cli_result_subtype_only_error_without_is_error_flag():
    # The CLI may signal failure via subtype alone; is_error must derive from it.
    obj = {"result": "partial", "subtype": "error_during_execution", "session_id": "z"}
    res = b.parse_cli_result(json.dumps(obj), "", 0)
    assert res.is_error and not res.ok


def test_parse_cli_result_non_numeric_fields_coerced_not_crashing():
    obj = {
        "result": "ok",
        "subtype": "success",
        "num_turns": "3",
        "duration_ms": None,
        "total_cost_usd": "oops",
    }
    res = b.parse_cli_result(json.dumps(obj), "", 0)
    assert res.num_turns == 3
    assert res.duration_ms is None
    assert res.total_cost_usd is None
    # Formatting must not raise on the coerced values.
    assert "run summary" in res.format_for_agent()


def test_parse_cli_result_non_json_clean_exit_is_text():
    res = b.parse_cli_result("plain text reply", "", 0)
    assert res.ok and res.result_text == "plain text reply"


def test_parse_cli_result_non_json_error_exit():
    res = b.parse_cli_result("", "boom on stderr", 1)
    assert not res.ok and res.is_error
    assert "boom on stderr" in (res.error or "")


def test_parse_cli_result_embedded_json_last_line():
    out = 'some preamble\n{"result": "ok", "subtype": "success", "session_id": "s"}'
    res = b.parse_cli_result(out, "", 0)
    assert res.ok and res.result_text == "ok" and res.session_id == "s"


# --- SessionStore ------------------------------------------------------------


def test_session_store_roundtrip(tmp_path):
    store = b.SessionStore(tmp_path / "sessions.json")
    assert store.get("t1", "/repo") is None
    store.set("t1", "/repo", "sess-1")
    assert store.get("t1", "/repo") == "sess-1"
    # cwd-scoped: a different cwd is a different key.
    assert store.get("t1", "/other") is None


def test_session_store_set_empty_is_noop(tmp_path):
    store = b.SessionStore(tmp_path / "sessions.json")
    store.set("t1", "/repo", "")
    assert store.get("t1", "/repo") is None


# --- git summary -------------------------------------------------------------


def test_git_snapshot_non_repo_is_empty(tmp_path):
    snap = b.git_snapshot(str(tmp_path))
    assert snap.head is None
    assert snap.dirty == set()


def _git_init(cwd):
    import subprocess

    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t.t"],
        ["config", "user.name", "t"],
        ["commit", "--allow-empty", "-q", "-m", "base"],
    ):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_git_diff_summary_reports_only_run_changes(tmp_path):
    """The before/after diff must report files changed DURING the run, not the
    user's pre-existing dirty files (regression: the union used to report all)."""
    cwd = str(tmp_path)
    _git_init(cwd)
    # A pre-existing dirty file (the user's, not Claude Code's).
    (tmp_path / "preexisting.txt").write_text("user edit")

    before = b.git_snapshot(cwd)
    # Claude Code "changes" a new file during the run.
    (tmp_path / "new_by_cc.txt").write_text("cc edit")

    files, commits = b.git_diff_summary(before, cwd)
    assert "new_by_cc.txt" in files
    assert "preexisting.txt" not in files
    assert commits == []


def test_git_diff_summary_reports_commits(tmp_path):
    import subprocess

    cwd = str(tmp_path)
    _git_init(cwd)
    before = b.git_snapshot(cwd)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=cwd, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "cc commit"], cwd=cwd, check=True, capture_output=True
    )
    files, commits = b.git_diff_summary(before, cwd)
    assert "f.txt" in files
    assert any("cc commit" in c for c in commits)


def test_the_git_summary_is_confined(monkeypatch):
    """`git` runs as the WRAPPED argv, not as the raw command.

    The argv is fixed and read-only, so the reason is not what this call does,
    it is where it does it: `cwd` is the agent-named working directory, and a
    git repository carries executable configuration. A planted `.git/config`
    with `core.fsmonitor` makes `git status` run a script of the repo's
    choosing. Measured on this host: that script reads the launching process's
    `/proc/<pid>/environ` unsandboxed and is denied it under the policy.

    The failure this guards is the two-line shape reading as confined while
    running unconfined: build a launch, then spawn the original command.
    """
    captured: dict = {}

    def fake_launch(argv, kwargs, *, creation_roots=None):
        captured["argv"] = list(argv)
        captured["creation_roots"] = creation_roots
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        # The real wrapper MUTATES the caller's kwargs to carry the policy in
        # the child env, and that half is as load-bearing as the argv: a launch
        # that gets the shim but not the policy makes the shim refuse to run at
        # all. Standing in for that mutation here is what makes the assertion
        # below able to see a caller that passes a COPY of its kwargs.
        kwargs["env"] = {**kwargs["env"], "NYMERIA_SANDBOX_POLICY": "sentinel"}
        return ["/shim", *argv]

    def fake_run(cmd, **kwargs):
        captured["spawned"] = list(cmd)
        captured["spawn_env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="deadbeef\n", stderr="")

    monkeypatch.setattr(b, "sandbox_argv_launch", fake_launch)
    monkeypatch.setattr(b.subprocess, "run", fake_run)

    assert b._git(["rev-parse", "HEAD"], "/some/repo") == "deadbeef\n"

    assert captured["argv"] == ["git", "rev-parse", "HEAD"]
    assert captured["spawned"] == ["/shim", "git", "rev-parse", "HEAD"]
    assert captured["cwd"] == "/some/repo"
    # Default creation roots. The child is git in someone else's checkout, so
    # its write area is the same concession bash_execute gets, not a narrower
    # one this surface could honestly claim.
    assert captured["creation_roots"] is None
    # The wrapper refuses a launch with no explicit env; the scrub is above it.
    assert captured["env"] is not None
    # The policy has to reach the CHILD, not just the wrapper. Passing a copy of
    # the kwargs (`sandbox_argv_launch(argv, dict(spawn_kwargs))`) still gets the
    # shim argv, so every other assertion here passes, and in production the
    # shim then refuses every call with "no policy set; refusing to run
    # unsandboxed". Measured: without this line that mutation is invisible on a
    # host with no Landlock, and on one with Landlock it surfaces only as a
    # confusing failure in the unrelated git-diff tests.
    assert captured["spawn_env"]["NYMERIA_SANDBOX_POLICY"] == "sentinel"


def test_a_sandbox_refusal_drops_the_summary_rather_than_the_run(monkeypatch):
    """A policy that cannot be built costs the summary, not the Claude Code run.

    Wrapping inside the try is not unusual (`core/python_custom_tools.py` and
    `core/hooks/actions.py` both do it, each to keep a total-function contract).
    What is unusual, and what this pins, is that the refusal is SWALLOWED into
    the same `None` a non-repo returns rather than surfaced in the surface's own
    error shape. That is right for a best-effort diff and wrong anywhere the
    launch is the answer. What must never happen is the third option: degrading
    to an unconfined launch, which is why the spawn list is asserted empty.
    """
    from nymeria.exec_sandbox import SandboxError

    def refusing_launch(argv, kwargs, *, creation_roots=None):
        raise SandboxError("carve budget exceeded")

    spawned = []
    monkeypatch.setattr(b, "sandbox_argv_launch", refusing_launch)
    monkeypatch.setattr(b.subprocess, "run", lambda cmd, **kw: spawned.append(cmd))

    assert b._git(["rev-parse", "HEAD"], "/some/repo") is None
    assert spawned == [], "a refused launch must not fall through to a raw spawn"
    # The caller above it degrades rather than raising.
    snap = b.git_snapshot("/some/repo")
    assert snap.head is None and snap.dirty == set()


# --- cancellation ------------------------------------------------------------


_RESULT_LINE = json.dumps({"type": "result", "subtype": "success", "result": "ok",
                           "session_id": "sess-fake"}) + "\n"


class _CapturingStdin(io.StringIO):
    """Keeps what was written after the bridge closes it."""

    def close(self):
        self.written = self.getvalue()
        super().close()


class _FakeProc:
    """A Popen stand-in with real pipes: stdout stays open (the reader pump
    blocks, as on a live child) until the process finishes or is killed."""

    def __init__(self, *, finish_after_polls=None, returncode=0, stdout=_RESULT_LINE):
        self.pid = 4242
        self.returncode = None
        self._rc = returncode
        self._payload = stdout
        self._polls = 0
        self._finish_after = finish_after_polls  # None = never finish alone
        self._killed = False
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        self.stdout = os.fdopen(out_r, "r")
        self._stdout_w = os.fdopen(out_w, "w")
        self.stderr = os.fdopen(err_r, "r")
        self._stderr_w = os.fdopen(err_w, "w")
        self.stdin = _CapturingStdin()

    def _finish(self, rc, *, write=True):
        if self.returncode is not None:
            return
        if write:
            self._stdout_w.write(self._payload)
        self._stdout_w.close()
        self._stderr_w.close()
        self.returncode = rc

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is not None:
            return self.returncode
        self._polls += 1
        if self._finish_after is not None and self._polls >= self._finish_after:
            self._finish(self._rc)
            return self.returncode
        if timeout:
            time.sleep(min(timeout, 0.02))
        raise b.subprocess.TimeoutExpired(cmd="claude", timeout=timeout)


def _kill_spy(killed):
    def _spy(proc, grace=b.GROUP_KILL_GRACE_SECONDS):
        killed.append(proc)
        proc._killed = True
        proc._finish(-15, write=False)  # a killed child closes its pipes

    return _spy


def test_run_local_blocking_cancels_via_cancel_check(monkeypatch, tmp_path):
    proc = _FakeProc()  # never finishes on its own
    killed: list = []
    monkeypatch.setattr(b.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(b, "git_snapshot", lambda cwd: b.GitSnapshot(head=None, dirty=set()))
    monkeypatch.setattr(b, "git_diff_summary", lambda before, cwd: ([], []))
    monkeypatch.setattr(b, "terminate_process_group", _kill_spy(killed))

    cfg = b.ClaudeCodeRunConfig(executable="claude")
    req = b.ClaudeCodeRequest(prompt="hi", cwd=str(tmp_path), permission_mode="dontAsk")
    res = b.run_local_blocking(
        req,
        cfg,
        timeout=30,
        env=b.build_subprocess_env(bare=False),
        cancel_check=lambda: True,
        poll_interval=0.01,
    )

    assert res.subtype == "cancelled"
    assert res.is_error is True
    assert killed and killed[0] is proc  # the process group was killed


def test_run_local_blocking_group_kills_on_timeout(monkeypatch, tmp_path):
    proc = _FakeProc()
    killed: list = []
    monkeypatch.setattr(b.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(b, "git_snapshot", lambda cwd: b.GitSnapshot(head=None, dirty=set()))
    monkeypatch.setattr(b, "git_diff_summary", lambda before, cwd: ([], []))
    monkeypatch.setattr(b, "terminate_process_group", _kill_spy(killed))

    cfg = b.ClaudeCodeRunConfig(executable="claude")
    req = b.ClaudeCodeRequest(prompt="hi", cwd=str(tmp_path), permission_mode="dontAsk")
    res = b.run_local_blocking(
        req, cfg, timeout=0.05,
        env=b.build_subprocess_env(bare=False), poll_interval=0.01,
    )

    assert res.subtype == "timeout"
    assert killed and killed[0] is proc


def test_run_local_blocking_success_path_uses_popen(monkeypatch, tmp_path):
    proc = _FakeProc(finish_after_polls=1)  # completes on the first wait
    monkeypatch.setattr(b.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(b, "git_snapshot", lambda cwd: b.GitSnapshot(head=None, dirty=set()))
    monkeypatch.setattr(b, "git_diff_summary", lambda before, cwd: (["x.py"], ["abc done"]))

    cfg = b.ClaudeCodeRunConfig(executable="claude")
    req = b.ClaudeCodeRequest(prompt="hi", cwd=str(tmp_path), permission_mode="dontAsk")
    res = b.run_local_blocking(
        req, cfg, timeout=5, env=b.build_subprocess_env(bare=False)
    )

    assert res.ok is True
    assert res.result_text == "ok"
    assert res.session_id == "sess-fake"
    assert [t.text for t in res.end_turns] == ["ok"]
    assert res.files_changed == ["x.py"]
    assert res.commits == ["abc done"]
    # The prompt went down stdin, never argv.
    assert proc.stdin.written == "hi"


def test_run_local_blocking_streams_every_end_turn_to_the_observer(monkeypatch, tmp_path):
    """A run that ends two turns (re-invoked by background subagents) reports
    both through the observer AS THEY ARRIVE and the result carries both;
    the terminal result text is the last turn's."""
    init = json.dumps({"type": "system", "subtype": "init", "session_id": "sess-2"})
    first = json.dumps({"type": "result", "subtype": "success", "result": "suite running, will pick up"})
    tool = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}})
    second = json.dumps({"type": "result", "subtype": "success", "result": "all green, committed"})
    proc = _FakeProc(finish_after_polls=3)
    # Feed the stream in two bursts: the first end-turn lands before the
    # process finishes, the second at exit.
    proc._stdout_w.write(init + "\n" + first + "\n")
    proc._stdout_w.flush()
    proc._payload = tool + "\n" + second + "\n"
    monkeypatch.setattr(b.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(b, "git_snapshot", lambda cwd: b.GitSnapshot(head=None, dirty=set()))
    monkeypatch.setattr(b, "git_diff_summary", lambda before, cwd: ([], []))

    seen: list[tuple[int, int, str]] = []
    observer = b.RunObserver()
    sessions: list[str] = []
    observer.on_session = sessions.append
    observer.on_end_turn = lambda t: seen.append((t.index, proc._polls, t.text))

    cfg = b.ClaudeCodeRunConfig(executable="claude")
    req = b.ClaudeCodeRequest(prompt="go", cwd=str(tmp_path))
    res = b.run_local_blocking(
        req, cfg, timeout=5, env=b.build_subprocess_env(bare=False),
        observer=observer, poll_interval=0.01,
    )

    assert sessions == ["sess-2"]
    assert [i for i, _, _ in seen] == [1, 2]
    assert seen[0][2] == "suite running, will pick up"
    assert seen[0][1] < 3, "the first end-turn was observed before the process exited"
    assert res.ok is True
    assert res.result_text == "all green, committed"
    assert res.session_id == "sess-2"
    assert [t.index for t in res.end_turns] == [1, 2]
    tail = observer.snapshot(10)
    assert tail["running"] is False
    kinds = [e["kind"] for e in tail["tail"]]
    assert kinds == ["end_turn", "tool_use", "end_turn"]
    assert tail["tail"][1]["tool"] == "Bash"


def test_run_local_blocking_without_result_event_falls_back_to_raw_parse(monkeypatch, tmp_path):
    """A child that dies before any ``result`` (no JSON) is an error result
    built from its exit, and the observer's partial view still reports."""
    proc = _FakeProc(finish_after_polls=1, returncode=1, stdout="not json at all\n")
    proc._stderr_w.write("boom on stderr")
    monkeypatch.setattr(b.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(b, "git_snapshot", lambda cwd: b.GitSnapshot(head=None, dirty=set()))
    monkeypatch.setattr(b, "git_diff_summary", lambda before, cwd: ([], []))

    cfg = b.ClaudeCodeRunConfig(executable="claude")
    req = b.ClaudeCodeRequest(prompt="x", cwd=str(tmp_path))
    res = b.run_local_blocking(req, cfg, timeout=5, env=b.build_subprocess_env(bare=False))
    assert res.ok is False and res.is_error is True
    assert "boom on stderr" in (res.error or "")
    assert res.end_turns == []


def test_cancelled_run_keeps_the_turns_seen_so_far(monkeypatch, tmp_path):
    proc = _FakeProc()
    proc._stdout_w.write(json.dumps({"type": "result", "subtype": "success",
                                     "result": "partial", "session_id": "s-c"}) + "\n")
    proc._stdout_w.flush()
    killed: list = []
    monkeypatch.setattr(b.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(b, "git_snapshot", lambda cwd: b.GitSnapshot(head=None, dirty=set()))
    monkeypatch.setattr(b, "terminate_process_group", _kill_spy(killed))
    observer = b.RunObserver()
    flags = iter([False, False, True])
    cfg = b.ClaudeCodeRunConfig(executable="claude")
    req = b.ClaudeCodeRequest(prompt="x", cwd=str(tmp_path))
    res = b.run_local_blocking(
        req, cfg, timeout=5, env=b.build_subprocess_env(bare=False),
        cancel_check=lambda: next(flags, True), poll_interval=0.01, observer=observer,
    )
    assert res.subtype == "cancelled"
    assert res.session_id == "s-c"
    assert [t.text for t in res.end_turns] == ["partial"]
    assert killed


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group kill")
def test_terminate_process_group_sigterm_then_sigkill(monkeypatch):
    sigs: list = []

    class _P:
        pid = 999

        def poll(self):
            return None  # still alive

        def wait(self, timeout=None):
            raise b.subprocess.TimeoutExpired("claude", timeout)  # force SIGKILL

    monkeypatch.setattr(b.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(b.os, "killpg", lambda pgid, sig: sigs.append(sig))

    b.terminate_process_group(_P(), grace=0.01)

    assert b.signal.SIGTERM in sigs
    assert b.signal.SIGKILL in sigs


def test_terminate_process_group_noop_when_already_exited(monkeypatch):
    calls: list = []

    class _P:
        pid = 1

        def poll(self):
            return 0  # already exited

    monkeypatch.setattr(b.os, "killpg", lambda pgid, sig: calls.append(sig))
    b.terminate_process_group(_P())
    assert calls == []  # nothing signalled


def _fake_http(monkeypatch, status, body):
    """Stand in for the policy HTTP client: every GET answers ``status``/``body``."""
    class _Resp:
        status_code = status
        text = json.dumps(body) if isinstance(body, dict) else body

        def json(self):
            if not isinstance(body, dict):
                raise ValueError("not json")
            return body

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None, headers=None):
            return _Resp()

    monkeypatch.setattr(b, "_http_client", lambda timeout: _Client())


def test_remote_peek_404_tells_a_missing_route_from_an_expired_job(monkeypatch):
    """Bug 2 (2026-09-04): the one 404 message blamed "unknown job, or an old
    runner" and quoted only the runner's job id. FastAPI's bare ``Not Found``
    means the route is missing (old service); the runner's own ``job not
    found`` means its record is gone."""
    client = b.RemoteRunnerClient("http://runner:8200", "tok")
    _fake_http(monkeypatch, 404, {"detail": "Not Found"})
    with pytest.raises(b.RemoteRunnerError, match="predates the peek endpoint.*restart it"):
        client.peek("0ab7ee1d41ee55a1")
    _fake_http(monkeypatch, 404, {"detail": "job not found"})
    with pytest.raises(b.RemoteRunnerError, match="no longer has its job 0ab7ee1d41ee55a1"):
        client.peek("0ab7ee1d41ee55a1")
    _fake_http(monkeypatch, 404, "<html>gateway</html>")
    with pytest.raises(b.RemoteRunnerError, match="predates the peek endpoint"):
        client.peek("x")
    _fake_http(monkeypatch, 200, {"session_id": "s", "running": True, "tail": [], "tail_total": 0})
    assert client.peek("x")["session_id"] == "s"
