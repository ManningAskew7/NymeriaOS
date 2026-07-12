"""Interactive chat SSE and sync routes."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...core.accounts import AuthenticatedUser
from ...core.agent_compaction import COMPACTING_MESSAGE
from ...core.interactive_admission import (
    InteractiveCapacityError,
    TurnSlot,
    admit_interactive_turn,
    attach_release_backstop,
)
from ...core.event_bus import (
    publish_agent_stream_chunk as default_publish_agent_stream_chunk,
    publish_autonomous_event as default_publish_autonomous_event,
    publish_sync_event as default_publish_sync_event,
)
from ...core.mention import (
    MentionAmbiguity,
    MentionTarget,
    resolve_thread_mention,
)
from ...core.notification_dispatch import (
    create_autonomous_notification as default_create_autonomous_notification,
    should_notify_autonomous as default_should_notify_autonomous,
)
from ...core.pending_prompt_queue import PENDING_QUEUE_META_EVENT_TYPES
from ...core.turn_stream_buffer import (
    STATE_ABORTED,
    STATE_DONE,
    STATE_ERROR,
    TurnStreamBuffer,
    get_turn_stream_registry,
)
from ..schemas.chat import ChatRequest, ChatResponse
from ..sse import SSE_RESPONSE_HEADERS, with_sse_keepalive
from ..thread_config_helpers import effective_provider_model

logger = logging.getLogger(__name__)


def run_sync_turn_with_tool_count(
    agent: Any, message: str, **chat_kwargs: Any
) -> tuple[str, int]:
    """Run one synchronous agent turn and return (response, tool_call_count).

    Must execute on the worker thread the turn is dispatched to
    (``asyncio.to_thread``): the count comes from the agent's per-thread turn
    metadata (``_chat_turn_local``), which cannot race between concurrent
    sync turns the way the shared ``_last_chat_tool_calls`` attribute does.
    Agents without the thread-local (test fakes) fall back to the shared
    attribute. Shared by ``/chat/sync`` and the in-process webhook-bot
    adapter (``_bot_inprocess.py``).
    """
    response = agent.chat(message, **chat_kwargs)
    local = getattr(agent, "_chat_turn_local", None)
    count = getattr(local, "tool_calls", None) if local is not None else None
    if count is None:
        count = getattr(agent, "_last_chat_tool_calls", 0)
    return response, count


def _attachment_dicts(request: ChatRequest) -> list[dict[str, str]] | None:
    if not request.attachments:
        return None
    return [
        {
            "file_type": att.file_type,
            "data_url": att.data_url,
            "mime_type": att.mime_type,
            "file_name": att.file_name or "",
        }
        for att in request.attachments
    ]


def _enforce_attachment_caps(request: ChatRequest, agent: Any, thread_id: str) -> None:
    """Reject requests whose attachments exceed the per-model caps.

    Source of truth is ``get_attachment_limits`` keyed by the thread's
    effective model. Raises 413 with the limit so the frontend can show
    the cap to the user. Skipped when the request has no attachments.
    """
    if not request.attachments and not request.images:
        return

    from ...config.model_capabilities import get_attachment_limits
    from ...config.model_capabilities import infer_mime_type, normalize_attachment_file_type

    effective_model = effective_provider_model(agent, thread_id).model
    limits = get_attachment_limits(effective_model)

    image_count = 0
    total_data_url_bytes = 0
    max_single_image_bytes = limits.get("max_image_bytes")

    for att in (request.attachments or []):
        mime = infer_mime_type(att.mime_type or "", att.file_name or "")
        file_type = normalize_attachment_file_type(
            att.file_type or "", mime, att.file_name or ""
        )
        # data: URL length over-counts vs the decoded bytes by ~33%; that
        # over-approximation is fine for a cap check.
        att_len = len(att.data_url or "")
        total_data_url_bytes += att_len
        if file_type == "image":
            image_count += 1
            if max_single_image_bytes is not None and att_len > max_single_image_bytes * 2:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "code": "attachment_too_large",
                        "limit_bytes": max_single_image_bytes,
                        "model": effective_model,
                        "file_name": att.file_name,
                    },
                )

    # Legacy `images` list counts toward the image cap too.
    for img in (request.images or []):
        image_count += 1
        total_data_url_bytes += len(img.data_url or "")

    max_images = limits.get("max_images_per_request")
    if max_images is not None and image_count > max_images:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "too_many_images",
                "limit": max_images,
                "received": image_count,
                "model": effective_model,
            },
        )

    max_total = limits.get("max_total_bytes")
    if max_total is not None and total_data_url_bytes > max_total * 2:
        raise HTTPException(
            status_code=413,
            detail={
                "code": "attachments_total_too_large",
                "limit_bytes": max_total,
                "model": effective_model,
            },
        )


def _agent_prompt_source(request: ChatRequest) -> tuple[str, str | None, str | None]:
    """Resolve queue source metadata from trusted server-side signals.

    Normal interactive chat is always ``user`` even if a client sends a
    ``source`` field. In-cluster autonomous callers already mark requests with
    ``is_self_invoke``; only that path may pass richer source metadata.
    """
    if not request.is_self_invoke or not request.source:
        return "user", None, None
    source = request.source
    if source not in {"trigger", "ticker", "watchdog", "callable", "mcp", "dream"}:
        source = "ticker"
    return source, request.source_id, request.source_label


def _spawn_goal_supervisor(
    agent: Any, user_id: str, worker_thread_id: str, goal: Any
) -> tuple[str | None, str | None]:
    """Spawn a fresh callable thread to serve as the supervisor for ``goal``.

    Returns ``(supervisor_thread_id, None)`` on success or
    ``(None, error_message)`` on failure.
    """
    import re

    from ...tools.spawn_thread import spawn_thread as _spawn_tool  # noqa: WPS433

    instructions = (
        f"You are the supervisor for goal {goal.goal_id} (objective: "
        f'"{goal.objective[:120]}"). You receive review requests from the '
        "worker thread; verify each one independently against the task's "
        "criterion, then call `mark_task_done(task_id)` to approve or "
        "`provide_review_feedback(task_id, feedback)` to send refinement "
        'guidance. Load `Skill(name="goal-supervisor")` for the full playbook.'
    )
    title = f"Supervisor: {goal.objective[:60]}"

    config = {
        "configurable": {
            "thread_id": worker_thread_id,
            "user_id": user_id,
        }
    }

    try:
        result = _spawn_tool.invoke(
            {
                "title": title,
                "instructions": instructions,
                "mode": "fresh",
                "ttl_hours": 48,
                "make_callable": True,
                "optional_tools": ["web_search_perplexity", "http_request", "tool_search"],
            },
            config=config,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "Failed to spawn supervisor for goal %s", goal.goal_id
        )
        return None, f"spawn_thread raised: {e}"

    if isinstance(result, str) and result.startswith("[Error]"):
        return None, result

    match = re.search(r"thread_id=(spawned-[A-Za-z0-9-]+)", str(result))
    if not match:
        return None, (
            f"Could not parse supervisor thread_id from spawn result: "
            f"{str(result)[:200]}"
        )
    return match.group(1), None


def _legacy_image_dicts(request: ChatRequest) -> list[dict[str, str]] | None:
    if not request.images:
        return None
    return [
        {
            "data_url": image.data_url,
            "mime_type": image.mime_type,
        }
        for image in request.images
    ]


def _dispatch_payload(target: MentionTarget, original_thread_id: str) -> dict[str, str]:
    return {
        "thread_id": target.thread_id,
        "title": target.title,
        "original_thread_id": original_thread_id,
    }


def _dispatch_stream_fields(
    target: MentionTarget | None,
    original_thread_id: str,
) -> dict[str, Any]:
    if target is None:
        return {}
    return {
        "dispatched_to": _dispatch_payload(target, original_thread_id),
        "original_thread_id": original_thread_id,
    }


def _mention_ambiguity_error(
    ambiguity: MentionAmbiguity,
    *,
    thread_id: str,
) -> dict[str, Any]:
    candidates = [
        {"thread_id": candidate.thread_id, "title": candidate.title}
        for candidate in ambiguity.candidates
    ]
    labels = ", ".join(
        f"{candidate.title} ({candidate.thread_id})"
        for candidate in ambiguity.candidates[:5]
    )
    suffix = "..." if len(ambiguity.candidates) > 5 else ""
    return {
        "type": "error",
        "code": "mention_ambiguous",
        "content": f"@{ambiguity.reference} matches multiple threads: {labels}{suffix}",
        "thread_id": thread_id,
        "details": {
            "reference": ambiguity.reference,
            "candidates": candidates,
        },
    }


def _create_quick_thread(
    agent: Any,
    user_id: str,
    parent_thread_id: str,
    publish_sync_event_fn: Callable[..., Any],
) -> str:
    """Create a fresh, clean-context temporary thread for a ``/quick`` query.

    No ``ThreadConfig`` is written, so the thread resolves to global defaults
    (the wanted clean context: no current-thread history, tools, or persona
    bleed). It is flagged ``lifetime=temporary`` so the idle sweep reaps it if
    the user never continues it, and claimed to the user so ``@<id>`` and
    ``/thread switch`` resolve it. Returns the new thread id; raises on the
    metadata/claim write so the caller can surface an error.
    """
    from ...core.time_utils import utc_now as _utc_now
    from ...tools.spawn_thread import (
        DEFAULT_IDLE_TIMEOUT_HOURS,
        PLATFORM_META_IDLE_TIMEOUT,
        PLATFORM_META_LAST_ACTIVE,
        PLATFORM_META_LIFETIME,
    )

    quick_thread_id = f"spawned-quick-{uuid.uuid4().hex[:8]}"
    platform_meta = {
        PLATFORM_META_LIFETIME: "temporary",
        PLATFORM_META_IDLE_TIMEOUT: str(DEFAULT_IDLE_TIMEOUT_HOURS),
        PLATFORM_META_LAST_ACTIVE: _utc_now().isoformat(),
        "spawn_parent": parent_thread_id,
    }
    agent.thread_metadata_manager.upsert_thread(
        user_id,
        quick_thread_id,
        title="Quick query",
        title_source="auto",
        platform_meta=platform_meta,
    )
    agent.accounts_repo.claim_thread(quick_thread_id, user_id)
    try:
        publish_sync_event_fn(
            event_type="thread_created",
            thread_id=quick_thread_id,
            user_id=user_id,
            data={
                "title": "Quick query",
                "title_source": "auto",
                "platform_meta": platform_meta,
            },
            # Empty origin (not the caller's client id): the caller did not
            # optimistically create this thread, so it MUST receive the
            # thread_created event, otherwise the sync origin filter would hide
            # the new thread from the very client that ran /quick.
            origin_client_id="",
        )
    except Exception:
        logger.warning(
            "Failed to publish thread_created for /quick thread %s",
            quick_thread_id,
            exc_info=True,
        )
    return quick_thread_id


def _quick_continue_footer(quick_thread_id: str) -> str:
    """Display-only footer appended to a /quick thread's first response."""
    return (
        f"\n\n---\nContinue this thread: start a message with "
        f"`@{quick_thread_id}` on any surface, or "
        f"`/thread switch {quick_thread_id}` in the CLI or a chat app."
    )


