"""#410 + #398: which process runs the schedule, and the missed-work release,
answered from ANY process on the data dir.

The Docker API runs no ticker (the worker runs the schedule), so before this
its status said nobody owned the schedule and its release answered 409 while
the worker held the missed work and paused trigger polling, re-held on every
restart. ``core/scheduler_control.py`` now probes the scheduler lock for the
status and relays a release through a request record the owner honors on its
next poll.

A ticker here stands in for one process: ``Ticker._release_schedule`` for its
exit, ``scheduler_status(None, ...)`` for the ticker-less Docker API.
"""

from __future__ import annotations

import errno
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from nymeria.core import scheduler_control, scheduler_lock
from nymeria.core import ticker as ticker_module
from nymeria.core.command_service import (
    CommandBackendClient,
    CommandContext,
    CommandService,
    _CommandBackendUser,
)
from nymeria.core.scheduler_control import (
    SchedulerControlError,
    release_missed_work,
    scheduler_status,
)
from nymeria.core.scheduler_lock import SCHEDULER_LOCK_FILENAME, ProbeOutcome, ScheduleLock
from nymeria.core.scheduler_state import (
    SchedulerStateManager,
    read_release_request,
    release_request_path,
    write_release_request,
)
from nymeria.core.ticker import Ticker

from cli_fixtures import run
from test_scheduler_lock import _due_todo, _ticker

PID = f"pid {os.getpid()}"


@pytest.fixture
def tickers():
    built: list[Ticker] = []

    def build(data_dir: Path, *, take_over: bool = True, **settings: Any):
        ticker, agent = _ticker(data_dir, take_over=take_over, **settings)
        built.append(ticker)
        return ticker, agent

    yield build
    for ticker in built:
        ticker.stop()
        ticker._running = False
        ticker._release_schedule()


def _holding_owner(tickers, data_dir: Path):
    """An owner process under ``ask`` that holds one missed TODO."""
    owner, agent = tickers(data_dir, scheduler_missed_work_policy="ask")
    todo = _due_todo(agent)
    owner.prepare_startup_recovery()
    assert owner._pending_startup_missed_ids == {todo.id}
    assert owner._trigger_catchup_paused is True
    return owner, agent, todo


# ------------------------------------------------------------------
# The lock probe
# ------------------------------------------------------------------


def test_a_probe_of_a_data_dir_nobody_scheduled_is_free_and_creates_nothing(tmp_path):
    assert ScheduleLock(tmp_path).probe() == (ProbeOutcome.FREE, "")
    assert not (tmp_path / SCHEDULER_LOCK_FILENAME).exists()


def test_a_probe_names_the_holder_and_leaves_its_note_and_lock_alone(tmp_path, tickers):
    owner, _ = tickers(tmp_path)
    owner.prepare_startup_recovery()
    note_before = (tmp_path / SCHEDULER_LOCK_FILENAME).read_bytes()

    outcome, holder = ScheduleLock(tmp_path).probe()

    assert outcome is ProbeOutcome.HELD
    assert PID in holder
    assert (tmp_path / SCHEDULER_LOCK_FILENAME).read_bytes() == note_before
    # The owner still holds it: another claimant still stands by.
    assert ScheduleLock(tmp_path).acquire()[0].value == "held_elsewhere"


def test_a_probe_after_the_owner_exited_is_free_and_leaves_it_claimable(tmp_path, tickers):
    owner, _ = tickers(tmp_path)
    owner.prepare_startup_recovery()
    owner._release_schedule()  # the process exits; its note stays behind

    assert ScheduleLock(tmp_path).probe() == (ProbeOutcome.FREE, "")
    successor, _ = tickers(tmp_path)
    assert successor.prepare_startup_recovery()["owns_schedule"] is True


# ------------------------------------------------------------------
# #410: status from any process
# ------------------------------------------------------------------


