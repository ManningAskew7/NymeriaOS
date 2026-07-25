"""Interactive hook approvals: durable pending records + in-process rendezvous.

The ``require_approval`` lifecycle-hook action (``core/hooks/actions.py``)
holds a matched ``pre_tool_use`` tool call while the user decides. This module
owns everything around that hold:

- A durable JSON record per pending approval under
  ``data_dir/hooks/approvals/`` (the shared ``approval_records`` store:
  atomic writes, corrupt-skipping lists), so every resolve surface (REST,
  ``/hook`` command, bots, CLI, frontends) can list what is pending and so a
  crash leaves an auditable orphan instead of nothing.
- A :class:`FutureRendezvous` coordinator (the auth-prompt/browser-command
  idiom) keyed by ``record_id``. The action awaits the future with the hook's
  window; a resolver wakes it from any thread. Single-process by design: the
  API process is the only agent runtime (repo invariant), so the resolver and
  the waiting hook always share a process.
- Announce/resolve event helpers: ``hook_approval`` / ``hook_approval_resolved``
  autonomous events (carrying ``tool_call_id`` so frontends can pin the
  approve/deny UI to the rendered tool-call card) plus an in-app notification
  and FCM push, mirroring the ``notify`` action's delivery (deliberately
  bypassing the autonomous suppression gate: an approval request must always
  reach the user).

Resolution mutual exclusion is the coordinator's atomic pop, NOT claim files:
two concurrent resolves race on ``resolve()`` and exactly one wins. The
durable record is deleted by the WAITER (the action's cleanup), never the
resolver; a record with no live future means the waiter is gone (restart or
abort) and the resolver reports it stale. ``sweep_stale_records`` ages out
crash orphans on the API's hourly heartbeat.

Timeout semantics are the developer's spec: no answer within the window means
DENY, with a hardened no-consent message to the model (see the action).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from .approval_records import ApprovalRecordStore
from .approval_records import clamp_window as _clamp_window
from .future_rendezvous import FutureRendezvous
from .notifications import notify_user_best_effort
from .time_utils import utc_now as _utc_now

logger = logging.getLogger(__name__)

# Author-side window bounds (the hook definition's ``timeout_seconds``; the
# logic model in ``core/hook_manager.py`` enforces the same bounds at
# authoring, these are the runtime clamp/backstop).
DEFAULT_APPROVAL_WINDOW_SECONDS = 180.0
MIN_APPROVAL_WINDOW_SECONDS = 10.0
MAX_APPROVAL_WINDOW_SECONDS = 600.0

# A user's concurrently held tool calls are bounded so a hook matching a
# fan-out turn cannot mint unbounded records (mirrors MAX_PENDING_PER_WORKFLOW).
MAX_PENDING_PER_USER = 20

# Coordinator orphan TTL: past every legal window, so the sweep only ever
# collects futures whose waiter died without cleanup (should not happen; the
# action cleans up in ``finally``).
_ORPHAN_TTL_SECONDS = MAX_APPROVAL_WINDOW_SECONDS + 60
_SWEEP_INTERVAL_SECONDS = 60

# Durable records older than expiry by this slack are crash orphans; the
# hourly heartbeat sweep removes them.
STALE_RECORD_SLACK_SECONDS = 300

_ARGS_PREVIEW_CAP = 2_000
_PROMPT_CAP = 500


# --------------------------------------------------------------------------- #
# Durable record store (data_dir/hooks/approvals/<record_id>.json)
# --------------------------------------------------------------------------- #
# Mechanics live in the shared ApprovalRecordStore; the module-level names
# below are the stable public surface. The store resolves paths itself, so
# the redirect seam for tests is settings-level (`nymeria.config.get_settings`),
# not these functions.

_STORE = ApprovalRecordStore("hooks/approvals", noun="hook approval")


def approvals_dir() -> Path:
    return _STORE.dir()


def _record_path(record_id: str) -> Path:
    return _STORE.record_path(record_id)


def _write_record(record: Dict[str, Any]) -> None:
    _STORE.write(record)


def load_record(record_id: str) -> Optional[Dict[str, Any]]:
    return _STORE.load(record_id)


def delete_record(record_id: str) -> None:
    _STORE.delete(record_id)


def list_pending(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Pending records, newest first; ``user_id`` filters to one owner."""
    return _STORE.list(user_id)


