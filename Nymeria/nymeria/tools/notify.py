"""Unified notification tool for sending messages to external platforms.

Allows Nymeria to proactively send notifications to users via Telegram,
Discord, or Slack. Useful for autonomous task completion notifications,
reminders, and alerts.
"""

import logging
from typing import Literal, Optional

import httpx
from langchain_core.tools import tool

from ..config import get_settings

logger = logging.getLogger(__name__)

# Timeout for HTTP requests
HTTP_TIMEOUT = 30.0


def _send_telegram(message: str, settings) -> str:
    """Send notification via Telegram."""
    bot_token = settings.telegram_bot_token
    if not bot_token:
        return None  # Not configured

    chat_id = settings.telegram_default_chat_id
    if not chat_id:
        return None  # Not configured

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            response = client.post(url, json=payload)

        if response.status_code == 200:
            result = response.json()
            if result.get("ok"):
                message_id = result.get("result", {}).get("message_id")
                logger.info(f"Telegram notification sent: message_id={message_id}")
                return f"Sent to Telegram (message_id: {message_id})"
            return f"Telegram API error: {result.get('description', 'Unknown')}"
        return f"Telegram HTTP error: {response.status_code}"
    except Exception as e:
        logger.error(f"Telegram notification failed: {e}")
        return f"Telegram error: {str(e)}"


def _send_discord(message: str, settings) -> str:
    """Send notification via Discord webhook."""
    webhook_url = settings.discord_webhook_url
    if not webhook_url:
        return None  # Not configured

    payload = {"content": message, "username": "Nymeria"}

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            response = client.post(webhook_url, json=payload)

        if response.status_code in (200, 204):
            logger.info("Discord notification sent")
            return "Sent to Discord"
        return f"Discord HTTP error: {response.status_code}"
    except Exception as e:
        logger.error(f"Discord notification failed: {e}")
        return f"Discord error: {str(e)}"


def _send_slack(message: str, settings) -> str:
    """Send notification via Slack webhook."""
    webhook_url = settings.slack_webhook_url
    if not webhook_url:
        return None  # Not configured

    payload = {"text": message, "username": "Nymeria"}

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            response = client.post(webhook_url, json=payload)

        if response.status_code == 200 and response.text == "ok":
            logger.info("Slack notification sent")
            return "Sent to Slack"
        return f"Slack error: {response.text[:100]}"
    except Exception as e:
        logger.error(f"Slack notification failed: {e}")
        return f"Slack error: {str(e)}"


@tool
def notify(
    message: str,
    platform: Literal["auto", "telegram", "discord", "slack"] = "auto",
) -> str:
    """
    Send a notification to the user via messaging platform.

    Use this to notify the user about completed tasks, reminders, or important
    updates when they might not be actively watching the desktop app.

    Args:
        message: The message text to send.
        platform: Target platform. "auto" tries all configured platforms.
                  Options: "auto", "telegram", "discord", "slack"

    Returns:
        Success message or error description.
    """
    logger.info(f"notify called: platform={platform}, message={message[:50]}...")
    settings = get_settings()

    # Platform-specific handlers
    handlers = {
        "telegram": _send_telegram,
        "discord": _send_discord,
        "slack": _send_slack,
    }

    if platform != "auto":
        # Specific platform requested
        handler = handlers.get(platform)
        if not handler:
            return f"[Error]: Unknown platform '{platform}'. Use: telegram, discord, slack, or auto."

        result = handler(message, settings)
        if result is None:
            return f"[Error]: {platform.title()} not configured. Set credentials in .env file."
        if result.startswith("Sent"):
            return f"[Success]: {result}"
        return f"[Error]: {result}"

    # Auto mode: try all configured platforms
    results = []
    errors = []

    for name, handler in handlers.items():
        result = handler(message, settings)
        if result is None:
            continue  # Not configured, skip
        if result.startswith("Sent"):
            results.append(result)
        else:
            errors.append(f"{name}: {result}")

    if results:
        return f"[Success]: {'; '.join(results)}"
    elif errors:
        return f"[Error]: All platforms failed - {'; '.join(errors)}"
    else:
        return (
            "[Error]: No notification platforms configured. "
            "Set TELEGRAM_BOT_TOKEN, DISCORD_WEBHOOK_URL, or SLACK_WEBHOOK_URL in .env"
        )


# Single unified tool
NOTIFY_TOOLS = [notify]
