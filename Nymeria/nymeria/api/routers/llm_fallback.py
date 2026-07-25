"""REST endpoints for LLM fallback consent prompts.

Mounted at ``/llm``. A parked turn (the ``ask`` fallback switch mode, or an
``ask``-mode refusal swap) mints a durable record in
``core/fallback_approvals.py`` and waits; these endpoints are the list/resolve
surface, mirroring the hook-approvals router shape (owner-or-admin, 404 never
leaks another user's record, 409 for a hold that is no longer pending).
Resolution is deliberately human-only: there is no agent tool for it.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ...core.accounts import AuthenticatedUser

logger = logging.getLogger(__name__)


class FallbackApprovalResolveRequest(BaseModel):
    """Approve (swap, with an optional hold choice) or decline a parked switch."""

    approved: bool
    hold_seconds: Optional[int] = Field(
        default=None,
        ge=0,
        le=604800,
        description=(
            "Hold duration for an approved swap (seconds). Omitted = the "
            "global default hold. Ignored on decline."
        ),
    )
    hold_permanent: bool = Field(
        default=False,
        description="Hold the fallback until manually reverted (wins over hold_seconds).",
    )
    note: Optional[str] = Field(default=None, max_length=500)


def create_llm_fallback_router(verify_api_key_fn) -> APIRouter:
    router = APIRouter(prefix="/llm", tags=["LLM Fallback"])

    @router.get("/fallback-approvals")
    async def list_fallback_approvals(
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Pending fallback consent prompts (admins all, others own-only).

        Each entry is a parked turn asking whether to swap to the fallback
        model; resolve with ``POST /llm/fallback-approvals/{record_id}/resolve``
        before ``expires_at`` (an unanswered prompt auto-swaps on timeout).
        """
        from ...core.fallback_approvals import list_pending, public_entry

        records = await run_in_threadpool(
            list_pending, None if user.role == "admin" else user.id
        )
        return {"approvals": [public_entry(r) for r in records]}

    @router.post("/fallback-approvals/{record_id}/resolve")
    async def resolve_fallback_approval(
        record_id: str,
        body: FallbackApprovalResolveRequest,
        user: AuthenticatedUser = Depends(verify_api_key_fn),
    ):
        """Approve or decline a parked model switch (owner or admin).

        404 covers a missing record, another user's record, and the common
        already-ended cases: the parked waiter deletes its record on every
        exit, so a timed-out/resolved/aborted prompt is usually gone before
        a late resolve arrives. 409 is the rarer stale shape: the record
        still exists but no waiter is parked on it (typically a crash orphan
        surviving a restart); stale records are cleaned up on the spot.
        """
        from ...core.fallback_approvals import (
            delete_record,
            get_fallback_approval_coordinator,
            load_record,
            publish_resolved_event,
        )

        record = await run_in_threadpool(load_record, record_id)
        if record is None or (
            user.role != "admin" and record.get("user_id") != user.id
        ):
            raise HTTPException(status_code=404, detail="Fallback prompt not found")
        woke = get_fallback_approval_coordinator().resolve(
            record_id,
            approved=body.approved,
            resolved_by=user.id,
            hold_seconds=body.hold_seconds,
            hold_permanent=body.hold_permanent,
            note=body.note or "",
        )
        if not woke:
            await run_in_threadpool(delete_record, record_id)
            publish_resolved_event(record, outcome="stale", resolved_by=user.id)
            raise HTTPException(
                status_code=409,
                detail="This fallback prompt is no longer pending (it timed "
                "out and auto-swapped, was resolved elsewhere, or its turn "
                "ended).",
            )
        return {
            "record_id": record_id,
            "outcome": "approved" if body.approved else "declined",
        }

    return router
