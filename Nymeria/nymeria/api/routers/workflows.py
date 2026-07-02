"""Workflow approval, runtime-suspension, run-record, and execute routes.

Two distinct approval flows live here, both REST-only (no agent tool can
resolve either, so a prompt-injected turn cannot satisfy them):

- REVISION approvals (the authoring gate, admin-only): admins list pending
  workflow revisions, review their source, and approve or decline them. The
  shared state transitions live in ``tools/tool_create.py``
  (``list_pending_workflows`` / ``resolve_workflow_approval``) so this router
  and the ``workflow_info`` tool cannot drift.
- RUNTIME approvals (``nym.approve`` suspensions, owner-or-admin): the run's
  owner decides whether the suspended action proceeds, following the
  credential-prompt ownership shape. Resolution claims the durable record and
  resumes the declared continuation in a fresh subprocess
  (``core/workflows/approvals.py``).

Run records (``GET /workflows/runs/{workflow_id}``) are per-user: admins see
every run, other callers only their own. ``POST /{workflow_id}/execute`` is
the headless execution surface (and the worker's relay target for scheduled
workflow TODOs); it re-gates the revision approval like every other path.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from typing import Any, List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...core.accounts import AuthenticatedUser

logger = logging.getLogger(__name__)

# Keep strong references to in-flight resume tasks (a bare create_task result
# may be garbage-collected mid-run).
_RESUME_TASKS: Set["asyncio.Task"] = set()


class WorkflowApprovalTarget(BaseModel):
    """One pending revision to resolve."""

    kind: str = Field(..., description="'draft' or 'tool'")
    id: str = Field(..., min_length=1, description="Draft id or tool id")
    owner_user_id: str = Field(
        default="",
        description="Draft owner's user id (required for kind='draft')",
    )
    note: Optional[str] = Field(
        default=None, max_length=500, description="Optional note for the author"
    )
    revision: Optional[str] = Field(
        default=None,
        description=(
            "Content hash the admin reviewed (from /workflows/pending or "
            "/workflows/source); the resolution is refused with 409 if the "
            "content has changed since"
        ),
    )


class WorkflowApprovalAck(BaseModel):
    ok: bool
    decision: str
    kind: str
    id: str


class WorkflowApprovalResolveRequest(BaseModel):
    """Resolve one nym.approve suspension."""

    approved: bool
    note: Optional[str] = Field(
        default=None, max_length=500, description="Passed to the continuation"
    )


class WorkflowExecuteRequest(BaseModel):
    """Run one published workflow tool directly."""

    params: dict = Field(default_factory=dict)
    thread_id: Optional[str] = Field(
        default=None, description="Thread context for the run (credentials, trace)"
    )


def create_workflows_router(
    require_admin_user: Callable[..., Any],
    verify_api_key: Callable[..., Any],
) -> APIRouter:
    """Create the workflows router with app dependencies injected."""
    router = APIRouter(prefix="/workflows", tags=["Workflows"])

    @router.get("/pending")
    async def pending_workflows(
        user: AuthenticatedUser = Depends(require_admin_user),
    ) -> dict:
        """List workflow revisions awaiting approval (drafts and tools)."""
        from ...tools.tool_create import list_pending_workflows

        pending = list_pending_workflows()
        return {"pending": pending, "total": len(pending)}

    @router.get("/source")
    async def workflow_source(
        kind: str = Query(..., description="'draft' or 'tool'"),
        id: str = Query(..., min_length=1),
        owner_user_id: str = Query(default=""),
        user: AuthenticatedUser = Depends(require_admin_user),
    ) -> dict:
        """One target's source and metadata (the admin review view)."""
        from ...tools.tool_create import get_workflow_source

        if kind not in ("draft", "tool"):
            raise HTTPException(status_code=400, detail="kind must be 'draft' or 'tool'")
        entry = get_workflow_source(kind, owner_user_id, id)
        if entry is None:
            raise HTTPException(status_code=404, detail="Workflow target not found")
        return entry

    def _resolve(target: WorkflowApprovalTarget, approve: bool, actor: str) -> WorkflowApprovalAck:
        from ...tools.tool_create import (
            WorkflowRevisionMismatch,
            resolve_workflow_approval,
        )

        if target.kind not in ("draft", "tool"):
            raise HTTPException(status_code=400, detail="kind must be 'draft' or 'tool'")
        if target.kind == "draft" and not target.owner_user_id:
            raise HTTPException(
                status_code=400, detail="owner_user_id is required for kind='draft'"
            )
        try:
            result = resolve_workflow_approval(
                kind=target.kind,
                owner_user_id=target.owner_user_id,
                target_id=target.id,
                approve=approve,
                actor_user_id=actor,
                note=target.note,
                expected_revision=target.revision,
            )
        except WorkflowRevisionMismatch as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return WorkflowApprovalAck(
            ok=True,
            decision=result["decision"],
            kind=result["kind"],
            id=result["id"],
        )

    @router.post("/approve", response_model=WorkflowApprovalAck)
    async def approve_workflow(
        target: WorkflowApprovalTarget,
        user: AuthenticatedUser = Depends(require_admin_user),
    ) -> WorkflowApprovalAck:
        """Approve one workflow revision (pins the content hash)."""
        return _resolve(target, True, user.id)

    @router.post("/decline", response_model=WorkflowApprovalAck)
    async def decline_workflow(
        target: WorkflowApprovalTarget,
        user: AuthenticatedUser = Depends(require_admin_user),
    ) -> WorkflowApprovalAck:
        """Decline one workflow revision (the author is notified)."""
        return _resolve(target, False, user.id)

    @router.get("/approvals")
    async def pending_runtime_approvals(
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> dict:
        """Pending nym.approve suspensions; non-admins see only their own."""
        from ...core.workflows.approvals import (
            list_pending_approvals,
            public_approval_entry,
        )

        records = list_pending_approvals(
            user_id=None if user.role == "admin" else user.id
        )
        entries = [public_approval_entry(record) for record in records]
        return {"approvals": entries, "total": len(entries)}

    @router.post("/approvals/{record_id}/resolve")
    async def resolve_runtime_approval(
        record_id: str,
        body: WorkflowApprovalResolveRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> dict:
        """Resolve one suspension (owner or admin) and run its continuation.

        The continuation runs in the background (it is a full workflow run,
        up to the wall-clock cap); the ack carries the run id so the outcome
        can be read from ``GET /workflows/runs/{workflow_id}``. A declined
        resolution ALSO runs the continuation, with ``approved: false``.
        """
        from ...core.workflows.approvals import (
            claim_approval,
            load_approval,
            resolve_approval_record,
        )

        record = load_approval(record_id)
        if record is None or (
            user.role != "admin" and record.get("user_id") != user.id
        ):
            # 404 for missing AND not-yours: do not leak existence.
            raise HTTPException(status_code=404, detail="Approval request not found")
        claimed = claim_approval(record_id)
        if claimed is None:
            raise HTTPException(
                status_code=409, detail="Approval is already being resolved"
            )
        run_id = uuid.uuid4().hex[:12]
        decision = "approved" if body.approved else "declined"

        async def _resolve() -> None:
            try:
                await resolve_approval_record(
                    claimed,
                    approved=body.approved,
                    resolved_by=user.id,
                    note=body.note,
                    run_id=run_id,
                )
            except Exception:  # noqa: BLE001 - background task, log and move on
                logger.warning(
                    "workflow approval resolution failed for %s",
                    record_id,
                    exc_info=True,
                )

        task = asyncio.create_task(_resolve())
        _RESUME_TASKS.add(task)
        task.add_done_callback(_RESUME_TASKS.discard)
        return {
            "ok": True,
            "record_id": record_id,
            "decision": decision,
            "run_id": run_id,
            "workflow_id": str(claimed.get("workflow_id") or ""),
        }

    @router.get("/runs")
    async def recent_workflow_runs(
        limit: int = Query(default=20, ge=1, le=100),
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> dict:
        """Recent runs across ALL workflows (the dashboard feed shape),
        newest first; non-admins see only their own."""
        from ...core.workflows.trace import read_recent_run_records

        records: List[dict] = await asyncio.to_thread(
            read_recent_run_records,
            limit=limit,
            user_id=None if user.role == "admin" else user.id,
        )
        return {"runs": records, "total": len(records)}

    @router.get("/runs/{workflow_id}")
    async def workflow_runs(
        workflow_id: str,
        limit: int = Query(default=20, ge=1, le=100),
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> dict:
        """Recent run records for a workflow; non-admins see only their own."""
        from ...core.workflows.trace import read_run_records

        records: List[dict] = await asyncio.to_thread(
            read_run_records,
            workflow_id,
            limit=limit,
            user_id=None if user.role == "admin" else user.id,
        )
        return {"workflow_id": workflow_id, "runs": records, "total": len(records)}

    @router.post("/{workflow_id}/execute")
    async def execute_workflow_endpoint(
        workflow_id: str,
        body: WorkflowExecuteRequest,
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> dict:
        """Run one published workflow tool as the caller; returns the envelope.

        The headless execution surface: the worker relays scheduled workflow
        TODOs here (service token + Act-As the owner), and frontends can run
        a workflow directly. Blocks until the run finishes (bounded by the
        workflow's wall-clock cap). Re-gated at call time; never delivers
        output anywhere itself (delivery is the workflow's own job).
        """
        from ...core.custom_tools import get_custom_tool_loader
        from ...core.workflows.tool_runtime import run_workflow_by_id

        refusal, run = await run_workflow_by_id(
            get_custom_tool_loader(),
            workflow_id,
            dict(body.params or {}),
            user_id=user.id,
            thread_id=body.thread_id or "",
        )
        if refusal is not None:
            status = 404 if "was not found" in refusal else 403
            raise HTTPException(status_code=status, detail=refusal)
        assert run is not None  # exactly one of (refusal, run) is None
        return {
            "workflow_id": workflow_id,
            "run_id": run.trace.run_id,
            "envelope": run.envelope.to_dict(),
        }

    return router
