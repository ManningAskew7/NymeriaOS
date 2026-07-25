"""User-consented LLM fallback switching: pending records + rendezvous + gate.

Two trigger families share this module (backlog: the consented fallback plan
plus #105 P2):

- ``transport``: the primary model exhausted its retries on a retryable
  provider error and the runtime wants to switch the thread to the next
  fallback candidate (previously always silent).
- ``refusal``: the provider ended a response with an EMPTY refusal (Fable 5's
  safety classifiers; no text, no tool calls) and the runtime can discard the
  refused response and re-run the call on the next fallback candidate.

Both reduce to "may this turn continue on a different model, and who
decides?", so they share the durable pending records, the in-process
:class:`FutureRendezvous` coordinator, the ``fallback_prompt`` /
``fallback_prompt_resolved`` events, and the decision callback that
``vendor/react_agent`` consults across the vendored boundary
(``LLMConfig.fallback_decision_callback``).

Structure mirrors ``core/hook_approvals.py``: durable JSON records under
``data_dir/llm/fallback_approvals/`` with atomic writes, waiter-deletes-record
semantics, an orphan TTL sweep, and announce helpers that publish autonomous
events plus an in-app notification. This is the THIRD near-copy of the durable
approval-record store (hook_approvals, workflows/approvals); extracting a
shared ``ApprovalRecordStore`` is a filed backlog follow-up, the same
threshold that produced ``future_rendezvous.py``.

Deliberate policy (developer-locked, do not "fix"):

- Timeout on an unanswered prompt = AUTO-SWAP, not deny. A fallback is a
  resilience action; an unanswered prompt should keep the turn working.
  (Inverse of the hook ``require_approval`` deny-on-timeout, which guards
  dangerous actions.)
- Only a turn a human is actually watching parks. That means ALL of:
  interactive source stamp (``is_autonomous`` False; autonomous/self-invoke/
  sourceless turns never park), holder kind "user" (callable/handoff child
  turns serve a waiting parent tool call, not a person), an async streaming
  surface (``sync_surface`` False: ``/chat/sync`` serves bots and
  programmatic callers that cannot render the prompt), and no chat-bot
  platform origin (``core/bot_reactions.py``; interactive bot buttons are a
  later phase). Everything else skips: transport asks resolve "auto"
  (legacy silent swap), refusal asks resolve "swap".
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .future_rendezvous import FutureRendezvous, safe_set_result
from .storage_paths import mtime_sort_key, write_text_atomic

logger = logging.getLogger(__name__)

# Prompt-window bounds (mirror the settings-field bounds so a raw record edit
# cannot park a turn for hours).
DEFAULT_PROMPT_WINDOW_SECONDS = 180.0
MIN_PROMPT_WINDOW_SECONDS = 10.0
MAX_PROMPT_WINDOW_SECONDS = 600.0

# Hold-duration presets offered at the prompt (seconds). PERMANENT rides the
# separate ``hold_permanent`` flag on resolve.
HOLD_PRESET_SECONDS = (600, 3600, 7200, 28800)

# A user's concurrently parked turns are bounded (a thread parks at most one
# turn, so this is a generous backstop, mirroring MAX_PENDING_PER_USER in
# hook_approvals).
MAX_PENDING_PER_USER = 20

_ORPHAN_TTL_SECONDS = MAX_PROMPT_WINDOW_SECONDS + 60
_SWEEP_INTERVAL_SECONDS = 60

# Durable records older than expiry by this slack are crash orphans; the
# hourly heartbeat sweep removes them.
STALE_RECORD_SLACK_SECONDS = 300

VALID_KINDS = ("transport", "refusal")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Durable record store (data_dir/llm/fallback_approvals/<record_id>.json)
# --------------------------------------------------------------------------- #

def approvals_dir() -> Path:
    from ..config import get_settings
    return get_settings().data_dir / "llm" / "fallback_approvals"


def _record_path(record_id: str) -> Path:
    safe = "".join(c for c in record_id if c.isalnum() or c in ("-", "_"))
    return approvals_dir() / f"{safe}.json"


def _write_record(record: Dict[str, Any]) -> None:
    path = _record_path(record["record_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(record, indent=2, default=str))


def load_record(record_id: str) -> Optional[Dict[str, Any]]:
    try:
        record = json.loads(_record_path(record_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def delete_record(record_id: str) -> None:
    try:
        _record_path(record_id).unlink(missing_ok=True)
    except OSError:
        logger.warning(
            "could not delete fallback approval record %s", record_id, exc_info=True
        )


def list_pending(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Pending records, newest first; ``user_id`` filters to one owner."""
    base = approvals_dir()
    if not base.is_dir():
        return []
    records: List[Dict[str, Any]] = []
    for path in sorted(base.glob("*.json"), key=mtime_sort_key, reverse=True):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if user_id is not None and record.get("user_id") != user_id:
            continue
        records.append(record)
    return records


