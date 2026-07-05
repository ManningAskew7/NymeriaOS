"""Stale-TODO watchdog sweep, driven by the Ticker as a supervisory sub-loop.

This is the fold of the former standalone watchdog process
(``triggers/watchdog_worker.py``, backlog #78): the same staleness filter,
nudge dedupe, retry cap, and off-frontend alerting, running inside the one
process that already hosts the scheduler. In slim that is the API process
(the agent's in-process Ticker); in Docker it is the worker container's
Ticker. Both already hold everything the sweep needs:

- TODO listings come straight from ``TodoManager`` (shared data dir) instead
  of polling ``GET /todos`` over HTTP.
- Nudge turns fire through the Ticker's ``TurnExecutor`` seam (local agent in
  slim, ``/chat`` relay in Docker), the same path scheduled TODOs and
  triggers use.
- Off-frontend alerts dispatch directly via
  ``notification_dispatch.send_external_notifications`` (both runtimes hold
  the master secrets key), so no shape-specific transport is needed.

Because the executor is invoked with ``publish_autonomous_events=False``
semantics (Docker) or bypasses the API router entirely (slim), the sweep
publishes its own autonomous bookends and chunk fan-out with the same task-id
shape the API used for watchdog turns (``watchdog-<thread_id>``), so
``/autonomous/stream`` consumers see identical events.

The supervisor-vs-scheduler distinction survives as a loop boundary: the
sweep keeps its own interval (``WATCHDOG_INTERVAL_MINUTES``), its own enable
flag (``WATCHDOG_ENABLED``), and both kill switches
(``NYMERIA_WATCHDOG_DISABLED`` env, re-read every cycle, and the
``{data_dir}/flags/watchdog-off`` flag file).
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .event_bus import publish_agent_stream_chunk, publish_autonomous_event
from .notification_dispatch import (
    create_autonomous_notification,
    send_external_notifications,
    should_notify_autonomous,
)
from .pending_prompt_queue import PENDING_QUEUE_META_EVENT_TYPES
from .stream_bridge import stream_and_collect
from .time_utils import ensure_aware_utc
from .todo_manager import TodoItem, TodoManager, TodoStatus

if TYPE_CHECKING:
    from ..config.settings import Settings
    from .thread_config import ThreadConfigManager
    from .turn_executor import TurnExecutor

logger = logging.getLogger(__name__)

# TODO task text is truncated to this many characters in nudge messages
# and external alerts.
TASK_PREVIEW_CHARS = 80

# TODOs from before per-thread scoping carry no thread_id; group them under
# this bucket so they still nudge one deterministic thread. (TodoItem also
# backfills "legacy" at parse time; this is the belt to that suspenders.)
FALLBACK_THREAD_ID = "legacy"

# A failed nudge turn (transport error, or an error before any response)
# retries on the next cycle. After this many consecutive failures the TODO is
# marked nudged anyway, so a deterministically broken thread does not burn an
# LLM turn plus an error notification every cycle forever. Activity on the
# TODO (updated_at advancing) re-arms it as usual.
MAX_NUDGE_FAILURES = 3


class WatchdogSweep:
    """Detect stale TODOs and nudge their threads through the turn executor."""

    def __init__(
        self,
        *,
        executor: "TurnExecutor",
        settings: "Settings",
        todo_manager: TodoManager,
        thread_config_manager: "ThreadConfigManager",
    ) -> None:
        self._turn_executor = executor
        self.settings = settings
        self.todo_manager = todo_manager
        self.thread_config_manager = thread_config_manager
        self.interval_minutes = max(1, settings.watchdog_interval_minutes)
        self.staleness_minutes = max(1, settings.todo_staleness_minutes)

        # In-memory nudge state. Clears naturally on process restart; the
        # TODO files are the source of truth for TODO state. Guarded by
        # _state_lock: detection iterates these on the ticker's housekeeping
        # thread while nudge turns mutate them on the autonomous worker pool
        # (the old standalone watchdog was single-threaded, so the fold is
        # what introduced the cross-thread access).
        self._state_lock = threading.Lock()
        self._nudged: Dict[Tuple[str, str], float] = {}
        self._timestamps: Dict[Tuple[str, str], datetime] = {}
        self._nudge_failures: Dict[Tuple[str, str], int] = {}

        # Track in-flight per-thread nudges so cycles don't pile turns onto a
        # thread that is still being nudged. Guarded by a lock because nudges
        # run concurrently on the ticker's autonomous worker pool.
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()

        # Lifetime delivery counters, surfaced via the worker heartbeat so
        # operators can see whether external notifications actually deliver.
        # Guarded by _state_lock (incremented from concurrent nudge threads).
        self._nudges_sent = 0
        self._notifications_sent = 0
        self._notification_failures = 0

    # ── Kill switches ─────────────────────────────────────────────────────

    def disabled(self) -> bool:
        """Operator kill switches, re-read every cycle."""
        env = os.environ.get("NYMERIA_WATCHDOG_DISABLED", "").strip().lower()
        if env in ("1", "true", "yes"):
            return True
        try:
            flag = self.settings.data_dir / "flags" / "watchdog-off"
            if flag.exists():
                return True
        except Exception:
            logger.error("Failed to check watchdog kill-switch flag", exc_info=True)
        return False

    # ── Staleness ─────────────────────────────────────────────────────────

    def _is_stale(self, todo: TodoItem) -> bool:
        """Minutes-based staleness check, same rules as the old watchdog.

        Skips TODOs that are done, scheduled for the future, or recurring.
        """
        if not todo.is_active():
            return False

        if todo.recurrence:
            return False

        now = datetime.now(timezone.utc)

        if todo.scheduled_for is not None:
            if ensure_aware_utc(todo.scheduled_for) > now:
                return False

        threshold = now - timedelta(minutes=self.staleness_minutes)
        return ensure_aware_utc(todo.updated_at) < threshold

    # ── Cycle ─────────────────────────────────────────────────────────────

    def run_cycle(self, worker_pool: Optional[ThreadPoolExecutor] = None) -> None:
        """One detection pass; nudge turns are submitted to *worker_pool*.

        With ``worker_pool=None`` nudges run inline (tests, shutdown edge).
        """
        if self.disabled():
            logger.debug("Watchdog sweep skipped: kill switch active")
            return

        try:
            users = self.todo_manager.get_all_users_with_todos()
        except Exception as e:
            logger.error("[WATCHDOG] failed to list users with todos: %s", e)
            return

        for user_id in users:
            try:
                self._check_user(user_id, worker_pool)
            except Exception as e:
                logger.error("[WATCHDOG] sweep for user %s failed: %s", user_id, e)

    def _check_user(
        self, user_id: str, worker_pool: Optional[ThreadPoolExecutor]
    ) -> None:
        items = self.todo_manager.get_todos(user_id).items

        stale_by_thread: Dict[str, List[TodoItem]] = {}
        with self._state_lock:
            # Prune nudge/timestamp state for this user's TODOs that no
            # longer exist (deleted or archived), so a long-lived process
            # does not accumulate dead keys. Only safe after a successful
            # listing. Under the lock: nudge turns from a previous cycle may
            # still be mutating these dicts on the autonomous pool.
            live_keys = {(user_id, todo.id) for todo in items}
            for state in (self._nudged, self._timestamps, self._nudge_failures):
                dead = [
                    k for k in state if k[0] == user_id and k not in live_keys
                ]
                for key in dead:
                    del state[key]

            # Build the list of stale TODOs not yet nudged.
            for todo in items:
                if not self._is_stale(todo):
                    continue

                key = (user_id, todo.id)
                updated_at = ensure_aware_utc(todo.updated_at)

                # Clear nudge state when updated_at advances (purely
                # timestamp-driven, same as the standalone watchdog).
                last_seen = self._timestamps.get(key)
                if last_seen is None or updated_at > last_seen:
                    self._nudged.pop(key, None)
                    self._nudge_failures.pop(key, None)
                self._timestamps[key] = updated_at

                if key in self._nudged:
                    continue

                thread_id = todo.thread_id or FALLBACK_THREAD_ID
                stale_by_thread.setdefault(thread_id, []).append(todo)

        if not stale_by_thread:
            return

        for thread_id, todos in stale_by_thread.items():
            if worker_pool is not None:
                try:
                    worker_pool.submit(self._nudge_thread, user_id, thread_id, todos)
                    continue
                except Exception as e:
                    logger.debug("[WATCHDOG] nudge submit fell back inline: %s", e)
            self._nudge_thread(user_id, thread_id, todos)

    # ── Nudge ─────────────────────────────────────────────────────────────

    def _nudge_thread(
        self, user_id: str, thread_id: str, stale_todos: List[TodoItem]
    ) -> None:
        with self._in_flight_lock:
            if thread_id in self._in_flight:
                logger.debug(
                    "Skipping nudge for thread %s: already in flight", thread_id
                )
                return
            self._in_flight.add(thread_id)

        try:
            self._nudge_thread_locked(user_id, thread_id, stale_todos)
        except Exception:
            # Isolation backstop: the pool would swallow this otherwise.
            logger.error(
                "[WATCHDOG] nudge for thread %s crashed", thread_id, exc_info=True
            )
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(thread_id)

    def _nudge_thread_locked(
        self, user_id: str, thread_id: str, stale_todos: List[TodoItem]
    ) -> None:
        # Re-check the nudged markers at execution time: if the autonomous
        # pool stayed saturated past one interval, detection can queue a
        # second nudge for the same TODOs while the first is still pending,
        # and the in-flight guard only covers temporal overlap.
        with self._state_lock:
            stale_todos = [
                t for t in stale_todos if (user_id, t.id) not in self._nudged
            ]
        if not stale_todos:
            logger.debug(
                "Skipping nudge for thread %s: all todos nudged meanwhile",
                thread_id,
            )
            return

        message = self._build_nudge_message(stale_todos)
        task_id = f"watchdog-{thread_id}"
        logger.info(
            "[WATCHDOG] nudging thread=%s user=%s stale=%d",
            thread_id,
            user_id,
            len(stale_todos),
        )

        # The sweep publishes its own autonomous bookends and chunk fan-out
        # (the executor path does not: slim bypasses the API router, Docker
        # relays with publish_autonomous_events=False). Payload shapes match
        # what the API published for watchdog /chat turns.
        started = False
        had_response = False
        saw_error_chunk = False
        response_parts: List[str] = []

        def on_chunk(chunk: Dict[str, Any], _collection: Any) -> None:
            nonlocal started, had_response, saw_error_chunk
            ctype = chunk.get("type")
            # Defer task_started until the turn owns the thread lock and a
            # real content event arrives; queue-meta events signal queue
            # transitions, not the start of model work.
            if not started and ctype not in PENDING_QUEUE_META_EVENT_TYPES:
                publish_autonomous_event(
                    event_type="task_started",
                    thread_id=thread_id,
                    user_id=user_id,
                    task_id=task_id,
                    data={"prompt": message, "source": "watchdog"},
                )
                started = True
            if ctype == "response":
                had_response = True
                content = chunk.get("content", "")
                if content:
                    response_parts.append(content)
            elif ctype == "error":
                saw_error_chunk = True
                logger.error(
                    "[WATCHDOG] stream error for thread %s: %s",
                    thread_id,
                    chunk.get("content"),
                )
            publish_agent_stream_chunk(
                chunk, thread_id=thread_id, user_id=user_id, task_id=task_id
            )

        completion_error = False
        try:
            stream_result = stream_and_collect(
                self._turn_executor,
                astream_kwargs={
                    "message": message,
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "_is_self_invoke": True,
                    "_trigger_override": "watchdog",
                    "source": "watchdog",
                    "source_label": "watchdog",
                },
                on_chunk=on_chunk,
            )
        except Exception as e:
            # Transport failure or an error event with no response yet: the
            # nudge never reached the agent (or died before producing
            # anything). Leave the TODOs unmarked so the next cycle retries,
            # up to MAX_NUDGE_FAILURES. A turn that errored AFTER producing a
            # response still counts as delivered, matching the standalone
            # watchdog's had_response semantics.
            if not had_response:
                logger.warning(
                    "[WATCHDOG] thread=%s nudge turn failed with no response; "
                    "will retry next cycle: %s",
                    thread_id,
                    e,
                )
                self._record_nudge_failures(user_id, stale_todos)
                self._publish_completion(
                    user_id, thread_id, task_id, str(e), started=started, error=True
                )
                return
            logger.warning(
                "[WATCHDOG] thread=%s nudge errored after a response; "
                "treating as delivered: %s",
                thread_id,
                e,
            )
            if saw_error_chunk:
                # In-band error chunk after a response: the API's finally
                # block published the accumulated content as a normal
                # completion, so keep the partial response.
                response_text = "".join(response_parts)
            else:
                # Transport failure after a response: the API's exception
                # handler published an error bookend carrying the error
                # text; match it. The nudge still counts as delivered.
                response_text = str(e)
                completion_error = True
        else:
            response_text = stream_result.response_text(fallback_to_thinking=False)

        # Mark as nudged: the turn completed (or at least produced a response
        # before erroring). Nudge state clears when updated_at advances, so
        # the TODO becomes eligible again after activity.
        now = datetime.now(timezone.utc).timestamp()
        with self._state_lock:
            for todo in stale_todos:
                key = (user_id, todo.id)
                self._nudged[key] = now
                self._nudge_failures.pop(key, None)
            self._nudges_sent += 1

        # Best-effort off-frontend alert via the user's "default"
        # notification profile. Both runtimes hold the master secrets key,
        # so this dispatches in-process in every shape.
        self._send_external_notifications(user_id, thread_id, stale_todos)

        self._publish_completion(
            user_id,
            thread_id,
            task_id,
            response_text,
            started=started,
            error=completion_error,
        )

        logger.info(
            "[WATCHDOG] thread=%s nudge complete (response=%s)",
            thread_id,
            had_response,
        )

    def _publish_completion(
        self,
        user_id: str,
        thread_id: str,
        task_id: str,
        content: str,
        *,
        started: bool,
        error: bool,
    ) -> None:
        """Mirror the API's task_completed bookend + autonomous notification."""
        if not started:
            return
        try:
            data: Dict[str, Any] = {
                "content": content,
                "notify": should_notify_autonomous(
                    thread_id, self.thread_config_manager
                ),
                "source": "watchdog",
            }
            if error:
                data["error"] = True
            publish_autonomous_event(
                event_type="task_completed",
                thread_id=thread_id,
                user_id=user_id,
                task_id=task_id,
                data=data,
            )
            create_autonomous_notification(
                user_id=user_id,
                thread_id=thread_id,
                task_id=task_id,
                summary=(content or "Watchdog nudge completed")[:200],
                settings=self.settings,
                thread_config_manager=self.thread_config_manager,
            )
        except Exception:
            logger.warning(
                "[WATCHDOG] completion publish failed for thread %s",
                thread_id,
                exc_info=True,
            )

    def _record_nudge_failures(
        self, user_id: str, stale_todos: List[TodoItem]
    ) -> None:
        """Count a failed nudge attempt; give up after MAX_NUDGE_FAILURES.

        Giving up means marking the TODO nudged without a delivered turn, the
        pre-retry behavior: it stays quiet until its updated_at advances (or
        it disappears), instead of burning an errored LLM turn every cycle.
        """
        now = datetime.now(timezone.utc).timestamp()
        with self._state_lock:
            for todo in stale_todos:
                key = (user_id, todo.id)
                count = self._nudge_failures.get(key, 0) + 1
                if count >= MAX_NUDGE_FAILURES:
                    self._nudged[key] = now
                    self._nudge_failures.pop(key, None)
                    logger.warning(
                        "[WATCHDOG] giving up on todo %s after %d failed nudge "
                        "attempts; suppressed until it is updated",
                        todo.id,
                        count,
                    )
                else:
                    self._nudge_failures[key] = count

    def _build_nudge_message(self, stale_todos: List[TodoItem]) -> str:
        lines = [
            f"[WATCHDOG ALERT] The following TODO(s) on this thread have not been "
            f"updated in over {self.staleness_minutes} minutes and need your attention:",
            "",
        ]

        now = datetime.now(timezone.utc)
        for todo in stale_todos:
            elapsed = (now - ensure_aware_utc(todo.updated_at)).total_seconds() / 60
            icon = "[>]" if todo.status == TodoStatus.IN_PROGRESS else "[ ]"
            task = (todo.task or "")[:TASK_PREVIEW_CHARS]
            lines.append(f"  {icon} [{todo.id}] {task} (stale for {elapsed:.0f}min)")

        lines.append("")
        lines.append(
            "For each TODO above, please do one of the following:\n"
            "- If complete: mark it done using the nym_todo tool.\n"
            "- If still in progress: continue working on it, or update its notes/status.\n"
            "- If no longer needed: delete it with nym_todo_delete."
        )
        return "\n".join(lines)

    def _send_external_notifications(
        self, user_id: str, thread_id: str, stale_todos: List[TodoItem]
    ) -> None:
        """Best-effort off-frontend delivery of the stale-TODO alert."""
        msg = (
            f"[Nymeria Watchdog] {len(stale_todos)} TODO(s) stale "
            f"(no update for {self.staleness_minutes}m+) on thread {thread_id}:\n"
            + "\n".join(
                f"- {(t.task or '')[:TASK_PREVIEW_CHARS]}" for t in stale_todos
            )
        )

        try:
            delivered = send_external_notifications(
                msg, self.settings, user_id=user_id, thread_id=thread_id,
            )
        except Exception as e:
            # Backstop only: send_external_notifications isolates its own
            # failures (per-destination and wholesale) and returns an empty
            # list, logging the cause itself, so this rarely fires. Kept so
            # an unexpected raise can never break the nudge loop; the nudge
            # itself already reached the agent.
            with self._state_lock:
                self._notification_failures += 1
            logger.warning(
                "[WATCHDOG] external notification failed for thread %s: %s",
                thread_id,
                e,
            )
            return

        with self._state_lock:
            self._notifications_sent += len(delivered)
        for name in delivered:
            logger.info("[WATCHDOG] notification sent to %s", name)

    # ── Introspection ─────────────────────────────────────────────────────

    def stats(self) -> Dict[str, Any]:
        """Counters for the worker heartbeat / operator visibility.

        ``enabled`` reflects construction (WATCHDOG_ENABLED); a live kill
        switch shows as ``kill_switch_active`` so the heartbeat does not read
        "enabled" while every cycle is being skipped.
        """
        with self._in_flight_lock:
            in_flight = len(self._in_flight)
        with self._state_lock:
            return {
                "enabled": True,
                "kill_switch_active": self.disabled(),
                "interval_minutes": self.interval_minutes,
                "staleness_minutes": self.staleness_minutes,
                "in_flight": in_flight,
                "nudges_sent": self._nudges_sent,
                "notifications_sent": self._notifications_sent,
                "notification_failures": self._notification_failures,
                "tracked_todos": len(self._timestamps),
            }
