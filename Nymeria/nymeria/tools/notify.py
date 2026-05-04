"""Unified notification tool for sending messages to external platforms.

Allows Nymeria to proactively send notifications to users via Telegram,
Discord, or Slack. Useful for autonomous task completion notifications,
reminders, and alerts.
"""

import logging
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..config import get_settings
from ..core.notification_dispatch import (
    create_in_app_notification,
    get_in_app_notification_level,
    send_discord,
    send_slack,
    send_teams,
    send_telegram,
)
from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)


def _thread_in_app_notification_level(thread_id: str) -> str:
    """Return this thread's in-app notification mode.

    Thin wrapper that resolves the ``ThreadConfigManager`` from the global
    agent so the dispatch module's pure helper can be called.
    """
    if not thread_id:
        return "notify_only"
    try:
        from ..core.agent import get_current_agent

        agent = get_current_agent()
        if agent is None:
            return "notify_only"
        return get_in_app_notification_level(thread_id, agent.thread_config_manager)
    except Exception as e:
        logger.debug("Could not read in-app notification level for %s: %s", thread_id, e)
        return "notify_only"


def _create_in_app(message: str, user_id: str, thread_id: str) -> str:
    """Create an unread notification-center item for the caller."""
    level = _thread_in_app_notification_level(thread_id)
    result = create_in_app_notification(message, user_id, thread_id, in_app_level=level)
    return result or "Desktop error: unknown"


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
    logger.info("notify called: platform=%s, message=%s...", platform, message[:50])
    settings = get_settings()
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)

    handlers = {
        "desktop": lambda msg, st: _create_in_app(msg, user_id, thread_id),
        "telegram": lambda msg, st: send_telegram(msg, st, user_id, thread_id),
        "discord": send_discord,
        "slack": send_slack,
        "teams": send_teams,
    }

    if platform != "auto":
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
                logger.debug("Activity logging failed for notification")
            return f"[Success]: {result}"
        return f"[Error]: {result}"

    # Auto mode: try all configured platforms
    results = []
    errors = []

    for name, handler in handlers.items():
        result = handler(message, settings)
        if result is None:
            continue
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
            logger.debug("Activity logging failed for notification")
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
