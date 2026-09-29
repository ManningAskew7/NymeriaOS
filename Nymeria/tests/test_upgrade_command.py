"""`nymeria upgrade` (#353): stop, upgrade, restart; never from inside the env on Windows."""

from __future__ import annotations

import codecs
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import run
from nymeria import __version__
from nymeria import upgrade as up

_OLD = frozenset({"nymeriaos-0.2.0b6.dist-info", "httpx-0.28.1.dist-info"})
_NEW = frozenset({"nymeriaos-0.2.0b7.dist-info", "httpx-0.28.1.dist-info"})
_DEPS_ONLY = frozenset({"nymeriaos-0.2.0b6.dist-info", "httpx-0.28.2.dist-info"})


def _tool_prefix(
    root: Path,
    *,
    editable: str | None = None,
    kind: str = "uv",
    package: str = "nymeriaos",
    shim: str | None = None,
) -> Path:
    # Deliberately not under a `uv/tools` or `pipx/venvs` path: detection must
    # come from the marker file, as with a relocated UV_TOOL_DIR or PIPX_HOME.
    prefix = root / "tools-dir" / "nymeriaos"
    prefix.mkdir(parents=True)
    if kind == "uv":
        extra = f', editable = "{editable}"' if editable else ""
        entry = (
            f"\nentrypoints = [{{ name = \"nymeria\", install-path = '{shim}', from = \"nymeriaos\" }}]"
            if shim
            else ""
        )
        (prefix / "uv-receipt.toml").write_text(
            f'[tool]\nrequirements = [{{ name = "{package}"{extra} }}]{entry}\n', encoding="utf-8"
        )
    elif kind == "pipx":
        (prefix / "pipx_metadata.json").write_text(
            json.dumps({"main_package": {"package": package}}), encoding="utf-8"
        )
    return prefix


class _Service:
    def __init__(self, fail: bool = False) -> None:
        self.restarts = 0
        self.fail = fail

    def restart(self) -> None:
        if self.fail:
            raise RuntimeError("systemctl said no")
        self.restarts += 1


def _deps(
    prefix: Path,
    *,
    platform: str = "linux",
    service=None,
    answering=False,
    snapshots=(_OLD, _NEW),
    returncode: int = 0,
    calls: list | None = None,
    tty: bool = True,
    answer: str = "y",
    popen_calls: list | None = None,
    temp: Path | None = None,
    env: dict | None = None,
):
    calls = calls if calls is not None else []
    order: list = []
    shots = iter(snapshots)

    def fake_run(argv, *, env):
        order.append({"restarts_so_far": service.restarts if service else None, "env": env})
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode)

    deps = up.UpgradeDeps(
        platform=platform,
        prefix=prefix,
        which=lambda name: f"/bin/{name}",
        run=fake_run,
        popen=lambda argv, **k: (popen_calls if popen_calls is not None else []).append((argv, k)),
        isatty=lambda: tty,
        ask=lambda _prompt: answer,
        temp_dir=lambda: str(temp or prefix),
        getenv=(env or {}).get,  # never the developer's own environment
        snapshot=lambda: next(shots),
        active_service=lambda: service,
        backend_answering=lambda: answering,
    )
    return deps, order


def test_detects_each_install_shape_from_its_marker_file(tmp_path: Path) -> None:
    assert up.detect_install(_tool_prefix(tmp_path / "a")).kind == up.UV_TOOL
    shape = up.detect_install(_tool_prefix(tmp_path / "b", editable="/src/Nymeria"))
    assert (shape.kind, shape.checkout) == (up.UV_TOOL_EDITABLE, "/src/Nymeria")
    assert up.detect_install(_tool_prefix(tmp_path / "c", kind="pipx")).kind == up.PIPX
    assert up.detect_install(_tool_prefix(tmp_path / "d", kind="none")).kind == up.OTHER
    # nymeriaos pulled into another tool's env is not ours to upgrade by name.
    assert up.detect_install(_tool_prefix(tmp_path / "e", package="other-tool")).kind == up.OTHER
    assert up.detect_install(_tool_prefix(tmp_path / "f", kind="pipx", package="other")).kind == up.OTHER
    # The receipt names the real launcher, which beats whatever PATH finds first.
    shim = "C:\\Users\\u\\.local\\bin\\nymeria.exe"
    assert up.detect_install(_tool_prefix(tmp_path / "g", shim=shim)).shim == shim


