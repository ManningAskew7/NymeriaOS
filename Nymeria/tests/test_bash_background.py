from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, cast

from langchain_core.runnables import RunnableConfig

from nymeria.core.pending_prompt_queue import (
    PendingPromptQueueClosingError,
    get_pending_queue,
    reset_pending_queue_for_tests,
)
from nymeria.core.thread_lock_manager import ThreadLockManager
from nymeria.tools.bash import bash_execute
from nymeria.tools import bash_background as bg


class _Settings:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.nymeria_confine_file_to_workspace = False


class _AccountsRepo:
    def __init__(self, owner: str | None = "user-1"):
        self.owner = owner

    def get_thread_owner(self, thread_id: str) -> str | None:
        return self.owner


def _config(thread_id: str = "thread-1", user_id: str = "user-1") -> RunnableConfig:
    return cast(
        RunnableConfig,
        {"configurable": {"thread_id": thread_id, "user_id": user_id}},
    )


def _project_root(tmp_path: Path, monkeypatch) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(
        "nymeria.tools.execution_environment.get_settings",
        lambda: _Settings(project),
    )
    return project


def _job_id(result: str) -> str:
    match = re.search(r"job_id: ([0-9a-f]+)", result)
    assert match is not None
    return match.group(1)


def _bash_func() -> Callable[..., str]:
    return cast(Callable[..., str], getattr(bash_execute, "func"))


def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


def _record(
    tmp_path: Path,
    *,
    stdout: str = "hello\n",
    stderr: str = "warn\n",
    exit_code: int = 0,
) -> bg.BackgroundJobRecord:
    stdout_path = tmp_path / "job.stdout"
    stderr_path = tmp_path / "job.stderr"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return bg.BackgroundJobRecord(
        id="job123",
        pid=1234,
        command="echo hello",
        working_directory=str(tmp_path),
        started_at=time.time() - 1,
        finished_at=time.time(),
        exit_code=exit_code,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        status="completed",
        thread_id="thread-1",
        user_id="user-1",
    )


def _agent(
    locks: ThreadLockManager | None = None,
    owner: str | None = "user-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        _thread_locks=locks or ThreadLockManager(),
        accounts_repo=_AccountsRepo(owner),
    )


def setup_function(_function) -> None:
    bg.reset_registry_for_tests()
    reset_pending_queue_for_tests()


def teardown_function(_function) -> None:
    reset_pending_queue_for_tests()
    bg.reset_registry_for_tests()


def test_missing_thread_context_uses_legacy_background_path(
    tmp_path,
    monkeypatch,
):
    _project_root(tmp_path, monkeypatch)

    class FakeProc:
        pid = 4321

    monkeypatch.setattr(
        "nymeria.tools.bash.subprocess.Popen",
        lambda *args, **kwargs: FakeProc(),
    )

    result = _bash_func()("sleep 1", run_in_background=True)

    assert result == "Process started in background (PID: 4321)"
    assert bg.get_registry().records() == []


def test_background_launch_registers_job_and_temp_files(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)

    result = _bash_func()(
        "sleep 0.1",
        run_in_background=True,
        config=_config(),
    )

    record = bg.get_registry().get(_job_id(result))
    assert record is not None
    try:
        assert record.thread_id == "thread-1"
        assert record.user_id == "user-1"
        assert record.status in {"running", "completed"}
        assert Path(record.stdout_path).exists()
        assert Path(record.stderr_path).exists()
        assert stat_mode(record.stdout_path) == 0o600
        assert stat_mode(record.stderr_path) == 0o600
    finally:
        def completed() -> bool:
            refreshed = bg.get_registry().get(record.id)
            return refreshed is not None and refreshed.status == "completed"

        _wait_until(completed)
        bg.cleanup_output_files(record.stdout_path, record.stderr_path)


