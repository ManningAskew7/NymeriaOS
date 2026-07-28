"""Notification system for Nymeria.

Provides in-app notification storage and retrieval for urgent
messages from autonomous agent executions.
"""

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .keyed_locks import KeyedRLockMap
from .storage_paths import safe_path_segment, write_text_atomic
from .time_utils import utc_now

logger = logging.getLogger(__name__)

# Thread-safe locks for notification operations (keyed by user_id)
_notification_locks = KeyedRLockMap()


class Notification(BaseModel):
    """A single notification entry.

    Doubles as the in-app audit log for every ``notify`` tool call: even when
    the message is delivered to external destinations only (Telegram, email,
    webhook, etc.), a row is created here so the user has a single feed of
    everything Nymeria has notified them about. ``attempted`` / ``delivered_to``
    / ``errors`` record where the agent tried to send it and what happened.
    """

    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    user_id: str
    summary: str = Field(..., max_length=200)
    thread_id: Optional[str] = None
    task_id: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    read: bool = False
    profile: Optional[str] = Field(
        default=None,
        description="Notification profile that was used (None for autonomous/system notifications)",
    )
    attempted: List[str] = Field(
        default_factory=list,
        description="Destination names that were attempted",
    )
    delivered_to: List[str] = Field(
        default_factory=list,
        description="Destination names that successfully received the message",
    )
    errors: Dict[str, str] = Field(
        default_factory=dict,
        description="Per-destination error details for attempts that failed",
    )


