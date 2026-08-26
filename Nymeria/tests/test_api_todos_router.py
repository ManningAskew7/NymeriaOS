from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


from nymeria.api.routers.todos import _reschedule_recurring_done
from nymeria.core.accounts import AccountsRepo
from nymeria.core.todo_manager import TodoList, TodoManager, TodoStatus


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.accounts_repo = AccountsRepo(data_dir / "accounts.db")
        self.synced_tools = 0

    def sync_agent_tools(self):
        self.synced_tools += 1


def _client(tmp_path: Path, api_client_builder):
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)
    return api_client_builder.client(agent, settings), agent


def _create_user(agent: FakeAgent, user_id: str, *, role: str = "user") -> str:
    agent.accounts_repo.create_user(
        user_id,
        f"{user_id}@example.com",
        user_id.title(),
        role=role,
    )
    return agent.accounts_repo.issue_token(user_id)


def _scheduled_row_count(data_dir: Path, todo_id: str) -> int:
    with sqlite3.connect(data_dir / "todo_schedule.db") as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM scheduled_todos WHERE todo_id = ?",
            (todo_id,),
        ).fetchone()
    return row[0]


def _scheduled_row_time(data_dir: Path, todo_id: str) -> datetime:
    """The armed slot as the ticker's ``get_due`` sees it, not as the JSON says."""
    with sqlite3.connect(data_dir / "todo_schedule.db") as conn:
        row = conn.execute(
            "SELECT scheduled_for FROM scheduled_todos WHERE todo_id = ?",
            (todo_id,),
        ).fetchone()
    assert row is not None, "TODO fell out of the schedule index"
    return datetime.fromtimestamp(row[0], timezone.utc)


def test_reschedule_recurring_done_adopts_and_pins_month_origin(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    """The REST complete / PATCH-to-done reschedule path (shared with the
    slash-command completion) must derive month-end slots from a stable origin,
    not the previous clamped slot. Regression guard for the drift bug on the
    primary user-facing "mark done" flow."""
    _client(tmp_path, api_client_builder)  # initialises settings/data_dir
    tm = TodoManager(tmp_path)
    jan31 = datetime(2026, 1, 31, 11, 0, tzinfo=timezone.utc)
    # Freeze the clock mid-September: the next origin-pinned slot (Sep 30) is
    # CLAMPED (origin day 31), so the origin-vs-creep arithmetic below stays
    # discriminating on every date this suite runs, not only in clamped
    # months. Unfrozen, the expected slots drift with the wall clock and in
    # six months of the year the creep implementation gives the same answer.
    frozen_now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "nymeria.core.todo_constants.utc_now", lambda: frozen_now
    )
    with tm.atomic_update("owner") as todo_list:
        created = todo_list.add_item(
            "Month-end report",
            scheduled_for=jan31,
            thread_id="thread-1",
            recurrence="1mo",
        )
    assert created is not None
    todo_id = created.id

    with tm.atomic_update("owner") as todo_list:
        item = todo_list.get_item(todo_id)
        _reschedule_recurring_done(todo_list, item, todo_id)
        first_slot = item.scheduled_for
        # Deterministic under the frozen clock: the first future slot in the
        # jan31 monthly series is Sep 30 (origin day 31 clamped to 30).
        assert first_slot == datetime(2026, 9, 30, 11, 0, tzinfo=timezone.utc)
        # Origin adopted from the first slot (the ship-blocker fix): before, the
        # REST path never set recurrence_anchor.
        assert item.recurrence_anchor == jan31

    # Completing again with no intervening run recomputes the SAME slot: the
    # anchor is the occurrence just completed (last_execution), so a second
    # "done" for one occurrence no longer consumes a second slot. Before the
    # 2026-08-26 anchor fix this advanced again, which is the same arithmetic
    # that ate a day whenever the ticker's re-arm got there first.
    with tm.atomic_update("owner") as todo_list:
        item = todo_list.get_item(todo_id)
        _reschedule_recurring_done(todo_list, item, todo_id)
        assert item.recurrence_anchor == jan31
        assert item.scheduled_for == first_slot

    # A real second occurrence (the ticker fired first_slot and stamped it as
    # last_execution) does advance, by exactly one calendar month, and must NOT
    # re-adopt the origin to the clamped slot: the series stays pinned to the
    # Jan-31 origin so month-end slots cannot creep downward.
    with tm.atomic_update("owner") as todo_list:
        item = todo_list.get_item(todo_id)
        item.last_execution = first_slot
        _reschedule_recurring_done(todo_list, item, todo_id)
        assert item.recurrence_anchor == jan31
        # Origin-pinned: the slot after Sep 30 is Oct 31 (origin day 31).
        # The clamp-creep this test guards (origin re-adopted from the
        # clamped slot, or anchor plus one month) lands Oct 30 instead.
        assert item.scheduled_for == datetime(
            2026, 10, 31, 11, 0, tzinfo=timezone.utc
        )