def test_uv_tool_upgrade_runs_then_restarts_the_active_service(tmp_path: Path, capsys) -> None:
    # U1: upgrade in place, restart AFTER (never before: a failed upgrade must
    # leave the running service alone), report the version change.
    service = _Service()
    calls: list = []
    deps, order = _deps(_tool_prefix(tmp_path), service=service, calls=calls)
    assert up.upgrade_cli(yes=True, deps=deps) == 0
    assert calls == [["/bin/uv", "tool", "upgrade", "nymeriaos"]]
    assert order[0]["restarts_so_far"] == 0  # not restarted yet when uv ran
    assert service.restarts == 1
    out = capsys.readouterr().out
    assert "Upgraded nymeriaos 0.2.0b6 -> 0.2.0b7." in out and "Restarted the background service." in out


def test_nothing_changed_restarts_nothing(tmp_path: Path, capsys) -> None:
    service = _Service()
    deps, _ = _deps(_tool_prefix(tmp_path), service=service, snapshots=(_OLD, _OLD))
    assert up.upgrade_cli(yes=True, deps=deps) == 0
    assert service.restarts == 0
    assert "Already up to date (0.2.0b6)." in capsys.readouterr().out


def test_a_dependency_only_upgrade_still_restarts(tmp_path: Path, capsys) -> None:
    # The running backend imported the old httpx; it must pick up the new one.
    service = _Service()
    deps, _ = _deps(_tool_prefix(tmp_path), service=service, snapshots=(_OLD, _DEPS_ONLY))
    assert up.upgrade_cli(yes=True, deps=deps) == 0
    assert service.restarts == 1
    assert "nymeriaos 0.2.0b6 is current; some of its dependencies were upgraded." in capsys.readouterr().out


def test_an_unreadable_environment_counts_as_changed(tmp_path: Path, capsys) -> None:
    service = _Service()
    deps, _ = _deps(_tool_prefix(tmp_path), service=service, snapshots=(frozenset(), frozenset()))
    assert up.upgrade_cli(yes=True, deps=deps) == 0
    assert service.restarts == 1
    assert "Upgrade finished" in capsys.readouterr().out


def test_the_real_snapshot_reads_this_environment() -> None:
    # The default seam, unfaked: the suite's own environment has dist-info dirs.
    assert any(name.startswith("pytest-") for name in up.installed_distributions())


def test_a_failed_upgrade_restarts_nothing_and_says_what_still_runs(tmp_path: Path, capsys) -> None:
    # U7
    service = _Service()
    deps, _ = _deps(_tool_prefix(tmp_path), service=service, returncode=3)
    assert up.upgrade_cli(yes=True, deps=deps) == 3
    assert service.restarts == 0
    out = capsys.readouterr().out
    assert "failed (exit 3)" in out and f"keeps version {__version__}" in out


def test_a_foreground_backend_gets_a_restart_hint(tmp_path: Path, capsys) -> None:
    deps, _ = _deps(_tool_prefix(tmp_path), answering=True)
    assert up.upgrade_cli(yes=True, deps=deps) == 0
    assert "Restart the backend that is running in another terminal" in capsys.readouterr().out


def test_a_service_restart_failure_is_reported_not_hidden(tmp_path: Path, capsys) -> None:
    deps, _ = _deps(_tool_prefix(tmp_path), service=_Service(fail=True))
    assert up.upgrade_cli(yes=True, deps=deps) == 1
    assert "nymeria service restart" in capsys.readouterr().out


def test_the_upgrade_never_inherits_the_deployment_secrets(tmp_path: Path, monkeypatch) -> None:
    # U9: run.py loads the deployment .env into this process; a package build
    # (arbitrary setup.py) must not see it. The user's own package-manager and
    # network settings must survive, or uv looks in the wrong tool dir.
    for name, value in {
        "NYMERIA_SECRETS_KEY": "vault-key",
        "NYMERIA_SERVICE_TOKEN": "svc-token",
        "OPENAI_API_KEY": "sk-live",
        "UV_TOOL_DIR": "/opt/uv-tools",
        "PIPX_HOME": "/opt/pipx",
        "HTTPS_PROXY": "http://proxy:3128",
    }.items():
        monkeypatch.setenv(name, value)
    deps, order = _deps(_tool_prefix(tmp_path))
    assert up.upgrade_cli(yes=True, deps=deps) == 0
    env = order[0]["env"]
    assert not {"NYMERIA_SECRETS_KEY", "NYMERIA_SERVICE_TOKEN", "OPENAI_API_KEY"} & set(env)
    assert env["UV_TOOL_DIR"] == "/opt/uv-tools" and env["PIPX_HOME"] == "/opt/pipx"
    assert env["HTTPS_PROXY"] == "http://proxy:3128"


