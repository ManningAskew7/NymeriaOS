"""FastAPI REST API trigger with SSE streaming for Nymeria."""

import logging
import math
import os
from pathlib import Path
from typing import Any, Callable, NoReturn, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .body_limit import BodySizeLimitMiddleware
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import Settings, get_settings
from ..core.agent import NymeriaAgent
from ..core.accounts import (
    AuthenticatedUser,
)
from ..core.event_bus import publish_sync_event
from ..core.rate_limit import SlidingWindowRateLimiter
from ..core.request_context import reset_request_id, set_request_id
from ..core import thread_classification as _thread_classification
from ..tools import SEED_TOOLS
from ..api.routers.accounts import create_accounts_router
from ..api.routers.autonomous_stream import create_autonomous_stream_router
from ..api.routers.browser_commands import create_browser_commands_router
from ..api.routers.chat import create_chat_router
from ..api.routers.chat_apps import create_chat_apps_router
from ..api.routers.cliproxy import create_cliproxy_router
from ..api.routers.commands import create_commands_router
from ..api.routers.credentials import create_credentials_router
from ..api.routers.credential_prompts import create_credential_prompts_router
from ..api.routers.custom_tools import create_custom_tools_router
from ..api.routers.activity import create_activity_router
from ..api.routers.notifications_config import create_notifications_config_router
from ..api.routers.agent_threads import create_agent_threads_router
from ..api.routers.devices import create_devices_router
from ..api.routers.google_chat_bot import create_google_chat_bot_router
from ..api.routers.instagram_bot import create_instagram_bot_router
from ..api.routers.line_bot import create_line_bot_router
from ..api.routers.messenger_bot import create_messenger_bot_router
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
from ..api.routers.teams_bot import create_teams_bot_router
from ..api.routers.unified_tools import create_unified_tools_router
from ..api.routers.user_tools import create_user_tools_router
from ..api.routers.voice import create_voice_router
from ..api.routers.webex_bot import create_webex_bot_router
from ..api.routers.whatsapp_bot import create_whatsapp_bot_router
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
# Per-user cap on expensive authenticated endpoints (chat / voice / commands)
# to bound runaway LLM/STT spend from a compromised or looping token. Generous
# by default (a human never approaches it), admins are exempt (the worker
# relays autonomous turns as the admin/service user), and 0 disables it.
_EXPENSIVE_ENDPOINT_RATE_WINDOW_SECONDS = 60.0
_expensive_endpoint_rate_limiter = SlidingWindowRateLimiter()
_FRONTEND_RESERVED_PREFIXES = {"_app", "docs", "openapi.json", "redoc"}
_REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_ALLOWED_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "._:-"
)
_REQUEST_ID_MAX_LENGTH = 128


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


def _normalize_request_id(raw_request_id: str | None) -> str:
    if raw_request_id:
        request_id = raw_request_id.strip()
        if (
            0 < len(request_id) <= _REQUEST_ID_MAX_LENGTH
            and all(char in _REQUEST_ID_ALLOWED_CHARS for char in request_id)
        ):
            return request_id
    return str(uuid4())


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


def _expensive_endpoint_rate_limit() -> int:
    """Per-user request cap per window for chat/voice/commands (0 disables)."""
    try:
        return int(os.environ.get("NYMERIA_USER_REQUEST_RATE_LIMIT", "300"))
    except ValueError:
        return 300


def _make_rate_limited_auth(scope: str) -> Callable[..., Any]:
    """Build an auth dependency that adds a per-user cap on top of verify_api_key.

    Admins are exempt so the worker's autonomous-turn relay (which calls /chat
    as the admin/service user) is never throttled. Returns the resolved user so
    routes can keep ``user: AuthenticatedUser = Depends(...)`` unchanged.
    """

    async def _dependency(
        request: Request,
        authorization: Optional[str] = Header(None),
        x_nymeria_act_as: Optional[str] = Header(None),
        settings: Settings = Depends(get_settings),
    ) -> AuthenticatedUser:
        user = await verify_api_key(
            request=request,
            authorization=authorization,
            x_nymeria_act_as=x_nymeria_act_as,
            settings=settings,
        )
        limit = _expensive_endpoint_rate_limit()
        if limit <= 0 or getattr(user, "role", None) == "admin":
            return user
        result = _expensive_endpoint_rate_limiter.check(
            f"{scope}:{user.id}",
            limit=limit,
            window_seconds=_EXPENSIVE_ENDPOINT_RATE_WINDOW_SECONDS,
        )
        if not result.allowed:
            retry_after = max(1, math.ceil(result.retry_after))
            raise HTTPException(
                status_code=429,
                detail="Too many requests; please slow down.",
                headers={"Retry-After": str(retry_after)},
            )
        return user

    return _dependency


