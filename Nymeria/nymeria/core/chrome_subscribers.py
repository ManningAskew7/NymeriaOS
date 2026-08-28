"""Per-process registry of Chrome-extension SSE subscribers.

The ``/autonomous/stream`` endpoint registers a subscriber here when the
incoming ``client_id`` looks like a Nymeria browser extension. The
``chrome_*`` tools query :func:`is_chrome_connected` to fail fast with a
clear message when the user has no extension attached, instead of
publishing into the void and waiting for the per-command timeout, and
:func:`chrome_disconnect_age` to tell a genuinely absent extension from
one mid-recycle (Chrome idle-kills the MV3 service worker and a heartbeat
alarm reconnects it within a minute; the dispatch path rides that window
out, backlog #172).

In-process state. The set is rebuilt by reconnects; no persistence needed.
"""

from __future__ import annotations

import threading
import time
from typing import Optional, Set

# Stable prefix the extension's background SW generates when it first
# initialises (see ``nymeria-browser/src/utils/storage.ts::ensureClientId``).
CHROME_CLIENT_ID_PREFIX = "nymeria-browser-"

# Autonomous event types only the browser extension can act on, so only it is
# served them. ``browser_command`` carries the whole command envelope, an
# upload's base64 file bytes included, and every other consumer (desktop,
# mobile, both CLI transports, the bots' admin firehose) parsed it and
# dropped it: a 10MB upload reached each of them as ~14MB of JSON to throw
# away. ``browser_session_release`` (#191) is the turn-end signal to drop
# idle debugger holds: nothing but the extension holds one.
# ``browser_login_input`` carries an operator's keystrokes toward the tab
# they are signing into, so fanning it out would mail a password to every
# other subscriber; it is chrome-only for confidentiality, not just waste.
# Lives here, beside the "is this caller the extension" test, so the stream
# and the in-process CLI transport cannot filter differently.
CHROME_ONLY_EVENT_TYPES = frozenset(
    {"browser_command", "browser_session_release", "browser_login_input"}
)

_lock = threading.Lock()
_subscribers_by_user: dict[str, Set[str]] = {}
_user_by_subscriber: dict[str, str] = {}
# When a user's LAST extension stream dropped (monotonic clock). Chrome
# idle-kills the extension's service worker ~30s after its last activity and
# a heartbeat alarm re-establishes the stream within a minute, so a recent
# entry here means "probably recycling, seconds from back" rather than
# "gone". The chrome_* dispatch path reads it to ride that window out
# (backlog #172) instead of fail-fasting with a false "not connected".
_last_disconnect_by_user: dict[str, float] = {}
# The manifest version the extension announced when it subscribed, and a
# per-user connect counter. The counter lets `chrome_reload_extension` tell
# "a NEW stream landed after the reload" apart from "the old stream is still
# up" without tracking stream identities; the version answers "which build
# is running now". Last-write-wins per user: a user driving two Chrome
# profiles reads as whichever subscribed last, which is fine for a dev-loop
# report and not worth a per-subscriber map.
_version_by_user: dict[str, str] = {}
_connects_by_user: dict[str, int] = {}
# When the user's most recent extension stream subscribed (monotonic clock).
# Dates the announced version: a subscribe stamped after a deploy is the
# rebuilt extension announcing itself, one from before it is the old build
# still up. Read by chrome_health's tab-free connection probe (#223).
_last_connect_by_user: dict[str, float] = {}


def is_chrome_client_id(client_id: str | None) -> bool:
    return bool(client_id) and client_id.startswith(CHROME_CLIENT_ID_PREFIX)


def add_chrome_subscriber(
    *, user_id: str, subscriber_id: str, version: Optional[str] = None
) -> None:
    """Record that ``subscriber_id`` is a Chrome-extension SSE stream for
    ``user_id``. Idempotent for membership; every call still counts as a
    connect and refreshes the announced version."""
    with _lock:
        _subscribers_by_user.setdefault(user_id, set()).add(subscriber_id)
        _user_by_subscriber[subscriber_id] = user_id
        _last_disconnect_by_user.pop(user_id, None)
        _connects_by_user[user_id] = _connects_by_user.get(user_id, 0) + 1
        _last_connect_by_user[user_id] = time.monotonic()
        if version:
            _version_by_user[user_id] = version


def remove_chrome_subscriber(subscriber_id: str) -> None:
    """Drop ``subscriber_id`` from the registry on disconnect.

    Stamps the user's last-disconnect time only when this was their LAST
    stream: while another extension stream is still up the user is simply
    connected, not recycling.
    """
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
            _last_disconnect_by_user[user_id] = time.monotonic()


def is_chrome_connected(user_id: str) -> bool:
    """Return True if at least one Chrome extension is currently subscribed
    to the autonomous stream for ``user_id``."""
    with _lock:
        return bool(_subscribers_by_user.get(user_id))


def chrome_subscribers_for(user_id: str) -> Set[str]:
    """Return the set of subscriber IDs for ``user_id`` (copy)."""
    with _lock:
        return set(_subscribers_by_user.get(user_id, ()))


def chrome_disconnect_age(user_id: str) -> Optional[float]:
    """Seconds since ``user_id``'s last extension stream dropped.

    ``None`` when the user is connected or has never connected this process
    lifetime (in-process state, like the registry itself: after an API
    restart every user reads as never-connected, which fails fast, the
    honest pre-#172 behavior).
    """
    with _lock:
        stamp = _last_disconnect_by_user.get(user_id)
        return None if stamp is None else time.monotonic() - stamp


def chrome_extension_version(user_id: str) -> Optional[str]:
    """The manifest version the user's extension last announced, if any."""
    with _lock:
        return _version_by_user.get(user_id)


def chrome_last_connect_age(user_id: str) -> Optional[float]:
    """Seconds since ``user_id``'s most recent extension stream subscribed.

    ``None`` when no stream has subscribed this process lifetime. Ages the
    announced version (see :func:`chrome_extension_version`): the stamp is
    written on every subscribe, whether or not a version rode on it.
    """
    with _lock:
        stamp = _last_connect_by_user.get(user_id)
        return None if stamp is None else time.monotonic() - stamp


def chrome_connect_count(user_id: str) -> int:
    """How many extension streams have subscribed for ``user_id`` this
    process lifetime. Monotonic; compare snapshots to detect a NEW stream."""
    with _lock:
        return _connects_by_user.get(user_id, 0)


def reset_for_tests() -> None:
    """Test-only: drop all tracked subscribers and disconnect stamps."""
    with _lock:
        _subscribers_by_user.clear()
        _user_by_subscriber.clear()
        _last_disconnect_by_user.clear()
        _version_by_user.clear()
        _connects_by_user.clear()
        _last_connect_by_user.clear()


__all__ = [
    "CHROME_CLIENT_ID_PREFIX",
    "CHROME_ONLY_EVENT_TYPES",
    "is_chrome_client_id",
    "add_chrome_subscriber",
    "remove_chrome_subscriber",
    "is_chrome_connected",
    "chrome_subscribers_for",
    "chrome_disconnect_age",
    "chrome_extension_version",
    "chrome_last_connect_age",
    "chrome_connect_count",
    "reset_for_tests",
]
