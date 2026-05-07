"""Thread read/list/metadata/lifecycle routes."""

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from ...core.accounts import AuthenticatedUser
from ...core.checkpoint_cleanup import delete_thread_checkpoints
from ...core.checkpoint_status import (
    get_graph_state_revision,
    get_latest_checkpoint_revision,
    has_direct_checkpoint_revision_backend,
)
from ...core.event_bus import publish_sync_event as default_publish_sync_event
from ...core.thread_classification import (
    classify_platform as _classify_thread_platform_from_id,
    is_shared_channel as _is_shared_channel_thread,
)
from ...core.thread_deletion import ThreadDeletionBusy, cascade_delete_thread
from ...tools import ALL_TOOLS
from ..schemas.threads import (
    ThreadHistoryResponse,
    ThreadMetadataMigrateRequest,
    ThreadMetadataUpdateRequest,
    ThreadStatusResponse,
)
from ..thread_config_helpers import validate_callable_name

logger = logging.getLogger(__name__)


def _bound_chatapp_platform(agent: Any, thread_id: str) -> str | None:
    """Return the sidebar platform implied by an explicit chat-app binding."""
    try:
        if agent.chat_bindings_repo.lookup_thread_binding_by_thread(
            "telegram", thread_id
        ):
            return "telegram"
    except Exception as e:
        logger.warning(
            "Failed to inspect chat-app binding platform for %s: %s",
            thread_id,
            e,
        )
    return None


def _thread_list_platform(agent: Any, thread_id: str, meta: Any = None) -> str:
    """Resolve the platform value the frontend should render for a thread."""
    platform = meta.platform if meta else _classify_thread_platform_from_id(thread_id)
    bound_platform = _bound_chatapp_platform(agent, thread_id)
    if bound_platform:
        return bound_platform

    native_platform = _classify_thread_platform_from_id(thread_id)
    if native_platform in {"discord", "telegram", "slack", "trigger", "twitch"}:
        return native_platform
    if platform in {"discord", "telegram", "slack", "trigger", "twitch"}:
        return platform

    thread_config_manager = getattr(agent, "thread_config_manager", None)
    tc = (
        thread_config_manager.get_config(thread_id)
        if thread_config_manager is not None
        else None
    )
    if tc and tc.callable:
        platform = "callable"
    elif platform == "callable":
        platform = "desktop"

    return platform


def _get_checkpoint_thread_ids(settings: Any) -> list[str]:
    """Query distinct thread IDs from the checkpoint database."""
    thread_ids: list[str] = []

    if settings.database_backend == "sqlite":
        import sqlite3 as _sqlite3

        db_path = str(settings.db_path)
        try:
            conn = _sqlite3.connect(db_path)
            cursor = conn.execute("SELECT DISTINCT thread_id FROM checkpoints")
            thread_ids = [row[0] for row in cursor.fetchall()]
            conn.close()
        except Exception as e:
            logger.warning(f"Failed to query thread IDs from SQLite: {e}")

    elif settings.database_backend == "postgres":
        import psycopg  # type: ignore[import-untyped]

        try:
            with psycopg.connect(settings.postgres_uri) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
                    thread_ids = [row[0] for row in cur.fetchall()]
        except Exception as e:
            logger.warning(f"Failed to query thread IDs from PostgreSQL: {e}")

    return thread_ids


def _add_thread_source(
    sources: dict[str, set[str]],
    thread_id: str | None,
    source: str,
) -> None:
    if not thread_id:
        return
    normalized = str(thread_id).strip()
    if not normalized:
        return
    sources.setdefault(normalized, set()).add(source)


def _can_show_orphan_checkpoint_thread(
    agent: Any,
    user: AuthenticatedUser,
    user_id: str,
    thread_id: str,
) -> bool:
    """Return True when a checkpoint-only thread can be safely listed."""
    if user.role == "admin":
        return True
    if thread_id.startswith("telegram_") and not thread_id.startswith("telegram_-"):
        provider_user_id = thread_id[len("telegram_"):]
        return agent.accounts_repo.resolve_platform("telegram", provider_user_id) == user_id
    return False


