#!/usr/bin/env python3
"""Retry container starts that failed on a volume mount, host-scheduled.

Docker's restart policy (`always`, `unless-stopped`) only reacts to a container
PROCESS exiting. A start that fails before the process exists is abandoned:
the daemon logs `failed to start container` once and never tries again. The
case this exists for is a network volume (CIFS/NFS `local` volume) mounted
while the Docker host's own network stack is still coming up after a daemon
restart: the mount fails (`interrupted system call`, `no route to host`,
`operation now in progress`), the container stays down although the share is
reachable seconds later, and everything fronting it (a tunnel, a proxy, a
dependent container) reports the outage until a human runs `docker start`.
Incident and derivation: `Nymeria/docs/private/plans/shipped/10-operational-gotchas.md`
(CIFS section, 2026-09-04).

One tick selects containers that match ALL of:

    not running                        (`docker ps` status created or exited)
    name starts with --prefix          (default `nymeria-`)
    restart policy always/unless-stopped   Docker itself meant to keep it up
    State.Error contains               the daemon's own verdict on the last
    "error while mounting volume"      start attempt; a deliberate `docker
                                       stop` leaves this EMPTY, so an
                                       operator-stopped container is never
                                       touched, and other start failures
                                       (bad image, port clash) are not
                                       retried blindly either

and runs `docker start` on each. Failures are retried on the next tick with no
backoff (a share that is genuinely down costs one ~10 s failed mount per
tick). The log receives only attempts and outcomes: a tick with nothing to do,
or with the daemon unreachable, writes nothing, so the file stays a record of
incidents rather than heartbeat noise. It is rotated to `.1` past 256 KiB.

Exit codes: 0 nothing to do or every start succeeded, 1 at least one start
failed or timed out, 2 daemon unreachable.

stdlib only: this runs from a host interpreter on a scheduler, never from the
project venv. Nothing runs it by default; each host schedules it:

    Windows (Docker Desktop):
        python3 scripts/docker_start_retry.py --install-windows-task [--every 2]
        registers a Task Scheduler task running pythonw.exe hidden every N
        minutes in the interactive session (Docker Desktop's own lifetime),
        non-overlapping, allowed on battery. --uninstall-windows-task removes it.
    Linux: a systemd user timer calling `python3 scripts/docker_start_retry.py`
        (recipe in the doc above).
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

MOUNT_ERROR_MARKER = "error while mounting volume"
RETRY_POLICIES = frozenset({"always", "unless-stopped"})
DEFAULT_PREFIX = "nymeria-"
TASK_NAME = "Nymeria docker start retry"
LOG_CAP_BYTES = 256 * 1024
START_TIMEOUT_S = 120
PS_TIMEOUT_S = 30

# A subprocess started from a windowless host (pythonw.exe under Task
# Scheduler) would otherwise pop a console for every docker call. Zero
# everywhere else.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

Runner = Callable[[list[str], int], tuple[int, str]]


def run_command(argv: list[str], timeout: int) -> tuple[int, str]:
    """Run argv, return (rc, combined output). rc -1 means the timeout hit.

    stderr is folded into the output because `docker start` reports the mount
    error there and the log wants the daemon's own words.
    """
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return -1, f"timeout after {timeout}s"
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def docker_bin() -> str:
    found = shutil.which("docker")
    if found:
        return found
    if os.name == "nt":
        desktop = Path(r"C:\Program Files\Docker\Docker\resources\bin\docker.exe")
        if desktop.exists():
            return str(desktop)
    return "docker"


def default_log_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "nymeria" / "docker-start-retry.log"


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def container_name(record: dict[str, Any]) -> str:
    return str(record.get("Name", "")).lstrip("/")


def wants_retry(record: dict[str, Any], prefix: str) -> bool:
    """The mount-race signature, and nothing looser (see module docstring)."""
    if not container_name(record).startswith(prefix):
        return False
    state = record.get("State") or {}
    if state.get("Running"):
        return False
    policy = ((record.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name")
    if policy not in RETRY_POLICIES:
        return False
    return MOUNT_ERROR_MARKER in str(state.get("Error") or "")


def find_candidates(
    prefix: str, docker: str, runner: Runner
) -> list[dict[str, Any]] | None:
    """Inspect every stopped container; None means the daemon is unreachable."""
    rc, out = runner(
        [docker, "ps", "-aq", "--filter", "status=created", "--filter", "status=exited"],
        PS_TIMEOUT_S,
    )
    if rc != 0:
        return None
    ids = out.split()
    if not ids:
        return []
    # One JSON object per line rather than the default array: a container
    # removed between the two calls makes inspect exit non-zero and print
    # `Error: No such object` on stderr, which the runner folds into the same
    # text, and a CLI warning line does the same on a good run. Per-line
    # parsing keeps the survivors and drops the chatter.
    _, out = runner([docker, "inspect", "--format", "{{json .}}", *ids], PS_TIMEOUT_S)
    records: list[dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return [r for r in records if wants_retry(r, prefix)]


# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------


def rotate_if_needed(path: Path) -> None:
    """Rotate past the cap; a rotation that cannot happen (the file held open
    by a viewer on Windows) must not cost the tick its remaining starts."""
    try:
        if path.stat().st_size <= LOG_CAP_BYTES:
            return
        rotated = path.with_name(path.name + ".1")
        rotated.unlink(missing_ok=True)
        path.rename(rotated)
    except OSError:
        return


def log_line(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_if_needed(path)
    stamp = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


def tick(
    prefix: str,
    log_path: Path,
    dry_run: bool = False,
    runner: Runner = run_command,
    docker: str | None = None,
    start_timeout: int = START_TIMEOUT_S,
    out=None,
) -> int:
    out = out or sys.stdout
    docker = docker or docker_bin()
    candidates = find_candidates(prefix, docker, runner)
    if candidates is None:
        print("docker daemon unreachable", file=out)
        return 2
    failed = 0
    for record in candidates:
        name = container_name(record)
        last_error = _first_line(str(record["State"].get("Error") or ""))
        if dry_run:
            print(f"would start {name}: {last_error}", file=out)
            continue
        rc, output = runner([docker, "start", name], start_timeout)
        if rc == 0:
            verdict = "ok"
        else:
            failed += 1
            verdict = f"failed rc={rc}: {_first_line(output)}"
        log_line(log_path, f"{name}: start after '{last_error}' -> {verdict}")
        print(f"{name}: {verdict}", file=out)
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Windows Task Scheduler registration
# ---------------------------------------------------------------------------


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def windows_task_xml(
    *,
    user_id: str,
    pythonw: Path,
    script: Path,
    every_minutes: int,
    prefix: str,
    log_path: Path | None,
    start: _dt.datetime,
) -> str:
    """Task definition: hidden, interactive-session, repeating, non-overlapping.

    Generated rather than built with `schtasks /create /sc minute` because the
    CLI cannot express the settings that matter here: IgnoreNew (a tick still
    waiting on a slow mount must not be joined by the next one), the battery
    conditions (the CLI default refuses to run on battery), and a hard
    execution limit.
    """
    # Both values quoted: an empty prefix ('' means every container) would
    # otherwise leave `--prefix` with no argument and every tick dying in
    # argparse, invisibly, under pythonw.
    args = f'"{script}" --prefix "{prefix}"'
    if log_path is not None:
        args += f' --log "{log_path}"'
    boundary = start.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Retries Docker containers whose start failed on a volume mount (Nymeria scripts/docker_start_retry.py).</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT{every_minutes}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{boundary}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{_xml_escape(user_id)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_xml_escape(str(pythonw))}</Command>
      <Arguments>{_xml_escape(args)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def windows_pythonw() -> Path:
    """pythonw.exe beside the running interpreter: a windowless host so the
    task never flashes a console (the docker children get CREATE_NO_WINDOW)."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if not candidate.exists():
        raise SystemExit(
            f"no pythonw.exe beside {sys.executable}; run the install with an "
            "interpreter that ships one (a uv-managed CPython does)"
        )
    return candidate