def public_entry(record: Dict[str, Any]) -> Dict[str, Any]:
    """The caller-facing view of a pending record (already secret-free)."""
    return {
        "record_id": record.get("record_id", ""),
        "kind": record.get("kind", "transport"),
        "user_id": record.get("user_id", ""),
        "thread_id": record.get("thread_id", ""),
        "from_provider": record.get("from_provider", ""),
        "from_model": record.get("from_model", ""),
        "to_provider": record.get("to_provider", ""),
        "to_model": record.get("to_model", ""),
        "reason": record.get("reason", ""),
        "http_status": record.get("http_status"),
        "timeout_seconds": record.get("timeout_seconds"),
        "hold_options": record.get("hold_options", list(HOLD_PRESET_SECONDS)),
        "allow_permanent": bool(record.get("allow_permanent", True)),
        "default_hold_seconds": record.get("default_hold_seconds"),
        "is_autonomous": bool(record.get("is_autonomous", False)),
        "created_at": record.get("created_at", ""),
        "expires_at": record.get("expires_at", ""),
    }


def sweep_stale_records() -> int:
    """Remove crash-orphaned records (past expiry + slack). Returns the count.

    Timeout enforcement lives in the waiting decision gate (which also
    deletes its record); this sweep is hygiene for records whose waiter died
    without cleanup. Called from the API's hourly heartbeat.
    """
    now = _utc_now()
    removed = 0
    for record in list_pending():
        expires = record.get("expires_at")
        try:
            expiry = datetime.fromisoformat(str(expires))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            expiry = None
        if expiry is None or now > expiry + timedelta(seconds=STALE_RECORD_SLACK_SECONDS):
            delete_record(str(record.get("record_id") or ""))
            removed += 1
    if removed:
        logger.info("swept %d stale fallback approval record(s)", removed)
    return removed


# --------------------------------------------------------------------------- #
# In-process rendezvous
# --------------------------------------------------------------------------- #

@dataclass
class PendingFallbackApproval:
    record_id: str
    user_id: str
    thread_id: str
    kind: str
    future: asyncio.Future
    created_at: float = field(default_factory=time.monotonic)