class NotificationStore:
    """
    JSON-based notification storage.

    Stores notifications in data/notifications/{user_id}.json
    with automatic retention of unread notifications.
    """

    # In-app feed doubles as an audit log of every notify call, so the cap
    # is higher than a typical "unread bell" UI. Unread rows are always
    # preserved; once over the cap, the oldest read rows are evicted first.
    MAX_NOTIFICATIONS = 200

    def __init__(self, data_dir: Path):
        """
        Initialize the notification store.

        Args:
            data_dir: Base data directory (notifications stored in data_dir/notifications/)
        """
        self.notifications_dir = data_dir / "notifications"
        self.notifications_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"NotificationStore initialized with directory: {self.notifications_dir}")

    def _get_lock(self, user_id: str) -> threading.RLock:
        """Get or create a lock for a specific user."""
        return _notification_locks.get(user_id)

    def _get_notifications_path(self, user_id: str) -> Path:
        """Get the path to a user's notifications file."""
        safe_user_id = safe_path_segment(user_id)
        return self.notifications_dir / f"{safe_user_id}.json"

    def create(
        self,
        user_id: str,
        summary: str,
        thread_id: Optional[str] = None,
        task_id: Optional[str] = None,
        *,
        profile: Optional[str] = None,
        attempted: Optional[List[str]] = None,
        delivered_to: Optional[List[str]] = None,
        errors: Optional[Dict[str, str]] = None,
    ) -> Notification:
        """
        Create a new notification.

        Args:
            user_id: User to notify
            summary: Short notification text
            thread_id: Optional thread to navigate to
            task_id: Optional originating task ID
            profile: Notification profile that routed this notification
            attempted: Destination names that were tried
            delivered_to: Destination names that succeeded
            errors: Per-destination error details

        Returns:
            The created Notification
        """
        notification = Notification(
            user_id=user_id,
            summary=summary[:200],  # Truncate if too long
            thread_id=thread_id,
            task_id=task_id,
            profile=profile,
            attempted=list(attempted or []),
            delivered_to=list(delivered_to or []),
            errors=dict(errors or {}),
        )

        lock = self._get_lock(user_id)
        with lock:
            notifications = self._load_notifications(user_id)
            notifications.append(notification)

            # Keep only the last MAX_NOTIFICATIONS
            if len(notifications) > self.MAX_NOTIFICATIONS:
                # Keep unread ones and most recent read ones
                unread = [n for n in notifications if not n.read]
                read = [n for n in notifications if n.read]
                # Sort read by created_at descending
                read.sort(key=lambda n: n.created_at, reverse=True)
                # Keep all unread + fill remaining with most recent read
                remaining_slots = self.MAX_NOTIFICATIONS - len(unread)
                notifications = unread + read[:max(0, remaining_slots)]

            self._save_notifications(user_id, notifications)

        logger.info(f"Notification created for {user_id}: {summary[:50]}...")
        return notification

    def get_unread(self, user_id: str) -> List[Notification]:
        """
        Get all unread notifications for a user.

        Args:
            user_id: User identifier

        Returns:
            List of unread Notification objects, newest first
        """
        lock = self._get_lock(user_id)
        with lock:
            notifications = self._load_notifications(user_id)

        unread = [n for n in notifications if not n.read]
        # Sort by created_at descending (newest first)
        unread.sort(key=lambda n: n.created_at, reverse=True)
        return unread

    def get_all(self, user_id: str, limit: int = 50) -> List[Notification]:
        """
        Get all notifications for a user.

        Args:
            user_id: User identifier
            limit: Maximum number to return

        Returns:
            List of Notification objects, newest first
        """
        lock = self._get_lock(user_id)
        with lock:
            notifications = self._load_notifications(user_id)

        # Sort by created_at descending (newest first)
        notifications.sort(key=lambda n: n.created_at, reverse=True)
        return notifications[:limit]

    def mark_read(self, notification_id: str, user_id: str = "default") -> bool:
        """
        Mark a notification as read.

        Args:
            notification_id: ID of notification to mark
            user_id: User identifier (for finding the right file)

        Returns:
            True if notification was found and marked, False otherwise
        """
        lock = self._get_lock(user_id)
        with lock:
            notifications = self._load_notifications(user_id)

            found = False
            for n in notifications:
                if n.id == notification_id:
                    n.read = True
                    found = True
                    break

            if found:
                self._save_notifications(user_id, notifications)
                logger.debug(f"Notification {notification_id} marked as read")

            return found

    def mark_all_read(self, user_id: str) -> int:
        """
        Mark all notifications as read for a user.

        Args:
            user_id: User identifier

        Returns:
            Number of notifications marked as read
        """
        lock = self._get_lock(user_id)
        with lock:
            notifications = self._load_notifications(user_id)

            count = 0
            for n in notifications:
                if not n.read:
                    n.read = True
                    count += 1

            if count > 0:
                self._save_notifications(user_id, notifications)
                logger.debug(f"Marked {count} notifications as read for {user_id}")

            return count

    def get_unread_count(self, user_id: str) -> int:
        """
        Get the count of unread notifications.

        Args:
            user_id: User identifier

        Returns:
            Number of unread notifications
        """
        lock = self._get_lock(user_id)
        with lock:
            notifications = self._load_notifications(user_id)

        return sum(1 for n in notifications if not n.read)

    def delete(self, notification_id: str, user_id: str = "default") -> bool:
        """
        Delete a notification.

        Args:
            notification_id: ID of notification to delete
            user_id: User identifier

        Returns:
            True if notification was found and deleted, False otherwise
        """
        lock = self._get_lock(user_id)
        with lock:
            notifications = self._load_notifications(user_id)
            original_len = len(notifications)
            notifications = [n for n in notifications if n.id != notification_id]

            if len(notifications) < original_len:
                self._save_notifications(user_id, notifications)
                logger.debug(f"Notification {notification_id} deleted")
                return True

            return False

    def _load_notifications(self, user_id: str) -> List[Notification]:
        """Load notifications from disk."""
        notifications_path = self._get_notifications_path(user_id)

        if not notifications_path.exists():
            return []

        try:
            with open(notifications_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return [Notification.model_validate(item) for item in data]
        except Exception as e:
            logger.error(f"Failed to load notifications for {user_id}: {e}")
            return []

    def _save_notifications(self, user_id: str, notifications: List[Notification]) -> bool:
        """Save notifications to disk."""
        notifications_path = self._get_notifications_path(user_id)

        try:
            notifications_path.parent.mkdir(parents=True, exist_ok=True)

            write_text_atomic(
                notifications_path,
                json.dumps(
                    [n.model_dump(mode="json") for n in notifications],
                    indent=2,
                    default=str,
                ),
            )
            return True
        except Exception as e:
            logger.error(f"Failed to save notifications for {user_id}: {e}")
            return False

    def clear(self, user_id: str) -> bool:
        """Clear all notifications for a user."""
        lock = self._get_lock(user_id)
        with lock:
            notifications_path = self._get_notifications_path(user_id)
            if notifications_path.exists():
                try:
                    notifications_path.unlink()
                    return True
                except Exception as e:
                    logger.error(f"Failed to clear notifications for {user_id}: {e}")
                    return False
        return True

    def delete_for_thread(self, user_id: str, thread_id: str) -> int:
        """Delete notifications that point at a thread for one user."""
        lock = self._get_lock(user_id)
        with lock:
            notifications = self._load_notifications(user_id)
            kept = [n for n in notifications if n.thread_id != thread_id]
            deleted = len(notifications) - len(kept)
            if deleted:
                self._save_notifications(user_id, kept)
            return deleted

    def delete_thread_globally(self, thread_id: str) -> int:
        """Delete notifications that point at a thread from all user stores."""
        deleted = 0
        if not self.notifications_dir.exists():
            return deleted
        for path in self.notifications_dir.iterdir():
            if path.is_file() and path.suffix == ".json":
                deleted += self.delete_for_thread(path.stem, thread_id)
        return deleted


# Global notification store instance (initialized lazily)
_notification_store: Optional[NotificationStore] = None


def get_notification_store() -> NotificationStore:
    """Get or create the global notification store."""
    global _notification_store
    if _notification_store is None:
        from ..config import get_settings

        settings = get_settings()
        _notification_store = NotificationStore(settings.data_dir)
    return _notification_store


def create_notification(
    user_id: str,
    summary: str,
    thread_id: Optional[str] = None,
    task_id: Optional[str] = None,
    *,
    profile: Optional[str] = None,
    attempted: Optional[List[str]] = None,
    delivered_to: Optional[List[str]] = None,
    errors: Optional[Dict[str, str]] = None,
) -> Notification:
    """Convenience function to create a notification.

    Passes optional audit-log fields through to the store so the in-app feed
    reflects every channel the notification was sent to.
    """
    return get_notification_store().create(
        user_id=user_id,
        summary=summary,
        thread_id=thread_id,
        task_id=task_id,
        profile=profile,
        attempted=attempted,
        delivered_to=delivered_to,
        errors=errors,
    )


def active_admin_user_ids(repo: object = None) -> List[str]:
    """All enabled admin account ids; empty on any resolution failure.

    The shared enumeration half of every "announce to admins" path (workflow
    approval requests, the service-token expiry sweep). Pass an
    ``AccountsRepo`` when the caller already holds one; otherwise the repo is
    resolved from the current agent, and any failure yields ``[]`` because
    announcements are best-effort.
    """
    try:
        if repo is None:
            from .agent import get_current_agent

            agent = get_current_agent()
            repo = getattr(agent, "accounts_repo", None) if agent is not None else None
        if repo is None:
            return []
        return [
            user.id
            for user in repo.list_users()  # type: ignore[attr-defined]
            if getattr(user, "role", None) == "admin"
            and not getattr(user, "disabled", False)
        ]
    except Exception:  # noqa: BLE001 - announcements are best-effort
        logger.warning("could not enumerate admin users", exc_info=True)
        return []


def notify_user_best_effort(
    user_id: str,
    summary: str,
    thread_id: Optional[str] = None,
    *,
    push: bool = False,
    log_label: str = "user notification",
) -> None:
    """In-app notification row (and optionally an FCM push) that never raises.

    The shared delivery half of the approval announce paths (hook, workflow,
    and fallback approvals): a notification-center row (summary truncated to
    the store's 200-char cap) plus, when ``push`` and ``settings.fcm_enabled``,
    a push carrying the untruncated text. Summary COPY stays with the caller;
    only delivery lives here.
    """
    try:
        create_notification(
            user_id=str(user_id or ""),
            summary=summary[:200],
            thread_id=str(thread_id) if thread_id else None,
            task_id=None,
        )
        if push:
            from ..config import get_settings

            settings = get_settings()
            if getattr(settings, "fcm_enabled", False):
                from .fcm import send_to_all_devices

                send_to_all_devices(
                    data_dir=str(settings.data_dir),
                    text=summary,
                    thread_id=str(thread_id or ""),
                    user_id=str(user_id or ""),
                )
    except Exception:  # noqa: BLE001 - announcements are best-effort
        logger.warning("%s failed", log_label, exc_info=True)
