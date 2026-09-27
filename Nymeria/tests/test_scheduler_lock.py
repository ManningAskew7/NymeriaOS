"""#397: one scheduler per data directory, enforced across processes.

A second process on a data dir used to start its own ticker (the fat CLI's
local transport beside a running slim instance): its startup recovery deleted
the live ticker's execution markers, then both polled the same schedule and
fired the same triggers. The ticker now claims an OS lock on the data dir
before it touches the schedule and holds it until its process exits; a
second ticker stands by, and takes over when the owner exits only if its
construction site allows it (the services do, the fat CLI does not).

``Ticker._release_schedule`` stands in for the owner's process exiting.
"""

from __future__ import annotations

import errno
import json
import os
import time
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from nymeria.core import scheduler_lock as scheduler_lock_module
from nymeria.core.scheduler_lock import (
    SCHEDULER_LOCK_FILENAME,
    LockOutcome,
    ScheduleLock,
)
from nymeria.core.scheduler_state import SchedulerStateManager
from nymeria.core.ticker import Ticker
from nymeria.core.todo_manager import TodoManager
from nymeria.core.todo_schedule_db import TodoScheduleDB
from nymeria.core.turn_executor import LocalAgentExecutor
from nymeria.core.user_profile import UserProfileManager

USER = "owner"
BACKEND_ROOT = Path(__file__).resolve().parents[1]


class FakeThreadConfigManager:
    def get_config(self, thread_id: str):
        return None


class FakeSettings:
    context_management = "none"
    sliding_window_cycles = 5
    max_results = 10
    notification_level = "none"
    max_concurrent_autonomous = 1

    def __init__(self, data_dir: Path, **overrides: Any):
        self.data_dir = data_dir
        self.scheduler_missed_work_policy = "run"
        for key, value in overrides.items():
            setattr(self, key, value)


class FakeAgent:
    """One process's view of a data dir: its own stores and schedule handle."""

    def __init__(self, data_dir: Path, **settings: Any):
        self.todo_manager = TodoManager(data_dir)
        self.thread_config_manager = FakeThreadConfigManager()
        self.profile_manager = UserProfileManager(data_dir)
        self.settings = FakeSettings(data_dir, **settings)
        self._schedule_db = TodoScheduleDB(data_dir / "todo_schedule.db")

    def sync_agent_tools(self):
        pass


def _ticker(
    data_dir: Path, *, take_over: bool = True, **settings: Any
) -> tuple[Ticker, FakeAgent]:
    agent = FakeAgent(data_dir, **settings)
    fake: Any = agent
    ticker = Ticker(
        executor=LocalAgentExecutor(fake),
        settings=cast(Any, agent.settings),
        schedule_db=agent._schedule_db,
        todo_manager=agent.todo_manager,
        thread_config_manager=cast(Any, agent.thread_config_manager),
        profile_manager=agent.profile_manager,
        busy_agent=fake,
        spawn_sweeper=None,
        take_over=take_over,
    )
    return ticker, agent


@pytest.fixture
def tickers():
    """Build tickers and release every lock they took, pass or fail."""
    built: list[Ticker] = []

    def build(
        data_dir: Path, *, take_over: bool = True, **settings: Any
    ) -> tuple[Ticker, FakeAgent]:
        ticker, agent = _ticker(data_dir, take_over=take_over, **settings)
        built.append(ticker)
        return ticker, agent

    yield build
    for ticker in built:
        ticker.stop()
        ticker._running = False
        ticker._release_schedule()


def _due_todo(agent: FakeAgent, *, minutes_ago: int = 5):
    with agent.todo_manager.atomic_update(USER) as todo_list:
        todo = todo_list.add_item(
            "Water the plants",
            scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
            thread_id="thread-1",
            created_by="user",
        )
    assert todo is not None
    agent.todo_manager.sync_schedule_to_db(USER, todo.id, agent._schedule_db)
    return todo


class _PollRecorder:
    """Stands in for every unit of scheduling work one poll can start."""

    NAMES = (
        "_check_and_execute",
        "_maybe_submit_archive",
        "_maybe_submit_trigger_poll",
        "_maybe_submit_spawn_sweep",
        "_maybe_submit_dream_sweep",
        "_maybe_submit_watchdog_sweep",
    )

    def __init__(self, ticker: Ticker, monkeypatch):
        self.calls: list[str] = []
        for name in self.NAMES:
            monkeypatch.setattr(
                ticker, name, (lambda n: lambda *a, **k: self.calls.append(n))(name)
            )


