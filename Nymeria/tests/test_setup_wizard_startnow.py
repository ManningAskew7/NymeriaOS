"""Opt-in start-now and the post-start chat smoke test.

Split out of the former monolithic test_setup_wizard.py (dev-todo #54).
"""


from __future__ import annotations

import pytest
from nymeria.onboarding import HostingOption
from nymeria.setup import finalize as finalize_mod
from nymeria.setup.runner import main as setup_main

from _setup_wizard_helpers import (  # type: ignore[import-not-found]
    _capture_console,
    _serve_chat_sync,
    _stub_llm,
)


# --- opt-in start-now -------------------------------------------------------


def test_run_init_parser_and_state_accept_start_flag():
    from nymeria.onboarding import NextAction
    from nymeria.setup.runner import _build_state, build_parser

    args = build_parser().parse_args(
        ["--hosting", "docker", "--provider", "anthropic",
         "--model", "m", "--api-key", "k", "--start"]
    )
    assert args.start is True
    state = _build_state(args)
    assert state.next_action is NextAction.START_API_OPEN_FRONTEND
    # Pre-seeded so the interactive start-now step reflects the flag.
    assert state.extras.get("start_now") is NextAction.START_API_OPEN_FRONTEND

    # --start wins over --next-action.
    args2 = build_parser().parse_args(
        ["--provider", "anthropic", "--model", "m", "--api-key", "k",
         "--next-action", "print_commands", "--start"]
    )
    assert _build_state(args2).next_action is NextAction.START_API_OPEN_FRONTEND

    # Without an explicit choice, nothing is pre-seeded (shape default applies).
    args3 = build_parser().parse_args(
        ["--hosting", "docker", "--provider", "anthropic", "--model", "m",
         "--api-key", "k"]
    )
    assert _build_state(args3).next_action is NextAction.PRINT_COMMANDS
    assert "start_now" not in _build_state(args3).extras


def test_start_now_step_applies_and_choices_are_shape_aware():
    from nymeria.onboarding import NextAction
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps.start_now import start_now_applies, start_now_choices

    assert start_now_applies(WizardState(hosting=HostingOption.LOCAL)) is True
    assert start_now_applies(WizardState(hosting=HostingOption.DOCKER)) is True
    assert start_now_applies(WizardState(hosting=HostingOption.SERVICE)) is True
    # A CLI handoff chosen by flag suppresses the start-now question.
    assert (
        start_now_applies(
            WizardState(hosting=HostingOption.LOCAL, next_action=NextAction.CLI)
        )
        is False
    )

    docker = start_now_choices(HostingOption.DOCKER)
    assert docker[0].value is NextAction.START_API_OPEN_FRONTEND  # docker -> start
    local = start_now_choices(HostingOption.LOCAL)
    assert local[0].value is NextAction.PRINT_COMMANDS  # local -> print
    # The service install is detached like Docker, so it defaults to starting.
    service = start_now_choices(HostingOption.SERVICE)
    assert service[0].value is NextAction.START_API_OPEN_FRONTEND
    assert "service install" in service[1].description
    for choices in (docker, local, service):
        assert {c.value for c in choices} == {
            NextAction.START_API_OPEN_FRONTEND,
            NextAction.PRINT_COMMANDS,
        }


def test_start_now_is_in_default_flow_before_review():
    from nymeria.setup.nav import Navigator
    from nymeria.setup.state import WizardState
    from nymeria.setup.steps import build_default_steps

    steps = build_default_steps()
    ids = [step.id for step in steps]
    assert "start_now" in ids
    assert ids.index("start_now") < ids.index("review")

    def applicable(state: "WizardState") -> list[str]:
        nav = Navigator(steps, state)
        nav.start()
        return [steps[i].id for i in nav.applicable_indices()]

    assert "start_now" in applicable(WizardState(hosting=HostingOption.LOCAL))
    assert "start_now" in applicable(WizardState(hosting=HostingOption.DOCKER))
    assert "start_now" in applicable(WizardState(hosting=HostingOption.SERVICE))