def public_entry(record: Dict[str, Any]) -> Dict[str, Any]:
    """The caller-facing view of a pending record (already secret-free)."""
    return {
        "record_id": record.get("record_id", ""),
        "user_id": record.get("user_id", ""),
        "thread_id": record.get("thread_id", ""),
        "hook_id": record.get("hook_id", ""),
        "hook_name": record.get("hook_name", ""),
        "tool_name": record.get("tool_name", ""),
        "tool_call_id": record.get("tool_call_id", ""),
        "tool_args_preview": record.get("tool_args_preview", ""),
        "prompt": record.get("prompt", ""),
        "is_autonomous": bool(record.get("is_autonomous", False)),
        "created_at": record.get("created_at", ""),
        "expires_at": record.get("expires_at", ""),
    }


def sweep_stale_records() -> int:
    """Remove crash-orphaned records (past expiry + slack). Returns the count.

    Timeout enforcement lives in the waiting action (which also deletes its
    record); this sweep is hygiene for records whose waiter died without
    cleanup (process crash mid-hold). Called from the API's hourly heartbeat.
    """
    return _STORE.sweep_stale(STALE_RECORD_SLACK_SECONDS)


# --------------------------------------------------------------------------- #
# In-process rendezvous
# --------------------------------------------------------------------------- #

@dataclass
class PendingHookApproval:
    record_id: str
    user_id: str
    thread_id: str
    tool_name: str
    tool_call_id: str
    future: asyncio.Future
    created_at: float = field(default_factory=time.monotonic)


class HookApprovalCoordinator(FutureRendezvous[PendingHookApproval]):
    """Pending hook-approval futures keyed by ``record_id``."""

    def __init__(self) -> None:
        super().__init__(
            ttl_seconds=_ORPHAN_TTL_SECONDS,
            sweep_interval_seconds=_SWEEP_INTERVAL_SECONDS,
            log_label="hook_approval_coordinator",
        )

    def register(
        self,
        *,
        record_id: str,
        user_id: str,
        thread_id: str,
        tool_name: str,
        tool_call_id: str,
    ) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._add(
            record_id,
            PendingHookApproval(
                record_id=record_id,
                user_id=user_id,
                thread_id=thread_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
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
        note: str = "",
    ) -> bool:
        """Wake the held tool call. False = nothing to wake (lost the race,
        already timed out, or the waiter is gone, including a waiter whose
        loop died: the base ``_wake`` reports that as False, so the caller
        takes the stale-record path instead of surfacing a RuntimeError).
        """
        return self._resolve(
            record_id,
            lambda rec: {
                "status": "resolved",
                "approved": bool(approved),
                "resolved_by": resolved_by,
                "note": note or "",
            },
        )

    def abort_thread(self, thread_id: str) -> int:
        """Resolve every pending approval for ``thread_id`` as aborted.

        Called by ``agent_callable_lifecycle.abort_with_cascade`` so
        ``POST /threads/{id}/stop`` snaps a held tool call back immediately
        (it resolves to a deny; the turn is dying anyway). Covers the sync
        dispatch path too, where task cancellation cannot reach the wait.
        """
        matched = self._drain_matching(lambda rec: rec.thread_id == thread_id)
        aborted = 0
        for rec in matched:
            if self._wake(rec.future, {"status": "aborted"}):
                aborted += 1
        if aborted:
            logger.info(
                "hook_approval_coordinator aborted %d pending approval(s) for thread %s",
                aborted, thread_id,
            )
        return aborted

    def _swept_result(self, record: PendingHookApproval) -> dict[str, Any]:
        return {"status": "aborted"}

    def _on_orphan_swept(self, record: PendingHookApproval) -> None:
        logger.warning(
            "hook_approval_coordinator swept orphaned approval %s "
            "(tool=%s, user=%s, thread=%s)",
            record.record_id, record.tool_name, record.user_id, record.thread_id,
        )


_coordinator: Optional[HookApprovalCoordinator] = None


def get_hook_approval_coordinator() -> HookApprovalCoordinator:
    global _coordinator
    if _coordinator is None:
        _coordinator = HookApprovalCoordinator()
    return _coordinator


# --------------------------------------------------------------------------- #
# Mint + announce + resolve-event helpers (shared by the action and REST)
# --------------------------------------------------------------------------- #

def clamp_window(value: Any) -> float:
    """The effective approval window for an author-supplied value."""
    return _clamp_window(
        value,
        default=DEFAULT_APPROVAL_WINDOW_SECONDS,
        minimum=MIN_APPROVAL_WINDOW_SECONDS,
        maximum=MAX_APPROVAL_WINDOW_SECONDS,
    )


def args_preview(tool_args: Optional[dict]) -> str:
    """Bounded JSON preview of the held call's args for the resolve surfaces."""
    if not tool_args:
        return ""
    try:
        text = json.dumps(tool_args, default=str)
    except Exception:  # noqa: BLE001 - a preview must never raise
        return ""
    return text[:_ARGS_PREVIEW_CAP]


def create_pending_approval(
    *,
    user_id: str,
    thread_id: str,
    hook_id: str,
    hook_name: str,
    tool_name: str,
    tool_call_id: str,
    tool_args: Optional[dict],
    prompt: str,
    window_seconds: float,
    is_autonomous: bool = False,
    holder_kind: Optional[str] = None,
    trigger_label: Optional[str] = None,
) -> tuple[Dict[str, Any], asyncio.Future]:
    """Mint the durable record and register the rendezvous future.

    Raises ``ValueError`` when the per-user pending cap is reached (the
    action maps that to a fail-closed deny). Must run with an event loop
    (both dispatch paths provide one: the async seam natively, the sync
    bridge via ``asyncio.run``).
    """
    open_count = len(list_pending(user_id))
    if open_count >= MAX_PENDING_PER_USER:
        raise ValueError(
            f"user {user_id!r} already has {open_count} pending hook approvals"
        )
    now = _utc_now()
    record: Dict[str, Any] = {
        "record_id": uuid.uuid4().hex[:12],
        "user_id": user_id,
        "thread_id": thread_id,
        "hook_id": hook_id,
        "hook_name": hook_name,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "tool_args_preview": args_preview(tool_args),
        "prompt": (prompt or "")[:_PROMPT_CAP],
        "timeout_seconds": window_seconds,
        "is_autonomous": bool(is_autonomous),
        "holder_kind": holder_kind or "",
        "trigger_label": trigger_label or "",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=window_seconds)).isoformat(),
    }
    _write_record(record)
    future = get_hook_approval_coordinator().register(
        record_id=record["record_id"],
        user_id=user_id,
        thread_id=thread_id,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
    )
    return record, future


