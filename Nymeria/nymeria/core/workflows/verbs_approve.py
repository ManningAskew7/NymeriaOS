"""The ``nym.approve`` verb: the single suspension point.

``nym.approve(prompt, state, resume="continuation_name")`` does NOT return to
the author's code: the parent mints a durable pending record (never the
child; the executor verifies the child's ``needs_approval`` finish against
the parent-minted token), the child SDK raises a suspend signal, and the run
ends with a ``needs_approval`` envelope. Resolution later spawns a fresh
subprocess into the declared continuation (see ``approvals.py``).

Guardrails enforced here, all author-facing errors:

- Only runs of a SAVED workflow (published tool or draft) may suspend; an
  adhoc ``execute_workflow`` call has nothing to re-validate at resume time.
- ``resume`` must be one of the definition's declared continuations (they
  are statically validated and part of the approved revision hash).
- ``state`` must be a JSON object within ``budget.state_cap_bytes``
  (``state_too_large``; rejected at suspend time, never silently truncated).
- One suspension per run: a second call is a ``verb_error``.
"""

from __future__ import annotations

import asyncio
import json
import logging

from .envelope import KIND_STATE_TOO_LARGE
from .registry import VerbContext, VerbError, register_verb

logger = logging.getLogger(__name__)


def _announce_request(record: dict) -> None:
    """Notify the run's owner and emit the ``workflow_approval`` SSE event."""
    try:
        from ..notifications import create_notification

        prompt_preview = str(record.get("prompt") or "")[:120]
        create_notification(
            user_id=str(record.get("user_id") or ""),
            summary=(
                f"Workflow '{record.get('workflow_id')}' awaits your approval: "
                f"{prompt_preview}"
            )[:200],
            thread_id=str(record.get("thread_id") or "") or None,
            task_id=None,
        )
    except Exception:  # noqa: BLE001 - announcements are best-effort
        logger.warning("workflow approval announce failed", exc_info=True)
    try:
        from ..event_bus import publish_autonomous_event

        publish_autonomous_event(
            event_type="workflow_approval",
            thread_id=str(record.get("thread_id") or ""),
            user_id=str(record.get("user_id") or ""),
            task_id="",
            data={
                "record_id": record.get("record_id", ""),
                "workflow_id": record.get("workflow_id", ""),
                "prompt": str(record.get("prompt") or "")[:500],
                "expires_at": record.get("expires_at", ""),
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug("workflow_approval event publish failed", exc_info=True)


@register_verb(
    "approve",
    side_effect=True,
    positional=("prompt",),
    description=(
        "Suspend the run for human approval; resumes into the named "
        "continuation with (state, decision)"
    ),
)
async def _approve_verb(ctx: VerbContext, verb: str, args: dict):
    approval = ctx.approval
    if approval is None:
        raise VerbError(
            "nym.approve requires a saved workflow (published tool or draft); "
            "adhoc runs cannot be resumed"
        )
    if approval.record_id:
        raise VerbError(
            "this run already requested an approval; one suspension per run"
        )
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise VerbError("nym.approve needs a non-empty prompt for the human")
    resume = str(args.get("resume") or "").strip()
    if resume not in approval.continuations:
        declared = ", ".join(approval.continuations) or "none declared"
        raise VerbError(
            f"resume={resume!r} is not a declared continuation ({declared}); "
            "declare it in the workflow's continuations list"
        )
    state = args.get("state")
    if state is None:
        state = {}
    if not isinstance(state, dict):
        raise VerbError("state must be a JSON object (dict)")
    try:
        serialized = json.dumps(state)
    except (TypeError, ValueError) as exc:
        raise VerbError(f"state must be JSON-serializable: {exc}") from exc
    cap = ctx.budget.state_cap_bytes
    if len(serialized.encode("utf-8")) > cap:
        raise VerbError(
            f"state is larger than the {cap} byte cap; persist bulky data "
            "elsewhere and keep ids in state",
            kind=KIND_STATE_TOO_LARGE,
        )

    from .approvals import create_pending_approval

    try:
        record = await asyncio.to_thread(
            create_pending_approval,
            run_id=ctx.run_id,
            workflow_id=ctx.workflow_id,
            origin=approval.origin,
            owner_user_id=approval.owner_user_id or ctx.user_id,
            user_id=ctx.user_id,
            thread_id=ctx.thread_id,
            revision_hash=approval.revision_hash,
            resume_entrypoint=resume,
            state=state,
            prompt=prompt,
            depth=ctx.depth,
        )
    except ValueError as exc:
        raise VerbError(str(exc)) from exc

    approval.record_id = str(record["record_id"])
    approval.token = str(record["resume_token"])
    approval.prompt = prompt
    approval.expires_at = str(record.get("expires_at") or "")
    await asyncio.to_thread(_announce_request, record)

    # The child SDK raises its suspend signal when this value returns.
    return {"resume_token": approval.token, "record_id": approval.record_id}