def test_finalize_starts_docker_when_opted_in(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    calls: list[tuple[list[str], object]] = []
    fake_token = "nym_dockertoken_abc123"

    class _Result:
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout

    def fake_run(cmd, *args, **kwargs):
        calls.append((cmd, kwargs.get("cwd")))
        # After a clean start the wizard execs into the container to read the
        # real bootstrap token from its /data volume.
        if "exec" in cmd:
            return _Result(stdout=f"Token: {fake_token}\n")
        return _Result()

    health: list[dict] = []
    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        finalize_mod, "wait_for_health", lambda **kw: bool(health.append(kw)) or True
    )
    # Keep the start-now path hermetic: the real smoke turn would hit whatever
    # answers on localhost:8000 (the dedicated smoke tests cover it).
    monkeypatch.setattr(
        finalize_mod, "run_chat_smoke_test", lambda **kw: (True, "ok")
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "docker",
         "--root", str(root), "--start", "--non-interactive"]
    )
    out = capsys.readouterr().out

    assert rc == 0
    # The compose `up -d` runs first, in the runtime root...
    assert calls[0][0] == ["docker", "compose", "-f", "docker-compose.single.yml", "up", "-d"]
    assert calls[0][1] == str(root)
    assert health  # polled for health after a clean start
    assert "Nymeria is up" in out
    # ...then the wizard execs in to surface the container-minted token (there is
    # no host token for the Docker shape).
    assert any("exec" in cmd and "cat" in cmd for cmd, _cwd in calls)
    assert fake_token in out


def test_finalize_starts_local_foreground_when_opted_in(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    calls: list[tuple[list[str], object, dict]] = []

    class _Result:
        returncode = 0

    def fake_run(cmd, *args, **kwargs):
        calls.append((cmd, kwargs.get("cwd"), kwargs.get("env") or {}))
        return _Result()

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        finalize_mod,
        "wait_for_health",
        lambda **kw: pytest.fail("local foreground start must not health-poll"),
    )
    # The launcher command comes from service_install.resolve_exec_argv, the one
    # place that knows every install shape. Under uv's Windows console-script
    # trampoline sys.argv[0] is `...\.local\bin\nymeria` (no such file, and
    # nymeria.exe is not a Python script), so `[sys.executable, argv0, "slim"]`
    # died with "can't open file" on the first public-beta Windows test.
    import nymeria.service_install as si

    launcher = ["C:\\Users\\t\\.local\\bin\\nymeria.exe", "slim"]
    monkeypatch.setattr(si, "resolve_exec_argv", lambda *a, **k: list(launcher))

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "local",
         "--root", str(root), "--start", "--non-interactive"]
    )

    assert rc == 0
    assert len(calls) == 1
    cmd, cwd, env = calls[0]
    assert cmd == launcher
    assert cwd == str(root)
    assert env.get("NYMERIA_PROJECT_ROOT") == str(root)


def test_local_foreground_copy_only_cites_a_token_that_was_printed(monkeypatch, tmp_path):
    """A reconfigure prints no bootstrap token (the admin already exists), so
    "paste the bootstrap token shown above" pointed at nothing on the first
    public-beta Windows test. A fresh non-interactive install did print one
    (browser handoff off), and keeps the paste instruction."""
    from nymeria.setup.state import WizardState

    class _Result:
        returncode = 0

    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: _Result())
    import nymeria.service_install as si

    monkeypatch.setattr(si, "resolve_exec_argv", lambda *a, **k: ["nymeria", "slim"])

    def run(**kw):
        console, out = _capture_console()
        finalize_mod._start_now_local(
            console, state=WizardState(), root=tmp_path, handoff_token=None, **kw
        )
        return out.getvalue()

    # The console wraps at 80 columns; compare on collapsed whitespace.
    reconfigure = " ".join(run(token_printed=False).split())
    assert "bootstrap token" not in reconfigure
    assert "sign in" in reconfigure
    fresh = " ".join(run(token_printed=True).split())
    assert "paste the bootstrap token from the handoff printed above" in fresh


# --- post-start chat smoke test ----------------------------------------------


def test_run_chat_smoke_test_round_trips_and_deletes_thread():
    base_url, seen, shutdown = _serve_chat_sync(lambda payload: (200, "INIT SMOKE OK"))
    try:
        ok, detail = finalize_mod.run_chat_smoke_test(
            token="nym_x", base_url=base_url, timeout=10.0
        )
    finally:
        shutdown()
    assert ok is True
    assert "INIT SMOKE OK" in detail
    posts = [s for s in seen if s[0] == "POST"]
    deletes = [s for s in seen if s[0] == "DELETE"]
    assert posts == [("POST", "/chat/sync", "Bearer nym_x")]
    # The throwaway thread is cleaned up with the same identity.
    assert deletes and deletes[0][1].startswith("/threads/init-smoke-")
    assert deletes[0][2] == "Bearer nym_x"