def test_editable_install_is_refused_with_git_pull(tmp_path: Path, capsys) -> None:
    # U2
    calls: list = []
    deps, _ = _deps(_tool_prefix(tmp_path, editable="/src/Nymeria"), calls=calls)
    assert up.upgrade_cli(yes=True, deps=deps) == 2
    assert calls == []
    assert "git -C /src/Nymeria pull --ff-only" in capsys.readouterr().out


def test_pipx_install_runs_pipx_upgrade(tmp_path: Path) -> None:
    # U3
    calls: list = []
    deps, _ = _deps(_tool_prefix(tmp_path, kind="pipx"), calls=calls)
    assert up.upgrade_cli(yes=True, deps=deps) == 0
    assert calls == [["/bin/pipx", "upgrade", "nymeriaos"]]


def test_unknown_install_is_refused_with_the_pip_line(tmp_path: Path, capsys) -> None:
    # U4
    calls: list = []
    deps, _ = _deps(tmp_path / "venv", calls=calls)
    assert up.upgrade_cli(yes=True, deps=deps) == 2
    assert calls == []
    assert "-m pip install --upgrade nymeriaos" in capsys.readouterr().out


def test_missing_uv_is_refused(tmp_path: Path, capsys) -> None:
    calls: list = []
    deps, _ = _deps(_tool_prefix(tmp_path), calls=calls)
    deps.which = lambda name: None
    assert up.upgrade_cli(yes=True, deps=deps) == 2
    assert calls == [] and "uv tool upgrade nymeriaos" in capsys.readouterr().out


def test_dry_run_and_non_interactive_change_nothing(tmp_path: Path, capsys) -> None:
    # U5
    calls: list = []
    deps, _ = _deps(_tool_prefix(tmp_path), calls=calls, service=_Service())
    assert up.upgrade_cli(dry_run=True, deps=deps) == 0
    assert "Dry run: nothing changed." in capsys.readouterr().out
    deps, _ = _deps(_tool_prefix(tmp_path / "x"), calls=calls, tty=False)
    assert up.upgrade_cli(deps=deps) == 1
    assert "pass --yes" in capsys.readouterr().out
    deps, _ = _deps(_tool_prefix(tmp_path / "y"), calls=calls, answer="n")
    assert up.upgrade_cli(deps=deps) == 1
    assert calls == []


