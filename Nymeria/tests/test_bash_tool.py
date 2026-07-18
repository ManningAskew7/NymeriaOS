"""Tests for the rewritten bash_execute foreground path, the hardline command
guard, output truncation/spill, env scrubbing, and the bash_job catalog tool."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, cast

import pytest
from langchain_core.runnables import RunnableConfig

from nymeria.core.thread_lock_manager import ThreadLockManager
from nymeria.tools import bash_background as bg
from nymeria.tools.bash import (
    MAX_FOREGROUND_TIMEOUT_SECONDS,
    _format_foreground_result,
    _OutputIngestor,
    _SPILL_THRESHOLD_BYTES,
    _TAIL_KEEP_BYTES,
    bash_execute,
)
from nymeria.tools.bash_job import bash_job
from nymeria.tools.command_guard import check_command_guard


class _Settings:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.nymeria_confine_file_to_workspace = False


def _config(thread_id: str = "thread-1", user_id: str = "user-1") -> RunnableConfig:
    return cast(
        RunnableConfig,
        {"configurable": {"thread_id": thread_id, "user_id": user_id}},
    )


def _project_root(tmp_path: Path, monkeypatch) -> Path:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "nymeria.tools.execution_environment.get_settings",
        lambda: _Settings(project),
    )
    return project


def _bash_func() -> Callable[..., str]:
    return cast(Callable[..., str], getattr(bash_execute, "func"))


def _job_func() -> Callable[..., str]:
    return cast(Callable[..., str], getattr(bash_job, "func"))


def _wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


def _pid_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def setup_function(_function) -> None:
    bg.reset_registry_for_tests()


def teardown_function(_function) -> None:
    bg.reset_registry_for_tests()


# --- hardline command guard -----------------------------------------------


BLOCKED_COMMANDS = [
    "rm -rf /",
    "rm -fr /*",
    "rm -rf ~",
    "rm -Rf $HOME",
    "rm -r -f /",
    "rm --recursive --force /",
    "sudo rm -rf /",
    "cd /tmp && rm -rf /",
    "rm -rf --no-preserve-root /anything",
    "sudo reboot",
    "reboot",
    "shutdown -h now",
    "nohup poweroff",
    "env FOO=1 halt",
    "init 0",
    "systemctl reboot",
    "sudo systemctl poweroff",
    "mkfs.ext4 /dev/sda1",
    "sudo mkfs -t ext4 /dev/sdb",
    "wipefs -a /dev/sda",
    "pkill -f nymeria",
    "killall nymeria",
    "systemctl stop nymeria-api",
    "docker stop nymeria-api",
    "docker kill nymeria-worker",
    ":(){ :|:& };:",
    "dd if=/dev/zero of=/dev/sda",
    "cat foo > /dev/nvme0n1",
    # Wrapper options that take a separate value must not hide the verb.
    "sudo -u root reboot",
    "sudo --user root shutdown -h now",
    "nice -n 5 reboot",
    "env -u PATH reboot",
    "timeout 30 reboot",
    # A shell -c payload is recursively checked (common model idiom).
    'bash -c "rm -rf /"',
    "sh -c 'reboot'",
    'sudo bash -c "reboot"',
    # Windows cmd catastrophes (bash_execute runs through cmd.exe there).
    "format c:",
    "format /fs:ntfs C:",
    "del /s /q C:\\",
    "del /s C:\\*",
    "rd /S /Q c:",
    "rmdir /s /q C:/",
    "rd /s /q %USERPROFILE%",
    "del /s %userprofile%\\*",
    "shutdown /s /t 0",
    # cmd /c payloads are the Windows twin of bash -c.
    'cmd /c "format c:"',
    "cmd.exe /c del /s /q C:\\",
]

ALLOWED_COMMANDS = [
    "ls -la && git status",
    "rm -rf /tmp/build",
    "rm -r node_modules",
    "rm -f /tmp/x.lock",
    "echo reboot the box",
    'echo "rm -rf /"',
    'git commit -m "reboot the box"',
    'git commit -m "fix; reboot loop"',
    'git commit -m "add mkfs support"',
    "grep reboot /var/log/syslog",
    "docker stop other-container",
    "systemctl status nymeria-api",
    'echo ":(){ :|:& };:"',
    "dd if=/dev/zero of=/tmp/img bs=1M count=10",
    "echo done > /dev/null",
    "man reboot",
    "timeout 30 sleep 60",
    "pkill -f my-script",
    # Names that merely START with a blocked verb are legitimate commands.
    "reboot-guard --status",
    "shutdown.sh",
    "mkfs-wrapper /tmp/x",
    "wipefs-helper --all",
    # Heredoc bodies are data, not commands.
    "cat > s.sh <<EOF\nreboot\nEOF",
    "cat <<'EOF'\ndd if=/dev/zero of=/dev/sda example\nEOF",
    # Shell -c payloads are checked recursively, so benign ones pass.
    'bash -c "echo reboot"',
    "bash deploy.sh",
    # Windows: recursive deletes below a drive root and benign lookalikes.
    "del /s /q build\\*",
    "del /s C:\\temp\\build",
    "rd /s /q node_modules",
    "rmdir /s /q C:\\Users\\me\\AppData\\Local\\Temp\\build",
    "rd /s /q %USERPROFILE%\\AppData\\Local\\Temp\\build",
    "del C:\\temp\\notes.txt",
    "format --help",
    "format-patch --help",
    'echo "del /s /q C:\\*"',
    "cmd /c echo hello",
]


@pytest.mark.parametrize("command", BLOCKED_COMMANDS)
def test_guard_blocks_catastrophic_commands(command):
    assert check_command_guard(command) is not None


@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_guard_allows_ordinary_commands(command):
    assert check_command_guard(command) is None


def test_blocked_command_never_reaches_popen(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)

    def _fail_popen(*args, **kwargs):
        raise AssertionError("Popen must not be called for a blocked command")

    monkeypatch.setattr("nymeria.tools.bash.subprocess.Popen", _fail_popen)

    result = _bash_func()("sudo reboot")

    assert result.startswith("[Blocked]")
    assert "was not run" in result


# --- foreground basics ------------------------------------------------------


def test_foreground_returns_output_without_exit_line_on_success(
    tmp_path, monkeypatch
):
    _project_root(tmp_path, monkeypatch)

    result = _bash_func()("echo hello-world")

    assert "hello-world" in result
    assert "[exit code:" not in result


def test_foreground_interleaves_stdout_and_stderr(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)

    result = _bash_func()("echo out-line; echo err-line 1>&2; echo after")

    assert "out-line" in result
    assert "err-line" in result
    assert "after" in result
    # No separate [stderr]: section anymore; streams are merged in order.
    assert "[stderr]" not in result


def test_foreground_reports_nonzero_exit_code(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)

    result = _bash_func()("exit 3")

    assert "[exit code: 3]" in result


def test_foreground_annotates_grep_no_match(tmp_path, monkeypatch):
    project = _project_root(tmp_path, monkeypatch)
    (project / "haystack.txt").write_text("nothing here\n", encoding="utf-8")

    result = _bash_func()(
        "grep zzz-not-there haystack.txt", working_directory=str(project)
    )

    assert "[exit code: 1 - no matches found (not an error)]" in result


def test_foreground_no_output_placeholder(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)

    result = _bash_func()("true")

    assert "[No output]" in result


def test_foreground_strips_ansi_and_control_chars(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)

    result = _bash_func()(
        "printf '\\033[31mred\\033[0m plain\\n'; printf 'a\\001b\\n'"
    )

    assert "red plain" in result
    assert "\x1b" not in result
    assert "a?b" in result


def test_working_directory_must_exist(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)

    result = _bash_func()("echo hi", working_directory=str(tmp_path / "missing"))

    assert "[Error]: Working directory does not exist" in result


# --- timeout and process-group teardown -------------------------------------


def test_timeout_kills_command_and_reports(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)

    start = time.monotonic()
    result = _bash_func()("sleep 30", timeout_seconds=1)
    elapsed = time.monotonic() - start

    assert "timed out after 1s" in result
    assert elapsed < 10


def test_timeout_kills_backgrounded_grandchild(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)

    start = time.monotonic()
    result = _bash_func()(
        "sleep 30 & echo GRANDPID=$!; wait", timeout_seconds=1
    )
    elapsed = time.monotonic() - start

    assert "timed out" in result
    assert elapsed < 10
    match = re.search(r"GRANDPID=(\d+)", result)
    assert match is not None
    _wait_until(lambda: _pid_dead(int(match.group(1))))


def test_grandchild_holding_pipe_does_not_hang_return(tmp_path, monkeypatch):
    """The classic 'server &' bug: a grandchild keeps the stdout pipe open after
    the shell exits. The tool must return promptly and reap the grandchild."""
    _project_root(tmp_path, monkeypatch)

    start = time.monotonic()
    result = _bash_func()(
        "sleep 30 & echo GRANDPID=$!; echo shell-done", timeout_seconds=60
    )
    elapsed = time.monotonic() - start

    assert "shell-done" in result
    assert elapsed < 10
    match = re.search(r"GRANDPID=(\d+)", result)
    assert match is not None
    _wait_until(lambda: _pid_dead(int(match.group(1))))


def test_oversized_timeout_is_rejected_not_run(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)

    def _fail_popen(*args, **kwargs):
        raise AssertionError("Popen must not be called for a rejected timeout")

    monkeypatch.setattr("nymeria.tools.bash.subprocess.Popen", _fail_popen)

    result = _bash_func()(
        "echo hi", timeout_seconds=MAX_FOREGROUND_TIMEOUT_SECONDS + 1
    )

    assert "[Error]" in result
    assert "run_in_background" in result


def test_foreground_cap_bounded_by_deployment_tool_timeout(tmp_path, monkeypatch):
    """SafeToolNode wraps every tool call in settings.tool_timeout; bash's own
    deadline must always fire first, so the cap is tool_timeout minus margin."""
    _project_root(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "nymeria.tools.bash.get_settings",
        lambda: SimpleNamespace(tool_timeout=300, bash_env_passthrough=""),
    )

    def _fail_popen(*args, **kwargs):
        raise AssertionError("Popen must not be called for a rejected timeout")

    monkeypatch.setattr("nymeria.tools.bash.subprocess.Popen", _fail_popen)

    result = _bash_func()("echo hi", timeout_seconds=400)

    assert "[Error]" in result
    assert "290s foreground limit" in result
    assert "run_in_background" in result


def test_timeout_clamped_to_minimum(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)

    start = time.monotonic()
    result = _bash_func()("sleep 30", timeout_seconds=0)
    elapsed = time.monotonic() - start

    assert "timed out after 1s" in result
    assert elapsed < 10


# --- abort integration -------------------------------------------------------


def test_abort_event_terminates_command(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)
    locks = ThreadLockManager()
    locks.signal_abort("thread-1")
    agent = SimpleNamespace(_thread_locks=locks)
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: agent)

    start = time.monotonic()
    result = _bash_func()("sleep 30", config=_config())
    elapsed = time.monotonic() - start

    assert "[Aborted]" in result
    assert elapsed < 10


# --- truncation and spill ----------------------------------------------------


def test_large_output_truncates_and_spills_to_thread_dir(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)
    cmd_dir = tmp_path / "cmds"
    cmd_dir.mkdir()
    monkeypatch.setattr(
        "nymeria.core.attachment_sandbox.get_thread_command_dir",
        lambda thread_id: cmd_dir,
    )

    result = _bash_func()("yes | head -c 200000", config=_config())

    assert "[Output truncated: 200000 bytes total" in result
    match = re.search(r"Full output saved to (.+?) \(read", result)
    assert match is not None
    spill = Path(match.group(1))
    assert spill.parent == cmd_dir
    assert spill.stat().st_size == 200000
    assert (spill.stat().st_mode & 0o777) == 0o600


def test_large_output_without_thread_falls_back_to_temp(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)
    swept: list[int] = []
    monkeypatch.setattr(
        "nymeria.tools.bash.sweep_stale_output_files",
        lambda *args, **kwargs: swept.append(1) or 0,
    )

    result = _bash_func()("yes | head -c 200000")

    match = re.search(r"Full output saved to (.+?) \(read", result)
    assert match is not None
    spill = Path(match.group(1))
    try:
        assert "nymeria-bash-" in spill.name
        assert spill.stat().st_size == 200000
        assert swept  # ownerless temp spills trigger the age sweep
    finally:
        spill.unlink(missing_ok=True)


def test_small_output_is_never_truncated(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)

    def _fail_dir(thread_id):
        raise AssertionError("small output must not open a spill file")

    monkeypatch.setattr(
        "nymeria.core.attachment_sandbox.get_thread_command_dir", _fail_dir
    )

    result = _bash_func()("echo small", config=_config())

    assert "small" in result
    assert "truncated" not in result


# --- _OutputIngestor unit tests ------------------------------------------------


def test_ingestor_small_output_stays_in_memory(tmp_path):
    def _fail_target():
        raise AssertionError("must not spill")

    ingestor = _OutputIngestor(_fail_target)
    ingestor.feed(b"hello ")
    ingestor.feed(b"world")
    ingestor.close()

    assert ingestor.full_head_bytes() == b"hello world"
    assert ingestor.spill_path is None
    assert ingestor.total_bytes == 11


def test_ingestor_spills_full_output_and_bounds_tail(tmp_path):
    spill = tmp_path / "spill.log"

    def _target():
        return str(spill), open(spill, "wb")

    ingestor = _OutputIngestor(_target)
    payload = b"x" * 1000
    for _ in range(300):  # 300KB total
        ingestor.feed(payload)
    ingestor.close()

    assert ingestor.total_bytes == 300000
    assert ingestor.spill_path == str(spill)
    assert ingestor.full_head_bytes() is None
    assert spill.stat().st_size == 300000
    assert len(ingestor.tail_bytes()) <= _TAIL_KEEP_BYTES


def test_ingestor_caps_spill_file_size(tmp_path, monkeypatch):
    monkeypatch.setattr("nymeria.tools.bash._SPILL_THRESHOLD_BYTES", 1000)
    monkeypatch.setattr("nymeria.tools.bash._SPILL_MAX_BYTES", 3000)
    spill = tmp_path / "spill.log"

    def _target():
        return str(spill), open(spill, "wb")

    ingestor = _OutputIngestor(_target)
    for _ in range(10):
        ingestor.feed(b"z" * 1000)
    ingestor.close()

    assert ingestor.total_bytes == 10000
    assert ingestor.spill_truncated is True
    assert spill.stat().st_size == 3000  # disk use is bounded
    assert len(ingestor.tail_bytes()) <= _TAIL_KEEP_BYTES  # tail still complete


def test_ingestor_ignores_feed_after_close(tmp_path):
    def _fail_target():
        raise AssertionError("must not spill")

    ingestor = _OutputIngestor(_fail_target)
    ingestor.feed(b"before")
    ingestor.close()
    ingestor.feed(b"after")

    assert ingestor.total_bytes == 6
    assert ingestor.tail_bytes() == b"before"


def test_ingestor_survives_spill_failure(tmp_path):
    ingestor = _OutputIngestor(lambda: None)
    payload = b"y" * 1000
    for _ in range(300):
        ingestor.feed(payload)
    ingestor.close()

    assert ingestor.total_bytes == 300000
    assert ingestor.spill_path is None
    assert ingestor.full_head_bytes() is None  # capped, not the full output
    assert len(ingestor.tail_bytes()) <= _TAIL_KEEP_BYTES
    assert len(ingestor._head) <= _SPILL_THRESHOLD_BYTES


def test_unknown_exit_code_is_reported_honestly(tmp_path):
    """When the final wait cannot read the process state, say 'unknown' rather
    than fabricating a signal from a -1 sentinel."""
    ingestor = _OutputIngestor(lambda: None)
    ingestor.feed(b"partial\n")
    ingestor.close()

    result = _format_foreground_result(
        "some-command", ingestor, None, False, False, 120
    )

    assert "partial" in result
    assert "[exit code: unknown" in result
    assert "signal" not in result


# --- environment scrubbing -----------------------------------------------------


def test_env_scrub_hides_process_secrets(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)
    monkeypatch.setenv("NYMERIA_TEST_SECRET", "sekrit-value")
    monkeypatch.setattr(
        "nymeria.tools.bash.get_settings",
        lambda: SimpleNamespace(bash_env_passthrough=""),
    )

    result = _bash_func()('echo "v=[$NYMERIA_TEST_SECRET]"')

    assert "v=[]" in result
    assert "sekrit-value" not in result


def test_env_passthrough_setting_exposes_named_vars(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)
    monkeypatch.setenv("NYMERIA_TEST_SECRET", "sekrit-value")
    monkeypatch.setattr(
        "nymeria.tools.bash.get_settings",
        lambda: SimpleNamespace(bash_env_passthrough="NYMERIA_TEST_SECRET"),
    )

    result = _bash_func()('echo "v=[$NYMERIA_TEST_SECRET]"')

    assert "v=[sekrit-value]" in result


def test_env_scrub_keeps_path(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "nymeria.tools.bash.get_settings",
        lambda: SimpleNamespace(bash_env_passthrough=""),
    )

    result = _bash_func()('test -n "$PATH" && echo path-present')

    assert "path-present" in result


# --- temp-file sweep -------------------------------------------------------------


def test_sweep_removes_only_old_nymeria_files(tmp_path, monkeypatch):
    monkeypatch.setattr(bg.tempfile, "gettempdir", lambda: str(tmp_path))
    old_time = time.time() - 8 * 24 * 3600
    old_bg = tmp_path / "nymeria-bg-old.stdout"
    old_spill = tmp_path / "nymeria-bash-old.log"
    fresh = tmp_path / "nymeria-bg-new.stdout"
    unrelated = tmp_path / "other-old.log"
    for path in (old_bg, old_spill, fresh, unrelated):
        path.write_text("x", encoding="utf-8")
    os.utime(old_bg, (old_time, old_time))
    os.utime(old_spill, (old_time, old_time))
    os.utime(unrelated, (old_time, old_time))

    removed = bg.sweep_stale_output_files()

    assert removed == 2
    assert not old_bg.exists()
    assert not old_spill.exists()
    assert fresh.exists()
    assert unrelated.exists()


# --- bash_job catalog tool --------------------------------------------------------


def _fake_record(
    tmp_path: Path,
    *,
    job_id: str = "job123",
    user_id: str = "user-1",
    status: str = "completed",
    exit_code: int | None = 0,
) -> bg.BackgroundJobRecord:
    stdout_path = tmp_path / f"{job_id}.stdout"
    stderr_path = tmp_path / f"{job_id}.stderr"
    stdout_path.write_text("hello-out\n", encoding="utf-8")
    stderr_path.write_text("hello-err\n", encoding="utf-8")
    return bg.BackgroundJobRecord(
        id=job_id,
        pid=99999,
        command="echo hello",
        working_directory=str(tmp_path),
        started_at=time.time() - 2,
        finished_at=time.time() if status == "completed" else None,
        exit_code=exit_code if status == "completed" else None,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        status=status,
        thread_id="thread-1",
        user_id=user_id,
    )


def test_bash_job_requires_user_context():
    result = _job_func()("list")

    assert "[Error]" in result
    assert "user context" in result


def test_bash_job_list_empty():
    result = _job_func()("list", config=_config())

    assert "No background jobs" in result


def test_bash_job_list_is_user_scoped(tmp_path):
    get = bg.get_registry()
    get.register(_fake_record(tmp_path, job_id="aaa111", user_id="user-1"))
    get.register(_fake_record(tmp_path, job_id="bbb222", user_id="user-2"))

    mine = _job_func()("list", config=_config(user_id="user-1"))
    theirs = _job_func()("list", config=_config(user_id="user-2"))

    assert "aaa111" in mine and "bbb222" not in mine
    assert "bbb222" in theirs and "aaa111" not in theirs


def test_bash_job_status_shows_fields(tmp_path):
    record = _fake_record(tmp_path)
    bg.get_registry().register(record)

    result = _job_func()("status", job_id="job123", config=_config())

    assert "status: completed" in result
    assert "exit_code: 0" in result
    assert record.stdout_path in result
    assert record.stderr_path in result


def test_bash_job_log_shows_output_tails(tmp_path):
    bg.get_registry().register(_fake_record(tmp_path))

    result = _job_func()("log", job_id="job123", config=_config())

    assert "hello-out" in result
    assert "hello-err" in result


def test_bash_job_actions_reject_other_users_jobs(tmp_path):
    bg.get_registry().register(_fake_record(tmp_path, user_id="user-1"))

    for action in ("status", "log", "kill"):
        result = _job_func()(
            action, job_id="job123", config=_config(user_id="user-2")
        )
        assert "found for you" in result


def test_bash_job_requires_job_id_for_targeted_actions():
    for action in ("status", "log", "kill"):
        result = _job_func()(action, config=_config())
        assert "requires a job_id" in result


def test_bash_job_unknown_action():
    result = _job_func()("dance", config=_config())

    assert "Unknown action" in result


def test_bash_job_kill_completed_job_is_noop(tmp_path):
    bg.get_registry().register(_fake_record(tmp_path, status="completed"))

    result = _job_func()("kill", job_id="job123", config=_config())

    assert "already completed" in result


def test_bash_job_kill_terminates_running_job(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)
    submitted: list[bg.BackgroundJobRecord] = []
    monkeypatch.setattr(bg, "_submit_completion_prompt", submitted.append)

    launch = _bash_func()("sleep 30", run_in_background=True, config=_config())
    match = re.search(r"job_id: ([0-9a-f]+)", launch)
    assert match is not None
    job_id = match.group(1)
    record = bg.get_registry().get(job_id)
    assert record is not None

    try:
        result = _job_func()("kill", job_id=job_id, config=_config())

        assert "Sent termination" in result
        _wait_until(
            lambda: (bg.get_registry().get(job_id) or record).status == "completed"
        )
        _wait_until(lambda: _pid_dead(record.pid))
        assert submitted  # watcher fired the completion path after the kill
    finally:
        bg.cleanup_output_files(record.stdout_path, record.stderr_path)


def test_background_launch_mentions_bash_job(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)
    monkeypatch.setattr(bg, "_submit_completion_prompt", lambda record: None)

    result = _bash_func()("true", run_in_background=True, config=_config())

    assert "bash_job" in result
    match = re.search(r"job_id: ([0-9a-f]+)", result)
    assert match is not None
    record = bg.get_registry().get(match.group(1))
    assert record is not None
    bg.cleanup_output_files(record.stdout_path, record.stderr_path)
