"""Regression tests for AGENT-015 ticker extraction.

Validates that the extracted helpers (`_TodoConsoleRenderer`,
`_stream_todo_execution`, `_run_continuation_pass`,
`_finalize_successful_execution`, `_handle_execution_failure`) preserve
the same behavior as the original monolithic `_execute_scheduled_todo`.
"""

from __future__ import annotations

import ast
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nymeria.core.ticker import (
    Ticker,
    _TodoConsoleRenderer,
    _should_continue_after_limit,
    _stream_error_message,
)
from nymeria.core.todo_manager import TodoManager
from nymeria.core.todo_schedule_db import ScheduledTodoEntry, TodoScheduleDB
from nymeria.core.turn_executor import LocalAgentExecutor
from nymeria.core.user_profile import UserProfileManager
from nymeria.core import ticker as ticker_module


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def test_stream_error_message_prefers_content():
    assert _stream_error_message({"content": "oops", "code": "500"}) == "oops"


def test_stream_error_message_falls_back_to_code():
    assert "code=500" in _stream_error_message({"content": "", "code": "500"})


def test_should_continue_after_limit_main_agent():
    assert _should_continue_after_limit(
        {"scope": "main_agent", "reason": "max_iterations"}
    )


def test_should_continue_rejects_sub_agent():
    assert not _should_continue_after_limit(
        {"scope": "sub_agent", "reason": "max_iterations"}
    )


def test_should_continue_rejects_repeated_tool_result():
    assert not _should_continue_after_limit(
        {"scope": "main_agent", "reason": "repeated_tool_result"}
    )


# ------------------------------------------------------------------
# _TodoConsoleRenderer
# ------------------------------------------------------------------


class TestTodoConsoleRenderer:
    def test_render_chunk_buffers_response(self):
        r = _TodoConsoleRenderer()
        r.render_chunk({"type": "response", "content": "hello "})
        r.render_chunk({"type": "response", "content": "world"})
        assert r.response_buffer == "hello world"

    def test_render_chunk_tracks_tool_calls(self):
        r = _TodoConsoleRenderer()
        r.render_chunk({
            "type": "tool_call",
            "id": "c1",
            "name": "search",
            "args": {"q": "test"},
        })
        assert "c1" in r.pending_calls
        assert r.pending_calls["c1"]["name"] == "search"

    def test_render_chunk_marks_had_tool_calls_on_result(self):
        r = _TodoConsoleRenderer()
        r.render_chunk({"type": "tool_result", "id": "c1", "result": "ok"})
        assert r.had_tool_calls is True

    def test_flush_response_buffer_clears_buffer(self):
        r = _TodoConsoleRenderer()
        r.response_buffer = "some text"
        r.flush_response_buffer()
        assert r.response_buffer == ""

    def test_flush_remaining_clears_buffer(self):
        r = _TodoConsoleRenderer()
        r.response_buffer = "leftover"
        r.flush_remaining()
        assert r.response_buffer == ""


# ------------------------------------------------------------------
# Structural: _execute_scheduled_todo delegates to extracted helpers
# ------------------------------------------------------------------


def test_execute_scheduled_todo_under_100_lines():
    """Acceptance criterion: orchestrator body is <100 lines."""
    src = Path(__file__).resolve().parent.parent / "nymeria" / "core" / "ticker.py"
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_execute_scheduled_todo":
            body_lines = node.end_lineno - node.lineno + 1
            assert body_lines < 100, (
                f"_execute_scheduled_todo is {body_lines} lines, should be <100"
            )
            return
    pytest.fail("_execute_scheduled_todo not found in ticker.py")


def test_execute_delegates_to_extracted_helpers():
    """The orchestrator calls the expected helper methods."""
    src = Path(__file__).resolve().parent.parent / "nymeria" / "core" / "ticker.py"
    tree = ast.parse(src.read_text())

    expected_calls = {
        "_print_wakeup_banner",
        "_stream_todo_execution",
        "_finalize_successful_execution",
        "_handle_execution_failure",
    }
    found_calls: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_execute_scheduled_todo":
            for child in ast.walk(node):
                if isinstance(child, ast.Attribute) and child.attr in expected_calls:
                    found_calls.add(child.attr)

    missing = expected_calls - found_calls
    assert not missing, f"_execute_scheduled_todo does not call: {missing}"


