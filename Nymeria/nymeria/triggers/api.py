"""FastAPI REST API trigger with SSE streaming for Nymeria."""

import json
import logging
import math
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

import httpx
from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..core.agent import NymeriaAgent
from ..core.accounts import (
    AmbiguousTokenPrefix,
    AuthenticatedUser,
    LastAdminError,
    TokenNotFound,
    UserAlreadyExists,
    UserHasResources,
    UserNotFound,
)
from ..core.chat_bindings import (
    BindCodeInvalid,
    BindingAlreadyExists,
    BotAlreadyRegistered,
)
from ..core import secrets as nymeria_secrets
from ..core.checkpoint_cleanup import delete_thread_checkpoints
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
from ..core.thread_classification import (
    classify_platform as _classify_thread_platform_from_id,
    is_native_platform_thread as _is_native_platform_thread,
    is_shared_channel as _is_shared_channel_thread,
)
from ..core.todo_manager import TodoManager
from ..core.thread_deletion import ThreadDeletionBusy, cascade_delete_thread
from ..tools import ALL_TOOLS
from ..api.routers.autonomous_stream import create_autonomous_stream_router
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
from ..api.routers.thread_operations import create_thread_operations_router
from ..api.routers.todos import create_todos_router
from ..api.routers.tools import create_tools_router
from ..api.routers.unified_tools import create_unified_tools_router
from ..api.routers.user_tools import create_user_tools_router
from ..api.routers.voice import create_voice_router
from ..api.routers.workspace import create_workspace_router
from ..api.schemas.thread_operations import FileData

logger = logging.getLogger(__name__)

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


class ThreadHistoryResponse(BaseModel):
    """Response model for conversation history."""

    thread_id: str
    messages: list


# --- Admin user/token/platform models --------------------------------------


class AdminUserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    role: Literal["user", "admin"]
    disabled: bool
    created_at: str
    updated_at: str
    token_count: int
    last_token_use: Optional[str]
    thread_count: Optional[int] = None
    todo_count: Optional[int] = None
    platform_count: Optional[int] = None


class AdminUserCreateRequest(BaseModel):
    email: str
    display_name: Optional[str] = None
    role: Literal["user", "admin"] = "user"
    id: Optional[str] = None
    token_label: Optional[str] = None


class AdminUserUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    role: Optional[Literal["user", "admin"]] = None
    disabled: Optional[bool] = None


class TokenInfoResponse(BaseModel):
    token_hash_prefix: str
    label: Optional[str]
    created_at: str
    last_used_at: Optional[str]
    revoked_at: Optional[str]


class TokenIssueRequest(BaseModel):
    label: Optional[str] = None


class IssuedTokenResponse(BaseModel):
    raw_token: str
    metadata: TokenInfoResponse


class RotatedTokensResponse(BaseModel):
    raw_token: str
    metadata: TokenInfoResponse
    revoked_count: int


class PlatformIdentityResponse(BaseModel):
    provider: Literal["discord", "telegram", "twitch"]
    provider_user_id: str
    created_at: str


class PlatformLinkRequest(BaseModel):
    provider: Literal["discord", "telegram", "twitch"]
    provider_user_id: str


class MeUpdateRequest(BaseModel):
    display_name: Optional[str] = None


# --- Chat-app bindings (per-thread Telegram/Discord/etc routing) ----------


class ChatAppProviderField(BaseModel):
    """Body fragment shared by chatapp endpoints — only telegram is wired today."""

    provider: Literal["telegram"]


class ChatAppBindCodeRequest(ChatAppProviderField):
    """Request a short-lived code the user types into the bot to bind a chat
    to a Nymeria thread."""


class ChatAppBindCodeResponse(BaseModel):
    code: str
    expires_at: str
    bot_username: Optional[str] = None
    deep_link: Optional[str] = None  # populated only if bot_username is configured


class PlatformLinkCodeRequest(ChatAppProviderField):
    """Request a short-lived code the user types into the bot via /start
    link_<code> to associate their Telegram identity with their Nymeria
    account. Self-service alternative to ``users link-platform``."""


class ChatAppBindingResponse(BaseModel):
    id: int
    thread_id: str
    provider: Literal["discord", "telegram", "twitch"]
    platform_chat_id: str
    created_at: str
    # None when the binding is served by the shared global bot; the row id
    # of a user_telegram_bots entry when served by a user-owned bot. The
    # Chat App UI can use this to label "via @YourBot" vs "via shared bot".
    user_telegram_bot_id: Optional[int] = None


# --- Admin chat-app endpoints (called by bots with the service token) ----


class AdminBindingLookupResponse(BaseModel):
    id: int
    thread_id: str
    provider: Literal["discord", "telegram", "twitch"]
    platform_chat_id: str
    user_id: str
    created_at: str
    user_telegram_bot_id: Optional[int] = None


class AdminChatAppBindClaimRequest(BaseModel):
    code: str
    provider: Literal["telegram"]
    platform_chat_id: str
    # The platform's user_id (e.g. Telegram from.id) — must match the
    # Nymeria user that issued the code, after platform_identities resolution.
    expected_provider_user_id: str


class AdminChatAppBindClaimResponse(BaseModel):
    binding_id: int
    thread_id: str
    user_id: str


class AdminChatAppSwitchRequest(BaseModel):
    provider: Literal["telegram"]
    platform_chat_id: str
    thread_id: str
    user_id: str
    user_telegram_bot_id: Optional[int] = None


class AdminChatAppSwitchResponse(BaseModel):
    binding_id: int
    thread_id: str
    user_id: str
    previous_thread_id: Optional[str] = None


class AdminPlatformLinkClaimRequest(BaseModel):
    code: str
    provider: Literal["telegram"]
    # The platform's user_id to link to the Nymeria account that issued the code.
    platform_user_id: str


class AdminPlatformLinkClaimResponse(BaseModel):
    user_id: str
    provider: Literal["telegram"]
    provider_user_id: str
    created_at: str


# --- BYO Telegram bots (user-owned, paste-token wizard) -----------------


class MyTelegramBotResponse(BaseModel):
    """User-facing bot record. The token is never exposed here."""

    id: int
    bot_username: str
    enabled: bool
    created_at: str
    last_seen_at: Optional[str]


class RegisterTelegramBotRequest(BaseModel):
    """Body for ``POST /me/telegram-bots``: the raw token from BotFather.

    The server validates the token via Telegram's ``getMe`` API, encrypts
    it with ``NYMERIA_SECRETS_KEY``, stores the ciphertext, and returns the
    bot's metadata. Plaintext never crosses the response boundary.
    """

    bot_token: str


class AdminTelegramBotResponse(BaseModel):
    """Admin-only bot record including the **decrypted** token. Consumed
    by the supervisor process to start polling loops."""

    id: int
    owner_user_id: str
    bot_username: str
    bot_token: str  # decrypted; only this endpoint surfaces it
    enabled: bool
    created_at: str
    last_seen_at: Optional[str]


# Extends the existing AdminChatAppBindClaimRequest path with an optional
# bot id so the bot's /bind handler can prove it consumed the code from
# inside a user-owned bot, bypassing the platform_identities check.
class AdminChatAppBindClaimViaBotRequest(BaseModel):
    code: str
    provider: Literal["telegram"]
    platform_chat_id: str
    via_user_telegram_bot_id: int


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


_CALLABLE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_THREAD_TEAM_SLUG_RE = re.compile(r"[^a-z0-9_-]+")


def _validate_callable_name(name: str) -> None:
    """Reject callable names that would crash LLM tool/function binding.

    OpenAI and Anthropic both require tool names matching
    ``^[a-zA-Z0-9_-]{1,64}$``. We enforce here so the rejection is HTTP 400
    at config time rather than a runtime explosion the first time the agent
    tries to call the tool. Used by both POST /agents/threads (via Pydantic
    field pattern) and PATCH /threads/{id}/config + the metadata-rename path
    where Pydantic isn't sufficient because the title flows into
    ``callable_name``.
    """
    if not _CALLABLE_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid callable name '{name}': must match "
                "[a-zA-Z0-9_-]{1,64} for LLM tool binding (no spaces, dots, "
                "or punctuation)."
            ),
        )


def _normalize_thread_team_name(name: str) -> str:
    normalized = " ".join((name or "").strip().split())
    if not normalized:
        raise HTTPException(status_code=400, detail="Team name is required")
    if len(normalized) > 120:
        raise HTTPException(status_code=400, detail="Team name must be 120 characters or fewer")
    return normalized


def _make_thread_team_id(name: str) -> str:
    slug = _THREAD_TEAM_SLUG_RE.sub("-", name.strip().lower()).strip("-_")
    if not slug:
        slug = "team"
    return f"team-{slug[:48]}-{uuid.uuid4().hex[:8]}"


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


_TOKEN_HASH_PREFIX_LEN = 8


def _bot_username_for(provider: str, settings: Settings) -> Optional[str]:
    """Return the bot's public @username if configured, else None.

    Today only telegram is wired; others can plug in by adding fields to
    Settings (e.g. ``discord_bot_username``).
    """
    if provider == "telegram":
        # Strip a leading '@' if the user copy-pasted with it.
        raw = (settings.telegram_bot_username or "").lstrip("@").strip()
        return raw or None
    return None


def _build_chatapp_link_payload(
    *,
    code: str,
    expires_at: str,
    provider: str,
    kind: str,  # 'thread_bind' or 'platform_link'
    settings: Settings,
) -> Dict[str, Any]:
    """Shape the response for issue-bind-code endpoints.

    Builds a Telegram deep link of the form ``t.me/<bot>?start=<prefix>_<code>``
    when the bot username is known (so a single tap pre-fills the right
    command). The bot interprets the ``start`` payload in its ``/start``
    handler. Falls back to just the code when bot_username isn't configured —
    the wizard then renders manual instructions.
    """
    bot_username = _bot_username_for(provider, settings)
    deep_link: Optional[str] = None
    if bot_username:
        if kind == "platform_link":
            deep_link = f"https://t.me/{bot_username}?start=link_{code}"
        elif kind == "thread_bind":
            deep_link = f"https://t.me/{bot_username}?start=bind_{code}"
    return {
        "code": code,
        "expires_at": expires_at,
        "bot_username": bot_username,
        "deep_link": deep_link,
    }