def test_run_chat_smoke_test_fails_on_empty_response_and_5xx():
    # An empty model response is a failure, not a pass on HTTP 200 alone.
    base_url, _seen, shutdown = _serve_chat_sync(lambda payload: (200, ""))
    try:
        ok, detail = finalize_mod.run_chat_smoke_test(
            token="t", base_url=base_url, timeout=10.0
        )
    finally:
        shutdown()
    assert ok is False
    assert "without a model response" in detail

    base_url, _seen, shutdown = _serve_chat_sync(lambda payload: (500, "boom"))
    try:
        ok, detail = finalize_mod.run_chat_smoke_test(
            token="t", base_url=base_url, timeout=10.0
        )
    finally:
        shutdown()
    assert ok is False
    assert "HTTP 500" in detail

    # A dead backend reports, never raises.
    ok, detail = finalize_mod.run_chat_smoke_test(
        token="t", base_url="http://127.0.0.1:1", timeout=2.0
    )
    assert ok is False
    assert "could not reach" in detail


def test_finalize_docker_start_runs_chat_smoke(monkeypatch, tmp_path, capsys):
    """The Docker start-now path reads the in-container SERVICE token for the
    smoke turn (the bootstrap token is consumed by its first auth, which would
    break the printed handoff) and reports before the token handoff."""
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    execs: list[list[str]] = []

    class _Result:
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout

    def fake_run(cmd, *args, **kwargs):
        if "exec" in cmd:
            execs.append(cmd)
            if any("SLIM_SERVICE_TOKEN" in part for part in cmd):
                return _Result(stdout="nym_servicetoken_x\n")
            return _Result(stdout="Token: nym_boot_y\n")
        return _Result()

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    smoked: list[str] = []
    monkeypatch.setattr(
        finalize_mod,
        "run_chat_smoke_test",
        lambda **kw: (smoked.append(kw["token"]), (True, "the model answered"))[1],
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "docker",
         "--root", str(root), "--start", "--non-interactive"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert smoked == ["nym_servicetoken_x"]
    assert "Chat smoke test passed" in out
    assert out.index("Chat smoke test passed") < out.index("nym_boot_y")


def test_finalize_docker_smoke_skipped_with_skip_llm_test(
    monkeypatch, tmp_path, capsys
):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    execs: list[list[str]] = []

    class _Result:
        def __init__(self, stdout=""):
            self.returncode = 0
            self.stdout = stdout

    def fake_run(cmd, *args, **kwargs):
        if "exec" in cmd:
            execs.append(cmd)
            return _Result(stdout="Token: nym_boot_y\n")
        return _Result()

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    monkeypatch.setattr(
        finalize_mod,
        "run_chat_smoke_test",
        lambda **kw: pytest.fail("--skip-llm-test must skip the smoke turn"),
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "docker",
         "--root", str(root), "--start", "--non-interactive", "--skip-llm-test"]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "Skipping the chat smoke test" in out
    # The skip path runs no service-token exec either.
    assert not any(
        any("SLIM_SERVICE_TOKEN" in part for part in cmd) for cmd in execs
    )


def test_finalize_service_smoke_failure_does_not_fail_setup(
    monkeypatch, tmp_path, capsys
):
    import nymeria.service_install as si

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    class _FakeManager:
        name = "systemd user service"

        def install(self, *, exec_argv, root):
            return si.InstallReport(
                artifact=tmp_path / "nymeria.service",
                lines=("Installed systemd user unit: fake",),
            )

        def log_hint(self):
            return "journalctl --user -u nymeria.service"

    monkeypatch.setattr(si, "service_manager", lambda: _FakeManager())
    monkeypatch.setattr(si, "resolve_exec_argv", lambda: ["/usr/bin/python3", "slim"])
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    (root / "data").mkdir(parents=True)
    (root / "data" / "SLIM_SERVICE_TOKEN.txt").write_text("nym_svc_x", "utf-8")
    monkeypatch.setattr(
        finalize_mod, "run_chat_smoke_test", lambda **kw: (False, "boom")
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "service",
         "--root", str(root), "--start", "--non-interactive"]
    )
    out = capsys.readouterr().out
    # The backend is up and the config was written fine: a failed smoke turn
    # informs, it never fails setup.
    assert rc == 0
    assert "Chat smoke test FAILED" in out
    assert "nymeria doctor" in out


def test_finalize_local_start_spawns_smoke_thread_and_stops_it(monkeypatch, tmp_path):
    import threading

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(finalize_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        finalize_mod,
        "wait_for_health",
        lambda **kw: pytest.fail("local foreground start must not health-poll"),
    )
    worker_dirs: list = []
    stopped = threading.Event()

    def fake_worker(data_dir, stop, base_url="http://localhost:8000"):
        worker_dirs.append(data_dir)
        if stop.wait(5.0):
            stopped.set()

    monkeypatch.setattr(finalize_mod, "_local_smoke_worker", fake_worker)

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "local",
         "--root", str(root), "--start", "--non-interactive"]
    )
    assert rc == 0
    # Still exactly one subprocess (the slim server); the smoke worker ran in a
    # thread, pointed at the install's data dir, and was stop-signalled when
    # the server exited.
    assert len(calls) == 1
    assert worker_dirs == [root / "data"]
    assert stopped.wait(2.0)


