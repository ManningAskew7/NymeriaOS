"""Scheduler status and the missed-work release, for every door (#398, #410).

The REST routes (``GET /scheduler/status``, ``POST
/scheduler/missed-work/run`` in ``api/routers/system.py``) and the in-process
command client behind ``/scheduler`` both call these two functions, so the
answer cannot differ by surface.

Which process runs the schedule is decided by the data dir's scheduler lock
(#397, ``core/scheduler_lock.py``), so it can be answered from ANY process on
that data dir: a ticker that owns the schedule says so; everything else
(the Docker API, whose ticker is disabled because the worker runs the
schedule; a standby; a fat CLI) probes the lock. ``schedule_owner`` is one of
``this_process``, ``another_process`` (``schedule_held_by`` names it),
``none`` (nobody runs a scheduler for this data dir right now) or
``unknown`` (the lock cannot be tested here).

The release: under ``SCHEDULER_MISSED_WORK_POLICY=ask`` the hold lives in the
process that runs the scheduler. The owner releases directly; any other
process records a request in the data dir (``scheduler_state``'s release
request) that the owner honors on its next poll, and waits briefly to report
the outcome. Blocking by design: callers run it off the event loop.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .scheduler_lock import ProbeOutcome, ScheduleLock, describe_current_process
from .scheduler_state import (
    SchedulerStateManager,
    parse_state_time,
    read_release_request,
    write_release_request,
)

# How often a relayed release re-reads the shared state while it waits.
_RELEASE_WAIT_STEP_SECONDS = 0.25


class SchedulerControlError(Exception):
    """A release that cannot be carried out; ``status_code`` is its HTTP twin."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def schedule_owner(ticker: Any, data_dir: Path) -> tuple[str, Optional[str]]:
    """``(schedule_owner, schedule_held_by)`` as seen from this process.

    The ticker is asked first: an owner must never probe its own lock (on a
    filesystem with process-owned locks, closing the probe's descriptor
    would drop the owner's lock, ``ScheduleLock.probe``).
    """
    if ticker is not None and getattr(ticker, "owns_schedule", False):
        return "this_process", None
    outcome, detail = ScheduleLock(data_dir).probe()
    if outcome is ProbeOutcome.HELD:
        return "another_process", detail or "another process"
    if outcome is ProbeOutcome.FREE:
        return "none", None
    return "unknown", None


def scheduler_status(ticker: Any, settings: Any) -> dict[str, Any]:
    """The scheduler's lifecycle state, from whichever process asks."""
    if ticker is not None and hasattr(ticker, "get_scheduler_status"):
        status = ticker.get_scheduler_status()
    else:
        status = _status_without_ticker(settings)
    owner, holder = schedule_owner(ticker, settings.data_dir)
    status["schedule_owner"] = owner
    status["schedule_held_by"] = holder
    status["schedule_runner"] = (
        describe_current_process() if owner == "this_process" else holder
    )
    status["release_requested_at"] = _pending_request_time(settings.data_dir, status)
    return status


