"""Tests for goal_manager: lifecycle transitions, authority check, circuit breakers."""

from __future__ import annotations

from pathlib import Path

import pytest

from nymeria.core.goal_manager import (
    GoalManager,
    GoalNotFoundError,
    GoalStateError,
)


@pytest.fixture
def gm(tmp_path: Path) -> GoalManager:
    return GoalManager(tmp_path)


class TestCreate:
    def test_creates_with_pending_approval_status(self, gm: GoalManager):
        goal = gm.create_goal("u1", "thread-A", "make pizza")
        assert goal.status == "pending_approval"
        assert goal.thread_id == "thread-A"
        assert goal.helper_thread_id is None
        assert goal.objective == "make pizza"
        assert goal.tasks == []
        assert goal.max_turns == 20

    def test_rejects_second_active_goal_on_same_thread(self, gm: GoalManager):
        gm.create_goal("u1", "thread-A", "first")
        with pytest.raises(GoalStateError, match="already has an active goal"):
            gm.create_goal("u1", "thread-A", "second")

    def test_allows_new_goal_after_previous_cleared(self, gm: GoalManager):
        g1 = gm.create_goal("u1", "thread-A", "first")
        gm.clear_goal("u1", g1.goal_id)
        g2 = gm.create_goal("u1", "thread-A", "second")
        assert g2.goal_id != g1.goal_id


class TestApprove:
    def test_approve_requires_tasks(self, gm: GoalManager):
        goal = gm.create_goal("u1", "thread-A", "obj")
        with pytest.raises(GoalStateError, match="no tasks"):
            gm.approve_goal("u1", goal.goal_id, "spawned-supervisor")

    def test_approve_transitions_to_active(self, gm: GoalManager):
        goal = gm.create_goal("u1", "thread-A", "obj")
        gm.add_task("u1", goal.goal_id, "task 1", criterion="done when X")
        approved = gm.approve_goal("u1", goal.goal_id, "spawned-supervisor-1")
        assert approved.status == "active"
        assert approved.helper_thread_id == "spawned-supervisor-1"

    def test_approve_rejects_non_pending(self, gm: GoalManager):
        goal = gm.create_goal("u1", "thread-A", "obj")
        gm.add_task("u1", goal.goal_id, "task 1")
        gm.approve_goal("u1", goal.goal_id, "supervisor-1")
        with pytest.raises(GoalStateError, match="cannot approve"):
            gm.approve_goal("u1", goal.goal_id, "supervisor-2")


class TestAuthority:
    def _active_goal(self, gm: GoalManager) -> str:
        goal = gm.create_goal("u1", "thread-A", "obj")
        gm.add_task("u1", goal.goal_id, "task 1")
        gm.approve_goal("u1", goal.goal_id, "supervisor-S")
        return goal.goal_id

    def test_supervisor_thread_has_authority(self, gm: GoalManager):
        gid = self._active_goal(gm)
        assert gm.can_authority("u1", gid, "supervisor-S") is True

    def test_worker_thread_has_no_authority(self, gm: GoalManager):
        gid = self._active_goal(gm)
        assert gm.can_authority("u1", gid, "thread-A") is False

    def test_third_thread_has_no_authority(self, gm: GoalManager):
        gid = self._active_goal(gm)
        assert gm.can_authority("u1", gid, "random-thread") is False

    def test_no_authority_before_approval(self, gm: GoalManager):
        goal = gm.create_goal("u1", "thread-A", "obj")
        assert gm.can_authority("u1", goal.goal_id, "thread-A") is False

    def test_unknown_goal_returns_false(self, gm: GoalManager):
        assert gm.can_authority("u1", "ffffffff", "any-thread") is False


class TestTasks:
    def _seed(self, gm: GoalManager) -> tuple[str, str]:
        goal = gm.create_goal("u1", "thread-A", "obj")
        task = gm.add_task("u1", goal.goal_id, "task 1", criterion="done when X")
        return goal.goal_id, task.task_id

    def test_add_task(self, gm: GoalManager):
        gid, tid = self._seed(gm)
        goal = gm.require_goal("u1", gid)
        assert len(goal.tasks) == 1
        assert goal.tasks[0].task_id == tid
        assert goal.tasks[0].status == "pending"

    def test_add_task_rejected_on_terminal_goal(self, gm: GoalManager):
        goal = gm.create_goal("u1", "thread-A", "obj")
        gm.clear_goal("u1", goal.goal_id)
        with pytest.raises(GoalStateError):
            gm.add_task("u1", goal.goal_id, "task")

    def test_set_status_increments_attempts_on_awaiting_review(self, gm: GoalManager):
        gid, tid = self._seed(gm)
        gm.set_task_status("u1", gid, tid, "in_progress")
        t = gm.set_task_status("u1", gid, tid, "awaiting_review")
        assert t.attempts == 1
        t = gm.set_task_status("u1", gid, tid, "in_progress")
        gm.set_task_status("u1", gid, tid, "awaiting_review")
        assert gm.require_goal("u1", gid).get_task(tid).attempts == 2

    def test_record_review_feedback_appends_and_resets_to_in_progress(
        self, gm: GoalManager
    ):
        gid, tid = self._seed(gm)
        gm.set_task_status("u1", gid, tid, "awaiting_review")
        t = gm.record_review_feedback("u1", gid, tid, "missed edge case")
        assert t.status == "in_progress"
        assert t.review_notes == ["missed edge case"]
        gm.set_task_status("u1", gid, tid, "awaiting_review")
        t = gm.record_review_feedback("u1", gid, tid, "still broken")
        assert t.review_notes == ["missed edge case", "still broken"]


