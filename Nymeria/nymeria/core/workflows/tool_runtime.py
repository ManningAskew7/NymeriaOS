"""Execution glue for the ``workflow`` custom-tool type.

``CustomToolLoader._create_workflow_tool`` binds a saved workflow definition
as a LangChain tool; this module is the coroutine behind it. Responsibilities:

- Re-read the definition from the loader at CALL time (never the bound
  closure), so an edit or a revoked approval takes effect immediately (the
  run_command execution-time re-gate precedent).
- Apply the per-revision approval gate (``authoring.workflow_execution_gate``),
  fail closed.
- Resolve ``user_id``/``thread_id`` from the standard ``RunnableConfig``
  configurable (the bash.py injection pattern; custom tools historically ran
  context-free, workflows cannot).
- Enforce nesting depth: a workflow dispatched from inside another workflow
  (``nym.tools.<name>``) carries ``workflow_depth`` in its configurable;
  beyond ``budget.max_depth`` the call is refused BEFORE spawning, which is
  what makes workflow-calls-workflow recursion bounded.
- Map the result envelope to agent-facing text: plain output on success,
  ``[Error]:`` taxonomy plus the author traceback on failure (the plan's
  self-repair affordance), always with a compact budget stats line.

Turn abort is NOT handled here: ``execute_workflow`` kills the child process
group and re-raises ``CancelledError``, which must keep propagating.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional, Tuple

from .authoring import (
    budget_from_config,
    config_revision_hash,
    workflow_execution_gate,
)
from .envelope import STATUS_NEEDS_APPROVAL, STATUS_OK, WorkflowEnvelope

logger = logging.getLogger(__name__)


def format_envelope_for_agent(envelope: WorkflowEnvelope) -> str:
    """Envelope to tool-result text (output, or taxonomy + traceback)."""
    budget = envelope.budget or {}
    stats = (
        f"[workflow: {budget.get('calls_used', 0)} calls, "
        f"{budget.get('ai_calls_used', 0)} ai, "
        f"{float(budget.get('wall_seconds', 0.0) or 0.0):.1f}s]"
    )
    if envelope.status == STATUS_OK:
        output = envelope.output
        if isinstance(output, (dict, list)):
            text = json.dumps(output, indent=2, default=str)
        else:
            text = str(output) if output is not None else ""
        return f"{text}\n\n{stats}" if text else stats
    if envelope.status == STATUS_NEEDS_APPROVAL:
        details = envelope.output if isinstance(envelope.output, dict) else {}
        record_id = details.get("record_id") or envelope.resume_token or ""
        prompt = str(details.get("prompt") or "")[:200]
        expires = details.get("expires_at") or ""
        text = f"[Workflow suspended] approval requested (id: {record_id})"
        if prompt:
            text += f": {prompt}"
        text += (
            ". The owner has been notified and the run resumes automatically "
            "once resolved"
        )
        if expires:
            text += f" (expires {expires})"
        return f"{text}. {stats}"
    error = envelope.error
    kind = error.kind if error else "runner_error"
    message = error.message if error else "workflow failed"
    text = f"[Error]: workflow {envelope.status} ({kind}) - {message}"
    if error is not None and error.step is not None:
        verb = f", verb {error.verb}" if error.verb else ""
        text += f" (step {error.step}{verb})"
    if error is not None and error.traceback:
        # Author bugs return the traceback so the authoring agent can self-repair.
        text += f"\n{error.traceback}"
    return f"{text}\n{stats}"


async def run_workflow_by_id(
    loader: Any,
    tool_id: str,
    params: dict,
    *,
    user_id: str,
    thread_id: str,
    depth: int = 0,
) -> Tuple[Optional[str], Optional[Any]]:
    """Gate and run one saved workflow; ``(refusal, result)``.

    The single by-id execution path shared by the bound tool coroutine, the
    REST execute endpoint, trigger fires, and scheduled TODOs. Exactly one
    side of the tuple is set: a human-readable refusal (missing definition,
    approval gate, nesting depth), or the ``WorkflowRunResult``.
    """
    definition = loader.get_definition(tool_id)
    if definition is None or definition.workflow_config is None:
        return (
            f"workflow definition {tool_id!r} was not found (deleted or disabled?)",
            None,
        )
    workflow_config = definition.workflow_config

    gate_error = workflow_execution_gate(workflow_config, definition.parameters)
    if gate_error:
        return f"approval_required - {gate_error}", None

    budget = budget_from_config(workflow_config)
    if depth > budget.max_depth:
        return (
            "workflow error (budget_exceeded) - workflow nesting depth "
            f"{depth} exceeds max_depth {budget.max_depth}",
            None,
        )

    from .executor import execute_workflow
    from .registry import ApprovalRuntime

    run = await execute_workflow(
        source=workflow_config.source_code,
        entrypoint=workflow_config.entrypoint,
        params=dict(params or {}),
        user_id=user_id,
        thread_id=thread_id,
        workflow_id=tool_id,
        budget=budget,
        depth=depth,
        approval=ApprovalRuntime(
            origin="tool",
            revision_hash=config_revision_hash(
                workflow_config, definition.parameters
            ),
            continuations=tuple(workflow_config.continuations or []),
        ),
    )
    return None, run


def workflow_declares_event(workflow_id: str) -> bool:
    """Whether the published workflow's signature declares an ``event`` param."""
    from ..custom_tools import get_custom_tool_loader

    definition = get_custom_tool_loader().get_definition(workflow_id)
    return bool(
        definition is not None
        and definition.implementation_type == "workflow"
        and "event" in (definition.parameters or {})
    )