def _poll(ticker: Ticker) -> None:
    ticker._running = True
    ticker._poll_once()


# ------------------------------------------------------------------
# L2: the first ticker on a free data dir behaves as before
# ------------------------------------------------------------------


def test_the_first_ticker_owns_the_schedule_and_runs_recovery(tmp_path, tickers):
    owner, agent = tickers(tmp_path)
    todo = _due_todo(agent)
    assert agent._schedule_db.mark_execution_started(todo.id, USER, "thread-1")

    status = owner.prepare_startup_recovery()

    assert status["owns_schedule"] is True
    assert status["schedule_held_by"] is None
    assert status["startup_execution_markers_cleared"] == 1
    assert status["startup_missed_count"] == 1
    assert (tmp_path / SCHEDULER_LOCK_FILENAME).exists()


# ------------------------------------------------------------------
# L1 + L6 + L7: a second ticker on a held schedule stands by
# ------------------------------------------------------------------


def test_a_second_ticker_touches_none_of_the_owners_state(tmp_path, tickers):
    owner, owner_agent = tickers(tmp_path, scheduler_missed_work_policy="ask")
    todo = _due_todo(owner_agent)
    # Built before the owner decided its hold: its in-memory copy is empty.
    second, second_agent = tickers(tmp_path)  # policy "run": would release
    owner.prepare_startup_recovery()
    # The owner's run is in flight: its marker is live.
    assert owner_agent._schedule_db.mark_execution_started(todo.id, USER, "thread-1")
    state_before = SchedulerStateManager(tmp_path).load()
    assert state_before["pending_missed_todo_ids"] == [todo.id]

    status = second.prepare_startup_recovery()

    assert status["owns_schedule"] is False
    assert status["startup_execution_markers_cleared"] == 0
    assert second_agent._schedule_db.count_active_executions() == 1
    # Neither the start stamp nor the owner's held missed work moved.
    assert SchedulerStateManager(tmp_path).load() == state_before
    # The standby reads the hold from the shared file, not its own policy.
    assert status["pending_missed_todo_ids"] == [todo.id]
    assert status["trigger_catchup_paused"] is True


@pytest.mark.parametrize("take_over", [True, False])
def test_a_standby_poll_runs_no_scheduling_work(
    tmp_path, tickers, monkeypatch, take_over
):
    owner, _ = tickers(tmp_path)
    owner.prepare_startup_recovery()
    second, _ = tickers(tmp_path, take_over=take_over)
    second.prepare_startup_recovery()
    recorder = _PollRecorder(second, monkeypatch)

    for _ in range(3):
        _poll(second)

    assert recorder.calls == []
    assert second.owns_schedule is False


def test_the_standby_names_the_holder_once(tmp_path, tickers, monkeypatch, caplog):
    owner, _ = tickers(tmp_path)
    owner.prepare_startup_recovery()
    notices: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        Ticker,
        "_print_standby_notice",
        staticmethod(lambda holder, takes_over: notices.append((holder, takes_over))),
    )
    second, _ = tickers(tmp_path)

    with caplog.at_level("WARNING", logger="nymeria.core.ticker"):
        status = second.prepare_startup_recovery()
        second.prepare_startup_recovery()  # start() asks again
        for _ in range(3):
            _poll(second)

    holder = f"pid {os.getpid()}"
    assert len(notices) == 1 and holder in notices[0][0]
    assert notices[0][1] is True
    assert holder in (status["schedule_held_by"] or "")
    standby_lines = [r for r in caplog.records if "Scheduler on standby" in r.getMessage()]
    assert len(standby_lines) == 1
    assert holder in standby_lines[0].getMessage()
    assert "then takes over" in standby_lines[0].getMessage()


def test_a_standby_release_of_missed_work_leaves_the_owners_hold(tmp_path, tickers):
    owner, owner_agent = tickers(tmp_path, scheduler_missed_work_policy="ask")
    todo = _due_todo(owner_agent)
    owner.prepare_startup_recovery()
    second, _ = tickers(tmp_path)
    second.prepare_startup_recovery()

    status = second.release_missed_work()

    assert status["released_todo_ids"] == []
    assert SchedulerStateManager(tmp_path).load()["pending_missed_todo_ids"] == [todo.id]
    assert owner._pending_startup_missed_ids == {todo.id}