def release_missed_work(
    ticker: Any,
    settings: Any,
    *,
    requested_by: str,
    wait_seconds: Optional[float] = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Release held missed work; the status plus ``release`` and
    ``released_todo_ids``.

    ``release`` is ``released``, ``nothing_held`` or ``requested`` (the
    owner had not picked the request up within the wait; it will on its next
    poll). Raises ``SchedulerControlError`` when no process runs the
    scheduler (409) or the request cannot be recorded (503).
    """
    data_dir = settings.data_dir
    if ticker is not None and getattr(ticker, "owns_schedule", False):
        status = ticker.release_missed_work()
        status["schedule_owner"] = "this_process"
        status["schedule_held_by"] = None
        status["schedule_runner"] = describe_current_process()
        status["release_requested_at"] = None
        return status

    owner, _ = schedule_owner(ticker, data_dir)
    if owner == "none":
        raise SchedulerControlError(
            "No process runs the scheduler for this data directory, so nothing "
            "holds missed work: it is decided when a scheduler starts.",
            409,
        )
    held_ids, paused = _hold(SchedulerStateManager(data_dir).load())
    if not held_ids and not paused:
        status = scheduler_status(ticker, settings)
        status["release"] = "nothing_held"
        status["released_todo_ids"] = []
        return status

    try:
        write_release_request(data_dir, requested_by)
    except OSError as exc:
        raise SchedulerControlError(
            f"Could not record the release request in {data_dir}: {exc}", 503
        ) from exc

    wait = _default_wait(settings) if wait_seconds is None else max(0.0, wait_seconds)
    deadline = monotonic() + wait
    released = False
    while monotonic() < deadline:
        sleep(_RELEASE_WAIT_STEP_SECONDS)
        now_ids, now_paused = _hold(SchedulerStateManager(data_dir).load())
        if not now_ids and not now_paused:
            released = True
            break

    status = scheduler_status(ticker, settings)
    if released:
        status["release"] = "released"
        status["released_todo_ids"] = held_ids
    else:
        status["release"] = "requested"
        status["released_todo_ids"] = []
    return status


def _default_wait(settings: Any) -> float:
    """Two owner polls plus slack, bounded for an HTTP caller."""
    poll = float(getattr(settings, "ticker_poll_interval", 5) or 5)
    return min(15.0, max(3.0, 2 * poll + 2))


def _hold(state: dict[str, Any]) -> tuple[list[str], bool]:
    ids = sorted(str(t) for t in state.get("pending_missed_todo_ids") or [] if t)
    return ids, bool(state.get("trigger_catchup_paused"))


def _pending_request_time(data_dir: Path, status: dict[str, Any]) -> Optional[str]:
    """When a release request the owner has not yet honored was made, or None.

    A request counts only against a hold detected BEFORE it, and not at all
    against a hold of unknown age (the rule the owner applies,
    ``Ticker._honor_release_request``).
    """
    if not (status.get("pending_missed_todo_ids") or status.get("trigger_catchup_paused")):
        return None
    request = read_release_request(data_dir)
    if request is None:
        return None
    detected = parse_state_time(status.get("last_missed_detection_at"))
    if detected is None or request.requested_at <= detected:
        return None
    return request.requested_at.isoformat(timespec="seconds")


def _status_without_ticker(settings: Any) -> dict[str, Any]:
    """Status for a process with no ticker (the Docker API): the hold and the
    lifecycle stamps come from the shared state file."""
    from .todo_schedule_db import TodoScheduleDB

    schedule_db = TodoScheduleDB(settings.data_dir / "todo_schedule.db")
    state = SchedulerStateManager(settings.data_dir).load()
    pending_ids, paused = _hold(state)
    pending_entries = []
    for todo_id in pending_ids:
        entry = schedule_db.get_entry(todo_id)
        if entry is None:
            continue
        pending_entries.append(
            {
                "todo_id": entry.todo_id,
                "user_id": entry.user_id,
                "thread_id": entry.thread_id,
                "scheduled_for": datetime.fromtimestamp(
                    entry.scheduled_for,
                    timezone.utc,
                ).isoformat(),
                "task_preview": entry.task_preview,
            }
        )

    return {
        "status": "ok",
        "ticker_running": False,
        "owns_schedule": False,
        "schedule_held_by": None,
        "missed_work_policy": getattr(settings, "scheduler_missed_work_policy", "run"),
        "pending_missed_todo_count": len(pending_ids),
        "pending_missed_todo_ids": pending_ids,
        "pending_missed_todos": pending_entries,
        "trigger_catchup_paused": paused,
        "last_started_at": state.get("last_started_at"),
        "last_clean_shutdown_at": state.get("last_clean_shutdown_at"),
        "last_missed_detection_at": state.get("last_missed_detection_at"),
        "active_execution_count": schedule_db.count_active_executions(),
        "active_execution_stale_minutes": int(
            getattr(settings, "scheduler_active_execution_stale_minutes", 1440)
        ),
    }