def announce_request(record: Dict[str, Any]) -> None:
    """Publish the ``hook_approval`` event + in-app notification + push.

    Best-effort: a delivery failure is logged, never raised; the durable
    record and the SSE-connected surfaces can still resolve the hold.

    The SSE publish is inline (in-process fan-out, effectively instant and
    load-bearing for the button surfaces). The notification + FCM half runs
    on a fire-and-forget daemon thread: ``require_approval`` awaits on the
    API event loop, and a blocking push (per-device HTTP) inline here would
    stall the loop AND eat into the approval window before the wait starts.
    """
    entry = public_entry(record)
    try:
        from .event_bus import publish_autonomous_event
        publish_autonomous_event(
            event_type="hook_approval",
            thread_id=str(record.get("thread_id") or ""),
            user_id=str(record.get("user_id") or ""),
            task_id="",
            data=entry,
        )
    except Exception:  # noqa: BLE001
        logger.warning("hook_approval event publish failed", exc_info=True)
    threading.Thread(
        target=_announce_notifications,
        args=(dict(record),),
        daemon=True,
        name="hook-approval-notify",
    ).start()


def _announce_notifications(record: Dict[str, Any]) -> None:
    """In-app notification + FCM push for a pending approval; never raises."""
    summary = (
        f"Approval needed: {record.get('tool_name')} "
        f"({record.get('prompt') or 'tool call held by a hook'})"
    )
    notify_user_best_effort(
        str(record.get("user_id") or ""),
        summary,
        thread_id=str(record.get("thread_id") or "") or None,
        push=True,
        log_label="hook approval notification",
    )


def publish_resolved_event(
    record: Dict[str, Any],
    *,
    outcome: str,
    resolved_by: str = "",
    note: str = "",
) -> None:
    """Publish ``hook_approval_resolved`` (outcome: approved|denied|timeout|
    aborted|stale) so every surface can drop its pending UI."""
    try:
        from .event_bus import publish_autonomous_event
        publish_autonomous_event(
            event_type="hook_approval_resolved",
            thread_id=str(record.get("thread_id") or ""),
            user_id=str(record.get("user_id") or ""),
            task_id="",
            data={
                "record_id": record.get("record_id", ""),
                "tool_call_id": record.get("tool_call_id", ""),
                "tool_name": record.get("tool_name", ""),
                "outcome": outcome,
                "resolved_by": resolved_by,
                "note": note or "",
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("hook_approval_resolved event publish failed", exc_info=True)


__all__ = [
    "DEFAULT_APPROVAL_WINDOW_SECONDS",
    "MAX_APPROVAL_WINDOW_SECONDS",
    "MAX_PENDING_PER_USER",
    "MIN_APPROVAL_WINDOW_SECONDS",
    "HookApprovalCoordinator",
    "PendingHookApproval",
    "announce_request",
    "approvals_dir",
    "args_preview",
    "clamp_window",
    "create_pending_approval",
    "delete_record",
    "get_hook_approval_coordinator",
    "list_pending",
    "load_record",
    "public_entry",
    "publish_resolved_event",
    "sweep_stale_records",
]