def test_stopping_a_standby_does_not_stamp_the_owners_shutdown(tmp_path, tickers):
    owner, _ = tickers(tmp_path)
    owner.prepare_startup_recovery()
    second, _ = tickers(tmp_path)
    second.start()
    try:
        assert second.owns_schedule is False
    finally:
        second.stop()

    assert SchedulerStateManager(tmp_path).load().get("last_clean_shutdown_at") is None
    assert owner.owns_schedule is True


# ------------------------------------------------------------------
# L3 + L4: the owner's process exits, a service standby takes over
# ------------------------------------------------------------------


def test_the_standby_takes_over_when_the_owner_process_exits(
    tmp_path, tickers, monkeypatch, caplog
):
    owner, owner_agent = tickers(tmp_path)
    todo = _due_todo(owner_agent)
    owner.prepare_startup_recovery()
    second, second_agent = tickers(tmp_path)
    second.prepare_startup_recovery()
    # The owner's run was in flight when its process died.
    assert owner_agent._schedule_db.mark_execution_started(todo.id, USER, "thread-1")
    recorder = _PollRecorder(second, monkeypatch)

    owner._release_schedule()  # the owner's process exits
    with caplog.at_level("INFO", logger="nymeria.core.ticker"):
        _poll(second)

    assert second.owns_schedule is True
    # Recovery ran at the takeover: the orphaned marker is gone.
    assert second_agent._schedule_db.count_active_executions() == 0
    assert "_check_and_execute" in recorder.calls
    assert any("Scheduler takeover" in r.getMessage() for r in caplog.records)
    assert second.get_scheduler_status()["schedule_held_by"] is None

    # The recovery ran ONCE: a run the new owner starts survives its next poll.
    assert second_agent._schedule_db.mark_execution_started(todo.id, USER, "thread-1")
    _poll(second)
    assert second_agent._schedule_db.count_active_executions() == 1


def test_stop_keeps_the_schedule_until_the_process_exits(
    tmp_path, tickers, monkeypatch
):
    """stop() leaves in-flight runs going (the pool shuts down without
    waiting), so handing the lock over then would let the standby clear
    their markers and fire a still-due TODO a second time."""
    owner, owner_agent = tickers(tmp_path)
    todo = _due_todo(owner_agent)
    owner.prepare_startup_recovery()
    owner._running = True  # started: stop() takes its full shutdown path
    second, second_agent = tickers(tmp_path)
    second.prepare_startup_recovery()
    recorder = _PollRecorder(second, monkeypatch)
    # A run still going when its service is told to stop.
    assert owner_agent._schedule_db.mark_execution_started(todo.id, USER, "thread-1")

    owner.stop()
    _poll(second)

    assert second.owns_schedule is False
    assert recorder.calls == []
    assert second_agent._schedule_db.count_active_executions() == 1
    assert SchedulerStateManager(tmp_path).load().get("last_clean_shutdown_at")

    owner._release_schedule()  # now the process exits
    _poll(second)
    assert second.owns_schedule is True


def test_a_stopped_owner_started_again_keeps_the_schedule_without_recovering(
    tmp_path, tickers
):
    """start() after stop() in one process must not re-run the recovery: it
    would clear the markers of runs the first start left going."""
    owner, agent = tickers(tmp_path)
    todo = _due_todo(agent, minutes_ago=-60)  # not due: the loop leaves it
    owner.start()
    owner.stop()
    second, _ = tickers(tmp_path)
    assert second.prepare_startup_recovery()["owns_schedule"] is False
    assert agent._schedule_db.mark_execution_started(todo.id, USER, "thread-1")

    owner.start()
    try:
        assert owner.owns_schedule is True
        assert agent._schedule_db.count_active_executions() == 1
    finally:
        owner.stop()


