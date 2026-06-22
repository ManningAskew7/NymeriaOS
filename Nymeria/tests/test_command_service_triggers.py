"""Tests for the /triggers backend commands added in Phase 2a.3."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cli_fixtures import run
import nymeria.core.agent as agent_module
from nymeria.core.command_service import CommandContext, CommandService


def _make_trigger(
    trigger_id: str,
    *,
    name: str = "",
    source_type: str = "webhook",
    action_type: str = "send_message",
    enabled: bool = True,
    thread_id: str = "thread-1",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=trigger_id,
        name=name or trigger_id,
        source_type=source_type,
        enabled=enabled,
        thread_id=thread_id,
        action=SimpleNamespace(type=action_type),
    )


class _FakeTriggerManager:
    def __init__(self, triggers: list[SimpleNamespace] | None = None) -> None:
        self.store: dict[str, dict[str, SimpleNamespace]] = {}
        for trigger in triggers or []:
            self.store.setdefault("alice", {})[trigger.id] = trigger
        self.executions: dict[str, list[dict[str, Any]]] = {}
        self.deletion_log: list[tuple[str, str]] = []
        self.executions_cleared: list[tuple[str, tuple[str, ...]]] = []

    def get_triggers(self, user_id: str) -> list[SimpleNamespace]:
        return list(self.store.get(user_id, {}).values())

    def update_trigger(self, user_id: str, trigger_id: str, **kwargs) -> bool:
        triggers = self.store.get(user_id, {})
        trigger = triggers.get(trigger_id)
        if trigger is None:
            return False
        for key, value in kwargs.items():
            setattr(trigger, key, value)
        return True

    def delete_trigger(self, user_id: str, trigger_id: str) -> bool:
        triggers = self.store.get(user_id, {})
        if trigger_id in triggers:
            self.deletion_log.append((user_id, trigger_id))
            del triggers[trigger_id]
            return True
        return False

    def delete_executions_for_triggers(
        self,
        user_id: str,
        trigger_ids: list[str],
    ) -> int:
        self.executions_cleared.append((user_id, tuple(trigger_ids)))
        return len(trigger_ids)

    def get_executions(
        self,
        user_id: str,
        trigger_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        all_executions = self.executions.get(user_id, [])
        if trigger_id:
            all_executions = [
                e for e in all_executions if e.get("trigger_id") == trigger_id
            ]
        return list(reversed(all_executions[-limit:]))


class _FakeAccountsRepo:
    def get_user_by_id(self, user_id: str):
        return SimpleNamespace(
            id=user_id,
            email=f"{user_id}@example.test",
            display_name=user_id,
            role="admin",
        )


@pytest.fixture(autouse=True)
def patched_agent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        agent_module,
        "get_current_agent",
        lambda: SimpleNamespace(accounts_repo=_FakeAccountsRepo()),
    )


@pytest.fixture
def patched_manager(monkeypatch: pytest.MonkeyPatch):
    """Patch ``tools.triggers._get_trigger_manager`` with a controllable fake."""

    def install(manager: _FakeTriggerManager) -> _FakeTriggerManager:
        monkeypatch.setattr(
            "nymeria.tools.triggers._get_trigger_manager",
            lambda: manager,
        )
        return manager

    return install


def _ctx(thread_id: str | None = "thread-1") -> CommandContext:
    return CommandContext(
        user_id="alice",
        thread_id=thread_id or "",
        actor="user",
        surface="cli",
        is_admin=True,
    )


def test_triggers_list_renders_table(patched_manager) -> None:
    patched_manager(_FakeTriggerManager(triggers=[
        _make_trigger("t1", name="Webhook A"),
        _make_trigger("t2", name="Cron B", source_type="rss", enabled=False),
    ]))

    result = run(CommandService().execute(_ctx(), "/triggers list"))

    assert result.success is True, result.markdown
    assert "Webhook A" in result.markdown
    assert "Cron B" in result.markdown
    assert "| ID | Status | Source | Action | Name |" in result.markdown


def test_triggers_list_filters_enabled_only(patched_manager) -> None:
    patched_manager(_FakeTriggerManager(triggers=[
        _make_trigger("active", enabled=True),
        _make_trigger("paused", enabled=False),
    ]))

    result = run(
        CommandService().execute(_ctx(), "/triggers list --enabled-only")
    )

    assert "active" in result.markdown
    assert "paused" not in result.markdown


def test_triggers_list_filters_by_thread(patched_manager) -> None:
    patched_manager(_FakeTriggerManager(triggers=[
        _make_trigger("alpha", thread_id="thread-1"),
        _make_trigger("bravo", thread_id="thread-2"),
    ]))

    result = run(
        CommandService().execute(_ctx(), "/triggers list --thread thread-2")
    )

    assert "bravo" in result.markdown
    assert "alpha" not in result.markdown


def test_triggers_list_reports_empty(patched_manager) -> None:
    patched_manager(_FakeTriggerManager())

    result = run(CommandService().execute(_ctx(), "/triggers list"))
    assert result.success is True
    assert "No triggers found" in result.markdown


def test_triggers_enable_sets_flag(patched_manager) -> None:
    trigger = _make_trigger("t1", enabled=False)
    manager = patched_manager(_FakeTriggerManager(triggers=[trigger]))

    result = run(CommandService().execute(_ctx(), "/triggers enable t1"))

    assert result.success is True, result.markdown
    assert "Enabled" in result.markdown
    assert manager.store["alice"]["t1"].enabled is True


def test_triggers_disable_sets_flag(patched_manager) -> None:
    trigger = _make_trigger("t1", enabled=True)
    manager = patched_manager(_FakeTriggerManager(triggers=[trigger]))

    result = run(CommandService().execute(_ctx(), "/triggers disable t1"))

    assert result.success is True, result.markdown
    assert manager.store["alice"]["t1"].enabled is False


def test_triggers_enable_reports_unknown(patched_manager) -> None:
    patched_manager(_FakeTriggerManager())

    result = run(CommandService().execute(_ctx(), "/triggers enable missing"))
    assert result.success is False
    assert "not found" in result.markdown


def test_triggers_delete_removes_and_clears_executions(patched_manager) -> None:
    manager = patched_manager(_FakeTriggerManager(triggers=[_make_trigger("t1")]))

    result = run(CommandService().execute(_ctx(), "/triggers delete t1"))

    assert result.success is True, result.markdown
    assert manager.deletion_log == [("alice", "t1")]
    assert manager.executions_cleared == [("alice", ("t1",))]


def test_triggers_history_filters_by_trigger_id_and_limit(patched_manager) -> None:
    manager = patched_manager(_FakeTriggerManager())
    manager.executions["alice"] = [
        {"trigger_id": "t1", "triggered_at": "2026-05-18T10:00:00", "status": "ok", "summary": "first"},
        {"trigger_id": "t2", "triggered_at": "2026-05-18T11:00:00", "status": "ok", "summary": "other"},
        {"trigger_id": "t1", "triggered_at": "2026-05-18T12:00:00", "status": "ok", "summary": "second"},
    ]

    result = run(
        CommandService().execute(_ctx(), "/triggers history t1 --limit 5")
    )

    assert result.success is True, result.markdown
    assert "first" in result.markdown
    assert "second" in result.markdown
    assert "other" not in result.markdown


def test_triggers_history_empty_state(patched_manager) -> None:
    patched_manager(_FakeTriggerManager())

    result = run(CommandService().execute(_ctx(), "/triggers history"))
    assert result.success is True
    assert "No trigger executions" in result.markdown
