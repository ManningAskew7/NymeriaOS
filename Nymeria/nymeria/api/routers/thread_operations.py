"""Thread portability, attachment validation, compaction, and stop routes."""

import logging
import mimetypes
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from ...core.accounts import AuthenticatedUser
from ...core.event_bus import publish_sync_event as default_publish_sync_event
from ..schemas.thread_operations import (
    AttachmentLimits,
    AttachmentLimitsResponse,
    AttachmentValidationRequest,
    AttachmentValidationResponse,
    ThreadRewindRequest,
    ThreadRewindResponse,
)

logger = logging.getLogger(__name__)


def _thread_share_available_tool_names(
    agent: Any,
    user_role: str,
) -> tuple[set[str], set[str], set[str]]:
    """Return (available tools, role-gated tools, callable tool names)."""
    from ...tools import (
        ADMIN_ONLY_OPTIONAL_TOOL_NAMES,
        ALL_TOOLS,
        DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES,
        OPTIONAL_TOOLS,
    )

    names = {t.name for t in ALL_TOOLS}
    names.update(OPTIONAL_TOOLS.keys())
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
    role_gated = set(ADMIN_ONLY_OPTIONAL_TOOL_NAMES)
    if user_role != "admin":
        role_gated.update(DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES)
    return names - callable_names, role_gated, callable_names


def _thread_share_available_skill_names(agent: Any, user_id: str) -> set[str]:
    if getattr(agent, "skill_manager", None) is None:
        return set()
    try:
        return {s.name for s in agent.skill_manager.list_installed(user_id=user_id)}
    except Exception:
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
        llm_cfg = agent._get_llm_config_for_thread(thread_id)
        effective_provider = llm_cfg.provider or agent.settings.llm_provider
        effective_model = llm_cfg.model or agent.settings.llm_model

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
        llm_cfg = agent._get_llm_config_for_thread(thread_id)
        effective_provider = llm_cfg.provider or agent.settings.llm_provider
        effective_model = llm_cfg.model or agent.settings.llm_model

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
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Manually trigger compaction for a thread.

        Compresses conversation history into a summary while preserving recent messages.
        Compaction always runs under the authenticated caller — admins impersonate via
        ``X-Nymeria-Act-As``, which ``verify_api_key`` resolves before we get here.
        """
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        result = await agent.compact_now(thread_id, user.id)
        return result

    @router.post("/threads/{thread_id}/prune")
    async def prune_thread(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Deterministically compress tool returns in a thread (no LLM).

        Each ToolMessage in the thread's active state is rewritten to a short
        placeholder marker that preserves the message id and tool_call_id so the
        AIMessage to ToolMessage linkage remains valid. The agent retains the
        full reasoning trail; re-invoking a tool fetches the real result.
        Idempotent; skips ToolMessages already pruned and very short ones.
        """
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        result = await agent.prune_now(thread_id, user.id)
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
        Remove the last N user+assistant exchanges from a thread.

        Backs the CLI /undo and /retry commands. An exchange starts at a
        HumanMessage and includes every following AIMessage/ToolMessage up to
        the next HumanMessage. Uses LangGraph's RemoveMessage + update_state,
        the same mechanism as context trimming.
        """
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        steps = request.steps if request is not None else 1

        removed = await run_in_threadpool(
            agent.rewind_thread_exchanges, thread_id, steps
        )

        client_id = http_request.headers.get("x-nymeria-client-id", "")
        publish_sync_event_fn(
            event_type="thread_rewound",
            thread_id=thread_id,
            user_id=user_id,
            data={"steps": steps, "removed": removed},
            origin_client_id=client_id,
        )

        return ThreadRewindResponse(
            status="ok",
            thread_id=thread_id,
            steps=steps,
            removed=removed,
        )

    @router.post("/threads/{thread_id}/stop")
    async def stop_thread(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Stop any running operation on a thread.

        Signals the abort event for the thread and cascades to any active
        callable threads it has spawned. The current stream()/astream() call
        breaks at the next iteration boundary, releasing the thread lock.
        """
        require_thread_access_fn(user, thread_id)
        agent = get_agent_fn()
        lock_info = agent._thread_locks.get_lock_info(thread_id)

        # Only create abort events for threads with active operations
        # to prevent unbounded memory growth from arbitrary thread IDs
        if lock_info:
            agent.abort_with_cascade(thread_id)
            return {
                "status": "stopping",
                "thread_id": thread_id,
                "message": (
                    f"Stop signal sent. Thread was held by '{lock_info.get('holder')}' "
                    f"for {lock_info.get('held_seconds', 0):.0f}s. "
                    f"Will stop at next iteration boundary."
                ),
            }
        return {
            "status": "idle",
            "thread_id": thread_id,
            "message": "Thread was not running. No stop signal needed.",
        }

    return router
