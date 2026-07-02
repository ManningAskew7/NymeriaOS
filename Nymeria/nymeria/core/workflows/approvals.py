"""Durable ``nym.approve`` pending records and the resume path.

A suspended workflow holds NO live process or future: the JSON record under
``data_dir/workflows/pending/`` is the whole story (plan: "Durability and
approvals"), which is what makes it survive API restarts and day-later
approvals. This module owns that store plus the resume executor:

- The ``approve`` verb mints a record (parent-side, never the child) with a
  run-scoped ``resume_token`` the executor later verifies the child's
  ``needs_approval`` finish frame against.
- Resolution (owner or admin, REST-only) claims the record atomically, then
  spawns a FRESH subprocess into the declared continuation with the persisted
  ``state`` and a ``decision`` dict. A declined approval ALSO runs the
  continuation (``approved: false``) so the author can clean up or notify.
- The record pins ``revision_hash``: resume against changed content or a
  revoked revision approval is refused with ``resume_invalid``, never run.
- Expiry (default 7 days) resolves as declined with note ``expired``, driven
  by a small heartbeat in the API app (the process workflows execute in, in
  both deployment shapes).

Records are integrity state, not observability: writes are atomic
(temp + replace, the hook-store idiom), unlike the best-effort run records
in ``trace.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...config import get_settings
from .envelope import (
    KIND_RESUME_INVALID,
    WorkflowError,
    error_envelope,
)

logger = logging.getLogger(__name__)

DEFAULT_APPROVAL_EXPIRY_DAYS = 7
MAX_PENDING_PER_WORKFLOW = 10
# A claimed-but-never-finished record (crash mid-resume) is reclaimed by the
# sweep after this long.
STALE_CLAIM_SECONDS = 24 * 3600
APPROVAL_SWEEP_INTERVAL_SECONDS = 3600


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def pending_dir() -> Path:
    return get_settings().data_dir / "workflows" / "pending"


def _record_path(record_id: str) -> Path:
    safe = "".join(c for c in record_id if c.isalnum() or c in ("-", "_"))
    return pending_dir() / f"{safe}.json"


def _write_record(record: Dict[str, Any]) -> None:
    path = _record_path(record["record_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def create_pending_approval(
    *,
    run_id: str,
    workflow_id: str,
    origin: str,
    owner_user_id: str,
    user_id: str,
    thread_id: str,
    revision_hash: str,
    resume_entrypoint: str,
    state: Dict[str, Any],
    prompt: str,
    depth: int = 0,
) -> Dict[str, Any]:
    """Mint and persist one pending-approval record; returns it.

    Raises ``ValueError`` when this workflow already has the maximum number
    of unresolved approvals (a recurring fire must not accumulate unbounded
    pending records).
    """
    open_count = sum(
        1 for rec in list_pending_approvals() if rec.get("workflow_id") == workflow_id
    )
    if open_count >= MAX_PENDING_PER_WORKFLOW:
        raise ValueError(
            f"workflow {workflow_id!r} already has {open_count} unresolved "
            "approval requests; resolve or expire them first"
        )
    now = _utc_now()
    record: Dict[str, Any] = {
        "record_id": run_id,
        "run_id": run_id,
        "workflow_id": workflow_id,
        "origin": origin,
        "owner_user_id": owner_user_id,
        "user_id": user_id,
        "thread_id": thread_id,
        "revision_hash": revision_hash,
        "resume_entrypoint": resume_entrypoint,
        "state": state,
        "prompt": prompt,
        "resume_token": secrets.token_urlsafe(32),
        "depth": depth,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=DEFAULT_APPROVAL_EXPIRY_DAYS)).isoformat(),
    }
    _write_record(record)
    return record


def load_approval(record_id: str) -> Optional[Dict[str, Any]]:
    path = _record_path(record_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def list_pending_approvals(user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Pending records, newest first; ``user_id`` filters to one owner."""
    base = pending_dir()
    if not base.is_dir():
        return []
    records: List[Dict[str, Any]] = []
    for path in sorted(
        base.glob("*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True
    ):
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


def public_approval_entry(record: Dict[str, Any]) -> Dict[str, Any]:
    """The caller-facing view: never the resume token, never the raw state."""
    return {
        "record_id": record.get("record_id", ""),
        "workflow_id": record.get("workflow_id", ""),
        "origin": record.get("origin", ""),
        "user_id": record.get("user_id", ""),
        "thread_id": record.get("thread_id", ""),
        "prompt": str(record.get("prompt") or "")[:500],
        "resume_entrypoint": record.get("resume_entrypoint", ""),
        "created_at": record.get("created_at", ""),
        "expires_at": record.get("expires_at", ""),
    }


def claim_approval(record_id: str) -> Optional[Dict[str, Any]]:
    """Atomically claim a record for resolution (rename); None if gone.

    The rename is the mutual-exclusion primitive: two concurrent resolves of
    the same record race on it and exactly one wins.
    """
    path = _record_path(record_id)
    claimed = path.with_name(path.name + ".claimed")
    try:
        path.rename(claimed)
    except OSError:
        return None
    try:
        # Rename preserves the inode mtime (record CREATION time); restamp so
        # the stale-claim purge measures age since the CLAIM, as documented.
        os.utime(claimed)
    except OSError:
        pass  # best-effort: worst case the purge window starts at creation
    try:
        record = json.loads(claimed.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        claimed.unlink(missing_ok=True)
        return None
    return record if isinstance(record, dict) else None


def finish_claim(record_id: str) -> None:
    """Drop a claimed record once its resolution finished (either way)."""
    path = _record_path(record_id)
    path.with_name(path.name + ".claimed").unlink(missing_ok=True)


def delete_approval(record_id: str) -> None:
    """Remove an unclaimed record (e.g. the run ended without suspending)."""
    try:
        _record_path(record_id).unlink(missing_ok=True)
    except OSError:
        logger.warning("could not delete approval record %s", record_id, exc_info=True)


def _parse_iso(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def expired_approvals() -> List[Dict[str, Any]]:
    """Pending records past ``expires_at`` (also purges stale claim files)."""
    now = _utc_now()
    base = pending_dir()
    if base.is_dir():
        for stale in base.glob("*.json.claimed"):
            try:
                age = now.timestamp() - stale.stat().st_mtime
                if age > STALE_CLAIM_SECONDS:
                    stale.unlink(missing_ok=True)
                    logger.warning("purged stale claimed approval %s", stale.name)
            except OSError:
                continue
    out: List[Dict[str, Any]] = []
    for record in list_pending_approvals():
        expires = _parse_iso(record.get("expires_at"))
        if expires is not None and expires <= now:
            out.append(record)
    return out


# --- resume ---------------------------------------------------------------


def _load_target(record: Dict[str, Any]):
    """Current (config, parameters) for the record's workflow, or None.

    ``origin == "tool"`` re-reads the published definition from the loader;
    ``origin == "draft"`` re-reads the owner's draft. Either may have been
    edited or deleted since the suspension; the caller compares hashes.
    """
    workflow_id = str(record.get("workflow_id") or "")
    if record.get("origin") == "tool":
        from ..custom_tools import get_custom_tool_loader

        definition = get_custom_tool_loader().get_definition(workflow_id)
        if definition is None or definition.workflow_config is None:
            return None
        return definition.workflow_config, definition.parameters
    from ...tools.tool_create import _draft_store, _normalize_draft_id

    draft = _draft_store().get(
        str(record.get("owner_user_id") or ""), _normalize_draft_id(workflow_id)
    )
    if (
        draft is None
        or draft.implementation_type != "workflow"
        or draft.workflow_config is None
    ):
        return None
    return draft.workflow_config, draft.parameters


def _notify_owner(record: Dict[str, Any], summary: str) -> None:
    try:
        from ..notifications import create_notification

        create_notification(
            user_id=str(record.get("user_id") or ""),
            summary=summary[:200],
            thread_id=str(record.get("thread_id") or "") or None,
            task_id=None,
        )
    except Exception:  # noqa: BLE001 - announcements are best-effort
        logger.warning("workflow approval notification failed", exc_info=True)


def _publish_resolved_event(
    record: Dict[str, Any], *, approved: bool, note: Optional[str], run_id: str
) -> None:
    try:
        from ..event_bus import publish_autonomous_event

        publish_autonomous_event(
            event_type="workflow_approval_resolved",
            thread_id=str(record.get("thread_id") or ""),
            user_id=str(record.get("user_id") or ""),
            task_id="",
            data={
                "record_id": record.get("record_id", ""),
                "workflow_id": record.get("workflow_id", ""),
                "approved": approved,
                "note": note or "",
                "run_id": run_id,
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("workflow_approval_resolved event publish failed", exc_info=True)


async def resolve_approval_record(
    record: Dict[str, Any],
    *,
    approved: bool,
    resolved_by: str,
    note: Optional[str] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the continuation for a CLAIMED record and report the outcome.

    The caller must have claimed the record (``claim_approval``) and checked
    the actor may resolve it. Returns ``{record_id, decision, run_id,
    envelope}``; validation failures return a ``resume_invalid`` envelope and
    never execute. Always finishes the claim.
    """
    from .authoring import (
        budget_from_config,
        config_revision_hash,
        workflow_execution_gate,
    )

    record_id = str(record.get("record_id") or "")
    decision_label = "approved" if approved else "declined"
    run_id = run_id or uuid.uuid4().hex[:12]

    async def _invalid(reason: str) -> Dict[str, Any]:
        from .trace import StepTrace, persist_run_record

        envelope = error_envelope(
            WorkflowError(kind=KIND_RESUME_INVALID, message=reason), {}
        )
        # The resolve ack already handed out this run_id: persist a stepless
        # run record so the id resolves in the runs surfaces instead of
        # dangling (persist never raises; the write runs off-loop).
        await asyncio.to_thread(
            persist_run_record,
            StepTrace(
                run_id=str(run_id),
                workflow_id=str(record.get("workflow_id") or "adhoc"),
            ),
            envelope.to_dict(),
            user_id=str(record.get("user_id") or ""),
            thread_id=str(record.get("thread_id") or ""),
        )
        _notify_owner(
            record,
            f"Workflow '{record.get('workflow_id')}' approval could not resume: {reason}",
        )
        return {
            "record_id": record_id,
            "decision": decision_label,
            "run_id": run_id,
            "envelope": envelope.to_dict(),
        }

    try:
        target = _load_target(record)
        if target is None:
            return await _invalid("the workflow no longer exists")
        config, parameters = target
        current_hash = config_revision_hash(config, parameters)
        if current_hash != str(record.get("revision_hash") or ""):
            return await _invalid(
                "the workflow changed since this approval was requested; "
                "re-fire the workflow instead"
            )
        gate_error = workflow_execution_gate(config, parameters)
        if gate_error:
            return await _invalid(
                f"the workflow revision is no longer approved: {gate_error}"
            )
        resume_entrypoint = str(record.get("resume_entrypoint") or "")
        if resume_entrypoint not in (config.continuations or []):
            return await _invalid(
                f"continuation {resume_entrypoint!r} is no longer declared"
            )

        decision = {
            "approved": bool(approved),
            "note": note or "",
            "resolved_by": resolved_by,
            "resolved_at": _utc_now().isoformat(),
        }
        state = record.get("state") if isinstance(record.get("state"), dict) else {}

        from .executor import execute_workflow
        from .registry import ApprovalRuntime

        run = await execute_workflow(
            source=config.source_code,
            entrypoint=resume_entrypoint,
            params={"state": state, "decision": decision},
            user_id=str(record.get("user_id") or "default"),
            thread_id=str(record.get("thread_id") or ""),
            workflow_id=str(record.get("workflow_id") or "adhoc"),
            budget=budget_from_config(config),
            depth=int(record.get("depth") or 0),
            run_id=run_id,
            # A continuation may itself suspend (multi-step approvals chain
            # through named entrypoints).
            approval=ApprovalRuntime(
                origin=str(record.get("origin") or "tool"),
                revision_hash=current_hash,
                continuations=tuple(config.continuations or []),
                owner_user_id=str(record.get("owner_user_id") or ""),
            ),
        )
        envelope = run.envelope
        _notify_owner(
            record,
            f"Workflow '{record.get('workflow_id')}' {decision_label} resume "
            f"finished: {envelope.status}",
        )
        return {
            "record_id": record_id,
            "decision": decision_label,
            "run_id": run_id,
            "envelope": envelope.to_dict(),
        }
    finally:
        finish_claim(record_id)
        _publish_resolved_event(record, approved=approved, note=note, run_id=run_id)


async def sweep_expired_approvals() -> int:
    """Resolve every expired pending approval as declined (note ``expired``).

    Returns how many were resolved. Called from the API app's heartbeat; each
    expiry runs the continuation with ``approved: false`` exactly like an
    explicit decline (plan: expiry IS a decline).
    """
    resolved = 0
    for record in expired_approvals():
        claimed = claim_approval(str(record.get("record_id") or ""))
        if claimed is None:
            continue
        try:
            await resolve_approval_record(
                claimed, approved=False, resolved_by="system", note="expired"
            )
            resolved += 1
        except Exception:  # noqa: BLE001 - one bad record must not stop the sweep
            logger.warning(
                "expiry resolution failed for approval %s",
                record.get("record_id"),
                exc_info=True,
            )
    return resolved


__all__ = [
    "APPROVAL_SWEEP_INTERVAL_SECONDS",
    "DEFAULT_APPROVAL_EXPIRY_DAYS",
    "MAX_PENDING_PER_WORKFLOW",
    "claim_approval",
    "create_pending_approval",
    "delete_approval",
    "expired_approvals",
    "finish_claim",
    "list_pending_approvals",
    "load_approval",
    "pending_dir",
    "public_approval_entry",
    "resolve_approval_record",
    "sweep_expired_approvals",
]
