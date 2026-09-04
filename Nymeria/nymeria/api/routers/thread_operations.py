"""Thread portability, attachment validation, compaction, stop, and turn re-attach routes."""

import json
import logging
import mimetypes
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ...core.accounts import AuthenticatedUser
from ...core.claude_code_delivery import cancel_active_job as cancel_active_code_job
from ...core.event_bus import publish_sync_event as default_publish_sync_event
from ...core.turn_stream_buffer import (
    TurnReplayGapError,
    get_turn_stream_registry,
)
from ..schemas.thread_operations import (
    AttachmentLimits,
    AttachmentLimitsResponse,
    AttachmentValidationRequest,
    AttachmentValidationResponse,
    ThreadRewindRequest,
    ThreadRewindResponse,
)
from ..sse import SSE_RESPONSE_HEADERS, with_sse_keepalive
from ..thread_config_helpers import effective_provider_model

logger = logging.getLogger(__name__)


def _thread_share_available_tool_names(
    agent: Any,
    user_role: str,
) -> tuple[set[str], set[str], set[str]]:
    """Return (available tools, role-gated tools, callable tool names)."""
    from ...tools import (
        ADMIN_ONLY_TOOL_NAMES,
        SEED_TOOLS,
        DEVELOPER_ONLY_TOOL_NAMES,
        CATALOG_TOOLS,
    )

    names = {t.name for t in SEED_TOOLS}
    names.update(CATALOG_TOOLS.keys())
    try:
        names.update(t.get("name") for t in agent.tool_registry.list_tools() if t.get("name"))
    except Exception:
        logger.warning("Failed to query tool registry for available tools", exc_info=True)

    callable_names = set(getattr(agent, "_callable_tool_thread_map", {}) or {})
    try:
        callable_names.update(
            tc.callable_name
            for tc in agent.thread_config_manager.list_callable_threads()
            if tc.callable_name
        )
    except Exception:
        logger.warning("Failed to list callable threads for tool names", exc_info=True)

    # Callable tools are resolved from ownership, not enabled_tools. Do
    # not preserve guessed callable names as portable tool enablements.
    role_gated = set(ADMIN_ONLY_TOOL_NAMES)
    if user_role != "admin":
        role_gated.update(DEVELOPER_ONLY_TOOL_NAMES)
    return names - callable_names, role_gated, callable_names


def _thread_share_available_skill_names(agent: Any, user_id: str) -> set[str]:
    if getattr(agent, "skill_manager", None) is None:
        return set()
    try:
        return {s.name for s in agent.skill_manager.list_installed(user_id=user_id)}
    except Exception:
        logger.warning(
            "Failed to list installed skills for thread-share export",
            exc_info=True,
        )
        return set()


def _thread_share_title(document: dict[str, Any]) -> str:
    raw = document.get("title")
    if not isinstance(raw, str) or not raw.strip():
        source = document.get("source")
        if isinstance(source, dict):
            raw = source.get("title")
    if not isinstance(raw, str) or not raw.strip():
        return "Imported Thread"
    return raw.strip()[:200]


