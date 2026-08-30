"""Cover for the API's self-restart mechanism (#300).

`restart_api_process` restarts IN PLACE via `os.execve` rather than spawning a
detached copy and exiting. That distinction is the whole fix: every way this
service is supervised keys on the process exiting, so the old shape left a
systemd unit `inactive (dead)` and tore down a container's PID namespace.

The process replacement itself is not unit-testable (a real execve would
replace the pytest worker), so these pin the contract around it: which
mechanism is used, with what arguments, in what order, and what happens when it
fails. The measured evidence that the mechanism works under real supervisors is
recorded in the shipped note, not here.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import nymeria.api.routers.system as system_mod


class _FakeTicker:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True
        self._events.append("ticker-stopped")


class _FakeAgent:
    def __init__(self, events: list[str]) -> None:
        self._ticker = _FakeTicker(events)


class _FakeSettings:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root


class _RestartHarness:
    """Fakes for the three process-level calls, so nothing actually restarts."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.execve_calls: list[tuple[str, list[str], dict[str, str]]] = []
        self.popen_calls: list[dict[str, Any]] = []
        self.exit_calls: list[int] = []
        self.execve_error: Exception | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_execve(path, argv, env):
            self.events.append("execve")
            self.execve_calls.append((path, list(argv), dict(env)))
            if self.execve_error is not None:
                raise self.execve_error

        def fake_popen(argv, **kwargs):
            self.events.append("popen")
            self.popen_calls.append({"argv": list(argv), **kwargs})
            return object()

        def fake_exit(code):
            self.events.append("exit")
            self.exit_calls.append(code)

        monkeypatch.setattr(os, "execve", fake_execve)
        monkeypatch.setattr(os, "_exit", fake_exit)
        monkeypatch.setattr(subprocess, "Popen", fake_popen)


async def _run_restart(agent: Any, settings: Any) -> None:
    """Fire the restart and let its scheduled task finish (it sleeps 0.5s)."""
    system_mod.restart_api_process(agent, settings)
    await asyncio.sleep(0.7)


@pytest.mark.asyncio
async def test_restart_replaces_the_process_image_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The core of #300: no child process, no exit. A supervisor must observe
    # nothing at all, because both systemd's KillMode=control-group and a
    # container's PID-namespace teardown trigger on this process exiting.
    harness = _RestartHarness()
    harness.install(monkeypatch)
    events = harness.events

    await _run_restart(_FakeAgent(events), _FakeSettings(tmp_path))

    assert harness.execve_calls, "expected an in-place execve"
    assert harness.popen_calls == [], "a restart must not spawn a child process"
    assert harness.exit_calls == [], "a restart must not exit the process"


@pytest.mark.asyncio
async def test_restart_rebuilds_the_launch_command_via_the_shared_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NOT `[sys.executable] + sys.argv`. That is wrong for a `python -m`
    # launch (argv[0] is the module file, which cannot be re-run as a script)
    # and for a frozen build, and the failure is silent: the exec succeeds and
    # the new image dies at startup. `service_install.resolve_exec_argv` already
    # handles those shapes for the service unit, and it is handed THIS
    # process's own arguments so the restart re-runs whatever it was started as.
    import nymeria.service_install as service_install

    seen_args: list[list[str]] = []

    def fake_resolve(args=("slim",)):
        seen_args.append(list(args))
        return ["/resolved/python", "-m", "nymeria.cli_entry", *args]

    monkeypatch.setattr(service_install, "resolve_exec_argv", fake_resolve)
    harness = _RestartHarness()
    harness.install(monkeypatch)

    await _run_restart(_FakeAgent(harness.events), _FakeSettings(tmp_path))

    assert seen_args == [sys.argv[1:]], "the resolver must get this process's args"
    path, argv, _env = harness.execve_calls[0]
    assert argv == ["/resolved/python", "-m", "nymeria.cli_entry", *sys.argv[1:]]
    # execve's path argument must be the resolved command, not sys.executable:
    # the console-script branch returns a script path as argv[0].
    assert path == "/resolved/python"


@pytest.mark.asyncio
async def test_restart_merges_dotenv_over_the_inherited_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is what makes a restart APPLY a config-file edit, and it is the
    # mechanism the settings applier points at when it declines to apply a
    # hand-edit itself. A key edited in the file must beat the stale value the
    # running process booted with; an unrelated process var must survive.
    (tmp_path / ".env").write_text("LLM_MODEL=edited-in-file\n", encoding="utf-8")
    monkeypatch.setenv("LLM_MODEL", "stale-in-process")
    monkeypatch.setenv("UNRELATED_PROCESS_VAR", "kept")
    harness = _RestartHarness()
    harness.install(monkeypatch)

    await _run_restart(_FakeAgent(harness.events), _FakeSettings(tmp_path))

    _path, _argv, env = harness.execve_calls[0]
    assert env["LLM_MODEL"] == "edited-in-file"
    assert env["UNRELATED_PROCESS_VAR"] == "kept"