def test_a_ticker_that_may_not_take_over_stays_on_standby(
    tmp_path, tickers, monkeypatch
):
    """The fat CLI's rule: a service restart leaves a gap a 5 s poll would
    win almost every time, stranding the service's schedule in a terminal."""
    owner, _ = tickers(tmp_path)
    owner.prepare_startup_recovery()
    notices: list[bool] = []
    monkeypatch.setattr(
        Ticker,
        "_print_standby_notice",
        staticmethod(lambda holder, takes_over: notices.append(takes_over)),
    )
    cli, _ = tickers(tmp_path, take_over=False)
    cli.prepare_startup_recovery()
    recorder = _PollRecorder(cli, monkeypatch)

    owner._release_schedule()
    for _ in range(3):
        _poll(cli)

    assert cli.owns_schedule is False
    assert recorder.calls == []
    assert notices == [False]
    # The restarted service claims the schedule, not the terminal.
    service, _ = tickers(tmp_path)
    assert service.prepare_startup_recovery()["owns_schedule"] is True


def test_a_ticker_that_may_not_take_over_never_claims_through_startup_either(
    tmp_path, tickers, monkeypatch
):
    """``start()`` re-runs ``prepare_startup_recovery`` on every standby, so
    the no-takeover rule must hold on that path too, not only on the poll."""
    owner, _ = tickers(tmp_path)
    owner.prepare_startup_recovery()
    monkeypatch.setattr(
        Ticker, "_print_standby_notice", staticmethod(lambda holder, takes_over: None)
    )
    cli, _ = tickers(tmp_path, take_over=False)
    cli.prepare_startup_recovery()

    owner._release_schedule()  # the service's process exits
    status = cli.prepare_startup_recovery()
    cli.start()
    try:
        assert status["owns_schedule"] is False
        assert cli.owns_schedule is False
    finally:
        cli.stop()
    free = ScheduleLock(tmp_path)
    try:
        assert free.acquire()[0] is LockOutcome.ACQUIRED  # left for the service
    finally:
        free.release()


def test_a_service_standby_claiming_through_startup_recovers_as_a_takeover(
    tmp_path, tickers, monkeypatch
):
    owner, owner_agent = tickers(tmp_path, scheduler_missed_work_policy="ask")
    held = _due_todo(owner_agent)
    owner.prepare_startup_recovery()
    monkeypatch.setattr(
        Ticker, "_print_standby_notice", staticmethod(lambda holder, takes_over: None)
    )
    second, second_agent = tickers(tmp_path, scheduler_missed_work_policy="ask")
    second.prepare_startup_recovery()
    fresh = _due_todo(second_agent, minutes_ago=0)  # due at the handoff

    owner._release_schedule()
    status = second.prepare_startup_recovery()  # what start() calls

    assert status["owns_schedule"] is True
    assert status["startup_missed_count"] == 0
    assert second._pending_startup_missed_ids == {held.id}
    assert fresh.id not in second._pending_startup_missed_ids


def test_a_takeover_whose_recovery_failed_is_retried_as_a_takeover(
    tmp_path, tickers, monkeypatch
):
    """A retry after a failed takeover recovery must not decide missed work as
    if the process had just booted: the owner ran until moments ago."""
    owner, owner_agent = tickers(tmp_path, scheduler_missed_work_policy="ask")
    held = _due_todo(owner_agent)
    owner.prepare_startup_recovery()
    second, second_agent = tickers(tmp_path, scheduler_missed_work_policy="ask")
    second.prepare_startup_recovery()
    _PollRecorder(second, monkeypatch)
    fresh = _due_todo(second_agent, minutes_ago=0)
    real_rebuild = second.rebuild_schedule_index
    failures = [RuntimeError("TODO file busy")]

    def rebuild_once():
        if failures:
            raise failures.pop()
        return real_rebuild()

    monkeypatch.setattr(second, "rebuild_schedule_index", rebuild_once)
    owner._release_schedule()

    _poll(second)  # the takeover recovery raises
    assert second._startup_recovery_prepared is False
    _poll(second)  # retried

    assert second._startup_recovery_prepared is True
    assert second._pending_startup_missed_ids == {held.id}
    assert fresh.id not in second._pending_startup_missed_ids


def test_a_standalone_ticker_that_may_not_take_over_still_owns_a_free_schedule(
    tmp_path, tickers
):
    cli, _ = tickers(tmp_path, take_over=False)
    assert cli.prepare_startup_recovery()["owns_schedule"] is True


