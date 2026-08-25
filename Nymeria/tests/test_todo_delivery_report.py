"""Tests for TODO delivery-outcome accounting (backlog #247).

``POST /todos/{todo_id}/delivery-report`` lets a chat bot report whether a
scheduled TODO turn's output actually reached the chat. Failed deliveries
accumulate on a streak PARALLEL to the #154 execution streak (the ticker
resets that one before the bot finishes delivering) and drive the same
owner-alert / auto-pause escalation; a delivered report resets the streak,
and an explicit reschedule of a paused todo clears the whole episode.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nymeria.core import delivery_accounting
from nymeria.core.accounts import AccountsRepo
from nymeria.core.todo_manager import TodoManager
from nymeria.core.todo_schedule_db import TodoScheduleDB


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")

    def sync_agent_tools(self):
        pass


@pytest.fixture()
def alerts(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    sent: list[dict] = []

    def _record(message, settings, *, user_id, thread_id="", task_id=None):
        sent.append(
            {"message": message, "user_id": user_id, "task_id": task_id}
        )

    monkeypatch.setattr(delivery_accounting, "send_owner_alert", _record)
    return sent


def _admin_client(tmp_path: Path, api_client_builder):
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    client, token = api_client_builder.authenticated_client(
        agent, settings, user_id="admin", role="admin"
    )
    # The TODO owner exists as a regular account so Act-As checks resolve.
    agent.accounts_repo.create_user(
        "owner", "owner@example.com", "Owner", role="user"
    )
    return client, agent, api_client_builder.auth(token)


def _make_todo(
    tmp_path: Path,
    *,
    user_id: str = "owner",
    recurrence: str | None = "1d",
    scheduled: bool = True,
) -> str:
    tm = TodoManager(tmp_path)
    with tm.atomic_update(user_id) as todo_list:
        created = todo_list.add_item(
            "Morning check-in",
            scheduled_for=(
                datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
                if scheduled
                else None
            ),
            thread_id="telegram_123",
            recurrence=recurrence,
            notes="Say something nice",
        )
    assert created is not None
    return created.id


def _report(client, headers, todo_id: str, outcome: str, **overrides):
    body = {
        "outcome": outcome,
        "platform": "telegram",
        "target": "chat 123",
        "error": "Chat not found (the recipient has never started this bot)",
        "thread_id": "telegram_123",
        **overrides,
    }
    return client.post(
        f"/todos/{todo_id}/delivery-report", json=body, headers=headers
    )


def _scheduled_row_count(data_dir: Path, todo_id: str) -> int:
    with sqlite3.connect(data_dir / "todo_schedule.db") as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM scheduled_todos WHERE todo_id = ?",
            (todo_id,),
        ).fetchone()
    return row[0]


def test_failed_report_increments_delivery_streak_not_execution_streak(
    tmp_path: Path, api_client_builder, alerts
):
    """#247 behavior 2: the delivery streak is parallel; the #154 execution
    fields stay untouched, and the state rides the wire like #154's does."""
    client, agent, headers = _admin_client(tmp_path, api_client_builder)
    todo_id = _make_todo(tmp_path)

    resp = _report(client, headers, todo_id, "failed")
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "failed"
    assert body["delivery_failures"] == 1
    assert body["paused"] is False

    tm = TodoManager(tmp_path)
    todo = tm.get_todo_by_id("owner", todo_id)
    assert todo.delivery_failures == 1
    assert "Chat not found" in todo.last_delivery_failure
    assert todo.last_delivery_failure_at is not None
    assert todo.consecutive_failures == 0  # execution streak untouched
    assert todo.last_failure is None
    assert todo.schedule_paused_at is None

    # Owner-side wire visibility (admin acts as the owner for the check).
    listed = client.get(
        "/todos", headers={**headers, "X-Nymeria-Act-As": "owner"}
    )
    assert listed.status_code == 200
    row = next(i for i in listed.json()["items"] if i["id"] == todo_id)
    assert row["delivery_failures"] == 1
    assert "Chat not found" in row["last_delivery_failure"]


def test_owner_alert_fires_at_threshold_only(
    tmp_path: Path, api_client_builder, alerts
):
    """#247 behavior 3: alert exactly at scheduler_failure_alert_after (2)."""
    client, _agent, headers = _admin_client(tmp_path, api_client_builder)
    todo_id = _make_todo(tmp_path)

    _report(client, headers, todo_id, "failed")
    assert alerts == []
    resp = _report(client, headers, todo_id, "failed")
    assert resp.json()["delivery_failures"] == 2
    assert len(alerts) == 1
    assert "could not be delivered" in alerts[0]["message"]
    assert "telegram chat 123" in alerts[0]["message"]
    assert alerts[0]["user_id"] == "owner"
    assert alerts[0]["task_id"] == todo_id
    # Streak keeps counting past the alert without re-alerting.
    _report(client, headers, todo_id, "failed")
    assert len(alerts) == 1


