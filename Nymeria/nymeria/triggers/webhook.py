"""Webhook endpoint for receiving messages from messaging platforms."""

import hashlib
import hmac
import json
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from ..core.agent import NymeriaAgent

logger = logging.getLogger(__name__)


# ============================================================================
# Data Models
# ============================================================================


@dataclass
class IncomingMessage:
    """Normalized incoming message from any platform."""

    platform: str  # telegram, discord, slack, generic
    sender_id: str
    sender_name: str
    channel_id: str
    message_text: str
    raw_payload: Dict[str, Any] = field(default_factory=dict)
    context_messages: List[Dict[str, str]] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    message_id: Optional[str] = None
    reply_to_id: Optional[str] = None
    attachments: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class OutgoingMessage:
    """Response message to be sent back to platform."""

    text: str
    reply_to_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class WebhookResponse(BaseModel):
    """Standard webhook response."""

    success: bool
    message: Optional[str] = None
    data: Optional[Dict[str, Any]] = None


# ============================================================================
# Platform Adapters
# ============================================================================


class PlatformAdapter(ABC):
    """Base class for platform-specific message parsing and formatting."""

    @abstractmethod
    def parse_request(self, payload: Dict[str, Any]) -> Optional[IncomingMessage]:
        """
        Parse platform-specific payload into normalized IncomingMessage.

        Args:
            payload: Raw webhook payload from the platform

        Returns:
            IncomingMessage if valid message, None if should be ignored
        """
        pass

    @abstractmethod
    def format_response(self, message: OutgoingMessage) -> Dict[str, Any]:
        """
        Format response for the platform's expected format.

        Args:
            message: Normalized outgoing message

        Returns:
            Platform-specific response payload
        """
        pass

    def validate_signature(
        self,
        payload: bytes,
        signature: Optional[str],
        secret: Optional[str],
    ) -> bool:
        """
        Validate webhook signature if applicable.

        Args:
            payload: Raw request body
            signature: Signature from request header
            secret: Webhook secret

        Returns:
            True if valid or no validation required
        """
        return True  # Default: no signature validation


class TelegramAdapter(PlatformAdapter):
    """Adapter for Telegram Bot API webhooks."""

    def parse_request(self, payload: Dict[str, Any]) -> Optional[IncomingMessage]:
        """Parse Telegram update payload."""
        # Handle message updates
        message = payload.get("message") or payload.get("edited_message")
        if not message:
            return None

        # Skip non-text messages for now
        text = message.get("text")
        if not text:
            return None

        sender = message.get("from", {})
        chat = message.get("chat", {})

        return IncomingMessage(
            platform="telegram",
            sender_id=str(sender.get("id", "")),
            sender_name=sender.get("first_name", "") + " " + sender.get("last_name", ""),
            channel_id=str(chat.get("id", "")),
            message_text=text,
            raw_payload=payload,
            message_id=str(message.get("message_id", "")),
            reply_to_id=str(message.get("reply_to_message", {}).get("message_id", ""))
            if message.get("reply_to_message")
            else None,
        )

    def format_response(self, message: OutgoingMessage) -> Dict[str, Any]:
        """Format response for Telegram."""
        response = {
            "method": "sendMessage",
            "text": message.text,
        }
        if message.reply_to_id:
            response["reply_to_message_id"] = message.reply_to_id
        return response


class DiscordAdapter(PlatformAdapter):
    """Adapter for Discord webhooks/interactions."""

    def parse_request(self, payload: Dict[str, Any]) -> Optional[IncomingMessage]:
        """Parse Discord interaction or webhook payload."""
        # Handle interactions (slash commands, buttons, etc.)
        if payload.get("type") == 1:  # PING
            return None  # Will be handled separately

        # Handle message create events (from bot gateway)
        if "content" in payload and "author" in payload:
            author = payload.get("author", {})

            # Skip bot messages to prevent loops
            if author.get("bot", False):
                return None

            return IncomingMessage(
                platform="discord",
                sender_id=str(author.get("id", "")),
                sender_name=author.get("username", "Unknown"),
                channel_id=str(payload.get("channel_id", "")),
                message_text=payload.get("content", ""),
                raw_payload=payload,
                message_id=str(payload.get("id", "")),
            )

        return None

    def format_response(self, message: OutgoingMessage) -> Dict[str, Any]:
        """Format response for Discord."""
        return {
            "content": message.text,
        }

    def validate_signature(
        self,
        payload: bytes,
        signature: Optional[str],
        secret: Optional[str],
    ) -> bool:
        """Validate Discord signature (Ed25519)."""
        if not secret or not signature:
            return True  # Skip if no secret configured

        # Discord uses Ed25519 signatures - would need nacl library
        # For now, return True and recommend using secret token verification
        return True


