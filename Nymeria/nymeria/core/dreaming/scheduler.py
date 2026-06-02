"""Phase 2: automatic dream scheduling.

A periodic sweep, run in the API process (where the agent runtime lives, since
:func:`~nymeria.core.dreaming.invoke.invoke_dream` spawns a local agent turn),
finds dream-enabled threads that have gone quiet and fires a dream. Three
cheapest-first gates decide eligibility, and ALL must pass:

1. ``min_interval_hours``  -- enough wall-clock since the last dream.
2. ``min_turns_since_last`` -- enough new user turns to be worth reflecting on.
3. ``min_idle_minutes``    -- the thread has been quiet long enough right now.

A thread currently mid-turn is skipped (never dream a live conversation).
Eligibility is read-only; ``invoke_dream`` writes ``last_dream_at``
synchronously, so the interval gate self-serializes against the next sweep and
``invoke_dream``'s own in-flight guard catches the rare manual/scheduler race.

The gate inputs come from the activity log (``USER_MESSAGE`` entries for the
turn count, the newest entry of any type for the idle clock). Activity is keyed
on the parent thread, so a dream's own shadow-thread turn never resets these.

Wiring (mirrors the spawned-thread idle sweep):
- Slim: the in-process Ticker calls :func:`sweep_dreamable_threads` via a
  closure over the local agent (``Ticker(dream_sweeper=...)``).
- Docker: the API process runs it on a heartbeat task, because the worker's
  ticker holds no local agent to dream against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..time_utils import utc_now

logger = logging.getLogger(__name__)

# How often the dream sweep runs. The per-thread gates do the real rate
# limiting; this is just the polling granularity, set below the 30-minute
# spawn sweep so a freshly-idle thread (gate floor is 5 minutes) does not wait
# too long for its first dream.
DREAM_SWEEP_INTERVAL_SECONDS = 600  # 10 minutes

# Newest-first cap on activity entries scanned per user per sweep. The gates
# only care about recent activity and post-dream turns, so this is generous
# while bounding work on a large log.
_ACTIVITY_SCAN_LIMIT = 2000


@dataclass
class DreamDecision:
    """Outcome of evaluating one thread, with a human-readable reason for logs."""

    eligible: bool
    reason: str


def evaluate_dream_eligibility(
    tc: Any,
    *,
    now: datetime,
    last_activity_at: Optional[datetime],
    user_turns_since_dream: int,
) -> DreamDecision:
    """Pure gating decision for one thread. No I/O.

    Exposed separately from the sweep so the gate matrix can be unit-tested
    without an agent or activity log. Gates are cheapest-first; the first
    failing gate short-circuits with its reason.
    """
    dreaming = getattr(tc, "dreaming", None)
    if dreaming is None or not dreaming.enabled:
        return DreamDecision(False, "dreaming disabled")
    if getattr(tc, "shadow_parent_id", None):
        return DreamDecision(False, "thread is a shadow")

    # 1. interval since last dream
    if dreaming.last_dream_at is not None:
        elapsed_h = (now - dreaming.last_dream_at).total_seconds() / 3600.0
        if elapsed_h < dreaming.min_interval_hours:
            return DreamDecision(
                False,
                f"interval {elapsed_h:.1f}h < {dreaming.min_interval_hours}h",
            )

    # 2. enough new user turns to reflect on
    if user_turns_since_dream < dreaming.min_turns_since_last:
        return DreamDecision(
            False,
            f"turns {user_turns_since_dream} < {dreaming.min_turns_since_last}",
        )

    # 3. thread quiet long enough right now
    if last_activity_at is None:
        return DreamDecision(False, "no recorded activity")
    idle_min = (now - last_activity_at).total_seconds() / 60.0
    if idle_min < dreaming.min_idle_minutes:
        return DreamDecision(
            False, f"idle {idle_min:.0f}m < {dreaming.min_idle_minutes}m"
        )

    return DreamDecision(
        True,
        f"idle {idle_min:.0f}m, {user_turns_since_dream} turn(s) since last dream",
    )


def _thread_is_processing(agent: Any, thread_id: str) -> bool:
    """True when a turn currently holds the thread lock (mirrors the API guard)."""
    locks = getattr(agent, "_thread_locks", None)
    if locks is None:
        return False
    try:
        return locks.get_lock_info(thread_id) is not None
    except Exception:  # noqa: BLE001
        return False


def _activity_summary_for_user(
    user_id: str,
) -> Tuple[Dict[str, datetime], Dict[str, List[datetime]]]:
    """Return ``(last_activity_by_thread, user_msg_times_by_thread)``.

    Reads the user's activity log once. ``user_msg_times_by_thread`` holds the
    timestamps of ``USER_MESSAGE`` entries so the caller can count turns since
    the last dream per-thread without re-querying. Both dicts are empty if the
    log can't be read.
    """
    from ..activity_log import ActivityType, get_activity_log

    last_activity: Dict[str, datetime] = {}
    user_msgs: Dict[str, List[datetime]] = {}
    try:
        entries = get_activity_log().get_entries(
            user_id=user_id, limit=_ACTIVITY_SCAN_LIMIT
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "dream sweep: activity load failed for %s", user_id, exc_info=True
        )
        return last_activity, user_msgs

    for entry in entries:  # newest-first
        tid = entry.thread_id
        if not tid:
            continue
        # newest-first: the first time we see a thread is its last activity.
        if tid not in last_activity:
            last_activity[tid] = entry.timestamp
        if entry.type == ActivityType.USER_MESSAGE:
            user_msgs.setdefault(tid, []).append(entry.timestamp)
    return last_activity, user_msgs


def sweep_dreamable_threads(agent: Any) -> int:
    """Fire a dream for every eligible dream-enabled thread.

    Mirrors ``sweep_idle_spawned_threads``' per-user enumeration over the
    thread metadata stores. Returns the number of dreams started. Intended to
    be called periodically from the Ticker (slim) or the API heartbeat
    (Docker); both run in the agent's own process.
    """
    from .invoke import DreamInvocationError, invoke_dream

    try:
        metadata_dir = agent.thread_metadata_manager.metadata_dir
    except AttributeError:
        return 0
    if not metadata_dir.exists():
        return 0

    now = utc_now()
    fired = 0
    for path in sorted(metadata_dir.glob("*.json")):
        user_id = path.stem
        if not user_id:
            continue
        try:
            store = agent.thread_metadata_manager.get_store(user_id)
        except Exception:  # noqa: BLE001
            logger.debug(
                "dream sweep: store load failed for %s", user_id, exc_info=True
            )
            continue

        last_activity, user_msgs = _activity_summary_for_user(user_id)

        for thread_id, meta in list(store.threads.items()):
            # Never dream a dream: shadow threads carry platform="dream"
            # (and shadow_parent_id, re-checked in the gate).
            if getattr(meta, "platform", None) == "dream":
                continue
            try:
                tc = agent.thread_config_manager.get_config(thread_id)
            except Exception:  # noqa: BLE001
                continue
            if tc is None or tc.dreaming is None or not tc.dreaming.enabled:
                continue

            # Authoritative-owner guard. A thread can appear in more than one
            # user's metadata store (shared/bot channels), so dream only under
            # its real owner -- otherwise the dream's global-memory writes would
            # land in whichever user the sweep visited first. A None owner means
            # an unclaimed personal thread: dream under the enumerating user.
            try:
                owner = agent.accounts_repo.get_thread_owner(thread_id)
            except Exception:  # noqa: BLE001
                owner = None
            if owner is not None and owner != user_id:
                logger.debug(
                    "dream sweep: skip %s (owned by %s, not %s)",
                    thread_id,
                    owner,
                    user_id,
                )
                continue

            since = tc.dreaming.last_dream_at
            times = user_msgs.get(thread_id, [])
            if since is not None:
                turns = sum(1 for t in times if t > since)
            else:
                turns = len(times)

            decision = evaluate_dream_eligibility(
                tc,
                now=now,
                last_activity_at=last_activity.get(thread_id),
                user_turns_since_dream=turns,
            )
            if not decision.eligible:
                logger.debug("dream sweep: skip %s (%s)", thread_id, decision.reason)
                continue

            if _thread_is_processing(agent, thread_id):
                logger.debug("dream sweep: skip %s (processing a turn)", thread_id)
                continue

            try:
                shadow_id, _summary = invoke_dream(
                    agent,
                    parent_thread_id=thread_id,
                    user_id=user_id,
                    model_override=tc.dreaming.model,
                )
                fired += 1
                logger.info(
                    "dream sweep: started %s for %s (%s)",
                    shadow_id,
                    thread_id,
                    decision.reason,
                )
            except DreamInvocationError as e:
                logger.warning(
                    "dream sweep: invoke_dream refused for %s: %s", thread_id, e
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "dream sweep: unexpected error firing dream for %s", thread_id
                )

    return fired
