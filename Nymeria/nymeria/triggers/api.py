"""FastAPI REST API trigger with SSE streaming for Nymeria."""

import json
import logging
import math
import os
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..core.agent import NymeriaAgent
from ..core.accounts import (
    AuthenticatedUser,
)
from ..core.event_bus import (
    publish_agent_stream_chunk,
    publish_autonomous_event,
    publish_sync_event,
)
from ..core.notification_dispatch import (
    create_autonomous_notification as _dispatch_autonomous_notification,
    should_notify_autonomous as _should_notify_autonomous,
)
from ..core.rate_limit import SlidingWindowRateLimiter
from ..core import thread_classification as _thread_classification
from ..tools import ALL_TOOLS
from ..api.routers.accounts import create_accounts_router
from ..api.routers.autonomous_stream import create_autonomous_stream_router
from ..api.routers.chat_apps import create_chat_apps_router
from ..api.routers.custom_tools import create_custom_tools_router
from ..api.routers.activity import create_activity_router
from ..api.routers.agent_threads import create_agent_threads_router
from ..api.routers.devices import create_devices_router
from ..api.routers.memory import create_memory_router
from ..api.routers.mcp_servers import create_mcp_servers_router
from ..api.routers.rag import create_rag_router
from ..api.routers.settings import create_settings_router
from ..api.routers.skills import create_skills_router
from ..api.routers.system import create_system_router
from ..api.routers.thread_config import create_thread_config_router
from ..api.routers.thread_operations import create_thread_operations_router
from ..api.routers.threads import create_threads_router
from ..api.routers.todos import create_todos_router
from ..api.routers.tools import create_tools_router
from ..api.routers.unified_tools import create_unified_tools_router
from ..api.routers.user_tools import create_user_tools_router
from ..api.routers.voice import create_voice_router
from ..api.routers.workspace import create_workspace_router
from ..api.schemas.thread_operations import FileData

logger = logging.getLogger(__name__)

_classify_thread_platform_from_id = _thread_classification.classify_platform
_is_native_platform_thread = _thread_classification.is_native_platform_thread
_is_shared_channel_thread = _thread_classification.is_shared_channel

# Global agent instance (initialized on startup)
_agent: Optional[NymeriaAgent] = None

_BOT_ADMIN_ENDPOINT_RATE_LIMIT = 120
_BOT_ADMIN_ENDPOINT_RATE_WINDOW_SECONDS = 60.0
_bot_admin_endpoint_rate_limiter = SlidingWindowRateLimiter()


def get_agent() -> NymeriaAgent:
    """Get the global agent instance."""
    if _agent is None:
        raise RuntimeError("Agent not initialized. Call create_api_app() first.")
    return _agent


# ============================================================================
# Request/Response Models
# ============================================================================


class ImageData(BaseModel):
    """Image attachment data for multimodal messages (legacy, use FileData)."""

    data_url: str = Field(..., description="Base64 data URL (data:image/...;base64,...)")
    mime_type: str = Field(..., description="MIME type (image/jpeg, image/png, etc.)")


class ChatRequest(BaseModel):
    """Request model for chat endpoint."""

    message: str = Field(..., min_length=1, description="User message")
    thread_id: Optional[str] = Field(
        default=None, description="Conversation thread ID (generated if not provided)"
    )
    user_id: str = Field(
        default="default",
        description="User ID for profile and memory access (defaults to 'default')"
    )
    attachments: Optional[List[FileData]] = Field(
        default=None, description="Optional list of file attachments for multimodal models"
    )
    images: Optional[List[ImageData]] = Field(
        default=None, description="Deprecated: use attachments instead"
    )
    force_unsupported_attachments: bool = Field(
        default=False,
        description="Allow send even when attachment compatibility checks fail",
    )
    is_self_invoke: bool = Field(
        default=False,
        description=(
            "Mark this invocation as autonomous/internal. Trusted in-cluster "
            "services (e.g. watchdog worker) use this to route requests through "
            "the autonomous prompt path and mark the message as internal. "
            "Requires API key auth like all /chat requests."
        ),
    )
    trigger_override: Optional[str] = Field(
        default=None,
        description="Trigger label for autonomous invocations (e.g. 'watchdog')",
    )
    trigger_id: Optional[str] = Field(
        default=None,
        description="Trigger ID when trigger_override=='trigger' -- surfaced in autonomous events for frontend classification.",
    )
    trigger_name: Optional[str] = Field(
        default=None,
        description="Trigger name when trigger_override=='trigger' -- surfaced in autonomous events for frontend classification.",
    )


