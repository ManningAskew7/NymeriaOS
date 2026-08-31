"""Cover for terminating the child processes the API process owns (#303).

Making `/restart api` replace the process image in place kept the pid alive,
which removed the only thing that had ever cleaned these up: the container's
PID-namespace teardown, or systemd's cgroup kill. Both were consequences of the
process EXITING, i.e. of the bug #300 fixed. The clean-exit path has the same
hole with no supervisor to cover it, since children are spawned into their own
session and a terminal Ctrl-C never reaches them.

These use real child processes, because the whole subject is what happens to a
real pid. They are careful to own every process they reap.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time

import pytest
from fastapi import FastAPI

from nymeria.core.child_teardown import terminate_owned_children
from nymeria.triggers.api import _register_child_teardown_lifecycle
from nymeria.tools import bash_background
from nymeria.tools.bash_background import (
    BackgroundJobRecord,
    get_registry,
    kill_process_group,
    pid_alive,
    reset_registry_for_tests,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


def _spawn_sleeper() -> subprocess.Popen:
    """A child in its own session, the shape every background job is launched in."""
    return subprocess.Popen(  # env-gate: test-only sleeper, inherits nothing meaningful
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _register(proc: subprocess.Popen, job_id: str = "job-1") -> BackgroundJobRecord:
    record = BackgroundJobRecord(
        id=job_id,
        pid=proc.pid,
        command="sleep 30",
        working_directory="/tmp",
        started_at=time.time(),
        finished_at=None,
        exit_code=None,
        stdout_path="/tmp/nonexistent.stdout",
        stderr_path="/tmp/nonexistent.stderr",
        status="running",
        thread_id="thread-1",
        user_id="alice",
    )
    get_registry().register(record)
    return record


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_teardown_terminates_a_running_background_job():
    # The job that actually strands: it writes to temp FILES rather than pipes,
    # so replacing the process closes nothing it cares about. It would run to
    # completion with its registry and watcher thread gone, so no completion
    # prompt would ever fire.
    proc = _spawn_sleeper()
    try:
        _register(proc)

        summary = terminate_owned_children()

        assert summary["bash_jobs"] == 1
        assert _wait_gone(proc.pid), "the job's process group should be gone"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_teardown_leaves_no_zombie_behind():
    # Killing without reaping just trades a running orphan for a zombie, and
    # after an in-place restart the pid survives, so there is no init to inherit
    # it. `waitpid` is what makes the kill actually final.
    proc = _spawn_sleeper()
    _register(proc)

    terminate_owned_children()

    assert _wait_gone(proc.pid)
    # Already collected by the teardown's reap sweep, so our own wait finds
    # nothing left to collect.
    with pytest.raises(ChildProcessError):
        os.waitpid(proc.pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_teardown_skips_jobs_that_already_finished():
    proc = subprocess.Popen(  # env-gate: test-only, exits immediately
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    proc.wait()
    record = _register(proc)
    get_registry().mark_completed(record.id, 0)

    assert terminate_owned_children()["bash_jobs"] == 0


def test_teardown_never_raises_when_a_step_fails(monkeypatch: pytest.MonkeyPatch):
    # It runs past the point of no return on the restart path, so a failure in
    # any one step must not propagate. Both steps are broken here at once.
    def boom(*_args, **_kwargs):
        raise RuntimeError("registry is wedged")

    monkeypatch.setattr(bash_background, "terminate_running_jobs", boom)
    import nymeria.core.mcp_manager as mcp_manager

    monkeypatch.setattr(mcp_manager, "shutdown_mcp_manager", boom)

    summary = terminate_owned_children()

    assert summary["bash_jobs"] == 0


def test_teardown_shuts_down_the_mcp_manager(monkeypatch: pytest.MonkeyPatch):
    # `shutdown_mcp_manager` had NO production caller before this: it was not a
    # helper being bypassed, it was dead code, and MCP stdio servers were only
    # ever cleaned up by the OS.
    import nymeria.core.mcp_manager as mcp_manager

    called: list[bool] = []
    monkeypatch.setattr(
        mcp_manager, "shutdown_mcp_manager", lambda: called.append(True)
    )

    terminate_owned_children()

    assert called == [True]


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_a_completion_prompt_is_not_submitted_during_a_shutdown(
    monkeypatch: pytest.MonkeyPatch,
):
    # A watcher whose job dies under the teardown signal must not start an
    # autonomous turn into a process that is being replaced: it would write
    # checkpoints and publish events, then be cut off mid-stream by the exec.
    submitted: list[str] = []
    monkeypatch.setattr(
        bash_background,
        "_submit_completion_prompt",
        lambda record: submitted.append(record.id),
    )

    proc = _spawn_sleeper()
    record = _register(proc)
    bash_background.spawn_watcher(record, proc)
    time.sleep(0.1)  # let the watcher reach proc.wait()

    terminate_owned_children()

    assert _wait_gone(proc.pid)
    time.sleep(0.3)  # give the watcher time to wake and (not) submit
    assert submitted == []
    assert get_registry().get(record.id).status == "completed"


def test_the_api_shutdown_handler_runs_the_teardown_off_the_event_loop():
    # The clean-exit half of #303. The teardown is synchronous and waits on
    # process groups (up to a second per job, plus the MCP shutdown's own
    # timeouts), and unlike the restart path there is no exec about to replace
    # the loop: uvicorn is still draining, so it has to go to a thread.
    app = FastAPI()
    seen: list[str] = []
    import nymeria.core.child_teardown as teardown_mod

    def _record() -> dict:
        seen.append(threading.current_thread().name)
        return {"bash_jobs": 0, "reaped": 0}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(teardown_mod, "terminate_owned_children", _record)
        _register_child_teardown_lifecycle(app)
        asyncio.run(app.router.on_shutdown[-1]())

    assert len(seen) == 1
    assert seen[0] != threading.main_thread().name


def test_the_api_shutdown_handler_never_blocks_the_rest_of_shutdown(caplog):
    # Shutdown handlers run in sequence, so an exception here would skip
    # whatever uvicorn still has queued. Best-effort, logged, never raised.
    app = FastAPI()
    import nymeria.core.child_teardown as teardown_mod

    def boom() -> dict:
        raise RuntimeError("registry is wedged")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(teardown_mod, "terminate_owned_children", boom)
        _register_child_teardown_lifecycle(app)
        with caplog.at_level(logging.ERROR, logger="nymeria.triggers.api"):
            asyncio.run(app.router.on_shutdown[-1]())  # must not raise

    assert "Child-process teardown failed" in caplog.text


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_kill_process_group_reports_an_already_dead_pid():
    # The kill primitive is best-effort and pid-based, so it has to answer
    # honestly about a job that exited between the registry read and the signal.
    proc = subprocess.Popen(  # env-gate: test-only, exits immediately
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    proc.wait()

    assert kill_process_group(proc.pid) is False
