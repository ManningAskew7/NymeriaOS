"""FastAPI REST API trigger with SSE streaming for Nymeria."""

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..core.user_profile import ToolPreferences

from fastapi import FastAPI, HTTPException, Depends, Header, Query, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..core.agent import NymeriaAgent
from ..core.activity_log import ActivityLog, ActivityEntry, ActivityType, log_activity
from ..core.event_bus import get_event_bus, AutonomousEvent
from ..core.notifications import NotificationStore, Notification
from ..core._deprecated.task_db import TaskDatabase, TaskStatus
from ..core.todo_manager import TodoManager, TodoItem, TodoStatus
from ..tools import ALL_TOOLS, get_all_tools_with_agents
from ..tools.definitions.schema import (
    CustomToolDefinition,
    HTTPToolConfig,
    MCPToolConfig,
    ToolParameter,
)
from ..core.custom_tools import (
    get_custom_tool_loader,
    load_custom_tools,
    reload_custom_tools,
)

logger = logging.getLogger(__name__)

# Global agent instance (initialized on startup)
_agent: Optional[NymeriaAgent] = None


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


class HealthResponse(BaseModel):
    """Response model for health check."""

    status: str = "ok"
    version: str = "1.0.0"


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
    # Context management settings
    context_management: str
    compact_threshold: float
    compact_keep_messages: int
    compact_model: Optional[str] = None
    sliding_window_cycles: int
    max_self_invokes_per_hour: int
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
    # Context management settings
    context_management: Optional[str] = None
    compact_threshold: Optional[float] = None
    compact_keep_messages: Optional[int] = None
    compact_model: Optional[str] = None
    sliding_window_cycles: Optional[int] = None
    max_self_invokes_per_hour: Optional[int] = None
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


class ScheduledTaskResponse(BaseModel):
    """Response model for a scheduled task."""

    id: str
    prompt: str
    execute_at: datetime
    status: str
    created_at: datetime
    thread_id: str


class ScheduledTasksResponse(BaseModel):
    """Response model for scheduled tasks list."""

    tasks: List[ScheduledTaskResponse]
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


# Sub-Agent Models




# ============================================================================
# Authentication
# ============================================================================


