"""Background-service install: unit/plist generation, command sequences, status.

Covers nymeria/service_install.py, which backs the wizard's Background service
hosting choice and the `nymeria service install|uninstall|status|restart` CLI.
All service-manager commands run through an injected runner; nothing here
touches the host's systemd or launchd.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from nymeria import service_install as si
from nymeria.service_install import (
    LAUNCHD_LABEL,
    SYSTEMD_UNIT_NAME,
    LaunchdAgentService,
    ServiceInstallError,
    ServiceUnavailableError,
    SystemdUserService,
    build_launchd_plist,
    build_systemd_unit,
    resolve_exec_argv,
    service_manager,
)


class ScriptedRunner:
    """Record service-manager commands; answer from substring-matched scripts."""

    def __init__(self, responses: dict[str, tuple[int, str, str]] | None = None):
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for pattern, (code, out, err) in self.responses.items():
            if pattern in joined:
                return subprocess.CompletedProcess(argv, code, out, err)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def command_lines(self) -> list[str]:
        return [" ".join(argv) for argv in self.calls]


@pytest.fixture
def linux_host(monkeypatch, tmp_path):
    """A fake healthy Linux host: systemd marker present, not a container."""
    marker = tmp_path / "run-systemd"
    marker.mkdir()
    monkeypatch.setattr(si, "_SYSTEMD_MARKER", marker)
    monkeypatch.setattr(si, "_CONTAINER_MARKERS", (tmp_path / "no-dockerenv",))
    monkeypatch.delenv("container", raising=False)
    linger_dir = tmp_path / "linger"
    linger_dir.mkdir()
    monkeypatch.setattr(si, "_LINGER_DIR", linger_dir)
    monkeypatch.setattr(
        SystemdUserService, "_username", lambda self: "nym", raising=True
    )
    return linger_dir


# --- artifact generation ------------------------------------------------------


def test_systemd_unit_directives():
    unit = build_systemd_unit(
        exec_argv=["/usr/bin/python3", "/opt/app/run.py", "slim"],
        root=Path("/home/nym/.nymeria"),
        path_env="/usr/bin:/bin",
    )
    assert "ExecStart=/usr/bin/python3 /opt/app/run.py slim" in unit
    assert "WorkingDirectory=/home/nym/.nymeria" in unit
    assert 'Environment="NYMERIA_PROJECT_ROOT=/home/nym/.nymeria"' in unit
    assert 'Environment="PATH=/usr/bin:/bin"' in unit
    # Crash-only restarts plus a start-rate budget: a broken config must not
    # crash-loop forever, and a deliberate stop must stay stopped.
    assert "Restart=on-failure" in unit
    assert "StartLimitIntervalSec=300" in unit
    assert "StartLimitBurst=5" in unit
    assert "Type=exec" in unit
    assert "WantedBy=default.target" in unit
    assert "SyslogIdentifier=nymeria" in unit
    # The agent's shell tool may legitimately sudo; this directive would
    # silently break that under the service.
    assert "NoNewPrivileges" not in unit


def test_systemd_unit_quotes_spaces_and_escapes_percent():
    unit = build_systemd_unit(
        exec_argv=["/opt/my tools/python", "/opt/100% nymeria/run.py", "slim"],
        root=Path("/opt/100% nymeria"),
        path_env="/usr/bin",
    )
    assert 'ExecStart="/opt/my tools/python" "/opt/100%% nymeria/run.py" slim' in unit
    assert "WorkingDirectory=/opt/100%% nymeria" in unit
    assert 'Environment="NYMERIA_PROJECT_ROOT=/opt/100%% nymeria"' in unit


def test_systemd_unit_neutralizes_apostrophe_dollar_and_semicolon():
    # A bare apostrophe opens a systemd quote section ("Unbalanced quoting",
    # the unit is fatally unparseable); $VAR expands even inside quotes
    # (literal dollars must double); an unquoted `;` separates commands.
    unit = build_systemd_unit(
        exec_argv=["/home/obrien's-pc/python", "/opt/$HOME-ish;x/run.py", "slim"],
        root=Path("/home/obrien's-pc"),
        path_env="/usr/bin",
    )
    assert (
        "ExecStart=\"/home/obrien's-pc/python\" \"/opt/$$HOME-ish;x/run.py\" slim"
        in unit
    )


def test_systemd_unit_rejects_newlines():
    with pytest.raises(ServiceInstallError):
        build_systemd_unit(
            exec_argv=["/usr/bin/python3", "evil\narg"],
            root=Path("/tmp"),
            path_env="/usr/bin",
        )


def test_launchd_plist_payload_roundtrips(tmp_path):
    payload = build_launchd_plist(
        exec_argv=["/usr/bin/python3", "/opt/app/run.py", "slim"],
        root=tmp_path,
        path_env="/usr/bin:/bin",
    )
    parsed = plistlib.loads(plistlib.dumps(payload))
    assert parsed["Label"] == LAUNCHD_LABEL
    assert parsed["ProgramArguments"] == ["/usr/bin/python3", "/opt/app/run.py", "slim"]
    assert parsed["RunAtLoad"] is True
    # Restart on crash only; deliberate stops (bootout) stay stopped.
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["EnvironmentVariables"]["NYMERIA_PROJECT_ROOT"] == str(tmp_path)
    assert parsed["StandardOutPath"] == str(tmp_path / "logs" / "service-stdout.log")
    assert parsed["StandardErrorPath"] == str(tmp_path / "logs" / "service-stderr.log")


# --- exec argv resolution -----------------------------------------------------


@pytest.fixture
def _no_main_spec(monkeypatch):
    """Pretend the process was NOT started via `python -m ...`.

    Under `python -m pytest`, __main__.__spec__ names pytest; without this the
    -m branch would (correctly!) fire and hide the script-path branches.
    """
    monkeypatch.setattr(sys.modules["__main__"], "__spec__", None, raising=False)


def test_resolve_exec_argv_uses_current_script(_no_main_spec, monkeypatch, tmp_path):
    script = tmp_path / "run.py"
    script.write_text("# entry\n")
    monkeypatch.setattr(sys, "argv", [str(script)])
    assert resolve_exec_argv() == [sys.executable, str(script), "slim"]


def test_resolve_exec_argv_falls_back_to_console_script(
    _no_main_spec, monkeypatch, tmp_path
):
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "gone")])
    monkeypatch.setattr(si.shutil, "which", lambda _name: "/fake/bin/nymeria")
    assert resolve_exec_argv() == ["/fake/bin/nymeria", "slim"]


def test_resolve_exec_argv_frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert resolve_exec_argv() == [sys.executable, "slim"]


def test_resolve_exec_argv_reproduces_dash_m_launch(monkeypatch, tmp_path):
    # `python -m nymeria.cli_entry` sets argv[0] to the module FILE, which
    # cannot be re-run as a script (relative imports fail); the unit must
    # reproduce the -m invocation instead.
    class _Spec:
        name = "nymeria.cli_entry"

    monkeypatch.setattr(sys.modules["__main__"], "__spec__", _Spec(), raising=False)
    module_file = tmp_path / "cli_entry.py"
    module_file.write_text("# looks like a real file\n")
    monkeypatch.setattr(sys, "argv", [str(module_file)])
    assert resolve_exec_argv() == [sys.executable, "-m", "nymeria.cli_entry", "slim"]


def test_resolve_exec_argv_reruns_a_zipapp_launcher(monkeypatch, tmp_path):
    """uv's Windows console-script launcher runs `python.exe nymeria.exe` as a
    zipapp: `__main__.__spec__.name` is "__main__" and its origin is
    `<exe>/__main__.py`, while the generated __main__ strips `.exe` from
    sys.argv[0] (no such file). The re-run is `python <exe>`, never
    `python -m __main__` (which cannot find a module spec) and never argv[0]."""
    archive = tmp_path / "nymeria.exe"
    archive.write_bytes(b"MZ-not-really-but-a-file")

    class _Spec:
        name = "__main__"
        origin = str(archive / "__main__.py")

    monkeypatch.setattr(sys.modules["__main__"], "__spec__", _Spec(), raising=False)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "nymeria")])
    monkeypatch.setattr(si.shutil, "which", lambda _name: str(tmp_path / "nymeria.exe"))
    assert resolve_exec_argv() == [sys.executable, str(archive), "slim"]


def test_resolve_exec_argv_main_spec_without_an_archive_falls_through(
    monkeypatch, tmp_path
):
    # A "__main__" spec whose origin is not inside a runnable archive (a
    # `python -c` style launch on some interpreters) must not become
    # `-m __main__`; the script / console-script branches decide instead.
    class _Spec:
        name = "__main__"
        origin = str(tmp_path / "missing.exe" / "__main__.py")

    monkeypatch.setattr(sys.modules["__main__"], "__spec__", _Spec(), raising=False)
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "gone")])
    monkeypatch.setattr(si.shutil, "which", lambda _name: "/fake/bin/nymeria")
    assert resolve_exec_argv() == ["/fake/bin/nymeria", "slim"]


def test_ephemeral_exec_warning_flags_uv_cache(tmp_path):
    argv = [f"{Path.home()}/.cache/uv/archive-v0/abc/bin/python", "slim"]
    warning = si.ephemeral_exec_warning(argv)
    assert warning and "temporary environment" in warning
    assert si.ephemeral_exec_warning(["/usr/bin/python3", "slim"]) is None


# --- systemd manager ----------------------------------------------------------


def test_systemd_install_writes_unit_and_runs_sequence(linux_host, tmp_path):
    (linux_host / "nym").touch()  # linger already enabled via marker file
    runner = ScriptedRunner({"is-system-running": (0, "running", "")})
    manager = SystemdUserService(home=tmp_path / "home", runner=runner)
    root = tmp_path / "runtime"
    root.mkdir()

    report = manager.install(
        exec_argv=["/usr/bin/python3", "/opt/app/run.py", "slim"], root=root
    )

    unit_path = tmp_path / "home" / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
    assert report.artifact == unit_path
    assert f"NYMERIA_PROJECT_ROOT={root}" in unit_path.read_text()
    lines = runner.command_lines()
    # Probe, then reload, enable, and restart (not `enable --now`, so a
    # reinstall replaces a stale running process).
    assert lines[0] == "systemctl --user is-system-running"
    assert "systemctl --user daemon-reload" in lines
    assert f"systemctl --user enable {SYSTEMD_UNIT_NAME}" in lines
    assert f"systemctl --user restart {SYSTEMD_UNIT_NAME}" in lines
    assert lines.index("systemctl --user daemon-reload") < lines.index(
        f"systemctl --user enable {SYSTEMD_UNIT_NAME}"
    ) < lines.index(f"systemctl --user restart {SYSTEMD_UNIT_NAME}")
    assert not report.warnings  # linger marker present, nothing to warn about


def test_systemd_install_enables_linger_when_missing(linux_host, tmp_path):
    runner = ScriptedRunner(
        {
            "is-system-running": (0, "running", ""),
            "show-user": (0, "Linger=no", ""),
        }
    )

    real_call = runner.__call__

    def call_and_mark(argv, **kwargs):
        result = real_call(argv, **kwargs)
        if argv[:2] == ["loginctl", "enable-linger"]:
            (linux_host / "nym").touch()  # logind would create the marker
        return result

    manager = SystemdUserService(home=tmp_path / "home", runner=call_and_mark)
    report = manager.install(exec_argv=["/usr/bin/python3", "slim"], root=tmp_path)
    assert ["loginctl", "enable-linger"] in runner.calls
    assert not report.warnings
    assert any("survives logout" in line for line in report.lines)


def test_systemd_install_linger_failure_warns_not_fails(linux_host, tmp_path):
    runner = ScriptedRunner(
        {
            "is-system-running": (0, "running", ""),
            "show-user": (0, "Linger=no", ""),
            "enable-linger": (1, "", "Access denied"),
        }
    )
    manager = SystemdUserService(home=tmp_path / "home", runner=runner)
    report = manager.install(exec_argv=["/usr/bin/python3", "slim"], root=tmp_path)
    assert any("sudo loginctl enable-linger" in w for w in report.warnings)


def test_systemd_unavailable_headless_bus(linux_host, tmp_path):
    runner = ScriptedRunner(
        {
            "is-system-running": (
                1,
                "",
                "Failed to connect to bus: No medium found",
            )
        }
    )
    manager = SystemdUserService(home=tmp_path, runner=runner)
    with pytest.raises(ServiceUnavailableError) as excinfo:
        manager.ensure_available()
    assert any("enable-linger" in hint for hint in excinfo.value.hints)


def test_systemd_unavailable_without_systemd(monkeypatch, tmp_path):
    monkeypatch.setattr(si, "_SYSTEMD_MARKER", tmp_path / "absent")
    monkeypatch.setattr(si, "_CONTAINER_MARKERS", (tmp_path / "no-dockerenv",))
    manager = SystemdUserService(home=tmp_path, runner=ScriptedRunner())
    with pytest.raises(ServiceUnavailableError, match="systemd is not running"):
        manager.ensure_available()


def test_systemd_refuses_containers(monkeypatch, tmp_path):
    dockerenv = tmp_path / "dockerenv"
    dockerenv.touch()
    monkeypatch.setattr(si, "_CONTAINER_MARKERS", (dockerenv,))
    manager = SystemdUserService(home=tmp_path, runner=ScriptedRunner())
    with pytest.raises(ServiceUnavailableError, match="container"):
        manager.ensure_available()


def test_systemd_env_injects_runtime_dir_when_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    manager = SystemdUserService(home=tmp_path, runner=ScriptedRunner())
    env = manager._env
    assert env is not None  # systemd always builds a repaired env (never None)
    assert env["XDG_RUNTIME_DIR"].startswith("/run/user/")
    assert env["DBUS_SESSION_BUS_ADDRESS"].startswith("unix:path=/run/user/")


def test_systemd_uninstall_removes_unit(linux_host, tmp_path):
    runner = ScriptedRunner()
    manager = SystemdUserService(home=tmp_path / "home", runner=runner)
    manager.artifact_path.parent.mkdir(parents=True)
    manager.artifact_path.write_text("[Unit]\n")

    lines = manager.uninstall()

    assert not manager.artifact_path.exists()
    assert f"systemctl --user disable --now {SYSTEMD_UNIT_NAME}" in runner.command_lines()
    assert "systemctl --user daemon-reload" in runner.command_lines()
    assert f"systemctl --user reset-failed {SYSTEMD_UNIT_NAME}" in runner.command_lines()
    assert any("Removed" in line for line in lines)
    assert not any("Warning" in line for line in lines)
    # Lingering stays: other user services may rely on it.
    assert all("enable-linger" not in " ".join(argv) for argv in runner.calls)
    assert any("nothing to remove" in line for line in manager.uninstall())


def test_systemd_uninstall_warns_when_stop_fails(linux_host, tmp_path):
    # A dead user bus must not let "Removed ..." imply the process stopped.
    runner = ScriptedRunner(
        {"disable --now": (1, "", "Failed to connect to bus: no medium")}
    )
    manager = SystemdUserService(home=tmp_path / "home", runner=runner)
    manager.artifact_path.parent.mkdir(parents=True)
    manager.artifact_path.write_text("[Unit]\n")

    lines = manager.uninstall()

    assert not manager.artifact_path.exists()  # the unit file still goes
    assert any("could not stop the service" in line for line in lines)


def test_systemd_status_parses_show_output(tmp_path):
    runner = ScriptedRunner(
        {"show": (0, "ActiveState=active\nSubState=running\nMainPID=1234", "")}
    )
    manager = SystemdUserService(home=tmp_path, runner=runner)
    assert manager.status().installed is False

    manager.artifact_path.parent.mkdir(parents=True)
    manager.artifact_path.write_text("[Unit]\n")
    status = manager.status()
    assert status.installed and status.running
    assert status.pid == 1234
    assert "active" in status.detail


def test_systemd_restart_requires_install(linux_host, tmp_path):
    runner = ScriptedRunner({"is-system-running": (0, "running", "")})
    manager = SystemdUserService(home=tmp_path / "home", runner=runner)
    with pytest.raises(ServiceInstallError, match="not installed"):
        manager.restart()


# --- launchd manager ----------------------------------------------------------


def test_launchd_install_writes_plist_and_runs_sequence(tmp_path):
    manager = LaunchdAgentService(home=tmp_path / "home", runner=ScriptedRunner())
    label_target = f"{manager._gui_target()}/{LAUNCHD_LABEL}"
    # The domain probe answers, the label probe says "gone" (so the
    # post-bootout drain wait returns immediately).
    runner = ScriptedRunner({f"launchctl print {label_target}": (1, "", "not found")})
    manager = LaunchdAgentService(home=tmp_path / "home", runner=runner)
    root = tmp_path / "runtime"
    root.mkdir()

    report = manager.install(
        exec_argv=["/usr/bin/python3", "/opt/app/run.py", "slim"], root=root
    )

    with open(manager.artifact_path, "rb") as fh:
        parsed = plistlib.load(fh)
    assert parsed["Label"] == LAUNCHD_LABEL
    assert (root / "logs").is_dir()  # launchd does not create log dirs itself
    verbs = [argv[1] for argv in runner.calls if argv[0] == "launchctl"]
    # print (availability), bootout, print (drain check: bootout is async),
    # enable, bootstrap; no kickstart (RunAtLoad starts the job; a
    # `kickstart -k` would SIGTERM the fresh process).
    assert verbs == ["print", "bootout", "print", "enable", "bootstrap"]
    assert any("Login Items" in note for note in report.notes)


def test_launchd_bootstrap_retries_while_old_job_drains(monkeypatch, tmp_path):
    # Real launchd fails bootstrap with error 5 while a booted-out job is
    # still winding down; one bounded retry must absorb that.
    monkeypatch.setattr(si.time, "sleep", lambda _s: None)
    manager = LaunchdAgentService(home=tmp_path / "home", runner=ScriptedRunner())
    label_target = f"{manager._gui_target()}/{LAUNCHD_LABEL}"
    attempts: list[int] = []

    base = ScriptedRunner({f"launchctl print {label_target}": (1, "", "not found")})

    def runner(argv, **kwargs):
        if argv[:2] == ["launchctl", "bootstrap"]:
            attempts.append(1)
            code = 5 if len(attempts) == 1 else 0
            return subprocess.CompletedProcess(argv, code, "", "Bootstrap failed: 5")
        return base(argv, **kwargs)

    manager = LaunchdAgentService(home=tmp_path / "home", runner=runner)
    root = tmp_path / "runtime"
    root.mkdir()
    report = manager.install(exec_argv=["/usr/bin/python3", "slim"], root=root)
    assert len(attempts) == 2
    assert any("started" in line for line in report.lines)


def test_launchd_unavailable_without_gui_session(tmp_path):
    runner = ScriptedRunner({"print": (1, "", "Could not find domain")})
    manager = LaunchdAgentService(home=tmp_path, runner=runner)
    with pytest.raises(ServiceUnavailableError, match="GUI"):
        manager.ensure_available()


def test_launchd_status_parses_print_output(tmp_path):
    runner = ScriptedRunner({"print": (0, "state = running\n\tpid = 4321", "")})
    manager = LaunchdAgentService(home=tmp_path, runner=runner)
    assert manager.status().installed is False

    manager.artifact_path.parent.mkdir(parents=True)
    manager.artifact_path.write_bytes(plistlib.dumps({"Label": LAUNCHD_LABEL}))
    status = manager.status()
    assert status.installed and status.running
    assert status.pid == 4321


def test_launchd_uninstall_boots_out_and_removes(tmp_path):
    runner = ScriptedRunner()
    manager = LaunchdAgentService(home=tmp_path / "home", runner=runner)
    manager.artifact_path.parent.mkdir(parents=True)
    manager.artifact_path.write_bytes(plistlib.dumps({"Label": LAUNCHD_LABEL}))

    lines = manager.uninstall()

    assert not manager.artifact_path.exists()
    assert any(argv[:2] == ["launchctl", "bootout"] for argv in runner.calls)
    assert any("Removed" in line for line in lines)


# --- shared subprocess base (_run) --------------------------------------------


class KwargRunner:
    """Record argv + kwargs of each call; optionally raise or return a code."""

    def __init__(self, *, returncode=0, stdout="", stderr="", raises=None):
        self.calls: list[tuple[list[str], dict]] = []
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(argv, self._returncode, self._stdout, self._stderr)


def test_systemd_run_forwards_repaired_env(tmp_path):
    # systemd --user needs XDG_RUNTIME_DIR/DBus repaired for headless shells,
    # so the shared base must forward the manager's _env to the runner.
    runner = KwargRunner(stdout="ok")
    manager = SystemdUserService(home=tmp_path, runner=runner)
    manager._run(["systemctl", "--user", "daemon-reload"])
    _argv, kwargs = runner.calls[-1]
    assert kwargs["env"] is manager._env
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == si.COMMAND_TIMEOUT_SECONDS


def test_launchd_run_omits_env(tmp_path):
    # launchd inherits the caller's env: the base must NOT pass an env kwarg,
    # exactly as the former hand-rolled launchd _run did.
    runner = KwargRunner(stdout="ok")
    manager = LaunchdAgentService(home=tmp_path, runner=runner)
    manager._run(["launchctl", "print", manager._gui_target()])
    _argv, kwargs = runner.calls[-1]
    assert "env" not in kwargs
    assert manager._env is None


def test_subprocess_base_translates_oserror_to_127(tmp_path):
    # Both managers share the base: a spawn failure degrades to rc 127 with the
    # error text, never an exception, when check is False.
    for manager in (
        SystemdUserService(home=tmp_path, runner=KwargRunner(raises=OSError("boom"))),
        LaunchdAgentService(home=tmp_path, runner=KwargRunner(raises=OSError("boom"))),
    ):
        result = manager._run(["some", "cmd"])
        assert result.returncode == 127
        assert "boom" in result.stderr


def test_subprocess_base_check_reraises_spawn_failure(tmp_path):
    runner = KwargRunner(raises=subprocess.TimeoutExpired(cmd="systemctl", timeout=1))
    manager = SystemdUserService(home=tmp_path, runner=runner)
    with pytest.raises(ServiceInstallError, match="systemctl failed"):
        manager._run(["systemctl", "--user", "daemon-reload"], check=True)


def test_subprocess_base_check_raises_on_nonzero_for_both_managers(tmp_path):
    systemd = SystemdUserService(
        home=tmp_path, runner=KwargRunner(returncode=1, stderr="nope")
    )
    with pytest.raises(ServiceInstallError, match="nope"):
        systemd._run(["systemctl", "--user", "enable", "x"], check=True)

    launchd = LaunchdAgentService(
        home=tmp_path, runner=KwargRunner(returncode=2, stdout="bad")
    )
    with pytest.raises(ServiceInstallError, match="bad"):
        launchd._run(["launchctl", "enable", "y"], check=True)


# --- platform selection and CLI -------------------------------------------------


def test_service_manager_unsupported_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ServiceUnavailableError) as excinfo:
        service_manager()
    hints = excinfo.value.hints
    # Windows autostart now routes through install.ps1's logon scheduled task.
    assert any("NymeriaOS Slim" in hint for hint in hints)
    assert any("schtasks" in hint for hint in hints)


def test_installed_artifact_path_is_none_off_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert si.installed_artifact_path() is None


def test_service_cli_unavailable_prints_hints_and_fallback(monkeypatch, tmp_path, capsys):
    def raise_unavailable():
        raise ServiceUnavailableError("no manager here", hints=("do the thing",))

    monkeypatch.setattr(si, "service_manager", raise_unavailable)
    rc = si.service_cli("install", root=tmp_path)
    out = capsys.readouterr().out
    assert rc == 2
    assert "do the thing" in out
    assert "nymeria slim" in out


def test_service_cli_status_not_installed(monkeypatch, tmp_path, capsys):
    manager = SystemdUserService(home=tmp_path, runner=ScriptedRunner())
    monkeypatch.setattr(si, "service_manager", lambda: manager)
    rc = si.service_cli("status", root=tmp_path)
    out = capsys.readouterr().out
    assert rc == 1
    assert "nymeria service install" in out


def test_service_cli_install_reports_health(linux_host, monkeypatch, tmp_path, capsys):
    (linux_host / "nym").touch()
    (tmp_path / "config.env").write_text("LLM_PROVIDER=anthropic\n")
    runner = ScriptedRunner({"is-system-running": (0, "running", "")})
    manager = SystemdUserService(home=tmp_path / "home", runner=runner)
    monkeypatch.setattr(si, "service_manager", lambda: manager)
    monkeypatch.setattr(si, "resolve_exec_argv", lambda: ["/usr/bin/python3", "slim"])
    monkeypatch.setattr(si, "wait_for_backend_health", lambda _url: True)
    monkeypatch.setattr(
        si, "default_health_url", lambda _root=None: "http://127.0.0.1:8000/health"
    )

    rc = si.service_cli("install", root=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Backend is up" in out


def test_default_health_url_reads_config_port(tmp_path):
    # No root (or no config) keeps the slim default.
    assert si.default_health_url() == "http://127.0.0.1:8000/health"
    assert si.default_health_url(tmp_path) == "http://127.0.0.1:8000/health"
    (tmp_path / "config.env").write_text("# comment\nAPI_PORT=8010\n")
    assert si.default_health_url(tmp_path) == "http://127.0.0.1:8010/health"
    (tmp_path / "config.env").write_text('API_PORT="8011"\n')
    assert si.default_health_url(tmp_path) == "http://127.0.0.1:8011/health"
    (tmp_path / "config.env").write_text("API_PORT=junk\n")
    assert si.default_health_url(tmp_path) == "http://127.0.0.1:8000/health"


def test_service_cli_install_refuses_without_config(monkeypatch, tmp_path, capsys):
    # A config-less unit would just crash-loop (and launchd never gives up),
    # so the CLI refuses with the actual fix.
    manager = SystemdUserService(home=tmp_path, runner=ScriptedRunner())
    monkeypatch.setattr(si, "service_manager", lambda: manager)
    rc = si.service_cli("install", root=tmp_path / "empty-root")
    out = capsys.readouterr().out
    assert rc == 2
    assert "nymeria init" in out


def test_command_timeout_clears_stop_timeout():
    # `systemctl --user restart` blocks through the stop phase (up to
    # TimeoutStopSec before SIGKILL) plus the start; a tighter subprocess
    # timeout would report failure on a successful slow reinstall.
    assert si.COMMAND_TIMEOUT_SECONDS >= si.UNIT_STOP_TIMEOUT_SECONDS * 2
    assert f"TimeoutStopSec={si.UNIT_STOP_TIMEOUT_SECONDS}" in build_systemd_unit(
        exec_argv=["/usr/bin/python3", "slim"], root=Path("/tmp"), path_env="/usr/bin"
    )


# --- ServiceSpec: a second supervised service beside the backend ------------------

# The backend's unit text BEFORE the spec parameter existed, for these exact
# inputs. Pinned verbatim so the refactor that made the identity a parameter
# can never change what every existing install re-generates.
_BACKEND_UNIT_GOLDEN = """[Unit]
Description=Nymeria backend (slim)
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=exec
ExecStart=/usr/bin/python3 /opt/app/run.py slim
WorkingDirectory=/home/nym/.nymeria
Environment="NYMERIA_PROJECT_ROOT=/home/nym/.nymeria"
Environment="PATH=/usr/bin:/bin"
Restart=on-failure
RestartSec=2
TimeoutStopSec=30
SyslogIdentifier=nymeria

