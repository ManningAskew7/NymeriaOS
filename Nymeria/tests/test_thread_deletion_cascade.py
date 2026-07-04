"""Regression tests for full thread deletion cleanup."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from nymeria.core.accounts import AccountsRepo
from nymeria.core.chat_bindings import ChatBindingsRepo
from nymeria.core.activity_log import ActivityLog, ActivityType
from nymeria.core.fcm import load_tokens, save_tokens
from nymeria.core.hook_manager import HookManager
from nymeria.core.notifications import NotificationStore
from nymeria.core.thread_config import ThreadConfig, ThreadConfigManager
from nymeria.core.thread_deletion import ThreadDeletionResult, cascade_delete_thread
from nymeria.core.thread_metadata import ThreadMetadataManager
from nymeria.core.todo_manager import TodoManager
from nymeria.core.todo_schedule_db import TodoScheduleDB
from nymeria.core.agent_compaction import CompactionManager
from nymeria.core.token_tracker import TokenTracker
from nymeria.core.trigger_manager import (
    TriggerAction,
    TriggerDefinition,
    TriggerExecution,
    TriggerManager,
    TriggerStore,
)


class FakeThreadLocks:
    def __init__(self):
        self._locks: dict[str, threading.Lock] = {}
        self._events: dict[str, threading.Event] = {}
        self._info: dict[str, str] = {}

    def get_lock(self, thread_id: str) -> threading.Lock:
        self._locks.setdefault(thread_id, threading.Lock())
        return self._locks[thread_id]

    def set_lock_info(self, thread_id: str, holder: str, task_id: str | None = None):
        self._info[thread_id] = holder

    def clear_lock_info(self, thread_id: str):
        self._info.pop(thread_id, None)

    def signal_abort(self, thread_id: str):
        self._events.setdefault(thread_id, threading.Event()).set()

    def clear_abort(self, thread_id: str):
        self._events.setdefault(thread_id, threading.Event()).clear()


class FakeMemoryIndex:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def delete_by_thread(self, user_id: str, thread_id: str) -> int:
        self.calls.append((user_id, thread_id))
        return 3


class FakeAgent:
    def __init__(self, data_dir: Path):
        self.thread_metadata_manager = ThreadMetadataManager(data_dir)
        self.thread_config_manager = ThreadConfigManager(data_dir)
        self.todo_manager = TodoManager(data_dir)
        self.trigger_manager = TriggerManager(data_dir)
        accounts_db = data_dir / "accounts.db"
        self.accounts_repo = AccountsRepo(accounts_db)
        self.chat_bindings_repo = ChatBindingsRepo(accounts_db)
        self._schedule_db = TodoScheduleDB(data_dir / "todo_schedule.db")
        self._thread_locks = FakeThreadLocks()
        self._token_tracker = TokenTracker()
        self._compaction = CompactionManager(self)
        self._pending_tool_reload = {}
        self._turn_reload_count = {}
        self._user_graphs = {}
        self._async_user_graphs = {}
        self._graph_cache_lock = threading.Lock()
        self.memory_index = FakeMemoryIndex()
        self.aborted: list[str] = []
        self.invalidated: list[str] = []
        self.synced_tools = 0

    def abort_with_cascade(self, thread_id: str):
        self.aborted.append(thread_id)
        self._thread_locks.signal_abort(thread_id)

    def invalidate_thread_config_cache(self, thread_id: str):
        self.invalidated.append(thread_id)

    def sync_agent_tools(self):
        self.synced_tools += 1

    def _get_memory_index(self, user_id: str):
        return self.memory_index


def _create_checkpoint_rows(db_path: Path, target: str, survivor: str) -> None:
    with sqlite3.connect(db_path) as conn:
        for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
            conn.execute(f"CREATE TABLE {table} (thread_id TEXT, payload TEXT)")
            conn.execute(
                f"INSERT INTO {table} (thread_id, payload) VALUES (?, ?)",
                (target, "delete-me"),
            )
            conn.execute(
                f"INSERT INTO {table} (thread_id, payload) VALUES (?, ?)",
                (survivor, "keep-me"),
            )
        conn.commit()


def _count_rows(db_path: Path, table: str, thread_id: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return int(
            conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()[0]
        )


def test_thread_deletion_result_requires_explicit_counts():
    result = ThreadDeletionResult(thread_id="thread-1", user_id="default")

    with pytest.raises(TypeError):
        result.set("thread_owners_deleted", True)

    with pytest.raises(TypeError):
        result.inc("thread_owners_deleted", False)


def test_cascade_delete_thread_removes_active_and_ui_resources(tmp_path: Path, api_client_builder):
    target = "telegram_5551234567"
    survivor = "survivor-thread"
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)

    _create_checkpoint_rows(settings.db_path, target, survivor)

    agent.accounts_repo.create_user("default", "owner@example.com", "Owner", role="admin")
    agent.accounts_repo.claim_thread(target, "default")
    agent.accounts_repo.claim_thread(survivor, "default")
    agent.accounts_repo.link_platform("telegram", "5551234567", "default")
    bot = agent.chat_bindings_repo.register_user_telegram_bot(
        owner_user_id="default",
        bot_username="NymeriaV1Bot",
        bot_token_ciphertext="ciphertext",
    )
    agent.chat_bindings_repo.create_thread_binding(
        thread_id=target,
        provider="telegram",
        platform_chat_id="5551234567",
        user_id="default",
        user_telegram_bot_id=bot.id,
    )
    agent.chat_bindings_repo.issue_bind_code(
        kind="thread_bind",
        provider="telegram",
        user_id="default",
        thread_id=target,
    )

    agent.thread_metadata_manager.upsert_thread("default", target, title="Delete me")
    agent.thread_metadata_manager.upsert_thread("default", survivor, title="Keep me")
    agent.thread_config_manager.save_config(
        ThreadConfig(thread_id=target, callable=True, callable_name="old_bot")
    )
    notes_dir = tmp_path / "thread_notes"
    notes_dir.mkdir()
    (notes_dir / f"{target}.md").write_text("old notes", encoding="utf-8")

    with agent.todo_manager.atomic_update("default") as todo_list:
        doomed = todo_list.add_item("Daily briefing", thread_id=target)
        kept = todo_list.add_item("Keep task", thread_id=survivor)
    agent._schedule_db.add_scheduled(
        todo_id=doomed.id,
        user_id="default",
        scheduled_for=doomed.created_at,
        task_preview=doomed.task,
        thread_id=target,
    )
    agent._schedule_db.add_scheduled(
        todo_id=kept.id,
        user_id="default",
        scheduled_for=kept.created_at,
        task_preview=kept.task,
        thread_id=survivor,
    )

    trigger = TriggerDefinition(
        id="trigdead",
        name="Old trigger",
        source_type="webhook",
        source_config={},
        action=TriggerAction(type="agent_prompt", config={"prompt": "run"}),
        thread_id=target,
    )
    survivor_trigger = TriggerDefinition(
        id="trigkeep",
        name="Keep trigger",
        source_type="webhook",
        source_config={},
        action=TriggerAction(type="agent_prompt", config={"prompt": "run"}),
        thread_id=survivor,
    )
    agent.trigger_manager._save(
        TriggerStore(user_id="default", triggers=[trigger, survivor_trigger])
    )
    agent.trigger_manager.log_execution(
        "default",
        TriggerExecution(trigger_id=trigger.id, trigger_name=trigger.name),
    )

    # Lifecycle hooks: a thread-scoped hook on the doomed thread (removed), plus a
    # global hook and a survivor-thread hook (both kept). Exercises the fallback
    # HookManager construction (agent has no hook_manager).
    hook_manager = HookManager(tmp_path)
    doomed_hook = hook_manager.add_hook(
        "default", name="doomed", event="done", text="x", scope="thread", thread_id=target
    )
    hook_manager.add_hook(
        "default", name="global", event="done", text="x", scope="global"
    )
    hook_manager.add_hook(
        "default", name="kept", event="done", text="x", scope="thread", thread_id=survivor
    )

    ActivityLog(tmp_path).log(ActivityType.TASK_COMPLETED, "old", "default", thread_id=target)
    ActivityLog(tmp_path).log(ActivityType.TASK_COMPLETED, "new", "default", thread_id=survivor)
    NotificationStore(tmp_path).create("default", "old", thread_id=target)
    NotificationStore(tmp_path).create("default", "new", thread_id=survivor)
    save_tokens(
        str(tmp_path),
        [
            {"token": "t1", "platform": "android", "user_id": "default", "thread_ids": [target, survivor]},
            {"token": "t2", "platform": "android", "user_id": "default"},
        ],
    )

    agent._pending_tool_reload[target] = {"new_tools": ["x"]}
    agent._turn_reload_count[target] = 1
    agent._user_graphs[("default", target)] = object()
    agent._user_graphs[("default", survivor)] = object()
    agent._async_user_graphs[("default", target)] = object()
    agent._token_tracker.record_turn(target, turn_input_tokens=100, turn_output_tokens=10, context_tokens=100)

    trigger_manager = agent.trigger_manager
    delattr(agent, "trigger_manager")

    result = cascade_delete_thread(agent, settings, "default", target)

    assert result.warnings == []
    assert result.deleted["todos_deleted"] == 1
    assert result.deleted["scheduled_todos_deleted"] == 1
    assert result.deleted["triggers_deleted"] == 1
    assert result.deleted["chat_bindings_deleted"] == 1
    assert result.deleted["bind_codes_deleted"] == 1
    assert result.deleted["thread_owners_deleted"] == 1
    assert result.deleted["checkpoint_rows_remaining"] == 0
    assert result.deleted["activity_entries_deleted"] == 1
    assert result.deleted["notifications_deleted"] == 1
    assert result.deleted["fcm_filters_updated"] == 1
    assert result.deleted["rag_chunks_deleted"] == 3
    assert result.deleted["in_memory_entries_deleted"] == 4

    for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
        assert _count_rows(settings.db_path, table, target) == 0
        assert _count_rows(settings.db_path, table, survivor) == 1

    assert agent.thread_metadata_manager.get_thread("default", target) is None
    assert agent.thread_metadata_manager.get_thread("default", survivor) is not None
    assert agent.thread_config_manager.get_config(target) is None
    assert not (notes_dir / f"{target}.md").exists()

    remaining_todos = agent.todo_manager.get_todos("default")
    assert remaining_todos.get_item(doomed.id) is None
    assert remaining_todos.get_item(kept.id) is not None
    assert agent._schedule_db.get_entry(doomed.id) is None
    assert agent._schedule_db.get_entry(kept.id) is not None

    assert trigger_manager.get_trigger("default", trigger.id) is None
    assert trigger_manager.get_trigger("default", survivor_trigger.id) is not None
    assert trigger_manager.get_executions("default", trigger_id=trigger.id) == []

    assert result.deleted["hooks_deleted"] == 1
    assert hook_manager.get_hook("default", doomed_hook.id) is None
    remaining_hooks = {h.name for h in hook_manager.get_hooks("default")}
    assert remaining_hooks == {"global", "kept"}

    assert agent.accounts_repo.get_thread_owner(target) is None
    assert agent.accounts_repo.get_thread_owner(survivor) == "default"
    assert agent.chat_bindings_repo.lookup_thread_binding_by_thread("telegram", target) is None
    assert agent.accounts_repo.resolve_platform("telegram", "5551234567") == "default"
    assert agent.chat_bindings_repo.get_user_telegram_bot(bot.id) is not None

    assert ActivityLog(tmp_path).get_entries("default", thread_id=target) == []
    assert len(ActivityLog(tmp_path).get_entries("default", thread_id=survivor)) == 1
    assert NotificationStore(tmp_path).get_all("default")[0].thread_id == survivor
    assert load_tokens(str(tmp_path))[0]["thread_ids"] == [survivor]

    assert target in agent.aborted
    assert target in agent.invalidated
    assert agent.synced_tools == 1
    assert agent.memory_index.calls == [("default", target)]
    assert ("default", target) not in agent._user_graphs
    assert ("default", survivor) in agent._user_graphs
    assert agent._token_tracker.get_usage(target).total_tokens == 0


def test_cascade_delete_thread_reports_missing_thread_owner_as_zero(tmp_path: Path, api_client_builder):
    target = "orphan-thread"
    settings = api_client_builder.settings(tmp_path)
    agent = FakeAgent(tmp_path)

    result = cascade_delete_thread(agent, settings, "default", target)

    assert result.warnings == []
    assert result.deleted["thread_owners_deleted"] == 0
    assert agent.accounts_repo.get_thread_owner(target) is None