class FallbackApprovalCoordinator(FutureRendezvous[PendingFallbackApproval]):
    """Pending fallback-consent futures keyed by ``record_id``."""

    def __init__(self) -> None:
        super().__init__(
            ttl_seconds=_ORPHAN_TTL_SECONDS,
            sweep_interval_seconds=_SWEEP_INTERVAL_SECONDS,
            log_label="fallback_approval_coordinator",
        )

    def register(
        self,
        *,
        record_id: str,
        user_id: str,
        thread_id: str,
        kind: str,
    ) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._add(
            record_id,
            PendingFallbackApproval(
                record_id=record_id,
                user_id=user_id,
                thread_id=thread_id,
                kind=kind,
                future=future,
            ),
        )
        return future

    def resolve(
        self,
        record_id: str,
        *,
        approved: bool,
        resolved_by: str,
        hold_seconds: Optional[int] = None,
        hold_permanent: bool = False,
        note: str = "",
    ) -> bool:
        """Wake the parked turn. False = nothing to wake (lost the race,
        already timed out, or the waiter is gone)."""
        try:
            return self._resolve(
                record_id,
                lambda rec: {
                    "status": "resolved",
                    "approved": bool(approved),
                    "hold_seconds": hold_seconds,
                    "hold_permanent": bool(hold_permanent),
                    "resolved_by": resolved_by,
                    "note": note or "",
                },
            )
        except RuntimeError:
            logger.warning(
                "fallback approval %s could not be woken (waiter loop closed)",
                record_id,
            )
            return False

    def abort_thread(self, thread_id: str) -> int:
        """Resolve every pending prompt for ``thread_id`` as aborted.

        Called by ``agent_callable_lifecycle.abort_with_cascade`` so
        ``POST /threads/{id}/stop`` snaps a parked turn back immediately.
        """
        matched = self._drain_matching(lambda rec: rec.thread_id == thread_id)
        aborted = 0
        for rec in matched:
            future = rec.future
            if future.done():
                continue
            future.get_loop().call_soon_threadsafe(
                safe_set_result, future, {"status": "aborted"}
            )
            aborted += 1
        if aborted:
            logger.info(
                "fallback_approval_coordinator aborted %d pending prompt(s) for thread %s",
                aborted, thread_id,
            )
        return aborted

    def _swept_result(self, record: PendingFallbackApproval) -> dict[str, Any]:
        return {"status": "aborted"}

    def _on_orphan_swept(self, record: PendingFallbackApproval) -> None:
        logger.warning(
            "fallback_approval_coordinator swept orphaned prompt %s "
            "(kind=%s, user=%s, thread=%s)",
            record.record_id, record.kind, record.user_id, record.thread_id,
        )


_coordinator: Optional[FallbackApprovalCoordinator] = None


def get_fallback_approval_coordinator() -> FallbackApprovalCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = FallbackApprovalCoordinator()
    return _coordinator


# --------------------------------------------------------------------------- #
# Mint + announce + resolve-event helpers (shared by the gate and REST)
# --------------------------------------------------------------------------- #

def clamp_window(value: Any) -> float:
    """The effective prompt window for a configured value."""
    try:
        window = float(value)
    except (TypeError, ValueError):
        return DEFAULT_PROMPT_WINDOW_SECONDS
    return max(MIN_PROMPT_WINDOW_SECONDS, min(window, MAX_PROMPT_WINDOW_SECONDS))


def build_pending_record(
    *,
    kind: str,
    user_id: str,
    thread_id: str,
    payload: Dict[str, Any],
    window_seconds: float,
    default_hold_seconds: int,
    is_autonomous: bool = False,
) -> Dict[str, Any]:
    """The durable record dict for a pending prompt (pure, no I/O)."""
    now = _utc_now()
    return {
        "record_id": uuid.uuid4().hex[:12],
        "kind": kind if kind in VALID_KINDS else "transport",
        "user_id": user_id,
        "thread_id": thread_id,
        "from_provider": str(payload.get("from_provider") or ""),
        "from_model": str(payload.get("from_model") or ""),
        "to_provider": str(payload.get("to_provider") or ""),
        "to_model": str(payload.get("to_model") or ""),
        "reason": str(payload.get("reason") or ""),
        "http_status": payload.get("http_status"),
        "timeout_seconds": window_seconds,
        "hold_options": list(HOLD_PRESET_SECONDS),
        "allow_permanent": True,
        "default_hold_seconds": default_hold_seconds,
        "is_autonomous": bool(is_autonomous),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=window_seconds)).isoformat(),
    }