def windows_user_sid(runner: Runner = run_command) -> str:
    """The account SID for the task principal, the form Task Scheduler resolves
    unambiguously (Docker Desktop's own logon task uses it). The obvious
    `USERDOMAIN\\USERNAME` reads `WORKGROUP\\user` for a local account, which
    schtasks rejects with "No mapping between account names and security IDs".
    """
    whoami = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "whoami.exe"
    rc, out = runner([str(whoami), "/user", "/fo", "csv", "/nh"], 30)
    if rc == 0 and out.strip():
        fields = next(csv.reader([out.strip().splitlines()[-1]]))
        if len(fields) == 2 and fields[1].startswith("S-1-"):
            return fields[1]
    return f"{os.environ.get('COMPUTERNAME', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")


def install_windows_task(
    every_minutes: int, prefix: str, log_path: Path | None, runner: Runner = run_command
) -> int:
    if os.name != "nt":
        print("--install-windows-task is for Windows hosts; schedule a timer here instead", file=sys.stderr)
        return 2
    xml = windows_task_xml(
        user_id=windows_user_sid(runner),
        pythonw=windows_pythonw(),
        script=Path(__file__).resolve(),
        every_minutes=every_minutes,
        prefix=prefix,
        # The task has no working directory of its own (System32 by default),
        # so a relative path given at install must be pinned here.
        log_path=log_path.resolve() if log_path is not None else None,
        start=_dt.datetime.now().astimezone(),
    )
    with tempfile.NamedTemporaryFile("w", suffix=".xml", encoding="utf-16", delete=False) as handle:
        handle.write(xml)
        xml_path = handle.name
    try:
        rc, out = runner(["schtasks", "/create", "/tn", TASK_NAME, "/xml", xml_path, "/f"], 60)
    finally:
        Path(xml_path).unlink(missing_ok=True)
    print(out)
    if rc == 0:
        print(f"task '{TASK_NAME}' runs every {every_minutes} min; log: {log_path or default_log_path()}")
    return 0 if rc == 0 else 1


def uninstall_windows_task(runner: Runner = run_command) -> int:
    if os.name != "nt":
        print("--uninstall-windows-task is for Windows hosts", file=sys.stderr)
        return 2
    rc, out = runner(["schtasks", "/delete", "/tn", TASK_NAME, "/f"], 60)
    print(out)
    return 0 if rc == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help=f"only containers whose name starts with this (default {DEFAULT_PREFIX!r}; '' for all)",
    )
    parser.add_argument("--log", type=Path, default=None, help="log file (default: per-platform state dir)")
    parser.add_argument("--dry-run", action="store_true", help="list candidates, start nothing")
    parser.add_argument(
        "--install-windows-task",
        action="store_true",
        help="register the Task Scheduler task on this Windows host and exit",
    )
    parser.add_argument("--uninstall-windows-task", action="store_true", help="remove that task and exit")
    parser.add_argument("--every", type=int, default=2, help="minutes between ticks for the installed task")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.install_windows_task:
        if args.every < 1:
            print("--every must be at least 1 minute", file=sys.stderr)
            return 2
        return install_windows_task(args.every, args.prefix, args.log)
    if args.uninstall_windows_task:
        return uninstall_windows_task()
    return tick(args.prefix, args.log or default_log_path(), dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
