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
    CHROME_ONLY_EVENT_TYPES,
    add_chrome_subscriber,
    is_chrome_client_id,
    remove_chrome_subscriber,
)
from ...core.event_bus import (
    AutonomousEvent,
    EventBus,
    autonomous_event_to_payload,
    get_event_bus,
    should_log_stream_event_sample,
)
from ..sse import SSE_KEEPALIVE_FRAME, SSE_RESPONSE_HEADERS

logger = logging.getLogger(__name__)

# Idle subscribers re-poll the synchronous event queue on this cadence. It bounds
# event-delivery latency and the client-disconnect check interval, and is kept
# deliberately separate from the keepalive cadence below. (Switching the queue to
# an awaitable would drop the poll entirely but touches the shared event bus, out
# of scope here.)
_QUEUE_POLL_INTERVAL_SECONDS = 1.0

# A `: keepalive` SSE comment frame is emitted after this many seconds of
# continuous silence to hold the connection open. Decoupling it from the poll
# cadence means an idle stream no longer emits a comment frame every second. The
# value sits well under the desktop/mobile autonomous-store idle-reconnect timer
# (30s) and far under the proxy idle floor (Cloudflare closes at ~100s). It is
# intentionally not the chat stream's 25s constant, which would leave too thin a
# margin under that 30s client timer.
_KEEPALIVE_INTERVAL_SECONDS = 10.0

# Emit one keepalive every Nth consecutive idle poll (always at least one).
# Derived once at import from the two interval constants above, so a test or
# reconfig that changes a single interval should patch this value directly.
_KEEPALIVE_POLL_INTERVAL = max(
    1, round(_KEEPALIVE_INTERVAL_SECONDS / _QUEUE_POLL_INTERVAL_SECONDS)
)

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
    # Flatten via the shared helper so the SSE wire shape stays identical to the
    # CLI's in-process transport, which consumes the same dict directly.
    return json.dumps(autonomous_event_to_payload(event))


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
    filtered_kind_counts: dict[str, int] = {}
    # Loop-invariant: a subscriber's kind is fixed for the life of its stream.
    # Mirrors the registration gate below, so what gets served the extension's
    # events and what registers AS an extension cannot drift apart. The kind
    # signal is the prefix test that registry already trusts, so gating
    # delivery on it adds no new trust.
    is_extension_stream = is_chrome_client_id(client_id) and not firehose

    def bump(counter: dict[str, int], key: str) -> int:
        counter[key] = counter.get(key, 0) + 1
        return counter[key]

    try:
        idle_polls = 0
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

                if event.event_type in CHROME_ONLY_EVENT_TYPES and not is_extension_stream:
                    filtered_count = bump(filtered_kind_counts, event.event_type)
                    if should_log_stream_event_sample(event.event_type, filtered_count):
                        logger.info(
                            "[AUTONOMOUS SSE] filter subscriber=%s reason=not_chrome_client "
                            "type=%s count=%d client_id=%s firehose=%s thread=%s",
                            subscriber_id[:8],
                            event.event_type,
                            filtered_count,
                            client_id[:8] if client_id else "none",
                            firehose,
                            event.thread_id,
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
                idle_polls = 0

            except Empty:
                # Emit a keepalive comment frame only after a stretch of silence
                # (every `_KEEPALIVE_POLL_INTERVAL` idle polls), not on every
                # poll, so an idle stream is not flooded with one frame per
                # second. `: keepalive` is an SSE comment that every consumer
                # skips; the sleep bounds event latency and the disconnect check.
                idle_polls += 1
                if idle_polls % _KEEPALIVE_POLL_INTERVAL == 0:
                    yield SSE_KEEPALIVE_FRAME
                await asyncio.sleep(_QUEUE_POLL_INTERVAL_SECONDS)

    finally:
        event_bus.unsubscribe(subscriber_id)
        remove_chrome_subscriber(subscriber_id)
        logger.info(
            "[AUTONOMOUS SSE] subscriber_cleanup subscriber=%s user=%s "
            "received=%s yielded=%s filtered_user=%s filtered_origin=%s "
            "filtered_kind=%s",
            subscriber_id[:8],
            user_id,
            received_counts,
            yielded_counts,
            filtered_user_counts,
            filtered_origin_counts,
            filtered_kind_counts,
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
        client_version: Optional[str] = Query(
            default=None,
            description=(
                "The subscribing client's build version (the browser extension "
                "announces its manifest version so chrome_reload_extension can "
                "report which build reconnected)"
            ),
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
            add_chrome_subscriber(
                user_id=stream_user_id,
                subscriber_id=subscriber_id,
                version=client_version,
            )
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
            headers=SSE_RESPONSE_HEADERS,
        )

    return router