def create_thread_operations_router(
    verify_api_key: Callable[..., Any],
    authed_user_id: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    require_thread_access_fn: Callable[..., None],
    publish_sync_event_fn: Callable[..., Any] = default_publish_sync_event,
) -> APIRouter:
    """Create the thread operation router with app dependencies injected."""
    router = APIRouter(tags=["Threads"])

    @router.get("/threads/{thread_id}/export")
    async def export_thread_share(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Export a portable, shareable thread configuration document."""
        require_thread_access_fn(user, thread_id, claim=False)
        from ...core.thread_share import build_thread_share_document

        agent = get_agent_fn()
        meta = agent.thread_metadata_manager.get_thread(user.id, thread_id)
        title = meta.title if meta else "New Chat"
        tc = agent.thread_config_manager.get_config(thread_id)
        return build_thread_share_document(thread_id=thread_id, title=title, config=tc)

    @router.post("/threads/import")
    async def import_thread_share(
        http_request: Request,
        document: dict[str, Any],
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Create a new empty thread from a portable thread configuration."""
        from ...core.thread_share import ThreadShareError, sanitize_import_config

        agent = get_agent_fn()
        available_tools, admin_only_tools, _callable_tool_names = (
            _thread_share_available_tool_names(agent, user.role)
        )
        available_skills = _thread_share_available_skill_names(agent, user_id)
        owned = set(agent.accounts_repo.list_threads_for_user(user_id))
        owned_callable_names: set[str] = set()
        try:
            owned_callables = agent.thread_config_manager.list_callable_threads(
                owned_thread_ids=owned
            )
            owned_callable_names.update(
                tc.callable_name for tc in owned_callables if tc.callable_name
            )
        except Exception:
            logger.warning("Failed to list user callable threads during import", exc_info=True)

        title = _thread_share_title(document)
        thread_id = f"imported-{uuid.uuid4().hex[:12]}"
        try:
            tc, warnings = sanitize_import_config(
                document=document,
                new_thread_id=thread_id,
                importer_role=user.role,
                available_tool_names=available_tools,
                unavailable_enabled_tool_names=admin_only_tools,
                available_skill_names=available_skills,
                unavailable_callable_names=available_tools | owned_callable_names,
            )
        except ThreadShareError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

        if not agent.thread_config_manager.save_config(tc):
            raise HTTPException(status_code=500, detail="Failed to save imported thread config")

        agent.accounts_repo.claim_thread(thread_id, user_id)

        metadata_title = tc.callable_name if tc.callable and tc.callable_name else title
        metadata_platform = "callable" if tc.callable else "desktop"
        metadata_source = "callable" if tc.callable else "user"
        agent.thread_metadata_manager.upsert_thread(
            user_id,
            thread_id,
            title=metadata_title,
            title_source=metadata_source,
            platform=metadata_platform,
        )

        agent.invalidate_thread_config_cache(thread_id)
        if tc.callable:
            agent.sync_agent_tools()

        client_id = http_request.headers.get("x-nymeria-client-id", "")
        publish_sync_event_fn(
            event_type="thread_created",
            thread_id=thread_id,
            user_id=user_id,
            data={
                "title": metadata_title,
                "title_source": metadata_source,
                "platform": metadata_platform,
            },
            origin_client_id=client_id,
        )

        result = tc.model_dump(mode="json")
        result["has_customizations"] = tc.has_customizations()
        return {
            "status": "ok",
            "thread_id": thread_id,
            "title": metadata_title,
            "config": result,
            "warnings": warnings,
        }

    @router.post(
        "/threads/{thread_id}/attachments/validate",
        response_model=AttachmentValidationResponse,
    )
    async def validate_thread_attachments(
        thread_id: str,
        request: AttachmentValidationRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Preflight-check attachment compatibility against the effective thread model.

        This does not send content to any model. It only evaluates whether
        attachments are likely compatible and provides warnings plus force-send guidance.
        Ownership is still enforced — the effective provider/model can leak which
        upstream a thread is configured to use, so a guess-the-thread-id probe must
        be denied.
        """
        require_thread_access_fn(user, thread_id)
        from ...config.model_capabilities import (
            evaluate_attachment_compatibility,
            get_attachment_limits,
        )

        agent = get_agent_fn()
        effective_provider, effective_model = effective_provider_model(agent, thread_id)

        attachments = [
            {
                "file_type": att.file_type,
                "data_url": att.data_url,
                "mime_type": att.mime_type,
                "file_name": att.file_name or "",
            }
            for att in request.attachments
        ]

        report = evaluate_attachment_compatibility(
            effective_model,
            effective_provider,
            attachments,
        )
        limits = get_attachment_limits(effective_model)

        return AttachmentValidationResponse(
            compatible=bool(report["compatible"]),
            effective_provider=effective_provider,
            effective_model=effective_model,
            model_input_modalities=list(report["model_input_modalities"]),
            required_modalities=list(report["required_modalities"]),
            unsupported_modalities=list(report["unsupported_modalities"]),
            warnings=list(report["warnings"]),
            can_force_send=True,
            limits=AttachmentLimits(**limits),
        )

    @router.get(
        "/threads/{thread_id}/attachment_limits",
        response_model=AttachmentLimitsResponse,
    )
    async def get_thread_attachment_limits(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Return per-model attachment caps for this thread's effective model.

        Frontend polls this on thread/model change to drive the input bar's
        slot counter ("12 / 100 images") and pre-flight oversize rejection.
        Effective model is resolved against thread-level overrides so a thread
        routed to a different provider gets the correct numbers.
        """
        require_thread_access_fn(user, thread_id, claim=False)
        from ...config.model_capabilities import get_attachment_limits

        agent = get_agent_fn()
        effective_provider, effective_model = effective_provider_model(agent, thread_id)

        limits = get_attachment_limits(effective_model)
        return AttachmentLimitsResponse(
            effective_provider=effective_provider,
            effective_model=effective_model,
            limits=AttachmentLimits(**limits),
        )

    @router.get(
        "/threads/{thread_id}/attachments/{attachment_id}/download",
    )
    async def download_thread_attachment(
        thread_id: str,
        attachment_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Stream the original bytes of a sandboxed thread attachment.

        Owner-scoped: the caller must own the thread (the existing
        ``require_thread_access_fn`` returns 404 otherwise so attachment ids
        don't become a probe surface for which threads exist). Looks up the
        record by id via ``attachment_sandbox.find_attachment_by_id`` and
        streams the file from the per-thread sandbox.
        """
        require_thread_access_fn(user, thread_id, claim=False)
        from ...core.attachment_sandbox import (
            find_attachment_by_id,
            get_thread_attachment_dir,
        )

        record = find_attachment_by_id(thread_id, attachment_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Attachment not found")

        # Defense in depth: re-confine the persisted sandbox_path to this
        # thread's attachment directory before streaming it. The path is
        # system-written today, but resolving + containment-checking ensures a
        # tampered or legacy record can never serve a file outside the sandbox
        # (compare the workspace tools, which confine the same way).
        sandbox_dir = get_thread_attachment_dir(thread_id).resolve()
        try:
            path = Path(record.sandbox_path).resolve()
        except (OSError, RuntimeError):
            raise HTTPException(status_code=404, detail="Attachment not found")
        if not path.is_relative_to(sandbox_dir) or not path.is_file():
            raise HTTPException(status_code=404, detail="Attachment file missing on disk")

        media_type = (
            record.mime_type
            or mimetypes.guess_type(str(path))[0]
            or "application/octet-stream"
        )
        return FileResponse(
            path=str(path),
            media_type=media_type,
            filename=record.original_name or path.name,
        )

    @router.post("/threads/{thread_id}/compact")
    async def compact_thread(
        thread_id: str,
        priority: str | None = None,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Manually trigger compaction for a thread.

        Compresses conversation history into a summary while preserving recent messages.
        Compaction always runs under the authenticated caller — admins impersonate via
        ``X-Nymeria-Act-As``, which ``verify_api_key`` resolves before we get here.

        Optional ``priority`` query param is a free-text focus instruction that steers
        what the summary emphasizes (it never drops other required content).
        """
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        result = await agent.compact_now(thread_id, user.id, priority=priority)
        return result

    @router.post("/threads/{thread_id}/prune")
    async def prune_thread(
        thread_id: str,
        mode: str = "full",
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Deterministically compress tool returns in a thread (no LLM).

        Each large ToolMessage in the thread's active state is rewritten in
        place (id + tool_call_id preserved, so the AIMessage to ToolMessage
        linkage stays valid). ``mode=full`` (default) drops the body to a
        placeholder; ``mode=soft`` keeps the first 500 chars. The agent retains
        the full reasoning trail; re-invoking a tool fetches the real result.
        Idempotent; skips ToolMessages already pruned and ones below the mode's
        threshold.
        """
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        result = await agent.prune_now(thread_id, user.id, mode=mode)
        return result

    @router.post(
        "/threads/{thread_id}/rewind",
        response_model=ThreadRewindResponse,
    )
    async def rewind_thread(
        http_request: Request,
        thread_id: str,
        request: ThreadRewindRequest | None = None,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Remove trailing user+assistant exchanges from a thread.

        Backs the CLI /undo and /retry commands (steps mode) and the GUI
        edit/rewind affordances (to_message_id mode, exact targeting). An
        exchange starts at a HumanMessage and includes every following
        AIMessage/ToolMessage up to the next HumanMessage. Uses LangGraph's
        RemoveMessage + update_state, the same mechanism as context trimming.

        Refuses with 409 while the thread lock is held (a turn is running)
        and with 404 when to_message_id is not a user message in state.
        """
        from ...core.agent_context import RewindTargetNotFound

        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()

        # Advisory busy guard, mirroring stop_thread: the check is not atomic
        # with the rewind itself (real exclusion would require this route to
        # participate in the pending-prompt release protocol, which is turn
        # machinery). is_thread_busy probes the actual lock, so a turn that
        # has acquired it but not yet stamped lock_info is still refused.
        lock_info = agent._thread_locks.get_lock_info(thread_id)
        if lock_info is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Thread is busy (held by '{lock_info.get('holder')}' for "
                    f"{lock_info.get('held_seconds', 0):.0f}s). Stop the "
                    "current turn before rewinding."
                ),
            )
        if agent._thread_locks.is_thread_busy(thread_id):
            raise HTTPException(
                status_code=409,
                detail="Thread is busy. Stop the current turn before rewinding.",
            )

        steps = request.steps if request is not None else 1
        to_message_id = request.to_message_id if request is not None else None

        try:
            result = await run_in_threadpool(
                agent.rewind_thread,
                thread_id,
                steps=steps,
                to_message_id=to_message_id,
            )
        except RewindTargetNotFound:
            raise HTTPException(
                status_code=404,
                detail=(
                    "Rewind target not found in thread state. It may have "
                    "been removed by compaction; refresh the conversation "
                    "and try again."
                ),
            )

        steps_echo = result.exchanges if to_message_id is not None else steps
        if result.removed > 0:
            event_data: dict[str, Any] = {
                "steps": steps_echo,
                "removed": result.removed,
            }
            if to_message_id is not None:
                event_data["to_message_id"] = to_message_id

            client_id = http_request.headers.get("x-nymeria-client-id", "")
            publish_sync_event_fn(
                event_type="thread_rewound",
                thread_id=thread_id,
                user_id=user_id,
                data=event_data,
                origin_client_id=client_id,
            )

        return ThreadRewindResponse(
            status="ok",
            thread_id=thread_id,
            steps=steps_echo,
            removed=result.removed,
        )

    @router.post("/threads/{thread_id}/stop")
    async def stop_thread(
        http_request: Request,
        thread_id: str,
        user_id: str = Depends(authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Stop any running operation on a thread.

        Signals the abort event for the thread and cascades to any active
        callable threads it has spawned. The current stream()/astream() call
        breaks at the next iteration boundary, releasing the thread lock.

        A user-initiated stop hands queued user prompts back instead of
        discarding them: they are returned as ``restored_prompts`` (raw
        text, FIFO) so the client can restore them to the composer, and a
        ``queue_restored`` sync event tells the user's other clients to
        drop their queued-prompt displays.
        """
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        lock_info = agent._thread_locks.get_lock_info(thread_id)
        # A user's /code run holds no thread lock (its Claude Code job runs
        # on the host runner), so the holder check below cannot see it; its
        # own registry is the cancel seam, consulted on every stop.
        code_job = cancel_active_code_job(thread_id)

        # Only create abort events for threads with active operations
        # to prevent unbounded memory growth from arbitrary thread IDs
        if lock_info:
            from ...core.pending_prompt_queue import restored_prompts_payload

            restored = agent.abort_with_cascade(thread_id, restore_queue=True)
            restored_payload = restored_prompts_payload(restored)
            if restored_payload:
                # Best-effort: the queue is already drained and the response
                # body below is the initiating client's only copy of the
                # restored texts, so a publish failure must not 500 the stop.
                try:
                    client_id = http_request.headers.get("x-nymeria-client-id", "")
                    publish_sync_event_fn(
                        event_type="queue_restored",
                        thread_id=thread_id,
                        user_id=user_id,
                        data={
                            "count": len(restored_payload),
                            "prompts": [p["text"] for p in restored_payload],
                        },
                        origin_client_id=client_id,
                    )
                except Exception:  # pragma: no cover - defensive
                    logger.warning(
                        "queue_restored publish failed for thread %s",
                        thread_id,
                        exc_info=True,
                    )
            return {
                "status": "stopping",
                "thread_id": thread_id,
                "holder": lock_info.get("holder"),
                "held_seconds": lock_info.get("held_seconds", 0),
                "restored_prompts": restored_payload,
                "message": (
                    f"Stop signal sent. Thread was held by '{lock_info.get('holder')}' "
                    f"for {lock_info.get('held_seconds', 0):.0f}s. "
                    f"Will stop at next iteration boundary."
                ),
            }
        if code_job is not None:
            held = max(0.0, time.time() - code_job.started_at)
            return {
                "status": "stopping",
                "thread_id": thread_id,
                "holder": f"Claude Code job {code_job.id}",
                "held_seconds": round(held, 1),
                "restored_prompts": [],
                "message": (
                    f"Cancel signal sent to Claude Code job {code_job.id} "
                    f"(running for {held:.0f}s)."
                ),
            }
        return {
            "status": "idle",
            "thread_id": thread_id,
            "restored_prompts": [],
            "message": "Thread was not running. No stop signal needed.",
        }

    @router.get("/threads/{thread_id}/turn/stream")
    async def reattach_turn_stream(
        thread_id: str,
        turn_id: Optional[str] = Query(
            None,
            description=(
                "Turn to attach to (from the turn_started stream event). "
                "Omit to attach to the thread's current or most recent turn."
            ),
        ),
        from_seq: int = Query(
            0,
            ge=0,
            description="Replay only events with seq greater than this value.",
        ),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Re-attach to a thread's in-flight (or just-finished) interactive turn.

        Replays the turn's buffered SSE events (byte-identical to the original
        ``POST /chat`` stream, each stamped with ``seq``), then tails live
        events until the turn ends. The stream opens with a ``turn_attach``
        meta event carrying the turn's id/state so clients can render an
        honest recovery state. 404 ``turn_not_found`` when there is nothing
        to attach to (no turn ran recently, its buffer expired, or the buffer
        now holds a different turn than ``turn_id``); clients fall back to
        history reconciliation. 410 ``turn_replay_gap`` when overflow evicted
        events after ``from_seq``; same fallback. An in-flight turn whose
        writer died without a terminal event replays with state ``aborted``.
        """
        # claim=False: this is a read endpoint like the sibling status and
        # history GETs, so it must not TOFU-claim an ownerless thread.
        require_thread_access_fn(user, thread_id, claim=False)
        buffer = get_turn_stream_registry().get(thread_id)
        if buffer is None or (turn_id is not None and buffer.turn_id != turn_id):
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "turn_not_found",
                    "message": "No attachable turn for this thread.",
                },
            )
        if buffer.has_replay_gap(from_seq):
            raise HTTPException(
                status_code=410,
                detail={
                    "code": "turn_replay_gap",
                    "message": (
                        "Events after from_seq were evicted by the buffer "
                        "bounds; reconcile via thread history instead."
                    ),
                },
            )

        attach_meta = {
            "type": "turn_attach",
            "thread_id": thread_id,
            **buffer.snapshot(),
        }

        async def replay_generator():
            yield f"data: {json.dumps(attach_meta)}\n\n"
            try:
                async for payload in buffer.stream_payloads(from_seq=from_seq):
                    yield f"data: {payload}\n\n"
            except TurnReplayGapError:
                # Overflow eviction outran this reader mid-stream (the
                # attach-time check can only catch gaps that already exist).
                # Tell the client honestly instead of silently skipping the
                # evicted span; clients treat this like the 410: reconcile
                # via thread history.
                gap_event = {
                    "type": "turn_replay_gap",
                    "thread_id": thread_id,
                    "turn_id": buffer.turn_id,
                }
                yield f"data: {json.dumps(gap_event)}\n\n"

        return StreamingResponse(
            with_sse_keepalive(replay_generator()),
            media_type="text/event-stream",
            headers=SSE_RESPONSE_HEADERS,
        )

    return router