_DONE_USAGE = (
    "[Error]: Usage: `/done <prompt>` arms a one-shot follow-up that runs "
    "when the current turn finishes. With no turn running, the prompt is "
    "sent immediately."
)


def _try_arm_done_hook(
    agent, thread_id: str, user_id: str, prompt: str
) -> Optional[str]:
    """Arm a one-shot DONE hook carrying ``prompt`` when a turn is running.

    Returns the ack text when armed: the caller should return it and NOT run
    a turn (the hook fires at the running turn's DONE point and the shipped
    DONE-continue machinery re-drives the turn with the prompt). Returns
    ``None`` when the thread is idle, so the caller runs the prompt as a
    normal turn (the degenerate case).

    The hook is ``single_use`` (the recorder deletes it synchronously on its
    first ``ok`` run), which makes the create/turn-end race claimable: if the
    turn ends between the busy probe and the create, a successful delete
    proves the hook never fired and the prompt runs now; a failed delete
    proves the DONE fire already consumed it.
    """
    locks = agent._thread_locks
    if not locks.is_thread_busy(thread_id):
        return None
    # Arming is pointless when the hooks engine is off for this thread (the
    # DONE fire would never pick the hook up): fail honestly instead of
    # acking a follow-up that cannot run.
    from ...core.agent_safety import get_effective_hook_enabled

    hooks_on = get_effective_hook_enabled(
        SimpleNamespace(id=None, enabled=True),
        thread_id,
        thread_config_manager=getattr(agent, "thread_config_manager", None),
        settings=getattr(agent, "settings", None),
    )
    if not hooks_on:
        return (
            "[Error]: Lifecycle hooks are disabled for this thread, so `/done` "
            "cannot arm a follow-up. Enable hooks (`hooks_enabled`) or resend "
            "the prompt once the current turn finishes."
        )
    excerpt = prompt if len(prompt) <= 40 else prompt[:37] + "..."
    try:
        hook = agent.hook_manager.add_hook(
            user_id,
            name=f"/done: {excerpt}",
            event="done",
            action="inject_context",
            text=prompt,
            once=True,
            single_use=True,
            scope="thread",
            thread_id=thread_id,
            created_by="user",
        )
    except Exception as exc:  # noqa: BLE001 - surface as a command error
        return f"[Error]: Could not arm the follow-up: {exc}"
    if hook is None:
        return "[Error]: Hook limit reached; the follow-up was not armed."
    if not locks.is_thread_busy(thread_id):
        # The turn ended while we were arming. Claim the hook back by
        # deleting it: success = it never fired (run the prompt now);
        # failure = the DONE fire consumed it (single_use removal), so the
        # prompt already ran at turn end.
        if agent.hook_manager.delete_hook(user_id, hook.id):
            return None
    return (
        f"[Info]: Follow-up armed: your prompt will run when the current turn "
        f"finishes (one-shot hook `{hook.id}`, removed after firing)."
    )


