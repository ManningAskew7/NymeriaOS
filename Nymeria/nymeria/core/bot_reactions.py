"""Shared state for two-way chat-bot emoji reactions (backlog #45).

Two small pieces, both process-local and both safe because the API process is
the single agent runtime (the same justification as ``ThreadLockManager``):

- The **turn-origin registry**: chat-platform bots stamp every dispatched turn
  with the originating platform message (``ChatRequest.platform_origin``,
  honored only for admin/service-token callers), and the chat routes record it
  here per thread. Turns that do NOT carry an origin (desktop, CLI, worker
  relays, local autonomous turns) CLEAR the entry at turn start, so the
  registry always describes the current turn, never a stale bot turn. The
  ``react`` tool reads it to know which platform message the current turn is
  about, so it can post an emoji reaction back without the model juggling
  platform ids.
- The **reply-suppression flag**: the ``react`` tool sets it when called with
  ``suppress_reply=true``; the chat routes read it to stamp ``suppress_reply``
  on the terminal ``done`` event / ``ChatResponse``. The flag resets whenever a
  new origin is stamped, so it can never leak across turns.

The live mid-stream suppression signal starts here: the ``react`` tool sets
the per-thread flag AND appends :data:`REPLY_SUPPRESSED_MARKER` to its result
text; ``core/agent_results.tool_result_extra_events`` emits the
``reply_suppressed`` stream event only when the marker is present AND this
registry confirms the flag for the thread, so no other tool's output (not
even one relayed verbatim through ``tool_invoke``) can suppress a bot reply
by echoing the marker text. Doc: ``docs/private/plans/emoji-reactions.md``.

This module also hosts the per-process reaction-fire debouncer the bot
handlers use so toggling an emoji on and off cannot fire repeated full agent
turns (the bots run it in their own processes; the registry above runs in the
API process).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional, Tuple

#: Marker the ``react`` tool appends to its result text when the caller asked
#: to suppress the turn's reply. It is the tool's own result convention (what
#: the model reads back); it is NOT sufficient to trigger suppression on its
#: own: event emission is verified against this registry's per-thread flag,
#: which only the real react tool sets.
REPLY_SUPPRESSED_MARKER = "[nymeria:reply_suppressed]"

#: Autonomous event type carrying an outbound reaction send to the bots.
REACTION_REQUEST_EVENT = "reaction_request"

#: Seconds within which a repeated (platform, channel, message, reactor,
#: emoji) reaction is ignored by the bot handlers. Long enough to absorb
#: emoji toggling and client retries, short enough that a deliberate repeat
#: reaction a minute later still fires.
REACTION_DEBOUNCE_TTL_SECONDS = 45.0

# Soft cap on tracked threads; oldest entries evict first. Bot threads are a
# small set in practice, this only bounds pathological churn.
_MAX_TRACKED_THREADS = 2048

# Cap on debounce entries (per bot process). Pruned lazily on insert.
_MAX_DEBOUNCE_ENTRIES = 4096

_lock = threading.Lock()
_turn_origins: Dict[str, Dict[str, Any]] = {}

_debounce_lock = threading.Lock()
_recent_reaction_fires: Dict[Tuple[str, str, str, str, str], float] = {}


def set_turn_origin(
    thread_id: str,
    *,
    platform: str,
    channel_id: str,
    message_id: str,
    kind: str = "message",
) -> None:
    """Record the platform message the current turn originates from.

    Called by the chat routes when a privileged request carries
    ``platform_origin``. Latest-wins per thread (queued prompts absorbed into
    a running turn mean "the most recent user message", which is also what a
    reaction should target). Resets the reply-suppression flag for the thread.
    """
    if not thread_id:
        return
    entry = {
        "platform": str(platform),
        "channel_id": str(channel_id),
        "message_id": str(message_id),
        "kind": str(kind or "message"),
        "suppressed": False,
    }
    with _lock:
        _turn_origins.pop(thread_id, None)
        _turn_origins[thread_id] = entry
        while len(_turn_origins) > _MAX_TRACKED_THREADS:
            _turn_origins.pop(next(iter(_turn_origins)))


def get_turn_origin(thread_id: str) -> Optional[Dict[str, Any]]:
    """Return a copy of the thread's current turn origin, or None."""
    with _lock:
        entry = _turn_origins.get(thread_id)
        return dict(entry) if entry is not None else None


def mark_reply_suppressed(thread_id: str) -> bool:
    """Flag the thread's current turn as reply-suppressed.

    Returns False when the thread has no recorded origin (the ``react`` tool
    refuses before this, so False only means a caller raced a missing origin).
    """
    with _lock:
        entry = _turn_origins.get(thread_id)
        if entry is None:
            return False
        entry["suppressed"] = True
        return True


def reply_suppressed(thread_id: str) -> bool:
    """Whether the thread's current turn asked for reply suppression."""
    with _lock:
        entry = _turn_origins.get(thread_id)
        return bool(entry and entry.get("suppressed"))


def clear_turn_origin(thread_id: str) -> None:
    """Drop a thread's origin entry.

    Called at turn start for every turn that does NOT carry a platform
    origin (the chat routes for interactive/relayed turns, and
    ``stream_bridge.stream_and_collect`` for local autonomous turns), so the
    ``react`` tool can never act on a previous bot turn's stale origin.
    """
    if not thread_id:
        return
    with _lock:
        _turn_origins.pop(thread_id, None)


def debounce_reaction_fire(
    *,
    platform: str,
    channel_id: str,
    message_id: str,
    reactor_id: str,
    emoji: str,
    ttl_seconds: float = REACTION_DEBOUNCE_TTL_SECONDS,
    now: Optional[float] = None,
) -> bool:
    """Return True when this exact reaction fired within the TTL (drop it).

    Otherwise records the fire and returns False. Keyed per (platform,
    channel, message, reactor, emoji) so emoji toggling or client retries
    cannot fire repeated full agent turns, while a different emoji, message,
    or reactor still fires normally. Process-local (each bot debounces its
    own platform).
    """
    key = (
        str(platform),
        str(channel_id),
        str(message_id),
        str(reactor_id),
        str(emoji),
    )
    t = time.monotonic() if now is None else now
    with _debounce_lock:
        last = _recent_reaction_fires.get(key)
        if last is not None and (t - last) < ttl_seconds:
            return True
        _recent_reaction_fires[key] = t
        if len(_recent_reaction_fires) > _MAX_DEBOUNCE_ENTRIES:
            expired = [
                k
                for k, v in _recent_reaction_fires.items()
                if (t - v) >= ttl_seconds
            ]
            for k in expired:
                _recent_reaction_fires.pop(k, None)
            while len(_recent_reaction_fires) > _MAX_DEBOUNCE_ENTRIES:
                _recent_reaction_fires.pop(next(iter(_recent_reaction_fires)))
        return False
