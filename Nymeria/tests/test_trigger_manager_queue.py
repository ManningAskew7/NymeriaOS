"""Test that TriggerManager.check_triggers enqueues busy-thread events
to the pending-prompt queue (sub-turn steering) instead of stashing
them in trigger.pending_events (the legacy defer path)."""

from __future__ import annotations

from typing import Literal

import pytest

from nymeria.core.pending_prompt_queue import (
    create_pending_queue,
    reset_pending_queue_for_tests,
    set_pending_queue,
)
from nymeria.core.trigger_manager import (
    TriggerAction,
    TriggerDefinition,
    TriggerManager,
)


class _FakeThreadLocks:
    """Minimal lock manager surface used by TriggerManager.check_triggers."""

    def __init__(self, busy_threads: set[str] | None = None) -> None:
        self._busy = busy_threads or set()
        self._lock_info: dict[str, dict] = {}

    def is_thread_busy(self, thread_id: str) -> bool:
        return thread_id in self._busy

    def get_lock_info(self, thread_id: str) -> dict:
        return self._lock_info.get(thread_id, {"holder": "autonomous", "held_seconds": 5.0})


class _FakeAgent:
    def __init__(self, busy_threads: set[str] | None = None) -> None:
        self._thread_locks = _FakeThreadLocks(busy_threads)


class _StaticSource:
    """A trigger source that returns a fixed list of events on check."""

    def __init__(self, events: list[dict]) -> None:
        self._events = list(events)

    def check(self, source_config: dict, state: dict, user_id: str = "") -> list[dict]:
        return list(self._events)


@pytest.fixture
def isolated_queue():
    backend = create_pending_queue(None)
    set_pending_queue(backend)
    try:
        yield backend
    finally:
        reset_pending_queue_for_tests()


@pytest.fixture
def patched_sources(monkeypatch):
    """Wire AVAILABLE_SOURCES so ``get_source`` returns our fake."""
    from nymeria.triggers import sources as sources_module

    holder: dict[str, _StaticSource] = {}

    def _get_source(name: str):
        return holder.get(name)

    monkeypatch.setattr(sources_module, "get_source", _get_source)
    monkeypatch.setattr(
        sources_module,
        "AVAILABLE_SOURCES",
        holder,  # name -> source map
        raising=False,
    )
    return holder


def _make_trigger(
    *,
    thread_id: str = "t1",
    action_type: Literal["agent_prompt", "create_todo", "notify"] = "agent_prompt",
):
    return TriggerDefinition(
        id="trig-1",
        name="My Trigger",
        source_type="static",
        source_config={},
        action=TriggerAction(
            type=action_type,
            config={"prompt_template": "Event: {body}", "thread_id": thread_id},
        ),
        thread_id=thread_id,
        enabled=True,
    )


def test_busy_thread_enqueues_pending_prompts(tmp_path, isolated_queue, patched_sources):
    patched_sources["static"] = _StaticSource([
        {"body": "hello"},
        {"body": "world"},
    ])

    tm = TriggerManager(tmp_path)
    user_id = "u1"
    trigger = _make_trigger(thread_id="t1")
    with tm.atomic_update(user_id) as store:
        store.triggers.append(trigger)

    agent = _FakeAgent(busy_threads={"t1"})

    results = tm.check_triggers(user_id=user_id, agent=agent)  # type: ignore[bad-argument-type]
    # No fire_action call should happen for this trigger; check_triggers
    # returned no actionable (trigger, events) pairs.
    assert results == []

    # Two prompts should be sitting in the queue under thread "t1".
    assert isolated_queue.size("t1") == 2
    drained = isolated_queue.drain("t1")
    assert [p.message for p in drained] == ["Event: hello", "Event: world"]
    assert all(p.source == "trigger" for p in drained)
    assert all(p.source_id == trigger.id for p in drained)
    assert all(p.source_label == trigger.name for p in drained)
    assert all(p.is_autonomous for p in drained)


def test_idle_thread_returns_events_for_fire_action(tmp_path, isolated_queue, patched_sources):
    patched_sources["static"] = _StaticSource([
        {"body": "tick"},
    ])

    tm = TriggerManager(tmp_path)
    user_id = "u1"
    trigger = _make_trigger(thread_id="t-idle")
    with tm.atomic_update(user_id) as store:
        store.triggers.append(trigger)

    # No agent passed -> thread-busy check is skipped entirely.
    results = tm.check_triggers(user_id=user_id, agent=None)
    assert len(results) == 1
    returned_trigger, events = results[0]
    assert returned_trigger.id == trigger.id
    assert events == [{"body": "tick"}]
    # Nothing was queued -- the caller will fire_action_batch instead.
    assert isolated_queue.size("t-idle") == 0


