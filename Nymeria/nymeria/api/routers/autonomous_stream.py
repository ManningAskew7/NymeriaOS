"""Autonomous event stream routes."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from queue import Empty, Queue
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ...config import Settings
from ...core.chrome_subscribers import (
    add_chrome_subscriber,
    is_chrome_client_id,
    remove_chrome_subscriber,
)
from ...core.event_bus import (
    AutonomousEvent,
    EventBus,
    get_event_bus,
    should_log_stream_event_sample,
)

logger = logging.getLogger(__name__)


def _extract_bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def _resolve_stream_auth(
    *,
    get_agent_fn: Callable[[], Any],
    requested_user_id: str,
    presented_token: Optional[str],
    x_nymeria_act_as: Optional[str],
    request: Optional[Request] = None,
    auth_failure_handler: Optional[Callable[[Request, HTTPException], Any]] = None,
) -> tuple[str, bool]:
    """Resolve the effective stream user and firehose mode from a raw token.

    Authentication failures are routed through ``auth_failure_handler`` (the
    shared per-IP auth-failure rate limiter) when supplied, so this endpoint is
    subject to the same brute-force throttle as the rest of the API.
    """
    authorized = False
    firehose = False
    user_id = requested_user_id

    if presented_token:
        try:
            repo_user = get_agent_fn().accounts_repo.verify_token(presented_token)
            if repo_user is not None:
                authorized = True
                # Admin-role callers may use X-Nymeria-Act-As to stream another
                # user's events, or "*" for the full bot thin-client firehose.
                if x_nymeria_act_as:
                    if repo_user.role != "admin":
                        raise HTTPException(status_code=403, detail="Act-As requires admin")
                    if x_nymeria_act_as == "*":
                        firehose = True
                        user_id = "*"
                    else:
                        user_id = x_nymeria_act_as
                else:
                    # The account token is authoritative. The query user_id is
                    # only a compatibility hint for old clients.
                    user_id = repo_user.id
        except HTTPException:
            raise
        except Exception:
            authorized = False

    if not authorized:
        failure = HTTPException(status_code=401, detail="Invalid API key")
        if auth_failure_handler is not None and request is not None:
            auth_failure_handler(request, failure)
        raise failure

    return user_id, firehose


def _event_to_sse_payload(event: AutonomousEvent) -> str:
    # Strip internal fields and reserved keys. event.event_type is canonical so
    # interactive sync events keep their interactive_ prefix if present.
    payload = {
        k: v
        for k, v in event.data.items()
        if not k.startswith("_") and k not in ("type", "thread_id", "task_id", "timestamp")
    }
    event_data = {
        "type": event.event_type,
        "thread_id": event.thread_id,
        "task_id": event.task_id,
        "timestamp": event.timestamp.isoformat(),
        **payload,
    }
    return json.dumps(event_data)


async def _generate_autonomous_sse_events(
    *,
    request: Request,
    event_bus: EventBus,
    queue: Queue,
    subscriber_id: str,
    user_id: str,
    firehose: bool,
    client_id: Optional[str],
):
    """Generate SSE events from the event bus."""
    received_counts: dict[str, int] = {}
    yielded_counts: dict[str, int] = {}
    filtered_user_counts: dict[str, int] = {}
    filtered_origin_counts: dict[str, int] = {}

    def bump(counter: dict[str, int], key: str) -> int:
        counter[key] = counter.get(key, 0) + 1
        return counter[key]

    try:
        while True:
            if await request.is_disconnected():
                logger.info(
                    "[AUTONOMOUS SSE] subscriber_disconnected subscriber=%s user=%s "
                    "reason=request_disconnected received=%s yielded=%s",
                    subscriber_id[:8],
                    user_id,
                    received_counts,
                    yielded_counts,
                )
                break

            try:
                event: AutonomousEvent = queue.get_nowait()
                received_count = bump(received_counts, event.event_type)
                if should_log_stream_event_sample(event.event_type, received_count):
                    logger.info(
                        "[AUTONOMOUS SSE] queue_receive subscriber=%s type=%s "
                        "count=%d queue_size=%d stream_user=%s event_user=%s "
                        "thread=%s task=%s",
                        subscriber_id[:8],
                        event.event_type,
                        received_count,
                        queue.qsize(),
                        user_id,
                        event.user_id,
                        event.thread_id,
                        event.task_id,
                    )

                # Filter by user_id unless the caller requested the firehose
                # (admin + X-Nymeria-Act-As: *).
                if not firehose and event.user_id != user_id:
                    filtered_count = bump(filtered_user_counts, event.event_type)
                    if should_log_stream_event_sample(event.event_type, filtered_count):
                        logger.info(
                            "[AUTONOMOUS SSE] filter subscriber=%s reason=user_mismatch "
                            "type=%s count=%d stream_user=%s event_user=%s "
                            "thread=%s task=%s",
                            subscriber_id[:8],
                            event.event_type,
                            filtered_count,
                            user_id,
                            event.user_id,
                            event.thread_id,
                            event.task_id,
                        )
                    continue

                origin = event.data.get("_origin_client_id")
                if origin and client_id and origin == client_id:
                    filtered_count = bump(filtered_origin_counts, event.event_type)
                    if should_log_stream_event_sample(event.event_type, filtered_count):
                        logger.info(
                            "[AUTONOMOUS SSE] filter subscriber=%s reason=origin_client "
                            "type=%s count=%d client_id=%s thread=%s task=%s",
                            subscriber_id[:8],
                            event.event_type,
                            filtered_count,
                            client_id[:8],
                            event.thread_id,
                            event.task_id,
                        )
                    continue

                serialized = _event_to_sse_payload(event)
                yielded_count = bump(yielded_counts, event.event_type)
                if should_log_stream_event_sample(event.event_type, yielded_count):
                    logger.info(
                        "[AUTONOMOUS SSE] yield subscriber=%s type=%s count=%d "
                        "bytes=%d thread=%s task=%s",
                        subscriber_id[:8],
                        event.event_type,
                        yielded_count,
                        len(serialized),
                        event.thread_id,
                        event.task_id,
                    )
                yield f"data: {serialized}\n\n"

            except Empty:
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)

    finally:
        event_bus.unsubscribe(subscriber_id)
        remove_chrome_subscriber(subscriber_id)
        logger.info(
            "[AUTONOMOUS SSE] subscriber_cleanup subscriber=%s user=%s "
            "received=%s yielded=%s filtered_user=%s filtered_origin=%s",
            subscriber_id[:8],
            user_id,
            received_counts,
            yielded_counts,
            filtered_user_counts,
            filtered_origin_counts,
        )


def create_autonomous_stream_router(
    get_agent_fn: Callable[[], Any],
    get_settings_fn: Callable[[], Any],
    auth_failure_handler: Optional[Callable[[Request, HTTPException], Any]] = None,
) -> APIRouter:
    """Create the autonomous SSE router with app dependencies injected.

    ``auth_failure_handler`` is the shared per-IP auth-failure rate limiter; if
    omitted, invalid tokens still 401 but are not throttled.
    """
    router = APIRouter(tags=["Autonomous"])

    @router.get("/autonomous/stream")
    async def stream_autonomous_events(
        request: Request,
        user_id: str = Query(default="default", description="User ID to filter events"),
        api_key: Optional[str] = Query(
            default=None,
            description=(
                "Legacy API key fallback for clients that cannot send Authorization headers."
            ),
        ),
        client_id: Optional[str] = Query(
            default=None,
            description="Client ID for origin filtering (prevents seeing own sync events)",
        ),
        authorization: Optional[str] = Header(None),
        x_nymeria_act_as: Optional[str] = Header(None),
        _settings: Settings = Depends(get_settings_fn),
    ):
        """
        Stream autonomous task events and cross-client sync events via Server-Sent Events.

        Emits events when Nymeria executes scheduled tasks autonomously, when other
        clients send interactive chat messages, or when thread metadata changes.
        """
        presented = _extract_bearer_token(authorization)
        if presented is None and api_key:
            # Deprecated: tokens in the query string leak into proxy/access
            # logs and browser history. Prefer the Authorization header (or a
            # fetch-based SSE client). Kept for legacy EventSource clients.
            logger.warning(
                "[AUTONOMOUS SSE] deprecated api_key query parameter used; "
                "migrate to the Authorization header to avoid token log leakage"
            )
            presented = api_key

        stream_user_id, firehose = _resolve_stream_auth(
            get_agent_fn=get_agent_fn,
            requested_user_id=user_id,
            presented_token=presented,
            x_nymeria_act_as=x_nymeria_act_as,
            request=request,
            auth_failure_handler=auth_failure_handler,
        )

        subscriber_id = str(uuid.uuid4())
        event_bus = get_event_bus()
        queue = event_bus.subscribe(subscriber_id)
        if is_chrome_client_id(client_id) and not firehose:
            add_chrome_subscriber(user_id=stream_user_id, subscriber_id=subscriber_id)
        logger.info(
            "[AUTONOMOUS SSE] subscriber_connect subscriber=%s user=%s firehose=%s "
            "client_id=%s local_subscribers=%d chrome=%s",
            subscriber_id[:8],
            stream_user_id,
            firehose,
            client_id[:8] if client_id else "none",
            event_bus.get_subscriber_count(),
            is_chrome_client_id(client_id),
        )

        return StreamingResponse(
            _generate_autonomous_sse_events(
                request=request,
                event_bus=event_bus,
                queue=queue,
                subscriber_id=subscriber_id,
                user_id=stream_user_id,
                firehose=firehose,
                client_id=client_id,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