def _resolve_act_as_target(
    agent: Optional[NymeriaAgent], act_as_user_id: str
) -> AuthenticatedUser:
    """Resolve an ``X-Nymeria-Act-As`` header into the impersonated user.

    The caller owns the admin-role gate and must apply it *before* invoking
    this helper; it only performs the target lookup and ``AuthenticatedUser``
    reconstruction shared by :func:`resolve_authenticated_user` and
    :func:`require_admin_caller`.

    Raises 503 when the agent is not yet initialized, and 404 when the target
    user is missing or disabled.
    """
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    target = agent.accounts_repo.get_user_by_id(act_as_user_id)
    if target is None or target.disabled:
        raise HTTPException(status_code=404, detail="Act-As target not found")
    return AuthenticatedUser(
        id=target.id,
        email=target.email,
        display_name=target.display_name,
        role=target.role,
        via_act_as=True,
    )


async def resolve_authenticated_user(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_nymeria_act_as: Optional[str] = Header(None),
    settings: Settings = Depends(get_settings),
):
    """
    Resolve the caller to an :class:`AuthenticatedUser`.

    Only per-user account tokens (``nym_...``) are accepted; the legacy
    ``NYMERIA_API_KEY`` shared key is no longer recognized.

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
        return _resolve_act_as_target(agent, x_nymeria_act_as)

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
        return _resolve_act_as_target(agent, x_nymeria_act_as)

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


def _require_thread_access(
    user: AuthenticatedUser, thread_id: str, *, claim: bool = True
) -> None:
    """
    Enforce that ``user`` owns ``thread_id`` (or is admin).

    ``claim`` controls first-touch ownership. With ``claim=True`` (the
    default, used by write/mutating endpoints) a personal thread is claimed
    on first touch. With ``claim=False`` (read-only GET endpoints) an
    ownerless personal thread is NOT claimed: access is allowed without
    inserting a ``thread_owners`` row, and ownership is established later by
    the first write (POST /chat, command execution, PATCH metadata, explicit
    POST /claim). This stops merely opening a new thread tab — which fires
    read-only ``/skills`` and ``/callable-tools`` GETs — from registering a
    permanent empty "ghost" thread that reappears in the sidebar after a
    sync. A thread owned by a *different* user still resolves to 404 in both
    modes, so the read-only branch does not weaken cross-user isolation; it
    only drops the TOFU claim for an otherwise-ownerless thread.

    Admins bypass ownership for already-owned threads but DO claim on first
    touch for personal threads (UUIDs, ``discord_dm_*``, positive
    ``telegram_<id>``) when ``claim=True``. Without this, a thread an admin
    opened but never sent a message in stayed ownerless — the first non-admin
    user (or bot-routed act-as caller) to touch it would TOFU-claim, silently
    transferring ownership away from the creator. (With ``claim=False`` an
    admin merely reading a thread no longer locks it; the admin's first write
    still does.)

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
        if claim:
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
    if claim:
        owner = agent.accounts_repo.claim_thread(thread_id, user.id)
        if owner != user.id:
            raise HTTPException(status_code=404, detail="Not found")
        return
    # Read-only access: do not TOFU-claim an ownerless thread. Allow the read
    # when the thread is ownerless or owned by the caller; reject only when it
    # is owned by someone else (same 404 the claiming branch would raise).
    owner = agent.accounts_repo.get_thread_owner(thread_id)
    if owner is not None and owner != user.id:
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


