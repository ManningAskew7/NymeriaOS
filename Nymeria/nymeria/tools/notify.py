"""Unified notification tool for sending messages to external platforms.

Allows Nymeria to proactively send notifications to users via Telegram,
Discord, or Slack. Useful for autonomous task completion notifications,
reminders, and alerts.
"""

import logging
from typing import Annotated, Literal, Optional

import httpx
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..config import get_settings
from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)

# Timeout for HTTP requests
HTTP_TIMEOUT = 30.0


def _send_telegram_default(message: str, settings) -> str:
    """Send notification via the configured default Telegram chat."""
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


def _thread_has_telegram_route(thread_id: str) -> bool:
    """Return True if the current thread can be delivered through the Telegram bot."""
    if not thread_id:
        return False
    if thread_id.startswith("telegram_"):
        return True
    try:
        from ..core.agent import get_current_agent

        agent = get_current_agent()
        if agent is None:
            return False
        return agent.accounts_repo.lookup_thread_binding_by_thread("telegram", thread_id) is not None
    except Exception as e:
        logger.debug("Could not resolve Telegram route for %s: %s", thread_id, e)
        return False


def _publish_telegram_thread_notification(message: str, user_id: str, thread_id: str) -> Optional[str]:
    """Queue a notification event for a Telegram-bound thread."""
    if not _thread_has_telegram_route(thread_id):
        return None
    try:
        from ..core.event_bus import publish_autonomous_event

        publish_autonomous_event(
            event_type="notification",
            thread_id=thread_id,
            user_id=user_id,
            task_id="",
            data={"message": message, "summary": message[:200]},
        )
        logger.info("Telegram thread notification queued for thread=%s", thread_id)
        return "Queued to Telegram thread"
    except Exception as e:
        logger.error("Telegram thread notification failed: %s", e)
        return f"Telegram thread error: {str(e)}"


def _send_telegram(message: str, settings, user_id: str = "default", thread_id: str = "") -> str:
    """Send notification via the current Telegram thread if possible, else default chat."""
    routed = _publish_telegram_thread_notification(message, user_id, thread_id)
    if routed is not None:
        return routed
    return _send_telegram_default(message, settings)


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


def _send_teams(message: str, settings) -> str:
    """Send notification to Microsoft Teams channel via Graph API.

    Uses the same Outlook OAuth token (requires ChannelMessage.Send scope).
    """
    team_id = settings.teams_team_id
    channel_id = settings.teams_channel_id
    if not team_id or not channel_id:
        return None  # Not configured

    # Get access token from Outlook auth — use the dedicated Teams account if configured
    try:
        from .outlook_email import get_access_token, GRAPH_BASE
    except ImportError:
        return None

    teams_account = settings.teams_account_id
    token = get_access_token(teams_account)
    if not token:
        return "Teams error: No authenticated Microsoft account"

    url = f"{GRAPH_BASE}/teams/{team_id}/channels/{channel_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "body": {
            "contentType": "text",
            "content": message,
        }
    }

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            response = client.post(url, headers=headers, json=payload)

        if response.status_code == 201:
            msg_id = response.json().get("id", "")[:20]
            logger.info(f"Teams notification sent: {msg_id}")
            return f"Sent to Teams"
        try:
            err = response.json().get("error", {}).get("message", response.text[:200])
        except Exception:
            err = response.text[:200]
        return f"Teams HTTP error {response.status_code}: {err}"
    except Exception as e:
        logger.error(f"Teams notification failed: {e}")
        return f"Teams error: {str(e)}"


def _thread_in_app_notification_level(thread_id: str) -> str:
    """Return this thread's in-app notification mode."""
    if not thread_id:
        return "notify_only"
    try:
        from ..core.agent import get_current_agent

        agent = get_current_agent()
        if agent is None:
            return "notify_only"
        tc = agent.thread_config_manager.get_config(thread_id)
        if tc is None:
            return "notify_only"
        return getattr(tc, "in_app_notification_level", "notify_only") or "notify_only"
    except Exception as e:
        logger.debug("Could not read in-app notification level for %s: %s", thread_id, e)
        return "notify_only"