def test_tool_wrapper_injects_config_for_background_launch(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)

    result = bash_execute.invoke(
        {"command": "sleep 0.1", "run_in_background": True},
        config=_config(),
    )

    assert "job_id:" in result
    assert "Process started in background (PID:" not in result
    record = bg.get_registry().get(_job_id(result))
    assert record is not None
    try:
        assert record.thread_id == "thread-1"
        assert record.user_id == "user-1"

        def completed() -> bool:
            refreshed = bg.get_registry().get(record.id)
            return refreshed is not None and refreshed.status == "completed"

        _wait_until(completed)
    finally:
        bg.cleanup_output_files(record.stdout_path, record.stderr_path)


def test_watcher_marks_completed_and_submits_prompt(tmp_path, monkeypatch):
    _project_root(tmp_path, monkeypatch)
    submitted: list[bg.BackgroundJobRecord] = []
    monkeypatch.setattr(bg, "_submit_completion_prompt", submitted.append)

    result = _bash_func()(
        "sleep 0.1; echo hello; exit 1",
        run_in_background=True,
        config=_config(),
    )
    record = bg.get_registry().get(_job_id(result))
    assert record is not None

    try:
        _wait_until(lambda: bool(submitted), timeout=3.0)
        completed = submitted[0]
        assert completed.id == record.id
        assert completed.thread_id == "thread-1"
        assert completed.user_id == "user-1"
        assert completed.status == "completed"
        assert completed.exit_code == 1
        assert "hello" in Path(completed.stdout_path).read_text(encoding="utf-8")
    finally:
        bg.cleanup_output_files(record.stdout_path, record.stderr_path)


def test_busy_thread_enqueues_completion_prompt(tmp_path, monkeypatch):
    locks = ThreadLockManager()
    agent = _agent(locks)
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: agent)
    record = _record(tmp_path, exit_code=1)
    lock = locks.get_lock(record.thread_id)
    lock.acquire()

    try:
        bg._submit_completion_prompt(record)
        pending = get_pending_queue().drain(record.thread_id)
    finally:
        lock.release()

    assert len(pending) == 1
    prompt = pending[0]
    assert prompt.source == "background_bash"
    assert prompt.source_id == record.id
    assert prompt.user_id == record.user_id
    assert prompt.is_autonomous is True
    assert prompt.fanout_mailbox is None
    assert "exit_code=1" in prompt.message
    assert record.stdout_path in prompt.message
    assert record.stderr_path in prompt.message
    assert "hello" in prompt.message
    assert "warn" in prompt.message


def test_idle_thread_fires_autonomous_turn(tmp_path, monkeypatch):
    agent = _agent()
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: agent)
    fired: list[tuple[bg.BackgroundJobRecord, str, object]] = []
    monkeypatch.setattr(
        bg,
        "_fire_autonomous_turn",
        lambda record, prompt_text, agent: fired.append(
            (record, prompt_text, agent)
        ),
    )
    record = _record(tmp_path)

    bg._submit_completion_prompt(record)

    assert fired
    assert fired[0][0] is record
    assert "Background bash job job123 finished" in fired[0][1]
    assert get_pending_queue().drain(record.thread_id) == []


def test_abort_guard_drops_completion_prompt(tmp_path, monkeypatch, caplog):
    locks = ThreadLockManager()
    locks.signal_abort("thread-1")
    agent = _agent(locks)
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: agent)
    fired: list[object] = []
    monkeypatch.setattr(
        bg,
        "_fire_autonomous_turn",
        lambda *args, **kwargs: fired.append(args),
    )
    record = _record(tmp_path)

    with caplog.at_level(logging.INFO):
        bg._submit_completion_prompt(record)

    assert not fired
    assert get_pending_queue().drain(record.thread_id) == []
    assert "aborted" in caplog.text


