"""Workflow approval and run-record routes.

The resolution half of the per-revision authoring gate (plan:
docs/private/plans/workflow-tools.md "Authoring gate"): admins list pending
workflow revisions, review their source, and approve or decline them.
Resolution is deliberately REST-only (no agent tool can approve), so a
prompt-injected turn cannot satisfy the gate; the shared state transitions
live in ``tools/tool_create.py`` (``list_pending_workflows`` /
``resolve_workflow_approval``) so this router and the ``workflow_info`` tool
cannot drift.

Run records (``GET /workflows/runs/{workflow_id}``) are per-user: admins see
every run, other callers only their own.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...core.accounts import AuthenticatedUser


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

    @router.get("/runs/{workflow_id}")
    async def workflow_runs(
        workflow_id: str,
        limit: int = Query(default=20, ge=1, le=100),
        user: AuthenticatedUser = Depends(verify_api_key),
    ) -> dict:
        """Recent run records for a workflow; non-admins see only their own."""
        from ...core.workflows.trace import read_run_records

        records: List[dict] = read_run_records(
            workflow_id,
            limit=limit,
            user_id=None if user.role == "admin" else user.id,
        )
        return {"workflow_id": workflow_id, "runs": records, "total": len(records)}

    return router
