"""Read-only workflow visibility tool (the ``hook_info`` idiom).

``workflow_info`` surfaces the workflow custom-tool lifecycle: published
workflow tools and the caller's drafts with their approval states, one
workflow's detail (including source), recent run records (the step trace the
engine persists per run), and, for admins, the pending-approval queue.

Deliberately NOT here: approve/decline. Approval resolution is human-only
(REST ``POST /workflows/approve|decline``), so a prompt-injected agent turn
can never satisfy the authoring gate on the user's behalf.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import InjectedToolArg, tool

from .utils import get_user_id, is_admin

logger = logging.getLogger(__name__)

_SOURCE_PREVIEW_CAP = 6000


def _workflow_drafts_for(user_id: str) -> list[Any]:
    from .tool_create import _draft_store

    return [
        draft
        for draft in _draft_store().list(user_id)
        if draft.implementation_type == "workflow"
    ]


def _render_summary(summary: dict[str, Any], *, kind: str) -> str:
    approval = summary.get("approval") or "n/a"
    revision = summary.get("revision") or ""
    parts = [
        f"- [{kind}] {summary.get('tool_id') or summary.get('draft_id')}",
        f"approval={approval}",
    ]
    if revision:
        parts.append(f"revision={revision}")
    params = summary.get("parameter_names") or []
    if params:
        parts.append(f"params={','.join(params)}")
    return " ".join(parts)


def _list(user_id: str, admin: bool) -> str:
    from .tool_create import _iter_workflow_definitions, _published_summary

    lines: list[str] = []
    published = [_published_summary(d) for d in _iter_workflow_definitions()]
    if published:
        lines.append(f"{len(published)} published workflow tool(s):")
        lines.extend(_render_summary(s, kind="tool") for s in published)
    drafts = [d.public_summary() for d in _workflow_drafts_for(user_id)]
    if drafts:
        lines.append(f"{len(drafts)} workflow draft(s) of yours:")
        lines.extend(_render_summary(s, kind="draft") for s in drafts)
    if admin:
        from .tool_create import list_pending_workflows

        pending = list_pending_workflows()
        if pending:
            lines.append(
                f"{len(pending)} revision(s) pending approval "
                "(action='pending' for detail)."
            )
    if not lines:
        return "[Info]: No workflow tools or drafts exist yet."
    return "\n".join(lines)


def _show(user_id: str, admin: bool, workflow_id: str) -> str:
    from .tool_create import get_workflow_source

    entry = get_workflow_source("tool", "", workflow_id)
    if entry is None:
        # Fall back to the caller's own draft (admins may inspect any draft
        # via the pending queue's owner id, but the common case is your own).
        entry = get_workflow_source("draft", user_id, workflow_id)
    if entry is None:
        return f"[Error]: no workflow tool or draft found with id '{workflow_id}'."
    if entry["kind"] == "draft" and not admin and entry.get("owner_user_id") != user_id:
        return f"[Error]: no workflow tool or draft found with id '{workflow_id}'."
    source = str(entry.get("source_code") or "")
    truncated = ""
    if len(source) > _SOURCE_PREVIEW_CAP:
        source = source[:_SOURCE_PREVIEW_CAP]
        truncated = "\n[...source truncated...]"
    lines = [
        f"Workflow {entry.get('tool_id')} ({entry['kind']}), "
        f"approval={entry.get('approval')}, revision={str(entry.get('revision'))[:12]}",
        f"author={entry.get('author')}, entrypoint={entry.get('entrypoint')}, "
        f"continuations={entry.get('continuations') or []}",
        f"parameters={entry.get('parameter_names') or []}",
        "source:",
        source + truncated,
    ]
    return "\n".join(lines)


def _log(user_id: str, admin: bool, workflow_id: str, limit: int) -> str:
    from ..core.workflows.trace import read_run_records

    records = read_run_records(
        workflow_id,
        limit=max(1, limit),
        user_id=None if admin else user_id,
    )
    if not records:
        return f"[Info]: no run records for workflow '{workflow_id}'."
    lines = [f"{len(records)} run(s) of {workflow_id}, newest first:"]
    for record in records:
        envelope = record.get("envelope") or {}
        budget = envelope.get("budget") or {}
        steps = (record.get("trace") or {}).get("steps") or []
        error = envelope.get("error") or {}
        detail = f" error={error.get('kind')}: {error.get('message')}" if error else ""
        lines.append(
            f"- {record.get('timestamp', '')} run={record.get('run_id')} "
            f"status={record.get('status')} steps={len(steps)} "
            f"calls={budget.get('calls_used', 0)} "
            f"wall={float(budget.get('wall_seconds', 0.0) or 0.0):.1f}s{detail}"
        )
    return "\n".join(lines)


def _pending() -> str:
    from .tool_create import list_pending_workflows

    pending = list_pending_workflows()
    if not pending:
        return "[Info]: no workflow revisions are awaiting approval."
    lines = [f"{len(pending)} workflow revision(s) awaiting approval:"]
    for entry in pending:
        lines.append(
            f"- [{entry['kind']}] {entry['tool_id']} by {entry['author']} "
            f"(owner={entry['owner_user_id']}, id={entry['id']}, "
            f"revision={str(entry['revision'])[:12]}, updated={entry['updated_at']})"
        )
    lines.append(
        "Resolve via REST: POST /workflows/approve or /workflows/decline with "
        '{"kind", "owner_user_id", "id"}. Approval is human-only by design.'
    )
    return "\n".join(lines)


@tool
def workflow_info(
    action: str = "list",
    workflow_id: Optional[str] = None,
    limit: int = 20,
    *,
    config: Annotated[RunnableConfig, InjectedToolArg],
) -> str:
    """Inspect nym-SDK workflow tools: definitions, approvals, and run logs.

    Actions: "list" for published workflow tools plus your drafts with their
    approval states, "show" for one workflow's detail including its source,
    "log" for its recent run records (status, steps, budget consumption, and
    the failing error if any), and "pending" (admin only) for the revisions
    awaiting approval. Approving or declining happens over REST
    (/workflows/approve, /workflows/decline), never through an agent tool.

    Args:
        action: "list", "show", "log", or "pending".
        workflow_id: Required for show/log (tool id, or your draft id).
        limit: Max run records for log (default 20).
    """
    user_id = get_user_id(config)
    admin = is_admin(user_id)
    action_key = (action or "list").strip().lower()

    try:
        if action_key == "list":
            return _list(user_id, admin)
        if action_key == "show":
            if not workflow_id:
                return "[Error]: show requires workflow_id."
            return _show(user_id, admin, workflow_id)
        if action_key == "log":
            if not workflow_id:
                return "[Error]: log requires workflow_id."
            return _log(user_id, admin, workflow_id, limit)
        if action_key == "pending":
            if not admin:
                return "[Error]: the pending approval queue is admin-only."
            return _pending()
        return "[Error]: action must be one of: list, show, log, pending."
    except Exception as exc:  # noqa: BLE001 - a read surface must not crash a turn
        logger.error("workflow_info failed", exc_info=True)
        return f"[Error]: workflow_info failed: {exc}"


# Grouped export for CATALOG_TOOLS registration (opt-in).
WORKFLOW_INFO_TOOLS = [workflow_info]
