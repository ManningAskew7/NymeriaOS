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

Two granularities, deliberately:

* Per-USER aggregates (connected at all, last disconnect, connect count)
  drive the fail-fast/recycle-grace dispatch gates, which predate browser
  identity and still read naturally at user grain.
* Per-BROWSER rows keyed by the extension's persistent ``client_id`` (a
  ``nymeria-browser-<uuid4>`` minted once per Chrome profile and stored in
  ``chrome.storage.local``) back the roster: which distinct browsers are
  connected right now, each with its own version and connect/disconnect
  stamps. This is what fixes the #282 masking, where two simultaneously
  subscribed builds read as one healthy last-writer-wins entry, and it is
  the identity that browser-target routing selects on.

In-process state. The registry is rebuilt by reconnects; no persistence
needed. Human-readable browser labels are NOT here: they are durable user
data (``UserProfile.get_browser_preferences``).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
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
# Which browser (persistent extension client_id) each live stream belongs
# to. A browser normally has ONE stream, but a reconnect can briefly
# overlap old and new streams for the same browser, so browser rows carry a
# stream refcount rather than a boolean.
_browser_by_subscriber: dict[str, str] = {}
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
# is running now". Last-write-wins per user BY DESIGN for these dev-loop
# reports; the per-browser rows below are the truthful multi-browser view.
_version_by_user: dict[str, str] = {}
_connects_by_user: dict[str, int] = {}
# When the user's most recent extension stream subscribed (monotonic clock).
# Dates the announced version: a subscribe stamped after a deploy is the
# rebuilt extension announcing itself, one from before it is the old build
# still up. Read by chrome_health's tab-free connection probe (#223).
_last_connect_by_user: dict[str, float] = {}


@dataclass
class _BrowserState:
    """Mutable per-browser row, keyed under its user by client_id."""

    streams: int = 0
    version: Optional[str] = None
    connects: int = 0
    last_connect: Optional[float] = None
    last_disconnect: Optional[float] = None


# user_id -> {browser client_id -> state}. Rows persist for the process
# lifetime once seen (like the per-user stamps), so a disconnected browser
# still shows in the roster with its disconnect age instead of vanishing.
_browsers_by_user: dict[str, dict[str, _BrowserState]] = {}


@dataclass(frozen=True)
class BrowserRecord:
    """Read-model row for one known browser of one user.

    ``client_id`` is the extension's persistent identity (stable across
    worker recycles and browser restarts; reset only by the extension's
    Forget). Ages are seconds, computed at read time; ``None`` means the
    event never happened this process lifetime.
    """

    client_id: str
    version: Optional[str]
    connected: bool
    streams: int
    connects: int
    last_connect_age_s: Optional[float] = field(default=None)
    last_disconnect_age_s: Optional[float] = field(default=None)


def is_chrome_client_id(client_id: str | None) -> bool:
    return bool(client_id) and client_id.startswith(CHROME_CLIENT_ID_PREFIX)


# The announced version renders in trusted-voice platform text (roster
# lines, offline-target errors, reload notes) and arrives as a query param
# the extension chooses freely, so it is bounded at ingest: one line,
# printable, at most this many characters. Real manifest versions are
# dotted numerics under 16 chars; the headroom covers suffixed dev builds.
_VERSION_MAX_CHARS = 32


def _clean_version(version: str) -> Optional[str]:
    cleaned = "".join(
        ch for ch in " ".join(str(version).split()) if ch.isprintable()
    )
    return cleaned[:_VERSION_MAX_CHARS] or None


def add_chrome_subscriber(
    *,
    user_id: str,
    subscriber_id: str,
    version: Optional[str] = None,
    client_id: Optional[str] = None,
) -> None:
    """Record that ``subscriber_id`` is a Chrome-extension SSE stream for
    ``user_id``. Idempotent for membership; every call still counts as a
    connect and refreshes the announced version.

    ``client_id`` is the extension's persistent per-browser identity; it
    keys the per-browser roster row. A caller that omits it (older tests,
    hypothetical non-extension registrars) gets a row keyed by the
    subscriber id, which degrades to one-row-per-stream rather than
    breaking.
    """
    browser_key = client_id or subscriber_id
    version = _clean_version(version) if version else None
    now = time.monotonic()
    with _lock:
        _subscribers_by_user.setdefault(user_id, set()).add(subscriber_id)
        _user_by_subscriber[subscriber_id] = user_id
        _browser_by_subscriber[subscriber_id] = browser_key
        _last_disconnect_by_user.pop(user_id, None)
        _connects_by_user[user_id] = _connects_by_user.get(user_id, 0) + 1
        _last_connect_by_user[user_id] = now
        if version:
            _version_by_user[user_id] = version
        row = _browsers_by_user.setdefault(user_id, {}).setdefault(
            browser_key, _BrowserState()
        )
        row.streams += 1
        row.connects += 1
        row.last_connect = now
        if version:
            row.version = version


