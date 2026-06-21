"""Notification dispatch orchestration.

Reads per-thread notification settings, creates in-app notification-center
rows, and routes outbound delivery through the channel-type registry in
:mod:`nymeria.core.notification_channels` (profiles + destinations). The only
direct platform sender kept here is the Telegram default-chat helper, which
``TelegramChannel`` reuses for its default-chat fallback.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, List, Optional

import httpx

if TYPE_CHECKING:
    from .notification_channels import DispatchResult
    from .thread_config import ThreadConfigManager

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 30.0


# ── Per-thread notification settings ─────────────────────────────────────────


def get_in_app_notification_level(
    thread_id: str,
    thread_config_manager: "ThreadConfigManager",
) -> str:
    """Return the ``in_app_notification_level`` for *thread_id*.

    Returns ``"notify_only"`` (the default) when the thread has no config or
    when an error occurs.
    """
    if not thread_id:
        return "notify_only"
    try:
        tc = thread_config_manager.get_config(thread_id)
        if tc is None:
            return "notify_only"
        return getattr(tc, "in_app_notification_level", "notify_only") or "notify_only"
    except Exception as e:
        logger.debug("Could not read notification level for %s: %s", thread_id, e)
        return "notify_only"


def should_notify_autonomous(
    thread_id: str,
    thread_config_manager: "ThreadConfigManager",
) -> bool:
    """Whether autonomous completions should create in-app + FCM notifications."""
    return get_in_app_notification_level(thread_id, thread_config_manager) == "all_autonomous"


# ── Telegram default-chat sender ─────────────────────────────────────────────
#
# The one direct-HTTP sender kept outside the channel registry: TelegramChannel
# reuses it for the default-chat fallback. Every other platform delivery goes
# through the channel registry in notification_channels.py.


def send_telegram_default(message: str, settings) -> Optional[str]:
    """Send via the configured default Telegram chat (direct HTTP, no bot)."""
    bot_token = settings.telegram_bot_token
    if not bot_token:
        return None
    chat_id = settings.telegram_default_chat_id
    if not chat_id:
        return None

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message}
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            response = client.post(url, json=payload)
        if response.status_code == 200:
            result = response.json()
            if result.get("ok"):
                message_id = result.get("result", {}).get("message_id")
                logger.info("Telegram notification sent: message_id=%s", message_id)
                return f"Sent to Telegram (message_id: {message_id})"
            return f"Telegram API error: {result.get('description', 'Unknown')}"
        return f"Telegram HTTP error: {response.status_code}"
    except Exception as e:
        logger.error("Telegram notification failed: %s", e)
        return f"Telegram error: {e}"


def thread_has_telegram_route(thread_id: str) -> bool:
    """Return True if *thread_id* can be delivered through the Telegram bot."""
    if not thread_id:
        return False
    if thread_id.startswith("telegram_"):
        return True
    try:
        from .agent import get_current_agent

        agent = get_current_agent()
        if agent is None:
            return False
        return agent.chat_bindings_repo.lookup_thread_binding_by_thread("telegram", thread_id) is not None
    except Exception as e:
        logger.debug("Could not resolve Telegram route for %s: %s", thread_id, e)
        return False


def publish_telegram_thread_notification(
    message: str, user_id: str, thread_id: str,
) -> Optional[str]:
    """Queue a ``notification`` autonomous event for a Telegram-bound thread."""
    if not thread_has_telegram_route(thread_id):
        return None
    try:
        from .event_bus import publish_autonomous_event

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
        return f"Telegram thread error: {e}"


# ── Composite dispatch helpers ───────────────────────────────────────────────


def create_in_app_notification(
    message: str,
    user_id: str,
    thread_id: str,
    *,
    task_id: Optional[str] = None,
    in_app_level: Optional[str] = None,
    profile: Optional[str] = None,
    attempted: Optional[List[str]] = None,
    delivered_to: Optional[List[str]] = None,
    errors: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Create an unread notification-center item and publish an ``in_app_only`` event.

    *in_app_level* may be passed to avoid re-reading thread config when the
    caller already knows it. If ``"off"``, the call is skipped.

    The ``profile`` / ``attempted`` / ``delivered_to`` / ``errors`` fields are
    audit-log metadata: when the notify tool dispatches through a profile,
    this is how the frontend shows badges like "sent to telegram, email" on
    the in-app row even though only the in-app channel goes through this
    helper.
    """
    if in_app_level == "off":
        return "Skipped Desktop notification (disabled for thread)"
    try:
        from .notifications import create_notification

        notification = create_notification(
            user_id=user_id,
            summary=message[:200],
            thread_id=thread_id if thread_id and thread_id != "default" else None,
            task_id=task_id,
            profile=profile,
            attempted=attempted,
            delivered_to=delivered_to,
            errors=errors,
        )
        try:
            from .event_bus import publish_autonomous_event

            publish_autonomous_event(
                event_type="notification",
                thread_id=thread_id if thread_id else "default",
                user_id=user_id,
                task_id=task_id or "",
                data={"summary": message[:200], "in_app_only": True},
            )
        except Exception:
            logger.warning("Failed to publish notification event to event bus", exc_info=True)
        logger.info("In-app notification created: id=%s user=%s", notification.id, user_id)
        return f"Sent to Desktop (notification_id: {notification.id})"
    except Exception as e:
        logger.error("In-app notification failed: %s", e)
        return f"Desktop error: {e}"


