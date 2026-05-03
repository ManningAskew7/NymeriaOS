"""
DEPRECATED: Durable scheduler for self_invoke autonomous behavior.

This module is deprecated. Scheduling is now handled through the TODO system:
- TodoScheduleDB provides efficient polling for scheduled TODOs
- The nym_todo tool accepts a scheduled_for parameter
- Ticker polls TodoScheduleDB instead of TaskDatabase

This module is kept for:
1. Backwards compatibility during migration
2. The DurableScheduler class (deprecated, will be removed in a future version)

Rate limiting has been extracted to rate_limiter.py.

The DurableScheduler.schedule() method still works but creates entries
in the old TaskDatabase. These are migrated to TODOs on startup.
"""

import logging
import threading
import time
import uuid
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from ._deprecated.task_db import DurableTask, TaskDatabase, TaskStatus
from .rate_limiter import RateLimiter
from .time_utils import parse_duration

if TYPE_CHECKING:
    from .agent import NymeriaAgent

logger = logging.getLogger(__name__)


@dataclass
class ExecutionResult:
    """Result of a self_invoke execution, for 'while away' display."""

    task_id: str
    prompt: str
    response: str
    executed_at: datetime
    muted: bool


class DurableScheduler:
    """
    DEPRECATED: Durable scheduler for self_invoke scheduled tasks.

    This class is deprecated. Use TODO scheduling instead:
    - nym_todo(task="task", scheduled_for="1h")
    - nym_todo(todo_id="id", scheduled_for="30m")

    Features:
    - SQLite persistence (survives restarts)
    - Single pending task per user (new replaces old)
    - Auto-cancel when user sends a message
    - Rate limiting to prevent runaway loops
    - Structured output visibility control (activity vs full)
    """

    MAX_DELAY_SECONDS = 86400  # 24 hours

    def __init__(
        self,
        agent: "NymeriaAgent",
        task_db: TaskDatabase,
        max_per_hour: int = RateLimiter.DEFAULT_MAX_PER_HOUR,
    ):
        warnings.warn(
            "DurableScheduler is deprecated. Use TODO scheduling with scheduled_for parameter instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        """
        Initialize the durable scheduler.

        Args:
            agent: The NymeriaAgent instance for executing scheduled prompts
            task_db: TaskDatabase instance for persistence
            max_per_hour: Maximum self_invokes per user per hour
        """
        self.agent = agent
        self.task_db = task_db
        self._rate_limiter = RateLimiter(max_per_hour)
        self._lock = threading.Lock()

        logger.info(f"DurableScheduler initialized with rate limit {max_per_hour}/hour")

    def schedule(self, prompt: str, delay: str, user_id: str, thread_id: str) -> str:
        """
        Schedule a self_invoke.

        Args:
            prompt: The prompt to execute when timer fires
            delay: Delay string like "30s", "5m", "1h", "1d"
            user_id: User ID for this task
            thread_id: Thread ID for executing the prompt

        Returns:
            Success or error message
        """
        # Validate delay
        delay_seconds = self._parse_delay(delay)
        if delay_seconds is None:
            return f"[Error]: Invalid delay format '{delay}'. Use: 30s, 5m, 1h, 1d"
        if delay_seconds > self.MAX_DELAY_SECONDS:
            return "[Error]: Delay too long. Maximum is 24 hours."
        if delay_seconds < 1:
            return "[Error]: Delay must be at least 1 second."

        # Check rate limit
        allowed, remaining = self._rate_limiter.check_and_record(user_id)
        if not allowed:
            return (
                f"[Error]: Rate limit exceeded. Maximum {self._rate_limiter.max_per_hour} "
                f"self_invokes per hour. Please wait before scheduling more."
            )

        # Create new task
        now = time.time()
        task = DurableTask(
            id=str(uuid.uuid4())[:8],
            user_id=user_id,
            thread_id=thread_id,
            prompt=prompt,
            execute_at=now + delay_seconds,
            status=TaskStatus.PENDING,
            created_at=now,
        )

        # Insert into database (this also cancels any existing pending task)
        self.task_db.create_task(task)

        logger.info(f"Scheduled self_invoke for user {user_id} in {delay}: {prompt[:50]}...")

        rate_info = f" ({remaining} remaining this hour)" if remaining < 10 else ""
        return f"[Scheduled]: Will wake up in {delay} to: {prompt[:50]}...{rate_info}"

    def cancel(self, user_id: str) -> bool:
        """
        Cancel pending self_invoke for a user.

        Args:
            user_id: User ID to cancel for

        Returns:
            True if a task was cancelled, False if nothing was pending
        """
        count = self.task_db.cancel_user_tasks(user_id)
        if count > 0:
            logger.info(f"Cancelled {count} pending task(s) for user {user_id}")
            return True
        return False

    def is_self_invoke_turn(self, user_id: str) -> bool:
        """
        Check if currently executing a self_invoke for this user.

        Args:
            user_id: User ID

        Returns:
            True if in a self_invoke turn
        """
        task = self.task_db.get_processing_for_user(user_id)
        return task is not None

    def get_pending(self, user_id: str) -> Optional[DurableTask]:
        """
        Get pending task info for a user.

        Args:
            user_id: User ID

        Returns:
            The pending task or None
        """
        return self.task_db.get_pending_for_user(user_id)

    def get_rate_limit_remaining(self, user_id: str) -> int:
        """
        Get remaining self_invokes for this hour.

        Args:
            user_id: User ID

        Returns:
            Number of remaining invokes
        """
        return self._rate_limiter.get_remaining(user_id)

    def _parse_delay(self, delay: str) -> Optional[int]:
        """
        Parse delay string to seconds.

        Args:
            delay: String like "30s", "5m", "1h", "1d"

        Returns:
            Seconds as int, or None if invalid format
        """
        return parse_duration(delay)


# Backwards compatibility alias
Scheduler = DurableScheduler