def remove_chrome_subscriber(subscriber_id: str) -> None:
    """Drop ``subscriber_id`` from the registry on disconnect.

    Stamps the user's last-disconnect time only when this was their LAST
    stream: while another extension stream is still up the user is simply
    connected, not recycling. The browser row's own disconnect stamp lands
    when ITS last stream drops, so one browser going away is visible even
    while another stays connected.
    """
    with _lock:
        user_id = _user_by_subscriber.pop(subscriber_id, None)
        browser_key = _browser_by_subscriber.pop(subscriber_id, None)
        if user_id is None:
            return
        now = time.monotonic()
        if browser_key is not None:
            row = _browsers_by_user.get(user_id, {}).get(browser_key)
            if row is not None and row.streams > 0:
                row.streams -= 1
                if row.streams == 0:
                    row.last_disconnect = now
        bucket = _subscribers_by_user.get(user_id)
        if bucket is None:
            return
        bucket.discard(subscriber_id)
        if not bucket:
            _subscribers_by_user.pop(user_id, None)
            _last_disconnect_by_user[user_id] = now


def is_chrome_connected(user_id: str) -> bool:
    """Return True if at least one Chrome extension is currently subscribed
    to the autonomous stream for ``user_id``."""
    with _lock:
        return bool(_subscribers_by_user.get(user_id))


def chrome_subscribers_for(user_id: str) -> Set[str]:
    """Return the set of subscriber IDs for ``user_id`` (copy)."""
    with _lock:
        return set(_subscribers_by_user.get(user_id, ()))


def chrome_browser_roster(user_id: str) -> list[BrowserRecord]:
    """Every browser this process has seen for ``user_id``, connected first.

    Within each half (connected / disconnected), most recently connected
    first. Disconnected rows persist for the process lifetime so a browser
    that just dropped is reported with its disconnect age rather than
    silently vanishing from the list.
    """
    now = time.monotonic()
    with _lock:
        rows = _browsers_by_user.get(user_id, {})
        records = [
            BrowserRecord(
                client_id=key,
                version=state.version,
                connected=state.streams > 0,
                streams=state.streams,
                connects=state.connects,
                last_connect_age_s=(
                    None if state.last_connect is None else now - state.last_connect
                ),
                last_disconnect_age_s=(
                    None
                    if state.last_disconnect is None
                    else now - state.last_disconnect
                ),
            )
            for key, state in rows.items()
        ]
    records.sort(
        key=lambda r: (
            not r.connected,
            r.last_connect_age_s if r.last_connect_age_s is not None else float("inf"),
        )
    )
    return records


def is_chrome_browser_connected(user_id: str, client_id: str) -> bool:
    """True when the specific browser ``client_id`` has a live stream."""
    with _lock:
        row = _browsers_by_user.get(user_id, {}).get(client_id)
        return bool(row and row.streams > 0)


def chrome_browser_disconnect_age(user_id: str, client_id: str) -> Optional[float]:
    """Seconds since browser ``client_id``'s last stream dropped.

    ``None`` when that browser is connected, or has never been seen this
    process lifetime (the roster distinguishes the two).
    """
    with _lock:
        row = _browsers_by_user.get(user_id, {}).get(client_id)
        if row is None or row.streams > 0 or row.last_disconnect is None:
            return None
        return time.monotonic() - row.last_disconnect


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
    """The manifest version the user's extension last announced, if any.

    Last-writer-wins across the user's browsers; per-browser truth is in
    :func:`chrome_browser_roster`.
    """
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
        _browser_by_subscriber.clear()
        _last_disconnect_by_user.clear()
        _version_by_user.clear()
        _connects_by_user.clear()
        _last_connect_by_user.clear()
        _browsers_by_user.clear()


__all__ = [
    "CHROME_CLIENT_ID_PREFIX",
    "CHROME_ONLY_EVENT_TYPES",
    "BrowserRecord",
    "is_chrome_client_id",
    "add_chrome_subscriber",
    "remove_chrome_subscriber",
    "is_chrome_connected",
    "chrome_subscribers_for",
    "chrome_browser_roster",
    "is_chrome_browser_connected",
    "chrome_browser_disconnect_age",
    "chrome_disconnect_age",
    "chrome_extension_version",
    "chrome_last_connect_age",
    "chrome_connect_count",
    "reset_for_tests",
]