def _token_info(record) -> "TokenInfoResponse":
    """Project a TokenRecord onto the public TokenInfoResponse — exposing
    only the first 8 hex chars of the sha256 hash so the UI has a stable
    handle for revoke without ever seeing raw token material."""
    return TokenInfoResponse(
        token_hash_prefix=record.token_hash[:_TOKEN_HASH_PREFIX_LEN],
        label=record.label,
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        revoked_at=record.revoked_at,
    )


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
        create_thread_operations_router(
            verify_api_key,
            _authed_user_id,
            get_agent,
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

    def _bound_chatapp_platform(thread_id: str) -> Optional[str]:
        """Return the sidebar platform implied by an explicit chat-app binding."""
        try:
            if get_agent().chat_bindings_repo.lookup_thread_binding_by_thread(
                "telegram", thread_id
            ):
                return "telegram"
        except Exception as e:
            logger.warning(
                "Failed to inspect chat-app binding platform for %s: %s",
                thread_id,
                e,
            )
        return None

    def _thread_list_platform(thread_id: str, meta=None) -> str:
        """Resolve the platform value the frontend should render for a thread."""
        platform = meta.platform if meta else _classify_thread_platform_from_id(thread_id)
        bound_platform = _bound_chatapp_platform(thread_id)
        if bound_platform:
            return bound_platform

        native_platform = _classify_thread_platform_from_id(thread_id)
        if native_platform in {"discord", "telegram", "slack", "trigger", "twitch"}:
            return native_platform
        if platform in {"discord", "telegram", "slack", "trigger", "twitch"}:
            return platform

        thread_config_manager = getattr(get_agent(), "thread_config_manager", None)
        tc = (
            thread_config_manager.get_config(thread_id)
            if thread_config_manager is not None
            else None
        )
        if tc and tc.callable:
            platform = "callable"
        elif platform == "callable":
            platform = "desktop"

        return platform

    def _publish_chatapp_platform_sync(thread_id: str, user_id: str, origin_client_id: str = "") -> None:
        """Notify clients when a chat-app binding changes a thread's platform icon."""
        meta = get_agent().thread_metadata_manager.get_thread(user_id, thread_id)
        publish_sync_event(
            event_type="thread_updated",
            thread_id=thread_id,
            user_id=user_id,
            data={"platform": _thread_list_platform(thread_id, meta)},
            origin_client_id=origin_client_id,
        )

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

    # ========================================================================
    # Endpoints
    # ========================================================================

    @app.get("/me", tags=["Auth"])
    async def get_me(user=Depends(resolve_authenticated_user)):
        """
        Return the authenticated user's identity.

        Frontends call this on first connect to learn their own ``user_id`` so
        they can namespace ``localStorage`` keys (``nymeria-<user_id>-*``).
        Requires a per-user account token (``nym_...``); the legacy shared
        ``NYMERIA_API_KEY`` was retired in Step 3c. Honors
        ``X-Nymeria-Act-As: <user_id>`` for admin callers (returns the target
        user's identity instead of the admin's).
        """
        return {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "role": user.role,
        }

    @app.get("/platform/resolve", tags=["Auth"])
    async def platform_resolve(
        provider: str,
        provider_user_id: str,
        _admin=Depends(require_admin_user),
    ):
        """
        Resolve a platform identity (Discord/Telegram/Twitch user ID) to a
        Nymeria ``user_id``. Admin-only — used by bot thin clients with the
        service token to route per-user traffic without holding raw per-user
        tokens.
        """
        if provider not in ("discord", "telegram", "twitch"):
            raise HTTPException(status_code=400, detail="Unknown provider")
        user_id = get_agent().accounts_repo.resolve_platform(provider, provider_user_id)
        if user_id is None:
            raise HTTPException(status_code=404, detail="Not linked")
        return {"user_id": user_id}

    # ========================================================================
    # Self (PATCH /me, /me/tokens) — any authenticated user
    # ========================================================================

    @app.patch("/me", tags=["Auth"])
    async def patch_me(
        body: MeUpdateRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """Update the current user's own profile fields (display_name only)."""
        if body.display_name is not None and not body.display_name.strip():
            raise HTTPException(status_code=400, detail="display_name cannot be empty")
        try:
            updated = get_agent().accounts_repo.update_user(
                user.id, display_name=body.display_name
            )
        except UserNotFound:
            raise HTTPException(status_code=404, detail="User not found")
        return {
            "id": updated.id,
            "email": updated.email,
            "display_name": updated.display_name,
            "role": updated.role,
        }

    @app.get(
        "/me/tokens",
        response_model=List[TokenInfoResponse],
        tags=["Auth"],
    )
    async def list_my_tokens(user: AuthenticatedUser = Depends(verify_api_key)):
        """List the current user's own API tokens."""
        records = get_agent().accounts_repo.list_tokens_for_user(user.id)
        return [_token_info(r) for r in records]

    @app.post(
        "/me/tokens",
        response_model=IssuedTokenResponse,
        tags=["Auth"],
    )
    async def issue_my_token(
        body: TokenIssueRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Issue a new token for the current user. Raw token returned ONCE."""
        repo = get_agent().accounts_repo
        raw = repo.issue_token(user.id, label=body.label)
        records = repo.list_tokens_for_user(user.id)
        # Find the freshly-issued one (matches the raw's hash).
        import hashlib as _hashlib
        hash_full = _hashlib.sha256(raw.encode("utf-8")).hexdigest()
        rec = next((r for r in records if r.token_hash == hash_full), None)
        if rec is None:
            raise HTTPException(status_code=500, detail="Token issued but not found")
        return IssuedTokenResponse(raw_token=raw, metadata=_token_info(rec))

    @app.delete("/me/tokens/{token_hash_prefix}", tags=["Auth"])
    async def revoke_my_token(
        token_hash_prefix: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Revoke one of the current user's tokens by hash prefix."""
        try:
            revoked = get_agent().accounts_repo.revoke_token(user.id, token_hash_prefix)
        except TokenNotFound:
            raise HTTPException(status_code=404, detail="Token not found")
        except AmbiguousTokenPrefix as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"revoked": revoked}

    # ========================================================================
    # Admin: /admin/users — caller must be admin
    # ========================================================================

    @app.get(
        "/admin/users",
        response_model=List[AdminUserResponse],
        tags=["Admin"],
    )
    async def admin_list_users(
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        """List every account. Counts are summary-level (no per-user counts
        for threads/todos/platforms — fetch via GET /admin/users/{id} for that)."""
        repo = get_agent().accounts_repo
        users = repo.list_users()
        out: List[AdminUserResponse] = []
        for u in users:
            tokens = repo.list_tokens_for_user(u.id)
            active = [t for t in tokens if t.revoked_at is None]
            last_use = max(
                (t.last_used_at for t in active if t.last_used_at), default=None
            )
            out.append(
                AdminUserResponse(
                    id=u.id,
                    email=u.email,
                    display_name=u.display_name,
                    role=u.role,
                    disabled=u.disabled,
                    created_at=u.created_at,
                    updated_at=u.updated_at,
                    token_count=len(active),
                    last_token_use=last_use,
                )
            )
        return out

    @app.post(
        "/admin/users",
        response_model=IssuedTokenResponse,
        tags=["Admin"],
    )
    async def admin_create_user(
        body: AdminUserCreateRequest,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Create a new user and issue them a first token. Raw token shown ONCE."""
        repo = get_agent().accounts_repo
        email = body.email.strip().lower()
        if not email or "@" not in email:
            raise HTTPException(status_code=400, detail="Valid email required")
        # Derive a stable id when not supplied. Prefer the local-part of the
        # email; sanitize to repo-safe characters; fall back to a uuid suffix
        # on collision (admins can rename later via the path-id form).
        import re as _re
        import uuid as _uuid
        if body.id:
            user_id = body.id
        else:
            user_id = _re.sub(r"[^a-z0-9_-]+", "", email.split("@", 1)[0])[:32] or _uuid.uuid4().hex[:12]
        if repo.get_user_by_id(user_id) is not None:
            user_id = f"{user_id}-{_uuid.uuid4().hex[:6]}"
        display_name = (body.display_name or email.split("@", 1)[0]).strip()
        try:
            repo.create_user(
                user_id=user_id,
                email=email,
                display_name=display_name,
                role=body.role,
            )
        except UserAlreadyExists as e:
            raise HTTPException(status_code=409, detail=str(e))
        raw = repo.issue_token(user_id, label=body.token_label or "initial")
        records = repo.list_tokens_for_user(user_id)
        import hashlib as _hashlib
        hash_full = _hashlib.sha256(raw.encode("utf-8")).hexdigest()
        rec = next((r for r in records if r.token_hash == hash_full), None)
        return IssuedTokenResponse(raw_token=raw, metadata=_token_info(rec))

    @app.get(
        "/admin/users/{user_id}",
        response_model=AdminUserResponse,
        tags=["Admin"],
    )
    async def admin_get_user(
        user_id: str,
        _admin: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings),
    ):
        """Get a single user with full counts (threads, todos, platforms)."""
        repo = get_agent().accounts_repo
        u = repo.get_user_by_id(user_id)
        if u is None:
            raise HTTPException(status_code=404, detail="User not found")
        tokens = repo.list_tokens_for_user(user_id)
        active = [t for t in tokens if t.revoked_at is None]
        last_use = max((t.last_used_at for t in active if t.last_used_at), default=None)
        threads = repo.list_threads_for_user(user_id)
        platforms = repo.list_platforms_for_user(user_id)
        try:
            todos = TodoManager(settings.data_dir).get_todos(user_id)
            todo_count = len(todos.items) if todos else 0
        except Exception:
            todo_count = 0
        return AdminUserResponse(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            role=u.role,
            disabled=u.disabled,
            created_at=u.created_at,
            updated_at=u.updated_at,
            token_count=len(active),
            last_token_use=last_use,
            thread_count=len(threads),
            todo_count=todo_count,
            platform_count=len(platforms),
        )

    @app.patch(
        "/admin/users/{user_id}",
        response_model=AdminUserResponse,
        tags=["Admin"],
    )
    async def admin_update_user(
        user_id: str,
        body: AdminUserUpdateRequest,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Update display_name / role / disabled. Last-admin guard applies."""
        repo = get_agent().accounts_repo
        try:
            if body.display_name is not None or body.role is not None:
                if body.display_name is not None and not body.display_name.strip():
                    raise HTTPException(status_code=400, detail="display_name cannot be empty")
                repo.update_user(
                    user_id,
                    display_name=body.display_name,
                    role=body.role,
                )
            if body.disabled is not None:
                repo.set_disabled(user_id, body.disabled)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="User not found")
        except LastAdminError as e:
            raise HTTPException(status_code=409, detail=str(e))
        u = repo.get_user_by_id(user_id)
        tokens = repo.list_tokens_for_user(user_id)
        active = [t for t in tokens if t.revoked_at is None]
        last_use = max((t.last_used_at for t in active if t.last_used_at), default=None)
        return AdminUserResponse(
            id=u.id,
            email=u.email,
            display_name=u.display_name,
            role=u.role,
            disabled=u.disabled,
            created_at=u.created_at,
            updated_at=u.updated_at,
            token_count=len(active),
            last_token_use=last_use,
        )

    @app.delete("/admin/users/{user_id}", tags=["Admin"])
    async def admin_delete_user(
        user_id: str,
        _admin: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings),
    ):
        """Delete a user and cascade their tokens / platform identities.

        Refuses (409) if the user still owns threads or todos — admin must
        empty those first. Last-admin guard also applies.
        """
        repo = get_agent().accounts_repo
        # Pre-check todos here since AccountsRepo doesn't know about them.
        try:
            todos = TodoManager(settings.data_dir).get_todos(user_id)
            if todos and todos.items:
                raise HTTPException(
                    status_code=409,
                    detail=f"User still owns {len(todos.items)} todo(s); delete those first",
                )
        except HTTPException:
            raise
        except Exception:
            logger.debug("Todo file cleanup skipped, may not exist")
        try:
            repo.delete_user_cascade(user_id)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="User not found")
        except UserHasResources as e:
            raise HTTPException(status_code=409, detail=str(e))
        except LastAdminError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"deleted": True}

    # --- admin: tokens for any user --------------------------------------

    @app.get(
        "/admin/users/{user_id}/tokens",
        response_model=List[TokenInfoResponse],
        tags=["Admin"],
    )
    async def admin_list_user_tokens(
        user_id: str,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        repo = get_agent().accounts_repo
        if repo.get_user_by_id(user_id) is None:
            raise HTTPException(status_code=404, detail="User not found")
        return [_token_info(r) for r in repo.list_tokens_for_user(user_id)]

    @app.post(
        "/admin/users/{user_id}/tokens",
        response_model=IssuedTokenResponse,
        tags=["Admin"],
    )
    async def admin_issue_user_token(
        user_id: str,
        body: TokenIssueRequest,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        repo = get_agent().accounts_repo
        try:
            raw = repo.issue_token(user_id, label=body.label)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="User not found")
        records = repo.list_tokens_for_user(user_id)
        import hashlib as _hashlib
        hash_full = _hashlib.sha256(raw.encode("utf-8")).hexdigest()
        rec = next((r for r in records if r.token_hash == hash_full), None)
        return IssuedTokenResponse(raw_token=raw, metadata=_token_info(rec))

    @app.post(
        "/admin/users/{user_id}/tokens/rotate",
        response_model=RotatedTokensResponse,
        tags=["Admin"],
    )
    async def admin_rotate_user_tokens(
        user_id: str,
        body: TokenIssueRequest,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Revoke every active token for the user, then issue a fresh one.

        WARNING: any session/bot using a previous token will get 401 on its
        next request — including this caller if they're rotating their own.
        """
        repo = get_agent().accounts_repo
        if repo.get_user_by_id(user_id) is None:
            raise HTTPException(status_code=404, detail="User not found")
        revoked_count = repo.revoke_all_tokens(user_id)
        raw = repo.issue_token(user_id, label=body.label or "rotated")
        records = repo.list_tokens_for_user(user_id)
        import hashlib as _hashlib
        hash_full = _hashlib.sha256(raw.encode("utf-8")).hexdigest()
        rec = next((r for r in records if r.token_hash == hash_full), None)
        return RotatedTokensResponse(
            raw_token=raw,
            metadata=_token_info(rec),
            revoked_count=revoked_count,
        )

    @app.delete(
        "/admin/users/{user_id}/tokens/{token_hash_prefix}",
        tags=["Admin"],
    )
    async def admin_revoke_user_token(
        user_id: str,
        token_hash_prefix: str,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        repo = get_agent().accounts_repo
        if repo.get_user_by_id(user_id) is None:
            raise HTTPException(status_code=404, detail="User not found")
        try:
            revoked = repo.revoke_token(user_id, token_hash_prefix)
        except TokenNotFound:
            raise HTTPException(status_code=404, detail="Token not found")
        except AmbiguousTokenPrefix as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"revoked": revoked}

    # --- admin: platform identities for any user -------------------------

    @app.get(
        "/admin/users/{user_id}/platforms",
        response_model=List[PlatformIdentityResponse],
        tags=["Admin"],
    )
    async def admin_list_user_platforms(
        user_id: str,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        repo = get_agent().accounts_repo
        if repo.get_user_by_id(user_id) is None:
            raise HTTPException(status_code=404, detail="User not found")
        return [
            PlatformIdentityResponse(
                provider=p.provider,
                provider_user_id=p.provider_user_id,
                created_at=p.created_at,
            )
            for p in repo.list_platforms_for_user(user_id)
        ]

    @app.post(
        "/admin/users/{user_id}/platforms",
        response_model=PlatformIdentityResponse,
        tags=["Admin"],
    )
    async def admin_link_user_platform(
        user_id: str,
        body: PlatformLinkRequest,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        repo = get_agent().accounts_repo
        existing_owner = repo.resolve_platform(body.provider, body.provider_user_id)
        if existing_owner is not None and existing_owner != user_id:
            raise HTTPException(
                status_code=409,
                detail=f"Platform identity already linked to user '{existing_owner}'",
            )
        try:
            repo.link_platform(body.provider, body.provider_user_id, user_id)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="User not found")
        # Round-trip to fetch the row we just wrote, so created_at is honest.
        for p in repo.list_platforms_for_user(user_id):
            if p.provider == body.provider and p.provider_user_id == body.provider_user_id:
                return PlatformIdentityResponse(
                    provider=p.provider,
                    provider_user_id=p.provider_user_id,
                    created_at=p.created_at,
                )
        raise HTTPException(status_code=500, detail="Linked but not found")

    @app.delete(
        "/admin/users/{user_id}/platforms/{provider}/{provider_user_id}",
        tags=["Admin"],
    )
    async def admin_unlink_user_platform(
        user_id: str,
        provider: str,
        provider_user_id: str,
        _admin: AuthenticatedUser = Depends(require_admin_user),
    ):
        if provider not in ("discord", "telegram", "twitch"):
            raise HTTPException(status_code=400, detail="Unknown provider")
        repo = get_agent().accounts_repo
        # Resolve current owner first so we can give a clear error if the
        # identity exists but belongs to a different user.
        owner = repo.resolve_platform(provider, provider_user_id)
        if owner is None:
            raise HTTPException(status_code=404, detail="Not linked")
        if owner != user_id:
            raise HTTPException(
                status_code=404, detail="Not linked to this user"
            )
        repo.unlink_platform(provider, provider_user_id)
        return {"unlinked": True}

    # --- admin: chat-app bindings (called by bots via service token) -----

    @app.get(
        "/admin/chatapp/bindings",
        response_model=List[AdminBindingLookupResponse],
        tags=["Admin"],
    )
    async def admin_list_chatapp_bindings(
        provider: Optional[str] = None,
        _admin=Depends(require_rate_limited_admin_bot_user),
    ):
        """List every chat-app binding (optionally filtered by provider).

        Called by chat-app bots on startup to populate their in-memory
        ``chat_id <-> thread_id`` lookup cache; also called periodically
        to absorb desktop-wizard binding changes the bot hasn't seen.
        """
        if provider is not None and provider not in ("discord", "telegram", "twitch"):
            raise HTTPException(status_code=400, detail="Unknown provider")
        repo = get_agent().chat_bindings_repo
        return [
            AdminBindingLookupResponse(
                id=b.id,
                thread_id=b.thread_id,
                provider=b.provider,
                platform_chat_id=b.platform_chat_id,
                user_id=b.user_id,
                created_at=b.created_at,
                user_telegram_bot_id=b.user_telegram_bot_id,
            )
            for b in repo.list_thread_bindings_global(provider=provider)
        ]

    @app.get(
        "/admin/chatapp/bindings/lookup",
        response_model=AdminBindingLookupResponse,
        tags=["Admin"],
    )
    async def admin_lookup_chatapp_binding(
        provider: str,
        platform_chat_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        _admin=Depends(require_rate_limited_admin_bot_user),
    ):
        """Look up a thread<->chat binding by either chat_id or thread_id.

        Used by chat-app bots (Telegram, future Discord) to:
          * resolve inbound messages from a chat to the bound thread, and
          * resolve outbound SSE events on a non-default thread to the
            destination chat.
        """
        if provider not in ("discord", "telegram", "twitch"):
            raise HTTPException(status_code=400, detail="Unknown provider")
        if (platform_chat_id is None) == (thread_id is None):
            raise HTTPException(
                status_code=400,
                detail="Provide exactly one of 'platform_chat_id' or 'thread_id'",
            )
        repo = get_agent().chat_bindings_repo
        binding = (
            repo.lookup_thread_binding_by_chat(provider, platform_chat_id)
            if platform_chat_id is not None
            else repo.lookup_thread_binding_by_thread(provider, thread_id)
        )
        if binding is None:
            raise HTTPException(status_code=404, detail="No binding")
        return AdminBindingLookupResponse(
            id=binding.id,
            thread_id=binding.thread_id,
            provider=binding.provider,
            platform_chat_id=binding.platform_chat_id,
            user_id=binding.user_id,
            created_at=binding.created_at,
            user_telegram_bot_id=binding.user_telegram_bot_id,
        )

    @app.post(
        "/admin/chatapp/bindings/claim",
        response_model=AdminChatAppBindClaimResponse,
        tags=["Admin"],
    )
    async def admin_claim_thread_bind_code(
        body: AdminChatAppBindClaimRequest,
        _admin=Depends(require_rate_limited_admin_bot_user),
    ):
        """Atomically consume a thread-bind code and create the binding.

        Called by the bot's ``/bind <code>`` handler. Verifies that the
        Telegram (or other platform) user invoking the command resolves —
        via ``platform_identities`` — to the same Nymeria user that issued
        the code. Prevents user A from binding user B's thread by simply
        knowing the code.
        """
        repo = get_agent().accounts_repo
        bindings = get_agent().chat_bindings_repo
        # Phase 1: inspect (read-only) to get claim info for authorization.
        try:
            claim = bindings.inspect_bind_code(
                body.code, kind="thread_bind", provider=body.provider
            )
        except BindCodeInvalid as e:
            raise HTTPException(status_code=400, detail=f"Invalid code: {e}")
        if claim.thread_id is None:
            raise HTTPException(status_code=500, detail="Code has no thread_id")
        # Phase 2: authorize before consuming the code.
        resolved_user = repo.resolve_platform(
            body.provider, body.expected_provider_user_id
        )
        if resolved_user != claim.user_id:
            raise HTTPException(
                status_code=403,
                detail="Code was issued by a different Nymeria account",
            )
        # Phase 3: create the binding before consuming the code, so a
        # failure (e.g. BindingAlreadyExists) leaves the code reusable.
        try:
            binding = bindings.create_thread_binding(
                thread_id=claim.thread_id,
                provider=body.provider,
                platform_chat_id=body.platform_chat_id,
                user_id=claim.user_id,
            )
        except BindingAlreadyExists as e:
            raise HTTPException(status_code=409, detail=str(e))
        # Phase 4: consume the code now that everything succeeded.
        try:
            bindings.claim_bind_code(
                body.code, kind="thread_bind", provider=body.provider
            )
        except BindCodeInvalid:
            pass  # bind code already consumed or expired, continue
        _publish_chatapp_platform_sync(binding.thread_id, binding.user_id)
        return AdminChatAppBindClaimResponse(
            binding_id=binding.id,
            thread_id=binding.thread_id,
            user_id=binding.user_id,
        )

    @app.delete(
        "/admin/chatapp/bindings/by-chat",
        tags=["Admin"],
    )
    async def admin_unbind_chatapp_by_chat(
        provider: str,
        platform_chat_id: str,
        _admin=Depends(require_rate_limited_admin_bot_user),
    ):
        """Remove the binding for a given (provider, chat_id). Called by the
        bot's ``/unbind`` handler. Returns ``{unbound: bool}``.
        """
        if provider not in ("discord", "telegram", "twitch"):
            raise HTTPException(status_code=400, detail="Unknown provider")
        repo = get_agent().chat_bindings_repo
        binding = repo.lookup_thread_binding_by_chat(provider, platform_chat_id)
        if binding is None:
            return {"unbound": False}
        # Bypass the user_id check by passing the binding's owner — admin
        # path is operating on behalf of whoever owns it.
        repo.delete_thread_binding(binding.id, user_id=binding.user_id)
        _publish_chatapp_platform_sync(binding.thread_id, binding.user_id)
        return {"unbound": True, "thread_id": binding.thread_id}

    @app.post(
        "/admin/chatapp/bindings/switch",
        response_model=AdminChatAppSwitchResponse,
        tags=["Admin"],
    )
    async def admin_switch_chatapp_binding(
        body: AdminChatAppSwitchRequest,
        _admin=Depends(require_rate_limited_admin_bot_user),
    ):
        """Move a chat-app chat to another existing user-owned thread.

        Called by Telegram ``/switch`` and ``/new``. Unlike bind-code claims,
        this does not prove possession of a desktop-issued code; instead the
        bot supplies the resolved Nymeria user id and the API verifies that
        the target thread is already owned by that user.
        """
        repo = get_agent().accounts_repo
        bindings = get_agent().chat_bindings_repo
        target_thread_id = body.thread_id.strip()
        if not target_thread_id:
            raise HTTPException(status_code=400, detail="thread_id is required")
        if _is_native_platform_thread(target_thread_id):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Native platform threads cannot be switch targets; use "
                    "/unbind to return a Telegram chat to its default thread"
                ),
            )
        user = repo.get_user_by_id(body.user_id)
        if user is None or user.disabled:
            raise HTTPException(status_code=404, detail="User not found")
        if target_thread_id not in set(repo.list_threads_for_user(body.user_id)):
            raise HTTPException(status_code=404, detail="Target thread not found")

        if body.user_telegram_bot_id is not None:
            bot = bindings.get_user_telegram_bot(body.user_telegram_bot_id)
            if bot is None or not bot.enabled:
                raise HTTPException(status_code=404, detail="Telegram bot not found")
            if bot.owner_user_id != body.user_id:
                raise HTTPException(
                    status_code=403,
                    detail="Telegram bot belongs to a different Nymeria user",
                )

        try:
            binding, previous_thread_id = bindings.switch_thread_binding_for_chat(
                thread_id=target_thread_id,
                provider=body.provider,
                platform_chat_id=body.platform_chat_id,
                user_id=body.user_id,
                user_telegram_bot_id=body.user_telegram_bot_id,
            )
        except BindingAlreadyExists as e:
            raise HTTPException(status_code=409, detail=str(e))
        except UserNotFound as e:
            raise HTTPException(status_code=404, detail=str(e))

        if previous_thread_id and previous_thread_id != binding.thread_id:
            _publish_chatapp_platform_sync(previous_thread_id, binding.user_id)
        _publish_chatapp_platform_sync(binding.thread_id, binding.user_id)
        return AdminChatAppSwitchResponse(
            binding_id=binding.id,
            thread_id=binding.thread_id,
            user_id=binding.user_id,
            previous_thread_id=previous_thread_id,
        )

    @app.post(
        "/admin/chatapp/bindings/claim-via-bot",
        response_model=AdminChatAppBindClaimResponse,
        tags=["Admin"],
    )
    async def admin_claim_thread_bind_code_via_bot(
        body: AdminChatAppBindClaimViaBotRequest,
        _admin=Depends(require_rate_limited_admin_bot_user),
    ):
        """Variant of /claim used by user-owned bots.

        The bot is registered to a Nymeria user via ``user_telegram_bots``,
        so the bot itself is the credential — we don't need a
        ``platform_identities`` lookup. We require the bot's owner to match
        the bind code's issuer (prevents user A's bot from claiming user B's
        code if the code somehow leaked). The created binding records
        ``user_telegram_bot_id`` so outbound delivery routes through the
        right bot's token.
        """
        repo = get_agent().chat_bindings_repo
        bot = repo.get_user_telegram_bot(body.via_user_telegram_bot_id)
        if bot is None or not bot.enabled:
            logger.warning(
                "claim-via-bot: bot id=%s not found / disabled",
                body.via_user_telegram_bot_id,
            )
            raise HTTPException(status_code=404, detail="Bot not found")
        # Phase 1: inspect (read-only) to get claim info for authorization.
        try:
            claim = repo.inspect_bind_code(
                body.code, kind="thread_bind", provider=body.provider
            )
        except BindCodeInvalid as e:
            redacted = (
                (body.code[:2] + "*" * max(0, len(body.code) - 4) + body.code[-2:])
                if body.code else "(empty)"
            )
            logger.warning(
                "claim-via-bot: BindCodeInvalid for code=%s len=%d kind=thread_bind "
                "provider=%s bot_id=%s reason=%s",
                redacted,
                len(body.code or ""),
                body.provider,
                body.via_user_telegram_bot_id,
                e,
            )
            raise HTTPException(status_code=400, detail=f"Invalid code: {e}")
        if claim.thread_id is None:
            raise HTTPException(status_code=500, detail="Code has no thread_id")
        # Phase 2: authorize before consuming the code.
        if bot.owner_user_id != claim.user_id:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Bind code was issued by a different Nymeria account "
                    "than this bot's owner."
                ),
            )
        # Phase 3: create the binding before consuming the code, so a
        # failure (e.g. BindingAlreadyExists) leaves the code reusable.
        try:
            binding = repo.create_thread_binding(
                thread_id=claim.thread_id,
                provider=body.provider,
                platform_chat_id=body.platform_chat_id,
                user_id=claim.user_id,
                user_telegram_bot_id=bot.id,
            )
        except BindingAlreadyExists as e:
            raise HTTPException(status_code=409, detail=str(e))
        # Phase 4: consume the code now that everything succeeded.
        try:
            repo.claim_bind_code(
                body.code, kind="thread_bind", provider=body.provider
            )
        except BindCodeInvalid:
            pass  # bind code already consumed or expired, continue
        _publish_chatapp_platform_sync(binding.thread_id, binding.user_id)
        return AdminChatAppBindClaimResponse(
            binding_id=binding.id,
            thread_id=binding.thread_id,
            user_id=binding.user_id,
        )

    @app.get(
        "/admin/telegram-bots",
        response_model=List[AdminTelegramBotResponse],
        tags=["Admin"],
    )
    async def admin_list_telegram_bots(
        _admin=Depends(require_rate_limited_admin_bot_user),
    ):
        """List every enabled user-owned bot **with decrypted tokens**.

        Consumed by the supervisor process inside the telegram-bot container
        on a periodic refresh — it diffs the result against its current set
        of polling tasks, starting new bots and tearing down removed ones.
        Returns an empty list if ``NYMERIA_SECRETS_KEY`` isn't configured
        (in which case no bots could have been registered anyway).
        """
        if not nymeria_secrets.has_secrets_key():
            return []
        repo = get_agent().chat_bindings_repo
        out: List[AdminTelegramBotResponse] = []
        for bot, ciphertext in repo.list_user_telegram_bots_with_ciphertext():
            try:
                token = nymeria_secrets.decrypt(ciphertext)
            except (
                nymeria_secrets.InvalidToken,
                nymeria_secrets.SecretsKeyMissing,
                nymeria_secrets.SecretsKeyInvalid,
            ) as e:
                # Token is unrecoverable (e.g. key was rotated without
                # re-encrypting). Skip this bot rather than 500-ing the
                # whole supervisor refresh.
                logger.error(
                    "Couldn't decrypt token for bot id=%s username=@%s: %s",
                    bot.id, bot.bot_username, e,
                )
                continue
            out.append(
                AdminTelegramBotResponse(
                    id=bot.id,
                    owner_user_id=bot.owner_user_id,
                    bot_username=bot.bot_username,
                    bot_token=token,
                    enabled=bot.enabled,
                    created_at=bot.created_at,
                    last_seen_at=bot.last_seen_at,
                )
            )
        return out

    @app.post("/admin/telegram-bots/{bot_id}/seen", tags=["Admin"])
    async def admin_telegram_bot_seen(
        bot_id: int,
        _admin=Depends(require_rate_limited_admin_bot_user),
    ):
        """Heartbeat ping from the supervisor after a successful poll cycle."""
        repo = get_agent().chat_bindings_repo
        if repo.get_user_telegram_bot(bot_id) is None:
            raise HTTPException(status_code=404, detail="Bot not found")
        repo.update_user_telegram_bot_seen(bot_id)
        return {"updated": True}

    @app.post(
        "/admin/platform/link-codes/claim",
        response_model=AdminPlatformLinkClaimResponse,
        tags=["Admin"],
    )
    async def admin_claim_platform_link_code(
        body: AdminPlatformLinkClaimRequest,
        _admin=Depends(require_rate_limited_admin_bot_user),
    ):
        """Atomically consume a platform-link code and link the platform user
        to the issuing Nymeria account. Called by the bot's
        ``/start link_<code>`` handler so non-admin users can self-service
        their initial Telegram-to-account link without an admin running
        ``users link-platform``.
        """
        repo = get_agent().accounts_repo
        bindings = get_agent().chat_bindings_repo
        # Phase 1: inspect (read-only) to get claim info for authorization.
        try:
            claim = bindings.inspect_bind_code(
                body.code, kind="platform_link", provider=body.provider
            )
        except BindCodeInvalid as e:
            raise HTTPException(status_code=400, detail=f"Invalid code: {e}")
        # Phase 2: authorize before consuming the code.
        existing = repo.resolve_platform(body.provider, body.platform_user_id)
        if existing is not None and existing != claim.user_id:
            raise HTTPException(
                status_code=409,
                detail=f"Platform identity already linked to user '{existing}'",
            )
        # Phase 3: link the platform before consuming the code, so a
        # failure (e.g. UserNotFound) leaves the code reusable.
        try:
            repo.link_platform(body.provider, body.platform_user_id, claim.user_id)
        except UserNotFound:
            raise HTTPException(status_code=404, detail="User not found")
        # Phase 4: consume the code now that everything succeeded.
        try:
            bindings.claim_bind_code(
                body.code, kind="platform_link", provider=body.provider
            )
        except BindCodeInvalid:
            pass  # bind code already consumed or expired, continue
        for p in repo.list_platforms_for_user(claim.user_id):
            if p.provider == body.provider and p.provider_user_id == body.platform_user_id:
                return AdminPlatformLinkClaimResponse(
                    user_id=claim.user_id,
                    provider=body.provider,
                    provider_user_id=body.platform_user_id,
                    created_at=p.created_at,
                )
        raise HTTPException(status_code=500, detail="Linked but not found")

    # ========================================================================
    # Self-service chat-app linking & per-thread chat bindings
    # ========================================================================

    @app.get(
        "/me/platforms",
        response_model=List[PlatformIdentityResponse],
        tags=["Auth"],
    )
    async def list_my_platforms(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List the current user's linked platform identities (Discord/Telegram/Twitch).

        Self-service equivalent of ``GET /admin/users/{id}/platforms`` — used by
        the desktop wizard to decide whether the user already has a Telegram
        identity linked, or whether step 1 of the wizard (link via deep code)
        is needed.
        """
        repo = get_agent().accounts_repo
        return [
            PlatformIdentityResponse(
                provider=p.provider,
                provider_user_id=p.provider_user_id,
                created_at=p.created_at,
            )
            for p in repo.list_platforms_for_user(user.id)
        ]

    @app.post(
        "/me/platform-link-codes",
        response_model=ChatAppBindCodeResponse,
        tags=["Auth"],
    )
    async def issue_my_platform_link_code(
        body: PlatformLinkCodeRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """Issue a short-lived code so the user can self-service-link their
        chat-app identity (e.g. Telegram user_id) to their Nymeria account.

        Flow: code is shown in the wizard, user taps the deep link or types
        ``/start link_<code>`` into the bot, the bot's ``/start`` handler
        claims the code (atomic) and writes the ``platform_identities`` row.
        Replaces the previously admin-only ``users link-platform`` CLI step.
        """
        repo = get_agent().chat_bindings_repo
        raw = repo.issue_bind_code(
            kind="platform_link",
            provider=body.provider,
            user_id=user.id,
            ttl_seconds=600,
        )
        # Round-trip: read back the row so the response carries the canonical
        # expiry timestamp (avoids drift from clock formatting).
        # We don't expose a get-by-hash repo method (codes are write-once and
        # consumed by the bot, not the API), so compute expiry locally.
        expires = (datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat(
            timespec="seconds"
        )
        return ChatAppBindCodeResponse(
            **_build_chatapp_link_payload(
                code=raw,
                expires_at=expires,
                provider=body.provider,
                kind="platform_link",
                settings=settings,
            )
        )

    @app.post(
        "/threads/{thread_id}/chatapp/bind-code",
        response_model=ChatAppBindCodeResponse,
        tags=["Threads"],
    )
    async def issue_thread_chatapp_bind_code(
        thread_id: str,
        body: ChatAppBindCodeRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """Issue a short-lived code the user types into the bot to bind a
        chat to this thread.

        Caller must own the thread. The wizard polls
        ``GET /threads/{id}/chatapp/bindings`` to detect when the bot has
        consumed the code and the binding is in place.
        """
        _require_thread_access(user, thread_id)
        # Don't pre-issue if a binding already exists — the user should
        # unbind first. Surfaces a clear 409 instead of a confusing
        # double-bind UX.
        repo = get_agent().chat_bindings_repo
        existing = repo.lookup_thread_binding_by_thread(body.provider, thread_id)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Thread already bound to {body.provider} chat {existing.platform_chat_id}",
            )
        raw = repo.issue_bind_code(
            kind="thread_bind",
            provider=body.provider,
            user_id=user.id,
            thread_id=thread_id,
            ttl_seconds=600,
        )
        expires = (datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat(
            timespec="seconds"
        )
        return ChatAppBindCodeResponse(
            **_build_chatapp_link_payload(
                code=raw,
                expires_at=expires,
                provider=body.provider,
                kind="thread_bind",
                settings=settings,
            )
        )

    @app.get(
        "/threads/{thread_id}/chatapp/bindings",
        response_model=List[ChatAppBindingResponse],
        tags=["Threads"],
    )
    async def list_thread_chatapp_bindings(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List chat-app bindings on this thread. Caller must own the thread."""
        _require_thread_access(user, thread_id)
        repo = get_agent().chat_bindings_repo
        return [
            ChatAppBindingResponse(
                id=b.id,
                thread_id=b.thread_id,
                provider=b.provider,
                platform_chat_id=b.platform_chat_id,
                created_at=b.created_at,
                user_telegram_bot_id=b.user_telegram_bot_id,
            )
            for b in repo.list_thread_bindings(thread_id)
        ]

    @app.delete(
        "/threads/{thread_id}/chatapp/bindings/{binding_id}",
        tags=["Threads"],
    )
    async def delete_thread_chatapp_binding(
        http_request: Request,
        thread_id: str,
        binding_id: int,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Unbind a chat from this thread. Caller must own the thread, and the
        binding row must belong to the caller (extra guard alongside thread
        ownership).
        """
        _require_thread_access(user, thread_id)
        repo = get_agent().chat_bindings_repo
        thread_bindings = repo.list_thread_bindings(thread_id)
        binding = next(
            (b for b in thread_bindings if b.id == binding_id and b.user_id == user.id),
            None,
        )
        ok = repo.delete_thread_binding(binding_id, user_id=user.id)
        if not ok:
            raise HTTPException(status_code=404, detail="Binding not found")
        if binding is not None:
            client_id = http_request.headers.get("x-nymeria-client-id", "")
            _publish_chatapp_platform_sync(
                binding.thread_id,
                binding.user_id,
                origin_client_id=client_id,
            )
        return {"unbound": True}

    # --- self-service BYO Telegram bots -----------------------------------

    @app.get(
        "/me/telegram-bots",
        response_model=List[MyTelegramBotResponse],
        tags=["Auth"],
    )
    async def list_my_telegram_bots(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List the current user's registered Telegram bots (no token material)."""
        repo = get_agent().chat_bindings_repo
        return [
            MyTelegramBotResponse(
                id=b.id,
                bot_username=b.bot_username,
                enabled=b.enabled,
                created_at=b.created_at,
                last_seen_at=b.last_seen_at,
            )
            for b in repo.list_user_telegram_bots(user.id)
        ]

    @app.get(
        "/me/telegram-bots/{bot_id}",
        response_model=MyTelegramBotResponse,
        tags=["Auth"],
    )
    async def get_my_telegram_bot(
        bot_id: int,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Single-bot fetch. The wizard polls this after registration to wait
        for the supervisor's first heartbeat (``last_seen_at`` becomes
        non-null) before showing the bind-code step — that way users don't
        try to ``/bind`` a bot that isn't online yet.
        """
        repo = get_agent().chat_bindings_repo
        bot = repo.get_user_telegram_bot(bot_id, owner_user_id=user.id)
        if bot is None:
            raise HTTPException(status_code=404, detail="Bot not found")
        return MyTelegramBotResponse(
            id=bot.id,
            bot_username=bot.bot_username,
            enabled=bot.enabled,
            created_at=bot.created_at,
            last_seen_at=bot.last_seen_at,
        )

    @app.post(
        "/me/telegram-bots",
        response_model=MyTelegramBotResponse,
        tags=["Auth"],
    )
    async def register_my_telegram_bot(
        body: RegisterTelegramBotRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Paste a BotFather token to register a user-owned Telegram bot.

        Validates the token via Telegram's ``getMe`` (cheap, no side effects),
        encrypts it with ``NYMERIA_SECRETS_KEY``, and persists the ciphertext.
        The supervisor process picks the new bot up on its next refresh tick
        and starts a polling loop for it. Idempotent: pasting a token that's
        already registered to the same user returns the existing record.
        """
        if not nymeria_secrets.has_secrets_key():
            raise HTTPException(
                status_code=503,
                detail=(
                    "NYMERIA_SECRETS_KEY is not configured on the server. "
                    "Add it to .env.docker (generate with `python3 -c \"from "
                    "cryptography.fernet import Fernet; print(Fernet."
                    "generate_key().decode())\"`) and restart the api "
                    "container before registering BYO bots."
                ),
            )
        token = (body.bot_token or "").strip()
        if not token:
            raise HTTPException(status_code=400, detail="bot_token is required")

        # Validate via Telegram's getMe before persisting anything.
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            try:
                resp = await client.get(
                    f"https://api.telegram.org/bot{token}/getMe"
                )
            except httpx.HTTPError as e:
                raise HTTPException(
                    status_code=502,
                    detail=f"Couldn't reach Telegram: {e}",
                )
        if resp.status_code != 200:
            # Surface Telegram's response body so the user sees the real
            # reason (typically "Unauthorized" for a wrong/revoked token).
            try:
                detail = resp.json().get("description") or resp.text[:200]
            except Exception:  # noqa: BLE001
                detail = resp.text[:200]
            raise HTTPException(
                status_code=400,
                detail=f"Telegram rejected the token: {detail}",
            )
        try:
            data = resp.json()
        except Exception:
            raise HTTPException(status_code=502, detail="Bad JSON from Telegram")
        if not data.get("ok"):
            raise HTTPException(
                status_code=400,
                detail=f"Token invalid: {data.get('description', 'unknown')}",
            )
        result = data.get("result") or {}
        bot_username = result.get("username")
        if not bot_username:
            raise HTTPException(
                status_code=400,
                detail="Telegram didn't return a username for this token",
            )

        try:
            ciphertext = nymeria_secrets.encrypt(token)
        except (
            nymeria_secrets.SecretsKeyMissing,
            nymeria_secrets.SecretsKeyInvalid,
        ) as e:
            raise HTTPException(status_code=503, detail=str(e))

        repo = get_agent().chat_bindings_repo
        try:
            bot = repo.register_user_telegram_bot(
                owner_user_id=user.id,
                bot_username=bot_username,
                bot_token_ciphertext=ciphertext,
            )
        except BotAlreadyRegistered:
            # Same username already exists — idempotent if it's this user's,
            # 409 if it belongs to someone else.
            existing = repo.get_user_telegram_bot_by_username(bot_username)
            if existing is None:
                raise HTTPException(
                    status_code=409,
                    detail=f"Bot @{bot_username} is already registered",
                )
            if existing.owner_user_id != user.id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Bot @{bot_username} is registered to a different "
                        "account. Use BotFather to revoke + reissue the token, "
                        "then paste the new token."
                    ),
                )
            bot = existing
        return MyTelegramBotResponse(
            id=bot.id,
            bot_username=bot.bot_username,
            enabled=bot.enabled,
            created_at=bot.created_at,
            last_seen_at=bot.last_seen_at,
        )

    @app.delete("/me/telegram-bots/{bot_id}", tags=["Auth"])
    async def delete_my_telegram_bot(
        bot_id: int,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Remove one of your registered bots. Cascade-deletes any bindings
        served by it (the binding's chat falls off the bot's reach when the
        bot stops polling). The actual polling loop is torn down on the
        supervisor's next refresh.
        """
        repo = get_agent().chat_bindings_repo
        affected_bindings = [
            b
            for b in repo.list_thread_bindings_for_user(user.id)
            if b.user_telegram_bot_id == bot_id
        ]
        ok = repo.delete_user_telegram_bot(bot_id, owner_user_id=user.id)
        if not ok:
            raise HTTPException(status_code=404, detail="Bot not found")
        for binding in affected_bindings:
            _publish_chatapp_platform_sync(binding.thread_id, binding.user_id)
        return {"deleted": True}

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

    @app.get("/threads/{thread_id}/history", response_model=ThreadHistoryResponse, tags=["Threads"])
    async def get_thread_history(
        thread_id: str,
        include_internal: bool = Query(
            False,
            description="Include internal system messages (autonomous wake-ups, compaction prompts)"
        ),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Get conversation history for a thread.

        Returns all messages in the conversation including tool calls and results.
        By default, internal system messages (autonomous wake-ups, compaction prompts)
        are filtered out. Set include_internal=true for debugging to see all messages.
        """
        _require_thread_access(user, thread_id)
        agent = get_agent()

        # Check per-thread config for visibility flags
        show_autonomous = False
        show_prompt_metadata = False
        if not include_internal:
            tc = agent.thread_config_manager.get_config(thread_id)
            if tc:
                if tc.show_autonomous_prompts:
                    show_autonomous = True
                if tc.show_prompt_metadata:
                    show_prompt_metadata = True

        history = agent.get_conversation_history(
            thread_id,
            include_internal=include_internal,
            show_autonomous_prompts=show_autonomous,
            show_prompt_metadata=show_prompt_metadata,
        )
        return ThreadHistoryResponse(thread_id=thread_id, messages=history)

    @app.get("/threads/{thread_id}/context", tags=["Threads"])
    async def get_thread_context_stats(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Get context window usage statistics for a thread.

        Returns token usage, context limit, and compaction history.
        """
        _require_thread_access(user, thread_id)
        agent = get_agent()
        stats = agent.get_context_stats(thread_id)
        # Include thread processing status so frontends can poll for completion
        lock_info = agent._thread_locks.get_lock_info(thread_id)
        if isinstance(stats, dict):
            stats["processing"] = lock_info is not None
        return stats

    @app.get("/threads/{thread_id}/metadata", tags=["Threads"])
    async def get_thread_metadata(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Get platform metadata for a thread.

        Parses the thread ID to detect platform origin (desktop, discord,
        telegram, slack) and returns relevant metadata.
        """
        _require_thread_access(user, thread_id)
        if thread_id.startswith("discord_dm_"):
            return {
                "platform": "discord",
                "type": "dm",
                "channel_id": thread_id[len("discord_dm_"):],
            }
        if thread_id.startswith("discord_"):
            parts = thread_id.split("_")
            return {
                "platform": "discord",
                "type": "guild",
                "guild_id": parts[1] if len(parts) >= 2 else None,
                "channel_id": parts[2] if len(parts) >= 3 else None,
            }
        if thread_id.startswith("telegram_"):
            return {
                "platform": "telegram",
                "channel_id": thread_id[len("telegram_"):],
            }
        if thread_id.startswith("slack_"):
            return {
                "platform": "slack",
                "channel_id": thread_id[len("slack_"):],
            }
        return {"platform": "desktop"}

    # =========================================================================
    # Thread Listing
    # =========================================================================

    def _get_checkpoint_thread_ids() -> list[str]:
        """Query distinct thread IDs from the checkpoint database."""
        settings = get_settings()
        thread_ids: list[str] = []

        if settings.database_backend == "sqlite":
            import sqlite3 as _sqlite3

            db_path = str(settings.db_path)
            try:
                conn = _sqlite3.connect(db_path)
                cursor = conn.execute("SELECT DISTINCT thread_id FROM checkpoints")
                thread_ids = [row[0] for row in cursor.fetchall()]
                conn.close()
            except Exception as e:
                logger.warning(f"Failed to query thread IDs from SQLite: {e}")

        elif settings.database_backend == "postgres":
            import psycopg  # type: ignore[import-untyped]

            try:
                with psycopg.connect(settings.postgres_uri) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT DISTINCT thread_id FROM checkpoints")
                        thread_ids = [row[0] for row in cur.fetchall()]
            except Exception as e:
                logger.warning(f"Failed to query thread IDs from PostgreSQL: {e}")

        return thread_ids

    def _add_thread_source(
        sources: dict[str, set[str]],
        thread_id: Optional[str],
        source: str,
    ) -> None:
        if not thread_id:
            return
        normalized = str(thread_id).strip()
        if not normalized:
            return
        sources.setdefault(normalized, set()).add(source)

    def _can_show_orphan_checkpoint_thread(
        user: AuthenticatedUser,
        user_id: str,
        thread_id: str,
    ) -> bool:
        """Return True when a checkpoint-only thread can be safely listed."""
        if user.role == "admin":
            return True
        if thread_id.startswith("telegram_") and not thread_id.startswith("telegram_-"):
            provider_user_id = thread_id[len("telegram_"):]
            return get_agent().accounts_repo.resolve_platform("telegram", provider_user_id) == user_id
        return False

    def _can_list_recovered_thread(
        user: AuthenticatedUser,
        user_id: str,
        thread_id: str,
    ) -> bool:
        """Return True when a recovered /threads row can be opened by caller.

        Recovery sources are advisory. Some old metadata rows can point at a
        thread now owned by another user; listing those rows creates sidebar
        zombies because detail routes correctly return 404. This mirrors
        _require_thread_access without claiming ownerless personal threads from
        a read-only list request.
        """
        owner = get_agent().accounts_repo.get_thread_owner(thread_id)
        if owner == user_id:
            return True
        if owner is not None:
            return user.role == "admin"
        if _is_shared_channel_thread(thread_id):
            return user.role == "admin" or user.via_act_as
        return True

    def _collect_recoverable_thread_sources(
        *,
        user: AuthenticatedUser,
        user_id: str,
        owned_ids: set[str],
        checkpoint_ids: list[str],
        metadata_thread_ids: set[str],
    ) -> dict[str, set[str]]:
        """
        Find thread IDs referenced by thread-bound resources that can wake,
        route, or explain a thread even if ordinary metadata/owner rows are
        missing. These are shown by GET /threads so the desktop can surface and
        delete old partially-deleted threads.
        """
        agent = get_agent()
        settings = get_settings()
        sources: dict[str, set[str]] = {}

        for tid in metadata_thread_ids:
            if tid not in owned_ids:
                _add_thread_source(sources, tid, "metadata")

        for tid in checkpoint_ids:
            if tid not in owned_ids and _can_show_orphan_checkpoint_thread(user, user_id, tid):
                _add_thread_source(sources, tid, "checkpoint")

        try:
            if user_id in agent.todo_manager.get_all_users_with_todos():
                todo_list = agent.todo_manager.get_todos(user_id)
                for item in todo_list.items:
                    _add_thread_source(sources, item.thread_id, "todo")
        except Exception as e:
            logger.warning("Failed to collect TODO thread references for %s: %s", user_id, e)

        try:
            schedule_db = getattr(agent, "_schedule_db", None)
            if schedule_db is not None:
                for entry in schedule_db.get_for_user(user_id):
                    _add_thread_source(sources, entry.thread_id, "scheduled_todo")
        except Exception as e:
            logger.warning("Failed to collect scheduled TODO thread references for %s: %s", user_id, e)

        try:
            from ..core.trigger_manager import TriggerManager

            manager = getattr(agent, "trigger_manager", None) or TriggerManager(settings.data_dir)
            for trigger in manager.get_triggers(user_id):
                _add_thread_source(sources, trigger.thread_id, "trigger")
        except Exception as e:
            logger.warning("Failed to collect trigger thread references for %s: %s", user_id, e)

        try:
            for binding in agent.chat_bindings_repo.list_thread_bindings_for_user(user_id):
                _add_thread_source(sources, binding.thread_id, "chat_binding")
        except Exception as e:
            logger.warning("Failed to collect chat binding thread references for %s: %s", user_id, e)

        try:
            for tid in agent.chat_bindings_repo.list_bind_code_thread_ids_for_user(user_id):
                _add_thread_source(sources, tid, "bind_code")
        except Exception as e:
            logger.warning("Failed to collect bind-code thread references for %s: %s", user_id, e)

        return sources

    def _thread_list_payload(
        thread_id: str,
        meta,
        *,
        recovered: bool = False,
        recovery_sources: Optional[set[str]] = None,
    ) -> dict:
        if meta:
            payload = meta.model_dump(mode="json")
        else:
            payload = {
                "thread_id": thread_id,
                "title": "Recovered thread" if recovered else "New Chat",
                "pinned": False,
                "platform": _classify_thread_platform_from_id(thread_id),
                "platform_meta": None,
                "created_at": None,
                "updated_at": None,
                "title_source": "recovered" if recovered else "default",
            }
        payload["platform"] = _thread_list_platform(thread_id, meta)
        thread_config_manager = getattr(get_agent(), "thread_config_manager", None)
        tc = (
            thread_config_manager.get_config(thread_id)
            if thread_config_manager is not None
            else None
        )
        is_callable = bool(tc and tc.callable)
        payload["callable"] = is_callable
        if is_callable:
            if tc.callable_name:
                payload["title"] = tc.callable_name
                payload["title_source"] = "callable"
        payload["recovered"] = recovered
        payload["recovery_sources"] = sorted(recovery_sources or [])
        return payload

    @app.get("/threads", tags=["Threads"])
    async def list_threads(
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        List all threads with metadata (titles, pins, platform info).

        Merges thread IDs from the checkpoint database, stored metadata, and
        thread-bound resources so all surfaces see the same thread list. The
        resource pass intentionally surfaces old partially-deleted threads so
        the desktop can show and delete them instead of hiding wake-up paths.
        """
        agent = get_agent()
        # Restrict to threads owned by the authenticated user. Admins can see
        # any user's threads by act-as'ing as that user (X-Nymeria-Act-As);
        # no universal "all threads" view, which is intentional.
        owned_ids = set(agent.accounts_repo.list_threads_for_user(user_id))
        all_checkpoint_ids = _get_checkpoint_thread_ids()
        checkpoint_ids = [t for t in all_checkpoint_ids if t in owned_ids]
        checkpoint_set = set(checkpoint_ids)

        # Get stored metadata
        store = agent.thread_metadata_manager.get_store(user_id)
        recovery_sources = _collect_recoverable_thread_sources(
            user=user,
            user_id=user_id,
            owned_ids=owned_ids,
            checkpoint_ids=all_checkpoint_ids,
            metadata_thread_ids=set(store.threads),
        )

        threads = []
        seen: set[str] = set()

        # 1. Threads in checkpoints (with metadata if available)
        for tid in checkpoint_ids:
            meta = store.threads.get(tid)
            threads.append(_thread_list_payload(tid, meta))
            seen.add(tid)

        # 2. Metadata-only threads owned by this user but without checkpoints yet
        for tid, meta in store.threads.items():
            if tid in owned_ids and tid not in checkpoint_set:
                threads.append(_thread_list_payload(tid, meta))
                seen.add(tid)

        # 3. Recoverable resource-only/orphaned threads. These are the
        # "zombie" cases: a thread-bound resource survived while the normal
        # metadata/owner/checkpoint path is incomplete.
        for tid in sorted(recovery_sources):
            if tid in seen:
                continue
            if not _can_list_recovered_thread(user, user_id, tid):
                continue
            threads.append(
                _thread_list_payload(
                    tid,
                    store.threads.get(tid),
                    recovered=True,
                    recovery_sources=recovery_sources[tid],
                )
            )
            seen.add(tid)

        return {"threads": threads, "total": len(threads)}

    # -- Thread metadata endpoints --

    class ThreadMetadataUpdateRequest(BaseModel):
        title: Optional[str] = Field(default=None, max_length=200)
        pinned: Optional[bool] = None

    @app.patch("/threads/{thread_id}/metadata", tags=["Threads"])
    async def update_thread_metadata(
        http_request: Request,
        thread_id: str,
        request: ThreadMetadataUpdateRequest,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Update thread metadata (title, pin status)."""
        _require_thread_access(user, thread_id)
        agent = get_agent()
        fields: Dict[str, Any] = {}
        title_source = None

        if request.title is not None:
            fields["title"] = request.title.strip()
            title_source = "user"
        if request.pinned is not None:
            fields["pinned"] = request.pinned

        if title_source:
            fields["title_source"] = title_source

        # If renaming a callable thread, sync title → callable_name. Reject
        # titles that aren't valid LLM tool names (spaces/dots/punctuation)
        # so the rename doesn't poison the registry — same constraint POST
        # /agents/threads applies. The user can rename via the metadata
        # endpoint OR keep the title display-friendly and the callable_name
        # separate via PATCH /threads/{id}/config.
        if request.title is not None:
            tc = agent.thread_config_manager.get_config(thread_id)
            if tc and tc.callable:
                new_name = request.title.strip()
                if not new_name:
                    raise HTTPException(
                        status_code=400,
                        detail="Cannot rename callable thread to empty title",
                    )
                _validate_callable_name(new_name)
                from ..tools import ALL_TOOLS
                core_tool_names = {t.name for t in ALL_TOOLS}
                if new_name in core_tool_names:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Callable name '{new_name}' conflicts with a core tool name",
                    )
                owned = set(agent.accounts_repo.list_threads_for_user(user_id))
                existing = agent.thread_config_manager.get_callable_thread_by_name(
                    new_name, owned_thread_ids=owned
                )
                if existing is not None and existing.thread_id != thread_id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Callable name '{new_name}' is already used by thread {existing.thread_id}",
                    )
                tc.callable_name = new_name
                agent.thread_config_manager.save_config(tc)
                agent.invalidate_thread_config_cache(thread_id)
                agent.sync_agent_tools()
                # Override title_source to "callable" for callable threads
                fields["title_source"] = "callable"

        meta = agent.thread_metadata_manager.upsert_thread(
            user_id, thread_id, **fields
        )

        # Publish sync event so other clients see the metadata change
        client_id = http_request.headers.get("x-nymeria-client-id", "")
        sync_data: Dict[str, Any] = {}
        if request.title is not None:
            sync_data["title"] = fields.get("title", request.title.strip())
            sync_data["title_source"] = fields.get("title_source", "user")
        if request.pinned is not None:
            sync_data["pinned"] = request.pinned
        if sync_data:
            publish_sync_event(
                event_type="thread_updated",
                thread_id=thread_id,
                user_id=user_id,
                data=sync_data,
                origin_client_id=client_id,
            )

        return meta.model_dump(mode="json")

    class ThreadMetadataMigrateRequest(BaseModel):
        threads: List[dict] = Field(default_factory=list)

    @app.post("/threads/metadata/migrate", tags=["Threads"])
    async def migrate_thread_metadata(
        request: ThreadMetadataMigrateRequest,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        One-time migration: import thread metadata from frontend localStorage.

        Accepts the frontend's Thread[] format and imports into the backend
        metadata store. Only imports threads that don't already have metadata.
        """
        agent = get_agent()
        count = agent.thread_metadata_manager.migrate_from_frontend(
            user_id, request.threads
        )
        return {"migrated_threads": count}

    @app.post("/threads/{thread_id}/claim", tags=["Threads"])
    async def claim_thread_endpoint(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Eagerly claim ownership of a thread for the calling user.

        Used by the desktop frontend after locally generating a UUID for a
        new thread, so the thread_owners row exists before any chat-app
        binding (Telegram/Discord) routes a message into it. Without this,
        the first non-admin caller to hit /chat for the UUID would TOFU-claim
        and silently transfer ownership.

        Idempotent. Honors ``X-Nymeria-Act-As`` like all thread routes.

        - 400 if the thread id matches a shared-channel pattern
          (``discord_<g>_<c>``, ``telegram_-<id>``, ``twitch_<c>``) — these
          are inherently multi-user and not claimable per-user.
        - 200 ``{thread_id, owner}`` if the caller is the owner (fresh claim
          or already-owned-by-self), or if the caller is admin (admin always
          sees the truth even when someone else owns it).
        - 404 for non-admin callers when another user owns the thread.
          Mirrors the leak surface of ``_require_thread_access`` so callers
          can't probe for thread existence under other users.
        """
        if _is_shared_channel_thread(thread_id):
            raise HTTPException(
                status_code=400,
                detail="Shared-channel threads cannot be claimed",
            )
        agent = get_agent()
        owner = agent.accounts_repo.claim_thread(thread_id, user.id)
        if owner != user.id and user.role != "admin":
            raise HTTPException(status_code=404, detail="Not found")
        return {"thread_id": thread_id, "owner": owner}

    @app.delete("/threads/{thread_id}", tags=["Threads"])
    async def delete_thread(
        http_request: Request,
        thread_id: str,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Fully delete a thread and all resources that can recreate it.

        This is the proper way to remove a thread from all surfaces. It
        cascades into checkpoints, metadata, config, notepad, RAG chunks,
        TODOs/schedule rows, triggers, chat bindings, bind codes, owner rows,
        activity entries, notifications, and device thread filters.
        """
        _require_thread_access(user, thread_id)
        agent = get_agent()
        settings = get_settings()
        try:
            deletion = cascade_delete_thread(agent, settings, user_id, thread_id)
        except ThreadDeletionBusy as e:
            raise HTTPException(status_code=409, detail=str(e))
        except Exception as e:
            logger.error(f"Thread {thread_id} deletion failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))

        logger.info(
            "Thread %s fully deleted: %s",
            thread_id,
            deletion.deleted,
        )

        # Publish sync event so other clients remove the thread
        client_id = http_request.headers.get("x-nymeria-client-id", "")
        publish_sync_event(
            event_type="thread_deleted",
            thread_id=thread_id,
            user_id=user_id,
            data={},
            origin_client_id=client_id,
        )

        return deletion.model_dump()

    @app.post("/threads/{thread_id}/clear", tags=["Threads"])
    async def clear_thread(
        http_request: Request,
        thread_id: str,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Clear conversation history for a thread (checkpoints only).

        Preserves thread config (tools, instructions, model overrides),
        notepad content, and metadata. Use DELETE /threads/{id} to
        remove everything.
        """
        _require_thread_access(user, thread_id)
        agent = get_agent()
        settings = get_settings()

        # 0. Flush messages to RAG before destroying state — defensive backup
        # of the per-turn indexer. Catches anything missed (tool-heavy turns,
        # pre-fix history). Failures must not block the clear.
        try:
            config = {"configurable": {"thread_id": thread_id}}
            state = await agent._default_async_graph.aget_state(config)
            messages = state.values.get("messages", [])
            if messages:
                agent._flush_memories_before_trim(user_id, thread_id, messages)
        except Exception as e:
            logger.warning(f"Pre-clear RAG flush failed for {thread_id}: {e}")

        # 1. Delete metadata
        agent.thread_metadata_manager.delete_thread(user_id, thread_id)

        # 2. Delete checkpoints (messages, tool calls, thinking blocks)
        try:
            delete_thread_checkpoints(settings, thread_id)
        except Exception as e:
            logger.warning(f"Failed to delete checkpoints for {thread_id}: {e}")

        logger.info(f"Thread {thread_id} conversation cleared (config + notepad preserved)")

        # Publish sync event so other clients refresh
        client_id = http_request.headers.get("x-nymeria-client-id", "")
        publish_sync_event(
            event_type="thread_cleared",
            thread_id=thread_id,
            user_id=user_id,
            data={},
            origin_client_id=client_id,
        )

        return {"status": "ok", "thread_id": thread_id}

    # =========================================================================
    # Thread Configuration
    # =========================================================================

    class ThreadLLMConfigRequest(BaseModel):
        provider: Optional[str] = None
        model: Optional[str] = None
        temperature: Optional[float] = None
        max_tokens: Optional[int] = None
        extended_thinking: Optional[bool] = None
        reasoning_effort: Optional[str] = None
        use_model_defaults: Optional[bool] = None
        openai_api_mode: Optional[Literal["chat_completions", "responses"]] = None
        base_url: Optional[str] = None  # "" = direct API (no proxy), None = inherit global
        api_key: Optional[str] = None  # per-thread key; None = inherit env/global

    class ThreadConfigUpdateRequest(BaseModel):
        instructions: Optional[str] = Field(default=None, max_length=5000)
        disabled_tools: Optional[List[str]] = None
        enabled_tools: Optional[List[str]] = None
        enabled_skills: Optional[List[str]] = None
        disabled_skills: Optional[List[str]] = None
        llm_config: Optional[ThreadLLMConfigRequest] = None
        system_prompt: Optional[str] = Field(default=None, max_length=50000)
        callable: Optional[bool] = None
        callable_name: Optional[str] = Field(default=None, max_length=64)
        callable_description: Optional[str] = Field(default=None, max_length=500)
        callable_max_iterations: Optional[int] = Field(default=None, ge=1, le=1000)
        callable_team_id: Optional[str] = Field(default=None, max_length=120)
        callable_team_name: Optional[str] = Field(default=None, max_length=120)
        inject_todos_in_prompt: Optional[bool] = None
        show_autonomous_prompts: Optional[bool] = None
        show_prompt_metadata: Optional[bool] = None
        telegram_autonomous_delivery: Optional[Literal["full", "notify_only", "off"]] = None
        in_app_notification_level: Optional[Literal["notify_only", "all_autonomous", "off"]] = None
        clear_instructions: bool = False
        clear_disabled_tools: bool = False
        clear_enabled_tools: bool = False
        clear_enabled_skills: bool = False
        clear_disabled_skills: bool = False
        clear_llm_config: bool = False
        clear_system_prompt: bool = False

    @app.get("/threads/{thread_id}/config", tags=["Threads"])
    async def get_thread_config(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Get per-thread configuration (returns defaults if none saved)."""
        _require_thread_access(user, thread_id)
        agent = get_agent()
        tc = agent.thread_config_manager.get_config(thread_id)
        if tc:
            result = tc.model_dump(mode="json")
            result["has_customizations"] = tc.has_customizations()
            return result
        # Return empty default
        return {
            "thread_id": thread_id,
            "instructions": None,
            "disabled_tools": [],
            "enabled_tools": [],
            "llm_config": None,
            "system_prompt": None,
            "callable": False,
            "callable_name": None,
            "callable_description": None,
            "callable_max_iterations": None,
            "callable_team_id": None,
            "callable_team_name": None,
            "inject_todos_in_prompt": False,
            "show_autonomous_prompts": False,
            "show_prompt_metadata": False,
            "telegram_autonomous_delivery": "full",
            "in_app_notification_level": "notify_only",
            "created_at": None,
            "updated_at": None,
            "has_customizations": False,
        }

    @app.patch("/threads/{thread_id}/config", tags=["Threads"])
    async def update_thread_config(
        thread_id: str,
        request: ThreadConfigUpdateRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Update per-thread configuration (partial update)."""
        _require_thread_access(user, thread_id)
        from ..core.thread_config import ThreadConfig, ThreadLLMConfig

        agent = get_agent()
        tc = agent.thread_config_manager.get_config(thread_id)

        if tc is None:
            tc = ThreadConfig(thread_id=thread_id)

        # Apply clears first
        if request.clear_instructions:
            tc.instructions = None
        if request.clear_disabled_tools:
            tc.disabled_tools = []
        if request.clear_enabled_tools:
            tc.enabled_tools = []
        if request.clear_enabled_skills:
            tc.enabled_skills = []
        if request.clear_disabled_skills:
            tc.disabled_skills = []
        if request.clear_llm_config:
            tc.llm_config = None
        if request.clear_system_prompt:
            tc.system_prompt = None

        # Apply updates
        if request.instructions is not None and not request.clear_instructions:
            tc.instructions = request.instructions
        if request.disabled_tools is not None and not request.clear_disabled_tools:
            tc.disabled_tools = request.disabled_tools
        if request.enabled_tools is not None and not request.clear_enabled_tools:
            # Admin-only optional tools (self-modify, runtime-admin reload) are
            # equivalent to authenticated RCE on the shared backend — a
            # non-admin must not be able to enable them via thread config.
            if user.role != "admin":
                from ..tools import (
                    ADMIN_ONLY_OPTIONAL_TOOL_NAMES,
                    DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES,
                )
                blocked = ADMIN_ONLY_OPTIONAL_TOOL_NAMES.intersection(request.enabled_tools)
                if blocked:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Admin-only tools cannot be enabled by this user: {sorted(blocked)}",
                    )
                blocked = DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES.intersection(request.enabled_tools)
                if blocked:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Developer-only diagnostic tools cannot be enabled by this user: {sorted(blocked)}",
                    )
            tc.enabled_tools = request.enabled_tools
        if request.enabled_skills is not None and not request.clear_enabled_skills:
            tc.enabled_skills = request.enabled_skills
        if request.disabled_skills is not None and not request.clear_disabled_skills:
            tc.disabled_skills = request.disabled_skills
        if request.llm_config is not None and not request.clear_llm_config:
            # Use exclude_unset to distinguish "not sent" from "explicitly set to null"
            llm_data = request.llm_config.model_dump(exclude_unset=True)
            if tc.llm_config is None:
                # For new configs, filter out None values (no field to clear)
                tc.llm_config = ThreadLLMConfig(**{k: v for k, v in llm_data.items() if v is not None})
            else:
                for key, value in llm_data.items():
                    setattr(tc.llm_config, key, value)
        if request.system_prompt is not None and not request.clear_system_prompt:
            tc.system_prompt = request.system_prompt
        if request.callable is not None:
            tc.callable = request.callable
            # Require callable_name when enabling callable
            if request.callable and not (request.callable_name or tc.callable_name):
                raise HTTPException(
                    status_code=400,
                    detail="callable_name is required when enabling callable",
                )
        if request.callable_name is not None:
            # Validate callable_name doesn't collide with core tool names
            if request.callable_name:
                _validate_callable_name(request.callable_name)
                from ..tools import ALL_TOOLS
                core_tool_names = {t.name for t in ALL_TOOLS}
                if request.callable_name in core_tool_names:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Callable name '{request.callable_name}' conflicts with a core tool name",
                    )
                # Check for duplicate callable_name within this user's own
                # callables. Cross-user collisions are fine: callable threads
                # are scoped per-user at invocation time.
                owned = set(agent.accounts_repo.list_threads_for_user(user.id))
                existing = agent.thread_config_manager.get_callable_thread_by_name(
                    request.callable_name, owned_thread_ids=owned
                )
                if existing and existing.thread_id != thread_id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Callable name '{request.callable_name}' is already used by thread {existing.thread_id}",
                    )
            tc.callable_name = request.callable_name
        if request.callable_description is not None:
            tc.callable_description = request.callable_description
        if request.callable_max_iterations is not None:
            tc.callable_max_iterations = request.callable_max_iterations
        if request.callable_team_id is not None:
            tc.callable_team_id = request.callable_team_id
        if request.callable_team_name is not None:
            tc.callable_team_name = request.callable_team_name
        if request.inject_todos_in_prompt is not None:
            tc.inject_todos_in_prompt = request.inject_todos_in_prompt
        if request.show_autonomous_prompts is not None:
            tc.show_autonomous_prompts = request.show_autonomous_prompts
        if request.show_prompt_metadata is not None:
            tc.show_prompt_metadata = request.show_prompt_metadata
        if request.telegram_autonomous_delivery is not None:
            tc.telegram_autonomous_delivery = request.telegram_autonomous_delivery
        if request.in_app_notification_level is not None:
            tc.in_app_notification_level = request.in_app_notification_level

        if not agent.thread_config_manager.save_config(tc):
            raise HTTPException(status_code=500, detail="Failed to save thread config")

        agent.invalidate_thread_config_cache(thread_id)
        if request.callable_team_id is not None or request.callable_team_name is not None:
            for owned_thread_id in agent.accounts_repo.list_threads_for_user(user.id):
                agent.invalidate_thread_config_cache(owned_thread_id)
            agent.invalidate_thread_config_cache("")

        # If this is an agent thread config change, rebuild agent tools
        if (
            request.callable is not None
            or request.callable_name is not None
            or request.callable_description is not None
            or request.callable_max_iterations is not None
        ):
            agent.sync_agent_tools()

        # Sync callable thread metadata (title = callable_name) under the
        # caller — _require_thread_access above already proved this user owns
        # the thread (or is admin acting-as the owner), so user.id is the
        # right partition for the metadata store.
        if tc.callable and tc.callable_name:
            agent.thread_metadata_manager.upsert_thread(
                user.id, thread_id,
                title=tc.callable_name,
                title_source="callable",
                platform="callable",
            )

        result = tc.model_dump(mode="json")
        result["has_customizations"] = tc.has_customizations()
        return result

    @app.delete("/threads/{thread_id}/config", tags=["Threads"])
    async def delete_thread_config(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Reset thread to global defaults (delete custom config)."""
        _require_thread_access(user, thread_id)
        agent = get_agent()
        # Check if this was an agent thread before deleting
        tc = agent.thread_config_manager.get_config(thread_id)
        was_agent = tc.callable if tc else False
        agent.thread_config_manager.delete_config(thread_id)
        agent.invalidate_thread_config_cache(thread_id)
        # If it was an agent thread, rebuild tool registry to remove the stale tool
        if was_agent:
            agent.sync_agent_tools()
        return {"status": "ok", "thread_id": thread_id}

    # =========================================================================
    # Callable Thread Teams
    # =========================================================================

    class ThreadTeamCreateRequest(BaseModel):
        name: str = Field(..., min_length=1, max_length=120)
        thread_ids: List[str] = Field(default_factory=list)

    class ThreadTeamUpdateRequest(BaseModel):
        name: Optional[str] = Field(default=None, max_length=120)
        thread_ids: Optional[List[str]] = None

    def _serialize_thread_teams(agent, user_id: str) -> Dict[str, Any]:
        owned = list(agent.accounts_repo.list_threads_for_user(user_id))
        teams: Dict[str, Dict[str, Any]] = {}
        for thread_id in owned:
            tc = agent.thread_config_manager.get_config(thread_id)
            if not (tc and tc.callable_team_id):
                continue
            team_id = tc.callable_team_id
            team = teams.setdefault(
                team_id,
                {
                    "id": team_id,
                    "name": tc.callable_team_name or team_id,
                    "thread_ids": [],
                },
            )
            team["thread_ids"].append(thread_id)
            if tc.callable_team_name:
                team["name"] = tc.callable_team_name

        result = sorted(teams.values(), key=lambda t: str(t["name"]).lower())
        return {"teams": result, "total": len(result)}

    def _get_thread_team(agent, user_id: str, team_id: str) -> Optional[Dict[str, Any]]:
        for team in _serialize_thread_teams(agent, user_id)["teams"]:
            if team["id"] == team_id:
                return team
        return None

    def _thread_team_name_exists(
        agent,
        user_id: str,
        name: str,
        *,
        excluding_team_id: Optional[str] = None,
    ) -> bool:
        needle = name.strip().lower()
        for team in _serialize_thread_teams(agent, user_id)["teams"]:
            if excluding_team_id and team["id"] == excluding_team_id:
                continue
            if str(team["name"]).strip().lower() == needle:
                return True
        return False

    def _require_team_thread_ids(user: AuthenticatedUser, thread_ids: List[str]) -> List[str]:
        seen: set[str] = set()
        clean: List[str] = []
        for raw_id in thread_ids:
            thread_id = str(raw_id or "").strip()
            if not thread_id or thread_id in seen:
                continue
            _require_thread_access(user, thread_id)
            clean.append(thread_id)
            seen.add(thread_id)
        if not clean:
            raise HTTPException(status_code=400, detail="At least one thread is required")
        return clean

    def _save_thread_team_membership(
        agent,
        thread_id: str,
        *,
        team_id: Optional[str],
        team_name: Optional[str],
    ) -> None:
        from ..core.thread_config import ThreadConfig

        tc = agent.thread_config_manager.get_config(thread_id)
        if tc is None:
            tc = ThreadConfig(thread_id=thread_id)
        tc.callable_team_id = team_id
        tc.callable_team_name = team_name
        if not agent.thread_config_manager.save_config(tc):
            raise HTTPException(
                status_code=500,
                detail=f"Failed to save team membership for thread {thread_id}",
            )

    def _invalidate_user_team_graphs(agent, user_id: str) -> None:
        for owned_thread_id in agent.accounts_repo.list_threads_for_user(user_id):
            agent.invalidate_thread_config_cache(owned_thread_id)
        # Also clear per-user no-custom sentinel graphs, whose key uses "".
        agent.invalidate_thread_config_cache("")

    @app.get("/thread-teams", tags=["Threads"])
    async def list_thread_teams(user: AuthenticatedUser = Depends(verify_api_key)):
        """List callable visibility teams for the authenticated user's threads."""
        agent = get_agent()
        return _serialize_thread_teams(agent, user.id)

    @app.post("/thread-teams", tags=["Threads"])
    async def create_thread_team(
        request: ThreadTeamCreateRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Create a callable team and move the requested threads into it."""
        agent = get_agent()
        name = _normalize_thread_team_name(request.name)
        if _thread_team_name_exists(agent, user.id, name):
            raise HTTPException(status_code=409, detail=f"Thread team '{name}' already exists")
        thread_ids = _require_team_thread_ids(user, request.thread_ids)
        team_id = _make_thread_team_id(name)
        for thread_id in thread_ids:
            _save_thread_team_membership(
                agent,
                thread_id,
                team_id=team_id,
                team_name=name,
            )
        _invalidate_user_team_graphs(agent, user.id)
        team = _get_thread_team(agent, user.id, team_id)
        return team or {"id": team_id, "name": name, "thread_ids": thread_ids}

    @app.patch("/thread-teams/{team_id}", tags=["Threads"])
    async def update_thread_team(
        team_id: str,
        request: ThreadTeamUpdateRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Rename a callable team and/or replace its thread membership."""
        agent = get_agent()
        existing = _get_thread_team(agent, user.id, team_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Thread team not found")

        name = str(existing["name"])
        if request.name is not None:
            name = _normalize_thread_team_name(request.name)
            if _thread_team_name_exists(agent, user.id, name, excluding_team_id=team_id):
                raise HTTPException(status_code=409, detail=f"Thread team '{name}' already exists")

        if request.thread_ids is None:
            thread_ids = list(existing["thread_ids"])
        else:
            thread_ids = _require_team_thread_ids(user, request.thread_ids)

        old_ids = set(existing["thread_ids"])
        new_ids = set(thread_ids)
        for thread_id in sorted(old_ids - new_ids):
            _save_thread_team_membership(
                agent,
                thread_id,
                team_id=None,
                team_name=None,
            )
        for thread_id in thread_ids:
            _save_thread_team_membership(
                agent,
                thread_id,
                team_id=team_id,
                team_name=name,
            )

        _invalidate_user_team_graphs(agent, user.id)
        team = _get_thread_team(agent, user.id, team_id)
        return team or {"id": team_id, "name": name, "thread_ids": thread_ids}

    @app.delete("/thread-teams/{team_id}", tags=["Threads"])
    async def delete_thread_team(
        team_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Delete a callable team by clearing membership from its threads."""
        agent = get_agent()
        existing = _get_thread_team(agent, user.id, team_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Thread team not found")
        for thread_id in existing["thread_ids"]:
            _save_thread_team_membership(
                agent,
                thread_id,
                team_id=None,
                team_name=None,
            )
        _invalidate_user_team_graphs(agent, user.id)
        return {"status": "ok", "team_id": team_id}

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