def test_paused_todo_state_is_wire_visible_and_done_does_not_resume(
    tmp_path: Path,
    api_client_builder,
):
    """#154: the failure-policy fields ride the response (api.md documents
    them), and completing a paused recurring TODO must NOT silently resume
    it (the done re-arm writes scheduled_for, which would trip the
    resume-clear and erase the pause marker plus the streak)."""
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    headers = api_client_builder.auth(token)
    tm = TodoManager(tmp_path)
    now = datetime.now(timezone.utc)
    with tm.atomic_update("owner") as todo_list:
        created = todo_list.add_item(
            "Morning briefing",
            thread_id="thread-1",
            recurrence="1d",
            notes="Include weather",
        )
        assert created is not None
        item = todo_list.get_item(created.id)
        item.consecutive_failures = 5
        item.last_failure = "provider exploded"
        item.last_failure_at = now
        item.schedule_paused_at = now
    todo_id = created.id

    listed = client.get("/todos", headers=headers)
    assert listed.status_code == 200
    row = next(i for i in listed.json()["items"] if i["id"] == todo_id)
    assert row["consecutive_failures"] == 5
    assert row["last_failure"] == "provider exploded"
    assert row["last_failure_at"] is not None
    assert row["schedule_paused_at"] is not None

    done = client.post(f"/todos/{todo_id}/complete", headers=headers)
    assert done.status_code == 200
    body = done.json()
    assert body["status"] == "done"
    assert body["schedule_paused_at"] is not None
    assert body["consecutive_failures"] == 5
    assert body["scheduled_for"] is None
    assert _scheduled_row_count(tmp_path, todo_id) == 0