def _can_list_recovered_thread(
    agent: Any,
    user: AuthenticatedUser,
    user_id: str,
    thread_id: str,
) -> bool:
    """Return True when a recovered /threads row can be opened by caller.

    Recovery sources are advisory. Some old metadata rows can point at a
    thread now owned by another user; listing those rows creates sidebar
    zombies because detail routes correctly return 404. This mirrors
    _require_thread_access without claiming ownerless personal threads from
    a read-only list request.
    """
    owner = agent.accounts_repo.get_thread_owner(thread_id)
    if owner == user_id:
        return True
    if owner is not None:
        return user.role == "admin"
    if _is_shared_channel_thread(thread_id):
        return user.role == "admin" or user.via_act_as
    return True


def _collect_recoverable_thread_sources(
    *,
    agent: Any,
    settings: Any,
    user: AuthenticatedUser,
    user_id: str,
    owned_ids: set[str],
    checkpoint_ids: list[str],
    metadata_thread_ids: set[str],
) -> dict[str, set[str]]:
    """
    Find thread IDs referenced by thread-bound resources that can wake,
    route, or explain a thread even if ordinary metadata/owner rows are
    missing. These are shown by GET /threads so the desktop can surface and
    delete old partially-deleted threads.
    """
    sources: dict[str, set[str]] = {}

    for tid in metadata_thread_ids:
        if tid not in owned_ids:
            _add_thread_source(sources, tid, "metadata")

    for tid in checkpoint_ids:
        if tid not in owned_ids and _can_show_orphan_checkpoint_thread(
            agent, user, user_id, tid
        ):
            _add_thread_source(sources, tid, "checkpoint")

    try:
        if user_id in agent.todo_manager.get_all_users_with_todos():
            todo_list = agent.todo_manager.get_todos(user_id)
            for item in todo_list.items:
                _add_thread_source(sources, item.thread_id, "todo")
    except Exception as e:
        logger.warning("Failed to collect TODO thread references for %s: %s", user_id, e)

    try:
        schedule_db = getattr(agent, "_schedule_db", None)
        if schedule_db is not None:
            for entry in schedule_db.get_for_user(user_id):
                _add_thread_source(sources, entry.thread_id, "scheduled_todo")
    except Exception as e:
        logger.warning("Failed to collect scheduled TODO thread references for %s: %s", user_id, e)

    try:
        from ...core.trigger_manager import TriggerManager

        manager = getattr(agent, "trigger_manager", None) or TriggerManager(settings.data_dir)
        for trigger in manager.get_triggers(user_id):
            _add_thread_source(sources, trigger.thread_id, "trigger")
    except Exception as e:
        logger.warning("Failed to collect trigger thread references for %s: %s", user_id, e)

    try:
        for binding in agent.chat_bindings_repo.list_thread_bindings_for_user(user_id):
            _add_thread_source(sources, binding.thread_id, "chat_binding")
    except Exception as e:
        logger.warning("Failed to collect chat binding thread references for %s: %s", user_id, e)

    try:
        for tid in agent.chat_bindings_repo.list_bind_code_thread_ids_for_user(user_id):
            _add_thread_source(sources, tid, "bind_code")
    except Exception as e:
        logger.warning("Failed to collect bind-code thread references for %s: %s", user_id, e)

    return sources


def _thread_list_payload(
    agent: Any,
    thread_id: str,
    meta: Any,
    *,
    recovered: bool = False,
    recovery_sources: set[str] | None = None,
) -> dict[str, Any]:
    if meta:
        payload = meta.model_dump(mode="json")
    else:
        payload = {
            "thread_id": thread_id,
            "title": "Recovered thread" if recovered else "New Chat",
            "pinned": False,
            "platform": _classify_thread_platform_from_id(thread_id),
            "platform_meta": None,
            "created_at": None,
            "updated_at": None,
            "title_source": "recovered" if recovered else "default",
        }
    payload["platform"] = _thread_list_platform(agent, thread_id, meta)
    thread_config_manager = getattr(agent, "thread_config_manager", None)
    tc = (
        thread_config_manager.get_config(thread_id)
        if thread_config_manager is not None
        else None
    )
    is_callable = bool(tc and tc.callable)
    payload["callable"] = is_callable
    if is_callable and tc.callable_name:
        payload["title"] = tc.callable_name
        payload["title_source"] = "callable"
    payload["recovered"] = recovered
    payload["recovery_sources"] = sorted(recovery_sources or [])
    return payload