def test_stream_todo_returns_completed_early_flag():
    """_stream_todo_execution returns 3-tuple with completed_early bool."""
    src = Path(__file__).resolve().parent.parent / "nymeria" / "core" / "ticker.py"
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_stream_todo_execution":
            returns = [
                n for n in ast.walk(node)
                if isinstance(n, ast.Return) and isinstance(n.value, ast.Tuple)
            ]
            assert returns, "_stream_todo_execution should return tuples"
            for ret in returns:
                assert len(ret.value.elts) == 3, (
                    "_stream_todo_execution should return 3-tuples "
                    "(result, renderer, completed_early)"
                )
            return
    pytest.fail("_stream_todo_execution not found")


def test_no_nested_function_defs_in_orchestrator():
    """Nested closures have been extracted; none should remain."""
    src = Path(__file__).resolve().parent.parent / "nymeria" / "core" / "ticker.py"
    tree = ast.parse(src.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_execute_scheduled_todo":
            nested = [
                n.name
                for n in ast.walk(node)
                if isinstance(n, ast.FunctionDef) and n.name != "_execute_scheduled_todo"
            ]
            assert not nested, (
                f"Nested function defs found in _execute_scheduled_todo: {nested}"
            )
            return
    pytest.fail("_execute_scheduled_todo not found")


# ------------------------------------------------------------------
# Integration: continuation + double-limit backoff
# ------------------------------------------------------------------


class FakeThreadConfigManager:
    def get_config(self, thread_id: str):
        return None


class FakeSettings:
    data_dir = Path("/tmp")
    context_management = "none"
    sliding_window_cycles = 5
    max_results = 10
    notification_level = "none"


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.todo_manager = TodoManager(data_dir)
        self.thread_config_manager = FakeThreadConfigManager()
        self.profile_manager = UserProfileManager(data_dir)
        self.settings = FakeSettings()
        self.settings.data_dir = data_dir
        self._schedule_db = TodoScheduleDB(data_dir / "todo_schedule.db")

    def sync_agent_tools(self):
        pass


def _make_ticker(tmp_path: Path):
    agent = FakeAgent(tmp_path)
    ticker = Ticker(
        executor=LocalAgentExecutor(agent),
        settings=agent.settings,
        schedule_db=agent._schedule_db,
        todo_manager=agent.todo_manager,
        thread_config_manager=agent.thread_config_manager,
        profile_manager=agent.profile_manager,
        busy_agent=agent,
        spawn_sweeper=None,
    )
    return ticker, agent


def _add_todo(agent: FakeAgent, user_id: str = "owner"):
    with agent.todo_manager.atomic_update(user_id) as todo_list:
        return todo_list.add_item(
            "Do the thing",
            scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
            thread_id="thread-1",
            created_by="user",
        )


def test_continuation_pass_merges_results(tmp_path: Path, monkeypatch):
    """After an initial iteration_limit, the continuation result is merged."""
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent)
    # The entry slot mirrors the item's scheduled_for, as at a real fire
    # (the schedule row is always derived from the item): a divergent slot
    # would read as a mid-run schedule rewrite and skip finalize's re-arm.
    entry = ScheduledTodoEntry(
        todo_id=todo.id,
        user_id="owner",
        thread_id="thread-1",
        scheduled_for=todo.scheduled_for.timestamp(),
        task_preview=todo.task,
        created_at=time.time(),
    )

    call_count = 0

    async def fake_astream(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield {"type": "response", "content": "part1 "}
            yield {
                "type": "iteration_limit",
                "scope": "main_agent",
                "reason": "max_iterations",
                "max_iterations": 25,
                "tool_call_count": 25,
            }
        else:
            yield {"type": "response", "content": "part2"}

    agent.astream = fake_astream

    events = []
    monkeypatch.setattr(
        ticker_module, "publish_autonomous_event",
        lambda **kw: events.append(kw) if "event_type" in kw else events.append(kw),
    )
    monkeypatch.setattr(
        ticker_module, "publish_autonomous_event",
        lambda event_type, **kw: events.append({"event_type": event_type, **kw}),
    )
    monkeypatch.setattr(ticker_module, "publish_agent_stream_chunk", lambda *a, **kw: None)
    monkeypatch.setattr(ticker_module, "log_activity", lambda *a, **kw: None)
    monkeypatch.setattr(ticker_module, "create_autonomous_notification", lambda **kw: None)

    ticker._execute_scheduled_todo(entry)

    assert call_count == 2
    completed = [e for e in events if e.get("event_type") == "task_completed"]
    assert len(completed) == 1
    assert "part1 part2" in completed[0]["data"]["content"]


def test_double_iteration_limit_triggers_backoff(tmp_path: Path, monkeypatch):
    """Two consecutive iteration limits reschedule 10 min ahead."""
    ticker, agent = _make_ticker(tmp_path)
    todo = _add_todo(agent)
    # Entry slot mirrors the item (see test_continuation_pass_merges_results).
    entry = ScheduledTodoEntry(
        todo_id=todo.id,
        user_id="owner",
        thread_id="thread-1",
        scheduled_for=todo.scheduled_for.timestamp(),
        task_preview=todo.task,
        created_at=time.time(),
    )

    async def fake_astream(**kwargs):
        yield {"type": "response", "content": "partial"}
        yield {
            "type": "iteration_limit",
            "scope": "main_agent",
            "reason": "max_iterations",
            "max_iterations": 25,
            "tool_call_count": 25,
        }

    agent.astream = fake_astream

    events = []
    monkeypatch.setattr(
        ticker_module, "publish_autonomous_event",
        lambda event_type, **kw: events.append({"event_type": event_type, **kw}),
    )
    monkeypatch.setattr(ticker_module, "publish_agent_stream_chunk", lambda *a, **kw: None)
    monkeypatch.setattr(ticker_module, "log_activity", lambda *a, **kw: None)
    monkeypatch.setattr(ticker_module, "create_autonomous_notification", lambda **kw: None)

    ticker._execute_scheduled_todo(entry)

    completed = [e for e in events if e.get("event_type") == "task_completed"]
    assert len(completed) == 1
    assert "Rescheduled" in completed[0]["data"]["content"]

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed.scheduled_for > datetime.now(timezone.utc) + timedelta(minutes=9)


def test_check_triggers_isolates_per_user_failure(tmp_path: Path, monkeypatch):
    """F11: one user's failed trigger check (e.g. a disk error now surfaced by
    ``atomic_update``) must not abort the rest of the poll cycle."""
    ticker, _agent = _make_ticker(tmp_path)
    processed: list[str] = []

    class _FakeTriggerManager:
        def get_all_users_with_triggers(self):
            return ["u1", "u2"]

        def check_triggers(self, user_id, agent=None):
            processed.append(user_id)
            if user_id == "u1":
                raise RuntimeError("Failed to persist triggers for user u1")
            return []

    monkeypatch.setattr(ticker, "_get_trigger_manager", lambda: _FakeTriggerManager())

    # Must not raise; u2 is still processed after u1's failure.
    ticker._check_triggers()
    assert processed == ["u1", "u2"]


def test_archive_isolates_per_user_failure(tmp_path: Path, monkeypatch):
    """F11: one user's failed archival save must not abort the sweep for the
    remaining users."""
    import contextlib

    ticker, _agent = _make_ticker(tmp_path)
    processed: list[str] = []

    class _FakeList:
        def archive_completed(self, days_old):
            return 0

    class _FakeTodoManager:
        def get_all_users_with_todos(self):
            return ["u1", "u2"]

        @contextlib.contextmanager
        def atomic_update(self, user_id):
            processed.append(user_id)
            if user_id == "u1":
                raise RuntimeError("Failed to persist TODOs for user u1")
            yield _FakeList()

    monkeypatch.setattr(ticker, "todo_manager", _FakeTodoManager())

    # Must not raise; u2 is still processed after u1's failure.
    ticker._archive_completed_todos()
    assert processed == ["u1", "u2"]


# ------------------------------------------------------------------
# Workflow TODOs (phase 4): the headless run_workflow branch
# ------------------------------------------------------------------


class _FakeWorkflowExecutor:
    """TurnExecutor stand-in recording run_workflow calls."""

    is_remote = False

    def __init__(self, envelope: dict | None = None) -> None:
        self.envelope = envelope or {"ok": True, "status": "ok"}
        self.calls: list[dict] = []

    async def run_workflow(self, workflow_id, params=None, *, user_id, thread_id=None):
        self.calls.append(
            {
                "workflow_id": workflow_id,
                "params": dict(params or {}),
                "user_id": user_id,
                "thread_id": thread_id,
            }
        )
        return dict(self.envelope)


def _add_workflow_todo(agent: FakeAgent, *, recurrence: str | None = None):
    with agent.todo_manager.atomic_update("owner") as todo_list:
        return todo_list.add_item(
            "Run the report workflow",
            scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
            thread_id="thread-1",
            created_by="user",
            recurrence=recurrence,
            workflow_id="wf_report",
            workflow_params={"channel": "ops"},
        )


def _entry_for(todo) -> ScheduledTodoEntry:
    # Entry slot mirrors the item's scheduled_for, as at a real fire; a
    # divergent slot reads as a mid-run schedule rewrite and is honored
    # instead of re-armed.
    return ScheduledTodoEntry(
        todo_id=todo.id,
        user_id="owner",
        thread_id="thread-1",
        scheduled_for=todo.scheduled_for.timestamp(),
        task_preview=todo.task,
        created_at=time.time(),
    )


def _silence_ticker_io(monkeypatch, events: list):
    monkeypatch.setattr(
        ticker_module, "publish_autonomous_event",
        lambda event_type, **kw: events.append({"event_type": event_type, **kw}),
    )
    monkeypatch.setattr(ticker_module, "log_activity", lambda *a, **kw: None)
    monkeypatch.setattr(
        ticker_module, "create_autonomous_notification", lambda **kw: None
    )


def test_workflow_todo_runs_headlessly_and_completes(tmp_path: Path, monkeypatch):
    """No agent stream; the executor seam is called and the TODO closes."""
    ticker, agent = _make_ticker(tmp_path)
    executor = _FakeWorkflowExecutor()
    ticker._turn_executor = executor
    todo = _add_workflow_todo(agent)
    events: list = []
    _silence_ticker_io(monkeypatch, events)

    ticker._execute_scheduled_todo(_entry_for(todo))

    assert executor.calls == [
        {
            "workflow_id": "wf_report",
            "params": {"channel": "ops"},
            "user_id": "owner",
            "thread_id": "thread-1",
        }
    ]
    completed = [e for e in events if e["event_type"] == "task_completed"]
    assert len(completed) == 1
    assert "finished ok" in completed[0]["data"]["content"]
    # A workflow TODO has no agent turn to close it: the branch marks the
    # non-recurring item done itself.
    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed.status.value == "done"


def test_workflow_todo_recurring_reschedules(tmp_path: Path, monkeypatch):
    ticker, agent = _make_ticker(tmp_path)
    ticker._turn_executor = _FakeWorkflowExecutor()
    todo = _add_workflow_todo(agent, recurrence="1h")
    events: list = []
    _silence_ticker_io(monkeypatch, events)

    ticker._execute_scheduled_todo(_entry_for(todo))

    refreshed = agent.todo_manager.get_todo_by_id("owner", todo.id)
    assert refreshed.status.value == "pending"
    assert refreshed.scheduled_for is not None


def test_workflow_todo_needs_approval_counts_as_success(tmp_path: Path, monkeypatch):
    ticker, agent = _make_ticker(tmp_path)
    ticker._turn_executor = _FakeWorkflowExecutor(
        {"ok": False, "status": "needs_approval", "resume_token": "rec-1"}
    )
    todo = _add_workflow_todo(agent)
    events: list = []
    _silence_ticker_io(monkeypatch, events)

    ticker._execute_scheduled_todo(_entry_for(todo))

    completed = [e for e in events if e["event_type"] == "task_completed"]
    assert len(completed) == 1
    assert "awaiting your approval" in completed[0]["data"]["content"]
    assert todo.id not in ticker._retry_counts


def test_workflow_todo_error_envelope_engages_retry(tmp_path: Path, monkeypatch):
    ticker, agent = _make_ticker(tmp_path)
    ticker._turn_executor = _FakeWorkflowExecutor(
        {
            "ok": False,
            "status": "error",
            "error": {"kind": "author_error", "message": "boom"},
        }
    )
    todo = _add_workflow_todo(agent)
    events: list = []
    _silence_ticker_io(monkeypatch, events)

    ticker._execute_scheduled_todo(_entry_for(todo))

    # The failure handler engaged: error event published, retry scheduled.
    errored = [
        e
        for e in events
        if e["event_type"] == "task_completed" and e["data"].get("error")
    ]
    assert len(errored) == 1
    assert "author_error" in errored[0]["data"]["error_message"]
    assert ticker._retry_counts.get(todo.id) == 1


# ---------------------------------------------------------------------------
# Watchdog sweep sub-loop (backlog #78 fold)
# ---------------------------------------------------------------------------


def test_dream_sweep_noop_without_sweeper(tmp_path: Path):
    """The Docker worker shape: dream_sweeper=None makes the sweep inert."""
    ticker, _agent = _make_ticker(tmp_path)

    assert ticker._dream_sweeper is None
    ticker._run_dream_sweep()  # must not raise


def test_dream_sweep_cadence_and_error_isolation(tmp_path: Path):
    """Slim shape: the injected sweeper runs on the 600s cadence, errors isolated."""
    agent = FakeAgent(tmp_path)
    calls: list[int] = []

    def sweeper() -> int:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("sweep exploded")
        return len(calls)

    ticker = Ticker(
        executor=LocalAgentExecutor(agent),
        settings=agent.settings,
        schedule_db=agent._schedule_db,
        todo_manager=agent.todo_manager,
        thread_config_manager=agent.thread_config_manager,
        profile_manager=agent.profile_manager,
        busy_agent=agent,
        spawn_sweeper=None,
        dream_sweeper=sweeper,
    )

    now = time.time()
    # First due check fires (no housekeeping executor -> runs inline).
    assert ticker._maybe_submit_dream_sweep(now) is True
    assert len(calls) == 1
    # Within the 600s interval: skipped.
    assert ticker._maybe_submit_dream_sweep(now + 10) is False
    assert len(calls) == 1
    # Past the interval: fires again; the sweeper's error must not propagate.
    assert ticker._maybe_submit_dream_sweep(now + 601) is True
    assert len(calls) == 2


def test_watchdog_sweep_disabled_without_setting(tmp_path: Path):
    """FakeSettings carries no watchdog_enabled, so the sub-loop stays off."""
    ticker, _agent = _make_ticker(tmp_path)

    assert ticker._watchdog_sweep is None
    assert ticker._maybe_submit_watchdog_sweep(time.time()) is False
    assert ticker.watchdog_stats() == {"enabled": False}


def test_watchdog_sweep_cadence_and_running_guard(tmp_path: Path):
    agent = FakeAgent(tmp_path)
    agent.settings.watchdog_enabled = True
    agent.settings.watchdog_interval_minutes = 5
    agent.settings.todo_staleness_minutes = 20
    ticker = Ticker(
        executor=LocalAgentExecutor(agent),
        settings=agent.settings,
        schedule_db=agent._schedule_db,
        todo_manager=agent.todo_manager,
        thread_config_manager=agent.thread_config_manager,
        profile_manager=agent.profile_manager,
        busy_agent=agent,
        spawn_sweeper=None,
    )
    assert ticker._watchdog_sweep is not None

    calls: list = []
    ticker._watchdog_sweep.run_cycle = lambda pool: calls.append(pool)  # type: ignore[method-assign]

    now = time.time()
    # First due check fires (no housekeeping executor -> runs inline).
    assert ticker._maybe_submit_watchdog_sweep(now) is True
    assert len(calls) == 1
    # Within the interval: skipped.
    assert ticker._maybe_submit_watchdog_sweep(now + 10) is False
    assert len(calls) == 1
    # Past the interval (5m): fires again.
    assert ticker._maybe_submit_watchdog_sweep(now + 301) is True
    assert len(calls) == 2
    # Stats surface the sweep's counters via the ticker accessor.
    assert ticker.watchdog_stats()["enabled"] is True
