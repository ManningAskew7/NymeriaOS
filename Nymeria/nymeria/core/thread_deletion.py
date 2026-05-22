"""Full thread deletion cascade.

Deleting a thread must remove every active resource that can wake, route, or
recreate that thread. Checkpoints are only one persistence surface; TODOs,
triggers, chat bindings, and owner rows can all cause the same thread_id to
come back later if they survive.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, TYPE_CHECKING

from .checkpoint_cleanup import delete_thread_checkpoints

if TYPE_CHECKING:
    from .agent import NymeriaAgent
    from ..config.settings import Settings

logger = logging.getLogger(__name__)


class ThreadDeletionBusy(RuntimeError):
    """Raised when a thread cannot be locked for deletion."""


@dataclass
class ThreadDeletionResult:
    """Structured deletion counts returned by the API."""

    thread_id: str
    user_id: str
    deleted: Dict[str, int] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def inc(self, key: str, amount: int | None = 1) -> None:
        self.deleted[key] = self.deleted.get(key, 0) + self._count(key, amount)

    def set(self, key: str, amount: int | None) -> None:
        self.deleted[key] = self._count(key, amount)

    @staticmethod
    def _count(key: str, amount: int | None) -> int:
        if amount is None:
            return 0
        if isinstance(amount, bool):
            raise TypeError(
                f"Thread deletion result '{key}' must be an explicit count, got bool"
            )
        if not isinstance(amount, int):
            amount_type = type(amount).__name__
            raise TypeError(
                f"Thread deletion result '{key}' must be an integer count, got {amount_type}"
            )
        if amount < 0:
            raise ValueError(
                f"Thread deletion result '{key}' cannot be negative: {amount}"
            )
        return amount

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        logger.warning("Thread deletion %s: %s", self.thread_id, message)

    def model_dump(self) -> Dict[str, Any]:
        return {
            "status": "ok",
            "thread_id": self.thread_id,
            "deleted": dict(sorted(self.deleted.items())),
            "warnings": self.warnings,
        }

def cascade_delete_thread(
    agent: "NymeriaAgent",
    settings: "Settings",
    user_id: str,
    thread_id: str,
    *,
    lock_timeout_seconds: float = 10.0,
) -> ThreadDeletionResult:
    """
    Delete a thread across all thread-bound stores.

    Access control is intentionally not performed here; callers must authorize
    before invoking this helper.
    """
    result = ThreadDeletionResult(thread_id=thread_id, user_id=user_id)

    agent.abort_with_cascade(thread_id)
    lock = agent._thread_locks.get_lock(thread_id)
    acquired = lock.acquire(timeout=lock_timeout_seconds)
    if not acquired:
        raise ThreadDeletionBusy(
            f"Thread '{thread_id}' is busy and could not be locked for deletion"
        )

    try:
        agent._thread_locks.set_lock_info(thread_id, "deleting")

        _delete_metadata(agent, thread_id, result)
        _delete_checkpoints(settings, thread_id, result)
        _delete_thread_config(agent, thread_id, result)
        _delete_notepad(settings.data_dir, thread_id, result)
        _delete_rag_chunks(agent, user_id, thread_id, result)
        _delete_todos(agent, thread_id, result)
        _delete_triggers(agent, settings.data_dir, thread_id, result)
        _delete_chat_resources(agent, thread_id, result)
        _delete_activity_and_notifications(settings.data_dir, thread_id, result)
        _delete_attachments(thread_id, result)
        _delete_in_memory_state(agent, thread_id, result)
    finally:
        agent._thread_locks.clear_lock_info(thread_id)
        agent._thread_locks.clear_abort(thread_id)
        lock.release()

    return result


def _delete_metadata(agent: "NymeriaAgent", thread_id: str, result: ThreadDeletionResult) -> None:
    try:
        result.set(
            "thread_metadata_deleted",
            agent.thread_metadata_manager.delete_thread_globally(thread_id),
        )
    except Exception as e:
        result.warn(f"metadata cleanup failed: {e}")


def _delete_thread_config(agent: "NymeriaAgent", thread_id: str, result: ThreadDeletionResult) -> None:
    tc = agent.thread_config_manager.get_config(thread_id)
    was_callable = bool(tc and tc.callable)
    deleted = agent.thread_config_manager.delete_config(thread_id)
    if tc is not None and not deleted:
        raise RuntimeError(f"Thread config for {thread_id} could not be deleted")
    result.set("thread_configs_deleted", 1 if deleted else 0)
    agent.invalidate_thread_config_cache(thread_id)
    if was_callable:
        agent.sync_agent_tools()


def _safe_thread_file(data_dir: Path, folder: str, thread_id: str, suffix: str) -> Path:
    safe_id = "".join(c for c in thread_id if c.isalnum() or c in "-_") or "default"
    return data_dir / folder / f"{safe_id}{suffix}"


def _delete_notepad(data_dir: Path, thread_id: str, result: ThreadDeletionResult) -> None:
    try:
        path = _safe_thread_file(data_dir, "thread_notes", thread_id, ".md")
        if path.exists():
            path.unlink()
            result.inc("notepads_deleted")
        else:
            result.set("notepads_deleted", 0)
    except Exception as e:
        result.warn(f"notepad cleanup failed: {e}")


def _delete_rag_chunks(
    agent: "NymeriaAgent",
    user_id: str,
    thread_id: str,
    result: ThreadDeletionResult,
) -> None:
    try:
        idx = agent._get_memory_index(user_id)
        result.set(
            "rag_chunks_deleted",
            idx.delete_by_thread(user_id, thread_id) if idx else 0,
        )
    except Exception as e:
        result.warn(f"RAG cleanup failed: {e}")


def _delete_todos(agent: "NymeriaAgent", thread_id: str, result: ThreadDeletionResult) -> None:
    deleted_todo_ids: List[str] = []
    for todo_user_id in agent.todo_manager.get_all_users_with_todos():
        deleted = agent.todo_manager.delete_todos_for_thread(todo_user_id, thread_id)
        deleted_todo_ids.extend(item.id for item in deleted)
        result.inc("todos_deleted", len(deleted))

    schedule_db = getattr(agent, "_schedule_db", None)
    if schedule_db is not None:
        result.set(
            "scheduled_todos_deleted",
            schedule_db.remove_for_thread(thread_id, deleted_todo_ids),
        )


def _delete_triggers(
    agent: "NymeriaAgent",
    data_dir: Path,
    thread_id: str,
    result: ThreadDeletionResult,
) -> None:
    manager = getattr(agent, "trigger_manager", None)
    if manager is None:
        from .trigger_manager import TriggerManager

        manager = TriggerManager(data_dir)

    for trigger_user_id in manager.get_all_users_with_triggers():
        trigger_ids = manager.delete_triggers_for_thread(
            trigger_user_id,
            thread_id,
        )
        result.inc("triggers_deleted", len(trigger_ids))
        result.inc(
            "trigger_executions_deleted",
            manager.delete_executions_for_triggers(
                trigger_user_id,
                trigger_ids,
            ),
        )


def _delete_chat_resources(agent: "NymeriaAgent", thread_id: str, result: ThreadDeletionResult) -> None:
    bindings = agent.chat_bindings_repo
    result.set("chat_bindings_deleted", bindings.delete_thread_bindings_for_thread(thread_id))
    result.set("bind_codes_deleted", bindings.delete_bind_codes_for_thread(thread_id))
    owner_deleted = agent.accounts_repo.delete_thread_owner(thread_id)
    result.set("thread_owners_deleted", 1 if owner_deleted else 0)


def _delete_activity_and_notifications(
    data_dir: Path,
    thread_id: str,
    result: ThreadDeletionResult,
) -> None:
    try:
        from .activity_log import ActivityLog

        result.set(
            "activity_entries_deleted",
            ActivityLog(data_dir).delete_thread_globally(thread_id),
        )
    except Exception as e:
        result.warn(f"activity cleanup failed: {e}")

    try:
        from .notifications import NotificationStore

        result.set(
            "notifications_deleted",
            NotificationStore(data_dir).delete_thread_globally(thread_id),
        )
    except Exception as e:
        result.warn(f"notification cleanup failed: {e}")

    try:
        from .fcm import remove_thread_from_tokens

        result.set("fcm_filters_updated", remove_thread_from_tokens(str(data_dir), thread_id))
    except Exception as e:
        result.warn(f"FCM filter cleanup failed: {e}")


def _delete_attachments(thread_id: str, result: ThreadDeletionResult) -> None:
    """Remove the per-thread sandbox directory + extracted text + meta files."""
    try:
        from .attachment_sandbox import cleanup_thread_attachments

        result.set("attachment_files_deleted", cleanup_thread_attachments(thread_id))
    except Exception as e:
        result.warn(f"attachment cleanup failed: {e}")


def _delete_in_memory_state(agent: "NymeriaAgent", thread_id: str, result: ThreadDeletionResult) -> None:
    popped = 0
    # Compaction pending state lives on the CompactionManager
    compaction = getattr(agent, "_compaction", None)
    if compaction is not None:
        popped += compaction.clear_thread_state(thread_id)
    for attr in (
        "_pending_tool_reload",
        "_turn_reload_count",
    ):
        state = getattr(agent, attr, None)
        if isinstance(state, dict) and thread_id in state:
            state.pop(thread_id, None)
            popped += 1

    token_tracker = getattr(agent, "_token_tracker", None)
    if token_tracker is not None:
        token_tracker.clear_thread(thread_id)

    cache_deleted = 0
    graph_lock = getattr(agent, "_graph_cache_lock", None)
    if graph_lock is not None:
        with graph_lock:
            cache_deleted += _drop_graph_cache_entries(agent, "_user_graphs", thread_id)
            cache_deleted += _drop_graph_cache_entries(agent, "_async_user_graphs", thread_id)
    else:
        cache_deleted += _drop_graph_cache_entries(agent, "_user_graphs", thread_id)
        cache_deleted += _drop_graph_cache_entries(agent, "_async_user_graphs", thread_id)

    result.set("in_memory_entries_deleted", popped + cache_deleted)


def _drop_graph_cache_entries(agent: "NymeriaAgent", attr: str, thread_id: str) -> int:
    cache = getattr(agent, attr, None)
    if not isinstance(cache, dict):
        return 0
    keys = [key for key in cache if isinstance(key, tuple) and len(key) > 1 and key[1] == thread_id]
    for key in keys:
        cache.pop(key, None)
    return len(keys)


def _delete_checkpoints(settings: "Settings", thread_id: str, result: ThreadDeletionResult) -> None:
    for key, count in delete_thread_checkpoints(settings, thread_id).items():
        result.set(key, count)
