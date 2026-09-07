"""Thread read/list/metadata/lifecycle routes."""

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from ...core.accounts import AuthenticatedUser, InvalidIdentityId
from ...core.checkpoint_cleanup import delete_thread_checkpoints
from ...core.checkpoint_status import (
    get_graph_state_revision,
    get_latest_checkpoint_revision,
    has_direct_checkpoint_revision_backend,
)
from ...core.checkpointer_config import enumerate_checkpoint_thread_ids
from ...core.event_bus import publish_sync_event as default_publish_sync_event
from ...core.thread_classification import (
    classify_platform as _classify_thread_platform_from_id,
    is_shared_channel as _is_shared_channel_thread,
    parse_thread_metadata,
)
from ...core.thread_deletion import ThreadDeletionBusy, cascade_delete_thread
from ...core.turn_stream_buffer import get_turn_stream_registry
from ..schemas.threads import (
    ThreadBranchRequest,
    ThreadBranchResponse,
    ThreadClaimRequest,
    ThreadDreamRequest,
    ThreadDreamResponse,
    ThreadHistoryResponse,
    ThreadMetadataUpdateRequest,
    ThreadOverviewResponse,
    ThreadStatusResponse,
    ThreadTurnStatus,
)
from ..thread_overview import (
    build_thread_overview,
    is_thread_processing,
    resolve_display_platform,
)

logger = logging.getLogger(__name__)