@pytest.mark.parametrize("policy", ["ask", "run"])
def test_a_takeover_decides_no_missed_work_and_keeps_an_ask_hold(
    tmp_path, tickers, monkeypatch, policy
):
    """A scheduler ran until moments ago: nothing due at the takeover was
    missed. Under ``ask`` the owner's persisted hold carries over; under
    ``run`` it is let go."""
    owner, owner_agent = tickers(tmp_path, scheduler_missed_work_policy="ask")
    held = _due_todo(owner_agent)
    owner.prepare_startup_recovery()
    second, second_agent = tickers(tmp_path, scheduler_missed_work_policy=policy)
    second.prepare_startup_recovery()
    _PollRecorder(second, monkeypatch)
    fresh = _due_todo(second_agent, minutes_ago=0)  # due at the handoff

    owner._release_schedule()
    _poll(second)

    state = SchedulerStateManager(tmp_path).load()
    if policy == "ask":
        assert second._pending_startup_missed_ids == {held.id}
        assert second._trigger_catchup_paused is True
        assert state["pending_missed_todo_ids"] == [held.id]
    else:
        assert second._pending_startup_missed_ids == set()
        assert second._trigger_catchup_paused is False
        assert not state.get("pending_missed_todo_ids")
    assert fresh.id not in second._pending_startup_missed_ids


def test_a_killed_holder_process_frees_the_schedule(tmp_path, tickers, monkeypatch):
    """L4: the OS drops the lock with the process; no stale file blocks."""
    script = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path
        from nymeria.core.scheduler_lock import ScheduleLock
        lock = ScheduleLock(Path({str(tmp_path)!r}))
        print(lock.acquire()[0].value, flush=True)
        time.sleep(120)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=BACKEND_ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == LockOutcome.ACQUIRED.value
        second, _ = tickers(tmp_path)
        status = second.prepare_startup_recovery()
        assert status["owns_schedule"] is False
        assert f"pid {child.pid}" in (status["schedule_held_by"] or "")
        _PollRecorder(second, monkeypatch)

        child.kill()  # SIGKILL on POSIX, TerminateProcess on Windows
        child.wait(timeout=10)
        # Windows drops a dead process's locks asynchronously.
        deadline = time.monotonic() + 5
        while not second.owns_schedule and time.monotonic() < deadline:
            _poll(second)
            if not second.owns_schedule:
                time.sleep(0.05)

        assert second.owns_schedule is True
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_a_stopped_tickers_poll_thread_cannot_claim_the_lock(tmp_path, tickers):
    """A takeover attempt from a poll thread stop() already told to exit
    must not take a free lock and strand it in a stopped ticker."""
    owner, _ = tickers(tmp_path)
    owner.prepare_startup_recovery()
    second, _ = tickers(tmp_path)
    second.prepare_startup_recovery()
    owner._release_schedule()

    second._running = False  # stop() already told its poll thread to exit
    assert second._ensure_schedule_owner() is False

    outsider = ScheduleLock(tmp_path)
    try:
        assert outsider.acquire()[0] is LockOutcome.ACQUIRED
    finally:
        outsider.release()


# ------------------------------------------------------------------
# The lock file is deleted or replaced under a live owner
# ------------------------------------------------------------------


def test_a_deleted_lock_file_is_locked_again(tmp_path, tickers, caplog):
    owner, _ = tickers(tmp_path)
    owner.prepare_startup_recovery()
    (tmp_path / SCHEDULER_LOCK_FILENAME).unlink()

    with caplog.at_level("WARNING", logger="nymeria.core.ticker"):
        owner._check_lock_file()

    assert any("locked again" in r.getMessage() for r in caplog.records)
    outcome, holder = ScheduleLock(tmp_path).acquire()
    assert outcome is LockOutcome.HELD_ELSEWHERE
    assert f"pid {os.getpid()}" in holder


def test_a_replaced_lock_file_someone_else_holds_is_an_error_once(
    tmp_path, tickers, caplog
):
    owner, _ = tickers(tmp_path)
    owner.prepare_startup_recovery()
    (tmp_path / SCHEDULER_LOCK_FILENAME).unlink()
    intruder = ScheduleLock(tmp_path)
    assert intruder.acquire()[0] is LockOutcome.ACQUIRED
    try:
        with caplog.at_level("ERROR", logger="nymeria.core.ticker"):
            owner._check_lock_file()
            owner._check_lock_file()
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "two processes" in errors[0].getMessage()
    finally:
        intruder.release()