[Install]
WantedBy=default.target
"""


def test_default_spec_reproduces_the_backend_unit_byte_for_byte():
    unit = build_systemd_unit(
        exec_argv=["/usr/bin/python3", "/opt/app/run.py", "slim"],
        root=Path("/home/nym/.nymeria"),
        path_env="/usr/bin:/bin",
    )
    assert unit == _BACKEND_UNIT_GOLDEN
    explicit = build_systemd_unit(
        exec_argv=["/usr/bin/python3", "/opt/app/run.py", "slim"],
        root=Path("/home/nym/.nymeria"),
        path_env="/usr/bin:/bin",
        spec=si.BACKEND_SERVICE,
    )
    assert explicit == unit
    plist = build_launchd_plist(
        exec_argv=["/x"], root=Path("/r"), path_env="/usr/bin"
    )
    assert plist["Label"] == LAUNCHD_LABEL
    assert plist["StandardOutPath"] == "/r/logs/service-stdout.log"


def test_spec_parametrises_unit_plist_and_paths(tmp_path):
    spec = si.ServiceSpec(
        systemd_unit="nymeria-browser-9222.service",
        launchd_label="com.nymeria.browser.9222",
        description="Nymeria server browser",
        syslog_identifier="nymeria-browser",
        log_basename="server-browser-9222",
        install_hint="nymeria browser service install",
    )
    unit = build_systemd_unit(
        exec_argv=["/x", "browser", "run"], root=Path("/r"), path_env="/usr/bin", spec=spec
    )
    assert "Description=Nymeria server browser" in unit
    assert "SyslogIdentifier=nymeria-browser" in unit
    plist = build_launchd_plist(exec_argv=["/x"], root=Path("/r"), path_env="/usr/bin", spec=spec)
    assert plist["Label"] == "com.nymeria.browser.9222"
    assert plist["StandardOutPath"] == "/r/logs/server-browser-9222-stdout.log"
    systemd = SystemdUserService(home=tmp_path, runner=ScriptedRunner(), spec=spec)
    assert systemd.artifact_path.name == "nymeria-browser-9222.service"
    assert "nymeria-browser-9222.service" in systemd.log_hint()
    launchd = LaunchdAgentService(home=tmp_path, runner=ScriptedRunner(), spec=spec)
    assert launchd.artifact_path.name == "com.nymeria.browser.9222.plist"


def test_spec_systemd_install_drives_its_own_unit_name(linux_host, tmp_path):
    spec = si.ServiceSpec(systemd_unit="nymeria-browser-9300.service", description="d")
    runner = ScriptedRunner({"is-system-running": (0, "running", "")})
    manager = SystemdUserService(home=tmp_path, runner=runner, spec=spec)
    manager.install(exec_argv=["/x", "browser", "run"], root=tmp_path)
    lines = runner.command_lines()
    assert "systemctl --user enable nymeria-browser-9300.service" in lines
    assert "systemctl --user restart nymeria-browser-9300.service" in lines
    assert not any("enable nymeria.service" in line for line in lines)
    assert (tmp_path / ".config" / "systemd" / "user" / "nymeria-browser-9300.service").exists()
    # The backend's own unit is untouched by a browser uninstall.
    (tmp_path / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME).write_text("backend")
    manager.uninstall()
    assert (tmp_path / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME).read_text() == "backend"
    assert not manager.artifact_path.exists()
    with pytest.raises(ServiceInstallError, match="nymeria browser service install"):
        SystemdUserService(
            home=tmp_path,
            runner=runner,
            spec=si.ServiceSpec(systemd_unit="x.service", install_hint="nymeria browser service install"),
        ).restart()


def test_windows_task_manager_shim_and_command_sequence(tmp_path, monkeypatch):
    spec = si.ServiceSpec(windows_task_name="NymeriaOS Server Browser 9222", install_hint="h")
    runner = ScriptedRunner({"/Query": (0, "HostName: X\nStatus: Running\n", "")})
    manager = si.WindowsScheduledTaskService(spec=spec, home=tmp_path, runner=runner)
    monkeypatch.setattr(si.sys, "platform", "win32")
    monkeypatch.setattr(si.shutil, "which", lambda name: f"C:/W/{name}.exe")
    report = manager.install(
        exec_argv=["C:/Program Files/py/python.exe", "C:/nym/run.py", "browser", "run"],
        root=tmp_path,
    )
    shim = manager.artifact_path.read_text(encoding="utf-16")
    # Paths with spaces are double-double-quoted for VBScript; plain args are not.
    assert 'shell.Run "' in shim and '""C:/Program Files/py/python.exe""' in shim
    assert " browser run" in shim
    assert manager.artifact_path.name == "nymeriaos-server-browser-9222.vbs"
    lines = runner.command_lines()
    # The task is stopped before it is (re)created and run, so an install
    # means the same thing here as `systemctl restart` and launchd's
    # bootout+bootstrap: the previously started command does not survive it.
    assert lines.index("schtasks /End /TN NymeriaOS Server Browser 9222") < lines.index(
        "schtasks /Run /TN NymeriaOS Server Browser 9222"
    )
    assert any(
        line.startswith("schtasks /Create /F /TN NymeriaOS Server Browser 9222 /TR wscript.exe")
        and "/SC ONLOGON /RL LIMITED" in line
        for line in lines
    )
    assert "schtasks /Run /TN NymeriaOS Server Browser 9222" in lines
    assert report.artifact == manager.artifact_path
    status = manager.status()
    assert status.installed and status.running and status.detail == "Running"
    removed = manager.uninstall()
    assert "schtasks /Delete /F /TN NymeriaOS Server Browser 9222" in runner.command_lines()
    assert not manager.artifact_path.exists() and removed


def test_windows_shim_survives_a_non_ascii_project_root(tmp_path, monkeypatch):
    # `C:\Users\<name>` carries whatever the account is called. Writing the
    # shim as ascii raised UnicodeEncodeError (a ValueError, so it escaped
    # every OSError handler and came out of `nymeria init` as a traceback),
    # and plain UTF-8 would mojibake the path because wscript reads a
    # BOM-less .vbs as ANSI.
    spec = si.ServiceSpec(windows_task_name="NymeriaOS Server Browser 9222", install_hint="h")
    manager = si.WindowsScheduledTaskService(spec=spec, home=tmp_path, runner=ScriptedRunner())
    monkeypatch.setattr(si.sys, "platform", "win32")
    monkeypatch.setattr(si.shutil, "which", lambda name: f"C:/W/{name}.exe")
    argv = ["C:/Users/Jos\u00e9 \u041c\u0430\u0440\u0438\u044f/py.exe", "C:/nym/run.py", "browser", "run"]
    manager.install(exec_argv=argv, root=tmp_path)
    raw = manager.artifact_path.read_bytes()
    assert raw[:2] == b"\xff\xfe", "wscript detects UTF-16 only by its BOM"
    text = raw.decode("utf-16")
    assert '""C:/Users/Jos\u00e9 \u041c\u0430\u0440\u0438\u044f/py.exe""' in text


def test_windows_task_manager_requires_a_task_name():
    with pytest.raises(ServiceUnavailableError):
        si.WindowsScheduledTaskService(spec=si.ServiceSpec())


def test_service_manager_on_windows_needs_a_task_name(monkeypatch):
    monkeypatch.setattr(si.sys, "platform", "win32")
    with pytest.raises(ServiceUnavailableError):
        service_manager(runner=ScriptedRunner())
    manager = service_manager(
        runner=ScriptedRunner(), spec=si.ServiceSpec(windows_task_name="T")
    )
    assert isinstance(manager, si.WindowsScheduledTaskService)
