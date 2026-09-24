"""Notification system for Nymeria.

Provides in-app notification storage and retrieval for urgent
messages from autonomous agent executions.

A history file that does not load is never saved over (#401): the original
bytes go to ``notifications/quarantine/`` and the file is replaced with every
record that still validates. A file that cannot be read, or whose original
cannot be preserved, is read-only: ``create`` still returns the notification
(delivery never fails on bookkeeping) without saving it, and the other
mutators raise ``NotificationsUnavailableError``.
"""

import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from .keyed_locks import KeyedRLockMap
from .storage_paths import StoreUnavailableError, safe_path_segment, write_text_atomic
from .store_repair import (
    UnavailableEpisodes,
    read_json,
    record_store_repair,
    repair_file,
)
from .time_utils import utc_now

logger = logging.getLogger(__name__)

# Thread-safe locks for notification operations (keyed by user_id)
_notification_locks = KeyedRLockMap()

# user_ids whose history file is unreadable or unrepairable and already
# reported: one ERROR line per episode, since every notify call reads it.
_unavailable = UnavailableEpisodes()


class NotificationsUnavailableError(StoreUnavailableError):
    """A change was refused because the user's notification history file
    exists but could not be read or repaired (#401). Saving would replace it.
    The API answers it 503."""


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
            notifications, writable = self._load_state(user_id)
            if not writable:
                # The history cannot be read, so it is not saved over; the
                # caller's delivery goes ahead regardless.
                logger.warning(
                    "Notification for %s not recorded: its history file is unavailable",
                    user_id,
                )
                return notification
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
            notifications = self._writable_notifications(user_id)

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
            notifications = self._writable_notifications(user_id)

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
            notifications = self._writable_notifications(user_id)
            original_len = len(notifications)
            notifications = [n for n in notifications if n.id != notification_id]

            if len(notifications) < original_len:
                self._save_notifications(user_id, notifications)
                logger.debug(f"Notification {notification_id} deleted")
                return True

            return False

    def _load_notifications(self, user_id: str) -> List[Notification]:
        """Load notifications from disk (what could be read, for readers)."""
        return self._load_state(user_id)[0]

    def _writable_notifications(self, user_id: str) -> List[Notification]:
        notifications, writable = self._load_state(user_id)
        if not writable:
            raise NotificationsUnavailableError(
                f"Notification history for user {user_id} could not be read "
                f"or repaired; refusing to overwrite it"
            )
        return notifications

    def _load_state(self, user_id: str) -> Tuple[List[Notification], bool]:
        """The user's notifications, and whether the file may be written.

        Takes the user's lock (re-entrant: every caller already holds it). A
        list that fails validation keeps each record that validates alone; the
        original bytes are preserved in ``quarantine/`` before the file is
        replaced, so no save can destroy them.
        """
        path = self._get_notifications_path(user_id)
        with self._get_lock(user_id):
            read = read_json(path)
            if read.absent:
                _unavailable.end(user_id)
                return [], True
            if read.unreadable:
                return [], self._report_unavailable(user_id, str(read.error))
            data = read.data if read.parsed else None
            records = _validated(data)
            if records is not None:
                _unavailable.end(user_id)
                return records, True
            assert read.raw is not None
            kept = _salvaged(data)
            repaired = repair_file(
                path,
                read.raw,
                _serialize(kept),
                lambda again: _validated(again.data if again.parsed else None),
                store="notifications",
                user_id=user_id,
            )
            if repaired.status == "superseded":
                _unavailable.end(user_id)
                return repaired.fresh, True
            if repaired.status == "replaced" and repaired.quarantine is not None:
                _unavailable.end(user_id)
                if isinstance(data, list):
                    detail = (
                        f"invalid notification history repaired: kept "
                        f"{len(kept)}, dropped {len(data) - len(kept)}"
                    )
                else:
                    detail = "corrupt notification history preserved; the history starts empty"
                logger.warning(
                    "Notifications for %s did not load; %s (original at %s)",
                    user_id,
                    detail,
                    repaired.quarantine,
                )
                record_store_repair(
                    "notifications",
                    f"{detail} (original at quarantine/{repaired.quarantine.name})",
                    user_id=user_id,
                )
                return kept, True
            return kept, self._report_unavailable(
                user_id, f"{read.error or 'invalid records'}; could not be repaired"
            )

    @staticmethod
    def _report_unavailable(user_id: str, why: str) -> bool:
        """Log once per episode that the history is read-only; returns False."""
        if _unavailable.start(user_id):
            logger.error(
                "Notification history for %s is unavailable (%s); serving what "
                "could be read and refusing changes",
                user_id,
                why,
            )
        else:
            logger.debug("Notification history for %s still unavailable: %s", user_id, why)
        return False

    def _save_notifications(self, user_id: str, notifications: List[Notification]) -> bool:
        """Save notifications to disk."""
        notifications_path = self._get_notifications_path(user_id)

        try:
            notifications_path.parent.mkdir(parents=True, exist_ok=True)

            write_text_atomic(notifications_path, _serialize(notifications))
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
            notifications = self._writable_notifications(user_id)
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
                try:
                    deleted += self.delete_for_thread(path.stem, thread_id)
                except NotificationsUnavailableError:
                    # One user's unrepairable file must not stop the others'
                    # cleanup; its rows stay until the file loads again.
                    logger.warning(
                        "Notifications for thread %s left in %s's unavailable history",
                        thread_id,
                        path.stem,
                    )
        return deleted


def _validated(data: Any) -> Optional[List[Notification]]:
    """Every record validated, or None when the document is not a valid list."""
    if not isinstance(data, list):
        return None
    try:
        return [Notification.model_validate(item) for item in data]
    except Exception:  # noqa: BLE001 - the caller salvages
        return None


def _salvaged(data: Any) -> List[Notification]:
    kept: List[Notification] = []
    for item in data if isinstance(data, list) else []:
        try:
            kept.append(Notification.model_validate(item))
        except Exception:  # noqa: BLE001 - one bad record costs only itself
            continue
    return kept


def _serialize(notifications: List[Notification]) -> str:
    return json.dumps(
        [n.model_dump(mode="json") for n in notifications], indent=2, default=str
    )


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