class ChatResponse(BaseModel):
    """Response model for non-streaming chat."""

    response: str = Field(..., description="Agent response")
    thread_id: str = Field(..., description="Conversation thread ID")
    tool_call_count: int = Field(default=0, description="Number of tool calls made in this turn")


# Sub-Agent Models




# ============================================================================
# Authentication
# ============================================================================


async def verify_api_key(
    authorization: Optional[str] = Header(None),
    x_nymeria_act_as: Optional[str] = Header(None),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    """
    Authenticate a request and return the user it resolves to.

    Accepts a per-user account token (``nym_...``) issued via
    ``python run.py users add``. ``X-Nymeria-Act-As: <user_id>`` is honored
    only for admin-role callers and returns the target user; non-admin use
    → 403, unknown target → 404.

    Routes bind ``user: AuthenticatedUser = Depends(verify_api_key)`` and
    derive ``user_id = user.id`` rather than trusting client-claimed user
    IDs in request body or query params.
    """
    return await resolve_authenticated_user(
        authorization=authorization,
        x_nymeria_act_as=x_nymeria_act_as,
        settings=settings,
    )


# Alias so new routes can declare their intent clearly.
require_user = verify_api_key


def _extract_bearer_token(authorization: Optional[str]) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401, detail="Invalid Authorization header format. Use: Bearer <api_key>"
        )
    return parts[1]


async def resolve_authenticated_user(
    authorization: Optional[str] = Header(None),
    x_nymeria_act_as: Optional[str] = Header(None),
    settings: Settings = Depends(get_settings),
):
    """
    Resolve the caller to an :class:`AuthenticatedUser`.

    Only per-user account tokens (``nym_...``) are accepted — the legacy
    ``NYMERIA_API_KEY`` shared key was retired in Step 3c.

    ``X-Nymeria-Act-As: <user_id>`` is honored only for admin-role callers.
    When present, the dep returns the target user instead of the admin, so
    shared infrastructure (bots, ticker, watchdog) can route traffic per-user
    without holding each user's raw token. Non-admin use → 403. Unknown or
    disabled target → 404.
    """
    presented = _extract_bearer_token(authorization)

    try:
        agent = get_agent()
    except RuntimeError:
        agent = None

    caller = None
    if agent is not None:
        caller = agent.accounts_repo.verify_token(presented)

    if caller is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    if x_nymeria_act_as:
        if caller.role != "admin":
            raise HTTPException(status_code=403, detail="Act-As requires admin")
        if agent is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")
        target = agent.accounts_repo.get_user_by_id(x_nymeria_act_as)
        if target is None or target.disabled:
            raise HTTPException(status_code=404, detail="Act-As target not found")
        return AuthenticatedUser(
            id=target.id,
            email=target.email,
            display_name=target.display_name,
            role=target.role,
            via_act_as=True,
        )

    return caller


