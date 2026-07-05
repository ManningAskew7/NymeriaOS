"""Thin watchdog worker that nudges Nymeria about stale TODOs via the REST API.

This replaces the in-process Watchdog that used to live inside NymeriaAgent.
The worker owns no LLM, no graph, and no TodoManager — it just polls the API
for TODOs, applies the staleness filter, and POSTs nudges to /chat with
is_self_invoke=true so the API routes them through the autonomous prompt path.

All event publishing (task_started/task_completed, tool calls, response) is
handled by the API handler automatically when is_self_invoke=true, so this
worker only needs to care about scheduling and bookkeeping. Off-frontend
alerts are likewise routed through the API (POST /notifications/external),
so the worker never needs the master secrets key.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..core.service_health import HEARTBEAT_INTERVAL_SECONDS, write_service_heartbeat
from ..config.settings import Settings
from .api_client import NymeriaAPIClient

logger = logging.getLogger(__name__)

# Give the API a moment to finish booting before the first poll.
STARTUP_DELAY_SECONDS = 5

# TODO task text is truncated to this many characters in nudge messages
# and external alerts.
TASK_PREVIEW_CHARS = 80

# TODOs from before per-thread scoping carry no thread_id; group them under
# this bucket so they still nudge one deterministic thread.
FALLBACK_THREAD_ID = "legacy"

# A failed nudge turn (transport error, or an error event with no response)
# retries on the next cycle. After this many consecutive failures the TODO is
# marked nudged anyway, so a deterministically broken thread does not burn an
# LLM turn plus an error notification every cycle forever. Activity on the
# TODO (updated_at advancing) re-arms it as usual.
MAX_NUDGE_FAILURES = 3


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO8601 datetime; return tz-aware UTC."""
    if not value:
        return None
    try:
        # fromisoformat handles '+00:00' and naive strings; normalise to tz-aware UTC
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class WatchdogWorker:
    """Async watchdog that polls the Nymeria API and triggers nudges."""

    def __init__(
        self,
        client: NymeriaAPIClient,
        settings: Settings,
    ):
        self.client = client
        self.settings = settings
        self.interval_minutes = max(1, settings.watchdog_interval_minutes)
        self.staleness_minutes = max(1, settings.todo_staleness_minutes)
        self._stop = asyncio.Event()

        # In-memory nudge state. Clears naturally on process restart; the
        # API is the source of truth for TODO state.
        self._nudged: Dict[Tuple[str, str], float] = {}
        self._timestamps: Dict[Tuple[str, str], datetime] = {}
        self._nudge_failures: Dict[Tuple[str, str], int] = {}

        # Track in-flight per-thread nudges so we don't pile up
        self._in_flight: set[str] = set()

        # Lifetime delivery counters, surfaced via the health heartbeat so
        # operators can see whether external notifications actually deliver.
        self._nudges_sent = 0
        self._notifications_sent = 0
        self._notification_failures = 0

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main poll loop. Exits cleanly when stop() is called."""
        logger.info(
            "Watchdog worker started: interval=%dm, staleness=%dm",
            self.interval_minutes,
            self.staleness_minutes,
        )
        health_task = asyncio.create_task(self._health_heartbeat_loop())
        try:
            # Short initial delay to let the API finish booting
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=STARTUP_DELAY_SECONDS
                )
            except asyncio.TimeoutError:
                pass  # startup delay elapsed without stop(); begin polling

            while not self._stop.is_set():
                try:
                    await self._check_cycle()
                except Exception as e:
                    logger.error("Watchdog cycle failed: %s", e, exc_info=True)

                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.interval_minutes * 60
                    )
                except asyncio.TimeoutError:
                    pass  # poll interval elapsed; run the next cycle
        finally:
            health_task.cancel()
            await asyncio.gather(health_task, return_exceptions=True)

        logger.info("Watchdog worker stopped")

    async def _health_heartbeat_loop(self) -> None:
        """Publish watchdog health while the API is reachable."""
        while not self._stop.is_set():
            api_ok = await self.client.health()
            write_service_heartbeat(
                "watchdog",
                status="ok" if api_ok else "unhealthy",
                details={
                    "api_ok": api_ok,
                    "interval_minutes": self.interval_minutes,
                    "staleness_minutes": self.staleness_minutes,
                    "in_flight": len(self._in_flight),
                    "nudges_sent": self._nudges_sent,
                    "notifications_sent": self._notifications_sent,
                    "notification_failures": self._notification_failures,
                    "tracked_todos": len(self._timestamps),
                },
            )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=HEARTBEAT_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass  # Expected heartbeat tick; publish health again unless stopped.

    def stop(self) -> None:
        self._stop.set()

    # ── Kill switches ─────────────────────────────────────────────────────

    def _disabled(self) -> bool:
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

    def _is_stale(self, todo: Dict[str, Any]) -> bool:
        """Minutes-based staleness check with the same rules as the old watchdog.

        Skips TODOs that are done, scheduled for the future, or recurring.
        """
        status = (todo.get("status") or "").lower()
        if status == "done":
            return False

        if todo.get("recurrence"):
            return False

        now = datetime.now(timezone.utc)

        scheduled = _parse_iso(todo.get("scheduled_for"))
        if scheduled and scheduled > now:
            return False

        updated = _parse_iso(todo.get("updated_at"))
        if not updated:
            return False

        threshold = now - timedelta(minutes=self.staleness_minutes)
        return updated < threshold

    # ── Cycle ─────────────────────────────────────────────────────────────

    async def _check_cycle(self) -> None:
        if self._disabled():
            logger.debug("Watchdog cycle skipped: kill switch active")
            return

        try:
            users = await self.client.list_users_with_todos()
        except httpx.HTTPError as e:
            logger.error("Failed to list users with todos: %s", e)
            return

        if not users:
            return

        for user_id in users:
            try:
                await self._check_user(user_id)
            except Exception as e:
                logger.error("Watchdog user %s failed: %s", user_id, e)

    async def _check_user(self, user_id: str) -> None:
        try:
            items = await self.client.list_todos(user_id)
        except httpx.HTTPError as e:
            logger.warning("Failed to fetch TODOs for %s: %s", user_id, e)
            return

        # Prune nudge/timestamp state for this user's TODOs that no longer
        # exist (deleted or archived), so a long-lived process does not
        # accumulate dead keys. Only safe after a successful listing.
        live_keys = {(user_id, todo.get("id", "")) for todo in items}
        for state in (self._nudged, self._timestamps, self._nudge_failures):
            dead = [k for k in state if k[0] == user_id and k not in live_keys]
            for key in dead:
                del state[key]

        # Build the list of stale TODOs the user hasn't been nudged about yet.
        stale_by_thread: Dict[str, List[Dict[str, Any]]] = {}
        for todo in items:
            if not self._is_stale(todo):
                continue

            todo_id = todo.get("id", "")
            key = (user_id, todo_id)
            updated_at = _parse_iso(todo.get("updated_at"))

            # Clear nudge state when updated_at advances (replaces the old
            # in-process clear_nudge_tracking callbacks — purely timestamp-driven).
            last_seen = self._timestamps.get(key)
            if updated_at and (last_seen is None or updated_at > last_seen):
                self._nudged.pop(key, None)
                self._nudge_failures.pop(key, None)
            if updated_at:
                self._timestamps[key] = updated_at

            if key in self._nudged:
                continue

            thread_id = todo.get("thread_id") or FALLBACK_THREAD_ID
            stale_by_thread.setdefault(thread_id, []).append(todo)

        if not stale_by_thread:
            return

        # Fire nudges concurrently per thread. asyncio.gather keeps failures isolated.
        await asyncio.gather(
            *(self._nudge_thread(user_id, tid, todos) for tid, todos in stale_by_thread.items()),
            return_exceptions=True,
        )

    # ── Nudge ─────────────────────────────────────────────────────────────

    async def _nudge_thread(
        self, user_id: str, thread_id: str, stale_todos: List[Dict[str, Any]]
    ) -> None:
        if thread_id in self._in_flight:
            logger.debug("Skipping nudge for thread %s: already in flight", thread_id)
            return
        self._in_flight.add(thread_id)

        try:
            message = self._build_nudge_message(stale_todos)
            logger.info(
                "[WATCHDOG] nudging thread=%s user=%s stale=%d",
                thread_id,
                user_id,
                len(stale_todos),
            )

            had_response = False
            had_error = False
            try:
                async for chunk in self.client.chat_stream(
                    message=message,
                    thread_id=thread_id,
                    user_id=user_id,
                    is_self_invoke=True,
                    trigger_override="watchdog",
                    source="watchdog",
                    source_label="watchdog",
                ):
                    ctype = chunk.get("type")
                    if ctype == "response":
                        had_response = True
                    elif ctype == "error":
                        had_error = True
                        logger.error(
                            "[WATCHDOG] API error for thread %s: %s",
                            thread_id,
                            chunk.get("content"),
                        )
                    elif ctype == "done":
                        break
            except httpx.HTTPError as e:
                # Transport failure: the nudge never reached the agent.
                # Leave the TODOs unmarked so the next cycle retries.
                logger.error("[WATCHDOG] /chat failed for thread %s: %s", thread_id, e)
                self._record_nudge_failures(user_id, stale_todos)
                return

            if had_error and not had_response:
                # The turn errored before the agent produced anything; treat
                # it as undelivered and retry next cycle.
                logger.warning(
                    "[WATCHDOG] thread=%s nudge turn errored with no response; "
                    "will retry next cycle",
                    thread_id,
                )
                self._record_nudge_failures(user_id, stale_todos)
                return

            # Mark as nudged: the turn completed (or at least produced a
            # response before erroring). Nudge state clears when updated_at
            # advances, so the TODO becomes eligible again after activity.
            now = datetime.now(timezone.utc).timestamp()
            for todo in stale_todos:
                key = (user_id, todo.get("id", ""))
                self._nudged[key] = now
                self._nudge_failures.pop(key, None)
            self._nudges_sent += 1

            # Best-effort off-frontend alert via the user's "default"
            # notification profile, routed through the API (the master
            # secrets key holder) so it delivers in every runtime shape.
            await self._send_external_notifications(user_id, thread_id, stale_todos)

            logger.info(
                "[WATCHDOG] thread=%s nudge complete (response=%s)",
                thread_id,
                had_response,
            )
        finally:
            self._in_flight.discard(thread_id)

    def _record_nudge_failures(
        self, user_id: str, stale_todos: List[Dict[str, Any]]
    ) -> None:
        """Count a failed nudge attempt; give up after MAX_NUDGE_FAILURES.

        Giving up means marking the TODO nudged without a delivered turn, the
        pre-retry behavior: it stays quiet until its updated_at advances (or
        it disappears), instead of burning an errored LLM turn every cycle.
        """
        now = datetime.now(timezone.utc).timestamp()
        for todo in stale_todos:
            key = (user_id, todo.get("id", ""))
            count = self._nudge_failures.get(key, 0) + 1
            if count >= MAX_NUDGE_FAILURES:
                self._nudged[key] = now
                self._nudge_failures.pop(key, None)
                logger.warning(
                    "[WATCHDOG] giving up on todo %s after %d failed nudge "
                    "attempts; suppressed until it is updated",
                    todo.get("id", ""),
                    count,
                )
            else:
                self._nudge_failures[key] = count

    def _build_nudge_message(self, stale_todos: List[Dict[str, Any]]) -> str:
        lines = [
            f"[WATCHDOG ALERT] The following TODO(s) on this thread have not been "
            f"updated in over {self.staleness_minutes} minutes and need your attention:",
            "",
        ]

        now = datetime.now(timezone.utc)
        for todo in stale_todos:
            updated = _parse_iso(todo.get("updated_at"))
            elapsed = (now - updated).total_seconds() / 60 if updated else 0
            status = (todo.get("status") or "pending").lower()
            icon = "[>]" if status == "in_progress" else "[ ]"
            task = (todo.get("task") or "")[:TASK_PREVIEW_CHARS]
            lines.append(f"  {icon} [{todo.get('id', '')}] {task} (stale for {elapsed:.0f}min)")

        lines.append("")
        lines.append(
            "For each TODO above, please do one of the following:\n"
            "- If complete: mark it done using the nym_todo tool.\n"
            "- If still in progress: continue working on it, or update its notes/status.\n"
            "- If no longer needed: delete it with nym_todo_delete."
        )
        return "\n".join(lines)

    async def _send_external_notifications(
        self, user_id: str, thread_id: str, stale_todos: List[Dict[str, Any]]
    ) -> None:
        """Best-effort off-frontend delivery of the stale-TODO alert.

        Routed through ``POST /notifications/external`` so the API (the
        master secrets key holder) decrypts destination secrets and
        dispatches via the user's default notification profile. The worker
        stays a thin client with no vault access, and delivery works in
        both runtime shapes (slim in-process and the thin Docker container).
        """
        msg = (
            f"[Nymeria Watchdog] {len(stale_todos)} TODO(s) stale "
            f"(no update for {self.staleness_minutes}m+) on thread {thread_id}:\n"
            + "\n".join(
                f"- {(t.get('task') or '')[:TASK_PREVIEW_CHARS]}"
                for t in stale_todos
            )
        )

        try:
            delivered = await self.client.send_external_notification(
                user_id, msg, thread_id=thread_id,
            )
        except Exception as e:
            # Deliberately broad: a notification failure must never break
            # the nudge loop; the nudge itself already reached the agent.
            self._notification_failures += 1
            logger.warning(
                "[WATCHDOG] external notification failed for thread %s: %s",
                thread_id,
                e,
            )
            return

        self._notifications_sent += len(delivered)
        for name in delivered:
            logger.info("[WATCHDOG] notification sent to %s", name)