async def verify_api_key(
    authorization: Optional[str] = Header(None),
    settings: Settings = Depends(get_settings),
) -> bool:
    """
    Verify API key from Authorization header.

    Expected format: "Bearer <api_key>"
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=401, detail="Invalid Authorization header format. Use: Bearer <api_key>"
        )

    api_key = parts[1]
    if api_key != settings.nymeria_api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return True


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

    # Add webhook router for messaging platform integrations
    from .webhook import create_webhook_router
    webhook_router = create_webhook_router(get_agent, get_settings)
    app.include_router(webhook_router)

    # Add trigger system router (event-driven automation)
    from .trigger_api import create_trigger_router
    trigger_router = create_trigger_router(get_agent, verify_api_key)
    app.include_router(trigger_router)

    # Sync callable thread tools into the registry
    _agent.sync_agent_tools()

    # ========================================================================
    # Endpoints
    # ========================================================================

    @app.get("/health", response_model=HealthResponse, tags=["System"])
    async def health_check():
        """Health check endpoint."""
        return HealthResponse()

    @app.post("/restart", tags=["System"])
    async def restart_server(_: bool = Depends(verify_api_key)):
        """Restart the API server process.

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

    @app.post("/chat", tags=["Chat"])
    async def chat_streaming(
        http_request: Request,
        request: ChatRequest,
        _: bool = Depends(verify_api_key),
    ):
        """
        Send a message and receive streaming response via SSE.

        The response is streamed as Server-Sent Events (SSE) with the following event types:
        - `thinking`: Agent reasoning/planning
        - `tool_call`: Tool being invoked
        - `tool_result`: Tool execution result
        - `response`: Final response text
        - `error`: Error message
        - `done`: Stream complete

        Each event has `type` and `content`/`data` fields.
        """
        agent = get_agent()
        thread_id = request.thread_id or str(uuid.uuid4())[:8]
        user_id = request.user_id

        # Handle slash commands (e.g., /compact)
        msg_stripped = request.message.strip().lower()
        logger.info(f"[CHAT] Received message: '{request.message}' stripped: '{msg_stripped}' is_compact: {msg_stripped == '/compact'}")
        if msg_stripped == "/compact":
            async def compact_command_response():
                # Perform compaction (this may take a few seconds)
                result = await agent.compact_now(thread_id, user_id)
                # Send response based on result
                if result.get("success"):
                    messages_removed = result.get('messages_removed', 0)
                    # Emit compacted event so frontend clears chat UI
                    yield f"data: {json.dumps({'type': 'compacted', 'messages_removed': messages_removed, 'auto_resumed': False, 'thread_id': thread_id})}\n\n"
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

        async def event_generator():
            """Generate SSE events from agent stream."""
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

                async for chunk in agent.astream(
                    request.message,
                    thread_id=thread_id,
                    user_id=user_id,
                    attachments=attachments,
                    images=images,
                    force_unsupported_attachments=request.force_unsupported_attachments,
                ):
                    # Check if client disconnected (user clicked stop)
                    if await http_request.is_disconnected():
                        logger.info(f"Client disconnected for thread {thread_id}, signalling abort")
                        agent.abort_with_cascade(thread_id)
                        break

                    event_data = json.dumps({**chunk, "thread_id": thread_id})
                    yield f"data: {event_data}\n\n"

                # Only send done event if not disconnected
                if not await http_request.is_disconnected():
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
        _: bool = Depends(verify_api_key),
    ):
        """
        Send a message and receive a non-streaming response.

        Simpler alternative to the streaming endpoint for clients that
        don't support SSE.
        """
        agent = get_agent()
        thread_id = request.thread_id or str(uuid.uuid4())[:8]
        user_id = request.user_id

        response = agent.chat(
            request.message,
            thread_id=thread_id,
            user_id=user_id,
        )
        return ChatResponse(response=response, thread_id=thread_id)

    @app.get("/threads/{thread_id}/history", response_model=ThreadHistoryResponse, tags=["Threads"])
    async def get_thread_history(
        thread_id: str,
        include_internal: bool = Query(
            False,
            description="Include internal system messages (autonomous wake-ups, compaction prompts)"
        ),
        _: bool = Depends(verify_api_key),
    ):
        """
        Get conversation history for a thread.

        Returns all messages in the conversation including tool calls and results.
        By default, internal system messages (autonomous wake-ups, compaction prompts)
        are filtered out. Set include_internal=true for debugging to see all messages.
        """
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
        _: bool = Depends(verify_api_key),
    ):
        """
        Get context window usage statistics for a thread.

        Returns token usage, context limit, and compaction history.
        """
        agent = get_agent()
        return agent.get_context_stats(thread_id)

    @app.get("/threads/{thread_id}/metadata", tags=["Threads"])
    async def get_thread_metadata(
        thread_id: str,
        _: bool = Depends(verify_api_key),
    ):
        """
        Get platform metadata for a thread.

        Parses the thread ID to detect platform origin (desktop, discord,
        telegram, slack) and returns relevant metadata.
        """
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

    def _classify_thread_platform(thread_id: str) -> str:
        """Classify a thread ID into its platform origin."""
        if thread_id.startswith("trigger-"):
            return "trigger"
        if thread_id.startswith("discord_"):
            return "discord"
        if thread_id.startswith("telegram_"):
            return "telegram"
        if thread_id.startswith("slack_"):
            return "slack"
        if thread_id.startswith("agent-"):
            return "callable"
        return "desktop"

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

    @app.get("/threads", tags=["Threads"])
    async def list_threads(
        user_id: str = Query(default="default"),
        _: bool = Depends(verify_api_key),
    ):
        """
        List all threads with metadata (titles, pins, platform info).

        Merges thread IDs from the checkpoint database with stored metadata
        so all surfaces see the same thread list.
        """
        agent = get_agent()
        checkpoint_ids = _get_checkpoint_thread_ids()
        checkpoint_set = set(checkpoint_ids)

        # Get stored metadata
        store = agent.thread_metadata_manager.get_store(user_id)

        threads = []

        # 1. Threads in checkpoints (with metadata if available)
        for tid in checkpoint_ids:
            meta = store.threads.get(tid)
            if meta:
                threads.append(meta.model_dump(mode="json"))
            else:
                # Thread exists in checkpoints but has no metadata yet
                threads.append({
                    "thread_id": tid,
                    "title": "New Chat",
                    "pinned": False,
                    "platform": _classify_thread_platform(tid),
                    "platform_meta": None,
                    "created_at": None,
                    "updated_at": None,
                    "title_source": "default",
                })

        # 2. Metadata-only threads (created by frontend but no checkpoint yet)
        for tid, meta in store.threads.items():
            if tid not in checkpoint_set:
                threads.append(meta.model_dump(mode="json"))

        return {"threads": threads, "total": len(threads)}

    # -- Thread metadata endpoints --

    class ThreadMetadataUpdateRequest(BaseModel):
        title: Optional[str] = Field(default=None, max_length=200)
        pinned: Optional[bool] = None

    @app.patch("/threads/{thread_id}/metadata", tags=["Threads"])
    async def update_thread_metadata(
        thread_id: str,
        request: ThreadMetadataUpdateRequest,
        user_id: str = Query(default="default"),
        _: bool = Depends(verify_api_key),
    ):
        """Update thread metadata (title, pin status)."""
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

        # If renaming a callable thread, sync title → callable_name
        if request.title is not None:
            tc = agent.thread_config_manager.get_config(thread_id)
            if tc and tc.callable:
                new_name = request.title.strip()
                # Validate: no collision with core tools or other callables
                from ..tools import ALL_TOOLS
                core_tool_names = {t.name for t in ALL_TOOLS}
                collision = new_name in core_tool_names
                if not collision:
                    existing = agent.thread_config_manager.get_callable_thread_by_name(new_name)
                    collision = existing is not None and existing.thread_id != thread_id
                if not collision and new_name:
                    tc.callable_name = new_name
                    agent.thread_config_manager.save_config(tc)
                    agent.invalidate_thread_config_cache(thread_id)
                    agent.sync_agent_tools()
                    # Override title_source to "callable" for callable threads
                    fields["title_source"] = "callable"

        meta = agent.thread_metadata_manager.upsert_thread(
            user_id, thread_id, **fields
        )
        return meta.model_dump(mode="json")

    class ThreadMetadataMigrateRequest(BaseModel):
        threads: List[dict] = Field(default_factory=list)

    @app.post("/threads/metadata/migrate", tags=["Threads"])
    async def migrate_thread_metadata(
        request: ThreadMetadataMigrateRequest,
        user_id: str = Query(default="default"),
        _: bool = Depends(verify_api_key),
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

    @app.delete("/threads/{thread_id}", tags=["Threads"])
    async def delete_thread(
        thread_id: str,
        user_id: str = Query(default="default"),
        _: bool = Depends(verify_api_key),
    ):
        """
        Fully delete a thread: metadata, checkpoints, and config.

        This is the proper way to remove a thread from all surfaces.
        """
        agent = get_agent()
        settings = get_settings()

        # 1. Delete metadata
        agent.thread_metadata_manager.delete_thread(user_id, thread_id)

        # 2. Delete checkpoints
        if settings.database_backend == "sqlite":
            import sqlite3 as _sqlite3
            try:
                conn = _sqlite3.connect(str(settings.db_path))
                conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,)
                )
                # Also clean checkpoint_writes if the table exists
                try:
                    conn.execute(
                        "DELETE FROM checkpoint_writes WHERE thread_id = ?",
                        (thread_id,),
                    )
                except Exception:
                    pass  # Table may not exist
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
                    conn.commit()
            except Exception as e:
                logger.warning(f"Failed to delete checkpoints for {thread_id}: {e}")

        # 3. Delete thread config (if any)
        try:
            tc = agent.thread_config_manager.get_config(thread_id)
            was_callable = tc.callable if tc else False
            agent.thread_config_manager.delete_config(thread_id)
            agent.invalidate_thread_config_cache(thread_id)
            if was_callable:
                agent.sync_agent_tools()
        except Exception as e:
            logger.warning(f"Failed to delete thread config for {thread_id}: {e}")

        # 4. Delete thread notepad (if any)
        try:
            from ..tools.thread_notes import delete_notepad
            delete_notepad(thread_id)
        except Exception as e:
            logger.warning(f"Failed to delete notepad for {thread_id}: {e}")

        logger.info(f"Thread {thread_id} fully deleted")
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

    class ThreadConfigUpdateRequest(BaseModel):
        instructions: Optional[str] = Field(default=None, max_length=5000)
        disabled_tools: Optional[List[str]] = None
        enabled_tools: Optional[List[str]] = None
        llm_config: Optional[ThreadLLMConfigRequest] = None
        system_prompt: Optional[str] = Field(default=None, max_length=50000)
        callable: Optional[bool] = None
        callable_name: Optional[str] = Field(default=None, max_length=64)
        callable_description: Optional[str] = Field(default=None, max_length=500)
        show_autonomous_prompts: Optional[bool] = None
        show_prompt_metadata: Optional[bool] = None
        clear_instructions: bool = False
        clear_disabled_tools: bool = False
        clear_enabled_tools: bool = False
        clear_llm_config: bool = False
        clear_system_prompt: bool = False

    @app.get("/threads/{thread_id}/config", tags=["Threads"])
    async def get_thread_config(
        thread_id: str,
        _: bool = Depends(verify_api_key),
    ):
        """Get per-thread configuration (returns defaults if none saved)."""
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
            "show_autonomous_prompts": False,
            "show_prompt_metadata": False,
            "created_at": None,
            "updated_at": None,
            "has_customizations": False,
        }

    @app.patch("/threads/{thread_id}/config", tags=["Threads"])
    async def update_thread_config(
        thread_id: str,
        request: ThreadConfigUpdateRequest,
        _: bool = Depends(verify_api_key),
    ):
        """Update per-thread configuration (partial update)."""
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
            tc.enabled_tools = request.enabled_tools
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
                from ..tools import ALL_TOOLS
                core_tool_names = {t.name for t in ALL_TOOLS}
                if request.callable_name in core_tool_names:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Callable name '{request.callable_name}' conflicts with a core tool name",
                    )
                # Check for duplicate callable_name across other threads
                existing = agent.thread_config_manager.get_callable_thread_by_name(request.callable_name)
                if existing and existing.thread_id != thread_id:
                    raise HTTPException(
                        status_code=409,
                        detail=f"Callable name '{request.callable_name}' is already used by thread {existing.thread_id}",
                    )
            tc.callable_name = request.callable_name
        if request.callable_description is not None:
            tc.callable_description = request.callable_description
        if request.show_autonomous_prompts is not None:
            tc.show_autonomous_prompts = request.show_autonomous_prompts
        if request.show_prompt_metadata is not None:
            tc.show_prompt_metadata = request.show_prompt_metadata

        if not agent.thread_config_manager.save_config(tc):
            raise HTTPException(status_code=500, detail="Failed to save thread config")

        agent.invalidate_thread_config_cache(thread_id)

        # If this is an agent thread config change, rebuild agent tools
        if request.callable is not None or request.callable_name is not None or request.callable_description is not None:
            agent.sync_agent_tools()

        # Sync callable thread metadata (title = callable_name)
        if tc.callable and tc.callable_name:
            agent.thread_metadata_manager.upsert_thread(
                "default", thread_id,
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
        _: bool = Depends(verify_api_key),
    ):
        """Reset thread to global defaults (delete custom config)."""
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

    @app.post(
        "/threads/{thread_id}/attachments/validate",
        response_model=AttachmentValidationResponse,
        tags=["Threads"],
    )
    async def validate_thread_attachments(
        thread_id: str,
        request: AttachmentValidationRequest,
        _: bool = Depends(verify_api_key),
    ):
        """
        Preflight-check attachment compatibility against the effective thread model.

        This does not send content to any model. It only evaluates whether
        attachments are likely compatible and provides warnings plus force-send guidance.
        """
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
        user_id: str = "default",
        _: bool = Depends(verify_api_key),
    ):
        """
        Manually trigger compaction for a thread.

        Compresses conversation history into a summary while preserving recent messages.
        """
        agent = get_agent()
        result = await agent.compact_now(thread_id, user_id)
        return result

    @app.post("/threads/{thread_id}/stop", tags=["Threads"])
    async def stop_thread(thread_id: str, _=Depends(verify_api_key)):
        """Stop any running operation on a thread.

        Signals the abort event for the thread and cascades to any active
        callable threads it has spawned. The current stream()/astream() call
        breaks at the next iteration boundary, releasing the thread lock.
        """
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
    async def list_tools(_: bool = Depends(verify_api_key)):
        """List all available tools and their descriptions."""
        agent = get_agent()
        tools = agent.tool_registry.list_tools()
        return {"tools": tools}

    @app.get("/tools/optional", tags=["Tools"])
    async def list_optional_tools(
        user_id: str = Query("default"),
        _: bool = Depends(verify_api_key),
    ):
        """List tools available for per-thread enabling (not in the user's core set)."""
        from ..tools import ALL_TOOLS, OPTIONAL_TOOLS

        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)
        default_tools = profile.tool_preferences.default_thread_tools

        core_set = set(default_tools) if default_tools is not None else {t.name for t in ALL_TOOLS}
        result = []
        seen = set()
        for t in ALL_TOOLS:
            if t.name not in core_set and t.name not in seen:
                result.append({"name": t.name, "description": t.description})
                seen.add(t.name)
        for name, tool in OPTIONAL_TOOLS.items():
            if name not in core_set and name not in seen:
                result.append({"name": name, "description": tool.description})
                seen.add(name)
        return {"tools": result}

    @app.get("/tools/defaults", tags=["Tools"])
    async def get_default_tools(
        user_id: str = Query("default"),
        _: bool = Depends(verify_api_key),
    ):
        """Get the default tool set for new threads.

        Returns all available tools (core + optional) with is_default flags
        and callable thread count. Always uses default_thread_tools as source of truth.
        """
        from ..tools import ALL_TOOLS, OPTIONAL_TOOLS
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
            if name not in seen:
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

        callable_count = len(agent.thread_config_manager.list_callable_threads())

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
        user_id: str = Query("default"),
        _: bool = Depends(verify_api_key),
    ):
        """Set which tools new threads inherit by default."""
        from ..tools import ALL_TOOLS, OPTIONAL_TOOLS
        from ..tools.metadata import MCP_SERVER_TOOL_METADATA

        # Validate tool names (allow built-in, optional, and MCP server tools)
        known = {t.name for t in ALL_TOOLS} | set(OPTIONAL_TOOLS.keys()) | set(MCP_SERVER_TOOL_METADATA.keys())
        unknown = set(request.tool_names) - known
        if unknown:
            raise HTTPException(400, detail=f"Unknown tools: {sorted(unknown)}")

        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)
        profile.tool_preferences.default_thread_tools = list(request.tool_names)
        agent.profile_manager.save_profile(profile)

        # Clear graph caches and rebuild defaults so new threads pick up the change
        agent._user_graphs.clear()
        agent._async_user_graphs.clear()
        agent._default_graph = agent._build_graph_with_prompt(agent._base_system_prompt)
        agent._default_async_graph = agent._build_async_graph_with_prompt(agent._base_system_prompt)

        return {
            "status": "ok",
            "default_tools": sorted(request.tool_names),
            "count": len(request.tool_names),
        }

    @app.delete("/tools/defaults", tags=["Tools"])
    async def reset_default_tools(
        user_id: str = Query("default"),
        _: bool = Depends(verify_api_key),
    ):
        """Reset default tools to all core tools."""
        from ..tools import ALL_TOOLS

        agent = get_agent()
        profile = agent.profile_manager.get_profile(user_id)
        profile.tool_preferences.default_thread_tools = [t.name for t in ALL_TOOLS]
        agent.profile_manager.save_profile(profile)

        # Clear graph caches and rebuild defaults
        agent._user_graphs.clear()
        agent._async_user_graphs.clear()
        agent._default_graph = agent._build_graph_with_prompt(agent._base_system_prompt)
        agent._default_async_graph = agent._build_async_graph_with_prompt(agent._base_system_prompt)

        return {
            "status": "ok",
            "mode": "custom",
            "default_tools": sorted(profile.tool_preferences.default_thread_tools),
        }

    @app.get("/settings", response_model=ServerSettingsResponse, tags=["Settings"])
    async def get_server_settings(
        _: bool = Depends(verify_api_key),
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
            context_management=settings.context_management,
            compact_threshold=settings.compact_threshold,
            compact_keep_messages=settings.compact_keep_messages,
            compact_model=settings.compact_model,
            sliding_window_cycles=settings.sliding_window_cycles,
            max_self_invokes_per_hour=settings.max_self_invokes_per_hour,
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
        _: bool = Depends(verify_api_key),
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
        _: bool = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Update server settings with hot-reload.

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
            "context_management": "CONTEXT_MANAGEMENT",
            "compact_threshold": "COMPACT_THRESHOLD",
            "compact_keep_messages": "COMPACT_KEEP_MESSAGES",
            "compact_model": "COMPACT_MODEL",
            "sliding_window_cycles": "SLIDING_WINDOW_CYCLES",
            "max_self_invokes_per_hour": "MAX_SELF_INVOKES_PER_HOUR",
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
                       "llm_use_model_defaults", "llm_base_url"}
        if llm_fields & set(updates_dict.keys()):
            # Clear graph caches so they rebuild with new LLM config
            with agent._graph_cache_lock:
                agent._user_graphs.clear()
                agent._async_user_graphs.clear()
            # Rebuild default graphs
            agent._default_graph = agent._build_graph_with_prompt(agent._base_system_prompt)
            agent._default_async_graph = agent._build_async_graph_with_prompt(agent._base_system_prompt)
            logger.info(f"Hot-reloaded LLM settings: {llm_fields & set(updates_dict.keys())}")

        return {
            "message": "Settings updated and applied",
            "updated": list(updates_dict.keys()),
            "restart_required": False,
        }

    # ========================================================================
    # Model Metadata Endpoint
    # ========================================================================

    @app.get("/models", tags=["Settings"])
    async def get_openrouter_models(
        _: bool = Depends(verify_api_key),
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

    # ========================================================================
    # Dashboard Endpoints
    # ========================================================================

    @app.get("/todos/thread-counts", tags=["Dashboard"])
    async def get_thread_task_counts(
        user_id: str = Query(default="default", description="User ID"),
        _: bool = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """Get active task count per thread for badge display."""
        todo_manager = TodoManager(settings.data_dir)
        todo_list = todo_manager.get_todos(user_id)
        return todo_list.get_thread_task_counts()

    @app.get("/todos", response_model=TodoListResponse, tags=["Dashboard"])
    async def get_todos(
        user_id: str = Query(default="default", description="User ID"),
        filter_status: Optional[str] = Query(default=None, description="Filter by status"),
        thread_id: Optional[str] = Query(default=None, description="Filter by thread ID"),
        _: bool = Depends(verify_api_key),
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

    @app.post("/todos", response_model=TodoItemResponse, tags=["Dashboard"])
    async def create_todo(
        request: TodoCreateRequest,
        user_id: str = Query(default="default", description="User ID"),
        _: bool = Depends(verify_api_key),
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
        user_id: str = Query(default="default", description="User ID"),
        _: bool = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Update an existing TODO item.
        """
        from ..core.todo_schedule_db import TodoScheduleDB
        from ..core.todo_constants import VALID_RECURRENCES

        todo_manager = TodoManager(settings.data_dir)

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

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found"
                )

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
        schedule_db = TodoScheduleDB(settings.data_dir / "todo_schedule.db")
        todo_manager.sync_schedule_to_db(user_id, updated_item.id, schedule_db)
        logger.info(f"[API] Schedule synced for TODO {updated_item.id}")

        return _todo_to_response(updated_item)

    @app.delete("/todos/{todo_id}", tags=["Dashboard"])
    async def delete_todo(
        todo_id: str,
        user_id: str = Query(default="default", description="User ID"),
        _: bool = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Delete a TODO item.
        """
        from ..core.todo_schedule_db import TodoScheduleDB

        todo_manager = TodoManager(settings.data_dir)

        with todo_manager.atomic_update(user_id) as todo_list:
            deleted = todo_list.delete_item(todo_id)
            if not deleted:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found"
                )

            # Remove from schedule if it was scheduled
            schedule_db = TodoScheduleDB(settings.data_dir / "todo_schedule.db")
            schedule_db.remove_scheduled(todo_id)

            return {"status": "ok", "deleted_id": todo_id}

    @app.post("/todos/{todo_id}/complete", response_model=TodoItemResponse, tags=["Dashboard"])
    async def complete_todo(
        todo_id: str,
        user_id: str = Query(default="default", description="User ID"),
        _: bool = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Mark a TODO item as done.
        """
        from ..core.todo_schedule_db import TodoScheduleDB

        todo_manager = TodoManager(settings.data_dir)

        with todo_manager.atomic_update(user_id) as todo_list:
            item = todo_list.get_item(todo_id)
            if not item:
                raise HTTPException(
                    status_code=404,
                    detail=f"TODO '{todo_id}' not found"
                )

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
            schedule_db = TodoScheduleDB(settings.data_dir / "todo_schedule.db")
            if has_recurrence and item.scheduled_for:
                todo_manager.sync_schedule_to_db(user_id, todo_id, schedule_db)
            else:
                schedule_db.remove_scheduled(todo_id)

            return _todo_to_response(item)

    @app.get("/tasks", response_model=ScheduledTasksResponse, tags=["Dashboard"])
    async def get_scheduled_tasks(
        user_id: str = Query(default="default", description="User ID"),
        _: bool = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Get scheduled tasks for a user.

        Returns pending and processing tasks.
        """
        task_db = TaskDatabase(settings.data_dir / "tasks.db")

        tasks = []

        # Get pending task
        pending = task_db.get_pending_for_user(user_id)
        if pending:
            tasks.append(pending)

        # Get processing task
        processing = task_db.get_processing_for_user(user_id)
        if processing:
            tasks.append(processing)

        return ScheduledTasksResponse(
            tasks=[
                ScheduledTaskResponse(
                    id=task.id,
                    prompt=task.prompt,
                    execute_at=datetime.fromtimestamp(task.execute_at, tz=timezone.utc),
                    status=task.status.value,
                    created_at=datetime.fromtimestamp(task.created_at, tz=timezone.utc),
                    thread_id=task.thread_id,
                )
                for task in tasks
            ],
            total=len(tasks),
        )

    @app.get("/activity", response_model=ActivityLogResponse, tags=["Dashboard"])
    async def get_activity(
        user_id: str = Query(default="default", description="User ID"),
        limit: int = Query(default=50, le=100, description="Max entries to return"),
        activity_type: Optional[str] = Query(default=None, description="Filter by type"),
        thread_id: Optional[str] = Query(default=None, description="Filter by thread ID"),
        _: bool = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Get activity log for a user.

        Returns recent activity entries, newest first.
        Optionally filter by thread_id.
        """
        activity_log = ActivityLog(settings.data_dir)

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
        user_id: str = Query(default="default", description="User ID"),
        _: bool = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Get notifications for a user.

        Returns all notifications with unread count.
        """
        store = NotificationStore(settings.data_dir)
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
        user_id: str = Query(default="default", description="User ID"),
        _: bool = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Mark a notification as read.
        """
        store = NotificationStore(settings.data_dir)
        success = store.mark_read(notification_id, user_id)

        if not success:
            raise HTTPException(
                status_code=404,
                detail=f"Notification '{notification_id}' not found",
            )

        return {"status": "ok", "notification_id": notification_id}

    @app.post("/notifications/read-all", tags=["Dashboard"])
    async def mark_all_notifications_read(
        user_id: str = Query(default="default", description="User ID"),
        _: bool = Depends(verify_api_key),
        settings: Settings = Depends(get_settings),
    ):
        """
        Mark all notifications as read for a user.
        """
        store = NotificationStore(settings.data_dir)
        count = store.mark_all_read(user_id)

        return {"status": "ok", "marked_read": count}

    @app.get("/autonomous/stream", tags=["Autonomous"])
    async def stream_autonomous_events(
        request: Request,
        user_id: str = Query(default="default", description="User ID to filter events"),
        api_key: Optional[str] = Query(default=None, description="API key (for SSE which doesn't support headers)"),
        settings: Settings = Depends(get_settings),
    ):
        # Verify API key from query param (SSE doesn't support custom headers)
        if api_key != settings.nymeria_api_key:
            raise HTTPException(status_code=401, detail="Invalid API key")
        """
        Stream autonomous task events via Server-Sent Events.

        Emits events when Nymeria executes scheduled tasks autonomously.
        Events include: task_started, thinking, tool_call, tool_result, response, task_completed

        Connect to this endpoint to receive real-time updates about autonomous activity.
        """
        import asyncio
        from queue import Empty

        subscriber_id = str(uuid.uuid4())
        event_bus = get_event_bus()
        queue = event_bus.subscribe(subscriber_id)
        logger.info(f"[AUTONOMOUS SSE] Client connected for user={user_id}, subscriber={subscriber_id[:8]}...")

        async def event_generator():
            """Generate SSE events from the event bus."""
            try:
                while True:
                    # Check if client disconnected
                    if await request.is_disconnected():
                        logger.info(f"[AUTONOMOUS SSE] Client disconnected: {subscriber_id[:8]}...")
                        break

                    try:
                        # Non-blocking check for events
                        event: AutonomousEvent = queue.get_nowait()

                        # Filter by user_id if specified
                        if user_id != "default" and event.user_id != user_id:
                            continue

                        # Format as SSE event
                        event_data = {
                            "type": event.event_type,
                            "thread_id": event.thread_id,
                            "task_id": event.task_id,
                            "timestamp": event.timestamp.isoformat(),
                            **event.data,
                        }
                        yield f"data: {json.dumps(event_data)}\n\n"

                    except Empty:
                        # No events, send heartbeat to keep connection alive
                        yield f": heartbeat\n\n"
                        await asyncio.sleep(1)

            finally:
                event_bus.unsubscribe(subscriber_id)

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
        _: bool = Depends(verify_api_key),
    ):
        """List all custom tools."""
        loader = get_custom_tool_loader()
        definitions = loader.get_all_definitions()

        return CustomToolListResponse(
            tools=[_tool_definition_to_response(d) for d in definitions],
            total=len(definitions),
        )

    @app.post("/tools/custom", response_model=CustomToolResponse, tags=["Custom Tools"])
    async def create_custom_tool(
        request: CustomToolCreateRequest,
        _: bool = Depends(verify_api_key),
    ):
        """Create a new custom tool."""
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
        _: bool = Depends(verify_api_key),
    ):
        """Get a custom tool by ID."""
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
        _: bool = Depends(verify_api_key),
    ):
        """Update an existing custom tool."""
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
        _: bool = Depends(verify_api_key),
    ):
        """Delete a custom tool."""
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
        _: bool = Depends(verify_api_key),
    ):
        """Test a custom tool with sample parameters."""
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
        _: bool = Depends(verify_api_key),
    ):
        """Export all custom tools as JSON."""
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
        _: bool = Depends(verify_api_key),
    ):
        """Import custom tools from JSON."""
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
    async def list_mcp_servers(_: bool = Depends(verify_api_key)):
        """List all MCP server definitions with discovered tools."""
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
        _: bool = Depends(verify_api_key),
    ):
        """Add a new MCP server. Saves config and triggers tool discovery."""
        from ..core.mcp_servers import get_mcp_server_registry
        from ..tools.definitions.schema import MCPServerDefinition

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

        # If thread_id provided, auto-enable all discovered tools for that thread
        if thread_id and discovered:
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
        _: bool = Depends(verify_api_key),
    ):
        """Get a specific MCP server definition."""
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
        _: bool = Depends(verify_api_key),
    ):
        """Update an MCP server config. Re-discovers tools after update."""
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
        _: bool = Depends(verify_api_key),
    ):
        """Remove an MCP server and all its tools."""
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
        _: bool = Depends(verify_api_key),
    ):
        """Force re-discover tools from an MCP server."""
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
        _: bool = Depends(verify_api_key),
    ):
        """Test connectivity to an MCP server."""
        from ..core.mcp_servers import get_mcp_server_registry

        registry = get_mcp_server_registry()
        if not registry.get_server(server_id):
            raise HTTPException(404, detail=f"MCP server '{server_id}' not found")

        result = registry.test_connection(server_id)
        return result

    # ========================================================================
    # Agent Thread Endpoints
    # ========================================================================

    @app.get("/agents/templates", tags=["Agent Threads"])
    async def list_agent_templates(_: bool = Depends(verify_api_key)):
        """List agent templates (legacy — returns empty list)."""
        return {"templates": [], "total": 0}

    @app.get("/agents/threads", tags=["Agent Threads"])
    async def list_agent_threads(_: bool = Depends(verify_api_key)):
        """List all callable thread configs (callable=True)."""
        agent = get_agent()
        threads = agent.thread_config_manager.list_callable_threads()
        result = []
        for tc in threads:
            data = tc.model_dump(mode="json")
            data["has_customizations"] = tc.has_customizations()
            result.append(data)
        return {"threads": result, "total": len(result)}

    class AgentThreadCreateRequest(BaseModel):
        """Request to create a new callable thread."""
        callable_name: str = Field(..., min_length=1, max_length=64)
        callable_description: str = Field(default="", max_length=500)
        system_prompt: str = Field(default="", max_length=50000)
        llm_provider: Optional[str] = None
        llm_model: Optional[str] = None
        llm_temperature: Optional[float] = None
        llm_max_tokens: Optional[int] = None

    @app.post("/agents/threads", tags=["Agent Threads"])
    async def create_agent_thread(
        request: AgentThreadCreateRequest,
        _: bool = Depends(verify_api_key),
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

        # Check if a callable thread with this name already exists
        existing = agent.thread_config_manager.get_callable_thread_by_name(request.callable_name)
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

        # Create thread metadata with callable_name as title
        agent.thread_metadata_manager.upsert_thread(
            "default", thread_id,
            title=request.callable_name,
            title_source="callable",
            platform="callable",
        )

        # Rebuild agent tools to include the new callable thread
        agent.sync_agent_tools()

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
        _: bool = Depends(verify_api_key),
    ):
        """Get user's RAG configuration."""
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
        _: bool = Depends(verify_api_key),
    ):
        """Update user's RAG configuration."""
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
        _: bool = Depends(verify_api_key),
    ):
        """Get indexing statistics for a user."""
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

    @app.post("/users/{user_id}/rag/reindex", tags=["RAG"])
    async def reindex_user(
        user_id: str,
        _: bool = Depends(verify_api_key),
    ):
        """
        Rebuild user's entire RAG index from conversation history.

        This clears existing index and re-indexes all conversations.
        """
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

            # Clear existing index
            cleared = memory_index.clear_index(user_id)

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
                "cleared_chunks": cleared,
                "indexed_memories": indexed_memories,
                "message": "Index rebuilt. New conversations will be indexed automatically.",
            }

        except Exception as e:
            logger.error(f"Failed to reindex for user {user_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.delete("/users/{user_id}/rag/index", tags=["RAG"])
    async def clear_index(
        user_id: str,
        _: bool = Depends(verify_api_key),
    ):
        """Clear user's RAG index."""
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
        _: bool = Depends(verify_api_key),
    ):
        """
        List all tools with their enabled state for a specific user.

        Enabled state is derived from default_thread_tools membership.
        """
        from ..tools import ALL_TOOLS, OPTIONAL_TOOLS
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

        for name, t in OPTIONAL_TOOLS.items():
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
        _: bool = Depends(verify_api_key),
    ):
        """Get user's tool preferences."""
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
        _: bool = Depends(verify_api_key),
    ):
        """
        Set configuration for a specific tool.

        Configuration options depend on the tool. For example, bash_execute
        supports timeout_seconds.
        """
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
        _: bool = Depends(verify_api_key),
    ):
        """Reset all tool preferences to defaults (all core tools enabled)."""
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

    @app.get("/tools/categories", tags=["Tools"])
    async def list_tool_categories(
        _: bool = Depends(verify_api_key),
    ):
        """List all tool categories with their tools."""
        from ..tools.metadata import get_category_tools_summary

        return {
            "categories": get_category_tools_summary(),
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
        # Determine implementation type
        impl_type = None
        http_config = None
        mcp_config = None

        if defn.http:
            impl_type = "http"
            http_config = {
                "url": defn.http.url,
                "method": defn.http.method,
                "headers": defn.http.headers,
                "body_template": defn.http.body_template,
                "timeout": defn.http.timeout,
                "retries": defn.http.retries,
            }
        elif defn.mcp:
            impl_type = "mcp"
            mcp_config = {
                "server": defn.mcp.server,
                "tool": defn.mcp.tool,
            }

        # Convert parameters to dict
        params = None
        if defn.parameters:
            params = {p.name: p.model_dump() for p in defn.parameters}

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
        _: bool = Depends(verify_api_key),
    ):
        """
        List all tools (built-in and custom) in a unified format.

        Returns tools with consistent structure regardless of type,
        including enable status per user derived from default_thread_tools.
        """
        from ..tools import ALL_TOOLS, OPTIONAL_TOOLS
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
            if name in seen:
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

        # Custom tools: always available (managed per-thread, not via global toggle)
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
        _: bool = Depends(verify_api_key),
    ):
        """
        Enable or disable a built-in or MCP server tool for a user by adding/removing it
        from default_thread_tools. Custom tools are not affected (they are
        always available and managed per-thread via enabled_tools).
        """
        from ..tools import ALL_TOOLS, OPTIONAL_TOOLS
        from ..tools.metadata import get_tool_metadata, get_all_tool_metadata

        agent = get_agent()

        # Check if tool exists as built-in or MCP server tool
        tool_meta = get_all_tool_metadata(tool_id)
        if not tool_meta:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_id}' not found",
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
        _: bool = Depends(verify_api_key),
    ):
        """
        Set a custom description for a tool.

        Works for both built-in and custom tools.
        Pass null/None to clear the custom description and revert to default.
        """
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
        _: bool = Depends(verify_api_key),
    ):
        """
        Set configuration for a tool.

        Works for both built-in and custom tools.
        The config is merged with existing config (pass empty dict to clear).
        """
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
        _: bool = Depends(verify_api_key),
    ):
        """
        Create a new custom tool via the unified API.

        Same as POST /tools/custom but returns unified response format.
        """
        loader = get_custom_tool_loader()

        # Check if tool already exists
        if loader.get_definition(request.id):
            raise HTTPException(
                status_code=400,
                detail=f"Tool '{request.id}' already exists",
            )

        # Build parameters list
        params = None
        if request.parameters:
            from ..tools.definitions.schema import ToolParameter
            params = [ToolParameter(**p) for p in request.parameters]

        # Build HTTP config
        http_config = None
        if request.http:
            http_config = HTTPToolConfig(**request.http)

        # Build MCP config
        mcp_config = None
        if request.mcp:
            from ..tools.definitions.schema import MCPToolConfig
            mcp_config = MCPToolConfig(**request.mcp)

        definition = CustomToolDefinition(
            id=request.id,
            name=request.name,
            description=request.description,
            parameters=params,
            http=http_config,
            mcp=mcp_config,
            tags=request.tags,
        )

        loader.save_definition(definition)

        # Reload custom tools
        reload_custom_tools()

        return _custom_to_unified(definition, True, "default", {})

    @app.put("/tools/unified/{tool_id}", response_model=UnifiedToolResponse, tags=["Unified Tools"])
    async def update_unified_tool(
        tool_id: str,
        request: CustomToolUpdateRequest,
        _: bool = Depends(verify_api_key),
    ):
        """
        Update a custom tool via the unified API.

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
            from ..tools.definitions.schema import ToolParameter
            definition.parameters = [ToolParameter(**p) for p in request.parameters]
        if request.http is not None:
            definition.http = HTTPToolConfig(**request.http)
        if request.mcp is not None:
            from ..tools.definitions.schema import MCPToolConfig
            definition.mcp = MCPToolConfig(**request.mcp)
        if request.tags is not None:
            definition.tags = request.tags

        loader.save_definition(definition)
        reload_custom_tools()

        return _custom_to_unified(definition, True, "default", {})

    @app.delete("/tools/unified/{tool_id}", tags=["Unified Tools"])
    async def delete_unified_tool(
        tool_id: str,
        _: bool = Depends(verify_api_key),
    ):
        """
        Delete a custom tool via the unified API.

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
        user_id: str = Form(default="default"),
        _: bool = Depends(verify_api_key),
    ):
        """
        Voice conversation: audio in, audio out.

        Accepts an audio file, transcribes it (STT), sends the text through
        the Nymeria agent, then synthesizes the response (TTS) and returns audio.
        """
        from ..core.voice import get_stt_service, get_tts_service, VoiceServiceError

        settings = get_settings()

        try:
            stt = get_stt_service(settings)
            tts = get_tts_service(settings)
        except VoiceServiceError as e:
            raise HTTPException(status_code=503, detail=str(e))

        # Read uploaded audio
        audio_bytes = await audio.read()
        if not audio_bytes:
            raise HTTPException(status_code=400, detail="Empty audio file")

        # STT: audio -> text
        try:
            transcription = await stt.transcribe(
                audio_bytes,
                filename=audio.filename or "recording.wav",
                content_type=audio.content_type or "audio/wav",
            )
        except VoiceServiceError as e:
            raise HTTPException(status_code=502, detail=f"STT failed: {e}")

        if not transcription:
            raise HTTPException(status_code=422, detail="Could not transcribe audio (empty result)")

        logger.info(f"[VOICE] STT transcription: {transcription[:100]}...")

        # Agent: text -> response
        agent = get_agent()
        tid = thread_id or settings.voice_default_thread_id or "watch-default"
        try:
            response_text = agent.chat(
                transcription,
                thread_id=tid,
                user_id=user_id,
                _trigger_override="Smartwatch — respond concisely, your reply will be spoken aloud",
            )
        except Exception as e:
            logger.error(f"[VOICE] Agent error: {e}")
            raise HTTPException(status_code=500, detail=f"Agent error: {e}")

        if not response_text:
            response_text = "I received your message but had no response."

        # TTS: text -> audio
        try:
            audio_out, content_type = await tts.synthesize(response_text)
        except VoiceServiceError as e:
            raise HTTPException(status_code=502, detail=f"TTS failed: {e}")

        import io
        return StreamingResponse(
            io.BytesIO(audio_out),
            media_type=content_type,
            headers={"Cache-Control": "no-transform"},
        )

    @app.post("/voice/tts", tags=["Voice"])
    async def voice_tts(
        request: Request,
        _: bool = Depends(verify_api_key),
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
        _: bool = Depends(verify_api_key),
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