def test_an_intact_lock_file_is_left_alone(tmp_path, tickers, caplog):
    owner, _ = tickers(tmp_path)
    owner.prepare_startup_recovery()
    # An earlier test can leave logging at INFO: only this call's ticker
    # warnings count.
    caplog.clear()
    with caplog.at_level("WARNING", logger="nymeria.core.ticker"):
        owner._check_lock_file()
    assert [
        r for r in caplog.records
        if r.name == "nymeria.core.ticker" and r.levelno >= 30
    ] == []


# ------------------------------------------------------------------
# L5: a lock error other than contention fails open
# ------------------------------------------------------------------


def test_an_unavailable_lock_fails_open_with_a_warning_and_a_notice(
    tmp_path, tickers, monkeypatch, caplog
):
    def no_locks(fd):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(scheduler_lock_module, "_lock", no_locks)
    notices: list[str] = []
    monkeypatch.setattr(
        Ticker, "_print_lock_unavailable_notice", staticmethod(notices.append)
    )
    ticker, agent = tickers(tmp_path)
    todo = _due_todo(agent)
    assert agent._schedule_db.mark_execution_started(todo.id, USER, "thread-1")

    with caplog.at_level("WARNING", logger="nymeria.core.ticker"):
        status = ticker.prepare_startup_recovery()

    assert status["owns_schedule"] is True
    assert status["startup_execution_markers_cleared"] == 1
    assert any("lock unavailable" in r.getMessage() for r in caplog.records)
    # The fat CLI silences the logger: the console notice is its signal.
    assert len(notices) == 1 and "No locks available" in notices[0]


def _fail_open(tmp_path: Path, tickers, monkeypatch, caplog) -> Ticker:
    """An owner whose lock failed open with a transient error, then healed."""
    real_lock = scheduler_lock_module._lock

    def no_locks(fd):
        raise OSError(errno.EMFILE, "Too many open files")

    monkeypatch.setattr(scheduler_lock_module, "_lock", no_locks)
    monkeypatch.setattr(
        Ticker, "_print_lock_unavailable_notice", staticmethod(lambda detail: None)
    )
    ticker, _ = tickers(tmp_path)
    assert ticker.prepare_startup_recovery()["owns_schedule"] is True
    _PollRecorder(ticker, monkeypatch)
    caplog.clear()
    with caplog.at_level("INFO", logger="nymeria.core.ticker"):
        _poll(ticker)  # still failing: the claim already warned
    assert [r for r in caplog.records if r.name == "nymeria.core.ticker"] == []
    assert ticker.owns_schedule is True
    monkeypatch.setattr(scheduler_lock_module, "_lock", real_lock)
    return ticker


def test_an_unguarded_owner_takes_the_lock_once_it_can(
    tmp_path, tickers, monkeypatch, caplog
):
    """A transient lock error must not leave the guard off for the life of
    the process: the next poll that can lock does, and later tickers stand by."""
    ticker = _fail_open(tmp_path, tickers, monkeypatch, caplog)

    with caplog.at_level("INFO", logger="nymeria.core.ticker"):
        _poll(ticker)

    assert any("guard is on again" in r.getMessage() for r in caplog.records)
    outcome, holder = ScheduleLock(tmp_path).acquire()
    assert outcome is LockOutcome.HELD_ELSEWHERE
    assert f"pid {os.getpid()}" in holder
    later, _ = tickers(tmp_path)
    assert later.prepare_startup_recovery()["owns_schedule"] is False


def test_an_unguarded_owner_reports_a_second_scheduler_once(
    tmp_path, tickers, monkeypatch, caplog
):
    ticker = _fail_open(tmp_path, tickers, monkeypatch, caplog)
    other = ScheduleLock(tmp_path)  # a process that started while unguarded
    assert other.acquire()[0] is LockOutcome.ACQUIRED
    try:
        with caplog.at_level("ERROR", logger="nymeria.core.ticker"):
            for _ in range(3):
                _poll(ticker)
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "two processes" in errors[0].getMessage()
        assert f"pid {os.getpid()}" in errors[0].getMessage()
        assert ticker.owns_schedule is True  # in-flight runs keep going
    finally:
        other.release()


# ------------------------------------------------------------------
# The lock primitive
# ------------------------------------------------------------------