def workflow_binding_error(
    workflow_id: str,
    params: Mapping[str, Any],
    *,
    allow_event: bool = False,
) -> Optional[str]:
    """Bind-time validation for headless firing (triggers, scheduled TODOs).

    Checks the workflow exists as a published workflow tool, its current
    revision is approved, the bound ``params`` are all declared, and every
    required parameter is covered by the binding (plus ``event`` when the
    firing surface supplies one and the signature declares it). Fire time
    re-gates regardless; this catches misconfiguration where it is authored.
    Returns a human-readable error, or None when the binding is sound.
    """
    from ..custom_tools import get_custom_tool_loader

    definition = get_custom_tool_loader().get_definition(workflow_id)
    if (
        definition is None
        or definition.implementation_type != "workflow"
        or definition.workflow_config is None
    ):
        return f"no published workflow tool named {workflow_id!r}"
    gate_error = workflow_execution_gate(
        definition.workflow_config, definition.parameters
    )
    if gate_error:
        return gate_error
    declared = definition.parameters or {}
    unknown = [name for name in params if name not in declared]
    if unknown:
        return (
            f"unknown parameter(s) for workflow {workflow_id!r}: "
            f"{', '.join(sorted(unknown))}"
        )
    covered = set(params)
    if allow_event and "event" in declared:
        covered.add("event")
    missing = [
        name
        for name, parameter in declared.items()
        if parameter.required and name not in covered
    ]
    if missing:
        return (
            f"workflow {workflow_id!r} requires parameter(s) the binding does "
            f"not cover: {', '.join(sorted(missing))}"
        )
    return None


async def run_workflow_tool(
    loader: Any,
    tool_id: str,
    params: dict,
    config: Optional[Mapping[str, Any]],
) -> str:
    """One workflow-tool invocation: re-gate, budget, execute, format."""
    configurable = (config or {}).get("configurable") or {}
    user_id = str(configurable.get("user_id") or "default")
    thread_id = str(configurable.get("thread_id") or "")
    try:
        depth = int(configurable.get("workflow_depth") or 0)
    except (TypeError, ValueError):
        depth = 0

    refusal, run = await run_workflow_by_id(
        loader,
        tool_id,
        params,
        user_id=user_id,
        thread_id=thread_id,
        depth=depth,
    )
    if refusal is not None:
        return f"[Error]: {refusal}"
    assert run is not None  # exactly one of (refusal, run) is None
    return format_envelope_for_agent(run.envelope)


__all__ = [
    "format_envelope_for_agent",
    "run_workflow_by_id",
    "run_workflow_tool",
    "workflow_binding_error",
    "workflow_declares_event",
]