def test_a_process_without_a_ticker_names_the_process_that_runs_the_schedule(
    tmp_path, tickers
):
    owner, agent = tickers(tmp_path)
    owner.prepare_startup_recovery()

    status = scheduler_status(None, agent.settings)

    assert status["schedule_owner"] == "another_process"
    assert PID in status["schedule_held_by"]
    assert PID in status["schedule_runner"]


def test_nobody_runs_the_schedule_once_the_owner_exited_despite_its_note(
    tmp_path, tickers
):
    owner, agent = tickers(tmp_path)
    owner.prepare_startup_recovery()
    owner._release_schedule()
    assert PID in ScheduleLock(tmp_path).holder()  # the stale note says otherwise

    status = scheduler_status(None, agent.settings)

    assert status["schedule_owner"] == "none"
    assert status["schedule_held_by"] is None
    assert status["schedule_runner"] is None


def test_the_owner_reports_that_it_runs_the_schedule(tmp_path, tickers):
    owner, agent = tickers(tmp_path)
    owner.prepare_startup_recovery()

    status = scheduler_status(owner, agent.settings)

    assert status["schedule_owner"] == "this_process"
    assert status["schedule_held_by"] is None
    assert PID in status["schedule_runner"]


def test_a_standby_that_cannot_take_over_stops_naming_an_owner_that_exited(
    tmp_path, tickers
):
    owner, _ = tickers(tmp_path)
    owner.prepare_startup_recovery()
    cli, cli_agent = tickers(tmp_path, take_over=False)  # the fat CLI shape
    assert cli.prepare_startup_recovery()["owns_schedule"] is False
    assert PID in scheduler_status(cli, cli_agent.settings)["schedule_held_by"]

    owner._release_schedule()

    status = scheduler_status(cli, cli_agent.settings)
    assert status["schedule_owner"] == "none"
    assert status["schedule_held_by"] is None


# ------------------------------------------------------------------
# #398: the release, from the owner and relayed
# ------------------------------------------------------------------


def test_the_owner_releases_its_hold_directly(tmp_path, tickers):
    owner, agent, todo = _holding_owner(tickers, tmp_path)

    result = release_missed_work(owner, agent.settings, requested_by="admin")

    assert result["release"] == "released"
    assert result["released_todo_ids"] == [todo.id]
    assert owner._trigger_catchup_paused is False
    assert not release_request_path(tmp_path).exists()  # nothing to relay


def test_a_release_from_a_process_without_a_ticker_is_honored_by_the_owner(
    tmp_path, tickers
):
    """The Docker shape: the API has no ticker, the worker holds the work."""
    owner, agent, todo = _holding_owner(tickers, tmp_path)
    stop = threading.Event()

    def owner_polls() -> None:
        while not stop.is_set():
            owner._running = True
            owner._poll_once()
            time.sleep(0.02)

    for name in (
        "_check_and_execute",
        "_maybe_submit_archive",
        "_maybe_submit_trigger_poll",
        "_maybe_submit_spawn_sweep",
        "_maybe_submit_dream_sweep",
        "_maybe_submit_watchdog_sweep",
    ):
        setattr(owner, name, lambda *a, **k: None)
    poller = threading.Thread(target=owner_polls, daemon=True)
    poller.start()
    try:
        result = release_missed_work(
            None, agent.settings, requested_by="admin", wait_seconds=5
        )
    finally:
        stop.set()
        poller.join(timeout=5)

    assert result["release"] == "released"
    assert result["released_todo_ids"] == [todo.id]
    assert owner._pending_startup_missed_ids == set()
    assert owner._trigger_catchup_paused is False
    assert SchedulerStateManager(tmp_path).load()["pending_missed_todo_ids"] == []
    request = read_release_request(tmp_path)
    assert request is not None and request.requested_by == "admin"


