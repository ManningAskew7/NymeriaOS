"""Watchdog for monitoring TODO staleness and nudging Nymeria."""

import atexit
import concurrent.futures
import logging
import re
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Dict, Optional, Set

from rich.console import Console
from rich.panel import Panel

from .activity_log import ActivityType, log_activity
from .todo_manager import TodoItem, TodoManager

if TYPE_CHECKING:
    from .agent import NymeriaAgent

logger = logging.getLogger(__name__)

# Console for printing watchdog output
_console = Console(force_terminal=True)


def _sanitize_unicode(text: str) -> str:
    """
    Sanitize text for Windows console output.

    Replaces common Unicode characters with ASCII equivalents,
    then strips any remaining non-ASCII characters.
    """
    # Replace common Unicode symbols with ASCII equivalents
    replacements = {
        '\u2713': '[x]',  # checkmark
        '\u2714': '[x]',  # heavy checkmark
        '\u2715': '[ ]',  # multiplication x
        '\u2716': '[ ]',  # heavy multiplication x
        '\u2717': '[ ]',  # ballot x
        '\u2718': '[ ]',  # heavy ballot x
        '\u2022': '*',    # bullet
        '\u2023': '>',    # triangular bullet
        '\u2192': '->',   # rightwards arrow
        '\u2190': '<-',   # leftwards arrow
        '\u2194': '<->',  # left right arrow
        '\u2026': '...',  # ellipsis
        '\u2013': '-',    # en dash
        '\u2014': '--',   # em dash
        '\u201c': '"',    # left double quotation
        '\u201d': '"',    # right double quotation
        '\u2018': "'",    # left single quotation
        '\u2019': "'",    # right single quotation
    }

    for char, replacement in replacements.items():
        text = text.replace(char, replacement)

    # Strip any remaining non-ASCII characters
    return re.sub(r'[^\x00-\x7F]+', '', text)


# Global watchdog instance
_watchdog: Optional["Watchdog"] = None


def get_watchdog() -> Optional["Watchdog"]:
    """Get the global watchdog instance."""
    return _watchdog


def set_watchdog(watchdog: Optional["Watchdog"]) -> None:
    """Set the global watchdog instance."""
    global _watchdog
    _watchdog = watchdog


