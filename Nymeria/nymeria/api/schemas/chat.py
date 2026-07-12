"""Interactive chat API schemas."""

from typing import Optional

from pydantic import BaseModel, Field


class FileData(BaseModel):
    """Generic file attachment data for multimodal messages."""

    file_type: str = Field(..., description="File type: 'image' or 'document'")
    data_url: str = Field(..., description="Base64 data URL (data:mime/type;base64,...)")
    mime_type: str = Field(..., description="MIME type (image/jpeg, application/pdf, etc.)")
    file_name: str | None = Field(
        default=None,
        description="Original filename (used for MIME fallback when browser MIME type is missing)",
    )


class ImageData(BaseModel):
    """Image attachment data for multimodal messages (legacy, use FileData)."""

    data_url: str = Field(..., description="Base64 data URL (data:image/...;base64,...)")
    mime_type: str = Field(..., description="MIME type (image/jpeg, image/png, etc.)")


class ChatPlatformOrigin(BaseModel):
    """Provenance of the platform message a chat-bot turn originates from.

    Sent by chat-platform bots (Discord/Telegram) on every dispatched turn so
    the backend can (a) record the origin for the ``react`` tool (which posts
    an emoji reaction back to this message) and (b) for ``kind == "reaction"``
    turns, append the react-tool guidance block to the synthetic prompt when
    the tool is not currently bound to the thread.
    """

    platform: str = Field(
        ..., min_length=1, max_length=32,
        description="Origin chat platform id, e.g. 'discord' or 'telegram'",
    )
    channel_id: str = Field(
        ..., min_length=1, max_length=128,
        description="Platform-native channel/chat id the message lives in",
    )
    message_id: str = Field(
        ..., min_length=1, max_length=128,
        description=(
            "Platform-native id of the originating message: the user's "
            "message for a normal turn, the reacted-to message for a "
            "reaction-triggered turn"
        ),
    )
    kind: str = Field(
        default="message",
        max_length=16,
        description="'message' for a normal turn, 'reaction' for a reaction trigger",
    )


class ChatRequest(BaseModel):
    """Request model for chat endpoint."""

    message: str = Field(
        ..., min_length=1, max_length=1_000_000, description="User message"
    )
    thread_id: Optional[str] = Field(
        default=None, description="Conversation thread ID (generated if not provided)"
    )
    user_id: str = Field(
        default="default",
        description="User ID for profile and memory access (defaults to 'default')",
    )
    attachments: list[FileData] | None = Field(
        default=None,
        max_length=50,
        description="Optional list of file attachments for multimodal models",
    )
    images: list[ImageData] | None = Field(
        default=None, max_length=50, description="Deprecated: use attachments instead"
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
    trigger_override: str | None = Field(
        default=None,
        description="Trigger label for autonomous invocations (e.g. 'watchdog')",
    )
    trigger_id: str | None = Field(
        default=None,
        description=(
            "Trigger ID when trigger_override=='trigger' -- surfaced in autonomous "
            "events for frontend classification."
        ),
    )
    trigger_name: str | None = Field(
        default=None,
        description=(
            "Trigger name when trigger_override=='trigger' -- surfaced in autonomous "
            "events for frontend classification."
        ),
    )
    source: str | None = Field(
        default=None,
        description=(
            "Logical origin of this prompt -- 'user', 'trigger', 'callable', "
            "'ticker', 'watchdog', 'mcp'. Routes the prompt through the "
            "sub-turn pending-prompt queue when the target thread is busy. "
            "Defaults to 'user' (or 'ticker' when is_self_invoke=True is the "
            "only signal)."
        ),
    )
    source_id: str | None = Field(
        default=None,
        description=(
            "Stable id of the source for queued-prompt metadata (e.g. "
            "trigger.id, todo.id, caller_thread.id)."
        ),
    )
    source_label: str | None = Field(
        default=None,
        description=(
            "Human-readable label for the queued-prompt metadata header "
            "(e.g. trigger.name, todo task excerpt, caller_thread title)."
        ),
    )
    platform_origin: ChatPlatformOrigin | None = Field(
        default=None,
        description=(
            "Chat-platform provenance of this turn (set by the Discord/"
            "Telegram bots). Recorded per thread so the react tool can post "
            "an emoji reaction to the originating message; kind=='reaction' "
            "also appends the react-tool guidance block to the prompt when "
            "the tool is unbound."
        ),
    )
    publish_autonomous_events: bool = Field(
        default=True,
        description=(
            "For trusted self-invoke callers, whether /chat should mirror "
            "task_started, agent stream chunks, task_completed, and "
            "autonomous notifications onto the autonomous event bus. "
            "Defaults to True so existing watchdog and webhook-fire paths "
            "keep their API-owned publishing. The Docker worker container "
            "sets this to False because it publishes those events itself "
            "with stable task IDs (todo.id / trigger-<id>) and would "
            "otherwise emit duplicates."
        ),
    )


class ChatResponse(BaseModel):
    """Response model for non-streaming chat."""

    response: str = Field(..., description="Agent response")
    thread_id: str = Field(..., description="Conversation thread ID")
    tool_call_count: int = Field(default=0, description="Number of tool calls made in this turn")
    suppress_reply: bool = Field(
        default=False,
        description=(
            "True when the turn's react tool call asked to suppress the "
            "reply text (the user should see only the emoji reaction). Only "
            "ever set for requests that carried a platform_origin."
        ),
    )