class TestMarkGoalDone:
    def test_requires_all_tasks_done(self, gm: GoalManager):
        goal = gm.create_goal("u1", "thread-A", "obj")
        gm.add_task("u1", goal.goal_id, "t1")
        gm.add_task("u1", goal.goal_id, "t2")
        gm.approve_goal("u1", goal.goal_id, "s1")
        with pytest.raises(GoalStateError, match="unfinished tasks"):
            gm.mark_goal_done("u1", goal.goal_id)

    def test_marks_done_when_all_tasks_done(self, gm: GoalManager):
        goal = gm.create_goal("u1", "thread-A", "obj")
        t1 = gm.add_task("u1", goal.goal_id, "t1")
        gm.approve_goal("u1", goal.goal_id, "s1")
        gm.set_task_status("u1", goal.goal_id, t1.task_id, "done")
        marked = gm.mark_goal_done("u1", goal.goal_id)
        assert marked.status == "done"


class TestLifecycle:
    def _ready(self, gm: GoalManager):
        goal = gm.create_goal("u1", "thread-A", "obj")
        gm.add_task("u1", goal.goal_id, "t1")
        gm.approve_goal("u1", goal.goal_id, "s1")
        return goal.goal_id

    def test_pause_then_resume(self, gm: GoalManager):
        gid = self._ready(gm)
        gm.pause_goal("u1", gid, reason="user interrupt")
        assert gm.require_goal("u1", gid).status == "paused"
        resumed = gm.resume_goal("u1", gid)
        assert resumed.status == "active"
        assert resumed.consecutive_rejections == 0
        assert resumed.last_pause_reason is None

    def test_cannot_pause_terminal(self, gm: GoalManager):
        gid = self._ready(gm)
        gm.clear_goal("u1", gid)
        with pytest.raises(GoalStateError):
            gm.pause_goal("u1", gid)

    def test_resume_only_from_paused(self, gm: GoalManager):
        gid = self._ready(gm)
        with pytest.raises(GoalStateError):
            gm.resume_goal("u1", gid)


class TestCircuitBreakers:
    def _ready(self, gm: GoalManager, **kwargs):
        goal = gm.create_goal("u1", "thread-A", "obj", **kwargs)
        gm.add_task("u1", goal.goal_id, "t1")
        gm.approve_goal("u1", goal.goal_id, "s1")
        return goal.goal_id

    def test_max_turns_triggers_budget_limited(self, gm: GoalManager):
        gid = self._ready(gm, max_turns=3)
        for _ in range(2):
            g = gm.increment_turn("u1", gid)
            assert g.status == "active"
        g = gm.increment_turn("u1", gid)
        assert g.status == "budget_limited"
        assert "max_turns" in (g.last_pause_reason or "")

    def test_token_budget_triggers_budget_limited(self, gm: GoalManager):
        gid = self._ready(gm, token_budget=1000)
        gm.record_tokens("u1", gid, 500)
        assert gm.require_goal("u1", gid).status == "active"
        g = gm.record_tokens("u1", gid, 600)
        assert g.status == "budget_limited"

    def test_no_token_budget_means_unbounded(self, gm: GoalManager):
        gid = self._ready(gm)
        g = gm.record_tokens("u1", gid, 10_000_000)
        assert g.status == "active"

    def test_consecutive_rejections_pause_at_limit(self, gm: GoalManager):
        gid = self._ready(gm)
        for _ in range(2):
            g = gm.increment_rejection("u1", gid)
            assert g.status == "active"
        g = gm.increment_rejection("u1", gid)
        assert g.status == "paused"
        assert "rejections" in (g.last_pause_reason or "")

    def test_reset_rejection_clears_counter(self, gm: GoalManager):
        gid = self._ready(gm)
        gm.increment_rejection("u1", gid)
        gm.increment_rejection("u1", gid)
        gm.reset_rejection("u1", gid)
        g = gm.require_goal("u1", gid)
        assert g.consecutive_rejections == 0


class TestLookups:
    def test_get_active_goal_for_thread_excludes_terminal(self, gm: GoalManager):
        g1 = gm.create_goal("u1", "thread-A", "first")
        gm.clear_goal("u1", g1.goal_id)
        assert gm.get_active_goal_for_thread("u1", "thread-A") is None

        g2 = gm.create_goal("u1", "thread-A", "second")
        active = gm.get_active_goal_for_thread("u1", "thread-A")
        assert active is not None
        assert active.goal_id == g2.goal_id

    def test_get_active_goal_for_helper_finds_supervisor(self, gm: GoalManager):
        g = gm.create_goal("u1", "thread-A", "obj")
        gm.add_task("u1", g.goal_id, "t1")
        gm.approve_goal("u1", g.goal_id, "supervisor-S")
        found = gm.get_active_goal_for_helper("u1", "supervisor-S")
        assert found is not None
        assert found.goal_id == g.goal_id

    def test_persistence_across_manager_instances(self, tmp_path: Path):
        gm1 = GoalManager(tmp_path)
        goal = gm1.create_goal("u1", "thread-A", "persist me")

        gm2 = GoalManager(tmp_path)
        reloaded = gm2.require_goal("u1", goal.goal_id)
        assert reloaded.objective == "persist me"
        assert reloaded.thread_id == "thread-A"

    def test_unknown_goal_raises(self, gm: GoalManager):
        with pytest.raises(GoalNotFoundError):
            gm.require_goal("u1", "ffffffff")
