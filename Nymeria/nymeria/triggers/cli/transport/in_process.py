"""In-process CLI transport backed by a local ``NymeriaAgent`` instance."""

from __future__ import annotations

import asyncio
import copy
import logging
import sqlite3
import threading
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from ....core.checkpoint_cleanup import delete_thread_checkpoints
from ....core.stream_bridge import iter_agent_astream
from ....core.thread_classification import classify_platform
from ..events import ErrorEvent, NormalizedEvent, normalize_stream_event
from .base import Attachment

logger = logging.getLogger(__name__)

_STREAM_DONE = object()


class InProcessAgentClient:
    """Local transport that isolates direct ``NymeriaAgent`` access."""

    connection_label = "local agent"

    def __init__(self, agent: Any, *, default_user_id: str = "default") -> None:
        self.agent = agent
        self.default_user_id = default_user_id
        self._stop_lock = threading.Lock()
        self._stopping_threads: set[str] = set()

    async def stream_chat(
        self,
        message: str,
        thread_id: str,
        user_id: str = "default",
        attachments: Sequence[Attachment] | None = None,
        **options: Any,
    ) -> AsyncIterator[NormalizedEvent]:
        """Stream local ``agent.astream()`` chunks as normalized CLI events."""

        user_id = user_id or self.default_user_id
        with self._stop_lock:
            self._stopping_threads.discard(thread_id)

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[NormalizedEvent | object] = asyncio.Queue()
        cancel_event = threading.Event()

        def publish(item: NormalizedEvent | object) -> None:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                pass  # Event loop closed while worker was winding down.

        def worker() -> None:
            try:
                for raw_event in iter_agent_astream(
                    self.agent,
                    **_astream_kwargs(
                        message=message,
                        thread_id=thread_id,
                        user_id=user_id,
                        attachments=attachments,
                        options=options,
                    ),
                ):
                    if cancel_event.is_set():
                        break
                    publish(
                        normalize_stream_event(
                            raw_event,
                            default_thread_id=thread_id,
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - stream path must report errors.
                logger.exception("Local CLI transport stream failed")
                publish(
                    ErrorEvent(
                        thread_id=thread_id,
                        content=str(exc) or exc.__class__.__name__,
                        code="local_transport_error",
                        details={"error_type": exc.__class__.__name__},
                    )
                )
            finally:
                publish(_STREAM_DONE)

        thread = threading.Thread(
            target=worker,
            name=f"NymeriaCLILocalStream-{thread_id}",
            daemon=True,
        )
        thread.start()

        try:
            while True:
                item = await queue.get()
                if item is _STREAM_DONE:
                    break
                yield item
        finally:
            cancel_event.set()

    async def stream_autonomous(
        self,
        user_id: str = "default",
        *,
        client_id: str | None = None,
    ) -> AsyncIterator[NormalizedEvent]:
        """Local transport has no separate autonomous SSE subscription."""

        if False:
            yield ErrorEvent(thread_id=None, content=user_id or client_id or "")

    async def stop(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Abort local generation once per active stop request."""

        with self._stop_lock:
            if thread_id in self._stopping_threads:
                return {
                    "ok": True,
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "status": "already_stopping",
                }
            self._stopping_threads.add(thread_id)

        self.agent.abort_with_cascade(thread_id)
        return {
            "ok": True,
            "thread_id": thread_id,
            "user_id": user_id,
            "status": "stopping",
        }

    async def get_history(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        include_internal: bool = False,
        **options: Any,
    ) -> Mapping[str, Any]:
        """Return local history in the same top-level shape as the API."""

        history_options = {
            key: value
            for key, value in options.items()
            if key in {"show_autonomous_prompts", "show_prompt_metadata"}
        }
        messages = self.agent.get_conversation_history(
            thread_id,
            include_internal=include_internal,
            **history_options,
        )
        return {"thread_id": thread_id, "messages": copy.deepcopy(messages)}

    async def list_threads(
        self,
        user_id: str = "default",
    ) -> Sequence[Mapping[str, Any]]:
        """Return checkpoint and metadata-backed threads for ``user_id``."""

        user_id = user_id or self.default_user_id
        checkpoint_ids = _checkpoint_thread_ids(_settings_for_agent(self.agent))
        checkpoint_set = set(checkpoint_ids)
        metadata_manager = self.agent.thread_metadata_manager
        store = metadata_manager.get_store(user_id)

        threads: list[dict[str, Any]] = []
        seen: set[str] = set()

        for thread_id in checkpoint_ids:
            meta = store.threads.get(thread_id)
            threads.append(_thread_payload(self.agent, thread_id, meta))
            seen.add(thread_id)

        for thread_id, meta in store.threads.items():
            if thread_id in checkpoint_set or thread_id in seen:
                continue
            threads.append(_thread_payload(self.agent, thread_id, meta))
            seen.add(thread_id)

        return threads

    async def list_thread_teams(
        self,
        user_id: str = "default",
    ) -> Sequence[Mapping[str, Any]]:
        """Return callable team groupings visible to ``user_id``."""

        user_id = user_id or self.default_user_id
        teams: dict[str, dict[str, Any]] = {}
        for thread in await self.list_threads(user_id):
            thread_id = str(thread.get("thread_id") or thread.get("id") or "")
            if not thread_id:
                continue
            manager = getattr(self.agent, "thread_config_manager", None)
            config = manager.get_config(thread_id) if manager is not None else None
            team_id = str(getattr(config, "callable_team_id", "") or "")
            if not team_id:
                continue
            team_name = str(getattr(config, "callable_team_name", "") or team_id)
            team = teams.setdefault(
                team_id,
                {"id": team_id, "name": team_name, "thread_ids": []},
            )
            team["name"] = team_name
            team["thread_ids"].append(thread_id)
        return sorted(teams.values(), key=lambda item: str(item["name"]).casefold())

    async def list_todos(
        self,
        user_id: str = "default",
        *,
        filter_status: str | None = None,
        thread_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        """Return local TODOs with API-compatible filtering."""

        from ....core.todo_constants import STATUS_ORDER

        user_id = user_id or self.default_user_id
        todo_manager = getattr(self.agent, "todo_manager", None)
        if todo_manager is None:
            from ....core.todo_manager import TodoManager

            todo_manager = TodoManager(_settings_for_agent(self.agent).data_dir)
        todo_list = todo_manager.get_todos(user_id)
        if filter_status == "all":
            items = list(todo_list.items)
        elif filter_status:
            items = [
                item
                for item in todo_list.items
                if str(getattr(item.status, "value", item.status)) == filter_status
            ]
        else:
            items = list(todo_list.get_active_todos())
        if thread_id:
            items = [item for item in items if getattr(item, "thread_id", None) == thread_id]
        items.sort(
            key=lambda item: (
                STATUS_ORDER.get(getattr(item, "status", ""), 3),
                getattr(item, "created_at", ""),
            )
        )
        return [_model_dump(item) for item in items]

    async def list_triggers(
        self,
        user_id: str = "default",
        *,
        enabled_only: bool = False,
        thread_id: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        """Return local triggers with API-compatible filtering."""

        user_id = user_id or self.default_user_id
        manager = getattr(self.agent, "trigger_manager", None)
        if manager is None:
            from ....core.trigger_manager import TriggerManager

            manager = TriggerManager(_settings_for_agent(self.agent).data_dir)
        triggers = manager.get_triggers(user_id)
        if enabled_only:
            triggers = [trigger for trigger in triggers if bool(trigger.enabled)]
        if thread_id:
            triggers = [
                trigger for trigger in triggers if str(trigger.thread_id or "") == thread_id
            ]
        return [_model_dump(trigger) for trigger in triggers]

    async def get_context_stats(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Return local context stats."""

        return copy.deepcopy(self.agent.get_context_stats(thread_id))

    async def create_thread(
        self,
        user_id: str = "default",
        *,
        thread_id: str | None = None,
        title: str | None = None,
    ) -> Mapping[str, Any]:
        """Create thread metadata locally and return the created payload."""

        user_id = user_id or self.default_user_id
        selected_thread_id = thread_id or str(uuid.uuid4())[:8]
        fields: dict[str, Any] = {}
        if title:
            fields["title"] = title
            fields["title_source"] = "user"
        meta = self.agent.thread_metadata_manager.upsert_thread(
            user_id,
            selected_thread_id,
            **fields,
        )
        return _thread_payload(self.agent, selected_thread_id, meta)

    async def update_thread_metadata(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> Mapping[str, Any]:
        """Update local thread metadata."""

        selected_user_id = user_id or self.default_user_id
        fields: dict[str, Any] = {}
        if title is not None:
            fields["title"] = title.strip()
            fields["title_source"] = "user"
        if pinned is not None:
            fields["pinned"] = pinned
        meta = self.agent.thread_metadata_manager.upsert_thread(
            selected_user_id,
            thread_id,
            **fields,
        )
        return _thread_payload(self.agent, thread_id, meta)

    async def delete_thread(
        self,
        thread_id: str,
        user_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Delete local thread metadata, checkpoints, and thread config."""

        selected_user_id = user_id or self.default_user_id
        metadata_deleted = self.agent.thread_metadata_manager.delete_thread(
            selected_user_id,
            thread_id,
        )
        checkpoint_counts = delete_thread_checkpoints(
            _settings_for_agent(self.agent),
            thread_id,
        )

        config_deleted = False
        callable_deleted = False
        thread_config_manager = getattr(self.agent, "thread_config_manager", None)
        if thread_config_manager is not None:
            config = thread_config_manager.get_config(thread_id)
            callable_deleted = bool(config and getattr(config, "callable", False))
            if config is not None:
                thread_config_manager.delete_config(thread_id)
                config_deleted = True
                invalidate = getattr(self.agent, "invalidate_thread_config_cache", None)
                if callable(invalidate):
                    invalidate(thread_id)
                if callable_deleted:
                    sync_tools = getattr(self.agent, "sync_agent_tools", None)
                    if callable(sync_tools):
                        sync_tools()

        with self._stop_lock:
            self._stopping_threads.discard(thread_id)

        return {
            "ok": True,
            "thread_id": thread_id,
            "user_id": selected_user_id,
            "metadata_deleted": metadata_deleted,
            "config_deleted": config_deleted,
            "callable_deleted": callable_deleted,
            "checkpoints": checkpoint_counts,
        }

    async def branch_thread(
        self,
        thread_id: str,
        user_id: str | None = None,
        *,
        title: str | None = None,
        from_message_index: int | None = None,
    ) -> Mapping[str, Any]:
        """Create a local branch by cloning checkpoints and thread config."""

        from ....core.thread_branch import branch_thread

        selected_user_id = user_id or self.default_user_id
        return await asyncio.to_thread(
            branch_thread,
            agent=self.agent,
            settings=_settings_for_agent(self.agent),
            user_id=selected_user_id,
            source_thread_id=thread_id,
            title=title,
            from_message_index=from_message_index,
        )


def _astream_kwargs(
    *,
    message: str,
    thread_id: str,
    user_id: str,
    attachments: Sequence[Attachment] | None,
    options: Mapping[str, Any],
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "message": message,
        "thread_id": thread_id,
        "user_id": user_id,
    }
    if attachments is not None:
        kwargs["attachments"] = copy.deepcopy(list(attachments))

    option_aliases = {
        "is_self_invoke": "_is_self_invoke",
        "trigger_override": "_trigger_override",
    }
    for key, value in options.items():
        if value is None:
            continue
        kwargs[option_aliases.get(key, key)] = copy.deepcopy(value)
    return kwargs


def _thread_payload(agent: Any, thread_id: str, meta: Any) -> dict[str, Any]:
    if meta is not None:
        payload = _model_dump(meta)
    else:
        payload = {
            "thread_id": thread_id,
            "title": "New Chat",
            "pinned": False,
            "platform": classify_platform(thread_id),
            "platform_meta": None,
            "created_at": None,
            "updated_at": None,
            "title_source": "default",
        }

    thread_config_manager = getattr(agent, "thread_config_manager", None)
    config = (
        thread_config_manager.get_config(thread_id)
        if thread_config_manager is not None
        else None
    )
    is_callable = bool(config and getattr(config, "callable", False))
    payload["callable"] = is_callable
    if is_callable and getattr(config, "callable_name", ""):
        payload["title"] = config.callable_name
        payload["title_source"] = "callable"
    return payload


def _model_dump(value: Any) -> dict[str, Any]:
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    return copy.deepcopy(vars(value))


def _settings_for_agent(agent: Any) -> Any:
    settings = getattr(agent, "settings", None)
    if settings is not None:
        return settings
    from ....config.settings import get_settings

    return get_settings()


def _checkpoint_thread_ids(settings: Any) -> list[str]:
    backend = getattr(settings, "database_backend", "sqlite")
    if backend == "sqlite":
        return _sqlite_checkpoint_thread_ids(Path(settings.db_path))
    if backend == "postgres":
        return _postgres_checkpoint_thread_ids(str(settings.postgres_uri))
    return []


def _sqlite_checkpoint_thread_ids(db_path: Path) -> list[str]:
    try:
        with sqlite3.connect(str(db_path)) as conn:
            if not _sqlite_table_exists(conn, "checkpoints"):
                return []
            cursor = conn.execute("SELECT DISTINCT thread_id FROM checkpoints")
            return [str(row[0]) for row in cursor.fetchall() if row[0]]
    except Exception as exc:  # noqa: BLE001 - listing should degrade gracefully.
        logger.warning("Failed to query local SQLite thread IDs: %s", exc)
        return []


def _sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _postgres_checkpoint_thread_ids(postgres_uri: str) -> list[str]:
    try:
        import psycopg  # type: ignore[import-untyped]

        with psycopg.connect(postgres_uri) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
                return [str(row[0]) for row in cur.fetchall() if row[0]]
    except Exception as exc:  # noqa: BLE001 - listing should degrade gracefully.
        logger.warning("Failed to query local PostgreSQL thread IDs: %s", exc)
        return []


__all__ = ["InProcessAgentClient"]
