"""Goal manager — supervised execution lifecycle for `/goal`.

A Goal groups a worker thread's task list under a single objective and enforces
that the worker thread structurally cannot mark its own tasks complete; only
the supervisor thread has the authority. State transitions live here so the
slash command, the new goal tools, and the `nym_todo` task-lock guard all share
one source of truth.

Storage mirrors `TodoManager`: per-user JSON at ``data/goals/{user_id}.json``,
per-user `RLock` for atomic updates.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .keyed_locks import KeyedRLockMap
from .storage_paths import safe_path_segment
from .time_utils import utc_now

logger = logging.getLogger(__name__)

_goal_locks = KeyedRLockMap()

DEFAULT_MAX_TURNS = 20
DEFAULT_CONSECUTIVE_REJECTION_LIMIT = 3

GoalStatus = Literal[
    "pending_approval",
    "active",
    "paused",
    "done",
    "cleared",
    "budget_limited",
]
TaskStatus = Literal["pending", "in_progress", "awaiting_review", "done"]
TERMINAL_STATUSES: tuple[GoalStatus, ...] = ("done", "cleared", "budget_limited")


class Task(BaseModel):
    task_id: str
    description: str
    criterion: str = Field(
        default="",
        description=(
            "Verifiable 'done' condition the supervisor checks against the "
            "worker's review request. Empty string means use the description."
        ),
    )
    status: TaskStatus = "pending"
    attempts: int = 0
    review_notes: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Goal(BaseModel):
    goal_id: str
    thread_id: str = Field(description="The worker thread (X) — where the user is talking.")
    helper_thread_id: Optional[str] = Field(
        default=None,
        description="The supervisor thread (S) — set when the goal is approved.",
    )
    user_id: str
    objective: str
    status: GoalStatus = "pending_approval"
    tasks: List[Task] = Field(default_factory=list)

    turns_used: int = 0
    tokens_used: int = 0
    max_turns: int = DEFAULT_MAX_TURNS
    token_budget: Optional[int] = None  # None = unbounded

    consecutive_rejections: int = 0
    consecutive_rejection_limit: int = DEFAULT_CONSECUTIVE_REJECTION_LIMIT

    last_pause_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    def is_active(self) -> bool:
        return self.status == "active"

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def get_task(self, task_id: str) -> Optional[Task]:
        return next((t for t in self.tasks if t.task_id == task_id), None)


class GoalStore(BaseModel):
    user_id: str = "default"
    goals: Dict[str, Goal] = Field(default_factory=dict)  # goal_id → Goal
    updated_at: datetime = Field(default_factory=utc_now)


class GoalNotFoundError(LookupError):
    """Raised when the requested goal_id is not in the user's store."""


class GoalStateError(RuntimeError):
    """Raised when an operation is invalid for the goal's current state."""