def test_atomic_update_skips_write_on_noop_poll(
    tmp_path, isolated_queue, patched_sources
):
    """A poll that produces no events and no state change must not rewrite
    the trigger JSON (F1: dirty-aware ``atomic_update``)."""
    # Source returns nothing and does not touch trigger.state.
    patched_sources["static"] = _StaticSource([])

    tm = TriggerManager(tmp_path)
    user_id = "u1"
    trigger = _make_trigger(thread_id="t-idle")
    with tm.atomic_update(user_id) as store:
        store.triggers.append(trigger)

    # Spy on _save to count disk writes from here on.
    saves = {"n": 0}
    original_save = tm._save

    def _counting_save(store_arg):
        saves["n"] += 1
        return original_save(store_arg)

    tm._save = _counting_save  # type: ignore[method-assign]

    # No-op poll: no events fire and no trigger state changes -> no write.
    results = tm.check_triggers(user_id=user_id, agent=None)
    assert results == []
    assert saves["n"] == 0

    # A poll that fires mutates last_fired/fire_count -> exactly one write.
    patched_sources["static"] = _StaticSource([{"body": "tick"}])
    results = tm.check_triggers(user_id=user_id, agent=None)
    assert len(results) == 1
    assert saves["n"] == 1


def test_non_agent_actions_fire_even_when_thread_is_busy(
    tmp_path, isolated_queue, patched_sources
):
    patched_sources["static"] = _StaticSource([
        {"body": "alert"},
    ])

    tm = TriggerManager(tmp_path)
    user_id = "u1"
    trigger = _make_trigger(thread_id="t1", action_type="notify")
    trigger.action.config = {"message_template": "fired"}
    with tm.atomic_update(user_id) as store:
        store.triggers.append(trigger)

    agent = _FakeAgent(busy_threads={"t1"})
    results = tm.check_triggers(user_id=user_id, agent=agent)  # type: ignore[bad-argument-type]
    # notify actions don't need a thread lock -> still returned for firing.
    assert len(results) == 1
    # Nothing queued (the queue is only used for agent_prompt actions).
    assert isolated_queue.size("t1") == 0


def test_atomic_update_raises_on_save_failure(tmp_path):
    """F11: a failed save inside ``atomic_update`` raises instead of silently
    dropping the mutation."""
    tm = TriggerManager(tmp_path)
    tm._save = lambda store: False  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Failed to persist triggers"):
        with tm.atomic_update("u1") as store:
            store.triggers.append(_make_trigger())


def test_atomic_update_no_raise_on_noop_even_when_save_would_fail(tmp_path):
    """F11 + F1: a no-op block must neither save nor raise, even when ``_save``
    would fail (dirty-aware short-circuit happens before the save check)."""
    tm = TriggerManager(tmp_path)
    tm._save = lambda store: False  # type: ignore[method-assign]

    # No mutation -> snapshot unchanged -> no save attempted -> no raise.
    with tm.atomic_update("u1"):
        pass


def test_create_todo_action_reuses_canonical_singleton(tmp_path, monkeypatch):
    """F9: ``_fire_create_todo`` routes through the canonical
    ``tools.todo._get_todo_manager`` singleton instead of constructing a fresh
    ``TodoManager`` (mkdir + log) on every fire."""
    from nymeria.core.todo_manager import TodoManager
    from nymeria.tools import todo as todo_tools

    singleton = TodoManager(tmp_path)
    monkeypatch.setattr(todo_tools, "_todo_manager", singleton)

    calls = {"get": 0}
    real_get = todo_tools._get_todo_manager

    def _spy_get():
        calls["get"] += 1
        return real_get()

    monkeypatch.setattr(todo_tools, "_get_todo_manager", _spy_get)

    tm = TriggerManager(tmp_path)
    tm._fire_create_todo(
        config={"task_template": "Triggered: {trigger_name}"},
        template_vars={"trigger_name": "My Trigger", "trigger_id": "trig-1"},
        user_id="u1",
    )

    # Routed through the canonical accessor exactly once...
    assert calls["get"] == 1
    # ...and the TODO landed in that singleton's store.
    items = singleton.get_todos("u1").items
    assert len(items) == 1
    assert items[0].task == "Triggered: My Trigger"
    assert items[0].created_by == "trigger"
