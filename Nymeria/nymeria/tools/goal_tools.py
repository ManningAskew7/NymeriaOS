"""Goal-mode tools: worker proposes & escalates; supervisor approves & gates.

Four tools bound by the two goal skill kits:

  worker side (goal-worker kit):
    - propose_task          — add a task to the active goal's task list
    - request_review        — escalate a task to the supervisor; blocks for the verdict

  supervisor side (goal-supervisor kit):
    - mark_task_done        — authoritative `done` transition (authority enforced)
    - provide_review_feedback — send refinement guidance back to the worker

Authority is a single check: `can_authority(user_id, goal_id, thread_id)` returns
True only for the supervisor thread recorded on the Goal. The worker thread is
structurally unable to mark its own tasks done — the tool simply isn't bound on
its kit, and the runtime guard in `mark_task_done` plus the `nym_todo` lock are
defence-in-depth.
"""

from __future__ import annotations
from .registry import ToolGroup, register_tool_group

import logging
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..core.goal_manager import (
    GoalNotFoundError,
    GoalStateError,
    get_goal_manager,
)
from .utils import get_thread_id_or_none, get_user_id

logger = logging.getLogger(__name__)


def _no_active_goal_error(thread_id: Optional[str]) -> str:
    return (
        "[Error]: No active goal on this thread"
        + (f" ({thread_id})" if thread_id else "")
        + ". Use `/goal <objective>` to start one."
    )