class Watchdog:
    """
    Monitors TODO staleness and nudges Nymeria to take action.

    Runs as a daemon thread, checking all users' TODOs at a configurable
    interval. When TODOs haven't been updated for staleness_hours, sends
    a nudge via agent.chat() with _is_self_invoke=True.

    Features:
    - Configurable check interval (default 30 minutes)
    - Configurable staleness threshold (default 4 hours)
    - Tracks nudged TODOs to avoid repeat nudges until updated
    - Per-user thread context preservation
    """

    DEFAULT_INTERVAL_MINUTES = 30
    DEFAULT_STALENESS_HOURS = 4
    NUDGE_TIMEOUT_SECONDS = 120

    def __init__(
        self,
        agent: "NymeriaAgent",
        todo_manager: TodoManager,
        interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
        staleness_hours: int = DEFAULT_STALENESS_HOURS,
    ):
        """
        Initialize the watchdog.

        Args:
            agent: NymeriaAgent instance for sending nudges
            todo_manager: TodoManager instance for checking TODOs
            interval_minutes: Minutes between checks (from settings)
            staleness_hours: Hours without update before nudging
        """
        self.agent = agent
        self.todo_manager = todo_manager
        self.interval_minutes = interval_minutes
        self.staleness_hours = staleness_hours
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Track nudged TODOs to avoid repeat nudges
        # Key: (user_id, todo_id), Value: last_nudge_time
        self._nudged_todos: Dict[tuple, float] = {}

        # Track TODOs' updated_at to detect changes
        # Key: (user_id, todo_id), Value: last_seen_updated_at
        self._todo_timestamps: Dict[tuple, datetime] = {}

    def start(self) -> None:
        """Start the watchdog thread."""
        if self._running:
            logger.warning("Watchdog already running")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="NymeriaWatchdog",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"Watchdog started: checking every {self.interval_minutes}m, "
            f"staleness threshold {self.staleness_hours}h"
        )

        # Register cleanup on exit
        atexit.register(self.stop)

    def stop(self) -> None:
        """Stop the watchdog thread gracefully."""
        if not self._running:
            return

        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Watchdog stopped")

    def _watch_loop(self) -> None:
        """Main watching loop."""
        # Initial delay to let system stabilize
        time.sleep(5)

        while self._running:
            try:
                self._check_todos()
            except Exception as e:
                logger.error(f"Watchdog check error: {e}", exc_info=True)

            # Sleep in small increments to allow fast shutdown
            sleep_seconds = self.interval_minutes * 60
            sleep_increments = int(sleep_seconds / 0.5)  # 0.5s increments
            for _ in range(sleep_increments):
                if not self._running:
                    break
                time.sleep(0.5)

    def _check_todos(self) -> None:
        """Check all users' TODOs for staleness."""
        users = self.todo_manager.get_all_users_with_todos()

        for user_id in users:
            try:
                self._check_user_todos(user_id)
            except Exception as e:
                logger.error(f"Watchdog check failed for user {user_id}: {e}")

    def _check_user_todos(self, user_id: str) -> None:
        """Check a specific user's TODOs for staleness."""
        todo_list = self.todo_manager.get_todos(user_id)
        stale_todos = todo_list.get_stale_todos(self.staleness_hours)

        if not stale_todos:
            return

        # Filter out TODOs we've already nudged (that haven't been updated since)
        todos_to_nudge = []
        with self._lock:
            for todo in stale_todos:
                key = (user_id, todo.id)

                # Check if TODO has been updated since last nudge
                last_seen = self._todo_timestamps.get(key)
                if last_seen and todo.updated_at > last_seen:
                    # TODO was updated, clear nudge tracking
                    self._nudged_todos.pop(key, None)

                # Update timestamp tracking
                self._todo_timestamps[key] = todo.updated_at

                # Check if we've already nudged this TODO
                if key not in self._nudged_todos:
                    todos_to_nudge.append(todo)

        if todos_to_nudge:
            self._send_nudge(user_id, todos_to_nudge)

    def _send_nudge(self, user_id: str, stale_todos: list) -> None:
        """
        Send a nudge to Nymeria about stale TODOs.

        Args:
            user_id: The user whose TODOs are stale
            stale_todos: List of stale TodoItem objects
        """
        logger.info(f"Sending watchdog nudge for user {user_id}: {len(stale_todos)} stale TODO(s)")

        # Build nudge message
        lines = [
            f"[WATCHDOG ALERT] You have pending TODOs that haven't been updated in over {self.staleness_hours} hours:",
            "",
        ]

        for todo in stale_todos:
            hours = todo.hours_since_update()
            status_icon = "[>]" if todo.status.value == "in_progress" else "[ ]"
            lines.append(f"- {status_icon} [{todo.id}] {todo.task[:80]} (no update for {hours:.1f}h)")

        lines.append("")
        lines.append("Please review and update, complete, or delete these tasks.")

        nudge_message = "\n".join(lines)

        # Print alert to console
        sanitized_message = _sanitize_unicode(nudge_message)
        _console.print()
        _console.print(
            Panel(
                sanitized_message,
                title="[bold yellow]Watchdog Alert[/bold yellow]",
                border_style="yellow",
            )
        )

        thread_id = f"watchdog_{user_id}"
        nudge_succeeded = False

        try:
            # Send nudge via agent with timeout to prevent blocking forever
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self.agent.chat,
                    message=nudge_message,
                    thread_id=thread_id,
                    user_id=user_id,
                    _is_self_invoke=True,
                )
                future.result(timeout=self.NUDGE_TIMEOUT_SECONDS)

            nudge_succeeded = True
            logger.info(f"Watchdog nudge sent for user {user_id}")

        except concurrent.futures.TimeoutError:
            logger.error(
                f"Watchdog nudge timed out for user {user_id} "
                f"after {self.NUDGE_TIMEOUT_SECONDS}s"
            )
        except Exception as e:
            logger.error(f"Failed to send watchdog nudge for user {user_id}: {e}")

        # Also send a notification to external platforms so the user actually sees it
        try:
            from ..tools.notify import _send_telegram, _send_discord, _send_slack
            from ..config import get_settings

            settings = get_settings()
            notify_msg = (
                f"[Nymeria Watchdog] {len(stale_todos)} TODO(s) stale "
                f"(no update for {self.staleness_hours}h+):\n"
                + "\n".join(f"- {t.task[:80]}" for t in stale_todos)
            )

            for sender in (_send_telegram, _send_discord, _send_slack):
                try:
                    result = sender(notify_msg, settings)
                    if result and result.startswith("Sent"):
                        logger.info(f"Watchdog notification sent via {sender.__name__}")
                except Exception as notify_err:
                    logger.debug(f"Watchdog notify via {sender.__name__} failed: {notify_err}")
        except Exception as e:
            logger.debug(f"Watchdog notification attempt failed: {e}")

        if nudge_succeeded:
            # Only mark TODOs as nudged if the agent chat succeeded
            with self._lock:
                now = time.time()
                for todo in stale_todos:
                    key = (user_id, todo.id)
                    self._nudged_todos[key] = now

            # Log activity for watchdog nudge
            log_activity(
                ActivityType.WATCHDOG_NUDGE,
                f"Watchdog alert: {len(stale_todos)} stale TODO(s) need attention",
                user_id=user_id,
                thread_id=thread_id,
                metadata={
                    "stale_todo_ids": [t.id for t in stale_todos],
                    "staleness_hours": self.staleness_hours,
                },
            )

    def clear_nudge_tracking(self, user_id: str, todo_id: str) -> None:
        """
        Clear nudge tracking for a specific TODO.

        Call this when a TODO is updated, completed, or deleted.

        Args:
            user_id: The user ID
            todo_id: The TODO ID
        """
        with self._lock:
            key = (user_id, todo_id)
            self._nudged_todos.pop(key, None)
            self._todo_timestamps.pop(key, None)

    def is_running(self) -> bool:
        """Check if the watchdog is running."""
        return self._running