def _thread_list_platform(agent: Any, thread_id: str, meta: Any = None) -> str:
    """Resolve the platform value the frontend should render for a thread.

    Thin delegator over the shared resolver in ``thread_overview`` (kept as a
    stable name imported by the chat-app and webhook bot routers).
    """
    return resolve_display_platform(agent, thread_id, meta=meta)


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
    if is_callable and tc is not None and tc.callable_name:
        payload["title"] = tc.callable_name
        payload["title_source"] = "callable"
    payload["recovered"] = recovered
    payload["recovery_sources"] = sorted(recovery_sources or [])
    return payload


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
        "/threads/{thread_id}/overview",
        response_model=ThreadOverviewResponse,
    )
    async def get_thread_overview(
        thread_id: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Get the resolved read model for a thread header/dashboard.

        This keeps /threads/{id}/config as the editable config payload while
        surfacing effective settings, counts, and subsystem status in one
        read-only call.
        """
        require_thread_access_fn(user, thread_id, claim=False)
        agent = get_agent_fn()
        settings = get_settings_fn()
        return await run_in_threadpool(
            build_thread_overview,
            agent=agent,
            settings=settings,
            user=user,
            user_id=user_id,
            thread_id=thread_id,
        )

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
        require_thread_access_fn(user, thread_id, claim=False)
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

        turn_buffer = get_turn_stream_registry().get(thread_id)
        return ThreadStatusResponse(
            thread_id=thread_id,
            revision=revision,
            processing=is_thread_processing(agent, thread_id),
            turn=(
                ThreadTurnStatus(**turn_buffer.snapshot())
                if turn_buffer is not None
                else None
            ),
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
        show_autonomous_prompts: bool | None = Query(
            None,
            description=(
                "Override the per-thread show_autonomous_prompts flag for this "
                "request. When provided, takes precedence over the per-thread "
                "config. Lets clients with a global UI preference (e.g. desktop) "
                "control history filtering without mutating per-thread state."
            ),
        ),
        include_hidden_anchors: bool = Query(
            False,
            description=(
                "Emit invisible stub entries (hidden: true, message_id only) "
                "for autonomous wakeups the show_autonomous_prompts filter "
                "would drop, so live-attach viewers can anchor-trim precisely. "
                "No effect with include_internal=true."
            ),
        ),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Get conversation history for a thread.

        Returns all messages in the conversation including tool calls and results.
        By default, internal system messages (autonomous wake-ups, compaction prompts)
        are filtered out. Set include_internal=true for debugging to see all messages.
        Pass show_autonomous_prompts to override the per-thread filter without
        mutating thread config.
        """
        require_thread_access_fn(user, thread_id, claim=False)
        agent = get_agent_fn()

        show_autonomous = False
        show_prompt_metadata = False
        if not include_internal:
            tc = agent.thread_config_manager.get_config(thread_id)
            if show_autonomous_prompts is not None:
                show_autonomous = show_autonomous_prompts
            elif tc and tc.show_autonomous_prompts:
                show_autonomous = True
            if tc and tc.show_prompt_metadata:
                show_prompt_metadata = True

        history = await run_in_threadpool(
            agent.get_conversation_history,
            thread_id,
            include_internal=include_internal,
            show_autonomous_prompts=show_autonomous,
            show_prompt_metadata=show_prompt_metadata,
            include_hidden_anchors=include_hidden_anchors,
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
        require_thread_access_fn(user, thread_id, claim=False)
        agent = get_agent_fn()
        stats = await run_in_threadpool(agent.get_context_stats, thread_id)
        if isinstance(stats, dict):
            stats["processing"] = is_thread_processing(agent, thread_id)
        return stats

    @router.get("/threads/{thread_id}/checkpoint")
    async def get_thread_checkpoint(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Return the raw deserialized latest LangGraph checkpoint for a thread.

        Developer/debugging endpoint. Owner-only. Unlike /history, no display
        projection is applied: every message is returned verbatim (tool calls,
        tool results, metadata) so that what is actually persisted in the
        checkpoint store can be verified.
        """
        require_thread_access_fn(user, thread_id, claim=False)
        agent = get_agent_fn()
        return await run_in_threadpool(agent.get_raw_checkpoint, thread_id)

    @router.get("/threads/{thread_id}/metadata")
    async def get_thread_metadata(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Get platform metadata for a thread.

        Parses the thread ID to detect platform origin (desktop, discord,
        telegram, slack, whatsapp, teams) and returns relevant metadata.
        """
        require_thread_access_fn(user, thread_id, claim=False)
        return parse_thread_metadata(thread_id)

    @router.get("/threads")
    async def list_threads(
        owned_only: bool = Query(
            False,
            description=(
                "If true, return only threads owned by the user — skip the "
                "checkpoint/metadata/resource recovery enrichment. Recommended "
                "for cleanup workflows and automated tests where seeing "
                "orphaned cross-user checkpoints would be surprising or unsafe."
            ),
        ),
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        List all threads with metadata (titles, pins, platform info).

        Merges thread IDs from the checkpoint database, stored metadata, and
        thread-bound resources so all surfaces see the same thread list. The
        resource pass intentionally surfaces old partially-deleted threads so
        the desktop can show and delete them instead of hiding wake-up paths.

        When ``owned_only=true`` the resource pass and all admin bypasses are
        skipped — the response contains only threads recorded in
        ``thread_owners`` for the authenticated user. Cleanup tooling should
        prefer this mode so a stray admin role cannot delete other users'
        recovered checkpoints.
        """
        agent = get_agent_fn()
        settings = get_settings_fn()
        owned_ids = set(agent.accounts_repo.list_threads_for_user(user_id))

        if owned_only:
            store = agent.thread_metadata_manager.get_store(user_id)
            threads = [
                _thread_list_payload(agent, tid, store.threads.get(tid))
                for tid in sorted(owned_ids)
            ]
            return {"threads": threads, "total": len(threads)}

        all_checkpoint_ids = enumerate_checkpoint_thread_ids(settings)
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

        # NOTE: title is purely a display string and is independent of
        # ``callable_name`` (the LLM tool binding). To rename the binding,
        # callers must update ``callable_name`` explicitly via
        # PATCH /threads/{id}/config. The list_threads payload still derives
        # the display title from ``callable_name`` for callable threads
        # (see _thread_list_payload), so the user-visible invariant is
        # preserved by the read path without coupling the write paths.

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

    @router.post(
        "/threads/{thread_id}/branch",
        response_model=ThreadBranchResponse,
    )
    async def branch_thread_endpoint(
        http_request: Request,
        thread_id: str,
        request: ThreadBranchRequest,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Create a new thread from the current thread's checkpoint history.

        The branch inherits the source thread's persisted LangGraph checkpoints
        and per-thread config. Callable branches receive a deduplicated callable
        name so they do not collide with the source thread's tool registration.
        """
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        settings = get_settings_fn()

        if is_thread_processing(agent, thread_id):
            raise HTTPException(
                status_code=409,
                detail="Cannot branch while the source thread is processing",
            )

        from ...core.thread_branch import ThreadBranchError, branch_thread

        try:
            result = await run_in_threadpool(
                branch_thread,
                agent=agent,
                settings=settings,
                user_id=user_id,
                source_thread_id=thread_id,
                title=request.title,
                from_message_index=request.from_message_index,
            )
        except ThreadBranchError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error("Thread %s branch failed: %s", thread_id, e, exc_info=True)
            raise HTTPException(status_code=500, detail=str(e)) from e

        client_id = http_request.headers.get("x-nymeria-client-id", "")
        publish_sync_event_fn(
            event_type="thread_created",
            thread_id=result["thread_id"],
            user_id=user_id,
            data={
                "title": result["title"],
                "title_source": result["metadata"].get("title_source", "user"),
                "platform": result["metadata"].get("platform", "desktop"),
                "source_thread_id": thread_id,
            },
            origin_client_id=client_id,
        )

        return result

    @router.post(
        "/threads/{thread_id}/dream",
        response_model=ThreadDreamResponse,
    )
    async def dream_thread(
        thread_id: str,
        request: ThreadDreamRequest | None = None,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Manually trigger a dream (self-reflection) cycle for this thread.

        Spawns a shadow sibling thread with the dream system prompt and a
        tool whitelist scoped to memory, TODOs, instructions, skills, and
        trigger review.
        The shadow thread runs in the background; this endpoint returns
        immediately with its ID. Frontends observe progress via the
        autonomous SSE event stream keyed on ``shadow_thread_id``.

        Respects the parent's ``dreaming.enabled`` opt-in unless ``force``
        is True. The scheduler's interval/idle/turn gates are bypassed for
        manual triggers — that's the whole point of a manual run.
        """
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        req = request or ThreadDreamRequest()

        if is_thread_processing(agent, thread_id):
            raise HTTPException(
                status_code=409,
                detail="Cannot dream while the thread is processing a turn",
            )

        tc = agent.thread_config_manager.get_config(thread_id)
        if not req.force:
            if tc is None or tc.dreaming is None or not tc.dreaming.enabled:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Dreaming is not enabled for this thread. "
                        "Enable it via PATCH /threads/{id}/config with "
                        "dreaming.enabled=true, or retry with force=true."
                    ),
                )

        if tc and tc.shadow_parent_id:
            raise HTTPException(
                status_code=400,
                detail="Refusing to dream from a shadow thread.",
            )

        from ...core.dreaming import DreamInvocationError, invoke_dream

        # Only the explicit one-shot request model is an override here; when it
        # is absent invoke_dream resolves the per-thread dream model, then the
        # global dream-default model, then None.
        model_override = req.model

        try:
            shadow_thread_id, summary = await run_in_threadpool(
                invoke_dream,
                agent=agent,
                parent_thread_id=thread_id,
                user_id=user_id,
                model_override=model_override,
            )
        except DreamInvocationError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(
                "dream_thread: invoke_dream failed for %s: %s",
                thread_id,
                e,
                exc_info=True,
            )
            raise HTTPException(status_code=500, detail=str(e)) from e

        return ThreadDreamResponse(
            shadow_thread_id=shadow_thread_id,
            parent_thread_id=thread_id,
            started_at=summary["started_at"],
            model=summary["model"],
            enabled_optional_tools=summary["enabled_optional_tools"],
            disabled_core_tools=summary["disabled_core_tools"],
        )

    @router.post("/threads/{thread_id}/claim")
    async def claim_thread_endpoint(
        thread_id: str,
        request: ThreadClaimRequest | None = None,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Eagerly claim ownership of a thread for the calling user.

        Used by first-party clients after locally generating a UUID for a
        new personal thread, so the thread_owners row exists before any
        chat-app binding (Telegram/Discord) routes a message into it. Without
        this, the first non-admin caller to hit /chat for the UUID would
        TOFU-claim and silently transfer ownership. Clients may also include
        initial metadata such as ``platform="cli"`` for non-desktop surfaces.
        """
        if _is_shared_channel_thread(thread_id):
            raise HTTPException(
                status_code=400,
                detail="Shared-channel threads cannot be claimed",
            )
        agent = get_agent_fn()
        try:
            owner = agent.accounts_repo.claim_thread(thread_id, user.id)
        except InvalidIdentityId as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if owner != user.id and user.role != "admin":
            raise HTTPException(status_code=404, detail="Not found")
        metadata: dict[str, Any] | None = None
        if request is not None and owner == user.id:
            fields: dict[str, Any] = {}
            title = request.title.strip() if request.title is not None else None
            platform = request.platform.strip() if request.platform is not None else None
            if title:
                fields["title"] = title
                fields["title_source"] = "user"
            if platform:
                fields["platform"] = platform
            if fields:
                meta = agent.thread_metadata_manager.upsert_thread(
                    user.id,
                    thread_id,
                    **fields,
                )
                metadata = meta.model_dump(mode="json")
        if metadata is not None:
            return {"thread_id": thread_id, "owner": owner, **metadata}
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
