"""User-configurable notification dispatch tool.

The agent calls ``notify(message)`` and Nymeria routes the message through
the user's currently-configured notification *profile* -- a named bundle of
destinations (Telegram chat, email, webhook, FCM push, etc.). The user
configures destinations and profiles in settings; the tool itself never
sees the underlying delivery types, so adding new ones doesn't grow the
tool schema.

Resolution order for which profile is used:
1. ``profile`` arg on the call (per-call override).
2. ``ThreadConfig.notification_profile`` (per-thread override).
3. ``UserProfile.preferences['notifications']['default_profile']``
   (user-level default).
4. The built-in ``"default"`` profile, auto-seeded from any existing global
   webhook env config (TELEGRAM_BOT_TOKEN, DISCORD_WEBHOOK_URL, etc.).

Every successful or attempted call also writes an in-app audit-log row
visible in the notifications panel -- regardless of which external channels
received the message -- so the user has a single feed of everything Nymeria
has notified them about. Pre-existing ``platform=...`` callers (kept for
backward compatibility with the old enum and with trigger configs in the
wild) are mapped onto the new model with a deprecation log line.
"""

import logging
from typing import Annotated, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from ..config import get_settings
from ..core.notification_dispatch import (
    get_in_app_notification_level,
    send_via_profile,
)
from .utils import get_thread_id, get_user_id

logger = logging.getLogger(__name__)


# Map the legacy ``platform`` arg to its auto-seeded destination name so
# existing trigger configs and old prompts keep working without edits.
_LEGACY_PLATFORM_TO_DESTINATION = {
    "telegram": "telegram-default",
    "discord": "discord-default",
    "slack": "slack-default",
    "teams": "teams-default",
}


def _resolve_thread_default_profile(thread_id: str) -> Optional[str]:
    """Read the per-thread ``notification_profile`` override, if any."""
    if not thread_id:
        return None
    try:
        from ..core.agent import get_current_agent

        agent = get_current_agent()
        if agent is None:
            return None
        tc = agent.thread_config_manager.get_config(thread_id)
        if tc is None:
            return None
        return getattr(tc, "notification_profile", None) or None
    except Exception as exc:
        logger.debug("notify: thread profile resolution failed for %s: %s", thread_id, exc)
        return None


def _resolve_user_default_profile(user_id: str) -> Optional[str]:
    """Read the user-level ``default_profile`` notification preference."""
    if not user_id:
        return None
    try:
        from ..core.agent import get_current_agent

        agent = get_current_agent()
        if agent is None:
            return None
        profile_manager = getattr(agent, "user_profile_manager", None) or getattr(
            agent, "profile_manager", None,
        )
        if profile_manager is None:
            return None
        prof = profile_manager.get_profile(user_id)
        prefs = prof.get_notification_preferences()
        return prefs.get("default_profile")
    except Exception as exc:
        logger.debug("notify: user profile resolution failed for %s: %s", user_id, exc)
        return None


def _resolve_thread_in_app_level(thread_id: str) -> str:
    if not thread_id:
        return "notify_only"
    try:
        from ..core.agent import get_current_agent

        agent = get_current_agent()
        if agent is None:
            return "notify_only"
        return get_in_app_notification_level(thread_id, agent.thread_config_manager)
    except Exception as exc:
        logger.debug("notify: in-app level lookup failed for %s: %s", thread_id, exc)
        return "notify_only"


def _format_result(result, profile_name: str) -> str:
    """Render the dispatch outcome as a single status line for the tool log."""
    if result.delivered_to:
        delivered = ", ".join(result.delivered_to)
        if result.errors:
            err = "; ".join(f"{k}: {v}" for k, v in result.errors.items())
            return f"[Partial]: delivered to {delivered}; errors {err}"
        return f"[Success]: delivered to {delivered}"
    if result.errors:
        err = "; ".join(f"{k}: {v}" for k, v in result.errors.items())
        return f"[Error]: profile '{profile_name}' delivered nothing - {err}"
    return (
        f"[Logged]: notification recorded in the in-app feed; profile "
        f"'{profile_name}' has no destinations configured. Use "
        "/notifications settings or notification_destination_add to add one."
    )


@tool
def notify(
    message: str,
    profile: Optional[str] = None,
    platform: Optional[str] = None,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg] = None,
) -> str:
    """
    Send a notification to the user through their configured channels.

    The user maintains named "profiles" that bundle one or more delivery
    destinations (Telegram, email, webhook, push, etc.). Omit ``profile`` to
    route through whichever profile the user has set as the default for
    this thread (or globally). Override with a profile name for one-off
    routing changes ("urgent", "quiet", etc.).

    Every call also creates a row in the in-app notifications feed so the
    user has an audit trail regardless of which external channels were
    used.

    Args:
        message: The message text to send.
        profile: Optional profile name to override the default. Ask the user
            which profiles they have configured (or list them via the
            notification setup tools) if you're unsure.

    Returns:
        Status string describing which destinations succeeded or failed.
    """
    user_id = get_user_id(config)
    thread_id = get_thread_id(config)
    settings = get_settings()

    # Backward compatibility: log a hint for old callers passing ``platform``.
    if platform and not profile:
        if platform in _LEGACY_PLATFORM_TO_DESTINATION:
            logger.info(
                "notify: legacy platform=%s mapped to destination %s",
                platform,
                _LEGACY_PLATFORM_TO_DESTINATION[platform],
            )

    in_app_level = _resolve_thread_in_app_level(thread_id)
    thread_default = _resolve_thread_default_profile(thread_id)
    user_default = _resolve_user_default_profile(user_id)

    logger.info(
        "notify called: user=%s thread=%s profile=%s thread_default=%s user_default=%s msg=%s...",
        user_id, thread_id, profile, thread_default, user_default, message[:50],
    )

    result = send_via_profile(
        message=message,
        user_id=user_id,
        thread_id=thread_id,
        profile_name=profile,
        thread_default_profile=thread_default,
        user_default_profile=user_default,
        in_app_level=in_app_level,
        settings=settings,
    )

    if result.delivered_to:
        try:
            from ..core.activity_log import ActivityType, log_activity

            log_activity(
                ActivityType.NOTIFICATION_SENT,
                f"Notification sent via profile '{result.profile_name}' to: "
                + ", ".join(result.delivered_to),
                user_id=user_id,
                thread_id=thread_id if thread_id != "default" else None,
                metadata={
                    "profile": result.profile_name,
                    "delivered_to": result.delivered_to,
                    "errors": result.errors,
                },
            )
        except Exception:
            logger.debug("Activity logging failed for notification")

    return _format_result(result, result.profile_name)


# Single unified tool. Exported list kept for tools/__init__.py registration.
NOTIFY_TOOLS = [notify]