def create_api_app(
    agent: Optional[NymeriaAgent] = None,
    *,
    slim_mode: bool = False,
    slim_base_url: Optional[str] = None,
    enable_slim_mcp: bool = True,
    enable_slim_watchdog: bool = True,
) -> FastAPI:
    """
    Create the FastAPI application.

    Args:
        agent: Optional agent instance (creates default if not provided)
        slim_mode: True when launched via ``python run.py slim``. Forces the
            in-process ticker on (Redis must already be off), mounts the
            embedded MCP ASGI app at ``/mcp``, bootstraps an internal admin
            service token, and registers an async watchdog as a startup task.
        slim_base_url: Loopback URL slim-mode internal clients should use to
            reach this very process (e.g. ``http://127.0.0.1:8000``). Used
            by the embedded MCP backend client and the slim watchdog client.
        enable_slim_mcp: Debug escape hatch — set False to skip mounting MCP
            in slim mode.
        enable_slim_watchdog: Debug escape hatch — set False to skip the
            in-process watchdog task even when ``WATCHDOG_ENABLED=true``.

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
    if slim_mode:
        # Slim runs everything in one process. The run.py launcher has already
        # forced Redis off via env overrides before settings were cached.
        disable_ticker = False
    if disable_ticker:
        from ..core.event_bus import create_event_bus, set_event_bus
        event_bus = create_event_bus(settings)
        set_event_bus(event_bus)

    if agent is not None:
        _agent = agent
    else:
        _agent = NymeriaAgent(
            tools=list(SEED_TOOLS),
            enable_ticker=not disable_ticker,
        )

    # Bootstrap an internal admin service token so MCP / watchdog / trigger-fire
    # / command-service calls can authenticate without manual operator
    # provisioning. Slim mints it for its single process; the full Docker stack
    # mints it from the api into the shared ``nymeria_data`` volume, where the
    # sibling worker / mcp / watchdog containers read the file (see
    # ``core.service_bootstrap.resolve_service_token``). Persisted at
    # ``data/SLIM_SERVICE_TOKEN.txt`` (mode 0600) and reused on later boots.
    slim_service_token: Optional[str] = None
    configured_service_token = (
        getattr(settings, "nymeria_service_token", None) or ""
    ).strip()
    # ``disable_ticker`` is the canonical "multi-container Docker stack" signal
    # (Redis on; slim forced it False above). Only that shape needs the api to
    # mint a token its sibling containers can read; a local SQLite ``run.py api``
    # with no configured token is left untouched, as are redis-off unit tests.
    should_bootstrap_service_token = slim_mode or (
        disable_ticker and not configured_service_token
    )
    if (
        should_bootstrap_service_token
        and getattr(_agent, "accounts_repo", None) is not None
    ):
        from ..core.service_bootstrap import ensure_service_token

        try:
            slim_service_token = ensure_service_token(
                _agent.accounts_repo,
                settings.data_dir,
                configured_token=getattr(settings, "nymeria_service_token", None),
            )
        except Exception:  # noqa: BLE001
            if slim_mode:
                # Slim is one process: without the token its embedded MCP /
                # watchdog / command calls cannot authenticate, so fail loudly
                # rather than start a half-working backend (pre-broadening behavior).
                raise
            # Full stack: best-effort. A mint failure here must not block the api
            # from serving; internal callers stay unauthenticated until the next
            # boot mints successfully or an operator sets NYMERIA_SERVICE_TOKEN.
            logger.warning(
                "Service-token bootstrap failed; internal callers may be "
                "unauthenticated until NYMERIA_SERVICE_TOKEN is set.",
                exc_info=True,
            )
            slim_service_token = None

        if slim_service_token:
            # Mutate the cached settings instance and process env so every
            # in-process consumer (MCP client, command service, trigger-fire
            # helpers) picks the token up automatically.
            try:
                object.__setattr__(
                    settings, "nymeria_service_token", slim_service_token
                )
            except Exception:
                settings.nymeria_service_token = slim_service_token  # type: ignore[attr-defined]
            os.environ["NYMERIA_SERVICE_TOKEN"] = slim_service_token

    if slim_mode and slim_base_url:
        os.environ["NYMERIA_API_URL"] = slim_base_url

    # Initialize FCM if enabled
    if settings.fcm_enabled and settings.fcm_credentials_json:
        from ..core.fcm import _init_firebase
        _init_firebase(settings.fcm_credentials_json)

    # Create FastAPI app
    api_docs_enabled = settings.api_docs_enabled
    app = FastAPI(
        title="Nymeria API",
        description="Personal AI Assistant REST API with SSE streaming",
        version=__version__,
        docs_url="/docs" if api_docs_enabled else None,
        redoc_url="/redoc" if api_docs_enabled else None,
        openapi_url="/openapi.json" if api_docs_enabled else None,
    )

    async def _close_provider_http_pools() -> None:
        from ..vendor.react_agent.providers import (
            close_provider_async_http_pools_for_loop,
        )

        await close_provider_async_http_pools_for_loop()

    async def _stop_agent_ticker() -> None:
        ticker = getattr(_agent, "_ticker", None)
        if ticker is None:
            return
        try:
            ticker.stop()
        except Exception:
            logger.exception("Failed to stop agent ticker during API shutdown")

    app.router.add_event_handler("shutdown", _close_provider_http_pools)
    app.router.add_event_handler("shutdown", _stop_agent_ticker)

    @app.middleware("http")
    async def _request_id_context(request: Request, call_next):
        request_id = _normalize_request_id(request.headers.get(_REQUEST_ID_HEADER))
        request.state.request_id = request_id
        token = set_request_id(request_id)
        try:
            response = await call_next(request)
            response.headers[_REQUEST_ID_HEADER] = request_id
            return response
        finally:
            reset_request_id(token)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        if "x-content-type-options" not in response.headers:
            response.headers["X-Content-Type-Options"] = "nosniff"
        if "x-frame-options" not in response.headers:
            response.headers["X-Frame-Options"] = "DENY"
        if "referrer-policy" not in response.headers:
            response.headers["Referrer-Policy"] = "no-referrer"
        return response

    # Reject oversized request bodies before any handler buffers them. Added
    # before CORS so CORS ends up outermost (413s keep their CORS headers) but
    # the cap still runs ahead of auth/signature checks and route handlers.
    # Webhooks are server-to-server text payloads (tight cap); /chat and /voice
    # carry inline base64 attachments / audio (generous caps).
    _MB = 1024 * 1024
    app.add_middleware(
        BodySizeLimitMiddleware,
        default_limit=16 * _MB,
        path_limits=[
            ("/integrations/", 1 * _MB),
            ("/chat", 32 * _MB),
            ("/voice/", 32 * _MB),
        ],
    )

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
    app.include_router(
        create_whatsapp_bot_router(
            get_agent,
            get_settings,
            _require_thread_access,
            publish_sync_event,
        )
    )
    app.include_router(
        create_messenger_bot_router(
            get_agent,
            get_settings,
            _require_thread_access,
            publish_sync_event,
        )
    )
    app.include_router(
        create_instagram_bot_router(
            get_agent,
            get_settings,
            _require_thread_access,
            publish_sync_event,
        )
    )
    app.include_router(
        create_webex_bot_router(
            get_agent,
            get_settings,
            _require_thread_access,
            publish_sync_event,
        )
    )
    app.include_router(
        create_teams_bot_router(
            get_agent,
            get_settings,
            _require_thread_access,
            publish_sync_event,
        )
    )
    app.include_router(
        create_google_chat_bot_router(
            get_agent,
            get_settings,
            _require_thread_access,
            publish_sync_event,
        )
    )
    app.include_router(
        create_line_bot_router(
            get_agent,
            get_settings,
            _require_thread_access,
            publish_sync_event,
        )
    )
    app.include_router(create_credentials_router(verify_api_key, require_admin_user, get_agent))
    app.include_router(create_credential_prompts_router(verify_api_key, get_agent, get_settings))
    app.include_router(create_browser_commands_router(verify_api_key))
    app.include_router(create_workspace_router(verify_api_key))
    app.include_router(create_rag_router(verify_api_key, get_agent, _require_same_user_or_admin))
    app.include_router(create_memory_router(verify_api_key, get_agent, _require_same_user_or_admin))
    app.include_router(create_user_tools_router(verify_api_key, get_agent, _require_same_user_or_admin))
    app.include_router(create_skills_router(verify_api_key, _authed_user_id, get_agent, _require_thread_access))
    app.include_router(create_voice_router(_make_rate_limited_auth("voice"), get_agent, get_settings, _require_thread_access))
    app.include_router(create_agent_threads_router(verify_api_key, get_agent, publish_sync_event))
    app.include_router(create_activity_router(verify_api_key, _authed_user_id, get_settings))
    app.include_router(
        create_notifications_config_router(
            verify_api_key,
            _authed_user_id,
            get_agent,
            get_settings,
        )
    )
    app.include_router(create_todos_router(verify_api_key, require_admin_user, _authed_user_id, get_settings))
    app.include_router(create_commands_router(_make_rate_limited_auth("commands"), get_agent, get_settings))
    app.include_router(
        create_autonomous_stream_router(
            get_agent,
            get_settings,
            auth_failure_handler=_raise_rate_limited_auth_failure,
        )
    )
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
        create_cliproxy_router(
            require_admin_user,
            get_agent,
            get_settings,
            require_thread_access_fn=_require_thread_access,
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
            _make_rate_limited_auth("chat"),
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

    # Pre-embed the tool-search catalog in the background. Both slim and Docker
    # run the agent in this (API) process and the worker never builds this app,
    # so registering unconditionally warms exactly where searches run. Without
    # it, the first tool_search after a restart embeds ~1300 tools inline and
    # can hit the 300s tool-execution timeout.
    _register_tool_index_warm_lifecycle(app)

    # ========================================================================
    # Slim-mode wiring (embedded MCP + in-process watchdog)
    #
    # MCP must be mounted before the frontend catch-all route is registered,
    # otherwise the SPA fallback can swallow /mcp/* requests.
    # ========================================================================
    if slim_mode and enable_slim_mcp:
        from ..mcp_server import create_mcp_asgi_app, mcp as fastmcp_app

        mcp_app = create_mcp_asgi_app(
            api_url=slim_base_url,
            service_token=slim_service_token,
        )
        app.mount("/mcp", mcp_app, name="slim-mcp")
        # FastMCP's Streamable HTTP transport requires its session-manager
        # task group to be running. The sub-app declares its own lifespan,
        # but Starlette's Mount does not auto-propagate sub-app lifespans,
        # so we enter the session manager's run() context from the parent
        # FastAPI lifespan instead.
        _register_slim_mcp_lifecycle(app, fastmcp_app)

    if slim_mode and enable_slim_watchdog:
        _register_slim_watchdog_lifecycle(
            app,
            settings=settings,
            base_url=slim_base_url,
            service_token=slim_service_token,
        )

    # In Docker, scheduled TODOs / triggers are driven by the worker
    # container (which now relays turns back into this API), so the
    # ticker doesn't run here. Spawned-thread idle cleanup used to ride
    # along on the ticker; preserve it via a dedicated housekeeping task
    # so temporary callable threads still get reaped on schedule.
    if disable_ticker:
        _register_spawn_thread_housekeeping_lifecycle(app, agent_getter=get_agent)
        _register_dream_scheduler_lifecycle(app, agent_getter=get_agent)

    # ========================================================================
    # Frontend static hosting (Outlook add-in / web UI)
    # ========================================================================

    _frontend_dir = _frontend_static_dir()
    if os.path.isdir(_frontend_dir):
        _register_frontend_routes(app, _frontend_dir)

    return app


def _register_slim_mcp_lifecycle(app: FastAPI, fastmcp_app) -> None:
    """Enter the FastMCP session manager's task group during app startup.

    FastMCP's ``streamable_http_app()`` returns a Starlette app whose lifespan
    starts the StreamableHTTPSessionManager. When that Starlette app is
    mounted into a parent FastAPI via ``app.mount("/mcp", ...)``, the parent's
    lifespan does NOT propagate to the sub-app, so we must drive the session
    manager's task group ourselves or every MCP request fails with
    ``RuntimeError: Task group is not initialized``.
    """
    import contextlib

    state: dict[str, object] = {"stack": None}

    async def _start_mcp_session_manager() -> None:
        # ``session_manager`` is lazily created the first time
        # ``streamable_http_app()`` is called; ``create_mcp_asgi_app`` has
        # already done that before we get here.
        session_manager = fastmcp_app.session_manager
        stack = contextlib.AsyncExitStack()
        await stack.__aenter__()
        try:
            await stack.enter_async_context(session_manager.run())
        except Exception:
            await stack.aclose()
            raise
        state["stack"] = stack
        logger.info("Slim MCP session manager started")

    async def _stop_mcp_session_manager() -> None:
        stack = state.get("stack")
        if stack is None:
            return
        try:
            await stack.aclose()  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
            logger.warning("Slim MCP session manager shutdown error: %s", exc)
        state["stack"] = None
        logger.info("Slim MCP session manager stopped")

    app.router.add_event_handler("startup", _start_mcp_session_manager)
    app.router.add_event_handler("shutdown", _stop_mcp_session_manager)


def _register_slim_watchdog_lifecycle(
    app: FastAPI,
    *,
    settings: Settings,
    base_url: Optional[str],
    service_token: Optional[str],
) -> None:
    """Wire startup/shutdown event handlers for the in-process watchdog."""
    import asyncio

    async def _start_slim_watchdog() -> None:
        # Import lazily so the stubs swapped in by tests via monkeypatch take
        # effect even when the test patches the module-level attribute after
        # this factory is imported.
        from . import api_client as api_client_module
        from . import watchdog_worker as watchdog_module

        if not settings.watchdog_enabled:
            logger.info("Slim watchdog skipped: WATCHDOG_ENABLED=false")
            return
        if not service_token:
            logger.warning("Slim watchdog skipped: no service token available")
            return

        # base_url is required for the API client; the slim launcher always
        # passes one, but fall back to a sane local default if a caller
        # constructs the app directly without it.
        client_base_url = base_url or f"http://127.0.0.1:{settings.api_port}"
        client = api_client_module.NymeriaAPIClient(
            base_url=client_base_url,
            api_key=service_token,
        )
        worker = watchdog_module.WatchdogWorker(client=client, settings=settings)
        task = asyncio.create_task(worker.run())
        app.state.slim_watchdog_worker = worker
        app.state.slim_watchdog_client = client
        app.state.slim_watchdog_task = task
        logger.info("Slim watchdog started (in-process task)")

    async def _stop_slim_watchdog() -> None:
        worker = getattr(app.state, "slim_watchdog_worker", None)
        task = getattr(app.state, "slim_watchdog_task", None)
        client = getattr(app.state, "slim_watchdog_client", None)
        if worker is not None:
            worker.stop()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass  # Cancellation result is intentionally discarded during shutdown.
            except Exception as exc:  # noqa: BLE001 - shutdown is best-effort
                logger.warning("Slim watchdog task exited with error: %s", exc)
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Slim watchdog client close failed: %s", exc)
        logger.info("Slim watchdog stopped")

    app.router.add_event_handler("startup", _start_slim_watchdog)
    app.router.add_event_handler("shutdown", _stop_slim_watchdog)


def _register_spawn_thread_housekeeping_lifecycle(
    app: FastAPI,
    *,
    agent_getter: Callable[[], NymeriaAgent],
) -> None:
    """Run periodic spawned-thread idle cleanup when the API owns no ticker.

    Slim runs the cleanup from the in-process ticker's housekeeping
    executor. In Docker, the worker container drives scheduling but no
    longer holds a local agent, so it can't sweep spawned threads
    safely (and ``agent.sync_agent_tools()`` afterward has to run in
    the process that actually serves chat). This task replaces that
    code path with an API-side, 30-minute heartbeat that mirrors the
    ticker's existing cadence.
    """
    import asyncio

    SWEEP_INTERVAL_SECONDS = 1800  # 30 minutes (matches Ticker._spawn_sweep_interval)
    stop_event: "asyncio.Event | None" = None
    task: "asyncio.Task[None] | None" = None

    async def _housekeeping_loop(event: asyncio.Event) -> None:
        from ..tools.spawn_thread import sweep_idle_spawned_threads

        # Short startup delay so we don't race the agent's first
        # ``sync_agent_tools()`` call during app boot.
        try:
            await asyncio.wait_for(event.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass  # Expected — first heartbeat tick fires after the delay.

        while not event.is_set():
            try:
                agent = agent_getter()
                deleted = sweep_idle_spawned_threads(agent)
                if deleted:
                    logger.info(
                        "Spawn idle-sweep deleted %d temporary thread(s)",
                        deleted,
                    )
                    # Resync tool registry so the callable-thread tool list
                    # reflects the deletions on the next prompt.
                    try:
                        agent.sync_agent_tools()
                    except Exception:
                        logger.exception(
                            "Failed to sync agent tools after spawn sweep"
                        )
            except Exception:
                logger.exception("Spawn-thread housekeeping pass failed")

            try:
                await asyncio.wait_for(event.wait(), timeout=SWEEP_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass  # Normal heartbeat tick.

    async def _start() -> None:
        nonlocal stop_event, task
        stop_event = asyncio.Event()
        task = asyncio.create_task(_housekeeping_loop(stop_event))
        app.state.spawn_housekeeping_task = task
        app.state.spawn_housekeeping_stop = stop_event
        logger.info("Spawn-thread housekeeping task started (Docker mode)")

    async def _stop() -> None:
        if stop_event is not None:
            stop_event.set()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    app.router.add_event_handler("startup", _start)
    app.router.add_event_handler("shutdown", _stop)


def _register_dream_scheduler_lifecycle(
    app: FastAPI,
    *,
    agent_getter: Callable[[], NymeriaAgent],
) -> None:
    """Drive automatic dream scheduling API-side when the API owns no ticker.

    Slim runs the dream sweep from the in-process ticker's housekeeping
    executor (``Ticker(dream_sweeper=...)``). In Docker the worker drives
    scheduling but holds no local agent, and ``invoke_dream`` spawns the
    dream turn in-process, so the sweep must run where the agent lives: the
    API. This heartbeat mirrors the ticker's dream-sweep cadence.
    """
    import asyncio

    from ..core.dreaming import DREAM_SWEEP_INTERVAL_SECONDS

    stop_event: "asyncio.Event | None" = None
    task: "asyncio.Task[None] | None" = None

    async def _dream_loop(event: asyncio.Event) -> None:
        from ..core.dreaming import sweep_dreamable_threads

        # Short startup delay so the first sweep doesn't race app boot.
        try:
            await asyncio.wait_for(event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass  # Expected — first sweep fires after the delay.

        while not event.is_set():
            try:
                agent = agent_getter()
                started = await asyncio.to_thread(sweep_dreamable_threads, agent)
                if started:
                    logger.info("Dream sweep started %d dream(s)", started)
            except Exception:
                logger.exception("Dream scheduler pass failed")

            try:
                await asyncio.wait_for(
                    event.wait(), timeout=DREAM_SWEEP_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass  # Normal heartbeat tick.

    async def _start() -> None:
        nonlocal stop_event, task
        stop_event = asyncio.Event()
        task = asyncio.create_task(_dream_loop(stop_event))
        app.state.dream_scheduler_task = task
        app.state.dream_scheduler_stop = stop_event
        logger.info("Dream scheduler task started (Docker mode)")

    async def _stop() -> None:
        if stop_event is not None:
            stop_event.set()
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    app.router.add_event_handler("startup", _start)
    app.router.add_event_handler("shutdown", _stop)


# Tool-index warm: startup delay (let app boot settle) and the heartbeat that
# reconciles any change signal missed before the loop registered.
TOOL_INDEX_WARM_STARTUP_DELAY_SECONDS = 20.0
TOOL_INDEX_WARM_HEARTBEAT_SECONDS = 300.0


def _register_tool_index_warm_lifecycle(app: FastAPI) -> None:
    """Background-warm the tool-search embedding cache in the API process.

    The first ``tool_search`` after a restart would otherwise embed the whole
    catalog (~1300 tools) inline and could exceed the 300s tool-execution
    timeout. This task embeds the catalog into the persistent store / in-memory
    cache at startup (off the event loop) and re-embeds deltas when the catalog
    changes (``mark_tool_search_dirty`` sets the wake event). Until the warm
    completes, ``search`` serves keyword results instead of embedding inline.
    """
    import asyncio

    from ..core.tool_search_index import get_tool_search_index

    stop_event: "asyncio.Event | None" = None
    wake_event: "asyncio.Event | None" = None
    task: "asyncio.Task[None] | None" = None

    async def _warm_loop(stop: asyncio.Event, wake: asyncio.Event) -> None:
        index = get_tool_search_index()
        index.register_warm_loop(asyncio.get_running_loop(), wake)

        # Short startup delay so the first warm doesn't race app boot.
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=TOOL_INDEX_WARM_STARTUP_DELAY_SECONDS
            )
        except asyncio.TimeoutError:
            pass  # Expected: the first warm fires after the delay.

        while not stop.is_set():
            # Clear before warming so a change signaled mid-pass is not lost: it
            # stays set and immediately re-triggers the next pass.
            wake.clear()
            try:
                await asyncio.to_thread(index.warm_embeddings)
            except Exception:
                logger.exception("Tool index warm pass failed")
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(
                    wake.wait(), timeout=TOOL_INDEX_WARM_HEARTBEAT_SECONDS
                )
            except asyncio.TimeoutError:
                pass  # Heartbeat tick: re-warm (idempotent, delta-cheap).

    async def _start() -> None:
        nonlocal stop_event, wake_event, task
        stop_event = asyncio.Event()
        wake_event = asyncio.Event()
        task = asyncio.create_task(_warm_loop(stop_event, wake_event))
        app.state.tool_index_warm_task = task
        app.state.tool_index_warm_stop = stop_event
        app.state.tool_index_warm_wake = wake_event
        logger.info("Tool index warm task started")

    async def _stop() -> None:
        if stop_event is not None:
            stop_event.set()
        if wake_event is not None:
            wake_event.set()  # Break the wake wait promptly.
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    app.router.add_event_handler("startup", _start)
    app.router.add_event_handler("shutdown", _stop)


def run_api(
    host: str = "0.0.0.0",
    port: int = 8000,
    agent: Optional[NymeriaAgent] = None,
    *,
    slim_mode: bool = False,
    slim_base_url: Optional[str] = None,
    enable_slim_mcp: bool = True,
    enable_slim_watchdog: bool = True,
) -> None:
    """
    Run the API server.

    Args:
        host: Host to bind to
        port: Port to listen on
        agent: Optional agent instance
        slim_mode: Enable slim single-process mode (embedded MCP + watchdog).
        slim_base_url: Loopback URL internal slim clients should use.
        enable_slim_mcp: Disable to skip mounting MCP in slim mode.
        enable_slim_watchdog: Disable to skip the in-process watchdog task.
    """
    import uvicorn

    app = create_api_app(
        agent,
        slim_mode=slim_mode,
        slim_base_url=slim_base_url,
        enable_slim_mcp=enable_slim_mcp,
        enable_slim_watchdog=enable_slim_watchdog,
    )

    # Honor X-Forwarded-For only from explicitly trusted proxy IPs. Without
    # this, behind Caddy every client collapses to the proxy IP, so the per-IP
    # auth-failure limiter both fails to isolate attackers and lets one client
    # lock everyone out. Opt-in via NYMERIA_FORWARDED_ALLOW_IPS (e.g.
    # "127.0.0.1" when Caddy is colocated); unset means do not trust the header.
    forwarded_allow_ips = os.environ.get("NYMERIA_FORWARDED_ALLOW_IPS", "").strip()
    uvicorn_kwargs: dict = {"host": host, "port": port}
    if forwarded_allow_ips:
        uvicorn_kwargs["proxy_headers"] = True
        uvicorn_kwargs["forwarded_allow_ips"] = forwarded_allow_ips

    uvicorn.run(app, **uvicorn_kwargs)