def test_a_release_the_owner_has_not_picked_up_yet_is_reported_as_requested(
    tmp_path, tickers
):
    owner, agent, todo = _holding_owner(tickers, tmp_path)

    result = release_missed_work(None, agent.settings, requested_by="admin", wait_seconds=0)

    assert result["release"] == "requested"
    assert result["released_todo_ids"] == []
    assert PID in result["schedule_runner"]
    assert scheduler_status(None, agent.settings)["release_requested_at"] is not None
    assert owner._pending_startup_missed_ids == {todo.id}  # still held

    owner._honor_release_request()  # the owner's next poll

    assert owner._pending_startup_missed_ids == set()
    after = scheduler_status(None, agent.settings)
    assert after["pending_missed_todo_ids"] == []
    assert after["release_requested_at"] is None


def test_releasing_when_nothing_is_held_records_no_request(tmp_path, tickers):
    owner, agent = tickers(tmp_path, scheduler_missed_work_policy="ask")
    owner.prepare_startup_recovery()  # nothing was missed

    result = release_missed_work(None, agent.settings, requested_by="admin", wait_seconds=5)

    assert result["release"] == "nothing_held"
    assert not release_request_path(tmp_path).exists()


def test_the_owner_says_so_when_it_holds_nothing(tmp_path, tickers):
    owner, agent = tickers(tmp_path, scheduler_missed_work_policy="ask")
    owner.prepare_startup_recovery()  # nothing was missed

    result = release_missed_work(owner, agent.settings, requested_by="admin")

    assert result["release"] == "nothing_held"
    assert result["released_todo_ids"] == []
    assert "Nothing is held" in _command(agent, owner, "/scheduler release").markdown


def test_releasing_when_no_process_runs_the_scheduler_is_refused(tmp_path, tickers):
    owner, agent, _ = _holding_owner(tickers, tmp_path)
    owner._release_schedule()  # the worker is down; its hold stays on disk

    with pytest.raises(SchedulerControlError) as refused:
        release_missed_work(None, agent.settings, requested_by="admin", wait_seconds=5)

    assert refused.value.status_code == 409
    assert "No process runs the scheduler" in str(refused.value)
    assert not release_request_path(tmp_path).exists()


def test_a_release_the_worker_cannot_be_asked_for_is_refused_and_holds(
    tmp_path, tickers, monkeypatch
):
    owner, agent, todo = _holding_owner(tickers, tmp_path)

    def unwritable(data_dir, requested_by):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(scheduler_control, "write_release_request", unwritable)

    with pytest.raises(SchedulerControlError) as refused:
        release_missed_work(None, agent.settings, requested_by="admin", wait_seconds=5)
    assert refused.value.status_code == 503
    assert "Could not record the release request" in str(refused.value)
    reply = _command(agent, None, "/scheduler release")
    assert reply.success is False
    assert "Could not record the release request" in reply.markdown
    assert owner._pending_startup_missed_ids == {todo.id}


def test_a_release_is_still_relayed_when_the_lock_cannot_be_tested(
    tmp_path, tickers, monkeypatch
):
    """A filesystem without locks: the status cannot say who runs the
    schedule, but a release must not be refused as if nobody did."""
    owner, agent, todo = _holding_owner(tickers, tmp_path)

    def no_locks(fd):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(scheduler_lock, "_lock", no_locks)

    status = scheduler_status(None, agent.settings)
    assert status["schedule_owner"] == "unknown"
    assert "the scheduler lock cannot be tested" in _command(agent, None, "/scheduler").markdown

    result = release_missed_work(None, agent.settings, requested_by="admin", wait_seconds=0)
    assert result["release"] == "requested"
    owner._honor_release_request()
    assert owner._pending_startup_missed_ids == set()
    assert todo.id not in scheduler_status(None, agent.settings)["pending_missed_todo_ids"]


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.mark.parametrize(
    ("poll_seconds", "waits_about"),
    [(0.2, 3.0), (5, 12.0), (60, 15.0)],
)
def test_a_relayed_release_waits_about_two_polls_but_never_past_fifteen_seconds(
    tmp_path, tickers, poll_seconds, waits_about
):
    """Long enough for the owner's next poll, short enough for an HTTP
    caller (the command clients read with a 30 s timeout)."""
    _owner, agent, _ = _holding_owner(tickers, tmp_path)
    agent.settings.ticker_poll_interval = poll_seconds
    clock = _FakeClock()

    result = release_missed_work(
        None, agent.settings, requested_by="admin", sleep=clock.sleep, monotonic=clock.monotonic
    )

    assert result["release"] == "requested"  # the owner never polled
    assert waits_about <= clock.now < waits_about + 0.5