def test_pause_at_threshold_clears_schedule_keeps_recurrence(
    tmp_path: Path, api_client_builder, alerts
):
    """#247 behavior 4: pause at scheduler_failure_pause_after (5), exactly
    the #154 pause shape (marker, note prefix, schedule row removed,
    recurrence kept), plus the pause alert."""
    client, _agent, headers = _admin_client(tmp_path, api_client_builder)
    todo_id = _make_todo(tmp_path)
    tm = TodoManager(tmp_path)
    tm.sync_schedule_to_db("owner", todo_id, TodoScheduleDB(tmp_path / "todo_schedule.db"))
    assert _scheduled_row_count(tmp_path, todo_id) == 1

    for _ in range(4):
        _report(client, headers, todo_id, "failed")
    resp = _report(client, headers, todo_id, "failed")
    assert resp.json() == {
        "outcome": "failed",
        "delivery_failures": 5,
        "alerted": True,
        "paused": True,
    }

    todo = tm.get_todo_by_id("owner", todo_id)
    assert todo.schedule_paused_at is not None
    assert todo.scheduled_for is None
    assert todo.recurrence == "1d"
    assert todo.notes.startswith("[auto-paused after 5 consecutive undelivered runs")
    assert "Say something nice" in todo.notes
    assert _scheduled_row_count(tmp_path, todo_id) == 0
    assert "[SCHEDULED TASK PAUSED]" in alerts[-1]["message"]


def test_pause_is_idempotent_across_replayed_reports(
    tmp_path: Path, api_client_builder, alerts
):
    """A replayed failed report after the pause must not stack a second
    note prefix (resume strips only one) or fire a second pause alert."""
    client, _agent, headers = _admin_client(tmp_path, api_client_builder)
    todo_id = _make_todo(tmp_path)
    for _ in range(5):
        _report(client, headers, todo_id, "failed")
    pause_alerts = [a for a in alerts if "PAUSED" in a["message"]]
    assert len(pause_alerts) == 1

    resp = _report(client, headers, todo_id, "failed")
    assert resp.status_code == 200
    assert resp.json()["paused"] is False

    tm = TodoManager(tmp_path)
    todo = tm.get_todo_by_id("owner", todo_id)
    assert todo.notes.count("[auto-paused after") == 1
    assert len([a for a in alerts if "PAUSED" in a["message"]]) == 1


def test_delivered_report_resets_the_streak(
    tmp_path: Path, api_client_builder, alerts
):
    """#247 behavior 5: delivered (or partial) ends the episode."""
    client, _agent, headers = _admin_client(tmp_path, api_client_builder)
    todo_id = _make_todo(tmp_path)
    _report(client, headers, todo_id, "failed")

    resp = _report(client, headers, todo_id, "delivered", error=None)
    assert resp.json()["delivery_failures"] == 0

    tm = TodoManager(tmp_path)
    todo = tm.get_todo_by_id("owner", todo_id)
    assert todo.delivery_failures == 0
    assert todo.last_delivery_failure is None
    assert todo.last_delivery_failure_at is None


def test_one_shot_failed_delivery_alerts_immediately(
    tmp_path: Path, api_client_builder, alerts
):
    """#247 behavior 7: no next occurrence to accumulate on."""
    client, _agent, headers = _admin_client(tmp_path, api_client_builder)
    todo_id = _make_todo(tmp_path, recurrence=None)

    _report(client, headers, todo_id, "failed")
    assert len(alerts) == 1
    assert "[SCHEDULED TASK NOT DELIVERED]" in alerts[0]["message"]
    tm = TodoManager(tmp_path)
    assert tm.get_todo_by_id("owner", todo_id).delivery_failures == 1


def test_explicit_reschedule_clears_delivery_episode(
    tmp_path: Path, api_client_builder, alerts
):
    """#247 behavior 6: the #154 resume semantics extend to the delivery
    trio and strip the pause note."""
    client, _agent, headers = _admin_client(tmp_path, api_client_builder)
    todo_id = _make_todo(tmp_path)
    for _ in range(5):
        _report(client, headers, todo_id, "failed")
    tm = TodoManager(tmp_path)
    assert tm.get_todo_by_id("owner", todo_id).schedule_paused_at is not None

    resp = client.patch(
        f"/todos/{todo_id}",
        json={"scheduled_for": "2h"},
        headers={**headers, "X-Nymeria-Act-As": "owner"},
    )
    assert resp.status_code == 200
    todo = tm.get_todo_by_id("owner", todo_id)
    assert todo.schedule_paused_at is None
    assert todo.delivery_failures == 0
    assert todo.last_delivery_failure is None
    assert todo.last_delivery_failure_at is None
    assert todo.notes == "Say something nice"


def test_unknown_todo_404_and_non_admin_rejected(
    tmp_path: Path, api_client_builder, alerts
):
    client, agent, headers = _admin_client(tmp_path, api_client_builder)
    todo_id = _make_todo(tmp_path)

    resp = _report(client, headers, "no-such-todo", "failed")
    assert resp.status_code == 404
    assert alerts == []

    agent.accounts_repo.create_user(
        "plain", "plain@example.com", "Plain", role="user"
    )
    plain_headers = api_client_builder.auth(
        agent.accounts_repo.issue_token("plain")
    )
    resp = _report(client, plain_headers, todo_id, "failed")
    assert resp.status_code == 403
    tm = TodoManager(tmp_path)
    assert tm.get_todo_by_id("owner", todo_id).delivery_failures == 0
