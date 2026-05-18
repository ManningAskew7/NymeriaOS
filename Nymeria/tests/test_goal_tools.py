"""Tests for the four goal-mode tools: propose_task, request_review,
mark_task_done, provide_review_feedback."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.runnables import RunnableConfig

from nymeria.core.goal_manager import GoalManager, set_goal_manager
from nymeria.tools.goal_tools import (
    mark_task_done,
    propose_task,
    provide_review_feedback,
    request_review,
)


@pytest.fixture
def gm(tmp_path: Path) -> GoalManager:
    manager = GoalManager(tmp_path)
    set_goal_manager(manager)
    yield manager
    set_goal_manager(None)


def _config(thread_id: str, user_id: str = "u1") -> RunnableConfig:
    return {
        "configurable": {"thread_id": thread_id, "user_id": user_id}
    }


def _seed_active_goal(
    gm: GoalManager,
    *,
    worker: str = "worker-X",
    supervisor: str = "supervisor-S",
) -> str:
    """Create and approve a goal with one task; returns goal_id."""
    goal = gm.create_goal("u1", worker, "objective")
    gm.add_task("u1", goal.goal_id, "task description", criterion="criterion X")
    gm.approve_goal("u1", goal.goal_id, supervisor)
    return goal.goal_id


class TestProposeTask:
    def test_adds_task_to_active_goal(self, gm):
        gm.create_goal("u1", "worker-X", "obj")
        result = propose_task.invoke(
            {"description": "do the thing", "criterion": "thing is done"},
            config=_config("worker-X"),
        )
        assert "[Added]" in result
        goal = gm.get_active_goal_for_thread("u1", "worker-X")
        assert goal is not None
        assert len(goal.tasks) == 1
        assert goal.tasks[0].description == "do the thing"
        assert goal.tasks[0].criterion == "thing is done"

    def test_fails_without_active_goal(self, gm):
        result = propose_task.invoke(
            {"description": "stray task"},
            config=_config("worker-X"),
        )
        assert "No active goal" in result

    def test_fails_with_empty_description(self, gm):
        gm.create_goal("u1", "worker-X", "obj")
        result = propose_task.invoke(
            {"description": "   "},
            config=_config("worker-X"),
        )
        assert "description is required" in result


class TestRequestReview:
    def test_fails_when_supervisor_not_set(self, gm):
        goal = gm.create_goal("u1", "worker-X", "obj")
        task = gm.add_task("u1", goal.goal_id, "task 1")
        result = request_review.invoke(
            {
                "task_id": task.task_id,
                "summary": "did the thing",
            },
            config=_config("worker-X"),
        )
        assert "not yet approved" in result

    def test_fails_on_unknown_task(self, gm):
        _seed_active_goal(gm)
        result = request_review.invoke(
            {"task_id": "00000000", "summary": "x"},
            config=_config("worker-X"),
        )
        assert "not found" in result

    def test_fails_on_already_done_task(self, gm):
        gid = _seed_active_goal(gm)
        tid = gm.require_goal("u1", gid).tasks[0].task_id
        gm.set_task_status("u1", gid, tid, "done")
        result = request_review.invoke(
            {"task_id": tid, "summary": "x"},
            config=_config("worker-X"),
        )
        assert "already done" in result

    @patch("nymeria.core.thread_agent_executor.invoke")
    def test_approved_path_returns_verdict_and_marks_awaiting_review(
        self, mock_invoke, gm
    ):
        gid = _seed_active_goal(gm)
        tid = gm.require_goal("u1", gid).tasks[0].task_id

        # Simulate the supervisor approving via mark_task_done during invocation.
        def _supervisor_action(thread_id, task, caller_user_id, callable_name,
                              trigger_override=None):
            gm.set_task_status("u1", gid, tid, "done")
            return "Looks good. Approving."

        mock_invoke.side_effect = _supervisor_action
        result = request_review.invoke(
            {"task_id": tid, "summary": "did the work"},
            config=_config("worker-X"),
        )
        assert "APPROVED" in result
        assert "Looks good" in result
        # Verify attempts was incremented when status flipped to awaiting_review.
        task = gm.require_goal("u1", gid).get_task(tid)
        assert task is not None
        assert task.status == "done"

    @patch("nymeria.core.thread_agent_executor.invoke")
    def test_refine_path_returns_refine_verdict(self, mock_invoke, gm):
        gid = _seed_active_goal(gm)
        tid = gm.require_goal("u1", gid).tasks[0].task_id

        def _supervisor_action(thread_id, task, caller_user_id, callable_name,
                              trigger_override=None):
            gm.record_review_feedback("u1", gid, tid, "missing the edge case")
            return "Needs more work on edge cases."

        mock_invoke.side_effect = _supervisor_action
        result = request_review.invoke(
            {"task_id": tid, "summary": "first attempt"},
            config=_config("worker-X"),
        )
        assert "REFINE" in result
        task = gm.require_goal("u1", gid).get_task(tid)
        assert task is not None
        assert task.status == "in_progress"
        assert task.review_notes == ["missing the edge case"]


class TestMarkTaskDone:
    def test_only_supervisor_can_approve(self, gm):
        gid = _seed_active_goal(gm)
        tid = gm.require_goal("u1", gid).tasks[0].task_id
        # Worker tries to mark its own task done
        result = mark_task_done.invoke(
            {"task_id": tid},
            config=_config("worker-X"),
        )
        assert "[Error]" in result
        assert "supervisor" in result.lower()
        # Task should NOT be done
        assert gm.require_goal("u1", gid).get_task(tid).status != "done"

    def test_supervisor_approval_marks_done(self, gm):
        gid = _seed_active_goal(gm)
        tid = gm.require_goal("u1", gid).tasks[0].task_id
        result = mark_task_done.invoke(
            {"task_id": tid},
            config=_config("supervisor-S"),
        )
        assert "[Approved]" in result
        assert gm.require_goal("u1", gid).get_task(tid).status == "done"

    def test_single_task_completion_auto_marks_goal_done(self, gm):
        gid = _seed_active_goal(gm)
        tid = gm.require_goal("u1", gid).tasks[0].task_id
        result = mark_task_done.invoke(
            {"task_id": tid},
            config=_config("supervisor-S"),
        )
        assert "DONE" in result
        assert gm.require_goal("u1", gid).status == "done"

    def test_multi_task_keeps_goal_active_until_all_done(self, gm):
        gid = _seed_active_goal(gm)
        # Add second task
        t2 = gm.add_task("u1", gid, "task 2")
        t1id = gm.require_goal("u1", gid).tasks[0].task_id
        # Approve first task only
        result = mark_task_done.invoke(
            {"task_id": t1id},
            config=_config("supervisor-S"),
        )
        assert "remaining" in result
        assert gm.require_goal("u1", gid).status == "active"
        # Approve second task → goal completes
        result2 = mark_task_done.invoke(
            {"task_id": t2.task_id},
            config=_config("supervisor-S"),
        )
        assert "DONE" in result2
        assert gm.require_goal("u1", gid).status == "done"

    def test_unknown_supervisor_rejected(self, gm):
        gid = _seed_active_goal(gm)
        tid = gm.require_goal("u1", gid).tasks[0].task_id
        # Some random thread tries
        result = mark_task_done.invoke(
            {"task_id": tid},
            config=_config("random-thread"),
        )
        assert "[Error]" in result

    def test_resets_consecutive_rejections_on_approval(self, gm):
        gid = _seed_active_goal(gm)
        gm.increment_rejection("u1", gid)
        gm.increment_rejection("u1", gid)
        assert gm.require_goal("u1", gid).consecutive_rejections == 2
        tid = gm.require_goal("u1", gid).tasks[0].task_id
        mark_task_done.invoke(
            {"task_id": tid},
            config=_config("supervisor-S"),
        )
        assert gm.require_goal("u1", gid).consecutive_rejections == 0


class TestProvideReviewFeedback:
    def test_only_supervisor_can_provide(self, gm):
        gid = _seed_active_goal(gm)
        tid = gm.require_goal("u1", gid).tasks[0].task_id
        result = provide_review_feedback.invoke(
            {"task_id": tid, "feedback": "wrong"},
            config=_config("worker-X"),
        )
        assert "[Error]" in result
        assert "supervisor" in result.lower()

    def test_records_feedback_and_increments_rejection(self, gm):
        gid = _seed_active_goal(gm)
        tid = gm.require_goal("u1", gid).tasks[0].task_id
        gm.set_task_status("u1", gid, tid, "awaiting_review")

        result = provide_review_feedback.invoke(
            {"task_id": tid, "feedback": "missed the edge case"},
            config=_config("supervisor-S"),
        )
        assert "[Refinement requested]" in result
        task = gm.require_goal("u1", gid).get_task(tid)
        assert task.review_notes == ["missed the edge case"]
        assert task.status == "in_progress"
        assert gm.require_goal("u1", gid).consecutive_rejections == 1

    def test_empty_feedback_rejected(self, gm):
        gid = _seed_active_goal(gm)
        tid = gm.require_goal("u1", gid).tasks[0].task_id
        result = provide_review_feedback.invoke(
            {"task_id": tid, "feedback": "   "},
            config=_config("supervisor-S"),
        )
        assert "cannot be empty" in result

    def test_auto_pauses_after_consecutive_rejection_limit(self, gm):
        gid = _seed_active_goal(gm)
        tid = gm.require_goal("u1", gid).tasks[0].task_id
        # Default limit is 3
        for i in range(3):
            gm.set_task_status("u1", gid, tid, "awaiting_review")
            result = provide_review_feedback.invoke(
                {"task_id": tid, "feedback": f"attempt {i+1}"},
                config=_config("supervisor-S"),
            )
        assert "auto-paused" in result.lower()
        assert gm.require_goal("u1", gid).status == "paused"