def persist_pending_record(record: Dict[str, Any]) -> None:
    """Cap-check and write a pending record (blocking; run off-loop).

    Raises ``ValueError`` when the per-user pending cap is reached (the gate
    maps that to an auto-swap).
    """
    user_id = str(record.get("user_id") or "")
    open_count = len(list_pending(user_id)) if user_id else len(list_pending())
    if open_count >= MAX_PENDING_PER_USER:
        raise ValueError(
            f"user {user_id!r} already has {open_count} pending fallback prompts"
        )
    _write_record(record)


def create_pending_approval(
    *,
    kind: str,
    user_id: str,
    thread_id: str,
    payload: Dict[str, Any],
    window_seconds: float,
    default_hold_seconds: int,
    is_autonomous: bool = False,
) -> tuple[Dict[str, Any], asyncio.Future]:
    """Mint the durable record and register the rendezvous future.

    The future registers BEFORE the record is written, so a resolver can
    never find a record whose waiter does not exist yet. Raises ``ValueError``
    on the per-user pending cap. Must run with an event loop.
    """
    record = build_pending_record(
        kind=kind,
        user_id=user_id,
        thread_id=thread_id,
        payload=payload,
        window_seconds=window_seconds,
        default_hold_seconds=default_hold_seconds,
        is_autonomous=is_autonomous,
    )
    future = get_fallback_approval_coordinator().register(
        record_id=record["record_id"],
        user_id=user_id,
        thread_id=thread_id,
        kind=record["kind"],
    )
    try:
        persist_pending_record(record)
    except BaseException:
        get_fallback_approval_coordinator().discard(record["record_id"])
        raise
    return record, future


def announce_request(record: Dict[str, Any]) -> None:
    """Publish the ``fallback_prompt`` event + an in-app notification.

    Best-effort: a delivery failure is logged, never raised; the durable
    record and the REST/command surfaces can still resolve the park, and the
    timeout keeps the turn from stranding either way.
    """
    entry = public_entry(record)
    try:
        from .event_bus import publish_autonomous_event
        publish_autonomous_event(
            event_type="fallback_prompt",
            thread_id=str(record.get("thread_id") or ""),
            user_id=str(record.get("user_id") or ""),
            task_id="",
            data=entry,
        )
    except Exception:  # noqa: BLE001
        logger.warning("fallback_prompt event publish failed", exc_info=True)
    threading.Thread(
        target=_announce_notifications,
        args=(dict(record),),
        daemon=True,
        name="fallback-approval-notify",
    ).start()


def _announce_notifications(record: Dict[str, Any]) -> None:
    """In-app notification + FCM push for a pending prompt; never raises."""
    try:
        from ..config import get_settings
        from .notifications import create_notification
        label = "refused this turn" if record.get("kind") == "refusal" else "is failing"
        summary = (
            f"Model {record.get('from_model') or 'primary'} {label}: "
            f"swap to {record.get('to_model') or 'the fallback model'}? "
            f"(auto-swaps in {int(float(record.get('timeout_seconds') or 0))}s)"
        )
        create_notification(
            user_id=str(record.get("user_id") or ""),
            summary=summary[:200],
            thread_id=str(record.get("thread_id") or "") or None,
            task_id=None,
        )
        settings = get_settings()
        if getattr(settings, "fcm_enabled", False):
            from .fcm import send_to_all_devices
            send_to_all_devices(
                data_dir=str(settings.data_dir),
                text=summary,
                thread_id=str(record.get("thread_id") or ""),
                user_id=str(record.get("user_id") or ""),
            )
    except Exception:  # noqa: BLE001
        logger.warning("fallback approval notification failed", exc_info=True)


