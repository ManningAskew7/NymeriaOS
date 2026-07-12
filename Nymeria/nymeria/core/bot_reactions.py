"""Shared state for two-way chat-bot emoji reactions (backlog #45).

Two small pieces, both process-local and both safe because the API process is
the single agent runtime (the same justification as ``ThreadLockManager``):

- The **turn-origin registry**: chat-platform bots stamp every dispatched turn
  with the originating platform message (``ChatRequest.platform_origin``), and
  the chat routes record it here per thread (latest-wins). The ``react`` tool
  reads it to know which platform message the current turn is about, so it can
  post an emoji reaction back without the model juggling platform ids.
- The **reply-suppression flag**: the ``react`` tool sets it when called with
  ``suppress_reply=true``; the chat routes read it to stamp ``suppress_reply``
  on the terminal ``done`` event / ``ChatResponse``. The flag resets whenever a
  new origin is stamped, so it can never leak across turns that carry an
  origin, and turns without an origin never read it.

The live mid-stream suppression signal does NOT come from here: the tool also
appends :data:`REPLY_SUPPRESSED_MARKER` to its result text, which
``core/agent_results.tool_result_extra_events`` converts into a
``reply_suppressed`` stream event, so the marker rides the wire (and the
autonomous mirror, and the turn buffer) deterministically. Doc:
``docs/private/plans/emoji-reactions.md``.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

#: Deterministic marker the ``react`` tool appends to its result text when the
#: caller asked to suppress the turn's reply. Namespaced so arbitrary tool
#: output cannot plausibly contain it; the extra-event emission additionally
#: keys on the tool name (react / tool_invoke).
REPLY_SUPPRESSED_MARKER = "[nymeria:reply_suppressed]"

#: Autonomous event type carrying an outbound reaction send to the bots.
REACTION_REQUEST_EVENT = "reaction_request"

# Soft cap on tracked threads; oldest entries evict first. Bot threads are a
# small set in practice, this only bounds pathological churn.
_MAX_TRACKED_THREADS = 2048

_lock = threading.Lock()
_turn_origins: Dict[str, Dict[str, Any]] = {}


def set_turn_origin(
    thread_id: str,
    *,
    platform: str,
    channel_id: str,
    message_id: str,
    kind: str = "message",
) -> None:
    """Record the platform message the current turn originates from.

    Called by the chat routes when a request carries ``platform_origin``.
    Latest-wins per thread (queued prompts absorbed into a running turn mean
    "the most recent user message", which is also what a reaction should
    target). Resets the reply-suppression flag for the thread.
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
    """Drop a thread's origin entry (test hygiene; production relies on
    latest-wins overwrites instead of explicit clears)."""
    with _lock:
        _turn_origins.pop(thread_id, None)
