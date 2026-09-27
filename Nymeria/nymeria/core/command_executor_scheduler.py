"""The ``/scheduler`` command family (backlog #410, #398).

House style: backend handler families live in their own domain mixin module
(see ``command_executor_aliases.py``). ``_CommandExecutor`` inherits
:class:`SchedulerCommandsMixin`, which owns the family:

- ``/scheduler`` and ``/scheduler status``: which process runs the schedule
  (exactly one per data dir, #397: the Docker worker, the slim service, or a
  fat CLI that started while its service was down), the missed-work policy
  and anything it holds.
- ``/scheduler release``: run the missed work ``ask`` holds, from any process
  (the owner picks up a relayed request on its next poll).

Both read through ``core/scheduler_control.py`` (in process) or the two REST
routes over it (HTTP client), so every surface gets the same answer. Admin
only, gated at dispatch.

Runtime leaf: imports ``command_forms``/``command_params`` and ``httpx`` only,
never ``command_service`` at module scope.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from .command_forms import CommandOutput, command_error, command_info, command_success
from .command_params import BoundArgs

# Held TODOs listed by name before the rest are counted.
_HELD_PREVIEW_LIMIT = 5


def _when(value: Any) -> str:
    """An ISO stamp as ``YYYY-MM-DD HH:MM UTC``; anything else verbatim."""
    if not isinstance(value, str) or not value:
        return "unknown time"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _runs_in(status: dict[str, Any]) -> str:
    owner = status.get("schedule_owner")
    runner = status.get("schedule_runner") or "another process"
    if owner == "this_process":
        return f"{runner} (the process answering this command)"
    if owner == "another_process":
        return str(runner)
    if owner == "none":
        return (
            "no process right now: no scheduled TODOs, trigger polls or sweeps "
            "run for this data directory until a scheduler starts"
        )
    return "unknown: the scheduler lock cannot be tested from this process"


def _held_ids(status: dict[str, Any]) -> list[str]:
    return [str(t) for t in status.get("pending_missed_todo_ids") or [] if t]


def render_scheduler_status(status: dict[str, Any]) -> str:
    """The `/scheduler` readout (plain text, safe on every surface)."""
    policy = status.get("missed_work_policy") or "run"
    policy_text = (
        "ask (missed work waits for an admin's release)"
        if policy == "ask"
        else "run (missed work runs as soon as the scheduler starts)"
    )
    held = _held_ids(status)
    paused = bool(status.get("trigger_catchup_paused"))
    if held:
        held_text = (
            f"{len(held)} TODO(s) missed while the scheduler was down, held "
            f"since {_when(status.get('last_missed_detection_at'))}"
        )
    elif paused:
        held_text = "no TODOs, but trigger polling waits for a release"
    else:
        held_text = "nothing"
    rows: list[tuple[str, str]] = [
        ("Runs in", _runs_in(status)),
        ("Missed work", policy_text),
        ("Held", held_text),
    ]
    if held and paused:
        rows.append(("Triggers", "polling paused until the release"))
    requested_at = status.get("release_requested_at")
    if requested_at:
        rows.append(
            ("Release", f"requested {_when(requested_at)}; waiting for the scheduler's next poll")
        )
    active = status.get("active_execution_count")
    if isinstance(active, int):
        rows.append(("Running now", f"{active} scheduled run(s)"))

    width = max(len(label) for label, _ in rows)
    lines = ["Scheduler", ""]
    lines.extend(f"  {label:<{width}}  {value}" for label, value in rows)

    previews = {
        str(entry.get("todo_id")): entry
        for entry in status.get("pending_missed_todos") or []
        if isinstance(entry, dict)
    }
    if held:
        lines.append("")
        for todo_id in held[:_HELD_PREVIEW_LIMIT]:
            entry = previews.get(todo_id) or {}
            task = str(entry.get("task_preview") or "").strip()[:60]
            owner = entry.get("user_id")
            suffix = f" ({owner})" if owner else ""
            lines.append(f"  - {todo_id}{suffix} {task}".rstrip())
        if len(held) > _HELD_PREVIEW_LIMIT:
            lines.append(f"  ... and {len(held) - _HELD_PREVIEW_LIMIT} more")
    if (held or paused) and not requested_at:
        lines.append("")
        lines.append("Run /scheduler release to run the held work now.")
    return "\n".join(lines)


class SchedulerCommandsMixin:
    """/scheduler command bodies mixed into ``_CommandExecutor``.

    The host provides ``api`` and ``user_id``; the annotations let the
    static checker see them on the mixin in isolation.
    """

    api: Any
    user_id: str

    async def _cmd_scheduler(self, bound: BoundArgs) -> str | CommandOutput:
        return await self._cmd_scheduler_status(BoundArgs())

    async def _cmd_scheduler_status(self, bound: BoundArgs) -> str | CommandOutput:
        try:
            status = await self.api.get_scheduler_status(user_id=self.user_id)
        except httpx.HTTPStatusError as exc:
            from .command_service import http_error_detail

            return command_error(f"Could not read the scheduler status: {http_error_detail(exc)}")
        return command_info(render_scheduler_status(status), data={"scheduler": status})

    async def _cmd_scheduler_release(self, bound: BoundArgs) -> str | CommandOutput:
        try:
            result = await self.api.release_missed_work(user_id=self.user_id)
        except httpx.HTTPStatusError as exc:
            from .command_service import http_error_detail

            return command_error(http_error_detail(exc))
        outcome = result.get("release")
        released = [str(t) for t in result.get("released_todo_ids") or [] if t]
        if outcome == "released":
            if released:
                text = (
                    f"Released {len(released)} held TODO(s): {', '.join(released)}. "
                    "They run now, and trigger polling has resumed."
                )
            else:
                text = "Released: trigger polling has resumed."
            return command_success(text, data={"scheduler": result})
        if outcome == "requested":
            runner = result.get("schedule_runner") or "the scheduler process"
            return command_info(
                f"Release requested. The scheduler in {runner} picks it up on "
                "its next poll; /scheduler shows when it has. If that scheduler "
                "restarts first, its new hold needs a new release.",
                data={"scheduler": result},
            )
        if outcome == "nothing_held":
            return command_info(
                "Nothing is held: no missed work is waiting for a release.",
                data={"scheduler": result},
            )
        return command_error(
            "This process does not run the scheduler, and could not pass the "
            "release on to the one that does."
        )