def _create_in_app_notification(message: str, user_id: str, thread_id: str) -> str:
    """Create an unread notification-center item for the caller."""
    if _thread_in_app_notification_level(thread_id) == "off":
        return "Skipped Desktop notification (disabled for thread)"
    try:
        from ..core.notifications import create_notification

        notification = create_notification(
            user_id=user_id,
            summary=message[:200],
            thread_id=thread_id if thread_id and thread_id != "default" else None,
        )
        try:
            from ..core.event_bus import publish_autonomous_event

            publish_autonomous_event(
                event_type="notification",
                thread_id=thread_id if thread_id else "default",
                user_id=user_id,
                task_id="",
                data={"summary": message[:200], "in_app_only": True},
            )
        except Exception:
            pass
        logger.info("In-app notification created: id=%s user=%s", notification.id, user_id)
        return f"Sent to Desktop (notification_id: {notification.id})"
    except Exception as e:
        logger.error("In-app notification failed: %s", e)
        return f"Desktop error: {str(e)}"


@tool
def notify(
    message: str,
    platform: Literal["auto", "desktop", "telegram", "discord", "slack", "teams"] = "auto",
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Send a notification to the user via messaging platform.

    Use this to notify the user about completed tasks, reminders, or important
    updates when they might not be actively watching the desktop app.

    Args:
        message: The message text to send.
        platform: Target platform. "auto" tries all configured platforms.
                  Options: "auto", "desktop", "telegram", "discord", "slack", "teams"

    Returns:
        Success message or error description.
    """
    logger.info(f"notify called: platform={platform}, message={message[:50]}...")
    settings = get_settings()
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)

    # Platform-specific handlers
    handlers = {
        "desktop": lambda msg, st: _create_in_app_notification(msg, user_id, thread_id),
        "telegram": lambda msg, st: _send_telegram(msg, st, user_id, thread_id),
        "discord": _send_discord,
        "slack": _send_slack,
        "teams": _send_teams,
    }

    if platform != "auto":
        # Specific platform requested
        handler = handlers.get(platform)
        if not handler:
            return f"[Error]: Unknown platform '{platform}'. Use: desktop, telegram, discord, slack, teams, or auto."

        result = handler(message, settings)
        if result is None:
            return f"[Error]: {platform.title()} not configured. Set credentials in .env file."
        if result.startswith("Sent") or result.startswith("Queued") or result.startswith("Skipped"):
            try:
                from ..core.activity_log import ActivityType, log_activity
                log_activity(
                    ActivityType.NOTIFICATION_SENT,
                    f"Notification sent: {result}",
                    user_id=user_id,
                    thread_id=thread_id if thread_id != "default" else None,
                    metadata={"platforms": [platform]},
                )
            except Exception:
                pass
            return f"[Success]: {result}"
        return f"[Error]: {result}"

    # Auto mode: try all configured platforms
    results = []
    errors = []

    for name, handler in handlers.items():
        result = handler(message, settings)
        if result is None:
            continue  # Not configured, skip
        if result.startswith("Skipped"):
            continue
        if result.startswith("Sent") or result.startswith("Queued"):
            results.append(result)
        else:
            errors.append(f"{name}: {result}")

    if results:
        try:
            from ..core.activity_log import ActivityType, log_activity
            log_activity(
                ActivityType.NOTIFICATION_SENT,
                f"Notification sent: {'; '.join(results)}",
                user_id=user_id,
                thread_id=thread_id if thread_id != "default" else None,
                metadata={"platforms": [r.split("to ")[-1] for r in results]},
            )
        except Exception:
            pass
        return f"[Success]: {'; '.join(results)}"
    elif errors:
        return f"[Error]: All platforms failed - {'; '.join(errors)}"
    else:
        return (
            "[Error]: No notification platforms configured. "
            "Use platform='desktop' for in-app notifications or set TELEGRAM_BOT_TOKEN, "
            "DISCORD_WEBHOOK_URL, SLACK_WEBHOOK_URL, or TEAMS_TEAM_ID + TEAMS_CHANNEL_ID in .env"
        )


# Single unified tool
NOTIFY_TOOLS = [notify]