def test_a_request_older_than_the_hold_never_releases_it(tmp_path, tickers):
    """`ask` must still hold after every restart, even with a request left on
    disk from before it (a leftover, or a plant)."""
    write_release_request(tmp_path, "admin")
    time.sleep(0.01)
    owner, _, todo = _holding_owner(tickers, tmp_path)  # detects after the request

    owner._honor_release_request()

    assert owner._pending_startup_missed_ids == {todo.id}
    assert owner._trigger_catchup_paused is True


def test_an_old_request_is_not_shown_as_waiting_while_a_new_hold_stands(tmp_path, tickers):
    write_release_request(tmp_path, "admin")
    time.sleep(0.01)
    _owner, agent, _ = _holding_owner(tickers, tmp_path)

    status = scheduler_status(None, agent.settings)
    assert status["release_requested_at"] is None
    view = _command(agent, None, "/scheduler").markdown
    assert "Run /scheduler release" in view  # the admin is still asked
    assert "waiting for the scheduler" not in view


def _plant_request(data_dir: Path, requested_at: str) -> None:
    release_request_path(data_dir).write_text(
        json.dumps({"requested_at": requested_at, "requested_by": "x"}), encoding="utf-8"
    )


def test_a_request_dated_in_the_future_never_releases_a_hold(tmp_path, tickers):
    """Such a stamp would be newer than every hold any restart detects, so
    one planted write would turn `ask` into `run` for good."""
    _plant_request(tmp_path, "2999-01-01T00:00:00+00:00")
    owner, agent, todo = _holding_owner(tickers, tmp_path)

    owner._honor_release_request()
    assert owner._pending_startup_missed_ids == {todo.id}
    assert scheduler_status(None, agent.settings)["release_requested_at"] is None

    owner._release_schedule()  # a restart detects afresh; still held
    successor, _ = tickers(tmp_path, scheduler_missed_work_policy="ask")
    successor.prepare_startup_recovery()
    successor._honor_release_request()
    assert successor._pending_startup_missed_ids == {todo.id}
    assert successor._trigger_catchup_paused is True


def test_a_hold_of_unknown_age_is_never_released_by_a_request(tmp_path, tickers):
    """Which requests are newer than the hold cannot be told, so none is."""
    owner, _, todo = _holding_owner(tickers, tmp_path)
    owner._hold_since = None
    write_release_request(tmp_path, "admin")

    owner._honor_release_request()

    assert owner._pending_startup_missed_ids == {todo.id}


def test_a_request_within_a_clock_skew_of_now_still_counts(tmp_path, tickers):
    owner, _, _ = _holding_owner(tickers, tmp_path)
    soon = datetime.now(timezone.utc) + timedelta(seconds=30)
    _plant_request(tmp_path, soon.isoformat())

    owner._honor_release_request()

    assert owner._pending_startup_missed_ids == set()


@pytest.mark.parametrize(
    "payload",
    ["not json", "[]", '{"requested_at": "2099-01-01T00:00:00"}', '{"requested_by": "x"}'],
    ids=["garbage", "not-an-object", "naive-time", "no-time"],
)
def test_a_malformed_request_is_no_request(tmp_path, tickers, payload):
    owner, agent, todo = _holding_owner(tickers, tmp_path)
    release_request_path(tmp_path).write_text(payload, encoding="utf-8")

    owner._honor_release_request()

    assert owner._pending_startup_missed_ids == {todo.id}
    assert scheduler_status(None, agent.settings)["release_requested_at"] is None