def test_the_lock_is_exclusive_per_open_and_reusable_after_release(tmp_path):
    first, second = ScheduleLock(tmp_path), ScheduleLock(tmp_path)
    try:
        assert first.acquire()[0] is LockOutcome.ACQUIRED
        outcome, holder = second.acquire()
        assert outcome is LockOutcome.HELD_ELSEWHERE
        assert f"pid {os.getpid()}" in holder
        first.release()
        # Released, not deleted: deleting is what lets two owners coexist.
        assert (tmp_path / SCHEDULER_LOCK_FILENAME).exists()
        assert second.acquire()[0] is LockOutcome.ACQUIRED
    finally:
        first.release()
        second.release()


@pytest.mark.parametrize(
    ("argv", "label"),
    [
        (["/usr/local/bin/nymeria", "slim", "--port", "8010"], "nymeria slim"),
        (["run.py", "worker"], "run.py worker"),
        # The first bare word after a flag is that flag's value.
        (["/usr/local/bin/nymeria", "--token", "nym_secret", "slim"], "nymeria"),
        (["/usr/local/bin/nymeria", "/home/me/secret.env"], "nymeria"),
    ],
)
def test_the_holder_note_names_the_subcommand_never_a_flag_value(
    tmp_path, monkeypatch, argv, label
):
    monkeypatch.setattr(sys, "argv", argv)
    lock = ScheduleLock(tmp_path)
    try:
        assert lock.acquire()[0] is LockOutcome.ACQUIRED
        note = json.loads((tmp_path / SCHEDULER_LOCK_FILENAME).read_text())
    finally:
        lock.release()

    assert note["pid"] == os.getpid()
    assert note["command"] == label
    assert "secret" not in json.dumps(note)


def test_an_unreadable_note_still_reports_contention(tmp_path):
    first = ScheduleLock(tmp_path)
    try:
        assert first.acquire()[0] is LockOutcome.ACQUIRED
        (tmp_path / SCHEDULER_LOCK_FILENAME).write_bytes(b"\x00not json")
        outcome, holder = ScheduleLock(tmp_path).acquire()
        assert outcome is LockOutcome.HELD_ELSEWHERE
        assert holder == "another process"
    finally:
        first.release()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_the_lock_file_is_created_readable_by_other_users(tmp_path):
    """Another user (``sudo nymeria slim`` once, then the service user) can
    only lock the file through a read-only descriptor if it can read it."""
    previous = os.umask(0o022)
    try:
        lock = ScheduleLock(tmp_path)
        assert lock.acquire()[0] is LockOutcome.ACQUIRED
        lock.release()
    finally:
        os.umask(previous)

    mode = (tmp_path / SCHEDULER_LOCK_FILENAME).stat().st_mode & 0o777
    assert mode & 0o044 == 0o044


@pytest.mark.skipif(
    sys.platform == "win32" or os.geteuid() == 0,
    reason="needs POSIX permissions that bind the current user",
)
def test_a_lock_file_this_user_cannot_write_still_locks(tmp_path):
    """A lock file another user left (``sudo nymeria slim`` once) locks
    through a read-only descriptor instead of failing open."""
    path = tmp_path / SCHEDULER_LOCK_FILENAME
    path.write_text("")
    path.chmod(0o400)
    first, second = ScheduleLock(tmp_path), ScheduleLock(tmp_path)
    try:
        assert first.acquire()[0] is LockOutcome.ACQUIRED
        outcome, holder = second.acquire()
        assert outcome is LockOutcome.HELD_ELSEWHERE
        assert holder == "another process"  # no note could be written
    finally:
        first.release()
        second.release()


def test_the_holder_note_is_shown_without_control_characters(tmp_path):
    first = ScheduleLock(tmp_path)
    try:
        assert first.acquire()[0] is LockOutcome.ACQUIRED
        (tmp_path / SCHEDULER_LOCK_FILENAME).write_text(
            json.dumps({"pid": 7, "host": "box", "command": "x\x1b[2J\x07y"})
        )
        outcome, holder = ScheduleLock(tmp_path).acquire()
        assert outcome is LockOutcome.HELD_ELSEWHERE
        assert holder == "pid 7 on box (x[2Jy)"
    finally:
        first.release()


def test_an_unopenable_lock_file_is_unavailable_not_contended(tmp_path):
    (tmp_path / SCHEDULER_LOCK_FILENAME).mkdir()  # a directory cannot be opened RDWR
    outcome, detail = ScheduleLock(tmp_path).acquire()
    assert outcome is LockOutcome.UNAVAILABLE
    assert "cannot open" in detail
