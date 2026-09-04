"""Tests for the ``/code`` command (user-driven Claude Code, no model in the loop).

The command reuses the ``claude_code`` tool's run builder and background
watcher, so the run itself is faked at ``tools.claude_code.prepare_run`` (the
handler imports it function-locally, so the module attribute is the seam) and
no real Claude Code is ever spawned. What is pinned here is the command's own
contract: the gates, the flag semantics, the inline-or-ack decision, ``/stop``
reaching the run, and the model-free delivery (history record under the
thread hold, a real turn stream buffer opened BEFORE ``task_started`` so bots
attach to this turn and not the previous one, the bookends, and the
notification fallback), never a model completion turn.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import nymeria.config as config_module
import nymeria.core.activity_log as activity_log_module
import nymeria.core.agent as agent_module
import nymeria.core.claude_code_overrides as overrides_module
import nymeria.core.event_bus as event_bus_module
import nymeria.core.notification_dispatch as notification_module
import nymeria.tools.claude_code_background as bg
from nymeria.core import claude_code_delivery as delivery
from nymeria.core import command_executor_claude_code as code_cmd
from nymeria.core.command_service import CommandContext, CommandService
from nymeria.core.pending_prompt_queue import reset_pending_queue_for_tests
from nymeria.core.thread_lock_manager import ThreadLockManager
from nymeria.core.turn_stream_buffer import (
    STATE_DONE,
    get_turn_stream_registry,
    reset_turn_stream_registry,
)
from nymeria.tools.claude_code_bridge import ClaudeCodeResult

from tests.test_command_service import _FakeAccountsRepo  # shared fake

# The package re-exports the TOOL under the module's name, so the module
# itself (the handler's import target) is reached through importlib.
claude_module = importlib.import_module("nymeria.tools.claude_code")
PreparedRun = claude_module.PreparedRun

THREAD = "telegram_42"


def run(coro):
    return asyncio.run(coro)


def _ctx(**overrides) -> CommandContext:
    base: dict[str, Any] = dict(
        user_id="owner",
        thread_id=THREAD,
        actor="user",
        surface="telegram",
        is_admin=True,
    )
    base.update(overrides)
    return CommandContext(**base)


class _Graph:
    def __init__(self) -> None:
        self.updates: list[tuple[dict, dict]] = []

    def update_state(self, config, values):
        self.updates.append((config, values))


class _Agent:
    """The slice of NymeriaAgent the command, the hold, and the tee touch."""

    def __init__(self) -> None:
        self._thread_locks = ThreadLockManager()
        self._default_graph = _Graph()
        self.thread_config_manager = None
        self.accounts_repo = _FakeAccountsRepo(default_role="admin")
        self.accounts_repo.owners[THREAD] = "owner"
        self.settings = SimpleNamespace(llm_model="test-model")
        self.patched: list[tuple[str, int]] = []  # (thread, updates-so-far)
        self.patch_raises = False
        self.aborted: list[str] = []

    def _patch_dangling_tool_calls(self, graph, config):
        if self.patch_raises:
            raise RuntimeError("checkpoint unreadable")
        self.patched.append((config["configurable"]["thread_id"], len(graph.updates)))
        return 0

    def get_context_stats(self, thread_id):
        return {"total_tokens": 1}

    def _get_llm_config_for_thread(self, thread_id):
        return SimpleNamespace(model=None)

    def abort_with_cascade(self, thread_id, *, restore_queue=False):
        self.aborted.append(thread_id)
        return []


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Fake settings, agent, run builder, and event sinks around one test."""
    settings = SimpleNamespace(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        nymeria_claude_code_url=None,
        nymeria_claude_code_roots=None,
        nymeria_claude_code_model=None,
        user_timezone="UTC",
    )
    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    agent = _Agent()
    monkeypatch.setattr(agent_module, "get_current_agent", lambda: agent)
    reset_turn_stream_registry()
    reset_pending_queue_for_tests()
    delivery.reset_registry_for_tests()

    events: list[dict] = []
    registry = get_turn_stream_registry()

    def fake_publish(**kw):
        # Pin what the bots will see when they attach on this event: the
        # thread's CURRENT buffer at publish time.
        current = registry.get(str(kw["thread_id"]))
        kw["buffer_at_publish"] = current.snapshot() if current else None
        events.append(kw)

    monkeypatch.setattr(event_bus_module, "publish_autonomous_event", fake_publish)
    chunks: list[dict] = []
    monkeypatch.setattr(
        event_bus_module,
        "publish_agent_stream_chunk",
        lambda chunk, **kw: chunks.append(chunk) or True,
    )
    activity: list[tuple] = []
    monkeypatch.setattr(
        activity_log_module,
        "log_activity",
        lambda *a, **kw: activity.append((a, kw)),
    )
    notifications: list[tuple] = []
    monkeypatch.setattr(
        notification_module,
        "create_in_app_notification",
        lambda *a, **kw: notifications.append((a, kw)) or "ok",
    )
    # The tool's model-relay path must never fire for /code.
    relays: list[Any] = []
    monkeypatch.setattr(bg, "_submit_completion_prompt", lambda job, report: relays.append(job))
    monkeypatch.setattr(bg, "INLINE_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(bg, "END_TURN_SETTLE_SECONDS", 0.2)
    bg.reset_jobs_for_tests()
    monkeypatch.setattr(code_cmd, "INLINE_WAIT_SECONDS", 0.5)

    calls: list[dict] = []
    state = SimpleNamespace(
        result=ClaudeCodeResult(ok=True, result_text="PONG", session_id="sess-1"),
        release=threading.Event(),
        resume_session_id=None,
    )
    state.release.set()

    def fake_prepare_run(settings_arg, **kwargs):
        calls.append(dict(kwargs))
        cancel = kwargs["abort_event"]

        def producer():
            state.release.wait(5)
            if cancel.is_set():
                return ClaudeCodeResult(
                    ok=False, is_error=True, error="cancelled", subtype="cancelled"
                )
            return state.result

        return PreparedRun(
            producer=producer,
            persist=lambda r: None,
            run_cwd=str(tmp_path),
            resume_session_id=state.resume_session_id,
        )

    monkeypatch.setattr(claude_module, "prepare_run", fake_prepare_run)
    monkeypatch.setattr(claude_module, "stored_session_id", lambda s, t, d: "sess-stored")

    yield SimpleNamespace(
        settings=settings,
        agent=agent,
        events=events,
        chunks=chunks,
        activity=activity,
        notifications=notifications,
        relays=relays,
        calls=calls,
        state=state,
        registry=registry,
    )
    # Never leave a blocked producer behind for the next test.
    state.release.set()
    _wait_for(lambda: delivery.active_job(THREAD) is None, 3.0)
    delivery.reset_registry_for_tests()
    reset_turn_stream_registry()
    reset_pending_queue_for_tests()


def _execute(raw: str, ctx: CommandContext | None = None):
    # No ``api``: the service builds its in-process backend client over the
    # (faked) current agent, which is what ``/stop`` dispatches through.
    return run(CommandService().execute(ctx or _ctx(), raw))


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _completed(env) -> bool:
    return any(e["event_type"] == "task_completed" for e in env.events)


def _buffer_events(buffer) -> list[dict]:
    return [json.loads(payload) for _, payload in buffer._entries_after(0)]


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #


def test_code_is_refused_for_non_admins_agents_and_threadless_callers(env) -> None:
    denied = _execute("/code fix it", _ctx(is_admin=False))
    assert denied.success is False
    assert "requires an admin user" in denied.markdown

    agent = _execute("/code fix it", _ctx(actor="agent", source="agent"))
    assert agent.success is False
    assert "not available to the agent" in agent.markdown

    threadless = _execute("/code fix it", _ctx(thread_id=None))
    assert threadless.success is False
    assert "requires an active thread" in threadless.markdown

    assert env.calls == [], "no run may start behind a refused gate"


def test_unknown_admin_verdict_is_refused_by_the_handler(env) -> None:
    # The dispatcher only refuses a definite False; this command is host RCE,
    # so the handler itself refuses None (fail closed).
    unknown = _execute("/code fix it", _ctx(is_admin=None))
    assert unknown.success is False
    assert "requires an admin user" in unknown.markdown
    assert env.calls == []


# --------------------------------------------------------------------------- #
# Dispatch semantics
# --------------------------------------------------------------------------- #


def test_quick_run_replies_inline_and_records_history_under_the_hold(env) -> None:
    result = _execute("/code reply PONG")

    assert result.success is True, result.markdown
    assert "PONG" in result.markdown
    assert "session_id: sess-1" in result.markdown
    assert result.data["session_id"] == "sess-1"
    assert result.data.get("detached") is None

    (call,) = env.calls
    assert call["prompt"] == "reply PONG"
    assert call["cli_mode"] == "bypassPermissions"
    assert call["resume"] is True
    assert call["thread_id"] == THREAD
    # The run's cancel signal is its own event, not the thread abort flag.
    assert isinstance(call["abort_event"], threading.Event)
    assert call["abort_event"] is not env.agent._thread_locks.get_abort_event(THREAD)

    # The exchange is recorded: a hidden wake-up carrying the output as
    # data, then the relayed text as the assistant message, with dangling
    # tool calls patched BEFORE the write (the patch saw zero updates).
    (config, values) = env.agent._default_graph.updates[0]
    assert config["configurable"]["thread_id"] == THREAD
    human, ai = values["messages"]
    assert isinstance(human, HumanMessage)
    assert human.additional_kwargs.get("internal_type") == "autonomous_wakeup"
    assert "[Trigger: Claude Code]" in human.content
    assert "PONG" in human.content
    assert human.id
    assert isinstance(ai, AIMessage) and "PONG" in ai.content
    assert env.agent.patched == [(THREAD, 0)]

    # The hold is released cleanly and the ledger records the job.
    locks = env.agent._thread_locks
    assert locks.get_lock(THREAD).locked() is False
    assert locks.get_lock_info(THREAD) is None
    assert env.activity[0][0][0].value == "task_completed"
    assert delivery.last_outcome(THREAD) == (result.data["job_id"], "completed")

    # Inline replies publish no bookends and never wake the model.
    assert env.events == []
    assert env.relays == []


def test_thread_mode_override_beats_the_bypass_default_but_not_an_explicit_mode(
    env, monkeypatch
) -> None:
    monkeypatch.setattr(
        overrides_module,
        "get_effective_claude_code_mode",
        lambda thread_id, default, **kw: "dont_ask",
    )
    _execute("/code fix it")
    assert env.calls[-1]["cli_mode"] == "dontAsk"

    _execute("/code --mode bypass fix it")
    assert env.calls[-1]["cli_mode"] == "bypassPermissions"


def test_new_flag_mode_option_and_dir_reach_the_run_builder(env) -> None:
    result = _execute("/code --new --mode plan --dir sub/dir draft a plan for --verbose output")

    assert result.success is True, result.markdown
    (call,) = env.calls
    assert call["resume"] is False
    assert call["cli_mode"] == "plan"
    assert call["working_dir"] == "sub/dir"
    # Once the rest param starts, option-looking words belong to the prompt.
    assert call["prompt"] == "draft a plan for --verbose output"


def test_resume_flag_is_accepted_and_keeps_the_default(env) -> None:
    result = _execute("/code --resume yes, do it")
    assert result.success is True, result.markdown
    assert env.calls[0]["resume"] is True
    assert env.calls[0]["prompt"] == "yes, do it"


def test_invalid_mode_is_rejected_before_any_run(env) -> None:
    result = _execute("/code --mode yolo fix it")
    assert result.success is False
    assert env.calls == []


def test_failed_run_is_an_error_reply(env) -> None:
    env.state.result = ClaudeCodeResult(ok=False, is_error=True, error="runner unreachable")
    result = _execute("/code fix it")
    assert result.success is False
    assert "failed" in result.markdown
    assert "runner unreachable" in result.markdown
    assert delivery.last_outcome(THREAD)[1] == "failed"


def test_inline_record_is_skipped_when_a_turn_holds_the_thread(env, monkeypatch) -> None:
    monkeypatch.setattr(delivery, "INLINE_HOLD_SECONDS", 0.1)
    lock = env.agent._thread_locks.get_lock(THREAD)
    assert lock.acquire(blocking=False)
    try:
        result = _execute("/code fix it")
        assert result.success is True, result.markdown
        assert "PONG" in result.markdown
        assert env.agent._default_graph.updates == []
        assert env.activity[0][1]["metadata"]["history"] is False
    finally:
        lock.release()


# --------------------------------------------------------------------------- #
# Long runs: ack, then model-free delivery as a holder turn
# --------------------------------------------------------------------------- #


def test_long_run_acks_then_delivers_as_a_holder_turn_without_a_model(env) -> None:
    env.state.release.clear()
    env.state.resume_session_id = "sess-prev"

    ack = _execute("/code refactor everything")

    assert ack.success is True, ack.markdown
    assert ack.data["detached"] is True
    job_id = ack.data["job_id"]
    assert f"job {job_id}" in ack.markdown
    assert "resuming session sess-prev" in ack.markdown
    assert "/code <reply>" in ack.markdown
    assert env.events == [], "nothing is published until the run finishes"
    assert delivery.active_job(THREAD) is not None

    env.state.result = ClaudeCodeResult(
        ok=True, result_text="Plan: 1. do X. Shall I proceed?", session_id="sess-2"
    )
    env.state.release.set()
    assert _wait_for(lambda: _completed(env))

    started, completed = env.events
    task_id = f"claude-code-{job_id}"
    assert started["event_type"] == "task_started"
    assert (started["thread_id"], started["task_id"]) == (THREAD, task_id)
    assert started["data"]["source"] == "claude_code"
    assert completed["task_id"] == task_id
    assert "Shall I proceed?" in completed["data"]["content"]
    assert completed["data"]["session_id"] == "sess-2"
    assert "error" not in completed["data"]

    # The turn stream buffer (what bots and the desktop attach to) is THIS
    # turn, opened before task_started went out, and holds the result.
    buffer = env.registry.get(THREAD)
    assert buffer is not None
    assert started["buffer_at_publish"]["turn_id"] == buffer.turn_id
    assert started["buffer_at_publish"]["state"] == "live"
    snap = buffer.snapshot()
    assert snap["state"] == STATE_DONE
    assert snap["holder_kind"] == "autonomous"
    assert snap["user_message_internal"] is True
    wire = _buffer_events(buffer)
    assert [e["type"] for e in wire] == ["turn_started", "response", "done"]
    assert "Shall I proceed?" in wire[1]["content"]
    # The legacy firehose path saw the same chunk.
    assert env.chunks == [{"type": "response", "content": completed["data"]["content"]}]

    # History was written under the hold, anchored to the buffer.
    (_, values) = env.agent._default_graph.updates[0]
    human, ai = values["messages"]
    assert human.id == snap["user_message_id"]
    assert "Shall I proceed?" in ai.content

    locks = env.agent._thread_locks
    assert locks.get_lock(THREAD).locked() is False
    assert locks.get_lock_info(THREAD) is None
    assert env.relays == []
    assert env.notifications == []
    assert env.activity and env.activity[0][0][0].value == "task_completed"
    assert delivery.active_job(THREAD) is None
    assert delivery.last_outcome(THREAD) == (job_id, "completed")


def test_delivery_replaces_a_retained_previous_turn_buffer(env) -> None:
    # A chat turn finished moments ago: its buffer is retained, and a bot
    # attaching on task_started with no turn_id gets the thread's CURRENT
    # buffer. Delivery must have replaced it with the /code turn by then.
    previous = env.registry.begin_turn(THREAD, "owner", holder_kind="user")
    previous.append({"type": "response", "content": "PREVIOUS TURN TEXT"})
    previous.finish(STATE_DONE)

    env.state.release.clear()
    _execute("/code fix it")
    env.state.release.set()
    assert _wait_for(lambda: _completed(env))

    started = env.events[0]
    assert started["buffer_at_publish"]["turn_id"] != previous.turn_id
    current = env.registry.get(THREAD)
    assert current is not previous
    assert "PREVIOUS TURN TEXT" not in json.dumps(_buffer_events(current))
    assert "PONG" in _buffer_events(current)[1]["content"]


def test_failed_long_run_delivers_the_failure_as_content(env) -> None:
    env.state.release.clear()
    _execute("/code fix it")
    env.state.result = ClaudeCodeResult(ok=False, is_error=True, error="budget exceeded")
    env.state.release.set()
    assert _wait_for(lambda: _completed(env))

    completed = env.events[-1]
    assert "failed" in completed["data"]["content"]
    assert "budget exceeded" in completed["data"]["content"]
    # The delivery itself succeeded: the ledger, not the bookend, carries
    # the job outcome.
    assert "error" not in completed["data"]
    assert env.activity[0][0][0].value == "task_failed"
    assert delivery.last_outcome(THREAD)[1] == "failed"


def test_stop_cancels_the_run_and_the_result_stays_silent(env) -> None:
    env.state.release.clear()
    ack = _execute("/code fix it")
    job_id = ack.data["job_id"]

    stopped = _execute("/stop")
    assert stopped.success is True, stopped.markdown
    assert f"Claude Code job {job_id}" in stopped.markdown
    # No turn held the thread, so nothing was abort-cascaded; the run's own
    # cancel event carried the stop.
    assert env.agent.aborted == []
    assert env.calls[0]["abort_event"].is_set()

    env.state.release.set()
    assert _wait_for(lambda: delivery.active_job(THREAD) is None)
    time.sleep(0.2)
    assert env.events == []
    assert env.notifications == []
    assert env.agent._default_graph.updates == []
    assert delivery.last_outcome(THREAD) == (job_id, "cancelled")

    idle = _execute("/stop")
    assert "idle" in idle.markdown.lower()


def test_busy_thread_falls_back_to_a_notification(env, monkeypatch) -> None:
    monkeypatch.setattr(delivery, "IDLE_WAIT_SECONDS", 0.1)
    lock = env.agent._thread_locks.get_lock(THREAD)
    assert lock.acquire(blocking=False)
    try:
        env.state.release.clear()
        _execute("/code fix it")
        env.state.release.set()
        assert _wait_for(lambda: bool(env.notifications))
        assert _wait_for(lambda: any(e["event_type"] == "notification" for e in env.events))
    finally:
        lock.release()

    # Never a turn interleaved into the live one, never silent.
    assert not any(e["event_type"] in ("task_started", "task_completed") for e in env.events)
    assert env.agent._default_graph.updates == []
    (args, kwargs) = env.notifications[0]
    assert "PONG" in args[0] and args[1] == "owner" and args[2] == THREAD
    bus = next(e for e in env.events if e["event_type"] == "notification")
    assert "PONG" in bus["data"]["message"]
    assert "in_app_only" not in bus["data"]


def test_delivery_waits_for_a_live_turn_to_end(env, monkeypatch) -> None:
    monkeypatch.setattr(delivery, "IDLE_WAIT_SECONDS", 3.0)
    lock = env.agent._thread_locks.get_lock(THREAD)
    assert lock.acquire(blocking=False)
    env.state.release.clear()
    _execute("/code fix it")
    env.state.release.set()
    time.sleep(0.3)
    assert env.events == [], "must not publish while the turn holds the lock"
    lock.release()
    assert _wait_for(lambda: _completed(env))
    assert env.notifications == []
    assert len(env.agent._default_graph.updates) == 1


def test_unrepairable_tail_skips_the_history_write_but_still_delivers(env) -> None:
    env.agent.patch_raises = True
    env.state.release.clear()
    _execute("/code fix it")
    env.state.release.set()
    assert _wait_for(lambda: _completed(env))
    assert env.agent._default_graph.updates == []
    assert "PONG" in env.events[-1]["data"]["content"]
    assert [e["type"] for e in _buffer_events(env.registry.get(THREAD))] == [
        "turn_started",
        "response",
        "done",
    ]


def test_second_dispatch_is_refused_while_a_run_is_in_flight(env) -> None:
    env.state.release.clear()
    first = _execute("/code fix it")
    assert first.data["detached"] is True

    second = _execute("/code and this too")
    assert second.success is False
    assert f"job {first.data['job_id']} is still running" in second.markdown
    assert len(env.calls) == 1

    env.state.release.set()
    assert _wait_for(lambda: delivery.active_job(THREAD) is None)


def test_claim_is_atomic_across_a_concurrent_dispatch(env, monkeypatch) -> None:
    # A job claimed by another dispatch after this one's active check must
    # still refuse this one at the claim (check-and-set under one lock).
    other = bg.ClaudeCodeJob(
        id="other1",
        thread_id=THREAD,
        user_id="owner",
        prompt="x",
        cwd="/",
        mode="plan",
        started_at=time.time(),
        detached_message="",
    )
    original_active = delivery.active_job

    def active_then_claimed(thread_id):
        found = original_active(thread_id)
        if found is None:
            delivery.claim(other)
        return found

    monkeypatch.setattr(delivery, "active_job", active_then_claimed)
    result = _execute("/code fix it")
    assert result.success is False
    assert "job other1" in result.markdown
    assert env.events == []


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #


def test_bare_code_reports_session_and_run_state(env) -> None:
    idle = _execute("/code")
    assert idle.success is True, idle.markdown
    assert "No Claude Code run is in flight" in idle.markdown
    assert "resumes session sess-stored" in idle.markdown
    assert idle.data["session_id"] == "sess-stored"
    assert env.calls == []

    env.state.release.clear()
    ack = _execute("/code fix it")
    running = _execute("/code")
    assert f"job {ack.data['job_id']} is running" in running.markdown
    assert running.data["active_job"]["id"] == ack.data["job_id"]

    env.state.release.set()
    assert _wait_for(lambda: delivery.active_job(THREAD) is None)
    done = _execute("/code")
    assert f"Last job {ack.data['job_id']} completed" in done.markdown


def test_help_lists_code_with_its_flags() -> None:
    card = _execute("/help code")
    assert card.success is True, card.markdown
    assert "## /code" in card.markdown
    assert "--new" in card.markdown
    assert "--mode" in card.markdown


def test_every_end_turn_of_a_long_run_is_relayed_without_a_model(env, monkeypatch) -> None:
    """A /code run that ends a turn early ("suite running, will pick up") and
    a second one at exit delivers BOTH to the chat, each as its own
    model-free holder turn, tagged interim then final."""
    from nymeria.tools.claude_code_bridge import RunObserver

    second = threading.Event()

    def streaming_prepare_run(settings_arg, **kwargs):
        observer = RunObserver()

        def producer():
            observer.feed_event({"type": "system", "subtype": "init", "session_id": "sess-multi"})
            observer.feed_event({"type": "result", "subtype": "success",
                                 "result": "suite running, will pick up"})
            second.wait(5)
            observer.feed_event({"type": "result", "subtype": "success", "result": "all green"})
            return ClaudeCodeResult(ok=True, result_text="all green", session_id="sess-multi",
                                    end_turns=observer.turns_after(0))

        return PreparedRun(
            producer=producer, persist=lambda r: None, run_cwd="/repo",
            resume_session_id=None, observer=observer,
        )

    monkeypatch.setattr(claude_module, "prepare_run", streaming_prepare_run)
    # The first end-turn settles only after the command's inline wait.
    monkeypatch.setattr(code_cmd, "INLINE_WAIT_SECONDS", 0.05)
    ack = _execute("/code long job")
    assert ack.data["detached"] is True
    job_id = ack.data["job_id"]

    assert _wait_for(lambda: len(env.events) >= 2)
    started, completed = env.events[:2]
    assert started["task_id"] == f"claude-code-{job_id}-t1"
    assert started["data"]["report"] == "interim"
    assert "interim end-turn 1" in completed["data"]["content"]
    assert "suite running, will pick up" in completed["data"]["content"]
    assert f"job {job_id} (session sess-multi)" in completed["data"]["content"]
    assert delivery.active_job(THREAD) is not None, "the run is still in flight"
    # The interim exchange is in history, framed as data with the tag line.
    (_, values) = env.agent._default_graph.updates[0]
    human, _ai = values["messages"]
    assert "INTERIM end-turn 1" in human.content and "/code" in human.content

    second.set()
    assert _wait_for(lambda: len(env.events) >= 4)
    started2, completed2 = env.events[2:4]
    assert started2["task_id"] == f"claude-code-{job_id}"
    assert started2["data"]["report"] == "final"
    assert "finished after" in completed2["data"]["content"]
    assert "2 end-turn(s)" in completed2["data"]["content"]
    assert "all green" in completed2["data"]["content"]
    assert "suite running" not in completed2["data"]["content"]
    assert _wait_for(lambda: delivery.active_job(THREAD) is None)
    assert delivery.last_outcome(THREAD) == (job_id, "completed")
    assert env.relays == [] and env.notifications == []


def test_quick_interim_reply_is_info_and_the_final_still_lands(env, monkeypatch) -> None:
    """The command's 20s wait can return an INTERIM end-turn: the reply says
    so, the registry keeps the run, and the final is delivered later."""
    from nymeria.tools.claude_code_bridge import RunObserver

    finish = threading.Event()

    def streaming_prepare_run(settings_arg, **kwargs):
        observer = RunObserver()

        def producer():
            observer.feed_event({"type": "result", "subtype": "success",
                                 "result": "reviewers running", "session_id": "sess-q"})
            finish.wait(5)
            return ClaudeCodeResult(ok=True, result_text="reviewers running", session_id="sess-q",
                                    end_turns=observer.turns_after(0))

        return PreparedRun(
            producer=producer, persist=lambda r: None, run_cwd="/repo",
            resume_session_id=None, observer=observer,
        )

    monkeypatch.setattr(claude_module, "prepare_run", streaming_prepare_run)
    monkeypatch.setattr(code_cmd, "INLINE_WAIT_SECONDS", 2.0)
    reply = _execute("/code review this")
    assert reply.success is True
    assert reply.data.get("interim") is True and reply.data["session_id"] == "sess-q"
    assert "interim end-turn 1" in reply.markdown and "reviewers running" in reply.markdown
    assert delivery.active_job(THREAD) is not None
    assert env.events == []

    finish.set()
    assert _wait_for(lambda: _completed(env))
    final = [e for e in env.events if e["event_type"] == "task_completed"][0]
    assert "final message was end-turn 1, delivered earlier" in final["data"]["content"]
    assert _wait_for(lambda: delivery.active_job(THREAD) is None)