class GoalManager:
    """Manages per-user goal records with atomic update semantics."""

    def __init__(self, data_dir: Path):
        self.goals_dir = data_dir / "goals"
        self.goals_dir.mkdir(parents=True, exist_ok=True)
        logger.info("GoalManager initialized at %s", self.goals_dir)

    # -- locking & file I/O ---------------------------------------------------

    def _get_lock(self, user_id: str) -> threading.RLock:
        return _goal_locks.get(user_id)

    def _path_for(self, user_id: str) -> Path:
        safe = safe_path_segment(user_id)
        return self.goals_dir / f"{safe}.json"

    def get_store(self, user_id: str = "default") -> GoalStore:
        path = self._path_for(user_id)
        if not path.exists():
            return GoalStore(user_id=user_id)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return GoalStore.model_validate(data)
        except Exception:
            logger.exception("GoalManager: failed to load store for %s", user_id)
            return GoalStore(user_id=user_id)

    def save_store(self, store: GoalStore) -> bool:
        path = self._path_for(store.user_id)
        try:
            store.updated_at = utc_now()
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(store.model_dump(mode="json"), f, indent=2, default=str)
            tmp.replace(path)
            return True
        except Exception:
            logger.exception("GoalManager: failed to save store for %s", store.user_id)
            return False

    @contextmanager
    def atomic_update(self, user_id: str = "default"):
        """Read-modify-write a user's GoalStore under the per-user RLock.

        Raises ``RuntimeError`` if the save fails, so a disk error is surfaced
        rather than silently dropping the mutation. The save still runs in
        ``finally``, but its result is only raised on the success path, so an
        exception from inside the block propagates first and is never masked.
        """
        lock = self._get_lock(user_id)
        with lock:
            store = self.get_store(user_id)
            try:
                yield store
            finally:
                saved_ok = self.save_store(store)
            if not saved_ok:
                raise RuntimeError(f"Failed to persist goals for user {user_id}")

    # -- goal CRUD ------------------------------------------------------------

    def get_goal(self, user_id: str, goal_id: str) -> Optional[Goal]:
        return self.get_store(user_id).goals.get(goal_id)

    def require_goal(self, user_id: str, goal_id: str) -> Goal:
        goal = self.get_goal(user_id, goal_id)
        if goal is None:
            raise GoalNotFoundError(
                f"Goal {goal_id} not found for user {user_id}"
            )
        return goal

    def get_active_goal_for_thread(
        self, user_id: str, thread_id: str
    ) -> Optional[Goal]:
        """Return the non-terminal goal where ``thread_id`` is the worker.

        Excludes goals where the thread is the supervisor — supervisors look
        up the goal they're supervising via :meth:`get_active_goal_for_helper`.
        """
        store = self.get_store(user_id)
        for goal in store.goals.values():
            if goal.is_terminal():
                continue
            if goal.thread_id == thread_id:
                return goal
        return None

    def get_active_goal_for_helper(
        self, user_id: str, helper_thread_id: str
    ) -> Optional[Goal]:
        """Return the non-terminal goal where ``helper_thread_id`` is the supervisor."""
        store = self.get_store(user_id)
        for goal in store.goals.values():
            if goal.is_terminal():
                continue
            if goal.helper_thread_id == helper_thread_id:
                return goal
        return None

    def create_goal(
        self,
        user_id: str,
        thread_id: str,
        objective: str,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        token_budget: Optional[int] = None,
    ) -> Goal:
        with self.atomic_update(user_id) as store:
            for goal in store.goals.values():
                if goal.thread_id == thread_id and not goal.is_terminal():
                    raise GoalStateError(
                        f"Thread {thread_id} already has an active goal "
                        f"({goal.goal_id}, status={goal.status})"
                    )
            goal_id = uuid.uuid4().hex[:8]
            goal = Goal(
                goal_id=goal_id,
                thread_id=thread_id,
                user_id=user_id,
                objective=objective.strip(),
                max_turns=max_turns,
                token_budget=token_budget,
            )
            store.goals[goal_id] = goal
            return goal

    def approve_goal(
        self,
        user_id: str,
        goal_id: str,
        helper_thread_id: str,
    ) -> Goal:
        """Transition pending_approval → active and record the supervisor thread."""
        with self.atomic_update(user_id) as store:
            goal = self._require_in_store(store, goal_id)
            if goal.status != "pending_approval":
                raise GoalStateError(
                    f"Goal {goal_id} is {goal.status}, cannot approve"
                )
            if not goal.tasks:
                raise GoalStateError(
                    f"Goal {goal_id} has no tasks; worker must propose tasks first"
                )
            goal.helper_thread_id = helper_thread_id
            goal.status = "active"
            goal.updated_at = utc_now()
            return goal

    def pause_goal(
        self, user_id: str, goal_id: str, *, reason: Optional[str] = None
    ) -> Goal:
        with self.atomic_update(user_id) as store:
            goal = self._require_in_store(store, goal_id)
            if goal.is_terminal():
                raise GoalStateError(
                    f"Goal {goal_id} is {goal.status}; cannot pause a terminal goal"
                )
            goal.status = "paused"
            goal.last_pause_reason = reason
            goal.updated_at = utc_now()
            return goal

    def resume_goal(self, user_id: str, goal_id: str) -> Goal:
        with self.atomic_update(user_id) as store:
            goal = self._require_in_store(store, goal_id)
            if goal.status != "paused":
                raise GoalStateError(
                    f"Goal {goal_id} is {goal.status}; only paused goals can resume"
                )
            goal.status = "active"
            goal.consecutive_rejections = 0  # fresh budget on resume
            goal.last_pause_reason = None
            goal.updated_at = utc_now()
            return goal

    def clear_goal(self, user_id: str, goal_id: str) -> Goal:
        with self.atomic_update(user_id) as store:
            goal = self._require_in_store(store, goal_id)
            goal.status = "cleared"
            goal.updated_at = utc_now()
            return goal

    def mark_goal_done(self, user_id: str, goal_id: str) -> Goal:
        with self.atomic_update(user_id) as store:
            goal = self._require_in_store(store, goal_id)
            if goal.is_terminal():
                raise GoalStateError(
                    f"Goal {goal_id} is already {goal.status}"
                )
            if any(t.status != "done" for t in goal.tasks):
                raise GoalStateError(
                    f"Goal {goal_id} has unfinished tasks; cannot mark done"
                )
            goal.status = "done"
            goal.updated_at = utc_now()
            return goal

    # -- task CRUD ------------------------------------------------------------

    def add_task(
        self,
        user_id: str,
        goal_id: str,
        description: str,
        *,
        criterion: str = "",
    ) -> Task:
        with self.atomic_update(user_id) as store:
            goal = self._require_in_store(store, goal_id)
            if goal.is_terminal():
                raise GoalStateError(
                    f"Goal {goal_id} is {goal.status}; cannot add tasks"
                )
            task = Task(
                task_id=uuid.uuid4().hex[:8],
                description=description.strip(),
                criterion=criterion.strip(),
            )
            goal.tasks.append(task)
            goal.updated_at = utc_now()
            return task

    def set_task_status(
        self,
        user_id: str,
        goal_id: str,
        task_id: str,
        new_status: TaskStatus,
    ) -> Task:
        with self.atomic_update(user_id) as store:
            goal = self._require_in_store(store, goal_id)
            task = goal.get_task(task_id)
            if task is None:
                raise GoalNotFoundError(
                    f"Task {task_id} not found in goal {goal_id}"
                )
            task.status = new_status
            task.updated_at = utc_now()
            if new_status == "awaiting_review":
                task.attempts += 1
            goal.updated_at = utc_now()
            return task

    def record_review_feedback(
        self,
        user_id: str,
        goal_id: str,
        task_id: str,
        feedback: str,
    ) -> Task:
        with self.atomic_update(user_id) as store:
            goal = self._require_in_store(store, goal_id)
            task = goal.get_task(task_id)
            if task is None:
                raise GoalNotFoundError(
                    f"Task {task_id} not found in goal {goal_id}"
                )
            task.review_notes.append(feedback.strip())
            task.status = "in_progress"
            task.updated_at = utc_now()
            goal.updated_at = utc_now()
            return task

    # -- authority check & circuit breakers ----------------------------------

    def can_authority(
        self, user_id: str, goal_id: str, thread_id: str
    ) -> bool:
        """True only if ``thread_id`` is the supervisor for this goal.

        The single chokepoint used by `mark_task_done`, the `nym_todo` lock,
        and any other code path that mutates task completion.
        """
        goal = self.get_goal(user_id, goal_id)
        if goal is None:
            return False
        if goal.helper_thread_id is None:
            return False
        return goal.helper_thread_id == thread_id

    def increment_turn(self, user_id: str, goal_id: str) -> Goal:
        with self.atomic_update(user_id) as store:
            goal = self._require_in_store(store, goal_id)
            goal.turns_used += 1
            goal.updated_at = utc_now()
            if goal.turns_used >= goal.max_turns:
                goal.status = "budget_limited"
                goal.last_pause_reason = (
                    f"max_turns ({goal.max_turns}) exhausted"
                )
            return goal

    def record_tokens(
        self, user_id: str, goal_id: str, tokens: int
    ) -> Goal:
        with self.atomic_update(user_id) as store:
            goal = self._require_in_store(store, goal_id)
            goal.tokens_used += max(0, int(tokens))
            goal.updated_at = utc_now()
            if (
                goal.token_budget is not None
                and goal.tokens_used >= goal.token_budget
            ):
                goal.status = "budget_limited"
                goal.last_pause_reason = (
                    f"token_budget ({goal.token_budget}) exhausted"
                )
            return goal

    def increment_rejection(self, user_id: str, goal_id: str) -> Goal:
        with self.atomic_update(user_id) as store:
            goal = self._require_in_store(store, goal_id)
            goal.consecutive_rejections += 1
            goal.updated_at = utc_now()
            if (
                goal.consecutive_rejections >= goal.consecutive_rejection_limit
                and not goal.is_terminal()
            ):
                goal.status = "paused"
                goal.last_pause_reason = (
                    f"{goal.consecutive_rejections} consecutive review "
                    "rejections; manual intervention required"
                )
            return goal

    def reset_rejection(self, user_id: str, goal_id: str) -> Goal:
        with self.atomic_update(user_id) as store:
            goal = self._require_in_store(store, goal_id)
            goal.consecutive_rejections = 0
            goal.updated_at = utc_now()
            return goal

    # -- internal -------------------------------------------------------------

    @staticmethod
    def _require_in_store(store: GoalStore, goal_id: str) -> Goal:
        goal = store.goals.get(goal_id)
        if goal is None:
            raise GoalNotFoundError(
                f"Goal {goal_id} not found for user {store.user_id}"
            )
        return goal


_goal_manager: Optional[GoalManager] = None


def get_goal_manager() -> Optional[GoalManager]:
    """Return the process-global GoalManager set by the agent on boot."""
    return _goal_manager


def set_goal_manager(manager: Optional[GoalManager]) -> None:
    global _goal_manager
    _goal_manager = manager


__all__ = [
    "Goal",
    "GoalManager",
    "GoalNotFoundError",
    "GoalStateError",
    "GoalStatus",
    "GoalStore",
    "Task",
    "TaskStatus",
    "TERMINAL_STATUSES",
    "get_goal_manager",
    "set_goal_manager",
]