async def require_admin_user(
    user=Depends(resolve_authenticated_user),
):
    """Same as :func:`resolve_authenticated_user` but rejects non-admins."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


def _admin_bot_rate_limit_key(request: Request, admin: AuthenticatedUser) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    return f"{admin.id}:{request.method}:{route_path}"


async def require_rate_limited_admin_bot_user(
    request: Request,
    admin: AuthenticatedUser = Depends(require_admin_user),
) -> AuthenticatedUser:
    """Admin-token gate for bot-facing endpoints with a bounded request rate."""
    result = _bot_admin_endpoint_rate_limiter.check(
        _admin_bot_rate_limit_key(request, admin),
        limit=_BOT_ADMIN_ENDPOINT_RATE_LIMIT,
        window_seconds=_BOT_ADMIN_ENDPOINT_RATE_WINDOW_SECONDS,
    )
    if result.allowed:
        return admin

    retry_after = max(1, math.ceil(result.retry_after))
    logger.warning(
        "Rate limited admin bot endpoint %s %s for admin user %s; retry_after=%ss",
        request.method,
        getattr(request.scope.get("route"), "path", request.url.path),
        admin.id,
        retry_after,
    )
    raise HTTPException(
        status_code=429,
        detail="Rate limit exceeded for admin bot endpoint",
        headers={"Retry-After": str(retry_after)},
    )


def _reset_admin_bot_endpoint_rate_limiter_for_tests() -> None:
    _bot_admin_endpoint_rate_limiter.clear()


async def require_admin_caller(
    authorization: Optional[str] = Header(None),
    x_nymeria_act_as: Optional[str] = Header(None),
    settings: Settings = Depends(get_settings),
) -> AuthenticatedUser:
    """
    Like :func:`resolve_authenticated_user` but asserts the *caller's*
    token role is admin (pre-act-as), then returns the effective user.

    Use this on routes where the caller must be admin AND should be able
    to impersonate any user (admin OR non-admin) via ``X-Nymeria-Act-As``.
    Subsequent ownership checks like :func:`_require_thread_access` then
    run against the impersonated target — so an admin acting on behalf of
    user X can mutate X's thread-bound resources without claim-jacking
    threads owned by user Y.

    Contrast with :func:`require_admin_user`, which checks *target* role
    after act-as resolution and therefore rejects ``admin → non-admin``
    impersonation entirely (correct for endpoints whose privilege only
    makes sense as an admin operation, like raw MCP CRUD).
    """
    presented = _extract_bearer_token(authorization)

    try:
        agent = get_agent()
    except RuntimeError:
        agent = None

    caller = None
    if agent is not None:
        caller = agent.accounts_repo.verify_token(presented)

    if caller is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    if caller.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    if x_nymeria_act_as:
        if agent is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")
        target = agent.accounts_repo.get_user_by_id(x_nymeria_act_as)
        if target is None or target.disabled:
            raise HTTPException(status_code=404, detail="Act-As target not found")
        return AuthenticatedUser(
            id=target.id,
            email=target.email,
            display_name=target.display_name,
            role=target.role,
            via_act_as=True,
        )

    return caller


async def _authed_user_id(
    user: AuthenticatedUser = Depends(verify_api_key),
) -> str:
    """
    Resolve the effective user_id for a route from the authenticated user.

    Drop-in replacement for ``user_id: str = Query(default="default")``:
    routes declaring ``user_id: str = Depends(_authed_user_id)`` get the
    authenticated user's id regardless of any client-supplied ``?user_id=``
    query value. Admin callers impersonate via ``X-Nymeria-Act-As`` —
    ``verify_api_key`` returns the target user in that case, so ``user.id``
    already reflects the act-as target.

    FastAPI caches ``verify_api_key`` within a request, so this adds no
    extra auth overhead when a route declares both ``user`` and ``user_id``.
    """
    return user.id


def _require_same_user_or_admin(user: AuthenticatedUser, path_user_id: str) -> None:
    """
    For routes where ``user_id`` is part of the URL path (e.g.
    ``/users/{user_id}/memories``): reject if caller isn't that user. Admins
    reach a target via ``X-Nymeria-Act-As`` which rewrites ``user.id`` to
    the target, so the same check passes for them. Returns 404 (not 403)
    to avoid leaking the existence of other users' resources.
    """
    if path_user_id != user.id:
        raise HTTPException(status_code=404, detail="Not found")


def _require_thread_access(user: AuthenticatedUser, thread_id: str) -> None:
    """
    Enforce that ``user`` owns ``thread_id`` (or is admin).

    Admins bypass ownership for already-owned threads but DO claim on first
    touch for personal threads (UUIDs, ``discord_dm_*``, positive
    ``telegram_<id>``). Without this, a thread an admin opened but never
    sent a message in stayed ownerless — the first non-admin user (or
    bot-routed act-as caller) to touch it would TOFU-claim, silently
    transferring ownership away from the creator.

    Shared-channel threads (Discord guild channels, Telegram groups,
    Twitch chats) are NEVER claimed by admin — those are inherently
    multi-user and per-user ownership rows would just claim-jack to
    whichever admin spoke first. The bot-service token routing them
    via ``X-Nymeria-Act-As`` for individual users still works because
    act-as targets arrive with ``role="user"`` and never enter this
    admin branch (verify_api_key rewrites both id and role).

    Non-admin callers cannot reach shared-channel threads via the API.
    There is no membership table; the only legitimate path is the bot
    service token routing platform users via ``X-Nymeria-Act-As``.
    Returning 404 prevents an authenticated user from guessing a
    ``discord_<g>_<c>`` / ``telegram_-<id>`` / ``twitch_<c>`` ID.

    For 1:1 personal thread IDs, first touch atomically claims the
    thread; subsequent access by any other non-admin resolves to 404
    (intentional — don't leak whether a thread exists under a different
    owner).
    """
    agent = get_agent()
    if user.role == "admin":
        if _is_shared_channel_thread(thread_id):
            # Never per-user-claim a shared channel for an admin. The bot
            # service token routing without act_as lands here too — leaving
            # the thread unowned is correct.
            return
        # Personal thread: claim on first touch so the admin owns what they
        # created. claim_thread is INSERT OR IGNORE — already-owned threads
        # are not disturbed; admin still bypasses the ownership check.
        agent.accounts_repo.claim_thread(thread_id, user.id)
        return
    if _is_shared_channel_thread(thread_id):
        # Shared-channel threads (Discord guild channels, Telegram groups,
        # Twitch chats) bypass ownership ONLY when reached via admin act-as
        # — that's the bot service-token routing path (`Bearer <service>` +
        # `X-Nymeria-Act-As: <linked_user>`) where multiple users legitimately
        # share one thread. Direct non-admin API callers (e.g. a user's
        # desktop) get 404 instead, so they can't guess a `discord_<g>_<c>`
        # ID and read/mutate/delete a channel they don't belong to.
        if user.via_act_as:
            return
        raise HTTPException(status_code=404, detail="Not found")
    owner = agent.accounts_repo.claim_thread(thread_id, user.id)
    if owner != user.id:
        raise HTTPException(status_code=404, detail="Not found")


# ============================================================================
# API Application
# ============================================================================


def create_api_app(agent: Optional[NymeriaAgent] = None) -> FastAPI:
    """
    Create the FastAPI application.

    Args:
        agent: Optional agent instance (creates default if not provided)

    Returns:
        Configured FastAPI application
    """
    global _agent

    settings = get_settings()

    # Initialize agent
    # When Redis is enabled (Docker), a separate worker container runs the ticker.
    # Disable ticker in the API container to prevent duplicate task execution.
    # Initialize Redis event bus if configured (needed to receive worker events via SSE)
    disable_ticker = settings.redis_enabled and bool(settings.redis_url)
    if disable_ticker:
        from ..core.event_bus import create_event_bus, set_event_bus
        event_bus = create_event_bus(settings)
        set_event_bus(event_bus)

    if agent is not None:
        _agent = agent
    else:
        _agent = NymeriaAgent(
            tools=list(ALL_TOOLS),
            enable_ticker=not disable_ticker,
        )

    # Initialize FCM if enabled
    if settings.fcm_enabled and settings.fcm_credentials_json:
        from ..core.fcm import _init_firebase
        _init_firebase(settings.fcm_credentials_json)

    # Create FastAPI app
    app = FastAPI(
        title="Nymeria API",
        description="Personal AI Assistant REST API with SSE streaming",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Add CORS middleware with configurable origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Add trigger system router (event-driven automation)
    from .trigger_api import create_trigger_router
    trigger_router = create_trigger_router(
        get_agent,
        verify_api_key,
        require_thread_access_fn=_require_thread_access,
    )
    app.include_router(trigger_router)
    app.include_router(
        create_accounts_router(
            verify_api_key,
            resolve_authenticated_user,
            require_admin_user,
            get_agent,
            get_settings,
        )
    )
    app.include_router(
        create_chat_apps_router(
            verify_api_key,
            require_rate_limited_admin_bot_user,
            get_agent,
            get_settings,
            _require_thread_access,
            publish_sync_event,
        )
    )
    app.include_router(
        create_system_router(verify_api_key, require_admin_user, get_agent, get_settings)
    )
    app.include_router(create_devices_router(verify_api_key, get_settings))
    app.include_router(create_workspace_router(require_admin_user))
    app.include_router(create_rag_router(verify_api_key, get_agent, _require_same_user_or_admin))
    app.include_router(create_memory_router(verify_api_key, get_agent, _require_same_user_or_admin))
    app.include_router(create_user_tools_router(verify_api_key, get_agent, _require_same_user_or_admin))
    app.include_router(create_skills_router(verify_api_key, _authed_user_id, get_agent, _require_thread_access))
    app.include_router(create_voice_router(verify_api_key, get_agent, get_settings, _require_thread_access))
    app.include_router(create_agent_threads_router(verify_api_key, get_agent, publish_sync_event))
    app.include_router(create_activity_router(verify_api_key, _authed_user_id, get_settings))
    app.include_router(create_todos_router(verify_api_key, _authed_user_id, get_settings))
    app.include_router(create_autonomous_stream_router(get_agent, get_settings))
    app.include_router(create_custom_tools_router(require_admin_user, get_agent))
    app.include_router(
        create_settings_router(
            verify_api_key,
            require_admin_user,
            get_agent,
            get_settings,
        )
    )
    app.include_router(
        create_mcp_servers_router(
            require_admin_user,
            require_admin_caller,
            get_agent,
            get_settings,
            _require_thread_access,
        )
    )
    app.include_router(
        create_thread_config_router(
            verify_api_key,
            _authed_user_id,
            get_agent,
            _require_thread_access,
        )
    )
    app.include_router(
        create_thread_operations_router(
            verify_api_key,
            _authed_user_id,
            get_agent,
            _require_thread_access,
            publish_sync_event,
        )
    )
    app.include_router(
        create_threads_router(
            verify_api_key,
            _authed_user_id,
            get_agent,
            get_settings,
            _require_thread_access,
            publish_sync_event,
        )
    )
    app.include_router(
        create_unified_tools_router(
            verify_api_key,
            require_admin_user,
            get_agent,
            _require_same_user_or_admin,
        )
    )
    app.include_router(
        create_tools_router(
            verify_api_key,
            _authed_user_id,
            get_agent,
            _require_thread_access,
        )
    )

    # Sync callable thread tools into the registry
    _agent.sync_agent_tools()

    # ========================================================================
    # Frontend static hosting (Outlook add-in / web UI)
    # ========================================================================

    _frontend_dir = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
    if os.path.isdir(_frontend_dir):
        logger.info(f"Serving frontend from {_frontend_dir}")

        # Serve SvelteKit's _app/ assets and other static files
        _app_dir = os.path.join(_frontend_dir, "_app")
        if os.path.isdir(_app_dir):
            app.mount("/_app", StaticFiles(directory=_app_dir), name="frontend-assets")

        # Serve static assets from frontend root (icons, favicon, manifest, etc.)
        for _icon_name in ["favicon.png", "icon-16.png", "icon-32.png", "icon-80.png", "manifest.xml"]:
            _icon_path = os.path.join(_frontend_dir, _icon_name)
            if os.path.isfile(_icon_path):
                def _make_icon_handler(p: str):
                    async def handler():
                        return FileResponse(p)
                    return handler
                app.get(f"/{_icon_name}", include_in_schema=False)(_make_icon_handler(_icon_path))

        # SPA entry point — serves index.html at root
        # This must be registered AFTER all API routes so it doesn't shadow them
        @app.get("/", include_in_schema=False)
        async def serve_spa_root():
            return FileResponse(os.path.join(_frontend_dir, "index.html"))

    @app.post("/chat", tags=["Chat"])
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
        agent = get_agent()
        thread_id = request.thread_id or str(uuid.uuid4())[:8]
        # Ignore client-claimed user_id in the body; derive from auth instead.
        user_id = user.id
        _require_thread_access(user, thread_id)

        # Handle slash commands (e.g., /compact)
        msg_stripped = request.message.strip().lower()
        logger.info(f"[CHAT] Received message: '{request.message}' stripped: '{msg_stripped}' is_compact: {msg_stripped == '/compact'}")
        if msg_stripped == "/compact":
            async def compact_command_response():
                yield f"data: {json.dumps({'type': 'compacting', 'message': 'Compacting context...', 'thread_id': thread_id})}\n\n"
                # Perform compaction (this may take a few seconds)
                result = await agent.compact_now(thread_id, user_id)
                # Send response based on result
                if result.get("success"):
                    messages_removed = result.get('messages_removed', 0)
                    # Emit compacted event so frontend clears chat UI
                    yield f"data: {json.dumps({'type': 'compacted', 'messages_removed': messages_removed, 'auto_resumed': False, 'summary': result.get('summary'), 'thread_id': thread_id})}\n\n"
                    msg = f"✓ Conversation compacted. {messages_removed} messages summarized."
                else:
                    msg = f"Could not compact: {result.get('reason', 'unknown error')}"
                yield f"data: {json.dumps({'type': 'response', 'content': msg})}\n\n"
                # Include context_stats and model in done event
                context_stats = agent.get_context_stats(thread_id)
                effective_model = agent._get_llm_config_for_thread(thread_id).model or agent.settings.llm_model
                yield f"data: {json.dumps({'type': 'done', 'thread_id': thread_id, 'context_stats': context_stats, 'model': effective_model})}\n\n"
            return StreamingResponse(
                compact_command_response(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                }
            )

        # Read client ID from header for sync event origin filtering
        client_id = http_request.headers.get("x-nymeria-client-id", "")

        # For autonomous/self-invoke calls (e.g. watchdog worker), the "user message"
        # isn't from a real user — skip message_added so it doesn't appear in clients
        # as a user-authored message. Frontend subscribes to /autonomous/stream for
        # autonomous task events instead.
        if not request.is_self_invoke:
            publish_sync_event(
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

        def _trigger_fields() -> dict:
            """Common trigger identity fields for autonomous event payloads."""
            fields: Dict[str, Any] = {}
            if request.trigger_id:
                fields["trigger_id"] = request.trigger_id
            if request.trigger_name:
                fields["trigger_name"] = request.trigger_name
            return fields

        def _maybe_create_autonomous_notification(content: str, task_id: str) -> None:
            if not request.is_self_invoke:
                return
            _dispatch_autonomous_notification(
                user_id=user_id,
                thread_id=thread_id,
                task_id=task_id or None,
                summary=(content or "Autonomous task completed")[:200],
                settings=settings,
                thread_config_manager=agent.thread_config_manager,
            )

        async def event_generator():
            """Generate SSE events from agent stream."""
            autonomous_final_content_parts: List[str] = []
            autonomous_completed = False
            autonomous_started = False
            try:
                # Convert attachments to dict format for agent
                attachments = None
                if request.attachments:
                    attachments = [
                        {
                            "file_type": att.file_type,
                            "data_url": att.data_url,
                            "mime_type": att.mime_type,
                            "file_name": att.file_name or "",
                        }
                        for att in request.attachments
                    ]

                # Legacy images support - convert to attachments format
                images = None
                if request.images:
                    images = [{"data_url": img.data_url, "mime_type": img.mime_type} for img in request.images]

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
                            logger.info(f"Client disconnected for thread {thread_id}, agent will continue in background")
                        continue

                    # Publish task_started on the first non-queued chunk so the
                    # frontend handoff happens only after the thread lock is
                    # acquired.
                    if (
                        autonomous_task_id
                        and not autonomous_started
                        and chunk.get("type") != "queued"
                    ):
                        publish_autonomous_event(
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
                        publish_agent_stream_chunk(
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
                        logger.warning(f"Failed to get context stats: {e}")
                        context_stats = None

                    done_data = {
                        'type': 'done',
                        'thread_id': thread_id,
                        'context_stats': context_stats,
                        'model': agent._get_llm_config_for_thread(thread_id).model or agent.settings.llm_model,
                    }

                    # Auto-title the thread from the user's message if untitled
                    try:
                        new_title = agent.thread_metadata_manager.auto_title(
                            user_id, thread_id, request.message
                        )
                        if new_title:
                            done_data['title'] = new_title
                            done_data['title_source'] = 'auto'
                            # Publish title change so other clients update their sidebar
                            publish_sync_event(
                                event_type="thread_updated",
                                thread_id=thread_id,
                                user_id=user_id,
                                data={"title": new_title, "title_source": "auto"},
                                origin_client_id=client_id,
                            )
                    except Exception as e:
                        logger.warning(f"Failed to auto-title thread {thread_id}: {e}")

                    yield f"data: {json.dumps(done_data)}\n\n"

            except Exception as e:
                logger.error(f"Stream error: {e}", exc_info=True)
                error_data = json.dumps({
                    "type": "error",
                    "content": str(e),
                    "thread_id": thread_id,
                })
                yield f"data: {error_data}\n\n"
                if autonomous_task_id and not autonomous_completed:
                    error_text = str(e)
                    _maybe_create_autonomous_notification(error_text, autonomous_task_id)
                    publish_autonomous_event(
                        event_type="task_completed",
                        thread_id=thread_id,
                        user_id=user_id,
                        task_id=autonomous_task_id,
                        data={
                            "error": True,
                            "content": error_text,
                            "notify": _should_notify_autonomous(thread_id, agent.thread_config_manager),
                            "source": request.trigger_override or "autonomous",
                            **_trigger_fields(),
                        },
                    )
                    autonomous_completed = True
            finally:
                if autonomous_task_id and not autonomous_completed:
                    final_content = "".join(autonomous_final_content_parts)
                    _maybe_create_autonomous_notification(final_content, autonomous_task_id)
                    publish_autonomous_event(
                        event_type="task_completed",
                        thread_id=thread_id,
                        user_id=user_id,
                        task_id=autonomous_task_id,
                        data={
                            "content": final_content,
                            "notify": _should_notify_autonomous(thread_id, agent.thread_config_manager),
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
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )

    @app.post("/chat/sync", response_model=ChatResponse, tags=["Chat"])
    async def chat_sync(
        request: ChatRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Send a message and receive a non-streaming response.

        Simpler alternative to the streaming endpoint for clients that
        don't support SSE.
        """
        agent = get_agent()
        thread_id = request.thread_id or str(uuid.uuid4())[:8]
        # Ignore client-claimed user_id in the body; derive from auth instead.
        user_id = user.id
        _require_thread_access(user, thread_id)

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

    return app


def run_api(host: str = "0.0.0.0", port: int = 8000, agent: Optional[NymeriaAgent] = None) -> None:
    """
    Run the API server.

    Args:
        host: Host to bind to
        port: Port to listen on
        agent: Optional agent instance
    """
    import uvicorn

    app = create_api_app(agent)
    uvicorn.run(app, host=host, port=port)
