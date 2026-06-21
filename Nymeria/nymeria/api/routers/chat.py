"""Interactive chat SSE and sync routes."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...core.accounts import AuthenticatedUser
from ...core.agent_compaction import COMPACTING_MESSAGE
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
from ..schemas.chat import ChatRequest, ChatResponse
from ..sse import SSE_RESPONSE_HEADERS, with_sse_keepalive
from ..thread_config_helpers import effective_provider_model

logger = logging.getLogger(__name__)


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
    if source not in {"trigger", "ticker", "watchdog", "callable", "mcp"}:
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

        # Read client ID from header for sync event origin filtering
        client_id = http_request.headers.get("x-nymeria-client-id", "")

        # For autonomous/self-invoke calls (e.g. watchdog worker), the "user message"
        # isn't from a real user -- skip message_added so it doesn't appear in clients
        # as a user-authored message. Frontend subscribes to /autonomous/stream for
        # autonomous task events instead.
        if not request.is_self_invoke:
            publish_sync_event_fn(
                event_type="message_added",
                thread_id=thread_id,
                user_id=user_id,
                data={"role": "user", "content": display_message},
                origin_client_id=client_id,
            )

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
                ):
                    # If client disconnected, stop yielding SSE events but keep
                    # consuming the generator so the agent finishes its work.
                    # Results are saved to thread history and available on reconnect.
                    # Explicit cancellation uses POST /threads/{id}/stop instead.
                    if await http_request.is_disconnected():
                        if not client_disconnected:
                            client_disconnected = True
                            logger.info(
                                "Client disconnected for thread %s, agent will continue in background",
                                thread_id,
                            )
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

                    stream_thread_id = (
                        original_thread_id if dispatched_target is not None else thread_id
                    )
                    event_data = json.dumps(
                        {
                            **chunk,
                            "thread_id": stream_thread_id,
                            **_dispatch_stream_fields(
                                dispatched_target,
                                original_thread_id,
                            ),
                        }
                    )
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

                # Only send done event if client is still connected
                if not client_disconnected and not await http_request.is_disconnected():
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
                    try:
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

                    yield f"data: {json.dumps(done_data)}\n\n"

            except Exception as e:
                logger.error("Stream error: %s", e, exc_info=True)
                error_data = json.dumps(
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
        return StreamingResponse(
            with_sse_keepalive(event_generator()),
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
        response = agent.chat(
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
        )
        tool_call_count = getattr(agent, "_last_chat_tool_calls", 0)
        return ChatResponse(
            response=response,
            thread_id=thread_id,
            tool_call_count=tool_call_count,
        )

    return router