def test_finalize_local_skip_llm_test_spawns_no_thread(monkeypatch, tmp_path):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    class _Result:
        returncode = 0

    monkeypatch.setattr(
        finalize_mod.subprocess, "run", lambda *a, **kw: _Result()
    )
    monkeypatch.setattr(
        finalize_mod,
        "_local_smoke_worker",
        lambda *a, **kw: pytest.fail("--skip-llm-test must spawn no smoke thread"),
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "local",
         "--root", str(root), "--start", "--non-interactive", "--skip-llm-test"]
    )
    assert rc == 0


def test_local_smoke_worker_fires_after_health_and_token(monkeypatch, tmp_path, capsys):
    import threading

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "SLIM_SERVICE_TOKEN.txt").write_text("nym_local_x", "utf-8")
    monkeypatch.setattr(finalize_mod, "_smoke_health_ok", lambda url: True)
    smoked: list[str] = []
    monkeypatch.setattr(
        finalize_mod,
        "run_chat_smoke_test",
        lambda **kw: (smoked.append(kw["token"]), (True, "the model answered"))[1],
    )

    finalize_mod._local_smoke_worker(data_dir, threading.Event())
    out = capsys.readouterr().out
    assert smoked == ["nym_local_x"]
    assert out.strip() == "[smoke] chat smoke test passed: the model answered"


def test_local_smoke_worker_exits_silently_on_stop(monkeypatch, tmp_path, capsys):
    import threading

    monkeypatch.setattr(
        finalize_mod,
        "run_chat_smoke_test",
        lambda **kw: pytest.fail("a stopped worker must not fire the turn"),
    )
    stop = threading.Event()
    stop.set()
    finalize_mod._local_smoke_worker(tmp_path, stop)
    assert capsys.readouterr().out == ""


def test_print_next_action_includes_smoke_recipe(tmp_path):
    from nymeria.onboarding import HostingOption, NextAction
    from nymeria.setup.state import WizardState

    # Local/service manual handoff: the curl reads the host token file.
    console, buf = _capture_console()
    state = WizardState(
        hosting=HostingOption.LOCAL, root=tmp_path,
        next_action=NextAction.PRINT_COMMANDS,
    )
    finalize_mod.print_next_action(state, console)
    out = buf.getvalue()
    assert "/chat/sync" in out
    assert "SLIM_SERVICE_TOKEN.txt" in out

    # Docker manual handoff: the curl execs the token out of the container.
    console, buf = _capture_console()
    state = WizardState(
        hosting=HostingOption.DOCKER, root=tmp_path,
        next_action=NextAction.PRINT_COMMANDS,
    )
    finalize_mod.print_next_action(state, console)
    out = buf.getvalue()
    assert "/chat/sync" in out
    assert "exec -T" in out and "SLIM_SERVICE_TOKEN.txt" in out


def test_finalize_installs_service_when_opted_in(monkeypatch, tmp_path, capsys):
    import nymeria.service_install as si

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    installs: list[tuple[list[str], object]] = []

    class _FakeManager:
        name = "systemd user service"

        def install(self, *, exec_argv, root):
            installs.append((list(exec_argv), root))
            return si.InstallReport(
                artifact=tmp_path / "nymeria.service",
                lines=("Installed systemd user unit: fake",),
                notes=("Logs: journalctl --user -u nymeria.service",),
            )

        def log_hint(self):
            return "journalctl --user -u nymeria.service"

    monkeypatch.setattr(si, "service_manager", lambda: _FakeManager())
    monkeypatch.setattr(si, "resolve_exec_argv", lambda: ["/usr/bin/python3", "slim"])
    health: list[dict] = []
    monkeypatch.setattr(
        finalize_mod, "wait_for_health", lambda **kw: bool(health.append(kw)) or True
    )
    # Pre-mint the service token (the fake manager starts no real server) and
    # keep the smoke turn itself out of the network.
    (root / "data").mkdir(parents=True)
    (root / "data" / "SLIM_SERVICE_TOKEN.txt").write_text("nym_svc_x", "utf-8")
    smoked: list[str] = []
    monkeypatch.setattr(
        finalize_mod,
        "run_chat_smoke_test",
        lambda **kw: (smoked.append(kw["token"]), (True, "ok"))[1],
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "service",
         "--root", str(root), "--start", "--non-interactive"]
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert installs and installs[0][1] == root  # the unit points at the runtime root
    assert health  # verified the backend actually came up, not just "active"
    assert "Nymeria is up" in out
    # The smoke turn authenticated with the host-read service token.
    assert smoked == ["nym_svc_x"]
    assert "Chat smoke test passed" in out
    assert "nymeria service status" in out