class SlackAdapter(PlatformAdapter):
    """Adapter for Slack Events API webhooks."""

    def parse_request(self, payload: Dict[str, Any]) -> Optional[IncomingMessage]:
        """Parse Slack event payload."""
        # Handle URL verification challenge
        if payload.get("type") == "url_verification":
            return None  # Handled separately

        # Handle event callbacks
        event = payload.get("event", {})
        if event.get("type") != "message":
            return None

        # Skip bot messages and message subtypes (edits, deletes, etc.)
        if event.get("bot_id") or event.get("subtype"):
            return None

        return IncomingMessage(
            platform="slack",
            sender_id=event.get("user", ""),
            sender_name=event.get("user", "Unknown"),  # Would need API call to get name
            channel_id=event.get("channel", ""),
            message_text=event.get("text", ""),
            raw_payload=payload,
            message_id=event.get("ts", ""),
            reply_to_id=event.get("thread_ts"),
        )

    def format_response(self, message: OutgoingMessage) -> Dict[str, Any]:
        """Format response for Slack."""
        response = {
            "text": message.text,
        }
        if message.reply_to_id:
            response["thread_ts"] = message.reply_to_id
        return response

    def validate_signature(
        self,
        payload: bytes,
        signature: Optional[str],
        secret: Optional[str],
    ) -> bool:
        """Validate Slack request signature."""
        if not secret or not signature:
            return True

        # Slack uses HMAC-SHA256
        # Header format: v0=<signature>
        if not signature.startswith("v0="):
            return False

        expected = "v0=" + hmac.new(
            secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(signature, expected)


class GenericAdapter(PlatformAdapter):
    """Adapter for generic/custom webhook payloads."""

    def parse_request(self, payload: Dict[str, Any]) -> Optional[IncomingMessage]:
        """Parse generic webhook payload."""
        # Expect a specific format
        message_text = payload.get("message") or payload.get("text") or payload.get("content")
        if not message_text:
            return None

        return IncomingMessage(
            platform="generic",
            sender_id=payload.get("sender_id", payload.get("user_id", "unknown")),
            sender_name=payload.get("sender_name", payload.get("user_name", "Unknown")),
            channel_id=payload.get("channel_id", payload.get("chat_id", "default")),
            message_text=message_text,
            raw_payload=payload,
            message_id=payload.get("message_id"),
            context_messages=payload.get("context", []),
        )

    def format_response(self, message: OutgoingMessage) -> Dict[str, Any]:
        """Format generic response."""
        return {
            "response": message.text,
            "success": True,
        }


# Platform adapter registry
ADAPTERS: Dict[str, PlatformAdapter] = {
    "telegram": TelegramAdapter(),
    "discord": DiscordAdapter(),
    "slack": SlackAdapter(),
    "generic": GenericAdapter(),
}


# ============================================================================
# Webhook Router
# ============================================================================


def create_webhook_router(get_agent_fn, get_settings_fn) -> APIRouter:
    """
    Create the webhook router.

    Args:
        get_agent_fn: Function to get the agent instance
        get_settings_fn: Function to get settings

    Returns:
        Configured APIRouter
    """
    router = APIRouter(prefix="/webhook", tags=["Webhooks"])

    @router.post("/{platform}")
    async def handle_webhook(
        platform: str,
        request: Request,
        x_telegram_bot_api_secret_token: Optional[str] = Header(None),
        x_slack_signature: Optional[str] = Header(None),
        x_slack_request_timestamp: Optional[str] = Header(None),
        x_hub_signature_256: Optional[str] = Header(None),
    ):
        """
        Handle incoming webhook from messaging platform.

        Supported platforms:
        - telegram: Telegram Bot API webhooks
        - discord: Discord interactions/events
        - slack: Slack Events API
        - generic: Custom webhook format
        """
        settings = get_settings_fn()

        # Validate platform
        if platform not in ADAPTERS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported platform: {platform}. Supported: {list(ADAPTERS.keys())}",
            )

        adapter = ADAPTERS[platform]

        # Read raw body for signature validation
        body = await request.body()

        # Validate webhook signature if secret is configured
        signature = None
        if platform == "telegram":
            signature = x_telegram_bot_api_secret_token
        elif platform == "slack":
            signature = x_slack_signature
        elif platform == "discord":
            signature = x_hub_signature_256

        if settings.webhook_secret and not adapter.validate_signature(
            body, signature, settings.webhook_secret
        ):
            logger.warning(f"Invalid webhook signature from {platform}")
            raise HTTPException(status_code=401, detail="Invalid signature")

        # Parse JSON payload
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        # Handle Slack URL verification challenge
        if platform == "slack" and payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}

        # Handle Discord PING
        if platform == "discord" and payload.get("type") == 1:
            return {"type": 1}

        # Parse message
        incoming = adapter.parse_request(payload)
        if not incoming:
            # Not a message we need to respond to
            return WebhookResponse(success=True, message="Ignored")

        logger.info(
            f"Webhook received from {platform}: "
            f"sender={incoming.sender_name}, channel={incoming.channel_id}"
        )

        # Generate thread ID from platform + channel for conversation continuity
        thread_id = f"{platform}_{incoming.channel_id}"
        user_id = f"{platform}_{incoming.sender_id}"

        try:
            # Get agent and process message
            agent = get_agent_fn()
            response_text = agent.chat(
                incoming.message_text,
                thread_id=thread_id,
                user_id=user_id,
            )

            # Format response for platform
            outgoing = OutgoingMessage(
                text=response_text,
                reply_to_id=incoming.message_id,
            )
            formatted_response = adapter.format_response(outgoing)

            return WebhookResponse(
                success=True,
                message="Processed",
                data=formatted_response,
            )

        except Exception as e:
            logger.error(f"Error processing webhook: {e}", exc_info=True)
            return WebhookResponse(
                success=False,
                message=f"Error: {str(e)}",
            )

    @router.get("/health")
    async def webhook_health():
        """Health check for webhook endpoint."""
        return {"status": "ok", "platforms": list(ADAPTERS.keys())}

    return router
