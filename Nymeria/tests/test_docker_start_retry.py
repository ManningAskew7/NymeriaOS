"""Behavior tests for the mount-failed start retry tick (`scripts/docker_start_retry.py`).

Plan behaviours 1-12 in ``tmp/docker-mount-retry-plan.md``. The script starts
LIVE containers, so the assertions are about which `docker start` commands
ran, what the log says afterwards, and the exit code; the docker seam is an
injected fake and nothing here reaches a daemon.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "docker_start_retry.py"

spec = importlib.util.spec_from_file_location("docker_start_retry", SCRIPT)
assert spec is not None and spec.loader is not None
dsr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dsr)

MOUNT_ERR = (
    "error while mounting volume '/var/lib/docker/volumes/nymeria_nas_fast_automation/_data': "
    "failed to mount local volume: mount //192.168.2.10/fast automation:/var/lib/docker/"
    "volumes/nymeria_nas_fast_automation/_data, flags: 0x1, data: addr=192.168.2.10,"
    "username=nymeria.beta,password=********,vers=3.1.1: interrupted system call"
)


def record(
    name: str,
    *,
    running: bool = False,
    policy: str = "unless-stopped",
    error: str = MOUNT_ERR,
    status: str = "exited",
) -> dict:
    """The slice of `docker inspect` the script reads, as the daemon shapes it."""
    return {
        "Id": f"id-{name}",
        "Name": f"/{name}",
        "State": {"Status": status, "Running": running, "ExitCode": 255, "Error": error},
        "HostConfig": {"RestartPolicy": {"Name": policy, "MaximumRetryCount": 0}},
    }


class FakeDocker:
    """`docker ps -aq` lists the stopped records; `docker start` consults
    `start_rc` per name (a callable for timeouts) and flips the record running."""

    def __init__(self, records: list[dict], *, daemon_up: bool = True):
        self.records = records
        self.daemon_up = daemon_up
        self.start_rc: dict[str, int | Exception] = {}
        # Ids `ps` lists that `inspect` no longer finds (removed in between).
        self.vanished_ids: list[str] = []
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv, timeout):
        self.calls.append(tuple(argv))
        verb = argv[1]
        if not self.daemon_up:
            return 1, "error during connect: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified."
        if verb == "ps":
            ids = [r["Id"] for r in self.records if not r["State"]["Running"]]
            return 0, "\n".join(ids + self.vanished_ids)
        if verb == "inspect":
            # Real shape: `--format {{json .}}` prints one object per line on
            # stdout; an id that vanished between ps and inspect adds an
            # `Error: No such object` line on stderr (folded in by the runner)
            # and a non-zero exit.
            assert argv[2:4] == ["--format", "{{json .}}"]
            wanted = argv[4:]
            known = {r["Id"]: r for r in self.records}
            lines = [json.dumps(known[i]) for i in wanted if i in known]
            missing = [i for i in wanted if i not in known]
            out = "\n".join(lines + [f"Error: No such object: {i}" for i in missing])
            return (1 if missing else 0), out
        if verb == "start":
            name = argv[2]
            outcome = self.start_rc.get(name, 0)
            if isinstance(outcome, Exception):
                return -1, f"timeout after {timeout}s"
            if outcome != 0:
                return outcome, f"Error response from daemon: {MOUNT_ERR}\nError: failed to start containers: {name}"
            for r in self.records:
                if r["Name"] == f"/{name}":
                    r["State"]["Running"] = True
                    r["State"]["Error"] = ""
            return 0, name
        raise AssertionError(f"unexpected docker verb {verb}")

    def started(self) -> list[str]:
        return [c[2] for c in self.calls if c[1] == "start"]


def run_tick(fake: FakeDocker, tmp_path: Path, **kw) -> tuple[int, str]:
    log = tmp_path / "retry.log"
    rc = dsr.tick(kw.pop("prefix", "nymeria-"), log, runner=fake, docker="docker", **kw)
    return rc, log.read_text(encoding="utf-8") if log.exists() else ""


# B1: the signature is retried and the log records attempt + outcome.
def test_mount_failed_unless_stopped_container_is_started_and_logged(tmp_path):
    fake = FakeDocker([record("nymeria-api")])
    rc, log = run_tick(fake, tmp_path)
    assert fake.started() == ["nymeria-api"]
    assert rc == 0
    assert "nymeria-api: start after 'error while mounting volume" in log
    assert log.rstrip().endswith("-> ok")
    assert fake.records[0]["State"]["Running"] is True


def test_always_policy_is_retried_too(tmp_path):
    fake = FakeDocker([record("nymeria-api", policy="always", status="created")])
    rc, _ = run_tick(fake, tmp_path)
    assert fake.started() == ["nymeria-api"] and rc == 0


# B2: a deliberate `docker stop` leaves State.Error empty and is respected.
def test_operator_stopped_container_is_left_alone_and_log_stays_absent(tmp_path):
    fake = FakeDocker([record("nymeria-api", error="")])
    rc, log = run_tick(fake, tmp_path)
    assert fake.started() == []
    assert rc == 0
    assert log == ""


# B3: no restart policy means Docker never meant to keep it up.
def test_restart_policy_no_is_not_retried(tmp_path):
    fake = FakeDocker([record("nymeria-testrun", policy="no")])
    rc, _ = run_tick(fake, tmp_path)
    assert fake.started() == [] and rc == 0


# B4: only the mount signature, not any start failure.
def test_other_start_errors_are_not_retried(tmp_path):
    fake = FakeDocker(
        [record("nymeria-api", error="driver failed programming external connectivity: port is already allocated")]
    )
    rc, log = run_tick(fake, tmp_path)
    assert fake.started() == [] and rc == 0 and log == ""


def test_running_container_is_not_a_candidate_even_with_error_text():
    # The ps-then-inspect race: a container listed as stopped that came up
    # (a hand `docker start`) before inspect ran must not be started again.
    assert dsr.wants_retry(record("nymeria-api", running=True, status="running"), "nymeria-") is False
    assert dsr.wants_retry(record("nymeria-api"), "nymeria-") is True


def test_container_removed_between_ps_and_inspect_does_not_hide_the_survivors(tmp_path):
    fake = FakeDocker([record("nymeria-api")])
    fake.vanished_ids = ["id-gone"]
    rc, log = run_tick(fake, tmp_path)
    assert fake.started() == ["nymeria-api"]
    assert rc == 0 and log.rstrip().endswith("-> ok")


def test_cli_warning_lines_around_inspect_output_are_ignored(tmp_path):
    class Chatty(FakeDocker):
        def __call__(self, argv, timeout):
            rc, out = super().__call__(argv, timeout)
            if argv[1] == "inspect":
                out = "WARNING: DOCKER_INSECURE_NO_IPTABLES_RAW is set\n" + out
            return rc, out

    fake = Chatty([record("nymeria-api")])
    rc, _ = run_tick(fake, tmp_path)
    assert fake.started() == ["nymeria-api"] and rc == 0


# B12: the name prefix scopes the watchdog to this project's containers.
def test_prefix_scopes_to_matching_names_only(tmp_path):
    fake = FakeDocker([record("parts-app-1"), record("nymeria-api")])
    rc, _ = run_tick(fake, tmp_path)
    assert fake.started() == ["nymeria-api"] and rc == 0


def test_empty_prefix_covers_every_container(tmp_path):
    fake = FakeDocker([record("parts-app-1"), record("nymeria-api")])
    rc, _ = run_tick(fake, tmp_path, prefix="")
    assert sorted(fake.started()) == ["nymeria-api", "parts-app-1"] and rc == 0


# B5: one failure does not stop the others; exit 1; each outcome logged.
def test_failure_on_one_candidate_still_attempts_the_rest_and_exits_1(tmp_path):
    fake = FakeDocker([record("nymeria-api"), record("nymeria-mcp")])
    fake.start_rc["nymeria-api"] = 1
    rc, log = run_tick(fake, tmp_path)
    assert fake.started() == ["nymeria-api", "nymeria-mcp"]
    assert rc == 1
    lines = log.rstrip().splitlines()
    assert len(lines) == 2
    assert "nymeria-api:" in lines[0] and "-> failed rc=1: Error response from daemon: error while mounting" in lines[0]
    assert "nymeria-mcp:" in lines[1] and lines[1].endswith("-> ok")


# B9: a hung `docker start` is recorded as a timeout and the tick moves on.
def test_start_timeout_is_recorded_and_others_continue(tmp_path):
    fake = FakeDocker([record("nymeria-api"), record("nymeria-mcp")])
    fake.start_rc["nymeria-api"] = subprocess.TimeoutExpired("docker", 120)
    rc, log = run_tick(fake, tmp_path)
    assert rc == 1
    assert fake.started() == ["nymeria-api", "nymeria-mcp"]
    assert "nymeria-api: start after" in log and "failed rc=-1: timeout after 120s" in log


# B6: daemon down => exit 2, nothing written (Docker Desktop closed is normal).
def test_daemon_unreachable_exits_2_and_writes_nothing(tmp_path, capsys):
    fake = FakeDocker([record("nymeria-api")], daemon_up=False)
    rc, log = run_tick(fake, tmp_path)
    assert rc == 2 and log == "" and fake.started() == []
    assert "unreachable" in capsys.readouterr().out


def test_nothing_stopped_means_no_inspect_and_no_log(tmp_path):
    fake = FakeDocker([record("nymeria-api", running=True)])
    rc, log = run_tick(fake, tmp_path)
    assert rc == 0 and log == ""
    assert [c[1] for c in fake.calls] == ["ps"]


# B7: dry run reports and starts nothing.
def test_dry_run_lists_candidates_without_starting(tmp_path, capsys):
    fake = FakeDocker([record("nymeria-api")])
    rc, log = run_tick(fake, tmp_path, dry_run=True)
    assert rc == 0 and fake.started() == [] and log == ""
    assert "would start nymeria-api: error while mounting volume" in capsys.readouterr().out


# B8: the log rotates past the cap instead of growing forever.
def test_log_rotates_past_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(dsr, "LOG_CAP_BYTES", 200)
    log = tmp_path / "retry.log"
    log.write_text("x" * 250, encoding="utf-8")
    dsr.log_line(log, "nymeria-api: start -> ok")
    assert (tmp_path / "retry.log.1").read_text(encoding="utf-8") == "x" * 250
    assert log.read_text(encoding="utf-8").endswith("nymeria-api: start -> ok\n")
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


def test_log_below_cap_is_appended_not_rotated(tmp_path):
    log = tmp_path / "retry.log"
    dsr.log_line(log, "first")
    dsr.log_line(log, "second")
    assert not (tmp_path / "retry.log.1").exists()
    assert [line.split(" ", 1)[1] for line in log.read_text(encoding="utf-8").splitlines()] == ["first", "second"]


# B10: docker children must not open a console under the hidden task.
def test_run_command_requests_no_console_window(monkeypatch):
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)

        class Proc:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Proc()

    monkeypatch.setattr(dsr, "_NO_WINDOW", 0x08000000)
    monkeypatch.setattr(dsr.subprocess, "run", fake_run)
    assert dsr.run_command(["docker", "ps"], 5) == (0, "ok")
    assert seen["creationflags"] == 0x08000000
    assert seen["timeout"] == 5


def test_run_command_maps_timeout_to_rc_minus_one(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(dsr.subprocess, "run", fake_run)
    assert dsr.run_command(["docker", "start", "x"], 7) == (-1, "timeout after 7s")


# B11: the Task Scheduler definition carries the settings the CLI cannot set.
def test_windows_task_xml_encodes_schedule_host_and_conditions(tmp_path):
    xml = dsr.windows_task_xml(
        user_id="LENOVO\\Project Updates",
        pythonw=Path(r"C:\py\pythonw.exe"),
        script=Path(r"C:\NymeriaOS\scripts\docker_start_retry.py"),
        every_minutes=2,
        prefix="nymeria-",
        log_path=Path(r"C:\Users\Me\AppData\Local\nymeria\retry.log"),
        start=datetime(2026, 9, 4, 14, 5, 9, 123456),
    )
    assert "<Interval>PT2M</Interval>" in xml
    assert "<StartBoundary>2026-09-04T14:05:09</StartBoundary>" in xml
    assert "<Command>C:\\py\\pythonw.exe</Command>" in xml
    assert (
        "<Arguments>&quot;C:\\NymeriaOS\\scripts\\docker_start_retry.py&quot; --prefix &quot;nymeria-&quot; "
        "--log &quot;C:\\Users\\Me\\AppData\\Local\\nymeria\\retry.log&quot;</Arguments>"
    ) in xml
    assert "<UserId>LENOVO\\Project Updates</UserId>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>" in xml
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<Hidden>true</Hidden>" in xml


def test_windows_task_xml_without_log_path_omits_the_flag():
    xml = dsr.windows_task_xml(
        user_id="U",
        pythonw=Path("pythonw.exe"),
        script=Path("s.py"),
        every_minutes=5,
        prefix="nymeria-",
        log_path=None,
        start=datetime(2026, 1, 1),
    )
    assert "--log" not in xml and "<Interval>PT5M</Interval>" in xml


def test_windows_task_xml_quotes_an_empty_prefix_so_argparse_still_gets_a_value():
    xml = dsr.windows_task_xml(
        user_id="U",
        pythonw=Path("pythonw.exe"),
        script=Path("s.py"),
        every_minutes=2,
        prefix="",
        log_path=None,
        start=datetime(2026, 1, 1),
    )
    assert "--prefix &quot;&quot;</Arguments>" in xml
    # And the argument line round-trips through the CLI parser to the empty prefix.
    assert dsr.build_parser().parse_args(["--prefix", ""]).prefix == ""


def test_install_refuses_off_windows(monkeypatch, capsys):
    monkeypatch.setattr(dsr.os, "name", "posix")
    calls = []
    assert dsr.install_windows_task(2, "nymeria-", None, runner=lambda a, t: calls.append(a) or (0, "")) == 2
    assert calls == []
    assert "Windows" in capsys.readouterr().err


WHOAMI_CSV = '"LENOVO-SAP-PC\\project updates","S-1-5-21-3576432237-546580322-3218964884-1004"\n'


def test_windows_user_sid_parses_whoami_csv():
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        return 0, WHOAMI_CSV

    assert dsr.windows_user_sid(runner) == "S-1-5-21-3576432237-546580322-3218964884-1004"
    assert calls[0][0].lower().endswith("whoami.exe") and calls[0][1:] == ["/user", "/fo", "csv", "/nh"]


def test_windows_user_sid_falls_back_to_machine_qualified_name(monkeypatch):
    monkeypatch.setenv("COMPUTERNAME", "LENOVO-SAP-PC")
    monkeypatch.setenv("USERNAME", "project updates")
    assert dsr.windows_user_sid(lambda a, t: (1, "")) == "LENOVO-SAP-PC\\project updates"


@pytest.mark.skipif(dsr.os.name != "nt", reason="schtasks argv only built on Windows")
def test_install_registers_via_schtasks_xml(monkeypatch, tmp_path):
    monkeypatch.setattr(dsr, "windows_pythonw", lambda: tmp_path / "pythonw.exe")
    seen: dict = {}

    def runner(argv, timeout):
        if argv[0].lower().endswith("whoami.exe"):
            return 0, WHOAMI_CSV
        seen["argv"] = list(argv)
        seen["xml"] = Path(argv[argv.index("/xml") + 1]).read_text(encoding="utf-16")
        return 0, "SUCCESS: The scheduled task has successfully been created."

    monkeypatch.chdir(tmp_path)
    assert dsr.install_windows_task(3, "nymeria-", Path("relative.log"), runner=runner) == 0
    assert seen["argv"][:4] == ["schtasks", "/create", "/tn", dsr.TASK_NAME]
    assert "/f" in seen["argv"]
    assert "<Interval>PT3M</Interval>" in seen["xml"]
    assert "<UserId>S-1-5-21-3576432237-546580322-3218964884-1004</UserId>" in seen["xml"]
    assert str(SCRIPT) in seen["xml"]
    # A relative --log is pinned to an absolute path: the task runs with no
    # working directory of its own.
    assert f"--log &quot;{tmp_path / 'relative.log'}&quot;" in seen["xml"]
    # The temp XML is cleaned up after registration.
    assert not Path(seen["argv"][seen["argv"].index("/xml") + 1]).exists()


def test_main_rejects_zero_interval(capsys):
    assert dsr.main(["--install-windows-task", "--every", "0"]) == 2
    assert "at least 1" in capsys.readouterr().err