def test_finalize_service_unavailable_falls_back_to_foreground(
    monkeypatch, tmp_path, capsys
):
    import nymeria.service_install as si

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    def unavailable():
        raise si.ServiceUnavailableError("no user manager", hints=("try lingering",))

    monkeypatch.setattr(si, "service_manager", unavailable)
    monkeypatch.setattr(
        finalize_mod,
        "wait_for_health",
        lambda **kw: pytest.fail("must not health-poll when install is impossible"),
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "service",
         "--root", str(root), "--start", "--non-interactive"]
    )
    out = capsys.readouterr().out

    assert rc == 0  # config was written fine; the install is the optional part
    assert "no user manager" in out
    assert "try lingering" in out
    assert "nymeria slim" in out


def test_finalize_service_print_path_has_real_commands(monkeypatch, tmp_path, capsys):
    import nymeria.service_install as si

    _stub_llm(monkeypatch)
    # Keep the host's real unit (if any) out of the summary warning.
    monkeypatch.setattr(si, "installed_artifact_path", lambda: None)
    root = tmp_path / "runtime"

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "service",
         "--root", str(root), "--non-interactive"]
    )
    out = capsys.readouterr().out

    assert rc == 0
    # The old placeholder apology is gone; the handoff is the real install
    # command (pinned to this config's root, since a bare invocation can
    # resolve a different one) plus the management verbs.
    assert "not wired up yet" not in out
    assert "nymeria service install" in out
    assert "--root" in out
    assert "nymeria service status" in out
    assert "nymeria service uninstall" in out
    # The hosting choice round-trips via the wizard-only marker.
    assert "NYMERIA_HOSTING=service" in (root / "config.env").read_text()


def test_finalize_warns_about_stale_service_artifact(monkeypatch, tmp_path, capsys):
    import nymeria.service_install as si

    _stub_llm(monkeypatch)
    monkeypatch.setattr(
        si, "installed_artifact_path", lambda: tmp_path / "nymeria.service"
    )
    root = tmp_path / "runtime"

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "local",
         "--root", str(root), "--non-interactive"]
    )
    out = capsys.readouterr().out

    assert rc == 0
    # Switching away from SERVICE never tears the unit down silently; the
    # summary must say it is still installed and how to remove it.
    assert "nymeria service uninstall" in out


def test_hydrate_recovers_service_hosting(monkeypatch, tmp_path):
    import nymeria.service_install as si

    from nymeria.onboarding import HOSTING_MARKER_ENV
    from nymeria.setup.hydrate import hydrate_state_from_disk
    from nymeria.setup.state import WizardState

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"
    assert (
        setup_main(
            ["--provider", "anthropic", "--model", "claude-test-model",
             "--api-key", "sk-ant-test-key", "--hosting", "service",
             "--root", str(root), "--non-interactive"]
        )
        == 0
    )

    # The marker is authoritative: SERVICE round-trips even with no artifact
    # installed (and, the important direction, a switch away from SERVICE
    # sticks while the old unit still exists on disk).
    monkeypatch.setattr(si, "installed_artifact_path", lambda: None)
    state = WizardState(root=root)
    assert hydrate_state_from_disk(state)
    assert state.hosting is HostingOption.SERVICE

    # Marker-less configs (written before the marker existed) fall back to
    # the installed artifact to tell LOCAL from SERVICE.
    config = root / "config.env"
    config.write_text(
        "\n".join(
            line
            for line in config.read_text().splitlines()
            if not line.startswith(HOSTING_MARKER_ENV)
        )
        + "\n"
    )
    fake_unit = tmp_path / "nymeria.service"
    fake_unit.write_text("[Unit]\n")
    monkeypatch.setattr(si, "installed_artifact_path", lambda: fake_unit)
    legacy = WizardState(root=root)
    assert hydrate_state_from_disk(legacy)
    assert legacy.hosting is HostingOption.SERVICE

    monkeypatch.setattr(si, "installed_artifact_path", lambda: None)
    plain = WizardState(root=root)
    assert hydrate_state_from_disk(plain)
    assert plain.hosting is HostingOption.LOCAL


def test_finalize_default_does_not_start_a_process(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    def boom(*_a, **_k):
        pytest.fail("the default handoff must not launch a process")

    monkeypatch.setattr(finalize_mod.subprocess, "run", boom)

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "docker",
         "--root", str(root), "--non-interactive"]
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert "docker compose -f docker-compose.single.yml up -d" in out