@tool
def propose_task(
    description: str,
    criterion: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Add a task to the active goal's task list (worker side).

    Use this during decomposition (before `/goal approve`) to seed the initial
    plan, or mid-loop when a sub-task is discovered. Each task should have a
    clear ``description`` and a ``criterion`` — the verifiable "done" condition
    the supervisor will check.

    Args:
        description: What the task accomplishes. Short imperative sentence.
        criterion: Verifiable condition for "done" (e.g. "tests pass", "endpoint
            returns 200"). Leave empty to default to the description.

    Returns:
        ``[Added]: task_id=<id> description=<...>`` on success.
    """
    user_id = get_user_id(config)
    thread_id = get_thread_id_or_none(config)
    if not thread_id:
        return "[Error]: propose_task requires an active thread."
    gm = get_goal_manager()
    if gm is None:
        return "[Error]: Goal manager unavailable."
    goal = gm.get_active_goal_for_thread(user_id, thread_id)
    if goal is None:
        return _no_active_goal_error(thread_id)
    if not description.strip():
        return "[Error]: description is required."
    try:
        task = gm.add_task(
            user_id, goal.goal_id, description, criterion=criterion
        )
    except GoalStateError as e:
        return f"[Error]: {e}"
    summary = description.strip()[:80]
    return f"[Added]: task_id={task.task_id} description={summary}"


@tool
def request_review(
    task_id: str,
    summary: str,
    evidence: str = "",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Escalate a task to the supervisor for review (worker side). BLOCKS.

    The worker thread cannot mark its own tasks done — it must ask the
    supervisor. This call marks the task ``awaiting_review``, dispatches the
    review request to the supervisor thread as a callable invocation, and
    BLOCKS until the supervisor returns its verdict.

    The supervisor will either:
      - call ``mark_task_done`` (verdict: approved) — the task ticks to ``done``
        and the worker should move to the next task; OR
      - call ``provide_review_feedback`` (verdict: refine) — the task drops
        back to ``in_progress`` and the worker should iterate based on the
        feedback included in this tool's return value.

    Args:
        task_id: The task id returned by ``propose_task``.
        summary: One-paragraph description of what the worker did.
        evidence: Concrete evidence the supervisor can verify (file paths, test
            output, URLs, etc.). Verbose is better than terse.

    Returns:
        The supervisor's text response, prefixed with the goal-mode verdict.
    """
    from ..core.thread_agent_executor import invoke as invoke_callable

    user_id = get_user_id(config)
    thread_id = get_thread_id_or_none(config)
    if not thread_id:
        return "[Error]: request_review requires an active thread."
    gm = get_goal_manager()
    if gm is None:
        return "[Error]: Goal manager unavailable."
    goal = gm.get_active_goal_for_thread(user_id, thread_id)
    if goal is None:
        return _no_active_goal_error(thread_id)
    if goal.status == "paused":
        return (
            "[Error]: Goal is paused — the user explicitly stopped the loop. "
            "Stop after this tool result; wait for `/goal resume` before "
            "escalating again. Reason: "
            f"{goal.last_pause_reason or '(not specified)'}"
        )
    if goal.is_terminal():
        return (
            f"[Error]: Goal is `{goal.status}`; no more reviews accepted. "
            "Surface a summary to the user and stop the loop."
        )
    if goal.helper_thread_id is None:
        return (
            "[Error]: Goal is not yet approved; no supervisor thread exists. "
            "Wait for the user to run `/goal approve`."
        )
    task = goal.get_task(task_id)
    if task is None:
        return f"[Error]: Task {task_id} not found in goal {goal.goal_id}."
    if task.status == "done":
        return f"[Error]: Task {task_id} is already done."

    try:
        gm.set_task_status(user_id, goal.goal_id, task_id, "awaiting_review")
    except (GoalStateError, GoalNotFoundError) as e:
        return f"[Error]: {e}"

    review_prompt = (
        f"[Goal review request]\n"
        f"goal_id: {goal.goal_id}\n"
        f"task_id: {task_id}\n"
        f"task description: {task.description}\n"
        f"criterion: {task.criterion or '(use description as criterion)'}\n"
        f"attempt #: {task.attempts}\n"
        f"\n"
        f"Worker summary:\n{summary.strip()}\n"
    )
    if evidence and evidence.strip():
        review_prompt += f"\nEvidence:\n{evidence.strip()}\n"
    review_prompt += (
        "\nVerify this against the criterion. If satisfied, call "
        "`mark_task_done(task_id=...)`. If not, call "
        "`provide_review_feedback(task_id=..., feedback=...)` with concrete "
        "guidance the worker can act on."
    )

    try:
        response = invoke_callable(
            thread_id=goal.helper_thread_id,
            task=review_prompt,
            caller_user_id=user_id,
            callable_name=f"goal-supervisor-{goal.goal_id}",
            trigger_override=f"GoalReview({goal.goal_id})",
        )
    except Exception as e:
        logger.exception("request_review: supervisor invocation failed")
        return (
            f"[Error]: Supervisor invocation failed: {str(e)[:200]}. "
            f"Task remains in awaiting_review; you can retry."
        )

    # Re-read the task to see what the supervisor decided.
    latest = gm.require_goal(user_id, goal.goal_id).get_task(task_id)
    if latest is None:
        verdict_line = "[verdict: unknown — task disappeared from goal]"
    elif latest.status == "done":
        verdict_line = "[verdict: APPROVED — task marked done]"
    elif latest.status == "in_progress":
        verdict_line = "[verdict: REFINE — see feedback below]"
    else:
        verdict_line = (
            f"[verdict: indeterminate — task is in '{latest.status}'; "
            "supervisor may not have decided. See response.]"
        )

    return f"{verdict_line}\n\n{response or '(supervisor returned no text)'}"


@tool
def mark_task_done(
    task_id: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Approve a task as complete (supervisor side). Authority enforced.

    Only the supervisor thread for the goal can call this — the worker thread
    will be rejected even if the tool is somehow exposed. If this is the last
    pending task, the goal itself transitions to ``done`` automatically.

    Args:
        task_id: The task id from the review request.

    Returns:
        ``[Approved]: ...`` on success, ``[Error]: ...`` on authority failure
        or unknown task.
    """
    user_id = get_user_id(config)
    thread_id = get_thread_id_or_none(config)
    if not thread_id:
        return "[Error]: mark_task_done requires an active thread."
    gm = get_goal_manager()
    if gm is None:
        return "[Error]: Goal manager unavailable."
    goal = gm.get_active_goal_for_helper(user_id, thread_id)
    if goal is None:
        return (
            "[Error]: This thread is not a goal supervisor. "
            "Only supervisor threads can call mark_task_done."
        )
    if not gm.can_authority(user_id, goal.goal_id, thread_id):
        return (
            "[Error]: Authority check failed — thread is not the recorded "
            "supervisor for this goal."
        )
    task = goal.get_task(task_id)
    if task is None:
        return f"[Error]: Task {task_id} not found in goal {goal.goal_id}."
    if task.status == "done":
        return f"[Info]: Task {task_id} is already done."

    try:
        gm.set_task_status(user_id, goal.goal_id, task_id, "done")
        gm.reset_rejection(user_id, goal.goal_id)
    except (GoalStateError, GoalNotFoundError) as e:
        return f"[Error]: {e}"

    # Auto-complete the goal if all tasks are done.
    updated = gm.require_goal(user_id, goal.goal_id)
    if all(t.status == "done" for t in updated.tasks):
        try:
            gm.mark_goal_done(user_id, goal.goal_id)
            return (
                f"[Approved]: task_id={task_id}. All tasks complete — "
                f"goal {goal.goal_id} is DONE."
            )
        except GoalStateError:
            # Race: another caller already transitioned the goal to done
            # (or to a terminal state). Fall through to the "remaining"
            # message; the caller still gets a correct response.
            pass
    remaining = sum(1 for t in updated.tasks if t.status != "done")
    return (
        f"[Approved]: task_id={task_id}. "
        f"{remaining} task(s) remaining in goal {goal.goal_id}. "
        "Instruct the worker to continue to the next task."
    )


@tool
def provide_review_feedback(
    task_id: str,
    feedback: str,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Send refinement guidance back to the worker (supervisor side).

    Use when the worker's review request didn't fully meet the criterion.
    Records the feedback on the task, drops the task back to ``in_progress``,
    and increments the consecutive-rejection counter (which pauses the goal
    after 3 in a row, by default).

    Args:
        task_id: The task id from the review request.
        feedback: Concrete, actionable guidance the worker can act on
            (don't just say "do better"; say what's missing or wrong).

    Returns:
        ``[Refinement requested]: ...`` on success.
    """
    user_id = get_user_id(config)
    thread_id = get_thread_id_or_none(config)
    if not thread_id:
        return "[Error]: provide_review_feedback requires an active thread."
    gm = get_goal_manager()
    if gm is None:
        return "[Error]: Goal manager unavailable."
    goal = gm.get_active_goal_for_helper(user_id, thread_id)
    if goal is None:
        return (
            "[Error]: This thread is not a goal supervisor. "
            "Only supervisor threads can provide review feedback."
        )
    if not gm.can_authority(user_id, goal.goal_id, thread_id):
        return (
            "[Error]: Authority check failed — thread is not the recorded "
            "supervisor for this goal."
        )
    task = goal.get_task(task_id)
    if task is None:
        return f"[Error]: Task {task_id} not found in goal {goal.goal_id}."
    if not feedback.strip():
        return "[Error]: feedback cannot be empty."

    try:
        gm.record_review_feedback(user_id, goal.goal_id, task_id, feedback)
        gm.increment_rejection(user_id, goal.goal_id)
    except (GoalStateError, GoalNotFoundError) as e:
        return f"[Error]: {e}"

    # Re-read in case the rejection pushed status to paused.
    updated = gm.require_goal(user_id, goal.goal_id)
    suffix = ""
    if updated.status == "paused":
        suffix = (
            f"\n\n[Goal auto-paused]: {updated.last_pause_reason}. "
            "User intervention required to resume."
        )
    return (
        f"[Refinement requested]: task_id={task_id} attempts={task.attempts}. "
        f"Worker will retry with the feedback recorded.{suffix}"
    )


GOAL_TOOLS = [propose_task, request_review, mark_task_done, provide_review_feedback]


__all__ = [
    "GOAL_TOOLS",
    "propose_task",
    "request_review",
    "mark_task_done",
    "provide_review_feedback",
]


# Register this tool family for catalog auto-discovery (backlog #51).
register_tool_group(ToolGroup(name="goal", tools=tuple(GOAL_TOOLS)))
