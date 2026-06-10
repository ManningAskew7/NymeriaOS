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


# --- platform selection and CLI -------------------------------------------------


def test_service_manager_unsupported_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ServiceUnavailableError) as excinfo:
        service_manager()
    assert any("shell:startup" in hint for hint in excinfo.value.hints)


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
    monkeypatch.setattr(si, "default_health_url", lambda: "http://127.0.0.1:8000/health")

    rc = si.service_cli("install", root=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert "Backend is up" in out


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