def test_finalize_docker_start_failure_falls_back_to_printing(monkeypatch, tmp_path, capsys):
    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    class _Result:
        returncode = 1  # compose failed

    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **k: _Result())
    monkeypatch.setattr(
        finalize_mod,
        "wait_for_health",
        lambda **kw: pytest.fail("must not health-poll after a failed start"),
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "docker",
         "--root", str(root), "--start", "--non-interactive"]
    )
    out = capsys.readouterr().out

    assert rc == 0  # config was written; a failed start is non-fatal
    assert "did not start cleanly" in out
    assert "docker compose -f docker-compose.single.yml up -d" in out


# --- first-run browser token handoff ------------------------------------------


def _allow_browser_handoff(monkeypatch):
    """Make the handoff gates pass deterministically (tty + launchable browser)."""
    from types import SimpleNamespace

    from nymeria.setup import environment as env_mod

    monkeypatch.setattr(
        finalize_mod.sys, "stdin", SimpleNamespace(isatty=lambda: True)
    )
    monkeypatch.setattr(env_mod, "browser_launch_blocked_reason", lambda **kw: "")


def test_browser_token_handoff_gate_matrix(monkeypatch):
    from types import SimpleNamespace

    from nymeria.setup import environment as env_mod
    from nymeria.setup.state import WizardState

    _allow_browser_handoff(monkeypatch)
    allowed = finalize_mod.browser_token_handoff_allowed

    # Both native shapes on this machine may auto-open.
    for hosting in (HostingOption.LOCAL, HostingOption.SERVICE):
        assert (
            allowed(
                WizardState(hosting=hosting),
                connect_token="nym_x",
                non_interactive=False,
            )
            is True
        )

    base = WizardState(hosting=HostingOption.LOCAL)
    # No token minted this run (reconfigure, pre-existing admin): never open.
    assert allowed(base, connect_token=None, non_interactive=False) is False
    # Headless runs never open a browser.
    assert allowed(base, connect_token="nym_x", non_interactive=True) is False
    # print_credentials off = the user opted out of credential surfacing.
    assert (
        allowed(
            WizardState(hosting=HostingOption.LOCAL, print_credentials=False),
            connect_token="nym_x",
            non_interactive=False,
        )
        is False
    )
    # Docker mints in-container; its print path owns the token handoff.
    assert (
        allowed(
            WizardState(hosting=HostingOption.DOCKER),
            connect_token="nym_x",
            non_interactive=False,
        )
        is False
    )
    # A blocked browser (SSH, container, display-less host) would open on the
    # wrong machine or nowhere.
    monkeypatch.setattr(
        env_mod, "browser_launch_blocked_reason", lambda **kw: "SSH session"
    )
    assert allowed(base, connect_token="nym_x", non_interactive=False) is False
    monkeypatch.setattr(env_mod, "browser_launch_blocked_reason", lambda **kw: "")
    # A non-tty stdin (pipe, CI harness) is not a human watching the terminal.
    monkeypatch.setattr(
        finalize_mod.sys, "stdin", SimpleNamespace(isatty=lambda: False)
    )
    assert allowed(base, connect_token="nym_x", non_interactive=False) is False


def test_open_browser_with_token_builds_fragment_url(monkeypatch):
    import webbrowser

    opened: list[str] = []
    monkeypatch.setattr(
        webbrowser, "open", lambda url: (opened.append(url), True)[1]
    )
    assert finalize_mod._open_browser_with_token("http://localhost:8000", "nym_x")
    assert opened == ["http://localhost:8000/#token=nym_x"]

    def _boom(url):
        raise RuntimeError("no browser")

    # A broken launcher reports False, never raises (printed handoff remains).
    monkeypatch.setattr(webbrowser, "open", _boom)
    assert (
        finalize_mod._open_browser_with_token("http://localhost:8000", "nym_x")
        is False
    )


def test_run_next_action_gates_the_handoff_token(monkeypatch, tmp_path):
    from nymeria.onboarding import NextAction
    from nymeria.setup.state import WizardState

    _allow_browser_handoff(monkeypatch)
    seen: list = []
    monkeypatch.setattr(
        finalize_mod,
        "_start_now_service",
        lambda console, *, state, root, handoff_token=None: (
            seen.append(handoff_token),
            0,
        )[1],
    )
    console, _out = _capture_console()
    state = WizardState(
        hosting=HostingOption.SERVICE,
        next_action=NextAction.START_API_OPEN_FRONTEND,
    )
    assert (
        finalize_mod.run_next_action(
            state, console, root=tmp_path, connect_token="nym_x",
            non_interactive=False,
        )
        == 0
    )
    assert (
        finalize_mod.run_next_action(
            state, console, root=tmp_path, connect_token="nym_x",
            non_interactive=True,
        )
        == 0
    )
    # Gates pass -> the token reaches the start path; headless -> it never does.
    assert seen == ["nym_x", None]