def _is_thread_processing(agent: Any, thread_id: str) -> bool:
    thread_locks = getattr(agent, "_thread_locks", None)
    if thread_locks is None:
        return False
    try:
        return thread_locks.get_lock_info(thread_id) is not None
    except Exception as e:
        logger.warning("Failed to inspect processing state for %s: %s", thread_id, e)
        return False


def create_threads_router(
    verify_api_key: Callable[..., Any],
    authed_user_id: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Any],
    require_thread_access_fn: Callable[..., None],
    publish_sync_event_fn: Callable[..., Any] = default_publish_sync_event,
) -> APIRouter:
    """Create the thread read/list/metadata/lifecycle router."""
    router = APIRouter(tags=["Threads"])

    @router.get(
        "/threads/{thread_id}/status",
        response_model=ThreadStatusResponse,
    )
    async def get_thread_status(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Get lightweight status for a thread.

        Returns the latest checkpoint ID as an opaque revision marker and the
        current in-process state. SQL checkpoint backends query only checkpoint
        metadata; the graph-state fallback is reserved for non-SQL backends.
        """
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        settings = get_settings_fn()

        if has_direct_checkpoint_revision_backend(settings):
            revision = await run_in_threadpool(
                get_latest_checkpoint_revision,
                settings,
                thread_id,
            )
        else:
            revision = await run_in_threadpool(
                get_graph_state_revision,
                agent,
                thread_id,
            )

        return ThreadStatusResponse(
            thread_id=thread_id,
            revision=revision,
            processing=_is_thread_processing(agent, thread_id),
        )

    @router.get(
        "/threads/{thread_id}/history",
        response_model=ThreadHistoryResponse,
    )
    async def get_thread_history(
        thread_id: str,
        include_internal: bool = Query(
            False,
            description="Include internal system messages (autonomous wake-ups, compaction prompts)",
        ),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Get conversation history for a thread.

        Returns all messages in the conversation including tool calls and results.
        By default, internal system messages (autonomous wake-ups, compaction prompts)
        are filtered out. Set include_internal=true for debugging to see all messages.
        """
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()

        show_autonomous = False
        show_prompt_metadata = False
        if not include_internal:
            tc = agent.thread_config_manager.get_config(thread_id)
            if tc:
                if tc.show_autonomous_prompts:
                    show_autonomous = True
                if tc.show_prompt_metadata:
                    show_prompt_metadata = True

        history = await run_in_threadpool(
            agent.get_conversation_history,
            thread_id,
            include_internal=include_internal,
            show_autonomous_prompts=show_autonomous,
            show_prompt_metadata=show_prompt_metadata,
        )
        return ThreadHistoryResponse(thread_id=thread_id, messages=history)

    @router.get("/threads/{thread_id}/context")
    async def get_thread_context_stats(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Get context window usage statistics for a thread.

        Returns token usage, context limit, and compaction history.
        """
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        stats = await run_in_threadpool(agent.get_context_stats, thread_id)
        if isinstance(stats, dict):
            stats["processing"] = _is_thread_processing(agent, thread_id)
        return stats

    @router.get("/threads/{thread_id}/metadata")
    async def get_thread_metadata(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Get platform metadata for a thread.

        Parses the thread ID to detect platform origin (desktop, discord,
        telegram, slack) and returns relevant metadata.
        """
        require_thread_access_fn(user, thread_id)
        if thread_id.startswith("discord_dm_"):
            return {
                "platform": "discord",
                "type": "dm",
                "channel_id": thread_id[len("discord_dm_"):],
            }
        if thread_id.startswith("discord_"):
            parts = thread_id.split("_")
            return {
                "platform": "discord",
                "type": "guild",
                "guild_id": parts[1] if len(parts) >= 2 else None,
                "channel_id": parts[2] if len(parts) >= 3 else None,
            }
        if thread_id.startswith("telegram_"):
            return {
                "platform": "telegram",
                "channel_id": thread_id[len("telegram_"):],
            }
        if thread_id.startswith("slack_"):
            return {
                "platform": "slack",
                "channel_id": thread_id[len("slack_"):],
            }
        return {"platform": "desktop"}

    @router.get("/threads")
    async def list_threads(
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        List all threads with metadata (titles, pins, platform info).

        Merges thread IDs from the checkpoint database, stored metadata, and
        thread-bound resources so all surfaces see the same thread list. The
        resource pass intentionally surfaces old partially-deleted threads so
        the desktop can show and delete them instead of hiding wake-up paths.
        """
        agent = get_agent_fn()
        settings = get_settings_fn()
        owned_ids = set(agent.accounts_repo.list_threads_for_user(user_id))
        all_checkpoint_ids = _get_checkpoint_thread_ids(settings)
        checkpoint_ids = [t for t in all_checkpoint_ids if t in owned_ids]
        checkpoint_set = set(checkpoint_ids)

        store = agent.thread_metadata_manager.get_store(user_id)
        recovery_sources = _collect_recoverable_thread_sources(
            agent=agent,
            settings=settings,
            user=user,
            user_id=user_id,
            owned_ids=owned_ids,
            checkpoint_ids=all_checkpoint_ids,
            metadata_thread_ids=set(store.threads),
        )

        threads = []
        seen: set[str] = set()

        for tid in checkpoint_ids:
            meta = store.threads.get(tid)
            threads.append(_thread_list_payload(agent, tid, meta))
            seen.add(tid)

        for tid, meta in store.threads.items():
            if tid in owned_ids and tid not in checkpoint_set:
                threads.append(_thread_list_payload(agent, tid, meta))
                seen.add(tid)

        for tid in sorted(recovery_sources):
            if tid in seen:
                continue
            if not _can_list_recovered_thread(agent, user, user_id, tid):
                continue
            threads.append(
                _thread_list_payload(
                    agent,
                    tid,
                    store.threads.get(tid),
                    recovered=True,
                    recovery_sources=recovery_sources[tid],
                )
            )
            seen.add(tid)

        return {"threads": threads, "total": len(threads)}

    @router.patch("/threads/{thread_id}/metadata")
    async def update_thread_metadata(
        http_request: Request,
        thread_id: str,
        request: ThreadMetadataUpdateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Update thread metadata (title, pin status)."""
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        fields: dict[str, Any] = {}
        title_source = None

        if request.title is not None:
            fields["title"] = request.title.strip()
            title_source = "user"
        if request.pinned is not None:
            fields["pinned"] = request.pinned

        if title_source:
            fields["title_source"] = title_source

        if request.title is not None:
            tc = agent.thread_config_manager.get_config(thread_id)
            if tc and tc.callable:
                new_name = request.title.strip()
                if not new_name:
                    raise HTTPException(
                        status_code=400,
                        detail="Cannot rename callable thread to empty title",
                    )
                validate_callable_name(new_name)
                core_tool_names = {t.name for t in ALL_TOOLS}
                if new_name in core_tool_names:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Callable name '{new_name}' conflicts with a core tool name",
                    )
                owned = set(agent.accounts_repo.list_threads_for_user(user_id))
                existing = agent.thread_config_manager.get_callable_thread_by_name(
                    new_name, owned_thread_ids=owned
                )
                if existing is not None and existing.thread_id != thread_id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Callable name '{new_name}' is already used by thread {existing.thread_id}",
                    )
                tc.callable_name = new_name
                agent.thread_config_manager.save_config(tc)
                agent.invalidate_thread_config_cache(thread_id)
                agent.sync_agent_tools()
                fields["title_source"] = "callable"

        meta = agent.thread_metadata_manager.upsert_thread(
            user_id, thread_id, **fields
        )

        client_id = http_request.headers.get("x-nymeria-client-id", "")
        sync_data: dict[str, Any] = {}
        if request.title is not None:
            sync_data["title"] = fields.get("title", request.title.strip())
            sync_data["title_source"] = fields.get("title_source", "user")
        if request.pinned is not None:
            sync_data["pinned"] = request.pinned
        if sync_data:
            publish_sync_event_fn(
                event_type="thread_updated",
                thread_id=thread_id,
                user_id=user_id,
                data=sync_data,
                origin_client_id=client_id,
            )

        return meta.model_dump(mode="json")

    @router.post("/threads/metadata/migrate")
    async def migrate_thread_metadata(
        request: ThreadMetadataMigrateRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        One-time migration: import thread metadata from frontend localStorage.

        Accepts the frontend's Thread[] format and imports into the backend
        metadata store. Only imports threads that don't already have metadata.
        """
        agent = get_agent_fn()
        count = agent.thread_metadata_manager.migrate_from_frontend(
            user_id, request.threads
        )
        return {"migrated_threads": count}

    @router.post("/threads/{thread_id}/claim")
    async def claim_thread_endpoint(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Eagerly claim ownership of a thread for the calling user.

        Used by the desktop frontend after locally generating a UUID for a
        new thread, so the thread_owners row exists before any chat-app
        binding (Telegram/Discord) routes a message into it. Without this,
        the first non-admin caller to hit /chat for the UUID would TOFU-claim
        and silently transfer ownership.
        """
        if _is_shared_channel_thread(thread_id):
            raise HTTPException(
                status_code=400,
                detail="Shared-channel threads cannot be claimed",
            )
        agent = get_agent_fn()
        owner = agent.accounts_repo.claim_thread(thread_id, user.id)
        if owner != user.id and user.role != "admin":
            raise HTTPException(status_code=404, detail="Not found")
        return {"thread_id": thread_id, "owner": owner}

    @router.delete("/threads/{thread_id}")
    async def delete_thread(
        http_request: Request,
        thread_id: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Fully delete a thread and all resources that can recreate it.

        This is the proper way to remove a thread from all surfaces. It
        cascades into checkpoints, metadata, config, notepad, RAG chunks,
        TODOs/schedule rows, triggers, chat bindings, bind codes, owner rows,
        activity entries, notifications, and device thread filters.
        """
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        settings = get_settings_fn()
        try:
            deletion = cascade_delete_thread(agent, settings, user_id, thread_id)
        except ThreadDeletionBusy as e:
            raise HTTPException(status_code=409, detail=str(e))
        except Exception as e:
            logger.error(f"Thread {thread_id} deletion failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

        logger.info(
            "Thread %s fully deleted: %s",
            thread_id,
            deletion.deleted,
        )

        client_id = http_request.headers.get("x-nymeria-client-id", "")
        publish_sync_event_fn(
            event_type="thread_deleted",
            thread_id=thread_id,
            user_id=user_id,
            data={},
            origin_client_id=client_id,
        )

        return deletion.model_dump()

    @router.post("/threads/{thread_id}/clear")
    async def clear_thread(
        http_request: Request,
        thread_id: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Clear conversation history for a thread (checkpoints only).

        Preserves thread config (tools, instructions, model overrides),
        notepad content, and metadata. Use DELETE /threads/{id} to
        remove everything.
        """
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        settings = get_settings_fn()

        try:
            config = {"configurable": {"thread_id": thread_id}}
            state = await agent._default_async_graph.aget_state(config)
            messages = state.values.get("messages", [])
            if messages:
                agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning(f"Pre-clear RAG flush failed for {thread_id}: {e}")

        agent.thread_metadata_manager.delete_thread(user_id, thread_id)

        try:
            delete_thread_checkpoints(settings, thread_id)
        except Exception as e:
            logger.warning(f"Failed to delete checkpoints for {thread_id}: {e}")

        logger.info(f"Thread {thread_id} conversation cleared (config + notepad preserved)")

        client_id = http_request.headers.get("x-nymeria-client-id", "")
        publish_sync_event_fn(
            event_type="thread_cleared",
            thread_id=thread_id,
            user_id=user_id,
            data={},
            origin_client_id=client_id,
        )

        return {"status": "ok", "thread_id": thread_id}

    return router