def test_todo_crud_routes_filter_reschedule_and_sync_schedule_db(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    headers = api_client_builder.auth(token)

    created = client.post(
        "/todos",
        headers=headers,
        json={
            "task": "Check inbox",
            "notes": "Use dashboard route",
            "scheduled_for": "30m",
            "recurrence": "daily",
            "thread_id": "thread-1",
        },
    )
    assert created.status_code == 200
    body = created.json()
    todo_id = body["id"]
    assert body["task"] == "Check inbox"
    assert body["status"] == "pending"
    assert body["created_by"] == "user"
    assert body["thread_id"] == "thread-1"
    assert body["scheduled_for"] is not None
    assert _scheduled_row_count(tmp_path, todo_id) == 1

    counts = client.get("/todos/thread-counts", headers=headers)
    filtered = client.get(
        "/todos",
        headers=headers,
        params={"thread_id": "thread-1"},
    )
    invalid_filter = client.get(
        "/todos",
        headers=headers,
        params={"filter_status": "bogus"},
    )

    assert counts.status_code == 200
    assert counts.json() == {"thread-1": 1}
    assert filtered.status_code == 200
    assert filtered.json()["user_id"] == "owner"
    assert [item["id"] for item in filtered.json()["items"]] == [todo_id]
    assert invalid_filter.status_code == 400
    assert invalid_filter.json()["detail"] == "Invalid status filter 'bogus'"

    # Legacy "daily" alias is stored in canonical form ("1d").
    assert body["recurrence"] == "1d"

    completed = client.post(f"/todos/{todo_id}/complete", headers=headers)
    assert completed.status_code == 200
    completed_body = completed.json()
    assert completed_body["id"] == todo_id
    assert completed_body["status"] == "pending"
    assert completed_body["recurrence"] == "1d"
    assert completed_body["scheduled_for"] is not None
    assert completed_body["last_execution"] is not None
    assert _scheduled_row_count(tmp_path, todo_id) == 1

    deleted = client.delete(f"/todos/{todo_id}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json() == {"status": "ok", "deleted_id": todo_id}
    assert _scheduled_row_count(tmp_path, todo_id) == 0


def test_todo_routes_accept_arbitrary_recurrence_interval(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    headers = api_client_builder.auth(token)

    created = client.post(
        "/todos",
        headers=headers,
        json={
            "task": "Custom cadence",
            "scheduled_for": "30m",
            "recurrence": "2h",
            "thread_id": "thread-1",
        },
    )
    assert created.status_code == 200
    assert created.json()["recurrence"] == "2h"

    too_short = client.post(
        "/todos",
        headers=headers,
        json={
            "task": "Too fast",
            "scheduled_for": "30m",
            "recurrence": "30s",
        },
    )
    assert too_short.status_code == 400
    assert "60" in too_short.json()["detail"]


def test_todo_routes_are_effective_user_scoped_and_users_endpoint_remains(
    tmp_path: Path,
    api_client_builder,
):
    client, agent = _client(tmp_path, api_client_builder)
    owner_token = _create_user(agent, "owner")
    other_token = _create_user(agent, "other")
    admin_token = _create_user(agent, "admin", role="admin")
    owner_headers = api_client_builder.auth(owner_token)
    other_headers = api_client_builder.auth(other_token)
    admin_headers = api_client_builder.auth(admin_token)

    owner_todo = client.post(
        "/todos",
        headers=owner_headers,
        json={"task": "Owner task", "thread_id": "owner-thread"},
    )
    other_todo = client.post(
        "/todos",
        headers=other_headers,
        json={"task": "Other task", "thread_id": "other-thread"},
    )
    assert owner_todo.status_code == 200
    assert other_todo.status_code == 200
    manager = TodoManager(tmp_path)
    manager.save_todos(TodoList(user_id="empty-legacy"))

    owner_list_with_ignored_query = client.get(
        "/todos",
        headers=owner_headers,
        params={"user_id": "other", "filter_status": "all"},
    )
    users_denied = client.get("/todos/users", headers=owner_headers)
    users_with_todos = client.get("/todos/users", headers=admin_headers)

    assert owner_list_with_ignored_query.status_code == 200
    listed = owner_list_with_ignored_query.json()
    assert listed["user_id"] == "owner"
    assert [item["task"] for item in listed["items"]] == ["Owner task"]
    assert users_denied.status_code == 403
    assert users_with_todos.status_code == 200
    assert set(users_with_todos.json()) == {"owner", "other"}

    assert manager.get_todos("other").get_item(other_todo.json()["id"]) is not None


def test_patch_to_done_reschedules_recurring_todo(
    tmp_path: Path,
    api_client_builder,
):
    # The PATCH status=done path shares the recurrence-rollover helper with the
    # /complete path; assert it rolls the recurring TODO forward instead of
    # leaving it done (the call site that had no prior coverage).
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    headers = api_client_builder.auth(token)

    created = client.post(
        "/todos",
        headers=headers,
        json={
            "task": "Recurring via patch",
            "scheduled_for": "30m",
            "recurrence": "daily",
            "thread_id": "thread-1",
        },
    )
    assert created.status_code == 200
    todo_id = created.json()["id"]
    assert _scheduled_row_count(tmp_path, todo_id) == 1

    patched = client.patch(
        f"/todos/{todo_id}",
        headers=headers,
        json={"status": "done"},
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["status"] == "pending"
    assert body["recurrence"] == "1d"
    assert body["scheduled_for"] is not None
    assert body["last_execution"] is not None
    # Still scheduled after the rollover.
    assert _scheduled_row_count(tmp_path, todo_id) == 1


def test_patch_to_done_after_ticker_rearm_does_not_consume_the_armed_slot(
    tmp_path: Path,
    api_client_builder,
):
    """The REST done-path shares the late-completion defect with the tool path.

    Once the ticker has re-armed a recurring TODO (``scheduled_for`` already at
    day N+1, ``last_execution`` at the day N occurrence just run), a PATCH to
    ``done`` anchored on ``scheduled_for`` advanced to day N+2 and silently ate
    day N+1. The anchor is the occurrence being completed, so the armed slot
    must survive untouched. Covers ``POST /todos/{id}/complete`` too: both call
    ``_reschedule_recurring_done``.
    """
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    headers = api_client_builder.auth(token)
    tm = TodoManager(tmp_path)

    day_n = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3)
    day_n_plus_1 = day_n + timedelta(days=1)
    with tm.atomic_update("owner") as todo_list:
        created = todo_list.add_item(
            "Hound Alex about his meds",
            scheduled_for=day_n_plus_1,
            thread_id="thread-1",
            recurrence="1d",
        )
        assert created is not None
        created.last_execution = day_n
    todo_id = created.id

    patched = client.patch(
        f"/todos/{todo_id}",
        headers=headers,
        json={"status": "done"},
    )

    assert patched.status_code == 200
    body = patched.json()
    assert body["status"] == "pending"
    stored = tm.get_todos("owner").get_item(todo_id)
    assert stored is not None
    assert stored.scheduled_for == day_n_plus_1
    assert stored.last_execution == day_n
    assert _scheduled_row_count(tmp_path, todo_id) == 1
    assert _scheduled_row_time(tmp_path, todo_id) == day_n_plus_1


def test_reschedule_recurring_done_rolls_forward_and_records_anchor():
    todo_list = TodoList(user_id="owner")
    past_anchor = datetime.now(timezone.utc) - timedelta(hours=2)
    item = todo_list.add_item(task="ping", scheduled_for=past_anchor, recurrence="1h")
    assert item is not None
    original_schedule = item.scheduled_for
    item.status = TodoStatus.DONE

    _reschedule_recurring_done(todo_list, item, item.id)

    # Live in-list item is rolled forward to a future PENDING slot, with the
    # prior fire time recorded as the completion anchor.
    assert item.status == TodoStatus.PENDING
    assert item.last_execution == original_schedule
    assert item.scheduled_for is not None
    assert item.scheduled_for > original_schedule
    # The caller's reference and the stored item are the same object.
    assert todo_list.get_item(item.id) is item


def test_reschedule_recurring_done_is_noop_without_recurrence():
    todo_list = TodoList(user_id="owner")
    anchor = datetime.now(timezone.utc) - timedelta(hours=2)
    item = todo_list.add_item(task="ping", scheduled_for=anchor)
    assert item is not None
    item.status = TodoStatus.DONE

    _reschedule_recurring_done(todo_list, item, item.id)

    assert item.status == TodoStatus.DONE
    assert item.last_execution is None
    assert item.scheduled_for == anchor


def test_todo_create_workflow_binding_validation(
    tmp_path: Path,
    api_client_builder,
    monkeypatch,
):
    """Phase 4: workflow TODOs validate the binding at create time (400) and
    round-trip workflow_id/workflow_params through the response model."""
    client, agent = _client(tmp_path, api_client_builder)
    token = _create_user(agent, "owner")
    headers = api_client_builder.auth(token)

    monkeypatch.setattr(
        "nymeria.core.workflows.tool_runtime.workflow_binding_error",
        lambda wf, p, *, allow_event=False: f"no published workflow tool named {wf!r}",
    )
    rejected = client.post(
        "/todos",
        headers=headers,
        json={"task": "Run report", "scheduled_for": "30m", "workflow_id": "wf_x"},
    )
    assert rejected.status_code == 400
    assert "no published workflow tool" in rejected.json()["detail"]

    monkeypatch.setattr(
        "nymeria.core.workflows.tool_runtime.workflow_binding_error",
        lambda wf, p, *, allow_event=False: None,
    )
    created = client.post(
        "/todos",
        headers=headers,
        json={
            "task": "Run report",
            "scheduled_for": "30m",
            "workflow_id": "wf_x",
            "workflow_params": {"channel": "ops"},
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["workflow_id"] == "wf_x"
    assert body["workflow_params"] == {"channel": "ops"}

    listed = client.get("/todos", headers=headers).json()
    assert listed["items"][0]["workflow_id"] == "wf_x"