def test_run_next_action_tells_local_start_whether_a_token_was_printed(monkeypatch, tmp_path):
    """`token_printed` is derived from connect_token (the bootstrap token this
    run printed), independently of the browser handoff gate: a headless fresh
    install printed one (paste instruction), a reconfigure printed none."""
    from nymeria.onboarding import NextAction
    from nymeria.setup.state import WizardState

    seen: list[tuple[str | None, bool]] = []

    def fake_local(console, *, state, root, handoff_token=None, token_printed=False):
        seen.append((handoff_token, token_printed))
        return 0

    monkeypatch.setattr(finalize_mod, "_start_now_local", fake_local)
    console, _out = _capture_console()
    state = WizardState(
        hosting=HostingOption.LOCAL, next_action=NextAction.START_API_OPEN_FRONTEND
    )
    for token in ("nym_x", None):
        assert (
            finalize_mod.run_next_action(
                state, console, root=tmp_path, connect_token=token, non_interactive=True
            )
            == 0
        )
    # Headless: no browser handoff either way; the paste instruction only when
    # a token exists.
    assert seen == [(None, True), (None, False)]


def test_finalize_service_start_opens_browser_after_health(monkeypatch, tmp_path):
    import nymeria.service_install as si

    from nymeria.setup.state import WizardState

    root = tmp_path / "runtime"
    (root / "data").mkdir(parents=True)

    class _FakeManager:
        name = "systemd user service"

        def install(self, *, exec_argv, root):
            return si.InstallReport(
                artifact=tmp_path / "nymeria.service",
                lines=("Installed systemd user unit: fake",),
            )

        def log_hint(self):
            return "journalctl --user -u nymeria.service"

    monkeypatch.setattr(si, "service_manager", lambda: _FakeManager())
    monkeypatch.setattr(
        si, "resolve_exec_argv", lambda: ["/usr/bin/python3", "slim"]
    )
    import threading

    health_seen = threading.Event()
    monkeypatch.setattr(
        finalize_mod,
        "wait_for_health",
        lambda **kw: (health_seen.set(), True)[1],
    )
    opened: list[tuple[str, str, bool]] = []
    open_done = threading.Event()

    def fake_open(base_url, token):
        # The open runs from a daemon thread (a $BROWSER GenericBrowser open()
        # BLOCKS until the browser exits); record that health already passed.
        opened.append((base_url, token, health_seen.is_set()))
        open_done.set()
        return True

    monkeypatch.setattr(finalize_mod, "_open_browser_with_token", fake_open)
    console, out_buffer = _capture_console()
    state = WizardState(hosting=HostingOption.SERVICE, skip_llm_test=True)

    rc = finalize_mod._start_now_service(
        console, state=state, root=root, handoff_token="nym_tok"
    )
    out = out_buffer.getvalue()
    assert rc == 0
    # The browser opens (off-thread) only once the backend answers /health.
    assert open_done.wait(2.0)
    assert opened == [("http://localhost:8000", "nym_tok", True)]
    assert "signed in" in out
    # The opened URL carries the raw token; it must never be printed.
    assert "#token" not in out


def test_finalize_service_start_health_timeout_never_opens(monkeypatch, tmp_path):
    import nymeria.service_install as si

    from nymeria.setup.state import WizardState

    root = tmp_path / "runtime"
    (root / "data").mkdir(parents=True)

    class _FakeManager:
        name = "systemd user service"

        def install(self, *, exec_argv, root):
            return si.InstallReport(
                artifact=tmp_path / "nymeria.service", lines=("Installed: fake",)
            )

        def log_hint(self):
            return "journalctl --user -u nymeria.service"

    monkeypatch.setattr(si, "service_manager", lambda: _FakeManager())
    monkeypatch.setattr(
        si, "resolve_exec_argv", lambda: ["/usr/bin/python3", "slim"]
    )
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: False)
    monkeypatch.setattr(
        finalize_mod,
        "_open_browser_with_token",
        lambda *a: pytest.fail("an unhealthy backend must not open a browser"),
    )
    console, out_buffer = _capture_console()
    state = WizardState(hosting=HostingOption.SERVICE, skip_llm_test=True)

    rc = finalize_mod._start_now_service(
        console, state=state, root=root, handoff_token="nym_tok"
    )
    assert rc == 0
    assert "health check has not passed" in out_buffer.getvalue()


