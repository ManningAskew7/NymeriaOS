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
from ...core.command_forms import CommandResultLevel, render_outcome
from ...core.interactive_admission import (
    InteractiveCapacityError,
    TurnSlot,
    admit_interactive_turn,
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
from ...core.turn_runner import AsyncTurnSink, HOLDER_STARTED, TurnSpec, start_turn
from ...core.turn_stream_buffer import TurnReplayGapError
from ..schemas.chat import ChatRequest, ChatResponse
from ..sse import SSE_RESPONSE_HEADERS, with_sse_keepalive
from ..thread_config_helpers import effective_provider_model

from ...core.thread_lock_manager import THREAD_DELETED_MESSAGE, get_thread_epoch, thread_admission_guard

logger = logging.getLogger(__name__)


class _TurnStreamingResponse(StreamingResponse):
    """Close the observer even if ASGI never starts the body iterator."""

    def __init__(self, content: Any, sink: AsyncTurnSink) -> None:
        super().__init__(content, media_type="text/event-stream", headers=SSE_RESPONSE_HEADERS)
        self._sink = sink

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._sink.detach()


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


def _privileged_platform_caller(user: Any) -> bool:
    """Whether the authenticated caller may assert ``platform_origin``.

    The chat-platform bots (and the Docker worker) authenticate with the
    admin service token, usually acting as the linked user via
    ``X-Nymeria-Act-As`` (which resolves with ``via_act_as=True``); a direct
    admin token qualifies too. Anyone else could otherwise fabricate an
    origin pointing at another user's chat and drive cross-chat reaction
    writes through the bots, so the field is silently ignored for them.
    """
    return bool(getattr(user, "via_act_as", False)) or (
        getattr(user, "role", "") == "admin"
    )


def _effective_platform_origin(request: ChatRequest, privileged: bool) -> Any:
    """The request's ``platform_origin``, or None when absent or unprivileged."""
    origin = request.platform_origin
    if origin is None:
        return None
    if not privileged:
        logger.debug(
            "Ignoring platform_origin from non-privileged caller (thread %s)",
            request.thread_id,
        )
        return None
    return origin


def _apply_platform_origin(
    agent: Any,
    request: ChatRequest,
    thread_id: str,
    user_id: str,
    message: str,
    *,
    privileged: bool,
) -> str:
    """Record (or clear) the turn's platform origin; enrich reaction prompts.

    A privileged request carrying ``platform_origin`` stamps it into the
    per-thread registry (``core/bot_reactions.py``) so the ``react`` tool can
    target the originating message; every other request CLEARS the entry, so
    the registry describes the current turn and ``react`` can never act on a
    stale origin left by an earlier bot turn. For reaction-triggered turns
    (``kind == "reaction"``) the react-tool guidance block is appended while
    the tool is unbound on the thread (the bound check IS
    ``select_tools_for_graph``, so the omit rule cannot drift). Returns the
    (possibly extended) message.
    """
    from ...core.bot_reactions import clear_turn_origin, set_turn_origin

    origin = _effective_platform_origin(request, privileged)
    if origin is None:
        clear_turn_origin(thread_id)
        return message

    set_turn_origin(
        thread_id,
        platform=origin.platform,
        channel_id=origin.channel_id,
        message_id=origin.message_id,
        kind=origin.kind,
    )
    if origin.kind != "reaction":
        return message
    try:
        from ...tools.react import reaction_guidance_block

        guidance = reaction_guidance_block(agent, user_id, thread_id)
    except Exception:  # noqa: BLE001 - guidance is best-effort enrichment
        logger.debug("Failed to build reaction guidance block", exc_info=True)
        guidance = ""
    if guidance:
        return f"{message}\n\n{guidance}"
    return message


def _turn_reply_suppressed(
    request: ChatRequest, thread_id: str, *, privileged: bool
) -> bool:
    """Whether this turn's react call asked to suppress the reply text.

    Gated on the request having carried an honored ``platform_origin``: only
    those requests stamp the per-thread flag at turn start, and only their
    (bot) callers act on the terminal ``suppress_reply`` stamp.
    """
    if _effective_platform_origin(request, privileged) is None:
        return False
    from ...core.bot_reactions import reply_suppressed

    return reply_suppressed(thread_id)


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


def _slash_sse_response(
    body: str, level: CommandResultLevel, target_thread_id: str
) -> StreamingResponse:
    """One-shot SSE response for chat_stream slash outcomes that report state
    without triggering an agent turn: a single ``response`` chunk, then
    ``done``.

    ``body`` is a plain outcome body; the artifact prefix comes from
    ``render_outcome`` (the one producer, #144), so this router never spells
    the vocabulary itself. The ``response`` frame also carries ``level``
    verbatim (additive; documented in ``api.md``): current clients render
    the markdown and ignore it, but it lets a future slice (#146) style
    these outcomes (GUI error cards, CLI glyphs) without string matching.
    """

    async def _gen():
        yield (
            "data: "
            + json.dumps(
                {
                    "type": "response",
                    "content": render_outcome(level, body),
                    "level": level,
                    "thread_id": target_thread_id,
                }
            )
            + "\n\n"
        )
        yield (
            "data: "
            + json.dumps({"type": "done", "thread_id": target_thread_id})
            + "\n\n"
        )

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers=SSE_RESPONSE_HEADERS,
    )


