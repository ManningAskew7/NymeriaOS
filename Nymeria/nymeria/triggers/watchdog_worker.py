"""Thin watchdog worker that nudges Nymeria about stale TODOs via the REST API.

This replaces the in-process Watchdog that used to live inside NymeriaAgent.
The worker owns no LLM, no graph, and no TodoManager — it just polls the API
for TODOs, applies the staleness filter, and POSTs nudges to /chat with
is_self_invoke=true so the API routes them through the autonomous prompt path.

All event publishing (task_started/task_completed, tool calls, response) is
handled by the API handler automatically when is_self_invoke=true, so this
worker only needs to care about scheduling and bookkeeping.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config.settings import Settings
from .discord_api_client import NymeriaAPIClient

logger = logging.getLogger(__name__)


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

        # Track in-flight per-thread nudges so we don't pile up
        self._in_flight: set[str] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Main poll loop. Exits cleanly when stop() is called."""
        logger.info(
            "Watchdog worker started: interval=%dm, staleness=%dm",
            self.interval_minutes,
            self.staleness_minutes,
        )
        # Short initial delay to let the API finish booting
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass

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
                pass

        logger.info("Watchdog worker stopped")

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
            pass
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
            if updated_at:
                self._timestamps[key] = updated_at

            if key in self._nudged:
                continue

            thread_id = todo.get("thread_id") or "legacy"
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
            try:
                async for chunk in self.client.chat_stream(
                    message=message,
                    thread_id=thread_id,
                    user_id=user_id,
                    is_self_invoke=True,
                    trigger_override="watchdog",
                ):
                    ctype = chunk.get("type")
                    if ctype == "response":
                        had_response = True
                    elif ctype == "error":
                        logger.error(
                            "[WATCHDOG] API error for thread %s: %s",
                            thread_id,
                            chunk.get("content"),
                        )
                    elif ctype == "done":
                        break
            except httpx.HTTPError as e:
                logger.error("[WATCHDOG] /chat failed for thread %s: %s", thread_id, e)
                return

            # Mark as nudged regardless of whether the agent produced a response —
            # the attempt was made, and we'll retry naturally if updated_at advances.
            now = datetime.now(timezone.utc).timestamp()
            for todo in stale_todos:
                key = (user_id, todo.get("id", ""))
                self._nudged[key] = now

            # External notifications so the user actually sees it even if they
            # aren't watching the frontend. Reuses the existing stateless helpers.
            self._send_external_notifications(thread_id, stale_todos)

            logger.info(
                "[WATCHDOG] thread=%s nudge complete (response=%s)",
                thread_id,
                had_response,
            )
        finally:
            self._in_flight.discard(thread_id)

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
            task = (todo.get("task") or "")[:80]
            lines.append(f"  {icon} [{todo.get('id', '')}] {task} (stale for {elapsed:.0f}min)")

        lines.append("")
        lines.append(
            "For each TODO above, please do one of the following:\n"
            "- If complete: mark it done using the todo tool.\n"
            "- If still in progress: continue working on it, or update its notes/status.\n"
            "- If no longer needed: delete it."
        )
        return "\n".join(lines)

    def _send_external_notifications(
        self, thread_id: str, stale_todos: List[Dict[str, Any]]
    ) -> None:
        try:
            from ..tools.notify import _send_telegram, _send_discord, _send_slack
        except Exception as e:
            logger.debug("Notify helpers unavailable: %s", e)
            return

        msg = (
            f"[Nymeria Watchdog] {len(stale_todos)} TODO(s) stale "
            f"(no update for {self.staleness_minutes}m+) on thread {thread_id}:\n"
            + "\n".join(f"- {(t.get('task') or '')[:80]}" for t in stale_todos)
        )

        for sender in (_send_telegram, _send_discord, _send_slack):
            try:
                result = sender(msg, self.settings)
                if result and result.startswith("Sent"):
                    logger.info("Watchdog notification sent via %s", sender.__name__)
            except Exception as e:
                logger.debug("Notify via %s failed: %s", sender.__name__, e)