def send_via_profile(
    *,
    message: str,
    user_id: str,
    thread_id: str,
    task_id: Optional[str] = None,
    profile_name: Optional[str] = None,
    thread_default_profile: Optional[str] = None,
    user_default_profile: Optional[str] = None,
    in_app_level: Optional[str] = None,
    settings=None,
) -> "DispatchResult":
    """Resolve a notification profile and dispatch to every destination it
    references, then write a single in-app audit-log row capturing the
    outcome.

    Resolution order: explicit ``profile_name`` arg > per-thread default >
    per-user default > built-in ``"default"`` profile (auto-seeded from env
    config on first use).
    """
    from ..config import get_settings
    from .notification_channels import (
        SendContext,
        dispatch_to_profile,
        ensure_seeded_destinations,
        resolve_profile_name,
    )
    from .notification_destinations import get_destinations_repo

    settings = settings or get_settings()
    repo = get_destinations_repo()
    ensure_seeded_destinations(user_id=user_id, settings=settings, repo=repo)

    chosen_profile = resolve_profile_name(
        user_id=user_id,
        repo=repo,
        explicit=profile_name,
        thread_default=thread_default_profile,
        user_default=user_default_profile,
    )

    ctx = SendContext(
        user_id=user_id,
        thread_id=thread_id or "",
        settings=settings,
        task_id=task_id or "",
    )
    result = dispatch_to_profile(
        message=message,
        profile_name=chosen_profile,
        ctx=ctx,
        repo=repo,
    )

    if in_app_level != "off":
        create_in_app_notification(
            message,
            user_id=user_id,
            thread_id=thread_id,
            task_id=task_id,
            in_app_level=in_app_level,
            profile=chosen_profile,
            attempted=result.attempted,
            delivered_to=result.delivered_to,
            errors=result.errors,
        )

    return result


def create_autonomous_notification(
    *,
    user_id: str,
    thread_id: str,
    task_id: Optional[str] = None,
    summary: str,
    settings,
    thread_config_manager: "ThreadConfigManager",
) -> None:
    """Create in-app notification + FCM push for an autonomous completion.

    Called by the ticker and API chat endpoint.  Checks
    ``in_app_notification_level`` before creating; always attempts FCM if
    enabled and there is content to push.
    """
    if not should_notify_autonomous(thread_id, thread_config_manager):
        return

    try:
        from .notifications import create_notification

        create_notification(
            user_id=user_id,
            summary=summary[:200],
            thread_id=thread_id,
            task_id=task_id,
        )
    except Exception as e:
        logger.error("Failed to create autonomous notification: %s", e)

    if summary and settings.fcm_enabled:
        try:
            from .fcm import send_to_all_devices

            send_to_all_devices(
                data_dir=str(settings.data_dir),
                text=summary,
                thread_id=thread_id,
                task_id=task_id or "",
                summary="",
                user_id=user_id,
            )
        except Exception as e:
            logger.warning("FCM push failed during autonomous notification: %s", e)


def send_external_notifications(
    message: str,
    settings,
    *,
    user_id: str = "default",
    thread_id: str = "",
) -> List[str]:
    """Deliver to the external destinations in the user's ``default``
    notification profile (the same channel-registry path the ``notify`` tool
    uses).

    Unlike :func:`send_via_profile`, this does NOT write an in-app notification
    row: the watchdog (the only caller) wants external delivery only. Returns
    one ``"Sent to <destination>"`` string per delivered destination so the
    caller can log them; per-destination failures are isolated inside
    :func:`dispatch_to_profile` and simply omitted from the result.
    """
    from .notification_channels import (
        SendContext,
        dispatch_to_profile,
        ensure_seeded_destinations,
        resolve_profile_name,
    )
    from .notification_destinations import get_destinations_repo

    try:
        repo = get_destinations_repo()
        ensure_seeded_destinations(user_id=user_id, settings=settings, repo=repo)
        profile_name = resolve_profile_name(user_id=user_id, repo=repo)
        ctx = SendContext(
            user_id=user_id, thread_id=thread_id or "", settings=settings,
        )
        result = dispatch_to_profile(
            message=message, profile_name=profile_name, ctx=ctx, repo=repo,
        )
    except Exception as e:
        logger.debug("External notifications via profile failed: %s", e)
        return []

    results: List[str] = []
    for name in result.delivered_to:
        logger.info("External notification delivered via destination %s", name)
        results.append(f"Sent to {name}")
    return results