def publish_resolved_event(
    record: Dict[str, Any],
    *,
    outcome: str,
    resolved_by: str = "",
    hold_seconds: Optional[int] = None,
    hold_permanent: bool = False,
    note: str = "",
) -> None:
    """Publish ``fallback_prompt_resolved`` (outcome: approved|declined|
    timeout|aborted|stale) so every surface can drop its pending UI."""
    try:
        from .event_bus import publish_autonomous_event
        publish_autonomous_event(
            event_type="fallback_prompt_resolved",
            thread_id=str(record.get("thread_id") or ""),
            user_id=str(record.get("user_id") or ""),
            task_id="",
            data={
                "record_id": record.get("record_id", ""),
                "kind": record.get("kind", "transport"),
                "outcome": outcome,
                "resolved_by": resolved_by,
                "hold_seconds": hold_seconds,
                "hold_permanent": bool(hold_permanent),
                "note": note or "",
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("fallback_prompt_resolved event publish failed", exc_info=True)


# --------------------------------------------------------------------------- #
# The decision gate (LLMConfig.fallback_decision_callback)
# --------------------------------------------------------------------------- #

async def _await_parked_decision(
    *,
    kind: str,
    user_id: str,
    thread_id: str,
    payload: Dict[str, Any],
    window_seconds: float,
    default_hold_seconds: int,
    is_autonomous: bool,
) -> Dict[str, Any]:
    """Park the turn on a minted prompt and translate its outcome.

    The WAITER owns the durable record: every exit path deletes it and
    publishes ``fallback_prompt_resolved``. Timeout and abort follow the
    locked policy (timeout = auto-swap; abort = fail, the turn is dying).
    A cancellation (the turn task dying under the park) also cleans up: the
    coordinator entry is discarded and clients are told to drop the card.
    """
    record = build_pending_record(
        kind=kind,
        user_id=user_id,
        thread_id=thread_id,
        payload=payload,
        window_seconds=window_seconds,
        default_hold_seconds=default_hold_seconds,
        is_autonomous=is_autonomous,
    )
    record_id = record["record_id"]
    # Future first (a resolver can never see a record without a waiter), then
    # the cap check + record write off-loop: this coroutine runs on the API
    # event loop from the async consult sites.
    future = get_fallback_approval_coordinator().register(
        record_id=record_id,
        user_id=user_id,
        thread_id=thread_id,
        kind=record["kind"],
    )
    try:
        await asyncio.to_thread(persist_pending_record, record)
    except BaseException:
        get_fallback_approval_coordinator().discard(record_id)
        raise
    announce_request(record)
    try:
        result = await asyncio.wait_for(future, timeout=window_seconds)
    except asyncio.TimeoutError:
        get_fallback_approval_coordinator().discard(record_id)
        publish_resolved_event(record, outcome="timeout")
        logger.info(
            "fallback prompt %s (%s, thread %s) unanswered after %.0fs; auto-swapping",
            record_id, kind, thread_id, window_seconds,
        )
        return {"action": "swap"}
    except BaseException:
        # Cancellation (or another non-timeout exit) while parked: without
        # this, the coordinator entry leaks until the orphan sweep and every
        # client keeps a stale prompt card.
        get_fallback_approval_coordinator().discard(record_id)
        publish_resolved_event(record, outcome="aborted")
        raise
    finally:
        delete_record(record_id)

    status = (result or {}).get("status") if isinstance(result, dict) else None
    if status == "aborted":
        publish_resolved_event(record, outcome="aborted")
        return {"action": "fail"}
    approved = bool((result or {}).get("approved"))
    resolved_by = str((result or {}).get("resolved_by") or "")
    hold_seconds = (result or {}).get("hold_seconds")
    hold_permanent = bool((result or {}).get("hold_permanent"))
    publish_resolved_event(
        record,
        outcome="approved" if approved else "declined",
        resolved_by=resolved_by,
        hold_seconds=hold_seconds if approved else None,
        hold_permanent=hold_permanent if approved else False,
        note=str((result or {}).get("note") or ""),
    )
    if not approved:
        return {"action": "fail"}
    decision: Dict[str, Any] = {"action": "swap"}
    if hold_permanent:
        decision["hold_permanent"] = True
    elif hold_seconds is not None:
        try:
            decision["hold_seconds"] = max(0, min(604800, int(hold_seconds)))
        except (TypeError, ValueError):
            # Malformed hold from a resolver: drop it and let the activation
            # payload fall back to the configured default hold.
            pass
    return decision


def _thread_has_bot_origin(thread_id: str) -> bool:
    """True when the current turn came in through a chat-platform bot.

    Bots cannot render the consent prompt until the bot-buttons phase ships,
    so their turns are treated as non-button channels and auto-swap
    immediately (developer-locked: no reply-word convention)."""
    try:
        from .bot_reactions import get_turn_origin
        return get_turn_origin(thread_id) is not None
    except Exception:  # noqa: BLE001
        return False


def make_fallback_decision_callback(
    *,
    thread_id: str,
    user_id: str,
    switch_mode: Optional[str],
    refusal_mode: Optional[str],
    prompt_timeout_seconds: Any,
    default_hold_seconds: int,
):
    """Build the async ``fallback_decision_callback`` for one thread's LLMConfig.

    ``switch_mode`` and ``refusal_mode`` are the EFFECTIVE values
    (thread-override else global), resolved by ``get_llm_config_for_thread``.
    The returned coroutine receives a context dict: the site's fallback
    payload plus ``kind`` ("transport"|"refusal") and the turn-source stamps
    ``is_autonomous``/``holder_kind``/``sync_surface`` (None/absent stamps
    mean the source is unknown and the turn is treated as NOT
    consent-capable).
    """
    window = clamp_window(prompt_timeout_seconds)

    async def decide(context: Dict[str, Any]) -> Dict[str, Any]:
        kind = str(context.get("kind") or "transport")
        if kind == "refusal":
            mode = (refusal_mode or "off").lower()
            if mode == "off":
                return {"action": "fail"}
            skip_result: Dict[str, Any] = {"action": "swap"}
        else:
            mode = (switch_mode or "auto").lower()
            if mode == "auto":
                return {"action": "auto"}
            skip_result = {"action": "auto"}

        if mode == "auto":
            # refusal-auto: swap silently with the default hold.
            return skip_result

        # "ask": only a turn a human is watching parks (see the module
        # docstring's locked policy). Autonomous/self-invoke/source-unknown
        # turns, callable/handoff child turns (their "user" is a waiting
        # parent tool call), sync-surface turns (/chat/sync: bots and
        # programmatic callers), and bot-origin threads all skip.
        consent_capable = (
            context.get("is_autonomous") is False
            and context.get("holder_kind") == "user"
            and not context.get("sync_surface")
        )
        if not consent_capable or _thread_has_bot_origin(thread_id):
            return skip_result

        try:
            return await _await_parked_decision(
                kind=kind,
                user_id=user_id,
                thread_id=thread_id,
                payload=context,
                window_seconds=window,
                default_hold_seconds=default_hold_seconds,
                is_autonomous=False,
            )
        except Exception:  # noqa: BLE001
            # The gate must never be the reason a turn dies: any fault in the
            # park machinery falls back to the resilience default.
            logger.warning(
                "fallback decision gate failed for thread %s; auto-swapping",
                thread_id, exc_info=True,
            )
            return skip_result

    return decide


__all__ = [
    "DEFAULT_PROMPT_WINDOW_SECONDS",
    "HOLD_PRESET_SECONDS",
    "MAX_PENDING_PER_USER",
    "MAX_PROMPT_WINDOW_SECONDS",
    "MIN_PROMPT_WINDOW_SECONDS",
    "FallbackApprovalCoordinator",
    "PendingFallbackApproval",
    "announce_request",
    "approvals_dir",
    "build_pending_record",
    "clamp_window",
    "create_pending_approval",
    "delete_record",
    "persist_pending_record",
    "get_fallback_approval_coordinator",
    "list_pending",
    "load_record",
    "make_fallback_decision_callback",
    "public_entry",
    "publish_resolved_event",
    "sweep_stale_records",
]