def test_windows_hands_off_to_a_detached_script_and_runs_nothing_itself(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    # U6
    monkeypatch.setenv("NYMERIA_SECRETS_KEY", "vault-key")
    monkeypatch.setenv("PSModulePath", "C:\\Windows\\system32\\WindowsPowerShell\\v1.0\\Modules")
    calls: list = []
    popen_calls: list = []
    shim = "C:\\Users\\u\\.local\\bin\\nymeria.exe"
    prefix = _tool_prefix(tmp_path, shim=shim)
    deps, _ = _deps(prefix, platform="win32", calls=calls, popen_calls=popen_calls, temp=tmp_path)
    assert up.upgrade_cli(yes=True, deps=deps) == 0
    assert calls == []  # the parent never runs the upgrade
    [(argv, kwargs)] = popen_calls
    assert argv[:5] == ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
    assert "NYMERIA_SECRETS_KEY" not in kwargs["env"] and "PSModulePath" in kwargs["env"]
    # Windows PowerShell 5.1 reads a BOM-less script as ANSI and would mangle
    # a non-ASCII profile path, so the stop step would match nothing.
    assert Path(argv[5]).read_bytes().startswith(codecs.BOM_UTF8)
    script = Path(argv[5]).read_text(encoding="utf-8-sig")
    assert "& '/bin/uv' 'tool' 'upgrade' 'nymeriaos'" in script
    assert f"$envDir = '{prefix}'" in script
    assert f"$shim = '{shim}'" in script  # the receipt's launcher, not PATH's
    # The script outlives this process and whatever launched it.
    assert f"@({os.getpid()}, {os.getppid()})" in script
    assert "continues in a new window" in capsys.readouterr().out


def test_the_windows_script_stops_before_upgrading_and_restarts_only_a_backend() -> None:
    script = up.build_windows_script(
        ["C:\\uv\\uv.exe", "tool", "upgrade", "nymeriaos"],
        wait_pids=[101, 202],
        env_prefix=Path("C:/Users/o'brien/uv/tools/nymeriaos"),
        shim="C:\\Users\\o'brien\\.local\\bin\\nymeria.exe",
        log_path=Path("C:/tmp/up.log"),
    )
    lines = script.splitlines()

    def first(pred) -> int:
        return next(i for i, line in enumerate(lines) if pred(line))

    wait = first(lambda line: "Wait-Process" in line)
    stop = first(lambda line: "Stop-Process" in line)
    abort = first(lambda line: line.strip() == "exit 1")
    upgrade = first(lambda line: line.startswith("& 'C:\\uv\\uv.exe'"))
    guard = first(lambda line: line.startswith("if ($backend)"))
    restart = first(lambda line: "schtasks /run" in line)
    assert wait < stop < abort < upgrade < guard < restart
    assert "@(101, 202)" in lines[wait]
    # PowerShell single-quote escaping: o'brien must not end the string.
    assert "$envDir = 'C:/Users/o''brien/uv/tools/nymeriaos'" in script
    assert "$shim = 'C:\\Users\\o''brien\\.local\\bin\\nymeria.exe'" in script
    assert "'NymeriaOS Slim'" in lines[restart]


def test_the_cli_command_is_registered_and_passes_its_flags(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr("nymeria.upgrade.upgrade_cli", lambda **kw: seen.update(kw) or 0)
    assert run.run_upgrade(run.build_parser().parse_args(["upgrade", "--yes", "--dry-run"])) == 0
    assert seen == {"yes": True, "dry_run": True}
    assert run.run_upgrade(run.build_parser().parse_args(["upgrade"])) == 0
    assert seen == {"yes": False, "dry_run": False}
    assert run.COMMANDS["upgrade"].exits is True


@pytest.mark.parametrize("shape_kind", [up.UV_TOOL, up.PIPX])
def test_the_upgrade_argv_needs_its_tool_on_path(tmp_path: Path, shape_kind: str) -> None:
    shape = up.InstallShape(shape_kind, tmp_path)
    assert up.upgrade_argv(shape, which=lambda name: None) is None


_INDEX = "https://beta:s3cret-pw@pypi.example.com/simple/"


@pytest.mark.parametrize(
    ("kind", "flag", "tool"), [("uv", "--index", "/bin/uv"), ("pipx", "--index-url", "/bin/pipx")]
)
def test_the_installers_private_index_is_passed_again_and_never_printed(
    tmp_path: Path, capsys, kind: str, flag: str, tool: str
) -> None:
    # U8: uv strips the credentials from its receipt, so a bare upgrade of a
    # private-index install fails authentication; the URL must ride along.
    calls: list = []
    deps, _ = _deps(_tool_prefix(tmp_path, kind=kind), calls=calls, env={up.INDEX_ENV: _INDEX})
    assert up.upgrade_cli(yes=True, deps=deps) == 0
    assert calls[0][0] == tool and calls[0][-2:] == [flag, _INDEX]
    out = capsys.readouterr().out
    assert "s3cret-pw" not in out
    assert "https://***@pypi.example.com/simple/" in out


def test_the_windows_script_reads_the_index_from_the_environment(tmp_path: Path, capsys) -> None:
    popen_calls: list = []
    deps, _ = _deps(
        _tool_prefix(tmp_path), platform="win32", popen_calls=popen_calls, temp=tmp_path,
        env={up.INDEX_ENV: _INDEX},
    )
    assert up.upgrade_cli(yes=True, deps=deps) == 0
    [(argv, kwargs)] = popen_calls
    script = Path(argv[5]).read_text(encoding="utf-8")
    assert "s3cret-pw" not in script and "s3cret-pw" not in capsys.readouterr().out
    assert "& '/bin/uv' 'tool' 'upgrade' 'nymeriaos' '--index' $env:NYMERIA_PYPI_SIMPLE_INDEX_URL" in script
    # ...which the child console can only resolve if it was handed the value.
    assert kwargs["env"][up.INDEX_ENV] == _INDEX


def test_missing_uv_names_the_index_variable_rather_than_its_secret(tmp_path: Path, capsys) -> None:
    deps, _ = _deps(_tool_prefix(tmp_path), env={up.INDEX_ENV: _INDEX})
    deps.which = lambda name: None
    assert up.upgrade_cli(yes=True, deps=deps) == 2
    out = capsys.readouterr().out
    assert 'uv tool upgrade nymeriaos --index "$NYMERIA_PYPI_SIMPLE_INDEX_URL"' in out
    assert "s3cret-pw" not in out


def test_redaction_survives_an_at_sign_inside_the_password() -> None:
    assert up.redact("https://u:p@ss@host/simple/ and ftp://a@b/c") == "https://***@host/simple/ and ftp://***@b/c"


# The generated script, run for real under PowerShell with the Windows-only
# cmdlets stubbed. Runs wherever a PowerShell exists (the Windows dev box
# always; Linux with pwsh installed); skipped elsewhere.
_POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
# A non-ASCII profile: under Windows PowerShell 5.1 this only matches if the
# script carries its UTF-8 BOM.
_ENV = "C:\\Users\\José\\AppData\\Roaming\\uv\\tools\\nymeriaos"
_SHIM = "C:\\Users\\José\\.local\\bin\\nymeria.exe"
_BASE_PY = "C:\\Users\\José\\AppData\\Roaming\\uv\\python\\cpython-3.12\\python.exe"

_HARNESS = r"""
$global:procs = ConvertFrom-Json @'
__PROCS__
'@
# [int]: ConvertFrom-Json yields Int64, which ArrayList.Remove([int]) never matches.
$global:alive = [System.Collections.ArrayList]@($global:procs | ForEach-Object { [int]$_.ProcessId })
$global:stubborn = @(__STUBBORN__)
$global:rec = [ordered]@{ stopped = @(); waited = @(); uv = @(); schtasks = @() }
$global:clock = [datetime]'2026-01-01'
function Get-CimInstance { param($ClassName) $global:procs | Where-Object { $global:alive -contains $_.ProcessId } }
function Stop-Process { [CmdletBinding()] param([int]$Id, [switch]$Force)
    $global:rec.stopped += $Id
    if ($global:stubborn -notcontains $Id) { $global:alive.Remove($Id) } }
function Wait-Process { [CmdletBinding()] param([int]$Id, [int]$Timeout) $global:rec.waited += $Id }
function Start-Transcript { [CmdletBinding()] param($Path, [switch]$Append) }
function Stop-Transcript { [CmdletBinding()] param() }
function Read-Host { param($Prompt) }
function Start-Sleep { [CmdletBinding()] param([int]$Milliseconds) }
function Get-Date { $global:clock = $global:clock.AddSeconds(1); $global:clock }
function fakeuv { $global:rec.uv += ,@($args); 'Updated nymeriaos v0.2.0b6 -> v0.2.0b7'; $global:LASTEXITCODE = __UV_EXIT__ }
function schtasks { $global:rec.schtasks += ($args -join ' '); $global:LASTEXITCODE = 0 }
& '__SCRIPT__'
Write-Output ("RECORD:" + ($global:rec | ConvertTo-Json -Compress -Depth 5))
"""


def _run_under_powershell(
    tmp_path: Path, procs: list[dict], *, stubborn=(), uv_exit=0, argv=("fakeuv", "tool", "upgrade", "nymeriaos")
) -> tuple[dict, str]:
    script = up.build_windows_script(
        list(argv),
        wait_pids=[4242],
        env_prefix=Path(_ENV),
        shim=_SHIM,
        log_path=tmp_path / "up.log",
    )
    script_path = tmp_path / "upgrade.ps1"
    script_path.write_text(script, encoding="utf-8-sig")  # as _upgrade_windows writes it
    harness = (
        _HARNESS.replace("__PROCS__", json.dumps(procs))
        .replace("__STUBBORN__", ", ".join(str(i) for i in stubborn))
        .replace("__UV_EXIT__", str(uv_exit))
        .replace("__SCRIPT__", str(script_path))
    )
    harness_path = tmp_path / "harness.ps1"
    harness_path.write_text(harness, encoding="utf-8-sig")
    assert _POWERSHELL
    result = subprocess.run(
        [_POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness_path)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "NO_COLOR": "1"},
    )
    record = next(
        (json.loads(line[len("RECORD:"):]) for line in result.stdout.splitlines() if line.startswith("RECORD:")),
        None,
    )
    assert record is not None, result.stdout + result.stderr
    return record, result.stdout


def _proc(pid: int, exe: str | None, cmd: str | None, name: str = "python.exe") -> dict:
    return {"ProcessId": pid, "Name": name, "ExecutablePath": exe, "CommandLine": cmd}


# One running backend: all three layers of `nymeria slim`, plus bystanders.
_BACKEND = [
    _proc(11, _SHIM, f'"{_SHIM}" slim', "nymeria.exe"),
    _proc(12, _ENV + "\\Scripts\\python.exe", f'"{_ENV}\\Scripts\\python.exe" "{_SHIM}" slim'),
    _proc(13, _BASE_PY, f'"{_BASE_PY}" "{_SHIM}" slim'),  # outside the env: found by command line
]
_BYSTANDERS = [
    _proc(21, "C:\\Windows\\notepad.exe", "notepad.exe", "notepad.exe"),
    _proc(22, _ENV + "-extra\\Scripts\\python.exe", "python.exe x"),  # sibling dir, not ours
    _proc(23, None, None, "System"),  # another user's process: no path visible
    # Not python, so a command line mentioning the env does not make them ours.
    _proc(41, "C:\\Code\\Code.exe", f'"C:\\Code\\Code.exe" "{_ENV}\\Lib\\site-packages\\nymeria\\upgrade.py"', "Code.exe"),
    _proc(42, "C:\\Windows\\System32\\cmd.exe", f'cmd.exe /k cd /d "{_ENV}\\Lib"', "cmd.exe"),
]


@pytest.mark.skipif(_POWERSHELL is None, reason="no PowerShell on this host")
def test_powershell_stops_every_layer_of_a_backend_upgrades_and_restarts_the_task(tmp_path: Path) -> None:
    record, out = _run_under_powershell(tmp_path, _BACKEND + _BYSTANDERS)
    assert record["waited"] == [4242]
    assert sorted(set(record["stopped"])) == [11, 12, 13]
    assert record["uv"] == [["tool", "upgrade", "nymeriaos"]]
    assert "Updated nymeriaos v0.2.0b6 -> v0.2.0b7" in out  # the transcript sees uv's report
    assert record["schtasks"] == ["/query /tn NymeriaOS Slim", "/run /tn NymeriaOS Slim"]


@pytest.mark.skipif(_POWERSHELL is None, reason="no PowerShell on this host")
def test_powershell_stops_a_cli_window_but_restarts_no_backend(tmp_path: Path) -> None:
    # `--transport api` must not read as an `api` backend.
    cli = [
        _proc(31, _SHIM, f'"{_SHIM}" cli --transport api', "nymeria.exe"),
        _proc(32, _BASE_PY, f'"{_BASE_PY}" "{_SHIM}" cli --transport api'),
    ]
    record, _ = _run_under_powershell(tmp_path, cli + _BYSTANDERS)
    assert sorted(set(record["stopped"])) == [31, 32]
    assert record["uv"] == [["tool", "upgrade", "nymeriaos"]]
    assert record["schtasks"] == []


@pytest.mark.skipif(_POWERSHELL is None, reason="no PowerShell on this host")
def test_powershell_refuses_to_upgrade_over_a_process_that_will_not_stop(tmp_path: Path) -> None:
    # Upgrading with a locked file is exactly how the half-deleted install happens.
    record, out = _run_under_powershell(tmp_path, _BACKEND, stubborn=[13])
    assert record["uv"] == []
    assert "would not stop" in out and "13" in out


@pytest.mark.skipif(_POWERSHELL is None, reason="no PowerShell on this host")
def test_powershell_reports_a_failed_upgrade(tmp_path: Path) -> None:
    record, out = _run_under_powershell(tmp_path, [], uv_exit=1)
    assert record["uv"] == [["tool", "upgrade", "nymeriaos"]]
    assert "The upgrade failed (exit 1)" in out
    assert record["schtasks"] == []  # nothing was running, so nothing to restart


@pytest.mark.skipif(_POWERSHELL is None, reason="no PowerShell on this host")
def test_powershell_captures_a_native_upgrade_command_and_its_exit_code(tmp_path: Path) -> None:
    # uv is a native program that reports on stderr: its lines must reach the
    # window (and so the transcript), and its exit code must survive the pipe.
    if os.name == "nt":
        argv = ["cmd", "/c", "echo uv-said-this 1>&2 & exit /b 3"]
    else:
        argv = ["sh", "-c", "echo uv-said-this >&2; exit 3"]
    _record, out = _run_under_powershell(tmp_path, [], argv=argv)
    # A line of its own: the "Running: ..." echo also contains the words.
    assert "uv-said-this" in [line.strip() for line in out.splitlines()]
    assert "The upgrade failed (exit 3)" in out
