"""Per-tab drive leases: one thread drives one tab at a time.

Nothing else serializes two threads of one user driving the same browser
tab: their commands interleave freely on the shared CDP session, either
thread can consume the other's dialogs or clobber its form state, and the
result is the phantom-failure debugging shape from the #282 incident
family. A lease is claimed (or refreshed) by every tab-addressed
``chrome_*`` dispatch and by the tab a thread creates; a dispatch against
a tab another thread holds refuses fast, naming the holder, and nothing is
sent to the browser.

Leases end two ways: the holder's turn ends (the same
``agent._release_browser_session`` seam that publishes the #191 session
release), or the idle TTL lapses, deliberately equal to the extension's
own ``DETACH_LINGER_MS`` safety net (120s) so backend leases and
extension-side debugger holds age out together.

In-process by design, like the command coordinator beside it: the API
process is the only agent runtime in both deployment shapes, so every
dispatch already depends on in-process state. (Multi-worker APIs would
need this in Redis, uniformly with the coordinator.)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

# The extension detaches an idle debugger hold after 120s
# (``DETACH_LINGER_MS`` in nymeria-browser). Backend leases use the same
# clock so "the backend thinks the tab is claimed" and "the extension still
# holds it" cannot drift far apart.
IDLE_LEASE_TTL_SECONDS = 120


@dataclass
class _Lease:
    thread_id: str
    expires_at: float  # monotonic


@dataclass(frozen=True)
class LeaseHolder:
    """What a refused claimant is told about the current holder."""

    thread_id: str
    seconds_remaining: float


class BrowserDriveLeases:
    """Registry of (user, browser, tab) -> holding thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[tuple[str, str, int], _Lease] = {}

    def claim(
        self, *, user_id: str, client_id: str, tab_id: int, thread_id: str
    ) -> Optional[LeaseHolder]:
        """Claim (or refresh) the tab for ``thread_id``.

        Returns None on success; a :class:`LeaseHolder` naming the current
        holder when another thread holds an unexpired lease. Expired leases
        are pruned lazily on the claims that encounter them.
        """
        key = (user_id, client_id, tab_id)
        now = time.monotonic()
        with self._lock:
            lease = self._leases.get(key)
            if lease is not None and lease.expires_at <= now:
                lease = None
            if lease is not None and lease.thread_id != thread_id:
                return LeaseHolder(
                    thread_id=lease.thread_id,
                    seconds_remaining=max(0.0, lease.expires_at - now),
                )
            self._leases[key] = _Lease(
                thread_id=thread_id, expires_at=now + IDLE_LEASE_TTL_SECONDS
            )
            return None

    def release_thread(self, user_id: str, thread_id: str) -> set[str]:
        """Drop every lease ``thread_id`` holds for ``user_id``.

        Returns the client_ids of the browsers it held leases on (for the
        turn-end release bookkeeping). Called from the turn-end seam.
        """
        released: set[str] = set()
        with self._lock:
            for key in [
                key
                for key, lease in self._leases.items()
                if key[0] == user_id and lease.thread_id == thread_id
            ]:
                released.add(key[1])
                del self._leases[key]
        return released

    def other_thread_holds(
        self, *, user_id: str, client_id: str, thread_id: str
    ) -> bool:
        """True while a DIFFERENT thread holds an unexpired lease on any tab
        of this browser. The turn-end seam reads it to decide whether the
        ``browser_session_release`` event may be published: released while
        another thread is mid-task, it would drop that thread's idle
        debugger holds too (the extension releases per-browser, not
        per-tab), so the event is suppressed and the extension's own idle
        linger takes over instead."""
        now = time.monotonic()
        with self._lock:
            return any(
                key[0] == user_id
                and key[1] == client_id
                and lease.thread_id != thread_id
                and lease.expires_at > now
                for key, lease in self._leases.items()
            )

    def reset_for_tests(self) -> None:
        with self._lock:
            self._leases.clear()


_leases: Optional[BrowserDriveLeases] = None


def get_browser_drive_leases() -> BrowserDriveLeases:
    global _leases
    if _leases is None:
        _leases = BrowserDriveLeases()
    return _leases


__all__ = [
    "IDLE_LEASE_TTL_SECONDS",
    "BrowserDriveLeases",
    "LeaseHolder",
    "get_browser_drive_leases",
]