def create_chat_router(
    verify_api_key: Callable[..., Any],
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Any],
    require_thread_access_fn: Callable[..., None],
    publish_sync_event_fn: Callable[..., Any] = default_publish_sync_event,
    publish_agent_stream_chunk_fn: Callable[..., Any] = default_publish_agent_stream_chunk,
    publish_autonomous_event_fn: Callable[..., Any] = default_publish_autonomous_event,
    create_autonomous_notification_fn: Callable[..., Any] = default_create_autonomous_notification,
    should_notify_autonomous_fn: Callable[..., bool] = default_should_notify_autonomous,
) -> APIRouter:
    """Create the interactive chat router with app dependencies injected."""
    router = APIRouter(tags=["Chat"])

    @router.post("/chat")
    async def chat_streaming(
        http_request: Request,
        request: ChatRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Send a message and receive streaming response via SSE.

        The response is streamed as Server-Sent Events (SSE) with the following event types:
        - `thinking`: Agent reasoning/planning
        - `tool_call_delta`: Model is streaming tool-call argument chunks
        - `tool_call`: Tool being invoked
        - `tool_result`: Tool execution result
        - `response`: Final response text
        - `error`: Error message
        - `done`: Stream complete

        Each event has `type` and `content`/`data` fields.
        """
        agent = get_agent_fn()
        thread_id = request.thread_id or str(uuid.uuid4())[:8]
        original_thread_id = thread_id
        message = request.message
        dispatched_target: MentionTarget | None = None
        # Set by the /quick intercept below: this turn runs in a fresh throwaway
        # thread and its response should carry a "continue this thread" footer.
        is_quick = False
        # Ignore client-claimed user_id in the body; derive from auth instead.
        user_id = user.id
        require_thread_access_fn(user, thread_id)

        # Per-model attachment cap. Raises 413 with the limit before any
        # SSE handshake so the frontend can show the cap inline.
        _enforce_attachment_caps(request, agent, thread_id)

        if not request.is_self_invoke:
            mention_resolution = resolve_thread_mention(
                message,
                user_id=user_id,
                thread_metadata_manager=agent.thread_metadata_manager,
                accounts_repo=agent.accounts_repo,
                thread_config_manager=agent.thread_config_manager,
            )
            if isinstance(mention_resolution, MentionAmbiguity):

                async def ambiguous_mention_response():
                    yield (
                        "data: "
                        + json.dumps(
                            _mention_ambiguity_error(
                                mention_resolution,
                                thread_id=original_thread_id,
                            )
                        )
                        + "\n\n"
                    )
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "type": "done",
                                "thread_id": original_thread_id,
                                "status": "error",
                            }
                        )
                        + "\n\n"
                    )

                return StreamingResponse(
                    ambiguous_mention_response(),
                    media_type="text/event-stream",
                    headers=SSE_RESPONSE_HEADERS,
                )
            if isinstance(mention_resolution, MentionTarget):
                require_thread_access_fn(user, mention_resolution.thread_id)
                dispatched_target = mention_resolution
                thread_id = mention_resolution.thread_id
                message = mention_resolution.message

        # Handle slash commands (e.g., /compact [focus instruction])
        msg_stripped = message.strip().lower()
        compact_tokens = msg_stripped.split(maxsplit=1)
        is_compact_cmd = bool(compact_tokens) and compact_tokens[0] == "/compact"
        logger.info(
            "[CHAT] Received message: '%s' stripped: '%s' is_compact: %s",
            message,
            msg_stripped,
            is_compact_cmd,
        )
        if is_compact_cmd:
            # Optional trailing text steers what the summary prioritizes. Parse it
            # from the original (case-preserved) message, not msg_stripped (lowered).
            raw_compact_parts = message.strip().split(maxsplit=1)
            compact_priority = (
                raw_compact_parts[1].strip() if len(raw_compact_parts) > 1 else ""
            ) or None

            async def compact_command_response():
                if dispatched_target is not None:
                    dispatch_event = {
                        "type": "dispatched",
                        "thread_id": original_thread_id,
                        "target_thread_id": dispatched_target.thread_id,
                        "title": dispatched_target.title,
                        "matched_ref": dispatched_target.reference,
                        **_dispatch_stream_fields(
                            dispatched_target,
                            original_thread_id,
                        ),
                    }
                    yield f"data: {json.dumps(dispatch_event)}\n\n"
                compact_started = asyncio.Event()

                async def _on_compaction_started() -> None:
                    compact_started.set()

                compact_task = asyncio.create_task(
                    agent.compact_now(
                        thread_id,
                        user_id,
                        on_started=_on_compaction_started,
                        priority=compact_priority,
                    )
                )
                start_task = asyncio.create_task(compact_started.wait())
                sent_compacting = False
                try:
                    await asyncio.wait(
                        {compact_task, start_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if compact_started.is_set():
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "type": "compacting",
                                    "message": COMPACTING_MESSAGE,
                                    "thread_id": original_thread_id
                                    if dispatched_target
                                    else thread_id,
                                    **_dispatch_stream_fields(
                                        dispatched_target,
                                        original_thread_id,
                                    ),
                                }
                            )
                            + "\n\n"
                        )
                        sent_compacting = True
                    result = await compact_task
                    if compact_started.is_set() and not sent_compacting:
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "type": "compacting",
                                    "message": COMPACTING_MESSAGE,
                                    "thread_id": original_thread_id
                                    if dispatched_target
                                    else thread_id,
                                    **_dispatch_stream_fields(
                                        dispatched_target,
                                        original_thread_id,
                                    ),
                                }
                            )
                            + "\n\n"
                        )
                finally:
                    if not start_task.done():
                        start_task.cancel()
                # Send response based on result
                if result.get("success"):
                    messages_removed = result.get("messages_removed", 0)
                    # Emit compacted event so frontend clears chat UI
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "type": "compacted",
                                "messages_removed": messages_removed,
                                "auto_resumed": False,
                                "summary": result.get("summary"),
                                "thread_id": original_thread_id
                                if dispatched_target
                                else thread_id,
                                **_dispatch_stream_fields(
                                    dispatched_target,
                                    original_thread_id,
                                ),
                            }
                        )
                        + "\n\n"
                    )
                    msg = f"✓ Conversation compacted. {messages_removed} messages summarized."
                else:
                    msg = f"Could not compact: {result.get('reason', 'unknown error')}"
                response_data = {
                    "type": "response",
                    "content": msg,
                    "thread_id": original_thread_id if dispatched_target else thread_id,
                    **_dispatch_stream_fields(dispatched_target, original_thread_id),
                }
                yield f"data: {json.dumps(response_data)}\n\n"
                # Include context_stats and model in done event
                context_stats = agent.get_context_stats(thread_id)
                effective_model = effective_provider_model(agent, thread_id).model
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "done",
                            "thread_id": original_thread_id
                            if dispatched_target
                            else thread_id,
                            "context_stats": context_stats,
                            "model": effective_model,
                            **_dispatch_stream_fields(
                                dispatched_target,
                                original_thread_id,
                            ),
                        }
                    )
                    + "\n\n"
                )

            # Compaction is an LLM call that can stay silent past tunnel idle
            # timeouts; keepalive comments hold the connection open.
            return StreamingResponse(
                with_sse_keepalive(compact_command_response()),
                media_type="text/event-stream",
                headers=SSE_RESPONSE_HEADERS,
            )

        # Default the user-visible chat-history text to the message the user
        # actually typed. The /orchestrate and /goal intercepts below may
        # rewrite `message` (for the agent's first turn) while keeping
        # `display_message` pointing at the user's original input.
        display_message = message

        def _slash_sse_response(content: str, target_thread_id: str):
            """Return a StreamingResponse that emits a single response chunk
            and a done event. Used by /orchestrate and /goal subcommand
            handlers that report state without triggering an agent turn."""

            async def _gen():
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "response",
                            "content": content,
                            "thread_id": target_thread_id,
                        }
                    )
                    + "\n\n"
                )
                yield (
                    "data: "
                    + json.dumps(
                        {"type": "done", "thread_id": target_thread_id}
                    )
                    + "\n\n"
                )

            return StreamingResponse(
                _gen(),
                media_type="text/event-stream",
                headers=SSE_RESPONSE_HEADERS,
            )

        # Handle /quick slash command (chat_stream execution).
        # /quick <prompt> runs the prompt in a brand-new, clean-context thread
        # and streams the answer INLINE on the current thread, reusing the
        # @<thread> dispatch + SSE re-tag path (nothing is written to the
        # current thread's context). The fresh thread persists as a temporary,
        # continuable thread (idle-swept if abandoned), and its first response
        # carries a "continue this thread" footer. A fresh id with no
        # ThreadConfig resolves to global defaults, which is the wanted clean
        # context.
        quick_tokens = msg_stripped.split(maxsplit=1)
        if not request.is_self_invoke and quick_tokens and quick_tokens[0] == "/quick":
            raw_parts = message.strip().split(maxsplit=1)
            quick_prompt = raw_parts[1].strip() if len(raw_parts) > 1 else ""
            if not quick_prompt:
                return _slash_sse_response(
                    "[Error]: Usage: `/quick <prompt>` — runs a one-off query "
                    "in a fresh, clean-context thread and shows the answer "
                    "here without leaving the current thread.",
                    thread_id,
                )

            try:
                quick_thread_id = _create_quick_thread(
                    agent,
                    user_id,
                    original_thread_id,
                    publish_sync_event_fn,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to create /quick thread")
                return _slash_sse_response(
                    f"[Error]: Could not start a quick thread: {exc}",
                    thread_id,
                )

            dispatched_target = MentionTarget(
                thread_id=quick_thread_id,
                title="Quick query",
                reference="quick",
                message=quick_prompt,
            )
            thread_id = quick_thread_id
            message = quick_prompt
            display_message = quick_prompt
            is_quick = True

        # Handle /done slash command (chat_stream execution).
        # /done <prompt> arms a one-shot single_use DONE hook on the busy
        # thread: the running turn picks it up at its DONE fire point and
        # re-drives with the prompt. With no turn running, the prompt just
        # runs as a normal turn now (fall through with the rewritten message).
        done_tokens = msg_stripped.split(maxsplit=1)
        if not request.is_self_invoke and done_tokens and done_tokens[0] == "/done":
            raw_parts = message.strip().split(maxsplit=1)
            done_prompt = raw_parts[1].strip() if len(raw_parts) > 1 else ""
            if not done_prompt:
                return _slash_sse_response(_DONE_USAGE, thread_id)
            done_ack = _try_arm_done_hook(agent, thread_id, user_id, done_prompt)
            if done_ack is not None:
                return _slash_sse_response(done_ack, thread_id)
            message = done_prompt
            display_message = done_prompt
            msg_stripped = message.strip().lower()

        # /resume: message-less true resume of a graceful iteration-cap halt
        # (backlog #27). The resume runs as an ordinary holder turn, so SSE
        # streaming, the turn replay buffer, re-attach, and DONE hooks are
        # all inherited. Never queued: a busy thread has nothing to resume,
        # so the busy probe acks with an error instead of enqueueing.
        # Resumability of the tail is validated inside astream under the
        # thread lock, surfacing as an error event with code=resume_invalid.
        resume_halted_turn = False
        if not request.is_self_invoke and msg_stripped == "/resume":
            if agent._thread_locks.is_thread_busy(thread_id):
                return _slash_sse_response(
                    "[Error]: A turn is already running on this thread; "
                    "there is nothing to resume.",
                    thread_id,
                )
            resume_halted_turn = True
            message = ""
            display_message = ""
            msg_stripped = ""

        # Handle /skill and /kit slash commands (chat_stream execution).
        # /skill <name> [prompt] activates a markdown-only skill, prepends
        # its body to the prompt, then continues through the normal agent
        # stream. /kit <name> [ttl] [prompt] does the same for Skill Kits
        # after binding required tools.
        skill_tokens = msg_stripped.split(maxsplit=1)
        if skill_tokens and skill_tokens[0] in {"/skill", "/kit"}:
            from ...core.command_service import prepare_skill_slash_command

            raw_parts = message.strip().split(maxsplit=1)
            rest = raw_parts[1].strip() if len(raw_parts) > 1 else ""
            prepared = prepare_skill_slash_command(
                agent=agent,
                thread_id=thread_id,
                user_id=user_id,
                mode="kit" if skill_tokens[0] == "/kit" else "skill",
                rest=rest,
                has_attachments=bool(request.attachments or request.images),
            )
            if not prepared.success or not prepared.should_stream:
                return _slash_sse_response(prepared.message, thread_id)
            message = prepared.message
            msg_stripped = message.strip().lower()

        # Handle /orchestrate slash command (chat_stream execution).
        # /orchestrate <objective>  → activate the orchestrate skill kit,
        #     then fall through to the regular agent.astream flow with a
        #     rewritten kickoff message so the agent starts orchestrating.
        # /orchestrate clear        → deactivate the kit and report.
        # /orchestrate status       → report current state without activating.
        orch_tokens = msg_stripped.split(maxsplit=1)
        if orch_tokens and orch_tokens[0] == "/orchestrate":
            from ...core.command_service import (
                activate_skill_kit,
                deactivate_skill_kit,
            )

            raw_parts = message.strip().split(maxsplit=1)
            rest = raw_parts[1].strip() if len(raw_parts) > 1 else ""
            rest_lower = rest.lower()

            if not rest:
                return _slash_sse_response(
                    "[Error]: Usage: `/orchestrate <objective>` to start, "
                    "`/orchestrate status` to inspect, "
                    "`/orchestrate clear` to exit.",
                    thread_id,
                )

            if rest_lower == "clear":
                _, msg_text = deactivate_skill_kit(
                    agent=agent,
                    thread_id=thread_id,
                    user_id=user_id,
                    skill_name="orchestrate",
                )
                return _slash_sse_response(msg_text, thread_id)

            if rest_lower == "status":
                tc = agent.thread_config_manager.get_config(thread_id)
                is_active = bool(
                    tc and "orchestrate" in (tc.enabled_skills or [])
                )
                msg_text = (
                    "[Info]: Orchestrate mode is ACTIVE on this thread. "
                    "Use `/orchestrate clear` to exit."
                    if is_active
                    else "[Info]: Orchestrate mode is not active. "
                    "Use `/orchestrate <objective>` to start."
                )
                return _slash_sse_response(msg_text, thread_id)

            # Main form: /orchestrate <objective>. Activate the kit, then
            # rewrite `message` so the regular agent.astream flow below kicks
            # off the agent's first orchestrator turn. The user-typed text is
            # preserved on `display_message` so chat history still shows the
            # original `/orchestrate <objective>` line.
            ok, activation_msg = activate_skill_kit(
                agent=agent,
                thread_id=thread_id,
                user_id=user_id,
                skill_name="orchestrate",
                reason="/orchestrate kickoff",
            )
            if not ok:
                return _slash_sse_response(activation_msg, thread_id)

            message = (
                f"[Orchestrator mode activated.] Goal to orchestrate: {rest}\n\n"
                "Load the orchestrate skill kit (call "
                '`Skill(name="orchestrate")`) to read the playbook, decompose '
                "the goal into tasks via `nym_todo`, present the task list to "
                "the user for approval, then begin delegating to forked workers."
            )
            msg_stripped = message.strip().lower()

        # Handle /goal slash command (chat_stream execution).
        # The /goal lifecycle is more involved than /orchestrate because it
        # has a two-phase activation (pending_approval → active) with a
        # supervisor-thread spawn at the approval step, plus pause/resume/clear
        # state transitions.
        goal_tokens = msg_stripped.split(maxsplit=1)
        if goal_tokens and goal_tokens[0] == "/goal":
            from ...core.command_service import (
                activate_skill_kit,
                deactivate_skill_kit,
            )
            from ...core.goal_manager import (
                GoalNotFoundError,
                GoalStateError,
                get_goal_manager,
            )

            raw_parts = message.strip().split(maxsplit=1)
            rest = raw_parts[1].strip() if len(raw_parts) > 1 else ""

            if not rest:
                return _slash_sse_response(
                    "[Error]: Usage: `/goal <objective>` to start, "
                    "`/goal approve` to start work, `/goal status` to inspect, "
                    "`/goal pause`/`/goal resume`/`/goal clear` to control, "
                    "`/goal cancel` aborts before approval.",
                    thread_id,
                )

            gm = get_goal_manager()
            if gm is None:
                return _slash_sse_response(
                    "[Error]: Goal manager not available on this server.",
                    thread_id,
                )

            sub_token, _, sub_rest = rest.partition(" ")
            sub_lower = sub_token.lower()

            # ── /goal status ─────────────────────────────────────────────
            if sub_lower == "status":
                goal = gm.get_active_goal_for_thread(user_id, thread_id)
                if goal is None:
                    return _slash_sse_response(
                        "[Info]: No active goal on this thread. "
                        "Use `/goal <objective>` to start one.",
                        thread_id,
                    )
                lines = [
                    f"Goal {goal.goal_id} — status: **{goal.status}**",
                    f"Objective: {goal.objective}",
                    "",
                    f"Turns used: {goal.turns_used}/{goal.max_turns}",
                    f"Consecutive rejections: {goal.consecutive_rejections}",
                ]
                if goal.helper_thread_id:
                    lines.append(f"Supervisor: {goal.helper_thread_id}")
                if goal.last_pause_reason:
                    lines.append(f"Last pause reason: {goal.last_pause_reason}")
                lines.append("")
                lines.append("Tasks:")
                if not goal.tasks:
                    lines.append("  (none yet — worker proposes tasks before approval)")
                else:
                    for i, t in enumerate(goal.tasks, 1):
                        marker = {
                            "pending": "[ ]",
                            "in_progress": "[~]",
                            "awaiting_review": "[?]",
                            "done": "[x]",
                        }.get(t.status, "[ ]")
                        crit_str = (
                            f" — criterion: {t.criterion}"
                            if t.criterion
                            else ""
                        )
                        lines.append(
                            f"  {marker} {i}. {t.description}{crit_str}"
                        )
                return _slash_sse_response(
                    "[Info]: " + "\n".join(lines), thread_id
                )

            # ── /goal approve ───────────────────────────────────────────
            if sub_lower == "approve":
                goal = gm.get_active_goal_for_thread(user_id, thread_id)
                if goal is None:
                    return _slash_sse_response(
                        "[Error]: No active goal to approve.", thread_id
                    )
                if goal.status != "pending_approval":
                    return _slash_sse_response(
                        f"[Error]: Goal {goal.goal_id} is "
                        f"`{goal.status}`, not awaiting approval.",
                        thread_id,
                    )
                if not goal.tasks:
                    return _slash_sse_response(
                        "[Error]: Goal has no tasks yet. Wait for the worker "
                        "to propose tasks via `propose_task` before approving.",
                        thread_id,
                    )

                supervisor_id, err = _spawn_goal_supervisor(
                    agent, user_id, thread_id, goal
                )
                if supervisor_id is None:
                    return _slash_sse_response(
                        f"[Error]: Could not spawn supervisor: {err}",
                        thread_id,
                    )

                ok_kit, kit_msg = activate_skill_kit(
                    agent=agent,
                    thread_id=supervisor_id,
                    user_id=user_id,
                    skill_name="goal-supervisor",
                    reason=f"/goal approve {goal.goal_id}",
                )
                if not ok_kit:
                    return _slash_sse_response(
                        f"[Error]: Supervisor spawned ({supervisor_id}) but "
                        f"kit activation failed: {kit_msg}",
                        thread_id,
                    )

                try:
                    gm.approve_goal(user_id, goal.goal_id, supervisor_id)
                except (GoalStateError, GoalNotFoundError) as e:
                    return _slash_sse_response(
                        f"[Error]: {e}", thread_id
                    )

                message = (
                    f"[Goal {goal.goal_id} approved.] Supervisor thread "
                    f"`{supervisor_id}` is ready to receive review requests. "
                    "Start executing the first pending task. When you "
                    "believe it meets its criterion, call "
                    "`request_review(task_id, summary, evidence)` to "
                    "escalate. The supervisor returns either an approval "
                    "(task done, move on) or refinement feedback (retry)."
                )
                msg_stripped = message.strip().lower()

            # ── /goal cancel ────────────────────────────────────────────
            elif sub_lower == "cancel":
                goal = gm.get_active_goal_for_thread(user_id, thread_id)
                if goal is None:
                    return _slash_sse_response(
                        "[Info]: No active goal to cancel.", thread_id
                    )
                if goal.status != "pending_approval":
                    return _slash_sse_response(
                        f"[Error]: Goal {goal.goal_id} is already "
                        f"`{goal.status}`. Use `/goal clear` to abort an "
                        "approved goal instead.",
                        thread_id,
                    )
                gm.clear_goal(user_id, goal.goal_id)
                deactivate_skill_kit(
                    agent=agent,
                    thread_id=thread_id,
                    user_id=user_id,
                    skill_name="goal-worker",
                )
                return _slash_sse_response(
                    f"[Success]: Pending goal {goal.goal_id} cancelled. "
                    "Worker kit deactivated.",
                    thread_id,
                )

            # ── /goal clear ─────────────────────────────────────────────
            elif sub_lower == "clear":
                goal = gm.get_active_goal_for_thread(user_id, thread_id)
                if goal is None:
                    return _slash_sse_response(
                        "[Info]: No active goal on this thread.", thread_id
                    )
                gm.clear_goal(user_id, goal.goal_id)
                deactivate_skill_kit(
                    agent=agent,
                    thread_id=thread_id,
                    user_id=user_id,
                    skill_name="goal-worker",
                )
                supervisor_id = goal.helper_thread_id
                if supervisor_id:
                    try:
                        from ...tools.spawn_thread import _delete_spawned

                        _delete_spawned(
                            agent=agent,
                            target_thread_id=supervisor_id,
                            user_id=user_id,
                            caller_thread_id=None,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to delete supervisor %s on /goal clear",
                            supervisor_id,
                        )
                return _slash_sse_response(
                    f"[Success]: Goal {goal.goal_id} cleared. "
                    + (
                        f"Supervisor {supervisor_id} deleted."
                        if supervisor_id
                        else "(no supervisor was spawned)"
                    ),
                    thread_id,
                )

            # ── /goal pause ─────────────────────────────────────────────
            elif sub_lower == "pause":
                goal = gm.get_active_goal_for_thread(user_id, thread_id)
                if goal is None:
                    return _slash_sse_response(
                        "[Error]: No active goal to pause.", thread_id
                    )
                try:
                    gm.pause_goal(
                        user_id, goal.goal_id, reason="user requested pause"
                    )
                except GoalStateError as e:
                    return _slash_sse_response(
                        f"[Error]: {e}", thread_id
                    )
                return _slash_sse_response(
                    f"[Success]: Goal {goal.goal_id} paused. "
                    "Use `/goal resume` to continue.",
                    thread_id,
                )

            # ── /goal resume ────────────────────────────────────────────
            elif sub_lower == "resume":
                goal = gm.get_active_goal_for_thread(user_id, thread_id)
                if goal is None:
                    return _slash_sse_response(
                        "[Error]: No goal on this thread.", thread_id
                    )
                try:
                    gm.resume_goal(user_id, goal.goal_id)
                except GoalStateError as e:
                    return _slash_sse_response(
                        f"[Error]: {e}", thread_id
                    )
                message = (
                    f"[Goal {goal.goal_id} resumed.] Continue from where you "
                    "left off. The next pending or in-progress task is the "
                    "one to work on; consult `nym_todo_list` or the goal "
                    "task list if you need a reminder."
                )
                msg_stripped = message.strip().lower()

            # ── /goal edit ──────────────────────────────────────────────
            elif sub_lower == "edit":
                return _slash_sse_response(
                    "[Info]: `/goal edit` is not yet implemented. To change "
                    "the plan before approval, ask the agent to call "
                    "`propose_task` for new items or run `/goal cancel` and "
                    "restart with a refined objective.",
                    thread_id,
                )

            # ── /goal <objective> (default) ────────────────────────────
            else:
                existing = gm.get_active_goal_for_thread(user_id, thread_id)
                if existing is not None:
                    return _slash_sse_response(
                        f"[Error]: This thread already has an active goal "
                        f"`{existing.goal_id}` (status: {existing.status}). "
                        f"Use `/goal status` to inspect or `/goal clear` to "
                        f"abort before starting a new one.",
                        thread_id,
                    )
                try:
                    goal = gm.create_goal(user_id, thread_id, rest)
                except GoalStateError as e:
                    return _slash_sse_response(
                        f"[Error]: {e}", thread_id
                    )
                ok, activation_msg = activate_skill_kit(
                    agent=agent,
                    thread_id=thread_id,
                    user_id=user_id,
                    skill_name="goal-worker",
                    reason="/goal kickoff",
                )
                if not ok:
                    gm.clear_goal(user_id, goal.goal_id)
                    return _slash_sse_response(activation_msg, thread_id)

                message = (
                    f"[Goal {goal.goal_id} initiated.] Objective: {rest}\n\n"
                    "Load the goal-worker skill kit (call "
                    '`Skill(name="goal-worker")`) to read the playbook. '
                    "Decompose the objective into tasks via `propose_task` "
                    "(each with a clear `description` and verifiable "
                    "`criterion`). Present the task list to the user and "
                    "wait for `/goal approve` before executing — only then "
                    "does the supervisor thread exist and `request_review` "
                    "become callable."
                )
                msg_stripped = message.strip().lower()

        # Global interactive-turn admission (backlog #83): only requests
        # that would START a new holder turn are gated. A prompt aimed at a
        # busy thread queues onto the running turn (no new concurrency) and
        # self-invoke relays are already bounded by max_concurrent_autonomous,
        # so both pass through (slot None). Sheds with 429 BEFORE the
        # message_added publish and the SSE handshake, so other clients never
        # render a user message that was not run.
        try:
            turn_slot: Optional[TurnSlot] = await admit_interactive_turn(
                agent,
                get_settings_fn(),
                thread_id,
                is_self_invoke=request.is_self_invoke,
            )
        except InteractiveCapacityError as exc:
            raise HTTPException(
                status_code=429,
                detail=exc.detail,
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc

        # Read client ID from header for sync event origin filtering
        client_id = http_request.headers.get("x-nymeria-client-id", "")

        # For autonomous/self-invoke calls (e.g. watchdog worker), the "user message"
        # isn't from a real user -- skip message_added so it doesn't appear in clients
        # as a user-authored message. Frontend subscribes to /autonomous/stream for
        # autonomous task events instead.
        # A /resume adds no user message, so there is nothing to echo to
        # other clients (they learn about the continuation via turn_resumed
        # and the streamed events instead).
        if not request.is_self_invoke and not resume_halted_turn:
            try:
                publish_sync_event_fn(
                    event_type="message_added",
                    thread_id=thread_id,
                    user_id=user_id,
                    data={"role": "user", "content": display_message},
                    origin_client_id=client_id,
                )
            except BaseException:
                # The admission slot is otherwise released by the response
                # generator's finally; a failure before the response exists
                # must not strand it.
                if turn_slot is not None:
                    turn_slot.release()
                raise

        # Autonomous task bookends: publish task_started/task_completed to Redis so
        # /autonomous/stream subscribers see watchdog/ticker activity live. Matches
        # the event pattern the former in-process watchdog emitted.
        #
        # Worker-relayed calls (Docker scheduler) carry
        # ``publish_autonomous_events=False`` because the worker publishes
        # bookends/chunks itself using stable task IDs (todo.id /
        # trigger-<id>). Suppressing the API-side mirror via a None task_id
        # gates task_started, the chunk fan-out, task_completed and the
        # autonomous notification in one place.
        should_publish_autonomous = (
            request.is_self_invoke and request.publish_autonomous_events
        )
        autonomous_task_id = (
            f"{request.trigger_override or 'autonomous'}-{thread_id}"
            if should_publish_autonomous
            else None
        )
        # Defer task_started publish until the first non-queued chunk arrives.
        # A queued chunk means the astream call is still waiting on the thread
        # lock while a user chat may still be streaming, so publishing
        # task_started there would make the frontend enter autonomous-streaming
        # mode at the wrong time.

        def _trigger_fields() -> dict[str, Any]:
            """Common trigger identity fields for autonomous event payloads."""
            fields: dict[str, Any] = {}
            if request.trigger_id:
                fields["trigger_id"] = request.trigger_id
            if request.trigger_name:
                fields["trigger_name"] = request.trigger_name
            return fields

        def _maybe_create_autonomous_notification(content: str, task_id: str) -> None:
            if not request.is_self_invoke:
                return
            create_autonomous_notification_fn(
                user_id=user_id,
                thread_id=thread_id,
                task_id=task_id or None,
                summary=(content or "Autonomous task completed")[:200],
                settings=get_settings_fn(),
                thread_config_manager=agent.thread_config_manager,
            )

        async def event_generator():
            """Generate SSE events from agent stream."""
            autonomous_final_content_parts: list[str] = []
            autonomous_completed = False
            autonomous_started = False

            # Turn stream buffer: created iff THIS request becomes the
            # lock-holder turn (the astream callback fires after lock
            # acquisition). Queued-prompt requests never create one, so the
            # holder's events are buffered exactly once. The buffer retains
            # the turn's wire payloads (including after a client disconnect)
            # so GET /threads/{id}/turn/stream can replay them.
            turn_buffer: Optional[TurnStreamBuffer] = None
            turn_started_pending = False

            # Graph message id for this turn's initiating HumanMessage,
            # minted here so the buffer can expose it to live-attach viewers
            # (they anchor hydrated history to it) and the agent can stamp
            # the same id on the message it persists. Resume turns add no
            # message, so they carry no anchor.
            turn_user_message_id = (
                None if resume_halted_turn else str(uuid.uuid4())
            )

            def _mark_turn_started() -> None:
                nonlocal turn_buffer, turn_started_pending
                # Holder metadata: relay turns (the Docker worker's
                # APIClientExecutor) are self-invoke, so their buffer is a
                # first-class attachable autonomous turn, matching the
                # in-process tee in core/stream_bridge.py.
                buffer_label = (
                    request.trigger_name
                    or request.source_label
                    or request.trigger_override
                    or request.source
                )
                turn_buffer = get_turn_stream_registry().begin_turn(
                    thread_id,
                    user_id,
                    user_message_id=turn_user_message_id,
                    holder_kind=(
                        "autonomous" if request.is_self_invoke else "user"
                    ),
                    source_label=(
                        str(buffer_label)[:80]
                        if request.is_self_invoke and buffer_label
                        else None
                    ),
                    user_message_internal=request.is_self_invoke,
                )
                turn_started_pending = True

            def _wire_payload(event: dict[str, Any]) -> str:
                """Serialize an outbound event, teeing holder events into the buffer.

                The buffer stamps ``seq`` and returns the exact payload string,
                so live wire and replay stay byte-identical.
                """
                if turn_buffer is not None:
                    _, payload = turn_buffer.append(event)
                    return payload
                return json.dumps(event)

            try:
                attachments = _attachment_dicts(request)
                images = _legacy_image_dicts(request)
                prompt_source, prompt_source_id, prompt_source_label = _agent_prompt_source(request)

                client_disconnected = False
                if dispatched_target is not None:
                    dispatch_event = {
                        "type": "dispatched",
                        "thread_id": original_thread_id,
                        "target_thread_id": dispatched_target.thread_id,
                        "title": dispatched_target.title,
                        "matched_ref": dispatched_target.reference,
                        **_dispatch_stream_fields(
                            dispatched_target,
                            original_thread_id,
                        ),
                    }
                    yield f"data: {json.dumps(dispatch_event)}\n\n"

                # Reset the idle-timeout clock when a user interactively runs a
                # turn on a temporary spawned thread (e.g. continuing a /quick
                # thread). Without this, only the callable-invoke path refreshes
                # activity, so an actively-continued quick thread could be
                # reaped by the idle sweep despite being in use. No-ops for
                # permanent threads (self-guards on lifetime=temporary).
                if not request.is_self_invoke and thread_id.startswith("spawned-"):
                    from ...tools.spawn_thread import refresh_thread_activity

                    refresh_thread_activity(agent, user_id, thread_id)

                stream_thread_id = (
                    original_thread_id if dispatched_target is not None else thread_id
                )

                async for chunk in agent.astream(
                    message,
                    thread_id=thread_id,
                    user_id=user_id,
                    attachments=attachments,
                    images=images,
                    force_unsupported_attachments=request.force_unsupported_attachments,
                    _is_self_invoke=request.is_self_invoke,
                    _trigger_override=request.trigger_override,
                    source=prompt_source,
                    source_id=prompt_source_id,
                    source_label=prompt_source_label or user_id,
                    _on_turn_started=_mark_turn_started,
                    _resume_halted_turn=resume_halted_turn,
                    _turn_user_message_id=turn_user_message_id,
                ):
                    # This request lost the lock race and queued its prompt
                    # onto the running holder turn instead of starting one:
                    # give the admission slot back while it observes the
                    # holder's stream. Only prompt_queued proves the queued
                    # outcome (the legacy bare `queued` event also fires on
                    # paths that still become the holder).
                    if turn_slot is not None and chunk.get("type") == "prompt_queued":
                        turn_slot.release()

                    # If the client disconnected, stop yielding SSE events but
                    # keep consuming the generator so the agent finishes its
                    # work, teeing holder events into the turn buffer so the
                    # client can re-attach via GET /threads/{id}/turn/stream.
                    # Results are also saved to thread history.
                    # Explicit cancellation uses POST /threads/{id}/stop instead.
                    if not client_disconnected and await http_request.is_disconnected():
                        client_disconnected = True
                        logger.info(
                            "Client disconnected for thread %s, agent will continue in background",
                            thread_id,
                        )

                    # Surface the holder-turn start on the wire (and as the
                    # buffer's first event) so clients learn the turn_id used
                    # for re-attach. Synthesized here, route-level, so direct
                    # agent.astream() consumers never see it. Self-invoke
                    # relay turns (the Docker worker's APIClientExecutor)
                    # still buffer it but do not get it on the wire: the
                    # trigger manager's task_started gate and the autonomous
                    # event-bus mirror keep their pre-existing first-chunk
                    # timing, and the relay consumer has no use for it.
                    if turn_started_pending and turn_buffer is not None:
                        turn_started_pending = False
                        started_payload = _wire_payload(
                            {
                                "type": "turn_started",
                                "turn_id": turn_buffer.turn_id,
                                "thread_id": stream_thread_id,
                                **_dispatch_stream_fields(
                                    dispatched_target,
                                    original_thread_id,
                                ),
                            }
                        )
                        if not client_disconnected and not request.is_self_invoke:
                            yield f"data: {started_payload}\n\n"

                    # Non-holder (queued-prompt) streams have nothing to tee;
                    # skip payload building entirely once the client is gone.
                    if client_disconnected and turn_buffer is None:
                        continue

                    event_data = _wire_payload(
                        {
                            **chunk,
                            "thread_id": stream_thread_id,
                            **_dispatch_stream_fields(
                                dispatched_target,
                                original_thread_id,
                            ),
                        }
                    )

                    if client_disconnected:
                        continue

                    # Publish task_started on the first non-queue-meta chunk
                    # so the frontend handoff happens only after the thread
                    # lock is acquired AND a real content event arrives.
                    # Queue-meta events (queued / prompt_queued /
                    # prompt_injected / prompt_absorbed / turn_halted /
                    # fanout_dropped) signal queue transitions, not the
                    # start of work.
                    if (
                        autonomous_task_id
                        and not autonomous_started
                        and chunk.get("type") not in PENDING_QUEUE_META_EVENT_TYPES
                    ):
                        publish_autonomous_event_fn(
                            event_type="task_started",
                            thread_id=thread_id,
                            user_id=user_id,
                            task_id=autonomous_task_id,
                            data={
                                "prompt": message,
                                "source": request.trigger_override or "autonomous",
                                **_trigger_fields(),
                            },
                        )
                        autonomous_started = True

                    yield f"data: {event_data}\n\n"

                    # Mirror streaming chunks to the autonomous event bus for
                    # self-invoke calls so /autonomous/stream subscribers see
                    # live progress.
                    if autonomous_task_id:
                        ctype = chunk.get("type")
                        publish_agent_stream_chunk_fn(
                            chunk,
                            thread_id=thread_id,
                            user_id=user_id,
                            task_id=autonomous_task_id,
                        )
                        if ctype == "response":
                            autonomous_final_content_parts.append(chunk.get("content", ""))

                # Terminal handling. The done payload (context stats, model,
                # auto-title) is built whenever the client is still connected
                # OR this request ran the holder turn: a disconnected holder
                # still buffers `done` so a re-attaching client gets a proper
                # turn end, and auto-title no longer depends on the caller's
                # connection (disconnected turns previously never titled
                # their thread or published the title sync event).
                still_connected = (
                    not client_disconnected and not await http_request.is_disconnected()
                )
                if still_connected or turn_buffer is not None:
                    # For /quick, append a display-only footer telling the user
                    # how to continue the fresh thread. Emitted as a trailing
                    # `response` chunk (re-tagged to the caller thread) BEFORE
                    # `done`, so it accumulates into the same assistant bubble
                    # on every surface and is never written to any checkpoint.
                    # Buffered for holder turns so a re-attached client sees
                    # the same footer.
                    if is_quick:
                        footer_payload = _wire_payload(
                            {
                                "type": "response",
                                "content": _quick_continue_footer(thread_id),
                                "thread_id": original_thread_id,
                                **_dispatch_stream_fields(
                                    dispatched_target,
                                    original_thread_id,
                                ),
                            }
                        )
                        if still_connected:
                            yield f"data: {footer_payload}\n\n"

                    # Get context stats and model info for UI
                    try:
                        context_stats = agent.get_context_stats(thread_id)
                    except Exception as e:
                        logger.warning("Failed to get context stats: %s", e)
                        context_stats = None

                    done_data = {
                        "type": "done",
                        "thread_id": original_thread_id
                        if dispatched_target is not None
                        else thread_id,
                        "context_stats": context_stats,
                        "model": effective_provider_model(agent, thread_id).model,
                        **_dispatch_stream_fields(
                            dispatched_target,
                            original_thread_id,
                        ),
                    }

                    # Auto-title the thread from the user's message if untitled
                    # (skipped on /resume: there is no user message to title
                    # from, and the thread already existed at the halt).
                    try:
                        new_title = None
                        if not resume_halted_turn:
                            new_title = agent.thread_metadata_manager.auto_title(
                                user_id, thread_id, message
                            )
                        if new_title:
                            done_data["title"] = new_title
                            done_data["title_source"] = "auto"
                            # Publish title change so other clients update their sidebar
                            publish_sync_event_fn(
                                event_type="thread_updated",
                                thread_id=thread_id,
                                user_id=user_id,
                                data={"title": new_title, "title_source": "auto"},
                                origin_client_id=client_id,
                            )
                    except Exception as e:
                        logger.warning("Failed to auto-title thread %s: %s", thread_id, e)

                    done_payload = _wire_payload(done_data)
                    if turn_buffer is not None:
                        turn_buffer.finish(STATE_DONE)
                    if still_connected:
                        yield f"data: {done_payload}\n\n"

            except Exception as e:
                logger.error("Stream error: %s", e, exc_info=True)
                error_data = _wire_payload(
                    {
                        "type": "error",
                        "content": str(e),
                        "thread_id": original_thread_id
                        if dispatched_target is not None
                        else thread_id,
                        **_dispatch_stream_fields(
                            dispatched_target,
                            original_thread_id,
                        ),
                    }
                )
                if turn_buffer is not None:
                    turn_buffer.finish(STATE_ERROR)
                yield f"data: {error_data}\n\n"
                if autonomous_task_id and not autonomous_completed:
                    error_text = str(e)
                    _maybe_create_autonomous_notification(error_text, autonomous_task_id)
                    publish_autonomous_event_fn(
                        event_type="task_completed",
                        thread_id=thread_id,
                        user_id=user_id,
                        task_id=autonomous_task_id,
                        data={
                            "error": True,
                            "content": error_text,
                            "notify": should_notify_autonomous_fn(
                                thread_id, agent.thread_config_manager
                            ),
                            "source": request.trigger_override or "autonomous",
                            **_trigger_fields(),
                        },
                    )
                    autonomous_completed = True
            finally:
                # The admission slot is held for the turn's whole life,
                # including after a client disconnect (a disconnected holder
                # turn keeps running and doing real work, so it keeps drawing
                # against the interactive ceiling). release() is idempotent
                # (no-op after the prompt_queued early release above).
                if turn_slot is not None:
                    turn_slot.release()
                # A holder buffer still live here means the generator died
                # without a terminal event (GeneratorExit / task cancel):
                # mark it aborted so re-attachers get an honest end-of-stream
                # instead of waiting on a turn that will never finish.
                # finish() no-ops when a terminal state is already set.
                if turn_buffer is not None:
                    turn_buffer.finish(STATE_ABORTED)
                if autonomous_task_id and not autonomous_completed:
                    final_content = "".join(autonomous_final_content_parts)
                    _maybe_create_autonomous_notification(final_content, autonomous_task_id)
                    publish_autonomous_event_fn(
                        event_type="task_completed",
                        thread_id=thread_id,
                        user_id=user_id,
                        task_id=autonomous_task_id,
                        data={
                            "content": final_content,
                            "notify": should_notify_autonomous_fn(
                                thread_id, agent.thread_config_manager
                            ),
                            "source": request.trigger_override or "autonomous",
                            **_trigger_fields(),
                        },
                    )

        # Keepalive comments bridge long silent gaps (tool calls that emit
        # nothing for minutes) so tunnel edges with idle timeouts, like
        # Cloudflare's ~100s proxy limit, do not cut the turn mid-stream.
        stream = event_generator()
        if turn_slot is not None:
            # Disconnect-before-first-byte backstop: if the client is already
            # gone when the response starts, the server can cancel the
            # response task before `stream` is ever iterated, and a
            # never-started generator never runs the slot-releasing finally
            # above. The finalize fires on GC of the orphaned generator;
            # release() is idempotent so normal turns are unaffected.
            attach_release_backstop(stream, turn_slot, asyncio.get_running_loop())
        return StreamingResponse(
            with_sse_keepalive(stream),
            media_type="text/event-stream",
            headers=SSE_RESPONSE_HEADERS,
        )

    @router.post("/chat/sync", response_model=ChatResponse)
    async def chat_sync(
        request: ChatRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Send a message and receive a non-streaming response.

        Simpler alternative to the streaming endpoint for clients that
        don't support SSE.
        """
        agent = get_agent_fn()
        thread_id = request.thread_id or str(uuid.uuid4())[:8]
        message = request.message
        # Ignore client-claimed user_id in the body; derive from auth instead.
        user_id = user.id
        require_thread_access_fn(user, thread_id)

        # Per-model attachment cap (same surface as /chat). 413 lands as a
        # JSON error response since this endpoint is non-streaming.
        _enforce_attachment_caps(request, agent, thread_id)

        if not request.is_self_invoke:
            mention_resolution = resolve_thread_mention(
                message,
                user_id=user_id,
                thread_metadata_manager=agent.thread_metadata_manager,
                accounts_repo=agent.accounts_repo,
                thread_config_manager=agent.thread_config_manager,
            )
            if isinstance(mention_resolution, MentionAmbiguity):
                error = _mention_ambiguity_error(
                    mention_resolution,
                    thread_id=thread_id,
                )
                return ChatResponse(
                    response=error["content"],
                    thread_id=thread_id,
                    tool_call_count=0,
                )
            if isinstance(mention_resolution, MentionTarget):
                require_thread_access_fn(user, mention_resolution.thread_id)
                thread_id = mention_resolution.thread_id
                message = mention_resolution.message

        msg_stripped = message.strip().lower()

        # /quick <prompt>: run in a fresh, clean-context temporary thread and
        # return the answer with a continue-this-thread footer. Sync parity
        # with the streaming /quick intercept above.
        is_quick = False
        quick_tokens = msg_stripped.split(maxsplit=1)
        if not request.is_self_invoke and quick_tokens and quick_tokens[0] == "/quick":
            raw_parts = message.strip().split(maxsplit=1)
            quick_prompt = raw_parts[1].strip() if len(raw_parts) > 1 else ""
            if not quick_prompt:
                return ChatResponse(
                    response=(
                        "[Error]: Usage: `/quick <prompt>` — runs a one-off "
                        "query in a fresh, clean-context thread."
                    ),
                    thread_id=thread_id,
                    tool_call_count=0,
                )
            try:
                thread_id = _create_quick_thread(
                    agent, user_id, thread_id, publish_sync_event_fn
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to create /quick thread")
                return ChatResponse(
                    response=f"[Error]: Could not start a quick thread: {exc}",
                    thread_id=thread_id,
                    tool_call_count=0,
                )
            message = quick_prompt
            is_quick = True

        # /done <prompt>: sync parity with the streaming intercept above.
        done_tokens = msg_stripped.split(maxsplit=1)
        if not request.is_self_invoke and done_tokens and done_tokens[0] == "/done":
            raw_parts = message.strip().split(maxsplit=1)
            done_prompt = raw_parts[1].strip() if len(raw_parts) > 1 else ""
            if not done_prompt:
                return ChatResponse(
                    response=_DONE_USAGE, thread_id=thread_id, tool_call_count=0
                )
            done_ack = _try_arm_done_hook(agent, thread_id, user_id, done_prompt)
            if done_ack is not None:
                return ChatResponse(
                    response=done_ack, thread_id=thread_id, tool_call_count=0
                )
            message = done_prompt
            msg_stripped = message.strip().lower()

        # /resume: sync parity with the streaming intercept above (see there).
        resume_halted_turn = False
        if not request.is_self_invoke and msg_stripped == "/resume":
            if agent._thread_locks.is_thread_busy(thread_id):
                return ChatResponse(
                    response=(
                        "[Error]: A turn is already running on this thread; "
                        "there is nothing to resume."
                    ),
                    thread_id=thread_id,
                    tool_call_count=0,
                )
            resume_halted_turn = True
            message = ""
            msg_stripped = ""

        skill_tokens = msg_stripped.split(maxsplit=1)
        if skill_tokens and skill_tokens[0] in {"/skill", "/kit"}:
            from ...core.command_service import prepare_skill_slash_command

            raw_parts = message.strip().split(maxsplit=1)
            rest = raw_parts[1].strip() if len(raw_parts) > 1 else ""
            prepared = prepare_skill_slash_command(
                agent=agent,
                thread_id=thread_id,
                user_id=user_id,
                mode="kit" if skill_tokens[0] == "/kit" else "skill",
                rest=rest,
                has_attachments=bool(request.attachments or request.images),
            )
            if not prepared.success or not prepared.should_stream:
                return ChatResponse(
                    response=prepared.message,
                    thread_id=thread_id,
                    tool_call_count=0,
                )
            message = prepared.message

        prompt_source, prompt_source_id, prompt_source_label = _agent_prompt_source(request)
        attachments = _attachment_dicts(request)
        images = (
            [
                {
                    "data_url": img.data_url,
                    "mime_type": img.mime_type,
                }
                for img in request.images
            ]
            if request.images
            else None
        )
        # Reset the idle clock when interactively continuing a temporary
        # spawned thread (mirrors the streaming path). No-op for permanent
        # threads.
        if not request.is_self_invoke and thread_id.startswith("spawned-"):
            from ...tools.spawn_thread import refresh_thread_activity

            refresh_thread_activity(agent, user_id, thread_id)

        # Global interactive-turn admission (backlog #83); same exemptions
        # and 429 contract as the streaming endpoint above. The sync path
        # cannot observe queue events, so a request that loses the lock race
        # holds its slot until agent.chat() returns (accepted overcount in a
        # one-probe race window; capacity policy, not correctness).
        try:
            turn_slot = await admit_interactive_turn(
                agent,
                get_settings_fn(),
                thread_id,
                is_self_invoke=request.is_self_invoke,
            )
        except InteractiveCapacityError as exc:
            raise HTTPException(
                status_code=429,
                detail=exc.detail,
                headers={"Retry-After": str(exc.retry_after)},
            ) from exc

        # The whole synchronous turn (LLM round trips, tools, checkpoint
        # writes) runs off the event loop; running it inline would freeze
        # every SSE stream and probe in the process for the turn's duration.
        try:
            response, tool_call_count = await asyncio.to_thread(
                run_sync_turn_with_tool_count,
                agent,
                message,
                thread_id=thread_id,
                user_id=user_id,
                attachments=attachments,
                images=images,
                force_unsupported_attachments=request.force_unsupported_attachments,
                _is_self_invoke=request.is_self_invoke,
                _trigger_override=request.trigger_override,
                source=prompt_source,
                source_id=prompt_source_id,
                source_label=prompt_source_label or user_id,
                _resume_halted_turn=resume_halted_turn,
            )
        finally:
            if turn_slot is not None:
                turn_slot.release()
        if is_quick:
            response = f"{response}{_quick_continue_footer(thread_id)}"
        return ChatResponse(
            response=response,
            thread_id=thread_id,
            tool_call_count=tool_call_count,
        )

    return router