def test_closing_queue_falls_back_to_idle_fire(tmp_path, monkeypatch):
    locks = ThreadLockManager()
    agent = _agent(locks)
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: agent)
    record = _record(tmp_path)

    class ClosingQueue:
        def enqueue(self, thread_id, pending):
            raise PendingPromptQueueClosingError("closing")

    monkeypatch.setattr(
        "nymeria.core.pending_prompt_queue.get_pending_queue",
        lambda: ClosingQueue(),
    )
    fired: list[tuple[bg.BackgroundJobRecord, str, object]] = []
    monkeypatch.setattr(
        bg,
        "_fire_autonomous_turn",
        lambda record, prompt_text, agent: fired.append(
            (record, prompt_text, agent)
        ),
    )
    lock = locks.get_lock(record.thread_id)
    lock.acquire()

    try:
        bg._submit_completion_prompt(record)
    finally:
        lock.release()

    assert fired


def test_owner_mismatch_drops_completion_prompt(tmp_path, monkeypatch):
    agent = _agent(owner=None)
    monkeypatch.setattr("nymeria.core.agent.get_current_agent", lambda: agent)
    fired: list[object] = []
    monkeypatch.setattr(
        bg,
        "_fire_autonomous_turn",
        lambda *args, **kwargs: fired.append(args),
    )

    bg._submit_completion_prompt(_record(tmp_path))

    assert not fired
    assert get_pending_queue().drain("thread-1") == []


def test_fire_autonomous_turn_publishes_bookends_and_activity(
    tmp_path,
    monkeypatch,
):
    agent = _agent()
    record = _record(tmp_path)
    events: list[tuple[str, str, str, str, dict]] = []
    chunks: list[tuple[dict, str, str, str]] = []
    activities: list[tuple[object, str, str, str, dict | None]] = []
    captured_kwargs: dict = {}

    def fake_publish_autonomous_event(
        event_type: str,
        thread_id: str,
        user_id: str,
        task_id: str,
        data: dict,
    ) -> None:
        events.append((event_type, thread_id, user_id, task_id, data))

    def fake_publish_agent_stream_chunk(
        chunk: dict,
        *,
        thread_id: str,
        user_id: str,
        task_id: str,
    ) -> bool:
        chunks.append((chunk, thread_id, user_id, task_id))
        return True

    class FakeStreamResult:
        iteration_limit_hit = False

        def response_text(self, *, fallback_to_thinking: bool = True) -> str:
            return "ack"

    def fake_stream_and_collect(
        agent_arg,
        *,
        astream_kwargs,
        on_chunk,
        error_message_factory,
    ):
        captured_kwargs.update(astream_kwargs)
        on_chunk({"type": "response", "content": "ack"}, SimpleNamespace(chunk_count=1))
        return FakeStreamResult()

    def fake_log_activity(activity_type, message, user_id, thread_id, metadata):
        activities.append((activity_type, message, user_id, thread_id, metadata))

    monkeypatch.setattr(
        "nymeria.core.event_bus.publish_autonomous_event",
        fake_publish_autonomous_event,
    )
    monkeypatch.setattr(
        "nymeria.core.event_bus.publish_agent_stream_chunk",
        fake_publish_agent_stream_chunk,
    )
    monkeypatch.setattr(
        "nymeria.core.stream_bridge.stream_and_collect",
        fake_stream_and_collect,
    )
    monkeypatch.setattr("nymeria.core.activity_log.log_activity", fake_log_activity)

    bg._fire_autonomous_turn(record, "prompt text", agent)

    assert captured_kwargs["source"] == "background_bash"
    assert captured_kwargs["source_id"] == record.id
    assert captured_kwargs["_is_self_invoke"] is True
    assert [event[0] for event in events] == ["task_started", "task_completed"]
    assert events[0][4]["source"] == "background_bash"
    assert events[1][4]["content"] == "ack"
    assert chunks[0][0] == {"type": "response", "content": "ack"}
    assert activities
    assert activities[0][2] == record.user_id
    assert activities[0][3] == record.thread_id


def stat_mode(path: str) -> int:
    return os.stat(path).st_mode & 0o777
