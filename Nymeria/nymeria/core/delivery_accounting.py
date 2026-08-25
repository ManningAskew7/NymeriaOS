"""Chat-app delivery-outcome accounting for scheduled TODOs (backlog #247).

A scheduled TODO turn can succeed backend-side while every chat-app send of
its output fails (chat id bound to a peer who never started the bot, blocked
bot, platform outage). The ticker's #154 recurring-failure policy never sees
that: it is driven by exceptions raised inside the turn, and the turn
succeeded. This module is the missing half: bots report each TODO turn's
delivery outcome through ``POST /todos/{todo_id}/delivery-report`` and the
policy below turns consecutive undelivered occurrences into the same
owner-alert / auto-pause escalation the execution policy uses.

Scope honesty: this closes the loop only for failures the bot OBSERVES. A
thread with no chat binding files no report at all, and so does a bot
process that is down (the likeliest delivery failure of all); the proactive
setup-time reachability probe for the binding half is backlog #248.

Design notes, mirrored from the ticker's #154 helpers (``ticker.py``):

- The delivery streak (``delivery_failures`` + last-failure fields on
  ``TodoItem``) is PARALLEL to ``consecutive_failures``, never shared: the
  ticker resets the execution streak when the turn finishes, which happens
  before the bot finishes delivering, so a shared field would oscillate and
  never cross a threshold.
- One increment per failed occurrence; a delivered (or partial) report ends
  the episode. Thresholds reuse ``scheduler_failure_alert_after`` /
  ``scheduler_failure_pause_after`` (0 disables, same as #154).
- Unpersisted state must not drive policy: when the streak cannot be saved
  the increment reports 0 and the alert/pause branches no-op.
- Pause uses the exact #154 mechanism (schedule cleared, recurrence kept,
  ``schedule_paused_at`` marker, reason PREPENDED to the notes with the
  ``[auto-paused after`` prefix) so ``update_item``'s resume-clear handles
  both policies with one branch.
- A ONE-SHOT todo has no next occurrence for a streak to accumulate on, so
  a failed delivery alerts the owner immediately.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .notification_dispatch import send_owner_alert
from .todo_manager import PAUSE_NOTE_PREFIX, TodoManager, TodoStatus

logger = logging.getLogger(__name__)

# Outcome vocabulary lives in ONE place: the request schema's Literal
# (api/schemas/todos.py::TodoDeliveryReportRequest.outcome). ``partial``
# (some bubbles landed) counts as delivered for streak purposes; the bot's
# own log line carries the detail.


def record_delivery_report(
    *,
    user_id: str,
    todo_id: str,
    outcome: str,
    platform: str,
    target: str,
    error: Optional[str],
    thread_id: str,
    todo_manager: TodoManager,
    schedule_db: Any,
    settings: Any,
) -> dict:
    """Apply one bot delivery report to the TODO's delivery accounting.

    Returns ``{"outcome", "delivery_failures", "alerted", "paused"}`` for
    the route's response. Never raises for policy/bookkeeping trouble; the
    caller has already validated the todo exists for ``user_id``.
    """
    if outcome != "failed":
        _reset_delivery_streak(todo_manager, user_id, todo_id)
        return {
            "outcome": outcome,
            "delivery_failures": 0,
            "alerted": False,
            "paused": False,
        }

    todo = todo_manager.get_todo_by_id(user_id, todo_id)
    recurring = bool(todo and todo.recurrence)
    count = _record_delivery_failure(
        todo_manager, user_id, todo_id, error or "delivery failed"
    )
    task_text = (todo.task or "") if todo else ""
    where = f"{platform} {target}".strip() if target else platform
    err_text = (error or "unknown error")[:200]

    alerted = False
    paused = False
    if not recurring:
        # One-shot: no streak to accumulate, alert on the (only) failure.
        # Gated on the persisted increment like every other branch:
        # unpersisted state must not drive policy.
        if count:
            send_owner_alert(
                (
                    f"[SCHEDULED TASK NOT DELIVERED] TODO [{todo_id}] "
                    f"\"{task_text[:80]}\" ran, but its output could not be "
                    f"delivered to {where}: {err_text}. The output is saved "
                    f"in the thread's history."
                ),
                settings,
                user_id=user_id,
                thread_id=thread_id,
                task_id=todo_id,
            )
            alerted = True
    else:
        alert_after = max(
            0, int(getattr(settings, "scheduler_failure_alert_after", 2))
        )
        pause_after = max(
            0, int(getattr(settings, "scheduler_failure_pause_after", 5))
        )
        if pause_after and count >= pause_after:
            paused = _pause_undelivered_schedule(
                todo_manager,
                schedule_db,
                user_id,
                todo_id,
                count,
                err_text,
            )
            if paused:
                send_owner_alert(
                    (
                        f"[SCHEDULED TASK PAUSED] Recurring TODO [{todo_id}] "
                        f"\"{task_text[:80]}\" was auto-paused after {count} "
                        f"consecutive runs whose output could not be "
                        f"delivered to {where}. Last error: {err_text}. Its "
                        f"recurrence is kept; reschedule it "
                        f"(/todos schedule {todo_id} ...) to resume."
                    ),
                    settings,
                    user_id=user_id,
                    thread_id=thread_id,
                    task_id=todo_id,
                )
                alerted = True
        elif alert_after and count == alert_after:
            pause_note = (
                f" It auto-pauses after {pause_after} consecutive "
                f"undelivered runs." if pause_after else ""
            )
            send_owner_alert(
                (
                    f"[SCHEDULED TASK ALERT] Recurring TODO [{todo_id}] "
                    f"\"{task_text[:80]}\" keeps running, but its output "
                    f"could not be delivered to {where} for {count} "
                    f"consecutive runs. Last error: {err_text}. The output "
                    f"is saved in the thread's history.{pause_note} Manage "
                    f"it with /todos."
                ),
                settings,
                user_id=user_id,
                thread_id=thread_id,
                task_id=todo_id,
            )
            alerted = True

    return {
        "outcome": outcome,
        "delivery_failures": count,
        "alerted": alerted,
        "paused": paused,
    }


def _record_delivery_failure(
    todo_manager: TodoManager, user_id: str, todo_id: str, error: str
) -> int:
    """Persist one undelivered occurrence; returns the new streak count.

    Returns 0 when nothing persisted (todo vanished, or the save failed):
    unpersisted state must not drive the alert/pause policy.
    """
    count = 0
    try:
        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if item:
                count = item.delivery_failures + 1
                item.delivery_failures = count
                item.last_delivery_failure = error[:300]
                item.last_delivery_failure_at = datetime.now(timezone.utc)
    except Exception:
        logger.warning(
            "Failed to record delivery-failure streak for TODO %s",
            todo_id,
            exc_info=True,
        )
        count = 0
    return count


def _reset_delivery_streak(
    todo_manager: TodoManager, user_id: str, todo_id: str
) -> None:
    """A delivered occurrence ends the undelivered episode."""
    try:
        current = todo_manager.get_todo_by_id(user_id, todo_id)
        if not current or not (
            current.delivery_failures
            or current.last_delivery_failure
            or current.last_delivery_failure_at
        ):
            return
        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if item:
                item.delivery_failures = 0
                item.last_delivery_failure = None
                item.last_delivery_failure_at = None
    except Exception:
        logger.warning(
            "Failed to reset delivery-failure streak for TODO %s",
            todo_id,
            exc_info=True,
        )


def _pause_undelivered_schedule(
    todo_manager: TodoManager,
    schedule_db: Any,
    user_id: str,
    todo_id: str,
    failure_count: int,
    error: str,
) -> bool:
    """Auto-pause exactly as the #154 pause does (see ticker.py).

    Clear the schedule, KEEP recurrence, set the marker, prepend the reason
    to the notes (the ``[auto-paused after`` prefix is what update_item's
    resume-clear strips), and remove the schedule row only AFTER the marker
    persisted.
    """
    try:
        pause_note = (
            f"{PAUSE_NOTE_PREFIX} {failure_count} consecutive undelivered "
            f"runs: {error[:100]}]"
        )
        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if item is None:
                return False
            if item.schedule_paused_at is not None:
                # Already paused (a replayed report, or the #154 execution
                # pause won the race): a second pause would stack a second
                # note prefix (resume strips only one) and fire a second
                # alert. Idempotent no-op instead.
                return False
            existing = (item.notes or "").strip()
            todo_list.update_item(
                todo_id,
                status=TodoStatus.PENDING,
                notes=f"{pause_note} {existing}".strip()[:1000],
                clear_schedule=True,
            )
            item = todo_list.get_item(todo_id)
            if item:
                item.schedule_paused_at = datetime.now(timezone.utc)
        schedule_db.remove_scheduled(todo_id)
        logger.warning(
            "Recurring TODO %s auto-paused after %d consecutive undelivered "
            "runs",
            todo_id,
            failure_count,
        )
        return True
    except Exception:
        logger.error(
            "Failed to auto-pause undelivered TODO %s", todo_id, exc_info=True
        )
        return False