@pytest.mark.asyncio
async def test_restart_stops_the_ticker_before_replacing_the_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ordering matters: nothing after the exec runs, so a ticker stopped
    # "after" it would never be stopped at all.
    harness = _RestartHarness()
    harness.install(monkeypatch)
    agent = _FakeAgent(harness.events)

    await _run_restart(agent, _FakeSettings(tmp_path))

    assert agent._ticker.stopped is True
    assert harness.events == ["ticker-stopped", "execve"]


@pytest.mark.asyncio
async def test_restart_falls_back_to_spawning_when_exec_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed execve leaves a live process whose ticker is already stopped, so
    # it must not simply return. Falling back to the old shape is worse under a
    # supervisor but never worse than the behavior this replaced.
    harness = _RestartHarness()
    harness.execve_error = OSError("exec format error")
    harness.install(monkeypatch)

    await _run_restart(_FakeAgent(harness.events), _FakeSettings(tmp_path))

    assert harness.events == ["ticker-stopped", "execve", "popen", "exit"]
    assert harness.exit_calls == [0]


@pytest.mark.asyncio
async def test_restart_on_windows_keeps_the_spawn_and_exit_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows os.execv goes through the CRT and creates a process with a NEW
    # pid, so the in-place guarantee does not hold there and the desktop
    # shell's job object depends on the current shape. Deliberately unchanged.
    import nymeria.service_install as service_install

    monkeypatch.setattr(
        service_install,
        "resolve_exec_argv",
        lambda args=("slim",): ["/frozen/nymeria.exe", *args],
    )
    monkeypatch.setattr(sys, "platform", "win32")
    harness = _RestartHarness()
    harness.install(monkeypatch)

    await _run_restart(_FakeAgent(harness.events), _FakeSettings(tmp_path))

    assert harness.execve_calls == [], "Windows must not take the in-place path"
    assert harness.popen_calls, "expected the spawn-and-exit path"
    # The spawn path must use the RESOLVED command too, not
    # `[sys.executable] + sys.argv`. Windows is the only platform that has just
    # this path, and Windows is where the frozen build lives: under PyInstaller
    # sys.executable == sys.argv[0], so the naive form spawns [exe, exe, "api"]
    # and run.py's subparser reads the exe path as the subcommand.
    assert harness.popen_calls[0]["argv"] == ["/frozen/nymeria.exe", *sys.argv[1:]]
    assert harness.popen_calls[0]["start_new_session"] is False
    assert harness.exit_calls == [0]


@pytest.mark.asyncio
async def test_restart_survives_an_env_file_it_cannot_decode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-UTF-8 byte in an env file (a latin-1 password, a Notepad UTF-16
    # BOM) must not abort the restart. run.py swallows UnicodeDecodeError at
    # its own load site for this exact condition, and the pre-merge here is
    # belt-and-braces anyway: the restarted image re-merges the same files.
    # Aborting would be the worst outcome, since the caller was already told
    # the server is restarting.
    (tmp_path / ".env").write_bytes(b"PASSWORD=caf\xe9\nGOOD_KEY=readable\n")
    (tmp_path / "config.env").write_text("FROM_SECOND_FILE=yes\n", encoding="utf-8")
    harness = _RestartHarness()
    harness.install(monkeypatch)

    await _run_restart(_FakeAgent(harness.events), _FakeSettings(tmp_path))

    assert harness.execve_calls, "an undecodable env file must not abort the restart"
    _path, _argv, env = harness.execve_calls[0]
    # The readable file is still merged; the undecodable one is skipped whole.
    assert env["FROM_SECOND_FILE"] == "yes"
    assert "PASSWORD" not in env


@pytest.mark.asyncio
async def test_restart_does_not_stop_the_ticker_before_it_can_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stopping the ticker writes a clean-shutdown record and takes down
    # scheduled TODOs, trigger polling and the sweeps. If preparation blows up
    # after that, the API keeps answering with a dead scheduler and a shutdown
    # record that lies, and nothing looks wrong. So every fallible step runs
    # first: a failure here must leave the ticker RUNNING.
    import nymeria.service_install as service_install

    def exploding_resolver(args=("slim",)):
        raise RuntimeError("cannot resolve the launch command")

    monkeypatch.setattr(service_install, "resolve_exec_argv", exploding_resolver)
    harness = _RestartHarness()
    harness.install(monkeypatch)
    agent = _FakeAgent(harness.events)

    await _run_restart(agent, _FakeSettings(tmp_path))

    assert agent._ticker.stopped is False, "a failed restart must not kill the ticker"
    assert harness.events == []
