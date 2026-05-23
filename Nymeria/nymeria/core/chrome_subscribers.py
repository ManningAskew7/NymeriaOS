"""Per-process registry of Chrome-extension SSE subscribers.

The ``/autonomous/stream`` endpoint registers a subscriber here when the
incoming ``client_id`` looks like a Nymeria browser extension. The
``chrome_*`` tools query :func:`is_chrome_connected` to fail fast with a
clear message when the user has no extension attached, instead of
publishing into the void and waiting for the per-command timeout.

In-process state. The set is rebuilt by reconnects; no persistence needed.
"""

from __future__ import annotations

import threading
from typing import Set

# Stable prefix the extension's background SW generates when it first
# initialises (see ``nymeria-browser/src/utils/storage.ts::ensureClientId``).
CHROME_CLIENT_ID_PREFIX = "nymeria-browser-"

_lock = threading.Lock()
_subscribers_by_user: dict[str, Set[str]] = {}
_user_by_subscriber: dict[str, str] = {}


def is_chrome_client_id(client_id: str | None) -> bool:
    return bool(client_id) and client_id.startswith(CHROME_CLIENT_ID_PREFIX)


def add_chrome_subscriber(*, user_id: str, subscriber_id: str) -> None:
    """Record that ``subscriber_id`` is a Chrome-extension SSE stream for
    ``user_id``. Idempotent."""
    with _lock:
        _subscribers_by_user.setdefault(user_id, set()).add(subscriber_id)
        _user_by_subscriber[subscriber_id] = user_id


def remove_chrome_subscriber(subscriber_id: str) -> None:
    """Drop ``subscriber_id`` from the registry on disconnect."""
    with _lock:
        user_id = _user_by_subscriber.pop(subscriber_id, None)
        if user_id is None:
            return
        bucket = _subscribers_by_user.get(user_id)
        if bucket is None:
            return
        bucket.discard(subscriber_id)
        if not bucket:
            _subscribers_by_user.pop(user_id, None)


def is_chrome_connected(user_id: str) -> bool:
    """Return True if at least one Chrome extension is currently subscribed
    to the autonomous stream for ``user_id``."""
    with _lock:
        return bool(_subscribers_by_user.get(user_id))


def chrome_subscribers_for(user_id: str) -> Set[str]:
    """Return the set of subscriber IDs for ``user_id`` (copy)."""
    with _lock:
        return set(_subscribers_by_user.get(user_id, ()))


def reset_for_tests() -> None:
    """Test-only: drop all tracked subscribers."""
    with _lock:
        _subscribers_by_user.clear()
        _user_by_subscriber.clear()


__all__ = [
    "CHROME_CLIENT_ID_PREFIX",
    "is_chrome_client_id",
    "add_chrome_subscriber",
    "remove_chrome_subscriber",
    "is_chrome_connected",
    "chrome_subscribers_for",
    "reset_for_tests",
]
