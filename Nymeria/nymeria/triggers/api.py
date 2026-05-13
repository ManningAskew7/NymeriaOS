"""FastAPI REST API trigger with SSE streaming for Nymeria."""

import logging
import math
import os
from pathlib import Path
from typing import NoReturn, Optional

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import Settings, get_settings
from ..core.agent import NymeriaAgent
from ..core.accounts import (
    AuthenticatedUser,
)
from ..core.event_bus import publish_sync_event
from ..core.rate_limit import SlidingWindowRateLimiter
from ..core import thread_classification as _thread_classification
from ..tools import ALL_TOOLS
from ..api.routers.accounts import create_accounts_router
from ..api.routers.autonomous_stream import create_autonomous_stream_router
from ..api.routers.chat import create_chat_router
from ..api.routers.chat_apps import create_chat_apps_router
from ..api.routers.credentials import create_credentials_router
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

logger = logging.getLogger(__name__)

_classify_thread_platform_from_id = _thread_classification.classify_platform
_is_native_platform_thread = _thread_classification.is_native_platform_thread
_is_shared_channel_thread = _thread_classification.is_shared_channel

# Global agent instance (initialized on startup)
_agent: Optional[NymeriaAgent] = None

_BOT_ADMIN_ENDPOINT_RATE_LIMIT = 120
_BOT_ADMIN_ENDPOINT_RATE_WINDOW_SECONDS = 60.0
_bot_admin_endpoint_rate_limiter = SlidingWindowRateLimiter()
_AUTH_FAILURE_RATE_LIMIT = 10
_AUTH_FAILURE_RATE_WINDOW_SECONDS = 60.0
_auth_failure_rate_limiter = SlidingWindowRateLimiter()
_FRONTEND_RESERVED_PREFIXES = {"_app", "docs", "openapi.json", "redoc"}


def get_agent() -> NymeriaAgent:
    """Get the global agent instance."""
    if _agent is None:
        raise RuntimeError("Agent not initialized. Call create_api_app() first.")
    return _agent


def _frontend_static_dir() -> str:
    package_frontend = Path(__file__).resolve().parents[1] / "frontend"
    if (package_frontend / "index.html").is_file():
        return str(package_frontend)

    source_frontend = Path(__file__).resolve().parents[2] / "frontend"
    if (source_frontend / "index.html").is_file():
        return str(source_frontend)

    if package_frontend.exists():
        return str(package_frontend)
    return str(source_frontend)


def _first_path_segment(path: str) -> str:
    return path.strip("/").split("/", 1)[0]


def _registered_api_prefixes(app: FastAPI) -> set[str]:
    prefixes = set(_FRONTEND_RESERVED_PREFIXES)
    for route in app.routes:
        route_path = getattr(route, "path", "")
        first_segment = _first_path_segment(route_path)
        if first_segment and not first_segment.startswith("{"):
            prefixes.add(first_segment)
    return prefixes


def _is_api_like_frontend_miss(path: str, api_prefixes: set[str]) -> bool:
    first_segment = _first_path_segment(path)
    if not first_segment:
        return False
    if first_segment in api_prefixes:
        return True

    # Missing root assets should remain 404s instead of returning index.html.
    return any("." in segment for segment in path.strip("/").split("/"))


def _request_accepts_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _register_frontend_routes(app: FastAPI, frontend_dir: str) -> None:
    frontend_path = Path(frontend_dir)
    index_path = frontend_path / "index.html"
    if not index_path.is_file():
        logger.warning("Frontend directory exists but index.html is missing: %s", frontend_dir)
        return

    logger.info("Serving frontend from %s", frontend_dir)

    api_prefixes = _registered_api_prefixes(app)

    # Serve SvelteKit's _app/ assets and other static files.
    app_dir = frontend_path / "_app"
    if app_dir.is_dir():
        app.mount("/_app", StaticFiles(directory=str(app_dir)), name="frontend-assets")

    # Serve static assets emitted at the frontend root (icons, logos, manifests,
    # and any copied static/ files) without swallowing API 404s.
    for asset_path in sorted(frontend_path.iterdir()):
        asset_name = asset_path.name
        if asset_name in {"index.html", "_app"} or asset_name.startswith("."):
            continue
        if _first_path_segment(asset_name) in api_prefixes:
            logger.warning("Skipping frontend asset that conflicts with an API route: /%s", asset_name)
            continue

        if asset_path.is_dir():
            app.mount(
                f"/{asset_name}",
                StaticFiles(directory=str(asset_path)),
                name=f"frontend-{asset_name}",
            )
        elif asset_path.is_file():
            def _make_asset_handler(p: Path):
                async def handler():
                    return FileResponse(str(p))
                return handler

            app.get(f"/{asset_name}", include_in_schema=False)(_make_asset_handler(asset_path))

    # SPA entry point and browser-route fallback. These are registered after
    # all API routers so concrete API routes still win.
    @app.get("/", include_in_schema=False)
    async def serve_spa_root():
        return FileResponse(str(index_path))

    @app.get("/{path:path}", include_in_schema=False)
    async def serve_spa_fallback(path: str, request: Request):
        if not _request_accepts_html(request) or _is_api_like_frontend_miss(path, api_prefixes):
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(str(index_path))