def test_a_takeover_honors_a_request_made_against_the_hold_it_adopted(tmp_path, tickers):
    owner, _, todo = _holding_owner(tickers, tmp_path)
    successor, _ = tickers(tmp_path, scheduler_missed_work_policy="ask")
    assert successor.prepare_startup_recovery()["owns_schedule"] is False
    time.sleep(0.01)
    write_release_request(tmp_path, "admin")  # asked while the old owner ran
    owner._release_schedule()  # ... which exited before its next poll

    successor._running = True
    assert successor._ensure_schedule_owner() is True
    assert successor._pending_startup_missed_ids == {todo.id}  # adopted
    successor._honor_release_request()

    assert successor._pending_startup_missed_ids == set()
    assert successor._trigger_catchup_paused is False


def test_the_owner_reads_no_request_while_nothing_is_held(tmp_path, tickers, monkeypatch):
    reads: list[Path] = []

    def recording_read(data_dir):
        reads.append(data_dir)
        return None

    monkeypatch.setattr(ticker_module, "read_release_request", recording_read)
    owner, _ = tickers(tmp_path)
    owner.prepare_startup_recovery()

    owner._honor_release_request()
    assert reads == []

    held, _, _ = _holding_owner(tickers, tmp_path / "held")
    held._honor_release_request()
    assert reads == [tmp_path / "held"]


def test_a_poll_honors_the_release_before_it_looks_for_due_work(tmp_path, tickers, monkeypatch):
    owner, _ = tickers(tmp_path)
    owner.prepare_startup_recovery()
    order: list[str] = []
    monkeypatch.setattr(owner, "_honor_release_request", lambda: order.append("release"))
    monkeypatch.setattr(owner, "_check_and_execute", lambda: order.append("due"))
    for name in (
        "_maybe_submit_archive",
        "_maybe_submit_trigger_poll",
        "_maybe_submit_spawn_sweep",
        "_maybe_submit_dream_sweep",
        "_maybe_submit_watchdog_sweep",
    ):
        monkeypatch.setattr(owner, name, lambda *a, **k: None)

    owner._running = True
    owner._poll_once()

    assert order == ["release", "due"]


# ------------------------------------------------------------------
# The /scheduler command, through the real in-process backend client
# ------------------------------------------------------------------


def _command(agent: Any, ticker: Any, command: str, *, role: str = "admin"):
    agent._ticker = ticker
    api = CommandBackendClient(
        agent,
        user=_CommandBackendUser(id="boss", role=role),  # type: ignore[arg-type]
        settings_fn=lambda: agent.settings,
    )
    return run(
        CommandService().execute(
            CommandContext(
                user_id="boss",
                thread_id=None,
                actor="user",
                surface="cli",
                is_admin=role == "admin",
            ),
            command,
            api=api,
        )
    )


def test_scheduler_command_names_the_owner_and_the_held_work(tmp_path, tickers):
    _owner, agent, todo = _holding_owner(tickers, tmp_path)

    bare = _command(agent, None, "/scheduler")
    status = _command(agent, None, "/scheduler status")

    assert bare.markdown == status.markdown
    assert status.success is True
    assert PID in status.markdown
    assert "ask" in status.markdown
    assert todo.id in status.markdown
    assert "Water the plants" in status.markdown
    assert "/scheduler release" in status.markdown


def test_scheduler_command_without_a_hold_offers_no_release(tmp_path, tickers):
    owner, agent = tickers(tmp_path)
    owner.prepare_startup_recovery()

    result = _command(agent, owner, "/scheduler")

    assert result.success is True
    assert "the process answering this command" in result.markdown
    assert "/scheduler release" not in result.markdown


def test_scheduler_command_says_when_nobody_runs_the_schedule(tmp_path, tickers):
    _, agent = tickers(tmp_path)  # built, never started

    result = _command(agent, None, "/scheduler")

    assert "no process right now" in result.markdown


