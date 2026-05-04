"""Interactive chat SSE and sync routes."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...core.accounts import AuthenticatedUser
from ...core.event_bus import (
    publish_agent_stream_chunk as default_publish_agent_stream_chunk,
    publish_autonomous_event as default_publish_autonomous_event,
    publish_sync_event as default_publish_sync_event,
)
from ...core.notification_dispatch import (
    create_autonomous_notification as default_create_autonomous_notification,
    should_notify_autonomous as default_should_notify_autonomous,
)
from ..schemas.chat import ChatRequest, ChatResponse

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
        # Ignore client-claimed user_id in the body; derive from auth instead.
        user_id = user.id
        require_thread_access_fn(user, thread_id)

        # Handle slash commands (e.g., /compact)
        msg_stripped = request.message.strip().lower()
        logger.info(
            "[CHAT] Received message: '%s' stripped: '%s' is_compact: %s",
            request.message,
            msg_stripped,
            msg_stripped == "/compact",
        )
        if msg_stripped == "/compact":

            async def compact_command_response():
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "compacting",
                            "message": "Compacting context...",
                            "thread_id": thread_id,
                        }
                    )
                    + "\n\n"
                )
                # Perform compaction (this may take a few seconds)
                result = await agent.compact_now(thread_id, user_id)
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
                                "thread_id": thread_id,
                            }
                        )
                        + "\n\n"
                    )
                    msg = f"✓ Conversation compacted. {messages_removed} messages summarized."
                else:
                    msg = f"Could not compact: {result.get('reason', 'unknown error')}"
                yield f"data: {json.dumps({'type': 'response', 'content': msg})}\n\n"
                # Include context_stats and model in done event
                context_stats = agent.get_context_stats(thread_id)
                effective_model = (
                    agent._get_llm_config_for_thread(thread_id).model
                    or agent.settings.llm_model
                )
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "type": "done",
                            "thread_id": thread_id,
                            "context_stats": context_stats,
                            "model": effective_model,
                        }
                    )
                    + "\n\n"
                )

            return StreamingResponse(
                compact_command_response(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

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
                data={"role": "user", "content": request.message},
                origin_client_id=client_id,
            )

        # Autonomous task bookends: publish task_started/task_completed to Redis so
        # /autonomous/stream subscribers see watchdog/ticker activity live. Matches
        # the event pattern the former in-process watchdog emitted.
        autonomous_task_id = (
            f"{request.trigger_override or 'autonomous'}-{thread_id}"
            if request.is_self_invoke
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

                client_disconnected = False
                async for chunk in agent.astream(
                    request.message,
                    thread_id=thread_id,
                    user_id=user_id,
                    attachments=attachments,
                    images=images,
                    force_unsupported_attachments=request.force_unsupported_attachments,
                    _is_self_invoke=request.is_self_invoke,
                    _trigger_override=request.trigger_override,
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

                    # Publish task_started on the first non-queued chunk so the
                    # frontend handoff happens only after the thread lock is acquired.
                    if (
                        autonomous_task_id
                        and not autonomous_started
                        and chunk.get("type") != "queued"
                    ):
                        publish_autonomous_event_fn(
                            event_type="task_started",
                            thread_id=thread_id,
                            user_id=user_id,
                            task_id=autonomous_task_id,
                            data={
                                "prompt": request.message,
                                "source": request.trigger_override or "autonomous",
                                **_trigger_fields(),
                            },
                        )
                        autonomous_started = True

                    event_data = json.dumps({**chunk, "thread_id": thread_id})
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
                        "thread_id": thread_id,
                        "context_stats": context_stats,
                        "model": agent._get_llm_config_for_thread(thread_id).model
                        or agent.settings.llm_model,
                    }

                    # Auto-title the thread from the user's message if untitled
                    try:
                        new_title = agent.thread_metadata_manager.auto_title(
                            user_id, thread_id, request.message
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
                        "thread_id": thread_id,
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

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
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
        # Ignore client-claimed user_id in the body; derive from auth instead.
        user_id = user.id
        require_thread_access_fn(user, thread_id)

        response = agent.chat(
            request.message,
            thread_id=thread_id,
            user_id=user_id,
            _is_self_invoke=request.is_self_invoke,
            _trigger_override=request.trigger_override,
        )
        tool_call_count = getattr(agent, "_last_chat_tool_calls", 0)
        return ChatResponse(
            response=response,
            thread_id=thread_id,
            tool_call_count=tool_call_count,
        )

    return router
