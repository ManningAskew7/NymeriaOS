"""FastAPI REST API trigger with SSE streaming for Nymeria."""

import asyncio
import json
import logging
import math
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.user_profile import ToolPreferences

import httpx
from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response, FileResponse
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
from ..core.activity_log import ActivityLog, ActivityEntry, ActivityType, get_activity_log, log_activity
from ..core.event_bus import (
    AutonomousEvent,
    get_event_bus,
    publish_agent_stream_chunk,
    publish_autonomous_event,
    publish_sync_event,
    should_log_stream_event_sample,
)
from ..core.notification_dispatch import (
    create_autonomous_notification as _dispatch_autonomous_notification,
    should_notify_autonomous as _should_notify_autonomous,
)
from ..core.notifications import NotificationStore, Notification, create_notification, get_notification_store
from ..core.rate_limit import SlidingWindowRateLimiter
from ..core.todo_manager import TodoManager, TodoItem, TodoStatus
from ..core.thread_deletion import ThreadDeletionBusy, cascade_delete_thread
from ..tools import ALL_TOOLS, get_all_tools_with_agents
from ..tools.definitions.custom_tool_schema import (
    CustomToolDefinition,
    HTTPToolConfig,
    ToolParameter,
)
from ..tools.definitions.mcp_schema import (
    MCPToolConfig,
)
from ..core.custom_tools import (
    get_custom_tool_loader,
    load_custom_tools,
    reload_custom_tools,
)
from ..api.routers.devices import create_devices_router
from ..api.routers.memory import create_memory_router
from ..api.routers.rag import create_rag_router
from ..api.routers.system import router as system_router
from ..api.routers.user_tools import create_user_tools_router
from ..api.routers.voice import create_voice_router
from ..api.routers.workspace import create_workspace_router
from ..api.schemas.system import HealthResponse

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


class FileData(BaseModel):
    """Generic file attachment data for multimodal messages."""

    file_type: str = Field(..., description="File type: 'image' or 'document'")
    data_url: str = Field(..., description="Base64 data URL (data:mime/type;base64,...)")
    mime_type: str = Field(..., description="MIME type (image/jpeg, application/pdf, etc.)")
    file_name: Optional[str] = Field(
        default=None,
        description="Original filename (used for MIME fallback when browser MIME type is missing)",
    )


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


class AttachmentValidationRequest(BaseModel):
    """Request model for attachment preflight validation."""

    attachments: List[FileData] = Field(
        default_factory=list,
        description="Attachments to validate against the effective thread model",
    )


class AttachmentValidationResponse(BaseModel):
    """Response model for attachment preflight validation."""

    compatible: bool
    effective_provider: str
    effective_model: str
    model_input_modalities: List[str]
    required_modalities: List[str]
    unsupported_modalities: List[str]
    warnings: List[str]
    can_force_send: bool = True


class ChatResponse(BaseModel):
    """Response model for non-streaming chat."""

    response: str = Field(..., description="Agent response")
    thread_id: str = Field(..., description="Conversation thread ID")
    tool_call_count: int = Field(default=0, description="Number of tool calls made in this turn")


class ReportRequest(BaseModel):
    """Request model for error report endpoint."""

    thread_id: Optional[str] = None
    message_id: str = ""
    description: str = ""
    messages: List[dict] = Field(default_factory=list)
    timestamp: str = ""
    client_info: dict = Field(default_factory=dict)


class ThreadHistoryResponse(BaseModel):
    """Response model for conversation history."""

    thread_id: str
    messages: list


class ServerSettingsResponse(BaseModel):
    """Response model for server settings."""

    llm_provider: str
    llm_model: str
    llm_temperature: float
    llm_max_tokens: Optional[int] = None
    llm_top_p: Optional[float] = None
    llm_top_k: Optional[int] = None
    llm_frequency_penalty: Optional[float] = None
    llm_presence_penalty: Optional[float] = None
    llm_reasoning_effort: Optional[str] = None
    llm_extended_thinking: bool = False
    llm_use_model_defaults: bool = False
    llm_base_url: Optional[str] = None
    openai_api_mode: Optional[Literal["chat_completions", "responses"]] = "responses"
    llm_stream_max_retries: int
    llm_stream_retry_initial_delay: float
    llm_stream_retry_max_delay: float
    # Context management settings
    context_management: str
    compact_threshold: float
    compact_keep_messages: int
    compact_model: Optional[str] = None
    sliding_window_cycles: int
    tool_output_max_chars: int
    log_level: str
    watchdog_enabled: bool
    watchdog_interval_minutes: int
    todo_staleness_minutes: int
    activity_retention_hours: int
    # Voice settings
    tts_provider: str = "none"
    tts_base_url: Optional[str] = None
    tts_model: str = "tts-1-hd"
    tts_voice: str = "nova"
    tts_output_format: str = "mp3"
    tts_speed: float = 1.0
    stt_provider: str = "none"
    stt_base_url: Optional[str] = None
    stt_model: str = "gpt-4o-mini-transcribe"
    stt_language: Optional[str] = None
    voice_default_thread_id: Optional[str] = None


class ServerSettingsUpdate(BaseModel):
    """Request model for updating server settings."""

    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    llm_temperature: Optional[float] = None
    llm_max_tokens: Optional[int] = None
    llm_top_p: Optional[float] = None
    llm_top_k: Optional[int] = None
    llm_frequency_penalty: Optional[float] = None
    llm_presence_penalty: Optional[float] = None
    llm_reasoning_effort: Optional[str] = None
    llm_extended_thinking: Optional[bool] = None
    llm_use_model_defaults: Optional[bool] = None
    llm_base_url: Optional[str] = None
    openai_api_mode: Optional[Literal["chat_completions", "responses"]] = None
    llm_stream_max_retries: Optional[int] = None
    llm_stream_retry_initial_delay: Optional[float] = None
    llm_stream_retry_max_delay: Optional[float] = None
    # Context management settings
    context_management: Optional[str] = None
    compact_threshold: Optional[float] = None
    compact_keep_messages: Optional[int] = None
    compact_model: Optional[str] = None
    sliding_window_cycles: Optional[int] = None
    tool_output_max_chars: Optional[int] = None
    log_level: Optional[str] = None
    watchdog_enabled: Optional[bool] = None
    watchdog_interval_minutes: Optional[int] = None
    todo_staleness_minutes: Optional[int] = None
    activity_retention_hours: Optional[int] = None
    # Voice settings
    tts_provider: Optional[str] = None
    tts_base_url: Optional[str] = None
    tts_model: Optional[str] = None
    tts_voice: Optional[str] = None
    tts_output_format: Optional[str] = None
    tts_speed: Optional[float] = None
    stt_provider: Optional[str] = None
    stt_base_url: Optional[str] = None
    stt_model: Optional[str] = None
    stt_language: Optional[str] = None
    voice_default_thread_id: Optional[str] = None


class OpenRouterKeyDiagnostics(BaseModel):
    """Runtime details for the currently active OpenRouter API key."""

    label: Optional[str] = None
    limit: Optional[float] = None
    limit_remaining: Optional[float] = None
    usage: Optional[float] = None
    limit_reset: Optional[str] = None
    include_byok_in_limit: Optional[bool] = None
    is_management_key: Optional[bool] = None
    fetch_error: Optional[str] = None


class LLMRuntimeDiagnosticsResponse(BaseModel):
    """Runtime diagnostics for currently active LLM configuration."""

    provider: str
    model: str
    llm_max_tokens: Optional[int] = None
    effective_max_tokens: Optional[int] = None
    source_env_files: List[str] = []
    openrouter: Optional[OpenRouterKeyDiagnostics] = None


# Dashboard Response Models

class TodoItemResponse(BaseModel):
    """Response model for a single TODO item."""

    id: str
    task: str
    status: str
    created_at: datetime
    updated_at: datetime
    notes: Optional[str] = None
    # Scheduling fields
    scheduled_for: Optional[datetime] = None
    thread_id: Optional[str] = None
    last_execution: Optional[datetime] = None
    # User management & recurrence fields
    created_by: str = "agent"
    recurrence: Optional[str] = None


class TodoCreateRequest(BaseModel):
    """Request model for creating a new TODO."""

    task: str = Field(..., min_length=1, max_length=500, description="Task description")
    notes: Optional[str] = Field(default=None, max_length=1000, description="Additional notes")
    scheduled_for: Optional[str] = Field(default=None, description="When to execute: relative ('2h', '30m') or ISO datetime")
    recurrence: Optional[str] = Field(default=None, description="Recurrence: hourly, daily, weekly, monthly")
    thread_id: Optional[str] = Field(default=None, description="Thread ID for scheduled execution output")


class TodoUpdateRequest(BaseModel):
    """Request model for updating a TODO."""

    task: Optional[str] = Field(default=None, max_length=500, description="Task description")
    status: Optional[str] = Field(default=None, description="Status: pending, in_progress, done")
    notes: Optional[str] = Field(default=None, max_length=1000, description="Additional notes")
    scheduled_for: Optional[str] = Field(default=None, description="When to execute: relative ('2h', '30m') or ISO datetime")
    recurrence: Optional[str] = Field(default=None, description="Recurrence: hourly, daily, weekly, monthly")
    thread_id: Optional[str] = Field(default=None, description="Thread ID for scheduled execution output")
    clear_schedule: bool = Field(default=False, description="Clear the schedule")
    clear_recurrence: bool = Field(default=False, description="Clear the recurrence")


class TodoListResponse(BaseModel):
    """Response model for TODO list."""

    user_id: str
    items: List[TodoItemResponse]
    total: int


class ActivityEntryResponse(BaseModel):
    """Response model for an activity entry."""

    id: str
    timestamp: datetime
    type: str
    message: str
    thread_id: Optional[str] = None
    metadata: Optional[dict] = None


class ActivityLogResponse(BaseModel):
    """Response model for activity log."""

    entries: List[ActivityEntryResponse]
    total: int


class NotificationResponse(BaseModel):
    """Response model for a single notification."""

    id: str
    summary: str
    thread_id: Optional[str] = None
    task_id: Optional[str] = None
    created_at: datetime
    read: bool


class NotificationsListResponse(BaseModel):
    """Response model for notifications list."""

    notifications: List[NotificationResponse]
    unread_count: int


# Custom Tool Models


class ToolParameterModel(BaseModel):
    """API model for tool parameters."""

    type: str = "string"
    description: str = ""
    required: bool = False
    default: Optional[str] = None
    enum: Optional[List[str]] = None


class HTTPToolConfigModel(BaseModel):
    """API model for HTTP tool configuration."""

    method: str = "GET"
    url: str
    headers: Dict[str, str] = {}
    body_template: Optional[str] = None
    query_params: Dict[str, str] = {}
    timeout_seconds: int = 30
    response_path: Optional[str] = None
    response_format: str = "auto"


class MCPToolConfigModel(BaseModel):
    """API model for MCP tool configuration."""

    server_command: str
    server_args: List[str] = []
    tool_name: str
    env_vars: Dict[str, str] = {}
    working_directory: Optional[str] = None
    idle_timeout_seconds: int = 300
    startup_timeout_seconds: int = 30


class CustomToolResponse(BaseModel):
    """Response model for a custom tool."""

    id: str
    name: str
    description: str
    parameters: Dict[str, ToolParameterModel]
    implementation_type: str
    http_config: Optional[HTTPToolConfigModel] = None
    mcp_config: Optional[MCPToolConfigModel] = None
    enabled: bool
    tags: List[str] = []
    created_at: datetime
    updated_at: datetime


class CustomToolCreateRequest(BaseModel):
    """Request model for creating a custom tool."""

    id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(..., min_length=1, max_length=1000)
    parameters: Dict[str, ToolParameterModel] = {}
    implementation_type: str = Field(..., pattern=r"^(http|mcp)$")
    http_config: Optional[HTTPToolConfigModel] = None
    mcp_config: Optional[MCPToolConfigModel] = None
    enabled: bool = True
    tags: List[str] = []


class CustomToolUpdateRequest(BaseModel):
    """Request model for updating a custom tool."""

    name: Optional[str] = Field(default=None, max_length=64)
    description: Optional[str] = Field(default=None, max_length=1000)
    parameters: Optional[Dict[str, ToolParameterModel]] = None
    http_config: Optional[HTTPToolConfigModel] = None
    mcp_config: Optional[MCPToolConfigModel] = None
    enabled: Optional[bool] = None
    tags: Optional[List[str]] = None


class CustomToolTestRequest(BaseModel):
    """Request model for testing a custom tool."""

    params: Dict[str, Any] = {}


class CustomToolListResponse(BaseModel):
    """Response model for custom tools list."""

    tools: List[CustomToolResponse]
    total: int


# Unified Tool Models


class UnifiedToolResponse(BaseModel):
    """Response model for a unified tool (built-in or custom)."""

    id: str
    name: str
    description: str  # Effective description (custom if set, else default)
    default_description: str  # Original tool description
    custom_description: Optional[str] = None  # User's custom description override
    category: str
    security_level: str
    enabled: bool
    enabled_reason: str
    tool_type: Literal["builtin", "custom", "mcp_server"]
    implementation_type: Optional[str] = None  # "http" or "mcp" for custom tools
    config_schema: Optional[Dict[str, Any]] = None
    user_config: Dict[str, Any] = {}
    parameters: Optional[Dict[str, Any]] = None  # Custom tool parameters
    http_config: Optional[Dict[str, Any]] = None  # Custom HTTP tool config
    mcp_config: Optional[Dict[str, Any]] = None  # Custom MCP tool config
    tags: List[str] = []
    editable: bool = False
    configurable: bool = False  # True if tool has config_schema
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class UnifiedToolListResponse(BaseModel):
    """Response model for unified tools list."""

    tools: List[UnifiedToolResponse]
    total: int
    builtin_count: int
    custom_count: int


class UnifiedToolEnableRequest(BaseModel):
    """Request model for enabling/disabling a tool."""

    enabled: bool


class UnifiedToolDescriptionRequest(BaseModel):
    """Request model for setting a custom tool description."""

    description: Optional[str] = None  # None to clear and revert to default


class UnifiedToolConfigRequest(BaseModel):
    """Request model for setting tool configuration."""

    config: Dict[str, Any]


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