def test_scheduler_release_command_relays_and_reports(tmp_path, tickers, monkeypatch):
    owner, agent, todo = _holding_owner(tickers, tmp_path)
    monkeypatch.setattr(scheduler_control, "_default_wait", lambda settings: 0.0)

    requested = _command(agent, None, "/scheduler release")
    assert requested.success is True
    assert "Release requested" in requested.markdown
    assert PID in requested.markdown

    owner._honor_release_request()
    nothing = _command(agent, None, "/scheduler release")
    assert "Nothing is held" in nothing.markdown

    owner._release_schedule()  # the worker goes down
    refused = _command(agent, None, "/scheduler release")
    assert refused.success is False
    assert "No process runs the scheduler" in refused.markdown
    assert todo.id not in refused.markdown


def test_scheduler_release_by_the_owner_names_what_ran(tmp_path, tickers):
    owner, agent, todo = _holding_owner(tickers, tmp_path)

    result = _command(agent, owner, "/scheduler release")

    assert result.success is True
    assert todo.id in result.markdown
    assert owner._pending_startup_missed_ids == set()


def test_the_status_shows_a_release_the_scheduler_has_not_picked_up(
    tmp_path, tickers, monkeypatch
):
    _owner, agent, _ = _holding_owner(tickers, tmp_path)
    monkeypatch.setattr(scheduler_control, "_default_wait", lambda settings: 0.0)
    _command(agent, None, "/scheduler release")

    view = _command(agent, None, "/scheduler").markdown

    assert "requested" in view and "waiting for the scheduler's next poll" in view
    assert "Run /scheduler release" not in view  # already asked


@pytest.mark.parametrize("command", ["/scheduler", "/scheduler status", "/scheduler release"])
def test_the_scheduler_family_is_admin_only(tmp_path, tickers, command):
    owner, agent, _ = _holding_owner(tickers, tmp_path)

    result = _command(agent, owner, command, role="user")

    assert result.success is False
    assert owner._pending_startup_missed_ids  # still held


@pytest.mark.parametrize("method", ["get_scheduler_status", "release_missed_work"])
def test_the_in_process_client_refuses_a_non_admin_on_its_own(tmp_path, tickers, method):
    """The dispatcher gates first; the backend client does not rely on it."""
    owner, agent, _ = _holding_owner(tickers, tmp_path)
    agent._ticker = None
    api = CommandBackendClient(
        agent,
        user=_CommandBackendUser(id="nobody", role="user"),  # type: ignore[arg-type]
        settings_fn=lambda: agent.settings,
    )

    with pytest.raises(httpx.HTTPStatusError) as refused:
        run(getattr(api, method)(user_id="nobody"))

    assert refused.value.response.status_code == 403
    assert not release_request_path(tmp_path).exists()
    assert owner._pending_startup_missed_ids


class _PermissiveSchedulerApi:
    """A backend that would serve anyone, so only the dispatch gates stand
    between a caller and the scheduler."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_scheduler_status(self, *, user_id=None) -> dict:
        self.calls.append("status")
        return {"schedule_owner": "none"}

    async def release_missed_work(self, *, user_id=None) -> dict:
        self.calls.append("release")
        return {"release": "released", "released_todo_ids": ["a1b2c3d4"]}


def _dispatch(command: str, *, is_admin: bool, actor: str = "user"):
    api = _PermissiveSchedulerApi()
    result = run(
        CommandService().execute(
            CommandContext(
                user_id="someone",
                thread_id=None,
                actor=actor,  # type: ignore[arg-type]
                surface="cli",
                is_admin=is_admin,
            ),
            command,
            api=api,
        )
    )
    return result, api.calls


@pytest.mark.parametrize("command", ["/scheduler", "/scheduler status", "/scheduler release"])
def test_the_dispatcher_refuses_the_family_to_a_non_admin(command):
    result, calls = _dispatch(command, is_admin=False)

    assert result.success is False
    assert calls == []


def test_an_agent_may_read_the_scheduler_but_not_release_held_work():
    """Holding missed work for a human decision is the point of `ask`."""
    status, status_calls = _dispatch("/scheduler", is_admin=True, actor="agent")
    release, release_calls = _dispatch("/scheduler release", is_admin=True, actor="agent")

    assert status.success is True and status_calls == ["status"]
    assert release.success is False
    assert release_calls == []