# ============================================================================
# Authentication
# ============================================================================


async def verify_api_key(
    request: Request,
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
        request=request,
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


def _auth_failure_rate_limit_key(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown-client"


def _raise_rate_limited_auth_failure(request: Request, failure: HTTPException) -> NoReturn:
    result = _auth_failure_rate_limiter.check(
        _auth_failure_rate_limit_key(request),
        limit=_AUTH_FAILURE_RATE_LIMIT,
        window_seconds=_AUTH_FAILURE_RATE_WINDOW_SECONDS,
    )
    if result.allowed:
        raise failure

    retry_after = max(1, math.ceil(result.retry_after))
    logger.warning(
        "Rate limited failed authentication attempts from %s; retry_after=%ss",
        _auth_failure_rate_limit_key(request),
        retry_after,
    )
    raise HTTPException(
        status_code=429,
        detail="Too many failed authentication attempts",
        headers={"Retry-After": str(retry_after)},
    )


def _resolve_caller_from_bearer(
    *,
    request: Request,
    authorization: Optional[str],
) -> tuple[AuthenticatedUser, Optional[NymeriaAgent]]:
    try:
        presented = _extract_bearer_token(authorization)
    except HTTPException as exc:
        _raise_rate_limited_auth_failure(request, exc)

    try:
        agent = get_agent()
    except RuntimeError:
        agent = None

    caller = None
    if agent is not None:
        caller = agent.accounts_repo.verify_token(presented)

    if caller is None:
        _raise_rate_limited_auth_failure(
            request,
            HTTPException(status_code=401, detail="Invalid API key"),
        )

    return caller, agent


async def resolve_authenticated_user(
    request: Request,
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
    caller, agent = _resolve_caller_from_bearer(
        request=request,
        authorization=authorization,
    )

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


def _reset_auth_failure_rate_limiter_for_tests() -> None:
    _auth_failure_rate_limiter.clear()


async def require_admin_caller(
    request: Request,
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
    caller, agent = _resolve_caller_from_bearer(
        request=request,
        authorization=authorization,
    )

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


def _validate_cors_settings(settings: Settings) -> None:
    """Refuse unsafe wildcard CORS when credentialed requests are allowed."""
    origins = getattr(settings, "cors_origins_list", [])
    if "*" in origins:
        raise RuntimeError(
            "CORS_ORIGINS cannot include '*' while credentialed CORS is enabled. "
            "List explicit origins instead."
        )


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
    _validate_cors_settings(settings)

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
    api_docs_enabled = settings.api_docs_enabled
    app = FastAPI(
        title="Nymeria API",
        description="Personal AI Assistant REST API with SSE streaming",
        version="1.0.0",
        docs_url="/docs" if api_docs_enabled else None,
        redoc_url="/redoc" if api_docs_enabled else None,
        openapi_url="/openapi.json" if api_docs_enabled else None,
    )

    async def _close_provider_http_pools() -> None:
        from ..vendor.react_agent.providers import (
            close_provider_async_http_pools_for_loop,
        )

        await close_provider_async_http_pools_for_loop()

    app.router.add_event_handler("shutdown", _close_provider_http_pools)

    # Add CORS middleware with configurable origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if settings.nymeria_debug:
        @app.middleware("http")
        async def _log_request_origin(request: Request, call_next):
            origin = request.headers.get("origin")
            if origin:
                logger.debug("HTTP Origin header for %s: %s", request.url.path, origin)
            return await call_next(request)

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
    app.include_router(create_credentials_router(verify_api_key, require_admin_user, get_agent))
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
        create_chat_router(
            verify_api_key,
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

    _frontend_dir = _frontend_static_dir()
    if os.path.isdir(_frontend_dir):
        _register_frontend_routes(app, _frontend_dir)

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