from nymeria.core.thread_classification import (
    classify_platform as _classify_thread_platform_from_id,
    is_native_platform_thread as _is_native_platform_thread,
    is_shared_channel as _is_shared_channel_thread,
)


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
            tools=get_all_tools_with_agents(),
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
    app.include_router(system_router)
    app.include_router(create_devices_router(verify_api_key, get_settings))
    app.include_router(create_workspace_router(require_admin_user))
    app.include_router(create_rag_router(verify_api_key, get_agent, _require_same_user_or_admin))
    app.include_router(create_memory_router(verify_api_key, get_agent, _require_same_user_or_admin))
    app.include_router(create_user_tools_router(verify_api_key, get_agent, _require_same_user_or_admin))
    app.include_router(create_voice_router(verify_api_key, get_agent, get_settings, _require_thread_access))

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
            pass  # Todo file may not exist; that's fine.
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
            pass
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
            pass
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
            pass
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

    @app.post("/restart", tags=["System"])
    async def restart_server(user: AuthenticatedUser = Depends(require_admin_user)):
        """Restart the API server process. Admin-only — affects every user.

        Spawns a new server process after a short delay, then exits the
        current one.  The frontend should poll /health until the new
        instance is ready.
        """
        import asyncio
        import subprocess
        import sys

        async def _do_restart():
            await asyncio.sleep(0.5)            # Give the HTTP response time to flush
            # Stop the ticker cleanly if running
            agent = get_agent()
            if agent and agent._ticker:
                agent._ticker.stop()

            # Build child env from current process, then overlay .env files.
            # This guarantees restart picks up latest file values even if the
            # current process inherited stale variables from its parent shell/service.
            from dotenv import dotenv_values
            from pathlib import Path

            child_env = os.environ.copy()
            project_root = Path(__file__).resolve().parents[2]
            for filename in (".env", ".env.docker"):
                env_path = project_root / filename
                if not env_path.exists():
                    continue
                for key, value in dotenv_values(env_path).items():
                    if key and value is not None:
                        child_env[key] = value

            # Spawn a replacement process, then exit
            subprocess.Popen(
                [sys.executable] + sys.argv,
                env=child_env,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if sys.platform == "win32" else 0
                ),
                start_new_session=(sys.platform != "win32"),
            )
            import os
            os._exit(0)

        asyncio.create_task(_do_restart())
        return {"message": "Server restarting..."}

    @app.post("/report", tags=["System"])
    async def report_problem(
        request: ReportRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Send an error report email to support with debug context."""
        from html import escape as html_escape
        from ..tools.outlook_email import outlook_send_email

        sections = ['<h2>Nymeria Error Report</h2>']
        sections.append(f'<p><strong>Timestamp:</strong> {html_escape(request.timestamp)}</p>')

        if request.thread_id:
            sections.append(f'<p><strong>Thread ID:</strong> {html_escape(request.thread_id)}</p>')
        if request.message_id:
            sections.append(f'<p><strong>Message ID:</strong> {html_escape(request.message_id)}</p>')

        if request.description:
            sections.append(f'<h3>Description</h3><p>{html_escape(request.description)}</p>')

        if request.client_info:
            items = ''.join(
                f'<li><strong>{html_escape(str(k))}:</strong> {html_escape(str(v))}</li>'
                for k, v in request.client_info.items()
            )
            sections.append(f'<h3>Client Info</h3><ul>{items}</ul>')

        if request.messages:
            rows = ''
            for m in request.messages[-10:]:
                role = html_escape(m.get('role', '?'))
                content = html_escape((m.get('content', '') or '')[:500])
                ts = html_escape(m.get('timestamp', ''))
                rows += f'<tr><td style="white-space:nowrap">{ts}</td><td><strong>{role}</strong></td><td><pre style="margin:0;white-space:pre-wrap;max-width:400px">{content}</pre></td></tr>'
            sections.append(
                '<h3>Recent Messages</h3>'
                '<table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse;font-size:13px">'
                '<tr><th>Time</th><th>Role</th><th>Content</th></tr>'
                f'{rows}</table>'
            )

        body = '\n'.join(sections)
        date_str = request.timestamp[:10] if request.timestamp else 'unknown'
        subject = f'Nymeria Error Report - {date_str}'

        try:
            result = outlook_send_email.invoke({
                'to': 'reports@example.com',
                'subject': subject,
                'body': body,
                'is_html': True,
            })
            if '[Error]' in str(result):
                raise HTTPException(status_code=502, detail=str(result))
            return {'status': 'sent', 'detail': str(result)}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f'Failed to send error report: {e}')
            raise HTTPException(status_code=500, detail=str(e))

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
        if settings.database_backend == "sqlite":
            import sqlite3 as _sqlite3
            try:
                conn = _sqlite3.connect(str(settings.db_path))
                conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,)
                )
                for table in ("checkpoint_writes", "checkpoint_blobs"):
                    try:
                        conn.execute(
                            f"DELETE FROM {table} WHERE thread_id = ?",
                            (thread_id,),
                        )
                    except Exception:
                        pass
                conn.commit()
                conn.close()
            except Exception as e:
                logger.warning(f"Failed to delete checkpoints for {thread_id}: {e}")
        elif settings.database_backend == "postgres":
            import psycopg  # type: ignore[import-untyped]
            try:
                with psycopg.connect(settings.postgres_uri) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM checkpoints WHERE thread_id = %s",
                            (thread_id,),
                        )
                        try:
                            cur.execute(
                                "DELETE FROM checkpoint_writes WHERE thread_id = %s",
                                (thread_id,),
                            )
                        except Exception:
                            pass
                        try:
                            cur.execute(
                                "DELETE FROM checkpoint_blobs WHERE thread_id = %s",
                                (thread_id,),
                            )
                        except Exception:
                            pass
                    conn.commit()
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

    def _thread_share_available_tool_names(agent, user_role: str) -> tuple[set[str], set[str], set[str]]:
        """Return (available tools, role-gated tools, callable tool names)."""
        from ..tools import (
            ADMIN_ONLY_OPTIONAL_TOOL_NAMES,
            ALL_TOOLS,
            DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES,
            OPTIONAL_TOOLS,
        )

        names = {t.name for t in ALL_TOOLS}
        names.update(OPTIONAL_TOOLS.keys())
        try:
            names.update(t.get("name") for t in agent.tool_registry.list_tools() if t.get("name"))
        except Exception:
            pass

        callable_names = set(getattr(agent, "_callable_tool_thread_map", {}) or {})
        try:
            callable_names.update(
                tc.callable_name
                for tc in agent.thread_config_manager.list_callable_threads()
                if tc.callable_name
            )
        except Exception:
            pass

        # Callable tools are resolved from ownership, not enabled_tools. Do
        # not preserve guessed callable names as portable tool enablements.
        role_gated = set(ADMIN_ONLY_OPTIONAL_TOOL_NAMES)
        if user_role != "admin":
            role_gated.update(DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES)
        return names - callable_names, role_gated, callable_names

    def _thread_share_available_skill_names(agent, user_id: str) -> set[str]:
        if getattr(agent, "skill_manager", None) is None:
            return set()
        try:
            return {s.name for s in agent.skill_manager.list_installed(user_id=user_id)}
        except Exception:
            return set()

    def _thread_share_title(document: dict[str, Any]) -> str:
        raw = document.get("title")
        if not isinstance(raw, str) or not raw.strip():
            source = document.get("source")
            if isinstance(source, dict):
                raw = source.get("title")
        if not isinstance(raw, str) or not raw.strip():
            return "Imported Thread"
        return raw.strip()[:200]

    @app.get("/threads/{thread_id}/export", tags=["Threads"])
    async def export_thread_share(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Export a portable, shareable thread configuration document."""
        _require_thread_access(user, thread_id)
        from ..core.thread_share import build_thread_share_document

        agent = get_agent()
        meta = agent.thread_metadata_manager.get_thread(user.id, thread_id)
        title = meta.title if meta else "New Chat"
        tc = agent.thread_config_manager.get_config(thread_id)
        return build_thread_share_document(thread_id=thread_id, title=title, config=tc)

    @app.post("/threads/import", tags=["Threads"])
    async def import_thread_share(
        http_request: Request,
        document: Dict[str, Any],
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Create a new empty thread from a portable thread configuration."""
        from ..core.thread_share import ThreadShareError, sanitize_import_config

        agent = get_agent()
        available_tools, admin_only_tools, _callable_tool_names = _thread_share_available_tool_names(agent, user.role)
        available_skills = _thread_share_available_skill_names(agent, user_id)
        owned = set(agent.accounts_repo.list_threads_for_user(user_id))
        owned_callable_names: set[str] = set()
        try:
            owned_callables = agent.thread_config_manager.list_callable_threads(
                owned_thread_ids=owned
            )
            owned_callable_names.update(
                tc.callable_name for tc in owned_callables if tc.callable_name
            )
        except Exception:
            pass

        title = _thread_share_title(document)
        thread_id = f"imported-{uuid.uuid4().hex[:12]}"
        try:
            tc, warnings = sanitize_import_config(
                document=document,
                new_thread_id=thread_id,
                importer_role=user.role,
                available_tool_names=available_tools,
                unavailable_enabled_tool_names=admin_only_tools,
                available_skill_names=available_skills,
                unavailable_callable_names=available_tools | owned_callable_names,
            )
        except ThreadShareError as e:
            raise HTTPException(status_code=400, detail=str(e))

        if not agent.thread_config_manager.save_config(tc):
            raise HTTPException(status_code=500, detail="Failed to save imported thread config")

        agent.accounts_repo.claim_thread(thread_id, user_id)

        metadata_title = tc.callable_name if tc.callable and tc.callable_name else title
        metadata_platform = "callable" if tc.callable else "desktop"
        metadata_source = "callable" if tc.callable else "user"
        agent.thread_metadata_manager.upsert_thread(
            user_id,
            thread_id,
            title=metadata_title,
            title_source=metadata_source,
            platform=metadata_platform,
        )

        agent.invalidate_thread_config_cache(thread_id)
        if tc.callable:
            agent.sync_agent_tools()

        client_id = http_request.headers.get("x-nymeria-client-id", "")
        publish_sync_event(
            event_type="thread_created",
            thread_id=thread_id,
            user_id=user_id,
            data={
                "title": metadata_title,
                "title_source": metadata_source,
                "platform": metadata_platform,
            },
            origin_client_id=client_id,
        )

        result = tc.model_dump(mode="json")
        result["has_customizations"] = tc.has_customizations()
        return {
            "status": "ok",
            "thread_id": thread_id,
            "title": metadata_title,
            "config": result,
            "warnings": warnings,
        }

    @app.post(
        "/threads/{thread_id}/attachments/validate",
        response_model=AttachmentValidationResponse,
        tags=["Threads"],
    )
    async def validate_thread_attachments(
        thread_id: str,
        request: AttachmentValidationRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Preflight-check attachment compatibility against the effective thread model.

        This does not send content to any model. It only evaluates whether
        attachments are likely compatible and provides warnings plus force-send guidance.
        Ownership is still enforced — the effective provider/model can leak which
        upstream a thread is configured to use, so a guess-the-thread-id probe must
        be denied.
        """
        _require_thread_access(user, thread_id)
        from ..config.model_capabilities import evaluate_attachment_compatibility

        agent = get_agent()
        llm_cfg = agent._get_llm_config_for_thread(thread_id)
        effective_provider = llm_cfg.provider or agent.settings.llm_provider
        effective_model = llm_cfg.model or agent.settings.llm_model

        attachments = [
            {
                "file_type": att.file_type,
                "data_url": att.data_url,
                "mime_type": att.mime_type,
                "file_name": att.file_name or "",
            }
            for att in request.attachments
        ]

        report = evaluate_attachment_compatibility(
            effective_model,
            effective_provider,
            attachments,
        )

        return AttachmentValidationResponse(
            compatible=bool(report["compatible"]),
            effective_provider=effective_provider,
            effective_model=effective_model,
            model_input_modalities=list(report["model_input_modalities"]),
            required_modalities=list(report["required_modalities"]),
            unsupported_modalities=list(report["unsupported_modalities"]),
            warnings=list(report["warnings"]),
            can_force_send=True,
        )

    @app.post("/threads/{thread_id}/compact", tags=["Threads"])
    async def compact_thread(
        thread_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Manually trigger compaction for a thread.

        Compresses conversation history into a summary while preserving recent messages.
        Compaction always runs under the authenticated caller — admins impersonate via
        ``X-Nymeria-Act-As``, which ``verify_api_key`` resolves before we get here.
        """
        _require_thread_access(user, thread_id)
        agent = get_agent()
        result = await agent.compact_now(thread_id, user.id)
        return result

    @app.post("/threads/{thread_id}/stop", tags=["Threads"])
    async def stop_thread(thread_id: str, user: AuthenticatedUser = Depends(verify_api_key)):
        """Stop any running operation on a thread.

        Signals the abort event for the thread and cascades to any active
        callable threads it has spawned. The current stream()/astream() call
        breaks at the next iteration boundary, releasing the thread lock.
        """
        _require_thread_access(user, thread_id)
        agent = get_agent()
        lock_info = agent._thread_locks.get_lock_info(thread_id)

        # Only create abort events for threads with active operations
        # to prevent unbounded memory growth from arbitrary thread IDs
        if lock_info:
            agent.abort_with_cascade(thread_id)
            return {
                "status": "stopping",
                "thread_id": thread_id,
                "message": (
                    f"Stop signal sent. Thread was held by '{lock_info.get('holder')}' "
                    f"for {lock_info.get('held_seconds', 0):.0f}s. "
                    f"Will stop at next iteration boundary."
                ),
            }
        else:
            return {
                "status": "idle",
                "thread_id": thread_id,
                "message": "Thread was not running. No stop signal needed.",
            }

    @app.get("/tools", tags=["Tools"])
    async def list_tools(user: AuthenticatedUser = Depends(verify_api_key)):
        """List all available tools and their descriptions.

        Core and MCP tools are visible to every user (any user can enable
        them on a thread). Callable-thread tools come straight from the
        caller's owned ``ThreadConfig`` rows (NOT the global ToolRegistry),
        because the registry is name-keyed and last-write-wins on collisions
        — two users with a "Helper" would otherwise see only the surviving
        entry. The runtime gate in ``tool_factory.py`` blocks cross-user
        invocations even on cache stale paths.
        """
        agent = get_agent()
        registry_tools = agent.tool_registry.list_tools()
        callable_map = agent._callable_tool_thread_map or {}
        # Names that are CURRENTLY in the registry as callable threads —
        # filter these out wholesale, then re-add per-user from disk.
        callable_names_in_registry = set(callable_map.keys())

        is_admin = user.role == "admin"
        result = [t for t in registry_tools if t["name"] not in callable_names_in_registry]

        owned = set(agent.accounts_repo.list_threads_for_user(user.id))
        # Per-user callable threads (filter by owned thread IDs)
        for tc in agent.thread_config_manager.list_callable_threads(
            owned_thread_ids=owned
        ):
            if not tc.callable_name:
                continue
            result.append({
                "name": tc.callable_name,
                "description": (tc.callable_description
                                or f"Invoke the {tc.callable_name} thread"),
                "enabled": True,
            })

        # Admins additionally see legacy unowned callables (no row in
        # thread_owners) so they can audit/migrate them. Non-admins don't.
        if is_admin:
            seen_names = {t["name"] for t in result}
            for tc in agent.thread_config_manager.list_callable_threads():
                if not tc.callable_name or tc.callable_name in seen_names:
                    continue
                if agent.accounts_repo.get_thread_owner(tc.thread_id) is None:
                    result.append({
                        "name": tc.callable_name,
                        "description": (tc.callable_description
                                        or f"Invoke the {tc.callable_name} thread"),
                        "enabled": True,
                    })

        return {"tools": result}

    @app.get("/tools/optional", tags=["Tools"])
    async def list_optional_tools(
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List tools available for per-thread enabling (not in the user's core set).

        Reads the caller's profile (user-scoped via _authed_user_id) — no
        thread context is needed since the result depends only on the user's
        default tool preferences, not which thread they're enabling tools on.
        """
        from ..tools import (
            ALL_TOOLS,
            OPTIONAL_TOOLS,
            filter_discoverable_optional_tool_names,
        )

        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)
        default_tools = profile.tool_preferences.default_thread_tools

        core_set = set(default_tools) if default_tools is not None else {t.name for t in ALL_TOOLS}
        visible_optional = filter_discoverable_optional_tool_names(
            OPTIONAL_TOOLS.keys(),
            user.role,
        )
        result = []
        seen = set()
        for t in ALL_TOOLS:
            if t.name not in core_set and t.name not in seen:
                result.append({"name": t.name, "description": t.description})
                seen.add(t.name)
        for name, tool in OPTIONAL_TOOLS.items():
            if name in visible_optional and name not in core_set and name not in seen:
                result.append({"name": name, "description": tool.description})
                seen.add(name)
        return {"tools": result}

    @app.get("/threads/{thread_id}/callable-tools", tags=["Tools"])
    async def get_thread_callable_tools(
        thread_id: str,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Return callable threads actually available to a caller thread.

        Mirrors the runtime callable-tool binding rules: ownership scoping,
        callable-team scoping, self-exclusion, duplicate-name suppression, and
        per-thread disabled-tool filtering.
        """
        _require_thread_access(user, thread_id)
        from ..tools import ALL_TOOLS, OPTIONAL_TOOLS

        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)
        caller_tc = agent.thread_config_manager.get_config(thread_id)
        disabled = set(caller_tc.disabled_tools if caller_tc else [])
        own_callable_name = (
            caller_tc.callable_name
            if caller_tc and caller_tc.callable and caller_tc.callable_name
            else None
        )

        all_tools_dict = {t.name: t for t in ALL_TOOLS}
        all_tools_dict.update(OPTIONAL_TOOLS)
        default_tools = profile.tool_preferences.default_thread_tools
        core_names = (
            default_tools
            if default_tools is not None
            else [t.name for t in ALL_TOOLS]
        )
        existing_names = {name for name in core_names if name in all_tools_dict}
        if default_tools is not None:
            existing_names.update(
                name
                for name in core_names
                if name.startswith("mcp__") and agent.tool_registry.get_tool(name)
            )

        visible = []
        seen_names: set[str] = set(existing_names)
        for tc in agent._get_team_scoped_callable_threads(
            user_id=user_id,
            caller_thread_id=thread_id,
        ):
            if tc.thread_id == thread_id or not tc.callable_name:
                continue
            if own_callable_name and tc.callable_name == own_callable_name:
                continue
            if tc.callable_name in disabled or tc.callable_name in seen_names:
                continue
            seen_names.add(tc.callable_name)
            visible.append({
                "thread_id": tc.thread_id,
                "name": tc.callable_name,
                "description": tc.callable_description,
                "team_id": tc.callable_team_id,
                "team_name": tc.callable_team_name,
            })

        return {
            "thread_id": thread_id,
            "callable_thread_count": len(visible),
            "callable_threads": visible,
        }

    @app.get("/tools/defaults", tags=["Tools"])
    async def get_default_tools(
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Get the default tool set for new threads.

        Returns all available tools (core + optional) with is_default flags
        and callable thread count. Always uses default_thread_tools as source of truth.
        """
        from ..tools import (
            ALL_TOOLS,
            OPTIONAL_TOOLS,
            filter_discoverable_optional_tool_names,
        )
        from ..tools.metadata import get_tool_metadata

        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)
        prefs = profile.tool_preferences

        # default_thread_tools is the single source of truth
        if prefs.default_thread_tools is not None:
            default_set = set(prefs.default_thread_tools)
        else:
            # Not yet initialized — treat ALL_TOOLS as default
            default_set = {t.name for t in ALL_TOOLS}

        # Build unified tool list
        tools_out = []
        seen = set()
        visible_optional = filter_discoverable_optional_tool_names(
            OPTIONAL_TOOLS.keys(),
            user.role,
        )
        for t in ALL_TOOLS:
            meta = get_tool_metadata(t.name)
            tools_out.append({
                "name": t.name,
                "description": t.description,
                "category": meta.category.value if meta else "core",
                "security_level": meta.security_level.value if meta else "moderate",
                "is_optional": False,
                "is_default": t.name in default_set,
            })
            seen.add(t.name)
        for name, t in OPTIONAL_TOOLS.items():
            if name in visible_optional and name not in seen:
                meta = get_tool_metadata(name)
                tools_out.append({
                    "name": name,
                    "description": t.description,
                    "category": meta.category.value if meta else "unknown",
                    "security_level": meta.security_level.value if meta else "moderate",
                    "is_optional": True,
                    "is_default": name in default_set,
                })
                seen.add(name)

        # Include MCP server tools
        from ..tools.metadata import MCP_SERVER_TOOL_METADATA
        for name, meta in MCP_SERVER_TOOL_METADATA.items():
            if name not in seen:
                tools_out.append({
                    "name": name,
                    "description": meta.description,
                    "category": "mcp_server",
                    "security_level": "moderate",
                    "is_optional": True,
                    "is_default": name in default_set,
                })
                seen.add(name)

        owned = set(agent.accounts_repo.list_threads_for_user(user_id))
        callable_count = len(agent.thread_config_manager.list_callable_threads(owned_thread_ids=owned))

        return {
            "mode": "custom",
            "default_tools": sorted(default_set),
            "available_tools": tools_out,
            "callable_thread_count": callable_count,
        }

    class DefaultToolsUpdateRequest(BaseModel):
        tool_names: list = Field(..., description="Tool names to enable by default for new threads")

    @app.put("/tools/defaults", tags=["Tools"])
    async def set_default_tools(
        request: DefaultToolsUpdateRequest,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Set which tools new threads inherit by default."""
        from ..tools import (
            ALL_TOOLS,
            OPTIONAL_TOOLS,
            ADMIN_ONLY_OPTIONAL_TOOL_NAMES,
            DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES,
        )
        from ..tools.metadata import MCP_SERVER_TOOL_METADATA
        from ..core.user_profile import migrate_tool_names

        # Auto-migrate legacy names (e.g. todo -> nym_todo) from stale clients.
        tool_names = migrate_tool_names(list(request.tool_names))

        # Validate tool names (allow built-in, optional, and MCP server tools)
        known = {t.name for t in ALL_TOOLS} | set(OPTIONAL_TOOLS.keys()) | set(MCP_SERVER_TOOL_METADATA.keys())
        unknown = set(tool_names) - known
        if unknown:
            raise HTTPException(400, detail=f"Unknown tools: {sorted(unknown)}")

        # Self-modify / runtime-admin reload tools rewrite the shared codebase —
        # only admin defaults may include them.
        if user.role != "admin":
            blocked = ADMIN_ONLY_OPTIONAL_TOOL_NAMES.intersection(tool_names)
            if blocked:
                raise HTTPException(
                    status_code=403,
                    detail=f"Admin-only tools cannot be set as defaults by this user: {sorted(blocked)}",
                )
            blocked = DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES.intersection(tool_names)
            if blocked:
                raise HTTPException(
                    status_code=403,
                    detail=f"Developer-only diagnostic tools cannot be set as defaults by this user: {sorted(blocked)}",
                )

        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)
        profile.tool_preferences.default_thread_tools = tool_names
        agent.profile_manager.save_profile(profile)

        agent._rebuild_default_graphs()

        return {
            "status": "ok",
            "default_tools": sorted(tool_names),
            "count": len(tool_names),
        }

    @app.delete("/tools/defaults", tags=["Tools"])
    async def reset_default_tools(
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Reset default tools to all core tools."""
        from ..tools import ALL_TOOLS

        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)
        profile.tool_preferences.default_thread_tools = [t.name for t in ALL_TOOLS]
        agent.profile_manager.save_profile(profile)

        agent._rebuild_default_graphs()

        return {
            "status": "ok",
            "mode": "custom",
            "default_tools": sorted(profile.tool_preferences.default_thread_tools),
        }

    # ------------------------------------------------------------------
    # Agent Skills (SKILL.md progressive-disclosure bundles)
    # ------------------------------------------------------------------

    class SkillMetadataResponse(BaseModel):
        name: str
        description: str
        scope: str
        allowed_tools: List[str] = []
        required_tools: List[str] = []
        tool_ttl: str = "2h"
        is_skill_kit: bool = False
        default_active: bool = False
        has_scripts: bool = False
        has_references: bool = False
        has_assets: bool = False

    class SkillDetailResponse(SkillMetadataResponse):
        body: str
        path: str
        license: Optional[str] = None
        scripts: List[str] = []
        references: List[str] = []

    class SkillInstallRequest(BaseModel):
        name: str
        source: str = Field("anthropic", description="Marketplace source: 'anthropic' (Phase 1).")
        scope: str = Field("user", description="Install scope: 'user' or 'global'.")

    class GlobalSkillsUpdateRequest(BaseModel):
        skill_names: List[str]

    def _invalidate_graph_caches():
        agent = get_agent()
        with agent._graph_cache_lock:
            agent._user_graphs.clear()
        try:
            agent._async_user_graphs.clear()
        except Exception:
            pass

    def _skill_to_metadata(skill) -> dict:
        return {
            "name": skill.name,
            "description": skill.description,
            "scope": skill.scope,
            "allowed_tools": skill.allowed_tools,
            "required_tools": skill.required_tools,
            "tool_ttl": skill.tool_ttl,
            "is_skill_kit": skill.is_skill_kit,
            "default_active": False,
            "has_scripts": skill.has_scripts,
            "has_references": skill.has_references,
            "has_assets": skill.has_assets,
        }

    @app.get("/skills", tags=["Skills"])
    async def list_skills(
        user_id: str = Depends(_authed_user_id),
        scope: Optional[str] = Query(None, description="Filter by scope: user/global/bundled"),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List all installed skills visible to *user_id*."""
        agent = get_agent()
        if agent.skill_manager is None:
            return {"skills": [], "error": "skills subsystem unavailable"}
        skills = agent.skill_manager.list_installed(user_id=user_id)
        if scope:
            skills = [s for s in skills if s.scope == scope]
        return {"skills": [_skill_to_metadata(s) for s in skills]}

    @app.get("/skills/marketplace/search", tags=["Skills"])
    async def search_marketplace(
        source: str = Query("anthropic"),
        q: Optional[str] = Query(None),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Search a remote marketplace for skills."""
        from ..skills.marketplace import get_fetcher, MarketplaceError
        try:
            fetcher = get_fetcher(source)
            entries = fetcher.list(query=q)
        except NotImplementedError as e:
            raise HTTPException(status_code=501, detail=str(e))
        except MarketplaceError as e:
            raise HTTPException(status_code=502, detail=str(e))
        return {
            "source": source,
            "query": q,
            "results": [
                {"name": e.name, "description": e.description, "source": e.source, "repo_url": e.repo_url}
                for e in entries
            ],
        }

    @app.get("/skills/{name}", response_model=SkillDetailResponse, tags=["Skills"])
    async def get_skill(
        name: str,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Return the full body + frontmatter of an installed skill."""
        agent = get_agent()
        if agent.skill_manager is None:
            raise HTTPException(status_code=503, detail="skills subsystem unavailable")
        skill = agent.skill_manager.get(name, user_id=user_id)
        if skill is None:
            raise HTTPException(status_code=404, detail=f"skill not found: {name}")
        return {
            **_skill_to_metadata(skill),
            "body": skill.body,
            "path": str(skill.path),
            "license": skill.license,
            "scripts": skill.list_scripts(),
            "references": skill.list_references(),
        }

    @app.post("/skills/install", tags=["Skills"])
    async def install_skill_endpoint(
        request: SkillInstallRequest,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Install a skill from a marketplace into user or global scope.

        ``scope=user`` is per-user and any caller may install for themselves.
        ``scope=global`` writes into the shared skills directory visible to
        every user — admin only, since a skill bundle can ship scripts that
        the agent process can execute.
        """
        agent = get_agent()
        if agent.skill_manager is None:
            raise HTTPException(status_code=503, detail="skills subsystem unavailable")
        from ..skills.marketplace import get_fetcher, MarketplaceError
        if request.scope not in ("user", "global"):
            raise HTTPException(status_code=400, detail="scope must be 'user' or 'global'")
        if request.scope == "global" and user.role != "admin":
            raise HTTPException(status_code=403, detail="Global skill install requires admin")
        try:
            fetcher = get_fetcher(request.source)
        except NotImplementedError as e:
            raise HTTPException(status_code=501, detail=str(e))
        except MarketplaceError as e:
            raise HTTPException(status_code=400, detail=str(e))

        target_dir = agent.skill_manager.target_dir(
            request.scope, user_id=user_id if request.scope == "user" else None
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            skill = fetcher.fetch(request.name, target_dir)
        except MarketplaceError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.exception("install_skill_endpoint failed")
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")

        agent.skill_manager.reload()
        _invalidate_graph_caches()
        return {"status": "ok", "skill": _skill_to_metadata(skill), "path": str(skill.path)}

    @app.delete("/skills/{name}", tags=["Skills"])
    async def uninstall_skill(
        name: str,
        scope: str = Query("user"),
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Remove an installed skill from disk."""
        agent = get_agent()
        if agent.skill_manager is None:
            raise HTTPException(status_code=503, detail="skills subsystem unavailable")
        if scope not in ("user", "global"):
            raise HTTPException(status_code=400, detail="scope must be 'user' or 'global'")
        if scope == "global" and user.role != "admin":
            raise HTTPException(status_code=403, detail="Global skill uninstall requires admin")
        try:
            deleted = agent.skill_manager.uninstall(
                name, scope=scope,
                user_id=user_id if scope == "user" else None,
            )
        except PermissionError as e:
            raise HTTPException(status_code=403, detail=str(e))
        if not deleted:
            raise HTTPException(status_code=404, detail=f"skill not found in scope={scope}: {name}")

        # Also clean up any stale references to this skill name in profile/thread configs.
        try:
            profile = agent.profile_manager.get_profile(user_id)
            if name in getattr(profile, "enabled_global_skills", []):
                profile.enabled_global_skills = [
                    n for n in profile.enabled_global_skills if n != name
                ]
                agent.profile_manager.save_profile(profile)
        except Exception:
            pass

        _invalidate_graph_caches()
        return {"status": "ok", "deleted": name, "scope": scope}

    @app.get("/threads/{thread_id}/skills", tags=["Skills"])
    async def get_thread_active_skills(
        thread_id: str,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Resolved set of skills active on this thread (after scope + overrides)."""
        _require_thread_access(user, thread_id)
        agent = get_agent()
        if agent.skill_manager is None:
            return {"skills": []}
        profile = agent.profile_manager.get_profile(user_id)
        tc = agent.thread_config_manager.get_config(thread_id)
        enabled_global = list(getattr(profile, "enabled_global_skills", []) or [])
        enabled_thread = list(tc.enabled_skills) if tc and tc.enabled_skills else []
        disabled_thread = list(tc.disabled_skills) if tc and tc.disabled_skills else []
        active = agent.skill_manager.list_for_thread(
            user_id=user_id,
            enabled_global_skills=enabled_global,
            thread_enabled_skills=enabled_thread,
            thread_disabled_skills=disabled_thread,
        )
        return {
            "thread_id": thread_id,
            "default_enabled": [],
            "enabled_global": enabled_global,
            "thread_enabled": enabled_thread,
            "thread_disabled": disabled_thread,
            "skills": [_skill_to_metadata(s) for s in active],
        }

    @app.get("/settings/global-skills", tags=["Skills"])
    async def get_global_skills(
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Which skills are enabled-by-default for every new thread."""
        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)
        return {"enabled_global_skills": list(getattr(profile, "enabled_global_skills", []) or [])}

    @app.put("/settings/global-skills", tags=["Skills"])
    async def set_global_skills(
        request: GlobalSkillsUpdateRequest,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Replace the user's enabled-by-default skill list."""
        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)
        profile.enabled_global_skills = list(request.skill_names)
        agent.profile_manager.save_profile(profile)
        _invalidate_graph_caches()
        return {"enabled_global_skills": profile.enabled_global_skills}

    @app.get("/settings", response_model=ServerSettingsResponse, tags=["Settings"])
    async def get_server_settings(
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """Get current server settings."""
        return ServerSettingsResponse(
            llm_provider=settings.llm_provider,
            llm_model=settings.llm_model,
            llm_temperature=settings.llm_temperature,
            llm_max_tokens=settings.llm_max_tokens,
            llm_top_p=settings.llm_top_p,
            llm_top_k=settings.llm_top_k,
            llm_frequency_penalty=settings.llm_frequency_penalty,
            llm_presence_penalty=settings.llm_presence_penalty,
            llm_reasoning_effort=settings.llm_reasoning_effort,
            llm_extended_thinking=settings.llm_extended_thinking,
            llm_use_model_defaults=settings.llm_use_model_defaults,
            llm_base_url=settings.llm_base_url,
            openai_api_mode=settings.openai_api_mode,
            llm_stream_max_retries=settings.llm_stream_max_retries,
            llm_stream_retry_initial_delay=settings.llm_stream_retry_initial_delay,
            llm_stream_retry_max_delay=settings.llm_stream_retry_max_delay,
            context_management=settings.context_management,
            compact_threshold=settings.compact_threshold,
            compact_keep_messages=settings.compact_keep_messages,
            compact_model=settings.compact_model,
            sliding_window_cycles=settings.sliding_window_cycles,
            tool_output_max_chars=settings.tool_output_max_chars,
            log_level=settings.log_level,
            watchdog_enabled=settings.watchdog_enabled,
            watchdog_interval_minutes=settings.watchdog_interval_minutes,
            todo_staleness_minutes=settings.todo_staleness_minutes,
            activity_retention_hours=settings.activity_retention_hours,
            # Voice settings
            tts_provider=settings.tts_provider,
            tts_base_url=settings.tts_base_url,
            tts_model=settings.tts_model,
            tts_voice=settings.tts_voice,
            tts_output_format=settings.tts_output_format,
            tts_speed=settings.tts_speed,
            stt_provider=settings.stt_provider,
            stt_base_url=settings.stt_base_url,
            stt_model=settings.stt_model,
            stt_language=settings.stt_language,
            voice_default_thread_id=settings.voice_default_thread_id,
        )

    @app.get("/settings/llm/runtime", response_model=LLMRuntimeDiagnosticsResponse, tags=["Settings"])
    async def get_llm_runtime_diagnostics(
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """Get runtime LLM diagnostics including active OpenRouter key budget details."""
        from pathlib import Path
        import urllib.error
        import urllib.request
        from ..config.model_capabilities import get_max_output_tokens

        agent = get_agent()
        llm_cfg = agent._get_llm_config_for_thread("")

        effective_max_tokens = llm_cfg.max_tokens
        if effective_max_tokens is None and llm_cfg.provider == "openrouter":
            try:
                effective_max_tokens = get_max_output_tokens(llm_cfg.model)
            except Exception as e:
                logger.warning(f"Failed to resolve OpenRouter max output tokens: {e}")

        project_root = Path(__file__).resolve().parents[2]
        source_env_files = [
            str(project_root / filename)
            for filename in (".env", ".env.docker")
            if (project_root / filename).exists()
        ]

        response = LLMRuntimeDiagnosticsResponse(
            provider=llm_cfg.provider,
            model=llm_cfg.model,
            llm_max_tokens=settings.llm_max_tokens,
            effective_max_tokens=effective_max_tokens,
            source_env_files=source_env_files,
        )

        if llm_cfg.provider == "openrouter":
            if not llm_cfg.api_key:
                response.openrouter = OpenRouterKeyDiagnostics(
                    fetch_error="OPENROUTER_API_KEY is missing in active runtime settings"
                )
                return response

            request_obj = urllib.request.Request(
                "https://openrouter.ai/api/v1/key",
                headers={
                    "Authorization": f"Bearer {llm_cfg.api_key}",
                    "Content-Type": "application/json",
                },
            )

            try:
                with urllib.request.urlopen(request_obj, timeout=6) as api_response:
                    payload = json.loads(api_response.read().decode("utf-8"))
                data = payload.get("data", {}) if isinstance(payload, dict) else {}

                if isinstance(data, dict):
                    response.openrouter = OpenRouterKeyDiagnostics(
                        label=data.get("label"),
                        limit=data.get("limit"),
                        limit_remaining=data.get("limit_remaining"),
                        usage=data.get("usage"),
                        limit_reset=data.get("limit_reset"),
                        include_byok_in_limit=data.get("include_byok_in_limit"),
                        is_management_key=data.get("is_management_key"),
                    )
                else:
                    response.openrouter = OpenRouterKeyDiagnostics(
                        fetch_error="Unexpected response shape from OpenRouter /key endpoint"
                    )
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", errors="ignore")
                response.openrouter = OpenRouterKeyDiagnostics(
                    fetch_error=f"HTTP {e.code}: {body[:300]}"
                )
            except Exception as e:
                response.openrouter = OpenRouterKeyDiagnostics(
                    fetch_error=f"{type(e).__name__}: {e}"
                )

        return response

    @app.patch("/settings", tags=["Settings"])
    async def update_server_settings(
        updates: ServerSettingsUpdate,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings),
    ):
        """
        Update server settings with hot-reload. Admin-only — settings are
        global (LLM provider, env vars, etc.).

        Changes are applied immediately - no restart required.
        LLM model/provider changes trigger graph rebuild automatically.
        """
        # Use .env.docker if it exists (Docker deployment), otherwise .env (local)
        env_docker_path = settings.project_root / ".env.docker"
        env_path = env_docker_path if env_docker_path.exists() else settings.project_root / ".env"

        # Read existing .env content
        existing_lines = []
        if env_path.exists():
            existing_lines = env_path.read_text(encoding="utf-8").splitlines()

        # Map of setting names to env var names
        env_mapping = {
            "llm_provider": "LLM_PROVIDER",
            "llm_model": "LLM_MODEL",
            "llm_temperature": "LLM_TEMPERATURE",
            "llm_max_tokens": "LLM_MAX_TOKENS",
            "llm_top_p": "LLM_TOP_P",
            "llm_top_k": "LLM_TOP_K",
            "llm_frequency_penalty": "LLM_FREQUENCY_PENALTY",
            "llm_presence_penalty": "LLM_PRESENCE_PENALTY",
            "llm_reasoning_effort": "LLM_REASONING_EFFORT",
            "llm_extended_thinking": "LLM_EXTENDED_THINKING",
            "llm_use_model_defaults": "LLM_USE_MODEL_DEFAULTS",
            "llm_base_url": "LLM_BASE_URL",
            "openai_api_mode": "OPENAI_API_MODE",
            "llm_stream_max_retries": "LLM_STREAM_MAX_RETRIES",
            "llm_stream_retry_initial_delay": "LLM_STREAM_RETRY_INITIAL_DELAY",
            "llm_stream_retry_max_delay": "LLM_STREAM_RETRY_MAX_DELAY",
            "context_management": "CONTEXT_MANAGEMENT",
            "compact_threshold": "COMPACT_THRESHOLD",
            "compact_keep_messages": "COMPACT_KEEP_MESSAGES",
            "compact_model": "COMPACT_MODEL",
            "sliding_window_cycles": "SLIDING_WINDOW_CYCLES",
            "tool_output_max_chars": "TOOL_OUTPUT_MAX_CHARS",
            "log_level": "LOG_LEVEL",
            "watchdog_enabled": "WATCHDOG_ENABLED",
            "watchdog_interval_minutes": "WATCHDOG_INTERVAL_MINUTES",
            "todo_staleness_minutes": "TODO_STALENESS_MINUTES",
            "activity_retention_hours": "ACTIVITY_RETENTION_HOURS",
            # Voice settings
            "tts_provider": "TTS_PROVIDER",
            "tts_base_url": "TTS_BASE_URL",
            "tts_model": "TTS_MODEL",
            "tts_voice": "TTS_VOICE",
            "tts_output_format": "TTS_OUTPUT_FORMAT",
            "tts_speed": "TTS_SPEED",
            "stt_provider": "STT_PROVIDER",
            "stt_base_url": "STT_BASE_URL",
            "stt_model": "STT_MODEL",
            "stt_language": "STT_LANGUAGE",
            "voice_default_thread_id": "VOICE_DEFAULT_THREAD_ID",
            # API keys
            "perplexity_api_key": "PERPLEXITY_API_KEY",
            "perplexity_search_model": "PERPLEXITY_SEARCH_MODEL",
            "openai_api_key": "OPENAI_API_KEY",
            "anthropic_api_key": "ANTHROPIC_API_KEY",
            "anthropic_direct_api_key": "ANTHROPIC_DIRECT_API_KEY",
            "openrouter_api_key": "OPENROUTER_API_KEY",
            "embedding_api_key": "EMBEDDING_API_KEY",
            "embedding_base_url": "EMBEDDING_BASE_URL",
            "embedding_model": "EMBEDDING_MODEL",
            "gemini_api_key": "GEMINI_API_KEY",
            "gemini_extraction_model": "GEMINI_EXTRACTION_MODEL",
            "_prv_a_service_account_file": "_PRV_A_SERVICE_ACCOUNT_FILE",
            # Runtime tuning
            "user_timezone": "USER_TIMEZONE",
            "ticker_poll_interval": "TICKER_POLL_INTERVAL",
            "max_concurrent_autonomous": "MAX_CONCURRENT_AUTONOMOUS",
            "tool_timeout": "TOOL_TIMEOUT",
            "lock_timeout": "LOCK_TIMEOUT",
            "todo_auto_archive_days": "TODO_AUTO_ARCHIVE_DAYS",
            # Infrastructure (persist but need restart)
            "redis_url": "REDIS_URL",
            "redis_enabled": "REDIS_ENABLED",
            "postgres_uri": "POSTGRES_URI",
            "nymeria_data_dir": "NYMERIA_DATA_DIR",
            # Platform tokens (persist but need restart)
            "discord_bot_token": "DISCORD_BOT_TOKEN",
            "discord_webhook_url": "DISCORD_WEBHOOK_URL",
            "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
            "telegram_default_chat_id": "TELEGRAM_DEFAULT_CHAT_ID",
        }

        # Settings that are persisted but only take effect after /restart api
        restart_required_keys = {
            "redis_url", "redis_enabled", "postgres_uri", "nymeria_data_dir",
            "discord_bot_token", "discord_webhook_url",
            "telegram_bot_token", "telegram_default_chat_id",
        }

        # Get updates as dict, excluding None values
        updates_dict = {k: v for k, v in updates.model_dump().items() if v is not None}

        if not updates_dict:
            return {"message": "No updates provided", "restart_required": False}

        # Update or add lines
        updated_vars = set()
        new_lines = []

        for line in existing_lines:
            # Check if this line sets a variable we're updating
            updated = False
            for setting_name, env_var in env_mapping.items():
                if setting_name in updates_dict and line.startswith(f"{env_var}="):
                    value = updates_dict[setting_name]
                    if isinstance(value, bool):
                        value = str(value).lower()
                    new_lines.append(f"{env_var}={value}")
                    updated_vars.add(setting_name)
                    updated = True
                    break

            if not updated:
                new_lines.append(line)

        # Add any new variables that weren't in the file
        for setting_name, value in updates_dict.items():
            if setting_name not in updated_vars:
                env_var = env_mapping.get(setting_name)
                if env_var:
                    if isinstance(value, bool):
                        value = str(value).lower()
                    new_lines.append(f"{env_var}={value}")

        # Write back to .env
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

        # Hot-reload: sync ALL mapped env vars from the .env file we just wrote,
        # not just the ones in this update. This ensures Pydantic Settings
        # (which prioritizes os.environ over .env files) sees the correct values
        # even for settings that weren't part of this PATCH request.
        for line in new_lines:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                key = key.strip()
                # Only update env vars that are in our mapping
                if key in {v for v in env_mapping.values()}:
                    os.environ[key] = val

        # Clear cached settings and create fresh instance
        get_settings.cache_clear()
        new_settings = get_settings()
        logger.info(f"[SETTINGS] After hot-reload: TTS_PROVIDER={new_settings.tts_provider}, STT_PROVIDER={new_settings.stt_provider}, env TTS_PROVIDER={os.environ.get('TTS_PROVIDER')}")

        # Update agent's settings reference and rebuild graphs
        agent = get_agent()
        agent.settings = new_settings

        # Check if LLM-related settings changed (need graph rebuild)
        llm_fields = {"llm_provider", "llm_model", "llm_temperature",
                       "llm_max_tokens", "llm_top_p", "llm_top_k",
                       "llm_frequency_penalty", "llm_presence_penalty",
                       "llm_reasoning_effort", "llm_extended_thinking",
                       "llm_use_model_defaults", "llm_base_url",
                       "openai_api_mode", "llm_stream_max_retries",
                       "llm_stream_retry_initial_delay",
                       "llm_stream_retry_max_delay"}
        graph_fields = llm_fields | {"tool_output_max_chars"}
        if graph_fields & set(updates_dict.keys()):
            # Clear graph caches so they rebuild with new LLM config
            with agent._graph_cache_lock:
                agent._user_graphs.clear()
                agent._async_user_graphs.clear()
            # Rebuild default graphs
            agent._default_graph = agent._build_graph_with_prompt(agent._base_system_prompt)
            agent._default_async_graph = agent._build_async_graph_with_prompt(agent._base_system_prompt)
            logger.info(f"Hot-reloaded graph settings: {graph_fields & set(updates_dict.keys())}")

        needs_restart = bool(restart_required_keys & set(updates_dict.keys()))
        return {
            "message": "Settings updated and applied" + (
                " (some changes require /restart api to take effect)"
                if needs_restart else ""
            ),
            "updated": list(updates_dict.keys()),
            "restart_required": needs_restart,
        }

    # ========================================================================
    # Environment Variables Endpoint
    # ========================================================================

    @app.get("/settings/env", tags=["Settings"])
    async def get_env_vars(
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings),
    ):
        """Get all settable environment variables with masked sensitive values.
        Admin-only — even masked values reveal the shape of every secret.

        Returns a list of env var entries with name, env_var, value (masked
        for secrets), and category.
        """
        # Keys that should be masked in /env show
        secret_keys = {
            "nymeria_api_key",
            "openai_api_key", "anthropic_api_key", "anthropic_direct_api_key",
            "openrouter_api_key", "perplexity_api_key", "gemini_api_key",
            "discord_bot_token", "discord_webhook_url",
            "telegram_bot_token",
            "twitch_client_secret", "twitch_bot_access_token",
            "twitch_bot_refresh_token", "twitch_broadcaster_token",
            "twitch_broadcaster_refresh_token",
            "slack_bot_token",
            "postgres_uri", "redis_url",
            "tts_api_key", "stt_api_key",
            "fcm_credentials_json",
        }

        # Category groupings for display
        categories = {
            "LLM": [
                "llm_provider", "llm_model", "llm_temperature", "llm_max_tokens",
                "llm_top_p", "llm_top_k", "llm_frequency_penalty",
                "llm_presence_penalty", "llm_reasoning_effort",
                "llm_extended_thinking", "llm_use_model_defaults", "llm_base_url",
                "openai_api_mode", "llm_stream_max_retries",
                "llm_stream_retry_initial_delay", "llm_stream_retry_max_delay",
            ],
            "API Keys": [
                "nymeria_api_key", "openai_api_key", "anthropic_api_key",
                "anthropic_direct_api_key", "openrouter_api_key",
                "perplexity_api_key", "perplexity_search_model",
                "gemini_api_key", "gemini_extraction_model",
            ],
            "Context": [
                "context_management", "compact_threshold", "compact_keep_messages",
                "compact_model", "sliding_window_cycles",
            ],
            "System": [
                "log_level", "watchdog_enabled", "watchdog_interval_minutes",
                "user_timezone", "nymeria_data_dir",
                "tool_timeout", "tool_output_max_chars", "lock_timeout",
            ],
            "Tasks": [
                "ticker_poll_interval", "max_concurrent_autonomous",
                "todo_staleness_minutes", "todo_auto_archive_days",
                "activity_retention_hours",
            ],
            "Voice": [
                "tts_provider", "tts_base_url", "tts_api_key", "tts_model",
                "tts_voice", "tts_output_format", "tts_speed",
                "stt_provider", "stt_base_url", "stt_api_key", "stt_model",
                "stt_language", "voice_default_thread_id",
            ],
            "Infrastructure": [
                "redis_url", "redis_enabled", "postgres_uri",
            ],
            "Discord": [
                "discord_bot_token", "discord_webhook_url",
            ],
            "Telegram": [
                "telegram_bot_token", "telegram_default_chat_id",
            ],
            "Twitch": [
                "twitch_client_id", "twitch_client_secret",
                "twitch_bot_access_token", "twitch_bot_refresh_token",
                "twitch_bot_user_id", "twitch_broadcaster_token",
                "twitch_broadcaster_refresh_token", "twitch_channel",
                "twitch_buffer_size", "twitch_pulse_enabled",
                "twitch_pulse_interval", "twitch_respond_mode",
            ],
        }

        def mask_value(val: str) -> str:
            """Mask a secret value, showing first 4 and last 3 chars."""
            s = str(val)
            if len(s) <= 10:
                return s[:2] + "..." + s[-1:] if len(s) > 3 else "***"
            return s[:4] + "..." + s[-3:]

        entries = []
        for category, keys in categories.items():
            for key in keys:
                val = getattr(settings, key, None)
                env_var = key.upper()
                is_secret = key in secret_keys
                display_val = None
                if val is not None:
                    display_val = mask_value(str(val)) if is_secret else str(val)
                entries.append({
                    "name": key,
                    "env_var": env_var,
                    "value": display_val,
                    "is_set": val is not None and str(val) != "",
                    "is_secret": is_secret,
                    "category": category,
                })

        return {"entries": entries}

    @app.get("/settings/env/{key}", tags=["Settings"])
    async def get_env_var(
        key: str,
        user: AuthenticatedUser = Depends(require_admin_user),
        settings: Settings = Depends(get_settings),
    ):
        """Get a single environment variable's unmasked value. Admin-only —
        returns raw secrets including API keys and bot tokens."""
        val = getattr(settings, key, None)
        if val is None:
            # Also try looking up by env var name (uppercase)
            key_lower = key.lower()
            val = getattr(settings, key_lower, None)
            if val is None:
                raise HTTPException(status_code=404, detail=f"Unknown setting: {key}")
            key = key_lower
        return {"name": key, "env_var": key.upper(), "value": str(val) if val is not None else None}

    # ========================================================================
    # Model Metadata Endpoint
    # ========================================================================

    @app.get("/models", tags=["Settings"])
    async def get_openrouter_models(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Return cached OpenRouter model metadata for frontend enrichment."""
        from ..config.model_capabilities import list_all_models

        models = list_all_models()
        return [
            {
                "id": m.id,
                "name": m.name,
                "context_length": m.context_length,
                "max_completion_tokens": m.max_completion_tokens,
                "pricing_prompt": m.pricing_prompt,
                "pricing_completion": m.pricing_completion,
                "supported_parameters": sorted(m.supported_parameters),
                "input_modalities": sorted(m.input_modalities),
                "tokenizer": m.tokenizer,
                "default_temperature": m.default_temperature,
                "default_top_p": m.default_top_p,
                "default_frequency_penalty": m.default_frequency_penalty,
            }
            for m in models
        ]

    @app.get("/models/available", tags=["Settings"])
    async def get_available_models(
        provider: Optional[str] = Query(default=None, description="Provider to fetch models for (anthropic, openai). Defaults to global provider."),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Fetch available models from the configured LLM provider or CLIProxy.

        Queries the provider's /v1/models endpoint (or CLIProxy which aggregates
        all provider models). Returns a simplified list for frontend dropdowns.
        """
        import httpx

        settings = get_settings()
        effective_provider = provider or settings.llm_provider

        # Determine base URL and API key
        base_url = settings.llm_base_url
        if effective_provider == "anthropic":
            api_key = settings.anthropic_direct_api_key or settings.anthropic_api_key
            if not base_url:
                base_url = "https://api.anthropic.com"
        elif effective_provider == "openai":
            api_key = settings.openai_api_key
            if not base_url:
                base_url = "https://api.openai.com"
        else:
            # OpenRouter or other — use existing /models endpoint
            return []

        # When using CLIProxy, always use the global provider's key
        if settings.llm_base_url:
            api_key = settings.get_api_key_for_provider()

        if not api_key:
            return []

        # Fetch from /v1/models
        headers = {
            "x-api-key": api_key,
            "Authorization": f"Bearer {api_key}",
            "anthropic-version": "2023-06-01",
        }

        # OpenAI-style base URLs end in `/v1`; Anthropic-style don't. Normalize
        # so `/v1/models` is appended exactly once regardless of which provider
        # the global LLM_BASE_URL was configured for.
        clean_base = base_url.rstrip("/")
        if clean_base.endswith("/v1"):
            clean_base = clean_base[:-3]

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                url = f"{clean_base}/v1/models"
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()

            raw_models = data.get("data", [])
            # Return simplified list sorted by ID
            result = []
            for m in sorted(raw_models, key=lambda x: x.get("id", "")):
                model_id = m.get("id", "")
                result.append({
                    "id": model_id,
                    "name": m.get("name") or model_id,
                    "owned_by": m.get("owned_by", ""),
                    "created": m.get("created"),
                })
            return result
        except Exception as e:
            logger.warning(f"Failed to fetch models from {base_url}: {e}")
            return []

    # ========================================================================
    # Dashboard Endpoints
    # ========================================================================

    @app.get("/todos/thread-counts", tags=["Dashboard"])
    async def get_thread_task_counts(
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """Get active task count per thread for badge display."""
        todo_manager = TodoManager(settings.data_dir)
        todo_list = todo_manager.get_todos(user_id)
        return todo_list.get_thread_task_counts()

    @app.get("/todos/users", tags=["Dashboard"])
    async def list_users_with_todos(
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        List all user IDs that have TODO lists.

        Used by the watchdog worker to discover users to scan for stale TODOs
        without having to crawl the data directory itself.
        """
        todo_manager = TodoManager(settings.data_dir)
        return todo_manager.get_all_users_with_todos()

    @app.get("/todos", response_model=TodoListResponse, tags=["Dashboard"])
    async def get_todos(
        user_id: str = Depends(_authed_user_id),
        filter_status: Optional[str] = Query(default=None, description="Filter by status"),
        thread_id: Optional[str] = Query(default=None, description="Filter by thread ID"),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Get TODO items for a user.

        Returns all active TODOs by default. Use filter_status to filter by specific status.
        Optionally filter by thread_id.
        """
        todo_manager = TodoManager(settings.data_dir)
        todo_list = todo_manager.get_todos(user_id)

        # Filter items
        if filter_status == "all":
            items = todo_list.items
        elif filter_status:
            try:
                status = TodoStatus(filter_status.lower())
                items = [i for i in todo_list.items if i.status == status]
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid status filter '{filter_status}'",
                )
        else:
            # Default: active (non-done) items
            items = todo_list.get_active_todos()

        # Filter by thread_id if provided
        if thread_id:
            items = [i for i in items if i.thread_id == thread_id]

        # Sort: in_progress first, then pending, then done; then by created_at
        from ..core.todo_constants import STATUS_ORDER
        sorted_items = sorted(
            items,
            key=lambda i: (STATUS_ORDER.get(i.status, 3), i.created_at),
        )

        return TodoListResponse(
            user_id=user_id,
            items=[_todo_to_response(item) for item in sorted_items],
            total=len(sorted_items),
        )

    def _parse_scheduled_for(scheduled_for: Optional[str]) -> Optional[datetime]:
        """
        Parse scheduled_for string to a timezone-aware UTC datetime.

        Supports:
        - Relative times: "30s", "30m", "2h", "1d", "1w"
        - ISO datetime with Z suffix (e.g., "2024-01-01T12:00:00.000Z") - parsed as UTC
        - ISO datetime without Z (e.g., "2024-01-01T12:00") - interpreted as LOCAL time
        """
        import re

        if not scheduled_for:
            return None

        scheduled_for = scheduled_for.strip()
        logger.info(f"[API] Parsing scheduled_for: '{scheduled_for}'")

        # Try relative time parsing first: 30s, 5m, 1h, 1d, 1w
        match = re.match(r'^(\d+)(s|m|h|d|w)$', scheduled_for.lower())
        if match:
            value = int(match.group(1))
            unit = match.group(2)
            multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
            seconds = value * multipliers[unit]
            # Use timezone-aware UTC datetime to avoid timestamp() interpretation issues
            result = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            logger.info(f"[API] Parsed relative time '{scheduled_for}' -> {result} (UTC), timestamp={result.timestamp()}")
            return result

        # Handle ISO strings with Z suffix (UTC) - from frontend toISOString()
        if scheduled_for.endswith('Z'):
            utc_formats = [
                "%Y-%m-%dT%H:%M:%S.%fZ",  # With milliseconds: 2024-01-01T12:00:00.000Z
                "%Y-%m-%dT%H:%M:%SZ",      # Without milliseconds: 2024-01-01T12:00:00Z
            ]
            for fmt in utc_formats:
                try:
                    result = datetime.strptime(scheduled_for, fmt).replace(tzinfo=timezone.utc)
                    logger.info(f"[API] Parsed UTC time '{scheduled_for}' -> {result} (UTC), timestamp={result.timestamp()}")
                    return result
                except ValueError:
                    continue

        # Try absolute formats - these are interpreted as LOCAL time, then converted to UTC
        formats = [
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H:%M:%S",
        ]

        for fmt in formats:
            try:
                # Parse as naive datetime (assumed local time from user's browser)
                local_dt = datetime.strptime(scheduled_for, fmt)
                # Convert to UTC by assuming it's in the system's local timezone
                local_dt = local_dt.astimezone()  # Add local timezone info
                result = local_dt.astimezone(timezone.utc)  # Convert to UTC
                logger.info(f"[API] Parsed absolute time '{scheduled_for}' -> local={local_dt}, UTC={result}, timestamp={result.timestamp()}")
                return result
            except ValueError:
                continue

        raise HTTPException(
            status_code=400,
            detail=f"Invalid scheduled_for format: '{scheduled_for}'. Use relative (e.g., '30m', '2h') or datetime (e.g., '2024-03-15 14:00')."
        )

    def _todo_to_response(item: TodoItem) -> TodoItemResponse:
        """Convert a TodoItem to TodoItemResponse."""
        return TodoItemResponse(
            id=item.id,
            task=item.task,
            status=item.status.value,
            created_at=item.created_at,
            updated_at=item.updated_at,
            notes=item.notes,
            scheduled_for=item.scheduled_for,
            thread_id=item.thread_id,
            last_execution=item.last_execution,
            created_by=item.created_by,
            recurrence=item.recurrence,
        )

    def _get_todo_schedule_db(settings: Settings):
        """Create the schedule DB handle used by TODO routes."""
        from ..core.todo_schedule_db import TodoScheduleDB

        return TodoScheduleDB(settings.data_dir / "todo_schedule.db")

    def _raise_if_todo_executing(schedule_db, todo_id: str, user_id: str) -> None:
        """Reject user-facing TODO writes while a scheduled run owns the TODO."""
        if schedule_db.is_execution_active(todo_id, user_id):
            raise HTTPException(
                status_code=409,
                detail=f"TODO '{todo_id}' is currently executing; try again after the run finishes.",
            )

    @app.post("/todos", response_model=TodoItemResponse, tags=["Dashboard"])
    async def create_todo(
        request: TodoCreateRequest,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Create a new TODO item.

        The TODO is created by the user (created_by='user').
        """
        from ..core.todo_schedule_db import TodoScheduleDB
        from ..core.todo_constants import VALID_RECURRENCES

        todo_manager = TodoManager(settings.data_dir)

        # Validate recurrence
        if request.recurrence and request.recurrence.lower() not in VALID_RECURRENCES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid recurrence: '{request.recurrence}'. Use: {', '.join(VALID_RECURRENCES)}"
            )

        # Parse scheduled_for
        scheduled_for = _parse_scheduled_for(request.scheduled_for)

        # Default thread_id from request, fallback to user-scoped default
        todo_thread_id = request.thread_id or f"default-{user_id}"

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.add_item(
                task=request.task,
                notes=request.notes,
                scheduled_for=scheduled_for,
                thread_id=todo_thread_id,
                created_by="user",
                recurrence=request.recurrence.lower() if request.recurrence else None,
            )

            if item is None:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot create TODO: maximum limit reached"
                )

            # Store item data for after context exits
            created_item = item

        # Sync schedule AFTER atomic_update saves the file
        # (sync_schedule_to_db reads from disk, so file must be saved first)
        if created_item.scheduled_for:
            logger.info(f"[API] TODO {created_item.id} has scheduled_for={created_item.scheduled_for}, syncing to schedule DB")
            schedule_db = TodoScheduleDB(settings.data_dir / "todo_schedule.db")
            todo_manager.sync_schedule_to_db(user_id, created_item.id, schedule_db)
            logger.info(f"[API] Schedule synced for TODO {created_item.id}")
        else:
            logger.info(f"[API] TODO {created_item.id} has no schedule, skipping sync")

        return _todo_to_response(created_item)

    @app.patch("/todos/{todo_id}", response_model=TodoItemResponse, tags=["Dashboard"])
    async def update_todo(
        todo_id: str,
        request: TodoUpdateRequest,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Update an existing TODO item.
        """
        from ..core.todo_constants import VALID_RECURRENCES

        todo_manager = TodoManager(settings.data_dir)
        schedule_db = _get_todo_schedule_db(settings)

        # Parse status
        status = None
        if request.status:
            try:
                status = TodoStatus(request.status.lower())
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid status: '{request.status}'. Use: pending, in_progress, done"
                )

        # Validate recurrence
        recurrence = None
        if request.recurrence and not request.clear_recurrence:
            if request.recurrence.lower() not in VALID_RECURRENCES:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid recurrence: '{request.recurrence}'. Use: {', '.join(VALID_RECURRENCES)}"
                )
            recurrence = request.recurrence.lower()

        # Parse scheduled_for
        scheduled_for = None
        if request.scheduled_for and not request.clear_schedule:
            scheduled_for = _parse_scheduled_for(request.scheduled_for)

        existing = todo_manager.get_todos(user_id).get_item(todo_id)
        if not existing:
            raise HTTPException(
                status_code=404,
                detail=f"TODO '{todo_id}' not found"
            )
        _raise_if_todo_executing(schedule_db, todo_id, user_id)

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found"
                )

            _raise_if_todo_executing(schedule_db, todo_id, user_id)

            success = todo_list.update_item(
                todo_id=todo_id,
                task=request.task,
                status=status,
                notes=request.notes,
                scheduled_for=scheduled_for,
                clear_schedule=request.clear_schedule,
                thread_id=request.thread_id,
                recurrence=recurrence,
                clear_recurrence=request.clear_recurrence,
            )

            if not success:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found"
                )

            # Auto-reschedule recurring TODOs marked as done
            if status == TodoStatus.DONE:
                updated = todo_list.get_item(todo_id)
                if updated and updated.recurrence:
                    from ..core.todo_constants import RECURRENCE_DELTAS
                    delta = RECURRENCE_DELTAS.get(updated.recurrence)
                    if delta:
                        next_execution = datetime.now(timezone.utc) + delta
                        todo_list.update_item(
                            todo_id,
                            scheduled_for=next_execution,
                            status=TodoStatus.PENDING,
                        )
                        updated.last_execution = datetime.now(timezone.utc)
                        logger.info(f"[API] Auto-rescheduled recurring TODO {todo_id} for {next_execution}")

            # Re-fetch the updated item (still in memory)
            updated_item = todo_list.get_item(todo_id)

        # Sync schedule AFTER atomic_update saves the file
        # (sync_schedule_to_db reads from disk, so file must be saved first)
        todo_manager.sync_schedule_to_db(user_id, updated_item.id, schedule_db)
        logger.info(f"[API] Schedule synced for TODO {updated_item.id}")

        return _todo_to_response(updated_item)

    @app.delete("/todos/{todo_id}", tags=["Dashboard"])
    async def delete_todo(
        todo_id: str,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Delete a TODO item.
        """
        todo_manager = TodoManager(settings.data_dir)
        schedule_db = _get_todo_schedule_db(settings)

        existing = todo_manager.get_todos(user_id).get_item(todo_id)
        if not existing:
            raise HTTPException(
                status_code=404,
                detail=f"TODO '{todo_id}' not found"
            )
        _raise_if_todo_executing(schedule_db, todo_id, user_id)

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found"
                )

            _raise_if_todo_executing(schedule_db, todo_id, user_id)

            deleted = todo_list.delete_item(todo_id)
            if not deleted:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found"
                )

            # Remove from schedule if it was scheduled
            schedule_db.remove_scheduled(todo_id)

            return {"status": "ok", "deleted_id": todo_id}

    @app.post("/todos/{todo_id}/complete", response_model=TodoItemResponse, tags=["Dashboard"])
    async def complete_todo(
        todo_id: str,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Mark a TODO item as done.
        """
        todo_manager = TodoManager(settings.data_dir)
        schedule_db = _get_todo_schedule_db(settings)

        existing = todo_manager.get_todos(user_id).get_item(todo_id)
        if not existing:
            raise HTTPException(
                status_code=404,
                detail=f"TODO '{todo_id}' not found"
            )
        _raise_if_todo_executing(schedule_db, todo_id, user_id)

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found"
                )

            _raise_if_todo_executing(schedule_db, todo_id, user_id)

            has_recurrence = item.recurrence
            success = todo_list.complete_item(todo_id)
            if not success:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found"
                )

            # Auto-reschedule recurring TODOs
            if has_recurrence:
                from ..core.todo_constants import RECURRENCE_DELTAS
                delta = RECURRENCE_DELTAS.get(has_recurrence)
                if delta:
                    next_execution = datetime.now(timezone.utc) + delta
                    todo_list.update_item(
                        todo_id,
                        scheduled_for=next_execution,
                        status=TodoStatus.PENDING,
                    )
                    refreshed = todo_list.get_item(todo_id)
                    if refreshed:
                        refreshed.last_execution = datetime.now(timezone.utc)
                    logger.info(f"[API] Auto-rescheduled recurring TODO {todo_id} for {next_execution}")

            # Re-fetch the updated item
            item = todo_list.get_item(todo_id)

            # Sync schedule
            if has_recurrence and item.scheduled_for:
                todo_manager.sync_schedule_to_db(user_id, todo_id, schedule_db)
            else:
                schedule_db.remove_scheduled(todo_id)

            return _todo_to_response(item)

    @app.get("/activity", response_model=ActivityLogResponse, tags=["Dashboard"])
    async def get_activity(
        user_id: str = Depends(_authed_user_id),
        limit: int = Query(default=50, le=100, description="Max entries to return"),
        activity_type: Optional[str] = Query(default=None, description="Filter by type"),
        thread_id: Optional[str] = Query(default=None, description="Filter by thread ID"),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Get activity log for a user.

        Returns recent activity entries, newest first.
        Optionally filter by thread_id.
        """
        activity_log = get_activity_log()

        # Parse activity type filter
        type_filter = None
        if activity_type:
            try:
                type_filter = ActivityType(activity_type)
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid activity type '{activity_type}'",
                )

        entries = activity_log.get_entries(
            user_id, limit=limit, activity_type=type_filter, thread_id=thread_id
        )

        return ActivityLogResponse(
            entries=[
                ActivityEntryResponse(
                    id=entry.id,
                    timestamp=entry.timestamp,
                    type=entry.type.value,
                    message=entry.message,
                    thread_id=entry.thread_id,
                    metadata=entry.metadata,
                )
                for entry in entries
            ],
            total=len(entries),
        )

    @app.get("/notifications", response_model=NotificationsListResponse, tags=["Dashboard"])
    async def get_notifications(
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Get notifications for a user.

        Returns all notifications with unread count.
        """
        store = get_notification_store()
        notifications = store.get_all(user_id, limit=50)
        unread_count = store.get_unread_count(user_id)

        return NotificationsListResponse(
            notifications=[
                NotificationResponse(
                    id=n.id,
                    summary=n.summary,
                    thread_id=n.thread_id,
                    task_id=n.task_id,
                    created_at=n.created_at,
                    read=n.read,
                )
                for n in notifications
            ],
            unread_count=unread_count,
        )

    @app.post("/notifications/{notification_id}/read", tags=["Dashboard"])
    async def mark_notification_read(
        notification_id: str,
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Mark a notification as read.
        """
        store = get_notification_store()
        success = store.mark_read(notification_id, user_id)

        if not success:
            raise HTTPException(
                status_code=404,
                detail=f"Notification '{notification_id}' not found",
            )

        return {"status": "ok", "notification_id": notification_id}

    @app.post("/notifications/read-all", tags=["Dashboard"])
    async def mark_all_notifications_read(
        user_id: str = Depends(_authed_user_id),
        user: AuthenticatedUser = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Mark all notifications as read for a user.
        """
        store = get_notification_store()
        count = store.mark_all_read(user_id)

        return {"status": "ok", "marked_read": count}

    @app.get("/autonomous/stream", tags=["Autonomous"])
    async def stream_autonomous_events(
        request: Request,
        user_id: str = Query(default="default", description="User ID to filter events"),
        api_key: Optional[str] = Query(default=None, description="Legacy API key fallback for clients that cannot send Authorization headers."),
        client_id: Optional[str] = Query(default=None, description="Client ID for origin filtering (prevents seeing own sync events)"),
        authorization: Optional[str] = Header(None),
        x_nymeria_act_as: Optional[str] = Header(None),
        settings: Settings = Depends(get_settings),
    ):
        # Auth: prefer Authorization header, fall back to ?api_key= for
        # legacy EventSource-style clients that cannot set custom headers.
        # Both paths accept any valid account token (``nym_...``).
        presented: Optional[str] = None
        if authorization:
            parts = authorization.split()
            if len(parts) == 2 and parts[0].lower() == "bearer":
                presented = parts[1]
        if presented is None:
            presented = api_key

        authorized = False
        firehose = False  # admin + X-Nymeria-Act-As:* streams every user's events
        if presented:
            try:
                repo_user = get_agent().accounts_repo.verify_token(presented)
                if repo_user is not None:
                    authorized = True
                    # Admin-role callers may use X-Nymeria-Act-As to stream
                    # another user's events — or "*" for the full firehose
                    # (used by bot thin clients that route events to Discord /
                    # Telegram channels by thread_id prefix, regardless of
                    # which user's autonomous task produced them).
                    if x_nymeria_act_as:
                        if repo_user.role != "admin":
                            raise HTTPException(status_code=403, detail="Act-As requires admin")
                        if x_nymeria_act_as == "*":
                            firehose = True
                            user_id = "*"
                        else:
                            user_id = x_nymeria_act_as
                    else:
                        # Non-admin clients can only stream their own events;
                        # admins without act-as default to their own stream.
                        user_id = repo_user.id
            except HTTPException:
                raise
            except Exception:
                authorized = False

        if not authorized:
            raise HTTPException(status_code=401, detail="Invalid API key")
        """
        Stream autonomous task events and cross-client sync events via Server-Sent Events.

        Emits events when Nymeria executes scheduled tasks autonomously, when other
        clients send interactive chat messages, or when thread metadata changes.

        Events include: task_started, thinking, tool_call, tool_result, response, task_completed,
        message_added, thread_updated, thread_created, thread_deleted

        Connect to this endpoint to receive real-time updates about all activity.
        """
        import asyncio
        from queue import Empty

        subscriber_id = str(uuid.uuid4())
        event_bus = get_event_bus()
        queue = event_bus.subscribe(subscriber_id)
        logger.info(
            "[AUTONOMOUS SSE] subscriber_connect subscriber=%s user=%s firehose=%s "
            "client_id=%s local_subscribers=%d",
            subscriber_id[:8],
            user_id,
            firehose,
            client_id[:8] if client_id else "none",
            event_bus.get_subscriber_count(),
        )

        async def event_generator():
            """Generate SSE events from the event bus."""
            received_counts: Dict[str, int] = {}
            yielded_counts: Dict[str, int] = {}
            filtered_user_counts: Dict[str, int] = {}
            filtered_origin_counts: Dict[str, int] = {}

            def bump(counter: Dict[str, int], key: str) -> int:
                counter[key] = counter.get(key, 0) + 1
                return counter[key]

            try:
                while True:
                    # Check if client disconnected
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
                        # Non-blocking check for events
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

                        # Filter by user_id unless the caller requested the
                        # firehose (admin + X-Nymeria-Act-As: *). "default" as
                        # a query value historically meant "all" — still
                        # honored for backward compat with older browser
                        # clients, but the authenticated path is authoritative.
                        if not firehose and user_id != "default" and event.user_id != user_id:
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

                        # Skip events that originated from this client (dedup)
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

                        # Build SSE payload, stripping internal fields and reserved keys
                        # (event.data may contain a "type" key from the original chunk —
                        #  we use event.event_type as the canonical type to preserve
                        #  the interactive_ prefix for sync events)
                        payload = {
                            k: v for k, v in event.data.items()
                            if not k.startswith("_") and k not in ("type", "thread_id", "task_id", "timestamp")
                        }
                        event_data = {
                            "type": event.event_type,
                            "thread_id": event.thread_id,
                            "task_id": event.task_id,
                            "timestamp": event.timestamp.isoformat(),
                            **payload,
                        }
                        serialized = json.dumps(event_data)
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
                        # No events, send heartbeat to keep connection alive
                        yield f": heartbeat\n\n"
                        await asyncio.sleep(1)

            finally:
                event_bus.unsubscribe(subscriber_id)
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

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # ========================================================================
    # Custom Tools Endpoints
    # ========================================================================

    def _tool_definition_to_response(defn: CustomToolDefinition) -> CustomToolResponse:
        """Convert a CustomToolDefinition to API response format."""
        return CustomToolResponse(
            id=defn.id,
            name=defn.name,
            description=defn.description,
            parameters={
                k: ToolParameterModel(
                    type=v.type,
                    description=v.description,
                    required=v.required,
                    default=str(v.default) if v.default is not None else None,
                    enum=v.enum,
                )
                for k, v in defn.parameters.items()
            },
            implementation_type=defn.implementation_type,
            http_config=HTTPToolConfigModel(
                method=defn.http_config.method,
                url=defn.http_config.url,
                headers=defn.http_config.headers,
                body_template=defn.http_config.body_template,
                query_params=defn.http_config.query_params,
                timeout_seconds=defn.http_config.timeout_seconds,
                response_path=defn.http_config.response_path,
                response_format=defn.http_config.response_format,
            ) if defn.http_config else None,
            mcp_config=MCPToolConfigModel(
                server_command=defn.mcp_config.server_command,
                server_args=defn.mcp_config.server_args,
                tool_name=defn.mcp_config.tool_name,
                env_vars=defn.mcp_config.env_vars,
                working_directory=defn.mcp_config.working_directory,
                idle_timeout_seconds=defn.mcp_config.idle_timeout_seconds,
                startup_timeout_seconds=defn.mcp_config.startup_timeout_seconds,
            ) if defn.mcp_config else None,
            enabled=defn.enabled,
            tags=defn.tags,
            created_at=defn.created_at,
            updated_at=defn.updated_at,
        )

    @app.get("/tools/custom", response_model=CustomToolListResponse, tags=["Custom Tools"])
    async def list_custom_tools(
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """List all custom tools. Admin-only — definitions include URL
        templates, headers (with ``${env:VAR}`` interpolation hints) and
        local subprocess commands; non-admins should not enumerate them."""
        loader = get_custom_tool_loader()
        definitions = loader.get_all_definitions()

        return CustomToolListResponse(
            tools=[_tool_definition_to_response(d) for d in definitions],
            total=len(definitions),
        )

    @app.post("/tools/custom", response_model=CustomToolResponse, tags=["Custom Tools"])
    async def create_custom_tool(
        request: CustomToolCreateRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Create a new custom tool. Admin-only — custom tools register
        global HTTP/MCP entries that every user's agent can call, so
        non-admins must not be able to mint them."""
        loader = get_custom_tool_loader()

        # Check if tool ID already exists
        existing = loader.get_definition(request.id)
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Tool with ID '{request.id}' already exists",
            )

        # Build the definition
        try:
            http_config = None
            mcp_config = None

            if request.implementation_type == "http":
                if not request.http_config:
                    raise HTTPException(
                        status_code=400,
                        detail="http_config is required for HTTP tools",
                    )
                http_config = HTTPToolConfig(
                    method=request.http_config.method,
                    url=request.http_config.url,
                    headers=request.http_config.headers,
                    body_template=request.http_config.body_template,
                    query_params=request.http_config.query_params,
                    timeout_seconds=request.http_config.timeout_seconds,
                    response_path=request.http_config.response_path,
                    response_format=request.http_config.response_format,
                )
            elif request.implementation_type == "mcp":
                if not request.mcp_config:
                    raise HTTPException(
                        status_code=400,
                        detail="mcp_config is required for MCP tools",
                    )
                mcp_config = MCPToolConfig(
                    server_command=request.mcp_config.server_command,
                    server_args=request.mcp_config.server_args,
                    tool_name=request.mcp_config.tool_name,
                    env_vars=request.mcp_config.env_vars,
                    working_directory=request.mcp_config.working_directory,
                    idle_timeout_seconds=request.mcp_config.idle_timeout_seconds,
                    startup_timeout_seconds=request.mcp_config.startup_timeout_seconds,
                )

            definition = CustomToolDefinition(
                id=request.id,
                name=request.name,
                description=request.description,
                parameters={
                    k: ToolParameter(
                        type=v.type,
                        description=v.description,
                        required=v.required,
                        default=v.default,
                        enum=v.enum,
                    )
                    for k, v in request.parameters.items()
                },
                implementation_type=request.implementation_type,
                http_config=http_config,
                mcp_config=mcp_config,
                enabled=request.enabled,
                tags=request.tags,
            )

            loader.save_definition(definition)

            # Reload tools to make the new tool available
            agent = get_agent()
            agent.reload_tools()

            return _tool_definition_to_response(definition)

        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/tools/custom/{tool_id}", response_model=CustomToolResponse, tags=["Custom Tools"])
    async def get_custom_tool(
        tool_id: str,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Get a custom tool by ID. Admin-only — same secret-leakage
        concerns as the list endpoint."""
        loader = get_custom_tool_loader()
        definition = loader.get_definition(tool_id)

        if not definition:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        return _tool_definition_to_response(definition)

    @app.put("/tools/custom/{tool_id}", response_model=CustomToolResponse, tags=["Custom Tools"])
    async def update_custom_tool(
        tool_id: str,
        request: CustomToolUpdateRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Update an existing custom tool. Admin-only — mirrors the create
        endpoint."""
        loader = get_custom_tool_loader()
        definition = loader.get_definition(tool_id)

        if not definition:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        # Update fields
        if request.name is not None:
            definition.name = request.name
        if request.description is not None:
            definition.description = request.description
        if request.parameters is not None:
            definition.parameters = {
                k: ToolParameter(
                    type=v.type,
                    description=v.description,
                    required=v.required,
                    default=v.default,
                    enum=v.enum,
                )
                for k, v in request.parameters.items()
            }
        if request.enabled is not None:
            definition.enabled = request.enabled
        if request.tags is not None:
            definition.tags = request.tags

        # Update config based on type
        if request.http_config is not None and definition.implementation_type == "http":
            definition.http_config = HTTPToolConfig(
                method=request.http_config.method,
                url=request.http_config.url,
                headers=request.http_config.headers,
                body_template=request.http_config.body_template,
                query_params=request.http_config.query_params,
                timeout_seconds=request.http_config.timeout_seconds,
                response_path=request.http_config.response_path,
                response_format=request.http_config.response_format,
            )
        if request.mcp_config is not None and definition.implementation_type == "mcp":
            definition.mcp_config = MCPToolConfig(
                server_command=request.mcp_config.server_command,
                server_args=request.mcp_config.server_args,
                tool_name=request.mcp_config.tool_name,
                env_vars=request.mcp_config.env_vars,
                working_directory=request.mcp_config.working_directory,
                idle_timeout_seconds=request.mcp_config.idle_timeout_seconds,
                startup_timeout_seconds=request.mcp_config.startup_timeout_seconds,
            )

        loader.save_definition(definition)

        # Reload tools
        agent = get_agent()
        agent.reload_tools()

        return _tool_definition_to_response(definition)

    @app.delete("/tools/custom/{tool_id}", tags=["Custom Tools"])
    async def delete_custom_tool(
        tool_id: str,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Delete a custom tool. Admin-only — mirrors the create endpoint."""
        loader = get_custom_tool_loader()

        if not loader.delete_definition(tool_id):
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        # Reload tools
        agent = get_agent()
        agent.reload_tools()

        return {"status": "ok", "deleted_id": tool_id}

    @app.post("/tools/custom/{tool_id}/test", tags=["Custom Tools"])
    async def test_custom_tool(
        tool_id: str,
        request: CustomToolTestRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Test a custom tool with sample parameters. Admin-only — this
        actually executes the upstream HTTP call or MCP subprocess, so it
        must not be reachable by a non-admin who could probe arbitrary
        URLs/commands."""
        loader = get_custom_tool_loader()
        definition = loader.get_definition(tool_id)

        if not definition:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        # Execute the tool
        try:
            if definition.implementation_type == "http":
                from ..core.custom_tools import execute_http_tool
                result = await execute_http_tool(definition.http_config, request.params)
            elif definition.implementation_type == "mcp":
                result = await loader.mcp_manager.call_tool(definition.mcp_config, request.params)
            else:
                result = f"[Error]: Unknown implementation type: {definition.implementation_type}"

            return {
                "status": "ok",
                "tool_id": tool_id,
                "result": result,
            }
        except Exception as e:
            return {
                "status": "error",
                "tool_id": tool_id,
                "error": str(e),
            }

    @app.get("/tools/custom/export", tags=["Custom Tools"])
    async def export_custom_tools(
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Export all custom tools as JSON. Admin-only — exports include
        full HTTP/MCP configs."""
        loader = get_custom_tool_loader()
        definitions = loader.get_all_definitions()

        return {
            "tools": [d.model_dump() for d in definitions],
            "total": len(definitions),
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }

    @app.post("/tools/custom/import", tags=["Custom Tools"])
    async def import_custom_tools(
        request: Request,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Import custom tools from JSON. Admin-only — same as create."""
        loader = get_custom_tool_loader()
        body = await request.json()

        tools_data = body.get("tools", [])
        imported = 0
        errors = []

        for tool_data in tools_data:
            try:
                definition = CustomToolDefinition(**tool_data)
                loader.save_definition(definition)
                imported += 1
            except Exception as e:
                errors.append(f"{tool_data.get('id', 'unknown')}: {str(e)}")

        # Reload tools
        if imported > 0:
            agent = get_agent()
            agent.reload_tools()

        return {
            "status": "ok",
            "imported": imported,
            "errors": errors,
        }

    # ========================================================================
    # MCP Server Endpoints
    # ========================================================================

    class MCPServerCreateRequest(BaseModel):
        """Request model for creating an MCP server."""
        id: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")
        name: str = Field(..., min_length=1, max_length=128)
        description: str = ""
        server_command: str = Field(..., min_length=1)
        server_args: List[str] = []
        env_vars: Dict[str, str] = {}
        working_directory: Optional[str] = None
        idle_timeout_seconds: int = 300
        startup_timeout_seconds: int = 30
        enabled: bool = True

    class MCPServerUpdateRequest(BaseModel):
        """Request model for updating an MCP server."""
        name: Optional[str] = None
        description: Optional[str] = None
        server_command: Optional[str] = None
        server_args: Optional[List[str]] = None
        env_vars: Optional[Dict[str, str]] = None
        working_directory: Optional[str] = None
        idle_timeout_seconds: Optional[int] = None
        startup_timeout_seconds: Optional[int] = None
        enabled: Optional[bool] = None

    @app.get("/mcp-servers", tags=["MCP Servers"])
    async def list_mcp_servers(user: AuthenticatedUser = Depends(require_admin_user)):
        """List all MCP server definitions with discovered tools.

        Admin-only — server definitions include ``env_vars``, ``headers``,
        ``server_command``, and ``working_directory``, which can hold
        credentials and host paths. Per-thread MCP enablement uses the
        per-tool unified API; non-admin discovery has no need for raw
        configs.
        """
        from ..core.mcp_servers import get_mcp_server_registry

        registry = get_mcp_server_registry()
        servers = registry.get_all_servers()
        return {
            "servers": [s.model_dump() for s in servers],
            "total": len(servers),
        }

    @app.post("/mcp-servers", tags=["MCP Servers"])
    async def create_mcp_server(
        request: MCPServerCreateRequest,
        thread_id: Optional[str] = Query(None, description="Auto-enable tools for this thread"),
        user: AuthenticatedUser = Depends(require_admin_caller),
    ):
        """Add a new MCP server. Admin-only — saves config and triggers tool
        discovery. MCP server install runs local stdio commands; allowing every
        authenticated user to register one would let non-admin users execute
        arbitrary commands via the agent process.

        Uses :func:`require_admin_caller` so the admin can use
        ``X-Nymeria-Act-As`` to bind tools to a specific user's thread —
        ``_require_thread_access`` below then runs ownership against that
        target (admin direct still bypasses because admin role survives
        act-as for admin-as-admin).
        """
        from ..core.mcp_servers import get_mcp_server_registry
        from ..tools.definitions.mcp_schema import MCPServerDefinition

        registry = get_mcp_server_registry()

        # Check for duplicate ID
        if registry.get_server(request.id):
            raise HTTPException(400, detail=f"MCP server '{request.id}' already exists")

        defn = MCPServerDefinition(
            id=request.id,
            name=request.name,
            description=request.description,
            server_command=request.server_command,
            server_args=request.server_args,
            env_vars=request.env_vars,
            working_directory=request.working_directory,
            idle_timeout_seconds=request.idle_timeout_seconds,
            startup_timeout_seconds=request.startup_timeout_seconds,
            enabled=request.enabled,
        )
        registry.save_server(defn)

        # Discover tools
        discovered = []
        discovery_error = None
        try:
            discovered = registry.discover_tools(request.id)
        except Exception as e:
            discovery_error = str(e)
            logger.warning(f"Tool discovery failed for MCP server '{request.id}': {e}")

        # Reload agent tools so new MCP tools are available
        agent = get_agent()
        agent.reload_mcp_server_tools()

        # If thread_id provided, auto-enable all discovered tools for that thread.
        # Admin-only endpoint already, but still gate the thread mutation: an
        # admin acting on behalf of a user (or just typo'ing a thread ID) shouldn't
        # be able to mutate a thread the caller doesn't own.
        if thread_id and discovered:
            _require_thread_access(user, thread_id)
            tc = agent.thread_config_manager.get_config(thread_id)
            enabled_tools = list(tc.enabled_tools) if tc and tc.enabled_tools else []
            for dt in discovered:
                tool_name = f"mcp__{request.id}__{dt.name}"
                if tool_name not in enabled_tools:
                    enabled_tools.append(tool_name)
            agent.thread_config_manager.update_config(thread_id, enabled_tools=enabled_tools)
            agent.invalidate_thread_config_cache(thread_id)

        result = {
            "status": "ok",
            "server": registry.get_server(request.id).model_dump(),
            "discovered_tools": len(discovered),
        }
        if discovery_error:
            result["discovery_error"] = discovery_error
        if thread_id:
            result["thread_id"] = thread_id

        return result

    @app.get("/mcp-servers/{server_id}", tags=["MCP Servers"])
    async def get_mcp_server(
        server_id: str,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Get a specific MCP server definition. Admin-only — same secret
        leakage concerns as the list endpoint."""
        from ..core.mcp_servers import get_mcp_server_registry

        registry = get_mcp_server_registry()
        defn = registry.get_server(server_id)
        if not defn:
            raise HTTPException(404, detail=f"MCP server '{server_id}' not found")
        return defn.model_dump()

    @app.put("/mcp-servers/{server_id}", tags=["MCP Servers"])
    async def update_mcp_server(
        server_id: str,
        request: MCPServerUpdateRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Update an MCP server config. Admin-only — re-discovers tools after update."""
        from ..core.mcp_servers import get_mcp_server_registry

        registry = get_mcp_server_registry()
        defn = registry.get_server(server_id)
        if not defn:
            raise HTTPException(404, detail=f"MCP server '{server_id}' not found")

        # Apply updates
        update_data = request.model_dump(exclude_none=True)
        for key, value in update_data.items():
            setattr(defn, key, value)

        registry.save_server(defn)

        # Re-discover tools
        discovered = []
        discovery_error = None
        try:
            discovered = registry.discover_tools(server_id)
        except Exception as e:
            discovery_error = str(e)

        # Reload agent tools
        agent = get_agent()
        agent.reload_mcp_server_tools()

        result = {
            "status": "ok",
            "server": registry.get_server(server_id).model_dump(),
            "discovered_tools": len(discovered),
        }
        if discovery_error:
            result["discovery_error"] = discovery_error
        return result

    @app.delete("/mcp-servers/{server_id}", tags=["MCP Servers"])
    async def delete_mcp_server(
        server_id: str,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Remove an MCP server and all its tools. Admin-only."""
        from ..core.mcp_servers import get_mcp_server_registry

        registry = get_mcp_server_registry()
        if not registry.delete_server(server_id):
            raise HTTPException(404, detail=f"MCP server '{server_id}' not found")

        # Reload agent tools to remove deleted MCP tools
        agent = get_agent()
        agent.reload_mcp_server_tools()

        return {"status": "ok", "deleted": server_id}

    @app.post("/mcp-servers/{server_id}/discover", tags=["MCP Servers"])
    async def discover_mcp_server_tools(
        server_id: str,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Force re-discover tools from an MCP server. Admin-only — discovery starts the server process."""
        from ..core.mcp_servers import get_mcp_server_registry

        registry = get_mcp_server_registry()
        if not registry.get_server(server_id):
            raise HTTPException(404, detail=f"MCP server '{server_id}' not found")

        try:
            discovered = registry.discover_tools(server_id)
        except Exception as e:
            raise HTTPException(500, detail=f"Discovery failed: {str(e)}")

        # Reload agent tools
        agent = get_agent()
        agent.reload_mcp_server_tools()

        return {
            "status": "ok",
            "server_id": server_id,
            "discovered_tools": [dt.model_dump() for dt in discovered],
            "count": len(discovered),
        }

    @app.post("/mcp-servers/{server_id}/test", tags=["MCP Servers"])
    async def test_mcp_server(
        server_id: str,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Test connectivity to an MCP server. Admin-only — connects/launches the server."""
        from ..core.mcp_servers import get_mcp_server_registry

        registry = get_mcp_server_registry()
        if not registry.get_server(server_id):
            raise HTTPException(404, detail=f"MCP server '{server_id}' not found")

        result = registry.test_connection(server_id)
        return result

    class MCPServerInstallRequest(BaseModel):
        source: str = Field(
            default="",
            description="Install source: Claude Desktop JSON blob, bare stdio command, HTTP URL, or registry id",
        )
        name: Optional[str] = None
        preview_token: Optional[str] = None
        confirmed: bool = False
        config_values: Dict[str, str] = Field(default_factory=dict)
        auto_enable: bool = True
        thread_id: Optional[str] = None

    class MCPServerInstallPreviewRequest(BaseModel):
        source: str = Field(..., description="Install source to inspect")
        name: Optional[str] = None

    class MCPServerInstallRetryRequest(BaseModel):
        confirmed: bool = False
        config_values: Dict[str, str] = Field(default_factory=dict)

    def _mcp_install_response(
        *,
        registry,
        defn,
        parsed_summary: str,
        discovered,
        thread_id: Optional[str],
        status: str = "ok",
        discovery_error: Optional[str] = None,
    ):
        tool_names = [f"mcp__{defn.id}__{t.name}" for t in discovered]
        return {
            "status": status,
            "server": registry.get_server(defn.id).model_dump(),
            "parsed_summary": parsed_summary,
            "discovered_tools": len(discovered),
            "tool_names": tool_names,
            "thread_id": thread_id,
            "discovery_error": discovery_error,
            "install_logs": defn.install_logs,
            "missing_config": defn.missing_config,
            "requires_confirmation": bool(defn.confirmation_required and status != "ok"),
        }

    def _enable_mcp_tools_for_user_defaults(user_id: str, tool_names: List[str]) -> None:
        if not tool_names:
            return
        from ..tools import ALL_TOOLS

        agent = get_agent()
        with agent.profile_manager.atomic_update(user_id) as profile:
            dtt = profile.tool_preferences.default_thread_tools
            if dtt is None:
                dtt = [t.name for t in ALL_TOOLS]
            for name in tool_names:
                if name not in dtt:
                    dtt.append(name)
            profile.tool_preferences.default_thread_tools = dtt
        agent._rebuild_default_graphs()

    def _attach_mcp_tools_to_thread(user: AuthenticatedUser, thread_id: str, tool_names: List[str]) -> None:
        if not thread_id or not tool_names:
            return
        agent = get_agent()
        _require_thread_access(user, thread_id)
        tc = agent.thread_config_manager.get_config(thread_id)
        enabled_tools = list(tc.enabled_tools) if tc and tc.enabled_tools else []
        for tool_name in tool_names:
            if tool_name not in enabled_tools:
                enabled_tools.append(tool_name)
        agent.thread_config_manager.update_config(thread_id, enabled_tools=enabled_tools)
        agent.invalidate_thread_config_cache(thread_id)

    async def _run_mcp_install(
        *,
        defn,
        plan,
        registry,
        user: AuthenticatedUser,
        auto_enable: bool,
        thread_id: Optional[str],
        confirmed: bool,
        config_values: Dict[str, str],
    ):
        from ..core.mcp_runtime import make_failed_draft, prepare_runtime

        if plan.confirmation_required and not confirmed:
            defn.install_status = "draft"
            defn.enabled = False
            registry.save_server(defn)
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "This MCP install needs admin confirmation before Nymeria runs it.",
                    "preview": plan.to_dict(),
                    "server": registry.get_server(defn.id).model_dump(),
                },
            )

        logs: List[str] = []
        try:
            defn, logs = prepare_runtime(defn, plan, config_values=config_values, log_sink=logs)
        except Exception as e:
            failed = make_failed_draft(defn, plan, str(e), logs)
            registry.save_server(failed)
            return _mcp_install_response(
                registry=registry,
                defn=failed,
                parsed_summary=plan.parsed_summary,
                discovered=[],
                thread_id=thread_id,
                status="draft",
                discovery_error=str(e),
            )

        defn.enabled = bool(auto_enable)
        defn.install_status = "ready"
        defn.last_error = None
        defn.install_logs = logs
        defn.install_plan = plan.to_dict()
        defn.missing_config = []
        registry.save_server(defn)

        try:
            discovered = registry.discover_tools(defn.id)
        except Exception as e:
            failed = make_failed_draft(defn, plan, str(e), logs)
            registry.save_server(failed)
            logger.warning("install_mcp_server discovery failed for %s: %s", defn.id, e)
            return _mcp_install_response(
                registry=registry,
                defn=failed,
                parsed_summary=plan.parsed_summary,
                discovered=[],
                thread_id=thread_id,
                status="draft",
                discovery_error=str(e),
            )

        agent = get_agent()
        agent.reload_mcp_server_tools()
        tool_names = [f"mcp__{defn.id}__{t.name}" for t in discovered]
        if thread_id:
            _attach_mcp_tools_to_thread(user, thread_id, tool_names)
        elif auto_enable:
            _enable_mcp_tools_for_user_defaults(user.id, tool_names)

        return _mcp_install_response(
            registry=registry,
            defn=registry.get_server(defn.id),
            parsed_summary=plan.parsed_summary,
            discovered=discovered,
            thread_id=thread_id,
            status="ok",
        )

    @app.post("/mcp-servers/install/preview", tags=["MCP Servers"])
    async def preview_mcp_server_install(
        request: MCPServerInstallPreviewRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Parse an MCP install source and return a non-executing install plan."""
        from ..core.mcp_runtime import save_preview, plan_text_source
        from ..core.mcp_installer import MCPInstallError

        try:
            defn, plan = plan_text_source(request.source, name=request.name)
            token = save_preview(defn, plan, source=request.source)
        except MCPInstallError as e:
            raise HTTPException(400, detail=str(e))
        except Exception as e:
            logger.exception("preview_mcp_server_install failed")
            raise HTTPException(400, detail=f"{type(e).__name__}: {e}")

        return {
            "preview_token": token,
            "server": defn.model_dump(),
            "plan": plan.to_dict(),
        }

    @app.post("/mcp-servers/install/preview-upload", tags=["MCP Servers"])
    async def preview_mcp_server_bundle_upload(
        file: UploadFile = File(...),
        name: Optional[str] = Form(None),
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Preview an uploaded .mcpb/.dxt/.zip bundle without running it."""
        from ..core.mcp_runtime import plan_bundle_file, save_preview
        from ..core.mcp_installer import MCPInstallError

        filename = file.filename or "bundle.mcpb"
        if not filename.lower().endswith((".mcpb", ".dxt", ".zip")):
            raise HTTPException(400, detail="upload must be a .mcpb, .dxt, or .zip file")

        upload_dir = get_settings().data_dir / "mcp_install_previews" / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        upload_path = upload_dir / f"{uuid.uuid4().hex}-{Path(filename).name}"
        try:
            content = await file.read()
            upload_path.write_bytes(content)
            defn, plan = plan_bundle_file(upload_path, original_name=filename, name=name)
            token = save_preview(defn, plan, source=filename)
        except MCPInstallError as e:
            upload_path.unlink(missing_ok=True)
            raise HTTPException(400, detail=str(e))
        except Exception as e:
            upload_path.unlink(missing_ok=True)
            logger.exception("preview_mcp_server_bundle_upload failed")
            raise HTTPException(400, detail=f"{type(e).__name__}: {e}")

        return {
            "preview_token": token,
            "server": defn.model_dump(),
            "plan": plan.to_dict(),
        }

    @app.post("/mcp-servers/install", tags=["MCP Servers"])
    async def install_mcp_server(
        request: MCPServerInstallRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Install an MCP server from a user-pasted source string. Admin-only.

        Parses `source` or a saved preview into a server definition, prepares
        any managed runtime needed, discovers tools, and wires them into the
        agent. Failed setup/discovery is saved as a disabled draft so admins
        can inspect logs and retry.

        Admin-only because install can launch arbitrary stdio commands inside
        the agent process.
        """
        from ..core.mcp_runtime import (
            consume_preview,
            load_preview,
            plan_text_source,
        )
        from ..core.mcp_installer import (
            MCPInstallError,
        )
        from ..core.mcp_servers import get_mcp_server_registry

        try:
            if request.preview_token:
                _, defn, plan = load_preview(request.preview_token)
            else:
                defn, plan = plan_text_source(request.source, name=request.name)
        except MCPInstallError as e:
            raise HTTPException(400, detail=str(e))
        except Exception as e:
            logger.exception("install_mcp_server parse failed")
            raise HTTPException(400, detail=f"{type(e).__name__}: {e}")

        registry = get_mcp_server_registry()
        if registry.get_server(defn.id):
            # Very unlikely (ids include a random suffix) but handle it.
            raise HTTPException(409, detail=f"MCP server '{defn.id}' already exists")

        result = await _run_mcp_install(
            defn=defn,
            plan=plan,
            registry=registry,
            user=user,
            auto_enable=request.auto_enable,
            thread_id=request.thread_id,
            confirmed=request.confirmed,
            config_values=request.config_values,
        )
        if request.preview_token:
            consume_preview(request.preview_token)
        return result

    @app.post("/mcp-servers/{server_id}/retry", tags=["MCP Servers"])
    async def retry_mcp_server_install(
        server_id: str,
        request: MCPServerInstallRetryRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """Retry setup/discovery for a disabled draft or failed MCP server."""
        from ..core.mcp_runtime import MCPInstallPlan
        from ..core.mcp_servers import get_mcp_server_registry

        registry = get_mcp_server_registry()
        defn = registry.get_server(server_id)
        if not defn:
            raise HTTPException(404, detail=f"MCP server '{server_id}' not found")
        if not defn.install_plan:
            raise HTTPException(400, detail="MCP server has no install plan to retry")

        plan = MCPInstallPlan.from_dict(defn.install_plan)
        return await _run_mcp_install(
            defn=defn,
            plan=plan,
            registry=registry,
            user=user,
            auto_enable=True,
            thread_id=None,
            confirmed=request.confirmed,
            config_values=request.config_values,
        )

    # ========================================================================
    # Agent Thread Endpoints
    # ========================================================================

    @app.get("/agents/templates", tags=["Agent Threads"])
    async def list_agent_templates(user: AuthenticatedUser = Depends(verify_api_key)):
        """List agent templates (legacy — returns empty list)."""
        return {"templates": [], "total": 0}

    @app.get("/agents/threads", tags=["Agent Threads"])
    async def list_agent_threads(user: AuthenticatedUser = Depends(verify_api_key)):
        """List the caller's callable threads (callable=True). Admins see only
        their own callable threads here; act-as via X-Nymeria-Act-As to see
        another user's set."""
        agent = get_agent()
        owned = set(agent.accounts_repo.list_threads_for_user(user.id))
        threads = agent.thread_config_manager.list_callable_threads(owned_thread_ids=owned)
        result = []
        for tc in threads:
            data = tc.model_dump(mode="json")
            data["has_customizations"] = tc.has_customizations()
            result.append(data)
        return {"threads": result, "total": len(result)}

    class AgentThreadCreateRequest(BaseModel):
        """Request to create a new callable thread."""
        # callable_name becomes the LangChain tool name and is bound to the
        # LLM via tool/function specs. OpenAI and Anthropic both reject names
        # outside ^[a-zA-Z0-9_-]{1,64}$, so reject early instead of crashing
        # on the first invocation attempt.
        callable_name: str = Field(
            ..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$"
        )
        callable_description: str = Field(default="", max_length=500)
        system_prompt: str = Field(default="", max_length=50000)
        llm_provider: Optional[str] = None
        llm_model: Optional[str] = None
        llm_temperature: Optional[float] = None
        llm_max_tokens: Optional[int] = None

    @app.post("/agents/threads", tags=["Agent Threads"])
    async def create_agent_thread(
        http_request: Request,
        request: AgentThreadCreateRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Create a new callable thread."""
        import uuid
        from ..core.thread_config import ThreadConfig, ThreadLLMConfig

        agent = get_agent()

        # Validate callable_name doesn't collide with core tool names
        from ..tools import ALL_TOOLS
        core_tool_names = {t.name for t in ALL_TOOLS}
        if request.callable_name in core_tool_names:
            raise HTTPException(
                status_code=400,
                detail=f"Callable name '{request.callable_name}' conflicts with a core tool name",
            )

        # Check if a callable thread with this name already exists for THIS
        # user. Two users can each have a "Helper" — invocation is gated by
        # ownership at runtime so there's no actual conflict.
        owned = set(agent.accounts_repo.list_threads_for_user(user.id))
        existing = agent.thread_config_manager.get_callable_thread_by_name(
            request.callable_name, owned_thread_ids=owned
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Callable thread for '{request.callable_name}' already exists: {existing.thread_id}",
            )

        # Build LLM config
        llm_config = None
        if request.llm_provider or request.llm_model or request.llm_temperature is not None or request.llm_max_tokens is not None:
            llm_config = ThreadLLMConfig(
                provider=request.llm_provider,
                model=request.llm_model,
                temperature=request.llm_temperature,
                max_tokens=request.llm_max_tokens,
            )

        # Generate thread ID
        random_suffix = uuid.uuid4().hex[:8]
        thread_id = f"agent-{request.callable_name.lower()}-{random_suffix}"

        tc = ThreadConfig(
            thread_id=thread_id,
            system_prompt=request.system_prompt,
            callable=True,
            callable_name=request.callable_name,
            callable_description=request.callable_description or f"Invoke the {request.callable_name} callable thread",
            llm_config=llm_config,
        )

        if not agent.thread_config_manager.save_config(tc):
            raise HTTPException(status_code=500, detail="Failed to create agent thread")

        # Claim the thread for the creator so the runtime ownership gate
        # in create_callable_thread_tool() lets the creator invoke it but
        # rejects anyone else.
        agent.accounts_repo.claim_thread(thread_id, user.id)

        # Create thread metadata with callable_name as title (under creator)
        agent.thread_metadata_manager.upsert_thread(
            user.id, thread_id,
            title=request.callable_name,
            title_source="callable",
            platform="callable",
        )

        # Rebuild agent tools to include the new callable thread
        agent.sync_agent_tools()

        # Publish sync event so other clients see the new thread
        client_id = http_request.headers.get("x-nymeria-client-id", "")
        publish_sync_event(
            event_type="thread_created",
            thread_id=thread_id,
            user_id=user.id,
            data={"title": request.callable_name, "title_source": "callable", "platform": "callable"},
            origin_client_id=client_id,
        )

        result = tc.model_dump(mode="json")
        result["has_customizations"] = tc.has_customizations()
        return result

    # ==========================================================================
    # RAG (Retrieval Augmented Generation) Endpoints
    # ==========================================================================

    class RagSettingsUpdate(BaseModel):
        """Request model for updating RAG settings."""
        enabled: Optional[bool] = Field(default=None, description="Enable/disable RAG")
        max_chunks: Optional[int] = Field(default=None, ge=1, le=10, description="Max chunks per message")
        include_conversations: Optional[bool] = Field(default=None, description="Include conversation history")
        include_memories: Optional[bool] = Field(default=None, description="Include saved memories")
        include_todos: Optional[bool] = Field(default=None, description="Include TODO completions")
        auto_flush: Optional[bool] = Field(default=None, description="Auto-flush on context trim")

    @app.get("/users/{user_id}/rag/settings", tags=["RAG"])
    async def get_rag_settings(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Get user's RAG configuration."""
        _require_same_user_or_admin(user, user_id)
        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)
        rag_prefs = profile.get_rag_preferences()

        return {
            "enabled": profile.opt_in.rag_enabled,
            "max_chunks": rag_prefs.get("max_chunks", 5),
            "include_conversations": rag_prefs.get("include_conversations", True),
            "include_memories": rag_prefs.get("include_memories", True),
            "include_todos": rag_prefs.get("include_todos", True),
            "auto_flush": rag_prefs.get("auto_flush", True),
        }

    @app.put("/users/{user_id}/rag/settings", tags=["RAG"])
    async def update_rag_settings(
        user_id: str,
        settings_update: RagSettingsUpdate,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Update user's RAG configuration."""
        _require_same_user_or_admin(user, user_id)
        agent = get_agent()

        with agent.profile_manager.atomic_update(user_id) as profile:
            if settings_update.enabled is not None:
                profile.opt_in.rag_enabled = settings_update.enabled

            if settings_update.max_chunks is not None:
                profile.set_rag_preference("max_chunks", settings_update.max_chunks)

            if settings_update.include_conversations is not None:
                profile.set_rag_preference("include_conversations", settings_update.include_conversations)

            if settings_update.include_memories is not None:
                profile.set_rag_preference("include_memories", settings_update.include_memories)

            if settings_update.include_todos is not None:
                profile.set_rag_preference("include_todos", settings_update.include_todos)

            if settings_update.auto_flush is not None:
                profile.set_rag_preference("auto_flush", settings_update.auto_flush)

            rag_prefs = profile.get_rag_preferences()
            return {
                "status": "ok",
                "enabled": profile.opt_in.rag_enabled,
                "max_chunks": rag_prefs.get("max_chunks", 5),
                "include_conversations": rag_prefs.get("include_conversations", True),
                "include_memories": rag_prefs.get("include_memories", True),
                "include_todos": rag_prefs.get("include_todos", True),
                "auto_flush": rag_prefs.get("auto_flush", True),
            }

    @app.get("/users/{user_id}/rag/stats", tags=["RAG"])
    async def get_rag_stats(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Get indexing statistics for a user."""
        _require_same_user_or_admin(user, user_id)
        from ..core.memory_index import MemoryIndex

        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)

        if not profile.opt_in.rag_enabled:
            return {
                "enabled": False,
                "message": "RAG is not enabled for this user",
            }

        try:
            # Sanitize user_id for path
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in "-_") or "default"
            db_path = agent.settings.data_dir / "users" / safe_user_id / "memory.db"

            if not db_path.exists():
                return {
                    "enabled": True,
                    "total_chunks": 0,
                    "by_type": {},
                    "last_indexed": None,
                    "vector_count": 0,
                }

            memory_index = MemoryIndex(db_path)
            stats = memory_index.get_stats(user_id)
            stats["enabled"] = True
            return stats

        except Exception as e:
            logger.error(f"Failed to get RAG stats for user {user_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/users/{user_id}/rag/search", tags=["RAG"])
    async def search_rag(
        user_id: str,
        q: str = Query(..., min_length=1, description="Search query"),
        max_results: int = Query(default=5, ge=1, le=10),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Search a user's RAG index and return structured chunk results."""
        _require_same_user_or_admin(user, user_id)
        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)

        if not profile.opt_in.rag_enabled:
            raise HTTPException(
                status_code=400,
                detail=f"RAG is not enabled for user '{user_id}'.",
            )

        memory_index = agent._get_memory_index(user_id)
        if not memory_index:
            raise HTTPException(status_code=500, detail="Could not access memory index")

        rag_prefs = profile.get_rag_preferences()
        chunk_types: List[str] = []
        if rag_prefs.get("include_conversations", True):
            chunk_types.append("conversation")
        if rag_prefs.get("include_memories", True):
            chunk_types.append("memory")
        if rag_prefs.get("include_todos", True):
            chunk_types.append("todo")

        if not chunk_types:
            return {
                "user_id": user_id,
                "query": q,
                "results": [],
                "total": 0,
                "message": "All RAG content types are disabled.",
            }

        results = memory_index.search(
            query=q,
            user_id=user_id,
            limit=max_results,
            chunk_types=chunk_types,
        )

        return {
            "user_id": user_id,
            "query": q,
            "results": [
                {
                    "id": result.id,
                    "content": result.content,
                    "chunk_type": result.chunk_type,
                    "thread_id": result.thread_id,
                    "created_at": result.created_at.isoformat(),
                    "metadata": result.metadata,
                    "score": result.score,
                }
                for result in results
            ],
            "total": len(results),
        }

    @app.post("/users/{user_id}/rag/reindex", tags=["RAG"])
    async def reindex_user(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Rebuild saved-memory chunks in the user's RAG index."""
        _require_same_user_or_admin(user, user_id)
        from ..core.memory_index import MemoryIndex

        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)

        if not profile.opt_in.rag_enabled:
            raise HTTPException(
                status_code=400,
                detail="RAG is not enabled for this user. Enable it first.",
            )

        try:
            # Sanitize user_id for path
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in "-_") or "default"
            db_path = agent.settings.data_dir / "users" / safe_user_id / "memory.db"
            memory_index = MemoryIndex(db_path)

            # Clear only saved-memory chunks. Conversation and TODO chunks are
            # event-generated and should not be discarded by a memory reindex.
            cleared = memory_index.delete_by_type(user_id, "memory")

            # Re-index memories
            indexed_memories = 0
            for memory in profile.memories:
                memory_index.add_chunk(
                    content=f"{memory.key}: {memory.value}",
                    metadata={"key": memory.key},
                    chunk_type="memory",
                    user_id=user_id,
                )
                indexed_memories += 1

            # Note: We could also re-index conversation history from checkpointer,
            # but that would require iterating through all threads which is expensive.
            # For now, we just rebuild memories and let new conversations be indexed.

            return {
                "status": "ok",
                "cleared_memory_chunks": cleared,
                "indexed_memories": indexed_memories,
                "message": "Index rebuilt. New conversations will be indexed automatically.",
            }

        except Exception as e:
            logger.error(f"Failed to reindex for user {user_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.delete("/users/{user_id}/rag/index", tags=["RAG"])
    async def clear_index(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Clear user's RAG index."""
        _require_same_user_or_admin(user, user_id)
        from ..core.memory_index import MemoryIndex

        agent = get_agent()

        try:
            # Sanitize user_id for path
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in "-_") or "default"
            db_path = agent.settings.data_dir / "users" / safe_user_id / "memory.db"

            if not db_path.exists():
                return {
                    "status": "ok",
                    "cleared_chunks": 0,
                    "message": "No index found for this user.",
                }

            memory_index = MemoryIndex(db_path)
            cleared = memory_index.clear_index(user_id)

            return {
                "status": "ok",
                "cleared_chunks": cleared,
            }

        except Exception as e:
            logger.error(f"Failed to clear index for user {user_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ========================================================================
    # User Tool Preferences Endpoints
    # ========================================================================

    class ToolConfigRequest(BaseModel):
        """Request model for updating tool configuration."""
        config: Dict[str, Any] = Field(..., description="Tool configuration")

    class ToolPreferencesResponse(BaseModel):
        """Response model for tool preferences."""
        default_thread_tools: Optional[List[str]] = None
        tool_configs: Dict[str, Dict[str, Any]] = {}

    @app.get("/users/{user_id}/tools", tags=["User Tools"])
    async def list_user_tools(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        List all tools with their enabled state for a specific user.

        Enabled state is derived from default_thread_tools membership.
        """
        _require_same_user_or_admin(user, user_id)
        from ..tools import (
            ALL_TOOLS,
            OPTIONAL_TOOLS,
            filter_discoverable_optional_tool_names,
        )
        from ..tools.metadata import get_tool_metadata

        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)
        dtt = profile.tool_preferences.default_thread_tools
        dtt_set = set(dtt) if dtt is not None else {t.name for t in ALL_TOOLS}

        tools_list = []
        for t in ALL_TOOLS:
            meta = get_tool_metadata(t.name)
            tools_list.append({
                "name": t.name,
                "description": t.description,
                "category": meta.category.value if meta else "core",
                "security_level": meta.security_level.value if meta else "safe",
                "enabled": t.name in dtt_set,
                "enabled_reason": "default_thread_tools",
                "default_enabled": True,
                "config_schema": meta.config_schema if meta else None,
                "user_config": profile.tool_preferences.get_tool_config(t.name),
                "globally_disabled": False,
            })

        visible_optional = filter_discoverable_optional_tool_names(
            OPTIONAL_TOOLS.keys(),
            user.role,
        )
        for name, t in OPTIONAL_TOOLS.items():
            if name not in visible_optional:
                continue
            meta = get_tool_metadata(name)
            tools_list.append({
                "name": name,
                "description": t.description,
                "category": meta.category.value if meta else "unknown",
                "security_level": meta.security_level.value if meta else "moderate",
                "enabled": name in dtt_set,
                "enabled_reason": "default_thread_tools",
                "default_enabled": False,
                "config_schema": meta.config_schema if meta else None,
                "user_config": profile.tool_preferences.get_tool_config(name),
                "globally_disabled": False,
            })

        # Group by category
        by_category: Dict[str, List[dict]] = {}
        for tool in tools_list:
            category = tool.get("category", "unknown")
            if category not in by_category:
                by_category[category] = []
            by_category[category].append(tool)

        return {
            "user_id": user_id,
            "tools": tools_list,
            "by_category": by_category,
            "total": len(tools_list),
        }

    @app.get("/users/{user_id}/tools/preferences", response_model=ToolPreferencesResponse, tags=["User Tools"])
    async def get_tool_preferences(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Get user's tool preferences."""
        _require_same_user_or_admin(user, user_id)
        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)

        return ToolPreferencesResponse(
            default_thread_tools=profile.tool_preferences.default_thread_tools,
            tool_configs=profile.tool_preferences.tool_configs,
        )

    @app.put("/users/{user_id}/tools/{tool_name}/config", tags=["User Tools"])
    async def set_tool_config(
        user_id: str,
        tool_name: str,
        request: ToolConfigRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Set configuration for a specific tool.

        Configuration options depend on the tool. For example, bash_execute
        supports timeout_seconds.
        """
        _require_same_user_or_admin(user, user_id)
        from ..tools.metadata import get_tool_metadata

        agent = get_agent()

        # Verify tool exists
        tool = agent.tool_registry.get_tool(tool_name)
        if not tool:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_name}' not found"
            )

        # Validate against schema if available
        metadata = get_tool_metadata(tool_name)
        if metadata and metadata.config_schema:
            # Basic validation (in production, use jsonschema library)
            schema_props = metadata.config_schema.get("properties", {})
            for key in request.config:
                if key not in schema_props:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Unknown config key '{key}' for tool '{tool_name}'"
                    )

        with agent.profile_manager.atomic_update(user_id) as profile:
            profile.tool_preferences.set_tool_config(tool_name, request.config)
            profile.updated_at = datetime.utcnow()

        return {
            "status": "ok",
            "tool_name": tool_name,
            "config": request.config,
        }

    @app.post("/users/{user_id}/tools/reset", tags=["User Tools"])
    async def reset_tool_preferences(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Reset all tool preferences to defaults (all core tools enabled)."""
        _require_same_user_or_admin(user, user_id)
        from ..tools import ALL_TOOLS

        agent = get_agent()

        with agent.profile_manager.atomic_update(user_id) as profile:
            profile.tool_preferences.default_thread_tools = [t.name for t in ALL_TOOLS]
            profile.tool_preferences.tool_configs.clear()
            profile.tool_preferences.custom_descriptions.clear()
            profile.updated_at = datetime.utcnow()

        # Clear graph caches and rebuild defaults
        agent._user_graphs.clear()
        agent._async_user_graphs.clear()
        agent._default_graph = agent._build_graph_with_prompt(agent._base_system_prompt)
        agent._default_async_graph = agent._build_async_graph_with_prompt(agent._base_system_prompt)

        return {
            "status": "ok",
            "message": "Tool preferences reset to defaults",
        }

    # ========================================================================
    # User Memory Endpoints
    # ========================================================================

    @app.get("/users/{user_id}/memories", tags=["User Memories"])
    async def list_memories(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List all memories stored for a user."""
        _require_same_user_or_admin(user, user_id)
        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)
        return {
            "user_id": user_id,
            "memories": [
                {
                    "key": m.key,
                    "value": m.value,
                    "created_at": m.created_at.isoformat(),
                    "accessed_at": m.accessed_at.isoformat(),
                    "access_count": m.access_count,
                }
                for m in profile.memories
            ],
            "count": len(profile.memories),
        }

    class MemorySaveRequest(BaseModel):
        key: str = Field(..., description="Memory key identifier")
        value: str = Field(..., max_length=1000, description="Memory content")

    def _upsert_memory_rag_chunk(user_id: str, key: str, value: str) -> None:
        """Best-effort profile-memory sync into the user's RAG index."""
        agent = get_agent()
        memory_index = agent._get_memory_index(user_id)
        if not memory_index:
            return
        try:
            memory_index.delete_memory_key(user_id, key)
            memory_index.add_chunk(
                content=f"{key}: {value}",
                metadata={"key": key},
                chunk_type="memory",
                user_id=user_id,
            )
        except Exception as e:
            logger.warning(f"Failed to sync memory '{key}' into RAG index: {e}")

    def _delete_memory_rag_chunk(user_id: str, key: str) -> None:
        """Best-effort removal of a profile memory from the user's RAG index."""
        agent = get_agent()
        memory_index = agent._get_memory_index(user_id)
        if not memory_index:
            return
        try:
            memory_index.delete_memory_key(user_id, key)
        except Exception as e:
            logger.warning(f"Failed to remove memory '{key}' from RAG index: {e}")

    @app.post("/users/{user_id}/memories", tags=["User Memories"])
    async def save_memory(
        user_id: str,
        request: MemorySaveRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Save or update a memory for a user."""
        _require_same_user_or_admin(user, user_id)
        agent = get_agent()
        with agent.profile_manager.atomic_update(user_id) as profile:
            success = profile.add_memory(request.key, request.value)
        if not success:
            raise HTTPException(
                status_code=400,
                detail=f"Memory limit reached ({profile.MAX_MEMORIES})",
            )
        _upsert_memory_rag_chunk(user_id, request.key, request.value)
        return {"status": "ok", "key": request.key}

    @app.delete("/users/{user_id}/memories/{key}", tags=["User Memories"])
    async def forget_memory(
        user_id: str,
        key: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Remove a memory by key."""
        _require_same_user_or_admin(user, user_id)
        agent = get_agent()
        with agent.profile_manager.atomic_update(user_id) as profile:
            removed = profile.remove_memory(key)
        if not removed:
            raise HTTPException(status_code=404, detail=f"No memory with key '{key}'")
        _delete_memory_rag_chunk(user_id, key)
        return {"status": "ok", "key": key}

    @app.get("/users/{user_id}/memories/search", tags=["User Memories"])
    async def search_memories(
        user_id: str,
        q: str = Query(..., description="Search term"),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """Search memories by key or value substring."""
        _require_same_user_or_admin(user, user_id)
        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)
        results = profile.search_memories(q)
        return {
            "user_id": user_id,
            "query": q,
            "results": [
                {"key": m.key, "value": m.value, "access_count": m.access_count}
                for m in results
            ],
            "count": len(results),
        }

    @app.get("/tools/categories", tags=["Tools"])
    async def list_tool_categories(
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """List all tool categories with their tools."""
        from ..tools import filter_discoverable_optional_tool_names
        from ..tools.metadata import get_category_tools_summary

        categories = get_category_tools_summary()
        visible_names = filter_discoverable_optional_tool_names(
            {name for names in categories.values() for name in names},
            user.role,
        )
        return {
            "categories": {
                category: [name for name in names if name in visible_names]
                for category, names in categories.items()
            },
        }

    # ========================================================================
    # Unified Tools Endpoints
    # ========================================================================

    def _builtin_to_unified(
        tool_info: Dict[str, Any],
        user_id: str,
        tool_preferences: Optional["ToolPreferences"] = None,
    ) -> UnifiedToolResponse:
        """Convert built-in tool info to unified response format."""
        default_desc = tool_info.get("description", "")
        custom_desc = None

        # Check for custom description override
        if tool_preferences:
            custom_desc = tool_preferences.get_custom_description(tool_info["name"])

        # Effective description is custom if set, otherwise default
        effective_desc = custom_desc if custom_desc else default_desc

        # Check if tool is configurable (has config_schema)
        config_schema = tool_info.get("config_schema")
        configurable = config_schema is not None and len(config_schema) > 0

        return UnifiedToolResponse(
            id=tool_info["name"],
            name=tool_info["name"],
            description=effective_desc,
            default_description=default_desc,
            custom_description=custom_desc,
            category=tool_info.get("category", "core"),
            security_level=tool_info.get("security_level", "safe"),
            enabled=tool_info.get("enabled", True),
            enabled_reason=tool_info.get("enabled_reason", "default"),
            tool_type="builtin",
            implementation_type=None,
            config_schema=config_schema,
            user_config=tool_info.get("user_config", {}),
            parameters=None,
            http_config=None,
            mcp_config=None,
            tags=[],
            editable=False,
            configurable=configurable,
            created_at=None,
            updated_at=None,
        )

    def _custom_to_unified(
        defn: CustomToolDefinition,
        enabled: bool,
        enabled_reason: str,
        user_config: Dict[str, Any],
        tool_preferences: Optional["ToolPreferences"] = None,
    ) -> UnifiedToolResponse:
        """Convert custom tool definition to unified response format."""
        impl_type = defn.implementation_type
        http_config = None
        mcp_config = None

        if impl_type == "http" and defn.http_config is not None:
            http_config = {
                "method": defn.http_config.method,
                "url": defn.http_config.url,
                "headers": defn.http_config.headers,
                "body_template": defn.http_config.body_template,
                "query_params": defn.http_config.query_params,
                "timeout_seconds": defn.http_config.timeout_seconds,
                "response_path": defn.http_config.response_path,
                "response_format": defn.http_config.response_format,
            }
        elif impl_type == "mcp" and defn.mcp_config is not None:
            mcp_config = {
                "transport": defn.mcp_config.transport,
                "server_command": defn.mcp_config.server_command,
                "server_args": defn.mcp_config.server_args,
                "url": defn.mcp_config.url,
                "headers": defn.mcp_config.headers,
                "tool_name": defn.mcp_config.tool_name,
                "env_vars": defn.mcp_config.env_vars,
                "working_directory": defn.mcp_config.working_directory,
                "idle_timeout_seconds": defn.mcp_config.idle_timeout_seconds,
                "startup_timeout_seconds": defn.mcp_config.startup_timeout_seconds,
            }

        # Parameters is Dict[str, ToolParameter] — preserve names as keys.
        params = None
        if defn.parameters:
            params = {k: p.model_dump() for k, p in defn.parameters.items()}

        # Get description (custom tools can also have description overrides)
        default_desc = defn.description
        custom_desc = None
        if tool_preferences:
            custom_desc = tool_preferences.get_custom_description(defn.id)
        effective_desc = custom_desc if custom_desc else default_desc

        return UnifiedToolResponse(
            id=defn.id,
            name=defn.name,
            description=effective_desc,
            default_description=default_desc,
            custom_description=custom_desc,
            category="custom",
            security_level="moderate",
            enabled=enabled,
            enabled_reason=enabled_reason,
            tool_type="custom",
            implementation_type=impl_type,
            config_schema=None,
            user_config=user_config,
            parameters=params,
            http_config=http_config,
            mcp_config=mcp_config,
            tags=defn.tags or [],
            editable=True,
            configurable=False,  # Custom tools don't have config schemas (they are fully editable)
            created_at=defn.created_at,
            updated_at=defn.updated_at,
        )

    @app.get("/users/{user_id}/tools/unified", response_model=UnifiedToolListResponse, tags=["Unified Tools"])
    async def list_unified_tools(
        user_id: str,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        List all tools (built-in and custom) in a unified format.

        Returns tools with consistent structure regardless of type,
        including enable status per user derived from default_thread_tools.
        """
        _require_same_user_or_admin(user, user_id)
        from ..tools import (
            ALL_TOOLS,
            OPTIONAL_TOOLS,
            filter_discoverable_optional_tool_names,
        )
        from ..tools.metadata import get_tool_metadata

        agent = get_agent()
        loader = get_custom_tool_loader()

        # Get profile for user preferences
        profile = agent.profile_manager.get_profile(user_id)
        tool_prefs = profile.tool_preferences

        # Build default_thread_tools set for enabled check
        dtt = tool_prefs.default_thread_tools
        if dtt is None:
            dtt_set = {t.name for t in ALL_TOOLS}
        else:
            dtt_set = set(dtt)

        unified_tools = []

        # Built-in tools: derive enabled from default_thread_tools membership
        seen = set()
        visible_optional = filter_discoverable_optional_tool_names(
            OPTIONAL_TOOLS.keys(),
            user.role,
        )
        for t in ALL_TOOLS:
            meta = get_tool_metadata(t.name)
            enabled = t.name in dtt_set
            tool_info = {
                "name": t.name,
                "description": t.description,
                "category": meta.category.value if meta else "core",
                "security_level": meta.security_level.value if meta else "safe",
                "enabled": enabled,
                "enabled_reason": "default_thread_tools",
                "config_schema": meta.config_schema if meta else None,
                "user_config": tool_prefs.get_tool_config(t.name),
                "globally_disabled": False,
                "default_enabled": True,
            }
            unified_tools.append(_builtin_to_unified(tool_info, user_id, tool_prefs))
            seen.add(t.name)

        for name, t in OPTIONAL_TOOLS.items():
            if name in seen or name not in visible_optional:
                continue
            meta = get_tool_metadata(name)
            enabled = name in dtt_set
            tool_info = {
                "name": name,
                "description": t.description,
                "category": meta.category.value if meta else "unknown",
                "security_level": meta.security_level.value if meta else "moderate",
                "enabled": enabled,
                "enabled_reason": "default_thread_tools",
                "config_schema": meta.config_schema if meta else None,
                "user_config": tool_prefs.get_tool_config(name),
                "globally_disabled": False,
                "default_enabled": False,
            }
            unified_tools.append(_builtin_to_unified(tool_info, user_id, tool_prefs))
            seen.add(name)

        # Custom tools: always available (managed per-thread, not via global toggle).
        # Only admins see them in the unified list — definitions include
        # full HTTP/MCP config (URLs, headers, subprocess commands), which
        # would leak credentials/hosts to non-admin tool browsers.
        if user.role == "admin":
            custom_definitions = loader.get_all_definitions()
            for defn in custom_definitions:
                user_config = tool_prefs.get_tool_config(defn.id)
                unified_tools.append(_custom_to_unified(defn, True, "default", user_config, tool_prefs))

        # MCP server tools: appear with category "mcp_server"
        from ..tools.metadata import MCP_SERVER_TOOL_METADATA
        for tool_name, meta in MCP_SERVER_TOOL_METADATA.items():
            if tool_name not in seen:
                enabled = tool_name in dtt_set
                unified_tools.append(UnifiedToolResponse(
                    id=tool_name,
                    name=tool_name,
                    description=meta.description,
                    default_description=meta.description,
                    custom_description=None,
                    category="mcp_server",
                    security_level="moderate",
                    enabled=enabled,
                    enabled_reason="default_thread_tools",
                    tool_type="mcp_server",
                    implementation_type="mcp",
                    config_schema=None,
                    user_config={},
                    parameters=None,
                    http_config=None,
                    mcp_config=None,
                    tags=[],
                    editable=False,
                    configurable=False,
                    created_at=None,
                    updated_at=None,
                ))
                seen.add(tool_name)

        # Sort: built-in first, then mcp_server, then custom, alphabetically within each
        type_order = {"builtin": 0, "mcp_server": 1, "custom": 2}
        unified_tools.sort(key=lambda t: (type_order.get(t.tool_type, 9), t.name))

        builtin_count = sum(1 for t in unified_tools if t.tool_type == "builtin")
        custom_count = sum(1 for t in unified_tools if t.tool_type == "custom")

        return UnifiedToolListResponse(
            tools=unified_tools,
            total=len(unified_tools),
            builtin_count=builtin_count,
            custom_count=custom_count,
        )

    @app.put("/users/{user_id}/tools/unified/{tool_id}/enable", tags=["Unified Tools"])
    async def set_unified_tool_enabled(
        user_id: str,
        tool_id: str,
        request: UnifiedToolEnableRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Enable or disable a built-in or MCP server tool for a user by adding/removing it
        from default_thread_tools. Custom tools are not affected (they are
        always available and managed per-thread via enabled_tools).
        """
        _require_same_user_or_admin(user, user_id)
        from ..tools import (
            ALL_TOOLS,
            OPTIONAL_TOOLS,
            ADMIN_ONLY_OPTIONAL_TOOL_NAMES,
            DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES,
        )
        from ..tools.metadata import get_tool_metadata, get_all_tool_metadata

        agent = get_agent()

        # Check if tool exists as built-in or MCP server tool
        tool_meta = get_all_tool_metadata(tool_id)
        if not tool_meta:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        # Admin-only optional tools — self-modify, runtime-admin reload — must
        # not be enableable by a non-admin via the unified toggle.
        if (
            request.enabled
            and tool_id in ADMIN_ONLY_OPTIONAL_TOOL_NAMES
            and user.role != "admin"
        ):
            raise HTTPException(
                status_code=403,
                detail=f"Tool '{tool_id}' is admin-only",
            )
        if (
            request.enabled
            and tool_id in DEVELOPER_ONLY_OPTIONAL_TOOL_NAMES
            and user.role != "admin"
        ):
            raise HTTPException(
                status_code=403,
                detail=f"Tool '{tool_id}' is developer-only",
            )

        with agent.profile_manager.atomic_update(user_id) as profile:
            dtt = profile.tool_preferences.default_thread_tools
            if dtt is None:
                # Initialize from ALL_TOOLS if not yet set
                dtt = [t.name for t in ALL_TOOLS]

            if request.enabled:
                if tool_id not in dtt:
                    dtt.append(tool_id)
            else:
                if tool_id in dtt:
                    dtt.remove(tool_id)

            profile.tool_preferences.default_thread_tools = dtt
            profile.updated_at = datetime.utcnow()

        # Clear graph caches and rebuild defaults so changes take effect
        agent._user_graphs.clear()
        agent._async_user_graphs.clear()
        agent._default_graph = agent._build_graph_with_prompt(agent._base_system_prompt)
        agent._default_async_graph = agent._build_async_graph_with_prompt(agent._base_system_prompt)

        return {
            "status": "ok",
            "tool_id": tool_id,
            "enabled": request.enabled,
            "tool_type": tool_meta.category.value if tool_meta.category.value == "mcp_server" else "builtin",
        }

    @app.put("/users/{user_id}/tools/unified/{tool_id}/description", tags=["Unified Tools"])
    async def set_unified_tool_description(
        user_id: str,
        tool_id: str,
        request: UnifiedToolDescriptionRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Set a custom description for a tool.

        Works for both built-in and custom tools.
        Pass null/None to clear the custom description and revert to default.
        """
        _require_same_user_or_admin(user, user_id)
        from ..tools.metadata import get_tool_metadata

        agent = get_agent()
        loader = get_custom_tool_loader()

        # Check if tool exists (built-in or custom)
        builtin_meta = get_tool_metadata(tool_id)
        custom_defn = loader.get_definition(tool_id)

        if not builtin_meta and not custom_defn:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        with agent.profile_manager.atomic_update(user_id) as profile:
            if request.description is None:
                # Clear custom description
                cleared = profile.tool_preferences.clear_custom_description(tool_id)
                return {
                    "status": "ok",
                    "tool_id": tool_id,
                    "action": "cleared" if cleared else "no_change",
                    "description": None,
                }
            else:
                # Set custom description
                profile.tool_preferences.set_custom_description(tool_id, request.description)
                return {
                    "status": "ok",
                    "tool_id": tool_id,
                    "action": "set",
                    "description": request.description,
                }

    @app.put("/users/{user_id}/tools/unified/{tool_id}/config", tags=["Unified Tools"])
    async def set_unified_tool_config(
        user_id: str,
        tool_id: str,
        request: UnifiedToolConfigRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Set configuration for a tool.

        Works for both built-in and custom tools.
        The config is merged with existing config (pass empty dict to clear).
        """
        _require_same_user_or_admin(user, user_id)
        from ..tools.metadata import get_tool_metadata

        agent = get_agent()
        loader = get_custom_tool_loader()

        # Check if tool exists (built-in or custom)
        builtin_meta = get_tool_metadata(tool_id)
        custom_defn = loader.get_definition(tool_id)

        if not builtin_meta and not custom_defn:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        with agent.profile_manager.atomic_update(user_id) as profile:
            if not request.config:
                # Clear config
                profile.tool_preferences.tool_configs.pop(tool_id, None)
                return {
                    "status": "ok",
                    "tool_id": tool_id,
                    "action": "cleared",
                    "config": {},
                }
            else:
                # Set/merge config
                profile.tool_preferences.set_tool_config(tool_id, request.config)
                return {
                    "status": "ok",
                    "tool_id": tool_id,
                    "action": "set",
                    "config": request.config,
                }

    @app.post("/tools/unified", response_model=UnifiedToolResponse, tags=["Unified Tools"])
    async def create_unified_tool(
        request: CustomToolCreateRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """
        Create a new custom tool via the unified API. Admin-only — mirrors
        ``POST /tools/custom``.
        """
        loader = get_custom_tool_loader()

        # Check if tool already exists
        if loader.get_definition(request.id):
            raise HTTPException(
                status_code=400,
                detail=f"Tool '{request.id}' already exists",
            )

        # Build parameters dict (CustomToolDefinition.parameters is Dict[str, ToolParameter]).
        params: Dict[str, ToolParameter] = {
            k: ToolParameter(
                type=v.type,
                description=v.description,
                required=v.required,
                default=v.default,
                enum=v.enum,
            )
            for k, v in (request.parameters or {}).items()
        }

        http_config = None
        mcp_config = None
        if request.implementation_type == "http":
            if not request.http_config:
                raise HTTPException(
                    status_code=400,
                    detail="http_config is required for HTTP tools",
                )
            http_config = HTTPToolConfig(
                method=request.http_config.method,
                url=request.http_config.url,
                headers=request.http_config.headers,
                body_template=request.http_config.body_template,
                query_params=request.http_config.query_params,
                timeout_seconds=request.http_config.timeout_seconds,
                response_path=request.http_config.response_path,
                response_format=request.http_config.response_format,
            )
        elif request.implementation_type == "mcp":
            if not request.mcp_config:
                raise HTTPException(
                    status_code=400,
                    detail="mcp_config is required for MCP tools",
                )
            mcp_config = MCPToolConfig(
                server_command=request.mcp_config.server_command,
                server_args=request.mcp_config.server_args,
                tool_name=request.mcp_config.tool_name,
                env_vars=request.mcp_config.env_vars,
                working_directory=request.mcp_config.working_directory,
                idle_timeout_seconds=request.mcp_config.idle_timeout_seconds,
                startup_timeout_seconds=request.mcp_config.startup_timeout_seconds,
            )

        definition = CustomToolDefinition(
            id=request.id,
            name=request.name,
            description=request.description,
            parameters=params,
            implementation_type=request.implementation_type,
            http_config=http_config,
            mcp_config=mcp_config,
            enabled=request.enabled,
            tags=request.tags,
        )

        loader.save_definition(definition)

        # Reload tools so the new definition is bound on the agent graphs.
        agent = get_agent()
        agent.reload_tools()

        return _custom_to_unified(definition, True, "default", {})

    @app.put("/tools/unified/{tool_id}", response_model=UnifiedToolResponse, tags=["Unified Tools"])
    async def update_unified_tool(
        tool_id: str,
        request: CustomToolUpdateRequest,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """
        Update a custom tool via the unified API. Admin-only — mirrors
        ``PUT /tools/custom/{tool_id}``.

        Only custom tools can be updated. Built-in tools return 400.
        """
        from ..tools.metadata import get_tool_metadata

        loader = get_custom_tool_loader()

        # Check if it's a built-in tool
        if get_tool_metadata(tool_id):
            raise HTTPException(
                status_code=400,
                detail="Cannot edit built-in tools. Use enable/disable or configure instead.",
            )

        definition = loader.get_definition(tool_id)
        if not definition:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        # Update fields
        if request.name is not None:
            definition.name = request.name
        if request.description is not None:
            definition.description = request.description
        if request.parameters is not None:
            definition.parameters = {
                k: ToolParameter(
                    type=v.type,
                    description=v.description,
                    required=v.required,
                    default=v.default,
                    enum=v.enum,
                )
                for k, v in request.parameters.items()
            }
        if request.http_config is not None and definition.implementation_type == "http":
            definition.http_config = HTTPToolConfig(
                method=request.http_config.method,
                url=request.http_config.url,
                headers=request.http_config.headers,
                body_template=request.http_config.body_template,
                query_params=request.http_config.query_params,
                timeout_seconds=request.http_config.timeout_seconds,
                response_path=request.http_config.response_path,
                response_format=request.http_config.response_format,
            )
        if request.mcp_config is not None and definition.implementation_type == "mcp":
            definition.mcp_config = MCPToolConfig(
                server_command=request.mcp_config.server_command,
                server_args=request.mcp_config.server_args,
                tool_name=request.mcp_config.tool_name,
                env_vars=request.mcp_config.env_vars,
                working_directory=request.mcp_config.working_directory,
                idle_timeout_seconds=request.mcp_config.idle_timeout_seconds,
                startup_timeout_seconds=request.mcp_config.startup_timeout_seconds,
            )
        if request.enabled is not None:
            definition.enabled = request.enabled
        if request.tags is not None:
            definition.tags = request.tags

        loader.save_definition(definition)

        agent = get_agent()
        agent.reload_tools()

        return _custom_to_unified(definition, True, "default", {})

    @app.delete("/tools/unified/{tool_id}", tags=["Unified Tools"])
    async def delete_unified_tool(
        tool_id: str,
        user: AuthenticatedUser = Depends(require_admin_user),
    ):
        """
        Delete a custom tool via the unified API. Admin-only — mirrors
        ``DELETE /tools/custom/{tool_id}``.

        Only custom tools can be deleted. Built-in tools return 400.
        """
        from ..tools.metadata import get_tool_metadata

        loader = get_custom_tool_loader()

        # Check if it's a built-in tool
        if get_tool_metadata(tool_id):
            raise HTTPException(
                status_code=400,
                detail="Cannot delete built-in tools",
            )

        if not loader.get_definition(tool_id):
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
            )

        loader.delete_definition(tool_id)
        reload_custom_tools()

        return {
            "status": "ok",
            "deleted": tool_id,
        }

    # ========================================================================
    # Voice Endpoints
    # ========================================================================

    @app.post("/voice/chat", tags=["Voice"])
    async def voice_chat(
        audio: UploadFile = File(..., description="Audio file (WAV, MP3, AAC, etc.)"),
        thread_id: Optional[str] = Form(default=None),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Voice conversation: audio in, audio out.

        Accepts an audio file, transcribes it (STT), sends the text through
        the Nymeria agent, then synthesizes the response (TTS) and returns audio.

        Runs under the authenticated caller. The legacy ``user_id`` form field
        was removed in the multi-user refactor — admins impersonate via
        ``X-Nymeria-Act-As``, which ``verify_api_key`` resolves before we get
        here.
        """
        from ..core.voice import get_stt_service, get_tts_service, VoiceServiceError

        settings = get_settings()

        # Resolve target thread + enforce ownership BEFORE STT/TTS so a
        # cross-user probe doesn't waste an STT API call (and so non-admin
        # callers can't infer thread existence from STT vs 404 timing).
        tid = thread_id or settings.voice_default_thread_id or "watch-default"
        _require_thread_access(user, tid)

        try:
            stt = get_stt_service(settings)
            tts = get_tts_service(settings)
        except VoiceServiceError as e:
            raise HTTPException(status_code=503, detail=str(e))

        import time as _time

        # Read uploaded audio
        audio_bytes = await audio.read()
        if not audio_bytes:
            raise HTTPException(status_code=400, detail="Empty audio file")

        # STT: audio -> text
        t0 = _time.monotonic()
        try:
            transcription = await stt.transcribe(
                audio_bytes,
                filename=audio.filename or "recording.wav",
                content_type=audio.content_type or "audio/wav",
            )
        except VoiceServiceError as e:
            raise HTTPException(status_code=502, detail=f"STT failed: {e}")
        stt_elapsed = _time.monotonic() - t0

        if not transcription:
            raise HTTPException(status_code=422, detail="Could not transcribe audio (empty result)")

        logger.info(f"[VOICE] STT ({stt_elapsed:.1f}s): {transcription[:100]}...")

        # Agent: text -> response (use async streaming path for speed)
        agent = get_agent()
        t1 = _time.monotonic()
        try:
            response_text = ""
            async for event in agent.astream(
                transcription, thread_id=tid, user_id=user.id,
                _trigger_override=(
                    "Smartwatch — respond concisely (1-2 sentences max), "
                    "your reply will be spoken aloud via TTS"
                ),
            ):
                if event.get("type") == "response":
                    response_text += event.get("content", "")
                elif event.get("type") == "error":
                    raise Exception(event.get("content", "Unknown agent error"))
        except Exception as e:
            logger.error(f"[VOICE] Agent error: {e}")
            raise HTTPException(status_code=500, detail=f"Agent error: {e}")
        agent_elapsed = _time.monotonic() - t1

        if not response_text:
            response_text = "I received your message but had no response."

        logger.info(f"[VOICE] Agent ({agent_elapsed:.1f}s): {response_text[:100]}...")

        # TTS: text -> audio
        t2 = _time.monotonic()
        try:
            audio_out, content_type = await tts.synthesize(response_text)
        except VoiceServiceError as e:
            raise HTTPException(status_code=502, detail=f"TTS failed: {e}")
        tts_elapsed = _time.monotonic() - t2

        logger.info(f"[VOICE] TTS ({tts_elapsed:.1f}s): {len(audio_out)} bytes")
        logger.info(f"[VOICE] Total pipeline: STT={stt_elapsed:.1f}s + Agent={agent_elapsed:.1f}s + TTS={tts_elapsed:.1f}s = {stt_elapsed+agent_elapsed+tts_elapsed:.1f}s")

        import io
        return StreamingResponse(
            io.BytesIO(audio_out),
            media_type=content_type,
            headers={"Cache-Control": "no-transform"},
        )

    @app.post("/voice/tts", tags=["Voice"])
    async def voice_tts(
        request: Request,
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Text-to-Speech: accepts JSON {text: string}, returns audio bytes.
        """
        from ..core.voice import get_tts_service, VoiceServiceError

        body = await request.json()
        text = body.get("text", "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="'text' field is required")

        settings = get_settings()
        try:
            tts = get_tts_service(settings)
        except VoiceServiceError as e:
            raise HTTPException(status_code=503, detail=str(e))

        try:
            audio_bytes, content_type = await tts.synthesize(text)
        except VoiceServiceError as e:
            raise HTTPException(status_code=502, detail=f"TTS failed: {e}")

        return Response(content=audio_bytes, media_type=content_type)

    @app.post("/voice/stt", tags=["Voice"])
    async def voice_stt(
        audio: UploadFile = File(..., description="Audio file to transcribe"),
        user: AuthenticatedUser = Depends(verify_api_key),
    ):
        """
        Speech-to-Text: accepts audio file upload, returns JSON {text: string}.
        """
        from ..core.voice import get_stt_service, VoiceServiceError

        settings = get_settings()
        try:
            stt = get_stt_service(settings)
        except VoiceServiceError as e:
            raise HTTPException(status_code=503, detail=str(e))

        audio_bytes = await audio.read()
        if not audio_bytes:
            raise HTTPException(status_code=400, detail="Empty audio file")

        try:
            text = await stt.transcribe(
                audio_bytes,
                filename=audio.filename or "recording.wav",
                content_type=audio.content_type or "audio/wav",
            )
        except VoiceServiceError as e:
            raise HTTPException(status_code=502, detail=f"STT failed: {e}")

        return {"text": text}

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
