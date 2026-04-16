"""Watchdog for monitoring TODO staleness and nudging Nymeria on the owning thread."""

import atexit
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Dict, List, Optional, Set

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from .activity_log import ActivityType, log_activity
from .event_bus import publish_autonomous_event
from .notifications import create_notification
from .response_handler import create_response
from .ticker import _render_tool_line
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
    Monitors TODO staleness and nudges Nymeria on the owning thread.

    Runs as a daemon thread, checking all users' TODOs at a configurable
    interval. When TODOs haven't been updated for staleness_minutes, groups
    them by thread_id and sends per-thread nudges via agent.stream() with
    full SSE event publishing (same pattern as the ticker).

    Features:
    - Configurable check interval (default 5 minutes)
    - Configurable staleness threshold (default 20 minutes)
    - Per-thread nudges with streaming and SSE events
    - ThreadPoolExecutor for concurrent nudges across threads
    - Tracks nudged TODOs to avoid repeat nudges until updated
    """

    DEFAULT_INTERVAL_MINUTES = 5
    DEFAULT_STALENESS_MINUTES = 20

    def __init__(
        self,
        agent: "NymeriaAgent",
        todo_manager: TodoManager,
        interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
        staleness_minutes: int = DEFAULT_STALENESS_MINUTES,
    ):
        """
        Initialize the watchdog.

        Args:
            agent: NymeriaAgent instance for sending nudges
            todo_manager: TodoManager instance for checking TODOs
            interval_minutes: Minutes between checks (from settings)
            staleness_minutes: Minutes without update before nudging
        """
        self.agent = agent
        self.todo_manager = todo_manager
        self.interval_minutes = interval_minutes
        self.staleness_minutes = staleness_minutes
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._executor: Optional[ThreadPoolExecutor] = None

        # Track in-flight nudges per thread_id
        self._active_nudges: Dict[str, Future] = {}

        # Track nudged TODOs to avoid repeat nudges
        # Key: (user_id, todo_id), Value: last_nudge_time
        self._nudged_todos: Dict[tuple, float] = {}

        # Track TODOs' updated_at to detect changes
        # Key: (user_id, todo_id), Value: last_seen_updated_at
        self._todo_timestamps: Dict[tuple, datetime] = {}

    def start(self) -> None:
        """Start the watchdog thread and executor."""
        if self._running:
            logger.warning("Watchdog already running")
            return

        # Create executor with same pool size as ticker
        max_workers = self.agent.settings.max_concurrent_autonomous or 5
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="WatchdogNudge",
        )

        self._running = True
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="NymeriaWatchdog",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"Watchdog started: checking every {self.interval_minutes}m, "
            f"staleness threshold {self.staleness_minutes}m"
        )

        # Register cleanup on exit
        atexit.register(self.stop)

    def stop(self) -> None:
        """Stop the watchdog thread and executor gracefully."""
        if not self._running:
            return

        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._executor:
            self._executor.shutdown(wait=False)
            self._executor = None
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

    def _is_stale(self, todo: TodoItem) -> bool:
        """
        Check if a TODO is stale based on minutes threshold.

        Bypasses TodoItem.is_stale() which uses hours. This gives the
        watchdog its own minutes-based granularity.
        """
        now = datetime.now(timezone.utc)
        # Not stale if scheduled for the future
        if todo.scheduled_for:
            sf = todo.scheduled_for if todo.scheduled_for.tzinfo else todo.scheduled_for.replace(tzinfo=timezone.utc)
            if sf > now:
                return False
        # Not stale if it has a recurrence pattern (recurring TODOs are managed by the ticker)
        if todo.recurrence:
            return False
        threshold = now - timedelta(minutes=self.staleness_minutes)
        updated = todo.updated_at if todo.updated_at.tzinfo else todo.updated_at.replace(tzinfo=timezone.utc)
        return todo.is_active() and updated < threshold

    def _check_todos(self) -> None:
        """Check all users' TODOs for staleness and dispatch per-thread nudges."""
        # Runtime kill switches (can disable without restart):
        #   - Env var: NYMERIA_WATCHDOG_DISABLED=1
        #   - File flag: {data_dir}/flags/watchdog-off (persistent across restarts)
        import os
        if os.environ.get('NYMERIA_WATCHDOG_DISABLED', '').strip().lower() in ('1', 'true', 'yes'):
            return
        try:
            flag_path = self.agent.settings.data_dir / "flags" / "watchdog-off"
            if flag_path.exists():
                return
        except Exception:
            pass
        # Clean up completed futures
        with self._lock:
            done_threads = [
                tid for tid, fut in self._active_nudges.items() if fut.done()
            ]
            for tid in done_threads:
                # Log any exceptions from completed futures
                fut = self._active_nudges.pop(tid)
                exc = fut.exception()
                if exc:
                    logger.error(f"Watchdog nudge for thread {tid} failed: {exc}")

        users = self.todo_manager.get_all_users_with_todos()

        for user_id in users:
            try:
                self._check_user_todos(user_id)
            except Exception as e:
                logger.error(f"Watchdog check failed for user {user_id}: {e}")

    def _check_user_todos(self, user_id: str) -> None:
        """Check a specific user's TODOs for staleness, grouped by thread."""
        todo_list = self.todo_manager.get_todos(user_id)

        # Find stale TODOs using minutes-based check
        stale_todos: List[TodoItem] = []
        for todo in todo_list.items:
            if self._is_stale(todo):
                stale_todos.append(todo)

        if not stale_todos:
            return

        # Filter out TODOs we've already nudged (that haven't been updated since)
        todos_to_nudge: List[TodoItem] = []
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

        if not todos_to_nudge:
            return

        # Group by thread_id
        thread_groups: Dict[str, List[TodoItem]] = {}
        for todo in todos_to_nudge:
            tid = todo.thread_id or "legacy"
            thread_groups.setdefault(tid, []).append(todo)

        # Submit per-thread nudges to executor
        for thread_id, todos in thread_groups.items():
            with self._lock:
                # Skip if nudge already in-flight for this thread
                if thread_id in self._active_nudges:
                    continue

            # Skip if thread lock is held (ticker is running on it)
            thread_lock = self.agent._thread_locks.get_lock(thread_id)
            acquired = thread_lock.acquire(blocking=False)
            if not acquired:
                logger.debug(
                    f"Watchdog skipping thread {thread_id}: lock held (ticker or user)"
                )
                continue
            # Release immediately — we just tested availability.
            # agent.stream() will re-acquire it properly.
            thread_lock.release()

            if self._executor:
                future = self._executor.submit(
                    self._send_nudge_to_thread, user_id, thread_id, todos
                )
                with self._lock:
                    self._active_nudges[thread_id] = future

    def _send_nudge_to_thread(
        self, user_id: str, thread_id: str, stale_todos: List[TodoItem]
    ) -> None:
        """
        Send a nudge to the agent on the thread that owns the stale TODOs.

        Follows the ticker's _execute_scheduled_todo pattern: streams via
        agent.stream() and publishes SSE events for the frontend.
        """
        import time as _time
        _nudge_start = _time.monotonic()
        logger.info(
            f"[WATCHDOG] === START === thread={thread_id}, user={user_id}, "
            f"stale_todos={len(stale_todos)}"
        )

        # Build per-thread nudge prompt
        lines = [
            f"[WATCHDOG ALERT] The following TODO(s) on this thread have not been updated "
            f"in over {self.staleness_minutes} minutes and need your attention:",
            "",
        ]

        for todo in stale_todos:
            elapsed = (datetime.utcnow() - todo.updated_at).total_seconds() / 60
            status_icon = "[>]" if todo.status.value == "in_progress" else "[ ]"
            lines.append(
                f"  {status_icon} [{todo.id}] {todo.task[:80]} (stale for {elapsed:.0f}min)"
            )

        lines.append("")
        lines.append(
            "For each TODO above, please do one of the following:\n"
            "- If complete: mark it done using the todo tool.\n"
            "- If still in progress: continue working on it, or update its notes/status.\n"
            "- If no longer needed: delete it."
        )

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

        task_id = f"watchdog-{thread_id}"
        response_parts: list = []
        thinking_parts: list = []
        pending_calls: dict = {}
        nudge_failed = False
        iteration_limit_hit = False

        try:
            # Publish task_started so frontend enters streaming mode
            publish_autonomous_event(
                event_type="task_started",
                thread_id=thread_id,
                user_id=user_id,
                task_id=task_id,
                data={"prompt": nudge_message, "source": "watchdog"},
            )

            chunk_count = 0
            for chunk in self.agent.stream(
                message=nudge_message,
                thread_id=thread_id,
                user_id=user_id,
                _is_self_invoke=True,
            ):
                chunk_type = chunk.get("type")
                chunk_count += 1
                logger.debug(f"[WATCHDOG] thread={thread_id}: chunk #{chunk_count} type={chunk_type}")

                if chunk_type == "tool_call":
                    pending_calls[chunk.get("id", "")] = {
                        "name": chunk.get("name", "unknown"),
                        "args": chunk.get("args", {}),
                    }
                    publish_autonomous_event(
                        event_type="tool_call",
                        thread_id=thread_id,
                        user_id=user_id,
                        task_id=task_id,
                        data={
                            "id": chunk.get("id"),
                            "name": chunk.get("name"),
                            "args": chunk.get("args", {}),
                        },
                    )
                elif chunk_type == "tool_result":
                    _render_tool_line(pending_calls, chunk)
                    publish_autonomous_event(
                        event_type="tool_result",
                        thread_id=thread_id,
                        user_id=user_id,
                        task_id=task_id,
                        data={
                            "id": chunk.get("id"),
                            "name": chunk.get("name"),
                            "result": chunk.get("result"),
                        },
                    )
                elif chunk_type == "thinking":
                    content = chunk.get("content", "")
                    if content:
                        thinking_parts.append(content)
                    publish_autonomous_event(
                        event_type="thinking",
                        thread_id=thread_id,
                        user_id=user_id,
                        task_id=task_id,
                        data={"content": content},
                    )
                elif chunk_type == "response":
                    content = chunk.get("content", "")
                    if content:
                        response_parts.append(content)
                        publish_autonomous_event(
                            event_type="response",
                            thread_id=thread_id,
                            user_id=user_id,
                            task_id=task_id,
                            data={"content": content},
                        )

                elif chunk_type == "error":
                    error_content = chunk.get("content", "")
                    error_code = chunk.get("code", "unknown")
                    logger.error(
                        f"[WATCHDOG] Stream error for thread {thread_id}: "
                        f"code={error_code}, content={error_content}"
                    )
                    nudge_failed = True
                    raise RuntimeError(
                        error_content or f"Watchdog stream error (code={error_code})"
                    )

                elif chunk_type == "iteration_limit":
                    iteration_limit_hit = True
                    logger.warning(
                        f"[WATCHDOG] Iteration limit during nudge for thread "
                        f"{thread_id}: scope={chunk.get('scope')}. "
                        f"Proceeding with partial delivery."
                    )

            # Compute final response text
            if response_parts:
                response_text = "".join(response_parts)
            elif thinking_parts:
                response_text = "".join(thinking_parts)
            else:
                response_text = ""

            response = create_response(content=response_text, notify=False)

            # Show response in console
            if response_text:
                sanitized_content = _sanitize_unicode(response_text)
                _console.print()
                _console.print("[bold green]Nymeria:[/bold green]")
                _console.print(Markdown(sanitized_content))

            # Create notification if requested
            if response.notify and response.summary:
                create_notification(
                    user_id=user_id,
                    summary=response.summary,
                    thread_id=thread_id,
                    task_id=task_id,
                )

            # Only mark TODOs as nudged and log success if stream didn't fail
            if not nudge_failed:
                # Mark TODOs as nudged
                with self._lock:
                    now = time.time()
                    for todo in stale_todos:
                        key = (user_id, todo.id)
                        self._nudged_todos[key] = now

                # Log activity on the real thread
                activity_msg = (
                    f"Watchdog alert: {len(stale_todos)} stale TODO(s) need attention"
                )
                if iteration_limit_hit:
                    activity_msg += " (partial — hit iteration limit)"

                log_activity(
                    ActivityType.WATCHDOG_NUDGE,
                    activity_msg,
                    user_id=user_id,
                    thread_id=thread_id,
                    metadata={
                        "stale_todo_ids": [t.id for t in stale_todos],
                        "staleness_minutes": self.staleness_minutes,
                        "partial": iteration_limit_hit,
                    },
                )

                _elapsed = _time.monotonic() - _nudge_start
                logger.info(
                    f"[WATCHDOG] === END === thread={thread_id}, "
                    f"chunks={chunk_count}, response_len={len(response_text)}, "
                    f"partial={iteration_limit_hit}, elapsed={_elapsed:.1f}s"
                )

        except Exception as e:
            import traceback
            nudge_failed = True
            _elapsed = _time.monotonic() - _nudge_start
            logger.error(
                f"[WATCHDOG] === ERROR === thread={thread_id}, elapsed={_elapsed:.1f}s: {e}"
            )
            logger.error(f"[WATCHDOG] Traceback:\n{traceback.format_exc()}")
        finally:
            # Always publish task_completed so frontend exits streaming state
            completed_data: dict = {
                "notify": False,
                "content": "".join(response_parts),
                "source": "watchdog",
            }
            if nudge_failed:
                completed_data["error"] = True
            if iteration_limit_hit:
                completed_data["partial"] = True
            publish_autonomous_event(
                event_type="task_completed",
                thread_id=thread_id,
                user_id=user_id,
                task_id=task_id,
                data=completed_data,
            )

        # Send external notifications so the user actually sees it
        try:
            from ..tools.notify import _send_telegram, _send_discord, _send_slack
            from ..config import get_settings

            settings = get_settings()
            notify_msg = (
                f"[Nymeria Watchdog] {len(stale_todos)} TODO(s) stale "
                f"(no update for {self.staleness_minutes}m+) on thread {thread_id}:\n"
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