def _slash_sync_response(
    body: str, level: CommandResultLevel, thread_id: str
) -> ChatResponse:
    """Sync twin of ``_slash_sse_response``: same producer, same level
    semantics, `ChatResponse` shape (which has no level field; adding one
    is a #146 concern, and this helper is then the one place to do it)."""
    return ChatResponse(
        response=render_outcome(level, body),
        thread_id=thread_id,
        tool_call_count=0,
    )


def _quick_continue_footer(quick_thread_id: str) -> str:
    """Display-only footer appended to a /quick thread's first response."""
    return (
        f"\n\n---\nContinue this thread: start a message with "
        f"`@{quick_thread_id}` on any surface, or "
        f"`/thread switch {quick_thread_id}` in the CLI or a chat app."
    )


_DONE_USAGE = (
    "Usage: `/done <prompt>` arms a one-shot follow-up that runs "
    "when the current turn finishes. With no turn running, the prompt is "
    "sent immediately."
)


def _try_arm_done_hook(
    agent, thread_id: str, user_id: str, prompt: str
) -> Optional[tuple[CommandResultLevel, str]]:
    """Arm a one-shot DONE hook carrying ``prompt`` when a turn is running.

    Returns ``(level, body)`` when there is something to report: the caller
    should render it and NOT run a turn. ``level`` is ``"info"`` for the
    armed ack (the hook fires at the running turn's DONE point and the
    shipped DONE-continue machinery re-drives the turn with the prompt) and
    ``"error"`` for an arming failure. Returns ``None`` when the thread is
    idle, so the caller runs the prompt as a normal turn (the degenerate
    case).

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
            "error",
            "Lifecycle hooks are disabled for this thread, so `/done` "
            "cannot arm a follow-up. Enable hooks (`hooks_enabled`) or resend "
            "the prompt once the current turn finishes.",
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
        return ("error", f"Could not arm the follow-up: {exc}")
    if hook is None:
        return ("error", "Hook limit reached; the follow-up was not armed.")
    if not locks.is_thread_busy(thread_id):
        # The turn ended while we were arming. Claim the hook back by
        # deleting it: success = it never fired (run the prompt now);
        # failure = the DONE fire consumed it (single_use removal), so the
        # prompt already ran at turn end.
        if agent.hook_manager.delete_hook(user_id, hook.id):
            return None
    return (
        "info",
        "Follow-up armed: your prompt will run when the current turn "
        f"finishes (one-shot hook `{hook.id}`, removed after firing).",
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
        with thread_admission_guard(thread_id) as thread_epoch:
            if thread_epoch < 0:
                require_thread_access_fn(user, thread_id, claim=False)
                raise HTTPException(status_code=409, detail=THREAD_DELETED_MESSAGE)
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
                with thread_admission_guard(mention_resolution.thread_id) as thread_epoch:
                    if thread_epoch < 0:
                        require_thread_access_fn(user, mention_resolution.thread_id, claim=False)
                        raise HTTPException(status_code=409, detail=THREAD_DELETED_MESSAGE)
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
        # actually typed. The /orchestrate intercept below may rewrite
        # `message` (for the agent's first turn) while keeping
        # `display_message` pointing at the user's original input.
        display_message = message

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
                    "Usage: `/quick <prompt>`: runs a one-off query "
                    "in a fresh, clean-context thread and shows the answer "
                    "here without leaving the current thread.",
                    "error",
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
                    f"Could not start a quick thread: {exc}",
                    "error",
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
            thread_epoch = get_thread_epoch(thread_id)

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
                return _slash_sse_response(_DONE_USAGE, "error", thread_id)
            done_ack = _try_arm_done_hook(agent, thread_id, user_id, done_prompt)
            if done_ack is not None:
                ack_level, ack_body = done_ack
                return _slash_sse_response(ack_body, ack_level, thread_id)
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
                    "A turn is already running on this thread; "
                    "there is nothing to resume.",
                    "error",
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
                # The level is authored where the outcome is known (#144
                # review fix): a deactivation NO-OP arrives as info, so it
                # never claims **Done.**.
                return _slash_sse_response(
                    prepared.message, prepared.level, thread_id
                )
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
                    "Usage: `/orchestrate <objective>` to start, "
                    "`/orchestrate status` to inspect, "
                    "`/orchestrate clear` to exit.",
                    "error",
                    thread_id,
                )

            if rest_lower == "clear":
                clear_level, msg_text = deactivate_skill_kit(
                    agent=agent,
                    thread_id=thread_id,
                    user_id=user_id,
                    skill_name="orchestrate",
                )
                return _slash_sse_response(msg_text, clear_level, thread_id)

            if rest_lower == "status":
                tc = agent.thread_config_manager.get_config(thread_id)
                is_active = bool(
                    tc and "orchestrate" in (tc.enabled_skills or [])
                )
                msg_text = (
                    "Orchestrate mode is ACTIVE on this thread. "
                    "Use `/orchestrate clear` to exit."
                    if is_active
                    else "Orchestrate mode is not active. "
                    "Use `/orchestrate <objective>` to start."
                )
                return _slash_sse_response(msg_text, "info", thread_id)

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
                return _slash_sse_response(activation_msg, "error", thread_id)

            message = (
                f"[Orchestrator mode activated.] Goal to orchestrate: {rest}\n\n"
                "Load the orchestrate skill kit (call "
                '`Skill(name="orchestrate", ttl="24h")`) to read the playbook, decompose '
                "the goal into tasks via `nym_todo`, present the task list to "
                "the user for approval, then begin delegating to forked workers."
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

        # Transfer the permit to a task before handing any response to ASGI.
        # All request setup failures before that transfer return the permit.
        try:
            client_id = http_request.headers.get("x-nymeria-client-id", "")
            if not request.is_self_invoke and not resume_halted_turn:
                publish_sync_event_fn(
                    event_type="message_added", thread_id=thread_id, user_id=user_id,
                    data={"role": "user", "content": display_message}, origin_client_id=client_id,
                )
            source, source_id, source_label = _agent_prompt_source(request)
            origin = _effective_platform_origin(request, _privileged_platform_caller(user))
            dispatch_fields = _dispatch_stream_fields(dispatched_target, original_thread_id)
            trigger_fields = {
                key: value for key, value in {
                    "trigger_id": request.trigger_id, "trigger_name": request.trigger_name,
                }.items() if value
            }
            spec = TurnSpec(
                message=message, thread_id=thread_id, user_id=user_id,
                attachments=_attachment_dicts(request), images=_legacy_image_dicts(request),
                force_unsupported_attachments=request.force_unsupported_attachments,
                is_self_invoke=request.is_self_invoke, trigger_override=request.trigger_override,
                source=source, source_id=source_id, source_label=source_label,
                resume_halted_turn=resume_halted_turn, thread_epoch=thread_epoch,
                stream_thread_id=original_thread_id if dispatched_target else thread_id,
                dispatch_fields=dispatch_fields,
                quick_footer=_quick_continue_footer(thread_id) if is_quick else None,
                client_id=client_id, auto_title=not request.is_self_invoke, refresh_activity=True,
                holder_label=request.trigger_name or request.source_label or request.trigger_override or request.source,
                platform_origin={
                    "platform": origin.platform, "channel_id": origin.channel_id,
                    "message_id": origin.message_id, "kind": origin.kind,
                } if origin is not None else None,
                autonomous_task_id=(
                    f"{request.trigger_override or 'autonomous'}-{thread_id}"
                    if request.is_self_invoke and request.publish_autonomous_events else None
                ),
                trigger_fields=trigger_fields, settings=get_settings_fn(),
                publish_sync_event=publish_sync_event_fn,
                publish_autonomous_event=publish_autonomous_event_fn,
                publish_agent_stream_chunk=publish_agent_stream_chunk_fn,
                create_autonomous_notification=create_autonomous_notification_fn,
                should_notify_autonomous=should_notify_autonomous_fn,
            )
            sink = AsyncTurnSink()
            start_turn(agent, spec, sink, turn_slot)
        except BaseException:
            if turn_slot is not None:
                turn_slot.release()
            raise

        async def event_generator():
            try:
                if dispatched_target is not None:
                    event = {
                        "type": "dispatched", "thread_id": original_thread_id,
                        "target_thread_id": dispatched_target.thread_id,
                        "title": dispatched_target.title, "matched_ref": dispatched_target.reference,
                        **dispatch_fields,
                    }
                    yield f"data: {json.dumps(event)}\n\n"
                while (event := await sink.get()) is not None:
                    if event.get("type") != HOLDER_STARTED:
                        yield f"data: {json.dumps(event)}\n\n"
                        continue
                    # Pin this producer's buffer, even if a later holder has
                    # replaced the registry entry before we reach the marker.
                    buffer = event["buffer"]
                    try:
                        async for _, kind, payload in buffer.stream_entries(0):
                            if kind == "turn_started" and request.is_self_invoke:
                                continue
                            yield f"data: {payload}\n\n"
                    except TurnReplayGapError:
                        gap = {"type": "turn_replay_gap", "thread_id": spec.stream_thread_id,
                               "turn_id": buffer.turn_id}
                        yield f"data: {json.dumps(gap)}\n\n"
                    return
            finally:
                sink.detach()

        return _TurnStreamingResponse(with_sse_keepalive(event_generator()), sink)

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
        with thread_admission_guard(thread_id) as thread_epoch:
            if thread_epoch < 0:
                require_thread_access_fn(user, thread_id, claim=False)
                raise HTTPException(status_code=409, detail=THREAD_DELETED_MESSAGE)
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
                with thread_admission_guard(mention_resolution.thread_id) as thread_epoch:
                    if thread_epoch < 0:
                        require_thread_access_fn(user, mention_resolution.thread_id, claim=False)
                        raise HTTPException(status_code=409, detail=THREAD_DELETED_MESSAGE)
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
                return _slash_sync_response(
                    "Usage: `/quick <prompt>`: runs a one-off "
                    "query in a fresh, clean-context thread.",
                    "error",
                    thread_id,
                )
            try:
                thread_id = _create_quick_thread(
                    agent, user_id, thread_id, publish_sync_event_fn
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to create /quick thread")
                return _slash_sync_response(
                    f"Could not start a quick thread: {exc}", "error", thread_id
                )
            message = quick_prompt
            is_quick = True
            thread_epoch = get_thread_epoch(thread_id)

        # /done <prompt>: sync parity with the streaming intercept above.
        done_tokens = msg_stripped.split(maxsplit=1)
        if not request.is_self_invoke and done_tokens and done_tokens[0] == "/done":
            raw_parts = message.strip().split(maxsplit=1)
            done_prompt = raw_parts[1].strip() if len(raw_parts) > 1 else ""
            if not done_prompt:
                return _slash_sync_response(_DONE_USAGE, "error", thread_id)
            done_ack = _try_arm_done_hook(agent, thread_id, user_id, done_prompt)
            if done_ack is not None:
                ack_level, ack_body = done_ack
                return _slash_sync_response(ack_body, ack_level, thread_id)
            message = done_prompt
            msg_stripped = message.strip().lower()

        # /resume: sync parity with the streaming intercept above (see there).
        resume_halted_turn = False
        if not request.is_self_invoke and msg_stripped == "/resume":
            if agent._thread_locks.is_thread_busy(thread_id):
                return _slash_sync_response(
                    "A turn is already running on this thread; "
                    "there is nothing to resume.",
                    "error",
                    thread_id,
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
                # Same as the streaming intercept: the helper authors the
                # level, so a deactivation no-op stays info (#144 review fix).
                return _slash_sync_response(
                    prepared.message, prepared.level, thread_id
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

        # Both the origin stamp and the turn run under one guard so a failure
        # on either path releases the admission slot (no leak to turn end).
        try:
            # Chat-platform provenance (mirrors the streaming route): record
            # the origin for the react tool and enrich reaction-triggered
            # prompts. AFTER the admission gate, so a shed request never
            # touches the origin registry.
            message = _apply_platform_origin(
                agent,
                request,
                thread_id,
                user_id,
                message,
                privileged=_privileged_platform_caller(user),
            )

            # The whole synchronous turn (LLM round trips, tools, checkpoint
            # writes) runs off the event loop; running it inline would freeze
            # every SSE stream and probe in the process for the turn's duration.
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
                _thread_epoch=thread_epoch,
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
            suppress_reply=_turn_reply_suppressed(
                request, thread_id, privileged=_privileged_platform_caller(user)
            ),
        )

    return router