def test_finalize_service_start_without_token_keeps_manual_copy(
    monkeypatch, tmp_path
):
    import nymeria.service_install as si

    from nymeria.setup.state import WizardState

    root = tmp_path / "runtime"
    (root / "data").mkdir(parents=True)

    class _FakeManager:
        name = "systemd user service"

        def install(self, *, exec_argv, root):
            return si.InstallReport(
                artifact=tmp_path / "nymeria.service", lines=("Installed: fake",)
            )

        def log_hint(self):
            return "journalctl --user -u nymeria.service"

    monkeypatch.setattr(si, "service_manager", lambda: _FakeManager())
    monkeypatch.setattr(
        si, "resolve_exec_argv", lambda: ["/usr/bin/python3", "slim"]
    )
    monkeypatch.setattr(finalize_mod, "wait_for_health", lambda **kw: True)
    monkeypatch.setattr(
        finalize_mod,
        "_open_browser_with_token",
        lambda *a: pytest.fail("no handoff token must mean no browser open"),
    )
    console, out_buffer = _capture_console()
    state = WizardState(hosting=HostingOption.SERVICE, skip_llm_test=True)

    rc = finalize_mod._start_now_service(
        console, state=state, root=root, handoff_token=None
    )
    assert rc == 0
    assert "paste the" in out_buffer.getvalue()


def test_finalize_local_start_spawns_browser_open_thread(monkeypatch, tmp_path):
    import threading

    from nymeria.setup.state import WizardState

    root = tmp_path / "runtime"
    root.mkdir()
    calls: list[list[str]] = []

    class _Result:
        returncode = 0

    monkeypatch.setattr(
        finalize_mod.subprocess,
        "run",
        lambda cmd, **kw: (calls.append(cmd), _Result())[1],
    )
    monkeypatch.setattr(
        finalize_mod,
        "wait_for_health",
        lambda **kw: pytest.fail("local foreground start must not health-poll"),
    )
    worker_args: list = []
    stopped = threading.Event()

    def fake_worker(base_url, token, stop):
        worker_args.append((base_url, token))
        if stop.wait(5.0):
            stopped.set()

    monkeypatch.setattr(finalize_mod, "_local_browser_open_worker", fake_worker)
    console, out_buffer = _capture_console()
    state = WizardState(hosting=HostingOption.LOCAL, skip_llm_test=True)

    rc = finalize_mod._start_now_local(
        console, state=state, root=root, handoff_token="nym_tok"
    )
    assert rc == 0
    # Still exactly one subprocess (the slim server); the browser worker ran
    # in a thread and was stop-signalled when the server exited.
    assert len(calls) == 1
    assert worker_args == [("http://localhost:8000", "nym_tok")]
    assert stopped.wait(2.0)
    assert "browser opens already signed in" in out_buffer.getvalue()


def test_local_browser_open_worker_opens_after_health(monkeypatch):
    import threading

    monkeypatch.setattr(finalize_mod, "_smoke_health_ok", lambda url: True)
    opened: list[tuple[str, str]] = []
    monkeypatch.setattr(
        finalize_mod,
        "_open_browser_with_token",
        lambda base_url, token: (opened.append((base_url, token)), True)[1],
    )
    finalize_mod._local_browser_open_worker(
        "http://localhost:8000", "nym_x", threading.Event()
    )
    assert opened == [("http://localhost:8000", "nym_x")]


def test_local_browser_open_worker_exits_silently_on_stop(monkeypatch):
    import threading

    monkeypatch.setattr(
        finalize_mod,
        "_open_browser_with_token",
        lambda *a: pytest.fail("a stopped worker must not open a browser"),
    )
    stop = threading.Event()
    stop.set()
    finalize_mod._local_browser_open_worker("http://localhost:8000", "nym_x", stop)


def test_finalize_non_interactive_never_opens_a_browser(monkeypatch, tmp_path):
    import webbrowser

    _stub_llm(monkeypatch)
    root = tmp_path / "runtime"

    class _Result:
        returncode = 0

    monkeypatch.setattr(finalize_mod.subprocess, "run", lambda *a, **kw: _Result())
    monkeypatch.setattr(
        webbrowser,
        "open",
        lambda url: pytest.fail("a non-interactive run must never open a browser"),
    )

    rc = setup_main(
        ["--provider", "anthropic", "--model", "claude-test-model",
         "--api-key", "sk-ant-test-key", "--hosting", "local",
         "--root", str(root), "--start", "--non-interactive", "--skip-llm-test"]
    )
    assert rc == 0
